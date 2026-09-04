"""
Blender operators: Import GEM2 PLY, Export GEM2, and File Handler.
"""
import traceback
import os
import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from .i18n import _


def _get_paths():
    try:
        from .core import get_paths
        return get_paths()
    except Exception:
        return {'import': '', 'export': ''}

def _set_import_dir(d):
    try:
        from .core import set_import_dir as f
        f(d)
    except Exception:
        pass

def _set_export_dir(d):
    try:
        from .core import set_export_dir as f
        f(d)
    except Exception:
        pass


def _expanded_fbx_selection(context):
    """Include rig dependencies without pulling unrelated scene objects."""
    selected = {
        obj for obj in context.selected_objects
        if obj.type in {'ARMATURE', 'MESH', 'EMPTY'}
    }
    explicit_armatures = {
        obj for obj in selected if obj.type == 'ARMATURE'
    }
    for obj in tuple(selected):
        if obj.type != 'MESH':
            continue
        for modifier in obj.modifiers:
            if modifier.type == 'ARMATURE' and modifier.object:
                selected.add(modifier.object)
    if explicit_armatures:
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            if any(modifier.type == 'ARMATURE' and
                   modifier.object in explicit_armatures
                   for modifier in obj.modifiers):
                selected.add(obj)
    for obj in tuple(selected):
        parent = obj.parent
        while parent and parent.type in {'ARMATURE', 'EMPTY'}:
            selected.add(parent)
            parent = parent.parent
    return selected


class ImportGEM2PLY(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.gem2ply"
    bl_label = _("operator.import_ply.label")
    bl_description = _("operator.import_ply.desc")
    bl_options = {'UNDO'}
    filter_glob: StringProperty(default="*.ply", options={'HIDDEN'})
    auto_multipart: BoolProperty(
        name=_("operator.import_ply.auto_multipart"),
        description=_("operator.import_ply.auto_multipart.desc"),
        default=True)

    def invoke(self, context, event):
        paths = _get_paths()
        if paths.get('import') and os.path.isdir(paths['import']):
            self.filepath = os.path.join(paths['import'], "")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        self.layout.prop(self, "auto_multipart")

    def execute(self, context):
        try:
            _set_import_dir(os.path.dirname(self.filepath))
            from .multipart_import import import_model_from_ply
            summary = import_model_from_ply(
                self.filepath, auto_multipart=self.auto_multipart)
            if summary['part_count'] > 1:
                self.report({'INFO'}, _(
                    "operator.import_ply.imported_multi",
                    parts=summary['part_count'],
                    path=summary['directory']))
            else:
                self.report({'INFO'}, _(
                    "operator.import_ply.imported", path=self.filepath))
            return {'FINISHED'}
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, _("operator.import_ply.failed", error=e))
            return {'CANCELLED'}


class ImportGEM2PLYFolder(bpy.types.Operator):
    bl_idname = "import_scene.gem2ply_folder"
    bl_label = _("operator.import_ply_folder.label")
    bl_description = _("operator.import_ply_folder.desc")
    bl_options = {'UNDO'}

    directory: StringProperty(
        name=_("operator.import_ply_folder.directory"), subtype='DIR_PATH')
    filter_glob: StringProperty(
        default="*.mdl;*.ply", options={'HIDDEN'})

    def invoke(self, context, event):
        paths = _get_paths()
        if paths.get('import') and os.path.isdir(paths['import']):
            self.directory = paths['import']
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            directory = os.path.abspath(bpy.path.abspath(self.directory))
            _set_import_dir(directory)
            from .multipart_import import import_model_folder
            summary = import_model_folder(directory)
            self.report({'INFO'}, _(
                "operator.import_ply_folder.imported",
                parts=summary['part_count'], path=directory))
            return {'FINISHED'}
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, _("operator.import_ply.failed", error=e))
            return {'CANCELLED'}


class ImportGEM2VOL(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.gem2vol"
    bl_label = _("operator.import_vol.label")
    bl_options = {'UNDO'}
    filter_glob: StringProperty(default="*.vol", options={'HIDDEN'})

    def invoke(self, context, event):
        paths = _get_paths()
        if paths.get('import') and os.path.isdir(paths['import']):
            self.filepath = os.path.join(paths['import'], "")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            _set_import_dir(os.path.dirname(self.filepath))
            from .ply_io import import_vol
            import_vol(self.filepath)
            self.report({'INFO'}, _("operator.import_vol.imported", path=self.filepath))
            return {'FINISHED'}
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, _("operator.import_vol.failed", error=e))
            return {'CANCELLED'}


