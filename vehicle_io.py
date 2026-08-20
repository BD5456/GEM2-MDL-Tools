# -*- coding: utf-8 -*-
"""
GEM2 载具 (vehicle) 按文件夹导入 + 自动拆分导出
==================================================
载具 (如 m61a5) = 1 个 .mdl 骨架 (106 骨) + 41 个 .ply (每骨一个 VolumeView) +
10 个 .vol (EVLM 碰撞体) + .mtl/.def/动画。

导入:
  - 解析 .mdl 骨架 → 建 1 个 Blender 骨架 (gem2_world_mats 等属性)
  - 对每个 {bone "X" {VolumeView "Y.ply"}} → 导入 Y.ply, 顶点用 X 骨世界矩阵变换
    (body 等世界坐标部件挂 basis 骨矩阵≈identity, 轮子等局部部件按骨摆位)
  - 无骨引用的 .ply 直接导入 (保持文件坐标)
  - MDL 引用的 .vol → 可见碰撞线框；未引用 sidecar .vol → 保留但默认隐藏
  - MDL 内嵌 Box/Cylinder → 只读碰撞辅助网格

导出 (自动拆分):
  - 场景中带 gem2_vehicle_bone 属性的对象 → 每个导出独立 .ply
    (顶点 = 骨世界矩阵⁻¹ · 世界坐标, 还原为骨局部空间, 与游戏格式一致)
  - 带 gem2_vehicle_vol 标记的对象 → 安全回写原 EVLM 顶点
  - Box/Cylinder 辅助网格必须保持不变，原始定义由 MDL 原文保留
  - 重写 .mdl: 保留原骨架/动画/参数, 仅更新各骨 VolumeView 文件名
  - 复制并校验 .anm 动画，复制 .mtl + 贴图
"""
import bpy
import os
import re
import glob
import shutil
import json
from mathutils import Matrix, Vector

from .i18n import _
from .ply_io import import_ply, import_vol
from .ply_io import _parse_bones_flat, _precompute_bone_world_mats
from .mdl_io import build_armature
from .core import find_matching_brace, row34_to_blender, ori_to_blender

# 载具对象标记 (导入时写入)
BONE_KEY = 'gem2_vehicle_bone'    # 该对象挂的骨名 (导出拆分依据)
VOL_KEY = 'gem2_vehicle_vol'      # 外部 EVLM .vol 多面体
PRIMITIVE_VOL_KEY = 'gem2_vehicle_primitive_volume'  # MDL 内嵌 Box/Cylinder
FOLDER_KEY = 'gem2_vehicle_dir'   # 来源文件夹
_FLOAT_RE = r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?'


def _new_vehicle_collection(host_collection, name, hidden=False):
    """Create a named child collection for vehicle helper geometry."""
    collection = bpy.data.collections.new(name)
    host_collection.children.link(collection)
    collection.hide_render = True
    collection.hide_viewport = bool(hidden)
    return collection


def _move_to_collection(obj, collection):
    if obj.name not in collection.objects:
        collection.objects.link(obj)
    for current in list(obj.users_collection):
        if current != collection:
            current.objects.unlink(obj)


def _style_vol_object(obj, referenced):
    """Make active collision visible; keep unreferenced sidecars out of view."""
    obj.display_type = 'WIRE'
    obj.show_in_front = bool(referenced)
    obj.hide_render = True
    obj.color = ((1.0, 0.18, 0.02, 1.0) if referenced
                 else (0.35, 0.35, 0.35, 1.0))
    obj['gem2_vehicle_volume_referenced'] = bool(referenced)


def _find_mdl(dirpath):
    mdls = glob.glob(os.path.join(dirpath, '*.mdl'))
    return mdls[0] if mdls else None


def _find_volumeview_map(content):
    """返回 ``{bone_name: [ply_name, ...]}``，只认骨块的直属 VolumeView。

    不能用“下一个 bone 之前的文本窗口”：父骨块内嵌子骨，且引擎还有
    ``bone prizmatic``，窗口法会把子骨 VolumeView 归给父骨。这里对每个骨块
    做括号配平，并仅接受相对骨块深度 1 的 ``{VolumeView ...}``。
    """
    out = {}
    bone_re = re.compile(
        r'\{\s*bone\s+(?:(?:revolute|prizmatic|prismatic)\s+)?"([^"]+)"',
        re.IGNORECASE)
    vv_re = re.compile(r'\{\s*VolumeView\s+"([^"]+)"', re.IGNORECASE)
    for match in bone_re.finditer(content):
        start = match.start()
        end = find_matching_brace(content, start)
        if end < 0:
            raise ValueError('unclosed MDL bone block: %s' % match.group(1))
        block = content[start:end + 1]
        depth = 0
        refs = []
        i = 0
        while i < len(block):
            ch = block[i]
            if ch == '{':
                direct = depth == 1
                if direct:
                    vv = vv_re.match(block, i)
                    if vv:
                        refs.append(vv.group(1))
                depth += 1
            elif ch == '}':
                depth -= 1
            i += 1
        if refs:
            out[match.group(1)] = refs
    return out


def _node_local_matrix(block):
    """Parse a flat MDL node transform into Blender matrix convention."""
    matrix_match = re.search(r'\{Matrix34\s+([^{}]+)\}', block,
                             re.S | re.I)
    if matrix_match:
        values = [float(value) for value in re.findall(
            _FLOAT_RE, matrix_match.group(1))]
        if len(values) >= 12:
            rows = [values[index:index + 3] for index in range(0, 12, 3)]
            return row34_to_blender(rows)

    matrix = Matrix.Identity(4)
    orientation_match = re.search(r'\{Orientation\s+([^{}]+)\}',
                                  block, re.S | re.I)
    if orientation_match:
        values = [float(value) for value in re.findall(
            _FLOAT_RE, orientation_match.group(1))]
        if len(values) >= 9:
            rows = [values[index:index + 3] for index in range(0, 9, 3)]
            matrix = ori_to_blender(rows)
    position_match = re.search(r'\{Position\s+([^{}]+)\}',
                               block, re.S | re.I)
    if position_match:
        values = [float(value) for value in re.findall(
            _FLOAT_RE, position_match.group(1))]
        if len(values) >= 3:
            matrix.translation = Vector(values[:3])
    return matrix


