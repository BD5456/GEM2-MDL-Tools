bl_info = {
    "name": "GEM2 Engine Tools",
    "author": "BD5456+VegetaBird+Simon",
    "version": (1, 3, 18),
    "blender": (4, 3, 0),
    "location": "File > Import/Export; 3D View > GEM2 Engine Tools",
    "description": "GEM2 model, animation, FBX export, vehicle conversion, and vanilla GOH DEF tools.",
    "category": "Import-Export",
}

import bpy


# Retain the implementation for regression and possible redesign, but do not
# expose the unreliable experimental workflow in normal add-on registration.
GENERAL_WEIGHT_PROJECTION_ENABLED = False
_REGISTERED = False

_REQUIRED_OPERATORS = (
    ("import_scene", "gem2ply"),
    ("import_scene", "gem2ply_folder"),
    ("import_scene", "gem2vol"),
    ("import_scene", "gem2anm"),
    ("export_scene", "gem2_goh_fbx"),
    ("export_scene", "gem2_multipart"),
    ("export_scene", "gem2anm"),
    ("gem2", "multipart_preflight"),
    ("gem2", "extract_goh_anm"),
    ("gem2", "native_decimate_bake"),
    ("gem2", "mowas2_full_pipeline"),
    ("gem2", "mowas2_human_rest_convert"),
    ("gem2", "mowas2_human_rest_export"),
    ("gem2", "mowas2_import_vehicle_folder"),
    ("gem2", "mowas2_export_vehicle_folder"),
)


def _operator_class(category, name):
    try:
        rna_type = getattr(getattr(bpy.ops, category), name).get_rna_type()
        return bpy.types.Operator.bl_rna_get_subclass_py(rna_type.identifier)
    except (AttributeError, KeyError, RuntimeError):
        return None


def _operator_registered(category, name):
    return _operator_class(category, name) is not None


def _owned_by_this_addon(value):
    return value is not None and value.__module__.startswith(__name__ + ".")


def _registration_surfaces():
    operators = [_operator_registered(category, name)
                 for category, name in _REQUIRED_OPERATORS]
    file_handlers = [
        bpy.types.FileHandler.bl_rna_get_subclass_py(name) is not None
        for name in ("IO_FH_gem2ply", "IO_FH_gem2anm")
    ]
    panel = bpy.types.Panel.bl_rna_get_subclass_py("MOWAS2_PT_panel") is not None
    scene_property = hasattr(bpy.types.Scene, "mowas2_props")
    return operators + file_handlers + [panel, scene_property]


def _registration_complete():
    panel = bpy.types.Panel.bl_rna_get_subclass_py("MOWAS2_PT_panel")
    critical_owners = (
        _operator_class("export_scene", "gem2_goh_fbx"),
        _operator_class("export_scene", "gem2_multipart"),
        _operator_class("gem2", "multipart_preflight"),
        _operator_class("gem2", "extract_goh_anm"),
        _operator_class("gem2", "mowas2_full_pipeline"),
        _operator_class("gem2", "mowas2_human_rest_convert"),
        _operator_class("gem2", "mowas2_human_rest_export"),
        _operator_class("gem2", "mowas2_import_vehicle_folder"),
        panel,
    )
    return all(_registration_surfaces()) and all(
        _owned_by_this_addon(value) for value in critical_owners
    )


def _registration_present():
    panel = bpy.types.Panel.bl_rna_get_subclass_py("MOWAS2_PT_panel")
    owned_surfaces = [
        _operator_class(category, name) for category, name in _REQUIRED_OPERATORS
    ] + [panel]
    return any(_owned_by_this_addon(value) for value in owned_surfaces)


def _suppress_enabled_legacy_menus():
    try:
        if "gem2_mdl_tools" not in bpy.context.preferences.addons:
            return
        import gem2_mdl_tools
        if getattr(gem2_mdl_tools, "DEPRECATED_WORKSPACE_ANCHOR", False):
            return
        from gem2_mdl_tools import operators as legacy_operators
        for menu, callback in (
                (bpy.types.TOPBAR_MT_file_export, legacy_operators._menu_export),
                (bpy.types.TOPBAR_MT_file_import, legacy_operators._menu_import)):
            try:
                menu.remove(callback)
            except (RuntimeError, ValueError):
                pass
    except (AttributeError, ImportError):
        pass


def _restore_enabled_legacy_variant():
    try:
        enabled = "gem2_mdl_tools" in bpy.context.preferences.addons
    except AttributeError:
        enabled = False
    if not enabled:
        return False
    import gem2_mdl_tools
    if getattr(gem2_mdl_tools, "DEPRECATED_WORKSPACE_ANCHOR", False):
        return False
    gem2_mdl_tools.restore_full_registration()
    return True


def register():
    global _REGISTERED
    if _registration_complete():
        _suppress_enabled_legacy_menus()
        _REGISTERED = True
        return
    if _REGISTERED or _registration_present():
        unregister()
    from . import operators
    from . import native_decimate
    from . import general_weight_mapping
    from . import mowas2_pipeline
    from . import vehicle_io  # noqa: F401  (载具按文件夹导入/自动拆分导出)
    from . import anm_io  # noqa: F401  (.anm 解析/应用/导入)
    modules = [operators, native_decimate]
    if GENERAL_WEIGHT_PROJECTION_ENABLED:
        modules.append(general_weight_mapping)
    modules.extend([mowas2_pipeline, vehicle_io])
    attempted = []
    try:
        for module in modules:
            module.register()
            attempted.append(module)
        if not _registration_complete():
            raise RuntimeError("gem2_goh_tools registration completed with missing RNA surfaces")
        _suppress_enabled_legacy_menus()
        _REGISTERED = True
    except Exception as registration_error:
        _REGISTERED = False
        recovery_errors = []
        for module in reversed(attempted):
            try:
                module.unregister()
            except Exception as rollback_error:
                recovery_errors.append(rollback_error)
        try:
            _restore_enabled_legacy_variant()
        except Exception as legacy_error:
            recovery_errors.append(legacy_error)
        if recovery_errors:
            raise ExceptionGroup(
                "GOH GEM2 registration failed and recovery was incomplete",
                [registration_error, *recovery_errors])
        raise


def unregister():
    global _REGISTERED
    from . import operators
    from . import native_decimate
    from . import general_weight_mapping
    from . import mowas2_pipeline
    from . import vehicle_io
    from . import pak_io
    for module in (vehicle_io, mowas2_pipeline, general_weight_mapping,
                   native_decimate, operators):
        try:
            module.unregister()
        except Exception:
            # A simultaneously enabled GEM2 variant may own the same RNA id.
            pass
    pak_io.clear_pak_caches()
    _REGISTERED = False
    try:
        _restore_enabled_legacy_variant()
    except Exception as legacy_error:
        try:
            register()
        except Exception as goh_recovery_error:
            raise ExceptionGroup(
                "Neither GEM2 add-on could recover shared RNA registration",
                [legacy_error, goh_recovery_error])
        raise RuntimeError(
            "GOH GEM2 disable was aborted because the enabled legacy "
            "variant could not be restored; GOH registration was restored"
        ) from legacy_error
