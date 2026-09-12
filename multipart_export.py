"""Source-format-independent multipart GEM2 model export.

The exporter works on Blender mesh objects after import. It does not retarget an
arbitrary source rig; existing vertex groups must already name bones on the bound
armature. Final encoded records are partitioned without changing source geometry.
"""

from contextlib import contextmanager
import heapq
import json
import os
import re
import shutil
import tempfile
import uuid

import bpy
from mathutils import Matrix

from .i18n import _
from .texture_export import resolve_material_image

from .core import (
    D3DFVF_DIFFUSE,
    D3DFVF_LASTBETA_UBYTE4,
    D3DFVF_NORMAL,
    D3DFVF_TEX1,
    D3DFVF_XYZ,
    D3DFVF_XYZB2,
    MESH_FLAG_LIGHT,
    MESH_FLAG_MATERIAL,
    MESH_FLAG_SKINNED,
    MESH_FLAG_SUBSKIN,
    pack_B,
    pack_BBBB,
    pack_f,
    pack_ff,
    pack_fff,
    pack_H,
    pack_HHH,
    pack_I,
)


GAME_VERTEX_LIMIT = 0xFFFF
_TEMP_TOKEN_KEY = "gem2_multipart_export_token"


def _mode_set(mode):
    return bpy.ops.object.mode_set(mode=mode)


def _ascii_component(value, fallback, max_length=120):
    value = str(value or "")
    value = value.encode("ascii", errors="ignore").decode("ascii")
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    value = value or fallback
    return value[:max_length].rstrip("._") or fallback


def _material_key(material):
    try:
        return material.as_pointer()
    except (AttributeError, RuntimeError):
        return id(material)


def collect_mesh_objects(context, use_selection=True):
    if use_selection:
        meshes = [obj for obj in context.selected_objects if obj.type == "MESH"]
    else:
        meshes = [obj for obj in context.scene.objects if obj.type == "MESH"]
    meshes = sorted(set(meshes), key=lambda obj: obj.name.casefold())
    if not meshes:
        raise RuntimeError("Select at least one mesh object to export")
    return meshes


def _bound_armatures(mesh_obj):
    result = set()
    if mesh_obj.parent and mesh_obj.parent.type == "ARMATURE":
        result.add(mesh_obj.parent)
    for modifier in mesh_obj.modifiers:
        if modifier.type == "ARMATURE" and modifier.object:
            result.add(modifier.object)
    return result


def resolve_shared_armature(mesh_objects):
    armatures = set()
    unbound = []
    for mesh_obj in mesh_objects:
        bound = _bound_armatures(mesh_obj)
        if len(bound) > 1:
            raise RuntimeError(
                "Mesh %r is bound to more than one armature" % mesh_obj.name)
        if bound:
            armatures.update(bound)
        else:
            unbound.append(mesh_obj.name)
    if len(armatures) > 1:
        names = ", ".join(sorted(arm.name for arm in armatures))
        raise RuntimeError(
            "Selected meshes must share one armature; found: " + names)
    if armatures and unbound:
        raise RuntimeError(
            "Do not mix armature-bound and unbound meshes in one export: "
            + ", ".join(sorted(unbound, key=str.casefold)))
    return next(iter(armatures), None)


def _copy_export_material(original, entity_name, ordinal, token):
    base = _ascii_component(
        original.name if original else "default", "material", max_length=72)
    name = "%s_mat_%03d_%s" % (entity_name, ordinal, base)
    material = original.copy() if original else bpy.data.materials.new(name)
    material.name = name
    # Keep the source semantic name available after the temporary export copy
    # is prefixed. Alpha classification must still recognize names such as
    # Japanese ``目`` and original PMX clothing labels.
    if original and not material.get("mowas2_material_semantic_name"):
        material["mowas2_material_semantic_name"] = original.name
    material[_TEMP_TOKEN_KEY] = token
    return material


def _normalize_vertex_groups(mesh_obj, arm_obj, preferred_order=None):
    weighted_names = set()
    for vertex in mesh_obj.data.vertices:
        for element in vertex.groups:
            if element.weight > 1e-6 and element.group < len(mesh_obj.vertex_groups):
                weighted_names.add(mesh_obj.vertex_groups[element.group].name)

    if arm_obj is None:
        return []

    bone_names = [bone.name for bone in arm_obj.data.bones]
    unknown = sorted(weighted_names - set(bone_names), key=str.casefold)
    if unknown:
        preview = ", ".join(unknown[:12])
        if len(unknown) > 12:
            preview += ", ..."
        raise RuntimeError(
            "Weighted vertex groups do not exist on the bound armature: " + preview)

    configured = list(preferred_order or ())
    ordered_names = []
    for name in configured + bone_names:
        if name in weighted_names and name in bone_names and name not in ordered_names:
            ordered_names.append(name)
    if len(ordered_names) > 254:
        raise RuntimeError(
            "GEM2 PLY supports at most 254 weighted bone groups; found %d"
            % len(ordered_names))

    weights = []
    for vertex in mesh_obj.data.vertices:
        per_vertex = []
        for element in vertex.groups:
            if element.weight <= 1e-6 or element.group >= len(mesh_obj.vertex_groups):
                continue
            name = mesh_obj.vertex_groups[element.group].name
            if name in weighted_names:
                per_vertex.append((name, float(element.weight)))
        weights.append(per_vertex)

    for group in list(mesh_obj.vertex_groups)[::-1]:
        mesh_obj.vertex_groups.remove(group)
    rebuilt = {name: mesh_obj.vertex_groups.new(name=name) for name in ordered_names}
    for vertex_index, entries in enumerate(weights):
        for name, weight in entries:
            rebuilt[name].add([vertex_index], weight, "REPLACE")
    return ordered_names


def _skin_order_hint(mesh_obj):
    value = mesh_obj.get('gem2_skin_order')
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, (list, tuple)):
        return None
    result = []
    for name in value:
        name = str(name)
        if name not in result:
            result.append(name)
    return result or None


