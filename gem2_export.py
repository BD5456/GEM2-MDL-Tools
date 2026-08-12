"""
GEM2 export orchestration.
Combines PLY + MDL + MTL export into a single workflow.
"""
import os
from os import path, makedirs
from shutil import copyfile
import bpy
from .i18n import _


def _get_mesh_and_armature():
    all_meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    mesh_obj = max(all_meshes, key=lambda m: len(m.vertex_groups)) if all_meshes else None
    all_arms = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE']
    arm_obj = max(all_arms, key=lambda a: len(a.data.bones)) if all_arms else None
    return mesh_obj, arm_obj


def _copy_textures(output_dir, mesh_obj):
    for mat_slot in mesh_obj.material_slots:
        mat = mat_slot.material
        if not mat or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image:
                img = node.image
                if img.packed_file:
                    dest = os.path.join(output_dir, img.name)
                    with open(dest, "wb") as pf:
                        pf.write(img.packed_file.data)
                else:
                    # 用 bpy.path.abspath 解析 // 相对路径，os.path.isfile 不认 Blender 的 // 约定
                    src = bpy.path.abspath(img.filepath)
                    if src and os.path.isfile(src):
                        dest = os.path.join(output_dir, os.path.basename(src))
                        if not os.path.isfile(dest):
                            copyfile(src, dest)
                    else:
                        print(f"[GEM2] texture missing: {img.filepath}")


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
        from .mdl_io import write_mdl_file
        skin_world = write_mdl_file(mdl_path, arm_obj, mesh_obj, ply_name)

        # .ply
        ply_path = os.path.join(export_dir, ply_name)
        from .ply_io import export_ply
        export_ply(ply_path, mesh_obj, arm_obj, skin_world=skin_world)

        # .vol — collision geometry for meshes ending in .vol or with 'volume' property
        from .ply_io import export_vol
        for obj in bpy.context.scene.objects:
            if obj.type == 'MESH' and obj.name.endswith('.vol'):
                vol_path = os.path.join(export_dir, obj.data.name + '.vol')
                export_vol(vol_path, obj)

        # .mtl
        material_mode = getattr(operator, 'material_mode', 'SIMPLE')
        from .mtl_io import export_mtl
        for mat_slot in mesh_obj.material_slots:
            mat = mat_slot.material
            if not mat:
                continue
            mtl_path = os.path.join(export_dir, mat.name + ".mtl")
            export_mtl(mtl_path, mat, material_mode)

        _copy_textures(export_dir, mesh_obj)

        print(_("export.success", dir=export_dir))
        return {"FINISHED"}

    except Exception as e:
        import traceback
        traceback.print_exc()
        operator.report({"ERROR"}, str(e))
        return {"CANCELLED"}
