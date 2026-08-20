# -*- coding: utf-8 -*-
"""GEM/GOH EANM animation import and export.

The Blender armature used by this add-on is an editable, right-handed bone rig,
while the MDL bind matrices kept in ``gem2_world_mats`` retain GEM's mirrored
matrix signs.  ANM keys therefore cannot be assigned directly to
``PoseBone.matrix_basis``.  Import reconstructs the complete GEM pose, derives
its bind-pose deformation delta, conjugates it from engine world through the
MDL mesh-parent ancestor into Blender/PLY model space, and applies it to the
Blender rest bones. Export performs the exact inverse mapping.
"""

import base64
import bisect
import hashlib
import json
import math
import os
import struct

from mathutils import Matrix, Quaternion, Vector


B_POSITION = 0x0001
B_ORIENTATION = 0x0002
B_MIRRORED = 0x0004
B_VISIBLE_ON = 0x0008
B_VISIBLE_OFF = 0x0010
B_MESH = 0x0020
B_SCALE = 0x0040
KNOWN_FLAGS = (B_POSITION | B_ORIENTATION | B_MIRRORED |
               B_VISIBLE_ON | B_VISIBLE_OFF | B_MESH | B_SCALE)
MAX_FILE_SIZE = 512 * 1024 * 1024
MAX_DECLARED_FRAMES = 65536
MAX_BAKED_FRAMES = 20000
MAX_BONES = 255
MAX_BONE_NAME_SIZE = 4096
MAX_MESH_CHUNK_SIZE = 64 * 1024 * 1024
REFLECT_Z_3 = Matrix(((1.0, 0.0, 0.0),
                      (0.0, 1.0, 0.0),
                      (0.0, 0.0, -1.0)))


class _Reader:
    def __init__(self, data, path):
        self.data = data
        self.path = path
        self.pos = 0

    def _need(self, size, label):
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError('%s 截断: %s @ 0x%X' %
                             (os.path.basename(self.path), label, self.pos))

    def read(self, size, label='data'):
        self._need(size, label)
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def peek(self, size=4):
        return self.data[self.pos:self.pos + size]

    def tag(self, expected=None):
        value = self.read(4, 'FourCC')
        if expected is not None and value != expected:
            raise ValueError('期望 %r, 实际 %r @ 0x%X' %
                             (expected, value, self.pos - 4))
        return value

    def u8(self):
        return self.read(1, 'uint8')[0]

    def u16(self):
        return struct.unpack('<H', self.read(2, 'uint16'))[0]

    def u32(self):
        return struct.unpack('<I', self.read(4, 'uint32'))[0]

    def f32x3(self):
        values = struct.unpack('<3f', self.read(12, 'float3'))
        _require_finite(values, 'float3')
        return values


def _require_finite(values, label):
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError('%s 包含 NaN/Inf' % label)


def _quat_from_xyz(value):
    _require_finite(value, 'ANM quaternion XYZ')
    x, y, z = (float(value[0]), float(value[1]), float(value[2]))
    w2 = 1.0 - x * x - y * y - z * z
    if w2 < -1e-3:
        raise ValueError('ANM 四元数 XYZ 长度超过 1: %r' % (value,))
    q = Quaternion((math.sqrt(max(0.0, w2)), x, y, z))
    if q.magnitude < 1e-12:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    q.normalize()
    return q


def _negated_quaternion(q):
    return Quaternion((-q.w, -q.x, -q.y, -q.z))


def _compose_local_matrix(pos=None, orient=None, scale=None, mirrored=False):
    if pos is not None:
        _require_finite(pos, 'ANM position')
    if orient is not None:
        _require_finite(orient, 'ANM quaternion')
    if scale is not None:
        _require_finite(scale, 'ANM scale')
    q = orient.copy() if orient is not None else Quaternion((1.0, 0.0, 0.0, 0.0))
    if q.magnitude < 1e-12:
        raise ValueError('ANM quaternion 长度为 0')
    q.normalize()
    sx, sy, sz = scale if scale is not None else (1.0, 1.0, 1.0)
    if mirrored:
        sz = -sz
    stretch = Matrix.Diagonal((float(sx), float(sy), float(sz), 1.0))
    matrix = q.to_matrix().to_4x4() @ stretch
    if pos is not None:
        matrix.translation = Vector(pos)
    return matrix


def _matrix_to_components(matrix, tolerance=2e-4):
    """Return GEM position, quaternion, positive scale and mirror sign."""
    _require_finite((matrix[row][column]
                     for row in range(4) for column in range(4)),
                    'ANM matrix')
    linear = matrix.to_3x3()
    columns = [linear.col[i].copy() for i in range(3)]
    scale = Vector((columns[0].length, columns[1].length, columns[2].length))
    if min(scale) < 1e-8:
        raise ValueError('ANM 导出遇到零缩放矩阵')
    ortho = Matrix((columns[0] / scale.x,
                    columns[1] / scale.y,
                    columns[2] / scale.z)).transposed()
    mirrored = ortho.determinant() < 0.0
    rotation = ortho @ REFLECT_Z_3 if mirrored else ortho
    check = rotation.transposed() @ rotation
    error = max(abs(float(check[r][c]) - (1.0 if r == c else 0.0))
                for r in range(3) for c in range(3))
    if error > tolerance or rotation.determinant() < 0.0:
        raise ValueError('ANM 安全导出不支持剪切矩阵 (误差 %.6g)' % error)
    q = rotation.to_quaternion()
    q.normalize()
    if q.w < 0.0:
        q = _negated_quaternion(q)
    return (matrix.translation.copy(), q, scale, mirrored)


def _row43_matrix(values):
    rows = (values[0:3], values[3:6], values[6:9], values[9:12])
    return Matrix(((rows[0][0], rows[1][0], rows[2][0], rows[3][0]),
                   (rows[0][1], rows[1][1], rows[2][1], rows[3][1]),
                   (rows[0][2], rows[1][2], rows[2][2], rows[3][2]),
                   (0.0, 0.0, 0.0, 1.0)))


class AnmKey:
    __slots__ = ('bone', 'flags', 'pos', 'orient', 'scale', 'mesh', 'mesh_size')

    def __init__(self, bone, flags, pos=None, orient=None, scale=None,
                 mesh=None, mesh_size=0):
        self.bone = bone
        self.flags = int(flags)
        self.pos = pos
        self.orient = orient
        self.scale = scale
        self.mesh = mesh
        self.mesh_size = int(mesh_size)

    def local_matrix(self):
        q = _quat_from_xyz(self.orient) if self.orient is not None else None
        return _compose_local_matrix(self.pos, q, self.scale,
                                     bool(self.flags & B_MIRRORED))


class AnmFrame:
    __slots__ = ('frame_no', 'keys')

    def __init__(self, frame_no, keys):
        self.frame_no = int(frame_no)
        self.keys = keys

    def key_of(self, bone):
        for key in self.keys:
            if key.bone == bone:
                return key
        folded = bone.casefold()
        for key in self.keys:
            if key.bone.casefold() == folded:
                return key
        return None


