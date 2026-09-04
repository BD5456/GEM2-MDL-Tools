"""Deprecated generic weight projection retained for regression and redesign."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import traceback
from contextlib import contextmanager

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from .general_weight_core import (
    clean_weight_vector,
    stable_signature,
    weighted_coverage,
)
from .i18n import _


_EPSILON = 1.0e-10
_MAX_ESTIMATED_BYTES = 512 * 1024 * 1024
_SPATIAL_SOURCE_LIMIT = 100000
_SPATIAL_SAMPLE_LIMIT = 4096
_BLOCKER_I18N = {
    'multi_user': 'general.blocker.multi_user',
    'topology_mismatch': 'general.blocker.topology_mismatch',
    'no_source_surface': 'general.blocker.no_source_surface',
    'memory_limit': 'general.blocker.memory_limit',
    'invalid_source_armature': 'general.blocker.invalid_source_armature',
    'invalid_target_armature': 'general.blocker.invalid_target_armature',
    'posed_target_armature': 'general.blocker.posed_target_armature',
}


def _poll_mesh(_self, obj):
    return obj is not None and obj.type == 'MESH'


def _clear_preview(settings):
    settings.preview_fingerprint = ''
    settings.preview_json = ''


def _setting_changed(self, _context):
    _clear_preview(self)


def _object_matrix_signature(obj):
    return [round(float(value), 9) for row in obj.matrix_world for value in row]


def _geometry_signature(obj):
    """Hash every base-mesh coordinate and topology index efficiently."""
    import numpy as np

    mesh = obj.data
    geometry_digest = hashlib.sha256()
    topology_digest = hashlib.sha256()

    coordinates = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    if coordinates.size:
        mesh.vertices.foreach_get('co', coordinates)
        geometry_digest.update(coordinates.tobytes())

    edges = np.empty(len(mesh.edges) * 2, dtype=np.int32)
    if edges.size:
        mesh.edges.foreach_get('vertices', edges)
        topology_digest.update(edges.tobytes())

    loop_vertices = np.empty(len(mesh.loops), dtype=np.int32)
    if loop_vertices.size:
        mesh.loops.foreach_get('vertex_index', loop_vertices)
        topology_digest.update(loop_vertices.tobytes())

    polygon_sizes = np.empty(len(mesh.polygons), dtype=np.int32)
    if polygon_sizes.size:
        mesh.polygons.foreach_get('loop_total', polygon_sizes)
        topology_digest.update(polygon_sizes.tobytes())

    geometry_digest.update(topology_digest.digest())
    return {
        'vertices': len(mesh.vertices),
        'edges': len(mesh.edges),
        'polygons': len(mesh.polygons),
        'loops': len(mesh.loops),
        'topology_sha256': topology_digest.hexdigest(),
        'geometry_sha256': geometry_digest.hexdigest(),
    }


def _scan_group_stats(obj):
    groups = list(obj.vertex_groups)
    names = {group.index: group.name for group in groups}
    mass = {group.name: 0.0 for group in groups}
    counts = {group.name: 0 for group in groups}
    weighted_vertices = 0
    assignment_count = 0
    max_influences = 0
    digest = hashlib.sha256()
    for vertex in obj.data.vertices:
        influences = 0
        for assignment in vertex.groups:
            name = names.get(assignment.group)
            weight = float(assignment.weight)
            if name is None or weight <= _EPSILON:
                continue
            mass[name] += weight
            counts[name] += 1
            assignment_count += 1
            influences += 1
            digest.update(struct.pack('<IIf', int(vertex.index),
                                      int(assignment.group), weight))
        if influences:
            weighted_vertices += 1
        max_influences = max(max_influences, influences)
    weighted_groups = [group.name for group in groups
                       if mass[group.name] > _EPSILON]
    return {
        'group_order': [group.name for group in groups],
        'weighted_groups': weighted_groups,
        'mass': mass,
        'counts': counts,
        'weighted_vertices': weighted_vertices,
        'unweighted_vertices': len(obj.data.vertices) - weighted_vertices,
        'assignment_count': assignment_count,
        'max_influences': max_influences,
        'weight_sha256': digest.hexdigest(),
    }


def _pose_deviation(pose_bone):
    pose_matrix = pose_bone.matrix
    rest_matrix = pose_bone.bone.matrix_local
    return max(abs(float(pose_matrix[row][column]
                         - rest_matrix[row][column]))
               for row in range(4) for column in range(4))


def _armature_diagnostics(obj, group_names):
    """Describe invalid modifiers and poses that transferred weights would use."""
    relevant = set(group_names)
    invalid = []
    posed = []
    signature = []
    for modifier in obj.modifiers:
        if modifier.type != 'ARMATURE':
            continue
        armature = modifier.object
        record = {
            'modifier': modifier.name,
            'show_viewport': bool(modifier.show_viewport),
            'use_vertex_groups': bool(getattr(modifier, 'use_vertex_groups', True)),
            'use_bone_envelopes': bool(getattr(modifier, 'use_bone_envelopes', False)),
            'vertex_group': str(getattr(modifier, 'vertex_group', '')),
            'invert_vertex_group': bool(getattr(modifier, 'invert_vertex_group', False)),
            'preserve_volume': bool(getattr(modifier, 'use_deform_preserve_volume', False)),
            'use_multi_modifier': bool(getattr(modifier, 'use_multi_modifier', False)),
            'armature_pointer': int(armature.as_pointer()) if armature else 0,
            'armature': armature.name if armature else '',
            'armature_type': armature.type if armature else '',
        }
        effective = (record['show_viewport']
                     and (record['use_vertex_groups']
                          or record['use_bone_envelopes']))
        if armature is None or armature.type != 'ARMATURE':
            if effective:
                invalid.append({
                    'modifier': modifier.name,
                    'object': armature.name if armature else '',
                })
            signature.append(record)
            continue

        record['armature_matrix'] = _object_matrix_signature(armature)
        record['pose_position'] = armature.data.pose_position
        bone_state = []
        changed = []
        maximum = 0.0
        for name in sorted(relevant):
            pose_bone = armature.pose.bones.get(name)
            if pose_bone is None:
                continue
            basis_values = [round(float(value), 9)
                            for row in pose_bone.matrix_basis for value in row]
            pose_values = [round(float(value), 9)
                           for row in pose_bone.matrix for value in row]
            rest_values = [round(float(value), 9)
                           for row in pose_bone.bone.matrix_local for value in row]
            bone_state.append((name, bool(pose_bone.bone.use_deform),
                               basis_values, pose_values, rest_values))
            deviation = _pose_deviation(pose_bone)
            if pose_bone.bone.use_deform and deviation > 1.0e-6:
                changed.append(name)
                maximum = max(maximum, deviation)
        record['bones'] = bone_state
        signature.append(record)
        if (modifier.show_viewport
                and record['use_vertex_groups']
                and armature.data.pose_position == 'POSE'
                and changed):
            posed.append({
                'modifier': modifier.name,
                'armature': armature.name,
                'changed_bones': changed,
                'maximum_deviation': maximum,
            })
    return {'invalid': invalid, 'posed': posed, 'signature': signature}


def _has_invalid_armature_modifier(obj):
    return bool(_armature_diagnostics(obj, ())['invalid'])


def _validate_settings(settings, context, require_writable=False):
    source = settings.source_mesh
    target = settings.target_mesh
    if source is None or source.type != 'MESH':
        raise RuntimeError(_('general.err.source_mesh'))
    if target is None or target.type != 'MESH':
        raise RuntimeError(_('general.err.target_mesh'))
    if source is target:
        raise RuntimeError(_('general.err.same_mesh'))
    if require_writable:
        if target.library is not None or target.data.library is not None:
            raise RuntimeError(_('general.err.read_only'))
        if target.data.users != 1:
            raise RuntimeError(_('general.err.multi_user'))
        if target.name not in context.view_layer.objects:
            raise RuntimeError(_('general.err.target_not_visible'))
    return source, target


def _world_bbox_center(obj):
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    minimum = Vector((min(point.x for point in corners),
                      min(point.y for point in corners),
                      min(point.z for point in corners)))
    maximum = Vector((max(point.x for point in corners),
                      max(point.y for point in corners),
                      max(point.z for point in corners)))
    return (minimum + maximum) * 0.5


def _bbox_spatial_summary(source, target):
    source_dimensions = Vector(source.dimensions)
    target_dimensions = Vector(target.dimensions)
    ratios = []
    for source_value, target_value in zip(source_dimensions, target_dimensions):
        small = max(min(abs(source_value), abs(target_value)), 1.0e-8)
        large = max(abs(source_value), abs(target_value))
        ratios.append(float(large / small))
    diagonal = max(float(target_dimensions.length), 1.0e-8)
    center_distance = float((_world_bbox_center(source)
                             - _world_bbox_center(target)).length)
    return {
        'dimension_ratio': max(ratios),
        'center_distance': center_distance,
        'center_distance_ratio': center_distance / diagonal,
        'target_diagonal': diagonal,
    }


def _surface_distance_summary(source, target):
    summary = _bbox_spatial_summary(source, target)
    summary.update({'sample_count': 0, 'median': None, 'p95': None,
                    'maximum': None, 'skipped': ''})
    if (len(source.data.vertices) > _SPATIAL_SOURCE_LIMIT
            or len(source.data.polygons) > _SPATIAL_SOURCE_LIMIT * 2):
        summary['skipped'] = 'source_too_large'
        return summary
    if not source.data.polygons or not target.data.vertices:
        summary['skipped'] = 'no_surface'
        return summary

    source_vertices = [source.matrix_world @ vertex.co
                       for vertex in source.data.vertices]
    source_polygons = [tuple(polygon.vertices)
                       for polygon in source.data.polygons]
    bvh = BVHTree.FromPolygons(source_vertices, source_polygons,
                               all_triangles=False)
    target_count = len(target.data.vertices)
    step = max(1, math.ceil(target_count / _SPATIAL_SAMPLE_LIMIT))
    distances = []
    for index in range(0, target_count, step):
        vertex = target.data.vertices[index]
        found = bvh.find_nearest(target.matrix_world @ vertex.co)
        if found is not None and found[3] is not None:
            distances.append(float(found[3]))
    if not distances:
        summary['skipped'] = 'nearest_failed'
        return summary
    distances.sort()
    summary.update({
        'sample_count': len(distances),
        'median': distances[len(distances) // 2],
        'p95': distances[min(len(distances) - 1,
                             int((len(distances) - 1) * 0.95))],
        'maximum': distances[-1],
    })
    return summary


def _estimated_peak_bytes(source, target, target_stats, group_names):
    vertex_group_cells = len(target.data.vertices) * len(group_names)
    affected_assignments = sum(target_stats['counts'].get(name, 0)
                               for name in group_names)
    dense_and_transfer_buffers = vertex_group_cells * 4 * 3
    rollback_snapshot = affected_assignments * 112
    evaluated_geometry = (
        (len(source.data.vertices) + len(target.data.vertices)) * 64
        + (len(source.data.polygons) + len(target.data.polygons)) * 32
    )
    return dense_and_transfer_buffers + rollback_snapshot + evaluated_geometry


def _build_preview(settings, context):
    source, target = _validate_settings(settings, context,
                                        require_writable=False)
    for obj in (source, target):
        if obj.mode == 'EDIT':
            obj.update_from_editmode()
    source_stats = _scan_group_stats(source)
    target_stats = _scan_group_stats(target)
    groups = source_stats['weighted_groups']
    if not groups:
        raise RuntimeError(_('general.err.no_source_weights'))

    source_geometry = _geometry_signature(source)
    target_geometry = _geometry_signature(target)
    spatial = _surface_distance_summary(source, target)
    source_rig = _armature_diagnostics(source, groups)
    target_rig = _armature_diagnostics(target, groups)
    blockers = []
    warnings = []
    if target.data.users != 1:
        blockers.append('multi_user')
    if source_rig['invalid']:
        blockers.append('invalid_source_armature')
    if target_rig['invalid']:
        blockers.append('invalid_target_armature')
    if target_rig['posed']:
        blockers.append('posed_target_armature')
    if settings.mapping_method == 'TOPOLOGY':
        if (source_geometry['vertices'] != target_geometry['vertices']
                or source_geometry['topology_sha256']
                != target_geometry['topology_sha256']):
            blockers.append('topology_mismatch')
    elif (settings.mapping_method == 'POLYINTERP_NEAREST'
          and not source.data.polygons):
        blockers.append('no_source_surface')

    predicted_bytes = _estimated_peak_bytes(source, target, target_stats,
                                             groups)
    if predicted_bytes > _MAX_ESTIMATED_BYTES:
        blockers.append('memory_limit')
    if spatial['dimension_ratio'] > 1.5:
        warnings.append('dimension_ratio')
    if spatial['center_distance_ratio'] > 0.15:
        warnings.append('center_distance')
    if (spatial['p95'] is not None
            and spatial['p95'] > spatial['target_diagonal'] * 0.05):
        warnings.append('surface_distance')
    if (settings.use_max_distance and spatial['maximum'] is not None
            and spatial['maximum'] > settings.max_distance):
        warnings.append('max_distance_holes')

    replace_groups = [name for name in groups
                      if target.vertex_groups.get(name) is not None]
    payload = {
        'source': source.name,
        'target': target.name,
        'source_vertices': len(source.data.vertices),
        'target_vertices': len(target.data.vertices),
        'source_weighted_vertices': source_stats['weighted_vertices'],
        'source_coverage': weighted_coverage(
            source_stats['weighted_vertices'], len(source.data.vertices)),
        'source_max_influences': source_stats['max_influences'],
        'groups': groups,
        'replace_groups': replace_groups,
        'target_existing_weighted_vertices': target_stats['weighted_vertices'],
        'spatial': spatial,
        'rig': {
            'source_invalid_modifiers': source_rig['invalid'],
            'target_invalid_modifiers': target_rig['invalid'],
            'target_posed_armatures': target_rig['posed'],
        },
        'estimated_peak_mb': predicted_bytes / (1024 * 1024),
        'blockers': sorted(set(blockers)),
        'warnings': sorted(set(warnings)),
    }
    fingerprint_payload = {
        'source_pointer': int(source.as_pointer()),
        'target_pointer': int(target.as_pointer()),
        'source_data_pointer': int(source.data.as_pointer()),
        'target_data_pointer': int(target.data.as_pointer()),
        'source_geometry': source_geometry,
        'target_geometry': target_geometry,
        'source_weights': source_stats['weight_sha256'],
        'source_groups': [(name, round(source_stats['mass'][name], 9),
                           source_stats['counts'][name]) for name in groups],
        'target_weights': target_stats['weight_sha256'],
        'target_group_order': target_stats['group_order'],
        'source_matrix': _object_matrix_signature(source),
        'target_matrix': _object_matrix_signature(target),
        'source_armature_state': source_rig['signature'],
        'target_armature_state': target_rig['signature'],
        'mapping_method': settings.mapping_method,
        'normalize': bool(settings.normalize_weights),
        'smooth_iterations': int(settings.smooth_iterations),
        'limit_influences': bool(settings.limit_influences),
        'max_influences': int(settings.max_influences),
        'min_weight': round(float(settings.min_weight), 9),
        'allow_unweighted': bool(settings.allow_unweighted),
        'use_max_distance': bool(settings.use_max_distance),
        'max_distance': round(float(settings.max_distance), 9),
    }
    payload['fingerprint'] = stable_signature(fingerprint_payload)
    return payload


def _snapshot_vertex_groups(obj, group_names):
    affected = set(group_names)
    index_to_name = {group.index: group.name for group in obj.vertex_groups
                     if group.name in affected}
    weights = {name: [] for name in group_names}
    for vertex in obj.data.vertices:
        for assignment in vertex.groups:
            name = index_to_name.get(assignment.group)
            if name is not None and assignment.weight > _EPSILON:
                weights[name].append((int(vertex.index), float(assignment.weight)))
    active = obj.vertex_groups.active
    return {
        'active_name': active.name if active else '',
        'groups': [(name, obj.vertex_groups.get(name) is not None, weights[name])
                   for name in group_names],
        'vertex_selection': [vertex.select for vertex in obj.data.vertices],
        'edge_selection': [edge.select for edge in obj.data.edges],
        'polygon_selection': [polygon.select
                              for polygon in obj.data.polygons],
    }


def _restore_active_group(obj, snapshot):
    active_name = snapshot.get('active_name', '')
    active = obj.vertex_groups.get(active_name) if active_name else None
    if active is not None:
        obj.vertex_groups.active_index = active.index


def _restore_mesh_selection(obj, snapshot):
    for vertex, selected in zip(obj.data.vertices,
                                snapshot['vertex_selection']):
        vertex.select = selected
    for edge, selected in zip(obj.data.edges, snapshot['edge_selection']):
        edge.select = selected
    for polygon, selected in zip(obj.data.polygons,
                                 snapshot['polygon_selection']):
        polygon.select = selected


def _restore_vertex_groups(obj, snapshot):
    vertex_count = len(obj.data.vertices)
    for name, existed, entries in snapshot['groups']:
        group = obj.vertex_groups.get(name)
        if not existed:
            if group is not None:
                obj.vertex_groups.remove(group)
            continue
        if group is None:
            group = obj.vertex_groups.new(name=name)
        else:
            _clear_group_weights(group, vertex_count)
        for vertex_index, weight in entries:
            group.add([vertex_index], weight, 'REPLACE')
    _restore_active_group(obj, snapshot)
    _restore_mesh_selection(obj, snapshot)


def _clear_group_weights(group, vertex_count):
    chunk = 10000
    for start in range(0, vertex_count, chunk):
        group.remove(list(range(start, min(vertex_count, start + chunk))))


def _prepare_target_groups(target, group_names):
    for name in group_names:
        group = target.vertex_groups.get(name)
        if group is None:
            group = target.vertex_groups.new(name=name)
        else:
            _clear_group_weights(group, len(target.data.vertices))


def _restore_object_context(context, previous_active, previous_selected,
                            previous_mode, previous_hidden, target):
    errors = []
    try:
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in previous_selected:
            if obj.name in context.view_layer.objects:
                obj.select_set(True)
        if (previous_active is None
                or previous_active.name in context.view_layer.objects):
            context.view_layer.objects.active = previous_active
    except Exception as exc:
        errors.append(str(exc))
    try:
        target.hide_set(previous_hidden)
    except Exception as exc:
        errors.append(str(exc))
    mode_map = {
        'EDIT_MESH': 'EDIT', 'EDIT_CURVE': 'EDIT', 'EDIT_ARMATURE': 'EDIT',
        'POSE': 'POSE', 'SCULPT': 'SCULPT', 'WEIGHT_PAINT': 'WEIGHT_PAINT',
        'VERTEX_PAINT': 'VERTEX_PAINT', 'TEXTURE_PAINT': 'TEXTURE_PAINT',
    }
    restore_mode = mode_map.get(previous_mode)
    if (restore_mode and previous_active is not None
            and previous_active.name in context.view_layer.objects):
        try:
            bpy.ops.object.mode_set(mode=restore_mode)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        print('[GEM2 General Weights] Context restore warning: %s'
              % ' | '.join(errors))


@contextmanager
def _target_object_context(context, target):
    previous_active = context.view_layer.objects.active
    previous_selected = [obj for obj in context.selected_objects
                         if obj.name in context.view_layer.objects]
    previous_mode = context.mode
    previous_hidden = target.hide_get()
    try:
        if previous_mode != 'OBJECT':
            if previous_active is None:
                raise RuntimeError(_('general.err.object_mode'))
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in context.selected_objects:
            obj.select_set(False)
        target.hide_set(False)
        target.select_set(True)
        context.view_layer.objects.active = target
        yield
    finally:
        _restore_object_context(context, previous_active, previous_selected,
                                previous_mode, previous_hidden, target)


def _apply_surface_projection(context, source, target, group_names, settings):
    if context.mode != 'OBJECT' or context.view_layer.objects.active is not target:
        raise RuntimeError(_('general.err.object_mode'))

    # No destination data is changed until Object mode and target context have
    # both been established successfully by _target_object_context.
    _prepare_target_groups(target, group_names)
    modifier = target.modifiers.new(
        name='GEM2 General Weight Projection', type='DATA_TRANSFER')
    modifier.object = source
    modifier.use_vert_data = True
    modifier.data_types_verts = {'VGROUP_WEIGHTS'}
    modifier.vert_mapping = settings.mapping_method
    modifier.layers_vgroup_select_src = 'ALL'
    modifier.layers_vgroup_select_dst = 'NAME'
    modifier.mix_mode = 'REPLACE'
    modifier.mix_factor = 1.0
    modifier.use_object_transform = True
    modifier.use_max_distance = bool(settings.use_max_distance)
    modifier.max_distance = float(settings.max_distance)

    try:
        with context.temp_override(object=target, active_object=target,
                                   selected_objects=[target],
                                   selected_editable_objects=[target]):
            bpy.ops.object.modifier_move_to_index(modifier=modifier.name,
                                                  index=0)
            result = bpy.ops.object.modifier_apply(modifier=modifier.name)
        if 'FINISHED' not in result:
            raise RuntimeError(_('general.err.projection_failed'))
    except Exception:
        if target.modifiers.get(modifier.name) is not None:
            target.modifiers.remove(modifier)
        raise


def _smooth_transferred_weights(context, target, group_names, iterations):
    passes = max(0, int(iterations))
    groups = [target.vertex_groups.get(name) for name in group_names]
    groups = [group for group in groups if group is not None]
    if not passes or not groups:
        return

    previous_active = target.vertex_groups.active_index
    previous_mask = bool(target.data.use_paint_mask_vertex)
    previous_vertex_selection = [vertex.select
                                 for vertex in target.data.vertices]
    previous_edge_selection = [edge.select for edge in target.data.edges]
    previous_polygon_selection = [polygon.select
                                  for polygon in target.data.polygons]
    lock_states = {group.name: bool(group.lock_weight) for group in groups}
    try:
        target.data.use_paint_mask_vertex = False
        for group in groups:
            group.lock_weight = False
        bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
        for group in groups:
            target.vertex_groups.active_index = group.index
            result = bpy.ops.object.vertex_group_smooth(
                group_select_mode='ACTIVE', factor=0.5,
                repeat=passes, expand=0.0)
            if 'FINISHED' not in result:
                raise RuntimeError(_('general.err.smoothing_failed'))
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(_('general.err.smoothing_failed')) from exc
    finally:
        if target.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        target.data.use_paint_mask_vertex = previous_mask
        for vertex, selected in zip(target.data.vertices,
                                    previous_vertex_selection):
            vertex.select = selected
        for edge, selected in zip(target.data.edges, previous_edge_selection):
            edge.select = selected
        for polygon, selected in zip(target.data.polygons,
                                     previous_polygon_selection):
            polygon.select = selected
        for group in groups:
            group.lock_weight = lock_states[group.name]
        if target.vertex_groups:
            target.vertex_groups.active_index = min(
                previous_active, len(target.vertex_groups) - 1)


def _add_column_weights(group, matrix, column):
    import numpy as np

    nonzero = np.flatnonzero(matrix[:, column] > 0.0)
    buckets = {}
    for vertex_index in nonzero:
        weight = float(matrix[vertex_index, column])
        buckets.setdefault(weight, []).append(int(vertex_index))
        if len(buckets) > 2048:
            for fallback_index in nonzero:
                group.add([int(fallback_index)],
                          float(matrix[fallback_index, column]), 'REPLACE')
            return
    for weight, indices in buckets.items():
        group.add(indices, weight, 'REPLACE')


def _clean_transferred_weights(target, group_names, settings):
    import numpy as np

    vertex_count = len(target.data.vertices)
    group_count = len(group_names)
    matrix_bytes = vertex_count * group_count * 4
    if matrix_bytes > _MAX_ESTIMATED_BYTES:
        raise RuntimeError(_('general.err.matrix_too_large',
                             mb=matrix_bytes // (1024 * 1024)))

    name_to_column = {name: index for index, name in enumerate(group_names)}
    group_to_name = {group.index: group.name for group in target.vertex_groups
                     if group.name in name_to_column}
    matrix = np.zeros((vertex_count, group_count), dtype=np.float32)
    for vertex in target.data.vertices:
        source_weights = [
            (group_to_name[assignment.group], float(assignment.weight))
            for assignment in vertex.groups
            if assignment.group in group_to_name
        ]
        cleaned = clean_weight_vector(
            source_weights,
            min_weight=settings.min_weight,
            limit_influences=settings.limit_influences,
            max_influences=settings.max_influences,
            normalize=settings.normalize_weights,
        )
        for name, weight in cleaned:
            matrix[vertex.index, name_to_column[name]] = weight

    sums = matrix.sum(axis=1)
    valid = sums > _EPSILON
    window_manager = bpy.context.window_manager
    window_manager.progress_begin(0, max(1, group_count))
    try:
        for column, name in enumerate(group_names):
            group = target.vertex_groups.get(name)
            if group is None:
                group = target.vertex_groups.new(name=name)
            else:
                _clear_group_weights(group, vertex_count)
            _add_column_weights(group, matrix, column)
            window_manager.progress_update(column + 1)
    finally:
        window_manager.progress_end()

    counts = (matrix > 0.0).sum(axis=1) if vertex_count else np.array([])
    return {
        'weighted_vertices': int(valid.sum()),
        'unweighted_vertices': int((~valid).sum()),
        'max_influences': int(counts.max()) if counts.size else 0,
        'min_sum': float(sums[valid].min()) if valid.any() else 0.0,
        'max_sum': float(sums[valid].max()) if valid.any() else 0.0,
    }


def _unweighted_indices(target, group_names, threshold):
    indices = {group.index for group in target.vertex_groups
               if group.name in set(group_names)}
    output = []
    for vertex in target.data.vertices:
        total = sum(float(item.weight) for item in vertex.groups
                    if item.group in indices and item.weight >= threshold)
        if total <= _EPSILON:
            output.append(int(vertex.index))
    return output


def _has_nonzero_weights(obj):
    if not obj.vertex_groups:
        return False
    return any(assignment.weight > _EPSILON
               for vertex in obj.data.vertices for assignment in vertex.groups)


def _detect_objects(context, settings):
    active = context.view_layer.objects.active
    target = active if active is not None and active.type == 'MESH' else None
    if target is None:
        target = settings.target_mesh
    if target is None:
        selected_meshes = [obj for obj in context.selected_objects
                           if obj.type == 'MESH']
        if len(selected_meshes) == 1:
            target = selected_meshes[0]
    if target is None:
        raise RuntimeError(_('general.err.detect_target'))

    candidates = []
    for obj in context.scene.objects:
        if obj.type != 'MESH' or obj is target or not _has_nonzero_weights(obj):
            continue
        if _has_invalid_armature_modifier(obj):
            continue
        spatial = _bbox_spatial_summary(obj, target)
        score = (100.0
                 - abs(math.log(max(spatial['dimension_ratio'], 1.0))) * 30.0
                 - spatial['center_distance_ratio'] * 30.0)
        candidates.append((score, obj))
    if not candidates:
        raise RuntimeError(_('general.err.detect_source'))
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 1.0:
        raise RuntimeError(_('general.err.detect_ambiguous',
                             first=candidates[0][1].name,
                             second=candidates[1][1].name))
    source = candidates[0][1]
    settings.source_mesh = source
    settings.target_mesh = target
    _clear_preview(settings)
    return source, target


class GEM2_GeneralWeightSettings(bpy.types.PropertyGroup):
    source_mesh: bpy.props.PointerProperty(
        name=_('general.prop.source_mesh'), type=bpy.types.Object,
        poll=_poll_mesh, update=_setting_changed)
    target_mesh: bpy.props.PointerProperty(
        name=_('general.prop.target_mesh'), type=bpy.types.Object,
        poll=_poll_mesh, update=_setting_changed)
    mapping_method: bpy.props.EnumProperty(
        name=_('general.prop.mapping_method'),
        items=(
            ('POLYINTERP_NEAREST', _('general.method.surface'),
             _('general.method.surface.desc')),
            ('NEAREST', _('general.method.vertex'),
             _('general.method.vertex.desc')),
            ('TOPOLOGY', _('general.method.topology'),
             _('general.method.topology.desc')),
        ), default='POLYINTERP_NEAREST', update=_setting_changed)
    normalize_weights: bpy.props.BoolProperty(
        name=_('general.prop.normalize'), default=True, update=_setting_changed)
    smooth_iterations: bpy.props.IntProperty(
        name=_('general.prop.smooth_iterations'),
        description=_('general.prop.smooth_iterations.desc'),
        default=2, min=0, max=10, update=_setting_changed)
    limit_influences: bpy.props.BoolProperty(
        name=_('general.prop.limit_influences'), default=True,
        update=_setting_changed)
    max_influences: bpy.props.IntProperty(
        name=_('general.prop.max_influences'), default=4, min=1, max=12,
        update=_setting_changed)
    min_weight: bpy.props.FloatProperty(
        name=_('general.prop.min_weight'), default=0.0001, min=0.0,
        max=0.1, precision=5, update=_setting_changed)
    allow_unweighted: bpy.props.BoolProperty(
        name=_('general.prop.allow_unweighted'), default=False,
        update=_setting_changed)
    use_max_distance: bpy.props.BoolProperty(
        name=_('general.prop.use_max_distance'), default=False,
        update=_setting_changed)
    max_distance: bpy.props.FloatProperty(
        name=_('general.prop.max_distance'), default=0.05, min=0.0,
        soft_max=1.0, subtype='DISTANCE', update=_setting_changed)
    show_options: bpy.props.BoolProperty(
        name=_('general.prop.show_options'), default=False)
    show_diagnostics: bpy.props.BoolProperty(
        name=_('general.prop.show_diagnostics'), default=False)
    preview_fingerprint: bpy.props.StringProperty(default='', options={'HIDDEN'})
    preview_json: bpy.props.StringProperty(default='', options={'HIDDEN'})
    report: bpy.props.StringProperty(default='')
    last_groups_json: bpy.props.StringProperty(default='[]', options={'HIDDEN'})
    last_unweighted_count: bpy.props.IntProperty(default=0, options={'HIDDEN'})


class GEM2_OT_GeneralAutoDetect(bpy.types.Operator):
    bl_idname = 'gem2.general_weight_auto_detect'
    bl_label = _('general.op.auto_detect')
    bl_description = _('general.op.auto_detect.desc')
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.gem2_general_weights
        try:
            source, target = _detect_objects(context, settings)
            settings.report = _('general.report.detected', source=source.name,
                                target=target.name)
            self.report({'INFO'}, settings.report)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class GEM2_OT_GeneralUseSelection(bpy.types.Operator):
    bl_idname = 'gem2.general_weight_use_selection'
    bl_label = _('general.op.use_selection')
    bl_description = _('general.op.use_selection.desc')
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.gem2_general_weights
        meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        active = context.view_layer.objects.active
        if len(meshes) != 2 or active not in meshes:
            self.report({'ERROR'}, _('general.err.selection_meshes'))
            return {'CANCELLED'}
        target = active
        source = next(obj for obj in meshes if obj is not target)
        settings.source_mesh = source
        settings.target_mesh = target
        _clear_preview(settings)
        settings.report = _('general.report.selection', source=source.name,
                            target=target.name)
        self.report({'INFO'}, settings.report)
        return {'FINISHED'}


class GEM2_OT_GeneralPreviewWeights(bpy.types.Operator):
    bl_idname = 'gem2.general_weight_preview'
    bl_label = _('general.op.preview')
    bl_description = _('general.op.preview.desc')
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.gem2_general_weights
        try:
            preview = _build_preview(settings, context)
            settings.preview_json = json.dumps(preview, ensure_ascii=False,
                                               separators=(',', ':'))
            settings.preview_fingerprint = preview['fingerprint']
            settings.report = _('general.report.preview',
                                groups=len(preview['groups']),
                                coverage=preview['source_coverage'] * 100.0)
            self.report({'WARNING'} if preview['blockers'] else {'INFO'},
                        settings.report)
            return {'FINISHED'}
        except RuntimeError as exc:
            _clear_preview(settings)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            traceback.print_exc()
            _clear_preview(settings)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class GEM2_OT_GeneralClearPreview(bpy.types.Operator):
    bl_idname = 'gem2.general_weight_clear_preview'
    bl_label = _('general.op.clear')
    bl_options = {'INTERNAL'}

    def execute(self, context):
        settings = context.scene.gem2_general_weights
        _clear_preview(settings)
        settings.report = ''
        return {'FINISHED'}


class GEM2_OT_GeneralApplyWeights(bpy.types.Operator):
    bl_idname = 'gem2.general_weight_apply'
    bl_label = _('general.op.apply')
    bl_description = _('general.op.apply.desc')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.gem2_general_weights
        try:
            source, target = _validate_settings(settings, context,
                                                require_writable=True)
            if not settings.preview_fingerprint:
                raise RuntimeError(_('general.err.preview_required'))
            preview = _build_preview(settings, context)
            if preview['fingerprint'] != settings.preview_fingerprint:
                _clear_preview(settings)
                raise RuntimeError(_('general.err.preview_stale'))
            if preview['blockers']:
                raise RuntimeError(_('general.err.preview_blocked'))

            with _target_object_context(context, target):
                snapshot = _snapshot_vertex_groups(target, preview['groups'])
                try:
                    _apply_surface_projection(context, source, target,
                                              preview['groups'], settings)
                    _smooth_transferred_weights(
                        context, target, preview['groups'],
                        settings.smooth_iterations)
                    result_stats = _clean_transferred_weights(
                        target, preview['groups'], settings)
                    if (result_stats['unweighted_vertices']
                            and not settings.allow_unweighted):
                        raise RuntimeError(_('general.err.unweighted_result',
                                             count=result_stats['unweighted_vertices']))
                    _restore_active_group(target, snapshot)
                    _restore_mesh_selection(target, snapshot)
                except Exception:
                    _restore_vertex_groups(target, snapshot)
                    raise

            settings.last_groups_json = json.dumps(preview['groups'],
                                                   ensure_ascii=False)
            settings.last_unweighted_count = result_stats['unweighted_vertices']
            settings.report = _('general.report.applied',
                                weighted=result_stats['weighted_vertices'],
                                total=len(target.data.vertices),
                                unweighted=result_stats['unweighted_vertices'],
                                influences=result_stats['max_influences'])
            _clear_preview(settings)
            self.report({'INFO'}, settings.report)
            return {'FINISHED'}
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class GEM2_OT_GeneralSelectUnweighted(bpy.types.Operator):
    bl_idname = 'gem2.general_weight_select_unweighted'
    bl_label = _('general.op.select_unweighted')
    bl_description = _('general.op.select_unweighted.desc')
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.gem2_general_weights
        target = settings.target_mesh
        if target is None or target.type != 'MESH':
            self.report({'ERROR'}, _('general.err.target_mesh'))
            return {'CANCELLED'}
        if target.name not in context.view_layer.objects:
            self.report({'ERROR'}, _('general.err.target_not_visible'))
            return {'CANCELLED'}
        try:
            groups = json.loads(settings.last_groups_json)
        except Exception:
            groups = []
        if not groups:
            self.report({'ERROR'}, _('general.err.no_result'))
            return {'CANCELLED'}
        indices = _unweighted_indices(target, groups, settings.min_weight)
        if not indices:
            self.report({'INFO'}, _('general.report.selected_unweighted', count=0))
            return {'FINISHED'}

        previous_active = context.view_layer.objects.active
        previous_selected = [obj for obj in context.selected_objects
                             if obj.name in context.view_layer.objects]
        previous_mode = context.mode
        previous_hidden = target.hide_get()
        previous_vertex_selection = {vertex.index for vertex in target.data.vertices
                                     if vertex.select}
        try:
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for obj in context.selected_objects:
                obj.select_set(False)
            target.hide_set(False)
            target.select_set(True)
            context.view_layer.objects.active = target
            for vertex in target.data.vertices:
                vertex.select = False
            for index in indices:
                target.data.vertices[index].select = True
            bpy.ops.object.mode_set(mode='EDIT')
        except Exception as exc:
            for vertex in target.data.vertices:
                vertex.select = vertex.index in previous_vertex_selection
            _restore_object_context(context, previous_active, previous_selected,
                                    previous_mode, previous_hidden, target)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, _('general.report.selected_unweighted',
                               count=len(indices)))
        return {'FINISHED'}


class GEM2_PT_GeneralWeights(bpy.types.Panel):
    bl_label = _('general.panel.label')
    bl_idname = 'GEM2_PT_general_weights'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'GOH'
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        settings = context.scene.gem2_general_weights
        preview = None
        if settings.preview_json:
            try:
                preview = json.loads(settings.preview_json)
            except Exception:
                preview = None

        layout.prop(settings, 'source_mesh')
        layout.prop(settings, 'target_mesh')
        row = layout.row(align=True)
        row.operator('gem2.general_weight_auto_detect', icon='VIEWZOOM')
        row.operator('gem2.general_weight_use_selection', icon='RESTRICT_SELECT_OFF')

        layout.separator(factor=0.5)
        layout.prop(settings, 'mapping_method')
        options = layout.row(align=True)
        options.prop(settings, 'show_options', icon='DISCLOSURE_TRI_DOWN'
                     if settings.show_options else 'DISCLOSURE_TRI_RIGHT')
        if settings.show_options:
            layout.prop(settings, 'normalize_weights')
            layout.prop(settings, 'smooth_iterations')
            layout.prop(settings, 'limit_influences')
            limit_row = layout.row()
            limit_row.enabled = settings.limit_influences
            limit_row.prop(settings, 'max_influences')
            layout.prop(settings, 'min_weight')
            layout.prop(settings, 'allow_unweighted')
            if settings.mapping_method != 'TOPOLOGY':
                layout.prop(settings, 'use_max_distance')
                distance_row = layout.row()
                distance_row.enabled = settings.use_max_distance
                distance_row.prop(settings, 'max_distance')

        row = layout.row(align=True)
        row.operator('gem2.general_weight_preview', icon='VIEWZOOM')
        apply_row = row.row(align=True)
        apply_row.enabled = (bool(settings.preview_fingerprint)
                             and preview is not None
                             and not preview.get('blockers'))
        apply_row.operator('gem2.general_weight_apply', icon='MOD_DATA_TRANSFER')

        if preview is None:
            layout.label(text=_('general.status.no_preview'), icon='INFO')
        else:
            icon = 'ERROR' if preview['blockers'] else 'CHECKMARK'
            layout.label(text=_('general.status.source',
                                groups=len(preview['groups']),
                                weighted=preview['source_weighted_vertices'],
                                total=preview['source_vertices']), icon=icon)
            spatial = preview.get('spatial') or {}
            if spatial.get('p95') is not None:
                layout.label(text=_('general.status.spatial',
                                    p95=spatial['p95'],
                                    ratio=spatial['dimension_ratio']),
                             icon='DRIVER_DISTANCE')
            for blocker in preview['blockers']:
                key = _BLOCKER_I18N.get(blocker)
                if key:
                    layout.label(text=_(key), icon='ERROR')
            rig = preview.get('rig') or {}
            for item in rig.get('source_invalid_modifiers', [])[:2]:
                layout.label(text=_('general.status.invalid_source_modifier',
                                    modifier=item['modifier'],
                                    object=item['object'] or '<none>'),
                             icon='MOD_ARMATURE')
            for item in rig.get('target_invalid_modifiers', [])[:2]:
                layout.label(text=_('general.status.invalid_target_modifier',
                                    modifier=item['modifier'],
                                    object=item['object'] or '<none>'),
                             icon='MOD_ARMATURE')
            for item in rig.get('target_posed_armatures', [])[:2]:
                layout.label(text=_('general.status.posed_target',
                                    armature=item['armature'],
                                    count=len(item['changed_bones'])),
                             icon='ARMATURE_DATA')
                layout.label(text=_('general.status.posed_target_fix'),
                             icon='INFO')
            spatial_warnings = {'dimension_ratio', 'center_distance',
                                'surface_distance'}
            if spatial_warnings.intersection(preview['warnings']):
                layout.label(text=_('general.status.spatial_warning'),
                             icon='ERROR')
            if 'max_distance_holes' in preview['warnings']:
                layout.label(text=_('general.status.max_distance_warning'),
                             icon='ERROR')
            diagnostics = layout.row(align=True)
            diagnostics.prop(settings, 'show_diagnostics',
                             icon='DISCLOSURE_TRI_DOWN'
                             if settings.show_diagnostics else 'DISCLOSURE_TRI_RIGHT')
            diagnostics.operator('gem2.general_weight_clear_preview',
                                 text='', icon='X')
            if settings.show_diagnostics:
                layout.label(text=_('general.status.target',
                                    vertices=preview['target_vertices'],
                                    replace=len(preview['replace_groups'])),
                             icon='MESH_DATA')
                layout.label(text=_('general.status.memory',
                                    mb=preview['estimated_peak_mb']),
                             icon='INFO')
                for name in preview['groups'][:6]:
                    layout.label(text=name, icon='DOT')
                if len(preview['groups']) > 6:
                    layout.label(text=_('general.status.more',
                                        count=len(preview['groups']) - 6),
                                 icon='DOT')

        if settings.last_unweighted_count:
            layout.operator('gem2.general_weight_select_unweighted',
                            icon='VERTEXSEL')
        if settings.report:
            layout.separator(factor=0.5)
            layout.label(text=settings.report, icon='INFO')


_CLASSES = (
    GEM2_GeneralWeightSettings,
    GEM2_OT_GeneralAutoDetect,
    GEM2_OT_GeneralUseSelection,
    GEM2_OT_GeneralPreviewWeights,
    GEM2_OT_GeneralClearPreview,
    GEM2_OT_GeneralApplyWeights,
    GEM2_OT_GeneralSelectUnweighted,
    GEM2_PT_GeneralWeights,
)


def register():
    try:
        unregister()
    except Exception:
        pass
    for cls in _CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception as exc:
            print('[GEM2 General Weights] register %s: %s' % (cls.__name__, exc))
    bpy.types.Scene.gem2_general_weights = bpy.props.PointerProperty(
        type=GEM2_GeneralWeightSettings, options={'SKIP_SAVE'})


def unregister():
    try:
        del bpy.types.Scene.gem2_general_weights
    except Exception:
        pass
    for cls in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
