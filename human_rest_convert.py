# -*- coding: utf-8 -*-
"""Rest-space conversion for already skinned GEM2 human meshes.

This module is deliberately separate from the PMX alignment/weight pipeline.
A GEM2 human PLY stores vertices in the model space below its VolumeView
attachment.  The raw MDL world matrices are therefore normalized by the
attachment's immediate ancestor (normally ``basis``) before a rest transfer.
The target MDL metadata remains the authority used by the game exporter.
"""

import hashlib
import json
import math
import os

import bpy
from mathutils import Matrix, Vector

from .core import ori_to_blender, row34_to_blender


# Version 2 keeps the raw MDL transfer contract, but requires Blender armatures
# to be rebuilt from normalized (unmirrored) rest frames before they are used.
VERSION = 2
DISPLAY_REST_VERSION = 1
EPSILON = 1.0e-8
WEIGHT_EPSILON = 1.0e-8
DISPLAY_LENGTH = 0.2
# A reflected normalized GEM bone cannot be stored by Blender.  Flipping its
# local X axis preserves the bone's local Y (length) direction while producing
# the unique proper frame used for Blender display/pose evaluation.
_DISPLAY_REFLECTION_X = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0))

# The 24 groups used by the bundled human skins.  This is an order hint only;
# a source PLY may contain a compatible subset or a destination-specific order.
GOH_SKIN_ORDER = (
    'body', 'foot1l', 'foot2l', 'foot3l', 'foot1r', 'foot2r', 'foot3r',
    'ik_leftright', 'ik_updown', 'clavicle_left', 'hand1l', 'hand2l',
    'hand_rot1l', 'head', 'clavicle_right', 'hand1r', 'hand2r',
    'hand_rot1r', 'palm1r', 'palm2r', 'palm3r', 'palm1l', 'palm2l',
    'palm3l',
)
# The bundled MOWAS2 skin.ply uses the left palm groups before the right
# clavicle/hand groups and writes head last.
MOWAS2_SKIN_ORDER = (
    'body', 'foot1l', 'foot2l', 'foot3l', 'foot1r', 'foot2r', 'foot3r',
    'ik_leftright', 'ik_updown', 'clavicle_left', 'hand1l', 'hand2l',
    'hand_rot1l', 'palm1l', 'palm2l', 'palm3l', 'clavicle_right',
    'hand1r', 'hand2r', 'hand_rot1r', 'palm1r', 'palm2r', 'palm3r', 'head',
)
HUMAN_SKIN_ORDER = GOH_SKIN_ORDER


def skin_order_for_route(route):
    """Return the known palette order for a bundled human route."""
    return list(MOWAS2_SKIN_ORDER if str(route).upper() == 'MOWAS2'
                else GOH_SKIN_ORDER)


class HumanRestConversionError(RuntimeError):
    """Raised when a human rest conversion cannot be performed safely."""


class HumanRestGraph:
    """Raw MDL rest data needed by a mesh conversion."""

    __slots__ = ('path', 'bones', 'parents', 'mesh_parent', 'volume_views')

    def __init__(self, path, bones, parents, mesh_parent, volume_views=None):
        self.path = os.path.abspath(os.fspath(path)) if path else ''
        if not hasattr(bones, 'items') or not hasattr(parents, 'items'):
            raise HumanRestConversionError(
                'Rest bones and parents must be mappings')
        try:
            self.bones = {str(name): Matrix(matrix).copy()
                          for name, matrix in bones.items()}
        except (IndexError, TypeError, ValueError) as exc:
            raise HumanRestConversionError('Invalid rest matrix data') from exc
        self.parents = {str(name): (str(parent) if parent else '')
                        for name, parent in parents.items()}
        self.mesh_parent = str(mesh_parent or '')
        self.volume_views = {
            str(name): list(values or [])
            for name, values in (volume_views or {}).items()
        }
        self._validate()

    def _validate(self):
        if not self.bones:
            raise HumanRestConversionError('MDL contains no rest bones')
        folded = {}
        for name, matrix in self.bones.items():
            key = name.casefold()
            if key in folded and folded[key] != name:
                raise HumanRestConversionError(
                    'MDL has case-colliding bone names: %s / %s'
                    % (folded[key], name))
            folded[key] = name
            if len(matrix) != 4 or any(len(row) != 4 for row in matrix):
                raise HumanRestConversionError('Invalid rest matrix: ' + name)
            values = [float(matrix[row][column])
                      for row in range(4) for column in range(4)]
            if not all(math.isfinite(value) for value in values):
                raise HumanRestConversionError('Non-finite rest matrix: ' + name)
            if abs(float(matrix.to_3x3().determinant())) <= EPSILON:
                raise HumanRestConversionError('Singular rest matrix: ' + name)
        for name in self.bones:
            self.parents.setdefault(name, '')
        unknown_parents = sorted({parent for parent in self.parents.values()
                                  if parent and parent not in self.bones})
        if unknown_parents:
            raise HumanRestConversionError(
                'MDL references missing parent bones: ' + ', '.join(unknown_parents))
        if self.mesh_parent and self.mesh_parent not in self.bones:
            raise HumanRestConversionError(
                'MDL mesh parent is not a bone: ' + self.mesh_parent)

    @property
    def bone_names(self):
        return tuple(self.bones.keys())

    def ancestor_bridge(self):
        """Return the raw immediate ancestor matrix used by PLY model space."""
        if not self.mesh_parent:
            return Matrix.Identity(4)
        parent = self.parents.get(self.mesh_parent, '')
        if parent and parent in self.bones:
            return self.bones[parent].copy()
        return Matrix.Identity(4)

    def mesh_local_matrix(self):
        """Return the attachment local matrix peeled by the human PLY exporter."""
        if not self.mesh_parent:
            return Matrix.Identity(4)
        parent = self.parents.get(self.mesh_parent, '')
        if parent and parent in self.bones:
            return self.bones[parent].inverted() @ self.bones[self.mesh_parent]
        return self.bones[self.mesh_parent].copy()

    def normalized_bones(self):
        """Return rest matrices in the imported model/ancestor space."""
        bridge = self.ancestor_bridge()
        inverse = bridge.inverted()
        return {name: inverse @ matrix for name, matrix in self.bones.items()}

    def required_chain(self, names):
        """Return weighted bones plus all parents needed to validate a rig."""
        result = set(str(name) for name in names if name)
        pending = list(result)
        while pending:
            name = pending.pop()
            parent = self.parents.get(name, '')
            if parent and parent not in result:
                result.add(parent)
                pending.append(parent)
        if self.mesh_parent:
            result.add(self.mesh_parent)
            parent = self.parents.get(self.mesh_parent, '')
            if parent:
                result.add(parent)
        return result


