"""
MDL text format parser and writer for GEM2 skeleton data.
Handles recursive descent parsing of bone blocks,
Matrix34/Position/Orientation metadata, and nested hierarchy.
"""
import os
import json
import bpy
from mathutils import Matrix, Vector

from .core import (
    row34_to_blender, ori_to_blender, blender_to_row34, blender_to_row33,
    find_matching_brace, TAB,
)


def parse_mdl(content):
    """递归解析 MDL 文本，返回根骨骼节点列表和 mesh_parent"""
    import re

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
            ce = find_matching_brace(remaining, cs)
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
                r'(?:Orientation|orientation)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+'
                r'([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+'
                r'([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)',
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
        return [], None
    skel_end = find_matching_brace(content, skel_start)
    if skel_end == -1:
        return [], None
    skeleton_text = content[skel_start+1:skel_end]

    root_bones = []
    remaining = skeleton_text
    while True:
        bs = remaining.find('{bone')
        if bs == -1: break
        be = find_matching_brace(remaining, bs)
        if be == -1: break
        bone = _parse_node(remaining[bs:be+1])
        if bone: root_bones.append(bone)
        remaining = remaining[be+1:]

    # 找 mesh_parent
    mesh_parent = None
    def _find_mesh_parent(node):
        nonlocal mesh_parent
        if node.get('has_volumeview'):
            mesh_parent = node['name']
        for child in node.get('children', []):
            _find_mesh_parent(child)
    for bone in root_bones:
        _find_mesh_parent(bone)

    return root_bones, mesh_parent


def flatten_bones(root_bones):
    """将根骨骼列表扁平化为 {name: info} 字典"""
    flat = {}
    def _flatten(node, parent_name=None):
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
        for child in node.get('children', []):
            _flatten(child, node['name'])
    for bone in root_bones:
        _flatten(bone, None)
    return flat


def build_armature(mesh_name, root_bones, mesh_parent_name, mdl_path):
    """从根骨骼列表创建 Blender 骨架，返回 arm_obj"""
    flat = flatten_bones(root_bones)

    arm_data = bpy.data.armatures.new(mesh_name + "_Arm")
    arm_obj = bpy.data.objects.new(mesh_name + "_Armature", arm_data)
    bpy.context.collection.objects.link(arm_obj)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')
    edit_bones = arm_obj.data.edit_bones

    # 计算局部矩阵
    local_mats = {}
    for name, info in flat.items():
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

    # 计算世界矩阵；坏 MDL 必须明确报循环，不能递归到 Python 栈溢出。
    world_mats = {}
    visiting = set()

    def _compute_world(name):
        if name in world_mats:
            return world_mats[name]
        if name in visiting:
            raise ValueError('MDL bone parent cycle detected at %r' % name)
        visiting.add(name)
        try:
            info = flat[name]
            parent = info.get('parent')
            local = local_mats[name]
            if parent == name:
                raise ValueError('MDL bone %r cannot parent itself' % name)
            if parent and parent in flat:
                world = _compute_world(parent) @ local
            else:
                world = local.copy()
            world_mats[name] = world
            return world
        finally:
            visiting.discard(name)

    for name in flat:
        _compute_world(name)

    # PASS 1: 创建骨骼，设置世界矩阵
    created = {}
    for name in flat:
        eb = edit_bones.new(name)
        created[name] = eb
        eb.matrix = world_mats[name]

    # PASS 2: 设置父子关系
    for name, info in flat.items():
        parent_name = info.get('parent')
        if parent_name and parent_name in created:
            eb = created[name]
            eb.parent = created[parent_name]
            eb.use_connect = False

    # 修正尾部
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

    # 存储原始数据
    mat_data = {name: [[float(v) for v in row] for row in mat]
                for name, mat in world_mats.items()}
    parent_data = {name: (info.get('parent') or '') for name, info in flat.items()}
    meta_data = {}
    for name, info in flat.items():
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


# ═══════════════════════════════════════════════════════════════
#  MDL EXPORT
# ═══════════════════════════════════════════════════════════════

def write_mdl_file(filepath, arm_obj, mesh_obj, ply_name):
    """写出 MDL 骨架文件。返回 skin_world 矩阵"""
    skin_world = Matrix.Identity(4)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("{Skeleton\n")

        if not arm_obj:
            f.write("}\n")
            return skin_world

        stored_mats = arm_obj.get('gem2_world_mats')
        stored_parents = arm_obj.get('gem2_parents')

        if stored_mats and stored_parents:
            skin_world = _write_mdl_stored(f, arm_obj, mesh_obj, ply_name)
        else:
            skin_world = _write_mdl_fallback(f, arm_obj, mesh_obj, ply_name)

        f.write("}\n")

    return skin_world


