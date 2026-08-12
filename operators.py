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

def _menu_export(self, context):
    self.layout.operator(ExportGEM2.bl_idname, text=_("menu.export.mdl"))


OPERATOR_CLASSES = (ImportGEM2PLY, ImportGEM2VOL, ExportGEM2, IO_FH_gem2ply)


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
