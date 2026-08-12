# -*- coding: utf-8 -*-
"""
GEM2 PMX High→LowPoly Texture Bake Pipeline
============================================
Blender 4.3 native-only pipeline. No Open3D dependency.

Five phases:
  Phase 1 — Data backup & highpoly protection (read-only)
  Phase 2 — Face polygons separated → body decimate → rejoin
  Phase 3 — UV remap + single-material rebuild (Smart UV, merged mesh)
  Phase 4 — Cycles GPU diffuse bake (4096×4096, 1024 samples)
  Phase 5 — Cleanup & validation

Key design:
  - Face/eyes detected by VG-name + material-name (30 keywords)
  - Face separated pre-decimate, rejoined immediately after body COLLAPSE
  - Single material Merged_Material + Baked_Diffuse.png (4096×4096)
  - Cycles GPU, 1024 samples, 32px margin, pass_filter={'COLOR'}
  - No Open3D, no external tools

Usage:
  import native_decimate
  native_decimate.fast_run()
  native_decimate.run_pipeline(selected_obj)
"""

import bpy
import os
import math
import time
import subprocess
import tempfile
import shutil
import numpy as np

_np = None


def _get_np():
    global _np
    if _np is None:
        import numpy
        _np = numpy
    return _np


_BODY_UV_V_SCALE = 0.55
_FACE_UV_V_SCALE = 0.35
_FACE_UV_V_OFFSET = 0.65
_UV_REGION_GAP = 0.10


# ═══════════════════════════════════════════════════════════════
#  Phase 1: Data backup & highpoly protection
# ═══════════════════════════════════════════════════════════════

def phase1_backup(mesh_obj):
    print("\n── [P1] Data Backup ──")

    bpy.ops.object.select_all(action='DESELECT')
    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_obj

    bpy.ops.object.duplicate()
    highpoly = bpy.context.active_object

    mesh_obj.select_set(True)

    highpoly.name = "_highpoly_SOURCE"
    mesh_obj.name = "_lowpoly_TARGET"

    bpy.ops.object.make_single_user(object=True, obdata=True)

    for mod in list(mesh_obj.modifiers):
        if mod.type == 'ARMATURE':
            if mod.object:
                mod.object.data.pose_position = 'REST'
                bpy.context.view_layer.update()
            mesh_obj.modifiers.remove(mod)
            print(f"[P1] [Success] Removed ARMATURE modifier from '{mesh_obj.name}'")

    highpoly.hide_viewport = True
    highpoly.hide_render = True
    highpoly.select_set(False)

    bpy.context.view_layer.objects.active = mesh_obj
    mesh_obj.select_set(True)

    nv = len(mesh_obj.data.vertices)
    nf = len(mesh_obj.data.polygons)
    print(f"[P1] [Success] _highpoly_SOURCE created & hidden ({nv}v/{nf}f)")
    print(f"[P1] [Success] _lowpoly_TARGET active, ready for decimation")
    return highpoly, mesh_obj


# ═══════════════════════════════════════════════════════════════
#  Phase 2: Low-poly decimation (face-separate → body-deci → merge)
# ═══════════════════════════════════════════════════════════════

_FACE_KEYWORDS = (
    "face", "head", "kao", "eye", "hitomi", "me",
    "mabuta", "mayuge", "nose", "mouth", "kuchi",
    "mimi", "ear", "lip", "brow", "eyebrows", "lash", "tongue",
    "tooth", "cheek", "hoho", "chin", "ago",
    "forehead", "pupil", "eyeball", "glint",
    "sirome", "eyelash", "eyeline", "bican",
    "\u9854", "\u9762", "\u982d", "\u76ee", "\u53e3", "\u9f3b",
    "\u7709", "\u8033", "\u820c", "\u6b6e", "\u9850",
    "\u307b\u307b", "\u3042\u3054", "\u307e\u3048",
    "\u3072\u3068\u307f", "\u307e\u3076\u305f", "\u307e\u3086\u3052",
    "\u3057\u308d\u3081", "\u304f\u3061", "\u307f\u307f",
)


def _find_face_verts_by_vgroups(mesh_obj):
    keywords = _FACE_KEYWORDS
    face_vg_indices = []
    for vg in mesh_obj.vertex_groups:
        name_lower = vg.name.lower()
        for kw in keywords:
            if kw in name_lower:
                face_vg_indices.append(vg.index)
                break
    if not face_vg_indices:
        return set()
    face_vg_set = set(face_vg_indices)
    face_verts = set()
    for v in mesh_obj.data.vertices:
        for g in v.groups:
            if g.group in face_vg_set and g.weight > 0.0:
                face_verts.add(v.index)
                break
    return face_verts


def _find_face_polys_by_materials(mesh_obj):
    face_mat_indices = set()
    for mi, mat in enumerate(mesh_obj.data.materials):
        if mat is None:
            continue
        name_lower = mat.name.lower()
        for kw in _FACE_KEYWORDS:
            if kw in name_lower:
                face_mat_indices.add(mi)
                break
    if not face_mat_indices:
        return set()
    face_polys = set()
    for pi, poly in enumerate(mesh_obj.data.polygons):
        if poly.material_index in face_mat_indices:
            face_polys.add(pi)
    return face_polys


def _find_face_polygons(mesh_obj):
    face_polys_by_mat = _find_face_polys_by_materials(mesh_obj)
    face_verts_by_vg = _find_face_verts_by_vgroups(mesh_obj)

    face_polys = set(face_polys_by_mat)

    if face_verts_by_vg:
        for pi, poly in enumerate(mesh_obj.data.polygons):
            for vi in poly.vertices:
                if vi in face_verts_by_vg:
                    face_polys.add(pi)
                    break

    return face_polys


def _find_face_polys_by_vg(mesh_obj, vg_name="_face_protected"):
    """Find face polygons by checking the _face_protected vertex group.

    This is robust across join operations because vertex groups persist
    through joins, unlike material indices which get overwritten.
    """
    vg = mesh_obj.vertex_groups.get(vg_name)
    if vg is None:
        return set()
    vg_idx = vg.index
    face_polys = set()
    for poly in mesh_obj.data.polygons:
        for vi in poly.vertices:
            v = mesh_obj.data.vertices[vi]
            for g in v.groups:
                if g.group == vg_idx and g.weight > 0.0:
                    face_polys.add(poly.index)
                    break
            else:
                continue
            break
    return face_polys


