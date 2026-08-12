bl_info = {
    "name": "GEM2 Engine Tools",
    "author": "AI Assistant",
    "version": (1, 5, 0),
    "blender": (4, 3, 0),
    "location": "File > Import/Export > GEM2 PLY",
    "description": "Import/Export GEM2 Engine PLY / MDL / MTL",
    "category": "Import-Export",
}

import bpy

def register():
    from . import operators
    from . import transfer
    from . import native_decimate
    operators.register()
    transfer.register()
    native_decimate.register()

def unregister():
    from . import operators
    from . import transfer
    from . import native_decimate
    operators.unregister()
    transfer.unregister()
    native_decimate.unregister()
