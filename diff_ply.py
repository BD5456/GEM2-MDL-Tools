"""
PLY 对比工具 — 对比两个 GEM2 PLY 文件的顶点位置差异。
重点关注头部顶点 (Z > 某阈值)。

用法: python diff_ply.py <original.ply> <exported.ply>
"""
import struct
import sys
import math
import os

def read_ply_vertices(filepath):
    """读取 GEM2 PLY 的顶点位置和权重"""
    with open(filepath, 'rb') as f:
        data = f.read()

    if data[:4] != b'EPLY':
        raise ValueError(_("diff.invalid_ply", path=filepath))

    pos = 4 + 4 + 24  # EPLY + BNDS + 24B BBox

    # 跳过 SKIN
    skin_pos = data.find(b'SKIN', pos)
    if skin_pos != -1:
        pos = skin_pos + 4
        bc = struct.unpack_from('<I', data, pos)[0]; pos += 4
        for _ in range(bc):
            nl = data[pos]; pos += 1 + nl

    # 跳过 MESH 块，找到 VERT
    while data[pos:pos+4] == b'MESH':
        pos += 4
        fvf = struct.unpack_from('<I', data, pos)[0]; pos += 4
        ts = struct.unpack_from('<I', data, pos)[0]; pos += 4
        tc = struct.unpack_from('<I', data, pos)[0]; pos += 4
        mf = struct.unpack_from('<I', data, pos)[0]; pos += 4
        if mf & 0x0200:
            pos += 4
        nl = data[pos]; pos += 1; pos += nl
        if mf & 0x0800:
            sc = data[pos]; pos += 1; pos += sc
        else:
            pc = struct.unpack_from('<H', data, pos)[0]; pos += 2; pos += pc

    # VERT 块
    pos += 4  # 'VERT'
    vc = struct.unpack_from('<I', data, pos)[0]; pos += 4
    stride = struct.unpack_from('<H', data, pos)[0]; pos += 2 + 2

    has_skin = bool(fvf & 0x0008) or bool(fvf & 0x0006)
    has_diff = bool(fvf & 0x0040)

    verts = []
    for i in range(vc):
        off = pos + i * stride
        px, py, pz = struct.unpack_from('<3f', data, off)
        if has_skin:
            w1 = struct.unpack_from('<f', data, off+12)[0]
            b1,b2,b3,b4 = struct.unpack_from('<BBBB', data, off+16)
            weights = (w1, b1, b2, b3, b4)
        else:
            weights = None
        verts.append(((px, py, pz), weights))

    return verts, fvf, stride


def compare(orig_path, export_path):
    if not os.path.isfile(orig_path):
        print(_("diff.err.src_not_found", path=orig_path))
        return
    if not os.path.isfile(export_path):
        print(_("diff.err.export_not_found", path=export_path))
        return

    o_verts, o_fvf, o_stride = read_ply_vertices(orig_path)
    e_verts, e_fvf, e_stride = read_ply_vertices(export_path)

    print(f"源文件: {len(o_verts)} 顶点, FVF=0x{o_fvf:04X}, stride={o_stride}")
    print(f"导出文件: {len(e_verts)} 顶点, FVF=0x{e_fvf:04X}, stride={e_stride}")

    if len(o_verts) != len(e_verts):
        print(f"\n⚠ 顶点数不匹配! {len(o_verts)} vs {len(e_verts)}")
        return

    # 统计所有顶点的位置差异
    diffs = []
    head_diffs = []
    for i, (o, e) in enumerate(zip(o_verts, e_verts)):
        op, ow = o; ep, ew = e
        dx = abs(op[0] - ep[0])
        dy = abs(op[1] - ep[1])
        dz = abs(op[2] - ep[2])
        dist = math.sqrt(dx*dx + dy*dy + dz*dz)
        diffs.append(dist)
        if op[2] > 0.5 or ep[2] > 0.5:
            head_diffs.append((i, op, ep, dist))

    # 整体统计
    diffs.sort()
    print(f"\n整体差异 ({len(diffs)} 顶点):")
    print(f"  max:  {diffs[-1]:.6f}")
    print(f"  mean: {sum(diffs)/len(diffs):.6f}")
    print(f"  median: {diffs[len(diffs)//2]:.6f}")
    print(f"  > 0.001: {sum(1 for d in diffs if d > 0.001)} 顶点")

    # 头部统计 (Z > 0.5)
    if head_diffs:
        head_diffs.sort(key=lambda x: x[3], reverse=True)
        hd = [x[3] for x in head_diffs]
        print(f"\n头部差异 (Z>0.5, {len(head_diffs)} 顶点):")
        print(f"  max:  {hd[0]:.6f}")
        print(f"  mean: {sum(hd)/len(hd):.6f}")
        print(f"  > 0.001: {sum(1 for d in hd if d > 0.001)} 顶点")
        print(f"  > 0.01:  {sum(1 for d in hd if d > 0.01)} 顶点")
        print(f"  > 0.1:   {sum(1 for d in hd if d > 0.1)} 顶点")

        # 差异最大的5个头部顶点
        print(f"\n差异最大的5个头部顶点:")
        for idx, op, ep, dist in head_diffs[:5]:
            print(f"  #{idx}: src=({op[0]:.4f},{op[1]:.4f},{op[2]:.4f}) "
                  f"→ dst=({ep[0]:.4f},{ep[1]:.4f},{ep[2]:.4f}) dist={dist:.4f}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(_("diff.usage"))
        sys.exit(1)
    compare(sys.argv[1], sys.argv[2])