class ExportGEM2FBX(bpy.types.Operator, ExportHelper):
    """Export the current Blender scene or selection as FBX."""
    bl_idname = "export_scene.gem2_goh_fbx"
    bl_label = _("operator.export_fbx.label")
    bl_description = _("operator.export_fbx.desc")
    bl_options = {'REGISTER'}

    filename_ext = ".fbx"
    filter_glob: StringProperty(default="*.fbx", options={'HIDDEN'})
    use_selection: bpy.props.BoolProperty(
        name=_("operator.export_fbx.selection"), default=True)
    bake_animation: bpy.props.BoolProperty(
        name=_("operator.export_fbx.animation"), default=True)
    apply_modifiers: bpy.props.BoolProperty(
        name=_("operator.export_fbx.modifiers"), default=True)
    global_scale: bpy.props.FloatProperty(
        name=_("operator.export_fbx.scale"), default=1.0,
        min=0.0001, soft_max=100.0)

    def invoke(self, context, event):
        paths = _get_paths()
        directory = paths.get('export') or ''
        if not os.path.isdir(directory):
            directory = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else ''
        basename = (os.path.splitext(os.path.basename(bpy.data.filepath))[0]
                    if bpy.data.filepath else "untitled")
        self.filepath = os.path.join(directory, basename + self.filename_ext)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "use_selection")
        layout.prop(self, "bake_animation")
        layout.prop(self, "apply_modifiers")
        layout.prop(self, "global_scale")

    def execute(self, context):
        try:
            filepath = self.filepath
            if not filepath:
                self.report({'ERROR'}, _("operator.export_fbx.select_file"))
                return {'CANCELLED'}
            if not filepath.lower().endswith(self.filename_ext):
                filepath += self.filename_ext
            original_selected = set(context.selected_objects)
            original_active = context.view_layer.objects.active
            if self.use_selection:
                export_selection = _expanded_fbx_selection(context)
                if not export_selection:
                    self.report(
                        {'ERROR'}, _("operator.export_fbx.no_selection"))
                    return {'CANCELLED'}
                for obj in context.view_layer.objects:
                    obj.select_set(obj in export_selection)
                if original_active not in export_selection:
                    context.view_layer.objects.active = next(
                        (obj for obj in export_selection
                         if obj.type == 'ARMATURE'),
                        next(iter(export_selection)))
            os.makedirs(os.path.dirname(os.path.abspath(filepath)),
                        exist_ok=True)
            _set_export_dir(os.path.dirname(os.path.abspath(filepath)))
            try:
                result = bpy.ops.export_scene.fbx(
                    filepath=filepath,
                    use_selection=self.use_selection,
                    object_types={'ARMATURE', 'MESH', 'EMPTY'},
                    global_scale=self.global_scale,
                    apply_unit_scale=True,
                    apply_scale_options='FBX_SCALE_NONE',
                    use_space_transform=True,
                    bake_space_transform=False,
                    use_mesh_modifiers=self.apply_modifiers,
                    use_mesh_modifiers_render=self.apply_modifiers,
                    use_triangles=False,
                    use_custom_props=True,
                    add_leaf_bones=False,
                    primary_bone_axis='Y',
                    secondary_bone_axis='X',
                    bake_anim=self.bake_animation,
                    bake_anim_use_all_bones=True,
                    bake_anim_use_nla_strips=False,
                    bake_anim_use_all_actions=False,
                    bake_anim_simplify_factor=0.0,
                    path_mode='AUTO',
                    embed_textures=False,
                    axis_forward='-Z',
                    axis_up='Y',
                )
            finally:
                if self.use_selection:
                    for obj in context.view_layer.objects:
                        obj.select_set(obj in original_selected)
                    if (original_active and
                            original_active.name in context.view_layer.objects):
                        context.view_layer.objects.active = original_active
            if 'FINISHED' in result:
                self.report({'INFO'}, _(
                    "operator.export_fbx.done", file=filepath))
            return result
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, _("operator.export.failed", error=exc))
            return {'CANCELLED'}


