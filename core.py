import struct
import re
import os
import json
import bpy
from mathutils import Matrix, Vector
from .i18n import _

# ── Binary struct packers / unpackers ──────────────────────────
pack_B = struct.Struct("B").pack
pack_BBBB = struct.Struct("BBBB").pack
pack_H = struct.Struct("H").pack
pack_HHH = struct.Struct("HHH").pack
pack_I = struct.Struct("I").pack
pack_f = struct.Struct("f").pack
pack_ff = struct.Struct("ff").pack
pack_fff = struct.Struct("fff").pack

unpack_I = struct.Struct('<I').unpack
unpack_H = struct.Struct('<H').unpack
unpack_HHH = struct.Struct('<HHH').unpack
unpack_f = struct.Struct('<f').unpack
unpack_fff = struct.Struct('<fff').unpack
unpack_ff = struct.Struct('<ff').unpack
unpack_BBBB = struct.Struct('<BBBB').unpack

# ── D3D9 Flexible Vertex Format flags ─────────────────────────
D3DFVF_XYZ = 0x0002
D3DFVF_XYZB1 = 0x0006
D3DFVF_XYZB2 = 0x0008
D3DFVF_XYZB3 = 0x000A
D3DFVF_XYZB4 = 0x000C
D3DFVF_XYZB5 = 0x000E
D3DFVF_NORMAL = 0x0010
D3DFVF_PSIZE = 0x0020
D3DFVF_DIFFUSE = 0x0040
D3DFVF_SPECULAR = 0x0080
D3DFVF_TEX1 = 0x0100
D3DFVF_TEX2 = 0x0200
D3DFVF_TEX3 = 0x0300
D3DFVF_TEX8 = 0x0800
D3DFVF_LASTBETA_UBYTE4 = 0x1000

# ── GEM2 mesh flags ───────────────────────────────────────────
MESH_FLAG_TWO_SIDED = 0x0001
MESH_FLAG_ALPHA = 0x0002
MESH_FLAG_LIGHT = 0x0004
MESH_FLAG_SKINNED = 0x0010
MESH_FLAG_BUMP = 0x0100
MESH_FLAG_SPECULAR_COLOR = 0x0200
MESH_FLAG_MATERIAL = 0x0400
MESH_FLAG_SUBSKIN = 0x0800

# ── Coordinate system constants ────────────────────────────────
SCALE_RATIO = 0.5
TAB = "\t"

# ── Matrix utilities ───────────────────────────────────────────

def row34_to_blender(m):
    """行优先 4x3 矩阵 → Blender 列优先 4x4"""
    return Matrix((
        (m[0][0], m[1][0], m[2][0], m[3][0]),
        (m[0][1], m[1][1], m[2][1], m[3][1]),
        (m[0][2], m[1][2], m[2][2], m[3][2]),
        (0, 0, 0, 1)
    ))

def ori_to_blender(m):
    """行优先 3x3 旋转矩阵 → Blender 4x4"""
    return Matrix((
        (m[0][0], m[1][0], m[2][0], 0),
        (m[0][1], m[1][1], m[2][1], 0),
        (m[0][2], m[1][2], m[2][2], 0),
        (0, 0, 0, 1)
    ))

def blender_to_row34(mat):
    """Blender 4x4 → 行优先 4x3 列表"""
    m = mat.transposed()
    return [[m[i][j] for j in range(3)] for i in range(4)]

def blender_to_row33(mat):
    """Blender 4x4 → 行优先 3x3 旋转部分"""
    m = mat.transposed()
    return [[m[i][j] for j in range(3)] for i in range(3)]

# ── String/binary utilities ────────────────────────────────────

def read_string_with_length(f):
    """读取带长度前缀的字符串（二进制流）"""
    length = struct.unpack('<B', f.read(1))[0]
    raw = f.read(length)
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        try:
            return raw.decode('gbk')
        except:
            return raw.decode('latin-1', errors='replace')

