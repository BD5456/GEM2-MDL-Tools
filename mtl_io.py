"""
MTL material file parser and writer for GEM2 engine.
Supports {material simple} and {material bump} variants.
"""
import os
import bpy
from .i18n import _

TEXTURE_EXTENSIONS = ['.dds', '.DDS', '.tga', '.TGA', '.png', '.PNG',
                      '.jpg', '.JPG', '.jpeg', '.JPEG', '.bmp', '.BMP',
                      '.ctm', '.ebm', '.tex', '.tif', '.TIF']


def _find_texture_root(start_dir):
    """从给定目录向上逐级查找 mod 的 resource/texture 根目录。

    MOWAS2 的贴图按约定放在 <mod>/resource/texture/common/ 下，
    而 .ply/.mtl 通常在 <mod>/resource/entity/... 下。
    返回 ('<mod>/resource/texture', '<mod>/resource/texture/common') 或 (None, None)。
    """
    d = os.path.abspath(start_dir)
    for _ in range(12):
        texture_dir = os.path.join(d, 'resource', 'texture')
        common_dir = os.path.join(texture_dir, 'common')
        if os.path.isdir(common_dir):
            return texture_dir, common_dir
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None, None


def _find_game_texture_root():
    """尝试从 .gem2_paths.json 的 import 路径推导游戏 texture 根。"""
    try:
        from .core import get_paths
        imp = get_paths().get('import') or ''
        if imp and os.path.isdir(imp):
            texture_dir, common_dir = _find_texture_root(imp)
            if common_dir:
                return texture_dir, common_dir
    except Exception:
        pass
    return None, None


def _search_texture_file(tex_name, search_dirs, prefer_dirs=None):
    """在多个搜索根下查找贴图文件。

    tex_name 可能的形式：
      - 纯文件名:        "power_sabre_dif"
      - 相对路径:        "_w40/_weapon/power_sabre_dif"
      - $ 前缀路径:      "$/_w40/_weapon/power_sabre_dif"
      - $ + 特殊子目录:  "$/[nrm]/w40/power_sabre_nrm"
    """
    if not tex_name:
        return None

    name = tex_name.strip()
    is_dollar = name.startswith('$')
    if is_dollar:
        name = name[1:].lstrip('/\\')
    # 归一化斜杠
    name = name.replace('\\', '/')
    basename = os.path.basename(name)
    dirpart = os.path.dirname(name)

    for ext in TEXTURE_EXTENSIONS:
        # 1) 优先: 精确路径 + 扩展名
        for base in search_dirs:
            cand = os.path.join(base, name + ext)
            if os.path.isfile(cand):
                return cand
        # 2) 仅文件名 (搜索根直接命中, 如 common/<name>.dds)
        for base in search_dirs:
            cand = os.path.join(base, basename + ext)
            if os.path.isfile(cand):
                return cand
        # 3) 仅目录部分 + 扩展名
        if dirpart:
            for base in search_dirs:
                cand = os.path.join(base, dirpart, basename + ext)
                if os.path.isfile(cand):
                    return cand
    return None


def _collect_search_dirs(mtl_path, base_dir):
    """收集所有候选贴图搜索根目录。"""
    dirs = []
    seen = set()

    def _add(d):
        d = os.path.abspath(d)
        if os.path.isdir(d) and d not in seen:
            seen.add(d)
            dirs.append(d)

    # 1) .ply 同目录
    _add(base_dir)
    # 2) .mtl 同目录
    _add(os.path.dirname(os.path.abspath(mtl_path)))
    # 3) mod 的 resource/texture (向上推断)
    for start in (base_dir, os.path.dirname(os.path.abspath(mtl_path))):
        tex_root, common = _find_texture_root(start)
        if tex_root:
            _add(tex_root)
            _add(common)
    # 4) 游戏 mod 全局扫描到的 resource/texture/common
    tex_root2, common2 = _find_game_texture_root()
    if tex_root2:
        _add(tex_root2)
        _add(common2)
    return dirs