class GEM2_OT_MultipartPreflight(bpy.types.Operator):
    bl_idname = "gem2.multipart_preflight"
    bl_label = _("operator.export_multipart.preflight")
    bl_description = _("operator.export_multipart.preflight.desc")
    bl_options = {'REGISTER'}

    use_selection: bpy.props.BoolProperty(default=True, options={'HIDDEN'})
    record_limit: bpy.props.IntProperty(default=65535, min=3, max=65535)

    def execute(self, context):
        try:
            from .multipart_export import analyze_selected_model
            summary = analyze_selected_model(
                context,
                use_selection=self.use_selection,
                record_limit=self.record_limit,
            )
            message = _(
                "operator.export_multipart.preflight.done",
                meshes=summary['source_meshes'],
                triangles=summary['triangles'],
                records=summary['total_records'],
                parts=summary['required_parts'],
                limit=summary['record_limit'])
            truncated = summary['truncated_influence_vertices']
            if truncated:
                message += " | " + _(
                    "operator.export_multipart.top2_warning",
                    count=truncated)
            props = getattr(context.scene, 'mowas2_props', None)
            if props is not None:
                props.report = message
            print('[multipart-preflight]', summary)
            self.report({'WARNING'} if truncated else {'INFO'}, message)
            return {'FINISHED'}
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, _("operator.export.failed", error=exc))
            return {'CANCELLED'}


class ExportGEM2Multipart(bpy.types.Operator, ExportHelper):
    """Export selected imported meshes as one lossless multipart GEM2 model."""

    bl_idname = "export_scene.gem2_multipart"
    bl_label = _("operator.export_multipart.label")
    bl_description = _("operator.export_multipart.desc")
    bl_options = {'REGISTER'}

    filename_ext = ".mdl"
    filter_glob: StringProperty(default="*.mdl", options={'HIDDEN'})
    use_selection: bpy.props.BoolProperty(
        name=_("operator.export_multipart.selection"), default=True)
    record_limit: bpy.props.IntProperty(
        name=_("operator.export_multipart.limit"),
        description=_("operator.export_multipart.limit.desc"),
        default=65535, min=3, max=65535)
    material_mode: bpy.props.EnumProperty(
        name=_("operator.export.material_format"),
        items=(
            ('SIMPLE', _("operator.export.simple"), ''),
            ('BUMP', _("operator.export.bump"), ''),
        ),
        default='SIMPLE')
    copy_textures: bpy.props.BoolProperty(
        name=_("operator.export_multipart.textures"), default=True)

    def invoke(self, context, event):
        paths = _get_paths()
        directory = paths.get('export') or ''
        if not os.path.isdir(directory):
            directory = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else ''
        active = context.active_object
        basename = (active.name if active and active.type == 'MESH'
                    else os.path.splitext(os.path.basename(bpy.data.filepath))[0]
                    if bpy.data.filepath else 'model')
        if directory:
            self.filepath = os.path.join(directory, basename + self.filename_ext)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "use_selection")
        layout.prop(self, "record_limit")
        layout.prop(self, "material_mode")
        layout.prop(self, "copy_textures")

    def execute(self, context):
        try:
            if not self.filepath:
                self.report({'ERROR'}, _("operator.export.select_dir"))
                return {'CANCELLED'}
            _set_export_dir(os.path.dirname(self.filepath))
            from .multipart_export import export_selected_model
            summary = export_selected_model(
                context,
                self.filepath,
                use_selection=self.use_selection,
                record_limit=self.record_limit,
                material_mode=self.material_mode,
                copy_textures=self.copy_textures,
            )
            print('[multipart-export]', summary)
            if summary['truncated_influence_vertices']:
                self.report({'WARNING'}, _(
                    "operator.export_multipart.top2_warning",
                    count=summary['truncated_influence_vertices']))
            self.report({'INFO'}, _(
                "operator.export_multipart.done",
                parts=len(summary['parts']),
                records=summary['total_records'],
                dir=summary['output_dir']))
            return {'FINISHED'}
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, _("operator.export.failed", error=exc))
            return {'CANCELLED'}