def _find_face_polys_by_uv(mesh_obj, uv_layer_name="Bake_UV",
                           v_threshold=None):
    """Find face polygons by checking UV V coordinate.

    This is a secondary detection method that catches face polygons
    missed by VG-based detection (e.g., face-adjacent polys without
    face vertex group weights but with face UV coordinates).
    """
    if v_threshold is None:
        v_threshold = _BODY_UV_V_SCALE + _UV_REGION_GAP / 2

    uv_layer = mesh_obj.data.uv_layers.get(uv_layer_name)
    if uv_layer is None:
        return set()

    np = _get_np()
    total_loops = len(mesh_obj.data.loops)
    uvs = np.zeros(total_loops * 2, dtype=np.float32)
    uv_layer.data.foreach_get("uv", uvs)
    uvs = uvs.reshape((-1, 2))

    face_polys = set()
    for pi, poly in enumerate(mesh_obj.data.polygons):
        ls = poly.loop_start
        le = ls + poly.loop_total
        if le > len(uvs):
            continue
        poly_v = float(np.mean(uvs[ls:le, 1]))
        if poly_v > v_threshold:
            face_polys.add(pi)

    return face_polys


def _mark_face_vertex_group(mesh_obj, face_polys):
    """Create a _face_protected vertex group and assign all vertices
    of the given face polygons to it with weight 1.0.

    This allows face polygons to be tracked through join operations
    where material indices are overwritten.
    """
    vg = mesh_obj.vertex_groups.get("_face_protected")
    if vg is None:
        vg = mesh_obj.vertex_groups.new(name="_face_protected")
    else:
        vg.remove(range(len(mesh_obj.data.vertices)))

    face_verts = set()
    for pi in face_polys:
        if pi < len(mesh_obj.data.polygons):
            for vi in mesh_obj.data.polygons[pi].vertices:
                face_verts.add(vi)

    for vi in face_verts:
        vg.add([vi], 1.0, 'REPLACE')

    return vg


def _apply_collapse_pass(mesh_obj, name, ratio, vg_name=None):
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.modifier_add(type='DECIMATE')
    mod = mesh_obj.modifiers[-1]
    mod.name = name
    mod.decimate_type = 'COLLAPSE'
    mod.ratio = ratio
    mod.use_collapse_triangulate = True
    try:
        mod.delimit = {'NORMAL'}
    except TypeError:
        pass
    if vg_name:
        mod.vertex_group = vg_name
    bpy.ops.object.modifier_apply(modifier=name)
    bpy.context.view_layer.update()
    nv = len(mesh_obj.data.vertices)
    vg_note = f" VG={vg_name}" if vg_name else " global"
    print(f"[P2] [Success] {name} (ratio={ratio}{vg_note}): → {nv}v")
    return nv


def phase2_decimate(mesh_obj):
    print("\n── [P2] Decimation ──")
    nv0 = len(mesh_obj.data.vertices)
    nf0 = len(mesh_obj.data.polygons)
    print(f"[P2] Initial: {nv0}v / {nf0}f")

    bpy.ops.object.select_all(action='DESELECT')
    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_obj
    bpy.ops.object.mode_set(mode='OBJECT')

    s = mesh_obj.scale
    if abs(s.x - 1.0) > 0.001 or abs(s.y - 1.0) > 0.001 or abs(s.z - 1.0) > 0.001:
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        print(f"[P2] [Success] Scale applied (was {tuple(round(v, 4) for v in s)})")

    arm_mods_removed = 0
    for mod in list(mesh_obj.modifiers):
        if mod.type == 'ARMATURE':
            if mod.object:
                mod.object.data.pose_position = 'REST'
                bpy.context.view_layer.update()
            mesh_obj.modifiers.remove(mod)
            arm_mods_removed += 1
    if arm_mods_removed:
        print(f"[P2] [Success] Removed {arm_mods_removed} armature modifier(s)")

    if mesh_obj.data.shape_keys:
        mesh_obj.shape_key_clear()
        print(f"[P2] [Success] Shape keys cleared")

    bpy.context.view_layer.update()

    face_polys = _find_face_polygons(mesh_obj)
    body_poly_count = nf0 - len(face_polys)

    print(f"[P2] Face polygons: {len(face_polys)} | Body polygons: {body_poly_count}")

    face_obj = None
    body_obj = mesh_obj

    if face_polys and body_poly_count > 0:
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='OBJECT')

        for pi in face_polys:
            mesh_obj.data.polygons[pi].select = True

        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.mode_set(mode='OBJECT')

        for obj in bpy.context.selected_objects:
            if obj != mesh_obj:
                face_obj = obj
                break

        body_obj = mesh_obj
        body_obj.name = "_temp_body"
        face_obj.name = "_temp_face"

        face_obj.hide_viewport = True
        face_obj.hide_render = True

        bpy.ops.object.select_all(action='DESELECT')
        body_obj.select_set(True)
        bpy.context.view_layer.objects.active = body_obj
        bpy.ops.object.mode_set(mode='OBJECT')

        _apply_collapse_pass(body_obj, "Decimate_Body_P1", 0.5)
        _apply_collapse_pass(body_obj, "Decimate_Body_P2", 0.7)

        # ✅ 新增：仅对 Body 进行清理（在 Join 之前！）
        print(f"[P2] Cleaning up body mesh before joining...")
        bpy.ops.object.select_all(action='DESELECT')
        body_obj.select_set(True)
        bpy.context.view_layer.objects.active = body_obj
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.mesh.remove_doubles(threshold=0.0001)
        bpy.ops.object.mode_set(mode='OBJECT')
        print(f"[P2] [Success] Body Merge by Distance (0.0001)")

        # 法线清理也放到这里
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        try:
            bpy.ops.mesh.customdata_custom_splitnormals_clear()
        except RuntimeError:
            pass
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.shade_smooth()
        bpy.ops.object.shade_smooth_by_angle(angle=math.radians(60))
        print(f"[P2] [Success] Body Normals cleaned")

        face_obj.hide_viewport = False
        face_obj.select_set(True)
        body_obj.select_set(True)
        bpy.context.view_layer.objects.active = body_obj
        bpy.ops.object.join()
        bpy.context.view_layer.update()

        # ✅ Join 之后什么都不做，直接去取名字


    nv_final = len(mesh_obj.data.vertices)
    nf_final = len(mesh_obj.data.polygons)
    red = (1 - nv_final / max(1, nv0)) * 100
    print(f"[P2] [Success] {nv0}v → {nv_final}v ({red:.1f}% reduction)")
    return nv0, nf0, nv_final, nf_final


# ═══════════════════════════════════════════════════════════════
#  Phase 3: UV remap (MoF UnWrapConsole3 primary + Smart UV fallback)
# ═══════════════════════════════════════════════════════════════

def _find_mof_exe():
    addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mof_dir = os.path.join(addon_dir, "ministry_of_flat")
    exe_path = os.path.join(mof_dir, "UnWrapConsole3.exe")
    if os.path.exists(exe_path):
        return exe_path
    return None


