bl_info = {
    "name": "GEM2 Engine Tools",
    "author": "BD5456+VegetaBird+Simon",
    "version": (1, 1, 1),
    "blender": (4, 3, 0),
    "location": "File > Import/Export; 3D View > GEM2 Engine Tools",
    "description": "GOH/MOWAS2 GEM2 pipeline with FBX export, directional vehicle conversion, and vanilla GOH DEF generation.",
    "category": "Import-Export",
}

import bpy

def register():
    from . import operators
    from . import native_decimate
    from . import mowas2_pipeline
    from . import vehicle_io  # noqa: F401  (载具按文件夹导入/自动拆分导出)
    from . import anm_io  # noqa: F401  (.anm 解析/应用/导入)
    operators.register()
    native_decimate.register()
    mowas2_pipeline.register()
    vehicle_io.register()

def unregister():
    from . import operators
    from . import native_decimate
    from . import mowas2_pipeline
    from . import vehicle_io
    from . import pak_io
    for module in (operators, native_decimate, mowas2_pipeline, vehicle_io):
        try:
            module.unregister()
        except RuntimeError:
            # A simultaneously enabled GEM2 variant may own the same RNA id.
            pass
    pak_io.clear_pak_caches()