class ImportGEM2ANM(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.gem2anm"
    bl_label = _("anm.import.label")
    bl_description = _("anm.import.desc")
    bl_options = {'UNDO'}
    filter_glob: StringProperty(default="*.anm", options={'HIDDEN'})

    apply_only: bpy.props.BoolProperty(
        name=_("anm.import.apply_only"),
        description=_("anm.import.apply_only.desc"),
        default=False,
    )

    def invoke(self, context, event):
        # 拖放 (FileHandler) 路径: filepath/files 已由 Blender 设置 → 免对话框直接执行
        dropped = self.filepath or (getattr(self, 'files', None) and len(self.files))
        if dropped:
            return self.execute(context)
        if context.object is None or context.object.type != 'ARMATURE':
            self.report({'ERROR'}, _("anm.err.select_armature"))
            return {'CANCELLED'}
        paths = _get_paths()
        if paths.get('import') and os.path.isdir(paths['import']):
            self.filepath = os.path.join(paths['import'], "")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        self.layout.prop(self, "apply_only")

    def execute(self, context):
        try:
            from .anm_io import AnmData, apply_anm_frame, import_anm_action
            if getattr(self, 'files', None) and len(self.files):
                path = self.files[0].path if hasattr(self.files[0], 'path') else self.filepath
            else:
                path = self.filepath
            if not path:
                self.report({'ERROR'}, _("anm.err.no_file"))
                return {'CANCELLED'}
            if context.object is None or context.object.type != 'ARMATURE':
                self.report({'ERROR'}, _("anm.err.select_armature"))
                return {'CANCELLED'}
            arm = context.object
            anm = AnmData(path)
            if self.apply_only:
                first_frame = min(anm.frames.keys()) if anm.frames else 0
                info = apply_anm_frame(arm, anm, first_frame)
                self.report({'INFO'}, _("anm.info.frame0",
                            file=os.path.basename(path), applied=info['applied'],
                            frames=anm.frame_max + 1, bones=len(anm.bones)))
            else:
                _anm, action = import_anm_action(arm, path)
                self.report({'INFO'}, _("anm.info.action",
                            action=action.name, frames=anm.frame_max + 1,
                            bones=len(anm.bones)))
            return {'FINISHED'}
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, _("anm.err.import_failed", error=e))
            return {'CANCELLED'}


class ExportGEM2ANM(bpy.types.Operator, ImportHelper):
    bl_idname = "export_scene.gem2anm"
    bl_label = _("anm.export.label")
    bl_description = _("anm.export.desc")
    bl_options = {'UNDO'}
    filter_glob: StringProperty(default="*.anm", options={'HIDDEN'})
    filename_ext = ".anm"

    use_action: bpy.props.BoolProperty(
        name=_("anm.export.use_action"),
        description=_("anm.export.use_action.desc"),
        default=True,
    )

    def invoke(self, context, event):
        if context.object is None or context.object.type != 'ARMATURE':
            self.report({'ERROR'}, _("anm.err.select_armature"))
            return {'CANCELLED'}
        paths = _get_paths()
        if paths.get('export') and os.path.isdir(paths['export']):
            self.filepath = os.path.join(paths['export'], "pose.anm")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        self.layout.prop(self, "use_action")

    def execute(self, context):
        if context.object is None or context.object.type != 'ARMATURE':
            self.report({'ERROR'}, _("anm.err.select_armature"))
            return {'CANCELLED'}
        try:
            from .anm_io import write_anm
            arm = context.object
            if not self.filepath:
                raise ValueError(_("anm.err.export_path"))
            action = None
            if self.use_action and arm.animation_data and arm.animation_data.action:
                action = arm.animation_data.action
            path = self.filepath
            if not path.lower().endswith('.anm'):
                path += '.anm'
            n_bones, n_frames = write_anm(path, arm, action)
            self.report({'INFO'}, _("anm.info.exported", file=os.path.basename(path),
                                    bones=n_bones, frames=n_frames))
            return {'FINISHED'}
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, _("anm.err.export_failed", error=e))
            return {'CANCELLED'}


class IO_FH_gem2anm(bpy.types.FileHandler):
    bl_idname = "IO_FH_gem2anm"
    bl_label = _("anm.file_handler.label")
    bl_import_operator = "import_scene.gem2anm"
    bl_file_extensions = ".anm"

    @classmethod
    def poll_drop(cls, context):
        return (context.area and context.area.type == 'VIEW_3D')