def _mof_unwrap(mesh_obj, mof_exe):
    mesh_name = mesh_obj.name
    print(f"[P3] [Debug] MoF exe: {mof_exe}")

    tmp_dir = tempfile.mkdtemp(prefix="mof_uv_")
    export_path = os.path.join(tmp_dir, "export.obj")
    import_path = os.path.join(tmp_dir, "import.obj")

    try:
        bpy.ops.object.select_all(action='DESELECT')
        mesh_obj.select_set(True)
        bpy.context.view_layer.objects.active = mesh_obj
        bpy.ops.object.mode_set(mode='OBJECT')

        print(f"[P3] [Debug] Exporting mesh for MoF unwrap...")
        bpy.ops.wm.obj_export(
            filepath=export_path,
            export_selected_objects=True,
            export_materials=False,
            export_normals=True,
            export_uv=False,
            apply_modifiers=False,
            export_triangulated_mesh=False,
        )

        params = [
            "-separate", "0",
            "-aspect", "1.0",
            "-udims", "1",
            "-resolution", "1024",
            "-normals", "0",
            "-overlap", "0",
            "-mirror", "0",
            "-worldscale", "0",
        ]
        full_cmd = [mof_exe, export_path, import_path] + params

        print(f"[P3] [Debug] Running UnWrapConsole3.exe...")
        result = subprocess.run(full_cmd, check=True,
                                capture_output=True, text=True)
        if result.stdout.strip():
            print(f"[P3] [Debug] MoF stdout: {result.stdout.strip()[:200]}")

        existing = set(bpy.data.objects)
        bpy.ops.object.select_all(action='DESELECT')
        bpy.ops.wm.obj_import(filepath=import_path)

        imported = [o for o in bpy.data.objects
                    if o not in existing and o.type == 'MESH']

        if not imported:
            print(f"[P3] [Warning] MoF import produced no objects")
            return False

        imp_obj = imported[0]

        mesh_obj = bpy.data.objects.get(mesh_name)
        if mesh_obj is None:
            print(f"[P3] [Warning] {mesh_name} removed during import — aborting")
            bpy.data.objects.remove(imp_obj, do_unlink=True)
            return False

        print(f"[P3] [Debug] MoF imported: {imp_obj.name} "
              f"({len(imp_obj.data.vertices)}v/{len(imp_obj.data.polygons)}f)")

        if not imp_obj.data.uv_layers:
            print(f"[P3] [Warning] MoF result has no UV layers")
            bpy.data.objects.remove(imp_obj, do_unlink=True)
            return False

        uv_layer = mesh_obj.data.uv_layers.get("Bake_UV")
        if uv_layer is None:
            uv_layer = mesh_obj.data.uv_layers.new(name="Bake_UV")
        mesh_obj.data.uv_layers.active = uv_layer

        num_loops = len(mesh_obj.data.loops)
        num_uvs = len(imp_obj.data.uv_layers.active.data)

        if num_loops == 0 or num_uvs == 0:
            print(f"[P3] [Warning] Empty loops/uvs: {num_loops}L/{num_uvs}UV")
            bpy.data.objects.remove(imp_obj, do_unlink=True)
            return False

        if num_loops != num_uvs:
            print(f"[P3] [Warning] Loop mismatch: orig={num_loops} imp={num_uvs} "
                  f"- falling back to Smart UV Project")
            bpy.data.objects.remove(imp_obj, do_unlink=True)
            if imp_mesh and imp_mesh.users == 0:
                bpy.data.meshes.remove(imp_mesh)
            return False

        print(f"[P3] [Debug] Loop count matches: {num_loops}")

        temp_uvs = np.zeros(num_uvs * 2, dtype=np.float32)
        imp_obj.data.uv_layers.active.data.foreach_get("uv", temp_uvs)
        uvs = temp_uvs[:num_loops * 2].copy()
        uv_layer.data.foreach_set("uv", uvs)
        mesh_obj.data.update()

        u_min = temp_uvs[0::2].min()
        u_max = temp_uvs[0::2].max()
        v_min = temp_uvs[1::2].min()
        v_max = temp_uvs[1::2].max()
        print(f"[P3] [Debug] MoF UV range: U=[{u_min:.4f},{u_max:.4f}] "
              f"V=[{v_min:.4f},{v_max:.4f}]")

        imp_mesh = imp_obj.data
        bpy.data.objects.remove(imp_obj, do_unlink=True)
        if imp_mesh and imp_mesh.users == 0:
            bpy.data.meshes.remove(imp_mesh)

        return True

    except subprocess.CalledProcessError as e:
        print(f"[P3] [Warning] MoF subprocess failed: {e.stderr[:200] if e.stderr else e}")
        return False
    except Exception as e:
        print(f"[P3] [Warning] MoF unwrap error: {e}")
        return False
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass

def _build_merged_material(texture_name="Baked_Diffuse.png", image_size=4096):
    mat = bpy.data.materials.get("Merged_Material")
    if mat is None:
        mat = bpy.data.materials.new(name="Merged_Material")
    mat.use_nodes = True

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    output_node = nodes.new(type='ShaderNodeOutputMaterial')
    output_node.location = (600, 0)

    principled_node = nodes.new(type='ShaderNodeBsdfPrincipled')
    principled_node.location = (200, 0)

    tex_image_node = nodes.new(type='ShaderNodeTexImage')
    tex_image_node.location = (-300, 0)

    uv_map_node = nodes.new(type='ShaderNodeUVMap')
    uv_map_node.location = (-500, 0)
    uv_map_node.uv_map = "Bake_UV"

    links.new(uv_map_node.outputs['UV'], tex_image_node.inputs['Vector'])
    links.new(tex_image_node.outputs['Color'], principled_node.inputs['Base Color'])
    links.new(principled_node.outputs['BSDF'], output_node.inputs['Surface'])

    principled_node.inputs['Roughness'].default_value = 0.8
    principled_node.inputs['Specular IOR Level'].default_value = 0.2

    img = bpy.data.images.get(texture_name)
    if img is None:
        img = bpy.data.images.new(
            name=texture_name,
            width=image_size,
            height=image_size,
            alpha=True,
            float_buffer=False,
        )
    img.colorspace_settings.name = 'sRGB'
    tex_image_node.image = img

    nodes.active = tex_image_node
    tex_image_node.select = True

    return mat, img, tex_image_node



def _separate_face_from_mesh(mesh_obj, face_polys):
    if not face_polys:
        return None, mesh_obj
    vp = mesh_obj.hide_viewport
    mesh_obj.hide_viewport = False
    bpy.ops.object.select_all(action='DESELECT')
    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_obj
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    for pi in face_polys:
        mesh_obj.data.polygons[pi].select = True
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.separate(type='SELECTED')
    bpy.ops.object.mode_set(mode='OBJECT')
    mesh_obj.hide_viewport = vp
    body_obj = mesh_obj
    face_obj = None
    for obj in bpy.context.selected_objects:
        if obj != body_obj:
            face_obj = obj
            break
    return face_obj, body_obj


