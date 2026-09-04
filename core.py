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

# ── User settings persistence ──────────────────────────────────

_SETTINGS_VERSION = 2


def _get_legacy_paths_file():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '.gem2_paths.json')


def _get_settings_file():
    """Store personal paths/options outside .blend files and the add-on tree."""
    try:
        config_dir = bpy.utils.user_resource(
            'CONFIG', path='gem2_goh_tools', create=True)
    except Exception:
        config_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(config_dir, 'settings.json')


def _load_json_mapping(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _load_settings():
    data = _load_json_mapping(_get_settings_file()) or {}
    paths = data.get('paths')
    if not isinstance(paths, dict):
        # One-time migration from the original add-on-local path file.
        legacy = _load_json_mapping(_get_legacy_paths_file()) or {}
        paths = {
            'import': legacy.get('import', ''),
            'export': legacy.get('export', ''),
        }
    data['version'] = _SETTINGS_VERSION
    data['paths'] = {
        'import': str(paths.get('import') or ''),
        'export': str(paths.get('export') or ''),
    }
    if not isinstance(data.get('panels'), dict):
        data['panels'] = {}
    if not isinstance(data.get('export_presets'), dict):
        data['export_presets'] = {}
    return data


def _save_settings():
    path = _get_settings_file()
    temp_path = path + '.tmp'
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(temp_path, 'w', encoding='utf-8', newline='\n') as fh:
            json.dump(_settings, fh, ensure_ascii=False, indent=2,
                      sort_keys=True)
            fh.write('\n')
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        print('[GEM2] user settings save failed: %s' % exc)


_settings = _load_settings()
_paths = _settings['paths']


def get_paths():
    return _paths


def set_import_dir(dirpath, save=True):
    _paths['import'] = str(dirpath or '')
    if save:
        _save_settings()


def set_export_dir(dirpath, save=True):
    _paths['export'] = str(dirpath or '')
    if save:
        _save_settings()


def get_panel_settings(panel_id):
    """Return a detached copy of one panel's persisted primitive values."""
    panel = _settings['panels'].get(str(panel_id), {})
    return dict(panel) if isinstance(panel, dict) else {}


def set_panel_settings(panel_id, values):
    """Atomically replace one panel's persisted settings."""
    if not isinstance(values, dict):
        raise TypeError('panel settings must be a mapping')
    clean = {}
    for key, value in values.items():
        if isinstance(key, str) and isinstance(
                value, (str, bool, int, float, type(None))):
            clean[key] = value
    _settings['panels'][str(panel_id)] = clean
    _save_settings()


def clear_panel_settings(panel_id):
    _settings['panels'].pop(str(panel_id), None)
    _save_settings()


def _clean_json_setting(value, depth=0):
    if depth > 6:
        raise ValueError('preset settings are nested too deeply')
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_clean_json_setting(item, depth + 1) for item in value]
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError('preset setting keys must be nonempty text')
            clean[key] = _clean_json_setting(item, depth + 1)
        return clean
    raise TypeError('preset settings must contain JSON-compatible values')


def get_export_presets():
    """Return a detached mapping of named MOWAS2/GOH export presets."""
    return json.loads(json.dumps(_settings.get('export_presets', {}),
                                 ensure_ascii=False))


def get_export_preset(name):
    value = _settings.get('export_presets', {}).get(str(name), None)
    if not isinstance(value, dict):
        return None
    return json.loads(json.dumps(value, ensure_ascii=False))


def save_export_preset(name, values):
    name = str(name or '').strip()
    if not name or len(name) > 64 or any(ord(char) < 32 for char in name):
        raise ValueError('preset name must contain 1-64 printable characters')
    if not isinstance(values, dict):
        raise TypeError('preset settings must be a mapping')
    existing = next((key for key in _settings['export_presets']
                     if key.casefold() == name.casefold()), None)
    if existing and existing != name:
        del _settings['export_presets'][existing]
    _settings['export_presets'][name] = _clean_json_setting(values)
    _save_settings()


def delete_export_preset(name):
    name = str(name or '')
    if name not in _settings.get('export_presets', {}):
        return False
    del _settings['export_presets'][name]
    _save_settings()
    return True

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
