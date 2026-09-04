"""
GEM2 export orchestration.
Combines PLY + MDL + MTL export into a single workflow.
"""
import os
from os import path, makedirs
import bpy
from .i18n import _
from .texture_export import (
    TextureStager,
    convert_staged_to_tga,
    image_alpha_profile,
    resolve_material_image,
)


def _same_rna(left, right):
    if left is right:
        return True
    if left is None or right is None:
        return False
    try:
        return int(left.as_pointer()) == int(right.as_pointer())
    except (AttributeError, TypeError, ValueError, ReferenceError):
        return False


def _armature_for_mesh(mesh_obj):
    """Resolve the deforming armature from the mesh, never scene size."""
    modifiers = [modifier for modifier in mesh_obj.modifiers
                 if modifier.type == 'ARMATURE']
    if len(modifiers) > 1:
        raise RuntimeError(
            "Mesh %r has more than one Armature modifier" % mesh_obj.name)
    targets = [modifier.object for modifier in modifiers if modifier.object]
    if any(modifier.object is None for modifier in modifiers):
        raise RuntimeError(
            "Mesh %r has an Armature modifier without an armature object"
            % mesh_obj.name)
    unique = []
    for target in targets:
        if getattr(target, 'type', None) != 'ARMATURE':
            raise RuntimeError(
                "Mesh %r has an Armature modifier targeting a non-armature object"
                % mesh_obj.name)
        if not any(_same_rna(target, item) for item in unique):
            unique.append(target)
    parent = mesh_obj.parent if mesh_obj.parent and mesh_obj.parent.type == 'ARMATURE' else None
    if parent is not None and not any(_same_rna(parent, item) for item in unique):
        unique.append(parent)
    if len(unique) > 1:
        raise RuntimeError(
            "Mesh %r is bound to more than one armature" % mesh_obj.name)
    return unique[0] if unique else None


def _get_mesh_and_armature():
    all_meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if not all_meshes:
        return None, None
    active = getattr(bpy.context.view_layer.objects, 'active', None)
    if active is not None and active.type == 'MESH':
        mesh_obj = active
    else:
        selected = [obj for obj in bpy.context.selected_objects
                    if obj.type == 'MESH']
        pool = selected or all_meshes
        mesh_obj = max(pool, key=lambda m: len(m.vertex_groups))
    return mesh_obj, _armature_for_mesh(mesh_obj)


def _material_key(material):
    try:
        return material.as_pointer()
    except (AttributeError, RuntimeError):
        return id(material)


def _material_semantic_name(material):
    value = material.get("mowas2_material_semantic_name")
    return str(value or getattr(material, "name", "material"))


def _material_source_mode(material, image, stem):
    text = " ".join((
        _material_semantic_name(material),
        getattr(material, "name", ""),
        getattr(image, "name", ""),
        stem,
    )).casefold()
    return "kk" if "cf_" in text or "cf_s_" in text else "mmd"


def _infer_material_alpha_mode(material, image, ref):
    """Use the PMX pipeline's semantic alpha contract for generic exports."""
    # Imported PMX materials generally do not carry gem2_blend_mode yet. Keep
    # one classifier for all export routes so a pupil or transparent garment
    # cannot silently become ``blend none`` in the generic path.
    from . import mowas2_pipeline as pipeline

    semantic = _material_semantic_name(material)
    stem = str(ref.get("stem") or getattr(image, "name", ""))
    profile = image_alpha_profile(image)
    known = profile is not None
    if profile is None:
        profile = {
            "has_alpha": False,
            "partial_ratio": 0.0,
            "transparent_ratio": 0.0,
            "opaque_ratio": 1.0,
            "min_alpha": 1.0,
            "max_alpha": 1.0,
        }
    has_alpha = bool(profile.get("has_alpha"))
    # A semantic pupil/overlay must preserve alpha even if Blender cannot
    # sample a lazily loaded buffer; conversion itself still keeps source data.
    if not known:
        probe = semantic + " " + stem
        if pipeline._is_pupil_layer(probe) or pipeline._is_eye_shadow_layer(probe):
            has_alpha = True
    texture_mode = pipeline._classify_mode(
        stem, has_alpha, mode=_material_source_mode(material, image, stem),
        alpha_profile=profile, material_names=(semantic,))
    inferred = pipeline._material_alpha_mode(
        semantic, stem, texture_mode, has_alpha=has_alpha)
    explicit = str(material.get("gem2_blend_mode", "") or "").lower()
    if explicit in {"none", "blend", "test"}:
        return explicit, profile, known
    material["gem2_blend_mode"] = inferred
    if "gem2_alpha_ref" not in material:
        material["gem2_alpha_ref"] = 127
    return inferred, profile, known