def phase3_rebuild_uv_and_material(mesh_obj):
    print("\n── [P3] UV Remap & Material Rebuild ──")

    bpy.ops.object.select_all(action='DESELECT')
    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_obj
    bpy.ops.object.mode_set(mode='OBJECT')

    old_mat_count = len(mesh_obj.data.materials)
    total_poly = len(mesh_obj.data.polygons)
    print(f"[P3] Old materials: {old_mat_count}, total polys: {total_poly}")

    face_polys = _find_face_polygons(mesh_obj)
    face_cnt = len(face_polys)
    body_cnt = total_poly - face_cnt
    has_face = bool(face_polys) and face_cnt < total_poly
    has_body = body_cnt > 0

    print(f"[P3] Face={face_cnt} Body={body_cnt}")

    if has_face and has_body:
        print(f"[P3] Preserving face UV + Smart UV on body only")

        face_obj, body_obj = _separate_face_from_mesh(mesh_obj, face_polys)
        face_obj.name = "_temp_face_uv"

        _mark_face_vertex_group(face_obj, set(range(len(face_obj.data.polygons))))

        face_orig_uv = face_obj.data.uv_layers[0] if face_obj.data.uv_layers else None
        face_orig_uv_name = face_orig_uv.name if face_orig_uv else "UVMap"

        for uv in list(body_obj.data.uv_layers):
            body_obj.data.uv_layers.remove(uv)
        body_uv = body_obj.data.uv_layers.new(name="Bake_UV")
        body_uv.active = True

        mof_exe = _find_mof_exe()
        mof_ok = False
        if mof_exe:
            print(f"[P3] [Debug] MoF unwrap on body...")
            mof_ok = _mof_unwrap(body_obj, mof_exe)
            if mof_ok:
                print(f"[P3] [Success] Body MoF UV complete")
            else:
                print(f"[P3] [Warning] Body MoF failed")

        if not mof_ok:
            body_obj = bpy.data.objects.get(body_obj.name)
            if body_obj is None:
                body_obj = bpy.data.objects.get("_lowpoly_TARGET")
            if body_obj:
                bpy.ops.object.select_all(action='DESELECT')
                body_obj.select_set(True)
                bpy.context.view_layer.objects.active = body_obj
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.uv.smart_project(angle_limit=math.radians(66),
                    island_margin=0.006, area_weight=0.0,
                    correct_aspect=True, scale_to_bounds=False)
                bpy.ops.object.mode_set(mode='OBJECT')
                print(f"[P3] [Info] Body Smart UV done (island_margin=0.006)")

        bb = body_obj.data.uv_layers.get("Bake_UV")
        if bb:
            np = _get_np()
            bv = np.zeros(len(body_obj.data.loops) * 2, dtype=np.float32)
            bb.data.foreach_get("uv", bv)
            bv2 = bv.reshape((-1, 2))

            u_min, u_max = float(bv2[:, 0].min()), float(bv2[:, 0].max())
            v_min, v_max = float(bv2[:, 1].min()), float(bv2[:, 1].max())
            u_range = max(u_max - u_min, 1e-6)
            v_range = max(v_max - v_min, 1e-6)

            bv2[:, 0] = (bv2[:, 0] - u_min) / u_range
            bv2[:, 1] = (bv2[:, 1] - v_min) / v_range

            bv2[:, 1] *= _BODY_UV_V_SCALE

            bv2[:, 0] = np.clip(bv2[:, 0], 0.0, 1.0)
            bv2[:, 1] = np.clip(bv2[:, 1], 0.0, _BODY_UV_V_SCALE)

            bb.data.foreach_set("uv", bv2.ravel())
            body_obj.data.update()
            print(f"[P3] Body UV normalized+scaled: U=[0,1] V=[0,{_BODY_UV_V_SCALE:.2f}]")

        if face_orig_uv:
            face_copy = face_obj.data.uv_layers.new(name="Bake_UV")
            face_copy.active = True
            np = _get_np()
            fuv = np.zeros(len(face_obj.data.loops) * 2, dtype=np.float32)
            face_orig_uv.data.foreach_get("uv", fuv)
            face_copy.data.foreach_set("uv", fuv)
            face_obj.data.update()

            fv = np.zeros(len(face_obj.data.loops) * 2, dtype=np.float32)
            face_copy.data.foreach_get("uv", fv)
            fv2 = fv.reshape((-1, 2))

            fv2[:, 0] = np.clip(fv2[:, 0], 0.0, 1.0)
            fv2[:, 1] = np.clip(fv2[:, 1], 0.0, 1.0)

            fv2[:, 1] = fv2[:, 1] * _FACE_UV_V_SCALE + _FACE_UV_V_OFFSET

            fv2[:, 0] = np.clip(fv2[:, 0], 0.0, 1.0)
            fv2[:, 1] = np.clip(fv2[:, 1], _FACE_UV_V_OFFSET, 1.0)

            face_copy.data.foreach_set("uv", fv2.ravel())
            face_obj.data.update()
            print(f"[P3] Face UV clamped+shifted: U=[0,1] "
                  f"V=[{_FACE_UV_V_OFFSET:.2f},1.0] "
                  f"({len(face_obj.data.loops)} loops)")
            print(f"[P3] UV gap: V=[{_BODY_UV_V_SCALE:.2f},{_FACE_UV_V_OFFSET:.2f}] "
                  f"({_UV_REGION_GAP*100:.0f}% padding)")

        body_obj.select_set(True)
        face_obj.select_set(True)
        bpy.context.view_layer.objects.active = body_obj
        bpy.ops.object.join()
        bpy.context.view_layer.update()
        mesh_obj = body_obj
        mesh_obj.name = "_lowpoly_TARGET"
        print(f"[P3] Rejoined - Bake_UV: body Smart UV + face original UV")
        print(f"[P3] _face_protected VG preserved through join")

    elif has_body:
        print(f"[P3] Body only - Smart UV on everything")
        for uv in list(mesh_obj.data.uv_layers):
            mesh_obj.data.uv_layers.remove(uv)
        bake_uv = mesh_obj.data.uv_layers.new(name="Bake_UV")
        bake_uv.active = True
        mof_exe = _find_mof_exe()
        mof_ok = False
        if mof_exe:
            mof_ok = _mof_unwrap(mesh_obj, mof_exe)
        if not mof_ok:
            bpy.ops.object.select_all(action='DESELECT')
            mesh_obj.select_set(True)
            bpy.context.view_layer.objects.active = mesh_obj
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.smart_project(angle_limit=math.radians(66),
                island_margin=0.006, area_weight=0.0,
                correct_aspect=True, scale_to_bounds=False)
            bpy.ops.object.mode_set(mode='OBJECT')

    elif has_face:
        print(f"[P3] Face only - preserving original UV")
        orig_uv = mesh_obj.data.uv_layers[0] if mesh_obj.data.uv_layers else None
        if orig_uv:
            fuv = _get_np().zeros(len(mesh_obj.data.loops) * 2,
                                  dtype=_get_np().float32)
            orig_uv.data.foreach_get("uv", fuv)
        for uv in list(mesh_obj.data.uv_layers):
            mesh_obj.data.uv_layers.remove(uv)
        bake_uv = mesh_obj.data.uv_layers.new(name="Bake_UV")
        bake_uv.active = True
        if orig_uv:
            bake_uv.data.foreach_set("uv", fuv)
            mesh_obj.data.update()
        _mark_face_vertex_group(mesh_obj, set(range(len(mesh_obj.data.polygons))))

    mesh_obj = bpy.data.objects.get(mesh_obj.name)
    if mesh_obj is None:
        mesh_obj = bpy.data.objects.get("_lowpoly_TARGET")
    if mesh_obj is None:
        for o in bpy.data.objects:
            if o.type == 'MESH' and o.name not in ('_highpoly_SOURCE',):
                mesh_obj = o
                break
    if mesh_obj is None:
        print(f"[P3] [Error] No mesh - aborting")
        return None, 0, 0

    merged_mat, merged_img, merged_tex = _build_merged_material()
    mesh_obj.data.materials.clear()
    mesh_obj.data.materials.append(merged_mat)
    for poly in mesh_obj.data.polygons:
        poly.material_index = 0

    print(f"[P3] [Success] Merged_Material created")
    print(f"[P3] UV: {[u.name for u in mesh_obj.data.uv_layers]}")
    has_vg = "_face_protected" in [vg.name for vg in mesh_obj.vertex_groups]
    print(f"[P3] _face_protected VG: {'present' if has_vg else 'absent'}")

    return merged_mat, face_cnt if has_face else 0, body_cnt