@contextmanager
def temporary_merged_model(context, source_meshes, entity_name):
    token = uuid.uuid4().hex
    original_active = context.view_layer.objects.active
    original_selected = [obj for obj in context.selected_objects]
    original_mode = original_active.mode if original_active else "OBJECT"
    arm_obj = resolve_shared_armature(source_meshes)
    armature_inverse = arm_obj.matrix_world.inverted() if arm_obj else None
    order_hints = [_skin_order_hint(source) for source in source_meshes]
    nonempty_hints = [tuple(hint) for hint in order_hints if hint]
    palette_hint = []
    if nonempty_hints:
        for hint in nonempty_hints:
            common = [name for name in palette_hint if name in hint]
            hint_common = [name for name in hint if name in palette_hint]
            if common != hint_common:
                raise RuntimeError(
                    'Selected meshes carry conflicting gem2_skin_order metadata')
            palette_hint.extend(name for name in hint if name not in palette_hint)
    if not palette_hint:
        palette_hint = None
    material_map = {}
    duplicates = []
    rest_armatures = {}
    failed = False
    operation_error = None

    def rest_armature_copy(original):
        key = original.as_pointer()
        existing = rest_armatures.get(key)
        if existing is not None:
            return existing
        rest = original.copy()
        rest.data = original.data.copy()
        rest.name = "__GEM2_RestArmature_%s" % token[:8]
        rest.data.name = rest.name + "_Data"
        rest[_TEMP_TOKEN_KEY] = token
        rest.data[_TEMP_TOKEN_KEY] = token
        rest.data.pose_position = "REST"
        context.scene.collection.objects.link(rest)
        rest.matrix_world = original.matrix_world.copy()
        rest_armatures[key] = rest
        return rest

    try:
        if context.object and context.object.mode != "OBJECT":
            result = _mode_set("OBJECT")
            if "FINISHED" not in result:
                raise RuntimeError("Blender could not enter Object Mode for export")
        bpy.ops.object.select_all(action="DESELECT")

        for source in source_meshes:
            duplicate = source.copy()
            duplicate[_TEMP_TOKEN_KEY] = token
            context.scene.collection.objects.link(duplicate)
            duplicates.append(duplicate)

            # Preserve modifier order while excluding the current animation pose:
            # Armature modifiers evaluate against an isolated rest-pose rig copy.
            for modifier in duplicate.modifiers:
                if modifier.type == "ARMATURE" and modifier.object:
                    modifier.object = rest_armature_copy(modifier.object)
            context.view_layer.update()
            depsgraph = context.evaluated_depsgraph_get()
            evaluated = duplicate.evaluated_get(depsgraph)
            evaluated_mesh = bpy.data.meshes.new_from_object(
                evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
            if evaluated_mesh is None:
                raise RuntimeError(
                    "Blender could not evaluate mesh %r" % source.name)
            evaluated_mesh[_TEMP_TOKEN_KEY] = token
            duplicate.data = evaluated_mesh
            duplicate.modifiers.clear()

            transform = ((armature_inverse @ source.matrix_world)
                         if armature_inverse is not None
                         else source.matrix_world.copy())
            duplicate.data.transform(transform)
            if transform.to_3x3().determinant() < 0.0:
                duplicate.data.flip_normals()
            duplicate.matrix_world = Matrix.Identity(4)
            duplicate.parent = None

            # CRITICAL: do NOT call duplicate.data.materials.clear() here.
            # In Blender clearing the slot list resets every polygon's
            # material_index to 0, collapsing the whole mesh onto slot 0. That
            # made every face export under the first material (e.g. a hair
            # {blend test} alpha-clip material) -> a single MESH block and a
            # white/transparent model on re-import. Replace each slot IN PLACE
            # so per-face material_index is preserved.
            original_materials = list(duplicate.data.materials)
            if not original_materials:
                key = None
                material = material_map.get(key)
                if material is None:
                    material = _copy_export_material(
                        None, entity_name, len(material_map), token)
                    material_map[key] = material
                duplicate.data.materials.append(material)
            else:
                for slot_index, original in enumerate(original_materials):
                    key = original.as_pointer() if original else None
                    material = material_map.get(key)
                    if material is None:
                        material = _copy_export_material(
                            original, entity_name, len(material_map), token)
                        material_map[key] = material
                    duplicate.data.materials[slot_index] = material

        active = max(
            duplicates,
            key=lambda obj: (len(obj.vertex_groups), len(obj.data.vertices),
                             obj.name.casefold()),
        )
        for duplicate in duplicates:
            duplicate.select_set(True)
        context.view_layer.objects.active = active
        if len(duplicates) > 1:
            result = bpy.ops.object.join()
            if "FINISHED" not in result:
                raise RuntimeError("Blender could not merge the selected export meshes")
        merged = active
        merged.name = "__GEM2_Multipart_%s" % token[:8]
        merged.data.name = merged.name + "_Mesh"
        palette_names = _normalize_vertex_groups(
            merged, arm_obj, preferred_order=palette_hint)
        merged.data.update()
        yield merged, arm_obj, list(material_map.values()), palette_names
    except BaseException as exc:
        failed = True
        operation_error = exc
        raise
    finally:
        for obj in list(bpy.data.objects):
            if obj.get(_TEMP_TOKEN_KEY) == token:
                bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in list(bpy.data.meshes):
            if mesh.get(_TEMP_TOKEN_KEY) == token and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        for armature in list(bpy.data.armatures):
            if armature.get(_TEMP_TOKEN_KEY) == token and armature.users == 0:
                bpy.data.armatures.remove(armature)
        for material in list(bpy.data.materials):
            if material.get(_TEMP_TOKEN_KEY) == token and material.users == 0:
                bpy.data.materials.remove(material)

        restore_error = None
        try:
            for obj in context.view_layer.objects:
                if obj is not None:
                    obj.select_set(False)
            for obj in original_selected:
                if obj.name in context.view_layer.objects:
                    obj.select_set(True)
            if original_active and original_active.name in context.view_layer.objects:
                context.view_layer.objects.active = original_active
                if original_mode != "OBJECT":
                    result = _mode_set(original_mode)
                    if "FINISHED" not in result:
                        raise RuntimeError(
                            "Blender rejected mode restoration to " + original_mode)
        except (RuntimeError, TypeError) as exc:
            restore_error = exc
        if restore_error is not None:
            state_error = RuntimeError(
                "Could not restore the original Blender mode %s: %s"
                % (original_mode, restore_error))
            if failed and operation_error is not None:
                group_type = (ExceptionGroup
                              if isinstance(operation_error, Exception)
                              else BaseExceptionGroup)
                raise group_type(
                    "Multipart operation and Blender state restoration both failed",
                    [operation_error, state_error])
            raise state_error from restore_error


def _encode_ascii(value, kind, max_length=255):
    try:
        encoded = str(value).encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError("%s must be ASCII: %s" % (kind, value)) from exc
    if not encoded or len(encoded) > max_length:
        raise RuntimeError(
            "%s must contain 1-%d ASCII bytes: %s" % (kind, max_length, value))
    return encoded


def build_export_data(mesh_obj, arm_obj, skin_world=None):
    mesh = mesh_obj.data
    mesh.update()
    mesh.calc_loop_triangles()
    if mesh.uv_layers.active is None:
        raise RuntimeError("Mesh %r has no active UV map" % mesh_obj.name)
    if not mesh.materials:
        raise RuntimeError("Mesh %r has no materials" % mesh_obj.name)

    invalid_slots = sorted({
        int(triangle.material_index)
        for triangle in mesh.loop_triangles
        if (triangle.material_index >= len(mesh.materials)
            or mesh.materials[triangle.material_index] is None)
    })
    if invalid_slots:
        raise RuntimeError(
            "Triangles use null material slots: "
            + ", ".join(map(str, invalid_slots)))

    has_skin = bool(arm_obj and len(mesh_obj.vertex_groups))
    skin_names = [group.name for group in mesh_obj.vertex_groups] if has_skin else []
    for name in skin_names:
        _encode_ascii(name, "Bone group")
    if len(skin_names) > 254:
        raise RuntimeError("GEM2 PLY supports at most 254 bone groups")

    skin_inverse = (skin_world.inverted()
                    if has_skin and skin_world is not None
                    else Matrix.Identity(4))
    normal_matrix = skin_inverse.to_3x3().inverted().transposed()
    positions = [skin_inverse @ vertex.co for vertex in mesh.vertices]
    uvs = mesh.uv_layers.active.data

    weights = []
    influence_counts = []
    if has_skin:
        for vertex in mesh.vertices:
            influences = sorted(
                ((float(element.weight), int(element.group))
                 for element in vertex.groups
                 if element.weight > 1e-6
                 and element.group < len(mesh_obj.vertex_groups)),
                reverse=True,
            )
            influence_counts.append(len(influences))
            weights.append(influences[:2])
        used_vertices = {
            int(mesh.loops[loop_index].vertex_index)
            for triangle in mesh.loop_triangles
            for loop_index in triangle.loops
        }
        unweighted = sorted(
            vertex_index for vertex_index in used_vertices
            if not weights[vertex_index])
        if unweighted:
            preview = ", ".join(map(str, unweighted[:12]))
            if len(unweighted) > 12:
                preview += ", ..."
            raise RuntimeError(
                "Skinned export contains triangle vertices without a valid "
                "armature weight: " + preview)

    # ── 重合层分类（max-compat 保高光方案，与 mowas2_pipeline 同款）────────
    # 同 diffuse 贴图的重合层（toufa/toufa_Copy 纯复制）→ 删除（无损 z-fight 垃圾）；
    # 不同 diffuse 的高光层（Hair_D / Hair_Dhi）→ 保留并沿法线外移极小 shell，脱离
    # 同深度：不再 z-fight、也不被 Blender validate 当重复删 → 高光在游戏内与重导入
    # 都保住。shell 深度按重合簇内出现次序递增（base=0）。
    from .texture_export import resolve_material_image as _resolve_diffuse

    def _diffuse_stem(material_index):
        material = (mesh.materials[material_index]
                    if 0 <= material_index < len(mesh.materials) else None)
        if not material:
            return ''
        try:
            image = _resolve_diffuse(material, 'diffuse')
        except Exception:
            image = None
        name = image.name if image else ''
        return os.path.splitext(name)[0].casefold()

    _HL_TOKENS = ('dhi', '高光', 'hi li', 'highlight', 'hilight', '_hi',
                  'hi2', 'eyehi', 'rim', 'outline', '描边', 'copy')

    def _is_highlight(material_index):
        material = (mesh.materials[material_index]
                    if 0 <= material_index < len(mesh.materials) else None)
        name = (material.name if material else '').casefold()
        stem = _diffuse_stem(material_index)
        if '+' in name:
            return True
        return any(token in name or token in stem for token in _HL_TOKENS)

    mat_diffuse = {}
    mat_is_hl = {}
    for _mi in range(len(mesh.materials)):
        mat_diffuse[_mi] = _diffuse_stem(_mi)
        mat_is_hl[_mi] = _is_highlight(_mi)

    loop_tris = list(mesh.loop_triangles)
    shell_by_ordinal = {}
    drop_ordinals = set()
    clusters = {}
    for ordinal, triangle in enumerate(loop_tris):
        gkey = tuple(sorted(
            (round(positions[int(v)].x, 4),
             round(positions[int(v)].y, 4),
             round(positions[int(v)].z, 4))
            for v in triangle.vertices))
        clusters.setdefault(gkey, []).append(ordinal)
    def _discriminator(material_index):
        # Prefer diffuse texture stem; fall back to material name (Blender .001
        # suffix stripped) when the texture cannot be resolved, so a highlight is
        # never wrongly dropped as a pure duplicate (over-keeping is safe).
        stem = mat_diffuse.get(material_index, '')
        if stem:
            return stem
        material = (mesh.materials[material_index]
                    if 0 <= material_index < len(mesh.materials) else None)
        name = (material.name if material else '').casefold()
        return re.sub(r'\.\d{3}$', '', name)

    for gkey, ordinals in clusters.items():
        if len(ordinals) < 2:
            continue
        ordered = sorted(
            ordinals,
            key=lambda o: (1 if mat_is_hl[loop_tris[o].material_index] else 0,
                           loop_tris[o].material_index, o))
        seen_disc = {}
        next_depth = 0
        for o in ordered:
            mi = loop_tris[o].material_index
            disc = _discriminator(mi)
            if disc in seen_disc:
                drop_ordinals.add(o)
                continue
            seen_disc[disc] = next_depth
            if next_depth:
                shell_by_ordinal[o] = next_depth
            next_depth += 1

    if positions:
        xs = [p.x for p in positions]
        ys = [p.y for p in positions]
        zs = [p.z for p in positions]
        diag = max(1e-6, ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2
                          + (max(zs) - min(zs)) ** 2) ** 0.5)
    else:
        diag = 1.0
    shell_epsilon = diag * 2.0e-4

    records = []
    record_positions = []
    record_lookup = {}
    tris_by_mat = [[] for _ in mesh.materials]
    removed_faces = 0
    shell_faces = 0

    def record_index(loop_index, depth):
        loop = mesh.loops[loop_index]
        vertex_index = int(loop.vertex_index)
        position = positions[vertex_index]
        normal = normal_matrix @ loop.normal
        if normal.length_squared > 1e-20:
            normal.normalize()
        if depth:
            position = position + normal * (shell_epsilon * depth)
        record = bytearray(pack_fff(position.x, position.y, position.z))
        if has_skin:
            influences = weights[vertex_index]
            total = sum(weight for weight, _group in influences)
            first_weight = influences[0][0] / total if total > 1e-20 else 1.0
            slots = [group + 1 for _weight, group in influences]
            slots.extend([0] * (4 - len(slots)))
            record.extend(pack_f(first_weight))
            record.extend(pack_BBBB(*slots[:4]))
        record.extend(pack_fff(normal.x, normal.y, normal.z))
        # No DIFFUSE dword: vanilla stride 40 / FVF 0x1118.
        uv = uvs[loop_index].uv
        record.extend(pack_ff(float(uv.x), 1.0 - float(uv.y)))
        key = bytes(record)
        index = record_lookup.get(key)
        if index is None:
            index = len(records)
            record_lookup[key] = index
            records.append(key)
            record_positions.append(position.copy())
        return index

    for ordinal, triangle in enumerate(loop_tris):
        if ordinal in drop_ordinals:
            removed_faces += 1
            continue
        depth = shell_by_ordinal.get(ordinal, 0)
        if depth:
            shell_faces += 1
        loops = triangle.loops
        tris_by_mat[triangle.material_index].append((
            record_index(loops[0], depth),
            record_index(loops[2], depth),
            record_index(loops[1], depth),
        ))
    if removed_faces or shell_faces:
        print("[multipart] coincident-layer handling: dropped %d pure-duplicate "
              "faces (same diffuse), shelled %d highlight faces (offset %.5f "
              "along normal)" % (removed_faces, shell_faces, shell_epsilon))

    active_material_indices = [
        index for index, triangles in enumerate(tris_by_mat) if triangles
    ]
    if not records or not active_material_indices:
        raise RuntimeError("No triangles remain for GEM2 export")
    return {
        "mesh": mesh,
        "mesh_to_ply": skin_inverse,
        "parent_name": "skin",
        "skip_indices": set(),
        "records": records,
        "positions": record_positions,
        "tris_by_mat": tris_by_mat,
        "active_material_indices": active_material_indices,
        "has_skin": has_skin,
        "skin_names": skin_names,
        "truncated_influence_vertices": sum(
            1 for vertex_index in used_vertices
            if influence_counts[vertex_index] > 2) if has_skin else 0,
    }