class ExtractGOHANM(bpy.types.Operator):
    """GOH 版 (2026-08-18): 从 GOH properties.pak 一键提取 .anm。

    GOH 人形动画在 <game>\\resource\\properties.pak (zip) 的
    properties/animation/human/ 下 (1599 个), 该 pak 1.4MB~4GB 手动解包
    不便。本算子按关键词从 pak 内提取 .anm 到输出目录, 之后用
    现有 .anm 导入 (File > Import > GEM2 ANM) 测试动画。
    """
    bl_idname = "gem2.extract_goh_anm"
    bl_label = _("anm.extract_goh.label")
    bl_description = _("anm.extract_goh.desc")
    bl_options = {'REGISTER'}

    filepath: StringProperty(subtype='FILE_PATH')  # pak 路径
    keyword: StringProperty(name=_("anm.extract_goh.keyword"),
                            description=_("anm.extract_goh.keyword.desc"),
                            default='idle')
    limit: bpy.props.IntProperty(name=_("anm.extract_goh.limit"),
                                 description=_("anm.extract_goh.limit.desc"),
                                 default=50, min=1, max=2000)
    out_dir: StringProperty(name=_("anm.extract_goh.outdir"),
                            subtype='DIR_PATH', default="")

    def invoke(self, context, event):
        # 默认指向 GOH properties.pak (若本机装了 GOH)
        for cand in (
            r'D:\SteamLibrary\steamapps\common\Call to Arms - Gates of Hell\resource\properties.pak',
            r'E:\SteamLibrary\steamapps\common\Call to Arms - Gates of Hell\resource\properties.pak',
            r'C:\Program Files (x86)\Steam\steamapps\common\Call to Arms - Gates of Hell\resource\properties.pak',
        ):
            if os.path.isfile(cand):
                self.filepath = cand
                break
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "keyword")
        layout.prop(self, "limit")
        layout.prop(self, "out_dir")

    def execute(self, context):
        try:
            from .anm_io import list_goh_anm_in_pak, extract_goh_anm
            if not self.filepath or not os.path.isfile(self.filepath):
                self.report({'ERROR'}, _("anm.extract_goh.err_pak"))
                return {'CANCELLED'}
            entries = list_goh_anm_in_pak(self.filepath, self.keyword)
            if not entries:
                self.report({'ERROR'}, _("anm.extract_goh.err_none",
                                         kw=self.keyword))
                return {'CANCELLED'}
            out = self.out_dir or os.path.join(
                os.path.dirname(os.path.abspath(self.filepath)), 'anm_extracted')
            res = extract_goh_anm(self.filepath, self.keyword, out,
                                  limit=self.limit)
            self.report({'INFO'}, _("anm.extract_goh.done",
                                    n=len(res), total=len(entries),
                                    out=out))
            return {'FINISHED'}
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, _("anm.extract_goh.err", error=e))
            return {'CANCELLED'}


class IO_FH_gem2ply(bpy.types.FileHandler):
    bl_idname = "IO_FH_gem2ply"
    bl_label = _("file_handler.label")
    bl_import_operator = "import_scene.gem2ply"
    bl_file_extensions = ".ply"

    @classmethod
    def poll_drop(cls, context):
        return (context.area and context.area.type == 'VIEW_3D')


def _menu_import(self, context):
    self.layout.operator(
        ImportGEM2PLYFolder.bl_idname, text=_("menu.import.ply_folder"))
    self.layout.operator(ImportGEM2PLY.bl_idname, text=_("menu.import.ply"))
    self.layout.operator(ImportGEM2VOL.bl_idname, text=_("menu.import.vol"))
    self.layout.operator(ImportGEM2ANM.bl_idname, text=_("menu.import.anm"))

def _menu_export(self, context):
    self.layout.operator(
        ExportGEM2Multipart.bl_idname, text=_("menu.export.multipart"))
    self.layout.operator(ExportGEM2FBX.bl_idname, text=_("menu.export.fbx"))
    self.layout.operator(ExportGEM2ANM.bl_idname, text=_("menu.export.anm"))


OPERATOR_CLASSES = (ImportGEM2PLY, ImportGEM2PLYFolder,
                     ImportGEM2VOL, ImportGEM2ANM,
                    GEM2_OT_MultipartPreflight, ExportGEM2Multipart,
                    ExportGEM2FBX, ExportGEM2ANM,
                    ExtractGOHANM, IO_FH_gem2ply, IO_FH_gem2anm)


def register():
    print(_("gem2.ops.registering"))
    for cls in OPERATOR_CLASSES:
        try:
            bpy.utils.register_class(cls)
            print(_("gem2.ops.registered", **{"class": cls.__name__}))
        except Exception as e:
            print(_("gem2.ops.failed", **{"class": cls.__name__, "error": e}))
            traceback.print_exc()
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)
    print(_("gem2.ops.menu_added"))


def unregister():
    for menu, callback in (
            (bpy.types.TOPBAR_MT_file_export, _menu_export),
            (bpy.types.TOPBAR_MT_file_import, _menu_import)):
        try:
            menu.remove(callback)
        except (RuntimeError, ValueError):
            pass
    for cls in reversed(OPERATOR_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            # Another enabled GEM2 variant may have replaced the same RNA id.
            pass