# ═══════════════════════════════════════════════════════════════
#  Phase 4: Cycles GPU bake (diffuse color only)
# ═══════════════════════════════════════════════════════════════

def _find_highpoly_face_texture(highpoly_obj):
    face_mat_indices = set()
    for mi, mat in enumerate(highpoly_obj.data.materials):
        if mat is None:
            continue
        name_lower = mat.name.lower()
        for kw in _FACE_KEYWORDS:
            if kw in name_lower:
                face_mat_indices.add(mi)
                break
    for mi in face_mat_indices:
        mat = highpoly_obj.data.materials[mi]
        if mat and mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image and node.image.size[0] > 0:
                    return node.image
    for mat in highpoly_obj.data.materials:
        if mat and mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image and node.image.size[0] > 0:
                    return node.image
    return None


def _rasterize_face_uv_mask(uvs, face_poly_indices, polygons, image_w, image_h):
    _np = _get_np()
    mask_flat = _np.zeros(image_h * image_w, dtype=_np.bool_)
    num_polys = len(polygons)
    for pi in face_poly_indices:
        if pi >= num_polys:
            continue
        poly = polygons[pi]
        ls = poly.loop_start
        le = ls + poly.loop_total
        if le > len(uvs):
            continue
        tri_uvs = uvs[ls:le]
        tri_px = _np.zeros_like(tri_uvs)
        tri_px[:, 0] = tri_uvs[:, 0] * (image_w - 1)
        tri_px[:, 1] = (1.0 - tri_uvs[:, 1]) * (image_h - 1)
        for i in range(1, len(tri_px) - 1):
            v0, v1, v2 = tri_px[0], tri_px[i], tri_px[i + 1]
            min_x = max(0, int(_np.floor(min(v0[0], v1[0], v2[0]))))
            max_x = min(image_w - 1, int(_np.ceil(max(v0[0], v1[0], v2[0]))))
            min_y = max(0, int(_np.floor(min(v0[1], v1[1], v2[1]))))
            max_y = min(image_h - 1, int(_np.ceil(max(v0[1], v1[1], v2[1]))))
            if min_x > max_x or min_y > max_y:
                continue
            xs = _np.arange(min_x, max_x + 1, dtype=_np.float64)
            ys = _np.arange(min_y, max_y + 1, dtype=_np.float64)
            xx, yy = _np.meshgrid(xs, ys)
            denom = ((v1[1] - v2[1]) * (v0[0] - v2[0])
                     + (v2[0] - v1[0]) * (v0[1] - v2[1]))
            if abs(denom) < 1e-10:
                continue
            a = ((v1[1] - v2[1]) * (xx - v2[0])
                 + (v2[0] - v1[0]) * (yy - v2[1])) / denom
            b = ((v2[1] - v0[1]) * (xx - v2[0])
                 + (v0[0] - v2[0]) * (yy - v2[1])) / denom
            inside = (a >= 0) & (b >= 0) & (a + b <= 1)
            yy_i = yy[inside].astype(_np.int32)
            xx_i = xx[inside].astype(_np.int32)
            flat_idx = yy_i * image_w + xx_i
            mask_flat[flat_idx] = True
    return mask_flat


def _copy_face_texture_pixels(lowpoly_obj, highpoly_obj, dst_image,
                               face_poly_indices, face_uv_shifted=True):
    _np = _get_np()
    src_image = _find_highpoly_face_texture(highpoly_obj)
    if src_image is None:
        print(f"[P4] [Warning] No face texture found on highpoly — "
              f"face stays black")
        return 0
    src_w, src_h = src_image.size[0], src_image.size[1]
    dst_w, dst_h = dst_image.size[0], dst_image.size[1]
    print(f"[P4] [Debug] Face copy: src='{src_image.name}' ({src_w}x{src_h}) "
          f"→ dst='{dst_image.name}' ({dst_w}x{dst_h}) "
          f"shifted={face_uv_shifted}")
    if src_image.name == dst_image.name:
        print(f"[P4] [Warning] Source and destination are the same image — "
              f"skipping face copy")
        return 0
    if dst_w <= 0 or dst_h <= 0 or src_w <= 0 or src_h <= 0:
        return 0

    uv_layer = lowpoly_obj.data.uv_layers.get('Bake_UV')
    if uv_layer is None:
        return 0

    total_loops = len(lowpoly_obj.data.loops)
    uvs = _np.zeros(total_loops * 2, dtype=_np.float32)
    uv_layer.data.foreach_get("uv", uvs)
    uvs = uvs.reshape((-1, 2))

    mask_flat = _rasterize_face_uv_mask(
        uvs, face_poly_indices, lowpoly_obj.data.polygons, dst_w, dst_h)
    face_count = int(_np.sum(mask_flat))
    if face_count == 0:
        print(f"[P4] [Warning] Face UV rasterization produced 0 pixels")
        return 0
    total_px = dst_w * dst_h
    print(f"[P4] [Debug] Face mask: {face_count} px / {total_px} total "
          f"({face_count/total_px*100:.1f}%)")

    src_pixels = _np.array(src_image.pixels[:], dtype=_np.float32).reshape(
        (src_h, src_w, -1))
    dst_pixels = _np.array(dst_image.pixels[:], dtype=_np.float32).reshape(
        (dst_h, dst_w, -1))

    dst_ys, dst_xs = _np.where(mask_flat.reshape((dst_h, dst_w)))
    dst_u = dst_xs.astype(_np.float64) / max(1, dst_w - 1)
    dst_v = dst_ys.astype(_np.float64) / max(1, dst_h - 1)
    dst_v = 1.0 - dst_v

    if face_uv_shifted:
        src_u = dst_u
        src_v = (dst_v - _FACE_UV_V_OFFSET) / max(_FACE_UV_V_SCALE, 0.001)
    else:
        src_u = dst_u
        src_v = dst_v

    src_xs = _np.clip((src_u * (src_w - 1)).astype(_np.int32), 0, src_w - 1)
    src_ys = _np.clip(((1.0 - src_v) * (src_h - 1)).astype(_np.int32), 0, src_h - 1)

    channels = min(src_pixels.shape[2], dst_pixels.shape[2])
    dst_pixels[dst_ys, dst_xs, :channels] = src_pixels[src_ys, src_xs, :channels]

    dst_image.pixels.foreach_set(dst_pixels.ravel())
    dst_image.update()

    print(f"[P4] Face pixels copied from '{src_image.name}' "
          f"({src_w}x{src_h}): {face_count} px")
    return face_count