def _make_export_part(export_data, assignment):
    source_records = export_data["records"]
    source_positions = export_data["positions"]
    mesh = export_data["mesh"]
    used_records = set()
    ordered_by_material = {}
    for material_index, items in assignment.items():
        ordered = sorted(items, key=lambda item: item[0])
        triangles = [tuple(item[1]) for item in ordered]
        if not triangles:
            continue
        ordered_by_material[material_index] = triangles
        for triangle in triangles:
            used_records.update(triangle)
    if not used_records:
        raise RuntimeError("Multipart split produced an empty PLY part")

    global_indices = [index for index in range(len(source_records))
                      if index in used_records]
    local_indices = {global_index: index
                     for index, global_index in enumerate(global_indices)}
    tris_by_mat = [[] for _material in mesh.materials]
    for material_index, triangles in ordered_by_material.items():
        tris_by_mat[material_index] = [
            tuple(local_indices[index] for index in triangle)
            for triangle in triangles
        ]
    active_indices = [index for index, triangles in enumerate(tris_by_mat)
                      if triangles]
    return {
        "mesh": mesh,
        "mesh_to_ply": export_data["mesh_to_ply"],
        "parent_name": export_data["parent_name"],
        "skip_indices": set(export_data.get("skip_indices", ())),
        "records": [source_records[index] for index in global_indices],
        "positions": [source_positions[index] for index in global_indices],
        "tris_by_mat": tris_by_mat,
        "active_material_indices": active_indices,
        "global_record_indices": global_indices,
    }


