bl_info = {
    "name": "GEM2 GOH Tools",
    "author": "BD5456+VegetaBird+Simon",
    "version": (1, 0, 0),
    "blender": (4, 3, 0),
    "location": "File > Import/Export > GEM2 PLY",
    "description": "GOH (Call to Arms - Gates of Hell) PMX/MMD -> GEM2 skin pipeline. GFA custom skeleton based.",
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
    operators.unregister()
    native_decimate.unregister()
    mowas2_pipeline.unregister()
    vehicle_io.unregister()