def _setup_cycles_gpu():
    old_engine = bpy.context.scene.render.engine
    old_device = getattr(bpy.context.scene.cycles, 'device', 'CPU')
    old_samples = bpy.context.scene.cycles.samples
    try:
        old_bake_margin = bpy.context.scene.render.bake.margin
    except AttributeError:
        old_bake_margin = 16

    bpy.context.scene.render.engine = 'CYCLES'

    try:
        prefs = bpy.context.preferences.addons['cycles'].preferences
        compute_types = [d[0] for d in prefs.get_device_types(bpy.context)]
    except Exception:
        compute_types = []

    if 'OPTIX' in compute_types:
        bpy.context.scene.cycles.device = 'GPU'
        prefs.compute_device_type = 'OPTIX'
    elif 'CUDA' in compute_types:
        bpy.context.scene.cycles.device = 'GPU'
        prefs.compute_device_type = 'CUDA'
    elif 'HIP' in compute_types:
        bpy.context.scene.cycles.device = 'GPU'
        prefs.compute_device_type = 'HIP'
    else:
        bpy.context.scene.cycles.device = 'CPU'

    bpy.context.scene.cycles.samples = 512
    bpy.context.scene.render.bake.margin = 12
    try:
        bpy.context.scene.render.bake.margin_type = 'EXTEND'
    except AttributeError:
        pass
    try:
        bpy.context.scene.render.bake.cage_extrusion = 0.05
    except AttributeError:
        pass
    try:
        bpy.context.scene.render.bake.max_ray_distance = 0.1
    except AttributeError:
        pass
    try:
        bpy.context.scene.render.bake.use_clear = True
    except AttributeError:
        pass

    return old_engine, old_device, old_samples, old_bake_margin


def _restore_render_settings(old_engine, old_device, old_samples, old_bake_margin):
    bpy.context.scene.render.engine = old_engine
    if old_engine == 'CYCLES':
        bpy.context.scene.cycles.samples = old_samples
        try:
            bpy.context.scene.cycles.device = old_device
        except Exception:
            pass
    try:
        bpy.context.scene.render.bake.margin = old_bake_margin
    except AttributeError:
        pass


def _fix_face_orientation(mesh_obj):
    vp_hidden = mesh_obj.hide_viewport
    mesh_obj.hide_viewport = False
    bpy.ops.object.select_all(action='DESELECT')
    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_obj
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    try:
        bpy.ops.mesh.normals_make_consistent(inside=False)
        print(f"[P4] [Success] Face normals recalculated outside")
    except Exception as e:
        print(f"[P4] [Warning] Normal recalc skipped: {e}")
    bpy.ops.object.mode_set(mode='OBJECT')
    mesh_obj.hide_viewport = vp_hidden


def _select_polys_by_vg(mesh_obj, vg_name):
    """Select polygons on mesh_obj that have vertices in the given vertex group.
    Returns the set of selected polygon indices.
    """
    vg = mesh_obj.vertex_groups.get(vg_name)
    if vg is None:
        return set()
    vg_idx = vg.index
    selected = set()
    vp = mesh_obj.hide_viewport
    mesh_obj.hide_viewport = False

    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_obj

    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')

    for poly in mesh_obj.data.polygons:
        for vi in poly.vertices:
            v = mesh_obj.data.vertices[vi]
            for g in v.groups:
                if g.group == vg_idx and g.weight > 0.0:
                    poly.select = True
                    selected.add(poly.index)
                    break
            else:
                continue
            break

    bpy.ops.object.mode_set(mode='EDIT')
    mesh_obj.hide_viewport = vp
    return selected


def _select_polys_inverse(mesh_obj, exclude_poly_indices):
    """Select all polygons EXCEPT those in exclude_poly_indices."""
    vp = mesh_obj.hide_viewport
    mesh_obj.hide_viewport = False
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')

    selected = set()
    for poly in mesh_obj.data.polygons:
        if poly.index not in exclude_poly_indices:
            poly.select = True
            selected.add(poly.index)

    bpy.ops.object.mode_set(mode='EDIT')
    mesh_obj.hide_viewport = vp
    return selected


def _dilate_baked_region(image, mask_flat, iterations=3):
    """Dilate non-transparent pixels into transparent areas within a mask region.
    This prevents black seams at UV island boundaries in anime models.
    """
    _np = _get_np()
    w, h = image.size[0], image.size[1]
    if w <= 0 or h <= 0:
        return
    pixels = _np.array(image.pixels[:], dtype=_np.float32).reshape((h, w, 4))
    mask_2d = mask_flat.reshape((h, w))

    for _ in range(iterations):
        new_pixels = pixels.copy()
        for dy in [-1, 0, 1]:
            for dx in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                shifted = _np.roll(_np.roll(pixels, dx, axis=1), dy, axis=0)
                shifted_mask = _np.roll(_np.roll(mask_2d, dx, axis=1), dy, axis=0)
                empty = (pixels[:, :, 3] < 0.01) & mask_2d
                can_fill = empty & shifted_mask
                new_pixels[can_fill] = shifted[can_fill]
        pixels = new_pixels

    image.pixels.foreach_set(pixels.ravel())
    image.update()
    print(f"[P4] Dilated baked region ({iterations} iterations)")