def _partition_export_data(export_data, limit):
    records = export_data["records"]
    mesh = export_data["mesh"]
    active_indices = list(export_data["active_material_indices"])
    if not records or not active_indices:
        raise RuntimeError("Multipart split has no finalized geometry")

    material_items = {}
    material_records = {}
    for material_index in active_indices:
        items = [
            (ordinal, tuple(triangle))
            for ordinal, triangle in enumerate(
                export_data["tris_by_mat"][material_index])
        ]
        material_items[material_index] = items
        used = set()
        for _ordinal, triangle in items:
            used.update(triangle)
        material_records[material_index] = used
    ordered_materials = sorted(
        active_indices,
        key=lambda index: (-len(material_records[index]), index),
    )

    parts = []

    def new_part():
        if len(parts) >= 100:
            raise RuntimeError(
                "Multipart split needs more than 100 PLY files")
        parts.append({"records": set(), "assignment": {}})
        return len(parts) - 1

    def place_items(part_index, material_index, items, record_set):
        part = parts[part_index]
        part["records"].update(record_set)
        part["assignment"].setdefault(material_index, []).extend(items)

    new_part()
    for material_index in ordered_materials:
        items = material_items[material_index]
        record_set = material_records[material_index]
        if len(record_set) <= limit:
            candidates = []
            for part_index, part in enumerate(parts):
                added = len(record_set - part["records"])
                new_count = len(part["records"]) + added
                if new_count <= limit:
                    candidates.append(((added, limit - new_count, part_index),
                                       part_index))
            part_index = new_part() if not candidates else min(candidates)[1]
            place_items(part_index, material_index, items, record_set)
            continue

        item_by_ordinal = {
            ordinal: (ordinal, triangle) for ordinal, triangle in items}
        triangle_records = {
            ordinal: set(triangle) for ordinal, triangle in items}
        record_to_ordinals = {}
        for ordinal, record_ids in triangle_records.items():
            for record_id in record_ids:
                record_to_ordinals.setdefault(record_id, []).append(ordinal)
        remaining = set(item_by_ordinal)

        def fill_part(part_index):
            part = parts[part_index]
            missing = {
                ordinal: len(triangle_records[ordinal] - part["records"])
                for ordinal in remaining
            }
            heaps = [[] for _cost in range(4)]
            for ordinal, cost in missing.items():
                heapq.heappush(heaps[cost], ordinal)
            placed = 0
            while remaining:
                capacity = limit - len(part["records"])
                chosen = None
                for cost in range(min(3, capacity) + 1):
                    heap = heaps[cost]
                    while heap and (heap[0] not in remaining
                                    or missing.get(heap[0]) != cost):
                        heapq.heappop(heap)
                    if heap:
                        chosen = heapq.heappop(heap)
                        break
                if chosen is None:
                    break
                new_records = triangle_records[chosen] - part["records"]
                if len(new_records) > capacity:
                    raise RuntimeError(
                        "Multipart incremental record cost exceeded capacity")
                remaining.remove(chosen)
                part["assignment"].setdefault(material_index, []).append(
                    item_by_ordinal[chosen])
                part["records"].update(new_records)
                placed += 1
                for record_id in new_records:
                    for neighbor in record_to_ordinals.get(record_id, ()):
                        if neighbor not in remaining:
                            continue
                        old_cost = missing[neighbor]
                        if old_cost <= 0:
                            continue
                        new_cost = old_cost - 1
                        missing[neighbor] = new_cost
                        heapq.heappush(heaps[new_cost], neighbor)
            return placed

        for part_index in range(len(parts)):
            if not remaining:
                break
            fill_part(part_index)
        while remaining:
            part_index = new_part()
            if not fill_part(part_index):
                raise RuntimeError(
                    "Multipart split could not place an atomic triangle")

    parts = [part for part in parts if part["assignment"]]
    if not parts:
        raise RuntimeError("Multipart split did not assign any triangles")
    for material_index in active_indices:
        assigned = sorted(
            ordinal
            for part in parts
            for ordinal, _triangle in part["assignment"].get(
                material_index, ()))
        expected = list(range(len(material_items[material_index])))
        if assigned != expected:
            raise RuntimeError(
                "Multipart split lost or duplicated triangles for material %s"
                % mesh.materials[material_index].name)

    payloads = [
        _make_export_part(export_data, part["assignment"])
        for part in parts
    ]
    if any(not payload["records"] or len(payload["records"]) > limit
           for payload in payloads):
        raise RuntimeError("Multipart split emitted an invalid record count")
    return payloads