def read_string_at_p(file_bytes, p):
    """从 bytes 中读取带长度前缀的字符串，截断到第一个控制字符"""
    if p >= len(file_bytes):
        return '', p
    length = file_bytes[p]
    p += 1
    if length == 0:
        return '', p
    if p + length > len(file_bytes):
        return '', p - 1
    raw = file_bytes[p:p + length]
    p += length
    end = 0
    for b in raw:
        if b < 0x20:
            break
        end += 1
    if end == 0:
        return '', p
    return raw[:end].decode('ascii', errors='replace'), p

def find_matching_brace(text, start):
    """找到与 start 位置的 '{' 匹配的 '}' """
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return i
    return -1

# ── Path persistence ───────────────────────────────────────────

def _get_paths_file():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '.gem2_paths.json')

def _load_paths():
    pf = _get_paths_file()
    if os.path.isfile(pf):
        try:
            with open(pf, 'r') as f:
                return json.load(f)
        except:
            pass
    return {'import': '', 'export': ''}

def _save_paths(data):
    try:
        with open(_get_paths_file(), 'w') as f:
            json.dump(data, f)
    except:
        pass

_paths = _load_paths()

def get_paths():
    return _paths

def set_import_dir(dirpath):
    _paths['import'] = dirpath
    _save_paths(_paths)

def set_export_dir(dirpath):
    _paths['export'] = dirpath
    _save_paths(_paths)

# ── Template armature loading ──────────────────────────────────

def load_template_armature(plugin_dir):
    """从 samples/skeleton.blend 加载模板骨架"""
    template_path = os.path.join(plugin_dir, 'samples', 'skeleton.blend')
    if not os.path.isfile(template_path):
        print(_("core.template.not_found", path=template_path))
        return None, None

    with bpy.data.libraries.load(template_path, link=False) as (data_from, data_to):
        data_to.objects = [name for name in data_from.objects]

    arm_obj = None
    arm_data = None
    for obj in data_to.objects:
        if obj.type == 'ARMATURE':
            arm_obj = obj
            arm_data = obj.data
            break

    if arm_obj is None:
        print(_("core.template.no_armature"))
        return None, None

    print(_("core.template.loaded", name=arm_obj.name, count=len(arm_data.bones)))
    return arm_obj, arm_data

# ── FVF parsing ────────────────────────────────────────────────

def parse_fvf_layout(fvf_flags):
    """解析 FVF 标志，返回各字段的偏移和大小"""
    layout = {}
    offset = 0

    pos_size = 12
    layout['POSITION'] = (offset, pos_size)
    offset += pos_size

    skin_bw_count = 0
    fvf_xyz = fvf_flags & 0x0E
    if fvf_xyz == D3DFVF_XYZB5:
        skin_bw_count = 5
    elif fvf_xyz == D3DFVF_XYZB4:
        skin_bw_count = 4
    elif fvf_xyz == D3DFVF_XYZB3:
        skin_bw_count = 3
    elif fvf_xyz == D3DFVF_XYZB2:
        skin_bw_count = 2
    elif fvf_xyz == D3DFVF_XYZB1:
        skin_bw_count = 1
    has_skin = skin_bw_count > 0
    skin_size = skin_bw_count * 4
    if has_skin:
        layout['SKIN'] = (offset, skin_size)
        offset += skin_size

    if fvf_flags & D3DFVF_NORMAL:
        layout['NORMAL'] = (offset, 12)
        offset += 12

    if fvf_flags & D3DFVF_PSIZE:
        layout['PSIZE'] = (offset, 4)
        offset += 4

    if fvf_flags & D3DFVF_DIFFUSE:
        layout['DIFFUSE'] = (offset, 4)
        offset += 4

    if fvf_flags & D3DFVF_SPECULAR:
        layout['SPECULAR'] = (offset, 4)
        offset += 4

    tex_count = (fvf_flags >> 8) & 0x0F
    if tex_count > 0:
        layout['TEX'] = (offset, tex_count * 8)
        offset += tex_count * 8

    return layout, has_skin, offset