def _apply_alpha_from_texture(lowpoly_obj, highpoly_obj, dst_image):
    """Copy alpha channel from the highpoly's textures to the baked image.
    This preserves transparency for hair tips, accessories, etc.
    """
    _np = _get_np()
    dst_w, dst_h = dst_image.size[0], dst_image.size[1]
    if dst_w <= 0 or dst_h <= 0:
        return

    uv_layer = lowpoly_obj.data.uv_layers.get('Bake_UV')
    if uv_layer is None:
        return

    total_loops = len(lowpoly_obj.data.loops)
    uvs = _np.zeros(total_loops * 2, dtype=_np.float32)
    uv_layer.data.foreach_get("uv", uvs)
    uvs = uvs.reshape((-1, 2))

    dst_pixels = _np.array(dst_image.pixels[:], dtype=_np.float32).reshape(
        (dst_h, dst_w, 4))

    for mat in highpoly_obj.data.materials:
        if not mat or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type != 'TEX_IMAGE' or not node.image:
                continue
            src_img = node.image
            if src_img.size[0] <= 0 or src_img.name == dst_image.name:
                continue
            src_w, src_h = src_img.size[0], src_img.size[1]
            src_pixels = _np.array(src_img.pixels[:], dtype=_np.float32).reshape(
                (src_h, src_w, -1))
            if src_pixels.shape[2] < 4:
                continue

            for poly in lowpoly_obj.data.polygons:
                ls = poly.loop_start
                le = ls + poly.loop_total
                if le > len(uvs):
                    continue
                poly_uvs = uvs[ls:le]
                min_u = max(0.0, float(poly_uvs[:, 0].min()) - 0.002)
                max_u = min(1.0, float(poly_uvs[:, 0].max()) + 0.002)
                min_v = max(0.0, float(poly_uvs[:, 1].min()) - 0.002)
                max_v = min(1.0, float(poly_uvs[:, 1].max()) + 0.002)

                x0 = int(min_u * (dst_w - 1))
                x1 = int(max_u * (dst_w - 1)) + 1
                y0 = int((1.0 - max_v) * (dst_h - 1))
                y1 = int((1.0 - min_v) * (dst_h - 1)) + 1
                x0, x1 = max(0, x0), min(dst_w, x1)
                y0, y1 = max(0, y0), min(dst_h, y1)
                if x0 >= x1 or y0 >= y1:
                    continue

                for yy in range(y0, y1):
                    for xx in range(x0, x1):
                        dst_u = xx / max(1, dst_w - 1)
                        dst_v = 1.0 - yy / max(1, dst_h - 1)
                        is_body = dst_v < _BODY_UV_V_SCALE + _UV_REGION_GAP / 2
                        if is_body:
                            src_x = min(src_w - 1, int(dst_u * (src_w - 1)))
                            src_y = min(src_h - 1, int((1.0 - dst_v / _BODY_UV_V_SCALE) * (src_h - 1)))
                            src_a = src_pixels[src_y, src_x, 3]
                            if src_a < dst_pixels[yy, xx, 3]:
                                dst_pixels[yy, xx, 3] = src_a

    dst_image.pixels.foreach_set(dst_pixels.ravel())
    dst_image.update()
    print(f"[P4] Alpha channel transferred from highpoly textures")

def _cleanup_temp_highpoly(hp_face, hp_body, original_obj):
    """清理临时拆分的高模对象，合并回原对象"""
    print("[P4] Restoring highpoly source...")

    # 确定目标对象（通常是 hp_body，或原对象）
    target = hp_body if hp_body and hp_body.name in bpy.data.objects else original_obj
    if not target: return

    # 确保在 Object 模式下操作
    bpy.ops.object.mode_set(mode='OBJECT')

    # 恢复可见性
    target.hide_viewport = False
    target.hide_render = False

    # 清除选择并选中目标对象
    bpy.ops.object.select_all(action='DESELECT')
    target.select_set(True)
    bpy.context.view_layer.objects.active = target

    # 如果有脸部对象，合并回目标
    if hp_face and hp_face.name in bpy.data.objects:
        hp_face.hide_viewport = False
        hp_face.select_set(True)
        try:
            bpy.ops.object.join()
            print("[P4] Highpoly face merged back")
        except Exception as e:
            print(f"[P4] Warning: Merge failed {e}")

    # 恢复目标对象的隐藏状态
    target.name = "_highpoly_SOURCE"
    target.hide_viewport = True
    target.hide_render = True

def phase4_bake(lowpoly_obj, highpoly_obj, face_poly_count, body_poly_count):
    print("\n-- [P4] Diffuse Texture Bake --")
    merged_mat = bpy.data.materials.get("Merged_Material")
    if merged_mat is None or not merged_mat.use_nodes:
        print("[P4] [Warning] No Merged_Material - nothing to bake")
        return True

    if highpoly_obj is None or highpoly_obj.name not in bpy.data.objects:
        print("[P4] [Error] Highpoly source not found - cannot bake")
        return False

    lp_face_polys_vg = _find_face_polys_by_vg(lowpoly_obj, "_face_protected")
    lp_face_polys_uv = _find_face_polys_by_uv(lowpoly_obj)
    lp_face_polys = lp_face_polys_vg | lp_face_polys_uv
    has_face = len(lp_face_polys) > 0
    print(f"[P4] Face polys: VG={len(lp_face_polys_vg)} UV={len(lp_face_polys_uv)} union={len(lp_face_polys)}")
    print(f"[P4] Face pixel copy will use VG-only ({len(lp_face_polys_vg)} polys) to avoid body polys with face UVs")

    _fix_face_orientation(lowpoly_obj)
    (old_engine, old_device, old_samples, old_bake_margin) = _setup_cycles_gpu()

    baked_img = None
    for node in merged_mat.node_tree.nodes:
        if node.type == 'TEX_IMAGE':
            merged_mat.node_tree.nodes.active = node
            node.select = True
            baked_img = node.image
            break

    if baked_img is None:
        print("[P4] [Error] No baked image found")
        _restore_render_settings(old_engine, old_device, old_samples, old_bake_margin)
        return False

    try:
        print("[P4] Baking ALL polys via Cycles (selected_to_active)...")

        np = _get_np()
        clear_pixels = np.zeros(baked_img.size[0] * baked_img.size[1] * 4, dtype=np.float32)
        baked_img.pixels.foreach_set(clear_pixels)
        baked_img.update()
        print(f"[P4] Cleared baked image ({baked_img.size[0]}x{baked_img.size[1]})")

        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')

        highpoly_obj.hide_viewport = False
        highpoly_obj.hide_render = False
        highpoly_obj.select_set(True)

        lowpoly_obj.hide_viewport = False
        lowpoly_obj.select_set(True)
        bpy.context.view_layer.objects.active = lowpoly_obj

        bpy.ops.object.bake(
            type='DIFFUSE', pass_filter={'COLOR'},
            use_selected_to_active=True, margin=12,
            cage_extrusion=0.05, max_ray_distance=0.1,
        )
        print("[P4] Cycles bake complete (all polys)")

        bpy.ops.object.select_all(action='DESELECT')
        highpoly_obj.hide_viewport = True
        highpoly_obj.hide_render = True
        lowpoly_obj.select_set(True)
        bpy.context.view_layer.objects.active = lowpoly_obj

        if has_face:
            print("[P4] Copying face texture pixels (VG-only, native texture overwrite)...")
            face_copied = _copy_face_texture_pixels(
                lowpoly_obj, highpoly_obj,
                baked_img, lp_face_polys_vg, face_uv_shifted=True)

            if face_copied > 0:
                print(f"[P4] [Success] Face texture copied: {face_copied} px")
            else:
                print("[P4] [Warning] Face pixel copy returned 0 - face stays as Cycles bake")

    except Exception as e:
        print(f"[P4] Error: {e}")
        import traceback
        traceback.print_exc()
        bpy.ops.object.mode_set(mode='OBJECT')
        try:
            highpoly_obj.hide_viewport = True
            highpoly_obj.hide_render = True
        except Exception:
            pass
        _restore_render_settings(old_engine, old_device, old_samples, old_bake_margin)
        return False

    _restore_render_settings(old_engine, old_device, old_samples, old_bake_margin)

    pixels = np.array(baked_img.pixels[:]).reshape((-1, 4))
    nb = int(np.sum(np.max(pixels[:, :3], axis=1) > 0.02))
    print(f"[P4] Non-black: {nb}/{len(pixels)} ({nb / len(pixels) * 100:.1f}%)")

    return True