def partition_export_data(export_data, record_limit=GAME_VERTEX_LIMIT):
    record_limit = int(record_limit)
    if record_limit < 3 or record_limit > GAME_VERTEX_LIMIT:
        raise ValueError("Record limit must be between 3 and 65535")
    if len(export_data["records"]) <= record_limit:
        return [export_data]
    return _partition_export_data(export_data, limit=record_limit)


def write_ply_payload(filepath, payload, has_skin, skin_names):
    records = payload["records"]
    positions = payload["positions"]
    mesh = payload["mesh"]
    active_indices = payload["active_material_indices"]
    if not records or len(records) > GAME_VERTEX_LIMIT:
        raise RuntimeError("Invalid GEM2 PLY record count: %d" % len(records))
    if any(len(record) != len(records[0]) for record in records):
        raise RuntimeError("GEM2 PLY payload has mixed vertex strides")

    skin_bytes = [_encode_ascii(name, "Bone group") for name in skin_names]
    material_bytes = {}
    for material_index in active_indices:
        material = mesh.materials[material_index]
        material_bytes[material_index] = _encode_ascii(
            material.name + ".mtl", "Material filename")

    minimum = [min(float(position[axis]) for position in positions)
               for axis in range(3)]
    maximum = [max(float(position[axis]) for position in positions)
               for axis in range(3)]
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    with open(filepath, "wb") as handle:
        handle.write(b"EPLY")
        handle.write(b"BNDS")
        handle.write(pack_fff(*minimum))
        handle.write(pack_fff(*maximum))

        if has_skin:
            handle.write(b"SKIN")
            handle.write(pack_I(len(skin_bytes)))
            for encoded in skin_bytes:
                handle.write(pack_B(len(encoded)))
                handle.write(encoded)

        triangle_start = 0
        for material_index in active_indices:
            triangles = payload["tris_by_mat"][material_index]
            handle.write(b"MESH")
            # Vanilla humanskin FVF: no DIFFUSE (records are stride 40 / 0x1118).
            fvf = D3DFVF_NORMAL | D3DFVF_TEX1
            if has_skin:
                fvf |= D3DFVF_XYZB2 | D3DFVF_LASTBETA_UBYTE4
            else:
                fvf |= D3DFVF_XYZ
            handle.write(pack_I(fvf))
            handle.write(pack_I(triangle_start))
            handle.write(pack_I(len(triangles)))
            triangle_start += len(triangles)

            flags = MESH_FLAG_LIGHT | MESH_FLAG_MATERIAL
            if has_skin:
                flags |= MESH_FLAG_SKINNED | MESH_FLAG_SUBSKIN
            handle.write(pack_I(flags))
            encoded = material_bytes[material_index]
            handle.write(pack_B(len(encoded)))
            handle.write(encoded)
            if has_skin:
                palette = [0] + list(range(1, len(skin_names) + 1))
                handle.write(pack_B(len(palette)))
                handle.write(bytes(palette))

        handle.write(b"VERT")
        handle.write(pack_I(len(records)))
        handle.write(pack_H(len(records[0])))
        handle.write(b"\x07\x00")
        for record in records:
            handle.write(record)

        handle.write(b"INDX")
        triangle_count = sum(
            len(payload["tris_by_mat"][index]) for index in active_indices)
        handle.write(pack_I(triangle_count * 3))
        for material_index in active_indices:
            for triangle in payload["tris_by_mat"][material_index]:
                if max(triangle) >= len(records):
                    raise RuntimeError("PLY part contains an out-of-range index")
                handle.write(pack_HHH(*triangle))

    return {
        "path": filepath,
        "records": len(records),
        "triangles": triangle_count,
        "materials": len(active_indices),
        "stride": len(records[0]),
    }