def rest_graph_fingerprint(graph):
    """Return a path-independent identity for a raw human rest graph.

    Copied Blender files often retain an absolute MDL path from another
    installation.  The raw skeleton contract is still identifiable by its
    parent map, attachment bone, and matrices, so conversion records can use
    this digest when deciding whether an exact reverse is requested.
    """
    if not isinstance(graph, HumanRestGraph):
        raise HumanRestConversionError('Expected a human rest graph')
    rows = []
    for name in sorted(graph.bones):
        matrix = graph.bones[name]
        values = []
        for row in range(4):
            for column in range(4):
                value = float(matrix[row][column])
                # MDL text and Blender ID properties can differ only in signed
                # zero or insignificant serialization digits.
                values.append(0.0 if abs(value) < 5.0e-10 else round(value, 9))
        rows.append((name, graph.parents.get(name, ''), values))
    payload = {
        'mesh_parent': graph.mesh_parent,
        'bones': rows,
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def _proper_display_matrix(matrix, name):
    """Return a Blender-representable frame without changing its position.

    GEM human MDLs can contain a reflected local frame in the foot chain.  A
    Blender EditBone always has a proper orientation, so preserve the local Y
    axis (the bone length direction) and flip only local X before assigning the
    matrix.  Raw matrices are never modified by this helper.
    """
    value = _finite_matrix(matrix, name + ' display')
    linear = value.to_3x3()
    determinant = float(linear.determinant())
    if determinant < -EPSILON:
        linear = linear @ _DISPLAY_REFLECTION_X.to_3x3()
        determinant = float(linear.determinant())
    if determinant <= EPSILON:
        raise HumanRestConversionError(
            'Display rest matrix remains reflected or singular: ' + name)

    # MDL rotations are normally orthonormal.  Normalize only malformed or
    # numerically noisy input; retain the exact proper matrix otherwise.
    orthogonal_error = max(
        abs(float(linear.col[index].length) - 1.0)
        for index in range(3))
    orthogonal_error = max(orthogonal_error, abs(
        float(linear.col[0].dot(linear.col[1]))), abs(
        float(linear.col[0].dot(linear.col[2]))), abs(
        float(linear.col[1].dot(linear.col[2]))))
    if orthogonal_error > 1.0e-4 or abs(determinant - 1.0) > 1.0e-4:
        try:
            linear = linear.to_quaternion().to_matrix()
        except (AttributeError, RuntimeError, ValueError) as exc:
            raise HumanRestConversionError(
                'Could not normalize display rest matrix: ' + name) from exc
    result = linear.to_4x4()
    result.translation = value.translation
    return result


def display_world_matrices(graph):
    """Return proper Blender display frames derived from raw MDL matrices.

    ``HumanRestGraph.bones`` remains raw MDL world space.  The imported PLY
    model coordinates are already in the ancestor-normalized space, so the
    display frame is the normalized matrix, with only reflected local frames
    made representable for Blender.  This is intentionally not a replacement
    for ``gem2_world_mats`` and must never be exported as raw MDL data.
    """
    if not isinstance(graph, HumanRestGraph):
        raise TypeError('graph must be a HumanRestGraph')
    normalized = graph.normalized_bones()
    return {name: _proper_display_matrix(matrix, name)
            for name, matrix in normalized.items()}


def _matrix_delta_map(armature, expected):
    """Return the largest matrix delta and its bone name."""
    maximum = 0.0
    worst = ''
    for name, matrix in expected.items():
        bone = armature.data.bones.get(name)
        if bone is None:
            continue
        delta = max(abs(float(bone.matrix_local[row][column]
                             - matrix[row][column]))
                    for row in range(4) for column in range(4))
        if delta > maximum:
            maximum = delta
            worst = name
    return maximum, worst


def required_display_rest_delta(armature, graph):
    """Return the current Blender-rest error against the normalized raw graph."""
    if armature is None or getattr(armature, 'type', None) != 'ARMATURE':
        return float('inf')
    expected = display_world_matrices(graph)
    maximum, _worst = _matrix_delta_map(armature, expected)
    return maximum


def _finite_matrix(matrix, label):
    try:
        value = Matrix(matrix)
    except (IndexError, TypeError, ValueError) as exc:
        raise HumanRestConversionError('Invalid matrix: ' + label) from exc
    values = [float(value[row][column])
              for row in range(4) for column in range(4)]
    if not all(math.isfinite(item) for item in values):
        raise HumanRestConversionError('Non-finite matrix: ' + label)
    if abs(float(value.to_3x3().determinant())) <= EPSILON:
        raise HumanRestConversionError('Singular matrix: ' + label)
    return value


def _json_property(owner, key, default=None):
    value = owner.get(key) if owner is not None else None
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            raise HumanRestConversionError(
                '%s is not valid JSON on %s' % (key, getattr(owner, 'name', 'object'))
            ) from exc
    try:
        return value.to_dict()
    except (AttributeError, RuntimeError):
        return value


def _node_list(root_nodes):
    """Yield parsed nodes while detecting duplicate names."""
    result = []
    seen = {}

    def visit(node, parent=''):
        name = str(node.get('name') or '')
        if not name:
            raise HumanRestConversionError('MDL contains a nameless bone')
        folded = name.casefold()
        if folded in seen:
            raise HumanRestConversionError(
                'MDL contains duplicate bone name: %s' % name)
        seen[folded] = name
        result.append((node, parent))
        for child in node.get('children', []):
            visit(child, name)

    for root in root_nodes:
        visit(root, '')
    return result


def _node_local_matrix(node):
    matrix = node.get('matrix')
    if matrix:
        return _finite_matrix(row34_to_blender(matrix), str(node.get('name')))
    orientation = node.get('orientation')
    position = node.get('position')
    if orientation:
        value = ori_to_blender(orientation)
    else:
        value = Matrix.Identity(4)
    if position:
        value.translation = Vector(position)
    return _finite_matrix(value, str(node.get('name')))


def _validate_braces(content):
    depth = 0
    in_string = False
    escaped = False
    in_comment = False
    index = 0
    while index < len(content):
        char = content[index]
        if in_comment:
            if char in '\r\n':
                in_comment = False
            index += 1
            continue
        if not in_string and char == ';':
            in_comment = True
            index += 1
            continue
        if (not in_string and char == '/' and index + 1 < len(content)
                and content[index + 1] == '/'):
            in_comment = True
            index += 2
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth < 0:
                raise HumanRestConversionError('MDL has an unmatched closing brace')
        index += 1
    if in_string:
        raise HumanRestConversionError('MDL has an unterminated string')
    if depth:
        raise HumanRestConversionError('MDL has unmatched braces')


def load_rest_graph(path):
    """Parse an MDL into a validated raw-world rest graph."""
    path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(path):
        raise HumanRestConversionError('MDL file not found: ' + path)
    from . import mdl_io
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as handle:
            content = handle.read()
        _validate_braces(content)
        roots, parsed_mesh_parent = mdl_io.parse_mdl(content)
    except OSError as exc:
        raise HumanRestConversionError('Could not read MDL: ' + path) from exc
    if not roots:
        raise HumanRestConversionError('MDL has no valid {skeleton} bones: ' + path)

    nodes = _node_list(roots)
    local = {}
    parents = {}
    volume_views = {}
    for node, parent in nodes:
        name = str(node['name'])
        local[name] = _node_local_matrix(node)
        parents[name] = parent
        views = list(node.get('volume_views') or [])
        if views:
            volume_views[name] = views

    worlds = {}
    visiting = set()

    def resolve(name):
        if name in worlds:
            return worlds[name]
        if name in visiting:
            raise HumanRestConversionError(
                'MDL bone parent cycle detected at ' + name)
        visiting.add(name)
        try:
            parent = parents.get(name, '')
            if parent:
                if parent not in local:
                    raise HumanRestConversionError(
                        'MDL references missing parent bone: ' + parent)
                worlds[name] = resolve(parent) @ local[name]
            else:
                worlds[name] = local[name].copy()
            return worlds[name]
        finally:
            visiting.discard(name)

    for name in local:
        resolve(name)

    volume_parents = [name for name, values in volume_views.items() if values]
    if len(volume_parents) != 1:
        raise HumanRestConversionError(
            'Human MDL must have exactly one direct VolumeView parent; found %d'
            % len(volume_parents))
    mesh_parent = str(parsed_mesh_parent or volume_parents[0])
    if mesh_parent != volume_parents[0]:
        raise HumanRestConversionError(
            'MDL parser mesh parent disagrees with direct VolumeView data')
    return HumanRestGraph(path, worlds, parents, mesh_parent, volume_views)


def graph_from_armature(armature, fallback_path=None):
    """Read raw MDL metadata from an imported Blender armature."""
    if armature is None or getattr(armature, 'type', None) != 'ARMATURE':
        raise HumanRestConversionError('Expected a Blender armature object')
    raw_worlds = _json_property(armature, 'gem2_world_mats')
    raw_parents = _json_property(armature, 'gem2_parents')
    path = str(armature.get('gem2_mdl_path') or fallback_path or '')
    if not raw_worlds or not raw_parents:
        if path:
            return load_rest_graph(path)
        raise HumanRestConversionError(
            'Armature lacks gem2_world_mats/gem2_parents metadata')
    if not hasattr(raw_worlds, 'items') or not hasattr(raw_parents, 'items'):
        raise HumanRestConversionError('Armature rest metadata is not a mapping')
    worlds = {str(name): _finite_matrix(matrix, str(name))
              for name, matrix in raw_worlds.items()}
    parents = {str(name): (str(parent) if parent else '')
               for name, parent in raw_parents.items()}
    mesh_parent = str(armature.get('gem2_mesh_parent') or '')
    if not mesh_parent:
        # An old imported armature may omit the attachment property.  Recover
        # it from the MDL only; guessing a bone would corrupt export space.
        if path and os.path.isfile(path):
            return load_rest_graph(path)
        raise HumanRestConversionError('Armature lacks gem2_mesh_parent metadata')
    volume_views = {mesh_parent: ['']}
    return HumanRestGraph(path, worlds, parents, mesh_parent, volume_views)


def _weight_names_from_mesh(mesh):
    names = [group.name for group in mesh.vertex_groups]
    used = set()
    for vertex in mesh.data.vertices:
        for assignment in vertex.groups:
            if float(assignment.weight) > WEIGHT_EPSILON:
                if assignment.group >= len(names):
                    raise HumanRestConversionError(
                        'Vertex group index is out of range on vertex %d'
                        % vertex.index)
                used.add(names[assignment.group])
    return names, used


def validate_compatible(source, destination, weight_names=()):
    """Validate explicit name/parent correspondence for a human transfer."""
    if not source.mesh_parent or not destination.mesh_parent:
        raise HumanRestConversionError('Both MDLs must declare a mesh parent')
    if source.mesh_parent not in source.bones:
        raise HumanRestConversionError('Source mesh parent is missing')
    if destination.mesh_parent not in destination.bones:
        raise HumanRestConversionError('Destination mesh parent is missing')
    required = source.required_chain(weight_names)
    required.update(destination.required_chain(weight_names))
    missing_source = sorted(name for name in required if name not in source.bones)
    missing_destination = sorted(name for name in required if name not in destination.bones)
    if missing_source:
        raise HumanRestConversionError(
            'Source MDL lacks required bones: ' + ', '.join(missing_source))
    if missing_destination:
        raise HumanRestConversionError(
            'Destination MDL lacks required bones: ' + ', '.join(missing_destination))
    mismatched = []
    for name in sorted(required):
        source_parent = source.parents.get(name, '')
        destination_parent = destination.parents.get(name, '')
        if source_parent != destination_parent:
            mismatched.append('%s (%s != %s)' % (
                name, source_parent or '<root>', destination_parent or '<root>'))
    if mismatched:
        raise HumanRestConversionError(
            'Source/destination parent hierarchy differs: ' + '; '.join(mismatched))
    return True


def _coerce_graph(value, label):
    if isinstance(value, HumanRestGraph):
        return value
    if hasattr(value, 'items'):
        worlds = {str(name): _finite_matrix(matrix, str(name))
                  for name, matrix in value.items()}
        return HumanRestGraph('', worlds,
                              {name: '' for name in worlds}, '')
    raise TypeError('%s must be a HumanRestGraph or matrix mapping' % label)


def _weight_entries(value):
    if hasattr(value, 'items'):
        return [(str(name), float(amount)) for name, amount in value.items()]
    return [(str(name), float(amount)) for name, amount in value]


def build_transfer_matrices(source, destination, source_bridge=None,
                            destination_bridge=None):
    """Build model-space rest transforms for corresponding bone names.

    With no explicit bridges this is the conventional ``D @ S^-1`` contract.
    Imported human PLY meshes pass each MDL's immediate ancestor bridge, which
    yields ``B_dst^-1 @ D @ S^-1 @ B_src`` and prevents basis mirror baking.
    """
    source = _coerce_graph(source, 'source')
    destination = _coerce_graph(destination, 'destination')
    source_bridge = (_finite_matrix(source_bridge, 'source bridge')
                     if source_bridge is not None else Matrix.Identity(4))
    destination_bridge = (_finite_matrix(destination_bridge, 'destination bridge')
                          if destination_bridge is not None else Matrix.Identity(4))
    destination_inverse_bridge = destination_bridge.inverted()
    transforms = {}
    for name in sorted(set(source.bones).intersection(destination.bones)):
        source_matrix = _finite_matrix(source.bones[name], name + ' source')
        destination_matrix = _finite_matrix(destination.bones[name], name + ' destination')
        transforms[name] = (destination_inverse_bridge @ destination_matrix
                            @ source_matrix.inverted() @ source_bridge)
    return transforms


def _weighted_matrix(weights, transforms):
    """Return one normalized affine matrix for a vertex's influences."""
    total = 0.0
    result = Matrix(((0.0, 0.0, 0.0, 0.0),
                     (0.0, 0.0, 0.0, 0.0),
                     (0.0, 0.0, 0.0, 0.0),
                     (0.0, 0.0, 0.0, 0.0)))
    for name, weight in weights:
        amount = float(weight)
        if amount <= WEIGHT_EPSILON:
            continue
        matrix = transforms.get(name)
        if matrix is None:
            raise HumanRestConversionError(
                'No rest correspondence for weighted bone: ' + str(name))
        total += amount
        for row in range(4):
            for column in range(4):
                result[row][column] += amount * matrix[row][column]
    if total <= WEIGHT_EPSILON:
        return None
    result *= 1.0 / total
    return result


def convert_rest_space(vertices, weights, source_world, destination_world,
                       source_bridge=None, destination_bridge=None,
                       inverse_effective=False):
    """Convert vectors using weighted corresponding rest transforms.

    ``vertices`` is an iterable of Blender ``Vector``-compatible values.
    ``weights`` is one iterable per vertex containing ``(bone_name, weight)``.
    By default vertices are transformed by the weighted destination/source
    matrix. ``inverse_effective`` builds that same forward A->B matrix and
    inverts the blended result, useful for an exact A->B->A undo. The source
    and destination arguments keep the same A/B order in both modes.
    """
    vertices = list(vertices)
    weights = list(weights)
    if len(vertices) != len(weights):
        raise ValueError('vertices and weights must have equal lengths')
    source = _coerce_graph(source_world, 'source_world')
    destination = _coerce_graph(destination_world, 'destination_world')
    # Always build A->B first. A weighted blend is not generally invertible
    # by blending each bone's inverse, so reverse mode inverts each complete
    # per-vertex affine after the blend has been assembled.
    transforms = build_transfer_matrices(
        source, destination, source_bridge, destination_bridge)
    output = []
    unweighted = 0
    for value, vertex_weights in zip(vertices, weights):
        vector = Vector(value)
        entries = _weight_entries(vertex_weights)
        matrix = _weighted_matrix(entries, transforms)
        if matrix is None:
            output.append(vector.copy())
            unweighted += 1
        else:
            if inverse_effective:
                # Invert the forward blend, rather than blending inverse bones.
                try:
                    matrix = matrix.inverted()
                except (ValueError, ZeroDivisionError) as exc:
                    raise HumanRestConversionError(
                        'Weighted rest transform is singular') from exc
            output.append(matrix @ vector)
    return output, {'unweighted': unweighted, 'transformed': len(output) - unweighted}


def _matrix_close(left, right, tolerance=1.0e-5):
    return max(abs(float(left[row][column] - right[row][column]))
               for row in range(4) for column in range(4)) <= tolerance


def _set_identity_pose(armature):
    try:
        for pose_bone in armature.pose.bones:
            pose_bone.matrix_basis = Matrix.Identity(4)
    except (AttributeError, RuntimeError):
        pass


def _pose_is_identity(armature, tolerance=1.0e-5):
    try:
        for pose_bone in armature.pose.bones:
            if not _matrix_close(pose_bone.matrix_basis, Matrix.Identity(4), tolerance):
                return False
    except (AttributeError, RuntimeError):
        return False
    return True


def _assert_identity_pose(armature, label='Armature'):
    if not _pose_is_identity(armature):
        raise HumanRestConversionError(
            '%s is posed; rest conversion requires an identity pose' % label)


def has_raw_mdl_contract(armature):
    """Return whether an armature retains the minimum raw human MDL contract."""
    return bool(
        armature is not None
        and armature.get('gem2_world_mats')
        and armature.get('gem2_parents')
        and armature.get('gem2_mesh_parent'))


def _assert_human_display_source(armature, label='Armature'):
    """Reject only legacy PMX rigs that lack a raw human MDL contract.

    Older native GEM2 human .blend files can carry ``mowas2_frame0_rest``
    because their display bones were projected with the PMX-era mirror step.
    If raw world matrices, parents, and a mesh attachment are retained, that
    rig is repairable and must be admitted to the human converter.  A pure PMX
    source armature has no such contract and remains rejected.
    """
    if (armature.get('mowas2_frame0_rest')
            and not armature.get('gem2_human_rest_display_rest')
            and not has_raw_mdl_contract(armature)):
        raise HumanRestConversionError(
            '%s uses the legacy PMX frame-0 rest without raw human MDL '
            'metadata; import a raw human MDL/PLY or rebuild it through '
            'the human conversion path' % label)


def _assert_human_display_target(armature, label='Destination armature'):
    """Reject a pure legacy PMX target; raw native targets are repairable."""
    if (armature.get('mowas2_frame0_rest')
            and not armature.get('gem2_human_rest_display_rest')
            and not has_raw_mdl_contract(armature)):
        raise HumanRestConversionError(
            '%s uses the legacy PMX frame-0 display rest without raw human MDL '
            'metadata; build a raw-rest human target first' % label)


def _mode_set_for_armature(armature, mode):
    """Set mode with a complete context override when the UI context is thin."""
    try:
        bpy.ops.object.mode_set(mode=mode)
        return
    except RuntimeError as first_error:
        window = getattr(bpy.context, 'window', None)
        screen = getattr(window, 'screen', None) if window else None
        areas = list(getattr(screen, 'areas', ()) if screen else ())
        area = next((item for item in areas if item.type == 'VIEW_3D'), None)
        region = None
        if area is not None:
            region = next((item for item in area.regions
                           if item.type == 'WINDOW'), None)
        if window is None or screen is None or area is None or region is None:
            raise first_error
        try:
            with bpy.context.temp_override(
                    window=window, screen=screen, area=area, region=region,
                    active_object=armature, object=armature,
                    selected_objects=[armature],
                    selected_editable_objects=[armature]):
                bpy.ops.object.mode_set(mode=mode)
        except (RuntimeError, TypeError):
            raise first_error


def apply_human_display_rest(armature, graph=None, tolerance=1.0e-4,
                              mark_raw=False):
    """Rebuild a Blender armature from normalized raw GEM rest matrices.

    ``gem2_world_mats`` contains the raw MDL frames and is deliberately left
    untouched.  Blender receives the ancestor-normalized frames instead.  A
    reflected foot frame is made proper by flipping local X, which preserves
    its local Y length direction and avoids Blender's arbitrary polar
    projection of an improper matrix.
    """
    if armature is None or getattr(armature, 'type', None) != 'ARMATURE':
        raise HumanRestConversionError('Human display rest requires an armature')
    if getattr(armature, 'mode', 'OBJECT') != 'OBJECT':
        raise HumanRestConversionError(
            'Human display rest rebuild requires Object mode')
    _assert_identity_pose(armature, armature.name)
    if graph is None:
        graph = graph_from_armature(armature)
    if not isinstance(graph, HumanRestGraph):
        raise HumanRestConversionError('Invalid human display rest graph')
    expected = display_world_matrices(graph)
    missing = sorted(name for name in expected
                     if armature.data.bones.get(name) is None)
    if missing:
        raise HumanRestConversionError(
            'Display rest is missing Blender bones: ' + ', '.join(missing))

    previous_active = bpy.context.view_layer.objects.active
    previous_selected = [obj for obj in bpy.context.selected_objects]
    try:
        for obj in previous_selected:
            obj.select_set(False)
        armature.select_set(True)
        bpy.context.view_layer.objects.active = armature
        _mode_set_for_armature(armature, 'EDIT')
        try:
            edit_bones = armature.data.edit_bones
            depths = {}

            def depth(name, visiting=None):
                if name in depths:
                    return depths[name]
                if visiting is None:
                    visiting = set()
                if name in visiting:
                    raise HumanRestConversionError(
                        'Display rest parent cycle: ' + name)
                visiting.add(name)
                parent = graph.parents.get(name, '')
                value = 1 + depth(parent, visiting) if parent in expected else 0
                visiting.remove(name)
                depths[name] = value
                return value

            ordered = sorted(expected, key=lambda name: (depth(name), name))
            for name in ordered:
                edit_bone = edit_bones[name]
                parent_name = graph.parents.get(name, '')
                # MDL child heads are authoritative; connected EditBones would
                # silently snap them to the parent's tail during assignment.
                edit_bone.use_connect = False
                edit_bone.parent = (edit_bones.get(parent_name)
                                    if parent_name in expected else None)
            for name in ordered:
                edit_bones[name].matrix = expected[name]

            # Length changes do not alter the rest matrix when they follow the
            # assigned local Y axis, but make the otherwise tiny display bones
            # readable in the viewport.
            for name in ordered:
                edit_bone = edit_bones[name]
                axis = edit_bone.matrix.col[1].xyz
                if axis.length <= EPSILON:
                    edit_bone.length = DISPLAY_LENGTH
                    continue
                distances = [
                    (child.head - edit_bone.head).length
                    for child in edit_bone.children]
                length = max(distances) if distances else DISPLAY_LENGTH
                edit_bone.length = max(DISPLAY_LENGTH,
                                        min(float(length), 100.0))
        finally:
            _mode_set_for_armature(armature, 'OBJECT')
    finally:
        for obj in list(bpy.context.selected_objects):
            obj.select_set(False)
        for obj in previous_selected:
            try:
                if bpy.context.scene.objects.get(obj.name) is not None:
                    obj.select_set(True)
            except (AttributeError, ReferenceError, RuntimeError):
                pass
        try:
            bpy.context.view_layer.objects.active = (
                previous_active if previous_active is not None
                and bpy.context.scene.objects.get(previous_active.name) is not None
                else None)
        except (AttributeError, ReferenceError, RuntimeError):
            bpy.context.view_layer.objects.active = None

    maximum, worst = _matrix_delta_map(armature, expected)
    if maximum > tolerance:
        raise HumanRestConversionError(
            'Blender display rest differs from normalized MDL rest: '
            '%s (%.6g)' % (worst or '?', maximum))
    # ``mowas2_frame0_rest`` belongs to the legacy PMX Y-mirror display
    # convention.  A normalized human display rest has its own marker; keeping
    # both markers makes the PMX resolver treat this armature as interchangeable
    # with the legacy target and was the source of the recent regression.
    try:
        del armature['mowas2_frame0_rest']
    except KeyError:
        pass
    try:
        del armature['gem2_frame0_rest_mode']
    except KeyError:
        pass
    if mark_raw and armature.get('gem2_world_mats') is not None:
        armature['gem2_human_rest_raw_rest'] = True
    armature['gem2_human_rest_display_rest'] = True
    armature['gem2_human_rest_display_version'] = DISPLAY_REST_VERSION
    return {'max_delta': float(maximum), 'worst_bone': worst,
            'bones': len(expected)}


def _capture_pose_state(armature):
    try:
        return (armature,
                [(bone, bone.matrix_basis.copy())
                 for bone in armature.pose.bones],
                armature.data.pose_position)
    except (AttributeError, RuntimeError):
        return armature, [], None


def _restore_pose_state(state):
    armature, bones, pose_position = state
    for bone, matrix_basis in bones:
        try:
            bone.matrix_basis = matrix_basis
        except (ReferenceError, RuntimeError):
            pass
    if pose_position is not None and armature is not None:
        try:
            armature.data.pose_position = pose_position
        except (AttributeError, ReferenceError, RuntimeError):
            pass


def _current_conversion(mesh):
    value = _json_property(mesh, 'gem2_human_rest_conversion')
    return value if isinstance(value, dict) else None


def conversion_metadata(mesh):
    """Return stored conversion metadata without exposing ID-property details."""
    return dict(_current_conversion(mesh) or {})


def _conversion_is_reverse(mesh, source, destination,
                           source_armature=None, destination_armature=None):
    previous = _current_conversion(mesh)
    try:
        version = int(previous.get('version', -1)) if previous else -1
    except (TypeError, ValueError):
        version = -1
    # Version 1 used the same raw affine transfer; it is safe to reverse once
    # the armatures have been rebuilt into the normalized display space.  New
    # forward conversions are always written with VERSION.
    if version not in (1, VERSION):
        return False
    previous_destination = str(previous.get('destination_mdl') or '')
    previous_source = str(previous.get('source_mdl') or '')
    if not previous_destination or not previous_source or not source.path or not destination.path:
        return False
    path_match = (
        os.path.normcase(os.path.abspath(previous_destination))
        == os.path.normcase(os.path.abspath(source.path))
        and os.path.normcase(os.path.abspath(previous_source))
        == os.path.normcase(os.path.abspath(destination.path)))
    previous_source_fingerprint = str(
        previous.get('source_graph_fingerprint') or '')
    previous_destination_fingerprint = str(
        previous.get('destination_graph_fingerprint') or '')
    if previous_source_fingerprint and previous_destination_fingerprint:
        # Prefer the raw graph identity when available. This admits a copied
        # blend whose absolute source MDL path points at another installation,
        # while still rejecting a same-named but different skeleton.
        if (previous_destination_fingerprint != rest_graph_fingerprint(source)
                or previous_source_fingerprint != rest_graph_fingerprint(destination)):
            return False
    elif not path_match:
        # Version-1 and hand-authored records have no graph digest; retain the
        # original strict path contract for those records.
        return False
    if source_armature is not None or destination_armature is not None:
        if source_armature is None or destination_armature is None:
            return False
        # Object names are deliberately not compared: scene conversion may
        # create a raw-rest clone of the same MDL when the original rig is a
        # legacy frame-0 display copy. The normalized MDL paths and attachment
        # parents below are the stable identity for exact reversal.
        previous_destination_parent = previous.get('destination_mesh_parent')
        previous_source_parent = previous.get('source_mesh_parent')
        # Early v1 records did not always persist attachment-parent fields;
        # graph validation still checks the current raw MDL contracts.
        if (previous_destination_parent is not None
                and previous_destination_parent != source.mesh_parent):
            return False
        if (previous_source_parent is not None
                and previous_source_parent != destination.mesh_parent):
            return False
    return True


def _mesh_armature_modifiers(mesh):
    return [modifier for modifier in mesh.modifiers
            if modifier.type == 'ARMATURE']


def _same_rna(left, right):
    if left is right:
        return True
    if left is None or right is None:
        return False
    try:
        return int(left.as_pointer()) == int(right.as_pointer())
    except (AttributeError, TypeError, ValueError, ReferenceError):
        return False


def _modifier_key(modifier):
    try:
        return int(modifier.as_pointer())
    except (AttributeError, TypeError, ValueError):
        return id(modifier)


def _capture_mesh_state(mesh, destination_armature=None):
    shape_keys = getattr(mesh.data, 'shape_keys', None)
    key_blocks = list(shape_keys.key_blocks) if shape_keys is not None else []
    loop_normals = []
    has_custom_normals = bool(getattr(mesh.data, 'has_custom_normals', False))
    if has_custom_normals:
        try:
            mesh.data.calc_normals_split()
            loop_normals = [loop.normal.copy() for loop in mesh.data.loops]
        except (AttributeError, RuntimeError):
            has_custom_normals = False
    pose = []
    pose_position = None
    if destination_armature is not None:
        try:
            pose = [(bone, bone.matrix_basis.copy())
                    for bone in destination_armature.pose.bones]
            pose_position = destination_armature.data.pose_position
        except (AttributeError, RuntimeError):
            pass
    return {
        'mesh': mesh,
        'coordinates': [vertex.co.copy() for vertex in mesh.data.vertices],
        'parent': mesh.parent,
        'parent_type': mesh.parent_type,
        'parent_bone': mesh.parent_bone,
        'matrix_parent_inverse': mesh.matrix_parent_inverse.copy(),
        'matrix_world': mesh.matrix_world.copy(),
        'modifiers': [(modifier, modifier.object)
                      for modifier in _mesh_armature_modifiers(mesh)],
        'modifier_ids': {_modifier_key(modifier)
                         for modifier in _mesh_armature_modifiers(mesh)},
        'key_blocks': key_blocks,
        'key_coordinates': [
            [item.co.copy() for item in block.data] for block in key_blocks],
        'has_custom_normals': has_custom_normals,
        'loop_normals': loop_normals,
        'properties': {key: mesh.get(key) for key in (
            'gem2_human_rest_conversion', 'gem2_skin_order',
            'gem2_human_rest_source_mdl',
            'gem2_human_rest_destination_mdl',
            'gem2_human_rest_version') if mesh.get(key) is not None},
        'active_shape_key_index': getattr(mesh, 'active_shape_key_index', 0),
        'pose': pose,
        'pose_armature': destination_armature,
        'pose_position': pose_position,
    }


def _restore_mesh_state(state):
    mesh = state['mesh']
    for vertex, coordinate in zip(mesh.data.vertices, state['coordinates']):
        vertex.co = coordinate
    for block, coordinates in zip(state['key_blocks'], state['key_coordinates']):
        for item, coordinate in zip(block.data, coordinates):
            item.co = coordinate
    if state['has_custom_normals'] and state['loop_normals']:
        try:
            mesh.data.normals_split_custom_set(state['loop_normals'])
        except (AttributeError, RuntimeError):
            pass
    mesh.data.update()
    try:
        mesh.parent = state['parent']
        mesh.parent_type = state['parent_type']
        mesh.parent_bone = state['parent_bone']
        mesh.matrix_parent_inverse = state['matrix_parent_inverse']
        mesh.matrix_world = state['matrix_world']
    except (AttributeError, ReferenceError, RuntimeError):
        pass
    for modifier in list(_mesh_armature_modifiers(mesh)):
        if _modifier_key(modifier) not in state['modifier_ids']:
            try:
                mesh.modifiers.remove(modifier)
            except (ReferenceError, RuntimeError):
                pass
    for modifier, target in state['modifiers']:
        try:
            modifier.object = target
        except (ReferenceError, RuntimeError):
            pass
    for key in ('gem2_human_rest_conversion', 'gem2_skin_order',
                'gem2_human_rest_source_mdl',
                'gem2_human_rest_destination_mdl',
                'gem2_human_rest_version'):
        try:
            del mesh[key]
        except KeyError:
            pass
    for key, value in state['properties'].items():
        mesh[key] = value
    try:
        mesh.active_shape_key_index = state['active_shape_key_index']
    except (AttributeError, RuntimeError):
        pass
    for pose_bone, matrix_basis in state['pose']:
        try:
            pose_bone.matrix_basis = matrix_basis
        except (ReferenceError, RuntimeError):
            pass
    if state['pose_position'] is not None:
        try:
            state['pose_armature'].data.pose_position = state['pose_position']
        except (AttributeError, RuntimeError):
            pass


def _validate_mesh_binding(mesh, source_armature, destination_armature):
    modifiers = _mesh_armature_modifiers(mesh)
    if len(modifiers) != 1:
        if not modifiers:
            raise HumanRestConversionError(
                'Mesh must have one source Armature modifier')
        raise HumanRestConversionError(
            'Mesh must have exactly one Armature modifier')
    bound = modifiers[0].object
    if not _same_rna(bound, source_armature):
        if _same_rna(bound, destination_armature):
            raise HumanRestConversionError(
                'Mesh is already bound to the destination armature')
        name = getattr(bound, 'name', '<none>')
        raise HumanRestConversionError(
            'Mesh Armature modifier is bound to %s, not the source armature'
            % name)


def _rebind_mesh(mesh, source_armature, destination_armature):
    """Retarget the sole validated Armature modifier in place."""
    modifiers = _mesh_armature_modifiers(mesh)
    if len(modifiers) != 1 or not _same_rna(
            modifiers[0].object, source_armature):
        raise HumanRestConversionError(
            'Mesh binding changed before the conversion could be committed')
    modifiers[0].object = destination_armature

    # A number of imported Blender files parent the mesh object to the source
    # armature as well as using an Armature modifier.  Once the modifier is
    # rebound, retaining that object parent makes exporters see two armatures
    # and can apply the source transform a second time.  Keep the evaluated
    # world placement, then detach only the stale source-armature parent.
    if _same_rna(getattr(mesh, 'parent', None), source_armature):
        world = mesh.matrix_world.copy()
        mesh.parent = None
        # Clear the old bone-relative bookkeeping as well. It is ignored while
        # unparented, but would reintroduce a hidden source transform if a user
        # later parents the converted mesh again.
        mesh.parent_type = 'OBJECT'
        mesh.parent_bone = ''
        mesh.matrix_parent_inverse = Matrix.Identity(4)
        mesh.matrix_world = world
    return modifiers[0]


def _destination_skin_order(mesh, destination, preferred_order=None):
    existing = [group.name for group in mesh.vertex_groups]
    weighted = set()
    for vertex in mesh.data.vertices:
        for assignment in vertex.groups:
            if (0 <= assignment.group < len(existing)
                    and float(assignment.weight) > WEIGHT_EPSILON):
                weighted.add(existing[assignment.group])
    preferred = list(preferred_order or ())
    result = []
    for name in preferred + list(existing):
        if name in result or name not in weighted:
            continue
        if name in destination.bones:
            result.append(name)
    if not result:
        result = [name for name in existing
                  if name in weighted and name in destination.bones]
    return result


def _store_conversion_metadata(mesh, source, destination, order, report,
                               source_armature, destination_armature):
    payload = {
        'version': VERSION,
        'source_mdl': source.path,
        'destination_mdl': destination.path,
        'source_graph_fingerprint': rest_graph_fingerprint(source),
        'destination_graph_fingerprint': rest_graph_fingerprint(destination),
        'source_armature': source_armature.name,
        'destination_armature': destination_armature.name,
        'source_mesh_parent': source.mesh_parent,
        'destination_mesh_parent': destination.mesh_parent,
        'skin_order': list(order),
        'unweighted_vertices': int(report.get('unweighted', 0)),
        'transformed_vertices': int(report.get('transformed', 0)),
        'inverse_effective': bool(report.get('inverse_effective', False)),
        'display_space': 'normalized_model',
        'display_rest_version': DISPLAY_REST_VERSION,
    }
    mesh['gem2_human_rest_conversion'] = json.dumps(payload, sort_keys=True)
    mesh['gem2_skin_order'] = json.dumps(list(order))
    mesh['gem2_human_rest_source_mdl'] = source.path
    mesh['gem2_human_rest_destination_mdl'] = destination.path
    mesh['gem2_human_rest_version'] = VERSION


def convert_mesh(mesh, source_armature, destination_armature,
                 source_graph=None, destination_graph=None,
                 preferred_skin_order=None, allow_unweighted=True,
                 exact_reverse=True, _rollback_on_error=True,
                 _prepare_display=True):
    """Convert one already-skinned human mesh in place and rebind it.

    The source and destination armatures are repaired from their raw MDL
    metadata before the affine is applied.  ``_prepare_display`` is private
    and is disabled by ``convert_meshes`` after its one-time preparation.
    """
    if mesh is None or getattr(mesh, 'type', None) != 'MESH':
        raise HumanRestConversionError('Expected a mesh object')
    if source_armature is None or destination_armature is None:
        raise HumanRestConversionError('Both source and destination armatures are required')
    if _same_rna(source_armature, destination_armature):
        raise HumanRestConversionError('Source and destination armatures must differ')
    _assert_human_display_source(source_armature, 'Source armature')
    _assert_human_display_target(destination_armature)
    for obj, label in ((mesh, 'mesh'),
                       (source_armature, 'source armature'),
                       (destination_armature, 'destination armature')):
        if getattr(obj, 'mode', 'OBJECT') != 'OBJECT':
            raise HumanRestConversionError(
                '%s must be in Object mode' % label.capitalize())
    if getattr(mesh.data, 'users', 1) > 1:
        raise HumanRestConversionError(
            'Mesh data is shared by multiple objects; make a copy before conversion')
    _validate_mesh_binding(mesh, source_armature, destination_armature)
    _assert_identity_pose(source_armature, 'Source armature')
    _assert_identity_pose(destination_armature, 'Destination armature')
    source_graph = source_graph or graph_from_armature(source_armature)
    destination_graph = destination_graph or graph_from_armature(destination_armature)
    group_names, weighted_names = _weight_names_from_mesh(mesh)
    validate_compatible(source_graph, destination_graph, weighted_names)
    unknown = sorted(name for name in weighted_names
                     if name not in source_graph.bones
                     or name not in destination_graph.bones)
    if unknown:
        raise HumanRestConversionError(
            'Weighted groups are absent from a rest graph: ' + ', '.join(unknown))

    if _prepare_display:
        # A legacy frame-0 armature without retained raw metadata is rejected
        # above; otherwise make both rigs use the same normalized display rest.
        apply_human_display_rest(source_armature, source_graph)
        apply_human_display_rest(destination_armature, destination_graph)

    source_bridge = source_graph.ancestor_bridge()
    destination_bridge = destination_graph.ancestor_bridge()
    # The mesh coordinates here are the imported Blender coordinates, i.e. the
    # source PLY has already been multiplied by its local skin attachment
    # matrix.  T therefore maps source ancestor/model space to destination
    # ancestor/model space; the destination local attachment is peeled only by
    # the exporter when it serializes the converted mesh back to PLY.
    inverse_effective = bool(exact_reverse and _conversion_is_reverse(
        mesh, source_graph, destination_graph,
        source_armature=source_armature,
        destination_armature=destination_armature))
    if inverse_effective:
        transforms = build_transfer_matrices(
            destination_graph, source_graph, destination_bridge, source_bridge)
    else:
        transforms = build_transfer_matrices(
            source_graph, destination_graph, source_bridge, destination_bridge)

    mesh_world = mesh.matrix_world.copy()
    try:
        mesh_world_inverse = mesh_world.inverted()
    except (ValueError, ZeroDivisionError) as exc:
        raise HumanRestConversionError('Mesh object transform is singular') from exc
    source_object_world = source_armature.matrix_world.copy()
    destination_object_world = destination_armature.matrix_world.copy()
    try:
        source_object_inverse = source_object_world.inverted()
    except (ValueError, ZeroDivisionError) as exc:
        raise HumanRestConversionError('Source armature transform is singular') from exc
    group_names = list(group_names)
    new_coordinates = []
    per_vertex_object_transforms = []
    unweighted = 0
    for vertex in mesh.data.vertices:
        entries = []
        for assignment in vertex.groups:
            amount = float(assignment.weight)
            if amount <= WEIGHT_EPSILON:
                continue
            if assignment.group >= len(group_names):
                raise HumanRestConversionError(
                    'Invalid vertex group index on vertex %d' % vertex.index)
            entries.append((group_names[assignment.group], amount))
        blend = _weighted_matrix(entries, transforms)
        if blend is None:
            if not allow_unweighted:
                raise HumanRestConversionError(
                    'Vertex %d has no usable bone weight' % vertex.index)
            new_coordinates.append(vertex.co.copy())
            per_vertex_object_transforms.append(Matrix.Identity(4))
            unweighted += 1
            continue
        if inverse_effective:
            try:
                blend = blend.inverted()
            except (ValueError, ZeroDivisionError) as exc:
                raise HumanRestConversionError(
                    'Weighted rest transform is singular at vertex %d'
                    % vertex.index) from exc
        object_transform = (mesh_world_inverse @ destination_object_world
                            @ blend @ source_object_inverse @ mesh_world)
        new_coordinates.append(object_transform @ vertex.co)
        per_vertex_object_transforms.append(object_transform)

    # All validation and calculations finish before mutating Blender data.
    state = _capture_mesh_state(mesh, destination_armature)
    key_blocks = state['key_blocks']
    old_shape_key_coordinates = state['key_coordinates']
    old_loop_normals = state['loop_normals']
    had_custom_normals = state['has_custom_normals']
    try:
        for vertex, coordinate in zip(mesh.data.vertices, new_coordinates):
            vertex.co = coordinate
        if key_blocks:
            # Shape keys are in the same object space as the base vertices.
            # Apply the per-vertex affine used by the rest transfer to every
            # key, otherwise a later key activation would restore old-space data.
            for block_index, block in enumerate(key_blocks):
                original_coordinates = old_shape_key_coordinates[block_index]
                for index, item in enumerate(block.data):
                    object_transform = per_vertex_object_transforms[index]
                    item.co = object_transform @ original_coordinates[index]
        if had_custom_normals and old_loop_normals:
            converted_normals = []
            for loop, normal in zip(mesh.data.loops, old_loop_normals):
                object_transform = per_vertex_object_transforms[loop.vertex_index]
                if object_transform is None:
                    converted_normals.append(normal)
                    continue
                try:
                    normal_matrix = (object_transform.to_3x3().inverted().transposed())
                    converted = normal_matrix @ normal
                except (ValueError, ZeroDivisionError):
                    converted = object_transform.to_3x3() @ normal
                if converted.length > EPSILON:
                    converted.normalize()
                converted_normals.append(converted)
            mesh.data.normals_split_custom_set(converted_normals)
        mesh.data.update()
        _rebind_mesh(mesh, source_armature, destination_armature)
        _set_identity_pose(destination_armature)
        order = _destination_skin_order(
            mesh, destination_graph, preferred_order=preferred_skin_order)
        report = {
            'transformed': len(new_coordinates) - unweighted,
            'unweighted': unweighted,
            'inverse_effective': inverse_effective,
            'source': source_graph.path,
            'destination': destination_graph.path,
            'skin_order': order,
        }
        _store_conversion_metadata(
            mesh, source_graph, destination_graph, order, report,
            source_armature, destination_armature)
        bpy.context.view_layer.update()
        return report
    except Exception:
        if _rollback_on_error:
            _restore_mesh_state(state)
        raise


def convert_meshes(meshes, source_armature, destination_armature,
                   source_graph=None, destination_graph=None,
                   preferred_skin_order=None, allow_unweighted=True,
                   exact_reverse=True, _prepare_display=True):
    """Atomically convert a collection of meshes sharing one source rig."""
    meshes = list(meshes or [])
    if not meshes:
        raise HumanRestConversionError('No meshes were supplied')
    source_graph = source_graph or graph_from_armature(source_armature)
    destination_graph = destination_graph or graph_from_armature(destination_armature)
    _assert_human_display_source(source_armature, 'Source armature')
    _assert_human_display_target(destination_armature)
    if _prepare_display:
        apply_human_display_rest(source_armature, source_graph)
        apply_human_display_rest(destination_armature, destination_graph)
    snapshots = [_capture_mesh_state(mesh, destination_armature)
                 for mesh in meshes]
    reports = []
    try:
        for mesh in meshes:
            reports.append(convert_mesh(
                mesh, source_armature, destination_armature,
                source_graph=source_graph, destination_graph=destination_graph,
                preferred_skin_order=preferred_skin_order,
                allow_unweighted=allow_unweighted,
                exact_reverse=exact_reverse,
                _rollback_on_error=False, _prepare_display=False))
    except Exception:
        for state in snapshots:
            _restore_mesh_state(state)
        raise
    return {
        'meshes': len(reports),
        'vertices': sum(report['transformed'] + report['unweighted']
                         for report in reports),
        'transformed': sum(report['transformed'] for report in reports),
        'unweighted': sum(report['unweighted'] for report in reports),
        'inverse_effective': any(report['inverse_effective'] for report in reports),
        'source': source_graph.path,
        'destination': destination_graph.path,
        'skin_order': reports[0]['skin_order'],
    }


def max_rest_matrix_delta(armature, graph):
    """Compare stored raw matrices with a graph, ignoring Blender tail display."""
    current = _json_property(armature, 'gem2_world_mats', {})
    if not hasattr(current, 'items'):
        return float('inf')
    maximum = 0.0
    for name, expected in graph.bones.items():
        if name not in current:
            return float('inf')
        try:
            actual = Matrix(current[name])
        except (IndexError, TypeError, ValueError):
            return float('inf')
        maximum = max(maximum, max(
            abs(float(actual[row][column] - expected[row][column]))
            for row in range(4) for column in range(4)))
    return maximum


def evaluated_rest_delta(mesh, armature=None):
    """Return max evaluated-vs-source coordinate delta at an identity rest pose.

    A diagnostic query must not leave a user's rig in the temporary identity
    pose, so the original pose and pose-position are restored on every exit.
    """
    pose_state = _capture_pose_state(armature) if armature is not None else None
    evaluated = None
    try:
        if armature is not None:
            _set_identity_pose(armature)
            bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = mesh.evaluated_get(depsgraph)
        evaluated_mesh = evaluated.to_mesh()
        try:
            if len(evaluated_mesh.vertices) != len(mesh.data.vertices):
                return float('inf')
            maximum = 0.0
            for left, right in zip(mesh.data.vertices, evaluated_mesh.vertices):
                maximum = max(maximum, (left.co - right.co).length)
            return maximum
        finally:
            evaluated.to_mesh_clear()
    finally:
        if pose_state is not None:
            _restore_pose_state(pose_state)
            try:
                bpy.context.view_layer.update()
            except (AttributeError, RuntimeError):
                pass


def conversion_summary(report):
    """Produce a compact human-readable report for the Blender panel."""
    return (
        'Human rest conversion: %d mesh(es), %d transformed vertices, '
        '%d unweighted, %s -> %s%s'
    ) % (
        int(report.get('meshes', 1)),
        int(report.get('transformed', 0)),
        int(report.get('unweighted', 0)),
        os.path.basename(str(report.get('source') or '?')),
        os.path.basename(str(report.get('destination') or '?')),
        ' (exact reverse)' if report.get('inverse_effective') else '',
    )


__all__ = (
    'GOH_SKIN_ORDER',
    'MOWAS2_SKIN_ORDER',
    'HUMAN_SKIN_ORDER',
    'HumanRestConversionError',
    'HumanRestGraph',
    'apply_human_display_rest',
    'build_transfer_matrices',
    'display_world_matrices',
    'conversion_metadata',
    'conversion_summary',
    'convert_mesh',
    'convert_meshes',
    'convert_rest_space',
    'evaluated_rest_delta',
    'graph_from_armature',
    'load_rest_graph',
    'max_rest_matrix_delta',
    'required_display_rest_delta',
    'skin_order_for_route',
    'validate_compatible',
)
