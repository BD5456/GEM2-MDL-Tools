# -*- coding: utf-8 -*-
"""
Texture baking for decimated PMX models.
=========================================
After decimation, the new mesh has face-centre-matched UVs
that approximate the original mapping but may have seams.
Baking projects the original model's textures onto the new
UV layout using Cycles ray casting, producing seamless results.

Usage:
  import bake_textures
  bake_textures.bake(source_mesh, target_mesh, output_dir)
"""

import bpy
import os
import numpy as np
from .i18n import _


def bake(source_mesh_obj, target_mesh_obj, output_dir=None,
         image_size=1024, verbose=True):
    """Bake textures from source mesh to target mesh.

    For each material on the target mesh:
    1. Creates a new image texture
    2. Sets up Cycles bake settings
    3. Bakes diffuse/albedo from source → target

    Args:
        source_mesh_obj: Original high-poly mesh (hidden, used as source)
        target_mesh_obj: Decimated low-poly mesh (active, gets baked textures)
        output_dir: Where to save baked images (default: blend file dir)
        image_size: Resolution of baked textures
    """
    if output_dir is None:
        output_dir = os.path.dirname(bpy.data.filepath) or os.path.expanduser("~")

    # Save current render settings
    old_engine = bpy.context.scene.render.engine
    old_samples = bpy.context.scene.cycles.samples if old_engine == 'CYCLES' else 128

    bpy.context.scene.render.engine = 'CYCLES'
    bpy.context.scene.cycles.samples = 16  # fast preview bake
    bpy.context.scene.cycles.bake.use_pass_direct = False
    bpy.context.scene.cycles.bake.use_pass_indirect = False

    baked_count = 0

    for mat_slot in target_mesh_obj.material_slots:
        mat = mat_slot.material
        if not mat or not mat.use_nodes:
            continue

        # Find the original image texture node
        orig_image = None
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                orig_image = node.image
                break
        if orig_image is None:
            continue

        # Create new image for baking
        img_name = f"{mat.name}_baked"
        baked_img = bpy.data.images.get(img_name)
        if baked_img:
            bpy.data.images.remove(baked_img)
        baked_img = bpy.data.images.new(
            name=img_name,
            width=image_size, height=image_size,
            alpha=True, float_buffer=False)

        # Create image texture node for baking target
        bake_node = mat.node_tree.nodes.new(type='ShaderNodeTexImage')
        bake_node.image = baked_img
        bake_node.select = True
        mat.node_tree.nodes.active = bake_node

        if verbose:
            print(f"  [Bake] {mat.name} → {img_name} ({image_size}x{image_size})")

        # Bake
        try:
            bpy.context.view_layer.objects.active = target_mesh_obj
            target_mesh_obj.select_set(True)
            bpy.ops.object.bake(
                type='DIFFUSE',
                pass_filter={'COLOR'},
                use_selected_to_active=False,
                margin=4,
            )
            baked_count += 1
        except Exception as e:
            if verbose:
                print(_("bake.warn_failed", mat=mat.name, error=e))

        # Disconnect bake node (cleanup — keep original texture setup)
        mat.node_tree.nodes.remove(bake_node)

    # Restore render settings
    bpy.context.scene.render.engine = old_engine
    if old_engine == 'CYCLES':
        bpy.context.scene.cycles.samples = old_samples

    # Save baked images to disk
    saved = 0
    for img in bpy.data.images:
        if '_baked' in img.name and img.has_data:
            path = os.path.join(output_dir, img.name)
            ext = os.path.splitext(img.name)[1] or '.png'
            if not path.endswith('.png'):
                path = path + '.png'
            img.save_render(path)
            saved += 1
            if verbose:
                print(_("bake.saved", path=path))

    if verbose:
        print(_("bake.done", count=baked_count, saved=saved, dir=output_dir))

    return baked_count


def bake_with_duplicate(mesh_obj, output_dir=None, image_size=1024,
                         verbose=True):
    """Convenience: duplicate mesh, rebake textures onto the
    duplicate, return duplicate.  Original is hidden.

    The duplicate gets new UVs (Smart UV Project), then textures
    are baked from the original to the duplicate.
    """
    # Duplicate
    bpy.ops.object.select_all(action='DESELECT')
    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_obj
    bpy.ops.object.duplicate()
    dup = bpy.context.active_object
    mesh_obj.hide_set(True)

    # Smart UV Project on duplicate (clean UV layout for baking)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.smart_project(
        angle_limit=66, island_margin=0.004,
        area_weight=1.0, correct_aspect=True,
        scale_to_bounds=False,
    )
    bpy.ops.object.mode_set(mode='OBJECT')

    # Bake
    bake(mesh_obj, dup, output_dir=output_dir,
         image_size=image_size, verbose=verbose)

    return dup