def _direct_volume_view_matches(content, bone_name):
    from .mdl_io import find_named_bone_span

    bone_start, bone_end = find_named_bone_span(content, bone_name)

    view_pattern = re.compile(
        r'\{\s*VolumeView\s+"(?P<filename>[^"]*)"\s*\}',
        re.IGNORECASE)
    direct_views = []
    depth = 0
    in_string = False
    escaped = False
    index = bone_start
    while index <= bone_end:
        char = content[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "{":
            if depth == 1:
                match = view_pattern.match(content, index)
                if match is not None and match.end() <= bone_end + 1:
                    direct_views.append(match)
                    index = match.end()
                    continue
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return bone_start, bone_end, direct_views


def _append_direct_volume_view(content, bone_name, ply_filename):
    filename = str(ply_filename)
    if not filename or any(char in filename for char in ('"', "\r", "\n")):
        raise ValueError("Invalid split VolumeView filename: " + filename)
    _bone_start, _bone_end, direct_views = _direct_volume_view_matches(
        content, bone_name)
    if not direct_views:
        raise RuntimeError(
            "Expected a base VolumeView on MDL mesh parent %r" % bone_name)
    if any(match.group("filename").casefold() == filename.casefold()
           for match in direct_views):
        raise RuntimeError("Split VolumeView already exists: " + filename)
    anchor = direct_views[-1]
    line_start = content.rfind("\n", 0, anchor.start()) + 1
    indent = content[line_start:anchor.start()]
    if indent.strip():
        raise RuntimeError(
            "Cannot determine VolumeView indentation: " + str(bone_name))
    newline = "\r\n" if "\r\n" in content else "\n"
    insertion = newline + indent + '{VolumeView "' + filename + '"}'
    return content[:anchor.end()] + insertion + content[anchor.end():]


def _write_base_mdl(filepath, arm_obj, mesh_obj, ply_name):
    if arm_obj is None:
        content = (
            "{Skeleton\n"
            "\t{bone \"skin\"\n"
            "\t\t{Position 0\t0\t0}\n"
            "\t\t{VolumeView \"%s\"}\n"
            "\t}\n"
            "}\n" % ply_name)
        with open(filepath, "w", encoding="utf-8") as handle:
            handle.write(content)
        return Matrix.Identity(4), "skin"

    from .mdl_io import write_mdl_file
    # ``write_mdl_file`` retains its historical raw return value for callers;
    # PLY encoding needs the attachment-local frame instead.
    write_mdl_file(filepath, arm_obj, mesh_obj, ply_name)
    skin_world = _preview_skin_world(arm_obj)
    attachment = "skin"
    raw_mats = arm_obj.get("gem2_world_mats")
    mesh_parent = str(arm_obj.get("gem2_mesh_parent") or "")
    try:
        stored = json.loads(raw_mats) if isinstance(raw_mats, str) else raw_mats
        if mesh_parent and stored and mesh_parent in stored:
            attachment = mesh_parent
    except (TypeError, ValueError):
        pass
    return skin_world, attachment


def _material_diffuse_image(material):
    """Resolve diffuse through MMD groups and ordinary node chains."""
    return resolve_material_image(material, "diffuse")


def _write_flat_tga(filepath, material):
    color = tuple(float(value) for value in material.diffuse_color)
    red, green, blue = [max(0, min(255, int(round(value * 255.0))))
                        for value in color[:3]]
    alpha = max(0, min(255, int(round(color[3] * 255.0))))
    header = bytes((
        0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        1, 0, 1, 0, 32, 0x08,
    ))
    with open(filepath, "wb") as handle:
        handle.write(header)
        handle.write(bytes((blue, green, red, alpha)))


def _preview_skin_world(arm_obj):
    """Return the attachment-local matrix used to encode PLY positions.

    The historical name is kept for API compatibility.  Raw MDL world frames
    are converted to ``inverse(ancestor) * W_skin`` here; passing raw
    ``W_skin`` would mirror every native human on export and import again.
    """
    from .mdl_io import mesh_parent_local_matrix
    return mesh_parent_local_matrix(arm_obj)


def _relative_files(root):
    paths = []
    for directory, _subdirs, filenames in os.walk(root):
        for filename in filenames:
            absolute = os.path.join(directory, filename)
            paths.append(os.path.relpath(absolute, root))
    return sorted(paths, key=str.casefold)


def _commit_staged_files(stage_dir, output_dir, stale_predicate):
    new_files = _relative_files(stage_dir)
    if not new_files:
        raise RuntimeError("Multipart staging directory is empty")
    os.makedirs(output_dir, exist_ok=True)
    backup_dir = tempfile.mkdtemp(
        prefix=".gem2_backup_", dir=os.path.dirname(output_dir))
    existing = set(new_files)
    for relative in _relative_files(output_dir):
        if stale_predicate(relative):
            existing.add(relative)

    backed_up = []
    committed = []
    preserve_backup = False
    try:
        for relative in sorted(existing, key=str.casefold):
            target = os.path.join(output_dir, relative)
            if not os.path.exists(target):
                continue
            backup = os.path.join(backup_dir, relative)
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            os.replace(target, backup)
            backed_up.append(relative)
        for relative in new_files:
            staged = os.path.join(stage_dir, relative)
            target = os.path.join(output_dir, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(staged, target)
            committed.append(relative)
    except BaseException as commit_error:
        rollback_errors = []
        backed_set = set(backed_up)
        for relative in reversed(backed_up):
            backup = os.path.join(backup_dir, relative)
            target = os.path.join(output_dir, relative)
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.exists(backup):
                    os.replace(backup, target)
            except OSError as exc:
                rollback_errors.append(
                    "restore %s: %s" % (relative, exc))
        for relative in reversed(committed):
            if relative in backed_set:
                continue
            target = os.path.join(output_dir, relative)
            try:
                if os.path.isfile(target):
                    os.remove(target)
            except OSError as exc:
                rollback_errors.append(
                    "remove new %s: %s" % (relative, exc))
        if rollback_errors:
            preserve_backup = True
            raise RuntimeError(
                "Multipart commit failed (%s); rollback was incomplete. "
                "Original backups were preserved at %s. Errors: %s"
                % (commit_error, backup_dir, "; ".join(rollback_errors))) \
                from commit_error
        raise
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        if not preserve_backup:
            shutil.rmtree(backup_dir, ignore_errors=True)


def analyze_selected_model(context, use_selection=True,
                           record_limit=GAME_VERTEX_LIMIT):
    record_limit = int(record_limit)
    if record_limit < 3 or record_limit > GAME_VERTEX_LIMIT:
        raise ValueError("Record limit must be between 3 and 65535")
    source_meshes = collect_mesh_objects(context, use_selection=use_selection)
    source_vertices = sum(len(obj.data.vertices) for obj in source_meshes)

    with temporary_merged_model(
            context, source_meshes, "preflight") as (
                merged, arm_obj, _materials, palette_names):
        export_data = build_export_data(
            merged, arm_obj, skin_world=_preview_skin_world(arm_obj))
        payloads = partition_export_data(
            export_data, record_limit=record_limit)
        parts = []
        for payload in payloads:
            parts.append({
                "records": len(payload["records"]),
                "triangles": sum(
                    len(payload["tris_by_mat"][index])
                    for index in payload["active_material_indices"]),
                "materials": len(payload["active_material_indices"]),
            })
        evaluated_triangles = sum(part["triangles"] for part in parts)
        return {
            "source_meshes": len(source_meshes),
            "source_vertices": source_vertices,
            "evaluated_vertices": len(merged.data.vertices),
            "triangles": evaluated_triangles,
            "total_records": len(export_data["records"]),
            "record_limit": record_limit,
            "required_parts": len(parts),
            "parts": parts,
            "palette_groups": len(palette_names),
            "has_skin": export_data["has_skin"],
            "truncated_influence_vertices": export_data[
                "truncated_influence_vertices"],
        }


def _post_export_selfcheck(written_parts, export_data, palette_names):
    """Read back the just-written PLY files and flag regressions that silently
    break the model in-game (single-material collapse, duplicated triangles,
    non-standard vertex stride). Returns dict with per-part facts and warnings.

    This guards the exact failure mode seen in the field: a legacy exporter
    collapsed every face onto the first material slot (one MESH block instead of
    three) and duplicated triangles, which rendered Body/Head/Hair with the wrong
    texture and caused the 'facing texture broken/inside-out' reports in GOH.
    """
    from collections import Counter
    import struct as _st

    per_part = []
    warnings = []
    for part in written_parts:
        path = part.get("path")
        info = {
            "filename": part.get("filename"),
            "records": part.get("records"),
            "triangles": part.get("triangles"),
            "stride": part.get("stride"),
        }
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            # MESH block count and triangle counts
            pos = 4
            mesh_blocks = 0
            block_tris = 0
            if data[pos:pos + 4] == b"BNDS":
                pos += 28
            if data[pos:pos + 4] == b"SKIN":
                pos += 4
                n = _st.unpack_from("<I", data, pos)[0]
                pos += 4
                for _index in range(n):
                    ln = data[pos]
                    pos += 1 + ln
            while data[pos:pos + 4] == b"MESH":
                pos += 4
                _fvf = _st.unpack_from("<I", data, pos)[0]
                pos += 4
                _start = _st.unpack_from("<I", data, pos)[0]
                pos += 4
                count = _st.unpack_from("<I", data, pos)[0]
                pos += 4
                _flags = _st.unpack_from("<I", data, pos)[0]
                pos += 4
                _mlen = data[pos]
                pos += 1 + _mlen
                _plen = data[pos]
                pos += 1 + _plen
                mesh_blocks += 1
                block_tris += count
            # actual INDX triangles + uniqueness
            indx = data.find(b"INDX", pos)
            if indx >= 0:
                icount = _st.unpack_from("<I", data, indx + 4)[0]
                idxs = _st.unpack_from("<%dH" % icount, data, indx + 8)
                indx_tris = icount // 3
                unique = len(Counter(
                    tuple(idxs[i:i + 3]) for i in range(0, icount, 3)))
                info["mesh_blocks"] = mesh_blocks
                info["indx_tris"] = indx_tris
                info["unique_tris"] = unique
                if unique < indx_tris:
                    warnings.append(_(
                        "operator.export_multipart.selfcheck.dup_tris",
                        file=part.get("filename"),
                        dup=indx_tris - unique,
                        unique=unique,
                        total=indx_tris))
                if mesh_blocks == 1 and block_tris == indx_tris and \
                        len(export_data.get("tris_by_mat", ())) > 1:
                    warnings.append(_(
                        "operator.export_multipart.selfcheck.collapsed",
                        file=part.get("filename"),
                        tris=indx_tris,
                        slots=len(export_data.get("tris_by_mat", ()))))
        except Exception as exc:
            info["readback_error"] = repr(exc)
        per_part.append(info)

    stride = {p.get("stride") for p in written_parts}
    if stride and 40 not in stride:
        warnings.append(_(
            "operator.export_multipart.selfcheck.stride",
            stride=sorted(stride)))
    if len(palette_names) != len(export_data.get("skin_names", ())) and \
            export_data.get("skin_names"):
        warnings.append(_("operator.export_multipart.selfcheck.palette"))
    return {"parts": per_part, "warnings": warnings}


def export_selected_model(context, filepath, use_selection=True,
                          record_limit=GAME_VERTEX_LIMIT,
                          material_mode="SIMPLE", copy_textures=True):
    record_limit = int(record_limit)
    if record_limit < 3 or record_limit > GAME_VERTEX_LIMIT:
        raise ValueError("Record limit must be between 3 and 65535")

    source_meshes = collect_mesh_objects(context, use_selection=use_selection)
    source_vertices = sum(len(obj.data.vertices) for obj in source_meshes)
    chosen_dir = os.path.dirname(os.path.abspath(filepath))
    requested_name = os.path.splitext(os.path.basename(filepath))[0]
    entity_name = _ascii_component(requested_name, "model")
    # GOH/MOWAS2 humanskin convention: every entity owns its own
    # <root>/humanskin/<entity>/ folder holding <entity>.mdl/.ply/.def/.mtl and
    # the textures. The export dialog only yields one filepath, so nest into
    # <entity>/ here — otherwise the files were dumped flat into the chosen
    # directory (mixing many entities and breaking the def→mdl→ply lookup).
    # Skip nesting if the user already picked a folder named after the entity,
    # to avoid <entity>/<entity>/ on re-export.
    if os.path.basename(chosen_dir).casefold() == entity_name.casefold():
        output_dir = chosen_dir
    else:
        output_dir = os.path.join(chosen_dir, entity_name)
    parent_dir = os.path.dirname(output_dir)
    os.makedirs(parent_dir, exist_ok=True)
    stage_dir = tempfile.mkdtemp(
        prefix=".%s_stage_" % entity_name, dir=parent_dir)
    mdl_path = os.path.join(output_dir, entity_name + ".mdl")
    def_path = os.path.join(output_dir, entity_name + ".def")
    base_ply_name = entity_name + ".ply"

    try:
        with temporary_merged_model(
                context, source_meshes, entity_name) as (
                    merged, arm_obj, export_materials, palette_names):
            stage_mdl = os.path.join(stage_dir, entity_name + ".mdl")
            skin_world, attachment_bone = _write_base_mdl(
                stage_mdl, arm_obj, merged, base_ply_name)
            export_data = build_export_data(merged, arm_obj, skin_world=skin_world)
            payloads = partition_export_data(
                export_data, record_limit=record_limit)
            filenames = [base_ply_name]
            filenames.extend(
                "%s_split%02d.ply" % (entity_name, index)
                for index in range(1, len(payloads)))

            with open(stage_mdl, "r", encoding="utf-8") as handle:
                mdl_content = handle.read()
            for filename in filenames[1:]:
                mdl_content = _append_direct_volume_view(
                    mdl_content, attachment_bone, filename)
            with open(stage_mdl, "w", encoding="utf-8", newline="") as handle:
                handle.write(mdl_content)

            written_parts = []
            for filename, payload in zip(filenames, payloads):
                result = write_ply_payload(
                    os.path.join(stage_dir, filename), payload,
                    export_data["has_skin"], export_data["skin_names"])
                result["filename"] = filename
                result["path"] = os.path.join(output_dir, filename)
                written_parts.append(result)

            stage_def = os.path.join(stage_dir, entity_name + ".def")
            with open(stage_def, "w", encoding="utf-8") as handle:
                handle.write("{game_entity\n")
                handle.write('\t{extension "%s.mdl"}\n' % entity_name)
                handle.write("}\n")

            material_texture_refs = {}
            if copy_textures:
                from .gem2_export import _stage_material_textures
                material_texture_refs = _stage_material_textures(
                    stage_dir, export_materials)

            from .mtl_io import export_mtl
            for material in export_materials:
                refs = material_texture_refs.get(
                    _material_key(material), {})
                export_mtl(
                    os.path.join(stage_dir, material.name + ".mtl"),
                    material,
                    material_mode,
                    texture_names=refs,
                )
                # A flat fallback is valid only when the material genuinely
                # has no diffuse image. A resolved image is always staged and
                # referenced by its matching TGA above.
                if "diffuse" not in refs and _material_diffuse_image(material) is None:
                    _write_flat_tga(
                        os.path.join(stage_dir, material.name + ".tga"),
                        material,
                    )

            evaluated_triangles = sum(
                len(export_data["tris_by_mat"][index])
                for index in export_data["active_material_indices"])
            total_records = len(export_data["records"])
            written_records = sum(part["records"] for part in written_parts)
            written_triangles = sum(part["triangles"] for part in written_parts)
            if written_triangles != evaluated_triangles:
                raise RuntimeError(
                    "Multipart export triangle total changed: %d != %d"
                    % (written_triangles, evaluated_triangles))
            summary = {
                "output_dir": output_dir,
                "entity_name": entity_name,
                "mdl_path": mdl_path,
                "def_path": def_path,
                "ply_paths": [part["path"] for part in written_parts],
                "parts": written_parts,
                "source_meshes": len(source_meshes),
                "source_vertices": source_vertices,
                "evaluated_vertices": len(merged.data.vertices),
                "triangles": evaluated_triangles,
                "total_records": total_records,
                "written_records": written_records,
                "duplicated_boundary_records": written_records - total_records,
                "record_limit": record_limit,
                "palette_groups": len(palette_names),
                "truncated_influence_vertices": export_data[
                    "truncated_influence_vertices"],
                "attachment_bone": attachment_bone,
                "selfcheck": None,
            }

        split_pattern = re.compile(
            re.escape(entity_name) + r"_split\d{2}\.ply$", re.IGNORECASE)
        material_prefix = (entity_name + "_mat_").casefold()

        def stale_generated(relative):
            if os.path.dirname(relative):
                return False
            filename = os.path.basename(relative)
            folded = filename.casefold()
            return (split_pattern.fullmatch(filename) is not None
                    or (folded.startswith(material_prefix)
                        and folded.endswith((".mtl", ".tga"))))

        _commit_staged_files(stage_dir, output_dir, stale_generated)
        # Read-back the committed files (not the staging dir) so the selfcheck
        # sees exactly what lands on disk.
        selfcheck = _post_export_selfcheck(
            written_parts, export_data, palette_names)
        summary["selfcheck"] = selfcheck
        if selfcheck["warnings"]:
            print("[multipart-selfcheck] warnings:")
            for warn in selfcheck["warnings"]:
                print("  -", warn)
        return summary
    except BaseException:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