def phase5_cleanup(lowpoly_obj, highpoly_obj):
    print("\n── [P5] Cleanup ──")

    # Delete highpoly source
    highpoly_data = highpoly_obj.data
    highpoly_name = highpoly_obj.name
    try:
        bpy.data.objects.remove(highpoly_obj, do_unlink=True)
        print(f"[P5] [Success] Deleted {highpoly_name}")
    except Exception as e:
        print(f"[P5] [Warning] Could not delete {highpoly_name}: {e}")

    if highpoly_data and highpoly_data.users == 0:
        try:
            bpy.data.meshes.remove(highpoly_data)
            print(f"[P5] [Success] Orphan mesh data cleaned")
        except Exception:
            pass

    # Delete orphan materials
    removed_mats = 0
    for mat in list(bpy.data.materials):
        if mat.users == 0:
            try:
                bpy.data.materials.remove(mat)
                removed_mats += 1
            except Exception:
                pass
    if removed_mats:
        print(f"[P5] [Success] Removed {removed_mats} orphan material(s)")

    # Remove _face_protected vertex group (no longer needed after baking)
    face_vg = lowpoly_obj.vertex_groups.get("_face_protected")
    if face_vg:
        lowpoly_obj.vertex_groups.remove(face_vg)
        print(f"[P5] [Success] Removed _face_protected vertex group")

    # Save Baked_Diffuse.png
    blend_dir = os.path.dirname(bpy.data.filepath) or os.path.expanduser("~")
    for img in bpy.data.images:
        if "Baked_Diffuse" in img.name and img.has_data:
            out_path = os.path.join(blend_dir, "Baked_Diffuse.png")
            try:
                img.file_format = 'PNG'
                img.save_render(out_path)
                print(f"[P5] [Success] Baked_Diffuse.png → {out_path}")
            except Exception:
                try:
                    img.filepath_raw = out_path
                    img.save()
                    print(f"[P5] [Success] Baked_Diffuse.png saved via save()")
                except Exception:
                    pass
            break

    # Final stats
    nv = len(lowpoly_obj.data.vertices)
    nf = len(lowpoly_obj.data.polygons)
    nm = len(lowpoly_obj.data.materials)
    nu = len(lowpoly_obj.data.uv_layers)
    mat_names = [mat.name for mat in lowpoly_obj.data.materials if mat]

    print(f"\n[P5] ── Final Status ──")
    print(f"[P5]     Vertices: {nv} | Faces: {nf}")
    print(f"[P5]     Materials: {nm} → {mat_names}")
    print(f"[P5]     UV layers: {nu} → {[uv.name for uv in lowpoly_obj.data.uv_layers]}")

    print(f"\n[P5] ── Checklist ──")
    print(f"[P5]   □ 面数是否显著降低？ (verts: {nv})")
    print(f"[P5]   □ 单材质+单张贴图2048？ "
          f"({'Yes' if nm == 1 else 'CHECK'})")
    print(f"[P5]   □ 面部分离减面保护")
    # Non-black check
    merged_mat = bpy.data.materials.get("Merged_Material")
    if merged_mat and merged_mat.use_nodes:
        for node in merged_mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                img = node.image
                if img and img.size[0] > 0 and img.size[1] > 0:
                    pixels = _get_np().array(img.pixels[:]).reshape((-1, 4))
                    threshold = 0.02
                    non_black_ct = int(_get_np().sum(
                        _get_np().max(pixels[:, :3], axis=1) > threshold))
                    total_ct = pixels.shape[0]
                    pct = non_black_ct / total_ct * 100
                    print(f"[P5]   □ 烘焙像素非黑率: {pct:.1f}%")
                break


# ═══════════════════════════════════════════════════════════════
#  Main pipeline
# ═══════════════════════════════════════════════════════════════

def run_pipeline(mesh_obj=None):
    if mesh_obj is None:
        mesh_obj = bpy.context.active_object

    if mesh_obj is None or mesh_obj.type != 'MESH':
        print("[Error] No mesh object selected or active")
        return False

    t0 = time.time()
    print("=" * 60)
    print("GEM2 PMX High→LowPoly Texture Bake Pipeline (v2)")
    print("=" * 60)

    highpoly_obj, lowpoly_obj = phase1_backup(mesh_obj)
    nv0, nf0, nv_final, nf_final = phase2_decimate(lowpoly_obj)
    merged_mat, face_poly_count, body_poly_count = phase3_rebuild_uv_and_material(lowpoly_obj)
    bake_ok = phase4_bake(lowpoly_obj, highpoly_obj, face_poly_count, body_poly_count)
    phase5_cleanup(lowpoly_obj, highpoly_obj)

    elapsed = time.time() - t0
    print(f"\n[Pipeline] Total: {elapsed:.1f}s | "
          f"{'ALL PHASES OK' if bake_ok else 'BAKE FAILED'}")

    return bake_ok


def fast_run():
    run_pipeline()


# ═══════════════════════════════════════════════════════════════
#  Blender Operator
# ═══════════════════════════════════════════════════════════════

class GEM2_OT_NativeDecimateBake(bpy.types.Operator):
    bl_idname = "gem2.native_decimate_bake"
    bl_label = "Native Decimate + Bake"
    bl_description = (
        "Face-separate -> body two-pass COLLAPSE -> rejoin -> "
        "UV remap with gap (body Smart UV + face original UV) -> "
        "Cycles body bake + direct face pixel copy -> cleanup"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = getattr(context, 'active_object', None)
        if obj is None:
            return False
        return getattr(obj, 'type', '') == 'MESH'

    def execute(self, context):
        ok = run_pipeline(context.active_object)
        if ok:
            self.report({'INFO'}, "Decimate + Bake pipeline completed")
        else:
            self.report({'WARNING'}, "Pipeline finished with warnings — check console")
        return {'FINISHED'}


CLASSES = (GEM2_OT_NativeDecimateBake,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    fast_run()
