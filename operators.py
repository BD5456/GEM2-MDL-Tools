"""
Blender operators: Import GEM2 PLY, Export GEM2, and File Handler.
"""
import traceback
import os
import bpy
from bpy.props import StringProperty
from bpy_extras.io_utils import ImportHelper

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


class ImportGEM2PLY(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.gem2ply"
    bl_label = _("operator.import_ply.label")
    bl_options = {'UNDO'}
    filter_glob: StringProperty(default="*.ply", options={'HIDDEN'})

    def invoke(self, context, event):
        paths = _get_paths()
        if paths.get('import') and os.path.isdir(paths['import']):
            self.filepath = os.path.join(paths['import'], "")
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            _set_import_dir(os.path.dirname(self.filepath))
            from .ply_io import import_ply
            import_ply(self.filepath)
            self.report({'INFO'}, _("operator.import_ply.imported", path=self.filepath))
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


class ExportGEM2(bpy.types.Operator):
    bl_idname = "export_scene.gem2"
    bl_label = _("operator.export.label")
    bl_options = {'UNDO'}
    directory: StringProperty(subtype='DIR_PATH')

    material_mode: bpy.props.EnumProperty(
        name=_("operator.export.material_format"),
        items=[
            ('SIMPLE', _("operator.export.simple"), ''),
            ('BUMP', _("operator.export.bump"), ''),
        ],
        default='SIMPLE',
    )

    def invoke(self, context, event):
        paths = _get_paths()
        if paths.get('export') and os.path.isdir(paths['export']):
            self.directory = paths['export']
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        self.layout.prop(self, "material_mode")

    def execute(self, context):
        try:
            from .gem2_export import gem2_export
            if not self.directory:
                self.report({'ERROR'}, _("operator.export.select_dir"))
                return {'CANCELLED'}
            _set_export_dir(self.directory)
            return gem2_export(self.directory, self)
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, _("operator.export.failed", error=e))
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
    self.layout.operator(ImportGEM2PLY.bl_idname, text=_("menu.import.ply"))
    self.layout.operator(ImportGEM2VOL.bl_idname, text=_("menu.import.vol"))
    self.layout.operator(ImportGEM2ANM.bl_idname, text=_("menu.import.anm"))

def _menu_export(self, context):
    self.layout.operator(ExportGEM2.bl_idname, text=_("menu.export.mdl"))
    self.layout.operator(ExportGEM2ANM.bl_idname, text=_("menu.export.anm"))


OPERATOR_CLASSES = (ImportGEM2PLY, ImportGEM2VOL, ImportGEM2ANM, ExportGEM2,
                    ExportGEM2ANM, ExtractGOHANM,
                    IO_FH_gem2ply, IO_FH_gem2anm)


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
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for cls in reversed(OPERATOR_CLASSES):
        bpy.utils.unregister_class(cls)