def _write_mdl_stored(f, arm_obj, mesh_obj, ply_name):
    """使用存储的原始矩阵写出 MDL"""
    skin_world = Matrix.Identity(4)

    world_mats = {n: Matrix(m) for n, m in json.loads(arm_obj['gem2_world_mats']).items()}
    parents = json.loads(arm_obj['gem2_parents'])
    meta = json.loads(arm_obj.get('gem2_meta', '{}')) if arm_obj.get('gem2_meta') else {}
    is_skin = arm_obj.get('gem2_mesh_parent', '')

    children = {n: [] for n in world_mats}
    roots = []
    for name in world_mats:
        p = parents.get(name)
        if p and p in children:
            children[p].append(name)
        else:
            roots.append(name)

    def _write_bone(name, level):
        nonlocal skin_world
        world = world_mats[name]
        p_name = parents.get(name) if parents.get(name) in world_mats else None
        if p_name:
            local_mat = world_mats[p_name].inverted() @ world
        else:
            local_mat = world.copy()
        local_t = local_mat.transposed()

        bone_type = 'bone revolute ' if meta.get(name, {}).get('r') else 'bone '
        f.write(TAB * level + '{' + bone_type + '"' + name + '"\n')

        m = meta.get(name, {})
        if m.get('l'):
            f.write(TAB * (level+1) + '{limits %g %g}\n' % tuple(m['l']))
        if m.get('s') is not None:
            f.write(TAB * (level+1) + '{speed %g}\n' % m['s'])
        if m.get('p'):
            f.write(TAB * (level+1) + '{parameters "' + m['p'] + '"}\n')

        has_rot = any(abs(local_t[i][j]) > 1e-6
                      for i in range(3) for j in range(3)
                      if i != j or abs(local_t[i][j] - 1) > 1e-6)
        has_trans = any(abs(local_t[3][j]) > 1e-6 for j in range(3))

        if has_rot and has_trans:
            f.write(TAB * (level+1) + "{Matrix34\n")
            for i in range(4):
                f.write(TAB * (level+2) + "%.7g\t%.7g\t%.7g\n" % tuple(local_t[i][:3]))
            f.write(TAB * (level+1) + "}\n")
        elif has_trans:
            f.write(TAB * (level+1) + "{Position %.7g\t%.7g\t%.7g}\n" % tuple(local_t[3][:3]))
        elif has_rot:
            f.write(TAB * (level+1) + "{Orientation\n")
            for i in range(3):
                f.write(TAB * (level+2) + "%.7g\t%.7g\t%.7g\n" % tuple(local_t[i][:3]))
            f.write(TAB * (level+1) + "}\n")

        if name == is_skin and mesh_obj:
            skin_world = world.copy()
            f.write(TAB * (level+1) + '{VolumeView "' + ply_name + '"}\n')

        for child_name in children.get(name, []):
            _write_bone(child_name, level + 1)

        f.write(TAB * level + "}\n")

    for r in roots:
        _write_bone(r, 1)

    if mesh_obj and is_skin not in world_mats:
        f.write(TAB + '{bone "skin"\n')
        f.write(TAB * 2 + "{Position 0\t0\t0}\n")
        f.write(TAB * 2 + '{VolumeView "' + ply_name + '"}\n')
        f.write(TAB + "}\n")

    return skin_world


def _write_mdl_fallback(f, arm_obj, mesh_obj, ply_name):
    """从编辑骨骼回退写出 MDL"""
    skin_world = Matrix.Identity(4)
    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bones = arm_obj.data.edit_bones

    root_bones = [b for b in edit_bones if b.parent is None]
    skin_written = False

    def _write_recursive(eb, level):
        if eb.parent:
            local_mat = eb.parent.matrix.inverted() @ eb.matrix
        else:
            local_mat = eb.matrix.copy()
        local_t = local_mat.transposed()

        f.write(TAB * level + '{bone "' + eb.name + '"\n')

        has_rot = any(abs(local_t[i][j]) > 1e-6
                      for i in range(3) for j in range(3)
                      if i != j or abs(local_t[i][j] - 1) > 1e-6)
        has_trans = any(abs(local_t[3][j]) > 1e-6 for j in range(3))

        if has_rot and has_trans:
            f.write(TAB * (level+1) + "{Matrix34\n")
            for i in range(4):
                f.write(TAB * (level+2) + "%.7g\t%.7g\t%.7g\n" % tuple(local_t[i][:3]))
            f.write(TAB * (level+1) + "}\n")
        elif has_trans:
            f.write(TAB * (level+1) + "{Position %.7g\t%.7g\t%.7g}\n" % tuple(local_t[3][:3]))
        elif has_rot:
            f.write(TAB * (level+1) + "{Orientation\n")
            for i in range(3):
                f.write(TAB * (level+2) + "%.7g\t%.7g\t%.7g\n" % tuple(local_t[i][:3]))
            f.write(TAB * (level+1) + "}\n")

        nonlocal skin_written, skin_world
        if eb.name == "skin" and mesh_obj:
            skin_written = True
            skin_world = eb.matrix.copy()
            f.write(TAB * (level+1) + '{VolumeView "' + ply_name + '"}\n')

        for child in eb.children:
            _write_recursive(child, level + 1)

        f.write(TAB * level + "}\n")

    for root_bone in root_bones:
        _write_recursive(root_bone, 1)

    if mesh_obj and not skin_written:
        f.write(TAB + '{bone "skin"\n')
        f.write(TAB * 2 + "{Position 0\t0\t0}\n")
        f.write(TAB * 2 + '{VolumeView "' + ply_name + '"}\n')
        f.write(TAB + "}\n")

    bpy.ops.object.mode_set(mode="OBJECT")
    return skin_world
