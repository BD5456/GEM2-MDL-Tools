"""
PLY binary format I/O for GEM2 engine.
Handles EPLY/BNDS/SKIN/MESH/VERT/INDX blocks.
"""
import os
import struct
import glob
import json
import bpy
from mathutils import Matrix, Vector

from .i18n import _
from .core import (
    unpack_I, unpack_H, unpack_HHH, unpack_f, unpack_fff, unpack_ff, unpack_BBBB,
    pack_I, pack_H, pack_HHH, pack_f, pack_fff, pack_ff, pack_B, pack_BBBB,
    D3DFVF_XYZ, D3DFVF_XYZB2, D3DFVF_NORMAL, D3DFVF_TEX1, D3DFVF_LASTBETA_UBYTE4,
    D3DFVF_DIFFUSE,
    MESH_FLAG_LIGHT, MESH_FLAG_MATERIAL, MESH_FLAG_SKINNED, MESH_FLAG_SUBSKIN,
    row34_to_blender, ori_to_blender, read_string_at_p,
    find_matching_brace, parse_fvf_layout,
)

# ── PIL import for MTL/DDS (optional) ──────────────────────────
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def _find_mdl_path(filepath):
    """智能查找对应的 .mdl 文件"""
    base_dir = os.path.dirname(filepath)
    mesh_name = os.path.splitext(os.path.basename(filepath))[0]
    candidates = [
        os.path.join(base_dir, mesh_name + '.mdl'),
        os.path.join(base_dir, 'skin.mdl'),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    all_mdls = glob.glob(os.path.join(base_dir, '*.mdl'))
    if len(all_mdls) == 1:
        return all_mdls[0]
    return None


def _parse_bones_flat(content):
    """从 MDL 文本解析平铺骨骼字典。（副本，避免循环导入 mdl_io）"""
    import re

    def _find_brace(text, start):
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{': depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0: return i
        return -1

    def _parse_node(text):
        inner = text.strip()
        if inner.startswith('{'): inner = inner[1:].strip()
        if inner.endswith('}'): inner = inner[:-1].strip()
        nm = re.search(
            r'\bbone\s+(?:(revolute|prizmatic|prismatic)\s+)?"([^"]+)"',
            inner, re.IGNORECASE)
        if not nm:
            return None
        bone_kind = (nm.group(1) or '').lower()
        name = nm.group(2)
        is_revolute = bone_kind == 'revolute'
        is_prizmatic = bone_kind in {'prizmatic', 'prismatic'}
        children = []
        remaining = inner
        pre_text = ""
        while True:
            cs = remaining.find('{bone')
            if cs == -1:
                pre_text += remaining
                break
            pre_text += remaining[:cs]
            ce = _find_brace(remaining, cs)
            if ce == -1: break
            child = _parse_node(remaining[cs:ce+1])
            if child: children.append(child)
            remaining = remaining[ce+1:]

        matrix = None; position = None; orientation = None
        params = None; limits = None; speed = None

        m34_m = re.search(r'\{(?:Matrix34|matrix34)\s*([^}]*)\}', pre_text)
        if m34_m:
            vals = re.findall(r'[-+]?\d*\.?\d+(?:e[+-]?\d+)?', m34_m.group(1))
            if len(vals) >= 12:
                matrix = [list(map(float, vals[i*3:(i+1)*3])) for i in range(4)]

        pos_m = re.search(r'(?:Position|position)\s+([-\d.e+-]+)\s+([-\d.e+-]+)\s+([-\d.e+-]+)', pre_text)
        if pos_m:
            position = (float(pos_m.group(1)), float(pos_m.group(2)), float(pos_m.group(3)))

        ori_m = re.search(r'\{(?:Orientation|orientation)\s*([^}]*)\}', pre_text, re.DOTALL)
        if ori_m:
            vals = re.findall(r'[-+]?\d*\.?\d+(?:e[+-]?\d+)?', ori_m.group(1))
            if len(vals) >= 9:
                orientation = [list(map(float, vals[i*3:(i+1)*3])) for i in range(3)]
        if not ori_m:
            ori_m = re.search(
                r'(?:Orientation|orientation)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)',
                pre_text)
            if ori_m:
                vals = [float(ori_m.group(i)) for i in range(1, 10)]
                orientation = [vals[i*3:(i+1)*3] for i in range(3)]

        has_volumeview = '{VolumeView' in pre_text
        params_m = re.search(r'\{parameters\s+"([^"]*)"\}', pre_text)
        if params_m: params = params_m.group(1)
        limits_m = re.search(r'\{limits\s+([-\d.e+-]+)\s+([-\d.e+-]+)\}', pre_text)
        if limits_m: limits = (float(limits_m.group(1)), float(limits_m.group(2)))
        speed_m = re.search(r'\{speed\s+([-\d.e+-]+)\}', pre_text)
        if speed_m: speed = float(speed_m.group(1))

        return {
            'name': name, 'matrix': matrix, 'position': position,
            'orientation': orientation, 'children': children,
            'has_volumeview': has_volumeview, 'is_revolute': is_revolute,
            'is_prizmatic': is_prizmatic,
            'params': params, 'limits': limits, 'speed': speed,
        }

    skel_start = content.find('{Skeleton')
    if skel_start == -1:
        return None, None
    skel_end = find_matching_brace(content, skel_start)
    if skel_end == -1:
        return None, None
    skeleton_text = content[skel_start+1:skel_end]

    root_bones = []
    remaining = skeleton_text
    while True:
        bs = remaining.find('{bone')
        if bs == -1: break
        be = _find_brace(remaining, bs)
        if be == -1: break
        bone = _parse_node(remaining[bs:be+1])
        if bone: root_bones.append(bone)
        remaining = remaining[be+1:]

    if not root_bones:
        return None, None

    flat = {}
    mesh_parent = None
    def flatten(node, parent_name=None):
        nonlocal mesh_parent
        flat[node['name']] = {
            'matrix': node['matrix'],
            'position': node['position'],
            'orientation': node['orientation'],
            'parent': parent_name,
            'is_revolute': node.get('is_revolute', False),
            'is_prizmatic': node.get('is_prizmatic', False),
            'params': node.get('params'),
            'limits': node.get('limits'),
            'speed': node.get('speed'),
        }
        if node.get('has_volumeview'):
            mesh_parent = node['name']
        for child in node['children']:
            flatten(child, node['name'])
    for bone in root_bones:
        flatten(bone, None)
    return flat, mesh_parent


def _precompute_bone_world_mats(mdl_bones):
    """预计算所有骨骼的世界矩阵（自顶向下）"""
    local_mats = {}
    for name, info in mdl_bones.items():
        local_mat = Matrix.Identity(4)
        if info.get('matrix'):
            local_mat = row34_to_blender(info['matrix'])
        elif info.get('position') and info.get('orientation'):
            local_mat = ori_to_blender(info['orientation'])
            local_mat.translation = Vector(info['position'])
        elif info.get('orientation'):
            local_mat = ori_to_blender(info['orientation'])
        elif info.get('position'):
            local_mat = Matrix.Translation(Vector(info['position']))
        local_mats[name] = local_mat

    world_mats = {}
    visiting = set()

    def compute_world(name):
        if name in world_mats:
            return world_mats[name]
        if name in visiting:
            raise ValueError('MDL bone parent cycle detected at %r' % name)
        visiting.add(name)
        try:
            info = mdl_bones[name]
            parent = info.get('parent')
            local = local_mats[name]
            if parent == name:
                raise ValueError('MDL bone %r cannot parent itself' % name)
            if parent and parent in mdl_bones:
                world = compute_world(parent) @ local
            else:
                world = local.copy()
            world_mats[name] = world
            return world
        finally:
            visiting.discard(name)

    for name in mdl_bones:
        compute_world(name)
    return world_mats


# ═══════════════════════════════════════════════════════════════
#  PLY IMPORT
# ═══════════════════════════════════════════════════════════════


def _read_name_auto(file_bytes, p):
    """自适应读取字符串：优先长度前缀 [len][name]，回退 \\x00 结尾。

    GEM2 插件导出的文件用长度前缀（见 export_ply），游戏原版/第三方工具
    （如 GOH 提取器）也多用长度前缀，但个别文件用 \\x00 结尾。
    """
    total = len(file_bytes)
    if p >= total:
        return '', p
    n = file_bytes[p]
    if 0 < n < 128 and p + 1 + n <= total:
        chunk = file_bytes[p+1:p+1+n]
        if all(32 <= b < 127 or b == 0 for b in chunk):
            return chunk.decode('ascii', errors='replace').strip('\x00'), p + 1 + n
    end = file_bytes.find(b'\x00', p)
    if end == -1:
        return '', total
    return file_bytes[p:end].decode('utf-8', errors='replace'), end + 1


def _try_mtl_at(file_bytes, p):
    """尝试在 p 处按长度前缀读取材质名；失败返回 None。"""
    total = len(file_bytes)
    if p >= total:
        return None
    n = file_bytes[p]
    if 0 < n < 128 and p + 1 + n <= total:
        chunk = file_bytes[p+1:p+1+n]
        if all(32 <= b < 127 for b in chunk):
            return chunk.decode('ascii', errors='replace'), p + 1 + n
    return None


def import_ply(filepath, skip_mdl_transform=False, skip_armature=False,
               create_helpers=True):
    """主 PLY 导入函数。返回 (mesh_obj, arm_obj, root_empty)

    ``skip_armature``/``create_helpers=False`` 供载具文件夹导入：载具只创建一套
    MDL 主骨架，每个部件 PLY 不再额外扫描同目录 MDL、创建重复骨架和空节点。

    自适应解析多种二进制变体：
      - 插件导出的原生格式：EPLY+BNDS / SKIN(长度前缀) / MESH / VERT(带头) / INDX(count+u16)
      - 游戏原版格式（如 qbz-95_viwer.ply）：MESH 头多 4 字节 unk
      - GOH 提取器格式（如 KKS model.ply）：EPLYBNDSA(31B) / BSKIN / MESH /
        VERT(无头直连 32B) / INDX(无 count 头,u32) / WEIGHTS(尾部)

    skip_mdl_transform=True 时跳过「用 mdl mesh_parent 骨世界矩阵变换顶点」——
    载具按文件夹导入用：部件坐标由调用方按各自骨矩阵摆位，不能叠加 import_ply 的
    LOCAL→WORLD 自动变换（body.ply 是纯世界坐标，叠加会爆成天文数字）。
    """
    plugin_dir = os.path.dirname(os.path.abspath(__file__))

    with open(filepath, 'rb') as f:
        file_bytes = f.read()
    total = len(file_bytes)
    pos = 0

    if file_bytes[:4] != b'EPLY':
        raise Exception(_("ply.invalid_ply"))
    pos = 4
    if file_bytes[pos:pos+4] == b'BNDS':
        pos += 4 + 24
    else:
        # GOH 变体: magic(9) + bbox_min(12) + bbox_max_xy(8) + pad(2) = 31
        pos = 31

    # ── SKIN 块（BSKIN 的 'SKIN' 子串也会被 find 命中，起点一致）──
    bone_names = []
    has_skin = False
    skin_pos = file_bytes.find(b'SKIN', pos)
    if skin_pos != -1 and skin_pos + 8 <= total:
        p = skin_pos + 4
        bones_count = unpack_I(file_bytes[p:p+4])[0]
        p += 4
        if 0 < bones_count < 4096:
            names = []
            ok = True
            for _i in range(bones_count):
                name, p = _read_name_auto(file_bytes, p)
                if not name:
                    ok = False
                    break
                names.append(name)
            if ok:
                bone_names = names
                has_skin = True

    # ── MESH 块 ──
    materials_info = []
    mesh_pos = pos
    last_mesh_end = pos
    while True:
        mesh_pos = file_bytes.find(b'MESH', mesh_pos)
        if mesh_pos == -1 or mesh_pos + 20 > total:
            break
        p = mesh_pos + 4
        fvf_flags = unpack_I(file_bytes[p:p+4])[0]; p += 4
        tri_start = unpack_I(file_bytes[p:p+4])[0]; p += 4
        tri_count = unpack_I(file_bytes[p:p+4])[0]; p += 4
        mesh_flags = unpack_I(file_bytes[p:p+4])[0]; p += 4

        # 材质名: 先试直接长度前缀，再试跳过 4 字节 unk（qbz95 风格），
        # 最后回退 \x00 结尾。
        mtl_r = _try_mtl_at(file_bytes, p)
        if mtl_r:
            mat_name, p = mtl_r
        else:
            mtl_r2 = _try_mtl_at(file_bytes, p + 4)
            if mtl_r2:
                mat_name, p = mtl_r2
            else:
                mat_name, p = _read_name_auto(file_bytes, p)

        palette = None
        if has_skin and p < total:
            bm_count = file_bytes[p]; p += 1
            palette = list(file_bytes[p:p+bm_count])
            p += bm_count
        materials_info.append({
            'mat_name': mat_name, 'tri_count': tri_count,
            'fvf': fvf_flags, 'mesh_flags': mesh_flags, 'palette': palette,
        })
        last_mesh_end = p
        mesh_pos = p

    if not materials_info:
        raise Exception(_("ply.no_mesh_blocks"))
    # 用最后成功解析的 MESH 块末尾作为 VERT 探测起点 —— 否则 VERT 探测会命中
    # MESH 块材质名里的 "VERT" 子串 (如 material_#25.mtl), 顶点错位成天文数字
    pos = last_mesh_end

    # ── VERT / INDX 块: 探测带头(原生) vs 无头直连(GOH) ──
    vert_pos = file_bytes.find(b'VERT', pos)
    if vert_pos == -1:
        raise Exception(_("ply.missing_vert"))
    indx_pos = file_bytes.find(b'INDX', vert_pos)

    native_vert = False
    vert_flags = 0
    if vert_pos + 12 <= total:
        loops_probe = unpack_I(file_bytes[vert_pos+4:vert_pos+8])[0]
        stride_probe = unpack_H(file_bytes[vert_pos+8:vert_pos+10])[0]
        need = loops_probe * stride_probe
        if (0 < loops_probe < 5000000 and 24 <= stride_probe <= 128
                and 0 < need <= total - (vert_pos + 10)):
            native_vert = True

    if native_vert:
        loops_count = loops_probe
        stride = stride_probe
        vert_flags = (unpack_H(file_bytes[vert_pos+10:vert_pos+12])[0]
                      if vert_pos + 12 <= total else 0)
        vertex_data_offset = vert_pos + 12
        vertex_data = file_bytes[vertex_data_offset:vertex_data_offset+loops_count*stride]
        if indx_pos != -1 and indx_pos + 8 <= total:
            index_count = unpack_I(file_bytes[indx_pos+4:indx_pos+8])[0]
            index_data = file_bytes[indx_pos+8:indx_pos+8+index_count*2]
        else:
            raise Exception(_("ply.missing_indx"))
        fvf_global = materials_info[0]['fvf']
        layout, has_skin_actual, fvf_size = parse_fvf_layout(fvf_global)
        has_normal = 'NORMAL' in layout
        has_tex = 'TEX' in layout
        has_skin_actual = 'SKIN' in layout
        skin_offset, skin_size = layout.get('SKIN', (0, 0))
        pos_offset, pos_size = layout.get('POSITION', (0, 12))
        norm_offset, norm_size = layout.get('NORMAL', (0, 0))
        tex_offset, tex_size = layout.get('TEX', (0, 0))
        goh_weights = None
    else:
        # GOH 变体: VERT 无头直连 [pos12+norm12+uv8]*N; INDX 无 count 头, u32
        vp = vert_pos + 4
        vertex_data_offset = vp
        if indx_pos != -1 and indx_pos > vp:
            loops_count = (indx_pos - vp) // 32
        else:
            loops_count = 0
        stride = 32
        vertex_data = file_bytes[vp:vp + loops_count * 32]
        # GOH 的 MESH 字段是索引数(idx_count)，转换为三角形数
        for mi in materials_info:
            mi['tri_count'] = mi['tri_count'] // 3
        index_count = 0
        for mi in materials_info:
            index_count += mi['tri_count'] * 3
        index_data = file_bytes[indx_pos+4:indx_pos+4+index_count*4] if indx_pos != -1 else b''
        layout = {}
        has_normal = True
        has_tex = True
        has_skin_actual = False
        skin_offset = 0; skin_size = 0
        pos_offset = 0; pos_size = 12
        norm_offset = 0; norm_size = 12
        tex_offset = 0; tex_size = 8
        fvf_size = 32
        # 尾部 WEIGHTS 块: [count(4) + (bone_idx(4)+weight(4))*count] * N
        goh_weights = []
        wp = indx_pos + 4 + index_count * 4 if indx_pos != -1 else total
        for _v in range(loops_count):
            if wp + 4 > total:
                goh_weights.append([])
                break
            n = unpack_I(file_bytes[wp:wp+4])[0]
            wp += 4
            infs = []
            for _i in range(min(n, 8)):
                if wp + 8 > total:
                    break
                bi = unpack_I(file_bytes[wp:wp+4])[0]
                bw = unpack_f(file_bytes[wp+4:wp+8])[0]
                wp += 8
                if bw > 0.001:
                    infs.append((bw, bi))
            goh_weights.append(infs)

    # ── 解析顶点数据 ──
    verts_co = []
    verts_norm = []
    verts_uv = []
    verts_weights = []
    verts_tail = []  # E6.19: FVF 之外的多余字节 (原版载具 = tangent float3 + w=1.0)

    if goh_weights is not None:
        verts_weights = goh_weights

    for i in range(loops_count):
        off = i * stride
        x, y, z = unpack_fff(vertex_data[off:off+12])
        verts_co.append((x, y, z))
        off += 12
        # FVF 之外的尾载荷（通常 tangent float4）按实际 FVF 长度截取。
        # 不能硬编码 32：skinned stride56 的 FVF 本体是 40 字节。
        verts_tail.append(
            vertex_data[i * stride + fvf_size:i * stride + stride]
            if stride > fvf_size else b'')

        if has_skin_actual and goh_weights is None:
            w1 = unpack_f(vertex_data[off:off+4])[0]
            b1, b2, b3, b4 = unpack_BBBB(vertex_data[off+4:off+8])
            valid = [idx for idx in (b1, b2, b3, b4) if idx > 0]
            if not valid:
                verts_weights.append([])
            else:
                remaining = max(0.0, 1.0 - w1)
                if len(valid) == 1:
                    weights = [(1.0, valid[0])]
                elif len(valid) == 2:
                    weights = [(w1, valid[0]), (remaining, valid[1])]
                else:
                    per_bone = remaining / (len(valid) - 1)
                    weights = [(w1, valid[0])]
                    for idx in valid[1:]:
                        weights.append((per_bone, idx))
                verts_weights.append(weights)
            off += 8

        if has_normal:
            nx, ny, nz = unpack_fff(vertex_data[off:off+12])
            verts_norm.append((nx, ny, nz))
            off += 12

        if 'DIFFUSE' in layout:
            off += 4

        if has_tex:
            u, v = unpack_ff(vertex_data[off:off+8])
            v_fixed = -v if v < 0 else 1.0 - v
            verts_uv.append((u, v_fixed))
            off += 8

    # ── 解析索引（面） ──
    triangles = []
    if goh_weights is not None:
        for i in range(index_count // 3):
            i12 = i * 12
            idx1 = unpack_I(index_data[i12:i12+4])[0]
            idx2 = unpack_I(index_data[i12+4:i12+8])[0]
            idx3 = unpack_I(index_data[i12+8:i12+12])[0]
            triangles.append((idx1, idx3, idx2))
    else:
        for i in range(index_count // 3):
            idx1, idx2, idx3 = unpack_HHH(index_data[i*6:(i+1)*6])
            triangles.append((idx1, idx3, idx2))

    # ── 材质分配 ──
    triangle_materials = []
    for mi, info in enumerate(materials_info):
        triangle_materials.extend([mi] * info['tri_count'])
    while len(triangle_materials) < len(triangles):
        triangle_materials.append(0)

    source_vertex_count = len(verts_co)
    source_vertex_indices = list(range(source_vertex_count))

    # ── 修剪不被面引用的顶点 (E6.19) ──────────────────────────────
    # 一些导出文件 (如 m61a5 载具 body.ply) 在 VERT 里多写 1 个"填充/padding"
    # 顶点 (vertex_count = 28994, 但 INDX 只引用 0..28992)。该顶点字节是垃圾,
    # 按 float 读出来是天文数字 (x=1.06e21, z=1.05e21) —— 既撑爆整体顶点包围盒,
    # 又破坏载具"质心 vs 骨位置"摆位判据。策略: 丢掉任何不被三角形引用的顶点,
    # 并把 triangles 的索引重映射到连续新索引 (保 co/normal/UV/权重数组对齐)。
    if verts_co:
        referenced = set()
        for tri in triangles:
            referenced.update(tri)
        if len(referenced) < len(verts_co):
            old_idx = sorted(referenced)
            source_vertex_indices = old_idx
            remap = {old: new for new, old in enumerate(old_idx)}
            n_orig = len(verts_co)
            verts_co = [verts_co[old] for old in old_idx]
            if verts_norm:
                verts_norm = [verts_norm[old] for old in old_idx]
            if verts_uv:
                verts_uv = [verts_uv[old] for old in old_idx]
            if len(verts_weights) == n_orig:
                verts_weights = [verts_weights[old] for old in old_idx]
            if len(verts_tail) == n_orig:
                verts_tail = [verts_tail[old] for old in old_idx]
            triangles = [tuple(remap[i] for i in tri) for tri in triangles]
            print('[ply] pruned %d unreferenced padding vertices (%d -> %d)'
                  % (n_orig - len(referenced), n_orig, len(referenced)))

    # ── 构建网格 (保持原始顶点数, 不去重) ──
    mesh_name = os.path.splitext(os.path.basename(filepath))[0]
    mesh = bpy.data.meshes.new(mesh_name)
    obj = bpy.data.objects.new(mesh_name, mesh)
    bpy.context.collection.objects.link(obj)

    root_empty = None
    if create_helpers:
        root_empty = bpy.data.objects.new(mesh_name + "_Root", None)
        bpy.context.collection.objects.link(root_empty)
        root_empty.empty_display_type = 'PLAIN_AXES'
        root_empty.empty_display_size = 0.5
        root_empty.location = (0, 0, 0)
        obj.parent = root_empty

    mesh.vertices.add(len(verts_co))
    flat_co = [c for v in verts_co for c in v]
    mesh.vertices.foreach_set('co', flat_co)

    num_loops = len(triangles) * 3
    mesh.loops.add(num_loops)
    loop_verts = [tri[corner] for tri in triangles for corner in range(3)]
    mesh.loops.foreach_set('vertex_index', loop_verts)

    mesh.polygons.add(len(triangles))
    mesh.polygons.foreach_set('loop_start', list(range(0, num_loops, 3)))
    mesh.polygons.foreach_set('loop_total', [3] * len(triangles))

    mesh.update()

    # ── E6.19: 保存格式元数据 (载具往返导出用) ──
    # fvf / mesh_flags / stride 是原文件的字节级事实; tangent 尾 (stride 超出
    # pos+normal+uv 的部分, 原版载具 = tangent float3 + w=1.0 共 16B) 存进
    # 顶点属性 gem2_tail, 导出时原样写回 → bump/tangent 数据不丢。
    try:
        obj['gem2_ply_fvf'] = materials_info[0]['fvf'] if materials_info else 0
        obj['gem2_ply_mesh_flags'] = (materials_info[0].get('mesh_flags', 0)
                                      if materials_info else 0)
        obj['gem2_vertex_stride'] = int(stride)
        obj['gem2_ply_mat_name'] = (materials_info[0].get('mat_name', '')
                                    if materials_info else '')
        obj['gem2_vert_flags'] = int(vert_flags)
        obj['gem2_has_skin'] = bool(has_skin)
        obj['gem2_ply_source'] = os.path.abspath(filepath)
        obj['gem2_ply_native_vert'] = bool(native_vert)
        obj['gem2_vertex_data_offset'] = int(vertex_data_offset)
        obj['gem2_source_vertex_count'] = int(source_vertex_count)
        obj['gem2_source_vertex_indices'] = json.dumps(source_vertex_indices)
        obj['gem2_source_triangle_count'] = int(len(triangles))
    except Exception:
        pass
    # 原文件逐顶点 normal 字节 (Blender 平滑法线≠文件法线, 必须原样保存;
    # FLOAT_VECTOR 无色彩空间, 负分量不被 clamp)
    if verts_norm and len(verts_norm) == len(mesh.vertices):
        try:
            nrm_attr = mesh.attributes.new('gem2_nrm', 'FLOAT_VECTOR', 'POINT')
            for i, nv in enumerate(verts_norm):
                nrm_attr.data[i].vector = nv
        except Exception:
            pass
    # tangent 尾: xyz 存 FLOAT_VECTOR + w 存 FLOAT —— FLOAT_COLOR 会走色彩空间
    # 转换把负分量 clamp 成 0 (实测 x=-0.85 被吃), 必须用无色彩空间类型。
    if verts_tail and any(verts_tail) and len(verts_tail) == len(mesh.vertices):
        try:
            t_attr = mesh.attributes.new('gem2_tail', 'FLOAT_VECTOR', 'POINT')
            w_attr = mesh.attributes.new('gem2_tail_w', 'FLOAT', 'POINT')
            for i, t in enumerate(verts_tail):
                tb = t[:16].ljust(16, b'\x00')
                x, y, z, w = struct.unpack('<ffff', tb)
                t_attr.data[i].vector = (x, y, z)
                w_attr.data[i].value = w
        except Exception:
            pass

    # ── 材质 ──
    base_dir = os.path.dirname(filepath)
    for info in materials_info:
        mat_name = info['mat_name'].replace('.mtl', '')
        if not mat_name:
            mat_name = 'default'
        mtl_path = os.path.join(base_dir, mat_name + '.mtl')
        mtl_source = os.path.normcase(os.path.abspath(mtl_path))
        mat = bpy.data.materials.get(mat_name)
        if mat is not None and mat.get('gem2_mtl_source') != mtl_source:
            mat = None
        if mat is None:
            mat = bpy.data.materials.new(name=mat_name)
        if os.path.isfile(mtl_path) and mat.get('gem2_mtl_source') != mtl_source:
            _parse_and_apply_mtl(mtl_path, mat, base_dir)
            mat['gem2_mtl_source'] = mtl_source
        mesh.materials.append(mat)

    # ── UV ──
    if verts_uv:
        uv_layer = mesh.uv_layers.new(name="UVMap")
        for poly_idx, tri in enumerate(triangles):
            for corner in range(3):
                loop_idx = poly_idx * 3 + corner
                vert_idx = tri[corner]
                if vert_idx < len(verts_uv):
                    uv_layer.data[loop_idx].uv = verts_uv[vert_idx]

    # ── 法线 ──
    if verts_norm:
        try:
            loop_normals = []
            for poly_idx, tri in enumerate(triangles):
                for corner in range(3):
                    vert_idx = tri[corner]
                    if vert_idx < len(verts_norm):
                        loop_normals.append(verts_norm[vert_idx])
                    else:
                        loop_normals.append((0.0, 0.0, 1.0))
            mesh.normals_split_custom_set(loop_normals)
        except (AttributeError, RuntimeError):
            pass

    # ── 材质分配到面 ──
    for i, poly in enumerate(mesh.polygons):
        if i < len(triangle_materials):
            poly.material_index = triangle_materials[i]
    mesh.validate(clean_customdata=True)

    # ── 顶点所属材质索引映射 ──
    vert_mesh_idx = [-1] * len(mesh.vertices)
    for poly in mesh.polygons:
        mi = poly.material_index
        for li in poly.loop_indices:
            vi = mesh.loops[li].vertex_index
            if vert_mesh_idx[vi] == -1:
                vert_mesh_idx[vi] = mi
    mesh.update()

    # ── MDL 骨骼 ──
    mdl_path = None if skip_armature else _find_mdl_path(filepath)
    mdl_bones = None
    mesh_parent_name = None
    if mdl_path and os.path.isfile(mdl_path):
        print(_("ply.mdl_found", path=mdl_path))
        with open(mdl_path, 'r', encoding='utf-8', errors='ignore') as f:
            mdl_bones, mesh_parent_name = _parse_bones_flat(f.read())
    elif not skip_armature:
        print(_("ply.mdl_not_found"))

    # ── 骨架创建 ──
    arm_obj = None
    world_mats = None
    if mdl_bones:
        arm_obj = _build_armature_from_mdl(
            mesh_name, mdl_bones, mesh_parent_name, mdl_path)
        world_mats = _precompute_bone_world_mats(mdl_bones)
    elif not skip_armature and has_skin and bone_names:
        arm_data = bpy.data.armatures.new(mesh_name + "_Arm")
        arm_obj = bpy.data.objects.new(mesh_name + "_Armature", arm_data)
        bpy.context.collection.objects.link(arm_obj)
        bpy.context.view_layer.objects.active = arm_obj
        bpy.ops.object.mode_set(mode='EDIT')
        for name in bone_names:
            eb = arm_data.edit_bones.new(name)
            eb.head = (0, 0, 0)
            eb.tail = (0.5, 0, 0)
        bpy.ops.object.mode_set(mode='OBJECT')
        arm_obj.display_type = 'WIRE'
        arm_obj.show_in_front = True
        world_mats = None

    if arm_obj and root_empty:
        arm_obj.parent = root_empty

    # ── 原点居中 (skip_mdl_transform 模式跳过: 载具按骨摆位不居中) ──
    if (not skip_mdl_transform and root_empty and arm_obj
            and 'body' in arm_obj.data.bones):
        body_bone = arm_obj.data.bones['body']
        body_world = arm_obj.matrix_world @ body_bone.head_local
        root_empty.location = -body_world

    # ── 顶点空间变换：PLY → Blender ──
    # Skinned PLY coordinates exclude only the VolumeView node's local matrix.
    # Its ancestor basis/body transforms act on skeleton and mesh together in
    # GEM2 and must not be applied a second time here (doing so mirrors L/R in
    # GFA characters). Static/rigid PLY keeps the ordinary parent-world rule;
    # vehicle folder import skips this block and applies its own bone world.
    if not skip_mdl_transform and mesh_parent_name and world_mats \
            and mesh_parent_name in world_mats:
        mesh_world = world_mats[mesh_parent_name]
        if has_skin and mdl_bones and mesh_parent_name in mdl_bones:
            ancestor_name = mdl_bones[mesh_parent_name].get('parent')
            if ancestor_name and ancestor_name in world_mats:
                mesh_world = (world_mats[ancestor_name].inverted()
                              @ world_mats[mesh_parent_name])
        for v in mesh.vertices:
            v.co = mesh_world @ v.co
        mesh.update()

    mesh.validate(clean_customdata=True)

    # ``validate`` may remove duplicate/invalid source triangles (for example
    # t-90m/turret.ply contains one byte-identical duplicate face). Vehicle
    # safe-export must compare against the topology Blender actually retained,
    # while the raw source triangle count remains available for diagnostics.
    try:
        import hashlib
        mesh.calc_loop_triangles()
        topo = bytearray()
        for tri in mesh.loop_triangles:
            topo.extend(struct.pack('<III', *tri.vertices))
        obj['gem2_topology_hash'] = hashlib.sha256(topo).hexdigest()
        obj['gem2_imported_triangle_count'] = len(mesh.loop_triangles)
    except Exception:
        pass

    # ── 顶点权重 ──
    if has_skin and arm_obj and verts_weights:
        valid_bone_names = set(b.name for b in arm_obj.data.bones)

        vg_dict = {}
        for bone_idx, bone_name in enumerate(bone_names):
            if bone_name in valid_bone_names:
                vg_dict[bone_idx] = obj.vertex_groups.new(name=bone_name)

        mat_bone_map = {}
        for mi, info in enumerate(materials_info):
            pal = info.get('palette')
            if pal:
                bm = {}
                for li, gi in enumerate(pal):
                    if gi > 0:
                        bm[li] = gi - 1 if goh_weights is None else gi
                mat_bone_map[mi] = bm

        vert_acc = [{} for _i in range(len(verts_weights))]
        for tri_idx, (i0, i1, i2) in enumerate(triangles):
            mi = triangle_materials[tri_idx] if tri_idx < len(triangle_materials) else 0
            bm = mat_bone_map.get(mi) if mat_bone_map else None
            for vi in (i0, i1, i2):
                if vi >= len(verts_weights):
                    continue
                for w, local_idx in verts_weights[vi]:
                    if w <= 0.001:
                        continue
                    if bm:
                        bone_idx = bm.get(local_idx)
                    elif goh_weights is not None:
                        bone_idx = local_idx
                    else:
                        bone_idx = local_idx - 1 if local_idx > 0 else None
                    if bone_idx is not None and bone_idx in vg_dict:
                        acc = vert_acc[vi].get(bone_idx, 0.0)
                        vert_acc[vi][bone_idx] = acc + w

        for v_idx, wdict in enumerate(vert_acc):
            if not wdict:
                continue
            total = sum(wdict.values())
            if total <= 0:
                continue
            for bone_idx, w in wdict.items():
                vg_dict[bone_idx].add([v_idx], w / total, 'REPLACE')

        obj["gem2_ply_source"] = filepath

        mod = obj.modifiers.new(name="Armature", type='ARMATURE')
        mod.object = arm_obj
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)

    # ── 根节点归零 (skip_mdl_transform 模式: 载具按骨摆位, 保留坐标不归零) ──
    if not skip_mdl_transform and root_empty:
        _apply_root_transform(obj, arm_obj, root_empty)

    # ── 群组节点 ──
    group_empty = None
    if create_helpers:
        group_empty = bpy.data.objects.new(mesh_name + "_Model", None)
        bpy.context.collection.objects.link(group_empty)
        group_empty.empty_display_type = 'PLAIN_AXES'
        group_empty.empty_display_size = 0.5
        group_empty.location = (0, 0, 0)
        obj.parent = group_empty
        if arm_obj:
            arm_obj.parent = group_empty
        _fix_viewport()

    print(_("ply.import_success", name=mesh_name, verts=len(verts_co), tris=len(triangles)))
    return obj, arm_obj, group_empty


def _build_armature_from_mdl(mesh_name, mdl_bones, mesh_parent_name, mdl_path):
    """从 MDL 数据构建 Blender 骨架"""
    arm_data = bpy.data.armatures.new(mesh_name + "_Arm")
    arm_obj = bpy.data.objects.new(mesh_name + "_Armature", arm_data)
    bpy.context.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')
    edit_bones = arm_obj.data.edit_bones

    local_mats = {}
    for name, info in mdl_bones.items():
        local_mat = Matrix.Identity(4)
        if info.get('matrix'):
            local_mat = row34_to_blender(info['matrix'])
        elif info.get('position') and info.get('orientation'):
            local_mat = ori_to_blender(info['orientation'])
            local_mat.translation = Vector(info['position'])
        elif info.get('orientation'):
            local_mat = ori_to_blender(info['orientation'])
        elif info.get('position'):
            local_mat = Matrix.Translation(Vector(info['position']))
        local_mats[name] = local_mat

    world_mats = {}
    visiting = set()

    def compute_world(name):
        if name in world_mats:
            return world_mats[name]
        if name in visiting:
            raise ValueError('MDL bone parent cycle detected at %r' % name)
        visiting.add(name)
        try:
            info = mdl_bones[name]
            parent = info.get('parent')
            local = local_mats[name]
            if parent == name:
                raise ValueError('MDL bone %r cannot parent itself' % name)
            if parent and parent in mdl_bones:
                world = compute_world(parent) @ local
            else:
                world = local.copy()
            world_mats[name] = world
            return world
        finally:
            visiting.discard(name)

    for name in mdl_bones:
        compute_world(name)

    created_bones = {}
    # PASS 1: 创建所有骨骼，设置世界矩阵
    for name in mdl_bones:
        eb = edit_bones.new(name)
        created_bones[name] = eb
        eb.matrix = world_mats[name]

    # PASS 2: 设置父子关系
    for name, info in mdl_bones.items():
        parent_name = info.get('parent')
        if parent_name and parent_name in created_bones:
            eb = created_bones[name]
            eb.parent = created_bones[parent_name]
            eb.use_connect = False

    # 修正骨骼尾部（X 轴指向子骨骼）
    for eb in edit_bones:
        if eb.children:
            first_child = list(eb.children)[0]
            eb.tail = first_child.head.copy()
        else:
            x_axis = eb.matrix.col[0].xyz
            if x_axis.length < 0.001:
                x_axis = Vector((1, 0, 0))
            eb.tail = eb.head + x_axis.normalized() * 0.2

    bpy.ops.object.mode_set(mode='OBJECT')

    # 存储原始数据（供导出使用）
    mat_data = {name: [[float(v) for v in row] for row in mat]
                for name, mat in world_mats.items()}
    parent_data = {name: (info.get('parent') or '')
                   for name, info in mdl_bones.items()}
    meta_data = {}
    for name, info in mdl_bones.items():
        m = {}
        if info.get('is_revolute'): m['r'] = True
        if info.get('is_prizmatic'): m['z'] = True
        if info.get('params'): m['p'] = info['params']
        if info.get('limits'): m['l'] = list(info['limits'])
        if info.get('speed') is not None: m['s'] = info['speed']
        if m: meta_data[name] = m
    arm_obj['gem2_world_mats'] = json.dumps(mat_data)
    arm_obj['gem2_parents'] = json.dumps(parent_data)
    arm_obj['gem2_meta'] = json.dumps(meta_data)
    arm_obj['gem2_mesh_parent'] = mesh_parent_name or ''
    arm_obj['gem2_mdl_path'] = mdl_path
    arm_obj.display_type = 'WIRE'
    arm_obj.show_in_front = True

    return arm_obj


def _parse_and_apply_mtl(mtl_path, mat, base_dir):
    """解析 .mtl 并设置 Principled BSDF"""
    from .mtl_io import import_mtl
    import_mtl(mtl_path, mat, base_dir)


def _apply_root_transform(obj, arm_obj, root_empty):
    """归零根节点变换"""
    try:
        children = list(root_empty.children)
        for child in children:
            if child.type == 'MESH':
                bpy.context.view_layer.objects.active = child
                bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
            child.parent = None
        bpy.data.objects.remove(root_empty, do_unlink=True)
    except Exception as e:
        print(_("ply.auto_center_failed", error=e))


def _fix_viewport():
    """修复大模型视口裁剪"""
    try:
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.clip_end = max(space.clip_end, 100000)
                with bpy.context.temp_override(area=area):
                    bpy.ops.view3d.view_all(center=False)
                break
    except Exception as e:
        print(_("ply.viewport_fix_failed", error=e))


# ═══════════════════════════════════════════════════════════════
#  PLY EXPORT
# ═══════════════════════════════════════════════════════════════

def export_ply(filepath, mesh_obj, arm_obj, unit_scale=1.0, skin_world=None):
    """导出二进制 PLY 文件，并按最终属性共享可复用的顶点。"""
    mesh = mesh_obj.data
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = mesh_obj.evaluated_get(depsgraph)

    if not mesh.uv_layers.active:
        raise Exception("Mesh has no UV layers")
    if not mesh.materials:
        raise Exception("Mesh has no materials")

    mesh.update()
    mesh.calc_loop_triangles()
    loop_tris = mesh.loop_triangles

    has_skin = bool(arm_obj and mesh_obj.vertex_groups)
    bones_count = len(mesh_obj.vertex_groups) if has_skin else 0

    skin_world_inv = Matrix.Identity(4)
    if has_skin and skin_world is not None:
        skin_world_inv = skin_world.inverted()

    uvs = [uv.uv for uv in mesh.uv_layers.active.data]
    if has_skin:
        from heapq import nlargest
        vertex_weights = [
            [(g.weight, g.group) for g in nlargest(2, v.groups, key=lambda g: g.weight)]
            for v in mesh.vertices
        ]

    # Group triangles by material, then deduplicate the exact bytes that will
    # be written for each output vertex. This preserves UV/normal/weight seams.
    tris_by_mat = [[] for _i in mesh.materials]
    for tri in loop_tris:
        if 0 <= tri.material_index < len(tris_by_mat):
            tris_by_mat[tri.material_index].append(tri)

    vertex_records = []
    vertex_lookup = {}

    def record_for_loop(loop):
        v_idx = loop.vertex_index
        v = mesh.vertices[v_idx]
        pos = skin_world_inv @ v.co
        record = bytearray()
        record.extend(pack_fff(pos.x * unit_scale, pos.y * unit_scale,
                               pos.z * unit_scale))
        if has_skin:
            wl = vertex_weights[v_idx] + [(0, 0)] * (4 - len(vertex_weights[v_idx]))
            weight_sum = wl[0][0] + wl[1][0]
            inv = 1.0 / weight_sum if weight_sum > 0 else 1.0
            record.extend(pack_f(wl[0][0] * inv))
            record.extend(pack_BBBB(*(w[1] for w in wl)))
        record.extend(pack_fff(*loop.normal))
        record.extend(pack_I(0xFFFFFFFF))
        record.extend(pack_ff(uvs[loop.index][0], 1.0 - uvs[loop.index][1]))
        key = bytes(record)
        idx = vertex_lookup.get(key)
        if idx is None:
            idx = len(vertex_records)
            vertex_lookup[key] = idx
            vertex_records.append(key)
        return idx

    tri_indices_by_mat = []
    for mat_tris in tris_by_mat:
        out_tris = []
        for tri in mat_tris:
            out_tris.append((record_for_loop(mesh.loops[tri.loops[0]]),
                             record_for_loop(mesh.loops[tri.loops[2]]),
                             record_for_loop(mesh.loops[tri.loops[1]])))
        tri_indices_by_mat.append(out_tris)

    vertex_count = len(vertex_records)
    if vertex_count > 0xffff:
        raise Exception(_("ply.export.unique_vertex_limit", vertices=vertex_count))

    with open(filepath, "wb") as f:
        f.write(b"EPLY")

        bbox_min = skin_world_inv @ (Vector(eval_obj.bound_box[0]) * unit_scale)
        bbox_max = skin_world_inv @ (Vector(eval_obj.bound_box[6]) * unit_scale)
        f.write(b"BNDS")
        f.write(pack_fff(*bbox_min))
        f.write(pack_fff(*bbox_max))

        if has_skin:
            f.write(b"SKIN")
            f.write(pack_I(bones_count))
            for vg in mesh_obj.vertex_groups:
                try:
                    name_bytes = vg.name.encode("ascii")
                except UnicodeEncodeError:
                    continue  # skip Japanese-named VGs
                f.write(pack_B(len(name_bytes)))
                f.write(name_bytes)

        tri_start = 0
        for i, mat_tris in enumerate(tri_indices_by_mat):
            f.write(b"MESH")
            fvf = D3DFVF_NORMAL | D3DFVF_TEX1 | D3DFVF_DIFFUSE
            if has_skin:
                fvf |= D3DFVF_XYZB2 | D3DFVF_LASTBETA_UBYTE4
            else:
                fvf |= D3DFVF_XYZ
            f.write(pack_I(fvf))
            f.write(pack_I(tri_start))
            tri_count = len(mat_tris)
            f.write(pack_I(tri_count))
            tri_start += tri_count

            flags = MESH_FLAG_LIGHT | MESH_FLAG_MATERIAL
            if has_skin:
                flags |= MESH_FLAG_SKINNED | MESH_FLAG_SUBSKIN
            f.write(pack_I(flags))

            try:
                mat_name = mesh.materials[i].name
            except:
                mat_name = ""
            mtl_name = mat_name + ".mtl"
            f.write(pack_B(len(mtl_name)))
            f.write(mtl_name.encode("ascii"))

            if has_skin:
                f.write(pack_B(bones_count))
                f.write(struct.pack("B" * bones_count, *(i + 1 for i in range(bones_count))))

        stride = len(vertex_records[0]) if vertex_records else (44 if has_skin else 36)
        f.write(b"VERT")
        f.write(pack_I(vertex_count))
        f.write(pack_H(stride))
        f.write(b"\x07\x00")

        for record in vertex_records:
            f.write(record)

        f.write(b"INDX")
        index_count = sum(len(x) for x in tri_indices_by_mat) * 3
        f.write(pack_I(index_count))
        for mat_tris in tri_indices_by_mat:
            for tri in mat_tris:
                f.write(pack_HHH(*tri))


def export_vol(filepath, mesh_obj, unit_scale=1.0):
    """Export binary VOL collision geometry file (EVLM format)"""
    mesh = mesh_obj.data
    mesh.calc_loop_triangles()
    loop_tris = mesh.loop_triangles
    edges_count = len(loop_tris) * 3
    vertices_count = len(mesh.vertices)

    if edges_count > 0xffff:
        raise Exception(f"VOL edges count ({edges_count}) exceeds 65535")
    if vertices_count > 0xffff:
        raise Exception(f"VOL vertices count ({vertices_count}) exceeds 65535")

    coords = [v.co * unit_scale for v in mesh.vertices]

    with open(filepath, "wb") as f:
        f.write(b"EVLM")
        f.write(b"VERT")
        f.write(pack_I(vertices_count))
        for co in coords:
            f.write(pack_fff(*co))

        f.write(b"INDX")
        f.write(pack_I(edges_count))
        for tri in loop_tris:
            # E6.19: import_vol 导入时绕序翻转 (i0,i2,i1) → 导出翻回 (i0,i1,i2),
            # 与原文件字节一致 (否则碰撞面法线方向反)。
            f.write(pack_HHH(tri.vertices[0], tri.vertices[2], tri.vertices[1]))

        f.write(b"SIDE")
        f.write(pack_I(edges_count // 3))
        for tri in loop_tris:
            f.write(pack_B(tri.material_index + 1))


def import_vol(filepath):
    """Import binary VOL collision geometry file (EVLM format)"""
    with open(filepath, 'rb') as f:
        file_bytes = f.read()
    p = 0
    if file_bytes[:4] != b'EVLM':
        raise Exception(_("ply.vol_invalid"))

    vert_pos = file_bytes.find(b'VERT', 4)
    if vert_pos == -1:
        raise Exception(_("ply.vol_missing_vert"))
    p = vert_pos + 4
    vertex_count = unpack_I(file_bytes[p:p+4])[0]; p += 4
    vertex_data_offset = p
    verts = [unpack_fff(file_bytes[p+i*12:p+(i+1)*12]) for i in range(vertex_count)]
    p += vertex_count * 12

    indx_pos = file_bytes.find(b'INDX', p)
    if indx_pos == -1:
        raise Exception(_("ply.vol_missing_indx"))
    p = indx_pos + 4
    index_count = unpack_I(file_bytes[p:p+4])[0]; p += 4
    indices = [unpack_H(file_bytes[p+i*2:p+(i+1)*2])[0] for i in range(index_count)]
    p += index_count * 2

    side_pos = file_bytes.find(b'SIDE', p)
    material_ids = []
    face_count = index_count // 3
    if side_pos != -1:
        p = side_pos + 4
        side_face_count = unpack_I(file_bytes[p:p+4])[0]; p += 4
        material_ids = list(file_bytes[p:p+min(side_face_count, face_count)])

    triangles = [(indices[i*3], indices[i*3+2], indices[i*3+1]) for i in range(face_count)]

    mesh_name = os.path.splitext(os.path.basename(filepath))[0]
    mesh = bpy.data.meshes.new(mesh_name)
    obj = bpy.data.objects.new(mesh_name, mesh)
    bpy.context.collection.objects.link(obj)

    mesh.from_pydata(verts, [], triangles)
    mesh.update()
    obj['gem2_vol_source'] = os.path.abspath(filepath)
    obj['gem2_vol_vertex_data_offset'] = int(vertex_data_offset)
    obj['gem2_vol_vertex_count'] = int(vertex_count)
    obj['gem2_vol_index_count'] = int(index_count)

    if material_ids:
        for mat_id in sorted(set(material_ids)):
            mat_name = f"{mesh_name}_mat_{mat_id}"
            mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(name=mat_name)
            mesh.materials.append(mat)
        for i, poly in enumerate(mesh.polygons):
            if i < len(material_ids):
                poly.material_index = material_ids[i] - 1 if material_ids[i] > 0 else 0

    print(_("ply.vol_import_success", name=mesh_name, verts=vertex_count, faces=face_count))
    return obj