def _find_volume_map(mdl_text):
    """Return collision Polyhedron references from top-level Volume blocks."""
    result = {}
    pattern = re.compile(r'\{\s*Volume\s+"([^"]+)"', re.I)
    for match in pattern.finditer(mdl_text):
        start = match.start()
        end = find_matching_brace(mdl_text, start)
        if end < 0:
            raise ValueError('unclosed MDL Volume block: %s' % match.group(1))
        block = mdl_text[start:end + 1]
        poly_match = re.search(
            r'\{\s*Polyhedron\s+"([^"]+\.vol)"\s*\}', block, re.I)
        bone_match = re.search(r'\{\s*Bone\s+"([^"]+)"\s*\}',
                               block, re.I)
        if not poly_match or not bone_match:
            continue
        filename = os.path.basename(poly_match.group(1))
        result.setdefault(filename.lower(), []).append({
            'volume_name': match.group(1),
            'filename': filename,
            'bone_name': bone_match.group(1),
            'local_matrix': _node_local_matrix(block),
        })
    return result


def _find_primitive_volumes(mdl_text):
    """Return top-level MDL Box/Cylinder collision-volume descriptions."""
    result = []
    pattern = re.compile(r'\{\s*Volume\s+"([^"]+)"', re.I)
    for match in pattern.finditer(mdl_text):
        start = match.start()
        end = find_matching_brace(mdl_text, start)
        if end < 0:
            raise ValueError('unclosed MDL Volume block: %s' % match.group(1))
        block = mdl_text[start:end + 1]
        bone_match = re.search(r'\{\s*Bone\s+"([^"]+)"\s*\}', block, re.I)
        if not bone_match:
            continue
        shape = None
        values = None
        for keyword, count in (('Box', 3), ('Cylinder', 2)):
            shape_match = re.search(
                r'\{\s*%s\s+([^{}]+)\}' % keyword, block, re.I)
            if not shape_match:
                continue
            parsed = [float(value) for value in re.findall(
                _FLOAT_RE, shape_match.group(1))]
            if len(parsed) >= count:
                shape = keyword.lower()
                values = parsed[:count]
                break
        if shape:
            result.append({
                'volume_name': match.group(1),
                'shape': shape,
                'values': values,
                'bone_name': bone_match.group(1),
                'local_matrix': _node_local_matrix(block),
            })
    return result


def _create_primitive_volume_mesh(name, shape, values, cylinder_segments=24):
    """Create a centered GEM2 Box/Cylinder helper mesh in local space."""
    if shape == 'box':
        hx, hy, hz = (float(value) * 0.5 for value in values)
        verts = [
            (-hx, -hy, -hz), (hx, -hy, -hz),
            (hx, hy, -hz), (-hx, hy, -hz),
            (-hx, -hy, hz), (hx, -hy, hz),
            (hx, hy, hz), (-hx, hy, hz),
        ]
        faces = [
            (0, 3, 2, 1), (4, 5, 6, 7),
            (0, 1, 5, 4), (1, 2, 6, 5),
            (2, 3, 7, 6), (3, 0, 4, 7),
        ]
    elif shape == 'cylinder':
        import math
        radius, length = (float(value) for value in values)
        half = length * 0.5
        verts = []
        for z in (-half, half):
            for index in range(cylinder_segments):
                angle = 2.0 * math.pi * index / cylinder_segments
                verts.append((radius * math.cos(angle),
                              radius * math.sin(angle), z))
        faces = []
        for index in range(cylinder_segments):
            nxt = (index + 1) % cylinder_segments
            faces.append((index, nxt, cylinder_segments + nxt,
                          cylinder_segments + index))
        faces.append(tuple(reversed(range(cylinder_segments))))
        faces.append(tuple(range(cylinder_segments, cylinder_segments * 2)))
    else:
        raise ValueError('unsupported vehicle volume primitive: %s' % shape)

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    return mesh