def import_mtl(mtl_path, mat, base_dir):
    """解析 .mtl 文件，设置 Principled BSDF 材质"""
    if not os.path.isfile(mtl_path):
        print(_("mtl.not_found", path=mtl_path))
        return

    with open(mtl_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 找到 material 块
    mat_start = content.find('{material')
    if mat_start == -1:
        print(_("mtl.no_material_block", path=mtl_path))
        return

    import re

    def _find_brace(text, start):
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{': depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0: return i
        return -1

    mat_end = _find_brace(content, mat_start)
    if mat_end == -1:
        print(_("mtl.block_unclosed"))
        return
    block = content[mat_start:mat_end+1]

    def _extract_tex(key):
        m = re.search(r'{' + key + r'\s+"([^"]+)"}', block)
        return m.group(1).strip() if m else None

    diffuse = _extract_tex('diffuse')
    bump = _extract_tex('bump')
    specular = _extract_tex('specular')

    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    output = nodes.new(type='ShaderNodeOutputMaterial')
    principled = nodes.new(type='ShaderNodeBsdfPrincipled')
    output.location = (300, 0)
    principled.location = (0, 0)
    links.new(principled.outputs['BSDF'], output.inputs['Surface'])

    def _load_image(texture_name, socket_name, is_normal=False):
        if not texture_name:
            return
        search_dirs = _collect_search_dirs(mtl_path, base_dir)
        img_path = _search_texture_file(texture_name, search_dirs)
        if not img_path:
            # 兼容特殊子目录符号: [nrm]/[spc]/[dif] 若按普通相对路径没找到，
            # 再尝试把带 $ 的名字去掉 $ 后作为 base_dir 相对路径直接试
            direct = os.path.join(base_dir, texture_name.lstrip('$/\\').replace('\\', '/'))
            if os.path.isfile(direct):
                img_path = direct
        if not img_path:
            print(_("mtl.texture_not_found", name=texture_name, dir="; ".join(search_dirs) or base_dir))
            return
        try:
            img = bpy.data.images.load(img_path)
        except Exception as e:
            print(_("mtl.load_failed", path=img_path, error=e))
            return
        tex_node = nodes.new(type='ShaderNodeTexImage')
        tex_node.image = img
        tex_node.location = (-200, -200 * len(nodes))
        if is_normal:
            nm = nodes.new(type='ShaderNodeNormalMap')
            nm.location = (-50, -200 * len(nodes))
            links.new(tex_node.outputs['Color'], nm.inputs['Color'])
            links.new(nm.outputs['Normal'], principled.inputs[socket_name])
        else:
            links.new(tex_node.outputs['Color'], principled.inputs[socket_name])

    if diffuse:
        _load_image(diffuse, 'Base Color')
    if bump:
        _load_image(bump, 'Normal', is_normal=True)
    if specular:
        _load_image(specular, 'Specular IOR Level')


def export_mtl(filepath, mat, mode='SIMPLE'):
    """写出 MTL 材质文件"""

    def _get_diffuse_name(mat):
        if not mat.use_nodes:
            return None
        for node in mat.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                if node.inputs["Base Color"].links:
                    img_node = node.inputs["Base Color"].links[0].from_node
                    if img_node.type == 'TEX_IMAGE' and img_node.image:
                        return os.path.splitext(img_node.image.name)[0]
        return None

    diffuse = _get_diffuse_name(mat) or mat.name

    with open(filepath, "w", encoding="utf-8") as f:
        if mode == 'SIMPLE':
            f.write("{material simple\n")
            f.write('\t{diffuse "' + diffuse + '"}\n')
            f.write('\t{blend none}\n')
            f.write("}\n")
            return

        # BUMP 模式
        f.write("{material bump\n")
        f.write('\t{diffuse "' + diffuse + '"}\n')

        bump = None
        specular = None
        if mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    if node.inputs["Normal"].links:
                        img_node = node.inputs["Normal"].links[0].from_node
                        if img_node.type == 'NORMAL_MAP' and img_node.inputs["Color"].links:
                            final_node = img_node.inputs["Color"].links[0].from_node
                            if final_node.type == 'TEX_IMAGE' and final_node.image:
                                bump = os.path.splitext(final_node.image.name)[0]
                        elif img_node.type == 'TEX_IMAGE' and img_node.image:
                            bump = os.path.splitext(img_node.image.name)[0]
                    if node.inputs["Specular IOR Level"].links:
                        img_node = node.inputs["Specular IOR Level"].links[0].from_node
                        if img_node.type == 'TEX_IMAGE' and img_node.image:
                            specular = os.path.splitext(img_node.image.name)[0]
                    break

        f.write('\t{bump "' + (bump if bump else mat.name + '_bp') + '"}\n')
        f.write('\t{specular "' + (specular if specular else mat.name + '_sp') + '"}\n')

        color = (255, 255, 255, 255)
        if mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    if not node.inputs["Base Color"].links:
                        col = node.inputs["Base Color"].default_value
                        color = tuple(int(c * 255) for c in col[:3]) + (255,)
                    break
        f.write('\t{color "' + f"{color[0]} {color[1]} {color[2]} {color[3]}" + '"}\n')
        f.write('\t{blend none}\n')
        f.write("}\n")