def _stage_material_textures(output_dir, materials):
    """Stage role images and convert them to engine-readable TGA files.

    The returned mapping is keyed by material pointer and contains role refs
    (``diffuse``, ``bump``, ``specular``). It is intentionally shared by the
    generic and multipart exporters so MTL tokens cannot drift from files.
    Alpha mode is inferred from the same semantic classifier used by the PMX
    pipeline when an imported material has no explicit GEM2 mode.
    """
    stager = TextureStager(output_dir)
    refs_by_material = {}
    staged = []
    seen_materials = set()
    for material in materials:
        if not material:
            continue
        key = _material_key(material)
        if key in seen_materials:
            continue
        seen_materials.add(key)
        refs = {}
        for role in ("diffuse", "bump", "specular"):
            image = resolve_material_image(material, role)
            if image is None:
                continue
            label = "%s:%s" % (material.name, role)
            ref = stager.stage(image, source_label=label)
            refs[role] = ref
            if role == "diffuse":
                blend_mode, profile, known = _infer_material_alpha_mode(
                    material, image, ref)
                # Preserve alpha/RGB for blend and test consumers. If the
                # datablock could not be sampled, preserve it conservatively.
                ref["_gem2_preserve_alpha"] = bool(
                    ref.get("_gem2_preserve_alpha", False)
                    or blend_mode in {"blend", "test"}
                    or not known)
                ref["_gem2_diffuse_seen"] = True
                ref["_gem2_alpha_profile"] = profile
            else:
                # Bump/specular maps are not KK diffuse overlays; never run
                # black-RGB filling on them.
                ref["_gem2_non_diffuse_seen"] = True
            staged.append(ref)
        refs_by_material[key] = refs

    for ref in staged:
        if ref.get("_gem2_diffuse_seen"):
            ref["fill_black"] = not ref.get("_gem2_preserve_alpha", False)
        else:
            ref["fill_black"] = False
        ref.pop("_gem2_preserve_alpha", None)
        ref.pop("_gem2_diffuse_seen", None)
        ref.pop("_gem2_non_diffuse_seen", None)
        ref.pop("_gem2_alpha_profile", None)
    convert_staged_to_tga(staged, fill_black_default=False)
    return refs_by_material


def _copy_textures(output_dir, mesh_obj):
    """Compatibility wrapper used by older callers.

    Only images that can be referenced by the exported MTL are staged. Toon
    and sphere helper nodes are deliberately not treated as diffuse textures.
    """
    materials = [slot.material for slot in mesh_obj.material_slots]
    return _stage_material_textures(output_dir, materials)


def gem2_export(output_dir, operator):
    """Main export orchestrator."""
    try:
        mesh_obj, arm_obj = _get_mesh_and_armature()
        if not mesh_obj:
            raise Exception(_("export.no_mesh"))

        if bpy.data.filepath:
            basename = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
        else:
            basename = "untitled"

        export_dir = os.path.join(output_dir, basename)
        makedirs(export_dir, exist_ok=True)

        # .def
        def_file = os.path.join(export_dir, basename + ".def")
        if not os.path.isfile(def_file):
            with open(def_file, "w", encoding="utf-8") as f:
                f.write("{game_entity\n")
                f.write('\t{extension "' + basename + '.mdl"}\n')
                f.write("}\n")

        # .mdl
        ply_name = basename + ".ply"
        mdl_path = os.path.join(export_dir, basename + ".mdl")
        from .mdl_io import write_mdl_file, mesh_parent_local_matrix
        write_mdl_file(mdl_path, arm_obj, mesh_obj, ply_name)

        # .ply: encode below the VolumeView attachment, not with its raw MDL
        # world frame (the latter would apply the basis mirror twice).
        ply_path = os.path.join(export_dir, ply_name)
        from .ply_io import export_ply
        export_ply(
            ply_path, mesh_obj, arm_obj,
            skin_world=mesh_parent_local_matrix(arm_obj))

        # .vol — collision geometry for meshes ending in .vol or with 'volume' property
        from .ply_io import export_vol
        for obj in bpy.context.scene.objects:
            if obj.type == 'MESH' and obj.name.endswith('.vol'):
                vol_path = os.path.join(export_dir, obj.data.name + '.vol')
                export_vol(vol_path, obj)

        # Stage images before writing MTL so every diffuse/bump/specular token
        # names the exact TGA emitted beside the material file.
        material_refs = _copy_textures(export_dir, mesh_obj)

        # .mtl
        material_mode = getattr(operator, 'material_mode', 'SIMPLE')
        from .mtl_io import export_mtl
        seen_materials = set()
        for mat_slot in mesh_obj.material_slots:
            mat = mat_slot.material
            if not mat:
                continue
            key = _material_key(mat)
            if key in seen_materials:
                continue
            seen_materials.add(key)
            mtl_path = os.path.join(export_dir, mat.name + ".mtl")
            export_mtl(
                mtl_path, mat, material_mode,
                texture_names=material_refs.get(key, {}),
            )

        print(_("export.success", dir=export_dir))
        return {"FINISHED"}

    except Exception as e:
        import traceback
        traceback.print_exc()
        operator.report({"ERROR"}, str(e))
        return {"CANCELLED"}