def import_vehicle_folder(dirpath):
    """按 MDL 骨局部契约导入完整载具文件夹。

    每个直属 VolumeView 都生成一个对象实例；同一 PLY 被多个骨引用时共享同一
    Mesh datablock。绑定部件的对象矩阵恒等于所属骨世界矩阵，不再猜测质心。
    """
    dirpath = os.path.abspath(dirpath)
    if not os.path.isdir(dirpath):
        raise RuntimeError('目录不存在: %s' % dirpath)

    host_collection = bpy.context.collection
    veh_root = bpy.data.objects.new(os.path.basename(dirpath), None)
    host_collection.objects.link(veh_root)
    veh_root[FOLDER_KEY] = dirpath
    veh_root['gem2_vehicle_root'] = True
    vol_collection = _new_vehicle_collection(
        host_collection, veh_root.name + '__COLLISION_VOL')
    unused_vol_collection = _new_vehicle_collection(
        host_collection, veh_root.name + '__UNREFERENCED_VOL', hidden=True)
    veh_root['gem2_vehicle_vol_collection'] = vol_collection.name
    veh_root['gem2_vehicle_unused_vol_collection'] = unused_vol_collection.name

    mdl_path = _find_mdl(dirpath)
    arm_obj = None
    vv_map = {}
    vol_map = {}
    primitive_volumes = []
    world_mats = {}
    if mdl_path:
        with open(mdl_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        mdl_bones, mesh_parent = _parse_bones_flat(content)
        vv_map = _find_volumeview_map(content)
        vol_map = _find_volume_map(content)
        primitive_volumes = _find_primitive_volumes(content)
        if mdl_bones:
            arm_obj = build_armature(
                os.path.splitext(os.path.basename(mdl_path))[0],
                _root_list_from_flat(mdl_bones, mesh_parent), mesh_parent,
                mdl_path)
            arm_obj.name = os.path.splitext(os.path.basename(mdl_path))[0] + '_Armature'
            arm_obj[FOLDER_KEY] = dirpath
            arm_obj.parent = veh_root
            stored = arm_obj.get('gem2_world_mats')
            if stored:
                world_mats = {
                    name: Matrix(rows)
                    for name, rows in json.loads(stored).items()
                }
        veh_root['gem2_vehicle_mdl_file'] = os.path.basename(mdl_path)
    veh_root['gem2_vehicle_armature'] = arm_obj.name if arm_obj else ''
    veh_root['gem2_vehicle_vv_map'] = json.dumps(vv_map)

    refs_by_file = {}
    for bone_name, refs in vv_map.items():
        for ref in refs:
            key = os.path.basename(ref.replace('\\', '/')).casefold()
            refs_by_file.setdefault(key, []).append((bone_name, ref))

    imported = []
    for ply in sorted(glob.glob(os.path.join(dirpath, '*.ply'))):
        filename = os.path.basename(ply)
        refs = refs_by_file.get(filename.casefold(), [])
        try:
            obj, _arm, _helper = import_ply(
                ply, skip_mdl_transform=True, skip_armature=True,
                create_helpers=False)
        except Exception as exc:
            print('[vehicle] import fail %s: %s' % (filename, exc))
            continue

        obj[FOLDER_KEY] = dirpath
        obj['gem2_vehicle_ply_file'] = filename
        obj['gem2_vehicle_instance'] = 0
        if refs:
            bone_name, _ref = refs[0]
            if bone_name not in world_mats:
                raise RuntimeError('VolumeView %s references unknown bone %s'
                                   % (filename, bone_name))
            obj[BONE_KEY] = bone_name
            obj.matrix_world = world_mats[bone_name]
            obj.name = '%s__%s' % (os.path.splitext(filename)[0], bone_name)
        else:
            obj[BONE_KEY] = ''
            obj.matrix_world = Matrix.Identity(4)
        obj['gem2_vehicle_home_matrix'] = json.dumps(
            [[float(value) for value in row] for row in obj.matrix_world])
        obj.parent = veh_root
        imported.append(obj.name)

        # 同文件多骨引用必须是链接实例：局部顶点完全相同，仅骨世界矩阵不同。
        for instance_index, (bone_name, _ref) in enumerate(refs[1:], 1):
            if bone_name not in world_mats:
                raise RuntimeError('VolumeView %s references unknown bone %s'
                                   % (filename, bone_name))
            inst = bpy.data.objects.new(
                '%s__%s' % (os.path.splitext(filename)[0], bone_name), obj.data)
            bpy.context.collection.objects.link(inst)
            for key in obj.keys():
                inst[key] = obj[key]
            inst[BONE_KEY] = bone_name
            inst['gem2_vehicle_instance'] = instance_index
            inst.matrix_world = world_mats[bone_name]
            inst['gem2_vehicle_home_matrix'] = json.dumps(
                [[float(value) for value in row] for row in inst.matrix_world])
            inst.parent = veh_root
            imported.append(inst.name)
        print('[vehicle] imported %s -> %s' % (
            filename, ', '.join(b for b, _r in refs) if refs else '(unbound)'))

    vols = []
    active_vols = 0
    unused_vols = 0
    for vol in sorted(glob.glob(os.path.join(dirpath, '*.vol'))):
        filename = os.path.basename(vol)
        refs = vol_map.get(filename.casefold(), [])
        try:
            obj = import_vol(vol)
            obj['gem2_vol_topology_hash'] = _mesh_topology_hash(obj.data)
            objects = [obj]
            for instance_index in range(1, len(refs)):
                inst = bpy.data.objects.new(
                    '%s__vol%d' % (os.path.splitext(filename)[0], instance_index),
                    obj.data)
                bpy.context.collection.objects.link(inst)
                for key in obj.keys():
                    inst[key] = obj[key]
                objects.append(inst)

            # No MDL reference means a sidecar/alternative collision shape. Keep
            # it in file-local space so it still round-trips byte-identically.
            referenced = bool(refs)
            assignments = refs or [{
                'volume_name': os.path.splitext(filename)[0],
                'filename': filename,
                'bone_name': '',
                'local_matrix': Matrix.Identity(4),
            }]
            for instance_index, (vol_obj, ref) in enumerate(
                    zip(objects, assignments)):
                bone_name = ref['bone_name']
                if bone_name:
                    if bone_name not in world_mats:
                        raise RuntimeError(
                            'Volume %s references unknown bone %s'
                            % (ref['volume_name'], bone_name))
                    world_matrix = world_mats[bone_name] @ ref['local_matrix']
                else:
                    world_matrix = ref['local_matrix'].copy()
                if referenced:
                    vol_obj.name = 'VOL__%s' % ref['volume_name']
                else:
                    vol_obj.name = 'VOL_UNUSED__%s' % os.path.splitext(filename)[0]
                vol_obj[VOL_KEY] = True
                vol_obj[FOLDER_KEY] = dirpath
                vol_obj['gem2_vol_file'] = filename
                vol_obj['gem2_vehicle_volume_name'] = ref['volume_name']
                vol_obj[BONE_KEY] = bone_name
                vol_obj['gem2_vehicle_instance'] = instance_index
                vol_obj['gem2_vehicle_volume_local_matrix'] = json.dumps(
                    [[float(value) for value in row]
                     for row in ref['local_matrix']])
                vol_obj.matrix_world = world_matrix
                vol_obj['gem2_vehicle_home_matrix'] = json.dumps(
                    [[float(value) for value in row] for row in world_matrix])
                vol_obj.parent = veh_root
                _style_vol_object(vol_obj, referenced)
                _move_to_collection(
                    vol_obj, vol_collection if referenced else unused_vol_collection)
                if referenced:
                    active_vols += 1
                else:
                    unused_vols += 1
                vols.append(vol_obj.name)
        except Exception as exc:
            print('[vehicle] vol fail %s: %s' % (filename, exc))

    primitive_names = []
    for ref in primitive_volumes:
        try:
            bone_name = ref['bone_name']
            if bone_name not in world_mats:
                raise RuntimeError(
                    'Primitive Volume %s references unknown bone %s'
                    % (ref['volume_name'], bone_name))
            shape = ref['shape']
            obj_name = 'VOL_%s__%s' % (shape.upper(), ref['volume_name'])
            mesh = _create_primitive_volume_mesh(
                obj_name, shape, ref['values'])
            obj = bpy.data.objects.new(obj_name, mesh)
            host_collection.objects.link(obj)
            world_matrix = world_mats[bone_name] @ ref['local_matrix']
            obj[PRIMITIVE_VOL_KEY] = True
            obj[FOLDER_KEY] = dirpath
            obj['gem2_vehicle_volume_name'] = ref['volume_name']
            obj['gem2_vehicle_primitive_type'] = shape
            obj['gem2_vehicle_primitive_values'] = json.dumps(ref['values'])
            obj[BONE_KEY] = bone_name
            obj['gem2_vehicle_volume_local_matrix'] = json.dumps(
                [[float(value) for value in row]
                 for row in ref['local_matrix']])
            obj.matrix_world = world_matrix
            obj['gem2_vehicle_home_matrix'] = json.dumps(
                [[float(value) for value in row] for row in world_matrix])
            obj.parent = veh_root
            obj['gem2_vehicle_primitive_geometry_hash'] = \
                _mesh_geometry_hash(mesh)
            _style_vol_object(obj, True)
            _move_to_collection(obj, vol_collection)
            primitive_names.append(obj.name)
        except Exception as exc:
            print('[vehicle] primitive volume fail %s: %s'
                  % (ref.get('volume_name', '?'), exc))
    veh_root['gem2_vehicle_primitive_volume_count'] = len(primitive_names)

    mtls = glob.glob(os.path.join(dirpath, '*.mtl'))
    bpy.context.view_layer.objects.active = veh_root
    veh_root.select_set(True)
    print('[vehicle] 导入完成: %d PLY 实例, %d vol '
          '(%d MDL引用可见, %d 未引用已隐藏), %d MDL原语, %d mtl, '
          '骨架 %s (%d bones, %d VolumeView)'
          % (len(imported), len(vols), active_vols, unused_vols,
             len(primitive_names), len(mtls),
             arm_obj.name if arm_obj else '无',
             len(arm_obj.data.bones) if arm_obj else 0,
             sum(len(refs) for refs in vv_map.values())))
    return veh_root


def _root_list_from_flat(mdl_bones, mesh_parent):
    """把平铺骨字典还原成 build_armature 需要的根骨列表。"""
    # build_armature 需要 {name, matrix/position/orientation, children, has_volumeview}
    # 从 flat 重建树
    children_of = {}
    for name, info in mdl_bones.items():
        p = info.get('parent')
        if p:
            children_of.setdefault(p, []).append(name)
    roots = [n for n in mdl_bones if not mdl_bones[n].get('parent')]

    def to_node(name):
        info = mdl_bones[name]
        node = {
            'name': name,
            'matrix': info.get('matrix'),
            'position': info.get('position'),
            'orientation': info.get('orientation'),
            'children': [to_node(c) for c in children_of.get(name, [])],
            'has_volumeview': False,
            'is_revolute': info.get('is_revolute', False),
            'is_prizmatic': info.get('is_prizmatic', False),
            'params': info.get('params'),
            'limits': info.get('limits'),
            'speed': info.get('speed'),
        }
        return node

    return [to_node(r) for r in roots]


# ═══════════════════════════════════════════════════════════════
#  导出 (自动拆分)
# ═══════════════════════════════════════════════════════════════

_TEXTURE_DIRECTIVE_RE = re.compile(
    r'(\{\s*(diffuse|bump|specular)\s+")([^"]+)("\s*\})',
    re.IGNORECASE)
_TEXTURE_FORMAT_EXTENSIONS = {
    'BMP': '.bmp',
    'DDS': '.dds',
    'JPEG': '.jpg',
    'JPEG2000': '.jp2',
    'PNG': '.png',
    'TARGA': '.tga',
    'TARGA_RAW': '.tga',
    'TIFF': '.tif',
}


def _upstream_image(socket, visited=None):
    """Return the first image feeding a shader input socket."""
    if socket is None or not getattr(socket, 'is_linked', False):
        return None
    visited = visited or set()
    for link in socket.links:
        node = link.from_node
        key = node.as_pointer()
        if key in visited:
            continue
        visited.add(key)
        if node.type == 'TEX_IMAGE' and node.image:
            return node.image
        for input_socket in node.inputs:
            image = _upstream_image(input_socket, visited)
            if image is not None:
                return image
    return None


def _material_texture_images(material):
    """Return ``(role_images, all_images)`` for one Blender material."""
    if not material or not material.use_nodes or not material.node_tree:
        return {}, []

    role_images = {}
    principled = next(
        (node for node in material.node_tree.nodes
         if node.type == 'BSDF_PRINCIPLED'), None)
    if principled is not None:
        role_inputs = {
            'diffuse': ('Base Color',),
            'bump': ('Normal',),
            'specular': ('Specular IOR Level', 'Specular'),
        }
        for role, input_names in role_inputs.items():
            for input_name in input_names:
                socket = principled.inputs.get(input_name)
                image = _upstream_image(socket)
                if image is not None:
                    role_images[role] = image
                    break

    all_images = []
    seen = set()
    for node in material.node_tree.nodes:
        if node.type != 'TEX_IMAGE' or not node.image:
            continue
        key = node.image.as_pointer()
        if key not in seen:
            seen.add(key)
            all_images.append(node.image)
    return role_images, all_images


def _resolved_image_path(image):
    raw = getattr(image, 'filepath_raw', '') or image.filepath
    if not raw:
        return ''
    try:
        resolved = bpy.path.abspath(raw, library=image.library)
    except TypeError:
        resolved = bpy.path.abspath(raw)
    return os.path.abspath(resolved) if resolved else ''


def _image_export_filename(image, source_path=''):
    """Choose a stable filename for external and packed Blender images."""
    raw = getattr(image, 'filepath_raw', '') or image.filepath
    filename = os.path.basename(source_path)
    if not filename and raw:
        filename = os.path.basename(raw.replace('\\', '/'))
    if not filename:
        filename = os.path.basename(image.name.replace('\\', '/'))

    # Blender may suffix duplicate datablocks after the real extension.
    match = re.match(r'^(.*\.(?:dds|tga|png|bmp|jpe?g|tiff?|ctm|ebm|tex))\.\d{3}$',
                     filename, re.IGNORECASE)
    if match:
        filename = match.group(1)
    if not os.path.splitext(filename)[1]:
        extension = _TEXTURE_FORMAT_EXTENSIONS.get(
            str(getattr(image, 'file_format', '') or '').upper(), '.png')
        filename += extension
    return filename


def _sha256_file(filepath):
    import hashlib
    digest = hashlib.sha256()
    with open(filepath, 'rb') as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _mdl_animation_references(mdl_path):
    """Return sequence names and their conventional or explicit ANM files."""
    if not mdl_path or not os.path.isfile(mdl_path):
        return []
    with open(mdl_path, 'r', encoding='utf-8', errors='ignore') as handle:
        content = handle.read()

    sequence_re = re.compile(r'\{\s*sequence\s+"([^"]+)"', re.IGNORECASE)
    file_re = re.compile(r'\{\s*file\s+"([^"]+\.anm)"\s*\}',
                         re.IGNORECASE)
    references = []
    for match in sequence_re.finditer(content):
        end = find_matching_brace(content, match.start())
        if end < 0:
            continue
        block = content[match.start():end + 1]
        explicit = file_re.search(block)
        filename = explicit.group(1) if explicit else match.group(1) + '.anm'
        filename = filename.replace('/', os.sep).replace('\\', os.sep)
        references.append({
            'sequence': match.group(1),
            'file': filename.lstrip(os.sep),
        })
    return references


def _export_vehicle_animations(output_dir, src_dir, mdl_path):
    """Copy all ANM sidecars and audit every sequence declared by the MDL."""
    source_files = []
    for walk_root, _dirs, files in os.walk(src_dir):
        for filename in files:
            if filename.lower().endswith('.anm'):
                source = os.path.join(walk_root, filename)
                relative = os.path.relpath(source, src_dir)
                source_files.append((source, relative))
    source_files.sort(key=lambda item: item[1].casefold())

    copied = 0
    unchanged = 0
    available = set()
    for source, relative in source_files:
        available.add(relative.replace('\\', '/').casefold())
        destination = os.path.join(output_dir, relative)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if (os.path.isfile(destination) and
                _sha256_file(destination) == _sha256_file(source)):
            unchanged += 1
        else:
            shutil.copy2(source, destination)
            copied += 1

    references = _mdl_animation_references(mdl_path)
    missing = []
    for reference in references:
        key = reference['file'].replace('\\', '/').lstrip('./').casefold()
        if key not in available:
            missing.append('%s -> %s' % (
                reference['sequence'], reference['file']))
    missing = list(dict.fromkeys(missing))
    fire_sequences = sorted({
        reference['sequence'] for reference in references
        if reference['sequence'].casefold().startswith('fire')
    }, key=str.casefold)

    print('[vehicle] animations: %d copied, %d unchanged, %d MDL refs, '
          '%d missing'
          % (copied, unchanged, len(references), len(missing)))
    if fire_sequences:
        print('[vehicle] fire sequences: %s' % ', '.join(fire_sequences))
    for entry in missing:
        print('[vehicle] animation missing: %s' % entry)
    return {
        'copied': copied,
        'unchanged': unchanged,
        'references': references,
        'fire_sequences': fire_sequences,
        'missing': missing,
    }


def _export_vehicle_textures(output_dir, src_dir, parts):
    """Stage vehicle textures beside the exported MTL files.

    Source MTL paths such as ``$/model/vehicle/body`` are localized to the
    copied texture basename. This makes a vehicle export self-contained and
    also respects images reassigned in Blender's material graph.
    """
    import hashlib

    materials = []
    material_seen = set()
    material_by_source = {}
    material_by_filename = {}
    for obj in parts:
        for slot in obj.material_slots:
            material = slot.material
            if material is None:
                continue
            key = material.as_pointer()
            if key not in material_seen:
                material_seen.add(key)
                materials.append(material)
            mtl_source = material.get('gem2_mtl_source', '')
            if mtl_source:
                normalized = os.path.normcase(os.path.abspath(mtl_source))
                material_by_source.setdefault(normalized, material)
                material_by_filename.setdefault(
                    os.path.basename(mtl_source).casefold(), material)
            else:
                material_by_filename.setdefault(
                    (material.name + '.mtl').casefold(), material)

    planned = {}
    copied = 0
    unchanged = 0
    variants = 0
    localized_refs = 0
    missing = []
    image_stems = {}

    def _stage_payload(filename, digest, writer, source_label, is_variant=False):
        nonlocal copied, unchanged, variants
        filename = os.path.basename(filename)
        if not filename:
            raise RuntimeError('Texture has no usable filename: %s'
                               % source_label)
        destination = os.path.join(output_dir, filename)
        key = filename.casefold()
        previous = planned.get(key)
        if previous is not None:
            if previous['digest'] != digest:
                raise RuntimeError(
                    'Texture filename collision: %s comes from both %s and %s'
                    % (filename, previous['source'], source_label))
            return os.path.splitext(filename)[0]
        planned[key] = {'digest': digest, 'source': source_label}

        if os.path.isfile(destination) and _sha256_file(destination) == digest:
            unchanged += 1
        else:
            writer(destination)
            copied += 1
        if is_variant:
            variants += 1
        return os.path.splitext(filename)[0]

    def _stage_file(source_path, is_variant=False):
        source_path = os.path.abspath(source_path)
        digest = _sha256_file(source_path)

        def _copy(destination):
            if (os.path.isfile(destination) and
                    os.path.samefile(source_path, destination)):
                return
            shutil.copy2(source_path, destination)

        return _stage_payload(
            os.path.basename(source_path), digest, _copy, source_path,
            is_variant=is_variant)

    def _stage_variants(source_path):
        directory = os.path.dirname(source_path)
        filename = os.path.basename(source_path)
        stem, extension = os.path.splitext(filename)
        base_stem = stem.split('#', 1)[0]
        prefix = (base_stem + '#').casefold()
        try:
            siblings = sorted(os.listdir(directory))
        except OSError:
            return
        for sibling in siblings:
            sibling_stem, sibling_extension = os.path.splitext(sibling)
            if (sibling_stem.casefold().startswith(prefix) and
                    sibling_extension.casefold() == extension.casefold()):
                candidate = os.path.join(directory, sibling)
                if os.path.isfile(candidate):
                    _stage_file(candidate, is_variant=True)

    def _stage_image(image, context_label):
        image_key = image.as_pointer()
        if image_key in image_stems:
            return image_stems[image_key]

        source_path = _resolved_image_path(image)
        packed = image.packed_file
        if packed:
            payload = bytes(packed.data)
            filename = _image_export_filename(image, source_path)
            digest = hashlib.sha256(payload).hexdigest()

            def _write(destination):
                with open(destination, 'wb') as handle:
                    handle.write(payload)

            stem = _stage_payload(
                filename, digest, _write, 'packed image ' + image.name)
            if source_path and os.path.isfile(source_path):
                _stage_variants(source_path)
        elif source_path and os.path.isfile(source_path):
            stem = _stage_file(source_path)
            _stage_variants(source_path)
        else:
            missing.append('%s -> %s' % (
                context_label, source_path or image.filepath or image.name))
            stem = None
        image_stems[image_key] = stem
        return stem

    role_stems = {}
    for material in materials:
        role_images, all_images = _material_texture_images(material)
        for image in all_images:
            _stage_image(image, material.name)
        mapped = {}
        for role, image in role_images.items():
            stem = _stage_image(image, '%s:%s' % (material.name, role))
            if stem:
                mapped[role] = stem
        role_stems[material.as_pointer()] = mapped

    from .mtl_io import _collect_search_dirs, _search_texture_file
    for walk_root, _dirs, files in os.walk(src_dir):
        for filename in files:
            if not filename.lower().endswith('.mtl'):
                continue
            source_mtl = os.path.join(walk_root, filename)
            relative = os.path.relpath(source_mtl, src_dir)
            output_mtl = os.path.join(output_dir, relative)
            if not os.path.isfile(output_mtl):
                continue

            normalized = os.path.normcase(os.path.abspath(source_mtl))
            material = (material_by_source.get(normalized) or
                        material_by_filename.get(filename.casefold()))
            mapped = (role_stems.get(material.as_pointer(), {})
                      if material is not None else {})
            search_dirs = _collect_search_dirs(
                source_mtl, os.path.dirname(source_mtl))
            with open(output_mtl, 'r', encoding='utf-8', errors='ignore') as handle:
                content = handle.read()

            def _localize(match):
                nonlocal localized_refs
                role = match.group(2).lower()
                texture_ref = match.group(3)
                stem = mapped.get(role)
                if not stem:
                    source_texture = _search_texture_file(
                        texture_ref, search_dirs)
                    if source_texture:
                        stem = _stage_file(source_texture)
                        _stage_variants(source_texture)
                    else:
                        missing.append('%s -> %s' % (relative, texture_ref))
                if not stem:
                    return match.group(0)
                localized_refs += 1
                return match.group(1) + stem + match.group(4)

            localized = _TEXTURE_DIRECTIVE_RE.sub(_localize, content)
            if localized != content:
                with open(output_mtl, 'w', encoding='utf-8', newline='') as handle:
                    handle.write(localized)

    unique_missing = list(dict.fromkeys(missing))
    print('[vehicle] textures: %d copied, %d unchanged, %d variants, '
          '%d MTL refs localized, %d missing'
          % (copied, unchanged, variants, localized_refs,
             len(unique_missing)))
    for entry in unique_missing[:20]:
        print('[vehicle] texture missing: %s' % entry)
    if len(unique_missing) > 20:
        print('[vehicle] texture missing: ... and %d more'
              % (len(unique_missing) - 20))
    return {
        'copied': copied,
        'unchanged': unchanged,
        'variants': variants,
        'localized_refs': localized_refs,
        'missing': unique_missing,
    }


def export_vehicle_folder(output_dir, root_obj=None):
    """安全拆分导出载具；PLY 非坐标载荷和 MDL 原文保持不变。"""
    output_dir = os.path.abspath(output_dir)
    if root_obj is None:
        active = bpy.context.active_object
        while active and not active.get('gem2_vehicle_root'):
            active = active.parent
        if active:
            root_obj = active
        else:
            roots = [o for o in bpy.data.objects if o.get('gem2_vehicle_root')]
            if not roots:
                raise RuntimeError('场景中没有已导入的载具 (先执行【导入载具文件夹】)')
            root_obj = roots[0]

    src_dir = root_obj.get(FOLDER_KEY)
    if not src_dir or not os.path.isdir(src_dir):
        raise RuntimeError('载具来源目录缺失: %s' % src_dir)
    if os.path.normcase(output_dir) == os.path.normcase(os.path.abspath(src_dir)):
        raise RuntimeError('为避免覆盖原始模板，载具导出目录不能等于来源目录')
    os.makedirs(output_dir, exist_ok=True)

    arm_name = root_obj.get('gem2_vehicle_armature', '')
    arm_obj = bpy.data.objects.get(arm_name) if arm_name else None
    if arm_name and (arm_obj is None or arm_obj.type != 'ARMATURE'):
        raise RuntimeError('载具绑定骨架已丢失: %s' % arm_name)
    world_mats = {}
    if arm_obj and arm_obj.get('gem2_world_mats'):
        world_mats = {
            name: Matrix(rows)
            for name, rows in json.loads(arm_obj['gem2_world_mats']).items()
        }

    parts = [o for o in root_obj.children
             if o.type == 'MESH' and not o.get(VOL_KEY)
             and not o.get(PRIMITIVE_VOL_KEY)
             and o.get(BONE_KEY) is not None]
    vols = [o for o in root_obj.children
            if o.type == 'MESH' and o.get(VOL_KEY)]
    primitives = [o for o in root_obj.children
                  if o.type == 'MESH' and o.get(PRIMITIVE_VOL_KEY)]
    if 'gem2_vehicle_primitive_volume_count' in root_obj:
        expected_count = int(root_obj['gem2_vehicle_primitive_volume_count'])
        if len(primitives) != expected_count:
            raise RuntimeError(
                'MDL 原语碰撞体数量已改变: 期望 %d, 当前 %d'
                % (expected_count, len(primitives)))
    for obj in primitives:
        home_raw = obj.get('gem2_vehicle_home_matrix')
        geometry_hash = obj.get('gem2_vehicle_primitive_geometry_hash')
        if not home_raw or not geometry_hash:
            raise RuntimeError('MDL 原语碰撞体元数据不完整: %s' % obj.name)
        home = Matrix(json.loads(home_raw))
        current_local = root_obj.matrix_world.inverted() @ obj.matrix_world
        if _matrix_delta(current_local, home) > 1e-5:
            raise RuntimeError(
                'MDL 原语碰撞体 %s 的对象变换已改变；当前安全导出只读'
                % obj.name)
        if _mesh_geometry_hash(obj.data) != geometry_hash:
            raise RuntimeError(
                'MDL 原语碰撞体 %s 的网格已改变；当前安全导出只读'
                % obj.name)
        bone_name = obj.get(BONE_KEY, '')
        local_raw = obj.get('gem2_vehicle_volume_local_matrix')
        if bone_name and local_raw and bone_name in world_mats:
            expected_home = world_mats[bone_name] @ Matrix(json.loads(local_raw))
            if _matrix_delta(home, expected_home) > 1e-5:
                raise RuntimeError(
                    'MDL 原语碰撞体 %s 的骨/局部矩阵与 MDL 不一致'
                    % obj.name)
    by_file = {}
    for obj in parts:
        filename = obj.get('gem2_vehicle_ply_file')
        if not filename:
            raise RuntimeError('载具部件缺少原始 PLY 文件名: %s' % obj.name)
        by_file.setdefault(filename.casefold(), []).append(obj)

    exported_names = set()
    for instances in by_file.values():
        filename = instances[0]['gem2_vehicle_ply_file']
        _export_part_ply(os.path.join(output_dir, filename), instances,
                         world_mats)
        exported_names.add(filename.casefold())
        print('[vehicle] PLY %s <- %d linked instance(s)'
              % (filename, len(instances)))

    # 未能在场景中实例化的孤立 PLY 也保持原件，避免输出文件夹残缺。
    for source in glob.glob(os.path.join(src_dir, '*.ply')):
        if os.path.basename(source).casefold() not in exported_names:
            shutil.copy2(source, os.path.join(output_dir, os.path.basename(source)))

    vol_by_file = {}
    for obj in vols:
        filename = obj.get('gem2_vol_file')
        if not filename:
            raise RuntimeError('碰撞体缺少原始 VOL 文件名: %s' % obj.name)
        vol_by_file.setdefault(filename.casefold(), []).append(obj)
    exported_vols = set()
    for instances in vol_by_file.values():
        filename = instances[0]['gem2_vol_file']
        _export_part_vol(os.path.join(output_dir, filename), instances,
                         world_mats)
        exported_vols.add(filename.casefold())
        print('[vehicle] VOL %s <- %d linked instance(s)'
              % (filename, len(instances)))
    for source in glob.glob(os.path.join(src_dir, '*.vol')):
        if os.path.basename(source).casefold() not in exported_vols:
            shutil.copy2(source, os.path.join(output_dir, os.path.basename(source)))

    # MDL 不做正则重写：文件名保持原值，所以连编码、空白、动画和所有骨类型
    # 都可以逐字节保留。
    mdl_path = _find_mdl(src_dir)
    if mdl_path:
        shutil.copy2(mdl_path, os.path.join(output_dir, os.path.basename(mdl_path)))

    _export_vehicle_animations(output_dir, src_dir, mdl_path)

    # 复制全部旁车资源和子目录；PLY/MDL/VOL/ANM 由上面专门处理。
    for walk_root, _dirs, files in os.walk(src_dir):
        rel = os.path.relpath(walk_root, src_dir)
        dest_root = output_dir if rel == '.' else os.path.join(output_dir, rel)
        os.makedirs(dest_root, exist_ok=True)
        for filename in files:
            if os.path.splitext(filename)[1].lower() in {
                    '.ply', '.mdl', '.vol', '.anm'}:
                continue
            shutil.copy2(os.path.join(walk_root, filename),
                         os.path.join(dest_root, filename))

    _export_vehicle_textures(output_dir, src_dir, parts)

    print('[vehicle] 导出完成 -> %s' % output_dir)
    return output_dir


def _matrix_delta(a, b):
    return max(abs(float(a[row][col]) - float(b[row][col]))
               for row in range(4) for col in range(4))


def _mesh_topology_hash(mesh):
    import hashlib
    import struct
    mesh.calc_loop_triangles()
    payload = bytearray()
    for tri in mesh.loop_triangles:
        payload.extend(struct.pack('<III', *tri.vertices))
    return hashlib.sha256(payload).hexdigest()


def _mesh_geometry_hash(mesh):
    """Hash helper geometry so unsupported primitive edits cannot be lost."""
    import hashlib
    import struct
    mesh.calc_loop_triangles()
    payload = bytearray()
    for vertex in mesh.vertices:
        payload.extend(struct.pack('<fff', *vertex.co))
    for tri in mesh.loop_triangles:
        payload.extend(struct.pack('<III', *tri.vertices))
    return hashlib.sha256(payload).hexdigest()


def _export_part_vol(filepath, instances, world_mats):
    """Patch only VOL local vertex positions into its original binary file."""
    import struct
    if not instances:
        raise RuntimeError('empty vehicle VOL instance group')
    primary = instances[0]
    mesh = primary.data
    source = primary.get('gem2_vol_source')
    if not source or not os.path.isfile(source):
        raise RuntimeError('%s 的原始 VOL 不存在: %s' % (primary.name, source))
    if os.path.basename(source).casefold() != os.path.basename(filepath).casefold():
        raise RuntimeError('载具 VOL 文件名不可改: %s' % primary.name)

    expected_topology = primary.get('gem2_vol_topology_hash', '')
    if not expected_topology or _mesh_topology_hash(mesh) != expected_topology:
        raise RuntimeError('%s 拓扑已改变；安全模式只支持移动现有顶点'
                           % os.path.basename(source))

    root = primary.parent
    for obj in instances:
        if obj.data is not mesh:
            raise RuntimeError('%s 的重复 Volume 实例已拆分 Mesh；无法写回同一 VOL'
                               % os.path.basename(source))
        if obj.parent is not root:
            raise RuntimeError('碰撞体实例脱离原根节点: %s' % obj.name)
        home_raw = obj.get('gem2_vehicle_home_matrix')
        local_raw = obj.get('gem2_vehicle_volume_local_matrix')
        if not home_raw or not local_raw:
            raise RuntimeError('碰撞体缺少导入矩阵: %s' % obj.name)
        home = Matrix(json.loads(home_raw))
        current_local = root.matrix_world.inverted() @ obj.matrix_world
        if _matrix_delta(current_local, home) > 1e-5:
            raise RuntimeError('碰撞体 %s 的对象变换已改变；请在编辑模式移动顶点'
                               % obj.name)
        bone_name = obj.get(BONE_KEY, '')
        if bone_name:
            bone_world = world_mats.get(bone_name)
            if bone_world is None:
                raise RuntimeError('碰撞体 %s 引用的骨不存在: %s'
                                   % (obj.name, bone_name))
            expected_home = bone_world @ Matrix(json.loads(local_raw))
            if _matrix_delta(home, expected_home) > 1e-5:
                raise RuntimeError('碰撞体 %s 的骨/局部矩阵与 MDL 不一致'
                                   % obj.name)

    data_offset = int(primary.get('gem2_vol_vertex_data_offset', -1))
    source_count = int(primary.get('gem2_vol_vertex_count', 0))
    if source_count != len(mesh.vertices) or data_offset < 0:
        raise RuntimeError('%s 的 VOL 顶点布局元数据不完整'
                           % os.path.basename(source))
    with open(source, 'rb') as handle:
        raw = handle.read()
    if data_offset + source_count * 12 > len(raw):
        raise RuntimeError('%s 的 VOL VERT 范围越过文件末尾'
                           % os.path.basename(source))

    data = bytearray(raw)
    changed = False
    for index, vertex in enumerate(mesh.vertices):
        packed = struct.pack('<fff', *vertex.co)
        start = data_offset + index * 12
        if raw[start:start + 12] != packed:
            data[start:start + 12] = packed
            changed = True
    if changed:
        with open(filepath, 'wb') as handle:
            handle.write(data)
    else:
        shutil.copy2(source, filepath)


def _export_part_ply(filepath, instances, world_mats):
    """以原始 PLY 为模板，只补丁已编辑的局部顶点坐标。

    所有 MESH/SKIN/FVF/UV/法线/切线/索引和未知字段均逐字保留。拓扑改变、
    实例离开其导入 home 矩阵、重复引用被拆成不同 Mesh 时明确拒绝。
    """
    import struct
    if not instances:
        raise RuntimeError('empty vehicle PLY instance group')
    primary = instances[0]
    mesh = primary.data
    source = primary.get('gem2_ply_source')
    if not source or not os.path.isfile(source):
        raise RuntimeError('%s 的原始 PLY 不存在: %s' % (primary.name, source))
    if os.path.basename(source).casefold() != os.path.basename(filepath).casefold():
        raise RuntimeError('载具 PLY 文件名不可改: %s' % primary.name)

    expected_topology = primary.get('gem2_topology_hash', '')
    actual_topology = _mesh_topology_hash(mesh)
    if not expected_topology or actual_topology != expected_topology:
        raise RuntimeError('%s 拓扑已改变；安全模式只支持移动现有顶点'
                           % os.path.basename(source))

    root = primary.parent
    for obj in instances:
        if obj.data is not mesh:
            raise RuntimeError('%s 的重复 VolumeView 实例已拆分 Mesh；无法写回同一 PLY'
                               % os.path.basename(source))
        if obj.parent is not root:
            raise RuntimeError('载具实例脱离原根节点: %s' % obj.name)
        home_raw = obj.get('gem2_vehicle_home_matrix')
        if not home_raw:
            raise RuntimeError('载具实例缺少 home 矩阵: %s' % obj.name)
        home = Matrix(json.loads(home_raw))
        current_local = root.matrix_world.inverted() @ obj.matrix_world
        if _matrix_delta(current_local, home) > 1e-5:
            raise RuntimeError('载具实例 %s 的对象变换已改变；请在编辑模式移动顶点'
                               % obj.name)
        bone_name = obj.get(BONE_KEY)
        if bone_name:
            expected_home = world_mats.get(bone_name)
            if expected_home is None or _matrix_delta(home, expected_home) > 1e-5:
                raise RuntimeError('载具实例 %s 的骨矩阵与 MDL 不一致' % obj.name)

    stride = int(primary.get('gem2_vertex_stride', 0))
    data_offset = int(primary.get('gem2_vertex_data_offset', -1))
    source_count = int(primary.get('gem2_source_vertex_count', 0))
    try:
        source_indices = json.loads(primary.get('gem2_source_vertex_indices', '[]'))
    except Exception as exc:
        raise RuntimeError('PLY 顶点映射损坏: %s' % exc)
    if (stride < 12 or data_offset < 0 or source_count <= 0
            or len(source_indices) != len(mesh.vertices)):
        raise RuntimeError('%s 的 PLY 布局元数据不完整' % os.path.basename(source))

    with open(source, 'rb') as handle:
        raw = handle.read()
    if data_offset + source_count * stride > len(raw):
        raise RuntimeError('%s 的 VERT 范围越过文件末尾' % os.path.basename(source))

    packed_positions = []
    changed = False
    for vertex, source_index in zip(mesh.vertices, source_indices):
        if source_index < 0 or source_index >= source_count:
            raise RuntimeError('%s 的顶点映射越界' % os.path.basename(source))
        packed = struct.pack('<fff', *vertex.co)
        packed_positions.append((source_index, packed))
        start = data_offset + source_index * stride
        if raw[start:start + 12] != packed:
            changed = True

    if not changed:
        shutil.copy2(source, filepath)
        return

    data = bytearray(raw)
    for source_index, packed in packed_positions:
        start = data_offset + source_index * stride
        data[start:start + 12] = packed

    # BNDS 仅由被索引/显示的顶点重算，绝不让原文件的垃圾 padding 顶点撑爆包围盒。
    if data[4:8] == b'BNDS' and len(mesh.vertices):
        xs = [vertex.co.x for vertex in mesh.vertices]
        ys = [vertex.co.y for vertex in mesh.vertices]
        zs = [vertex.co.z for vertex in mesh.vertices]
        data[8:32] = struct.pack(
            '<ffffff', min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    with open(filepath, 'wb') as handle:
        handle.write(data)


# ═══════════════════════════════════════════════════════════════
#  算子
# ═══════════════════════════════════════════════════════════════

class MOWAS2_OT_ImportVehicleFolder(bpy.types.Operator):
    """按文件夹完整导入载具 (mdl + 全部 ply/vol)"""
    bl_idname = "gem2.mowas2_import_vehicle_folder"
    bl_label = _("vehicle.import.label")
    bl_description = _("vehicle.import.desc")
    bl_options = {'REGISTER', 'UNDO'}

    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            if not self.directory or not os.path.isdir(self.directory):
                self.report({'ERROR'}, _("vehicle.err.select_import_dir"))
                return {'CANCELLED'}
            root = import_vehicle_folder(self.directory)
            props = context.scene.mowas2_props
            props.report = _("vehicle.info.imported", dir=self.directory, name=root.name)
            self.report({'INFO'}, _("vehicle.info.imported", dir=self.directory, name=root.name))
            return {'FINISHED'}
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, _("vehicle.err.import_failed", error=e))
            return {'CANCELLED'}


class MOWAS2_OT_ExportVehicleFolder(bpy.types.Operator):
    """导出载具 (自动按骨拆分)"""
    bl_idname = "gem2.mowas2_export_vehicle_folder"
    bl_label = _("vehicle.export.label")
    bl_description = _("vehicle.export.desc")
    bl_options = {'REGISTER', 'UNDO'}

    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        props = context.scene.mowas2_props
        if props.output_dir and os.path.isdir(props.output_dir):
            self.directory = props.output_dir
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            if not self.directory:
                self.report({'ERROR'}, _("vehicle.err.select_export_dir"))
                return {'CANCELLED'}
            out = export_vehicle_folder(self.directory)
            props = context.scene.mowas2_props
            props.report = _("vehicle.info.exported", dir=out)
            self.report({'INFO'}, _("vehicle.info.exported", dir=out))
            return {'FINISHED'}
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, _("vehicle.err.export_failed", error=e))
            return {'CANCELLED'}


VEHICLE_CLASSES = (MOWAS2_OT_ImportVehicleFolder, MOWAS2_OT_ExportVehicleFolder)


def register():
    for cls in VEHICLE_CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception as e:
            print('[vehicle] FAIL register %s: %s' % (cls.__name__, e))


def unregister():
    for cls in reversed(VEHICLE_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