class AnmData:
    """Strict EANM parser with evaluated sparse local tracks."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        file_size = os.path.getsize(self.path)
        if file_size > MAX_FILE_SIZE:
            raise ValueError('ANM 文件超过安全上限: %d bytes' % file_size)
        with open(self.path, 'rb') as handle:
            self.raw_bytes = handle.read()
        self.source_sha256 = hashlib.sha256(self.raw_bytes).hexdigest()
        self.version = (0, 0)
        self.version_u32 = 0
        self.frame_max = 0
        self.bones = []
        self.frames = {}
        self.comment = ''
        self.comment_bytes = b''
        self.frame_format = ''
        self._tracks_cache = None
        self._parse()

    def _parse(self):
        reader = _Reader(self.raw_bytes, self.path)
        reader.tag(b'EANM')
        version_bytes = reader.read(4, 'version')
        self.version = struct.unpack('<HH', version_bytes)
        self.version_u32 = struct.unpack('<I', version_bytes)[0]
        reader.tag(b'FRMS')
        frame_count = reader.u32()
        if frame_count == 0:
            raise ValueError('FRMS 帧数不能为 0')
        if frame_count > MAX_DECLARED_FRAMES:
            raise ValueError('FRMS 帧数超过安全上限: %d' % frame_count)
        self.frame_max = frame_count - 1

        comments = []
        while reader.peek() == b'CMNT':
            reader.tag(b'CMNT')
            size = reader.u16()
            payload = reader.read(size, 'CMNT text')
            comments.append(payload)
        self.comment_bytes = b''.join(comments)
        self.comment = self.comment_bytes.decode('latin-1', 'replace')

        reader.tag(b'BMAP')
        bone_count = reader.u32()
        if bone_count > MAX_BONES:
            raise ValueError('BMAP 骨数超过安全上限: %d' % bone_count)
        for _ in range(bone_count):
            size = reader.u32()
            if size == 0 or size > MAX_BONE_NAME_SIZE:
                raise ValueError('BMAP 骨名长度无效: %d bytes' % size)
            self.bones.append(reader.read(size, 'bone name').decode('latin-1'))
        folded_names = [name.casefold() for name in self.bones]
        if len(set(folded_names)) != len(folded_names):
            raise ValueError('BMAP 含大小写重复骨名')

        next_tag = reader.peek()
        if not next_tag and not self.bones:
            # Native GOH contains an intentional empty-animation placeholder.
            self.frame_format = 'EMPTY'
        elif next_tag == b'FRM2':
            self.frame_format = 'FRM2'
            self._parse_frm2(reader)
        elif next_tag == b'FRMN':
            self.frame_format = 'FRMN'
            self._parse_frmn(reader)
        elif next_tag == b'FRM3':
            raise ValueError('暂不支持 FRM3 动画: %s' % self.path)
        else:
            raise ValueError('未知 ANM 帧格式 %r @ 0x%X' %
                             (next_tag, reader.pos))
        if reader.pos != len(reader.data):
            raise ValueError('ANM 尾部有未解析数据 @ 0x%X' % reader.pos)
        if self.frame_format != 'EMPTY' and not self.frames:
            raise ValueError('动画没有帧: %s' % self.path)

    def _store_frame(self, frame_no, keys):
        if frame_no > self.frame_max:
            raise ValueError('帧 %d 超出 FRMS 最大帧 %d' %
                             (frame_no, self.frame_max))
        if frame_no in self.frames:
            raise ValueError('重复动画帧: %d' % frame_no)
        seen = set()
        for key in keys:
            folded = key.bone.casefold()
            if folded in seen:
                raise ValueError('帧 %d 重复骨键: %s' % (frame_no, key.bone))
            seen.add(folded)
        self.frames[frame_no] = AnmFrame(frame_no, keys)

    def _parse_frm2(self, reader):
        while reader.pos < len(reader.data):
            reader.tag(b'FRM2')
            frame_no = reader.u16()
            key_count = reader.u8()
            keys = []
            for _ in range(key_count):
                bone_index = reader.u8()
                if bone_index >= len(self.bones):
                    raise ValueError('FRM2 骨索引越界: %d' % bone_index)
                flags = reader.u16()
                unknown = flags & ~KNOWN_FLAGS
                if unknown:
                    raise ValueError('未知 ANM key flags: 0x%04X' % flags)
                pos = reader.f32x3() if flags & B_POSITION else None
                orient = reader.f32x3() if flags & B_ORIENTATION else None
                scale = reader.f32x3() if flags & B_SCALE else None
                mesh = None
                mesh_size = 0
                if flags & B_MESH:
                    mesh_size = reader.u32()
                    if mesh_size > MAX_MESH_CHUNK_SIZE:
                        raise ValueError('MESH 子块超过安全上限: %d bytes' %
                                         mesh_size)
                    mesh = reader.read(mesh_size, 'mesh animation')
                keys.append(AnmKey(self.bones[bone_index], flags, pos, orient,
                                   scale, mesh, mesh_size))
            self._store_frame(frame_no, keys)

    def _parse_frmn(self, reader):
        while reader.pos < len(reader.data):
            reader.tag(b'FRMN')
            frame_no = reader.u32()
            keys = []
            while reader.pos < len(reader.data) and reader.peek() != b'FRMN':
                reader.tag(b'BONE')
                bone_index = reader.u32()
                if bone_index >= len(self.bones):
                    raise ValueError('FRMN 骨索引越界: %d' % bone_index)
                matrix = None
                visible = None
                mesh = None
                seen_chunks = set()
                while reader.pos < len(reader.data):
                    tag = reader.peek()
                    if tag in (b'BONE', b'FRMN'):
                        break
                    if tag not in (b'MATR', b'VISI', b'MESH'):
                        raise ValueError('未知 FRMN 子块 %r @ 0x%X' %
                                         (tag, reader.pos))
                    if tag in seen_chunks:
                        raise ValueError('FRMN BONE 重复子块 %r @ 0x%X' %
                                         (tag, reader.pos))
                    seen_chunks.add(tag)
                    if tag == b'MATR':
                        reader.tag(b'MATR')
                        values = struct.unpack('<12f', reader.read(48, 'MATR'))
                        _require_finite(values, 'FRMN MATR')
                        matrix = _row43_matrix(values)
                    elif tag == b'VISI':
                        reader.tag(b'VISI')
                        visible_value = reader.u32()
                        if visible_value not in (0, 1):
                            raise ValueError('FRMN VISI 必须是 0 或 1')
                        visible = bool(visible_value)
                    else:
                        reader.tag(b'MESH')
                        mesh_size = reader.u32()
                        if mesh_size > MAX_MESH_CHUNK_SIZE:
                            raise ValueError('MESH 子块超过安全上限: %d bytes' %
                                             mesh_size)
                        mesh = reader.read(mesh_size, 'MESH')
                if not seen_chunks:
                    raise ValueError('FRMN BONE 不含 MATR/VISI/MESH 子块')
                flags = 0
                pos = orient = scale = None
                if matrix is not None:
                    pos_v, q, scale_v, mirrored = _matrix_to_components(matrix)
                    pos = tuple(pos_v)
                    orient = (q.x, q.y, q.z)
                    flags |= B_POSITION | B_ORIENTATION
                    if mirrored:
                        flags |= B_MIRRORED
                    if max(abs(float(v) - 1.0) for v in scale_v) > 1e-5:
                        flags |= B_SCALE
                        scale = tuple(scale_v)
                if visible is not None:
                    flags |= B_VISIBLE_ON if visible else B_VISIBLE_OFF
                if mesh is not None:
                    flags |= B_MESH
                keys.append(AnmKey(self.bones[bone_index], flags, pos, orient,
                                   scale, mesh, len(mesh) if mesh else 0))
            self._store_frame(frame_no, keys)

    def _tracks(self):
        if self._tracks_cache is not None:
            return self._tracks_cache
        tracks = {bone: {'pos': [], 'rot': [], 'scale': [],
                         'mirror': [], 'visibility': []}
                  for bone in self.bones}
        for frame_no in sorted(self.frames):
            for key in self.frames[frame_no].keys:
                track = tracks[key.bone]
                if key.pos is not None:
                    track['pos'].append((frame_no, Vector(key.pos)))
                if key.orient is not None:
                    track['rot'].append((frame_no, _quat_from_xyz(key.orient)))
                if key.scale is not None:
                    track['scale'].append((frame_no, Vector(key.scale)))
                # Native invariant: every transform key repeats its current
                # matrix sign. Across all 1,592 GOH FRM2 files, mirrored
                # position/orientation-only keys are 0x05/0x06, and no bone
                # track mixes sign states. Visibility-only keys do not update it.
                if key.flags & (B_POSITION | B_ORIENTATION | B_SCALE |
                                B_MIRRORED):
                    track['mirror'].append((frame_no,
                                            bool(key.flags & B_MIRRORED)))
                if key.flags & B_VISIBLE_ON:
                    track['visibility'].append((frame_no, True))
                elif key.flags & B_VISIBLE_OFF:
                    track['visibility'].append((frame_no, False))
        self._tracks_cache = tracks
        return tracks

    @staticmethod
    def _sample(track, frame_no, quaternion=False, step=False):
        if not track:
            return None
        frames = [item[0] for item in track]
        index = bisect.bisect_left(frames, frame_no)
        if index < len(track) and frames[index] == frame_no:
            value = track[index][1]
            return value.copy() if hasattr(value, 'copy') else value
        if index == 0:
            value = track[0][1]
            return value.copy() if hasattr(value, 'copy') else value
        if index >= len(track) or step:
            value = track[index - 1][1]
            return value.copy() if hasattr(value, 'copy') else value
        frame_a, value_a = track[index - 1]
        frame_b, value_b = track[index]
        factor = float(frame_no - frame_a) / float(frame_b - frame_a)
        if quaternion:
            return value_a.slerp(value_b, factor)
        return value_a.lerp(value_b, factor)

    def evaluated_components(self, frame_no):
        """Evaluate sparse tracks at a declared frame using linear/slerp keys."""
        frame_no = max(0.0, min(float(frame_no), float(self.frame_max)))
        result = {}
        for bone, track in self._tracks().items():
            if not any(track.values()):
                continue
            result[bone] = {
                'pos': self._sample(track['pos'], frame_no),
                'rot': self._sample(track['rot'], frame_no, quaternion=True),
                'scale': self._sample(track['scale'], frame_no),
                'mirrored': self._sample(track['mirror'], frame_no, step=True),
                'visible': self._sample(track['visibility'], frame_no, step=True),
            }
        return result

    def frame_local_matrices(self, frame_no, base_frame=0):
        del base_frame
        matrices = {}
        for bone, values in self.evaluated_components(frame_no).items():
            matrices[bone] = _compose_local_matrix(
                values['pos'], values['rot'], values['scale'],
                bool(values['mirrored']))
        return matrices

    def _stream(self):
        """Compatibility view: evaluated state at stored FRM frame numbers."""
        stream = {}
        for frame_no in sorted(self.frames):
            state = {}
            for bone, values in self.evaluated_components(frame_no).items():
                q = values['rot']
                orient = (q.x, q.y, q.z) if q is not None else None
                scale = tuple(values['scale']) if values['scale'] is not None else None
                pos = tuple(values['pos']) if values['pos'] is not None else None
                state[bone] = (pos, orient, scale)
            stream[frame_no] = state
        return stream

    def __repr__(self):
        return '<AnmData %s v%s %s bones=%d frames=%d max=%d>' % (
            os.path.basename(self.path), self.version, self.frame_format,
            len(self.bones), len(self.frames), self.frame_max)


# -----------------------------------------------------------------------------
# Blender bind/pose conversion
# -----------------------------------------------------------------------------


def _casefold_map(names, label):
    result = {}
    for name in names:
        folded = name.casefold()
        if folded in result and result[folded] != name:
            raise ValueError('%s 存在大小写冲突: %s / %s' %
                             (label, result[folded], name))
        result[folded] = name
    return result


def _engine_world_metadata(arm):
    """Parse every stored MDL world matrix, including non-Blender helpers."""
    raw = arm.get('gem2_world_mats')
    if not raw:
        raise ValueError(
            '所选骨架缺少 gem2_world_mats；请使用本插件导入/生成的 GOH 目标骨架')
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise ValueError('gem2_world_mats 元数据不是有效 JSON') from error
    else:
        try:
            payload = raw.to_dict()
        except AttributeError:
            try:
                payload = dict(raw)
            except (TypeError, ValueError) as error:
                raise ValueError('gem2_world_mats 元数据不是矩阵映射') from error
    if not hasattr(payload, 'items'):
        raise ValueError('gem2_world_mats 元数据不是矩阵映射')
    result = {}
    folded = set()
    for source_name, values in payload.items():
        if not isinstance(source_name, str) or not source_name:
            raise ValueError('gem2_world_mats 含无效骨名')
        key = source_name.casefold()
        if key in folded:
            raise ValueError('gem2_world_mats 含大小写重复骨名: %s' % source_name)
        folded.add(key)
        try:
            matrix = Matrix(values)
        except (TypeError, ValueError) as error:
            raise ValueError('gem2_world_mats 矩阵无效: %s' % source_name) from error
        if len(matrix) != 4 or any(len(row) != 4 for row in matrix):
            raise ValueError('gem2_world_mats 不是 4x4 矩阵: %s' % source_name)
        _require_finite((matrix[row][column]
                         for row in range(4) for column in range(4)),
                        'gem2_world_mats[%s]' % source_name)
        if abs(float(matrix.to_3x3().determinant())) < 1e-8:
            raise ValueError('gem2_world_mats 含不可逆矩阵: %s' % source_name)
        result[source_name] = matrix
    return result


def _engine_rest_worlds(arm):
    payload = _engine_world_metadata(arm)
    actual = _casefold_map((bone.name for bone in arm.data.bones), 'Blender 骨架')
    result = {}
    for source_name, matrix in payload.items():
        target_name = actual.get(source_name.casefold())
        if target_name is not None:
            result[target_name] = matrix.copy()
    if not result:
        raise ValueError('gem2_world_mats 与所选骨架没有同名骨')
    return result


def _model_to_engine_bridge(arm):
    """Return the MDL ancestor transform from PLY/model space to engine world.

    Skinned PLY coordinates exclude the VolumeView node's local attachment, but
    its ancestors still act on both mesh and skeleton in GEM. A root-level mesh
    parent therefore has an identity bridge even when its own local transform is
    non-identity: that local attachment has already been removed from the PLY.
    Human MDLs normally return the mirrored ``basis`` ancestor here; omitting it
    makes left weights rotate around right-side pivots.
    """
    mesh_parent = arm.get('gem2_mesh_parent')
    if mesh_parent is None or mesh_parent == '':
        return Matrix.Identity(4)
    if not isinstance(mesh_parent, str):
        raise ValueError('gem2_mesh_parent 元数据无效')
    raw_parents = arm.get('gem2_parents')
    if not raw_parents:
        raise ValueError('骨架声明了 mesh-parent 但缺少 gem2_parents')
    if isinstance(raw_parents, str):
        try:
            parents = json.loads(raw_parents)
        except (TypeError, ValueError) as error:
            raise ValueError('gem2_parents 元数据不是有效 JSON') from error
    else:
        try:
            parents = raw_parents.to_dict()
        except AttributeError:
            try:
                parents = dict(raw_parents)
            except (TypeError, ValueError) as error:
                raise ValueError('gem2_parents 元数据不是骨骼映射') from error
    if not hasattr(parents, 'items'):
        raise ValueError('gem2_parents 元数据不是骨骼映射')

    all_world = _engine_world_metadata(arm)
    world_names = _casefold_map(all_world.keys(), 'gem2_world_mats')
    mesh_key = mesh_parent.casefold()
    if mesh_key not in world_names:
        raise ValueError('gem2_mesh_parent 不在 gem2_world_mats 中: %s' %
                         mesh_parent)

    parent_lookup = {}
    for child, parent in parents.items():
        if (not isinstance(child, str) or not child or
                not isinstance(parent, str)):
            raise ValueError('gem2_parents 含无效骨名')
        folded = child.casefold()
        if folded in parent_lookup:
            raise ValueError('gem2_parents 含大小写重复骨名: %s' % child)
        parent_lookup[folded] = parent
    if mesh_key not in parent_lookup:
        raise ValueError('gem2_mesh_parent 不在 gem2_parents 中: %s' %
                         mesh_parent)

    # Validate the complete declared ancestor chain and reject stale/cyclic
    # metadata. Only the immediate parent's absolute world matrix is the bridge.
    seen = set()
    current = mesh_key
    immediate_ancestor = None
    while True:
        if current in seen:
            raise ValueError('gem2_parents 含父子循环: %s' % mesh_parent)
        seen.add(current)
        if current not in parent_lookup:
            raise ValueError('gem2_parents 缺少祖先条目: %s' %
                             world_names.get(current, current))
        parent = parent_lookup[current]
        if not parent:
            break
        parent_name = world_names.get(parent.casefold())
        if parent_name is None:
            raise ValueError('gem2_parents 祖先不在 gem2_world_mats 中: %s' %
                             parent)
        if immediate_ancestor is None:
            immediate_ancestor = parent_name
        current = parent_name.casefold()
    if immediate_ancestor is None:
        return Matrix.Identity(4)
    return all_world[immediate_ancestor].copy()


def _engine_rest_locals(arm, engine_world):
    result = {}
    for name, world in engine_world.items():
        bone = arm.data.bones.get(name)
        parent_name = bone.parent.name if bone and bone.parent else None
        if parent_name in engine_world:
            result[name] = engine_world[parent_name].inverted() @ world
        else:
            result[name] = world.copy()
    return result


def _animation_bind_context(arm):
    """Build immutable rest/space data once for multi-frame import or export."""
    engine_rest = _engine_rest_worlds(arm)
    return {
        'engine_rest': engine_rest,
        'engine_local': _engine_rest_locals(arm, engine_rest),
        'model_to_engine': _model_to_engine_bridge(arm),
        'parent_of': {
            bone.name: bone.parent.name if bone.parent else None
            for bone in arm.data.bones
        },
    }


def _match_anm_bones(arm, anm):
    actual = _casefold_map((bone.name for bone in arm.data.bones), 'Blender 骨架')
    matches = {}
    missing = []
    for source_name in anm.bones:
        target_name = actual.get(source_name.casefold())
        if target_name is None:
            missing.append(source_name)
        else:
            matches[source_name] = target_name
    return matches, missing


def _pose_bases_for_frame(arm, anm, frame_no, bind_context=None):
    """Map a GEM pose onto Blender rest bones without changing their rest pose."""
    context = bind_context or _animation_bind_context(arm)
    engine_rest = context['engine_rest']
    engine_local = context['engine_local']
    matches, missing = _match_anm_bones(arm, anm)
    values_by_source = anm.evaluated_components(frame_no)

    pose_local = {name: matrix.copy() for name, matrix in engine_local.items()}
    animated = set()
    for source_name, values in values_by_source.items():
        target_name = matches.get(source_name)
        if target_name is None or target_name not in pose_local:
            continue
        rest_local = engine_local[target_name]
        pos = values['pos'] if values['pos'] is not None else rest_local.translation
        _rest_pos, rest_rot, rest_scale, rest_mirrored = _matrix_to_components(
            rest_local)
        rotation = values['rot'] if values['rot'] is not None else rest_rot
        scale = values['scale'] if values['scale'] is not None else rest_scale
        mirrored = (values['mirrored'] if values['mirrored'] is not None
                    else rest_mirrored)
        matrix = _compose_local_matrix(pos, rotation, scale, bool(mirrored))
        pose_local[target_name] = matrix
        animated.add(target_name)

    parent_of = context['parent_of']
    engine_pose = {}

    def resolve(name, visiting=None):
        if name in engine_pose:
            return engine_pose[name]
        if visiting is None:
            visiting = set()
        if name in visiting:
            raise ValueError('骨架父子循环: %s' % name)
        visiting.add(name)
        parent_name = parent_of.get(name)
        if parent_name in pose_local:
            world = resolve(parent_name, visiting) @ pose_local[name]
        else:
            world = pose_local[name].copy()
        visiting.remove(name)
        engine_pose[name] = world
        return world

    for name in pose_local:
        resolve(name)

    model_to_engine = context['model_to_engine']
    engine_to_model = model_to_engine.inverted()
    desired = {}
    for name, gem_pose in engine_pose.items():
        bone = arm.data.bones.get(name)
        if bone is None:
            continue
        engine_delta = gem_pose @ engine_rest[name].inverted()
        model_delta = engine_to_model @ engine_delta @ model_to_engine
        desired[name] = model_delta @ bone.matrix_local

    bases = {}
    for name, target_pose in desired.items():
        bone = arm.data.bones[name]
        if bone.parent and bone.parent.name in desired:
            parent_rest = bone.parent.matrix_local
            rest_relative = parent_rest.inverted() @ bone.matrix_local
            bases[name] = (rest_relative.inverted() @
                           desired[bone.parent.name].inverted() @ target_pose)
        else:
            bases[name] = bone.matrix_local.inverted() @ target_pose
    return bases, animated, missing


def apply_anm_frame(arm, anm, frame_no, reset_others=True):
    """Apply one evaluated ANM frame to an armature pose."""
    if not anm.bones or not anm.frames:
        raise ValueError('该 ANM 是空动画占位符，不包含可应用的骨骼姿态')
    bases, animated, missing = _pose_bases_for_frame(arm, anm, frame_no)
    if not animated:
        raise ValueError('ANM 与目标骨架没有可应用的同名动画骨')
    applied = 0
    for pose_bone in arm.pose.bones:
        if pose_bone.name in bases and (reset_others or pose_bone.name in animated):
            pose_bone.matrix_basis = bases[pose_bone.name]
            if pose_bone.name in animated:
                applied += 1
        elif reset_others:
            pose_bone.matrix_basis = Matrix.Identity(4)
    return {'applied': applied, 'missing': missing, 'mode': 'bind_delta'}


def _add_fcurve(action, datablock, data_path, index, values, group):
    curve = action.fcurve_ensure_for_datablock(
        datablock, data_path, index=index, group_name=group)
    if curve.keyframe_points:
        raise ValueError('Action 中出现重复 FCurve: %s[%d]' %
                         (data_path, index))
    curve.keyframe_points.add(len(values))
    flat = []
    for frame_no, value in values:
        flat.extend((float(frame_no), float(value)))
    curve.keyframe_points.foreach_set('co', flat)
    for point in curve.keyframe_points:
        point.interpolation = 'LINEAR'
    curve.update()
    return curve


def _assign_action(animation_data, action):
    """Assign an Action and bind a Blender 4.4+ compatible object slot."""
    animation_data.action = action
    if action is None or not hasattr(animation_data, 'action_slot'):
        return
    suitable = list(getattr(animation_data, 'action_suitable_slots', ()))
    if suitable:
        current = getattr(animation_data, 'action_slot', None)
        if current not in suitable:
            animation_data.action_slot = suitable[0]
    elif list(getattr(action, 'slots', ())):
        raise ValueError('Action 没有适用于所选骨架的 Blender slot')


def _action_fingerprint(action):
    digest = hashlib.sha256()
    for slot in getattr(action, 'slots', ()):
        digest.update(slot.identifier.encode('utf-8'))
        digest.update(slot.target_id_type.encode('ascii', 'replace'))
    for attr in ('use_frame_range', 'frame_start', 'frame_end',
                 'frame_range', 'curve_frame_range'):
        if hasattr(action, attr):
            value = getattr(action, attr)
            if not isinstance(value, (bool, int, float, str)):
                value = tuple(value)
            digest.update(attr.encode('ascii'))
            digest.update(repr(value).encode('ascii', 'replace'))
    for group in sorted(action.groups, key=lambda item: item.name):
        digest.update(group.name.encode('utf-8'))
        digest.update(bytes((bool(group.mute), bool(group.lock))))
    curves = sorted(action.fcurves,
                    key=lambda fc: (fc.data_path, fc.array_index))
    for curve in curves:
        digest.update(curve.data_path.encode('utf-8'))
        digest.update(struct.pack('<i', int(curve.array_index)))
        digest.update(curve.extrapolation.encode('ascii', 'replace'))
        digest.update(bytes((bool(curve.mute),)))
        for point in curve.keyframe_points:
            digest.update(struct.pack(
                '<6d', float(point.co.x), float(point.co.y),
                float(point.handle_left.x), float(point.handle_left.y),
                float(point.handle_right.x), float(point.handle_right.y)))
            for attr in ('interpolation', 'handle_left_type',
                         'handle_right_type', 'easing', 'amplitude',
                         'back', 'period'):
                digest.update(attr.encode('ascii'))
                digest.update(repr(getattr(point, attr)).encode('ascii', 'replace'))
        for modifier in curve.modifiers:
            digest.update(modifier.type.encode('ascii', 'replace'))
            for prop in modifier.bl_rna.properties:
                if prop.identifier == 'rna_type' or prop.is_readonly:
                    continue
                try:
                    value = getattr(modifier, prop.identifier)
                except Exception:
                    continue
                if isinstance(value, (bool, int, float, str)):
                    digest.update(prop.identifier.encode('ascii', 'replace'))
                    digest.update(repr(value).encode('ascii', 'replace'))
    return digest.hexdigest()


def import_anm_action(arm, path, action_name=None, frame_step=1):
    """Bake all declared ANM frames into a quaternion Blender Action."""
    import bpy

    anm = AnmData(path)
    if not anm.bones or not anm.frames:
        raise ValueError('该 ANM 是空动画占位符，不包含可导入的骨骼姿态')
    step = max(1, int(frame_step))
    sample_count = anm.frame_max // step + 1
    if anm.frame_max % step:
        sample_count += 1
    if sample_count > MAX_BAKED_FRAMES:
        raise ValueError('Action 烘焙帧数超过安全上限: %d' % sample_count)
    if action_name is None:
        action_name = os.path.splitext(os.path.basename(path))[0]

    def sampled_frames():
        last = None
        for frame_no in range(0, anm.frame_max + 1, step):
            last = frame_no
            yield frame_no
        if last != anm.frame_max:
            yield anm.frame_max

    bind_context = _animation_bind_context(arm)
    recorded = {}
    previous_quaternion = {}
    missing_union = set()
    animated_union = set()
    for frame_no in sampled_frames():
        bases, animated, missing = _pose_bases_for_frame(
            arm, anm, frame_no, bind_context=bind_context)
        animated_union.update(animated)
        missing_union.update(missing)
        for name, matrix in bases.items():
            location, rotation, scale = matrix.decompose()
            _require_finite((*location, *rotation, *scale),
                            'Blender Action pose')
            previous = previous_quaternion.get(name)
            if previous is not None and rotation.dot(previous) < 0.0:
                rotation = _negated_quaternion(rotation)
            previous_quaternion[name] = rotation.copy()
            recorded.setdefault(name, []).append(
                (frame_no, location.copy(), rotation.copy(), scale.copy()))
    if not animated_union:
        raise ValueError('ANM 与目标骨架没有可应用的同名动画骨')

    scene = bpy.context.scene
    old_scene_state = (scene.frame_start, scene.frame_end,
                       scene.frame_current)
    had_animation_data = arm.animation_data is not None
    if not had_animation_data:
        arm.animation_data_create()
    animation_data = arm.animation_data
    old_action = animation_data.action
    old_slot = getattr(animation_data, 'action_slot', None)
    old_animation_state = {
        attr: getattr(animation_data, attr)
        for attr in ('use_nla', 'action_blend_type', 'action_influence')
        if hasattr(animation_data, attr)
    }
    old_rotation_modes = {bone.name: bone.rotation_mode
                          for bone in arm.pose.bones}
    action = bpy.data.actions.new(action_name)
    try:
        animation_data.use_nla = False
        animation_data.action_blend_type = 'REPLACE'
        animation_data.action_influence = 1.0
        # Blender 5.2 requires the Action to be assigned before using the
        # datablock-aware FCurve API, which creates and binds its object slot.
        _assign_action(animation_data, action)
        for pose_bone in arm.pose.bones:
            records = recorded.get(pose_bone.name)
            if not records:
                continue
            pose_bone.rotation_mode = 'QUATERNION'
            paths = (pose_bone.path_from_id('location'),
                     pose_bone.path_from_id('rotation_quaternion'),
                     pose_bone.path_from_id('scale'))
            for index in range(3):
                _add_fcurve(action, arm, paths[0], index,
                            [(f, loc[index])
                             for f, loc, _rot, _sca in records],
                            pose_bone.name)
            for index in range(4):
                _add_fcurve(action, arm, paths[1], index,
                            [(f, rot[index])
                             for f, _loc, rot, _sca in records],
                            pose_bone.name)
            for index in range(3):
                _add_fcurve(action, arm, paths[2], index,
                            [(f, sca[index])
                             for f, _loc, _rot, sca in records],
                            pose_bone.name)
        _assign_action(animation_data, action)
        if hasattr(animation_data, 'action_slot') and not animation_data.action_slot:
            raise ValueError('Blender 未能为导入 Action 绑定对象 slot')

        action['gem2_anm_source_path'] = anm.path
        action['gem2_anm_source_sha256'] = anm.source_sha256
        action['gem2_anm_source_version'] = int(anm.version_u32)
        action['gem2_anm_source_frame_max'] = int(anm.frame_max)
        action['gem2_anm_source_format'] = anm.frame_format
        action['gem2_anm_comment_b64'] = base64.b64encode(
            anm.comment_bytes).decode('ascii')
        visibility_events = [
            [frame_no, key.bone,
             key.flags & (B_VISIBLE_ON | B_VISIBLE_OFF)]
            for frame_no, frame in sorted(anm.frames.items())
            for key in frame.keys
            if key.flags & (B_VISIBLE_ON | B_VISIBLE_OFF)
        ]
        action['gem2_anm_visibility_json'] = json.dumps(
            visibility_events, ensure_ascii=False, separators=(',', ':'))
        action['gem2_anm_has_mesh'] = any(
            key.flags & B_MESH
            for frame in anm.frames.values() for key in frame.keys)
        action['gem2_anm_bones_json'] = json.dumps(anm.bones,
                                                  ensure_ascii=False)
        action['gem2_anm_missing_json'] = json.dumps(sorted(missing_union),
                                                     ensure_ascii=False)
        action['gem2_anm_import_fingerprint'] = _action_fingerprint(action)
        action.use_fake_user = True
        scene.frame_start = 0
        scene.frame_end = anm.frame_max
        scene.frame_set(0)
    except Exception:
        for bone_name, rotation_mode in old_rotation_modes.items():
            arm.pose.bones[bone_name].rotation_mode = rotation_mode
        _assign_action(animation_data, old_action)
        if old_action is not None and old_slot is not None:
            animation_data.action_slot = old_slot
        for attr, value in old_animation_state.items():
            setattr(animation_data, attr, value)
        scene.frame_start, scene.frame_end = old_scene_state[:2]
        scene.frame_set(old_scene_state[2])
        action.use_fake_user = False
        bpy.data.actions.remove(action)
        if not had_animation_data:
            arm.animation_data_clear()
        raise
    return anm, action


# -----------------------------------------------------------------------------
# Export
# -----------------------------------------------------------------------------


def collect_pose_matrices(arm, frame=None, bind_context=None):
    """Collect current Blender pose as absolute GEM parent-local matrices."""
    import bpy

    if frame is not None:
        bpy.context.scene.frame_set(int(frame))
    bpy.context.view_layer.update()
    context = bind_context or _animation_bind_context(arm)
    engine_rest = context['engine_rest']
    model_to_engine = context['model_to_engine']
    engine_to_model = model_to_engine.inverted()
    engine_pose = {}
    for name, gem_rest in engine_rest.items():
        pose_bone = arm.pose.bones.get(name)
        bone = arm.data.bones.get(name)
        if pose_bone is None or bone is None:
            continue
        model_delta = pose_bone.matrix @ bone.matrix_local.inverted()
        engine_delta = model_to_engine @ model_delta @ engine_to_model
        engine_pose[name] = engine_delta @ gem_rest
    local = {}
    for name, pose_world in engine_pose.items():
        bone = arm.data.bones[name]
        parent_name = bone.parent.name if bone.parent else None
        if parent_name in engine_pose:
            local[name] = engine_pose[parent_name].inverted() @ pose_world
        else:
            local[name] = pose_world.copy()
    return local


def _source_visibility(anm):
    result = {}
    for frame_no, frame in anm.frames.items():
        for key in frame.keys:
            flags = key.flags & (B_VISIBLE_ON | B_VISIBLE_OFF)
            if flags:
                result[(frame_no, key.bone)] = flags
            if key.flags & B_MESH:
                raise ValueError('含 MESH 动画的数据只能原样导出，不能安全烘焙编辑')
    return result


def _validate_bone_names(bones, label='BMAP'):
    if not isinstance(bones, (list, tuple)) or not bones:
        raise ValueError('%s 必须是非空骨名列表' % label)
    if len(bones) > MAX_BONES:
        raise ValueError('%s 骨数超过安全上限: %d' % (label, len(bones)))
    folded = set()
    for name in bones:
        if not isinstance(name, str) or not name:
            raise ValueError('%s 含无效骨名' % label)
        try:
            encoded = name.encode('latin-1')
        except UnicodeEncodeError as error:
            raise ValueError('%s 骨名不能编码为 latin-1: %s' %
                             (label, name)) from error
        if len(encoded) > MAX_BONE_NAME_SIZE:
            raise ValueError('%s 骨名超过安全上限: %s' % (label, name))
        key = name.casefold()
        if key in folded:
            raise ValueError('%s 含大小写重复骨名: %s' % (label, name))
        folded.add(key)
    return list(bones)


def _visibility_from_metadata(action, bones, frame_max):
    raw = action.get('gem2_anm_visibility_json', '[]')
    try:
        events = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError('Action 的可见性元数据无效') from error
    if not isinstance(events, list):
        raise ValueError('Action 的可见性元数据必须是列表')
    bone_lookup = _casefold_map(bones, 'Action BMAP')
    result = {}
    for event in events:
        if (not isinstance(event, list) or len(event) != 3 or
                not isinstance(event[0], int) or
                not isinstance(event[1], str) or
                not isinstance(event[2], int)):
            raise ValueError('Action 的可见性事件格式无效')
        frame_no, bone, flags = event
        matched_bone = bone_lookup.get(bone.casefold())
        if matched_bone is None or not 0 <= frame_no <= frame_max:
            raise ValueError('Action 的可见性事件超出 BMAP/帧范围')
        if (flags not in (B_VISIBLE_ON, B_VISIBLE_OFF) or
                (frame_no, matched_bone) in result):
            raise ValueError('Action 的可见性事件 flags/重复项无效')
        result[(frame_no, matched_bone)] = flags
    return result


def _comment_from_metadata(action):
    raw = action.get('gem2_anm_comment_b64', '')
    if not isinstance(raw, str):
        raise ValueError('Action 的 CMNT 元数据无效')
    try:
        value = base64.b64decode(raw.encode('ascii'), validate=True)
    except (ValueError, UnicodeError) as error:
        raise ValueError('Action 的 CMNT 元数据不是有效 base64') from error
    if len(value) > 65535:
        raise ValueError('Action 的 CMNT 元数据超过 uint16 长度')
    return value


def _valid_sha256(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _has_evaluation_overrides(arm):
    animation_data = arm.animation_data
    if animation_data is not None:
        if animation_data.drivers:
            return True
        if (getattr(animation_data, 'use_nla', False) and
                any(not track.mute and len(track.strips)
                    for track in animation_data.nla_tracks)):
            return True
        if (getattr(animation_data, 'action_blend_type', 'REPLACE') != 'REPLACE'
                or abs(float(getattr(animation_data,
                                     'action_influence', 1.0)) - 1.0) > 1e-8):
            return True
    for pose_bone in arm.pose.bones:
        for constraint in pose_bone.constraints:
            if (not constraint.mute and
                    float(getattr(constraint, 'influence', 1.0)) > 1e-8):
                return True
    return False


def _write_atomic(path, payload):
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = path + '.gem2_tmp'
    try:
        with open(temporary, 'wb') as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _encode_frm2(bones, per_frame, version_u32=0x00060001,
                 comment_bytes=b'', visibility=None):
    bones = _validate_bone_names(bones)
    if not per_frame:
        raise ValueError('没有可导出的动画帧')
    if min(per_frame) < 0 or max(per_frame) > 65535:
        raise ValueError('FRM2 帧号必须在 0..65535')
    if max(per_frame) + 1 > 0xFFFFFFFF:
        raise ValueError('FRMS 帧数溢出')
    visibility = visibility or {}
    bone_index = {name: index for index, name in enumerate(bones)}

    output = bytearray(b'EANM')
    output += struct.pack('<I', int(version_u32))
    output += b'FRMS' + struct.pack('<I', max(per_frame) + 1)
    if comment_bytes:
        if len(comment_bytes) > 65535:
            raise ValueError('CMNT 超过 uint16 长度')
        output += b'CMNT' + struct.pack('<H', len(comment_bytes)) + comment_bytes
    output += b'BMAP' + struct.pack('<I', len(bones))
    for name in bones:
        encoded = name.encode('latin-1')
        output += struct.pack('<I', len(encoded)) + encoded

    for frame_no in sorted(per_frame):
        matrices = per_frame[frame_no]
        unknown = set(matrices) - set(bone_index)
        if unknown:
            raise ValueError('帧 %d 含 BMAP 外骨骼: %s' %
                             (frame_no, ', '.join(sorted(unknown))))
        key_names = [name for name in bones if name in matrices]
        if len(key_names) > 255:
            raise ValueError('帧 %d 超过 255 个 key' % frame_no)
        output += b'FRM2' + struct.pack('<H', frame_no)
        output += struct.pack('<B', len(key_names))
        for name in key_names:
            position, rotation, scale, mirrored = _matrix_to_components(
                matrices[name])
            flags = B_POSITION | B_ORIENTATION
            if mirrored:
                flags |= B_MIRRORED
            if max(abs(float(value) - 1.0) for value in scale) > 1e-5:
                flags |= B_SCALE
            visible_flags = int(visibility.get((frame_no, name), 0))
            if (visible_flags & ~(B_VISIBLE_ON | B_VISIBLE_OFF) or
                    visible_flags == (B_VISIBLE_ON | B_VISIBLE_OFF)):
                raise ValueError('非法可见性 flags: 0x%X' % visible_flags)
            flags |= visible_flags
            output += struct.pack('<BH', bone_index[name], flags)
            output += struct.pack('<3f', *position)
            output += struct.pack('<3f', rotation.x, rotation.y, rotation.z)
            if flags & B_SCALE:
                output += struct.pack('<3f', *scale)
    return bytes(output)


def write_anm(path, arm, action=None):
    """Export a pose or Action using the inverse GEM bind-delta mapping.

    An unchanged imported Action is copied byte-for-byte from its source.  Once
    edited, all integer frames are baked to FRM2. NLA mixing is disabled for
    deterministic Action sampling; active constraints and drivers are
    intentionally evaluated and baked with interpolation and mirror signs.
    """
    import bpy

    if action is not None and not action.fcurves:
        raise ValueError('所选 Action 不含可导出的 FCurve')
    source_anm = None
    source_path = action.get('gem2_anm_source_path') if action else None
    source_hash = action.get('gem2_anm_source_sha256') if action else None
    imported_fingerprint = (action.get('gem2_anm_import_fingerprint')
                            if action else None)
    current_fingerprint = _action_fingerprint(action) if action else None
    imported_action = bool(source_path)

    if source_path is not None and not isinstance(source_path, str):
        raise ValueError('Action 的源 ANM 路径元数据无效')
    if imported_action and not _valid_sha256(source_hash):
        raise ValueError('Action 缺少有效的源 ANM SHA-256')
    if source_path and os.path.isfile(source_path):
        with open(source_path, 'rb') as handle:
            source_bytes = handle.read()
        actual_hash = hashlib.sha256(source_bytes).hexdigest()
        if actual_hash != source_hash:
            raise ValueError('源 ANM 已被外部修改，拒绝覆盖式安全导出: %s' %
                             source_path)
        unchanged = (_valid_sha256(imported_fingerprint) and
                     current_fingerprint == imported_fingerprint and
                     not any(curve.modifiers for curve in action.fcurves) and
                     not _has_evaluation_overrides(arm))
        if unchanged:
            source_anm = AnmData(source_path)
            _write_atomic(path, source_bytes)
            return len(source_anm.bones), source_anm.frame_max + 1
        source_anm = AnmData(source_path)

    bind_context = _animation_bind_context(arm)
    engine_rest = bind_context['engine_rest']
    actual_by_folded = _casefold_map(engine_rest.keys(), 'GEM 目标骨架')
    if source_anm is not None:
        # Reaching this branch means passthrough was not possible. Bake the
        # complete target skeleton so edits to self-contained Action curves that
        # were outside the source's sparse BMAP cannot be silently discarded.
        bones = list(engine_rest.keys())
        visibility = _source_visibility(source_anm)
        version_u32 = (source_anm.version_u32
                       if source_anm.version_u32 in (0x00060000, 0x00060001)
                       else 0x00060001)
        comment_bytes = source_anm.comment_bytes
        frame_start = 0
        frame_end = source_anm.frame_max
    elif action and action.get('gem2_anm_bones_json'):
        try:
            source_bones = _validate_bone_names(
                json.loads(action['gem2_anm_bones_json']), 'Action BMAP')
        except (TypeError, ValueError) as error:
            if isinstance(error, ValueError) and str(error).startswith('Action BMAP'):
                raise
            raise ValueError('Action 的 BMAP 元数据无效') from error
        frame_start = 0
        frame_end = int(action.get('gem2_anm_source_frame_max',
                                   math.ceil(action.frame_range[1])))
        if imported_action and action.get('gem2_anm_has_mesh', False):
            raise ValueError('源 ANM 已丢失且含 MESH 数据，拒绝有损导出')
        visibility = _visibility_from_metadata(action, source_bones, frame_end)
        bones = list(engine_rest.keys())
        version_u32 = int(action.get('gem2_anm_source_version', 0x00060001))
        if version_u32 not in (0x00060000, 0x00060001):
            version_u32 = 0x00060001
        comment_bytes = _comment_from_metadata(action)
    else:
        bones = list(engine_rest.keys())
        visibility = {}
        version_u32 = 0x00060001
        comment_bytes = b''
        if action is None:
            frame_start = frame_end = int(bpy.context.scene.frame_current)
        else:
            start_value, end_value = action.frame_range
            frame_start = int(math.floor(start_value))
            frame_end = int(math.ceil(end_value))

    if frame_end < frame_start:
        raise ValueError('Action 帧范围无效: %d..%d' % (frame_start, frame_end))
    remapped_visibility = {}
    for (frame_no, source_name), flags in visibility.items():
        actual_name = actual_by_folded.get(source_name.casefold())
        if actual_name is None:
            raise ValueError('可见性事件缺少目标骨: %s' % source_name)
        event_key = (frame_no, actual_name)
        if event_key in remapped_visibility:
            raise ValueError('可见性事件大小写映射后重复: %s @ %d' %
                             (source_name, frame_no))
        remapped_visibility[event_key] = flags
    visibility = remapped_visibility
    export_frame_count = frame_end - frame_start + 1
    if export_frame_count > MAX_BAKED_FRAMES:
        raise ValueError('Action 导出烘焙帧数超过安全上限: %d' %
                         export_frame_count)

    source_to_actual = {}
    missing = []
    for source_name in bones:
        actual_name = actual_by_folded.get(source_name.casefold())
        if actual_name is None:
            missing.append(source_name)
        else:
            source_to_actual[source_name] = actual_name
    if missing:
        raise ValueError('安全导出缺少目标骨: %s' % ', '.join(missing[:12]))

    if action and source_anm is not None:
        start_value, end_value = action.frame_range
        if start_value < -1e-4 or end_value > source_anm.frame_max + 1e-4:
            raise ValueError('编辑后的 Action 关键帧超出源 ANM 0..%d 范围' %
                             source_anm.frame_max)

    animation_data = arm.animation_data
    created_animation_data = False
    if action is not None and animation_data is None:
        arm.animation_data_create()
        animation_data = arm.animation_data
        created_animation_data = True
    old_action = animation_data.action if animation_data else None
    old_slot = (getattr(animation_data, 'action_slot', None)
                if animation_data else None)
    old_frame = bpy.context.scene.frame_current
    animation_state = {}
    if animation_data is not None:
        for attr in ('use_nla', 'action_blend_type', 'action_influence'):
            if hasattr(animation_data, attr):
                animation_state[attr] = getattr(animation_data, attr)
    try:
        if action is not None:
            animation_data.use_nla = False
            animation_data.action_blend_type = 'REPLACE'
            animation_data.action_influence = 1.0
            _assign_action(animation_data, action)
            if action.fcurves:
                active_slot = getattr(animation_data, 'action_slot', None)
                suitable_slots = list(getattr(animation_data,
                                              'action_suitable_slots', ()))
                if active_slot is None or active_slot not in suitable_slots:
                    raise ValueError('Action 没有适用于所选骨架的 Blender slot')
        per_frame = {}
        output_offset = frame_start if source_anm is None else 0
        for scene_frame in range(frame_start, frame_end + 1):
            sample_frame = scene_frame if action is not None else None
            local_actual = collect_pose_matrices(
                arm, sample_frame, bind_context=bind_context)
            output_frame = scene_frame - output_offset
            per_frame[output_frame] = {
                source_name: local_actual[actual_name]
                for source_name, actual_name in source_to_actual.items()
            }
    finally:
        if action is not None and animation_data is not None:
            _assign_action(animation_data, old_action)
            if old_action is not None and old_slot is not None:
                animation_data.action_slot = old_slot
            for attr, value in animation_state.items():
                setattr(animation_data, attr, value)
        bpy.context.scene.frame_set(old_frame)
        bpy.context.view_layer.update()
        if created_animation_data:
            arm.animation_data_clear()

    payload = _encode_frm2(bones, per_frame, version_u32,
                           comment_bytes, visibility)
    _write_atomic(path, payload)
    return len(bones), len(per_frame)


# -----------------------------------------------------------------------------
# GOH properties.pak helpers
# -----------------------------------------------------------------------------


def list_goh_anm_in_pak(pak_path, keyword=''):
    """List ANM entries in a ZIP-compatible GOH properties package."""
    import zipfile
    with zipfile.ZipFile(pak_path) as archive:
        names = [name for name in archive.namelist()
                 if name.lower().endswith('.anm')]
        if keyword:
            keyword = keyword.lower()
            names = [name for name in names if keyword in name.lower()]
        return [(name, archive.getinfo(name).file_size) for name in names]


def extract_goh_anm(pak_path, keyword='', out_dir=None, limit=50):
    """Extract matching GOH ANM files while avoiding basename collisions."""
    import shutil
    import zipfile

    entries = list_goh_anm_in_pak(pak_path, keyword)
    if not entries:
        return []
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(pak_path)),
                               'anm_extracted')
    os.makedirs(out_dir, exist_ok=True)
    output = []
    with zipfile.ZipFile(pak_path) as archive:
        for entry, _size in entries[:limit]:
            relative = entry.replace('properties/animation/', '').replace('/', '_')
            destination = os.path.join(out_dir, relative)
            try:
                with archive.open(entry) as source, open(destination, 'wb') as target:
                    shutil.copyfileobj(source, target)
                output.append((os.path.basename(destination), entry))
            except Exception:
                continue
    return output
