# -*- coding: utf-8 -*-
"""
MOWAS2 PMX→GEM2 自动管线 v2（2026-08 修正版，手动跑通后固化）
================================================================
把 MMD/PMX 模型自动绑骨到 GEM2(MOWAS2) 58 骨士兵骨架并导出人物资源。

v1 的问题（用户实测反馈 + 修复）：
  1. 旧对齐(GFA 启发式缩放链)对 T-pose 源失效 → "宽体普京"。
     → 改为 Umeyama 刚性相似变换（只旋转+平移+均匀缩放，形状零失真）
       + T-pose 手臂姿态级旋转（绕肩 pivot + roll 迭代）。
  2. 新建 VolumeView 载体骨和直接挂 head 的静态 PLY 都会在人物动画路径中
     消失。GOH/GFA 可工作结构是：多个 stride-40 蒙皮 PLY 都作为同一个 skin
     骨的直接 VolumeView；GOH 自动拆分严格复用这一路径，不新增骨。
  3. Blender UV/自定义法线属于 loop，而 GEM2 使用交错式顶点记录；物理逐面
     拆点会制造大量无意义副本。同位置顶点只复用稳定游戏权重，法线必须保留
     每个 loop 的原值，再按最终 40-byte 位置/权重/法线/UV 记录精确去重。
  4. 权重转移丢躯干：该 PMX 躯干权重全在 cf_s_* 补充骨上（主骨只有 0 权重
     条目）→ 必须先跑骨骼铸造（未映射骨权重上卷到最近映射祖先）。
  5. mirror_swap 把 'ik_leftright' 误换名成 'ik_leftleft'（结尾 'right' 命中
     了裸后缀规则）→ 必须用 '_left'/'_right'（带下划线）后缀判断。
  6. modifier_apply 烘焙在 parent 链(1.9×Rz90)下双重变换 →
     改用 evaluated 顶点写回法冻结姿态。
"""
import bpy
import ast
import hashlib
import heapq
import os
import re
import struct
import math
import shutil
import subprocess
import tempfile
import json
from itertools import combinations
from datetime import datetime, timezone
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

from .i18n import _
from .texture_export import (
    TextureStager,
    fill_black_alpha,
    image_file_to_tga,
    iter_material_images,
    resolve_material_image,
    resolved_image_path,
)

OUT_DEFAULT = os.path.join(os.path.expanduser('~'), 'Desktop')  # 默认输出=用户桌面, 面板可改
GROUND_Z = -0.07  # 样本 skin 网格脚底高度（贴地基准）
# GOH cutout materials use alpharef 127 + blend test by default. Soft facial
# overlays (brows/lashes/eyelines/pupils) keep blend/DXT5 to preserve AA alpha.
GOH_ALPHA_TEST_TRANSPARENT = True
# ik_updown 对应源 UpperBody2 区域的额外缩放默认关闭，避免改变既有导出；
# 开启后以 GFA WidthExtraScaling_PerStep**0.5 为自动基准，再乘面板倍率。
GOH_IK_UPDOWN_SCALE = False
GOH_IK_UPDOWN_MULTIPLIER = 1.0
# 手掌/手指链重映射的最大长度比值。KK 源实测手链 ratio 1.22~1.33，
# 会把指根从腕部推开 ~1 单位 → 手掌腕部与手臂"接不上"。钳制后
# 手掌跟随腕部，残余长度差由蒙皮渐变吸收，优先保证连续。
GOH_HAND_RATIO_CLAMP = (0.85, 1.15)
# Diagnostic/compatibility gate: direct child-bone translation can tear MMD
# wrist seams. Runtime tests may disable it; the production default is selected
# after geometry and target-anchor validation below.
GOH_DIRECT_MMD_WRIST_REANCHOR = False

# GOH 版 (v10): 默认目标骨架 = GFA 长臂模板 (agf_feitusa.mdl 复制, 前臂
# 6.268/手 5.257, 与 GOH 动画 hand2l=6.268 一致); 旧 goh_skin.mdl (agit_yelan
# 复制, 短臂 5.38/4.51) 保留在 samples/ 供 GOH_GFA_LONGARM=False 时手动选用。
GOH_DEFAULT_MDL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'samples', 'goh_skin_gfa.mdl')
GOH_ROUTE_MDL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'samples', 'goh_skin.mdl')
MOWAS2_ROUTE_MDL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'samples', 'MOWAS2.mdl')

# E6.13: 有 alpha 通道但游戏内不需要透明的贴图 (用户确认: 脸部贴图不用 alpha)
EXCLUDE_TRANSPARENT = {'face@cf_m_face_00@alpha'}

# 121 测试: alpha 材质同步写入 PLY 的 MESH_FLAG_ALPHA。
#
# ═══ 透明材质分类总则 (2026-08-17 晚定案, 对照 GOH 实证) ═══
# 引擎规则 (MOWAS2 13533 ply + GOH humanskin 122 ply 扫描):
#   - mtl {blend blend} + PLY flags 0x0002  → 半透明混合 (皮肤透出)
#   - mtl {blend test}  + {alpharef 127}    → 阈值镂空 (无 0x0002)
#   - mtl {blend none}                      → 不透明
# KK 系贴图的透明像素 RGB 是纯黑, 用 none 会把黑底画出 (用户实测黑块)。
# 半透明混合对"高透丝袜 / 透明紧身衣"类正确 (贴图零改动, 精度无损);
# 硬镂空对"袜口/蕾丝/渔网"类正确 (alpha<127 直接丢弃, 黑底永不画出)。
# `toufa` 是当前 KK/KKS 样例实际使用的头发材质命名，不能只匹配 hair。
FORCE_OPAQUE_ALPHA_KW = (
    'hair', 'toufa', '髪', '头发',
    'shoe', 'shoes', 'boot', 'boots', 'heel', 'footwear',
    'armor', 'armour', 'greave', 'greaves', 'legguard', 'leg_armor',
    # 2026-08-18 v3 (GOH 实测): 紧身衣/连裤袜 (bodytights/tights) 改实体。
    # KK 系 bodytights 常延伸到脚底 (model_better2 的 bodytights*4 到
    # z=-0.07) → blend 半透明 = 游戏里"鞋子透明"。GOH 原版袜/鞋/紧身衣
    # 全 {blend none}; KK 黑底由 png_to_tga 的 fill_black 填充处理。
    'tight', 'bodytights',
)

# 2026-08-18: 硬实体前置判定 (鞋/靴/护甲) —— 任何模式 (含标准 MMD) 下
# 命中即强制不透明。修复: 鞋子贴图名常带 _gloss/_highlight/_lens 后缀
# (MMD_BLEND_KW 命中 → 误判 blend → 游戏里鞋子半透明), 必须先于 MMD 表。
# 头发/睫毛等"需要 test 镂空"的部件不在此表 (仍由 MMD_TEST_KW 先行)。
FORCE_OPAQUE_HARD_KW = (
    'shoe', 'shoes', 'boot', 'boots', 'heel', 'footwear',
    'sandal', 'slipper', 'clog', 'loafer', 'pump', 'leather',
    'armor', 'armour', 'greave', 'greaves', 'legguard', 'leg_armor',
    '鞋', '靴',
)

# 兼容模式的半透明混合类 (blend blend + PLY 0x0002)。默认场景
# 已切到 GOH/akq_youwu 的 alpha-test；关闭面板开关时才使用本表。
# panst 与眼部细节保留为旧版 blend 对照路径，tight/bodytights 在
# alpha-test 模式下由 _classify_mode 改走 test，避免黑底或深度排序问题。
FORCE_ALPHA_BLEND_KW = (
    'panst',
    'eyeline', 'eyelash', 'lash', 'hitomi', 'mayuge', 'pupil', 'brow',
)

# alpha-test 镂空类 (blend test + alpharef 127 + alphatocoverage, 无 0x0002):
# 贴图挖洞 + 实体表面 —— 袜子(袜口/图案镂空)等硬边缘, 半透明像素会被阈值裁掉
# (GOH 304 例实证)。按用户要求只保留 sock。
FORCE_ALPHA_TEST_KW = (
    'sock', 'socks',
)

# ═══ 标准 MMD 源独立透明过滤表 (2026-08-17 晚新增) ═══
# 与 KK/KKS 的 pmx 工作流【并联】: 源骨架无 cf_* 骨 (标准 MMD/崩3/原神等
# 模之屋模型) 时启用本表, 材质名原样保留在 PMX 里 (mmd_tools 只翻骨名不翻
# 材质名), 因此要覆盖 拼音/中文/日文/罗马音/英文 全部变体。
# 头发/睫毛/眉/眼线/瞳孔等"镂空部件" → test (threshold discard, 黑底不画出);
# 眼影/高光/镜片等半透明 → blend。
# 命中顺序: MMD 表 → KK 表 (opaque/blend/test) → none。
# 注意: 标准 MMD 的"头发"必须走 test, 不能被 KK 表的 hair→opaque 拦掉,
# 所以 MMD 表判定在 KK 表之前。
MMD_TEST_KW = (
    # 中文 (含繁体/单字; 注意不加'眼'单字, 会误伤'眼白')
    '头发', '发', '髮', '发丝', '发尾', '刘海', '睫毛', '眉毛', '眉',
    '眼线', '瞳孔', '泪痕',
    # 日文
    '髪', '前髪', '後髪', 'まつげ', 'まゆ', 'まゆげ', '目', '瞳', '涙',
    # 英文
    'hair', 'bang', 'forelock', 'eyelash', 'lash', 'eyebrow', 'brow',
    'eyeliner', 'pupil', 'tearline', 'tear',
    # 罗马音
    'kami', 'maegami', 'matsuge', 'mayu', 'mayuge', 'hitomi',
    # 拼音
    'toufa', 'liuhai', 'jiemao', 'meimao', 'yanxian', 'tongkong',
)
MMD_BLEND_KW = (
    # 中文 / 日文 / 英文 / 罗马音 / 拼音
    '眼影', '高光', '镜片', 'アイシャドウ', 'ハイライト', 'レンズ',
    'eyeshadow', 'gloss', 'highlight', 'glass', 'lens', 'lensglow',
    'kage', 'higa', 'yanying', 'gaoguang',
)

# ─── E6.19: 减面保护区域 (完全不允许减面移动这些顶点) ────────────────
# 目的(用户实测反馈):
#   1. 脸/脸部细节参与减面 → 脸上出现难看细长三角 → 全部保护不移;
#   2. 紧身衣物被减面 → 塌陷顶点位移进皮下 → 破皮 → 紧身衣物壳保护不移。
#
# FACE_DETAIL_KW: 脸 + 五官 + 牙齿/口腔 + 眼球细节 (材质名命中即整体保护)。
# SKIN_TIGHT_KW:  紧身贴体衣物壳 (bodysuit/连裤袜/泳装/内衣等)。贴体衣物与皮肤
#                 间距极小, 减面位移会让它戳进皮肤; 保护后衣物几何零位移,
#                 外轮廓(可见表面)保持原封 → 不新增破皮。
FACE_DETAIL_KW = ('face', 'kao', 'mayuge', 'sirome', 'eyeline', 'eyelash',
                  'eye', 'tooth', 'tongue', 'nose', 'mouth', 'lip',
                  'hitomi', 'pupil', 'eyeball', 'glint', 'highlight', 'tang')
SKIN_TIGHT_KW = ('panst', 'stocking', 'sock', 'tights', 'leotard', 'bodysuit',
                 'swimsuit', 'bikini', 'underwear', 'panties', 'bra',
                 'tight', 'bodysock', 'bodystocking', 'spats', 'legging')

# ─── 紧身衣物贴图 alpha 软化 (2026-08-17, 默认关闭) ─────────────────
# 背景: KK 的裤袜/紧身衣贴图是"深黑 RGB + 高 alpha" (实测 panst: 55% 像素
# a=192/255 且 RGB≈0)。GEM2 `simple` 材质是标准 alpha 混合
# result = src.rgb*a + dst.rgb*(1-a), 黑 src × 0.75 只剩 25% 皮肤色。
#
# 2026-08-17 晚对照 GOH (Call to Arms - Gates of Hell, 同 GEM2 引擎,
# mods\3227269384\humanskin): GOH 的透明方案是【完全不改贴图】——
#   - 半透明 (眼影/镜片/装饰, 66 例): mtl {blend blend} + PLY flags 0x0002
#   - 镂空 (蕾丝/透明边缘, 304 例): mtl {blend test} + {alpharef 127} +
#     {alphatocoverage}, PLY flags 无 0x0002; 贴图 DXT5 带 alpha 原样使用。
# 因此本插件默认 TIGHT_ALPHA_SCALE = 1.0 (不软化, 贴图数据零改动,
# TGA 32bpp 未压缩, 色彩/alpha 精度 100% 保留)。
#
# 若某个丝袜贴图半透明区太黑 (皮肤透出 <25%) 而用户希望更透肉, 才需要
# 手动调低: TIGHT_ALPHA_SCALE=0.5 → a=192→96, 皮肤透出 25%→62%。
# 这是可选的"透肉增强", 默认不做 (精度优先)。
# TIGHT_ALPHA_LIFT>0 时还会把低 alpha 像素的 RGB 向白提亮 (默认 0 不改色)。
TIGHT_ALPHA_SCALE = 1.0
TIGHT_ALPHA_LIFT = 0


def _tex_has_alpha(img_path, thresh=250):
    """检测贴图是否有真透明 (alpha<250/255)。用 bpy 加载采样 (不依赖 PIL)。
    img.pixels 是 0.0-1.0 float, 阈值换算 250/255≈0.9804。
    返回后删除 image 数据块释放内存。失败返回 False。"""
    thr = thresh / 255.0
    try:
        img = bpy.data.images.load(img_path, check_existing=False)
    except Exception:
        return False
    try:
        if img.channels < 4:
            return False
        px = img.pixels  # RGBA 序列, 0-1 float
        n = len(px) // 4
        if n == 0:
            return False
        # 采样: 大步长遍历 + 四角, 找到任一 alpha < thr 即透明
        step = max(1, n // 400)
        for i in range(0, n, step):
            if px[i * 4 + 3] < thr:
                return True
        for i in (0, n - 1, n // 2):
            if px[i * 4 + 3] < thr:
                return True
        return False
    finally:
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass


def png_to_tga(img_path, tga_path, soften=False, fill_black=False,
                return_profile=False):
    """Write the shared GEM2 type-2 BGRA32 TGA representation.

    This compatibility entry point keeps the human pipeline API stable while
    the encoder itself lives in ``texture_export`` for every export route.
    """
    return image_file_to_tga(
        img_path, tga_path, soften=soften, fill_black=fill_black,
        return_profile=return_profile, alpha_scale=TIGHT_ALPHA_SCALE,
        alpha_lift=TIGHT_ALPHA_LIFT)


def _fill_black_alpha(px, thresh=0.5, max_iter=48):
    """Compatibility alias for callers that used the former local helper."""
    return fill_black_alpha(px, thresh=thresh, max_iter=max_iter)


def _is_force_opaque_alpha(name):
    """返回贴图/材质名是否属于 120 测试版的强制不透明类别。"""
    base = os.path.splitext(os.path.basename(name or ''))[0].casefold()
    return any(kw.casefold() in base for kw in FORCE_OPAQUE_ALPHA_KW)


def _is_force_alpha_blend(name):
    """返回贴图/材质名是否应使用真正的半透明 blend。"""
    if _is_force_opaque_alpha(name):
        return False
    base = os.path.splitext(os.path.basename(name or ''))[0].casefold()
    return any(kw.casefold() in base for kw in FORCE_ALPHA_BLEND_KW)


def _is_force_alpha_test(name):
    """返回贴图/材质名是否应使用 alpha-test（blend test）。"""
    if _is_force_opaque_alpha(name) or _is_force_alpha_blend(name):
        return False
    base = os.path.splitext(os.path.basename(name or ''))[0].casefold()
    return any(kw.casefold() in base for kw in FORCE_ALPHA_TEST_KW)


def _is_force_opaque_hard(name):
    """硬实体前置判定 (鞋/靴/护甲/紧身衣): 任何模式强制不透明。

    2026-08-18: 从 _classify_mode 抽出供 TGA 黑底填充预估用。
    """
    base = os.path.splitext(os.path.basename(name or ''))[0].casefold()
    return any(kw.casefold() in base for kw in FORCE_OPAQUE_HARD_KW)


def _material_needs_alpha_flag(mat_name):
    """判断 PLY 的该 MESH 段是否必须带 MESH_FLAG_ALPHA (0x0002)。

    注意: 导出流程现在统一走 build_texture_plan → alpha_mats 判定 (与 mtl
    重写同一套规则), 本函数仅作无 plan 时的兜底。0x0002 位经游戏内 13533 个
    MOWAS2 ply + GOH humanskin 122 个 ply 扫描验证:
    - mtl {blend blend} → 带 0x0002 (flags 0x0C16/0x0C17) —— KK 移植皮肤
      2b/meihong/alice 与 GOH 66 例全如此;
    - mtl {blend test} → 【不带】0x0002 (flags 0x0C15, GOH 304 例全如此,
      test 是阈值丢弃不需要 alpha 混合标志);
    - mtl {blend none} → 不带 (0x0C14/0x0C15)。
    """
    return _is_force_alpha_blend(mat_name)


def _kw_hit(base, kws):
    base = (base or '').casefold()
    return any(kw.casefold() in base for kw in kws)


_PUPIL_LAYER_KW = (
    'hitomi', 'pupil', 'iris', 'eyered', 'eye_red', 'tongkong',
    '瞳孔', '瞳',
)
_SCLERA_LAYER_KW = (
    'sirome', 'sclera', 'eyewhite', 'eye_white', 'eye white',
    '白目', '眼白',
)
_EYE_SHADOW_LAYER_KW = (
    'eyeshadow', 'eye_shadow', 'eye shadow', '瞳影', '眼影',
)
_EYE_LID_LAYER_KW = (
    'eyeslid', 'eye_lid', 'eye lid', 'eyelid', '眼睑', '眼皮',
)
_PUPIL_LAYER_EXACT = {
    'eye', 'eye_', 'eye+', 'eye_+', 'eye__',
    'eyes', 'eyes+', 'eyes_', 'eyeleft', 'eyeright', '眼', '目',
}
_NECK_ACCESSORY_KW = (
    'neck', 'kubi', 'collar', 'choker', 'necklace', 'neckchain',
    'neck_chain', 'scarf', 'cravat', '领', '颈', '项圈',
)


def _is_sclera_layer(name):
    return _kw_hit(name, _SCLERA_LAYER_KW)


def _is_eye_shadow_layer(name):
    return _kw_hit(name, _EYE_SHADOW_LAYER_KW)


def _is_eye_lid_layer(name):
    return _kw_hit(name, _EYE_LID_LAYER_KW)


def _is_gfa_pupil_overlay(material_name, diffuse_name):
    """Match the Eyes+/eyeblend soft-alpha iris overlay."""
    diffuse = os.path.splitext(os.path.basename(diffuse_name or ''))[0]
    return (_is_pupil_layer(material_name)
            and _kw_hit(diffuse, ('eyeblend', 'eye_blend')))


def _is_pupil_layer(name):
    value = (name or '').casefold()
    if _is_sclera_layer(value):
        return False
    if any(key in value for key in ('shadow', 'eyeline', 'eyelash',
                                    'lash', 'brow')):
        return False
    return value in _PUPIL_LAYER_EXACT or _kw_hit(value, _PUPIL_LAYER_KW)


def _material_alpha_mode(material_name, diffuse_name, texture_mode,
                         has_alpha=False):
    """Refine a texture-level alpha plan with material semantics.

    Textures are converted once and can be shared by several materials. Sclera
    stays opaque, while a pupil layer uses the image's actual alpha fact rather
    than inheriting filename-based opaque/test classification.
    """
    semantic = material_name or ''
    if _is_sclera_layer(semantic):
        return 'none'
    if _is_eye_shadow_layer(semantic):
        return 'blend'
    if _is_pupil_layer(semantic):
        return 'blend' if has_alpha else 'none'
    diffuse_base = os.path.splitext(os.path.basename(diffuse_name or ''))[0]
    if diffuse_base in EXCLUDE_TRANSPARENT:
        return 'none'
    if (_is_force_opaque_hard(semantic)
            or _is_force_opaque_hard(diffuse_name)):
        return 'none'
    if (_is_force_opaque_alpha(semantic)
            or _is_force_opaque_alpha(diffuse_name)):
        tight = (_kw_hit(semantic, ('tight', 'bodytights'))
                 or _kw_hit(diffuse_name, ('tight', 'bodytights')))
        if tight and _goh_alpha_test_mode() and has_alpha:
            return 'test'
        return 'none'
    return texture_mode


def _material_static_alpha(mat):
    """Return and persist the source material's constant alpha when available."""
    if not mat:
        return None
    if bool(mat.get('gem2_force_export', False)):
        return 1.0
    if bool(mat.get('gem2_skip_export', False)):
        return 0.0
    persisted = mat.get('mowas2_material_static_alpha')
    if persisted is not None:
        try:
            return float(persisted)
        except (TypeError, ValueError):
            pass
    mmd = getattr(mat, 'mmd_material', None)
    if mmd is not None and hasattr(mmd, 'alpha'):
        try:
            alpha = float(mmd.alpha)
            mat['mowas2_material_static_alpha'] = alpha
            return alpha
        except (TypeError, ValueError):
            pass
    try:
        return float(mat.diffuse_color[3])
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def detect_source_mode(tgt=None, src=None):
    """判定源骨架类型: ``kk`` (KK/KKS) 或 ``mmd`` (标准 MMD)。

    判定必须基于源骨架本身，不能基于目标骨架或当前活动对象。KK/KKS 的
    mmd_tools INTERNAL 导入保留大量 ``cf_*`` / ``cf_s_*`` 补充骨；标准
    MMD 源通常没有这套命名空间。调用方若已经解析出源骨架应显式传入
    ``src``，否则才回退到场景扫描。
    """
    if src is None:
        arms = [o for o in bpy.data.objects
                if o.type == 'ARMATURE' and o is not tgt]
        src = max(arms, key=lambda a: len(a.data.bones)) if arms else None
    if src is None:
        print('[tex] source mode: kk (未找到源骨架，兼容旧场景默认)')
        return 'kk'
    names = [b.name.casefold() for b in src.data.bones]
    cf = sum(1 for n in names if n.startswith('cf_'))
    cfs = sum(1 for n in names if n.startswith('cf_s_'))
    # 一个偶然的同名骨不足以触发 KK 分支；补充骨链数量是稳定信号。
    mode = 'kk' if (cf >= 6 or cfs >= 3) else 'mmd'
    print('[tex] source mode: %s (cf_骨 %d, cf_s_骨 %d, 源 %s)'
          % (mode, cf, cfs, src.name))
    return mode


def _classify_mode(diffuse, has_alpha, exclude=EXCLUDE_TRANSPARENT,
                   mode='kk', alpha_profile=None, alpha_test=None,
                   material_names=None):
    """统一判定一个 diffuse 贴图的最终 mtl blend 模式。

    默认采用 GOH 原生的 alpha-test 写法处理 cutout：
    ``alpharef 127 + blend test + alphatocoverage``，不设置 PLY 0x0002。
    抗锯齿的眉毛、睫毛、眼线和瞳孔始终保留 blend/DXT5；none 仍优先保护
    身体、鞋和盔甲实体材质。

    材质语义优先于 diffuse 文件名。软 alpha 瞳孔必须保留 blend；尤其
    Eyes+/eyeblend 的透明背景不能丢弃，否则共面的眼睛底图会被黑色 RGB
    覆盖。默认不可见的 MMD 材质会在 export_all 中按常量 alpha 单独剔除。
    """
    if alpha_test is None:
        alpha_test = _goh_alpha_test_mode()
    alpha_test = bool(alpha_test)
    base = os.path.splitext(os.path.basename(diffuse or ''))[0]
    if isinstance(material_names, str):
        material_names = (material_names,)
    else:
        material_names = tuple(material_names or ())
    has_pupil_semantic = (_is_pupil_layer(base)
                          or any(_is_pupil_layer(name)
                                 for name in material_names))
    has_sclera_semantic = (_is_sclera_layer(base)
                           or any(_is_sclera_layer(name)
                                  for name in material_names))
    # Preserve pupil overlay alpha even when a shared-atlas filename otherwise
    # looks opaque. The black RGB outside the visible highlights is not a valid
    # replacement for transparency.
    if has_alpha and has_pupil_semantic:
        return 'blend'
    if base in exclude:
        return 'none'
    if has_sclera_semantic and not has_pupil_semantic:
        return 'none'
    # 2026-08-18: 硬实体 (鞋/靴/护甲) 前置 —— MMD 表的 gloss/highlight/lens 等
    # 词会命中鞋子贴图名 (如 shoes_gloss), 导致鞋子误判 blend 半透明。
    # 实体在任何模式都强制 none, 必须先于 MMD 表。
    if _kw_hit(base, FORCE_OPAQUE_HARD_KW):
        return 'none'
    # 头发/刘海/发尾必须完全不透明：MMD 头发贴图常带渐变 alpha
    # （alpha<127 的半透明像素），blend test 的 alpharef 会把这些
    # 像素整块丢弃 → 头发挖洞、后脑/脸透出来（Shinku 实测）。
    # 统一走 blend none + 黑底填充（png_to_tga fill_black 把透明
    # 区 RGB 涂成邻近不透明颜色），头发成为实心不透明材质。
    # 此判定先于 MMD 表。
    if _kw_hit(base, ('hair', 'toufa', '髪', '头发', '发', '髮',
                      'bang', 'forelock', 'maegami', 'kami', 'liuhai')):
        return 'none'
    # Brows, lashes, eyelines, and pupils are soft facial overlays. Their
    # antialiased source alpha must remain blend/DXT5 even when the global GOH
    # alpha-test preference is enabled: BC1a turns the edge into a 1-bit cutout
    # and exposes the KK texture's transparent-black RGB as broken dark seams.
    # Material semantics cover shared/generic texture filenames as well.
    if (_is_force_alpha_blend(base)
            or any(_is_force_alpha_blend(name) for name in material_names)):
        return 'blend'
    if mode == 'mmd':
        if _kw_hit(base, MMD_TEST_KW):
            return 'test'
        if _kw_hit(base, MMD_BLEND_KW):
            return 'test' if alpha_test else 'blend'
    # GFA/GOH 老流程：头发的透明区域是镂空，不应把透明 RGB
    # 当实体绘制；KK 头发也沿用 test。已知硬实体（鞋/盔甲等）
    # 仍由 FORCE_OPAQUE_HARD_KW 在前面锁定 none。
    if has_alpha and _kw_hit(base, ('hair', 'toufa', '髪', '头发')):
        return 'none'
    # bodytights/tights 的透明底在 none 下会变成黑层；在 GOH
    # alpha-test 模式中改为阈值镂空，保留实体区域且丢弃黑色透明底。
    if alpha_test and _kw_hit(base, ('tight', 'bodytights')):
        return 'test'
    if _is_force_opaque_alpha(base):
        return 'none'
    if _is_force_alpha_test(base):
        return 'test'
    # 未命名但确实带 alpha 的贴图：只有存在实质透明面积时才
    # 进入 test/blend。很多身体贴图只有少量抗锯齿边缘 alpha，不能
    # 因为通道存在就被挖空；该阈值也避免透明修复误伤实体材质。
    if has_alpha:
        profile = alpha_profile or {}
        transparent_ratio = float(profile.get('transparent_ratio', 0.0))
        partial_ratio = float(profile.get('partial_ratio', 0.0))
        min_alpha = float(profile.get('min_alpha', 1.0))
        max_alpha = float(profile.get('max_alpha', 1.0))
        substantive = (transparent_ratio > 0.01 or partial_ratio > 0.02)
        if not substantive:
            return 'none'
        if alpha_test:
            return 'test'
        if partial_ratio > 0.002 or (min_alpha > 0.0 and max_alpha < 1.0):
            return 'blend'
        return 'test'
    return 'none'


def _needs_soften(base):
    """贴图是否需要 alpha 软化。

    Alpha Test 模式严格沿用 GOH 原生贴图，不改 alpha；只有面板切回
    blend 且材质确实是紧身衣物时才保留旧的可选软化路径。
    """
    return (not _goh_alpha_test_mode()
            and _is_force_alpha_blend(base)
            and any(kw.casefold() in base.casefold() for kw in SKIN_TIGHT_KW))


_TEXTURE_SOURCE_EXTENSIONS = (
    '.png', '.tga', '.bmp', '.jpg', '.jpeg', '.dds',
    '.tif', '.tiff', '.webp', '.gif',
)
# NVTT is deliberately discovered as an external tool rather than bundled: its
# SDK license permits distribution only with NVIDIA notices and protective terms.
_NVTT_EXPORT_CANDIDATES = (
    r'C:\Program Files\NVIDIA Corporation\NVIDIA Texture Tools\nvtt_export.exe',
    r'C:\Program Files\NVIDIA Corporation\NVIDIA Texture Tools\nvcompress.exe',
)


def _find_nvtt_tool(explicit_path=None):
    """返回可用的外部 NVIDIA DDS CLI 及其类型。"""
    candidates = []
    if explicit_path:
        requested = os.path.abspath(bpy.path.abspath(explicit_path))
        if not os.path.isfile(requested):
            raise RuntimeError(_("mowas2.err.nvtt_path_missing",
                                 path=requested))
        basename = os.path.basename(requested).lower()
        if not (basename.startswith('nvtt_export') or
                basename.startswith('nvcompress')):
            raise RuntimeError(_("mowas2.err.nvtt_path_invalid",
                                 path=requested))
        kind = 'nvcompress' if basename.startswith('nvcompress') else 'nvtt_export'
        return kind, requested
    env_path = os.environ.get('NVTT_EXPORT')
    if env_path:
        candidates.append(env_path)
    candidates.extend(_NVTT_EXPORT_CANDIDATES)
    for command in ('nvtt_export.exe', 'nvtt_export',
                    'nvcompress.exe', 'nvcompress'):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(resolved)
    seen = set()
    for candidate in candidates:
        path = os.path.abspath(candidate)
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(path):
            kind = ('nvcompress' if os.path.basename(path).lower().startswith('nvcompress')
                    else 'nvtt_export')
            return kind, path
    raise RuntimeError(_("mowas2.err.nvtt_not_found"))


def _validate_legacy_dds(path, blend):
    """校验 GOH 兼容的 DXT9 DDS 头、FourCC 与 mip 链。"""
    with open(path, 'rb') as fh:
        header = fh.read(128)
    if len(header) < 128 or header[:4] != b'DDS ':
        raise RuntimeError(_("mowas2.err.dds_invalid", path=path))
    fourcc = header[84:88]
    expected = b'DXT5' if blend == 'blend' else b'DXT1'
    if fourcc != expected:
        raise RuntimeError(_(
            "mowas2.err.dds_fourcc", path=path,
            expected=expected.decode('ascii'),
            actual=fourcc.decode('ascii', errors='replace')))
    mip_count = struct.unpack_from('<I', header, 28)[0]
    if mip_count < 1:
        raise RuntimeError(_("mowas2.err.dds_no_mips", path=path))


def _compress_tga_to_dds(input_path, output_path, blend, nvtt_path=None):
    """用外部 NVTT 输出 GOH/legacy GEM2 可读的 DXT1/DXT5 DDS。"""
    tool_kind, tool_path = _find_nvtt_tool(nvtt_path)
    if os.path.isfile(output_path):
        os.remove(output_path)
    if tool_kind == 'nvtt_export':
        format_code = {'none': '15', 'test': '16', 'blend': '18'}[blend]
        args = [tool_path, '-f', format_code, '-q', 'normal', '--mips',
                '--mip-filter', 'kaiser']
        if blend == 'test':
            args.extend(('--alpha-threshold', '127', '--cutout-alpha',
                         '--scale-alpha'))
        args.extend(('-o', output_path, input_path))
    else:
        format_flag = {'none': '-bc1', 'test': '-bc1a', 'blend': '-bc3'}[blend]
        alpha_flag = '-noalpha' if blend == 'none' else '-alpha'
        args = [tool_path, '-silent', '-color', alpha_flag, '-mipfilter',
                'kaiser', format_flag, input_path, output_path]
    creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    completed = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding='utf-8', errors='replace', timeout=600,
        creationflags=creationflags)
    if completed.returncode != 0:
        if os.path.isfile(output_path):
            os.remove(output_path)
        detail = (completed.stdout or '').strip()
        raise RuntimeError(_(
            "mowas2.err.nvtt_convert_failed",
            code=completed.returncode, file=os.path.basename(input_path),
            detail=('\n' + detail) if detail else ''))
    _validate_legacy_dds(output_path, blend)
    return 'BC3/DXT5' if blend == 'blend' else ('BC1a/DXT1' if blend == 'test'
                                                else 'BC1/DXT1')


def _convert_textures(out_sub, texture_format='TGA',
                      exclude=EXCLUDE_TRANSPARENT, mode='kk', nvtt_path=None,
                      toon_shader=False, material_names_by_diffuse=None,
                      alpha_by_diffuse=None):
    """按用户选择输出 TGA 或 DDS，并让 MTL/PLY 共用同一透明分类。"""
    texture_format = str(texture_format or 'TGA').upper()
    if texture_format not in {'TGA', 'DDS'}:
        raise ValueError(_("mowas2.err.texture_format_unsupported",
                           format=texture_format))

    sources = []
    seen_bases = {}
    for filename in sorted(os.listdir(out_sub)):
        base, ext = os.path.splitext(filename)
        ext_lower = ext.lower()
        if ext_lower not in _TEXTURE_SOURCE_EXTENSIONS:
            continue
        key = base.casefold()
        previous = seen_bases.get(key)
        if previous is not None:
            raise RuntimeError(_(
                "mowas2.err.texture_duplicate", format=texture_format,
                first=previous, second=filename))
        seen_bases[key] = filename
        sources.append((filename, base, ext_lower))

    converted = 0
    retained = 0
    plan = {}
    target_ext = '.' + texture_format.lower()
    for i, (filename, base, ext_lower) in enumerate(sources, 1):
        source_path = os.path.join(out_sub, filename)
        target_path = os.path.join(out_sub, base + target_ext)
        temp_tga = os.path.join(out_sub, base + '.__gem2_texture_input.tga')
        soften = _needs_soften(base)
        semantic_names = ()
        if material_names_by_diffuse:
            semantic_names = material_names_by_diffuse.get(base)
            if semantic_names is None:
                semantic_names = material_names_by_diffuse.get(
                    base.casefold(), ())
        pupil_consumer = (_is_pupil_layer(base)
                          or any(_is_pupil_layer(name)
                                 for name in semantic_names))
        hair_mat = _kw_hit(base, ('hair', 'toufa', '髪', '头发',
                                  '发', '髮', 'bang', 'forelock',
                                  'maegami', 'kami', 'liuhai'))
        test_named = _kw_hit(base, ('tight', 'bodytights'))
        # Toon Shader's hair contract requires alpha-test. Shared pupil atlases
        # must retain their alpha/RGB even when the filename looks like hair,
        # clothing, or another normally opaque category.
        fill = (not pupil_consumer and (
                (hair_mat and not toon_shader)
                or (_is_force_opaque_alpha(base)
                    and not ((toon_shader and hair_mat)
                             or (_goh_alpha_test_mode() and test_named)))
                or _is_force_opaque_hard(base)))
        try:
            retained_source = ext_lower == target_ext
            profile_path = temp_tga if retained_source or texture_format == 'DDS' else target_path
            profile = png_to_tga(
                source_path, profile_path, soften=soften,
                fill_black=fill, return_profile=True)
            has_alpha = bool(profile.get('has_alpha'))
            if alpha_by_diffuse is not None:
                alpha_by_diffuse[base] = has_alpha
                alpha_by_diffuse[base.casefold()] = has_alpha
            blend = _classify_mode(
                base, has_alpha, exclude=exclude, mode=mode,
                alpha_profile=profile, material_names=semantic_names)
            if toon_shader and hair_mat and not pupil_consumer:
                blend = 'test'
            plan[base] = blend
            if retained_source:
                # Re-encode retained TGA sources as the canonical BGRA32
                # contract; DDS sources stay intact for the requested DDS path.
                if texture_format == 'TGA':
                    os.replace(temp_tga, source_path)
                retained += 1
                output_label = 'existing ' + texture_format
            elif texture_format == 'DDS':
                output_label = _compress_tga_to_dds(
                    temp_tga, target_path, blend, nvtt_path=nvtt_path)
                os.remove(source_path)
                converted += 1
            else:
                output_label = 'TGA/BGRA32'
                os.remove(source_path)
                converted += 1
            print('[tex] %d/%d %s -> %s (alpha=%s, transparent=%.3f, '
                  'partial=%.3f, blend=%s, soften=%s, fill=%s)'
                  % (i, len(sources), base, output_label, has_alpha,
                     profile.get('transparent_ratio', 0.0),
                     profile.get('partial_ratio', 0.0), blend, soften, fill))
        except Exception as exc:
            raise RuntimeError(_(
                "mowas2.err.texture_process_failed", format=texture_format,
                path=source_path, error=exc)) from exc
        finally:
            if os.path.isfile(temp_tga):
                os.remove(temp_tga)

    n_mtl = 0
    for filename in sorted(os.listdir(out_sub)):
        if not filename.lower().endswith('.mtl'):
            continue
        path = os.path.join(out_sub, filename)
        with open(path, 'r', encoding='utf-8') as fh:
            content = fh.read()
        match = re.search(r'\{diffuse\s+"([^"]+)"\}', content)
        if not match:
            continue
        diffuse = match.group(1)
        if diffuse in plan:
            blend = plan[diffuse]
        elif _kw_hit(diffuse, ('hair', 'toufa', '髪', '头发',
                               '发', '髮', 'bang', 'forelock',
                               'maegami', 'kami', 'liuhai')):
            blend = 'none'
        elif (_goh_alpha_test_mode()
              and _kw_hit(diffuse, ('tight', 'bodytights'))):
            blend = 'test'
        elif _is_force_opaque_alpha(diffuse):
            blend = 'none'
        elif _is_force_alpha_blend(diffuse):
            blend = 'blend'
        elif _is_force_alpha_test(diffuse):
            blend = 'test'
        else:
            blend = 'none'
        if blend == 'test':
            new = ('{material simple\n\t{diffuse "%s"}'
                   '\n\t{alpharef 127}\n\t{blend test}\n\t{alphatocoverage}\n}\n'
                   % diffuse)
        else:
            new = ('{material simple\n\t{diffuse "%s"}\n\t{blend %s}\n}\n'
                   % (diffuse, blend))
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(new)
        n_mtl += 1
    print('[tex] output:', texture_format, '| converted:', converted,
          '| retained:', retained, '| plan:', sorted(plan.items()),
          '| mtl 重写:', n_mtl)
    return plan


def convert_textures_to_tga(out_sub, exclude=EXCLUDE_TRANSPARENT, mode='kk',
                            toon_shader=False,
                            material_names_by_diffuse=None,
                            alpha_by_diffuse=None):
    """内置无依赖 TGA 工作流（默认，兼容既有导出）。"""
    return _convert_textures(
        out_sub, texture_format='TGA', exclude=exclude, mode=mode,
        toon_shader=toon_shader,
        material_names_by_diffuse=material_names_by_diffuse,
        alpha_by_diffuse=alpha_by_diffuse)


def convert_textures_to_dds(out_sub, exclude=EXCLUDE_TRANSPARENT, mode='kk',
                            nvtt_path=None, toon_shader=False,
                            material_names_by_diffuse=None,
                            alpha_by_diffuse=None):
    """可选 DDS 工作流；使用用户安装的外部 NVTT，不随插件分发。"""
    return _convert_textures(
        out_sub, texture_format='DDS', exclude=exclude, mode=mode,
        nvtt_path=nvtt_path, toon_shader=toon_shader,
        material_names_by_diffuse=material_names_by_diffuse,
        alpha_by_diffuse=alpha_by_diffuse)


# Material contract adapted from GFA_Model_Weight_Transfer/MaterialTools/
# ToonShaderConvert.py; Workshop shader assets remain external and are not copied.
_TOON_DIFFUSE_TAGS = (
    ('hair', ('HAIR', 'TOUFA', '髪', '头发', '髮', 'BANG', 'FORELOCK',
              'MAEGAMI', 'KAMI', 'LIUHAI')),
    ('face', ('FACE', 'EYE', 'HEAD')),
    ('body', ('BODY', 'SKIN')),
    ('cloth', ('SUIT', 'CLOTH', 'UNIFORM', 'PANTY', 'DRESS', 'OUTFIT',
               'GLASSES', 'GRENADE', 'WEAPON', 'SWORD')),
)
_TOON_NORMAL_REF = '$/dummyTex/normal'
_TOON_SPECULAR_REF = '$/dummyTex/black'
_TOON_HEIGHT_REF = '$/envmap/env'
_TOON_SHADER_WORKSHOP_ID = '3565678181'
_TOON_SHADER_ROOT_CANDIDATES = (
    r'D:\SteamLibrary\steamapps\workshop\content\400750\3565678181',
    r'C:\Program Files (x86)\Steam\steamapps\workshop\content\400750\3565678181',
)
_TOON_SHADER_REQUIRED_FILES = (
    r'LICENSE.txt',
    r'resource\shader\dx10\ps.hlsl',
    r'resource\shader\dx10\material.inc',
    r'resource\shader\dx10\npr\toonShading.hlsl',
    r'resource\shader\dx10\postprocess\lenseffects\outlinesNPR.hlsl',
    r'resource\texture\common\dummyTex\normal.dds',
    r'resource\texture\common\dummyTex\black.dds',
    r'resource\texture\common\envmap\env.dds',
)


def _toon_diffuse_type(diffuse):
    """复现 GFA ToonShaderConvert 的 diffuse 名称分类顺序。"""
    upper = (diffuse or '').upper()
    for material_type, tags in _TOON_DIFFUSE_TAGS:
        if any(tag in upper for tag in tags):
            return material_type
    return None


def _find_toon_shader_root():
    """检测 Toon Shader Workshop 素材；仅用于状态提示，不复制受限资产。"""
    candidates = []
    env_root = os.environ.get('GOH_TOON_SHADER_ROOT')
    if env_root:
        candidates.append(env_root)
    candidates.extend(_TOON_SHADER_ROOT_CANDIDATES)
    try:
        from .core import get_paths
        configured_paths = get_paths()
        configured = [configured_paths.get(key) or ''
                      for key in ('import', 'export')]
    except Exception:
        configured = []
    for start in configured:
        current = os.path.abspath(start) if start else ''
        for _ in range(15):
            if os.path.basename(current).casefold() == 'steamapps':
                candidates.append(os.path.join(
                    current, 'workshop', 'content', '400750',
                    _TOON_SHADER_WORKSHOP_ID))
                break
            parent = os.path.dirname(current)
            if not current or parent == current:
                break
            current = parent
    seen = set()
    for candidate in candidates:
        root = os.path.abspath(candidate)
        key = os.path.normcase(root)
        if key in seen:
            continue
        seen.add(key)
        if all(os.path.isfile(os.path.join(root, rel_path))
               for rel_path in _TOON_SHADER_REQUIRED_FILES):
            return root
    return None


def apply_toon_shader_conversion(out_sub, material_semantics=None):
    """按 GFA ToonShaderConvert 规则将已生成的 MTL 接入 Workshop Toon Shader。

    该转换只写 MTL 引用，不复制 ONCL-C 许可覆盖的 shader/贴图资产。当前
    blend/alpharef/alphatocoverage 会被保留；只有 hair 分类按原规则强制 test，
    并补齐 GOH 所需的 alpharef 127 与 alphatocoverage。眼睛保护必须同时使用
    清洗后的文件名和原始语义名，否则中文/日文材质名会被清洗成下划线，并因
    diffuse 文件名含 Eye 而被错误转换成 bump。
    """
    material_semantics = material_semantics or {}
    counts = {name: 0 for name, _tags in _TOON_DIFFUSE_TAGS}
    converted = 0
    for filename in sorted(os.listdir(out_sub)):
        if not filename.lower().endswith('.mtl'):
            continue
        path = os.path.join(out_sub, filename)
        with open(path, 'r', encoding='utf-8') as fh:
            content = fh.read()
        diffuse_match = re.search(
            r'\{diffuse\s+"([^"]+)"\}', content, re.IGNORECASE)
        if not diffuse_match:
            continue
        material_name = os.path.splitext(filename)[0]
        semantic_name = material_semantics.get(material_name, material_name)
        # GFA keeps the actual eye surfaces on material simple. Converting them
        # to bump/full_specular changes their render path and, together with
        # alpha flags, produces depth/sorting artifacts around the sclera.
        eye_names = (material_name, semantic_name)
        if any(_is_pupil_layer(name)
               or _is_sclera_layer(name)
               or _is_eye_shadow_layer(name)
               or _is_eye_lid_layer(name)
               for name in eye_names):
            continue
        material_type = _toon_diffuse_type(diffuse_match.group(1))
        if material_type is None:
            continue
        content, header_count = re.subn(
            r'\{material\s+[^\s{}]+', '{material bump', content,
            count=1, flags=re.IGNORECASE)
        if header_count != 1:
            raise RuntimeError(_("mowas2.err.mtl_header_missing",
                                 path=path))
        additions = []
        if not re.search(r'\{bump\s+', content, re.IGNORECASE):
            additions.append('\t{bump "%s"}\n' % _TOON_NORMAL_REF)
        if not re.search(r'\{specular\s+', content, re.IGNORECASE):
            additions.append('\t{specular "%s"}\n' % _TOON_SPECULAR_REF)
        if (material_type in {'body', 'cloth'} and
                not re.search(r'\{height\s+', content, re.IGNORECASE)):
            additions.append('\t{height "%s"}\n' % _TOON_HEIGHT_REF)
        if not re.search(r'\{full_specular(?:\s|})', content, re.IGNORECASE):
            additions.append('\t{full_specular}\n')
        if material_type == 'hair':
            if re.search(r'\{blend\s+', content, re.IGNORECASE):
                content = re.sub(
                    r'(\{blend\s+)[^\s{}]+', r'\1test', content,
                    count=1, flags=re.IGNORECASE)
            else:
                additions.append('\t{blend test}\n')
            if not re.search(r'\{alpharef\s+', content, re.IGNORECASE):
                additions.append('\t{alpharef 127}\n')
            if not re.search(r'\{alphatocoverage(?:\s|})', content,
                             re.IGNORECASE):
                additions.append('\t{alphatocoverage}\n')
        if additions:
            close_at = content.rfind('}')
            if close_at < 0:
                raise RuntimeError(_("mowas2.err.mtl_unclosed", path=path))
            content = content[:close_at] + ''.join(additions) + content[close_at:]
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(content)
        counts[material_type] += 1
        converted += 1
    dependency_root = _find_toon_shader_root()
    print('[toon] bump materials:', converted, '| groups:', counts,
          '| dependency:', dependency_root or ('Workshop ' + _TOON_SHADER_WORKSHOP_ID))
    return {'converted': converted, 'groups': counts,
            'dependency_root': dependency_root}


def _material_export_two_sided(material, semantic_name=None):
    """Preserve explicit/PMX double-sided intent except protected eye shells."""
    if material is None:
        return False
    probes = (semantic_name or material.name, material.name)
    protected_eye = any(
        _is_pupil_layer(name) or _is_sclera_layer(name)
        or _is_eye_shadow_layer(name) or _is_eye_lid_layer(name)
        for name in probes)
    if protected_eye:
        return False
    if 'gem2_two_sided' in material:
        return bool(material.get('gem2_two_sided'))
    mmd_material = getattr(material, 'mmd_material', None)
    return bool(mmd_material and getattr(
        mmd_material, 'is_double_sided', False))


def _validate_eye_material_contract(out_sub, material_semantics,
                                    mat_diffuse, material_plan,
                                    two_sided_mats, hidden_mats):
    """Fail export when static-hidden or soft-alpha eye semantics regress."""
    checked = []
    for material_name in hidden_mats:
        semantic = material_semantics.get(material_name, material_name)
        if material_name in mat_diffuse:
            raise RuntimeError(
                'Static alpha=0 material entered the material plan: '
                + material_name)
        path = os.path.join(out_sub, material_name + '.mtl')
        if os.path.exists(path):
            raise RuntimeError(
                'Static alpha=0 material produced an MTL: ' + path)
        checked.append((material_name, 'skipped', 'alpha=0',
                        'shadow' if _is_eye_shadow_layer(semantic)
                        else 'hidden'))

    for material_name, diffuse in mat_diffuse.items():
        semantic = material_semantics.get(material_name, material_name)
        probes = (semantic, material_name)
        pupil = any(_is_pupil_layer(name) for name in probes)
        sclera = any(_is_sclera_layer(name) for name in probes)
        shadow = any(_is_eye_shadow_layer(name) for name in probes)
        lid = any(_is_eye_lid_layer(name) for name in probes)
        if not (pupil or sclera or shadow or lid):
            continue
        path = os.path.join(out_sub, material_name + '.mtl')
        with open(path, 'r', encoding='utf-8') as handle:
            content = handle.read()
        header = re.search(r'\{material\s+([^\s{}]+)', content,
                           re.IGNORECASE)
        blend_match = re.search(r'\{blend\s+([^\s{}]+)', content,
                                re.IGNORECASE)
        shader = header.group(1).casefold() if header else ''
        blend = blend_match.group(1).casefold() if blend_match else ''
        expected = material_plan.get(material_name, 'none')
        if shader != 'simple' or blend != expected:
            raise RuntimeError(
                'Eye material contract failed for %s: '
                'shader=%s blend=%s expected=simple/%s'
                % (material_name, shader or '<missing>',
                   blend or '<missing>', expected))
        if material_name in two_sided_mats:
            raise RuntimeError(
                'Eye material must remain single-sided: ' + material_name)
        overlay = any(_is_gfa_pupil_overlay(name, diffuse)
                      for name in probes)
        if overlay and expected != 'blend':
            raise RuntimeError(
                'Eyes+/eyeblend must preserve soft-alpha blend: '
                + material_name)
        if pupil and expected == 'blend':
            dds_path = os.path.join(out_sub, diffuse + '.dds')
            if os.path.isfile(dds_path):
                with open(dds_path, 'rb') as handle:
                    header_bytes = handle.read(128)
                if len(header_bytes) < 88 or header_bytes[84:88] != b'DXT5':
                    raise RuntimeError(
                        'Blended pupil texture must use DXT5: ' + dds_path)
        if shadow and expected != 'blend':
            raise RuntimeError(
                'Visible EyeShadow must use blend blend: ' + material_name)
        checked.append((material_name, shader, blend,
                        'overlay' if overlay else
                        ('shadow' if shadow else
                         ('sclera' if sclera else
                          ('lid' if lid else 'pupil')))))
    print('[eye-contract] visible/hidden plan:', checked)
    return checked

# ═══════════════════════════════════════════════════════════════
#  PLY 游戏原生 per-vertex 格式（经 medicgirl/skin.ply 字节级校准）
# ═══════════════════════════════════════════════════════════════
D3DFVF_XYZ = 0x0002
D3DFVF_XYZB2 = 0x0008
D3DFVF_NORMAL = 0x0010
D3DFVF_TEX1 = 0x0100
D3DFVF_LASTBETA_UBYTE4 = 0x1000
GAME_VERTEX_LIMIT = 65535
MESH_FLAG_TWO_SIDED = 0x0001
MESH_FLAG_ALPHA = 0x0002
MESH_FLAG_LIGHT = 0x0004
MESH_FLAG_SKINNED = 0x0010
MESH_FLAG_MATERIAL = 0x0400
MESH_FLAG_SUBSKIN = 0x0800

pack_I = struct.Struct("I").pack
pack_H = struct.Struct("H").pack
pack_HHH = struct.Struct("HHH").pack
pack_f = struct.Struct("f").pack
pack_ff = struct.Struct("ff").pack
pack_fff = struct.Struct("fff").pack
pack_B = struct.Struct("B").pack
pack_BBBB = struct.Struct("BBBB").pack

# GOH 版 2026-08-18: TGT_VG_ORDER = GOH 原版皮肤 skin.ply 的 SKIN 骨顺序
# (agit_yelan 实测, 与 GFA TargetSkeleton 一致): head 在 hand_rot1l 之后,
# palm 骨 R 系在前 L 系在后。与 MOWAS2 版 (head 在最后、palm L 在前) 不同。
TGT_VG_ORDER = ['body', 'foot1l', 'foot2l', 'foot3l', 'foot1r', 'foot2r',
                'foot3r', 'ik_leftright', 'ik_updown', 'clavicle_left',
                'hand1l', 'hand2l', 'hand_rot1l', 'head', 'clavicle_right',
                'hand1r', 'hand2r', 'hand_rot1r', 'palm1r', 'palm2r',
                'palm3r', 'palm1l', 'palm2l', 'palm3l']


def _weighted_skin_group_names(mesh_obj):
    weighted = set()
    for vertex in mesh_obj.data.vertices:
        for assignment in vertex.groups:
            if float(assignment.weight) > 1.0e-8:
                if 0 <= assignment.group < len(mesh_obj.vertex_groups):
                    weighted.add(mesh_obj.vertex_groups[assignment.group].name)
    return weighted


def _export_skin_names(mesh_obj):
    """Return the effective SKIN order, honoring rest-converter metadata."""
    current = [group.name for group in mesh_obj.vertex_groups]
    configured = mesh_obj.get('gem2_skin_order')
    if isinstance(configured, str):
        try:
            configured = json.loads(configured)
        except (TypeError, ValueError):
            configured = None
    if not isinstance(configured, (list, tuple)):
        return current
    result = []
    current_set = set(current)
    # The hint may intentionally contain the complete destination skeleton;
    # only groups with positive assignments belong in this mesh's SKIN palette.
    weighted = _weighted_skin_group_names(mesh_obj)
    for value in configured:
        name = str(value)
        if (name in current_set and name in weighted
                and name not in result):
            result.append(name)
    # Keep newly-added weighted groups, but never put empty helper groups into
    # a destination SKIN palette merely because Blender retains the group.
    result.extend(name for name in current
                  if name not in result and name in weighted)
    return result


def _export_skin_slot_map(mesh_obj):
    names = _export_skin_names(mesh_obj)
    return names, {name: index + 1 for index, name in enumerate(names)}


def _sanitize_name(name):
    return re.sub(r'[^A-Za-z0-9_\-\.]', '_', name)


_ENTITY_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
_WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(('COM%d' % i) for i in range(1, 10)),
    *(('LPT%d' % i) for i in range(1, 10)),
}


def _validate_entity_name(name):
    """Return a filesystem/GEM resource-safe entity basename."""
    value = str(name or '').strip()
    if (not _ENTITY_NAME_RE.fullmatch(value) or
            value.upper() in _WINDOWS_RESERVED_NAMES):
        raise ValueError(_("mowas2.err.entity_name_invalid", name=value))
    return value


def _entity_output_dir(output_root, entity_name):
    """Always isolate cleanup inside <output root>/<entity name>."""
    root = os.path.abspath(bpy.path.abspath(output_root))
    return os.path.join(root, entity_name)


def _assert_output_does_not_delete_source_textures(out_sub, mesh):
    output_key = os.path.normcase(os.path.abspath(out_sub))
    for material in mesh.data.materials:
        if not material or not material.use_nodes or not material.node_tree:
            continue
        for image in iter_material_images(material):
            source = resolved_image_path(image)
            if not source or not os.path.isfile(source):
                continue
            source_key = os.path.normcase(os.path.abspath(source))
            try:
                inside_output = os.path.commonpath((output_key, source_key)) == output_key
            except ValueError:
                inside_output = False
            if inside_output:
                raise RuntimeError(_(
                    "mowas2.err.output_overwrites_source_texture", path=source))


def sanitize_materials():
    renamed = {}
    for mat in list(bpy.data.materials):
        new = _sanitize_name(mat.name)
        if new != mat.name:
            i = 1
            base = new
            while new in bpy.data.materials and bpy.data.materials[new] != mat:
                i += 1
                new = base + '_%d' % i
            renamed[mat.name] = new
            mat.name = new
    return renamed


def _material_diffuse_image(mat):
    """Return the resolved diffuse image, including packed/generated images."""
    return resolve_material_image(mat, 'diffuse')


def _image_has_staging_payload(image):
    """Return whether an image has a readable file, packed bytes, or pixels."""
    if image is None:
        return False
    packed = getattr(image, 'packed_file', None)
    if packed is not None:
        try:
            if bytes(packed.data):
                return True
        except (AttributeError, TypeError, ValueError, RuntimeError):
            pass
    source = resolved_image_path(image)
    if source and os.path.isfile(source):
        return True
    try:
        width, height = image.size
        return bool(getattr(image, 'has_data', False)
                    and int(width) > 0 and int(height) > 0)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return False


def _stage_pipeline_diffuse(texture_stager, material):
    """Stage a diffuse image, retaining the legacy no-payload fallback.

    The original one-click PMX exporter ignored stale image paths and emitted the
    material-name diffuse token. Keep that behavior only when an image has no
    file, packed payload, or Blender pixel buffer. Real staging failures for
    exportable images remain fatal.
    """
    image = _material_diffuse_image(material)
    if image is None:
        return None, None
    if not _image_has_staging_payload(image):
        return None, {
            'material': material.name,
            'image': image.name,
            'source': resolved_image_path(image) or '<no filepath>',
        }
    return texture_stager.stage(image, source_label=material.name), None


def _material_diffuse_path(mat):
    """Return a readable external diffuse path when one exists.

    Export code uses :func:`_material_diffuse_image` so an empty filepath does
    not discard a packed or generated Blender image. This path-only helper is
    retained for diagnostics and compatibility with older callers.
    """
    image = _material_diffuse_image(mat)
    path = resolved_image_path(image)
    return path if path and os.path.isfile(path) else None


# ═══════════════════════════════════════════════════════════════
#  骨骼映射（bone_mapping_v2 三层链 + 修正）
# ═══════════════════════════════════════════════════════════════
from .bone_mapping_v2 import resolve_pmx_bone, GFA_TO_GEM2_TARGET


# ═══════════════════════════════════════════════════════════════
#  GOH 版手部权重模式 (2026-08-18)
#  ───────────────────────────────────────────────────────────────
#  MOWAS2 版手部强制单骨 (HAND_SINGLE): medicgirl 原版皮肤手部 = palm1l
#  单骨 1.0, 双骨混合在持枪/背枪姿态被拉散 —— 那是对 MOWAS2 骨架/皮肤的
#  正确适配。
#  GOH 版还原 GFA 流程: GOH 原版皮肤 (agit_yelan/agf_feitusa) 手部实测是
#  palm1/palm2 分指混合 (0.53/0.47, 93% 顶点多骨), GFA 权重表
#  Step3_TransferWeightFinal 的手指条目也是分指 (Finger1: palm1 0.50/
#  palm2 0.425/palm3 0.075)。GOH 骨架手部 IK/FK (palm_ik_holder/ik_chain/
#  hand3) 驱动 palm1/2/3 分指, 单骨会让手指不跟随 → 手部 IK/FK 对不上。
#  因此在 GOH 版:
#    * bone_mapping_v2 手部 = GFA 分指表 (已改);
#    * bind_and_transfer 跳过 HAND_SINGLE 强制单骨 (保持分指);
#    * Wrist 不再特判 hand_rot1, 按 GFA 表去 palm1 (GOH 原版 hand_rot1
#      只有 64/12 个过渡顶点, palm1 才是主骨)。
GOH_HAND_SPLIT = True   # True=GFA 分指 (GOH 默认); False=MOWAS2 单骨兼容

# 2026-08-18 (v10 实验): 禁用 pose_hands 的肘弯 (elbow_cap=50°)。
# 根因假设: GOH/GFA 目标骨架手臂骨骼本身是直的 (实测 goh_skin 与 agf
# 骨架 hand1→hand2→hand_rot1 肘角≈0.4°), 而 GFA 原版皮肤网格肘角只有
# 4.5~8.8° (近直); 但 pose_hands 为补偿"源手比目标长"强行弯肘 ≤50°,
# freeze 后网格手臂就弯 20°+ → 比 GFA 原版弯 4~5 倍。
# True = 跳过 pose_hands 肘弯 (只保留腕屈), 手/臂长度差交给
# retarget_arm_segments 按骨轴比例重映射。
GOH_SKIP_POSE_ELBOW = True

# 2026-08-18 (v10): 【长臂模板 + 源骨骼分段缩放】。用户对比 GFA 原版模型发现
# "手臂差距相当大、整条手臂像黏在一起的一条弧线、没有上臂-前臂-手掌层次、
# 肩部没被拉伸" —— 批量扫描 GFA mod 122 个角色骨架发现两套模板:
#   * 长臂模板 (agf_*/acb_*/akq_*/agi_lei, 占 78%): 前臂(hand1→hand2)=6.268,
#     手(hand2→hand_rot1)=5.257/5.192 —— 与 GOH 动画 hand2l=6.268 一致
#   * 短臂模板 (agit_*/aba_*/agi_*, 占 22%): 前臂 5.0~5.5、手 4.0~4.6
# 我们 samples/goh_skin.mdl = agit_yelan 复制品 = 短臂模板 → 手臂短 15%+。
# GFA 程序 (Step2_BoneAlignment.py) 的真相: 它对【源骨骼】做逐段长度缩放
# (AlignBoneLength: 上臂 Arm→Elbow 缩放到 GOH Hand1→Hand2、前臂 Elbow→Wrist
# 缩放到 Hand2→Hand_rot1) + 整体肩高缩放 + 肩宽 WidthExtraScaling, 源网格
# 跟随源骨骼形变 → 自然的"上臂-前臂-手掌"层次。我们只旋转不对齐长度。
# True = 用长臂模板骨架 (samples/goh_skin_gfa.mdl) + 对齐后对源骨骼做
# GFA 式分段长度缩放 (goh_align_bone_lengths); False = 旧 goh_skin.mdl。
GOH_GFA_LONGARM = True

# GFA Step0.5 头归一化 (还原 GFA 流程): 用 GFA 固定期望比例
# (EyeLR 0.041682486 / EyeNeck 0.1070825) 缩放源 Neck。False = 执行
# goh_normalize_head 完整版 (与 GFA Step0.5 一致)。
GOH_GFA_SKIP_HEAD_NORM = False

# ═══════════════════════════════════════════════════════════════

def _goh_hand_split():
    """运行时读取手部分指开关（场景属性优先，其次模块常量）。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            return bool(sc.mowas2_props.goh_hand_split)
    except Exception:
        pass
    return GOH_HAND_SPLIT


def _goh_gfa_longarm():
    """运行时读取长臂模板+分段缩放开关（场景属性优先，其次模块常量）。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            if hasattr(sc.mowas2_props, 'goh_gfa_longarm'):
                return bool(sc.mowas2_props.goh_gfa_longarm)
    except Exception:
        pass
    return GOH_GFA_LONGARM


def _goh_enlarge_head():
    """读取 N 面板的可选头部放大开关。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            return bool(getattr(sc.mowas2_props, 'goh_enlarge_head', False))
    except Exception:
        pass
    return False


def _goh_head_scale():
    """读取头部放大倍率，限制在 0.9~1.5 的可调范围（无硬性上限，
    1.25 上限只是原 UI 的保守取值，Blender 的 pose scale 无限制）。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            value = float(getattr(sc.mowas2_props, 'goh_head_scale', 1.06))
            return max(0.9, min(1.5, value))
    except Exception:
        pass
    return 1.06


def _goh_head_neck_follow():
    """头部倍率对颈部和同网格颈饰的径向跟随比例。0=不跟随，1=完全跟随。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            value = float(getattr(sc.mowas2_props,
                                  'goh_head_neck_follow', 0.0))
            return max(0.0, min(1.0, value))
    except Exception:
        pass
    return 0.0


def _goh_alpha_test_mode():
    """读取透明材质模式；True=GOH alpharef/blend test。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            return bool(getattr(sc.mowas2_props,
                                'goh_alpha_test',
                                GOH_ALPHA_TEST_TRANSPARENT))
    except Exception:
        pass
    return GOH_ALPHA_TEST_TRANSPARENT


def _goh_fix_pupil_depth():
    """是否自动把陷入眼白或与眼白近共面的瞳孔层推出。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            return bool(getattr(sc.mowas2_props,
                                'goh_fix_pupil_depth', True))
    except Exception:
        pass
    return True


def _goh_pupil_clearance():
    """瞳孔表面相对眼白的最小间距（GOH 模型空间单位）。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            value = float(getattr(sc.mowas2_props,
                                  'goh_pupil_clearance', 0.006))
            return max(0.0, min(0.1, value))
    except Exception:
        pass
    return 0.006


def _goh_ik_updown_enabled():
    """读取 ik_updown 对应 UpperBody2 区域的额外缩放开关。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            return bool(getattr(sc.mowas2_props,
                                'goh_ik_updown_scale',
                                GOH_IK_UPDOWN_SCALE))
    except Exception:
        pass
    return GOH_IK_UPDOWN_SCALE


def _goh_ik_updown_multiplier():
    """读取 GFA 自动基准之上的 ik_updown 区域倍率。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            value = float(getattr(sc.mowas2_props,
                                  'goh_ik_updown_multiplier',
                                  GOH_IK_UPDOWN_MULTIPLIER))
            return max(0.5, min(1.8, value))
    except Exception:
        pass
    return GOH_IK_UPDOWN_MULTIPLIER


def _goh_foot_scale():
    """读取脚部尺寸倍率。1.0 = 保持当前贴地压缩行为。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            value = float(getattr(sc.mowas2_props, 'goh_foot_scale', 1.0))
            return max(0.7, min(2.0, value))
    except Exception:
        pass
    return 1.0


def _goh_shoulder_scale():
    """读取旧版视觉肩宽；v4 仅用于识别/迁移旧快照。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            value = float(getattr(sc.mowas2_props,
                                  'goh_shoulder_scale', 1.0))
            return max(0.5, min(1.3, value))
    except Exception:
        pass
    return 1.0


def _goh_foot1_spacing():
    """读取 foot1 左右腿根的视觉间距倍率；目标 rest 始终不变。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            value = float(getattr(sc.mowas2_props,
                                  'goh_foot1_spacing', 1.0))
            return max(0.8, min(1.3, value))
    except Exception:
        pass
    return 1.0


def _goh_arm_span_scale():
    """读取整臂横向间距倍率；小于 1 时左右整臂刚性向中心平移。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            value = float(getattr(sc.mowas2_props,
                                  'goh_arm_span_scale', 1.0))
            return max(0.75, min(1.1, value))
    except Exception:
        pass
    return 1.0


def _goh_torso_ik_merge():
    """读取腰腹 IK 降低影响开关：把 ik_leftright 权重按保留比例并入
    ik_updown，缓解 90° 弯腰时双骨交界拉伸（GOH 原版躯干几乎全挂
    ik_updown，ik_leftright 仅 0-3.5%）。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            return bool(getattr(sc.mowas2_props,
                                'goh_torso_ik_merge', False))
    except Exception:
        pass
    return False


def _goh_iklr_keep():
    """ik_leftright 保留比例：1.0=完全不改，0.05=几乎全并入 ik_updown。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            value = float(getattr(sc.mowas2_props, 'goh_iklr_keep', 0.5))
            return max(0.05, min(1.0, value))
    except Exception:
        pass
    return 0.5


def _goh_hand_clamp():
    """读取手掌链比值钳制开关：部分模型手链过度拉伸会把指根
    从腕部推开导致腕掌断开；开启后按 GOH_HAND_RATIO_CLAMP 钳制。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            return bool(getattr(sc.mowas2_props,
                                'goh_hand_clamp', False))
    except Exception:
        pass
    return False


def _goh_wrist_stitch():
    """GF2/MMD 腕锚复位与 hand_rot1 权重过渡总开关。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            return bool(getattr(sc.mowas2_props,
                                'goh_wrist_stitch', True))
    except Exception:
        pass
    return True


def _goh_finger_curl():
    """标准 MMD 源的手指内握卷曲开关。MMD 手指只有 2 节，统一应用
    GFA 40° 卷曲在一些模型上会外翻（Shinku 实测），默认关闭；
    需要内握的模型可手动开启。KK/KKS 分支不受此开关影响。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            return bool(getattr(sc.mowas2_props,
                                'goh_finger_curl', False))
    except Exception:
        pass
    return False


def _mirror_swap(name):
    """镜像源 L/R 换名。必须带下划线判断后缀，否则 ik_leftright 被误伤。"""
    if name.endswith('_left'):
        return name[:-5] + '_right'
    if name.endswith('_right'):
        return name[:-6] + '_left'
    if len(name) > 1 and name[-1] in 'lr' and name[-2].isdigit():
        return name[:-1] + ('r' if name[-1] == 'l' else 'l')
    return name


def pmx_vg_to_gem2(pmx_name, mirrored):
    # 手铐配件: 源模型手铐挂 GEM2 腕部 slot 骨 ca_slot06/07 (世界 Y±11.8,
    # 与 Wrist_L/R 同点 = 手腕位置), 无 GFA 映射 → 必须先于 gfa 判空处理,
    # 否则骨铸造会把它们上卷到最近映射祖先前臂 hand2 → 手铐不随腕关节转
    # (弯曲姿态下手铐留在前臂、与手分离)。直接映射到腕关节 hand_rot1
    # (与 Wrist 同款, 镜像换名)。实测: ca_slot06=左腕(+Y), ca_slot07=右腕(-Y),
    # 仅手铐材质使用, 零副作用。
    if pmx_name == 'ca_slot06':
        return [('hand_rot1r' if mirrored else 'hand_rot1l', 1.0)]
    if pmx_name == 'ca_slot07':
        return [('hand_rot1l' if mirrored else 'hand_rot1r', 1.0)]
    gfa = resolve_pmx_bone(pmx_name)
    if not gfa:
        return None
    _split = _goh_hand_split()
    # 腕骨：MOWAS2 版腕关节是 hand_rot1（样板 SKIN 中实际收权重的骨）。
    # GOH 版 (GOH_HAND_SPLIT): 原版 GOH 皮肤手部 palm1 才是主权重骨
    # (hand_rot1 仅 64/12 过渡顶点), GFA 表 Wrist→Palm1 → 走映射表 palm1
    # (不特判), 与 GOH 原版皮肤一致。
    if gfa == 'Wrist_L' and not _split:
        return [('hand_rot1r' if mirrored else 'hand_rot1l', 1.0)]
    if gfa == 'Wrist_R' and not _split:
        return [('hand_rot1l' if mirrored else 'hand_rot1r', 1.0)]
    targets = GFA_TO_GEM2_TARGET.get(gfa)
    if not targets:
        return None
    out = []
    for gem2_name, w in targets:
        if mirrored:
            gem2_name = _mirror_swap(gem2_name)
        out.append((gem2_name, w))
    return out


def is_source_mirrored(src, tgt):
    """源 Eye_L 与目标左脚(foot1l)异侧 ⇒ 源镜像（L/R 需换名）。"""
    def wpos(arm, name):
        return arm.matrix_world @ arm.pose.bones[name].head
    try:
        eye_l = wpos(src, 'Eye_L')
        foot_l = wpos(tgt, 'foot1l')
        return (eye_l.y > 0) != (foot_l.y > 0)
    except KeyError:
        return False


# ═══════════════════════════════════════════════════════════════
#  PLY 导出（游戏原生 per-vertex 格式，单文件多 MESH 块）
# ═══════════════════════════════════════════════════════════════
FACE_NORMAL_KW = ('face', 'kao', 'skinface', 'skin_face')


def _build_game_normals(mesh, loop_tris, log=True):
    """Build per-material face-smoothing overrides without changing topology.

    Non-face loops keep their imported custom normals. Face/kao materials receive
    an area-weighted normal shared only by coincident vertices in that material,
    preventing both facial triangle artifacts and cross-material light leakage.
    """
    tol = 1e-6

    def pos_key(co):
        return (int(round(float(co.x) / tol)),
                int(round(float(co.y) / tol)),
                int(round(float(co.z) / tol)))

    accum = {}
    face_materials = set()
    for tri in loop_tris:
        mi = tri.material_index
        if mi >= len(mesh.materials) or not mesh.materials[mi]:
            continue
        material = mesh.materials[mi]
        semantic = str(material.get(
            'mowas2_material_semantic_name', material.name)).lower()
        if not any(keyword in semantic for keyword in FACE_NORMAL_KW):
            continue
        face_materials.add(mi)
        a, b, c = (mesh.vertices[tri.vertices[0]].co,
                   mesh.vertices[tri.vertices[1]].co,
                   mesh.vertices[tri.vertices[2]].co)
        weighted = (b - a).cross(c - a)
        if weighted.length_squared <= 1e-16:
            continue
        for vertex_index in tri.vertices:
            key = (mi, pos_key(mesh.vertices[vertex_index].co))
            if key in accum:
                accum[key] += weighted
            else:
                accum[key] = weighted.copy()

    overrides = {}
    for tri in loop_tris:
        if tri.material_index not in face_materials:
            continue
        for vertex_index in tri.vertices:
            key = (tri.material_index,
                   pos_key(mesh.vertices[vertex_index].co))
            normal = accum.get(key)
            if normal is not None and normal.length_squared > 1e-16:
                overrides[(tri.material_index, int(vertex_index))] = \
                    normal.normalized()
    if log:
        print('[normals] face weighted overrides:', len(overrides))
    return overrides


def _mesh_parent_local_inverse(arm_obj):
    """Return the MDL VolumeView-parent LOCAL→PLY inverse transform.

    Skinned GEM2 vertices are serialized in skeleton/model space with only the
    VolumeView node's *local* attachment removed. Ancestor transforms such as
    the GFA ``basis`` mirror apply to the skeleton and mesh together at runtime;
    baking that mirror into the PLY swaps every left/right vertex while its bone
    slot remains unchanged. Native ``agf_nijita`` confirms the contract: left
    weighted vertices are +Y, right weighted vertices are -Y, while ``skin`` is
    a direct child of the mirrored basis and locally only translates +0.065429Y.
    """
    parent_name = arm_obj.get('gem2_mesh_parent') if arm_obj else None
    raw_mats = arm_obj.get('gem2_world_mats') if arm_obj else None
    raw_parents = arm_obj.get('gem2_parents') if arm_obj else None
    try:
        world_mats = json.loads(raw_mats) if isinstance(raw_mats, str) else raw_mats
        parents = (json.loads(raw_parents)
                   if isinstance(raw_parents, str) else raw_parents)
        rows = world_mats.get(parent_name) if world_mats and parent_name else None
        if rows and parents is not None:
            mesh_world = Matrix(rows)
            ancestor_name = parents.get(parent_name)
            if ancestor_name and ancestor_name in world_mats:
                local = Matrix(world_mats[ancestor_name]).inverted() @ mesh_world
            else:
                local = mesh_world
            if abs(local.to_3x3().determinant()) > 1e-8:
                return local.inverted(), parent_name
    except Exception as exc:
        print('[export] invalid gem2 mesh-parent metadata:', exc)

    mdl_path = arm_obj.get('gem2_mdl_path') if arm_obj else None
    if mdl_path and os.path.isfile(mdl_path):
        try:
            from .ply_io import _parse_bones_flat, _precompute_bone_world_mats
            with open(mdl_path, 'r', encoding='utf-8', errors='ignore') as handle:
                bones, parsed_parent = _parse_bones_flat(handle.read())
            parsed_mats = _precompute_bone_world_mats(bones)
            if parsed_parent in parsed_mats:
                mesh_world = parsed_mats[parsed_parent]
                ancestor_name = bones[parsed_parent].get('parent')
                if ancestor_name and ancestor_name in parsed_mats:
                    local = parsed_mats[ancestor_name].inverted() @ mesh_world
                else:
                    local = mesh_world
                return local.inverted(), parsed_parent
        except Exception as exc:
            print('[export] target MDL mesh-parent parse failed:', exc)

    raise RuntimeError(_("mowas2.err.mesh_parent_matrix_missing"))


def _build_game_export_data(mesh_obj, arm_obj, skip_mats=None, log=False,
                            preserve_source_splits=False):
    """Build the indexed 40-byte records consumed by ``export_ply_game``.

    Coincident source vertices may share one stable skin-weight prefix, but each
    triangle corner keeps its imported custom loop normal and UV. Reusing the
    representative vertex normal corrupts layered hair/card shading even when the
    positions and UVs are identical. This keeps Blender topology untouched while
    deduplicating only genuinely identical final GEM2 records. Set
    ``preserve_source_splits`` only for diagnostic exports that must also retain
    every imported split weight.
    """
    mesh = mesh_obj.data
    mesh.update()
    mesh.calc_loop_triangles()
    loop_tris = mesh.loop_triangles
    invalid_slots = sorted({
        int(tri.material_index) for tri in loop_tris
        if (tri.material_index >= len(mesh.materials)
            or mesh.materials[tri.material_index] is None)
    })
    if invalid_slots:
        raise RuntimeError(
            'PLY export has triangles assigned to null material slots: '
            + ', '.join(map(str, invalid_slots)))
    skin_names, slot_by_name = _export_skin_slot_map(mesh_obj)
    if len(skin_names) > 254:
        raise RuntimeError(
            'PLY export supports at most 254 skin groups plus palette slot 0')

    skip_mats = set(skip_mats or ())
    skip_indices = {
        index for index, material in enumerate(mesh.materials)
        if material and material.name in skip_mats
    }
    visible_loop_tris = [
        tri for tri in loop_tris if tri.material_index not in skip_indices
    ]
    if not visible_loop_tris:
        raise RuntimeError('No visible material triangles remain for PLY export')
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        raise RuntimeError(_("mowas2.err.no_uv_layer"))

    game_normals = _build_game_normals(mesh, visible_loop_tris, log=log)
    parent_inv, parent_name = _mesh_parent_local_inverse(arm_obj)
    mesh_to_ply = parent_inv @ mesh_obj.matrix_world
    normal_to_ply = mesh_to_ply.to_3x3().inverted().transposed()

    vertex_positions = [mesh_to_ply @ vertex.co for vertex in mesh.vertices]
    position_bytes = [pack_fff(pos.x, pos.y, pos.z)
                      for pos in vertex_positions]
    if preserve_source_splits:
        representative_indices = list(range(len(mesh.vertices)))
    else:
        representative_by_position = {}
        representative_indices = []
        for vertex_index, key in enumerate(position_bytes):
            representative_indices.append(
                representative_by_position.setdefault(key, vertex_index))

    vertex_prefixes = []
    for vertex_index, _vertex in enumerate(mesh.vertices):
        representative = representative_indices[vertex_index]
        weight_vertex = mesh.vertices[representative]
        groups = sorted(weight_vertex.groups,
                        key=lambda group: -group.weight)[:2]
        groups = [group for group in groups if group.weight > 1e-6]
        prefix = bytearray(position_bytes[vertex_index])
        if not groups:
            prefix.extend(pack_f(1.0))
            prefix.extend(pack_BBBB(0, 0, 0, 0))
        elif len(groups) == 1:
            group_name = mesh_obj.vertex_groups[groups[0].group].name
            slot = slot_by_name.get(group_name)
            if slot is None or slot > 255:
                raise RuntimeError('PLY skin group index exceeds uint8')
            prefix.extend(pack_f(1.0))
            prefix.extend(pack_BBBB(slot, 0, 0, 0))
        else:
            group_names = [mesh_obj.vertex_groups[group.group].name
                           for group in groups[:2]]
            slots = tuple(slot_by_name.get(name, 0) for name in group_names)
            if not slots[0] or not slots[1] or max(slots) > 255:
                raise RuntimeError('PLY skin group index exceeds uint8')
            total = groups[0].weight + groups[1].weight
            prefix.extend(pack_f(groups[0].weight / total))
            prefix.extend(pack_BBBB(slots[0], slots[1], 0, 0))
        vertex_prefixes.append(bytes(prefix))

    # ── 重合层分类（max-compat 保高光方案）──────────────────────────
    # 二游/KK 源常把头发/眼睛高光做成"与 base 逐点重合、但用不同贴图(_Dhi/高光hi)
    # 的第二层贴片"。GEM2 单遍深度渲染下两层同深度 → z-fighting 斑点；导出去重把它
    # 们塌成同一批记录 → Blender validate 又把高光面当重复删掉（高光丢失）。
    # 方案：把重合层分两类处理——
    #   · 同 diffuse 贴图（toufa/toufa_Copy 这类纯复制）→ 删除（无损，纯 z-fight 垃圾）
    #   · 不同 diffuse 贴图（Hair_D / Hair_Dhi 高光）→ 保留，并沿法线外移极小 shell，
    #     使其脱离同深度：不再 z-fight、也不被 validate 当重复删 → 高光在游戏内和
    #     重导入都保住。shell 深度按重合簇内出现次序递增（base=0），避免多层互相打架。
    from .texture_export import resolve_material_image as _resolve_diffuse

    def _diffuse_stem(material_index):
        material = mesh.materials[material_index]
        if not material:
            return ''
        try:
            image = _resolve_diffuse(material, 'diffuse')
        except Exception:
            image = None
        name = image.name if image else ''
        return os.path.splitext(name)[0].casefold()

    _HL_TOKENS = ('dhi', '高光', 'hi li', 'highlight', 'hilight', '_hi',
                  'hi2', 'eyehi', 'rim', 'outline', '描边', 'copy')

    def _is_highlight(material_index):
        material = mesh.materials[material_index]
        name = (material.name if material else '').casefold()
        stem = _diffuse_stem(material_index)
        if name.endswith('+') or name.endswith('+2') or '+' in name:
            return True
        return any(token in name or token in stem for token in _HL_TOKENS)

    mat_diffuse = {}
    mat_is_hl = {}
    for mi in range(len(mesh.materials)):
        mat_diffuse[mi] = _diffuse_stem(mi)
        mat_is_hl[mi] = _is_highlight(mi)

    shell_by_ordinal = {}   # tri ordinal -> shell depth (0 = base, no offset)
    drop_ordinals = set()   # tri ordinals dropped as pure duplicates
    if not preserve_source_splits:
        from collections import defaultdict as _dd
        clusters = _dd(list)
        for ordinal, tri in enumerate(visible_loop_tris):
            gkey = tuple(sorted(
                (round(vertex_positions[int(v)].x, 4),
                 round(vertex_positions[int(v)].y, 4),
                 round(vertex_positions[int(v)].z, 4))
                for v in tri.vertices))
            clusters[gkey].append(ordinal)
        def _discriminator(material_index):
            # Prefer the diffuse texture stem (真实内容判据). When the texture
            # cannot be resolved (moved/packed), fall back to the material name
            # with Blender's ``.001`` dedup suffix stripped, so a highlight is
            # NEVER wrongly dropped as a pure duplicate — under max-compat, over-
            # keeping (shelling) is safe, dropping a distinct layer is not.
            stem = mat_diffuse.get(material_index, '')
            if stem:
                return stem
            material = (mesh.materials[material_index]
                        if 0 <= material_index < len(mesh.materials) else None)
            name = (material.name if material else '').casefold()
            return re.sub(r'\.\d{3}$', '', name)

        for gkey, ordinals in clusters.items():
            if len(ordinals) < 2:
                continue
            ordered = sorted(
                ordinals,
                key=lambda o: (1 if mat_is_hl[visible_loop_tris[o].material_index]
                               else 0,
                               visible_loop_tris[o].material_index, o))
            seen_disc = {}
            next_depth = 0
            for o in ordered:
                mi = visible_loop_tris[o].material_index
                disc = _discriminator(mi)
                if disc in seen_disc:
                    drop_ordinals.add(o)      # same real content = pure duplicate
                    continue
                seen_disc[disc] = next_depth
                if next_depth:
                    shell_by_ordinal[o] = next_depth
                next_depth += 1

    # Shell offset scaled to model size so it stays sub-pixel visually yet
    # exceeds the game's depth-buffer resolution to defeat z-fighting.
    if vertex_positions:
        xs = [p.x for p in vertex_positions]
        ys = [p.y for p in vertex_positions]
        zs = [p.z for p in vertex_positions]
        diag = max(1e-6, ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2
                          + (max(zs) - min(zs)) ** 2) ** 0.5)
    else:
        diag = 1.0
    shell_epsilon = diag * 2.0e-4

    vertex_records = []
    record_positions = []
    vertex_lookup = {}
    tris_by_mat = [[] for _ in mesh.materials]
    removed_faces = 0
    shell_faces = 0
    for ordinal, tri in enumerate(visible_loop_tris):
        if ordinal in drop_ordinals:
            removed_faces += 1
            continue
        depth = shell_by_ordinal.get(ordinal, 0)
        if depth:
            shell_faces += 1
        output_triangle = []
        for loop_index, vertex_index in zip(tri.loops, tri.vertices):
            vertex_index = int(vertex_index)
            fallback_normal = mesh.loops[loop_index].normal
            source_normal = game_normals.get(
                (tri.material_index, vertex_index), fallback_normal)
            normal = normal_to_ply @ source_normal
            if normal.length_squared > 1e-20:
                normal.normalize()
            if depth:
                offset = normal * (shell_epsilon * depth)
                pos = vertex_positions[vertex_index] + offset
                prefix = bytearray(pack_fff(pos.x, pos.y, pos.z))
                prefix.extend(vertex_prefixes[vertex_index][12:])
                prefix = bytes(prefix)
                out_pos = pos
            else:
                prefix = vertex_prefixes[vertex_index]
                out_pos = vertex_positions[vertex_index]
            uv = uv_layer.data[loop_index].uv
            record = (prefix
                      + pack_fff(normal.x, normal.y, normal.z)
                      + pack_ff(float(uv.x), 1.0 - float(uv.y)))
            output_index = vertex_lookup.get(record)
            if output_index is None:
                output_index = len(vertex_records)
                vertex_lookup[record] = output_index
                vertex_records.append(record)
                record_positions.append(out_pos)
            output_triangle.append(output_index)
        tris_by_mat[tri.material_index].append(tuple(output_triangle))

    if log and (removed_faces or shell_faces):
        print('[export] coincident-layer handling: dropped %d pure-duplicate '
              'faces (same diffuse), shelled %d highlight faces (different '
              'diffuse, offset %.5f along normal)'
              % (removed_faces, shell_faces, shell_epsilon))

    active_material_indices = [
        index for index, triangles in enumerate(tris_by_mat) if triangles
    ]
    if log:
        print('[export] mesh parent:', parent_name, '| mesh_to_ply:', mesh_to_ply)
        mode = ('source-split diagnostic' if preserve_source_splits
                else 'compact game')
        print('[export] exact 40-byte records (%s):' % mode,
              len(mesh.vertices), 'source ->', len(vertex_records), 'output')
    return {
        'mesh': mesh,
        'mesh_to_ply': mesh_to_ply,
        'parent_name': parent_name,
        'skip_indices': skip_indices,
        'records': vertex_records,
        'positions': record_positions,
        'tris_by_mat': tris_by_mat,
        'active_material_indices': active_material_indices,
        'skin_names': skin_names,
    }


_AUTO_SPLIT_PREFERRED_KW = (
    'hair', 'toufa', '髪', '头发', 'ponytail', 'bang', 'braid',
    'hat', 'helmet', 'ribbon', 'flower', 'ornament', 'accessory',
)
_AUTO_SPLIT_BLOCKED_KW = (
    'face', 'skin', 'body', 'eye', 'mouth', 'tooth', 'teeth', 'tongue',
    'lash', 'brow', 'neck', 'sclera', 'pupil',
)


def _material_record_sets(export_data):
    """Return the exact final-record indices referenced by each material."""
    result = {}
    for material_index in export_data['active_material_indices']:
        used = set()
        for triangle in export_data['tris_by_mat'][material_index]:
            used.update(triangle)
        result[material_index] = used
    return result


def _single_bone_for_record_set(records, record_indices, mesh_obj):
    """Return the sole normalized skin bone used by all compact records."""
    slot = None
    one = pack_f(1.0)
    for record_index in record_indices:
        record = records[record_index]
        if len(record) != 40 or record[12:16] != one:
            return None
        slots = record[16:20]
        if not slots[0] or any(slots[1:]):
            return None
        if slot is None:
            slot = slots[0]
        elif slot != slots[0]:
            return None
    if slot is None:
        return None
    skin_names = _export_skin_names(mesh_obj)
    group_index = int(slot) - 1
    if not 0 <= group_index < len(skin_names):
        return None
    return skin_names[group_index]


def _auto_split_material_priority(material, record_count):
    semantic = str(material.get(
        'mowas2_material_semantic_name', material.name)).casefold()
    preferred = 0 if any(key.casefold() in semantic
                         for key in _AUTO_SPLIT_PREFERRED_KW) else 1
    return preferred, -int(record_count), semantic


def _auto_split_mdl_content(arm_obj):
    mdl_path = str(arm_obj.get('gem2_mdl_path') or '') if arm_obj else ''
    if not mdl_path or not os.path.isfile(mdl_path):
        mdl_path = GOH_DEFAULT_MDL
    if not mdl_path or not os.path.isfile(mdl_path):
        return None
    with open(mdl_path, 'rb') as handle:
        raw = handle.read()
    try:
        return raw.decode('gbk')
    except UnicodeDecodeError:
        return raw.decode('utf-8', errors='replace')


def _plan_game_auto_split_single_bone_legacy(
        mesh_obj, arm_obj, skip_mats=None, log=False, mdl_content=None):
    """Plan one lossless skinned material partition for an over-limit human.

    Only materials whose final compact records all normalize to the same single
    animation bone are eligible. The selected records remain stride-40 skinned
    data and are referenced by a second direct VolumeView on the existing mesh
    parent, matching working GOH/GFA multipart human assets.
    """
    export_data = _build_game_export_data(
        mesh_obj, arm_obj, skip_mats=skip_mats, log=False)
    records = export_data['records']
    total_records = len(records)
    base = {
        'required': total_records > GAME_VERTEX_LIMIT,
        'available': False,
        'total_records': total_records,
    }
    if total_records <= GAME_VERTEX_LIMIT:
        if log:
            print('[auto-split] not required:', total_records,
                  '<=', GAME_VERTEX_LIMIT)
        return base

    if mdl_content is None:
        mdl_content = _auto_split_mdl_content(arm_obj)
    if not mdl_content:
        if log:
            print('[auto-split] target MDL is unavailable for attachment preflight')
        return base
    mesh_parent_name = str(arm_obj.get('gem2_mesh_parent') or '')
    if not mesh_parent_name:
        if log:
            print('[auto-split] mesh parent metadata is unavailable')
        return base
    try:
        mdl_content = _strip_generated_auto_split_attachments(
            mdl_content, mesh_parent_name)
        _append_additional_direct_volume_view(
            mdl_content, mesh_parent_name, '__auto_split_probe__.ply')
    except (RuntimeError, ValueError) as exc:
        if log:
            print('[auto-split] mesh-parent view preflight failed:', exc)
        return base

    mesh = export_data['mesh']
    record_sets = _material_record_sets(export_data)
    active_indices = list(export_data['active_material_indices'])
    eligible_by_bone = {}
    for material_index in active_indices:
        material = mesh.materials[material_index]
        semantic = str(material.get(
            'mowas2_material_semantic_name', material.name)).casefold()
        if (not any(key.casefold() in semantic
                    for key in _AUTO_SPLIT_PREFERRED_KW)
                and any(key.casefold() in semantic
                        for key in _AUTO_SPLIT_BLOCKED_KW)):
            continue
        bone_name = _single_bone_for_record_set(
            records, record_sets[material_index], mesh_obj)
        if not bone_name or bone_name not in arm_obj.data.bones:
            continue
        eligible_by_bone.setdefault(bone_name, []).append(material_index)

    def build_plan(selected, bone_name):
        selected = set(selected)
        part_records = set()
        body_records = set()
        part_triangles = 0
        body_triangles = 0
        for material_index in active_indices:
            triangles = export_data['tris_by_mat'][material_index]
            if material_index in selected:
                part_records.update(record_sets[material_index])
                part_triangles += len(triangles)
            else:
                body_records.update(record_sets[material_index])
                body_triangles += len(triangles)
        if (not selected or not part_records or not body_records
                or len(part_records) > GAME_VERTEX_LIMIT
                or len(body_records) > GAME_VERTEX_LIMIT):
            return None
        material_indices = sorted(selected)
        return {
            **base,
            'available': True,
            'bone_name': bone_name,
            'material_indices': material_indices,
            'material_names': [mesh.materials[index].name
                               for index in material_indices],
            'body_records': len(body_records),
            'part_records': len(part_records),
            'body_triangles': body_triangles,
            'part_triangles': part_triangles,
        }

    solutions = []
    for bone_name, material_indices in eligible_by_bone.items():
        ordered = sorted(
            material_indices,
            key=lambda index: _auto_split_material_priority(
                mesh.materials[index], len(record_sets[index])))
        attempts = 0
        found_for_bone = False
        # Exact subset search for normal character material counts. A bounded
        # search prevents pathological custom files with hundreds of slots from
        # turning an export into an unbounded combinatorial operation.
        for size in range(1, len(ordered) + 1):
            for selected in combinations(ordered, size):
                attempts += 1
                if attempts > 20000:
                    break
                candidate = build_plan(selected, bone_name)
                if candidate:
                    preferred_count = sum(
                        _auto_split_material_priority(
                            mesh.materials[index], len(record_sets[index]))[0]
                        == 0 for index in selected)
                    score = (
                        size, -preferred_count,
                        -candidate['part_records'],
                        tuple(mesh.materials[index].name.casefold()
                              for index in selected),
                    )
                    solutions.append((score, candidate))
                    found_for_bone = True
            if found_for_bone or attempts > 20000:
                break
        if found_for_bone:
            continue
        # Bounded-search fallback: try prefixes under both the semantic and
        # largest-record orderings before declaring this owner bone unusable.
        fallback_orders = (
            ordered,
            sorted(ordered, key=lambda index: -len(record_sets[index])),
        )
        seen_subsets = set()
        for fallback in fallback_orders:
            selected = []
            for material_index in fallback:
                selected.append(material_index)
                key = tuple(sorted(selected))
                if key in seen_subsets:
                    continue
                seen_subsets.add(key)
                candidate = build_plan(selected, bone_name)
                if candidate:
                    preferred_count = sum(
                        _auto_split_material_priority(
                            mesh.materials[index], len(record_sets[index]))[0]
                        == 0 for index in selected)
                    score = (
                        len(selected), -preferred_count,
                        -candidate['part_records'],
                        tuple(mesh.materials[index].name.casefold()
                              for index in selected),
                    )
                    solutions.append((score, candidate))
                    break

    if not solutions:
        if log:
            print('[auto-split] no safe single-bone material partition for',
                  total_records, 'records')
        return base

    plan = None
    active_names = {
        index: mesh.materials[index].name for index in active_indices
    }
    base_skip_mats = set(skip_mats or ())
    for _score, candidate in sorted(solutions, key=lambda item: item[0]):
        selected_indices = set(candidate['material_indices'])
        selected_names = set(candidate['material_names'])
        body_skip_mats = base_skip_mats | selected_names
        part_skip_mats = base_skip_mats | {
            name for index, name in active_names.items()
            if index not in selected_indices
        }
        body_data = _build_game_export_data(
            mesh_obj, arm_obj, skip_mats=body_skip_mats, log=False)
        part_data = _build_game_export_data(
            mesh_obj, arm_obj, skip_mats=part_skip_mats, log=False)
        body_count = len(body_data['records'])
        part_count = len(part_data['records'])
        if (not body_count or not part_count
                or body_count > GAME_VERTEX_LIMIT
                or part_count > GAME_VERTEX_LIMIT):
            continue
        plan = dict(candidate)
        plan['body_records'] = body_count
        plan['part_records'] = part_count
        plan['body_triangles'] = sum(
            len(body_data['tris_by_mat'][index])
            for index in body_data['active_material_indices'])
        plan['part_triangles'] = sum(
            len(part_data['tris_by_mat'][index])
            for index in part_data['active_material_indices'])
        plan['body_skip_mats'] = sorted(body_skip_mats)
        plan['part_skip_mats'] = sorted(part_skip_mats)
        plan['attachment_bone'] = mesh_parent_name
        break
    if plan is None:
        if log:
            print('[auto-split] candidate skinned output still exceeds the limit')
        return base
    if log:
        print('[auto-split] selected:', plan['material_names'],
              '| owner bone:', plan['bone_name'],
              '| direct views on:', plan['attachment_bone'],
              '| body:', plan['body_records'],
              '| part:', plan['part_records'],
              '| total source:', total_records)
    return plan


def _auto_split_material_role(material):
    """Return the soft placement role for one visible material."""
    semantic = str(material.get(
        'mowas2_material_semantic_name', material.name)).casefold()
    protected_unicode = ('脸', '顔', '目', '眼', '瞳', '肌', '口', '嘴',
                         '牙', '齿', '歯', '舌', '眉', '睫')
    if (any(keyword.casefold() in semantic
            for keyword in _AUTO_SPLIT_BLOCKED_KW)
            or any(keyword in semantic for keyword in protected_unicode)
            or _is_pupil_layer(semantic)
            or _is_sclera_layer(semantic)
            or _is_eye_shadow_layer(semantic)
            or _is_eye_lid_layer(semantic)):
        return 'protected'
    preferred_unicode = ('马尾', '馬尾', '辫', '辮', '发饰', '髮飾',
                         '头饰', '頭飾')
    if (any(keyword.casefold() in semantic
            for keyword in _AUTO_SPLIT_PREFERRED_KW)
            or any(keyword in semantic for keyword in preferred_unicode)):
        return 'preferred'
    return 'neutral'


def _make_game_export_part(export_data, assignment):
    """Build one local-index PLY payload from immutable global records.

    ``assignment`` stores ``(source_triangle_ordinal, global_triangle)`` items by
    material. Records are copied byte-for-byte and only their u16 indices are
    remapped. A material may appear in several parts, matching working GOH/GFA
    multipart human assets.
    """
    source_records = export_data['records']
    source_positions = export_data['positions']
    mesh = export_data['mesh']
    ordered_by_material = {}
    used_records = set()
    for material_index, items in assignment.items():
        ordered = sorted(items, key=lambda item: item[0])
        triangles = [tuple(item[1]) for item in ordered]
        if not triangles:
            continue
        ordered_by_material[material_index] = triangles
        for triangle in triangles:
            used_records.update(triangle)
    if not used_records:
        raise RuntimeError('Auto-split produced an empty PLY part')
    global_indices = [index for index in range(len(source_records))
                      if index in used_records]
    local_index = {global_index: index
                   for index, global_index in enumerate(global_indices)}
    tris_by_mat = [[] for _ in mesh.materials]
    for material_index, triangles in ordered_by_material.items():
        tris_by_mat[material_index] = [
            tuple(local_index[index] for index in triangle)
            for triangle in triangles
        ]
    active_indices = [index for index, triangles in enumerate(tris_by_mat)
                      if triangles]
    return {
        'mesh': mesh,
        'mesh_to_ply': export_data['mesh_to_ply'],
        'parent_name': export_data['parent_name'],
        'skip_indices': set(export_data.get('skip_indices', ())),
        'records': [source_records[index] for index in global_indices],
        'positions': [source_positions[index] for index in global_indices],
        'tris_by_mat': tris_by_mat,
        'active_material_indices': active_indices,
        'global_record_indices': global_indices,
    }


def _partition_game_export_data(export_data, limit=GAME_VERTEX_LIMIT):
    """Pack finalized skinned triangles into deterministic u16-safe parts.

    Whole materials are kept together whenever possible. If a material alone is
    too large, its triangles are distributed atomically while retaining their
    stable source order inside every emitted MESH block. Placement uses the
    incremental unique-record cost; protected face/body/eye materials prefer the
    main part and hair/accessories prefer later parts, but those are soft rules.
    """
    records = export_data['records']
    mesh = export_data['mesh']
    active_indices = list(export_data['active_material_indices'])
    if not records or not active_indices:
        raise RuntimeError('Auto-split has no visible finalized geometry')
    if limit < 3 or limit > 0xFFFF:
        raise ValueError('Invalid GEM2 auto-split record limit: %r' % limit)

    material_items = {}
    material_records = {}
    for material_index in active_indices:
        items = [(ordinal, tuple(triangle))
                 for ordinal, triangle in enumerate(
                     export_data['tris_by_mat'][material_index])]
        material_items[material_index] = items
        used = set()
        for _ordinal, triangle in items:
            used.update(triangle)
        material_records[material_index] = used

    role_rank = {'protected': 0, 'neutral': 1, 'preferred': 2}
    roles = {index: _auto_split_material_role(mesh.materials[index])
             for index in active_indices}
    ordered_materials = sorted(
        active_indices,
        key=lambda index: (role_rank[roles[index]],
                           -len(material_records[index]), index))

    def new_part():
        if len(parts) >= 100:
            raise RuntimeError(
                'Auto-split needs more than 100 PLY files; simplify the model')
        part = {'records': set(), 'assignment': {}}
        parts.append(part)
        return len(parts) - 1

    def placement_score(part_index, record_set, role):
        part = parts[part_index]
        added = len(record_set - part['records'])
        new_count = len(part['records']) + added
        if new_count > limit:
            return None
        if role == 'protected':
            role_penalty = 0 if part_index == 0 else 1
        elif role == 'preferred':
            role_penalty = 0 if part_index != 0 else 1
        else:
            role_penalty = 0
        # Reuse existing records before considering bin fullness; this minimizes
        # duplicated boundary records and preserves capacity for connected data.
        return role_penalty, added, limit - new_count, part_index

    def place_items(part_index, material_index, items, record_set):
        part = parts[part_index]
        part['records'].update(record_set)
        part['assignment'].setdefault(material_index, []).extend(items)

    parts = []
    new_part()  # Stable main file.
    for material_index in ordered_materials:
        items = material_items[material_index]
        record_set = material_records[material_index]
        role = roles[material_index]
        if len(record_set) <= limit:
            candidates = []
            for part_index in range(len(parts)):
                score = placement_score(part_index, record_set, role)
                if score is not None:
                    candidates.append((score, part_index))
            if not candidates:
                part_index = new_part()
            else:
                _score, part_index = min(candidates)
            place_items(part_index, material_index, items, record_set)
            continue

        # Oversized single material: fill each candidate part by repeatedly
        # selecting the remaining triangle with the fewest missing records. Four
        # lazy heaps (cost 0..3) are updated through record adjacency, avoiding an
        # O(triangle^2) search while keeping connected/overlapping faces together.
        item_by_ordinal = {ordinal: (ordinal, triangle)
                           for ordinal, triangle in items}
        triangle_records = {
            ordinal: set(triangle) for ordinal, triangle in items
        }
        record_to_ordinals = {}
        for ordinal, record_ids in triangle_records.items():
            for record_id in record_ids:
                record_to_ordinals.setdefault(record_id, []).append(ordinal)
        remaining = set(item_by_ordinal)

        def fill_part(part_index):
            part = parts[part_index]
            missing = {
                ordinal: len(triangle_records[ordinal] - part['records'])
                for ordinal in remaining
            }
            heaps = [[] for _ in range(4)]
            for ordinal, cost in missing.items():
                heapq.heappush(heaps[cost], ordinal)
            placed = 0
            while remaining:
                capacity = limit - len(part['records'])
                chosen = None
                for cost in range(min(3, capacity) + 1):
                    heap = heaps[cost]
                    while heap and (heap[0] not in remaining
                                    or missing.get(heap[0]) != cost):
                        heapq.heappop(heap)
                    if heap:
                        chosen = heapq.heappop(heap)
                        break
                if chosen is None:
                    break
                new_records = triangle_records[chosen] - part['records']
                if len(new_records) > capacity:
                    raise RuntimeError(
                        'Auto-split incremental-cost queue exceeded capacity')
                remaining.remove(chosen)
                part['assignment'].setdefault(material_index, []).append(
                    item_by_ordinal[chosen])
                part['records'].update(new_records)
                placed += 1
                for record_id in new_records:
                    for neighbor in record_to_ordinals.get(record_id, ()):
                        if neighbor not in remaining:
                            continue
                        old_cost = missing[neighbor]
                        if old_cost <= 0:
                            continue
                        new_cost = old_cost - 1
                        missing[neighbor] = new_cost
                        heapq.heappush(heaps[new_cost], neighbor)
            return placed

        if role == 'preferred':
            existing_order = list(range(1, len(parts))) + [0]
        else:
            existing_order = list(range(len(parts)))
        for part_index in existing_order:
            if not remaining:
                break
            fill_part(part_index)
        while remaining:
            part_index = new_part()
            if not fill_part(part_index):
                raise RuntimeError(
                    'Auto-split could not place an atomic triangle')

    parts = [part for part in parts if part['assignment']]
    if not parts:
        raise RuntimeError('Auto-split did not assign any triangles')

    # Prove exact triangle ownership before local index remapping. Ordinals are
    # unique within a material even when duplicate triangles exist geometrically.
    for material_index in active_indices:
        assigned = sorted(
            ordinal
            for part in parts
            for ordinal, _triangle in part['assignment'].get(material_index, ()))
        expected = list(range(len(material_items[material_index])))
        if assigned != expected:
            raise RuntimeError(
                'Auto-split lost or duplicated triangles for material %s'
                % mesh.materials[material_index].name)

    payloads = [_make_game_export_part(export_data, part['assignment'])
                for part in parts]
    for payload in payloads:
        if not payload['records'] or len(payload['records']) > limit:
            raise RuntimeError('Auto-split emitted an invalid record count')
    return payloads


def _plan_game_auto_split(mesh_obj, arm_obj, skip_mats=None, log=False,
                          mdl_content=None, entity_name=None,
                          record_limit=GAME_VERTEX_LIMIT):
    """Plan lossless GOH multipart PLY output from finalized records."""
    record_limit = int(record_limit)
    if record_limit < 3 or record_limit > GAME_VERTEX_LIMIT:
        raise ValueError('Split record limit must be between 3 and 65535')
    export_data = _build_game_export_data(
        mesh_obj, arm_obj, skip_mats=skip_mats, log=False)
    total_records = len(export_data['records'])
    total_triangles = sum(
        len(export_data['tris_by_mat'][index])
        for index in export_data['active_material_indices'])
    base = {
        'required': total_records > record_limit,
        'available': False,
        'record_limit': record_limit,
        'total_records': total_records,
        'total_triangles': total_triangles,
    }
    if total_records <= record_limit:
        if log:
            print('[auto-split] not required:', total_records,
                  '<=', record_limit)
        return base

    if mdl_content is None:
        mdl_content = _auto_split_mdl_content(arm_obj)
    mesh_parent_name = str(arm_obj.get('gem2_mesh_parent') or '')
    if not mdl_content or not mesh_parent_name:
        if log:
            print('[auto-split] target MDL/mesh-parent metadata unavailable')
        return base
    try:
        probe = _strip_generated_auto_split_attachments(
            mdl_content, mesh_parent_name, entity_name=entity_name)
        _append_additional_direct_volume_view(
            probe, mesh_parent_name, '__auto_split_probe__.ply')
    except (RuntimeError, ValueError) as exc:
        if log:
            print('[auto-split] mesh-parent view preflight failed:', exc)
        return base

    payloads = _partition_game_export_data(export_data, limit=record_limit)
    if len(payloads) < 2:
        return base
    summaries = []
    for index, payload in enumerate(payloads):
        triangles = sum(len(payload['tris_by_mat'][material_index])
                        for material_index in payload['active_material_indices'])
        summaries.append({
            'index': index,
            'records': len(payload['records']),
            'triangles': triangles,
            'material_indices': list(payload['active_material_indices']),
            'material_names': [
                payload['mesh'].materials[material_index].name
                for material_index in payload['active_material_indices']],
        })
    written_records = sum(item['records'] for item in summaries)
    plan = {
        **base,
        'available': True,
        'attachment_bone': mesh_parent_name,
        'parts': payloads,
        'part_summaries': summaries,
        'written_records': written_records,
        'duplicated_boundary_records': written_records - total_records,
        # Compatibility fields for existing callers/logs.
        'body_records': summaries[0]['records'],
        'part_records': summaries[1]['records'],
        'body_triangles': summaries[0]['triangles'],
        'part_triangles': summaries[1]['triangles'],
    }
    if sum(item['triangles'] for item in summaries) != total_triangles:
        raise RuntimeError('Auto-split triangle total changed during planning')
    if log:
        print('[auto-split] %d finalized records -> %d direct skin views'
              % (total_records, len(payloads)))
        for summary in summaries:
            print('[auto-split] part %02d: %dv/%dt | %s'
                  % (summary['index'], summary['records'],
                     summary['triangles'], summary['material_names']))
        print('[auto-split] duplicated boundary records:',
              plan['duplicated_boundary_records'])
    return plan


def _bone_world_matrix(arm_obj, bone_name):
    """Return one stored MDL rest bone matrix in Blender world space."""
    raw_mats = arm_obj.get('gem2_world_mats') if arm_obj else None
    try:
        world_mats = json.loads(raw_mats) if isinstance(raw_mats, str) else raw_mats
        rows = world_mats.get(bone_name) if world_mats else None
        if rows:
            matrix = arm_obj.matrix_world @ Matrix(rows)
            if abs(matrix.to_3x3().determinant()) > 1e-8:
                return matrix
    except Exception as exc:
        raise RuntimeError(
            'Invalid stored MDL matrix for split bone %r: %s'
            % (bone_name, exc)) from exc
    raise RuntimeError(
        'Missing usable stored MDL matrix for split bone: ' + bone_name)


def _build_game_rigid_export_data(mesh_obj, arm_obj, material_indices,
                                  bone_name, log=False):
    """Build exact 32-byte position/normal/UV records in one bone's space."""
    mesh = mesh_obj.data
    mesh.update()
    mesh.calc_loop_triangles()
    selected = {int(index) for index in material_indices}
    if not selected:
        raise RuntimeError('Rigid split export has no selected materials')
    invalid = sorted(index for index in selected
                     if not 0 <= index < len(mesh.materials)
                     or mesh.materials[index] is None)
    if invalid:
        raise RuntimeError('Rigid split export has invalid material slots: '
                           + ', '.join(map(str, invalid)))
    visible_loop_tris = [
        tri for tri in mesh.loop_triangles if tri.material_index in selected
    ]
    if not visible_loop_tris:
        raise RuntimeError('Rigid split export has no triangles')
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        raise RuntimeError(_("mowas2.err.no_uv_layer"))

    game_normals = _build_game_normals(mesh, visible_loop_tris, log=log)
    bone_world = _bone_world_matrix(arm_obj, bone_name)
    mesh_to_ply = bone_world.inverted() @ mesh_obj.matrix_world
    linear_determinant = mesh_to_ply.to_3x3().determinant()
    if abs(linear_determinant) <= 1e-8:
        raise RuntimeError('Rigid split transform is singular: ' + bone_name)
    # GEM's established output reverses Blender winding for a proper transform.
    # A reflected bone-local transform already flips handedness, so reversing it
    # again would invert culling relative to the transformed vertex normals.
    reverse_winding = linear_determinant > 0.0
    normal_to_ply = mesh_to_ply.to_3x3().inverted().transposed()
    vertex_positions = [mesh_to_ply @ vertex.co for vertex in mesh.vertices]
    position_bytes = [pack_fff(pos.x, pos.y, pos.z)
                      for pos in vertex_positions]
    parent_inverse, _parent_name = _mesh_parent_local_inverse(arm_obj)
    mesh_to_skinned_ply = parent_inverse @ mesh_obj.matrix_world
    representative_keys = []
    for vertex in mesh.vertices:
        position = mesh_to_skinned_ply @ vertex.co
        representative_keys.append(pack_fff(position.x, position.y, position.z))
    representative_by_position = {}
    representatives = []
    for vertex_index, key in enumerate(representative_keys):
        representatives.append(
            representative_by_position.setdefault(key, vertex_index))

    records = []
    positions = []
    lookup = {}
    tris_by_mat = [[] for _ in mesh.materials]
    for tri in visible_loop_tris:
        output_triangle = []
        for loop_index, vertex_index in zip(tri.loops, tri.vertices):
            vertex_index = int(vertex_index)
            representative = representatives[vertex_index]
            fallback_normal = mesh.vertices[representative].normal
            source_normal = game_normals.get(
                (tri.material_index, vertex_index), fallback_normal)
            normal = normal_to_ply @ source_normal
            if normal.length_squared > 1e-20:
                normal.normalize()
            uv = uv_layer.data[loop_index].uv
            record = (position_bytes[vertex_index]
                      + pack_fff(normal.x, normal.y, normal.z)
                      + pack_ff(float(uv.x), 1.0 - float(uv.y)))
            output_index = lookup.get(record)
            if output_index is None:
                output_index = len(records)
                lookup[record] = output_index
                records.append(record)
                positions.append(vertex_positions[vertex_index])
            output_triangle.append(output_index)
        tris_by_mat[tri.material_index].append(tuple(output_triangle))
    active_material_indices = [
        index for index, triangles in enumerate(tris_by_mat) if triangles
    ]
    if log:
        print('[auto-split] rigid bone:', bone_name,
              '| materials:', [mesh.materials[index].name
                               for index in active_material_indices],
              '| records:', len(records),
              '| winding:', 'reversed' if reverse_winding else 'reflected-native')
    return {
        'mesh': mesh,
        'mesh_to_ply': mesh_to_ply,
        'bone_name': bone_name,
        'reverse_winding': reverse_winding,
        'records': records,
        'positions': positions,
        'tris_by_mat': tris_by_mat,
        'active_material_indices': active_material_indices,
    }


def _game_export_vertex_count(mesh_obj, arm_obj, skip_mats=None,
                              preserve_source_splits=False):
    """Return the real single-PLY vertex count without changing mesh topology."""
    return len(_build_game_export_data(
        mesh_obj, arm_obj, skip_mats=skip_mats,
        preserve_source_splits=preserve_source_splits)['records'])


def export_ply_game(filepath, mesh_obj, arm_obj, alpha_mats=None,
                    two_sided_mats=None, skip_mats=None,
                    preserve_source_splits=False, prepared_data=None):
    """导出游戏原生 per-vertex ply。

    alpha_mats: 可选 set, 材质名 → 需要 MESH_FLAG_ALPHA(0x0002)。
    two_sided_mats: 显式需要 MESH_FLAG_TWO_SIDED(0x0001) 的材质名集合。
    skip_mats: 默认完全不可见的源材质；其 MESH 块、三角形和孤立顶点均不写。
    preserve_source_splits: 诊断模式；保留完整 split 权重/loop 法线，默认关闭。
    prepared_data: 自动多 PLY 分区生成的冻结记录；只重映射局部 u16 索引，
    不重新计算位置、权重、法线或 UV。
    GFA 人皮默认均为单面；不能全局开启双面，否则眼白内侧背面也会写深度。
    alpha_mats 传 None 时用旧关键字兜底 (_material_needs_alpha_flag)。
    0x0002 位语义经 MOWAS2 13533 个 ply + GOH humanskin 122 个 ply 扫描实证:
    - blend 半透明材质 → 带 0x0002 (2b/meihong/alice flags 0x0C16/0x0C17,
      GOH 66 例 0x0C16);
    - test 镂空材质 → 不带 (GOH 304 例 0x0C15);
    - none → 不带 (0x0C14)。
    """
    if prepared_data is None:
        export_data = _build_game_export_data(
            mesh_obj, arm_obj, skip_mats=skip_mats, log=True,
            preserve_source_splits=preserve_source_splits)
    else:
        if skip_mats or preserve_source_splits:
            raise ValueError(
                'prepared_data cannot be combined with skip/diagnostic options')
        export_data = prepared_data
        print('[export] writing immutable auto-split payload:',
              len(export_data['records']), 'records')
    mesh = export_data['mesh']
    skip_indices = export_data['skip_indices']
    export_records = export_data['records']
    record_positions = export_data['positions']
    tris_by_mat = export_data['tris_by_mat']
    active_material_indices = export_data['active_material_indices']
    if skip_indices:
        skipped = [mesh.materials[i].name for i in sorted(skip_indices)]
        print('[export] skipped invisible materials:', skipped)
    if len(export_records) > GAME_VERTEX_LIMIT:
        raise RuntimeError(_(
            "mowas2.err.unique_vertex_limit",
            vertices=len(export_records)))

    skin_names = _export_skin_names(mesh_obj)
    skin_name_bytes = []
    for group_name in skin_names:
        try:
            encoded = group_name.encode('ascii')
        except UnicodeEncodeError as exc:
            raise RuntimeError('PLY skin group name is not ASCII: ' + group_name) from exc
        if not encoded or len(encoded) >= 128:
            raise RuntimeError(
                'PLY skin group name must contain 1-127 ASCII bytes: ' + group_name)
        skin_name_bytes.append(encoded)
    if len(skin_name_bytes) > 254:
        raise RuntimeError(
            'PLY full palette supports at most 254 skin groups, found %d'
            % len(skin_name_bytes))
    material_name_bytes = {}
    for material_index in active_material_indices:
        material = mesh.materials[material_index]
        material_name = material.name if material else 'mat%d' % material_index
        try:
            encoded = (material_name + '.mtl').encode('ascii')
        except UnicodeEncodeError as exc:
            raise RuntimeError(
                'PLY material name is not ASCII: ' + material_name) from exc
        if not encoded or len(encoded) >= 128:
            raise RuntimeError(
                'PLY material name must contain at most 123 ASCII bytes: '
                + material_name)
        material_name_bytes[material_index] = encoded

    with open(filepath, 'wb') as f:
        f.write(b'EPLY')
        bb0 = Vector((min(v.x for v in record_positions),
                      min(v.y for v in record_positions),
                      min(v.z for v in record_positions)))
        bb1 = Vector((max(v.x for v in record_positions),
                      max(v.y for v in record_positions),
                      max(v.z for v in record_positions)))
        f.write(b'BNDS')
        f.write(pack_fff(*bb0))
        f.write(pack_fff(*bb1))

        f.write(b'SKIN')
        f.write(pack_I(len(skin_names)))
        for name_bytes in skin_name_bytes:
            f.write(pack_B(len(name_bytes)))
            f.write(name_bytes)

        tri_start = 0
        for mi in active_material_indices:
            mat_tris = tris_by_mat[mi]
            f.write(b'MESH')
            f.write(pack_I(D3DFVF_XYZB2 | D3DFVF_NORMAL | D3DFVF_TEX1
                           | D3DFVF_LASTBETA_UBYTE4))
            f.write(pack_I(tri_start))
            f.write(pack_I(len(mat_tris)))
            tri_start += len(mat_tris)
            mat_name = mesh.materials[mi].name if mesh.materials[mi] else 'mat%d' % mi
            mesh_flags = (MESH_FLAG_LIGHT | MESH_FLAG_SKINNED
                          | MESH_FLAG_MATERIAL | MESH_FLAG_SUBSKIN)
            if two_sided_mats and mat_name in two_sided_mats:
                mesh_flags |= MESH_FLAG_TWO_SIDED
            if alpha_mats is not None:
                needs_alpha = mat_name in alpha_mats
            else:
                needs_alpha = _material_needs_alpha_flag(mat_name)
            if needs_alpha:
                mesh_flags |= MESH_FLAG_ALPHA
            f.write(pack_I(mesh_flags))
            mtl_name_bytes = material_name_bytes[mi]
            f.write(pack_B(len(mtl_name_bytes)))
            f.write(mtl_name_bytes)
            # ── 权重索引修复(问题4: 麻花+没脑袋根因) ──
            # 游戏约定: 权重字节 = 该 MESH palette 的槽位(0=无骨),
            # palette 值 = 1 基 SKIN 列表索引。zaomiao/medicgirl/vanilla 均如此
            # (zaomiao: byte4→palette[4]=5→SKIN[4]=foot1r; byte13→basis; byte24→head)。
            # 旧写法把 0 基 SKIN 索引直接当字节 + palette 缺前导 0 → 全错位。
            # 这里写全量 palette(所有骨骼 1 基 + 前导 0), 字节 = group+1, 与 vanilla 同款。
            palette_full = [0] + [u + 1 for u in range(len(skin_names))]
            f.write(pack_B(len(palette_full)))
            f.write(bytes(palette_full))

        f.write(b'VERT')
        f.write(pack_I(len(export_records)))
        f.write(pack_H(40))
        f.write(b'\x07\x00')
        for record in export_records:
            f.write(record)

        f.write(b'INDX')
        f.write(pack_I(sum(len(tris_by_mat[mi])
                           for mi in active_material_indices) * 3))
        for mi in active_material_indices:
            for tri in tris_by_mat[mi]:
                f.write(pack_HHH(tri[0], tri[2], tri[1]))

    written_materials = [mesh.materials[mi].name
                         for mi in active_material_indices]
    written_triangles = sum(len(tris_by_mat[mi])
                            for mi in active_material_indices)
    return {'materials': written_materials,
            'triangles': written_triangles,
            'vertices': len(export_records),
            'indices': written_triangles * 3,
            'skin_names': [name.decode('ascii') for name in skin_name_bytes],
            'record_sha256': hashlib.sha256(
                b''.join(export_records)).hexdigest()}


def export_ply_game_rigid(filepath, mesh_obj, arm_obj, material_indices,
                          bone_name, alpha_mats=None, two_sided_mats=None):
    """Write a static stride-32 PLY attached directly to an existing MDL bone."""
    export_data = _build_game_rigid_export_data(
        mesh_obj, arm_obj, material_indices, bone_name, log=True)
    mesh = export_data['mesh']
    records = export_data['records']
    positions = export_data['positions']
    tris_by_mat = export_data['tris_by_mat']
    active_material_indices = export_data['active_material_indices']
    reverse_winding = export_data['reverse_winding']
    if len(records) > GAME_VERTEX_LIMIT:
        raise RuntimeError(_(
            "mowas2.err.unique_vertex_limit", vertices=len(records)))

    material_name_bytes = {}
    for material_index in active_material_indices:
        material_name = mesh.materials[material_index].name
        try:
            encoded = (material_name + '.mtl').encode('ascii')
        except UnicodeEncodeError as exc:
            raise RuntimeError(
                'Rigid split material name is not ASCII: ' + material_name) from exc
        if not encoded or len(encoded) >= 128:
            raise RuntimeError(
                'Rigid split material name has invalid length: ' + material_name)
        material_name_bytes[material_index] = encoded

    if positions:
        xs = [position.x for position in positions]
        ys = [position.y for position in positions]
        zs = [position.z for position in positions]
        bounds_min = (min(xs), min(ys), min(zs))
        bounds_max = (max(xs), max(ys), max(zs))
    else:
        bounds_min = bounds_max = (0.0, 0.0, 0.0)

    with open(filepath, 'wb') as handle:
        handle.write(b'EPLY')
        handle.write(b'BNDS')
        handle.write(pack_fff(*bounds_min))
        handle.write(pack_fff(*bounds_max))
        triangle_start = 0
        for material_index in active_material_indices:
            triangles = tris_by_mat[material_index]
            material_name = mesh.materials[material_index].name
            handle.write(b'MESH')
            handle.write(pack_I(D3DFVF_XYZ | D3DFVF_NORMAL | D3DFVF_TEX1))
            handle.write(pack_I(triangle_start))
            handle.write(pack_I(len(triangles)))
            triangle_start += len(triangles)
            flags = MESH_FLAG_LIGHT | MESH_FLAG_MATERIAL
            if two_sided_mats and material_name in two_sided_mats:
                flags |= MESH_FLAG_TWO_SIDED
            needs_alpha = (material_name in alpha_mats
                           if alpha_mats is not None
                           else _material_needs_alpha_flag(material_name))
            if needs_alpha:
                flags |= MESH_FLAG_ALPHA
            handle.write(pack_I(flags))
            encoded = material_name_bytes[material_index]
            handle.write(pack_B(len(encoded)))
            handle.write(encoded)

        handle.write(b'VERT')
        handle.write(pack_I(len(records)))
        handle.write(pack_H(32))
        handle.write(b'\x07\x00')
        for record in records:
            handle.write(record)
        handle.write(b'INDX')
        triangle_count = sum(len(tris_by_mat[index])
                             for index in active_material_indices)
        handle.write(pack_I(triangle_count * 3))
        for material_index in active_material_indices:
            for triangle in tris_by_mat[material_index]:
                if reverse_winding:
                    handle.write(pack_HHH(
                        triangle[0], triangle[2], triangle[1]))
                else:
                    handle.write(pack_HHH(
                        triangle[0], triangle[1], triangle[2]))

    written_materials = [mesh.materials[index].name
                         for index in active_material_indices]
    written_triangles = sum(len(tris_by_mat[index])
                            for index in active_material_indices)
    return {
        'materials': written_materials,
        'triangles': written_triangles,
        'vertices': len(records),
        'indices': written_triangles * 3,
        'bone_name': bone_name,
        'winding': 'reversed' if reverse_winding else 'reflected-native',
    }


def _validate_exported_ply_contract(filepath, out_sub, hidden_mats,
                                    alpha_mats, two_sided_mats,
                                    material_semantics, mat_diffuse,
                                    expected_written):
    """Parse the written PLY and verify material, flag and index invariants."""
    with open(filepath, 'rb') as handle:
        data = handle.read()

    def require(condition, message):
        if not condition:
            raise RuntimeError(message + ': ' + filepath)

    p = 0
    require(data[p:p + 4] == b'EPLY', 'Exported PLY has no EPLY header')
    p += 4
    require(data[p:p + 4] == b'BNDS' and p + 28 <= len(data),
            'Exported PLY has no valid BNDS block')
    p += 28
    require(data[p:p + 4] == b'SKIN' and p + 8 <= len(data),
            'Exported PLY has no valid SKIN block')
    p += 4
    bone_count = struct.unpack_from('<I', data, p)[0]
    p += 4
    require(bone_count <= 254, 'Exported PLY has an invalid SKIN count')
    parsed_skin_names = []
    for _index in range(bone_count):
        require(p < len(data), 'Exported PLY has a truncated SKIN name')
        name_len = data[p]
        p += 1
        require(0 < name_len < 128 and p + name_len <= len(data),
                'Exported PLY has an invalid SKIN name')
        try:
            skin_name = data[p:p + name_len].decode('ascii')
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                'Exported PLY has a non-ASCII SKIN name: ' + filepath) from exc
        parsed_skin_names.append(skin_name)
        p += name_len

    rows = []
    while data[p:p + 4] == b'MESH':
        p += 4
        require(p + 17 <= len(data), 'Exported PLY has a truncated MESH')
        fvf, tri_start, tri_count, flags = struct.unpack_from('<IIII', data, p)
        p += 16
        name_len = data[p]
        p += 1
        require(0 < name_len < 128 and p + name_len <= len(data),
                'Exported PLY has an invalid MESH material name')
        try:
            name = data[p:p + name_len].decode('ascii')
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                'Exported PLY has a non-ASCII MESH material: ' + filepath) from exc
        p += name_len
        require(name.endswith('.mtl'),
                'Exported PLY MESH material lacks .mtl')
        require(p < len(data), 'Exported PLY has no MESH palette count')
        palette_count = data[p]
        p += 1
        require(p + palette_count <= len(data),
                'Exported PLY has a truncated MESH palette')
        palette = data[p:p + palette_count]
        expected_palette = bytes(range(bone_count + 1))
        require(palette == expected_palette,
                'Exported PLY MESH palette differs from full 1-based SKIN map')
        p += palette_count
        rows.append({
            'name': name[:-4], 'fvf': fvf, 'tri_start': tri_start,
            'tri_count': tri_count, 'flags': flags,
        })

    vert_pos = p
    require(data[vert_pos:vert_pos + 4] == b'VERT'
            and vert_pos + 12 <= len(data),
            'Exported PLY has no valid VERT block')
    vert_count = struct.unpack_from('<I', data, vert_pos + 4)[0]
    stride = struct.unpack_from('<H', data, vert_pos + 8)[0]
    require(stride == 40 and data[vert_pos + 10:vert_pos + 12] == b'\x07\x00',
            'Exported PLY has an unexpected VERT layout')
    index_pos = vert_pos + 12 + vert_count * stride
    if index_pos + 8 > len(data) or data[index_pos:index_pos + 4] != b'INDX':
        raise RuntimeError('Exported PLY has no valid INDX block: ' + filepath)
    index_count = struct.unpack_from('<I', data, index_pos + 4)[0]
    index_bytes = index_count * 2
    if index_pos + 8 + index_bytes != len(data):
        raise RuntimeError(
            'Exported PLY INDX byte count does not reach EOF: ' + filepath)
    indices = (struct.unpack_from('<%dH' % index_count, data, index_pos + 8)
               if index_count else ())
    expected_skin_names = expected_written.get('skin_names')
    if (expected_skin_names is not None
            and parsed_skin_names != list(expected_skin_names)):
        raise RuntimeError(
            'Exported PLY SKIN order differs from the writer result')
    expected_record_sha = expected_written.get('record_sha256')
    if expected_record_sha is not None:
        record_bytes = data[vert_pos + 12:index_pos]
        if hashlib.sha256(record_bytes).hexdigest() != expected_record_sha:
            raise RuntimeError(
                'Exported PLY VERT records differ from the immutable payload')

    parsed_materials = [row['name'] for row in rows]
    expected_materials = list(expected_written.get('materials', ()))
    if parsed_materials != expected_materials:
        raise RuntimeError(
            'Exported PLY MESH order differs from the writer result: %r != %r'
            % (parsed_materials, expected_materials))

    expected_start = 0
    expected_fvf = (D3DFVF_XYZB2 | D3DFVF_NORMAL | D3DFVF_TEX1
                    | D3DFVF_LASTBETA_UBYTE4)
    expected_base = (MESH_FLAG_LIGHT | MESH_FLAG_SKINNED
                     | MESH_FLAG_MATERIAL | MESH_FLAG_SUBSKIN)
    hidden_folded = {name.casefold() for name in hidden_mats}
    row_names_folded = set()
    for row in rows:
        name = row['name']
        row_names_folded.add(name.casefold())
        if row['fvf'] != expected_fvf:
            raise RuntimeError(
                'Exported PLY FVF mismatch for %s: 0x%04X != 0x%04X'
                % (name, row['fvf'], expected_fvf))
        if row['tri_start'] != expected_start:
            raise RuntimeError(
                'Exported PLY has a non-contiguous triangle span at ' + name)
        expected_start += row['tri_count']
        if name.casefold() in hidden_folded:
            raise RuntimeError(
                'Static alpha=0 material entered PLY MESH: ' + name)
        mtl_path = os.path.join(out_sub, name + '.mtl')
        if not os.path.isfile(mtl_path):
            raise RuntimeError(
                'Exported PLY references a missing local MTL: ' + mtl_path)
        expected_flags = expected_base
        if name in alpha_mats:
            expected_flags |= MESH_FLAG_ALPHA
        if name in two_sided_mats:
            expected_flags |= MESH_FLAG_TWO_SIDED
        if row['flags'] != expected_flags:
            raise RuntimeError(
                'Exported PLY flags mismatch for %s: 0x%04X != 0x%04X'
                % (name, row['flags'], expected_flags))
        semantic = material_semantics.get(name, name)
        diffuse = mat_diffuse.get(name, '')
        if (_is_gfa_pupil_overlay(semantic, diffuse)
                and row['flags'] != (expected_base | MESH_FLAG_ALPHA)):
            raise RuntimeError(
                'Eyes+/eyeblend PLY MESH must be exactly 0x0C16: ' + name)
        if ((_is_sclera_layer(semantic) or _is_eye_lid_layer(semantic))
                and row['flags'] != expected_base):
            raise RuntimeError(
                'Opaque eye PLY MESH must be exactly 0x0C14: ' + name)

    if expected_start != expected_written.get('triangles'):
        raise RuntimeError(
            'Exported PLY triangle total differs from the writer result')
    if vert_count != expected_written.get('vertices'):
        raise RuntimeError(
            'Exported PLY VERT count differs from the writer result')
    if index_count != expected_start * 3:
        raise RuntimeError(
            'Exported PLY INDX count does not match MESH triangles')
    if index_count != expected_written.get('indices'):
        raise RuntimeError(
            'Exported PLY INDX count differs from the writer result')
    if indices and max(indices) >= vert_count:
        raise RuntimeError('Exported PLY contains an out-of-range vertex index')
    if len(set(indices)) != vert_count:
        raise RuntimeError(
            'Exported PLY contains unreferenced VERT records: %d used / %d'
            % (len(set(indices)), vert_count))
    for hidden in hidden_mats:
        if hidden.casefold() in row_names_folded:
            raise RuntimeError(
                'Static alpha=0 material survived PLY validation: ' + hidden)
    print('[ply-contract] meshes=%d triangles=%d vertices=%d indices=%d'
          % (len(rows), expected_start, vert_count, index_count))
    return {'meshes': len(rows), 'triangles': expected_start,
            'vertices': vert_count, 'indices': index_count}


def _validate_exported_rigid_ply_contract(
        filepath, out_sub, alpha_mats, two_sided_mats, expected_written):
    """Verify the static stride-32 contract used by automatic split parts."""
    with open(filepath, 'rb') as handle:
        data = handle.read()

    def require(condition, message):
        if not condition:
            raise RuntimeError(message + ': ' + filepath)

    require(data[:4] == b'EPLY', 'Rigid split PLY has no EPLY header')
    require(data[4:8] == b'BNDS' and len(data) >= 32,
            'Rigid split PLY has no valid BNDS block')
    position = 32
    require(data[position:position + 4] != b'SKIN',
            'Rigid split PLY unexpectedly contains SKIN')
    rows = []
    while data[position:position + 4] == b'MESH':
        position += 4
        require(position + 17 <= len(data),
                'Rigid split PLY has a truncated MESH')
        fvf, triangle_start, triangle_count, flags = struct.unpack_from(
            '<IIII', data, position)
        position += 16
        name_length = data[position]
        position += 1
        require(0 < name_length < 128
                and position + name_length <= len(data),
                'Rigid split PLY has an invalid material name')
        name = data[position:position + name_length].decode('ascii')
        position += name_length
        require(name.endswith('.mtl'),
                'Rigid split PLY material lacks .mtl')
        rows.append({
            'name': name[:-4], 'fvf': fvf,
            'tri_start': triangle_start, 'tri_count': triangle_count,
            'flags': flags,
        })

    require(data[position:position + 4] == b'VERT'
            and position + 12 <= len(data),
            'Rigid split PLY has no valid VERT block')
    vertex_count = struct.unpack_from('<I', data, position + 4)[0]
    stride = struct.unpack_from('<H', data, position + 8)[0]
    require(stride == 32 and data[position + 10:position + 12] == b'\x07\x00',
            'Rigid split PLY has an unexpected VERT layout')
    index_position = position + 12 + vertex_count * stride
    require(index_position + 8 <= len(data)
            and data[index_position:index_position + 4] == b'INDX',
            'Rigid split PLY has no valid INDX block')
    index_count = struct.unpack_from('<I', data, index_position + 4)[0]
    require(index_position + 8 + index_count * 2 == len(data),
            'Rigid split PLY INDX does not reach EOF')
    indices = (struct.unpack_from('<%dH' % index_count,
                                  data, index_position + 8)
               if index_count else ())

    require([row['name'] for row in rows]
            == list(expected_written.get('materials', ())),
            'Rigid split PLY material order differs from writer result')
    expected_fvf = D3DFVF_XYZ | D3DFVF_NORMAL | D3DFVF_TEX1
    expected_start = 0
    for row in rows:
        require(row['fvf'] == expected_fvf,
                'Rigid split PLY has an unexpected FVF')
        require(row['tri_start'] == expected_start,
                'Rigid split PLY has a non-contiguous triangle span')
        expected_start += row['tri_count']
        require(os.path.isfile(os.path.join(
                    out_sub, row['name'] + '.mtl')),
                'Rigid split PLY references a missing MTL')
        expected_flags = MESH_FLAG_LIGHT | MESH_FLAG_MATERIAL
        if row['name'] in alpha_mats:
            expected_flags |= MESH_FLAG_ALPHA
        if row['name'] in two_sided_mats:
            expected_flags |= MESH_FLAG_TWO_SIDED
        require(row['flags'] == expected_flags,
                'Rigid split PLY has unexpected MESH flags')

    require(expected_start == expected_written.get('triangles'),
            'Rigid split PLY triangle count differs from writer result')
    require(vertex_count == expected_written.get('vertices'),
            'Rigid split PLY vertex count differs from writer result')
    require(index_count == expected_written.get('indices')
            == expected_start * 3,
            'Rigid split PLY index count differs from writer result')
    require(not indices or max(indices) < vertex_count,
            'Rigid split PLY contains an out-of-range index')
    require(len(set(indices)) == vertex_count,
            'Rigid split PLY contains unreferenced VERT records')
    print('[ply-contract:rigid] meshes=%d triangles=%d vertices=%d indices=%d'
          % (len(rows), expected_start, vertex_count, index_count))
    return {'meshes': len(rows), 'triangles': expected_start,
            'vertices': vertex_count, 'indices': index_count}


# ═══════════════════════════════════════════════════════════════
#  对齐数学（Umeyama 刚性拟合 + 姿态级旋转）
# ═══════════════════════════════════════════════════════════════
def _wpos(a, n):
    return a.matrix_world @ a.pose.bones[n].head


def _mid(a, names):
    pts = [_wpos(a, n) for n in names if n in a.pose.bones]
    return sum(pts, Vector()) / len(pts) if pts else Vector()


def umeyama_fit(src_pts, tgt_pts):
    import numpy as np
    P = np.array([p.to_tuple() for p in src_pts])
    Q = np.array([q.to_tuple() for q in tgt_pts])
    mp = P.mean(axis=0)
    mq = Q.mean(axis=0)
    Pp = P - mp
    Qq = Q - mq
    cov = Pp.T @ Qq
    U, S, Vt = np.linalg.svd(cov)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    D = np.diag([1, 1, d])
    R = Vt.T @ D @ U.T
    var_p = np.sum(Pp ** 2)
    s = np.trace(np.diag(S) @ D) / var_p if var_p > 0 else 1.0
    t = mq - s * (R @ mp)
    return s, R, t


def _set_world_rot(arm, bone, pivot, axis, angle_deg):
    b = arm.pose.bones[bone]
    wm = arm.matrix_world @ b.matrix
    r = Matrix.Rotation(math.radians(angle_deg), 4, axis)
    tt = Matrix.Translation(pivot)
    ti = Matrix.Translation(-pivot)
    b.matrix = arm.matrix_world.inverted() @ (tt @ r @ ti @ wm)
    bpy.context.view_layer.update()


def align_rigid(src, tgt, mesh, root_empty, mirrored, ground_z=GROUND_Z):
    """约束刚性拟合（v2.1，E6 修正）：只绕 Z 轴旋转 + 均匀缩放 + 平移。

    E6 血泪：原 3D Umeyama 的点集全部是左右对称中点（面内无前向信息），
    SVD 的面内旋转自由度退化 → 任意角度（实测把正对 +X 的模型转到 +23.4°，
    眼线 -66.6° 应 -90°）。修正：θ 由「源肩线→目标肩线」显式确定（两骨架
    的 up 都是 +z，无需其他旋转），s/t 用固定 R 的最小二乘，最后用 Eye_L
    与 foot1l 同侧判据消除 180° 歧义（顺带修正朝向符号）。
    """
    import numpy as np

    def wp(arm, n):
        return arm.matrix_world @ arm.pose.bones[n].head

    src_names = ['Leg_L', 'Leg_R', 'Knee_L', 'Knee_R', 'Ankle_L', 'Ankle_R',
                 'Shoulder_L', 'Shoulder_R', 'Neck', 'Head',
                 'UpperBody', 'LowerBody']
    tgt_names = ['foot1l', 'foot1r', 'foot2l', 'foot2r', 'foot3l', 'foot3r',
                 'clavicle_left', 'clavicle_right', 'head', 'head',
                 'ik_leftright', 'body']
    P = [wp(src, n) for n in src_names if n in src.pose.bones]
    Q = [wp(tgt, n) for n in tgt_names[:len(P)]]
    if len(P) < 3:
        raise RuntimeError(_("mowas2.err.align_key_bones_missing",
                             count=len(P)))

    # 1) θ: 水平面内 源肩线 → 目标肩线
    def line_dir(arm, a, b):
        v = wp(arm, b) - wp(arm, a)
        v.z = 0.0
        return v
    sline = line_dir(src, 'Shoulder_L', 'Shoulder_R')
    tline = line_dir(tgt, 'clavicle_left', 'clavicle_right')
    if sline.length > 1e-6 and tline.length > 1e-6:
        ang = math.atan2(sline.cross(tline).z, sline.dot(tline))
    else:
        ang = 0.0
    R = Matrix.Rotation(ang, 4, 'Z')

    # 2) GFA 肩高比整体缩放 (替代 v2.1 的全点集 Umeyama 最小二乘 s)。
    #    2026-08-18 夜 (v12, 全盘 GFA): 用户换 model_better2 测试 "全身尺寸
    #    对不上" —— 根因 = Umeyama 的 s 按全部 12 个点平均算, 模型上身比例
    #    与目标不同时会把上身放大过头 (实测 model_better2 头骨 33.0 vs 目标
    #    30.7, 网格顶 42.5 vs 目标 36.2)。GFA Step2 L528-537 的真相:
    #       OverallScale = GOH_ShoulderHeight / (MMD_ShoulderHeight - 鞋底)
    #   只按【净肩高】定整体等比 → 任何模型的肩/头/身高自动精确对齐。
    #   v13 (2026-08-19) 一分不差修正 (GFA L518/L534 原文):
    #     GOH_Shoulder_Name_List = [Hand1L, Hand1R]  → 目标肩用 hand1l/r 均 z
    #       (原用 clavicle —— clavicle 在肩胛根, 比 GFA 的 hand1 肩点高 0.65,
    #       导致整体偏大); MMD_Shoulder_Name_List = [Arm_R, Arm_L] → 源肩用
    #       Arm 均 z (MMD 骨架 Arm 才是肩关节点; ShoulderC 在锁骨处更高)。
    #   缩放锚定源鞋底 (脚不变, 由第4步贴地微调)。
    try:
        def z(name, arm, fallback):
            if name in arm.pose.bones:
                return wp(arm, name).z
            return fallback
        def zmean(arm, names, fallback):
            vals = [z(n, arm, None) for n in names]
            vals = [v for v in vals if v is not None]
            return sum(vals) / len(vals) if vals else fallback
        # 源肩: GFA 用 Arm_L/R (肩关节); 回退 Shoulder_L/R
        src_sh = zmean(src, ('Arm_L', 'Arm_R', 'Shoulder_L', 'Shoulder_R'), 0.0)
        src_floor = min((mesh.matrix_world @ v.co).z for v in mesh.data.vertices)
        # 目标肩: GFA 用 Hand1L/R (GOH 骨架肩点 = hand1l/r); 回退 clavicle
        tgt_sh = zmean(tgt, ('hand1l', 'hand1r', 'clavicle_left', 'clavicle_right'), 0.0)
        tgt_floor = ground_z
        s_gfa = (tgt_sh - tgt_floor) / (src_sh - src_floor) if abs(src_sh - src_floor) > 1e-6 else 1.0
        s_gfa = max(0.3, min(3.0, s_gfa))
        print('[rigid] GFA 肩高比整体缩放: src肩(Arm/Shoulder)-鞋底=%.2f → tgt肩(Hand1)-地面=%.2f → s=%.4f'
              % (src_sh - src_floor, tgt_sh - tgt_floor, s_gfa))
    except Exception:
        s_gfa = None
    if s_gfa is not None:
        s = s_gfa
    else:
        # 回退: 固定 R 下的尺度/平移最小二乘 (旧行为)
        Pn = np.array([(R @ p).to_tuple() for p in P])
        Qn = np.array([q.to_tuple() for q in Q])
        mp = Pn.mean(axis=0)
        mq = Qn.mean(axis=0)
        Pp = Pn - mp
        Qq = Qn - mq
        s = float(np.sum(Pp * Qq) / max(np.sum(Pp * Pp), 1e-12))
        t = mq - s * mp
    # 缩放锚定: 源鞋底点 (R 绕 Z 不影响 z, 锚点 z=src_floor)
    anchor = Vector((0.0, 0.0, src_floor)) if s_gfa is not None else Vector((0.0, 0.0, 0.0))
    # 平移最小二乘 (锚定缩放后): t = mq - s*(R·mp - anchor) - anchor
    Pn = np.array([(R @ p).to_tuple() for p in P])
    Qn = np.array([q.to_tuple() for q in Q])
    mp = Pn.mean(axis=0)
    mq = Qn.mean(axis=0)
    anchored_mp = np.array([mp[0], mp[1], mp[2] - anchor.z * (1 - s)])  # R·mp 减锚后缩放
    # 准确: t = mq - [ s*(R@mp - anchor) + anchor ]
    t = mq - (s * (mp - np.array(anchor.to_tuple()[:3])) + np.array(anchor.to_tuple()[:3]))

    M = Matrix.Identity(4)
    for i in range(3):
        for j in range(3):
            M[i][j] = R[i][j]
    # M = T(t) @ T(anchor) @ S(s) @ T(-anchor) @ R
    S_inv = Matrix.Translation(-anchor) @ M
    S_core = Matrix.Diagonal((s, s, s, 1.0)) @ S_inv
    S_full = Matrix.Translation(anchor) @ S_core
    root_empty.matrix_world = (
        Matrix.Translation(Vector(t)) @ S_full @ root_empty.matrix_world.copy())
    bpy.context.view_layer.update()

    # 3) 面向符号: Eye_L 应与目标左脚同侧, 否则绕 Z 转 180° (mirror 判据)
    try:
        eye_l = wp(src, 'Eye_L')
        foot_l = wp(tgt, 'foot1l')
        if (eye_l.y > 0) != (foot_l.y > 0):
            rz = Matrix.Rotation(math.pi, 4, 'Z')
            root_empty.matrix_world = rz @ root_empty.matrix_world
            bpy.context.view_layer.update()
    except KeyError:
        pass

    # 4) 贴地：网格脚底 bbox → 目标地面
    wpm = [mesh.matrix_world @ v.co for v in mesh.data.vertices]
    dz = ground_z - min(v.z for v in wpm)
    root_empty.location = root_empty.location + Vector((0, 0, dz))
    bpy.context.view_layer.update()


def detect_arm_pose(src):
    """检测源骨架手臂 rest 姿态模式 (帧0 分类, 2026-08-17 晚新增)。

    KK/KKS PMX 基准 = T-pose (手臂水平展开, 现有流程按此调优);
    模之屋/崩坏3/原神等标准 MMD 模型基准常为 A-pose (手臂斜下 ~45°)
    或自然垂手 —— 帧0 不同是标准 MMD 源"手陷身体/手扭"的直接原因
    (实测前鬼坊天狗: 臂向量 (0.67,0.08,-0.74) = 斜下 47°)。
    用左臂 肩→肘 世界向量与水平面夹角分类:
        <20°   → 'tpose'  (现有流程: 直接 align_arms)
        20~65° → 'apose'  (先抬到 T-pose 再走现有流程)
        >65°   → 'relax'  (垂手, 同上)
    缺标准骨名时返回 'tpose' (不干预, 交给 align_arms 硬尝试)。
    返回 (pose, angle_deg)。
    """
    try:
        arm_b = src.data.bones['Arm_L']
        elb_b = src.data.bones['Elbow_L']
    except KeyError:
        return 'tpose', 0.0
    wm = src.matrix_world
    v = (wm @ elb_b.head_local) - (wm @ arm_b.head_local)
    if v.length < 1e-6:
        return 'tpose', 0.0
    v.normalize()
    horiz = math.sqrt(v.x * v.x + v.y * v.y)
    angle = math.degrees(math.atan2(abs(v.z), horiz))  # 与水平面夹角(0=水平)
    if angle < 20:
        return 'tpose', angle
    if angle < 65:
        return 'apose', angle
    return 'relax', angle


def lift_arms_to_tpose(src):
    """把 A-pose / 垂手源手臂绕肩关节旋转到水平 T-pose (帧0 归一化)。

    标准 MMD 帧0 常为 A-pose/垂手; align_arms 的配对/迭代按 KK T-pose 调优,
    非 T-pose 起始会让腕落点错误 (实测标准 MMD 源: 手陷进身体)。
    这里先把 肩→腕 整臂绕肩旋转到水平 (腕抬到肩同高、侧向展开),
    得到近 T-pose 输入, 再复用成熟 align_arms 路径 —— 其他步骤
    (casting/绑定/导出) 全部不变。
    只转肩骨 (Arm_L/R), 肘部残余弯曲交给 align_arms 的 roll 迭代修正。
    """
    bpy.context.view_layer.objects.active = src
    bpy.ops.object.mode_set(mode='POSE')
    for side, arm_n, wrist_n, sx in (('L', 'Arm_L', 'Wrist_L', 1.0),
                                     ('R', 'Arm_R', 'Wrist_R', -1.0)):
        if arm_n not in src.pose.bones or wrist_n not in src.pose.bones:
            continue
        pivot = src.matrix_world @ src.pose.bones[arm_n].matrix.translation
        wv = (src.matrix_world @ src.pose.bones[wrist_n].matrix.translation) - pivot
        if wv.length < 1e-6:
            continue
        # 目标: 水平且保持长度; 水平方向优先沿用当前投影, 投影太弱(垂手)则强制侧向
        proj = wv.copy()
        proj.z = 0.0
        if proj.length < wv.length * 0.2:
            proj = Vector((sx * wv.length, 0.0, 0.0))
        else:
            proj = proj.normalized() * wv.length
        target = proj
        n = wv.cross(target)
        if n.length < 1e-6:
            continue
        n.normalize()
        ang = math.degrees(math.acos(max(-1.0, min(1.0,
                           wv.normalized().dot(target.normalized())))))
        if abs(ang) < 0.1:
            continue
        _set_world_rot(src, arm_n, pivot, n, ang)
        bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode='OBJECT')


def _finger_bones(src, finger, side):
    """返回源骨架中某根手指存在的关节骨名列表 (按 指根→指尖 顺序)。

    KK/KKS (cf_* 骨, INTERNAL 字典) 有完整 3 节: IndexFinger1/2/3_L + Thumb0/1/2_L;
    标准 MMD / 崩3 (前鬼坊天狗实测) 只有 2 节: IndexFinger1/2_L + Thumb0/1_L
    (无 Finger3 / Thumb2)。统一降级解析:
      四指: [Finger1, Finger2, Finger3?]  (取存在的, 至少 1 节)
      拇指: [Thumb0, Thumb1, Thumb2?]
    返回存在骨名列表; 一根都没有返回 []。
    """
    base = {'IndexFinger': 'IndexFinger', 'MiddleFinger': 'MiddleFinger',
            'RingFinger': 'RingFinger', 'LittleFinger': 'LittleFinger',
            'Thumb': 'Thumb'}[finger]
    if finger == 'Thumb':
        cands = ['%s0_%s' % (base, side), '%s1_%s' % (base, side),
                 '%s2_%s' % (base, side)]
    else:
        cands = ['%s1_%s' % (base, side), '%s2_%s' % (base, side),
                 '%s3_%s' % (base, side)]
    return [n for n in cands if n in src.pose.bones]


def goh_enlarge_head(src, factor=1.06):
    """按源 Head 骨局部尺度放大头部几何。

    只在 N 面板勾选时执行，围绕 Head head 放大带 Head 权重的网格，
    不改身体、颈根或目标骨架 rest；倍率由面板提供。1.25 并非硬性
    上限（Blender pose scale 无限制，PLY 导出只读顶点位置），
    这里仅保留 0.9~2.0 的合理范围防误输入。
    """
    if 'Head' not in src.pose.bones:
        print('[head] enlarge skip (缺 Head)')
        return 0
    factor = max(0.9, min(2.0, float(factor)))
    if abs(factor - 1.0) < 1e-7:
        return 0
    pb = src.pose.bones['Head']
    pb.scale = Vector((pb.scale.x * factor,
                       pb.scale.y * factor,
                       pb.scale.z * factor))
    bpy.context.view_layer.update()
    print('[head] optional enlarge: Head scale ×%.3f' % factor)
    return 1


def goh_normalize_head(src, tgt):
    """GOH 版 (2026-08-18): 头部大小归一化 + 微调 (GFA Step0.5 + Step2 移植)。

    GFA Step0.5 (NormalizeHeadSize): 源头部大小按参考骨架比例缩放——
      EyeLRRatio = 眼距/身长, EyeNeckRatio = 眼-颈/身长;
      RatioOffset = (EyeLR期望/EyeLR实际 × EyeNeck期望/EyeNeck实际)^0.5
      缩放 Neck 骨。
    GFA 用固定期望常数 (EyeLR 0.041682486 / EyeNeck 0.1070825, 从 GOH
    骨架实测)。注意 GOH 目标骨架 (goh_skin.mdl) **没有 Eye_L/R 骨**
    (只有 head), 无法实时算 tgt 比例 → 直接用 GFA 固定常数 (与 GFA 一致)。
    GFA Step2 头部微调 (GF2): Neck scale ×0.925, Head 沿 neck 方向压缩 ×0.85
      (head_pos = neck_pos + (head_pos - neck_pos) * 0.85)。
    在 freeze 之前、手臂对齐之前调用 (只改源骨架姿态)。
    缺 Eye/Neck 骨时跳过 (标准 MMD 有; 个别模型无眼骨时不影响导出)。
    返回 (ratio_offset, head_scale) 或 None。
    """
    def ww(a, n):
        return (a.matrix_world @ a.pose.bones[n].matrix).translation
    need = ('Eye_L', 'Eye_R', 'Neck', 'Head', 'Ankle_L', 'Ankle_R')
    for n in need:
        if n not in src.pose.bones:
            print('[head] skip (缺 %s)' % n)
            return None
    # 肩骨名兼容: GFA/3dsMax 源是 ShoulderP_L/R, KK/MMD 源是 Shoulder_L/R
    # (标准 MMD 甚至只有 Shoulder_L/R 或 ShoulderC_L/R)。取存在者。
    sh_l = next((n for n in ('ShoulderP_L', 'Shoulder_L', 'ShoulderC_L')
                 if n in src.pose.bones), None)
    sh_r = next((n for n in ('ShoulderP_R', 'Shoulder_R', 'ShoulderC_R')
                 if n in src.pose.bones), None)
    if not sh_l or not sh_r:
        print('[head] skip (缺肩骨 ShoulderP/Shoulder)')
        return None

    # GFA 固定期望比例 (从 GOH 骨架实测, TargetSkeleton 常数)
    EYE_LR_EXPECTED = 0.041682486
    EYE_NECK_EXPECTED = 0.1070825

    def ratios(arm):
        eye_l = ww(arm, 'Eye_L'); eye_r = ww(arm, 'Eye_R')
        neck = ww(arm, 'Neck')
        sh_mid = (ww(arm, sh_l) + ww(arm, sh_r)) / 2.0
        an_mid = (ww(arm, 'Ankle_L') + ww(arm, 'Ankle_R')) / 2.0
        body = sh_mid - an_mid
        bl = body.length
        if bl < 1e-3:
            return None
        eye_lr = (eye_l - eye_r).length / bl
        eye_mid = (eye_l + eye_r) / 2.0
        eye_neck = abs((eye_mid - neck).dot(body.normalized())) / bl
        return eye_lr, eye_neck

    s = ratios(src)
    if not s:
        print('[head] skip (比例计算失败)')
        return None
    s_lr, s_nk = s
    if s_lr < 1e-6 or s_nk < 1e-6:
        print('[head] skip (源比例为零)')
        return None
    ratio = math.sqrt((EYE_LR_EXPECTED / s_lr) * (EYE_NECK_EXPECTED / s_nk))
    print('[head] GFA 头部归一化: 源眼距比 %.4f (期望 %.4f) | 源眼颈比 %.4f (期望 %.4f) | ratio=%.3f'
          % (s_lr, EYE_LR_EXPECTED, s_nk, EYE_NECK_EXPECTED, ratio))

    # Step0.5: 缩放 Neck
    bpy.context.view_layer.objects.active = src
    bpy.ops.object.mode_set(mode='POSE')
    nb = src.pose.bones['Neck']
    nb.scale = nb.scale * ratio
    bpy.context.view_layer.update()

    # Step2 头部微调 (GF2): Neck scale 0.925 + Head 沿 neck 方向 0.85 压缩
    neck = ww(src, 'Neck')
    head = ww(src, 'Head')
    v = head - neck
    # 0.925 颈部缩放 (叠加在归一化 ratio 上)
    nb.scale = nb.scale * 0.925
    # Head 位置压缩: head = neck + v * 0.85
    hb = src.pose.bones['Head']
    wm = src.matrix_world @ hb.matrix
    new_head = neck + v * 0.85
    hb.matrix = src.matrix_world.inverted() @ (
        Matrix.Translation(new_head - head) @ wm)
    bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode='OBJECT')
    print('[head] GFA 头部微调: Neck ×0.925, Head 压缩 ×0.85 (ratio 总=%.3f)'
          % (ratio * 0.925))
    return ratio, ratio * 0.925


def goh_align_fingers(src, tgt, source_mode=None):
    """GOH 版 (2026-08-18 v3): 手指姿态 = GFA AutoRotateFinger 逐字移植。

    不再做多层叠加修复 (方向对齐/世界轴卷曲/顶点重映射互相干扰, 实测把
    手指拉偏)。直接按 GFA Step2 FK 的 AutoRotateFinger:

      GetExpectedRotationAxis (GFA 核心):
        对每节手指骨, 探测绕其【局部轴 X/Y/Z】旋转 ±45° 时, 手指前方
        30 单位点沿「卷曲目标方向」位移最大的局部轴 (GFA 用 Max dummy
        试转; Blender 用 bone.matrix 局部列 + 世界变换等价);
      AutoRotateFinger:
        按探测的局部轴旋转固定角度: 拇指 20°, 四指 25°×系数
        (Index 1.0/Middle 1.3/Ring 1.4/Little 1.375), 末节 ×0.75;
        旋转 = bone.matrix 右乘局部旋转 (绕局部轴, 与世界方向无关);
      GFA 手腕姿态角 (Step2 FK): Wrist_L -35° XY / Wrist_R +35° XY;
        Wrist_L +5° YZ / Wrist_R -5° YZ; 双侧 -15° XZ。

    卷曲目标方向 (RotatingTowardsAxis): GFA 用 Max 世界 -Z (掌心向下)。
    Blender 同样用世界 -Z (手心向下时手指向掌心卷 = 向 -Z 弯)。
    不改骨骼长度/平移 (只旋转), 在 goh_roll_hands 之后、freeze 之前调用。
    """
    source_mode = source_mode or detect_source_mode(tgt=tgt, src=src)

    def ww(a, n):
        return (a.matrix_world @ a.pose.bones[n].matrix).translation

    def get_expected_rot_axis(bone_name, curl_target, probe_len=30.0):
        """GFA GetExpectedRotationAxis: 返回局部轴索引 (0/1/2) + 符号,
        绕该局部轴旋转能让骨前方点沿 curl_target 位移最大。"""
        pb = src.pose.bones[bone_name]
        world_m = src.matrix_world @ pb.matrix
        pivot = world_m.translation
        # GFA dummy 的前向是当前骨的局部 +Y；只由 pose matrix
        # 变换一次，不能把 armature-space 的 matrix.col 再乘 world_m。
        front_local = Vector((0.0, 1.0, 0.0))
        front_dir = (world_m.to_3x3() @ front_local).normalized()
        front_pt = pivot + front_dir * probe_len
        best = None
        for ci in range(3):
            local_axis = Vector((0.0, 0.0, 0.0))
            local_axis[ci] = 1.0
            for sign in (1.0, -1.0):
                # 右乘真正的局部轴旋转，与 GFA ApplyRotationOnLocalAxis 相同。
                M = pb.matrix @ Matrix.Rotation(math.radians(45.0 * sign),
                                               4, local_axis)
                new_world = src.matrix_world @ M
                new_dir = (new_world.to_3x3() @ front_local).normalized()
                new_pt = new_world.translation + new_dir * probe_len
                val = (new_pt - front_pt).dot(curl_target)
                if best is None or val > best[0]:
                    best = (val, ci, sign)
        return best

    def auto_rotate_finger(chain, curl_target, ang_deg, last_ratio=0.5):
        """GFA AutoRotateFinger: 对链上每节骨绕探测的局部轴旋转固定角度。
        GFA LastBoneRotationRatio = 0.5 (末节 ×0.5)。
        链为空 (标准 MMD 无 Finger3/Thumb2) 或只有 1 节时静默跳过，
        不会 IndexError。"""
        if not chain:
            return
        for i, bn in enumerate(chain):
            pb = src.pose.bones[bn]
            res = get_expected_rot_axis(bn, curl_target)
            if res is None:
                continue
            _val, ci, sign = res
            a = ang_deg * (last_ratio if i == len(chain) - 1 else 1.0)
            a = sign * min(45.0, abs(a))
            local_axis = Vector((0.0, 0.0, 0.0))
            local_axis[ci] = 1.0
            pb.matrix = pb.matrix @ Matrix.Rotation(math.radians(a), 4,
                                                   local_axis)
            bpy.context.view_layer.update()

    # GFA 卷曲目标: 世界 -Z (Max 坐标, 掌心向下 → 手指向掌心弯)
    curl_target = Vector((0, 0, -1.0))

    # ══ v14e: 补全 GFA 段18 两块未移植操作 (审计确认遗漏) ══
    # GFA Step2 L801-833: 四指每节 (Finger1/2) 绕前臂方向对齐
    #   (AlignBoneRotationOnPlane(finger, LowerArm, XY/YZ)) —— 使手指
    #   起始朝向与前臂一致; L858-898: 指间互对齐中指 (Index/Little/Ring
    #   各节绕 MiddleFinger 的 XY/XZ/YZ) —— 四指朝向与中指一致。
    def align_rot_plane_finger(rot_pair, ref_pair, plane):
        """GFA AlignBoneRotationOnPlane 逐字移植:
        旋转 rot_pair[0] 使 (rot_pair[0]→rot_pair[1]) 平面投影与
        (ref_pair[0]→ref_pair[1]) 平行。绕 rot_pair[0] 自身 head。"""
        if not all(n in src.pose.bones for n in rot_pair):
            return 0
        if not all(n in src.pose.bones for n in ref_pair):
            return 0
        def pa(p1, p2, pl):
            d = p2 - p1
            if pl == 'XY':
                return math.degrees(math.atan2(d.y, d.x))
            if pl == 'YZ':
                return math.degrees(math.atan2(d.z, d.y))
            return math.degrees(math.atan2(d.z, d.x))
        a1 = pa(ww(src, rot_pair[0]), ww(src, rot_pair[1]), plane)
        a2 = pa(ww(src, ref_pair[0]), ww(src, ref_pair[1]), plane)
        ang = a2 - a1
        while ang < -180:
            ang += 360
        while ang > 180:
            ang -= 360
        if abs(ang) < 0.05:
            return 0
        pivot = ww(src, rot_pair[0])
        axis = {'XY': Vector((0, 0, 1.0)), 'YZ': Vector((1.0, 0, 0)),
                'XZ': Vector((0, 1.0, 0))}[plane]
        _set_world_rot(src, rot_pair[0], pivot, axis,
                       ang if plane != 'XZ' else -ang)
        return 1

    # 骨对: 每指 1/2 节 = (Finger1, Finger2) / (Finger2, Finger3)
    def finger_pair(finger, seg, side):
        n1 = '%s%s_%s' % (finger, seg, side)
        n2 = '%s%s_%s' % (finger, str(int(seg) + 1), side)
        return (n1, n2)

    # 前臂参考: MMD_LowerArmLeft = (Elbow_L, Wrist_L)
    for side, lower_arm in (('L', ('Elbow_L', 'Wrist_L')),
                            ('R', ('Elbow_R', 'Wrist_R'))):
        if not all(n in src.pose.bones for n in lower_arm):
            continue
        # ① 手指对齐前臂 (L801-833): 四指 1/2 节, XY+YZ
        for finger in ('IndexFinger', 'MiddleFinger', 'RingFinger',
                       'LittleFinger'):
            for seg in ('1', '2'):
                pair = finger_pair(finger, seg, side)
                if all(n in src.pose.bones for n in pair):
                    align_rot_plane_finger(pair, lower_arm, 'XY')
                    align_rot_plane_finger(pair, lower_arm, 'YZ')
        bpy.context.view_layer.update()
    # ② 指间互对齐中指 (L858-898):
    #   L858-863: 整指 (1→3) vs 中指整指, XY
    #   L865-877: Finger1 vs MiddleFinger1, XZ+YZ
    #   L879-891: Finger2 vs MiddleFinger2, XY+XZ
    #   L893-898: Finger2 vs MiddleFinger2, YZ
    for side in ('L', 'R'):
        mid1 = ('MiddleFinger1_' + side, 'MiddleFinger2_' + side)
        mid2 = ('MiddleFinger2_' + side, 'MiddleFinger3_' + side)
        mid_la = ('MiddleFinger1_' + side, 'MiddleFinger3_' + side)
        for finger in ('IndexFinger', 'RingFinger', 'LittleFinger'):
            # L858-863: (Finger1, Finger3) vs (Mid1, Mid3) XY
            la = ('%s1_%s' % (finger, side), '%s3_%s' % (finger, side))
            align_rot_plane_finger(la, mid_la, 'XY')
            # L865-877: Finger1 vs Mid1, XZ + YZ
            f1 = finger_pair(finger, '1', side)
            align_rot_plane_finger(f1, mid1, 'XZ')
            align_rot_plane_finger(f1, mid1, 'YZ')
            # L879-891: Finger2 vs Mid2, XY + XZ
            f2 = finger_pair(finger, '2', side)
            align_rot_plane_finger(f2, mid2, 'XY')
            align_rot_plane_finger(f2, mid2, 'XZ')
            # L893-898: Finger2 vs Mid2, YZ
            align_rot_plane_finger(f2, mid2, 'YZ')
        bpy.context.view_layer.update()

    for side in ('L', 'R'):
        # GFA AutoRotateFinger 逐字还原: 四指 GOH_FingerRotation=40°
        # (无逐指系数), 拇指 GOH_ThumbRotation=20°, 末节 ×0.5。
        # KK/KKS 直接应用 (已达标)；标准 MMD 只有 2 节手指骨, 统一
        # 40° 在一些模型上会外翻 (Shinku 实测), 由面板开关控制。
        GOH_FINGER_ROT = 40.0
        for finger in ('IndexFinger', 'MiddleFinger', 'RingFinger',
                       'LittleFinger'):
            chain = _finger_bones(src, finger, side)
            if source_mode == 'kk' or _goh_finger_curl():
                auto_rotate_finger(chain, curl_target, GOH_FINGER_ROT,
                                   last_ratio=0.5)
        # 拇指 20° 在 GFA 原文中属于 GF2-only。KK/KKS 保留自身拇指
        # 基准姿态，避免拇指根部被固定角度卷进掌心。
        if source_mode != 'kk' and _goh_finger_curl():
            tchain = _finger_bones(src, 'Thumb', side)
            auto_rotate_finger(tchain, curl_target, 20.0, last_ratio=0.5)

    # ── 手腕姿态角 ──────────────────────────────────────────────
    # GFA Step2 FK 对 GF2 (少女前线) 源模型特调: Wrist ±35° XY / ±5° YZ /
    # -15° XZ。2026-08-18 v3: KK/标准 MMD 源【关掉】—— 实测用户模型
    # 手臂肘角 9.7°/11° vs GOH 原版 6.3°/6.6° (近直线), 这组角度把
    # 手腕/前臂掰弯 → "手臂弯曲的弧线过于明显"。KK 源经 align_arms +
    # goh_roll_hands 后手腕已对齐, 不再需要 GFA 的 GF2 特调。
    # 保留常量供 GF2 类源手动开启 (面板暂不暴露, 改这里即可)。
    GOH_WRIST_POSE = False
    if GOH_WRIST_POSE:
        for side, sx in (('L', 1.0), ('R', -1.0)):
            wb_n = 'Wrist_' + side
            if wb_n not in src.pose.bones:
                continue
            piv_w = src.matrix_world @ src.pose.bones[wb_n].matrix.translation
            # XY: 左 -35° 右 +35°
            _set_world_rot(src, wb_n, piv_w, Vector((0, 0, 1.0)), -35.0 * sx)
            bpy.context.view_layer.update()
            # YZ: 左 +5° 右 -5°
            _set_world_rot(src, wb_n, piv_w, Vector((1.0, 0, 0)), 5.0 * sx)
            bpy.context.view_layer.update()
            # XZ: 双侧 -15°
            _set_world_rot(src, wb_n, piv_w, Vector((0, 1.0, 0)), -15.0)
            bpy.context.view_layer.update()


def goh_shoulder_fix(src):
    """GOH 版 (2026-08-18): 肩部微调 (GFA Step2 "Arm Further Fixing" 移植)。

    GFA (GF2): 左臂 +3° XY +1.25° YZ, 右臂 -1.25° YZ —— 补偿源模型肩部
    与 GOH 骨架的微小夹角差 (让手臂更贴合 GOH rest)。
    在 goh_roll_hands 之后、手指对齐之前调用。
    """
    for side, xy, yz in (('L', 3.0, 1.25), ('R', 0.0, -1.25)):
        an = 'Arm_' + side
        if an not in src.pose.bones:
            continue
        piv = src.matrix_world @ src.pose.bones[an].matrix.translation
        if abs(xy) > 0.1:
            _set_world_rot(src, an, piv, Vector((0, 0, 1.0)), xy)
            bpy.context.view_layer.update()
        if abs(yz) > 0.1:
            _set_world_rot(src, an, piv, Vector((1.0, 0, 0)), yz)
            bpy.context.view_layer.update()


def goh_gfa_bone_align(src, tgt, mesh=None, source_mode=None):
    """GOH v13 (2026-08-19): GFA Step2 参考版 L503-764 【主流程全文移植】。

    用户第六轮铁律: "必须把所有操作一分不差复刻到 goh 脚本内, 不允许任何
    新增修改和补充" —— 本函数为 GFA Step2_BoneAlignment.py L503 (下体预
    对齐) 到 L764 (Normalize Hand scale) 的逐段逐字 POSE 层翻译。

    v12.4 只实现了其中三段 (逐骨 Z 重排 / Shoulder Final / Neck+Head),
    本次补齐全部:
      a) OverallScale 用 Hand1 目标肩 (GFA L518/L534, 已改在 align_rigid);
      b) 上身缩放 L586-602 (UpperBodyHeightScale 根等比 + WidthExtra
         PerStep 分步, UpperBody/UpperBody2 世界 Y ×PerStep^0.5);
      c) Alignment Y L610-617 (ShoulderP/ShoulderC 均分偏移);
      d) Alignment X L634-639 (根平移 ×0.75, 肩+颈 x 差);
      e) BodyDirectionDiff 整体旋转 L539-547 + 腿上骨反向补偿 L550-551
         + 上身旋转分配 L569-584 (UB2BodyDirectionDiff ×0.5 分配);
      f) 腿 AlignBoneLength L656-667 (上腿 OtherAxisScaleFactor=1/3)
         + LowerLeg 膝高缩放 + 腿重旋转 L669-686 (Leg→目标脚方向);
      g) 手臂 L694-746 —— 差异核对: 现有 align_arms/pose_hands/
         goh_roll_hands + goh_align_bone_lengths 已实现其目标 (方向对齐
         + 长度对齐), 且 GOH_GFA_ARMS=False (v9 实证回滚让手臂弯 bug
         回来) → 不回滚, 该段对齐由现有精修流程承担;
      h) Head Further Fixing L754-760: 交由紧随其后的 goh_normalize_head
         与 Step0.5 合并执行一次，避免 Neck 0.925 / Head 0.85 重复应用;
      另补: L642-643 NormalizeScaleBreakLink (ShoulderC/Neck/上腿),
      L688-692 Normalize Foot (Ankle), L763-764 Normalize Hand (Wrist)。
    手指段 L766-898 (AlignBoneRotationOnPlane 各指节 + AutoRotateFinger)
    由 goh_align_fingers 承担 (GFA AutoRotateFinger 逐字移植)。

    坐标对照 (探针 v13_axis_probe 实测): Blender 世界与 GFA 世界一致
    (up=+Z, 左右=±Y, 前后=±X, 模型 face -X), 平面投影/绕轴旋转可直接
    使用。POSE 层操作均以当前骨世界 head 为 pivot 旋转 / 平移矩阵 /
    局部矩阵右乘缩放, 网格随 Armature modifier 自动形变。

    返回 moved 计数 (兼容调用处)。
    """
    source_mode = source_mode or detect_source_mode(tgt=tgt, src=src)
    kk_mode = source_mode == 'kk'
    print('[gfa] source branch:', source_mode)
    # v13 修正 (2026-08-19): mmd_tools 导入 (fix_ik_links=True) 会为腿建
    # IK/LIMIT_ROTATION/TRANSFORM 约束 (实测 Knee_L 有 IK+LIMIT_ROTATION、
    # KneeD_L/AnkleD_L 有 TRANSFORM), pose 矩阵写入被约束解算覆盖 →
    # 所有旋转/平移/缩放失效或歪斜 (v13 实测腿歪 15° 的根因之一)。
    # GFA 在 3dsMax 中无此类约束, 移植前必须先清除全部 pose 约束还原环境。
    try:
        for _pb in src.pose.bones:
            for _c in list(_pb.constraints):
                _pb.constraints.remove(_c)
    except Exception:
        pass
    def ww(a, n):
        return (a.matrix_world @ a.pose.bones[n].matrix).translation
    def has(a, n):
        # 可选 D 链缺失时调用方会传 None；Blender 集合的 contains
        # 对 None/嵌套元组会直接抛 TypeError，统一在探测层短路。
        return isinstance(n, str) and n in a.pose.bones

    # ---- 源链骨名自适应 (KK: Shoulder_L/R; MMD: ShoulderP_L/R) ----
    def first(names):
        for n in names:
            if has(src, n):
                return n
        return None
    B_LEG_L = first(['Leg_L', 'LegD_L'])
    B_LEG_R = first(['Leg_R', 'LegD_R'])
    B_KNEE_L = first(['Knee_L', 'KneeD_L'])
    B_KNEE_R = first(['Knee_R', 'KneeD_R'])
    B_ANK_L = first(['Ankle_L', 'AnkleD_L'])
    B_ANK_R = first(['Ankle_R', 'AnkleD_R'])
    # GFA D 链 (L487-499 原文: MMD_UpperLegDLeft=("LegD_L","KneeD_L") 等)。
    # v13 修正: GFA 对 D 链与主链【同步】执行全部腿操作 (旋转/缩放/归一/
    # 腿重旋转), 漏掉会致 D 链 (含 LegTipEX 足尖) 悬空/陷地 —— 实测
    # LegTipEX z=-5.3 陷地的根因。D 骨不存在 (标准 MMD 源) 时为 None,
    # 各操作内部 has() 检查自动跳过。
    B_LEGD_L = 'LegD_L' if has(src, 'LegD_L') else None
    B_LEGD_R = 'LegD_R' if has(src, 'LegD_R') else None
    B_KNED_L = 'KneeD_L' if has(src, 'KneeD_L') else None
    B_KNED_R = 'KneeD_R' if has(src, 'KneeD_R') else None
    B_ANKD_L = 'AnkleD_L' if has(src, 'AnkleD_L') else None
    B_ANKD_R = 'AnkleD_R' if has(src, 'AnkleD_R') else None
    B_UB = first(['UpperBody'])
    B_UB2 = first(['UpperBody2'])
    # GFA: ShoulderP = 肩胛根 (UpperBody2 子), ShoulderC = 锁骨 (手肩点)。
    # KK 单肩骨: 优先 Shoulder_L 作锁骨角色 (v12.4 已验证 Where it aligns).
    B_SHL = first(['ShoulderP_L', 'Shoulder_L', 'ShoulderSolo_L',
                   'ShoulderC_L', 'Arm_L'])
    B_SHR = first(['ShoulderP_R', 'Shoulder_R', 'ShoulderSolo_R',
                   'ShoulderC_R', 'Arm_R'])
    B_SHC_L = first(['ShoulderC_L', 'Shoulder_L', 'ShoulderSolo_L',
                     'ShoulderP_L', 'Arm_L'])
    B_SHC_R = first(['ShoulderC_R', 'Shoulder_R', 'ShoulderSolo_R',
                     'ShoulderP_R', 'Arm_R'])
    B_NCK = first(['Neck'])
    B_HD = first(['Head'])
    B_ARM_L = first(['Arm_L', 'Shoulder_L', 'ShoulderC_L', 'ShoulderP_L'])
    B_ARM_R = first(['Arm_R', 'Shoulder_R', 'ShoulderC_R', 'ShoulderP_R'])
    if not all([B_LEG_L, B_LEG_R, B_UB, B_UB2, B_SHL, B_SHR,
                 B_SHC_L, B_SHC_R, B_ARM_L, B_ARM_R, B_NCK]):
        print('[gfa] skip (缺源链骨: %s)' % [B_LEG_L, B_LEG_R, B_UB, B_UB2,
                                            B_SHL, B_SHR, B_SHC_L, B_SHC_R,
                                            B_ARM_L, B_ARM_R, B_NCK])
        return 0

    # ---- 目标侧 (GOH 骨架, 去 GFA_MWT_SKE_ 前缀) ----
    def tgt_first(names):
        for n in names:
            if has(tgt, n):
                return n
        return None
    T_HD1L = tgt_first(['hand1l'])
    T_HD1R = tgt_first(['hand1r'])
    T_HD2L = tgt_first(['hand2l'])
    T_HD2R = tgt_first(['hand2r'])
    T_F1L = tgt_first(['foot1l'])
    T_F1R = tgt_first(['foot1r'])
    T_F2L = tgt_first(['foot2l'])
    T_F2R = tgt_first(['foot2r'])
    T_F3L = tgt_first(['foot3l'])
    T_F3R = tgt_first(['foot3r'])
    T_HEAD = tgt_first(['head'])
    if not all([T_HD1L, T_HD1R, T_F1L, T_F1R, T_F2L, T_F2R, T_F3L, T_F3R,
                T_HEAD]):
        print('[gfa] skip (缺目标骨 hand1/foot1-3/head)')
        return 0

    # ---- POSE 层工具 (GFA 操作 → Blender 等价, 以骨 head 为 pivot) ----
    def mean_pos(arm, names):
        pts = [ww(arm, n) for n in names if has(arm, n)]
        if not pts:
            return Vector((0.0, 0.0, 0.0))
        return sum(pts, Vector((0.0, 0.0, 0.0))) / len(pts)

    def w_angle(p1, p2, plane):
        """GFA GetProjectedRotationOfPos: p1→p2 在 plane 上的投影角 (0..360)."""
        d = p2 - p1
        if plane == 'XY':
            ang = math.degrees(math.atan2(d.y, d.x))
        elif plane == 'YZ':
            ang = math.degrees(math.atan2(d.z, d.y))
        else:  # XZ
            ang = math.degrees(math.atan2(d.z, d.x))
        return ang + 360 if ang < 0 else ang

    def mean_pos_any(names):
        """GFA 场景级全局名语义: 目标侧端点常混用源骨名 (GFA L544 混用
        MMD_NeckBoneName; L573-574 直接用 MMD_UpperBodyChain[0]/[2] =
        源 Leg/UpperBody2), 名字先在目标骨架查, 找不到回退源骨架取位置."""
        pts = []
        for n in names:
            if has(tgt, n):
                pts.append(ww(tgt, n))
            elif has(src, n):
                pts.append(ww(src, n))
        if not pts:
            return Vector((0.0, 0.0, 0.0))
        return sum(pts, Vector((0.0, 0.0, 0.0))) / len(pts)

    def dir_diff(arm_pair_start, arm_pair_end, tgt_pair_start, tgt_pair_end,
                 plane):
        """GFA GetMeanPosDirectionDifferenceOnPlaneProjection (±180)."""
        sv = mean_pos(src, arm_pair_start)
        se = mean_pos(src, arm_pair_end)
        tv = mean_pos_any(tgt_pair_start)
        te = mean_pos_any(tgt_pair_end)
        d = w_angle(te, tv, plane) - w_angle(se, sv, plane)
        while d < -180:
            d += 360
        while d > 180:
            d -= 360
        return d

    def rot_plane(bone, angle, plane, pivot=None):
        """GFA ApplyRotationOnPlane: 绕骨 pivot (默认自身 head) 世界轴旋转.
        XY→绕Z, YZ→绕X, XZ→绕Y(负)。可选 D 链缺失时静默跳过。"""
        if not has(src, bone) or abs(angle) < 1e-7:
            return 0
        if pivot is None:
            pivot = ww(src, bone)
        axis = {'XY': Vector((0, 0, 1.0)), 'YZ': Vector((1.0, 0, 0)),
                'XZ': Vector((0, 1.0, 0))}[plane]
        _set_world_rot(src, bone, pivot, axis, angle if plane != 'XZ' else -angle)
        return 1

    def align_rot_plane(rot_pair, tgt_pair, plane):
        """GFA AlignBoneRotationOnPlane: 旋转 rot_pair[0] 使投影与 tgt 平行."""
        if not all(has(src, n) for n in rot_pair) or \
           not all(has(tgt, n) for n in tgt_pair):
            return 0
        sa = w_angle(ww(src, rot_pair[1]), ww(src, rot_pair[0]), plane)
        ta = w_angle(ww(tgt, tgt_pair[1]), ww(tgt, tgt_pair[0]), plane)
        return rot_plane(rot_pair[0], ta - sa, plane)

    def align_rot_plane_seq(rot_pair, tgt_pair, planes):
        n = 0
        for p in planes:
            n += align_rot_plane(rot_pair, tgt_pair, p)
        return n

    def scale_local_uniform(bone, s):
        """局部等比缩放骨骼 (绕骨局部原点, 子骨/网格跟随). GFA rt.scale 节点.
        v13 修正: 3dsMax rt.scale 绕节点 pivot (骨 head) 缩放, head 位置不动,
        只拉近/推远子骨与网格; Blender 局部矩阵右乘会连局部平移列一起缩放
        → head 被拉向父骨 (h 项实测 Neck z 偏 2.7 的根因)。改用 pose scale
        通道 (Loc 不变 → head 不动, 子骨偏移 ×s), 与 GFA 语义一致。"""
        if abs(s - 1.0) < 1e-7 or not has(src, bone):
            return 0
        pb = src.pose.bones[bone]
        pb.scale = Vector((pb.scale.x * s, pb.scale.y * s, pb.scale.z * s))
        bpy.context.view_layer.update()
        return 1

    def scale_local_y(bone, ratio):
        """局部 Y (骨长轴) 缩放 = GFA ApplyScaleOnLocalAxis MainAxis."""
        if abs(ratio - 1.0) < 1e-7 or not has(src, bone):
            return 0
        pb = src.pose.bones[bone]
        pb.matrix = pb.matrix @ Matrix.Scale(ratio, 4, Vector((0, 1.0, 0)))
        bpy.context.view_layer.update()
        return 1

    def scale_bone(bone, sx, sy, sz, pivot=None):
        """绕 pivot (默认骨世界 head) 做世界轴缩放 (≠ 局部):
        用于 UpperBody 宽度补偿 (世界 Y) 与 LowerLeg 膝高缩放 (世界 Z)."""
        if not has(src, bone):
            return 0
        if abs(sx - 1.0) < 1e-7 and abs(sy - 1.0) < 1e-7 and abs(sz - 1.0) < 1e-7:
            return 0
        pb = src.pose.bones[bone]
        pivot = pivot if pivot is not None else ww(src, bone)
        wm = src.matrix_world @ pb.matrix
        S = Matrix.Diagonal((sx, sy, sz, 1.0)).to_4x4()
        Tp = Matrix.Translation(pivot)
        Ti = Matrix.Translation(-pivot)
        pb.matrix = src.matrix_world.inverted() @ (Tp @ S @ Ti @ wm)
        bpy.context.view_layer.update()
        return 1

    def nudge(bone, d):
        """骨世界平移 (子骨跟随), GFA node.pos += d"""
        if d.length < 1e-7 or not has(src, bone):
            return 0
        pb = src.pose.bones[bone]
        wm = src.matrix_world @ pb.matrix
        pb.matrix = src.matrix_world.inverted() @ (Matrix.Translation(d) @ wm)
        bpy.context.view_layer.update()
        return 1

    def normalize_scale(bone):
        """GFA NormalizeScaleBreakLink: 该骨 scale 归一为等比 (各轴几何平均).
        POSE 层 = b.scale 各分量设为几何平均 (父骨缩放不在此骨内, 无需断链)."""
        if not has(src, bone):
            return 0
        pb = src.pose.bones[bone]
        s = pb.scale
        if abs(s.x) < 1e-9 or abs(s.y) < 1e-9 or abs(s.z) < 1e-9:
            return 0
        g = abs(s.x * s.y * s.z) ** (1.0 / 3.0)
        if abs(s.x - s.y) < 1e-7 and abs(s.y - s.z) < 1e-7:
            return 0
        ns = Vector((math.copysign(g, s.x), math.copysign(g, s.y),
                     math.copysign(g, s.z)))
        pb.scale = ns
        bpy.context.view_layer.update()
        return 1

    def _scale_along(arm, bone, pivot, u, sm, so):
        """绕 pivot, 沿世界方向 u 缩放 sm、垂直方向缩放 so (相似变换).
        GFA ApplyScaleOnLocalAxis 的 3dsMax 骨局部 Y = 骨长轴; Blender 中
        mmd_tools 骨骼 rest head→tail 不沿长轴 (实测 LegD rest 方向=+Z 而
        骨长轴=Leg→Knee=-Z) → 局部 Y 右乘会沿错误方向缩放 → 腿被甩飞。
        此函数按世界骨长轴方向缩放, 与 GFA 语义一致。"""
        u = u.normalized()
        ref = Vector((1.0, 0.0, 0.0)) if abs(u.x) < 0.9 else Vector((0.0, 1.0, 0.0))
        v = ref.cross(u)
        if v.length < 1e-9:
            v = Vector((0.0, 0.0, 1.0)).cross(u)
        v.normalize()
        w = u.cross(v).normalized()
        R = Matrix.Identity(3)
        for i in range(3):
            R[i][0] = u[i]; R[i][1] = v[i]; R[i][2] = w[i]
        D = Matrix.Diagonal((sm, so, so, 1.0)).to_4x4()
        S = R.to_4x4() @ D @ R.to_4x4().inverted()
        pb = arm.pose.bones[bone]
        wm = arm.matrix_world @ pb.matrix
        Tp = Matrix.Translation(pivot)
        Ti = Matrix.Translation(-pivot)
        pb.matrix = arm.matrix_world.inverted() @ (Tp @ S @ Ti @ wm)
        bpy.context.view_layer.update()

    def align_bone_length(s_pair, t_pair, main_axis='Y', factor=0.5):
        """GFA AlignBoneLength: 缩放 s_pair[0] 使骨长 (起点→终点距离) 对齐
        t_pair (目标骨长) × LengthRatioToTarget.
        v13 修正 (最终): Blender pose bone 的局部矩阵/scale 通道对"子骨偏移"
        存在坐标交叉耦合 (实测 scale.y 后子骨 x 也偏移 → 腿方向带偏 15°),
        无法直接沿骨长轴保证方向。改【直接平移子骨头】: 保持起点→终点当前
        方向不变, 把终点 (子骨) 平移到 起点 + 方向 × 目标长度 —— 网格经
        Armature 跟随, 与 GFA"缩放骨长"语义等价 (长度对齐 + 方向保持)。"""
        if not all(has(src, n) for n in s_pair) or \
           not all(has(tgt, n) for n in t_pair):
            return 0
        p0 = ww(src, s_pair[0])
        p1 = ww(src, s_pair[1])
        v1 = p1 - p0
        v2 = ww(tgt, t_pair[1]) - ww(tgt, t_pair[0])
        if v1.length < 1e-9 or v2.length < 1e-9:
            return 0
        new_pos = p0 + v1.normalized() * v2.length
        d = new_pos - p1
        if d.length < 1e-7:
            return 0
        nudge(s_pair[1], d)
        return 1

    if src.hide_get():
        src.hide_set(False)
    src.select_set(True)
    bpy.context.view_layer.objects.active = src
    bpy.ops.object.mode_set(mode='POSE')
    moved = 0

    # ============ 0) 根骨 (GFA MMD_Root) ============
    root_bone = None
    for pb in src.pose.bones:
        if not pb.parent:
            root_bone = pb.name
            break
    if root_bone is None:
        print('[gfa] skip (未找到源根骨)')
        bpy.ops.object.mode_set(mode='OBJECT')
        return 0

    # ============ 1) 腿预旋转 L503-514 (Pre-Overall: Lower Body) ============
    # GFA: 上腿/下腿 L/R (含 D 变体) YZ→XZ 对齐目标腿方向
    for s_pair, t_pair in (
        ((B_LEG_L, B_KNEE_L), (T_F1L, T_F2L)),
        ((B_LEG_R, B_KNEE_R), (T_F1R, T_F2R)),
        ((B_KNEE_L, B_ANK_L), (T_F2L, T_F3L)),
        ((B_KNEE_R, B_ANK_R), (T_F2R, T_F3R)),
        ((B_LEGD_L, B_KNED_L), (T_F1L, T_F2L)),
        ((B_LEGD_R, B_KNED_R), (T_F1R, T_F2R)),
        ((B_KNED_L, B_ANKD_L), (T_F2L, T_F3L)),
        ((B_KNED_R, B_ANKD_R), (T_F2R, T_F3R)),
    ):
        moved += align_rot_plane_seq(s_pair, t_pair, ['YZ', 'XZ'])
    bpy.context.view_layer.update()

    # ============ 2) BodyDirectionDiff 整体旋转 L539-547 ============
    # 源 (脚踝 → 肩+颈) vs 目标 (foot3 → hand1+源颈) 在 XZ 面方向差.
    # 注: GFA 原文 TargetNodeListEnd 混用源 Neck (L544), 照抄.
    body_diff = dir_diff(
        [B_ANK_L, B_ANK_R], [B_ARM_L, B_ARM_R, B_NCK, B_NCK],
        [T_F3L, T_F3R], [T_HD1L, T_HD1R, B_NCK, B_NCK], 'XZ')
    if abs(body_diff) > 0.05:   # <0.05° 视为已对齐 (rigid 已绕 Z 转)
        moved += rot_plane(root_bone, body_diff, 'XZ')
        # L550-551: 上腿反向补偿 (含 D 链, 保持腿接地方向)
        for bn in (B_LEG_L, B_LEG_R, B_LEGD_L, B_LEGD_R):
            moved += rot_plane(bn, -body_diff, 'XZ')
        print('[gfa] BodyDirectionDiff=%.2f° (整体绕Y旋转 + 上腿反向)' % body_diff)
    else:
        print('[gfa] BodyDirectionDiff=%.2f° (<0.05°, rigid 已转, 跳过)'
              % body_diff)
    bpy.context.view_layer.update()

    # ============ 3) 上身旋转分配 L569-584 ============
    # UB2BodyDirectionDiff = (UpperBody2→ShoulderP) vs (腿→UpperBody2) 在 XZ
    # 面差, ×0.5; UpperBody2 +d, Root -d/2, ShoulderP+Neck -d/2, 上腿 +d/2
    ub2_diff = dir_diff(
        [B_UB2], [B_SHL, B_SHR], [B_LEG_L, B_LEG_R], [B_UB2], 'XZ')
    ub2_diff *= 0.5
    moved += rot_plane(B_UB2, ub2_diff, 'XZ')
    moved += rot_plane(root_bone, -ub2_diff / 2, 'XZ')
    for bn in (B_SHL, B_SHR, B_NCK):
        if has(src, bn):
            moved += rot_plane(bn, -ub2_diff / 2, 'XZ')
    # GFA L583-584 原文含 D 链上腿
    for bn in (B_LEG_L, B_LEG_R, B_LEGD_L, B_LEGD_R):
        moved += rot_plane(bn, ub2_diff / 2, 'XZ')
    bpy.context.view_layer.update()

    # ============ 4) 上身缩放 L586-602 (b 项) ============
    # 原 GFA 将这一段按 GF2 源比例硬编码执行。KK/KKS 的肩宽、胸廓和
    # 上身骨链定义不同，刚性肩高拟合后再按肩宽补一次会把躯干横向拉宽，
    # 并把同一 root 下的脚/脚趾一起放大。KK 分支保留测量结果用于诊断，
    # 但不重复应用 GF2 的 root/width scale；肩点在后续由目标端点对齐。
    goh_ub_h = mean_pos(tgt, [T_HD1L, T_HD1R]).z - mean_pos(tgt, [T_F1L, T_F1R]).z
    mmd_ub_h = mean_pos(src, [B_ARM_L, B_ARM_R]).z - mean_pos(src, [B_LEG_L, B_LEG_R]).z
    h_scale = (goh_ub_h / mmd_ub_h) if abs(mmd_ub_h) > 1e-6 else 1.0
    gsv = mean_pos(tgt, [T_HD1L]) - mean_pos(tgt, [T_HD1R])
    msv = mean_pos(src, [B_SHC_L]) - mean_pos(src, [B_SHC_R])
    w_scale = (gsv.length / msv.length) if msv.length > 1e-6 else 1.0
    width_extra = (w_scale / h_scale) if abs(h_scale) > 1e-6 else 1.0
    # GFA Step2 L595-602 的自动基准。即使 KK 分支跳过 GF2 的整体
    # 躯干缩放，也保留这个几何量供 ik_updown 对应区域按需试调。
    gfa_per_step = (width_extra ** (1.0 / 2.5)
                    if width_extra > 0.0 else 1.0)
    gfa_ik_base = gfa_per_step ** 0.5
    if kk_mode:
        per_step = 1.0
        s_all = 1.0
        print('[gfa-kk] skip GF2 upper-body/shoulder-width rescale: '
              'H=%.3f W=%.3f Extra=%.3f (IK base=%.3f)'
              % (h_scale, w_scale, width_extra, gfa_ik_base))
    else:
        per_step = gfa_per_step
        s_all = h_scale * per_step
        anchor = mean_pos(src, [B_LEG_L, B_LEG_R])
        if root_bone and has(src, root_bone):
            moved += scale_bone(root_bone, s_all, s_all, s_all, pivot=anchor)
        elif B_UB and has(src, B_UB):
            moved += scale_bone(B_UB, s_all, s_all, s_all, pivot=anchor)
        for bn in (B_UB, B_UB2):
            if bn and has(src, bn):
                moved += scale_bone(bn, 1.0, gfa_ik_base, 1.0)
        print('[gfa] 上身缩放: H=%.3f W=%.3f Extra=%.3f PerStep=%.3f'
              % (h_scale, w_scale, width_extra, per_step))

    # 面板 ik_updown 宽度不再写入骨骼非等比 scale：align_arms_auto 会为
    # 保持手腕 roll 正常而把整条上身链 scale 等比化，旧倍率因而会变成整体
    # 放大而不是横向加宽。v133 在 freeze 后按最终 ik_updown 映射权重只改
    # 横向坐标，这里仅记录意图；GFA 自动基准仍由上面的原生步骤负责。
    user_factor = _goh_ik_updown_multiplier()
    src['goh_ik_updown_scale_factor'] = float(gfa_ik_base)
    src['mowas2_ik_updown_enabled'] = bool(_goh_ik_updown_enabled())
    src['mowas2_ik_updown_multiplier'] = float(user_factor)
    print('[gfa] ik_updown 横向倍率 %.3f 延后到冻结网格应用 '
          '(GFA基准 %.3f)' % (user_factor, gfa_ik_base))
    bpy.context.view_layer.update()

    # ============ 5) 整体位置 L606-608 ============
    # Root.pos += mean(GOH 上腿根=foot1) - mean(源腿根)
    g_full = mean_pos(tgt, [T_F1L, T_F1R])
    m_full = mean_pos(src, [B_LEG_L, B_LEG_R])
    moved += nudge(root_bone, g_full - m_full)
    bpy.context.view_layer.update()

    # ============ 6) Alignment Y L610-617 (c 项) ============
    # LeftShoulderOffset = hand1L.y - ShoulderC_L.y; ShoulderP_L/ShoulderC_L
    # 各 +off/2 (右同)。KK 单肩骨时 B_SHL==B_SHC, 只移一次 off/2.
    for shp, shc, hd in ((B_SHL, B_SHC_L, T_HD1L), (B_SHR, B_SHC_R, T_HD1R)):
        if not has(src, shc) or not has(src, shp):
            continue
        off = ww(tgt, hd).y - ww(src, shc).y
        moved += nudge(shp, Vector((0.0, off / 2.0, 0.0)))
        if shc != shp:
            moved += nudge(shc, Vector((0.0, off / 2.0, 0.0)))
    bpy.context.view_layer.update()

    # ============ 7) Alignment Z L619-632 (逐骨重排, v12.4 逻辑更新) ============
    # BaseZ = 源腿根均 z; Ratio = (GOH 肩(hand1) z - BaseZ)/(源 Arm 肩 z - BaseZ)
    # 对 UpperBody/UpperBody2/ShoulderP/ShoulderC/Arm 逐骨 z 重排 (GFA 列表)
    base_z = (ww(src, B_LEG_L).z + ww(src, B_LEG_R).z) / 2.0
    src_shoulder_z = (ww(src, B_ARM_L).z + ww(src, B_ARM_R).z) / 2.0
    tgt_shoulder_z = (ww(tgt, T_HD1L).z + ww(tgt, T_HD1R).z) / 2.0
    if abs(src_shoulder_z - base_z) < 1e-6:
        print('[gfa] skip (源肩-腿根距离为 0)')
    else:
        ratio = (tgt_shoulder_z - base_z) / (src_shoulder_z - base_z)
        align_bones = []
        # GFA L622 原文: MMD_UpperBodyChain[1:] + ShoulderC + MMD_Shoulder_LR
        # = UpperBody, UpperBody2, ShoulderP, ShoulderC, Arm —— 【不含 Neck】。
        # v12.4 曾加 Neck 进 Z 重排 (自创), v13 已按 GFA 去掉 (Neck 由
        # Shoulder Final 链 + 自身比例自然落位)。
        for bname in (B_UB, B_UB2, B_SHL, B_SHR, B_SHC_L, B_SHC_R,
                      B_ARM_L, B_ARM_R):
            if bname and has(src, bname) and bname not in align_bones:
                align_bones.append(bname)
        for bname in align_bones:
            pb = src.pose.bones[bname]
            cur = ww(src, bname)
            new_z = (cur.z - base_z) * ratio + base_z
            dz = new_z - cur.z
            if abs(dz) < 1e-5:
                continue
            wm = src.matrix_world @ pb.matrix
            pb.matrix = src.matrix_world.inverted() @ (
                Matrix.Translation(Vector((0.0, 0.0, dz))) @ wm)
            moved += 1
        bpy.context.view_layer.update()
        print('[gfa] Alignment Z: ratio=%.3f (源肩 %.2f→目标 %.2f, 腿根 %.2f)'
              % (ratio, src_shoulder_z, tgt_shoulder_z, base_z))

    # ============ 8) Alignment X L634-639 (d 项) ============
    # X_AlignmentRatio=0.75; 肩+颈(×2) x 差 → Root x 平移
    # GFA L636-637: GOH 侧 = Hand1×2 + 【源】Neck×2 (L637 原文混用源骨名)
    x_ratio = 0.75
    mdx = mean_pos(src, [B_ARM_L, B_ARM_R, B_NCK, B_NCK]).x
    gdx = mean_pos_any([T_HD1L, T_HD1R, B_NCK, B_NCK]).x
    moved += nudge(root_bone, Vector(((gdx - mdx) * x_ratio, 0.0, 0.0)))
    bpy.context.view_layer.update()

    # ============ 9) NormalizeScaleBreakLink L642-643 ============
    # GFA 原文: ShoulderC_L/R + Neck + 上腿 L/R + 【D 链上腿】
    for bn in (B_SHC_L, B_SHC_R, B_NCK, B_LEG_L, B_LEG_R,
               B_LEGD_L, B_LEGD_R):
        moved += normalize_scale(bn)
    bpy.context.view_layer.update()

    # ============ 10) Shoulder Final L644-650 ============
    # 源 ShoulderC x 不动, y/z → 目标 hand1 y/z
    for bname, tname in ((B_SHC_L, T_HD1L), (B_SHC_R, T_HD1R)):
        if not bname or not has(src, bname):
            continue
        tp = ww(tgt, tname)
        pb = src.pose.bones[bname]
        cur = ww(src, bname)
        d = Vector((0.0, tp.y - cur.y, tp.z - cur.z))
        if d.length < 1e-5:
            continue
        wm = src.matrix_world @ pb.matrix
        pb.matrix = src.matrix_world.inverted() @ (Matrix.Translation(d) @ wm)
        moved += 1
    bpy.context.view_layer.update()

    # ============ 10.5) 肩链最终对齐 (v14b, GFA 段10 语义在 normalize 后重保) ====
    # GFA 审计 (2026-08-20) 结论: GFA 原文的最终肩对齐 = ShoulderC→hand1 硬置
    # (L644-650), 【不】把 ShoulderP/Shoulder 对齐 clavicle (Clavicle 目标列表
    # 是死变量)。v14 曾新增 ShoulderP→clavicle —— 偏离 GFA, 且父骨移动连带
    # ShoulderC 被推离 hand1 (akq3 实测 ShoulderC z=32.79 vs 目标 28.97)。
    # v14b: 只重保 ShoulderC→hand1 y/z (在 align_arms_auto 的 normalize 之后
    # 由调用方再执行一次, 因为 normalize 上身链 scale 会把段10 的结果推走)。
    # 多级肩骨源 (GF2/MMD: ShoulderP→Shoulder→ShoulderC→Arm) 与 KK 单肩骨
    # (B_SHL==B_SHC) 都只依赖 ShoulderC→hand1, 无操作差异。
    for side, hand_t in (('L', 'hand1l'), ('R', 'hand1r')):
        shc = B_SHC_L if side == 'L' else B_SHC_R
        if not shc or not has(src, shc) or not has(tgt, hand_t):
            continue
        tp = ww(tgt, hand_t)
        cur = ww(src, shc)
        d = Vector((0.0, tp.y - cur.y, tp.z - cur.z))
        if d.length < 1e-4:
            continue
        wm = src.matrix_world @ src.pose.bones[shc].matrix
        src.pose.bones[shc].matrix = (
            src.matrix_world.inverted() @ (Matrix.Translation(d) @ wm))
        moved += 1
    bpy.context.view_layer.update()

    # ============ 11) 腿长对齐 L653-667 (f 项) ============
    # UpperLeg: AlignBoneLength(Leg→Knee vs foot1→foot2, Y, factor=1/3)
    # GFA L656-659 原文含 D 链上腿
    for s_pair, t_pair in (
        ((B_LEG_L, B_KNEE_L), (T_F1L, T_F2L)),
        ((B_LEG_R, B_KNEE_R), (T_F1R, T_F2R)),
        ((B_LEGD_L, B_KNED_L), (T_F1L, T_F2L)),
        ((B_LEGD_R, B_KNED_R), (T_F1R, T_F2R)),
    ):
        moved += align_bone_length(s_pair, t_pair, 'Y', factor=1.0 / 3.0)
    # v13 修正 (KK 源适配): GFA 原文下腿只做膝高比例缩放 (L663-667, 对
    # GF2 源≈恒等), 不做长度对齐 —— KK 源 (model_better2) 下腿比目标短
    # 9.2% → Ankle 悬空 0.78 (实测 Ankle 2.01 vs 目标 foot3 1.24)。
    # 补下腿 AlignBoneLength (Knee→Ankle vs foot2→foot3), 与上腿同语义。
    for s_pair, t_pair in (
        ((B_KNEE_L, B_ANK_L), (T_F2L, T_F3L)),
        ((B_KNEE_R, B_ANK_R), (T_F2R, T_F3R)),
        ((B_KNED_L, B_ANKD_L), (T_F2L, T_F3L)),
        ((B_KNED_R, B_ANKD_R), (T_F2R, T_F3R)),
    ):
        moved += align_bone_length(s_pair, t_pair, 'Y', factor=1.0 / 3.0)
    # LowerLeg 膝高缩放 (GFA L663-667): 源膝高 vs 膝高-鞋底; 世界 Z ×s,
    # X/Y ×s^0.25 —— 腿长度相对地面比例归一 (对齐后应≈1, 无操作)
    # GFA L663 原文: 膝高 = 主链+D 链 4 骨均值; L666 缩放 4 骨
    if mesh is not None:
        try:
            floor_z = min((mesh.matrix_world @ v.co).z for v in mesh.data.vertices)
        except Exception:
            floor_z = None
        if floor_z is not None:
            knee_zs = [ww(src, b).z for b in (B_KNEE_L, B_KNEE_R,
                                              B_KNED_L, B_KNED_R)
                       if b and has(src, b)]
            if knee_zs:
                knee_z = sum(knee_zs) / len(knee_zs)
                denom = knee_z - floor_z
                if abs(denom) > 1e-6:
                    leg_s = knee_z / denom
                    if abs(leg_s - 1.0) > 1e-4:
                        for bn in (B_KNEE_L, B_KNEE_R, B_KNED_L, B_KNED_R):
                            moved += scale_bone(bn, leg_s ** 0.25,
                                                leg_s ** 0.25, leg_s)
    bpy.context.view_layer.update()

    # ============ 12) 腿重旋转 L669-686 (f 项) ============
    # GOH_LegFinal = (Leg, foot3) 目标向量 = 源髋→目标脚踝;
    # 源向量 = (Leg, Ankle); YZ→XZ 对齐。GFA L679-686 原文含 D 链。
    # 【v13 修正】GFA L671-677: GOH_LegFinalLeft = ("Leg_L", foot3L) 的
    # 起点固定是【主链】Leg_L (场景全局名语义), D 链对同样用主链 Leg 作
    # 目标起点; 源向量起点才用各自链 (LegD→AnkleD)。原实现 D 链用
    # s_pair[0]=LegD 作目标起点 → 目标方向算错 → LegD 被多转 32°。
    for s_pair, t_f3, leg_root in (((B_LEG_L, B_ANK_L), T_F3L, B_LEG_L),
                                   ((B_LEG_R, B_ANK_R), T_F3R, B_LEG_R),
                                   ((B_LEGD_L, B_ANKD_L), T_F3L, B_LEG_L),
                                   ((B_LEGD_R, B_ANKD_R), T_F3R, B_LEG_R)):
        if not all(has(src, n) for n in s_pair) or not has(tgt, t_f3):
            continue
        if not leg_root or not has(src, leg_root):
            continue
        # 目标方向 = 目标 foot3 相对【主链源 Leg】 (GFA GOH_LegFinal 首元素)
        ta = w_angle(ww(tgt, t_f3), ww(src, leg_root), 'YZ')
        sa = w_angle(ww(src, s_pair[1]), ww(src, s_pair[0]), 'YZ')
        moved += rot_plane(s_pair[0], ta - sa, 'YZ')
        ta = w_angle(ww(tgt, t_f3), ww(src, leg_root), 'XZ')
        sa = w_angle(ww(src, s_pair[1]), ww(src, s_pair[0]), 'XZ')
        moved += rot_plane(s_pair[0], ta - sa, 'XZ')
    bpy.context.view_layer.update()

    # ============ 13) Normalize Foot Scale L688-692 ============
    # GFA 原文含 AnkleD
    for bn in (B_ANK_L, B_ANK_R, B_ANKD_L, B_ANKD_R):
        moved += normalize_scale(bn)
    bpy.context.view_layer.update()

    # GFA 后续腿部旋转/踝归一化会重新移动 Ankle 子骨。KK/KKS
    # 分支在最终姿态上再收敛一次下腿长度，消除流程顺序造成的关节折痕；
    # 方向保持当前源骨方向，不把髋/膝硬拉到目标坐标。
    if kk_mode:
        for s_pair, t_pair in (
            ((B_KNEE_L, B_ANK_L), (T_F2L, T_F3L)),
            ((B_KNEE_R, B_ANK_R), (T_F2R, T_F3R)),
            ((B_KNED_L, B_ANKD_L), (T_F2L, T_F3L)),
            ((B_KNED_R, B_ANKD_R), (T_F2R, T_F3R)),
        ):
            moved += align_bone_length(s_pair, t_pair, 'Y', factor=1.0)
        bpy.context.view_layer.update()
        print('[gfa-kk] final lower-leg endpoint lengths re-aligned')

    # ============ 14) 手臂 L694-746 (g 项, 差异核对, 不回滚) ============
    # GFA 的上臂/前臂/手链长度语义由 align_arms_auto 在步骤 14
    # 统一收敛；KK 使用世界空间端点，标准 MMD 保留旧兼容分支。
    # ============ 15) Head Further Fixing L754-760 (h 项) ============
    # 标准 MMD 的完整 Step0.5 + Step2 由紧接其后的 goh_normalize_head
    # 统一执行一次。旧代码在这里先做 0.925/0.85，随后又做第二次，导致
    # 头颈和不同权重的眼白/瞳孔层发生额外相对位移。KK 仍保持原比例。
    if kk_mode:
        print('[gfa-kk] skip GF2 fixed head/neck correction')
    else:
        print('[gfa] defer single head/neck correction to goh_normalize_head')
    bpy.context.view_layer.update()

    # ============ 16) Normalize Hand Scale L763-764 ============
    # Wrist_L/R 跨骨几何平均归一 (GFA NormalizeScaleBreakLinkEvenScale)
    wl, wr = 'Wrist_L', 'Wrist_R'
    if has(src, wl) and has(src, wr):
        sc = (list(src.pose.bones[wl].scale)
              + list(src.pose.bones[wr].scale))
        if all(abs(x) > 1e-9 for x in sc):
            mult = 1.0
            for x in sc:
                mult *= x
            g = abs(mult) ** (1.0 / 6.0)
            for bn in (wl, wr):
                pb = src.pose.bones[bn]
                ns = Vector((math.copysign(g, pb.scale.x),
                             math.copysign(g, pb.scale.y),
                             math.copysign(g, pb.scale.z)))
                if (ns - pb.scale).length > 1e-7:
                    pb.scale = ns
                    moved += 1
        bpy.context.view_layer.update()

    bpy.context.view_layer.update()
    bpy.ops.object.mode_set(mode='OBJECT')
    print('[gfa] pose align v13 (L503-764 全文移植): moved %d 项' % moved)
    return moved

def goh_align_bone_lengths(src, tgt, mirrored=False, source_mode=None):
    """完成源/目标手臂链的几何长度对齐。

    GFA 原版在 3ds Max 中缩放起始骨（Arm、Elbow）；Blender/mmd_tools 的
    rest 轴和子骨偏移并不满足“局部 Y = 骨段方向”，所以 KK/KKS 分支改为
    世界空间直接把 Arm/Elbow/Wrist 的骨头定位到目标端点。这样既保留
    蒙皮跟随，又不依赖错误的局部轴或模型常数。标准 MMD 保留旧 GFA
    分支，避免改变其已有帧0兼容行为。
    """
    def ww(a, n):
        return a.matrix_world @ a.pose.bones[n].head

    def wt(a, n):
        return a.matrix_world @ a.pose.bones[n].tail

    source_mode = source_mode or detect_source_mode(tgt=tgt, src=src)
    if source_mode == 'kk':
        def move_head_world(bone_name, target_pos):
            if bone_name not in src.pose.bones:
                return 0
            pb = src.pose.bones[bone_name]
            current = (src.matrix_world @ pb.matrix).translation
            delta = target_pos - current
            if delta.length < 1e-6:
                return 0
            world = src.matrix_world @ pb.matrix
            pb.matrix = src.matrix_world.inverted() @ (
                Matrix.Translation(delta) @ world)
            bpy.context.view_layer.update()
            return 1

        changed = 0

        # 先锚肩，再依父→子顺序定位肘和腕；父骨移动后立即更新依赖图，
        # 后续端点读到的就是最新父链。mirrored 时源 L/R 对应目标反侧。
        # Arm/Elbow/Wrist 必须先锚到目标动画的真实枢轴，绝不能绕世界中线
        # 缩放；肩宽倍率随后只改冻结几何，否则 0.90 会把腕缩入前臂 10%。
        for side in ('L', 'R'):
            tgt_side = ('r' if side == 'L' else 'l') if mirrored else side.lower()
            targets = (
                ('Arm_' + side, 'hand1' + tgt_side),
                ('Elbow_' + side, 'hand2' + tgt_side),
                ('Wrist_' + side, 'hand_rot1' + tgt_side),
            )
            for source_bone, target_bone in targets:
                if target_bone not in tgt.pose.bones:
                    continue
                tp = ww(tgt, target_bone)
                changed += move_head_world(source_bone, tp)

        # GFA 的下臂长度校准还用 MiddleFinger2 作为手端代理。KK
        # 分支不能再沿 mmd_tools 的局部 Y 缩放；以 Wrist 为锚，在世界空间
        # 按目标 hand_rot1→palm3 链长等比重定位全部手指关节，保留指间
        # 横向间距和当前卷曲姿态，避免短/长模板把指节压进掌心。
        for side in ('L', 'R'):
            tgt_side = ('r' if side == 'L' else 'l') if mirrored else side.lower()
            wrist_name = 'Wrist_' + side
            if wrist_name not in src.pose.bones:
                continue
            hand_tail = None
            for target_name in ('palm3' + tgt_side, 'palm2' + tgt_side,
                                'palm1' + tgt_side):
                if target_name in tgt.pose.bones:
                    hand_tail = wt(tgt, target_name)
                    break
            # 手链必须跟真实 hand_rot1/palm 枢轴；肩宽倍率不参与手长。
            middle = _finger_bones(src, 'MiddleFinger', side)
            source_end_name = ('MiddleFinger2_' + side
                               if 'MiddleFinger2_' + side in src.pose.bones
                               else (middle[-1] if middle else None))
            target_wrist_name = 'hand_rot1' + tgt_side
            if (hand_tail is None or source_end_name is None
                    or target_wrist_name not in tgt.pose.bones):
                continue
            wrist_pos = ww(src, wrist_name)
            source_end = ww(src, source_end_name)
            source_len = (source_end - wrist_pos).length
            target_len = (hand_tail - ww(tgt, target_wrist_name)).length
            if source_len < 1e-6 or target_len < 1e-6:
                continue
            ratio = target_len / source_len
            # 手掌/腕部连续（可选开关）：极端手长差会让指根脱离腕部。
            # 开启后整链均匀缩放改为分段渐变：指根 (i=0) 保持腕部距离、
            # 指尖按 ratio 伸展 —— 手掌始终贴住腕部，代价是中间指节
            # 不再完全贴合目标手长（KK 已验证模型默认关闭不受影响）。
            blend = _goh_hand_clamp()
            if blend:
                _clo, _chi = GOH_HAND_RATIO_CLAMP
                ratio = max(_clo, min(_chi, ratio))
            source_dir = (source_end - wrist_pos).normalized()
            target_dir = (hand_tail - ww(tgt, target_wrist_name)).normalized()
            hand_rot = source_dir.rotation_difference(target_dir)
            desired = {}
            for finger in ('IndexFinger', 'MiddleFinger', 'RingFinger',
                           'LittleFinger', 'Thumb'):
                chain = _finger_bones(src, finger, side)
                n = len(chain)
                for i, bone_name in enumerate(chain):
                    if blend and n > 1:
                        f = 1.0 + (ratio - 1.0) * (i / (n - 1))
                    else:
                        f = ratio
                    offset = ww(src, bone_name) - wrist_pos
                    desired[bone_name] = wrist_pos + (hand_rot @ offset) * f
            hand_changed = 0
            for bone_name, target_pos in desired.items():
                hand_changed += move_head_world(bone_name, target_pos)
            changed += hand_changed
            final_end = ww(src, source_end_name)
            print('[hand-kk] %s chain ratio=%.4f, end residual=%.5f, moved=%d'
                  % (side, ratio, (final_end - hand_tail).length, hand_changed))
        print('[arm-kk] target-driven arm endpoints aligned:', changed)
        return changed

    def _scale_local_y(arm, bone_name, ratio):
        b = arm.pose.bones[bone_name]
        b.matrix = b.matrix @ Matrix.Scale(ratio, 4, Vector((0.0, 1.0, 0.0)))

    changed = 0
    for side in ('L', 'R'):
        sl = side.lower()
        # 源中指第 2 节 (KK/MMD 都有 MiddleFinger2_*; 缺则回退 MiddleFinger1_*)
        m2s = ('MiddleFinger2_' + side if ('MiddleFinger2_' + side) in src.pose.bones
               else 'MiddleFinger1_' + side)
        # 目标手长: hand_rot1 head → palm3 tail (真实手链全长 0.36+1.29+0.58+0.20
        # ≈ 2.43; v10a 曾误用 palm1 head (=hand_rot1 旁边, 只有 0.36) → 手没拉伸)
        hand_tail = None
        for hn in ('palm3' + sl, 'palm2' + sl, 'palm1' + sl):
            if hn in tgt.pose.bones:
                hand_tail = wt(tgt, hn)
                break
        # 手链目标使用真实 palm 枢轴；肩宽倍率不参与手链，只在冻结后改肩部几何。
        pairs = (
            # (源子骨, 源段起点, 源段末端, 目标起点, 目标末端(可为None→用target骨tail))
            ('Elbow_' + side, 'Arm_' + side, 'Elbow_' + side, 'hand1' + sl, 'hand2' + sl),
            ('Wrist_' + side, 'Elbow_' + side, 'Wrist_' + side, 'hand2' + sl, 'hand_rot1' + sl),
            (m2s, 'Wrist_' + side, m2s, 'hand_rot1' + sl, None),
        )
        for bname, s0, s1, t0, t1 in pairs:
            try:
                p0 = ww(src, s0); p1 = ww(src, s1)
                q0 = ww(tgt, t0)
                q1 = hand_tail if t1 is None else ww(tgt, t1)
            except KeyError:
                continue
            if q1 is None:
                continue
            Lcur = (p1 - p0).length
            Lnew = (q1 - q0).length
            if Lcur < 1e-6 or Lnew < 1e-6:
                continue
            ratio = Lnew / Lcur
            # 只钳制手掌段 (t1 is None = m2s→palm3 手链): 上臂/前臂必须
            # 精确对齐目标长度, 不能钳; 手链过度拉伸才会撕开腕掌交界。
            if t1 is None and _goh_hand_clamp():
                _clo, _chi = GOH_HAND_RATIO_CLAMP
                ratio = max(_clo, min(_chi, ratio))
            _scale_local_y(src, bname, ratio)
            bpy.context.view_layer.update()
            changed += 1
    # 世界坐标重新锚定 (MMD 分支): _scale_local_y 局部矩阵右乘会把
    # 平移列一起缩放 → Elbow/Wrist 的 head 被拉离目标端点。缩放后把
    # Arm/Elbow/Wrist 精确放回目标动画的真实枢轴；肩宽倍率不参与。
    for side in ('L', 'R'):
        tgt_side = ('r' if side == 'L' else 'l') if mirrored else side.lower()
        for source_bone, target_bone in (('Arm_' + side, 'hand1' + tgt_side),
                                         ('Elbow_' + side, 'hand2' + tgt_side),
                                         ('Wrist_' + side, 'hand_rot1' + tgt_side)):
            if (source_bone.startswith('Wrist_') and source_mode == 'mmd'
                    and not GOH_DIRECT_MMD_WRIST_REANCHOR):
                continue
            if source_bone not in src.pose.bones or target_bone not in tgt.pose.bones:
                continue
            tp = ww(tgt, target_bone)
            cur = (src.matrix_world @ src.pose.bones[source_bone].matrix).translation
            d = tp - cur
            if d.length < 1e-6:
                continue
            wm = src.matrix_world @ src.pose.bones[source_bone].matrix
            src.pose.bones[source_bone].matrix = (
                src.matrix_world.inverted() @ (Matrix.Translation(d) @ wm))
            bpy.context.view_layer.update()
            changed += 1
    print('[arm] GFA 源骨骼分段长度缩放: 上臂+前臂 共调整 %d 段 (长臂模板)' % changed)
    return changed


def align_arms_auto(src, tgt, mirrored, source_mode=None):
    """帧0 自适应手臂对齐（步骤3 对齐 与 完整导出 共用入口）。

    KK/KKS 源（T-pose）直接走 align_arms（现有成熟流程）；
    标准 MMD 源（A-pose/垂手, 帧0 不同）先 lift_arms_to_tpose 抬到 T-pose
    再走同一路径 —— 避免非 T-pose 起始导致腕落点错误（实测: 手陷进身体）。
    KK/KKS 与标准 MMD 额外按源 Wrist rest 法向推导掌心向下 roll；
    不使用固定 180°。
    返回 (pose, angle_deg)。
    """
    source_mode = source_mode or detect_source_mode(tgt=tgt, src=src)
    pose, ang = detect_arm_pose(src)
    print('[arm] source arm pose: %s (上臂与水平夹角 %.1f°) | '
          'KK/T-pose 直接对齐; MMD A-pose/垂手先抬臂'
          % (pose, ang))
    # GOH 版 (2026-08-18): 头部归一化 + 微调 (GFA Step0.5 + Step2) ——
    # 在手臂对齐之前做 (源头部大小匹配 GOH 骨架比例, 避免头大/头小)。
    # 还原 GFA: GOH_GFA_SKIP_HEAD_NORM=False → 执行 goh_normalize_head
    # (GFA Step0.5 固定期望比例 + Step2 Neck×0.925 / Head×0.85 微调)。
    if (_goh_hand_split() and source_mode != 'kk'
            and not GOH_GFA_SKIP_HEAD_NORM):
        try:
            goh_normalize_head(src, tgt)
        except Exception as e:
            print('[head] normalize skip:', e)
    if _goh_enlarge_head():
        try:
            goh_enlarge_head(src, _goh_head_scale())
        except Exception as e:
            print('[head] optional enlarge skip:', e)
    if pose != 'tpose':
        lift_arms_to_tpose(src)
        print('[arm] lifted non-T-pose arms to T-pose')
    # v14 (2026-08-19): 手部修复三件套 —— 实测 (v13_step2t_probe) 全部达标:
    #   腕 delta 0.0/0.0、肘 delta 0.008/0.009、肘角 bend 0.13/0.12==目标。
    # ① 上身链 scale 归一【必须放在 align_arms 之前】: goh_gfa_bone_align 段4
    #    对 UpperBody 世界 Y 拉宽 (PerStep^0.5) 在 Blender 中留下【非等比 scale
    #    传递到 Arm 骨架矩阵】 (实测 Arm_L scale=0.998/1.271/0.968)。后续
    #    掌心 roll 绕轴旋转时 R·S≠S·R (旋转与非等比缩放不对易) → 腕会
    #    甩偏。按 GFA NormalizeScaleBreakLink 语义把整条上身链 scale 等比化
    #    (几何平均), roll 旋转变干净 (腕 0.0)。注意: normalize 会改变 UpperBody
    #    子骨位置, 必须在其后由 align_arms 的 E6.2 平移重新对齐 (若放在
    #    align_arms 之后则 Arm 被推离 hand1 → 肘角 12.7° 回归, v13_check 实测)。
    for bn in ('UpperBody', 'UpperBody2', 'Shoulder_L', 'Shoulder_R',
               'Arm_L', 'Arm_R', 'Neck'):
        if bn not in src.pose.bones:
            continue
        _pb = src.pose.bones[bn]
        _s = _pb.scale
        if abs(_s.x) < 1e-9 or abs(_s.y) < 1e-9 or abs(_s.z) < 1e-9:
            continue
        _g = abs(_s.x * _s.y * _s.z) ** (1.0 / 3.0)
        if abs(_s.x - _s.y) < 1e-6 and abs(_s.y - _s.z) < 1e-6:
            continue
        _pb.scale = Vector((math.copysign(_g, _s.x), math.copysign(_g, _s.y),
                            math.copysign(_g, _s.z)))
    bpy.context.view_layer.update()
    # normalize 会移动 UpperBody 子骨 (含 ShoulderC)，所以这里仍只把
    # ShoulderC 对齐到真实 hand1。v133 不再额外缩放中央躯干；体型宽度由
    # 上面的 ik_updown 控制和冻结后的 foot1 腿根间距共同调整。
    src['mowas2_shoulder_scale'] = 1.0
    src['mowas2_shoulder_geometry_version'] = 4
    for side, hand_t in (('L', 'hand1l'), ('R', 'hand1r')):
        _shc = next((x for x in ('ShoulderC_' + side, 'Shoulder_' + side,
                                 'ShoulderSolo_' + side)
                     if x in src.pose.bones), None)
        if not _shc or hand_t not in tgt.pose.bones:
            continue
        _tp = (tgt.matrix_world @ tgt.pose.bones[hand_t].matrix).translation
        _cur = (src.matrix_world @ src.pose.bones[_shc].matrix).translation
        _d = Vector((0.0, _tp.y - _cur.y, _tp.z - _cur.z))
        if _d.length > 1e-4:
            _wm = src.matrix_world @ src.pose.bones[_shc].matrix
            src.pose.bones[_shc].matrix = (
                src.matrix_world.inverted() @ (Matrix.Translation(_d) @ _wm))
            bpy.context.view_layer.update()
    align_arms(src, tgt, mirrored)
    # ② 平移落位 (直接移到目标骨位置, 保持当前姿态的旋转)
    #    注意: 每骨移动后必须 view_layer.update() —— pose_bone.matrix 是
    #    骨架空间矩阵 (含父链), 移动 Elbow 后不 update 的话 Wrist.matrix
    #    仍返回旧父链 → Wrist 偏移算错。肘/腕必须使用目标真实枢轴；
    #    肩宽倍率只在冻结后作用于几何，不改变这些动画枢轴。
    for side in ('L', 'R'):
        su = side.upper()
        ts = ('r' if side == 'L' else 'l') if mirrored else side.lower()
        for _bn, _tbn in (('Elbow_' + su, 'hand2' + ts),
                          ('Wrist_' + su, 'hand_rot1' + ts)):
            if (_bn.startswith('Wrist_') and source_mode == 'mmd'
                    and not GOH_DIRECT_MMD_WRIST_REANCHOR):
                continue
            if _bn not in src.pose.bones or _tbn not in tgt.pose.bones:
                continue
            _cur = (src.matrix_world @ src.pose.bones[_bn].matrix).translation
            _tgt = (tgt.matrix_world @ tgt.pose.bones[_tbn].matrix).translation
            _d = _tgt - _cur
            if _d.length < 1e-7:
                continue
            _wm = src.matrix_world @ src.pose.bones[_bn].matrix
            src.pose.bones[_bn].matrix = (
                src.matrix_world.inverted() @ (Matrix.Translation(_d) @ _wm))
            bpy.context.view_layer.update()
    bpy.context.view_layer.update()

    # KK/KKS T-pose 与标准 MMD A-pose 的源手掌 rest 法向均以
    # 手掌几何的世界 -Z 为基准；align_arms 只匹配肩→腕方向，会留下
    # 绕该轴的外翻 roll。把这个
    # rest 法向经当前 Wrist 姿态带到世界，再求绕肩→腕轴转向 -Z 的
    # 有符号角度。该角度由骨架 rest/当前姿态推导，不是固定 180°。
    if source_mode in ('kk', 'mmd'):
        roll_values = []
        for side in ('L', 'R'):
            arm_name = 'Arm_' + side
            wrist_name = 'Wrist_' + side
            if (arm_name not in src.pose.bones
                    or wrist_name not in src.pose.bones):
                continue
            arm_wm = src.matrix_world @ src.pose.bones[arm_name].matrix
            wrist_wm = src.matrix_world @ src.pose.bones[wrist_name].matrix
            axis = wrist_wm.translation - arm_wm.translation
            if axis.length < 1e-6:
                continue
            axis.normalize()
            rest_wm = (src.matrix_world
                        @ src.pose.bones[wrist_name].bone.matrix_local)
            palm_local = (rest_wm.to_3x3().inverted()
                          @ Vector((0.0, 0.0, -1.0)))
            palm_world = (wrist_wm.to_3x3() @ palm_local).normalized()
            palm_proj = palm_world - axis * palm_world.dot(axis)
            down = Vector((0.0, 0.0, -1.0))
            down_proj = down - axis * down.dot(axis)
            if palm_proj.length < 1e-6 or down_proj.length < 1e-6:
                continue
            palm_proj.normalize()
            down_proj.normalize()
            roll = math.atan2(axis.dot(palm_proj.cross(down_proj)),
                              palm_proj.dot(down_proj))
            pivot = arm_wm.translation.copy()
            pb = src.pose.bones[arm_name]
            R = Matrix.Rotation(roll, 4, axis)
            pb.matrix = src.matrix_world.inverted() @ (
                Matrix.Translation(pivot) @ R
                @ Matrix.Translation(-pivot) @ arm_wm)
            bpy.context.view_layer.update()
            roll_values.append((side, math.degrees(roll)))
        if roll_values:
            print('[arm-hand] palm-down roll derived:',
                  ', '.join('%s=%.2f°' % v for v in roll_values))

     # v10 实验: GOH_SKIP_POSE_ELBOW=True 时跳过肘弯 (elbow_cap=0), 只保留腕屈。
    if GOH_SKIP_POSE_ELBOW:
        pose_hands(src, tgt, mirrored, elbow_cap=0.0)
    else:
        pose_hands(src, tgt, mirrored)
    # GOH 版 (2026-08-18): 肩部微调 = GFA Step2 "Arm Further Fixing" 逐字移植
    # (左臂 +3° XY +1.25° YZ / 右臂 -1.25° YZ, 绕 Arm 骨 head 世界轴旋转)。
    if _goh_hand_split() and source_mode != 'kk':
        goh_shoulder_fix(src)
        print('[arm] GOH shoulder fix applied (GFA Arm Further Fixing)')
    elif _goh_hand_split():
        print('[arm-kk] skip GF2 shoulder further-fixing')
    # GOH 版 (2026-08-18): 手指姿态对齐 —— 使源手指朝向 GOH palm 骨链方向,
    # 配合分指权重让手部 IK/FK 对得上 (源张开直指 vs GOH 半握 rest 的大角度
    # 变形问题); 含 GFA 角度制卷曲 + 手腕姿态角。
    if _goh_hand_split():
        goh_align_fingers(src, tgt, source_mode=source_mode)
        print('[arm] GOH fingers aligned to palm chain (分指模式, GFA AutoRotateFinger)')
    # 手臂管线最后收敛骨段端点：KK/KKS 始终执行世界空间目标定位；
    # 标准 MMD 沿用原长臂开关和旧 GFA 兼容分支。
    if _goh_gfa_longarm() or source_mode == 'kk':
        try:
            goh_align_bone_lengths(src, tgt, mirrored=mirrored,
                                    source_mode=source_mode)
        except Exception as e:
            print('[arm] bone length scale skip:', e)
    return pose, ang


def align_arms(src, tgt, mirrored, iters=4):
    """T-pose 手臂 → 垂手：绕肩关节旋转 + 肘弯 roll 迭代（不缩放骨头）。

    E6 修正：配对必须与 mirrored 一致——mirrored=False 时源左臂 → 目标左臂
    （原表固定交叉配对，非镜像源被转到对侧射线 → 双手交叉落在身体中线/胯下）。
    v12.3: 开头的重置【只重置手臂链骨】—— goh_gfa_bone_align 在躯干骨
    (UpperBody/UpperBody2/Shoulder/Neck) 上留的 pose 必须保留, 否则
    骨骼级 GFA 被清空 (v12.1 教训)。"""
    if mirrored:
        pairs = [
            dict(src_arm='Arm_R', src_elbow='Elbow_R', src_wrist='Wrist_R',
                 tgt_shoulder='hand1l', tgt_elbow='hand2l', tgt_wrist='hand_rot1l'),
            dict(src_arm='Arm_L', src_elbow='Elbow_L', src_wrist='Wrist_L',
                 tgt_shoulder='hand1r', tgt_elbow='hand2r', tgt_wrist='hand_rot1r'),
        ]
    else:
        pairs = [
            dict(src_arm='Arm_L', src_elbow='Elbow_L', src_wrist='Wrist_L',
                 tgt_shoulder='hand1l', tgt_elbow='hand2l', tgt_wrist='hand_rot1l'),
            dict(src_arm='Arm_R', src_elbow='Elbow_R', src_wrist='Wrist_R',
                 tgt_shoulder='hand1r', tgt_elbow='hand2r', tgt_wrist='hand_rot1r'),
        ]
    bpy.context.view_layer.objects.active = src
    bpy.ops.object.mode_set(mode='POSE')
    # v12.3: 只重置手臂/手部链骨 (避免清掉躯干骨的 GFA pose)
    def _is_arm_chain(n):
        l = n.lower()
        return any(k in l for k in ('arm', 'elbow', 'wrist', 'hand', 'finger', 'thumb'))
    for pb in src.pose.bones:
        if _is_arm_chain(pb.name):
            pb.matrix_basis = Matrix.Identity(4)

    # E6.2 手臂链平移: MMD 源肩关节(≈锁骨处)比 GEM2 肩关节高 ~2.4 单位,
    # 直接绕源肩旋转会让手内收 2 单位(贴着腿)。先把整条手臂链平移到目标
    # 肩关节 (hand1l/r), 再旋转下垂。代价: 肩部接缝(三角肌/斜方肌)拉伸。
    for p in pairs:
        src_arm = p['src_arm']
        d = _wpos(tgt, p['tgt_shoulder']) - _wpos(src, src_arm)
        if d.length > 0.05:
            wm = src.matrix_world @ src.pose.bones[src_arm].matrix
            src.pose.bones[src_arm].matrix = (
                src.matrix_world.inverted() @ (Matrix.Translation(d) @ wm))
    bpy.context.view_layer.update()

    for _ in range(iters):
        changed = False
        for p in pairs:
            pivot = _wpos(src, p['src_arm'])
            v_src = _wpos(src, p['src_wrist']) - pivot
            t_sh = _wpos(tgt, p['tgt_shoulder'])
            v_tgt = _wpos(tgt, p['tgt_wrist']) - t_sh
            n = v_src.cross(v_tgt)
            if n.length < 1e-6:
                continue
            n.normalize()
            ang = math.degrees(math.acos(max(-1.0, min(1.0,
                               v_src.normalized().dot(v_tgt.normalized())))))
            if abs(ang) > 0.05:
                _set_world_rot(src, p['src_arm'], pivot, n, ang)
                changed = True
            # ── v4 (2026-08-18): 上臂方向对齐 ─────────────────────────
            # 根因: 源骨骼手臂本身直 (肘角 2.5°), 但【肩→肘方向】与目标差
            # ~11° (align_arms 只对齐了肩→腕方向, 上臂方位由源长度决定)。
            # 网格跟随源骨骼 → 绑到目标骨架上默认姿态手臂"弯" (实测网格
            # 肘角 9.6° vs 原版 4.4°)。这里绕肩旋转使源肘落到目标 肩→肘
            # 方向 (绕 肩→腕 轴旋转, 腕位置不变, 上臂方向对齐)。
            u_src = (_wpos(src, p['src_elbow']) - pivot).normalized()
            u_tgt = (_wpos(tgt, p['tgt_elbow']) - t_sh).normalized()
            axis_w = v_tgt.normalized()      # 肩→腕轴 (旋转不移动腕)
            u_src_p = u_src - axis_w * u_src.dot(axis_w)
            u_tgt_p = u_tgt - axis_w * u_tgt.dot(axis_w)
            if u_src_p.length > 1e-4 and u_tgt_p.length > 1e-4:
                u_src_p.normalize(); u_tgt_p.normalize()
                roll2 = math.degrees(math.acos(max(-1.0, min(1.0,
                                         u_src_p.dot(u_tgt_p)))))
                sgn2 = 1.0 if u_src_p.cross(u_tgt_p).dot(axis_w) > 0 else -1.0
                if abs(roll2) > 0.1:
                    _set_world_rot(src, p['src_arm'], pivot, axis_w, roll2 * sgn2)
                    changed = True
            v_new = (_wpos(src, p['src_wrist']) - pivot).normalized()
            e_src = (_wpos(src, p['src_elbow']) - pivot
                     - ((_wpos(src, p['src_elbow']) - pivot).dot(v_new)) * v_new)
            e_tgt = (_wpos(tgt, p['tgt_elbow']) - t_sh
                     - ((_wpos(tgt, p['tgt_elbow']) - t_sh).dot(v_tgt.normalized())) * v_tgt.normalized())
            if e_src.length > 1e-4 and e_tgt.length > 1e-4:
                e_src.normalize()
                e_tgt.normalize()
                sgn = 1.0 if e_src.cross(e_tgt).dot(v_new) > 0 else -1.0
                roll = math.degrees(math.acos(max(-1.0, min(1.0, e_src.dot(e_tgt))))) * sgn
                if abs(roll) > 0.05:
                    _set_world_rot(src, p['src_arm'], pivot, v_new, roll)
                    changed = True
        if not changed:
            break
    bpy.ops.object.mode_set(mode='OBJECT')


def pose_hands(src, tgt, mirrored, elbow_cap=50.0, wrist_cap=18.0, iters=10):
    """E6.3 手腕落位: 肘弯使腕达目标腕 + 腕屈使指尖接近目标掌骨水平。

    源手解剖比 GEM2 长 1.4-2.3 单位 (腕 0.9=臂长差, 掌/指 1.4-1.9=手更长):
    - 刚性卷曲手指够不到掌骨 (需折叠 130°=拳);
    - 肘弯 (GEM2 目标手臂本身带 ~34° 肘弯) 把腕带上目标;
    - 腕屈 ≤18° (自然放松手) 把指尖带向 palm3 水平。
    """
    def ww(a, n):
        return (a.matrix_world @ a.pose.bones[n].matrix).translation

    for side, eb, wb, tn_w, tn_p3 in (('L', 'Elbow_L', 'Wrist_L', 'hand_rot1l', 'palm3l'),
                                      ('R', 'Elbow_R', 'Wrist_R', 'hand_rot1r', 'palm3r')):
        # 1) 肘弯: 腕 → 目标腕
        for _ in range(iters):
            piv = ww(src, eb)
            cur = ww(src, wb)
            tgtp = ww(tgt, tn_w)
            v1 = cur - piv
            v2 = tgtp - piv
            if v1.length < 1e-6 or v2.length < 1e-6:
                break
            n = v1.cross(v2)
            if n.length < 1e-6:
                break
            n.normalize()
            ang = math.degrees(math.acos(max(-1.0, min(1.0,
                               v1.normalized().dot(v2.normalized())))))
            if abs(ang) < 0.1:
                break
            if abs(ang) > elbow_cap:
                ang = math.copysign(elbow_cap, ang)
            _set_world_rot(src, eb, piv, n, ang)
            bpy.context.view_layer.update()
        # 2) 腕屈: 中指末节 → palm3 水平 (限 18°)
        #    骨名兼容: KK 有 MiddleFinger3_L, 标准 MMD (崩3) 只有 MiddleFinger2_L
        mchain = _finger_bones(src, 'MiddleFinger', side)
        if len(mchain) < 2:
            continue
        tipn = mchain[-1]   # 指尖 = 最后一节
        for _ in range(iters):
            piv = ww(src, wb)
            cur = ww(src, tipn)
            tgtp = ww(tgt, tn_p3)
            v1 = cur - piv
            v2 = tgtp - piv
            if v1.length < 1e-6 or v2.length < 1e-6:
                break
            n = v1.cross(v2)
            if n.length < 1e-6:
                break
            n.normalize()
            ang = math.degrees(math.acos(max(-1.0, min(1.0,
                               v1.normalized().dot(v2.normalized())))))
            if abs(ang) < 0.1:
                break
            if abs(ang) > wrist_cap:
                ang = math.copysign(wrist_cap, ang)
            _set_world_rot(src, wb, piv, n, ang)
            bpy.context.view_layer.update()


def freeze_mesh(mesh):
    """evaluated 顶点写回法冻结姿态（不要用 modifier_apply——parent 链缩放会双重变换）。"""
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    dg = bpy.context.evaluated_depsgraph_get()
    ev = mesh.evaluated_get(dg)
    Mw = mesh.matrix_world.copy()
    world_verts = [(Mw @ v.co).copy() for v in ev.data.vertices]
    if mesh.data.shape_keys:
        mesh.shape_key_clear()
    for mod in list(mesh.modifiers):
        mesh.modifiers.remove(mod)
    # parent=None 会保留对象当前 world transform；此时顶点已经是世界坐标，
    # 若只写 matrix_local=I，Blender 仍可能留下源 root 的 scale/旋转，造成
    # 冻结后再次变换。显式清除 object matrix，建立唯一的 world-space bake 契约。
    mesh.parent = None
    mesh.matrix_parent_inverse = Matrix.Identity(4)
    mesh.matrix_world = Matrix.Identity(4)
    mesh.matrix_local = Matrix.Identity(4)
    for v, wv in zip(mesh.data.vertices, world_verts):
        v.co = wv
    mesh.data.update()
    bpy.context.view_layer.update()


def _smoothstep01(value):
    value = max(0.0, min(1.0, float(value)))
    return value * value * (3.0 - 2.0 * value)


def _ensure_point_vector_attribute(mesh, name):
    """Store a restorable point-coordinate baseline for viewport previews."""
    data = mesh.data
    attribute = data.attributes.get(name)
    if attribute is not None and (attribute.domain != 'POINT'
                                  or attribute.data_type != 'FLOAT_VECTOR'
                                  or len(attribute.data) != len(data.vertices)):
        data.attributes.remove(attribute)
        attribute = None
    if attribute is None:
        attribute = data.attributes.new(name=name, type='FLOAT_VECTOR',
                                        domain='POINT')
        for vertex, item in zip(data.vertices, attribute.data):
            item.vector = vertex.co
    return attribute


def _restore_point_vectors(mesh, attribute, indices):
    restored = 0
    for index in indices:
        baseline = Vector(attribute.data[index].vector)
        vertex = mesh.data.vertices[index]
        if (vertex.co - baseline).length > 1e-9:
            vertex.co = baseline
            restored += 1
    return restored


def goh_adjust_body_curve_geometry(mesh, src, tgt, mirrored=False, force=False):
    """Apply reversible ik_updown width and foot1 spacing without rest edits.

    Width is evaluated from each source group after walking unmapped bones to the
    same mapped ancestor used by bone_casting(). UpperBody2 descendants scale only
    along the target lateral axis. foot1 descendants move away from the center at
    the thigh root and fade at both the pelvis and foot2 ends. Both sides are
    accumulated from one baseline so centerline vertices cannot drift by order.
    """
    migration_version = 4
    version = 1
    ik_enabled = _goh_ik_updown_enabled()
    ik_factor = _goh_ik_updown_multiplier() if ik_enabled else 1.0
    foot_factor = _goh_foot1_spacing()
    if mesh is None or src is None or tgt is None or mesh.type != 'MESH':
        return {'changed': 0, 'skipped': 'missing mesh/source/target'}

    legacy_version = int(mesh.get('mowas2_shoulder_geometry_version', 0))
    if (force and bool(mesh.get('mowas2_frozen'))
            and legacy_version < migration_version):
        return {'changed': 0, 'skipped': 'v132 body preview requires re-align'}
    stored_version = int(mesh.get('mowas2_body_curve_version', 0))
    if (not force and stored_version >= version
            and bool(mesh.get('mowas2_ik_updown_enabled', False)) == ik_enabled
            and abs(float(mesh.get('mowas2_ik_updown_multiplier', 1.0))
                    - ik_factor) <= 1e-6
            and abs(float(mesh.get('mowas2_foot1_spacing', 1.0))
                    - foot_factor) <= 1e-6):
        return {'changed': 0, 'skipped': 'already adjusted'}

    required = ('foot1l', 'foot1r', 'foot2l', 'foot2r')
    if (not mesh.vertex_groups
            or not all(name in tgt.pose.bones for name in required)):
        return {'changed': 0, 'skipped': 'missing groups/target leg bones'}

    direct_targets = set(TGT_VG_ORDER)

    def eventual_targets(group_name):
        if group_name in direct_targets:
            return {group_name: 1.0}
        bone = src.data.bones.get(group_name)
        while bone is not None:
            canonical = resolve_pmx_bone(bone.name) or bone.name
            mapping = GFA_TO_GEM2_TARGET.get(canonical)
            if mapping:
                return {name: float(weight) for name, weight in mapping}
            bone = bone.parent
        canonical = resolve_pmx_bone(group_name) or group_name
        mapping = GFA_TO_GEM2_TARGET.get(canonical)
        return ({name: float(weight) for name, weight in mapping}
                if mapping else {})

    def target_name(name):
        if not mirrored:
            return name
        swaps = {
            'foot1l': 'foot1r', 'foot1r': 'foot1l',
            'foot2l': 'foot2r', 'foot2r': 'foot2l',
        }
        return swaps.get(name, name)

    ik_groups = {}
    arm_block_groups = set()
    foot_groups = {'l': {}, 'r': {}}
    knee_groups = {'l': {}, 'r': {}}
    for group in mesh.vertex_groups:
        raw_targets = eventual_targets(group.name)
        targets = (raw_targets if group.name in direct_targets else
                   {target_name(name): weight
                    for name, weight in raw_targets.items()})
        if any(name.startswith(('clavicle', 'hand', 'palm', 'finger'))
               for name in targets):
            arm_block_groups.add(group.index)
        if targets.get('ik_updown', 0.0) > 0.0:
            ik_groups[group.index] = targets['ik_updown']
        for side in ('l', 'r'):
            foot_weight = targets.get('foot1' + side, 0.0)
            knee_weight = targets.get('foot2' + side, 0.0)
            if foot_weight > 0.0:
                foot_groups[side][group.index] = foot_weight
            if knee_weight > 0.0:
                knee_groups[side][group.index] = knee_weight
    if not ik_groups and not foot_groups['l'] and not foot_groups['r']:
        return {'changed': 0, 'skipped': 'no body-curve vertex groups'}

    ik_vertices = {
        vertex.index for vertex in mesh.data.vertices
        if (any(assignment.group in ik_groups and assignment.weight > 1e-7
                for assignment in vertex.groups)
            and not any(assignment.group in arm_block_groups
                        and assignment.weight > 1e-7
                        for assignment in vertex.groups))
    }
    foot_vertices = {
        vertex.index for vertex in mesh.data.vertices
        if any(assignment.group in foot_groups['l']
               or assignment.group in foot_groups['r']
               for assignment in vertex.groups)
    }
    ik_attribute = mesh.data.attributes.get('gem2_ik_updown_width_base')
    foot_attribute = mesh.data.attributes.get('gem2_foot1_spacing_base')
    restored = 0
    if ik_attribute is not None and force:
        restored += _restore_point_vectors(mesh, ik_attribute, ik_vertices)
    if foot_attribute is not None and force:
        restored += _restore_point_vectors(mesh, foot_attribute, foot_vertices)
    ik_active = abs(ik_factor - 1.0) > 1e-6
    foot_active = abs(foot_factor - 1.0) > 1e-6
    if ik_attribute is None and ik_active:
        ik_attribute = _ensure_point_vector_attribute(
            mesh, 'gem2_ik_updown_width_base')
    if foot_attribute is None and foot_active:
        foot_attribute = _ensure_point_vector_attribute(
            mesh, 'gem2_foot1_spacing_base')

    def world_head(name):
        return (tgt.matrix_world @ tgt.pose.bones[name].matrix).translation

    left = world_head('foot1l')
    right = world_head('foot1r')
    lateral = left - right
    if lateral.length <= 1e-7:
        return {'changed': 0, 'skipped': 'zero foot1 span'}
    lateral.normalize()
    center = (left + right) * 0.5
    mesh_world = mesh.matrix_world
    world_to_local = mesh_world.inverted_safe().to_3x3()
    foot_geometry = {}
    for side in ('l', 'r'):
        root = world_head('foot1' + side)
        knee = world_head('foot2' + side)
        axis = knee - root
        length = axis.length
        if length <= 1e-7:
            continue
        axis.normalize()
        root_delta = lateral * (root - center).dot(lateral) * (foot_factor - 1.0)
        foot_geometry[side] = (root, axis, length, root_delta)

    changed_vertices = set()
    ik_changed = set()
    foot_changed = set()
    maximum_move = 0.0
    union_vertices = ik_vertices | foot_vertices
    for index in union_vertices:
        vertex = mesh.data.vertices[index]
        point_world = mesh_world @ vertex.co
        delta_world = Vector()
        ik_weight = 0.0
        arm_blocked = False
        foot_weights = {'l': 0.0, 'r': 0.0}
        knee_weights = {'l': 0.0, 'r': 0.0}
        for assignment in vertex.groups:
            if (assignment.group in arm_block_groups
                    and assignment.weight > 1e-7):
                arm_blocked = True
            ik_weight += (assignment.weight
                          * ik_groups.get(assignment.group, 0.0))
            for side in ('l', 'r'):
                foot_weights[side] += (
                    assignment.weight
                    * foot_groups[side].get(assignment.group, 0.0))
                knee_weights[side] += (
                    assignment.weight
                    * knee_groups[side].get(assignment.group, 0.0))
        if ik_active and ik_weight > 1e-7 and not arm_blocked:
            signed_width = (point_world - center).dot(lateral)
            contribution = (lateral * signed_width * (ik_factor - 1.0)
                            * min(1.0, ik_weight))
            delta_world += contribution
            if contribution.length > 1e-9:
                ik_changed.add(index)
        if foot_active:
            for side in ('l', 'r'):
                if foot_weights[side] <= 1e-7 or side not in foot_geometry:
                    continue
                root, axis, length, root_delta = foot_geometry[side]
                t = (point_world - root).dot(axis) / length
                pelvis_fade = _smoothstep01((t + 0.25) / 0.30)
                knee_fade = 1.0 - _smoothstep01((t - 0.05) / 0.60)
                influence = (min(1.0, foot_weights[side])
                             * max(0.0, 1.0 - min(1.0, knee_weights[side]))
                             * pelvis_fade * knee_fade)
                if influence <= 1e-7:
                    continue
                contribution = root_delta * influence
                delta_world += contribution
                if contribution.length > 1e-9:
                    foot_changed.add(index)
        delta_local = world_to_local @ delta_world
        if delta_local.length <= 1e-9:
            continue
        vertex.co += delta_local
        changed_vertices.add(index)
        maximum_move = max(maximum_move, delta_local.length)

    if changed_vertices or restored:
        mesh.data.update()
        bpy.context.view_layer.update()
    details = {
        'mode': 'body_curve_v4',
        'ik_enabled': bool(ik_enabled),
        'ik_factor': round(float(ik_factor), 7),
        'ik_vertices': len(ik_changed),
        'foot1_spacing': round(float(foot_factor), 7),
        'foot1_vertices': len(foot_changed),
        'vertices': len(changed_vertices),
        'restored': int(restored),
        'maximum_move': round(float(maximum_move), 7),
        'mirrored_source': bool(mirrored),
    }
    mesh['mowas2_body_curve_version'] = version
    mesh['mowas2_shoulder_geometry_version'] = migration_version
    mesh['mowas2_shoulder_scale'] = 1.0
    mesh['mowas2_ik_updown_enabled'] = bool(ik_enabled)
    mesh['mowas2_ik_updown_multiplier'] = float(ik_factor)
    mesh['mowas2_foot1_spacing'] = float(foot_factor)
    mesh['mowas2_body_curve_details'] = json.dumps(details, sort_keys=True)
    mesh['mowas2_shoulder_geometry_details'] = mesh['mowas2_body_curve_details']
    print('[body] ik %.3f + foot1 %.3f: %d vertices, max %.6f'
          % (ik_factor, foot_factor, len(changed_vertices), maximum_move))
    return {'changed': len(changed_vertices), 'restored': restored,
            'details': details}


def goh_adjust_shoulder_geometry(mesh, src, tgt, mirrored=False, force=False):
    """Compatibility wrapper for v132 callers."""
    return goh_adjust_body_curve_geometry(mesh, src, tgt, mirrored, force)


def goh_adjust_arm_inset_geometry(mesh, src, tgt, mirrored=False, force=False):
    """Rigidly translate each complete arm toward/away from the body center.

    Pure arm vertices receive one constant side translation, so upper arm,
    forearm, wrist, hand, fingers and attached accessories keep their internal
    lengths and proportions. Mixed clavicle/torso seam vertices blend by arm
    weight. Target rest bones and animation pivots are never edited.
    """
    version = 1
    factor = _goh_arm_span_scale()
    if mesh is None or src is None or tgt is None or mesh.type != 'MESH':
        return {'changed': 0, 'skipped': 'missing mesh/source/target'}
    stored_version = int(mesh.get('mowas2_arm_inset_version', 0))
    stored_factor = float(mesh.get('mowas2_arm_span_scale', factor))
    if (not force and stored_version >= version
            and abs(stored_factor - factor) <= 1e-6):
        return {'changed': 0, 'skipped': 'already adjusted'}
    required = ('hand1l', 'hand1r', 'foot1l', 'foot1r')
    if not all(name in tgt.pose.bones for name in required):
        return {'changed': 0, 'skipped': 'missing target arm/lateral bones'}

    direct_targets = set(TGT_VG_ORDER)

    def eventual_targets(group_name):
        if group_name in direct_targets:
            return {group_name: 1.0}
        bone = src.data.bones.get(group_name)
        while bone is not None:
            canonical = resolve_pmx_bone(bone.name) or bone.name
            mapping = GFA_TO_GEM2_TARGET.get(canonical)
            if mapping:
                return {name: float(weight) for name, weight in mapping}
            bone = bone.parent
        canonical = resolve_pmx_bone(group_name) or group_name
        mapping = GFA_TO_GEM2_TARGET.get(canonical)
        return ({name: float(weight) for name, weight in mapping}
                if mapping else {})

    def mirrored_target(name):
        if not mirrored:
            return name
        if name == 'clavicle_left':
            return 'clavicle_right'
        if name == 'clavicle_right':
            return 'clavicle_left'
        if name.startswith(('hand', 'palm', 'finger')):
            if name.endswith('l'):
                return name[:-1] + 'r'
            if name.endswith('r'):
                return name[:-1] + 'l'
        return name

    arm_groups = {'l': {}, 'r': {}}
    for group in mesh.vertex_groups:
        raw_targets = eventual_targets(group.name)
        targets = (raw_targets if group.name in direct_targets else
                   {mirrored_target(name): weight
                    for name, weight in raw_targets.items()})
        for name, weight in targets.items():
            if not name.startswith(('clavicle', 'hand', 'palm', 'finger')):
                continue
            side = ('l' if name.endswith(('l', '_left')) else
                    ('r' if name.endswith(('r', '_right')) else None))
            if side is not None and weight > 0.0:
                arm_groups[side][group.index] = max(
                    arm_groups[side].get(group.index, 0.0), weight)
    if not arm_groups['l'] and not arm_groups['r']:
        return {'changed': 0, 'skipped': 'no arm vertex groups'}

    candidates = {
        vertex.index for vertex in mesh.data.vertices
        if any(assignment.group in arm_groups['l']
               or assignment.group in arm_groups['r']
               for assignment in vertex.groups)
    }
    attribute = mesh.data.attributes.get('gem2_arm_inset_base')
    active = abs(factor - 1.0) > 1e-6
    restored = (_restore_point_vectors(mesh, attribute, candidates)
                if attribute is not None and force else 0)
    if attribute is None and active:
        attribute = _ensure_point_vector_attribute(mesh, 'gem2_arm_inset_base')

    def world_head(name):
        return (tgt.matrix_world @ tgt.pose.bones[name].matrix).translation

    left = world_head('hand1l')
    right = world_head('hand1r')
    lateral = world_head('foot1l') - world_head('foot1r')
    if lateral.length <= 1e-7:
        return {'changed': 0, 'skipped': 'zero body lateral axis'}
    lateral.normalize()
    if (left - right).dot(lateral) < 0.0:
        lateral.negate()
    center = (left + right) * 0.5
    translations = {
        'l': lateral * (left - center).dot(lateral) * (factor - 1.0),
        'r': lateral * (right - center).dot(lateral) * (factor - 1.0),
    }
    world_to_local = mesh.matrix_world.inverted_safe().to_3x3()
    local_translations = {
        side: world_to_local @ delta for side, delta in translations.items()
    }
    changed_vertices = set()
    side_vertices = {'l': set(), 'r': set()}
    maximum_move = 0.0
    if active:
        for index in candidates:
            vertex = mesh.data.vertices[index]
            weights = {'l': 0.0, 'r': 0.0}
            for assignment in vertex.groups:
                for side in ('l', 'r'):
                    weights[side] += (assignment.weight
                                      * arm_groups[side].get(
                                          assignment.group, 0.0))
            delta = Vector()
            for side in ('l', 'r'):
                influence = min(1.0, weights[side])
                if influence <= 1e-7:
                    continue
                delta += local_translations[side] * influence
                side_vertices[side].add(index)
            if delta.length <= 1e-9:
                continue
            vertex.co += delta
            changed_vertices.add(index)
            maximum_move = max(maximum_move, delta.length)

    if changed_vertices or restored:
        mesh.data.update()
        bpy.context.view_layer.update()
    details = {
        'mode': 'rigid_whole_arm_inset',
        'factor': round(float(factor), 7),
        'vertices': len(changed_vertices),
        'left_vertices': len(side_vertices['l']),
        'right_vertices': len(side_vertices['r']),
        'restored': int(restored),
        'maximum_move': round(float(maximum_move), 7),
        'left_translation': tuple(round(float(value), 7)
                                  for value in local_translations['l']),
        'right_translation': tuple(round(float(value), 7)
                                   for value in local_translations['r']),
        'mirrored_source': bool(mirrored),
    }
    mesh['mowas2_arm_inset_version'] = version
    mesh['mowas2_arm_span_scale'] = float(factor)
    mesh['mowas2_arm_inset_details'] = json.dumps(details, sort_keys=True)
    print('[arm-inset] factor %.3f: %d vertices, max %.6f'
          % (factor, len(changed_vertices), maximum_move))
    return {'changed': len(changed_vertices), 'restored': restored,
            'details': details}


def goh_scale_neck_follow_geometry(mesh, src, force=False):
    """Let neck skin/accessories follow head scale with reversible preview.

    The baseline attribute is captured after head scaling and before this pass.
    Reapplying the control first restores that baseline, so the viewport slider
    can move in either direction without accumulating deformation.
    """
    version = 2
    enabled = _goh_enlarge_head()
    factor = _goh_head_scale()
    follow = _goh_head_neck_follow()
    if mesh is None or src is None or mesh.type != 'MESH':
        return {'changed': 0, 'skipped': 'missing mesh/source'}
    stored_version = int(mesh.get('mowas2_neck_follow_version', 0))
    if (not force
            and stored_version >= version
            and bool(mesh.get('mowas2_head_enlarge_enabled', False)) == enabled
            and abs(float(mesh.get('mowas2_head_scale', factor)) - factor) <= 1e-6
            and abs(float(mesh.get('mowas2_head_neck_follow', follow))
                    - follow) <= 1e-6):
        return {'changed': 0, 'skipped': 'already adjusted'}

    def find_bone(canonical):
        if canonical in src.pose.bones:
            return canonical
        for bone in src.pose.bones:
            if resolve_pmx_bone(bone.name) == canonical:
                return bone.name
        return None

    neck_name = find_bone('Neck')
    head_name = find_bone('Head')
    if not neck_name or not head_name:
        return {'changed': 0, 'skipped': 'missing Neck/Head'}

    def under(bone, ancestor_name):
        while bone is not None:
            if bone.name == ancestor_name:
                return True
            bone = bone.parent
        return False

    head_groups = set()
    neck_groups = set()
    for group in mesh.vertex_groups:
        source_bone = src.data.bones.get(group.name)
        canonical = resolve_pmx_bone(group.name)
        if ((source_bone and under(source_bone, head_name))
                or canonical in ('Head', 'Eye_L', 'Eye_R')):
            head_groups.add(group.index)
        elif ((source_bone and under(source_bone, neck_name))
              or canonical == 'Neck'):
            neck_groups.add(group.index)

    accessory_materials = {
        index for index, material in enumerate(mesh.data.materials)
        if material and _kw_hit(
            material.get('mowas2_material_semantic_name', material.name),
            _NECK_ACCESSORY_KW)
    }
    accessory_vertices = set()
    if accessory_materials:
        for polygon in mesh.data.polygons:
            if polygon.material_index in accessory_materials:
                accessory_vertices.update(polygon.vertices)
    if not neck_groups and not accessory_vertices:
        return {'changed': 0, 'skipped': 'no neck weights/accessory materials'}

    candidate_vertices = set(accessory_vertices)
    for vertex in mesh.data.vertices:
        if any(assignment.group in neck_groups and assignment.weight > 1e-7
               for assignment in vertex.groups):
            candidate_vertices.add(vertex.index)
    attribute = mesh.data.attributes.get('gem2_neck_follow_base')
    if stored_version == 1 and attribute is None:
        return {'changed': 0, 'skipped': 'legacy neck preview requires re-align'}
    active = enabled and follow > 1e-6 and abs(factor - 1.0) > 1e-6
    if attribute is None and active:
        attribute = _ensure_point_vector_attribute(mesh,
                                                   'gem2_neck_follow_base')
    restored = (_restore_point_vectors(mesh, attribute, candidate_vertices)
                if attribute is not None and force else 0)

    if not active:
        if restored:
            mesh.data.update()
            bpy.context.view_layer.update()
        mesh['mowas2_neck_follow_version'] = version
        mesh['mowas2_head_enlarge_enabled'] = bool(enabled)
        mesh['mowas2_head_scale'] = float(factor)
        mesh['mowas2_head_neck_follow'] = float(follow)
        mesh['mowas2_neck_follow_changed'] = 0
        mesh['mowas2_neck_follow_max_move'] = 0.0
        return {'changed': 0, 'restored': restored,
                'skipped': 'disabled/no scale'}

    mesh_inv = mesh.matrix_world.inverted_safe()
    neck_world = (src.matrix_world @
                  src.pose.bones[neck_name].matrix).translation
    head_world = (src.matrix_world @
                  src.pose.bones[head_name].matrix).translation
    neck = mesh_inv @ neck_world
    head = mesh_inv @ head_world
    axis = head - neck
    axis_length_sq = axis.length_squared
    if axis_length_sq <= 1e-10:
        return {'changed': 0, 'skipped': 'zero neck axis'}
    delta_scale = (factor - 1.0) * follow
    changed = 0
    accessory_changed = 0
    maximum_move = 0.0
    for index in candidate_vertices:
        vertex = mesh.data.vertices[index]
        head_weight = 0.0
        neck_weight = 0.0
        for assignment in vertex.groups:
            if assignment.group in head_groups:
                head_weight += assignment.weight
            elif assignment.group in neck_groups:
                neck_weight += assignment.weight
        is_accessory = index in accessory_vertices
        candidate = max(neck_weight, 1.0 if is_accessory else 0.0)
        available = max(0.0, 1.0 - min(1.0, head_weight))
        influence = min(1.0, candidate, available)
        if influence <= 1e-6:
            continue
        relative = vertex.co - neck
        axial_t = relative.dot(axis) / axis_length_sq
        if axial_t <= -0.25 or axial_t >= 1.25:
            continue
        root_falloff = _smoothstep01((axial_t + 0.25) / 0.9)
        top_falloff = (1.0 - _smoothstep01((axial_t - 1.0) / 0.25)
                       if axial_t > 1.0 else 1.0)
        influence *= root_falloff * top_falloff
        if influence <= 1e-6:
            continue
        center = neck + axis * axial_t
        radial = vertex.co - center
        delta = radial * (delta_scale * influence)
        if delta.length <= 1e-9:
            continue
        vertex.co += delta
        changed += 1
        maximum_move = max(maximum_move, delta.length)
        if is_accessory:
            accessory_changed += 1

    if changed or restored:
        mesh.data.update()
        bpy.context.view_layer.update()
    mesh['mowas2_neck_follow_version'] = version
    mesh['mowas2_head_enlarge_enabled'] = bool(enabled)
    mesh['mowas2_head_scale'] = float(factor)
    mesh['mowas2_head_neck_follow'] = float(follow)
    mesh['mowas2_neck_follow_changed'] = int(changed)
    mesh['mowas2_neck_follow_max_move'] = float(maximum_move)
    print('[head] neck preview %.3f (head %.3f): %d vertices, %d accessory'
          % (follow, factor, changed, accessory_changed))
    return {'changed': changed, 'restored': restored,
            'accessory': accessory_changed, 'maximum_move': maximum_move,
            'effective_scale': 1.0 + delta_scale}


def goh_fix_pupil_depth_geometry(mesh, src=None, force=False):
    """Project pupils outside sclera with reversible viewport preview."""
    from mathutils.bvhtree import BVHTree

    version = 3
    enabled = _goh_fix_pupil_depth()
    clearance = _goh_pupil_clearance()
    if mesh is None or mesh.type != 'MESH':
        return {'changed': 0, 'skipped': 'missing mesh'}
    stored_version = int(mesh.get('mowas2_pupil_depth_version', 0))
    if (not force
            and stored_version >= version
            and bool(mesh.get('mowas2_pupil_depth_enabled', True)) == enabled
            and abs(float(mesh.get('mowas2_pupil_clearance', clearance))
                    - clearance) <= 1e-7):
        return {'changed': 0, 'skipped': 'already adjusted'}

    data = mesh.data
    data.calc_loop_triangles()

    def semantic_name(material):
        return str(material.get('mowas2_material_semantic_name', material.name))

    sclera_materials = {
        index for index, material in enumerate(data.materials)
        if material and _is_sclera_layer(semantic_name(material))
    }
    pupil_materials = {
        index for index, material in enumerate(data.materials)
        if material and _is_pupil_layer(semantic_name(material))
    }
    if not sclera_materials or not pupil_materials:
        return {'changed': 0, 'skipped': 'no sclera/pupil material pair'}

    sclera_tris = [tuple(triangle.vertices)
                   for triangle in data.loop_triangles
                   if triangle.material_index in sclera_materials]
    if not sclera_tris:
        return {'changed': 0, 'skipped': 'empty sclera geometry'}
    sclera_vertices = {index for triangle in sclera_tris for index in triangle}
    desired_clearance = {}
    pupil_vertices = set()
    for triangle in data.loop_triangles:
        if triangle.material_index not in pupil_materials:
            continue
        material = data.materials[triangle.material_index]
        name = semantic_name(material).casefold() if material else ''
        layer_clearance = clearance * (1.5 if ('+' in name
                                               or name == 'eyes_'
                                               or 'glint' in name
                                               or 'highlight' in name) else 1.0)
        for index in triangle.vertices:
            if index in sclera_vertices:
                continue
            pupil_vertices.add(index)
            desired_clearance[index] = max(
                desired_clearance.get(index, 0.0), layer_clearance)
    if not desired_clearance:
        return {'changed': 0, 'skipped': 'empty pupil geometry'}

    attribute = data.attributes.get('gem2_pupil_depth_base')
    # A v1 mesh was already moved without a baseline. Treating its current
    # coordinates as the baseline would double the correction; require a clean
    # re-align instead of silently damaging it.
    if stored_version > 0 and attribute is None:
        return {'changed': 0, 'skipped': 'legacy preview requires re-align'}
    attribute_was_present = attribute is not None
    if attribute is None and enabled:
        attribute = _ensure_point_vector_attribute(mesh,
                                                   'gem2_pupil_depth_base')
    restored = 0
    if attribute is not None and (force or attribute_was_present):
        restored = _restore_point_vectors(mesh, attribute, pupil_vertices)

    if not enabled:
        if restored:
            data.update()
            bpy.context.view_layer.update()
        mesh['mowas2_pupil_depth_version'] = version
        mesh['mowas2_pupil_depth_enabled'] = False
        mesh['mowas2_pupil_clearance'] = float(clearance)
        mesh['mowas2_pupil_depth_changed'] = 0
        mesh['mowas2_pupil_depth_min_before'] = 0.0
        mesh['mowas2_pupil_depth_min_front_before'] = 0.0
        mesh['mowas2_pupil_depth_min_front_after'] = 0.0
        mesh['mowas2_pupil_depth_min_normal_after'] = 0.0
        mesh['mowas2_pupil_depth_front_guarded'] = 0
        mesh['mowas2_pupil_depth_front_misses'] = 0
        mesh['mowas2_pupil_depth_normal_errors'] = 0
        mesh['mowas2_pupil_depth_front_errors'] = 0
        mesh['mowas2_pupil_depth_max_move'] = 0.0
        return {'changed': 0, 'restored': restored, 'skipped': 'disabled'}

    coords = [vertex.co.copy() for vertex in data.vertices]

    # Split the sclera mesh into connected eye components so a nearest/ray query
    # can never land on the opposite eye.
    vertex_triangles = {}
    for triangle_index, triangle in enumerate(sclera_tris):
        for vertex_index in triangle:
            vertex_triangles.setdefault(vertex_index, set()).add(triangle_index)
    unvisited = set(range(len(sclera_tris)))
    components = []
    while unvisited:
        pending = [unvisited.pop()]
        component_indices = set(pending)
        while pending:
            triangle_index = pending.pop()
            for vertex_index in sclera_tris[triangle_index]:
                for neighbor in vertex_triangles.get(vertex_index, ()):
                    if neighbor in unvisited:
                        unvisited.remove(neighbor)
                        component_indices.add(neighbor)
                        pending.append(neighbor)
        component_tris = [sclera_tris[index]
                          for index in component_indices]
        component_vertices = {
            index for triangle in component_tris for index in triangle
        }
        components.append((component_tris, component_vertices))
    components.sort(key=lambda value: len(value[1]), reverse=True)

    centers = []
    bone_axes = []
    mesh_inverse = mesh.matrix_world.inverted_safe()
    if src is not None and src.type == 'ARMATURE':
        seen = set()
        for bone in src.pose.bones:
            canonical = resolve_pmx_bone(bone.name) or bone.name
            if canonical not in ('Eye_L', 'Eye_R') or canonical in seen:
                continue
            world = src.matrix_world @ bone.head
            centers.append(mesh_inverse @ world)
            direction_world = (src.matrix_world.to_3x3()
                               @ (bone.tail - bone.head))
            direction_local = mesh_inverse.to_3x3() @ direction_world
            bone_axes.append(direction_local)
            seen.add(canonical)
    if not centers:
        component = components[0][1]
        centers = [sum((coords[index] for index in component), Vector())
                   / len(component)]
        bone_axes = [Vector()]

    component_centers = [
        sum((coords[index] for index in vertices), Vector()) / len(vertices)
        for _triangles, vertices in components
    ]
    eye_components = []
    unused_components = set(range(len(components)))
    for eye_center in centers:
        pool = unused_components or set(range(len(components)))
        component_index = min(
            pool,
            key=lambda value: (
                component_centers[value] - eye_center).length_squared)
        unused_components.discard(component_index)
        eye_components.append(components[component_index])
    eye_bvhs = [BVHTree.FromPolygons(coords, triangles,
                                     all_triangles=True)
                 for triangles, _vertices in eye_components]

    center_assignments = {}
    assigned_points = [[] for _center in centers]
    for index in desired_clearance:
        center_index = min(range(len(centers)),
                           key=lambda value: (
                               coords[index] - centers[value]).length_squared)
        center_assignments[index] = center_index
        assigned_points[center_index].append(coords[index])

    forward_axes = []
    eye_radii = []
    ray_extents = []
    correction_caps = []
    for center_index, eye_center in enumerate(centers):
        points = assigned_points[center_index]
        _triangles, component_vertices = eye_components[center_index]
        component_center = sum(
            (coords[index] for index in component_vertices), Vector())
        component_center /= len(component_vertices)
        pupil_direction = ((sum(points, Vector()) / len(points)) - eye_center
                           if points else component_center - eye_center)
        bone_direction = bone_axes[center_index]
        use_bone = (bone_direction.length_squared > 1e-12
                    and pupil_direction.length_squared > 1e-12)
        if use_bone:
            bone_direction.normalize()
            pupil_unit = pupil_direction.normalized()
            alignment = bone_direction.dot(pupil_unit)
            if alignment < 0.0:
                bone_direction.negate()
                alignment = -alignment
            # MMD eye bones often point roughly, but not exactly, along gaze.
            # Only trust the bone axis when it is nearly collinear with the
            # actual pupil surface; otherwise the centroid axis avoids edge-ray
            # misses at the upper iris.
            forward = (bone_direction if alignment >= 0.95
                       else pupil_direction)
        else:
            forward = pupil_direction
        if forward.length_squared <= 1e-12:
            forward = component_center - eye_center
        if forward.length_squared <= 1e-12 and points:
            forward = points[0] - component_center
        if forward.length_squared <= 1e-12:
            return {'changed': 0, 'skipped': 'cannot derive eye forward axis'}
        forward.normalize()
        forward_axes.append(forward)
        radius = max((coords[index] - eye_center).length
                     for index in component_vertices)
        projections = [(coords[index] - eye_center).dot(forward)
                       for index in component_vertices]
        depth = max(projections) - min(projections)
        eye_radii.append(radius)
        ray_extents.append(max(0.25, depth + radius * 0.5
                               + clearance * 2.0))
        correction_caps.append(min(
            max(clearance * 4.0, radius * 0.35), radius * 0.75))
    changed_vertices = set()
    clamped_vertices = set()
    minimum_before = None
    move_totals = {}
    # Re-query after every projection because the nearest sclera triangle can
    # change once the pupil crosses a neighboring face boundary.
    for pass_index in range(3):
        pass_changed = 0
        for index, target_clearance in desired_clearance.items():
            point = data.vertices[index].co
            center_index = center_assignments[index]
            nearest = eye_bvhs[center_index].find_nearest(point)
            if nearest is None or nearest[0] is None:
                continue
            location, normal, _face_index, _distance = nearest
            normal = normal.copy()
            eye_center = centers[center_index]
            radial = location - eye_center
            if radial.length_squared > 1e-12 and normal.dot(radial) < 0.0:
                normal.negate()
            signed = (point - location).dot(normal)
            if pass_index == 0:
                minimum_before = signed if minimum_before is None else min(
                    minimum_before, signed)
            effective_clearance = target_clearance * 1.05
            move = effective_clearance - signed
            if move <= 1e-7:
                continue
            moved_so_far = move_totals.get(index, 0.0)
            remaining = max(0.0, correction_caps[center_index]
                            - moved_so_far)
            if remaining <= 1e-7:
                clamped_vertices.add(index)
                continue
            if move > remaining:
                move = remaining
                clamped_vertices.add(index)
            point += normal * move
            move_totals[index] = moved_so_far + move
            changed_vertices.add(index)
            pass_changed += 1
        if pass_changed == 0:
            break

    # The normal projection can change the pupil centroid enough to matter at
    # its outer rim. Recompute the actual gaze axis from the corrected points
    # immediately before the forward-depth pass.
    for center_index, eye_center in enumerate(centers):
        indices = [index for index, assigned in center_assignments.items()
                   if assigned == center_index]
        if not indices:
            continue
        centroid = sum((data.vertices[index].co for index in indices), Vector())
        forward = centroid / len(indices) - eye_center
        if forward.length_squared > 1e-12:
            forward.normalize()
            forward_axes[center_index] = forward

    minimum_front_before = None
    front_guarded_vertices = set()
    front_misses = set()

    def front_gap(index, point):
        center_index = center_assignments[index]
        forward = forward_axes[center_index]
        extent = ray_extents[center_index]
        origin = point + forward * extent
        hit = eye_bvhs[center_index].ray_cast(
            origin, -forward, extent * 2.5)
        if hit is None or hit[0] is None:
            return None, forward
        return (point - hit[0]).dot(forward), forward

    for index, target_clearance in desired_clearance.items():
        point = data.vertices[index].co
        signed, forward = front_gap(index, point)
        if signed is None:
            front_misses.add(index)
            continue
        minimum_front_before = (signed if minimum_front_before is None else
                                min(minimum_front_before, signed))
        move = target_clearance * 1.05 - signed
        if move <= 1e-7:
            continue
        moved_so_far = move_totals.get(index, 0.0)
        center_index = center_assignments[index]
        remaining = max(0.0, correction_caps[center_index]
                        - moved_so_far)
        if remaining <= 1e-7:
            clamped_vertices.add(index)
            continue
        if move > remaining:
            move = remaining
            clamped_vertices.add(index)
        point += forward * move
        move_totals[index] = moved_so_far + move
        changed_vertices.add(index)
        front_guarded_vertices.add(index)

    minimum_front_after = None
    minimum_normal_after = None
    normal_errors = []
    front_errors = []
    for index, target_clearance in desired_clearance.items():
        point = data.vertices[index].co
        center_index = center_assignments[index]
        signed_front, _forward = front_gap(index, point)
        if signed_front is not None:
            minimum_front_after = (
                signed_front if minimum_front_after is None else
                min(minimum_front_after, signed_front))
            if signed_front + 1e-5 < target_clearance * 1.05:
                front_errors.append(index)
        nearest = eye_bvhs[center_index].find_nearest(point)
        if nearest is None or nearest[0] is None:
            normal_errors.append(index)
            continue
        location, normal, _face_index, _distance = nearest
        normal = normal.copy()
        radial = location - centers[center_index]
        if radial.length_squared > 1e-12 and normal.dot(radial) < 0.0:
            normal.negate()
        signed_normal = (point - location).dot(normal)
        minimum_normal_after = (
            signed_normal if minimum_normal_after is None else
            min(minimum_normal_after, signed_normal))
        if signed_normal + 1e-5 < target_clearance * 1.05:
            normal_errors.append(index)

    changed = len(changed_vertices)
    clamped = len(clamped_vertices)
    maximum_move = max(move_totals.values()) if move_totals else 0.0
    if changed or restored:
        data.update()
        bpy.context.view_layer.update()
    mesh['mowas2_pupil_depth_version'] = version
    mesh['mowas2_pupil_depth_enabled'] = True
    mesh['mowas2_pupil_clearance'] = float(clearance)
    mesh['mowas2_pupil_depth_changed'] = int(changed)
    mesh['mowas2_pupil_depth_min_before'] = float(minimum_before or 0.0)
    mesh['mowas2_pupil_depth_min_front_before'] = float(
        minimum_front_before or 0.0)
    mesh['mowas2_pupil_depth_min_front_after'] = float(
        minimum_front_after or 0.0)
    mesh['mowas2_pupil_depth_min_normal_after'] = float(
        minimum_normal_after or 0.0)
    mesh['mowas2_pupil_depth_front_guarded'] = len(front_guarded_vertices)
    mesh['mowas2_pupil_depth_front_misses'] = len(front_misses)
    mesh['mowas2_pupil_depth_normal_errors'] = len(normal_errors)
    mesh['mowas2_pupil_depth_front_errors'] = len(front_errors)
    mesh['mowas2_pupil_depth_max_move'] = float(maximum_move)
    print('[eye] pupil preview: %d vertices, normal %.6f->%.6f (%d err), '
          'front %.6f->%.6f (%d moved/%d miss/%d err), max %.6f, clamp %d'
          % (changed, minimum_before or 0.0,
             minimum_normal_after or 0.0, len(normal_errors),
             minimum_front_before or 0.0, minimum_front_after or 0.0,
             len(front_guarded_vertices), len(front_misses),
             len(front_errors), maximum_move, clamped))
    return {'changed': changed, 'restored': restored,
            'minimum_before': minimum_before,
            'minimum_normal_after': minimum_normal_after,
            'minimum_front_before': minimum_front_before,
            'minimum_front_after': minimum_front_after,
            'front_guarded': len(front_guarded_vertices),
            'front_misses': len(front_misses),
            'normal_errors': len(normal_errors),
            'front_errors': len(front_errors),
            'maximum_move': maximum_move, 'clamped': clamped}


def goh_retarget_mmd_forearm_geometry(mesh, src, tgt, mirrored=False,
                                      source_mode=None, force=False):
    """Map each frozen MMD forearm to the target segment without tearing wrists.

    Directly translating the source Wrist child bone makes differently weighted
    forearm/hand shells separate at their shared visual seam. Instead, keep the
    aligned elbow fixed and apply one continuous spatial map to every vertex in
    its forearm/hand capsule, regardless of material or vertex group:

    * elbow→wrist axial coordinates scale to target hand2→hand_rot1;
    * beyond the wrist the slope returns to 1, preserving hand length;
    * radial coordinates only rotate with the forearm, preserving thickness.

    The target rest skeleton is never edited. KK/KKS keep their established
    branch; this repair is for standard MMD/GF2 sources only.
    """
    version = 1
    if mesh is None or src is None or tgt is None or mesh.type != 'MESH':
        return {'changed': 0, 'skipped': 'missing mesh/source/target'}
    if not force and int(mesh.get('mowas2_forearm_retarget_version', 0)) >= version:
        return {'changed': 0, 'skipped': 'already retargeted'}
    source_mode = source_mode or detect_source_mode(tgt=tgt, src=src)
    if source_mode != 'mmd':
        return {'changed': 0, 'skipped': source_mode}

    mesh_world = mesh.matrix_world.copy()
    mesh_inv = mesh_world.inverted_safe()
    used = set()
    total = 0
    details = {}
    for source_side in ('L', 'R'):
        target_side = (('r' if source_side == 'L' else 'l')
                       if mirrored else source_side.lower())
        elbow_name = 'Elbow_' + source_side
        wrist_name = 'Wrist_' + source_side
        target_wrist = 'hand_rot1' + target_side
        if (elbow_name not in src.pose.bones or wrist_name not in src.pose.bones
                or target_wrist not in tgt.data.bones):
            continue

        elbow = src.matrix_world @ src.pose.bones[elbow_name].head
        wrist = src.matrix_world @ src.pose.bones[wrist_name].head
        desired = tgt.matrix_world @ tgt.data.bones[target_wrist].head_local
        source_vec = wrist - elbow
        target_vec = desired - elbow
        source_len = source_vec.length
        target_len = target_vec.length
        if source_len < 1e-6 or target_len < 1e-6:
            continue

        axis = source_vec.normalized()
        rotation = source_vec.rotation_difference(target_vec)
        radial_limit = max(2.0, min(3.2, source_len * 0.51))
        hand_reach = max(4.0, min(5.5, target_len * 0.95))
        moved = 0
        max_displacement = 0.0
        for vertex in mesh.data.vertices:
            if vertex.index in used:
                continue
            position = mesh_world @ vertex.co
            relative = position - elbow
            axial = relative.dot(axis)
            radial = relative - axis * axial
            if (axial < -0.05 or axial > source_len + hand_reach
                    or radial.length > radial_limit):
                continue

            if axial <= source_len:
                mapped_axial = axial * target_len / source_len
            else:
                mapped_axial = target_len + (axial - source_len)
            mapped = elbow + rotation @ (radial + axis * mapped_axial)
            # The mapping is already identity at the elbow; this short ramp also
            # avoids touching tiny negative projections from the upper-arm cap.
            blend = (max(0.0, min(1.0, (axial + 0.05) / 0.25))
                     if axial < 0.20 else 1.0)
            mapped = position.lerp(mapped, blend)
            displacement = (mapped - position).length
            if displacement <= 1e-7:
                continue
            vertex.co = mesh_inv @ mapped
            used.add(vertex.index)
            moved += 1
            total += 1
            max_displacement = max(max_displacement, displacement)

        mapped_wrist = elbow + rotation @ (axis * target_len)
        details[target_side] = {
            'source_length': source_len,
            'target_length': target_len,
            'ratio': target_len / source_len,
            'angle_deg': math.degrees(source_vec.angle(target_vec)),
            'moved': moved,
            'max_displacement': max_displacement,
            'mapped_wrist_error': (mapped_wrist - desired).length,
        }

    mesh['mowas2_forearm_retarget_version'] = version
    mesh['mowas2_forearm_retarget_count'] = total
    mesh['mowas2_forearm_retarget_details'] = json.dumps(details)
    if total:
        mesh.data.update()
        bpy.context.view_layer.update()
        print('[wrist-seam] spatial forearm retarget:', details,
              '| unique vertices', total)
    return {'changed': total, 'details': details}


def normalize_foot_geometry(mesh, src, tgt, ground_z=GROUND_Z):
    """按源脚部权重和目标 foot3/地面几何归一脚掌高度。

    KK/KKS 模型经过肩高刚性拟合后，鞋/脚掌厚度会随整体比例放大；标准
    MMD 在 GFA 上身/头部修正后也可能重新下沉。GFA 的
    骨段对齐只保证 Ankle 头点，不会改变脚部网格相对 ankle 的垂直厚度。
    这里不使用模型名或固定脚高：从源骨层级找 Ankle 后代权重，按每侧
    frozen 网格的最低点和目标 foot3 head、ground_z 推导压缩比。只改目标
    rest 空间的脚部顶点，骨骼端点和动画 rest 不动。
    """
    if mesh is None or src is None or tgt is None or not mesh.vertex_groups:
        return {'changed': 0, 'skipped': 'missing mesh/source/target'}
    if mesh.get('mowas2_foot_normalized'):
        return {'changed': 0, 'skipped': 'already normalized'}
    # 脚部尺寸倍率（用户可调）：1.0 保持旧贴地压缩；>1 时把脚/鞋
    # 往目标 ankle 锚点放大，脚底仍钳制在 ground_z 不陷地。
    foot_scale = _goh_foot_scale()

    def gfa_name(group_name):
        try:
            return resolve_pmx_bone(group_name) or group_name
        except Exception:
            return group_name

    def is_under(bone_name, roots):
        b = src.data.bones.get(bone_name)
        if b is None:
            return False
        seen = set()
        while b and b.name not in seen:
            if b.name in roots:
                return True
            seen.add(b.name)
            b = b.parent
        return False

    source_root_names = {
        'L': {'Ankle_L', 'AnkleD_L'},
        'R': {'Ankle_R', 'AnkleD_R'},
    }
    group_side = {'L': set(), 'R': set()}
    for vg in mesh.vertex_groups:
        mapped = gfa_name(vg.name)
        for side in ('L', 'R'):
            if (mapped.endswith('_' + side)
                    and (mapped.startswith('Ankle')
                         or mapped.startswith('LegTip'))):
                group_side[side].add(vg.index)
            elif is_under(vg.name, source_root_names[side]):
                group_side[side].add(vg.index)

    if not group_side['L'] and not group_side['R']:
        return {'changed': 0, 'skipped': 'no ankle descendant weights'}

    world = [mesh.matrix_world @ v.co for v in mesh.data.vertices]
    corrections = [Vector((0.0, 0.0, 0.0)) for _ in world]
    influence = [0.0] * len(world)
    ratios = {}
    selected = 0
    for side in ('L', 'R'):
        foot_groups = group_side[side]
        target_name = 'foot3' + side.lower()
        if not foot_groups or target_name not in tgt.pose.bones:
            continue
        anchor = (tgt.matrix_world @ tgt.pose.bones[target_name].head).copy()
        # 脚部尺寸倍率（用户可调）：绕「踝-地中点」做三维等比缩放，
        # 脚掌/鞋的宽度、长度、高度一起随倍率变化；缩放后按该侧
        # 最低点整体平移，把脚底钳回 ground_z —— 脚变大但绝不陷地。
        # F=1.0 保持源脚尺寸（不再把脚压扁——旧行为用 0.42 倍压缩
        # 是"脚和鞋子特别小"的根因）；F>1 更大，F<1 更小。
        # 踝关节上方 3 单位做高度渐变（factor 从 F 衰减到 1.0），
        # 大鞋/高筒靴自然向上延伸，不在踝部产生硬折痕。
        F = _goh_foot_scale()
        FADE = 3.0
        weights = {}
        for v in mesh.data.vertices:
            fw = sum(g.weight for g in v.groups if g.group in foot_groups)
            if fw >= 0.20 and world[v.index].z <= anchor.z + FADE:
                weights[v.index] = min(1.0, float(fw))
        if not weights:
            continue
        base = Vector((anchor.x, anchor.y, (ground_z + anchor.z) * 0.5))
        targets = {}
        for vi in weights:
            p = world[vi]
            dz = p.z - anchor.z
            if dz > 0.0:
                fade = max(0.0, 1.0 - dz / FADE)
                f = 1.0 + (F - 1.0) * fade
            else:
                f = F
            targets[vi] = base + (p - base) * f
        min_z = min(t.z for t in targets.values())
        lift = ground_z - min_z
        for vi, fw in weights.items():
            t = targets[vi].copy()
            t.z += lift
            corrections[vi] += t * fw
            influence[vi] += fw
            selected += 1
        ratios[side] = F

    changed = 0
    inv = mesh.matrix_world.inverted()
    for vi, inf in enumerate(influence):
        if inf <= 1e-8:
            continue
        target = corrections[vi] / min(1.0, inf)
        new_world = world[vi].lerp(target, min(1.0, inf))
        mesh.data.vertices[vi].co = inv @ new_world
        if (new_world - world[vi]).length > 1e-7:
            changed += 1
    mesh.data.update()
    mesh['mowas2_foot_normalized'] = True
    mesh['mowas2_foot_scale'] = _goh_foot_scale()
    bpy.context.view_layer.update()
    print('[foot] geometry normalized: vertices=%d ratios=%s ground=%.3f'
          % (changed, ratios, ground_z))
    return {'changed': changed, 'ratios': ratios, 'selected': selected}


def _merge_weights_to_parent(mesh_obj, arm_obj, bone_name_target_list):
    """未映射骨权重上卷到最近已映射祖先（GFA Step1 RemoveUnusedBones 的
    Blender 等价：非映射骨的权重按 1.0 合并到最近已映射父骨）。
    原位于 transfer.py，现随该冗余模块一并并入本文件。"""
    import numpy as np
    mesh = mesh_obj.data
    num_verts = len(mesh.vertices)
    vg_name_to_id = {vg.name: vg.index for vg in mesh_obj.vertex_groups}
    current_w = np.zeros((num_verts, len(mesh_obj.vertex_groups)), dtype=float)
    for vert in mesh.vertices:
        for g in vert.groups:
            current_w[vert.index, g.group] = g.weight
    target_set = set(bone_name_target_list)
    bone_to_merge_parent = {}
    for b in arm_obj.data.bones:
        if b.name in target_set:
            continue
        parent = b.parent
        while parent and parent.name not in target_set:
            parent = parent.parent
        if parent and parent.name in target_set:
            bone_to_merge_parent[b.name] = parent.name
    merged = set()
    for src_name, parent_name in bone_to_merge_parent.items():
        src_idx = vg_name_to_id.get(src_name)
        parent_idx = vg_name_to_id.get(parent_name)
        if src_idx is not None and parent_idx is not None:
            col = current_w[:, src_idx]
            mask = col > 1e-10
            if mask.any():
                current_w[mask, parent_idx] += col[mask]
            current_w[:, src_idx] = 0
            merged.add(src_name)
    row_sums = np.sum(current_w, axis=-1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    current_w /= row_sums
    current_w[current_w < 1e-10] = 0
    vg_id_to_name = {v: k for k, v in vg_name_to_id.items()}
    for vg in list(mesh_obj.vertex_groups):
        mesh_obj.vertex_groups.remove(vg)
    for vg_id in range(current_w.shape[1]):
        col = current_w[:, vg_id]
        mask = col >= 1e-10
        if mask.any():
            name = vg_id_to_name.get(vg_id, "")
            if name:
                new_vg = mesh_obj.vertex_groups.new(name=name)
                for vert_id in np.where(mask)[0]:
                    new_vg.add([int(vert_id)], col[vert_id], "REPLACE")
    return len(merged)


def bone_casting(mesh, src):
    """未映射骨权重上卷到最近已映射祖先（必须先跑，否则 cf_s_* 躯干权重全丢）。"""
    mapped = set()
    for b in src.data.bones:
        gfa = resolve_pmx_bone(b.name)
        # ca_slot06/07 (手铐腕部 slot 骨) 在 pmx_vg_to_gem2 有直接映射 →
        # 必须保留原组不铸造, 否则会被上卷到前臂 hand2 (手铐不随腕关节转)。
        if b.name in ('ca_slot06', 'ca_slot07'):
            mapped.add(b.name)
            continue
        if gfa and GFA_TO_GEM2_TARGET.get(gfa) is not None:
            mapped.add(b.name)
    return _merge_weights_to_parent(mesh, src, list(mapped))


def bind_and_transfer(mesh, tgt, mirrored):
    """绑定目标骨架 + 权重转移（镜像换名 + Wrist→hand_rot1 + top2 归一化）。"""
    import numpy as np
    mesh.parent = tgt
    mesh.matrix_parent_inverse = Matrix.Identity(4)
    mod = mesh.modifiers.new(name='Armature', type='ARMATURE')
    mod.object = tgt

    vg_names = [g.name for g in mesh.vertex_groups]
    n_vg = len(vg_names)
    n_verts = len(mesh.data.vertices)
    W = np.zeros((n_verts, n_vg), dtype=np.float64)
    for v in mesh.data.vertices:
        for g in v.groups:
            if g.group < n_vg:
                W[v.index, g.group] = g.weight
    target_cols = {name: [] for name in TGT_VG_ORDER}
    for ci, name in enumerate(vg_names):
        if name.startswith('mmd_'):
            continue
        mapping = pmx_vg_to_gem2(name, mirrored)
        if mapping is None:
            continue
        for gem2_name, w in mapping:
            if gem2_name in target_cols:
                target_cols[gem2_name].append((ci, w))
    Tw = np.zeros((n_verts, len(TGT_VG_ORDER)), dtype=np.float64)
    for ti, name in enumerate(TGT_VG_ORDER):
        for ci, w in target_cols[name]:
            Tw[:, ti] += W[:, ci] * w
    # GFA Step3 忠实还原映射分配，不做模型名特例、手部单骨强制或
    # GOH 原版形态重映射（ik_leftright 合并/上臂归躯干/脚踝收窄等）。
    # 但不同 PMX 的未映射补充骨可能让映射后的行和小于 1；必须显式按
    # 顶点归一化，避免 Blender/引擎用隐式 identity 权重制造关节折痕。
    T2 = Tw
    T2[T2 < 1e-6] = 0
    row_sum = T2.sum(axis=1)
    valid = row_sum > 1e-8
    T2[valid] /= row_sum[valid, None]
    if np.any(~valid):
        T2[~valid, TGT_VG_ORDER.index('body')] = 1.0
    # 大角度弯腰（上下身近 90°）时 ik_leftright/ik_updown 两骨大幅错动，
    # 腰腹渐变区顶点被拉开成尖角/撕裂。GOH 原版躯干几乎全部挂在
    # ik_updown（ik_leftright 仅 0-3.5%）。开启后按面板保留比例把
    # ik_leftright 权重按比例并入 ik_updown：不是"拆掉"，而是降低
    # ik_leftright 的拉扯影响，腰腹主体随 ik_updown 转动。默认关闭。
    if _goh_torso_ik_merge():
        idx_lr = TGT_VG_ORDER.index('ik_leftright')
        idx_ud = TGT_VG_ORDER.index('ik_updown')
        keep = _goh_iklr_keep()
        moved = 0
        if keep < 1.0 - 1e-6:
            for i in range(n_verts):
                wlr = T2[i, idx_lr]
                if wlr > 1e-6:
                    T2[i, idx_ud] += wlr * (1.0 - keep)
                    T2[i, idx_lr] = wlr * keep
                    moved += 1
        if moved:
            T2[T2 < 1e-6] = 0
            row_sum = T2.sum(axis=1)
            valid = row_sum > 1e-8
            T2[valid] /= row_sum[valid, None]
            print('[bind] torso ik merge (ik_leftright×%.2f → ik_updown): '
                  '%d verts' % (keep, moved))
    print('[bind] mapped weight sums normalized: valid=%d fallback_body=%d'
          % (int(valid.sum()), int((~valid).sum())))
    for vg in list(mesh.vertex_groups):
        mesh.vertex_groups.remove(vg)
    vg_objs = [mesh.vertex_groups.new(name=n) for n in TGT_VG_ORDER]
    for ti in range(len(TGT_VG_ORDER)):
        mask = T2[:, ti] > 0
        for vi in np.where(mask)[0]:
            vg_objs[ti].add([int(vi)], float(T2[vi, ti]), 'REPLACE')
    bpy.context.view_layer.update()


def goh_recover_wrist_anchor(mesh, src, tgt, mirrored=False, force=False):
    """Move an already-frozen hand back from a wrongly shoulder-scaled wrist.

    Older pipeline runs multiplied the world Y of Elbow/Wrist targets by the
    shoulder-width control. A value of 0.90 therefore pulled a wrist at Y=8.0
    inward by 0.8 units before the mesh was frozen. Move all target hand-chain
    vertices by the measured source-to-target wrist delta and fade that delta
    through the final forearm segment. Target rest bones and weights stay intact.
    """
    version = 1
    if not force and int(mesh.get('mowas2_forearm_retarget_version', 0)) >= 1:
        return 0
    if not force and int(mesh.get('mowas2_wrist_geometry_version', 0)) >= version:
        return 0
    if not force and not _goh_wrist_stitch():
        return 0
    if (mesh.type != 'MESH' or src is None or src.type != 'ARMATURE'
            or tgt is None or tgt.type != 'ARMATURE'):
        return 0

    mesh_inv = mesh.matrix_world.inverted_safe()
    moved_vertices = set()
    per_side = {}
    deltas = {}
    for target_side in ('l', 'r'):
        source_side = ('R' if target_side == 'l' else 'L') if mirrored else target_side.upper()
        source_name = 'Wrist_' + source_side
        target_name = 'hand_rot1' + target_side
        forearm_name = 'hand2' + target_side
        if (source_name not in src.pose.bones or target_name not in tgt.data.bones
                or forearm_name not in tgt.data.bones):
            continue

        source_world = src.matrix_world @ src.pose.bones[source_name].head
        target_world = tgt.matrix_world @ tgt.data.bones[target_name].head_local
        delta_world = target_world - source_world
        delta_length = delta_world.length
        # Normal retarget residuals are sub-centimetric. The known shoulder-scale
        # regression is 0.6-0.9 units; reject gross unrelated misalignment.
        if delta_length <= 0.04 or delta_length > 1.5:
            deltas[target_side] = float(delta_length)
            continue

        source_local = mesh_inv @ source_world
        target_local = mesh_inv @ target_world
        forearm_local = mesh_inv @ (
            tgt.matrix_world @ tgt.data.bones[forearm_name].head_local)
        delta_local = target_local - source_local
        forearm_axis = source_local - forearm_local
        forearm_length = forearm_axis.length
        if delta_local.length < 1e-6 or forearm_length < 1e-6:
            continue
        forearm_axis.normalize()
        blend_length = max(0.9, min(1.8, forearm_length * 0.30))
        radial_inner = max(0.7, min(1.35, forearm_length * 0.24))
        radial_outer = radial_inner * 1.55

        hand_indices = set()
        for name in ('hand_rot1' + target_side, 'palm1' + target_side,
                     'palm2' + target_side, 'palm3' + target_side):
            group = mesh.vertex_groups.get(name)
            if group is not None:
                hand_indices.add(group.index)
        forearm_group = mesh.vertex_groups.get(forearm_name)
        if not hand_indices or forearm_group is None:
            continue

        side_count = 0
        for vertex in mesh.data.vertices:
            hand_weight = 0.0
            forearm_weight = 0.0
            for item in vertex.groups:
                if item.group in hand_indices:
                    hand_weight += item.weight
                elif item.group == forearm_group.index:
                    forearm_weight = item.weight
            hand_factor = max(0.0, min(1.0, hand_weight))
            transition = 0.0
            if forearm_weight > 0.05:
                offset = vertex.co - source_local
                along = offset.dot(forearm_axis)
                axial = max(0.0, min(1.0,
                                     (along + blend_length) / blend_length))
                axial = axial * axial * (3.0 - 2.0 * axial)
                radial = (offset - forearm_axis * along).length
                if radial < radial_outer:
                    radial_factor = (1.0 if radial <= radial_inner else
                                     (radial_outer - radial)
                                     / (radial_outer - radial_inner))
                    radial_factor = radial_factor * radial_factor * (
                        3.0 - 2.0 * radial_factor)
                    transition = axial * radial_factor
            factor = max(hand_factor, transition)
            if factor <= 1e-4:
                continue
            vertex.co += delta_local * factor
            moved_vertices.add(vertex.index)
            side_count += 1
        per_side[target_side] = side_count
        deltas[target_side] = [float(value) for value in delta_world]

    mesh['mowas2_wrist_geometry_version'] = version
    mesh['mowas2_wrist_geometry_count'] = len(moved_vertices)
    mesh['mowas2_wrist_geometry_delta'] = json.dumps(deltas)
    if moved_vertices:
        mesh.data.update()
        bpy.context.view_layer.update()
        print('[wrist-anchor] restored target wrist pivots:', per_side,
              '| unique verts', len(moved_vertices), '| delta', deltas)
    return len(moved_vertices)


def goh_stitch_wrist_to_handrot(mesh, tgt, force=False):
    """Migrate the invalid v1 palm1→hand_rot1 experiment back to GFA weights.

    Native long-arm GFA skins (including ``agf_nijita`` and ``agf_feitusa``)
    contain no hand_rot1-weighted vertices at all. The short-arm yelan sample has
    only 0.6%-4.4% hand_rot1 mixed with hand2, never palm1. Therefore fresh GOH
    transfers must keep the GFA Wrist→palm1 table unchanged. If a scene carries
    the v1 marker, move only palm1+hand_rot1 co-weights back to palm1; legitimate
    slot/short-arm hand2+hand_rot1 weights are left untouched.
    """
    version = 2
    previous = int(mesh.get('mowas2_wrist_stitch_version', 0))
    if not force and previous >= version:
        return 0
    if not force and not _goh_wrist_stitch():
        return 0
    if mesh.type != 'MESH' or tgt is None or tgt.type != 'ARMATURE':
        return 0

    cleaned = 0
    if previous == 1:
        for side in ('l', 'r'):
            rot_group = mesh.vertex_groups.get('hand_rot1' + side)
            palm_group = mesh.vertex_groups.get('palm1' + side)
            if rot_group is None or palm_group is None:
                continue
            restore = []
            for vertex in mesh.data.vertices:
                rot_weight = 0.0
                palm_weight = 0.0
                for item in vertex.groups:
                    if item.group == rot_group.index:
                        rot_weight = item.weight
                    elif item.group == palm_group.index:
                        palm_weight = item.weight
                if rot_weight > 1e-6 and palm_weight > 1e-6:
                    restore.append((vertex.index, palm_weight + rot_weight))
            for vertex_index, palm_weight in restore:
                palm_group.add([vertex_index], palm_weight, 'REPLACE')
                rot_group.remove([vertex_index])
            cleaned += len(restore)

    mesh['mowas2_wrist_stitch_version'] = version
    mesh['mowas2_wrist_stitch_count'] = 0
    if cleaned:
        mesh.data.update()
        bpy.context.view_layer.update()
        print('[wrist-native] removed legacy hand_rot1 transfer:', cleaned)
    return cleaned


def _protected_region_verts(me, arm=None, protect_face=True,
                            protect_tight=True):
    """E6.19: 计算"完全不允许减面"的保护顶点集 (脸细节 + 紧身衣物壳)。

    me: 当前 Mesh (每次迭代传入"当前"网格, 因为 COLLAPSE 后会重排顶点索引,
        必须按当前索引重算保护集)。
    arm: 目标骨架 (几何头部兜底用), 可为 None。
    protect_face / protect_tight: 面板开关, False 时对应类别不保护 (回退旧行为)。
    返回 (protect_vids: set, info: dict)。
    物料来源:
      a. 材质名命中 FACE_DETAIL_KW → 整块脸部/五官面保护;
      b. 材质名命中 SKIN_TIGHT_KW → 紧身衣物壳保护 (修破皮);
      c. 兜底几何: 若 a/b 都没匹配到脸, 用 head 骨中心 + 半径圈选头部
         (覆盖头/ skull/脑壳高细节), 保证"脸上看不清减面"至少对脸有效。

    另将"脸部保护区"的 1 环邻接顶点一并保护 —— 否则 COLLAPSE 会在脸部
    边界外把长细三角拉进脸周 (旧版 "脸上减面痕迹" 根因)。
    """
    import bmesh as bm_mod
    low_names = [m.name.lower() if m else '' for m in me.materials]
    face_mats = set()
    tight_mats = set()
    if protect_face:
        face_mats = {i for i, n in enumerate(low_names)
                     if any(kw in n for kw in FACE_DETAIL_KW)}
    if protect_tight:
        tight_mats = {i for i, n in enumerate(low_names)
                      if any(kw in n for kw in SKIN_TIGHT_KW)}

    face_vids = set()
    tight_vids = set()
    face_face_count = 0
    tight_face_count = 0
    for p in me.polygons:
        if p.material_index in face_mats:
            face_face_count += 1
            face_vids.update(p.vertices)
        elif p.material_index in tight_mats:
            tight_face_count += 1
            tight_vids.update(p.vertices)

    # 兜底几何: 材质没匹配到脸时, 用 head 骨圈选整个头部按顶点保护。
    skull_vids = set()
    if protect_face and not face_vids and arm is not None and arm.data is not None:
        bone = arm.data.bones.get('head')
        if bone is not None:
            center = arm.matrix_world @ bone.head_local
            # 用当前网格算头部半径: 取离 head 骨中心最近的一簇高密度顶点
            # (头部网格顶点密度远高于四肢), 取中位距离×安全系数作半径。
            ds = sorted((arm.matrix_world @ v.co - center).length
                        for v in me.vertices)
            head_count = max(1, int(len(ds) * 0.02))
            radius = (ds[head_count - 1] if head_count <= len(ds)
                      else (ds[-1] if ds else 0.0)) * 1.6 + 0.02
            for v in me.vertices:
                if (arm.matrix_world @ v.co - center).length <= radius:
                    skull_vids.add(v.index)
            print('[decimate] geometric head fallback: radius %.3f, vids %d'
                  % (radius, len(skull_vids)))

    # 脸部保护区 1 环邻接 (仅对脸, 压掉脸周细长三角; 紧身衣物壳不需要)
    collar_vids = set()
    if face_vids:
        bm = bm_mod.new()
        bm.from_mesh(me)
        for f in bm.faces:
            vs = [v.index for v in f.verts]
            if any(i in face_vids for i in vs):
                collar_vids.update(vs)
        bm.free()
        # E6.28 修复 (千咲脸毁元凶): 环必须排除脸本体!
        # 旧 bug: 脸部材质三角的 3 个顶点全在 face_vids 中 → 上面的 any() 对
        # 每个脸部面都命中 → 脸本体全部混进 collar_vids (collar_vids ⊇ face_vids)。
        # 减面降级时 protect_vids -= collar_vids 把脸的保护一起删掉, 脸在降级后
        # 被当普通区域碾碎 (实测 face 10330→323 面, 用户: "脸减成几个面片")。
        collar_vids -= face_vids

    protect = set(face_vids) | set(tight_vids) | skull_vids | collar_vids
    info = {'face': len(face_vids), 'tight': len(tight_vids),
            'skull': len(skull_vids), 'collar_ring': len(collar_vids),
            'face_mats': len(face_mats), 'tight_mats': len(tight_mats),
            'face_faces': face_face_count, 'tight_faces': tight_face_count,
            'total': len(protect),
            # E6.24: 降级保护时需要的细分集合
            'collar_vids': collar_vids, 'tight_vids': tight_vids,
            'face_vids': face_vids}
    return protect, info


def _indexed_export_vertex_count(mesh_obj, arm_obj, skip_mats=None):
    """Return the exact final-record count used by the single-PLY writer.

    This is read-only: UV, normal and weight boundaries are represented by the
    serialized record key instead of destructive ``split_edges`` operations.
    """
    return _game_export_vertex_count(
        mesh_obj, arm_obj, skip_mats=skip_mats)


def depoke_tight_clothing(mesh, margin=None):
    """E6.28/E6.29: 减面后紧身衣物破皮修复 (双向)。

    破皮有两个方向, 单向修复不够 (千咲实测残留 1367 个 B 向穿透):
      方向A: 衣物顶点陷进皮肤 → 沿皮肤法线推出 (多遍迭代, 推量上限);
      方向B: 皮肤被减成粗网格后, 尖刺顶点从较细衣物面片之间顶出 ——
        B1 被衣物覆盖的皮肤顶点做 Laplacian 平滑收掉减面尖刺
           (覆盖区皮肤本就不可见, 平滑无视觉代价);
        B2 残余穿透: 把最近衣物面的顶点沿衣物法线外推盖住 (带距离过滤 +
           邻居判别, 防胯下/指缝等窄缝被误当尖刺粘连)。
    只位移顶点, 不动 UV/权重, 推/挪量毫米级, 视觉无形变。

    margin: 默认取皮肤包围盒高度 × 0.001 (≈1.6mm/1.6m 角色), 尺度无关。
    返回调整的顶点数。
    """
    from mathutils.bvhtree import BVHTree
    me = mesh.data
    Mw = mesh.matrix_world
    Minv3 = Mw.to_3x3().inverted()
    low = [m.name.lower() if m else '' for m in me.materials]
    tight_mats = {i for i, n in enumerate(low)
                  if any(kw in n for kw in SKIN_TIGHT_KW)}
    # 皮肤 = body/skin 材质, 但 'bodytights' 含 'body' → 必须排除紧身衣类
    skin_mats = {i for i, n in enumerate(low)
                 if ('body' in n or 'skin' in n)
                 and not any(kw in n for kw in SKIN_TIGHT_KW)}
    if not tight_mats or not skin_mats:
        return 0

    # ── 收集皮肤/衣物几何 (BVHTree.FromPolygons: 扁平顶点表+索引多边形) ──
    tight_vids = set()
    skin_vids = set()
    sv_co = []          # 皮肤顶点坐标 (世界空间, 去重)
    sv_map = {}
    sp_idx = []         # 皮肤多边形 (sv_co 下标序列)
    tv_co = []
    tv_map = {}
    tp_idx = []         # 衣物多边形 (tv_co 下标序列)
    tp_orig = []        # 衣物多边形对应的原始网格顶点索引 (B2 外推用)
    skin_adj = {}       # 皮肤顶点邻接 (平滑/邻居判别用)
    for p in me.polygons:
        mi = p.material_index
        if mi in tight_mats:
            tight_vids.update(p.vertices)
            idxs = []
            for i in p.vertices:
                j = tv_map.get(i)
                if j is None:
                    j = len(tv_co)
                    tv_map[i] = j
                    tv_co.append(Mw @ me.vertices[i].co)
                idxs.append(j)
            tp_idx.append(idxs)
            tp_orig.append(tuple(p.vertices))
        elif mi in skin_mats:
            skin_vids.update(p.vertices)
            idxs = []
            vs = list(p.vertices)
            for a in range(len(vs)):
                skin_adj.setdefault(vs[a], set()).add(vs[(a + 1) % len(vs)])
                skin_adj.setdefault(vs[(a + 1) % len(vs)], set()).add(vs[a])
            for i in p.vertices:
                j = sv_map.get(i)
                if j is None:
                    j = len(sv_co)
                    sv_map[i] = j
                    sv_co.append(Mw @ me.vertices[i].co)
                idxs.append(j)
            sp_idx.append(idxs)
    if not tight_vids or not sp_idx:
        return 0
    if margin is None:
        zs = [c.z for c in sv_co]
        h = (max(zs) - min(zs)) if zs else 1.0
        margin = max(h * 0.001, 1e-4)
    max_push = margin * 3.0   # 单次推/拉量上限: 防深陷/误配最近点把顶点拉飞

    def refresh_co(mp):
        inv = sorted(mp.items(), key=lambda kv: kv[1])
        return [Mw @ me.vertices[i].co for i, _j in inv]

    moved = 0
    tight_bvh = BVHTree.FromPolygons(tv_co, tp_idx) if tp_idx else None

    # ── 方向B1: 覆盖区皮肤 Laplacian 平滑 (收减面尖刺, 3 遍×0.5) ──
    if tight_bvh is not None:
        covered = set()
        for vi in skin_vids:
            wco = Mw @ me.vertices[vi].co
            loc, _n, _i, d = tight_bvh.find_nearest(wco)
            if loc is not None and d <= margin * 6:
                covered.add(vi)
        for _it in range(3):
            delta = {}
            for vi in covered:
                nb = skin_adj.get(vi)
                if not nb:
                    continue
                avg = Vector((0.0, 0.0, 0.0))
                for nj in nb:
                    avg += me.vertices[nj].co
                avg /= len(nb)
                delta[vi] = (avg - me.vertices[vi].co) * 0.5
            for vi, dco in delta.items():
                me.vertices[vi].co += dco
        if covered:
            me.update()
            moved += len(covered)

    # ── 方向B3: 残余真实穿透的皮肤顶点 → 沿衣物法线直接拉回衣物内侧 ──
    # 凹陷区(胯下等)不能用 Laplacian (局部质心在表面外侧, 越平滑越外鼓,
    # 实测 A/B 违规反而变多); 改为把"穿透衣物外侧 >0.5*margin"的皮肤顶点
    # 沿最近衣物点法线拉回到衣物内侧 0.25*margin —— 回拉方向远离缝隙,
    # 绝不粘连; 衣边(袜口/袖口)附近皮肤 signed<0 天然不会被误标。
    if tight_bvh is not None:
        n_b3 = 0
        for vi in skin_vids:
            wco = Mw @ me.vertices[vi].co
            loc, norm, _i, d = tight_bvh.find_nearest(wco)
            if loc is None or norm is None or d > margin * 6:
                continue
            signed = (wco - loc).dot(norm)
            if signed > margin * 0.5:
                step = min(signed + margin * 0.25, max_push)
                me.vertices[vi].co -= Minv3 @ (norm * step)
                n_b3 += 1
        if n_b3:
            me.update()
            moved += n_b3

    # ── 方向A: 衣物顶点陷进皮肤 → 沿皮肤法线推出 (多遍, 推量上限) ──
    skin_bvh = BVHTree.FromPolygons(refresh_co(sv_map), sp_idx)
    tv_sorted = sorted(tight_vids)
    for _pass in range(4):
        n_pass = 0
        for vi in tv_sorted:
            v = me.vertices[vi]
            wco = Mw @ v.co
            loc, norm, _idx, _dist = skin_bvh.find_nearest(wco)
            if loc is None or norm is None:
                continue
            signed = (wco - loc).dot(norm)
            if signed < margin:
                step = min(margin - signed, max_push)
                v.co += Minv3 @ (norm * step)
                n_pass += 1
        moved += n_pass
        if n_pass == 0:
            break

    # ── 方向B2: 残余皮肤顶点穿透衣物 → 外推最近衣物面盖住 ──
    if tp_idx:
        max_push_b = margin * 3.0
        push_budget = {}      # 衣物顶点累计推量上限 (防反复外推鼓包)
        sv_sorted = sorted(skin_vids)
        for _pass in range(3):
            tbvh = BVHTree.FromPolygons(refresh_co(tv_map), tp_idx)
            n_b = 0
            for vi in sv_sorted:
                wco = Mw @ me.vertices[vi].co
                loc, norm, pidx, dist = tbvh.find_nearest(wco)
                if loc is None or norm is None or dist > margin * 3:
                    continue
                signed = (wco - loc).dot(norm)
                need = signed + margin * 0.5   # 目标: 衣物盖住皮肤外 0.5*margin
                if need <= 0:
                    continue
                if dist > margin * 1.2:
                    # 远距离候选: 若多数皮肤邻居也在衣物外侧 → 是整个皮肤
                    # 面片朝向窄缝 (胯下/指缝), 不是尖刺 → 跳过防粘连
                    nb = skin_adj.get(vi)
                    if nb:
                        out_nb = 0
                        for nj in nb:
                            wnj = Mw @ me.vertices[nj].co
                            l2, n2, _i2, d2 = tbvh.find_nearest(wnj)
                            if (l2 is not None and d2 <= margin * 6
                                    and (wnj - l2).dot(n2) > -margin * 0.5):
                                out_nb += 1
                        if out_nb > len(nb) * 0.6:
                            continue
                step = min(need, max_push_b)
                dloc = Minv3 @ (norm * step)
                for ovi in tp_orig[pidx]:
                    used = push_budget.get(ovi, 0.0)
                    room = margin * 4.0 - used
                    if room <= 0:
                        continue
                    scale = min(1.0, room / step)
                    me.vertices[ovi].co += dloc * scale
                    push_budget[ovi] = used + step * scale
                    n_b += 1
            moved += n_b
            if n_b == 0:
                break
    if moved:
        me.update()
    return moved


def decimate_global(mesh, target_faces=None, arm=None,
                    protect_face=True, protect_tight=True, skip_mats=None):
    """E6.18+: 全局 COLLAPSE 减面 (skill v2 原方案) —— 替代分离-减面-合并。

    分离方案 (decimate_to_target, E6.11) 在 KK 类模型 (非流形边多/重复顶点多)
    上失效: 分离错乱 + COLLAPSE 卡在 ~3 万面 + 合并吸附拉飞顶点 → 网格破碎
    (千咲实测 24822 非流形边, 分离后模型碎片化)。

    本方案:
    1. 多用户 data 复制 (KK 幽灵共享引用) → 单用户, 否则 apply modifier 失败;
    2. remove_doubles(1e-6) 预清理 —— KK 有大量近距重复顶点 (千咲 93930→78843
       面), 重复顶点是 COLLAPSE 卡死的根因;
    3. 保护顶点 VG (E6.19: 脸细节 + 紧身衣物壳 + 几何头兜底 + 脸 1 环)
       —— COLLAPSE 不折叠/不位移这些顶点, 因此:
          * 脸/五官完全不变 (不再出现脸上减面三角);
          * 紧身衣物壳零位移 → 不再因减面位移戳进皮肤 (修破皮);
       arm 传入目标骨架用于几何头部兜底 (材质没匹配脸时)。
    4. 全局 COLLAPSE 迭代，直到面数目标和最终唯一属性记录 ≤65535 同时满足。

    返回最终面数。适用于所有模型 (老 MMD 模型同样有效)。
    """
    import bmesh as bm_mod
    # 1. 多用户 data → 单用户 (幽灵共享引用, users 计数不可靠)
    mesh.data = mesh.data.copy()
    me = mesh.data
    if target_faces is None:
        target_faces = len(me.polygons)
    target_faces = max(1, int(target_faces))

    # 2. 预清理: 重复顶点 (KK 模型大量 1e-6 近距对) + 删孤立/退化几何
    bm = bm_mod.new()
    bm.from_mesh(me)
    bm_mod.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    bm.to_mesh(me)
    bm.free()
    me.update()
    me.validate(verbose=False)  # 删孤立顶点/退化几何 (KK 有大量悬浮碎片)
    n_clean = len(me.polygons)
    print('[decimate] after remove_doubles+validate:', n_clean, 'faces')

    vg_name = '_mow_deci_protect'

    def rebuild_vg(me_cur, use_collar=True, use_tight=True):
        """按"当前"网格重算保护顶点集并强制重建保护 VG。

        COLLAPSE 每次 apply 会重排顶点索引 → 必须每次迭代用当前 me 重算,
        否则保护集索引漂移去保护错误的顶点。
        use_collar=False: 放开脸周 1 环 (脸本体仍全保护);
        use_tight=False: 紧身衣物壳参与减面 (减面后自动做破皮修复)。
        """
        try:
            old = mesh.vertex_groups.get(vg_name)
            if old is not None:
                mesh.vertex_groups.remove(old)
        except Exception:
            pass
        protect_vids, pinfo = _protected_region_verts(me_cur, arm,
                                                      protect_face,
                                                      protect_tight)
        if not protect_vids:
            return None
        if not use_collar:
            protect_vids -= pinfo.get('collar_vids', set())
        if not use_tight:
            protect_vids -= pinfo.get('tight_vids', set())
        if not protect_vids:
            return None
        vg = mesh.vertex_groups.new(name=vg_name)
        vids = sorted(vi for vi in protect_vids
                      if 0 <= vi < len(me_cur.vertices))
        for i in range(0, len(vids), 2000):
            vg.add(vids[i:i + 2000], 1.0, 'REPLACE')
        return vg

    # 3. 全局 COLLAPSE 迭代 (data 已单用户, 不再复制)
    #    硬约束 = 面数 ≤ target 且最终 40-byte 属性记录 ≤ 65535
    #    (u16 索引上限)。收敛检测: 连续 2 轮面数不再下降 → 保护集逐级降级
    #    (脸周 1 环 → 紧身衣物壳), 尽量达成目标。
    UV_LIMIT = 65535
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    mesh.select_set(True)
    bpy.context.view_layer.objects.active = mesh
    f = len(me.polygons)
    _p0, _pin0 = _protected_region_verts(me, arm, protect_face, protect_tight)
    # 打印时剥离顶点集合字段 (避免把几万个顶点索引刷进控制台)
    _pin0_print = {k: v for k, v in _pin0.items()
                   if k not in ('collar_vids', 'tight_vids', 'face_vids')}
    print('[decimate] protected region:', _pin0_print)
    # E6.28 预算预检: 保护面数 ≥ 目标时"全保护"永远无法收敛 (保护地板 > 目标),
    # 旧逻辑白跑多轮再逐级降级, 且紧身衣 (千咲 29639 面) 独占预算把脸/头发/
    # 身体全挤没 —— 直接按预算选起始保护组合:
    #   脸+紧身 ≤ 目标 90% → 全保护 (小模型: 紧身衣零位移, 绝不破皮);
    #   超出               → 紧身衣参与减面 (减面后 depoke 修破皮), 脸始终全保护。
    face_f = _pin0.get('face_faces', 0)
    tight_f = _pin0.get('tight_faces', 0)
    use_collar = True
    use_tight = bool(protect_tight and tight_f > 0)
    if use_tight and face_f + tight_f > target_faces * 0.9:
        use_tight = False
        print('[decimate] 预算预检: 脸 %d + 紧身衣 %d 面 > 目标 %d 的 90%%, '
              '紧身衣壳改为参与减面 (减面后自动破皮修复)'
              % (face_f, tight_f, target_faces))
    if face_f > target_faces * 0.9:
        use_collar = False
        print('[decimate] 警告: 脸部 %d 面已达目标 %d 的 90%%, 放开脸周 1 环 '
              '(脸本体仍全保护)' % (face_f, target_faces))
    tight_was_decimated = (not use_tight) and tight_f > 0
    guard = 0
    stall = 0
    while guard < 20:
        if f <= target_faces:
            est = _indexed_export_vertex_count(mesh, arm, skip_mats=skip_mats)
            print('[decimate] faces %d <= target %d | indexed records %d (limit %d)'
                  % (f, target_faces, est, UV_LIMIT))
            if est <= UV_LIMIT:
                break
            # 面数达标但最终属性记录仍超限：按实际超限比例做最小减面，
            # 额外留 0.5% 缓冲，避免固定 5% 对临界模型过度处理。
            ratio = max(0.5, min(0.995, (UV_LIMIT / est) * 0.995))
        else:
            ratio = max(0.3, (target_faces / max(1, f)) ** 0.5)
        bpy.ops.object.modifier_add(type='DECIMATE')
        mod = mesh.modifiers[-1]
        mod.decimate_type = 'COLLAPSE'
        mod.ratio = ratio
        mod.use_collapse_triangulate = True
        vg = rebuild_vg(me, use_collar, use_tight)
        if vg:
            mod.vertex_group = vg.name
            mod.invert_vertex_group = True
        bpy.ops.object.modifier_apply(modifier=mod.name)
        me = mesh.data  # 防御: apply 后 data 可能已换新数据块
        new_f = len(me.polygons)
        print('[decimate] global ratio %.3f -> %d faces (target %d)'
              % (ratio, new_f, target_faces))
        if new_f >= f * 0.99:
            # 收敛: 减不动了
            stall += 1
            if stall >= 2:
                if use_collar:
                    use_collar = False
                    stall = 0
                    print('[decimate] 减面收敛于 %d 面 → 降级保护: 放开脸部 1 环邻接 '
                          '(脸本体仍全保护)' % new_f)
                elif use_tight:
                    use_tight = False
                    tight_was_decimated = True
                    stall = 0
                    print('[decimate] 仍收敛于 %d 面 → 降级保护: 紧身衣物壳参与减面, '
                          '减面后自动破皮修复 (脸部保护不动)' % new_f)
                else:
                    print('[decimate] 保护降级后仍无法继续减面, 停止于 %d 面' % new_f)
                    break
        else:
            stall = 0
        f = new_f
        guard += 1
    # 清理保护 VG (用名字索引删除, 避免引用损坏)
    try:
        if vg_name in mesh.vertex_groups:
            mesh.vertex_groups.remove(mesh.vertex_groups[vg_name])
    except Exception:
        pass
    # E6.28/29: 破皮修复 —— 紧身衣物被减面(衣物陷进皮肤)或皮肤被减面(尖刺
    # 顶出衣物)都会破皮, 两个方向都在 depoke 内处理; 即使紧身衣全程受保护,
    # 皮肤减面尖刺仍会顶穿衣物 → 无条件执行。
    try:
        n_poke = depoke_tight_clothing(mesh)
        print('[decimate] 破皮修复: 调整 %d 个顶点 (皮肤尖刺平滑+衣物推出)'
              % n_poke)
    except Exception as _e:
        print('[decimate] 破皮修复跳过 (%r)' % _e)
    est_final = _indexed_export_vertex_count(mesh, arm, skip_mats=skip_mats)
    print('[decimate] final: %d faces, indexed records %d (limit %d)'
          % (f, est_final, UV_LIMIT))
    return f


def decimate_to_target(mesh, target_faces=21845, hand_ratio=0.7,
                       collar_ratio=0.5, snap_dist=0.8):
    """E6.11 减面 v3 (脸部 + 领圈 + 手部 + 手铐保护):

    - 脸部材质面: 完全排除减面 (隔离不减) —— 保持 8516 面完整;
    - 脸部领圈 (collar): 与脸部边界共享顶点的拓扑 1 环 —— 单独减面到
      collar_ratio (保结构, 消除脸部周围撕裂细长三角; 实测旧版 0.35 邻域
      内 114 个长宽比>8 的三角 = 用户看到的"脸上减面痕迹";
      距离选法面数爆炸 (0.15=7259 面), 1 环精确且小);
    - 手部 (palm*/hand_rot1* 顶点组的面, 含手指): 单独减面到 hand_ratio
      —— 实测旧版手部 3920→943 面 (76% 被削) = 手指被削/手掌塌陷根因;
    - 手铐 (acs_m_hand_cuffs): 小配件, 完全保护;
    - 主体: COLLAPSE 减面到 target 余量；单块 VERT/INDX 仍按最终唯一
      位置/权重/法线/UV 记录检查 u16 上限 65535；
    - 合并 + 边界吸附 (snap_dist) + remove_doubles(1e-7)。

    分离顺序 (关键): separate(type='SELECTED') 会把全部选中面放进同一个
    新对象 (不按岛拆分!) —— 必须 脸部/手铐/手部/领圈 分 4 次 separate,
    否则误分类 (v2 实测: 脸部被当手部以 0.7 减面 → 脸损)。
    """
    FACE_KW = ('face', 'mayuge', 'tooth', 'eyeline', 'sirome', 'hitomi', 'tang')
    PROTECT_KW = FACE_KW + ('hand_cuffs',)
    HAND_SINGLE = {'palm1l', 'palm1r', 'palm2l', 'palm2r', 'palm3l', 'palm3r',
                   'hand_rot1l', 'hand_rot1r'}
    me = mesh.data
    # 主流程必须先由 freeze_mesh 烘焙 evaluated 形态；禁止在此静默丢弃
    # Shape Key。这样旧脚本误把未冻结网格直接送来时会明确失败。
    if me.shape_keys:
        if not mesh.get('mowas2_frozen'):
            raise RuntimeError(
                'decimate_to_target requires freeze_mesh before Shape Keys')
        mesh.shape_key_clear()
        print('[decimate] stale shape keys cleared after evaluated freeze')
    face_mats = {i for i, m in enumerate(me.materials)
                 if m and any(kw in m.name.lower() for kw in FACE_KW)}
    cuff_mats = {i for i, m in enumerate(me.materials)
                 if m and 'hand_cuffs' in m.name.lower()}
    hand_vg_idx = {vg.index for vg in mesh.vertex_groups if vg.name in HAND_SINGLE}
    import bmesh as bm_mod
    import mathutils

    def vtop(vi):
        best, bw = None, 0.0
        for g in me.vertices[vi].groups:
            if g.weight > bw:
                best, bw = g.group, g.weight
        return best

    # 保护面统计
    n_face = n_cuff = n_hand = 0
    for f in me.polygons:
        if f.material_index in face_mats:
            n_face += 1
        elif f.material_index in cuff_mats:
            n_cuff += 1
        else:
            tops = {vtop(i) for i in f.vertices}
            if tops and all(t is None or t in hand_vg_idx for t in tops):
                n_hand += 1

    known = {mesh.name}
    isolated = []  # (label, [objects])

    def _separate(pred, label):
        """把满足 pred(bm_face) 的面分离成新对象 (不按岛, 全部进一个新对象)。
        若没有面满足 (如新模型无 cuff 材质) → 跳过分离, 避免 separate 空选择崩溃。"""
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        mesh.select_set(True)
        bpy.context.view_layer.objects.active = mesh
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='DESELECT')
        bm = bm_mod.from_edit_mesh(me)
        nsel = 0
        for f in bm.faces:
            f.select = pred(f, bm)
            if f.select:
                nsel += 1
        bm_mod.update_edit_mesh(me)
        if nsel == 0:
            bpy.ops.object.mode_set(mode='OBJECT')
            isolated.append((label, []))
            print('[decimate] separated %s: 0 faces (skip, no such material)' % label)
            return []
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.mode_set(mode='OBJECT')
        new = [o for o in bpy.context.selected_objects
               if o.name not in known and o.type == 'MESH']
        known.update(o.name for o in new)
        isolated.append((label, new))
        n = sum(len(o.data.polygons) for o in new)
        print('[decimate] separated %s: %d objects, %d faces' % (label, len(new), n))
        return new

    # 1) 脸部材质
    def _p_face(f, bm):
        return f.material_index in face_mats
    _separate(_p_face, 'face')

    # 2) 领圈 (拓扑 1 环): face 分离后主体洞边界顶点的邻接面。
    #    不能用距离法: freeze(蒙皮变形) 后头部皮肤相对颈部有 ~0.5 结构性间隙
    #    (head 骨与躯干骨 pose 相对关系 vs 源 rest 不同) → 0.05 距离判 96 面
    #    (实际 1 环 600+), 0.5 距离又太贵 (全分辨率 12611 面)。拓扑 1 环
    #    精确覆盖"会被 snap 拉拽产生细长三角"的区域 (细长三角 = 用户看到的
    #    "脸上减面痕迹"根源)。
    face_objs = [o for _, objs in isolated for o in objs if _ == 'face']
    bnd = set()
    _bm = bm_mod.new()
    _bm.from_mesh(me)
    for _e in _bm.edges:
        if len(_e.link_faces) == 1:
            bnd.update(_v.index for _v in _e.verts)
    _bm.free()
    collar_set = {fi for fi, f in enumerate(me.polygons)
                  if any(i in bnd for i in f.vertices)}
    print('[decimate] collar_set=%d (bnd_verts=%d)' % (len(collar_set), len(bnd)))

    def _p_collar(f, bm):
        return f.index in collar_set
    _separate(_p_collar, 'collar')

    # 3) 手铐 (材质)
    def _p_cuff(f, bm):
        return f.material_index in cuff_mats
    _separate(_p_cuff, 'cuff')

    # 4) 手部 (全顶点 top 在手骨集) — 顶点索引经 v.index 对照当前 me
    def _p_hand(f, bm):
        tops = {vtop(v.index) for v in f.verts}
        return bool(tops) and all(t is None or t in hand_vg_idx for t in tops)
    _separate(_p_hand, 'hand')

    # 分类汇总
    face_objs = [o for _, objs in isolated for o in objs if _ == 'face']
    cuff_objs = [o for _, objs in isolated for o in objs if _ == 'cuff']
    hand_objs = [o for _, objs in isolated for o in objs if _ == 'hand']
    collar_objs = [o for _, objs in isolated for o in objs if _ == 'collar']

    # 2) 对象级 COLLAPSE 减面
    def _apply(obj, ratio, delim=True):
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        # 多用户 data 无法 apply modifier (KK 模型有 100+ 幽灵共享引用,
        # users 计数不可靠) → 无条件复制为单用户 data, 否则 DECIMATE 异常
        obj.data = obj.data.copy()
        bpy.ops.object.modifier_add(type='DECIMATE')
        mod = obj.modifiers[-1]
        mod.decimate_type = 'COLLAPSE'
        mod.ratio = ratio
        mod.use_collapse_triangulate = True
        if delim:
            try:
                mod.delimit = {'NORMAL'}
            except TypeError:
                pass
        bpy.ops.object.modifier_apply(modifier=mod.name)
        return len(obj.data.polygons)

    n_hand_kept = 0
    for o in hand_objs:
        n_hand_kept += _apply(o, hand_ratio)
    n_collar_kept = 0
    for o in collar_objs:
        n_collar_kept += _apply(o, collar_ratio)
    n_face_kept = sum(len(o.data.polygons) for o in face_objs)
    n_cuff_kept = sum(len(o.data.polygons) for o in cuff_objs)

    body_target = max(2000, target_faces - n_face_kept - n_cuff_kept
                      - n_hand_kept - n_collar_kept)
    print('[decimate] body_target:', body_target, '| body pre:', len(me.polygons))
    f = _apply(mesh, 0.35)
    print('[decimate] body after 0.35:', f)
    guard = 0
    while f > body_target and guard < 4:
        r = max(0.4, (body_target / max(1, f)) ** 0.5)
        prev = f
        f = _apply(mesh, r)
        # delimit NORMAL 在硬边密集模型 (KK 类) 上会卡死 (每次只减几面)
        # → 卡住时 fallback 无 delimit 强制减 (E6.18: 千咲 body 30635 卡住)
        if f >= prev * 0.98:
            f = _apply(mesh, r, delim=False)
            print('[decimate] body fallback (no delimit) ratio %.3f: %d' % (r, f))
        else:
            print('[decimate] body after ratio %.3f: %d (target %d)' % (r, f, body_target))
        guard += 1
    # E6.18: KK 类模型非流形边多 (千咲 24822 条) → COLLAPSE 有减面极限 (~3万面),
    #     即使达不到 target_faces 也接受 (最终属性记录的 u16 硬限随后检查)。
    print('[decimate] body final:', f, '(target', body_target, ', KK limit ~30k)')

    # 3) 合并 + 边界吸附去重
    all_others = [o for _, objs in isolated for o in objs]
    if all_others:
        def boundary_coords(obj):
            bm = bm_mod.new()
            bm.from_mesh(obj.data)
            out = {}
            for e in bm.edges:
                if len(e.link_faces) == 1:
                    for v in e.verts:
                        out[v.index] = v.co.copy()
            bm.free()
            return out
        bv = {}
        for o in all_others:
            bv.update(boundary_coords(o))
        if bv:
            kd = mathutils.kdtree.KDTree(len(bv))
            for vi, co in bv.items():
                kd.insert(co, vi)
            kd.balance()
            bm = bm_mod.new()
            bm.from_mesh(me)
            moved = 0
            for e in bm.edges:
                if len(e.link_faces) == 1:
                    for v in e.verts:
                        co, idx, dist = kd.find(v.co)
                        if co and dist < snap_dist:
                            v.co = co
                            moved += 1
            bm.to_mesh(me)
            bm.free()
            me.update()
        for o in all_others:
            o.select_set(True)
        bpy.context.view_layer.objects.active = mesh
        bpy.ops.object.join()
        bm = bm_mod.new()
        bm.from_mesh(me)
        # dist 必须极小: 网格内有大量 1e-6~0.001 的近距顶点对 (睫毛薄条等),
        # 0.001 会把它们合并成退化面删掉 (实测 eyeline_down 572→197) → 只用 1e-7
        bm_mod.ops.remove_doubles(bm, verts=bm.verts, dist=1e-7)
        bm.to_mesh(me)
        bm.free()
        me.update()
    stats = {'face': n_face, 'cuff': n_cuff, 'hand': n_hand,
             'hand_kept': n_hand_kept, 'collar_kept': n_collar_kept,
             'face_kept': n_face_kept, 'body_final': f, 'total': len(me.polygons)}
    try:
        mesh['decimate_stats'] = stats
    except Exception:
        pass
    print('[decimate] stats:', stats)
    return len(me.polygons)


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════
def _align_options_changed(mesh, src):
    """检测冻结几何选项是否变化；变化时必须从 PMX 干净重导。"""
    try:
        stored = mesh.get('mowas2_foot_scale')
        if stored is not None and abs(float(stored) - _goh_foot_scale()) > 1e-4:
            return True
    except Exception:
        pass

    try:
        body_version = int(mesh.get('mowas2_shoulder_geometry_version', 0))
        # v1/v2 moved shoulder geometry and v3 widened only the central torso.
        # Rebuild once from PMX so v4 starts from the untouched body silhouette.
        if body_version < 4:
            return True
        if abs(float(mesh.get('mowas2_foot1_spacing', 1.0))
               - _goh_foot1_spacing()) > 1e-4:
            return True
        if bool(mesh.get('mowas2_ik_updown_enabled', False)) != _goh_ik_updown_enabled():
            return True
        if abs(float(mesh.get('mowas2_ik_updown_multiplier', 1.0))
               - _goh_ik_updown_multiplier()) > 1e-4:
            return True
    except Exception:
        pass

    enabled = _goh_enlarge_head()
    factor = _goh_head_scale()
    follow = _goh_head_neck_follow()
    try:
        stored_enabled = mesh.get('mowas2_head_enlarge_enabled')
        if stored_enabled is None:
            # 旧快照没有可靠的 Head pose 烘焙记录。当前开启时重导；关闭
            # 时保持兼容，不无故重跑已经验证的旧导出。
            if enabled:
                return True
        else:
            if bool(stored_enabled) != enabled:
                return True
            if enabled and abs(float(mesh.get('mowas2_head_scale', factor))
                               - factor) > 1e-4:
                return True
            if enabled and abs(float(mesh.get('mowas2_head_neck_follow', follow))
                               - follow) > 1e-4:
                return True
            if (enabled and follow > 1e-4
                    and int(mesh.get('mowas2_neck_follow_version', 0)) < 2):
                return True
    except Exception:
        pass

    # v2 在网格属性中保存对齐基准，可在视口里反复增减间距。v1 没有
    # 基准，必须重导；若预览回调未能在当前场景运行，设置差异也会重导。
    try:
        pupil_version = int(mesh.get('mowas2_pupil_depth_version', 0))
        if pupil_version in (1, 2):
            return True
        if pupil_version > 0:
            if bool(mesh.get('mowas2_pupil_depth_enabled', True)) != _goh_fix_pupil_depth():
                return True
            if abs(float(mesh.get('mowas2_pupil_clearance',
                                  _goh_pupil_clearance()))
                   - _goh_pupil_clearance()) > 1e-5:
                return True
    except Exception:
        pass
    return False


def _reimport_source(pmx_path, keep_tgt):
    """删除旧源骨架/网格，从 pmx_path 重新导入，返回 (src, mesh, root)。
    用于对齐期选项修改后的自动重对齐。"""
    for o in list(bpy.data.objects):
        if o.type == 'ARMATURE' and o is not keep_tgt:
            bpy.data.objects.remove(o, do_unlink=True)
    for o in list(bpy.data.objects):
        if o.type == 'MESH':
            bpy.data.objects.remove(o, do_unlink=True)
    snapshot = _effective_mmd_import_snapshot(bpy.context.scene)
    src, mesh = import_pmx(
        pmx_path,
        preset_name=snapshot['preset'],
        preset_settings=snapshot['settings'],
        preset_path=snapshot['preset_path'],
        preset_sha256=snapshot['preset_sha256'])
    root = src.parent if src.parent else src
    return src, mesh, root


def resolve_scene():
    """Resolve source, destination and mesh strictly within the active scene."""
    scene = bpy.context.scene
    scene_objects = list(scene.objects)
    arms = sorted([obj for obj in scene_objects if obj.type == 'ARMATURE'],
                  key=lambda arm: len(arm.data.bones))
    if not arms:
        raise RuntimeError(_("mowas2.err.scene_no_armature"))
    tagged_targets = [arm for arm in arms if arm.get('gem2_world_mats')]
    props = getattr(scene, 'mowas2_props', None)
    requested_path = _human_rest_path(getattr(props, 'mdl_path', ''))
    path_targets = [arm for arm in tagged_targets
                    if requested_path and _human_rest_armature_path(arm) == requested_path]
    named_target = next((obj for obj in scene_objects
                         if obj.name == 'skin_Armature'), None)
    tgt = (path_targets[0] if path_targets else
           (named_target if named_target in tagged_targets else
            (tagged_targets[0] if tagged_targets else None)))

    converted = [obj for obj in scene_objects
                 if obj.type == 'MESH'
                 and _human_rest_conversion_marked(obj)]
    active = bpy.context.view_layer.objects.active
    mesh = active if active in converted else None
    if mesh is None and converted:
        selected = [obj for obj in converted if obj.select_get()]
        if len(selected) == 1:
            mesh = selected[0]
        elif len(converted) == 1:
            mesh = converted[0]
    src = None
    if mesh is not None:
        metadata = _human_rest_conversion_metadata(mesh)
        target_name = metadata.get('destination_armature', '')
        source_name = metadata.get('source_armature', '')
        target_by_meta = next((obj for obj in scene_objects
                               if obj.name == target_name
                               and obj.type == 'ARMATURE'), None)
        source_by_meta = next((obj for obj in scene_objects
                               if obj.name == source_name
                               and obj.type == 'ARMATURE'), None)
        if target_by_meta is not None:
            tgt = target_by_meta
        if source_by_meta is not None:
            src = source_by_meta

    # Human conversion keeps a converted duplicate in the same scene for
    # comparison. PMX commands should prefer the untouched source mesh when it
    # is available; otherwise the explicit guard below rejects the converted
    # result instead of mutating its normalized destination rest.
    if mesh is not None and mesh in converted:
        untouched = [obj for obj in scene_objects
                     if obj.type == 'MESH'
                     and not _human_rest_conversion_marked(obj)]
        bound_untouched = [obj for obj in untouched
                           if src is not None and any(
                               mod.type == 'ARMATURE' and mod.object is src
                               for mod in obj.modifiers)]
        selected_untouched = [obj for obj in untouched if obj.select_get()]
        mesh = (active if active in untouched else
                (max(bound_untouched, key=lambda obj: len(obj.data.vertices))
                 if bound_untouched else
                 (max(selected_untouched, key=lambda obj: len(obj.data.vertices))
                  if selected_untouched else None)))

    if src is None and len(arms) >= 2:
        tgt = tgt if tgt else arms[0]
        candidates = [arm for arm in arms if arm is not tgt]
        src = max(candidates, key=lambda arm: len(arm.data.bones))
    elif src is None and len(arms) == 1:
        if tgt is None:
            raise RuntimeError(_("mowas2.err.scene_no_target"))
        src = arms[0]

    if mesh is None and src is not None:
        bound = [obj for obj in scene_objects if obj.type == 'MESH'
                 and not _human_rest_conversion_marked(obj)
                 and any(mod.type == 'ARMATURE' and mod.object is src
                         for mod in obj.modifiers)]
        if bound:
            mesh = max(bound, key=lambda obj: len(obj.data.vertices))
    if mesh is None:
        # 已冻结/已绑定到目标的网格：找当前场景中最大的未转换 MESH。
        big = [obj for obj in scene_objects if obj.type == 'MESH'
               and not _human_rest_conversion_marked(obj)
               and len(obj.data.vertices) > 500]
        if big:
            mesh = max(big, key=lambda obj: len(obj.data.vertices))
    if mesh is None and converted:
        # Leave the converted result visible to the explicit guard in the PMX
        # entry points when no untouched comparison mesh exists.
        mesh = max(converted, key=lambda obj: len(obj.data.vertices))
    if mesh is None:
        raise RuntimeError(_("mowas2.err.scene_no_mesh"))
    root = src.parent if src and src.parent else src
    return src, tgt, mesh, root


_MMD_PIPELINE_PRESET = '__PIPELINE__'
_MMD_RECOMMENDED_PRESET = 'gem2_goh_mowas2_lossless'
_MMD_SCENE_PRESET_KEY = 'gem2_mmd_import_preset'
_MMD_SCENE_SETTINGS_KEY = 'gem2_mmd_import_settings_json'
_MMD_SCENE_PATH_KEY = 'gem2_mmd_import_preset_path'
_MMD_SCENE_SHA256_KEY = 'gem2_mmd_import_preset_sha256'
_MMD_IMPORT_FIELDS = {
    'types', 'scale', 'clean_model', 'remove_doubles',
    'import_adduv2_as_vertex_colors', 'fix_bone_order', 'fix_ik_links',
    'ik_loop_factor', 'apply_bone_fixed_axis', 'rename_bones',
    'use_underscore', 'dictionary', 'bone_disp_mode', 'use_mipmap',
    'sph_blend_factor', 'spa_blend_factor', 'log_level', 'save_log',
}
_MMD_IMPORT_DEFAULTS = {
    # Match the shared operator preset. Source seam vertices and custom normals
    # stay intact; the GEM2 exporter performs exact final-record deduplication.
    'types': ['ARMATURE', 'MESH'],
    'scale': 1.0,
    'clean_model': True,
    'remove_doubles': False,
    'import_adduv2_as_vertex_colors': False,
    'fix_bone_order': True,
    'fix_ik_links': True,
    'ik_loop_factor': 5,
    'apply_bone_fixed_axis': False,
    'rename_bones': True,
    'use_underscore': True,
    'dictionary': 'INTERNAL',
    'bone_disp_mode': 'OCTAHEDRAL',
    'use_mipmap': True,
    'sph_blend_factor': 1.0,
    'spa_blend_factor': 1.0,
    'log_level': 'INFO',
    'save_log': False,
}
_mmd_preset_item_cache = []


def _mmd_preset_directories():
    directories = []
    try:
        directories.extend(bpy.utils.preset_paths(
            os.path.join('operator', 'mmd_tools.import_model')))
    except Exception:
        pass
    scripts_dir = bpy.utils.user_resource('SCRIPTS')
    config_dir = bpy.utils.user_resource('CONFIG')
    for root in (scripts_dir, config_dir):
        if root:
            directories.append(os.path.join(
                root, 'presets', 'operator', 'mmd_tools.import_model'))
    output = []
    for value in directories:
        path = os.path.abspath(value)
        if path not in output and os.path.isdir(path):
            output.append(path)
    return output


def list_mmd_import_presets():
    output = {}
    for directory in _mmd_preset_directories():
        try:
            names = sorted(os.listdir(directory), key=str.casefold)
        except OSError:
            continue
        for filename in names:
            if filename.endswith('.py'):
                output.setdefault(filename[:-3], os.path.join(directory, filename))
    return output


def _mmd_import_preset_items(_self, context):
    global _mmd_preset_item_cache
    paths = list_mmd_import_presets()
    ordered = []
    recommended_path = paths.get(_MMD_RECOMMENDED_PRESET)
    if recommended_path:
        ordered.append((_MMD_RECOMMENDED_PRESET,
                        'GEM2 GOH + MOWAS2 Lossless', recommended_path))
    ordered.append((_MMD_PIPELINE_PRESET, 'Pipeline defaults',
                    'Built-in lossless PMX pipeline settings'))
    for name, path in sorted(paths.items(), key=lambda item: item[0].casefold()):
        if name != _MMD_RECOMMENDED_PRESET:
            ordered.append((name, name, path))
    scene = getattr(context, 'scene', None)
    captured_name = str(scene.get(_MMD_SCENE_PRESET_KEY, '') if scene else '')
    known = {item[0] for item in ordered}
    if captured_name == 'Pipeline defaults':
        captured_name = _MMD_PIPELINE_PRESET
    if captured_name and captured_name not in known:
        ordered.append((captured_name, captured_name,
                        'Captured immutable preset snapshot'))
    _mmd_preset_item_cache = ordered
    return _mmd_preset_item_cache


def _validate_mmd_import_settings(values):
    if not isinstance(values, dict):
        raise ValueError('MMD import settings must be a mapping')
    unknown = sorted(set(values) - _MMD_IMPORT_FIELDS)
    if unknown:
        raise ValueError('Unsupported MMD import settings: ' + ', '.join(unknown))
    settings = dict(_MMD_IMPORT_DEFAULTS)
    settings.update(values)
    types = settings.get('types')
    allowed_types = {'MESH', 'ARMATURE', 'PHYSICS', 'DISPLAY', 'MORPHS'}
    if not isinstance(types, (list, tuple, set)) or not types:
        raise ValueError('MMD import types must be a nonempty collection')
    types = sorted(set(types))
    if any(not isinstance(value, str) or value not in allowed_types
           for value in types):
        raise ValueError('MMD import types contain an unsupported value')
    settings['types'] = types
    bool_fields = {
        'clean_model', 'remove_doubles', 'import_adduv2_as_vertex_colors',
        'fix_bone_order', 'fix_ik_links', 'apply_bone_fixed_axis',
        'rename_bones', 'use_underscore', 'use_mipmap', 'save_log',
    }
    for field in bool_fields:
        if type(settings[field]) is not bool:
            raise ValueError(field + ' must be true or false')
    for field, minimum, maximum in (
            ('scale', 0.0001, 100.0),
            ('sph_blend_factor', -100.0, 100.0),
            ('spa_blend_factor', -100.0, 100.0)):
        value = settings[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)) \
                or not minimum <= float(value) <= maximum:
            raise ValueError(field + ' is outside its supported range')
        settings[field] = float(value)
    loop_factor = settings['ik_loop_factor']
    if isinstance(loop_factor, bool) or not isinstance(loop_factor, int) \
            or not 1 <= loop_factor <= 100:
        raise ValueError('ik_loop_factor must be an integer from 1 to 100')
    for field in ('dictionary', 'bone_disp_mode', 'log_level'):
        value = settings[field]
        if not isinstance(value, str) or not value:
            raise ValueError(field + ' must be a nonempty identifier')
    if settings['log_level'] not in {'DEBUG', 'INFO', 'WARNING', 'ERROR'}:
        raise ValueError('Unsupported MMD import log level')
    if settings['bone_disp_mode'] not in {
            'OCTAHEDRAL', 'STICK', 'BBONE', 'ENVELOPE', 'WIRE'}:
        raise ValueError('Unsupported MMD bone display mode')
    return settings


def _validate_pipeline_mmd_invariants(settings):
    """Reject presets that irreversibly damage the GEM2 source contract."""
    problems = []
    required_types = {'MESH', 'ARMATURE'}
    if not required_types.issubset(set(settings['types'])):
        problems.append('types must include MESH and ARMATURE')
    if not settings['clean_model']:
        problems.append('clean_model must be enabled')
    if settings['remove_doubles']:
        problems.append('remove_doubles must be disabled (preserve UV/normal seams)')
    if not settings['fix_bone_order']:
        problems.append('fix_bone_order must be enabled')
    if not settings['fix_ik_links']:
        problems.append('fix_ik_links must be enabled')
    if settings['apply_bone_fixed_axis']:
        problems.append('apply_bone_fixed_axis must be disabled')
    if not settings['rename_bones']:
        problems.append('rename_bones must be enabled')
    if not settings['use_underscore']:
        problems.append('use_underscore must be enabled (Arm_L/Leg_L contract)')
    if settings['dictionary'] != 'INTERNAL':
        problems.append("dictionary must be 'INTERNAL'")
    if problems:
        raise ValueError(
            'Selected MMD preset is incompatible with the GEM2 pipeline: '
            + '; '.join(problems)
            + '. Select GEM2 GOH + MOWAS2 Lossless.')
    return settings


def _parse_mmd_import_preset(path):
    with open(path, 'rb') as handle:
        raw = handle.read(64 * 1024 + 1)
    if len(raw) > 64 * 1024:
        raise ValueError('MMD import preset is unexpectedly large')
    try:
        tree = ast.parse(raw.decode('utf-8-sig'), filename=path, mode='exec')
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError('Invalid MMD import preset: ' + path) from exc
    if sum(1 for _ in ast.walk(tree)) > 500:
        raise ValueError('MMD import preset is too complex')
    values = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == 'op'):
            continue
        if target.attr not in _MMD_IMPORT_FIELDS:
            raise ValueError('Unsupported MMD preset property: ' + target.attr)
        try:
            values[target.attr] = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise ValueError('MMD preset property is not a literal: '
                             + target.attr) from exc
    if not values:
        raise ValueError('MMD import preset has no supported settings')
    return _validate_mmd_import_settings(values), raw


def mmd_import_preset_snapshot(preset_name):
    preset_name = str(preset_name or _MMD_PIPELINE_PRESET)
    if preset_name in (_MMD_PIPELINE_PRESET, 'Pipeline defaults'):
        settings = _validate_mmd_import_settings(_MMD_IMPORT_DEFAULTS)
        return {
            'preset': 'Pipeline defaults', 'preset_path': '',
            'preset_sha256': '', 'settings': settings,
        }
    path = list_mmd_import_presets().get(preset_name)
    if not path:
        raise FileNotFoundError('MMD import preset not found: ' + preset_name)
    settings, raw = _parse_mmd_import_preset(path)
    return {
        'preset': preset_name, 'preset_path': path,
        'preset_sha256': hashlib.sha256(raw).hexdigest(),
        'settings': settings,
    }


def _normalized_mmd_preset_name(value):
    value = str(value or _MMD_PIPELINE_PRESET)
    return 'Pipeline defaults' if value == _MMD_PIPELINE_PRESET else value


def _validated_mmd_import_snapshot(value):
    if not isinstance(value, dict):
        raise ValueError('MMD import snapshot must be a mapping')
    preset = _normalized_mmd_preset_name(value.get('preset'))
    preset_path = str(value.get('preset_path') or '')
    preset_sha256 = str(value.get('preset_sha256') or '')
    if preset_sha256 and (len(preset_sha256) != 64
                          or any(ch not in '0123456789abcdefABCDEF'
                                 for ch in preset_sha256)):
        raise ValueError('MMD import preset SHA-256 is invalid')
    return {
        'preset': preset,
        'preset_path': preset_path,
        'preset_sha256': preset_sha256.casefold(),
        'settings': _validate_mmd_import_settings(value.get('settings')),
    }


def _store_mmd_import_snapshot(scene, snapshot):
    snapshot = _validated_mmd_import_snapshot(snapshot)
    scene[_MMD_SCENE_PRESET_KEY] = snapshot['preset']
    scene[_MMD_SCENE_PATH_KEY] = snapshot['preset_path']
    scene[_MMD_SCENE_SHA256_KEY] = snapshot['preset_sha256']
    scene[_MMD_SCENE_SETTINGS_KEY] = json.dumps(
        snapshot['settings'], ensure_ascii=False, sort_keys=True)
    return snapshot


def _stored_mmd_import_snapshot(scene):
    stored = scene.get(_MMD_SCENE_SETTINGS_KEY, '')
    if not stored:
        return None
    try:
        settings = json.loads(stored)
        return _validated_mmd_import_snapshot({
            'preset': scene.get(_MMD_SCENE_PRESET_KEY, 'Pipeline defaults'),
            'preset_path': scene.get(_MMD_SCENE_PATH_KEY, ''),
            'preset_sha256': scene.get(_MMD_SCENE_SHA256_KEY, ''),
            'settings': settings,
        })
    except (TypeError, ValueError):
        return None


def _effective_mmd_import_snapshot(scene, props=None):
    props = props or getattr(scene, 'mowas2_props', None)
    selected = _normalized_mmd_preset_name(
        getattr(props, 'mmd_import_preset', _MMD_PIPELINE_PRESET))
    stored = _stored_mmd_import_snapshot(scene)
    if stored is not None and stored['preset'] == selected:
        return stored
    return _store_mmd_import_snapshot(
        scene, mmd_import_preset_snapshot(selected))


def import_pmx(filepath, types=None, preset_name=None, preset_settings=None,
               preset_path='', preset_sha256=''):
    """步骤1：按已验证的 MMD Tools 预设导入 PMX。返回 (arm_obj, mesh_obj)。"""
    scene = bpy.context.scene
    if preset_settings is None and preset_name is None:
        snapshot = _effective_mmd_import_snapshot(scene)
    elif preset_settings is None:
        snapshot = _validated_mmd_import_snapshot(
            mmd_import_preset_snapshot(preset_name))
    else:
        snapshot = _validated_mmd_import_snapshot({
            'preset': preset_name or 'Captured settings',
            'preset_path': preset_path,
            'preset_sha256': preset_sha256,
            'settings': preset_settings,
        })
    settings = dict(snapshot['settings'])
    if types is not None:
        settings['types'] = sorted(set(types))
    snapshot['settings'] = _validate_pipeline_mmd_invariants(
        _validate_mmd_import_settings(settings))
    snapshot = _store_mmd_import_snapshot(scene, snapshot)
    settings = snapshot['settings']
    operator_settings = dict(settings)
    operator_settings['types'] = set(operator_settings['types'])
    operator_settings['filepath'] = filepath
    bpy.ops.mmd_tools.import_model(**operator_settings)
    arms = [o for o in bpy.data.objects if o.type == 'ARMATURE']
    arm = max(arms, key=lambda a: len(a.data.bones)) if arms else None
    big = [o for o in bpy.data.objects if o.type == 'MESH'
           and len(o.data.vertices) > 500]
    mesh = max(big, key=lambda o: len(o.data.vertices)) if big else None
    if arm is None or mesh is None:
        raise RuntimeError(_("mowas2.err.pmx_objects_missing",
                             file=os.path.basename(filepath)))
    return arm, mesh


def _legacy_pose_is_identity(tgt):
    identity = Matrix.Identity(4)
    for pose_bone in tgt.pose.bones:
        delta = max(abs(float(pose_bone.matrix_basis[row][column]
                          - identity[row][column]))
                    for row in range(4) for column in range(4))
        if delta > 1.0e-5:
            raise RuntimeError(
                'Cannot rebuild PMX rest while target is posed: ' + tgt.name)


def _legacy_rest_from_raw_metadata(tgt):
    """Restore the historical PMX display rest from retained MDL data.

    Some files were opened with the short-lived normalized human display
    implementation. Their raw matrices are still intact, so reconstruct the
    exact old ``mdl_io`` edit-bone state before applying the legacy Y mirror.
    """
    raw_value = tgt.get('gem2_world_mats')
    parent_value = tgt.get('gem2_parents')
    if not raw_value or not parent_value:
        raise RuntimeError(
            'Cannot restore legacy PMX rest without raw MDL metadata: '
            + tgt.name)
    try:
        decoded_matrices = json.loads(raw_value)
        parents = json.loads(parent_value)
        if not isinstance(decoded_matrices, dict) or not isinstance(parents, dict):
            raise TypeError('raw MDL metadata is not a bone mapping')
        raw_matrices = {name: Matrix(value)
                        for name, value in decoded_matrices.items()}
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            'Invalid raw MDL metadata on target: ' + tgt.name) from exc

    edit_names = {bone.name for bone in tgt.data.bones}
    raw_names = set(raw_matrices)
    if edit_names != raw_names:
        missing = sorted(raw_names - edit_names)
        extra = sorted(edit_names - raw_names)
        raise RuntimeError(
            'Target bones do not match retained MDL metadata (%s; %s): %s'
            % ('missing=' + ','.join(missing) if missing else 'no missing',
               'extra=' + ','.join(extra) if extra else 'no extra', tgt.name))
    unknown_parent_keys = set(parents) - raw_names
    missing_parent_keys = raw_names - set(parents)
    if unknown_parent_keys or missing_parent_keys:
        details = []
        if missing_parent_keys:
            details.append('missing=' + ','.join(sorted(missing_parent_keys)))
        if unknown_parent_keys:
            details.append('unknown=' + ','.join(sorted(unknown_parent_keys)))
        raise RuntimeError(
            'Raw MDL parent map does not cover target bones (%s): %s'
            % (';'.join(details), tgt.name))
    for name, parent_name in parents.items():
        if parent_name and parent_name not in raw_names:
            raise RuntimeError(
                'Raw MDL parent is missing for %s: %s' % (name, parent_name))

    if getattr(tgt, 'mode', 'OBJECT') != 'OBJECT':
        raise RuntimeError(
            'Cannot rebuild PMX rest outside Object mode: ' + tgt.name)
    _legacy_pose_is_identity(tgt)
    previous_active = bpy.context.view_layer.objects.active
    previous_selected = list(bpy.context.selected_objects)
    rest_module = _human_rest_module()
    try:
        for obj in previous_selected:
            obj.select_set(False)
        tgt.select_set(True)
        bpy.context.view_layer.objects.active = tgt
        rest_module._mode_set_for_armature(tgt, 'EDIT')
        try:
            edit_bones = tgt.data.edit_bones
            for edit_bone in edit_bones:
                edit_bone.use_connect = False
                edit_bone.parent = None
            for name, matrix in raw_matrices.items():
                edit_bones[name].matrix = matrix
            for name, parent_name in parents.items():
                if parent_name and parent_name != name:
                    edit_bones[name].parent = edit_bones[parent_name]
                    edit_bones[name].use_connect = False
            # Match mdl_io.build_armature(preserve_rest_matrix=False) exactly.
            for edit_bone in edit_bones:
                if edit_bone.children:
                    edit_bone.tail = list(edit_bone.children)[0].head.copy()
                else:
                    x_axis = edit_bone.matrix.col[0].xyz
                    if x_axis.length < 0.001:
                        x_axis = Vector((1.0, 0.0, 0.0))
                    edit_bone.tail = (edit_bone.head
                                      + x_axis.normalized() * 0.2)
            mirror = Matrix.Diagonal((1.0, -1.0, 1.0, 1.0))
            for edit_bone in edit_bones:
                edit_bone.matrix = mirror @ edit_bone.matrix
        finally:
            rest_module._mode_set_for_armature(tgt, 'OBJECT')
        bpy.context.view_layer.update()
    finally:
        try:
            if tgt.mode != 'OBJECT':
                rest_module._mode_set_for_armature(tgt, 'OBJECT')
        except (AttributeError, RuntimeError):
            pass
        for obj in list(bpy.context.selected_objects):
            try:
                obj.select_set(False)
            except (ReferenceError, RuntimeError):
                pass
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


def _legacy_mirror_current_rest(tgt):
    """Apply the original uniform Y mirror to a fresh target."""
    if getattr(tgt, 'mode', 'OBJECT') != 'OBJECT':
        raise RuntimeError(
            'Cannot rebuild PMX rest outside Object mode: ' + tgt.name)
    _legacy_pose_is_identity(tgt)
    previous_active = bpy.context.view_layer.objects.active
    previous_selected = list(bpy.context.selected_objects)
    rest_module = _human_rest_module()
    mirror = Matrix.Diagonal((1.0, -1.0, 1.0, 1.0))
    try:
        for obj in previous_selected:
            obj.select_set(False)
        tgt.select_set(True)
        bpy.context.view_layer.objects.active = tgt
        rest_module._mode_set_for_armature(tgt, 'EDIT')
        try:
            for edit_bone in tgt.data.edit_bones:
                edit_bone.matrix = mirror @ edit_bone.matrix
        finally:
            rest_module._mode_set_for_armature(tgt, 'OBJECT')
        bpy.context.view_layer.update()
    finally:
        try:
            if tgt.mode != 'OBJECT':
                rest_module._mode_set_for_armature(tgt, 'OBJECT')
        except (AttributeError, RuntimeError):
            pass
        for obj in list(bpy.context.selected_objects):
            try:
                obj.select_set(False)
            except (ReferenceError, RuntimeError):
                pass
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


def rebuild_frame0_rest(tgt):
    """Restore the historical PMX/GFA frame-0 rest convention.

    This is deliberately the legacy path: ``mdl_io`` creates raw MDL bones,
    tails are aimed at their first child, and one uniform Y mirror is applied.
    Human rest conversion uses ``rebuild_human_display_rest`` instead; the two
    display conventions are not interchangeable for PMX alignment/GFA. A
    human-only target marker is rejected here rather than silently remirrored.
    """
    # Only a target carrying both the human marker and the legacy marker is
    # treated as a transiently polluted PMX target. A human-only marker is an
    # intentional normalized display contract and must never be remirrored.
    if (not tgt.get('gem2_human_rest_display_rest')
            and tgt.get('gem2_frame0_rest_mode') == 'human_normalized'):
        raise RuntimeError(
            'Target has an unstable human rest mode marker without the stable '
            'display marker; refusing to mirror it: ' + tgt.name)
    if tgt.get('gem2_human_rest_display_rest'):
        if not tgt.get('mowas2_frame0_rest'):
            raise RuntimeError(
                'Target is an intentional human normalized-rest rig; refusing '
                'to replace it with the legacy PMX display: ' + tgt.name)
        _legacy_rest_from_raw_metadata(tgt)
        for key in ('gem2_human_rest_display_rest',
                    'gem2_human_rest_display_version',
                    'gem2_human_rest_raw_rest',
                    'gem2_frame0_rest_mode'):
            try:
                del tgt[key]
            except KeyError:
                pass
    elif tgt.get('mowas2_frame0_rest'):
        return {'legacy': True, 'reused': True, 'bones': len(tgt.data.bones)}
    elif tgt.get('gem2_frame0_rest_mode'):
        raise RuntimeError(
            'Target has an unstable rest mode marker without a stable display '
            'marker; refusing to mirror it: ' + tgt.name)
    else:
        _legacy_mirror_current_rest(tgt)
    tgt['mowas2_frame0_rest'] = True
    print('[frame0] target rest rebuilt (legacy Y-mirror):', tgt.name)
    return {'legacy': True, 'reused': False, 'bones': len(tgt.data.bones)}


def rebuild_human_display_rest(tgt, mark_raw=False):
    """Opt-in normalized display rest for the dedicated human converter."""
    rest = _human_rest_module()
    graph = rest.graph_from_armature(tgt)
    report = rest.apply_human_display_rest(tgt, graph, mark_raw=mark_raw)
    print('[human] target rest rebuilt from normalized MDL:',
          tgt.name, 'max_delta=', report.get('max_delta', 0.0))
    return report


def _ensure_legacy_frame0_rest(tgt):
    """Make a PMX pipeline target use the historical display convention."""
    if tgt is None:
        return None
    if (tgt.get('gem2_human_rest_display_rest')
            or tgt.get('gem2_frame0_rest_mode')
            or not tgt.get('mowas2_frame0_rest')):
        return rebuild_frame0_rest(tgt)
    return {'legacy': True, 'reused': True, 'bones': len(tgt.data.bones)}


def build_target_from_mdl(mdl_path, name='skin_Armature',
                          preserve_rest_matrix=False, rebuild_display=False):
    """Build a target with an explicit legacy or human display convention.

    ``rebuild_display=False`` is the PMX/GFA-compatible default and applies
    the historical uniform Y mirror.  Human rest conversion passes
    ``preserve_rest_matrix=True, rebuild_display=True`` to opt into normalized
    Blender display frames while raw MDL metadata remains authoritative.
    """
    from . import mdl_io
    with open(mdl_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    root_bones, mesh_parent_name = mdl_io.parse_mdl(content)
    tgt = mdl_io.build_armature(
        'skin', root_bones, mesh_parent_name, mdl_path,
        preserve_rest_matrix=preserve_rest_matrix)
    tgt.name = name
    if rebuild_display:
        rebuild_human_display_rest(tgt, mark_raw=True)
    else:
        rebuild_frame0_rest(tgt)
    tgt.select_set(True)
    bpy.context.view_layer.objects.active = tgt
    return tgt


def _human_rest_module():
    from . import human_rest_convert
    return human_rest_convert


def _human_rest_same_object(left, right):
    if left is right:
        return True
    if left is None or right is None:
        return False
    try:
        return int(left.as_pointer()) == int(right.as_pointer())
    except (AttributeError, TypeError, ValueError, ReferenceError):
        return False


def _human_rest_conversion_marked(obj):
    """Treat even an empty/corrupt conversion record as a guarded result."""
    try:
        return obj is not None and obj.get('gem2_human_rest_conversion') is not None
    except (AttributeError, ReferenceError, RuntimeError):
        return False


def _human_rest_path(value):
    if not value:
        return ''
    return os.path.normcase(os.path.abspath(bpy.path.abspath(str(value))))


def _human_rest_armature_path(armature):
    return _human_rest_path(armature.get('gem2_mdl_path') if armature else '')


def _human_rest_unique_name(base):
    if bpy.data.objects.get(base) is None:
        return base
    index = 1
    while bpy.data.objects.get('%s.%03d' % (base, index)) is not None:
        index += 1
    return '%s.%03d' % (base, index)


def _human_rest_mesh_candidates(scene, source_armature=None):
    candidates = []
    for obj in scene.objects:
        if obj.type != 'MESH' or not obj.vertex_groups:
            continue
        arm_mods = [modifier for modifier in obj.modifiers
                    if modifier.type == 'ARMATURE' and modifier.object]
        if source_armature is not None:
            if any(_human_rest_same_object(modifier.object, source_armature)
                   for modifier in arm_mods):
                candidates.append(obj)
        elif arm_mods:
            candidates.append(obj)
    return sorted(candidates, key=lambda obj: (-len(obj.data.vertices), obj.name.casefold()))


def _human_rest_convert_available(context):
    try:
        scene = context.scene
        props = getattr(scene, 'mowas2_props', None)
        path = _human_rest_path(getattr(props, 'mdl_path', ''))
        if not path or not os.path.isfile(path):
            return False
        active = getattr(context.view_layer.objects, 'active', None)
        mesh_hint = active if active is not None and active.type == 'MESH' else None
        if mesh_hint is not None:
            armature_modifiers = [modifier for modifier in mesh_hint.modifiers
                                  if modifier.type == 'ARMATURE'
                                  and modifier.object]
            if len(armature_modifiers) != 1:
                return False
        source = _human_rest_find_source(scene, mesh_hint)
        human_rest = _human_rest_module()
        if (source.mode != 'OBJECT'
                or not human_rest._pose_is_identity(source)):
            return False
        meshes = _human_rest_find_meshes(scene, source, active=active)
        if any(mesh.mode != 'OBJECT' or mesh.data.users > 1
               or sum(1 for modifier in mesh.modifiers
                      if modifier.type == 'ARMATURE') != 1
               for mesh in meshes):
            return False
        return True
    except Exception:
        # Blender calls poll while contexts and partially-registered scene
        # properties are changing; an unavailable button is safer than a UI
        # exception or an implicit all-mesh conversion.
        return False


def _human_rest_export_available(context):
    try:
        mesh = _human_rest_converted_mesh(context.scene)
        _human_rest_target_for_mesh(context.scene, mesh)
        return True
    except Exception:
        return False


def _human_rest_find_source(scene, mesh=None, explicit_name=''):
    if explicit_name:
        source = next((obj for obj in scene.objects
                       if obj.name == explicit_name), None)
        if source is None or source.type != 'ARMATURE':
            raise RuntimeError('Human rest source armature not found: ' + explicit_name)
        return source
    if mesh is not None:
        targets = [modifier.object for modifier in mesh.modifiers
                   if modifier.type == 'ARMATURE' and modifier.object
                   and modifier.object.type == 'ARMATURE']
        unique = []
        for target in targets:
            if not any(_human_rest_same_object(target, item)
                       for item in unique):
                unique.append(target)
        if len(unique) == 1:
            return unique[0]
        metadata = _human_rest_conversion_metadata(mesh)
        previous_destination = metadata.get('destination_armature', '')
        if previous_destination:
            tagged = next((obj for obj in scene.objects
                           if obj.name == previous_destination
                           and obj.type == 'ARMATURE'
                           and obj.get('gem2_world_mats')), None)
            if tagged is not None and not unique:
                return tagged
            if tagged is not None and any(
                    _human_rest_same_object(tagged, item) for item in unique):
                return tagged
        if len(unique) > 1:
            tagged = [arm for arm in unique if arm.get('gem2_world_mats')]
            if len(tagged) == 1:
                return tagged[0]
            raise RuntimeError('Mesh has multiple possible source armatures')
    selected = [obj for obj in scene.objects
                if obj.type == 'ARMATURE' and obj.select_get()]
    tagged = [obj for obj in selected if obj.get('gem2_world_mats')]
    if len(tagged) == 1:
        return tagged[0]
    tagged = [obj for obj in scene.objects
              if obj.type == 'ARMATURE' and obj.get('gem2_world_mats')]
    if len(tagged) == 1:
        return tagged[0]
    raise RuntimeError('Select a GEM2 human mesh or source armature')


def _human_rest_mesh_bound_to(mesh, armature):
    return any(modifier.type == 'ARMATURE'
               and _human_rest_same_object(modifier.object, armature)
               for modifier in mesh.modifiers)


def _human_rest_find_meshes(scene, source_armature, explicit_name='', active=None):
    if explicit_name:
        mesh = next((obj for obj in scene.objects
                     if obj.name == explicit_name), None)
        if mesh is None or mesh.type != 'MESH':
            raise RuntimeError('Human rest mesh not found: ' + explicit_name)
        if not _human_rest_mesh_bound_to(mesh, source_armature):
            raise RuntimeError(
                'Human rest mesh is not bound to the selected source armature: '
                + explicit_name)
        return [mesh]
    if active is not None and active.type == 'MESH':
        if _human_rest_mesh_bound_to(active, source_armature):
            return [active]
    selected = [obj for obj in scene.objects
                if obj.type == 'MESH' and obj.select_get()
                and _human_rest_mesh_bound_to(obj, source_armature)]
    if selected:
        return sorted(selected, key=lambda obj: obj.name.casefold())
    meshes = _human_rest_mesh_candidates(scene, source_armature)
    if not meshes:
        raise RuntimeError('No mesh bound to the selected source armature')
    if len(meshes) > 1:
        raise RuntimeError(
            'Select one or more human meshes bound to the source armature')
    return meshes


def _human_rest_target_usable(armature):
    """Accept raw rigs and repairable native legacy human targets."""
    if armature is None or armature.type != 'ARMATURE':
        return False
    # A legacy marker without raw MDL data is a pure PMX display rig and cannot
    # be repaired safely. Native human .blend files may retain the marker while
    # keeping the complete raw contract; the conversion path rebuilds them.
    if (armature.get('mowas2_frame0_rest')
            and not armature.get('gem2_human_rest_display_rest')):
        from . import human_rest_convert
        return human_rest_convert.has_raw_mdl_contract(armature)
    return True


def _human_rest_find_target(scene, mdl_path, source_armature):
    expected = _human_rest_path(mdl_path)
    candidates = [obj for obj in scene.objects
                  if obj.type == 'ARMATURE'
                  and not _human_rest_same_object(obj, source_armature)
                  and obj.get('gem2_world_mats')
                  and _human_rest_target_usable(obj)
                  and _human_rest_armature_path(obj) == expected]
    if not candidates:
        return None
    selected = [obj for obj in candidates if obj.select_get()]
    return (selected[0] if selected else
            sorted(candidates, key=lambda obj: obj.name.casefold())[0])


def _human_rest_duplicate_mesh(mesh, scene):
    data = mesh.data.copy()
    duplicate = mesh.copy()
    duplicate.data = data
    duplicate.name = _human_rest_unique_name(mesh.name + '_converted')
    scene.collection.objects.link(duplicate)
    duplicate.matrix_world = mesh.matrix_world.copy()
    duplicate.select_set(False)
    return duplicate


def _human_rest_restore_selection(scene, selected, active):
    try:
        for obj in scene.objects:
            obj.select_set(False)
        for obj in selected:
            if scene.objects.get(obj.name) is not None:
                obj.select_set(True)
        if active is not None and scene.objects.get(active.name) is not None:
            bpy.context.view_layer.objects.active = active
        else:
            bpy.context.view_layer.objects.active = None
    except (AttributeError, ReferenceError, RuntimeError):
        pass


def convert_human_rest_scene(scene, mdl_path, mesh_name='',
                             source_armature_name='', target_armature_name='',
                             duplicate_mesh=False, exact_reverse=True):
    """Convert imported GEM2 human meshes without invoking the PMX pipeline."""
    if not mdl_path or not os.path.isfile(bpy.path.abspath(str(mdl_path))):
        raise RuntimeError('Destination human MDL not found: ' + str(mdl_path))
    original_active = bpy.context.view_layer.objects.active
    original_selected = [obj for obj in scene.objects if obj.select_get()]
    active = original_active
    mesh_hint = (next((obj for obj in scene.objects if obj.name == mesh_name), None)
                 if mesh_name else
                 (active if active and active.type == 'MESH'
                  and scene.objects.get(active.name) is not None else None))
    source = _human_rest_find_source(scene, mesh_hint, source_armature_name)
    meshes = _human_rest_find_meshes(scene, source, mesh_name, active=active)
    destination_path = _human_rest_path(mdl_path)
    source_path = _human_rest_armature_path(source)
    if source_path and destination_path == source_path:
        raise RuntimeError(
            'Mesh is already bound to the destination armature: ' + source.name)
    for mesh in meshes:
        metadata = _human_rest_conversion_metadata(mesh)
        if (_human_rest_path(metadata.get('destination_mdl', ''))
                == destination_path
                and metadata.get('destination_armature') == source.name):
            raise RuntimeError(
                'Mesh is already in the destination rest space: ' + mesh.name)
    module = _human_rest_module()
    # A legacy PMX target is a different display contract. Do not silently
    # rewrite it when a user invokes the human converter on the wrong scene.
    module._assert_human_display_source(source, 'Source armature')
    # Imported PLY coordinates are already in normalized model space. Repair
    # raw source bones from metadata before any mesh math so Pose Mode and the
    # affine transfer use the same rest frame.
    source_graph = module.graph_from_armature(source)
    target = None
    created_target = False
    if target_armature_name:
        target = next((obj for obj in scene.objects
                       if obj.name == target_armature_name), None)
        if target is None or target.type != 'ARMATURE':
            raise RuntimeError('Human rest target armature not found: ' + target_armature_name)
        if _human_rest_same_object(target, source):
            raise RuntimeError('Human rest source and target armatures must differ')
        if not _human_rest_target_usable(target):
            raise RuntimeError(
                'Target armature uses a frame-0 display rest; build a raw-rest target')
        target_path = _human_rest_armature_path(target)
        if target_path != _human_rest_path(mdl_path):
            raise RuntimeError(
                'Target armature MDL does not match destination: ' + target.name)
    else:
        target = _human_rest_find_target(scene, mdl_path, source)
    if target is None:
        target = build_target_from_mdl(
            bpy.path.abspath(str(mdl_path)),
            name=_human_rest_unique_name('skin_Armature'),
            preserve_rest_matrix=True,
            rebuild_display=True)
        target['gem2_human_rest_raw_rest'] = True
        created_target = True

    # Existing targets may have been built by the old project-then-mirror
    # implementation.  Rebuild from raw MDL data every time; the helper is
    # idempotent and never overwrites gem2_world_mats.
    target_graph = module.graph_from_armature(target)
    source_display_report = module.apply_human_display_rest(
        source, source_graph)
    target_display_report = module.apply_human_display_rest(
        target, target_graph, mark_raw=True)
    target['gem2_human_rest_raw_rest'] = True

    converted_meshes = []
    originals = []
    try:
        for mesh in meshes:
            if duplicate_mesh:
                duplicate = _human_rest_duplicate_mesh(mesh, scene)
                converted_meshes.append(duplicate)
                originals.append(duplicate)
            else:
                converted_meshes.append(mesh)
        route = _infer_export_route(str(mdl_path))
        preferred_order = (module.skin_order_for_route(route)
                           if route in {'GOH', 'MOWAS2'} else None)
        report = module.convert_meshes(
            converted_meshes, source, target,
            source_graph=source_graph,
            destination_graph=target_graph,
            preferred_skin_order=preferred_order,
            exact_reverse=exact_reverse,
            _prepare_display=False)
        report.update({
            'source_display_max_delta': source_display_report.get('max_delta', 0.0),
            'destination_display_max_delta': target_display_report.get('max_delta', 0.0),
        })
    except Exception:
        if created_target and target is not None:
            try:
                data = target.data
                bpy.data.objects.remove(target, do_unlink=True)
                if data.users == 0:
                    bpy.data.armatures.remove(data)
            except (ReferenceError, RuntimeError):
                pass
        for duplicate in originals:
            try:
                data = duplicate.data
                bpy.data.objects.remove(duplicate, do_unlink=True)
                if data.users == 0:
                    bpy.data.meshes.remove(data)
            except (ReferenceError, RuntimeError):
                pass
        _human_rest_restore_selection(scene, original_selected, original_active)
        raise

    for obj in scene.objects:
        obj.select_set(False)
    for mesh in converted_meshes:
        mesh.select_set(True)
    bpy.context.view_layer.objects.active = converted_meshes[0]
    try:
        scene.mowas2_props.mdl_path = bpy.path.abspath(str(mdl_path))
        inferred = _infer_export_route(mdl_path)
        if inferred != 'CUSTOM':
            scene.mowas2_props.export_route = inferred
    except (AttributeError, RuntimeError):
        pass
    report.update({
        'source_armature': source.name,
        'destination_armature': target.name,
        'mesh_names': [mesh.name for mesh in converted_meshes],
        'duplicated': bool(duplicate_mesh),
    })
    return report


def _ensure_bundled_target_variant(src, tgt, mesh, root, pmx_path=None,
                                    force_short=False):
    """Keep the bundled target skeleton consistent with the long-arm toggle.

    Older scenes often keep ``samples/goh_skin.mdl`` even after the long-arm
    option was enabled. That short template shares the same torso and shoulder
    with GFA but ends hand_rot1 about 2.6 units inward (agf_nijita ground truth),
    so fitting to it visibly recesses the hand into the forearm. Only replace
    the two known bundled templates; an explicitly selected custom MDL is never
    changed. Frozen meshes must be rebuilt from their PMX rather than stretched.
    ``force_short`` is used by the explicit GOH route so programmatic callers
    without registered scene properties still get the route reference target.
    """
    short_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'samples', 'goh_skin.mdl')
    long_path = GOH_DEFAULT_MDL
    desired = (short_path if force_short
               else (long_path if _goh_gfa_longarm() else short_path))
    current = tgt.get('gem2_mdl_path') if tgt else None
    if not current:
        return src, tgt, mesh, root, False

    norm = lambda path: os.path.normcase(os.path.abspath(path))
    known = {norm(short_path), norm(long_path)}
    if norm(current) not in known or norm(current) == norm(desired):
        return src, tgt, mesh, root, False

    frozen = bool(mesh.get('mowas2_frozen')) or (
        not any(mod.type == 'ARMATURE' for mod in mesh.modifiers)
        and mesh.parent == tgt)
    if frozen and (not pmx_path or not os.path.isfile(pmx_path)):
        raise RuntimeError(_("mowas2.err.longarm_requires_pmx"))

    old_name = tgt.name
    old_data = tgt.data
    bpy.data.objects.remove(tgt, do_unlink=True)
    if old_data.users == 0:
        bpy.data.armatures.remove(old_data)
    tgt = build_target_from_mdl(desired, name=old_name)
    try:
        bpy.context.scene.mowas2_props.mdl_path = desired
    except Exception:
        pass
    print('[target] bundled template switched:', os.path.basename(current),
          '->', os.path.basename(desired))

    if frozen:
        src, mesh, root = _reimport_source(pmx_path, tgt)
        print('[target] frozen short-arm mesh reimported from PMX')
    return src, tgt, mesh, root, True


def _assert_legacy_pmx_scene(mesh, target=None):
    """Keep PMX alignment from mutating a human-conversion scene."""
    if _human_rest_conversion_marked(mesh):
        raise RuntimeError(
            'This mesh is a human-rest conversion result; use the dedicated '
            'human export operator instead of PMX alignment')
    if (target is not None
            and target.get('gem2_human_rest_display_rest')
            and not target.get('mowas2_frame0_rest')):
        raise RuntimeError(
            'The selected target uses the human normalized rest; use the '
            'human conversion/export path instead of PMX alignment')
    if target is None:
        return
    scene = bpy.context.scene
    for candidate in scene.objects:
        if candidate.type != 'MESH' \
                or not _human_rest_conversion_marked(candidate):
            continue
        references_target = any(
            modifier.type == 'ARMATURE'
            and _human_rest_same_object(modifier.object, target)
            for modifier in candidate.modifiers)
        if references_target:
            raise RuntimeError(
                'The selected target armature is used by a human-rest '
                'conversion result; export that result separately before '
                'running PMX alignment')


def align_only(output_dir=None, ground_z=GROUND_Z, pmx_path=None):
    """仅对齐：把源模型整体刚性拟合到目标骨架（头/身/腿摆好、贴地、手臂垂落）。
    不绑骨、不减面、不导出 —— 供面板【步骤3 摆好头身腿】使用。
    若网格已冻结（跑过本步骤/完整管线），直接返回已保存快照，避免重复变换；
    但冻结后修改脚部、肩宽、头颈或已应用的瞳孔间距时，会自动重新导入源
    模型重新对齐（否则新值不生效或无法恢复旧几何）。"""
    if not output_dir:
        output_dir = OUT_DEFAULT
    src, tgt, mesh, root = resolve_scene()
    _assert_legacy_pmx_scene(mesh, tgt)
    src, tgt, mesh, root, _target_changed = _ensure_bundled_target_variant(
        src, tgt, mesh, root, pmx_path=pmx_path)
    # 旧场景兼容：PMX 管线始终使用历史 frame-0 显示约定；若场景曾被
    # 临时 human-normalized 版本污染，从保留的 raw MDL 元数据恢复。
    if tgt is not None:
        _ensure_legacy_frame0_rest(tgt)
    if mesh.get('mowas2_frozen'):
        if _align_options_changed(mesh, src):
            if not pmx_path or not os.path.isfile(pmx_path):
                raise RuntimeError(_("mowas2.err.align_changed_requires_pmx"))
            print('[reimport] 冻结几何选项已修改, 重新导入并重新对齐')
            src, mesh, root = _reimport_source(pmx_path, tgt)
        else:
            print('[align] 已冻结，跳过（直接执行步骤4 完整导出即可）')
            return os.path.join(output_dir, 'mowas2', 'mowas2_aligned.blend')
    print('=' * 60)
    print('GOH align-only (head/body/legs)')
    print('  src:', src.name, '| tgt:', tgt.name, '| mesh:', mesh.name)

    # 1. rigids
    def _collect(o):
        out = []
        for c in o.children:
            out.extend(_collect(c))
        out.append(o)
        return out
    removed = 0
    for rn in ('rigidbodies', 'joints'):
        r = bpy.data.objects.get(rn)
        if r:
            for o in _collect(r):
                try:
                    bpy.data.objects.remove(o, do_unlink=True)
                    removed += 1
                except Exception:
                    pass
    print('[1] rigids removed:', removed)

    # 2. mirror
    # 2026-08-17 晚: 标准 MMD 源跳过镜像判定 —— is_source_mirrored 用
    # Eye_L.y vs 目标 foot1l.y 符号判左右, 该判据依赖"源导入朝向与 GEM2
    # 目标一致"(KK 满足); 标准 MMD 导入朝向不同 (实测前鬼坊 Eye_L.y=-0.55
    # vs foot1l.y=+2.17) → 误判镜像 → align_arms 交叉配对 → 手错位。
    # 模之屋/崩3/原神模型左右正常 (Eye_L 与 Shoulder_L 同侧自洽), 直接
    # mirrored=False; 确为镜像的 MMD 源可手动改本行。
    mode = detect_source_mode(tgt=tgt, src=src)
    if mode == 'mmd':
        mirrored = False
        print('[2] MMD 源 (%s): 跳过镜像判定, mirrored=False' % src.name)
    else:
        mirrored = is_source_mirrored(src, tgt)
        print('[2] mirrored:', mirrored)

    # 3. rigid fit + ground
    align_rigid(src, tgt, mesh, root, mirrored, ground_z)
    print('[3] rigid fit done')

    # 3.25 GOH v12: GFA 骨骼级躯干链对齐 (freeze 前, 网格随骨架形变)
    #     全盘 GFA 思路: 骨架缩放/偏移 + 网格自动跟随 → 无顶点压缩/撕裂。
    if _goh_hand_split():
        n_gfa = goh_gfa_bone_align(src, tgt, mesh, source_mode=mode)
        print('[3.25] GFA bone align moved:', n_gfa)

    # 4. arms + hands (帧0 自适应: KK/T-pose 直接; MMD A-pose/垂手先抬臂)
    pose, ang = align_arms_auto(src, tgt, mirrored, source_mode=mode)
    print('[4] arms posed (pose=%s, %.1f°)' % (pose, ang))
    print('[4.5] hands posed')

    # 5. freeze：把对齐后的网格固化（世界坐标写回），并打上「已对齐」标记。
    #    冻结后 mesh 脱离源骨架，后续 run_full 检测到 mowas2_frozen 会跳过对齐阶段，
    #    直接从绑定开始 —— 避免重复刚性拟合导致双重变换。
    freeze_mesh(mesh)
    neck_result = goh_scale_neck_follow_geometry(mesh, src)
    if neck_result.get('changed'):
        print('[5.06] neck/accessory follow:', neck_result['changed'])
    pupil_result = goh_fix_pupil_depth_geometry(mesh, src)
    if pupil_result.get('changed'):
        print('[5.07] pupil depth geometry:', pupil_result['changed'])
    forearm_result = goh_retarget_mmd_forearm_geometry(
        mesh, src, tgt, mirrored=mirrored, source_mode=mode)
    if forearm_result.get('changed'):
        print('[5.1] MMD forearm/wrist geometry:', forearm_result['changed'])
    # GFA 后任一源分支都可能改变脚底；归一化只读取源 ankle 权重，
    # 不改变 KK/MMD 的骨骼对齐分支。
    normalize_foot_geometry(mesh, src, tgt, ground_z)
    body_result = goh_adjust_body_curve_geometry(
        mesh, src, tgt, mirrored=mirrored)
    if body_result.get('changed'):
        print('[5.2] body curve / foot1 spacing:', body_result['changed'])
    arm_inset_result = goh_adjust_arm_inset_geometry(
        mesh, src, tgt, mirrored=mirrored)
    if arm_inset_result.get('changed'):
        print('[5.3] rigid whole-arm inset:', arm_inset_result['changed'])
    mesh['mowas2_frozen'] = True
    mesh['mowas2_mirrored'] = mirrored   # 复用首次判定的镜像方向，供绑定阶段换名
    mesh['mowas2_source_mode'] = mode
    print('[5] frozen (aligned state saved)')

    # 保存对齐后快照（供用户检查效果）
    snap_dir = os.path.join(output_dir, 'mowas2')
    os.makedirs(snap_dir, exist_ok=True)
    snap = os.path.join(snap_dir, 'mowas2_aligned.blend')
    try:
        bpy.ops.wm.save_as_mainfile(filepath=snap, copy=True)
    except Exception as e:
        print('[snap] save skipped:', e)
    print('=' * 60)
    return snap


def _route_target_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _validate_export_route_target(export_route, target_mdl):
    route = str(export_route or 'CUSTOM')
    if route not in {'GOH', 'MOWAS2', 'CUSTOM'}:
        raise ValueError('Unsupported export route: ' + route)
    if route == 'CUSTOM':
        return route
    expected = GOH_ROUTE_MDL if route == 'GOH' else MOWAS2_ROUTE_MDL
    if not target_mdl or not os.path.isfile(target_mdl):
        raise FileNotFoundError('Route target MDL not found: ' + str(target_mdl))
    if not os.path.isfile(expected):
        raise FileNotFoundError('Bundled route reference not found: ' + expected)
    same_path = os.path.normcase(os.path.abspath(target_mdl)) == \
        os.path.normcase(os.path.abspath(expected))
    if not same_path and _route_target_sha256(target_mdl) != _route_target_sha256(expected):
        raise ValueError('%s route target does not match its reference skeleton' % route)
    return route


def _replace_direct_bone_volume_view(content, bone_name, ply_filename):
    """Replace the direct VolumeView on one named bone, ignoring descendants."""
    from .core import find_matching_brace

    header = re.compile(
        r'\{\s*bone(?:\s+[A-Za-z_][A-Za-z0-9_]*)*\s+"'
        + re.escape(str(bone_name)) + r'"', re.IGNORECASE)
    matches = list(header.finditer(content))
    if len(matches) != 1:
        raise RuntimeError(
            'Expected exactly one MDL bone %r, found %d'
            % (bone_name, len(matches)))
    bone_start = matches[0].start()
    bone_end = find_matching_brace(content, bone_start)
    if bone_end < 0:
        raise RuntimeError('Unbalanced MDL bone block: ' + str(bone_name))

    view_pattern = re.compile(
        r'\{\s*VolumeView\s+"(?P<filename>[^"]*)"\s*\}', re.IGNORECASE)
    direct_matches = []
    depth = 0
    in_string = False
    escaped = False
    index = bone_start
    while index <= bone_end:
        char = content[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == '{':
            if depth == 1:
                match = view_pattern.match(content, index)
                if match is not None and match.end() <= bone_end + 1:
                    direct_matches.append(match)
                    index = match.end()
                    continue
            depth += 1
        elif char == '}':
            depth -= 1
        index += 1

    if not direct_matches:
        raise RuntimeError(
            'Expected a direct VolumeView on MDL bone %r, found none'
            % bone_name)
    # The first direct view is the mesh parent's primary payload. Preserve any
    # additional user-authored views; exact exporter-owned splitNN views are
    # reconciled separately by entity basename before this replacement.
    match = direct_matches[0]
    start, end = match.span('filename')
    return content[:start] + str(ply_filename) + content[end:]


def _append_direct_bone_volume_view(content, bone_name, ply_filename):
    """Append one direct VolumeView to an existing animation bone."""
    from .core import find_matching_brace

    filename = str(ply_filename)
    if not filename or any(char in filename for char in ('"', '\r', '\n')):
        raise ValueError('Invalid split VolumeView filename: ' + filename)
    header = re.compile(
        r'\{\s*bone(?:\s+[A-Za-z_][A-Za-z0-9_]*)*\s+"'
        + re.escape(str(bone_name)) + r'"', re.IGNORECASE)
    matches = list(header.finditer(content))
    if len(matches) != 1:
        raise RuntimeError(
            'Expected exactly one MDL split bone %r, found %d'
            % (bone_name, len(matches)))
    bone_start = matches[0].start()
    bone_end = find_matching_brace(content, bone_start)
    if bone_end < 0:
        raise RuntimeError('Unbalanced MDL split bone block: ' + str(bone_name))

    direct_views = 0
    first_direct_child = None
    view_pattern = re.compile(
        r'\{\s*VolumeView\s+"[^"]*"\s*\}', re.IGNORECASE)
    child_pattern = re.compile(
        r'\{\s*bone(?:\s+[A-Za-z_][A-Za-z0-9_]*)*\s+"',
        re.IGNORECASE)
    depth = 0
    in_string = False
    escaped = False
    index = bone_start
    while index <= bone_end:
        char = content[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == '{':
            if depth == 1:
                match = view_pattern.match(content, index)
                if match is not None and match.end() <= bone_end + 1:
                    direct_views += 1
                    index = match.end()
                    continue
                if (first_direct_child is None
                        and child_pattern.match(content, index) is not None):
                    first_direct_child = index
            depth += 1
        elif char == '}':
            depth -= 1
        index += 1
    if direct_views:
        raise RuntimeError(
            'Auto-split bone %r already owns %d direct VolumeView(s)'
            % (bone_name, direct_views))

    anchor = first_direct_child if first_direct_child is not None else bone_end
    line_start = content.rfind('\n', 0, anchor) + 1
    anchor_indent = content[line_start:anchor]
    if anchor_indent.strip():
        raise RuntimeError('Cannot determine MDL indentation for split bone: '
                           + str(bone_name))
    newline = '\r\n' if '\r\n' in content else '\n'
    child_indent = (anchor_indent if first_direct_child is not None
                    else anchor_indent + '\t')
    insertion = (child_indent + '{VolumeView "' + filename + '"}'
                 + newline)
    return content[:line_start] + insertion + content[line_start:]


def _direct_volume_view_matches(content, bone_name):
    """Return one bone span and its direct VolumeView matches."""
    from .core import find_matching_brace

    header = re.compile(
        r'\{\s*bone(?:\s+[A-Za-z_][A-Za-z0-9_]*)*\s+"'
        + re.escape(str(bone_name)) + r'"', re.IGNORECASE)
    headers = list(header.finditer(content))
    if len(headers) != 1:
        raise RuntimeError(
            'Expected exactly one MDL bone %r, found %d'
            % (bone_name, len(headers)))
    bone_start = headers[0].start()
    bone_end = find_matching_brace(content, bone_start)
    if bone_end < 0:
        raise RuntimeError('Unbalanced MDL bone block: ' + str(bone_name))

    view_pattern = re.compile(
        r'\{\s*VolumeView\s+"(?P<filename>[^"]*)"\s*\}',
        re.IGNORECASE)
    direct_views = []
    depth = 0
    in_string = False
    escaped = False
    index = bone_start
    while index <= bone_end:
        char = content[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == '{':
            if depth == 1:
                match = view_pattern.match(content, index)
                if match is not None and match.end() <= bone_end + 1:
                    direct_views.append(match)
                    index = match.end()
                    continue
            depth += 1
        elif char == '}':
            depth -= 1
        index += 1
    return bone_start, bone_end, direct_views


def _append_additional_direct_volume_view(content, bone_name, ply_filename):
    """Append another skinned PLY view to the existing mesh-parent bone."""
    filename = str(ply_filename)
    if not filename or any(char in filename for char in ('"', '\r', '\n')):
        raise ValueError('Invalid split VolumeView filename: ' + filename)
    _bone_start, _bone_end, direct_views = _direct_volume_view_matches(
        content, bone_name)
    if not direct_views:
        raise RuntimeError(
            'Expected a base VolumeView on MDL mesh parent %r, found none'
            % bone_name)
    if any(match.group('filename').casefold() == filename.casefold()
           for match in direct_views):
        raise RuntimeError('Split VolumeView already exists: ' + filename)

    # Append after the current final direct view so split01..splitNN retain their
    # deterministic file/draw order across repeated insertions.
    anchor = direct_views[-1]
    line_start = content.rfind('\n', 0, anchor.start()) + 1
    indent = content[line_start:anchor.start()]
    if indent.strip():
        raise RuntimeError(
            'Cannot determine mesh-parent VolumeView indentation: '
            + str(bone_name))
    newline = '\r\n' if '\r\n' in content else '\n'
    insertion = newline + indent + '{VolumeView "' + filename + '"}'
    return content[:anchor.end()] + insertion + content[anchor.end():]


def _append_skinned_split_carrier(content, mesh_parent_name, carrier_name,
                                   ply_filename):
    """Clone the main mesh-parent bone as a sibling skinned PLY carrier."""
    from .core import find_matching_brace

    source_name = str(mesh_parent_name)
    carrier_name = str(carrier_name)
    filename = str(ply_filename)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', carrier_name):
        raise ValueError('Invalid split carrier bone name: ' + carrier_name)
    if not filename or any(char in filename for char in ('"', '\r', '\n')):
        raise ValueError('Invalid split VolumeView filename: ' + filename)

    def bone_header(name):
        return re.compile(
            r'\{\s*bone(?:\s+[A-Za-z_][A-Za-z0-9_]*)*\s+"(?P<name>'
            + re.escape(name) + r')"', re.IGNORECASE)

    source_matches = list(bone_header(source_name).finditer(content))
    if len(source_matches) != 1:
        raise RuntimeError(
            'Expected exactly one MDL mesh-parent bone %r, found %d'
            % (source_name, len(source_matches)))
    if bone_header(carrier_name).search(content) is not None:
        raise RuntimeError('MDL split carrier already exists: ' + carrier_name)

    source_match = source_matches[0]
    source_start = source_match.start()
    source_end = find_matching_brace(content, source_start)
    if source_end < 0:
        raise RuntimeError('Unbalanced MDL mesh-parent block: ' + source_name)
    source_block = content[source_start:source_end + 1]
    relative_header_end = source_match.end() - source_start
    if re.search(
            r'\{\s*bone(?:\s+[A-Za-z_][A-Za-z0-9_]*)*\s+"',
            source_block[relative_header_end:], re.IGNORECASE):
        raise RuntimeError(
            'Cannot clone a mesh-parent carrier with child bones: '
            + source_name)

    name_start, name_end = source_match.span('name')
    name_start -= source_start
    name_end -= source_start
    carrier_block = (source_block[:name_start] + carrier_name
                     + source_block[name_end:])
    carrier_block = _replace_direct_bone_volume_view(
        carrier_block, carrier_name, filename)

    line_start = content.rfind('\n', 0, source_start) + 1
    indent = content[line_start:source_start]
    if indent.strip():
        raise RuntimeError(
            'Cannot determine MDL mesh-parent indentation: ' + source_name)
    newline = '\r\n' if '\r\n' in content else '\n'
    insertion = newline + indent + carrier_block
    return content[:source_end + 1] + insertion + content[source_end + 1:]


def _strip_generated_split_carrier(content, mesh_parent_name):
    """Remove an exact carrier clone made by the auto-split exporter."""
    from .core import find_matching_brace

    source_name = str(mesh_parent_name)
    carrier_name = source_name + '_split01'

    def bone_header(name):
        return re.compile(
            r'\{\s*bone(?:\s+[A-Za-z_][A-Za-z0-9_]*)*\s+"(?P<name>'
            + re.escape(name) + r')"', re.IGNORECASE)

    carrier_matches = list(bone_header(carrier_name).finditer(content))
    if not carrier_matches:
        return content
    if len(carrier_matches) != 1:
        raise RuntimeError(
            'Expected at most one generated split carrier %r, found %d'
            % (carrier_name, len(carrier_matches)))
    source_matches = list(bone_header(source_name).finditer(content))
    if len(source_matches) != 1:
        raise RuntimeError(
            'Cannot verify split carrier without one mesh-parent bone %r'
            % source_name)

    def bone_block(match):
        start = match.start()
        end = find_matching_brace(content, start)
        if end < 0:
            raise RuntimeError('Unbalanced MDL bone block: ' + match.group('name'))
        return start, end, content[start:end + 1]

    source_start, source_end, source_block = bone_block(source_matches[0])
    carrier_start, carrier_end, carrier_block = bone_block(carrier_matches[0])
    between = content[source_end + 1:carrier_start]
    if (source_start == carrier_start or source_end >= carrier_start
            or between.strip()):
        raise RuntimeError('Generated split carrier is not adjacent to the mesh parent')
    nested_bone = re.compile(
        r'\{\s*bone(?:\s+[A-Za-z_][A-Za-z0-9_]*)*\s+"',
        re.IGNORECASE)
    if (nested_bone.search(source_block[source_matches[0].end() - source_start:])
            or nested_bone.search(
                carrier_block[carrier_matches[0].end() - carrier_start:])):
        raise RuntimeError('Generated split carrier verification requires leaf bones')

    view_pattern = re.compile(
        r'(\{\s*VolumeView\s+")[^"]*("\s*\})', re.IGNORECASE)

    def normalize(block, name):
        header = bone_header(name)
        normalized, header_count = header.subn('{bone "__carrier__"', block,
                                               count=1)
        normalized, view_count = view_pattern.subn(
            r'\1__view__.ply\2', normalized)
        if header_count != 1 or view_count != 1:
            raise RuntimeError(
                'Generated split carrier verification requires one direct view')
        return normalized

    if normalize(source_block, source_name) != normalize(carrier_block, carrier_name):
        raise RuntimeError(
            'MDL bone %r exists but is not an exact generated carrier clone'
            % carrier_name)

    line_start = content.rfind('\n', 0, carrier_start) + 1
    if content[line_start:carrier_start].strip():
        raise RuntimeError(
            'Cannot determine generated split carrier indentation')
    remove_end = carrier_end + 1
    if content.startswith('\r\n', remove_end):
        remove_end += 2
    elif content.startswith('\n', remove_end):
        remove_end += 1
    return content[:line_start] + content[remove_end:]


def _strip_generated_direct_split_volume_view(
        content, mesh_parent_name, entity_name=None):
    """Remove only this entity's generated direct split PLY views."""
    if not entity_name:
        return content
    _bone_start, _bone_end, direct_views = _direct_volume_view_matches(
        content, mesh_parent_name)
    split_pattern = re.compile(
        re.escape(str(entity_name)) + r'_split\d+\.ply', re.IGNORECASE)
    generated = [
        match for match in direct_views
        if split_pattern.fullmatch(match.group('filename'))
    ]
    for match in reversed(generated):
        line_start = content.rfind('\n', 0, match.start()) + 1
        if content[line_start:match.start()].strip():
            raise RuntimeError(
                'Cannot determine generated direct split view indentation')
        remove_end = match.end()
        if content.startswith('\r\n', remove_end):
            remove_end += 2
        elif content.startswith('\n', remove_end):
            remove_end += 1
        content = content[:line_start] + content[remove_end:]
    return content


def _strip_generated_auto_split_attachments(
        content, mesh_parent_name, entity_name=None):
    """Remove only attachments that can be proven to be plug-in generated."""
    try:
        content = _strip_generated_split_carrier(content, mesh_parent_name)
    except RuntimeError as exc:
        # A similarly named user bone is not exporter-owned. Preserve it instead
        # of making an unrelated single-file export impossible.
        print('[auto-split] preserving unverified legacy carrier:', exc)
    return _strip_generated_direct_split_volume_view(
        content, mesh_parent_name, entity_name=entity_name)


_ACTIVE_ENTITY_EXPORTS = set()


def _missing_export_artifact_in_exception(exc, out_sub):
    """Return the missing generated path when an exception chain contains one."""
    output_key = os.path.normcase(os.path.abspath(out_sub))
    current = exc
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, FileNotFoundError) and current.filename:
            missing_path = os.path.abspath(str(current.filename))
            try:
                inside_output = os.path.commonpath(
                    (output_key, os.path.normcase(missing_path))) == output_key
            except ValueError:
                inside_output = False
            if inside_output:
                return missing_path
        current = current.__cause__ or current.__context__
    return None


def _export_all_once(mesh, tgt, output_dir=None, skin_name='skin',
                     texture_format='TGA', nvtt_path=None, toon_shader=True,
                     export_route=None, auto_split_over_limit=False,
                     split_record_limit=GAME_VERTEX_LIMIT,
                     split_plan_hint=None):
    """导出 PLY/MDL/MTL，并按用户选择生成 TGA 或 DDS 贴图。

    skin_name: 游戏 Entity 资源名；输出 <name>/<name>.def/.mdl/.ply。
    auto_split_over_limit: GOH/MOWAS2 按记录上限将冻结数据无损分区到多个蒙皮 PLY。
    split_record_limit: 每个 PLY 的用户自定义最终记录上限，硬上限为 65535。
    split_plan_hint: run_full 在同一调用中生成的冻结规划，避免重复遍历大网格。

    E6.29 抽为独立函数 —— 网格已完成绑定/可选减面后, 可不重跑全流程
    单独重做导出 (bind_and_transfer 不幂等: 重复调用会叠第二个 Armature
    修改器且按 PMX 组名读不到权重 → 已处理网格上严禁重跑 run_full)。
    """
    if not output_dir:
        output_dir = OUT_DEFAULT
    split_record_limit = int(split_record_limit)
    if split_record_limit < 3 or split_record_limit > GAME_VERTEX_LIMIT:
        raise ValueError('Split record limit must be between 3 and 65535')
    skin_name = _validate_entity_name(skin_name)
    texture_format = str(texture_format or 'TGA').upper()
    if texture_format not in {'TGA', 'DDS'}:
        raise ValueError(_("mowas2.err.texture_format_unsupported",
                           format=texture_format))
    target_mdl = str(tgt.get('gem2_mdl_path') or '')
    route = export_route or _infer_export_route(target_mdl)
    route = _validate_export_route_target(route, target_mdl)
    if route == 'MOWAS2' and toon_shader:
        print('[route:MOWAS2] ToonShader forced off')
        toon_shader = False
    multipart_enabled = bool(
        auto_split_over_limit and route in {'GOH', 'MOWAS2'})
    if auto_split_over_limit and not multipart_enabled:
        print('[auto-split] disabled for route %s: direct multipart skin views '
              'are verified only for GOH and MOWAS2' % route)
    if texture_format == 'DDS':
        _tool_kind, nvtt_path = _find_nvtt_tool(nvtt_path)
        print('[8.5] DDS tool:', _tool_kind, nvtt_path)
    if toon_shader:
        toon_root = _find_toon_shader_root()
        if toon_root is None:
            raise RuntimeError(_(
                "mowas2.err.toon_dependency_missing",
                workshop=_TOON_SHADER_WORKSHOP_ID))
        print('[toon] dependency root:', toon_root)
    # 保留清洗前的语义名：Unicode/符号可能正是 hitomi/sirome/Eyes+ 的
    # 分类依据，文件名清洗后不能再恢复。
    original_material_names = {}
    for mat in mesh.data.materials:
        if not mat:
            continue
        semantic = mat.get('mowas2_material_semantic_name')
        if not semantic:
            semantic = mat.name
            mat['mowas2_material_semantic_name'] = semantic
        original_material_names[mat.as_pointer()] = str(semantic)
    sanitize_materials()
    material_semantics = {
        mat.name: original_material_names.get(mat.as_pointer(), mat.name)
        for mat in mesh.data.materials if mat
    }
    hidden_mats = set()
    hidden_alpha = {}
    for mat in mesh.data.materials:
        alpha = _material_static_alpha(mat)
        if mat and alpha is not None and alpha <= 1e-4:
            hidden_mats.add(mat.name)
            hidden_alpha[mat.name] = alpha
    if hidden_mats:
        print('[materials] skip static alpha=0:', sorted(hidden_alpha.items()))
    # Preserve PMX double-sided intent. Closed eye/sclera layers retain their
    # dedicated single-sided contract unless the user explicitly overrides it.
    two_sided_mats = {
        mat.name for mat in mesh.data.materials
        if mat and _material_export_two_sided(
            mat, material_semantics.get(mat.name, mat.name))
    }
    if two_sided_mats:
        print('[materials] two-sided:', sorted(two_sided_mats))
    out_sub = _entity_output_dir(output_dir, skin_name)
    _assert_output_does_not_delete_source_textures(out_sub, mesh)
    os.makedirs(out_sub, exist_ok=True)
    # 材质/贴图沿用旧清理流程；旧 split PLY 延迟到新 PLY+MDL 全部验证并
    # 写成后再删，避免中途失败让旧 MDL 指向已被提前删除的拆件。
    stale_split_paths = []
    for _f in os.listdir(out_sub):
        stale_split_ply = re.fullmatch(
            re.escape(skin_name) + r'_split\d+\.ply', _f,
            re.IGNORECASE)
        cleanup_path = os.path.join(out_sub, _f)
        if stale_split_ply:
            stale_split_paths.append(cleanup_path)
            continue
        if _f.lower().endswith(('.mtl', '.tga', '.dds', '.png', '.bmp',
                                '.jpg', '.jpeg', '.tif', '.tiff', '.webp',
                                '.gif')):
            try:
                os.remove(cleanup_path)
            except OSError as exc:
                raise RuntimeError(_(
                    "mowas2.err.cleanup_failed", path=cleanup_path,
                    error=exc)) from exc
    # PLY 在贴图处理后写入：TGA/DDS 共用的材质级 plan 决定每个 MESH 的
    # MESH_FLAG_ALPHA。Eyes+/eyeblend 保留软 alpha；MMD 常量 alpha=0 的
    # EyeShadow 等默认隐藏层不写 MTL、MESH 或三角形。

    mdl_src = tgt.get('gem2_mdl_path')
    if not mdl_src or not os.path.isfile(mdl_src):
        # GOH 版: 兜底用插件 samples 内的 GFA 长臂模板 (goh_skin_gfa.mdl,
        # 前臂/手与 GOH 动画一致); 若连它都没有则明确报错。
        _plug = os.path.dirname(os.path.abspath(__file__))
        mdl_src = GOH_DEFAULT_MDL
        if not mdl_src or not os.path.isfile(mdl_src):
            raise RuntimeError(_("mowas2.err.target_mdl_missing"))
    with open(mdl_src, 'rb') as f:
        raw = f.read()
    try:
        mdl_content = raw.decode('gbk')
    except UnicodeDecodeError:
        mdl_content = raw.decode('utf-8', errors='replace')
    # Preserve the target MDL verbatim except for the exact mesh-parent view.
    # A global first-match regex can replace a rigid accessory or nested LODView
    # that happens to appear before the skinned human mesh.
    mesh_parent_name = str(tgt.get('gem2_mesh_parent') or '')
    if not mesh_parent_name:
        raise RuntimeError(_("mowas2.err.mesh_parent_matrix_missing"))
    mdl_content = _strip_generated_auto_split_attachments(
        mdl_content, mesh_parent_name, entity_name=skin_name)
    mdl_content = _replace_direct_bone_volume_view(
        mdl_content, mesh_parent_name, skin_name + '.ply')
    with open(os.path.join(out_sub, skin_name + '.def'), 'w', encoding='utf-8') as f:
        f.write('{game_entity\n\t{Extension "%s.mdl"}\n}\n' % skin_name)

    # 材质循环: 写初始 mtl (blend none 占位, [8.5] 按 plan 重写) + 拷贴图。
    # MMD 节点树常同时包含 diffuse/toon/sphere；必须选 Base Color 语义图，
    # 不能依赖 TEX_IMAGE 的迭代顺序。
    mat_diffuse = {}   # 材质名 -> diffuse 贴图名(去扩展名), 供 ply alpha_mats
    material_names_by_diffuse = {}
    texture_stager = TextureStager(out_sub)
    missing_diffuse_fallbacks = []
    for mat in mesh.data.materials:
        if not mat or mat.name in hidden_mats:
            continue
        try:
            texture_ref, missing_diffuse = _stage_pipeline_diffuse(
                texture_stager, mat)
        except Exception as exc:
            raise RuntimeError(
                'Failed to stage diffuse texture for %s: %s' %
                (mat.name, exc)) from exc
        if missing_diffuse is not None:
            missing_diffuse_fallbacks.append(missing_diffuse)
        diffuse = (texture_ref['stem']
                   if texture_ref is not None else mat.name)
        semantic_name = material_semantics.get(mat.name, mat.name)
        mat_diffuse[mat.name] = diffuse
        bucket = material_names_by_diffuse.setdefault(diffuse, [])
        bucket.append(semantic_name)
        with open(os.path.join(out_sub, mat.name + '.mtl'), 'w', encoding='utf-8') as f:
            f.write('{material simple\n\t{diffuse "%s"}\n\t{blend none}\n}\n' % (diffuse,))

    if missing_diffuse_fallbacks:
        print('[tex] legacy fallback for unavailable diffuse images:',
              missing_diffuse_fallbacks)
    print('[8] exported ->', out_sub)

    # 8.5 用户可选内置 TGA 或外部 NVTT DDS；两者复用完全相同的 alpha
    # 分类、黑底填充、MTL 重写和 PLY flag 判定。
    texture_alpha = {}
    try:
        mode = mesh.get('mowas2_source_mode') or detect_source_mode(tgt=tgt)
        if texture_format == 'DDS':
            plan = convert_textures_to_dds(
                out_sub, mode=mode, nvtt_path=nvtt_path,
                toon_shader=toon_shader,
                material_names_by_diffuse=material_names_by_diffuse,
                alpha_by_diffuse=texture_alpha)
        else:
            plan = convert_textures_to_tga(
                out_sub, mode=mode, toon_shader=toon_shader,
                material_names_by_diffuse=material_names_by_diffuse,
                alpha_by_diffuse=texture_alpha)
        print('[8.5] textures ->', texture_format, 'OK, plan:', len(plan),
              '| mode:', mode,
              '| alpha:', 'test' if _goh_alpha_test_mode() else 'blend-aware')
    except Exception as exc:
        # The operator boundary prints the final traceback once. Keeping this
        # layer quiet also prevents a recovered export retry from looking fatal.
        raise RuntimeError(_(
            "mowas2.err.texture_export_failed", format=texture_format,
            error=exc)) from exc

    # 贴图按所有可见 consumer 的最强 alpha 需求编码，再按材质细分 render
    # mode。默认隐藏材质已从 consumer 集合移除，因此 Eyes+/eyeblend 可以
    # 独立得到 DXT5 + pupil=blend。
    plan_folded = {key.casefold(): value for key, value in plan.items()}
    material_plan = {}
    for material_name, diffuse in mat_diffuse.items():
        texture_mode = plan.get(diffuse,
                                plan_folded.get(diffuse.casefold(), 'none'))
        semantic_name = material_semantics.get(material_name, material_name)
        has_alpha = bool(texture_alpha.get(
            diffuse, texture_alpha.get(diffuse.casefold(), False)))
        blend = _material_alpha_mode(semantic_name, diffuse, texture_mode,
                                     has_alpha=has_alpha)
        material_plan[material_name] = blend
        path = os.path.join(out_sub, material_name + '.mtl')
        if blend == 'test':
            content = ('{material simple\n\t{diffuse "%s"}'
                       '\n\t{alpharef 127}\n\t{blend test}'
                       '\n\t{alphatocoverage}\n}\n' % diffuse)
        else:
            content = ('{material simple\n\t{diffuse "%s"}'
                       '\n\t{blend %s}\n}\n' % (diffuse, blend))
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(content)
    print('[8.55] material alpha plan:', sorted(material_plan.items()))

    if toon_shader:
        apply_toon_shader_conversion(
            out_sub, material_semantics=material_semantics)
    else:
        print('[toon] disabled: keep material simple')
    _validate_eye_material_contract(
        out_sub, material_semantics, mat_diffuse, material_plan,
        two_sided_mats, hidden_mats)

    # PLY 写入: MESH_FLAG_ALPHA 与 mtl blend 同源 (plan)。
    # 2026-08-17 晚 (GOH 对照): 0x0002 只给 blend 半透明材质; test 镂空材质
    # 不带 (GOH 304 例 test 全为 0x0C15 无 0x0002)。
    # 同一贴图可被眼白/瞳孔等不同材质共享；PLY flag 必须用材质级 plan。
    alpha_mats = {name for name, blend in material_plan.items()
                  if blend == 'blend'}
    if plan:
        print('[8.6] alpha MESH (MESH_FLAG_ALPHA):', sorted(alpha_mats))
    split_plan = {
        'required': False, 'available': False, 'total_records': None,
    }
    prepared_parts = None
    part_filenames = [skin_name + '.ply']
    if multipart_enabled:
        mesh_data = mesh.data
        try:
            mesh_data_key = mesh_data.as_pointer()
        except (AttributeError, RuntimeError, ReferenceError):
            mesh_data_key = id(mesh_data)

        def _same_mesh_data(value):
            try:
                return value.as_pointer() == mesh_data_key
            except (AttributeError, RuntimeError, ReferenceError):
                return value is mesh_data

        reusable_hint = bool(
            split_plan_hint and split_plan_hint.get('available')
            and split_plan_hint.get('record_limit') == split_record_limit
            and split_plan_hint.get('attachment_bone') == mesh_parent_name
            and all(_same_mesh_data(part.get('mesh'))
                    for part in split_plan_hint.get('parts', ())))
        if reusable_hint:
            split_plan = split_plan_hint
            print('[auto-split] reusing frozen run_full plan:',
                  len(split_plan['parts']), 'parts')
        else:
            split_plan = _plan_game_auto_split(
                mesh, tgt, skip_mats=hidden_mats, log=True,
                mdl_content=mdl_content, entity_name=skin_name,
                record_limit=split_record_limit)
        if split_plan['required'] and not split_plan['available']:
            raise RuntimeError(_(
                "mowas2.err.auto_split_unavailable",
                vertices=split_plan['total_records']))
        if split_plan['available']:
            prepared_parts = split_plan['parts']
            part_filenames.extend(
                '%s_split%02d.ply' % (skin_name, index)
                for index in range(1, len(prepared_parts)))
            for split_filename in part_filenames[1:]:
                mdl_content = _append_additional_direct_volume_view(
                    mdl_content, mesh_parent_name, split_filename)

    written_parts = []
    payloads = (prepared_parts if prepared_parts is not None else [None])
    for part_index, (part_filename, payload) in enumerate(
            zip(part_filenames, payloads)):
        part_path = os.path.join(out_sub, part_filename)
        if payload is None:
            written = export_ply_game(
                part_path, mesh, tgt,
                alpha_mats=alpha_mats,
                two_sided_mats=two_sided_mats,
                skip_mats=hidden_mats)
        else:
            written = export_ply_game(
                part_path, mesh, tgt,
                alpha_mats=alpha_mats,
                two_sided_mats=two_sided_mats,
                prepared_data=payload)
        _validate_exported_ply_contract(
            part_path, out_sub, hidden_mats, alpha_mats, two_sided_mats,
            material_semantics, mat_diffuse, written)
        written_parts.append(written)
        if payload is not None:
            summary = split_plan['part_summaries'][part_index]
            if (written['vertices'] != summary['records']
                    or written['triangles'] != summary['triangles']):
                raise RuntimeError(
                    'Auto-split payload changed between planning and writing')

    if prepared_parts is not None:
        if sum(part['triangles'] for part in written_parts) != \
                split_plan['total_triangles']:
            raise RuntimeError(
                'Auto-split triangle total changed between planning and writing')
        print('[auto-split] wrote:', [
            '%s (%dv/%dt)' % (filename, result['vertices'],
                              result['triangles'])
            for filename, result in zip(part_filenames, written_parts)
        ])

    with open(os.path.join(out_sub, skin_name + '.mdl'), 'wb') as handle:
        handle.write(mdl_content.encode('gbk', errors='replace'))

    kept_split_paths = {
        os.path.normcase(os.path.abspath(os.path.join(out_sub, filename)))
        for filename in part_filenames[1:]
    }
    for stale_path in stale_split_paths:
        if os.path.normcase(os.path.abspath(stale_path)) in kept_split_paths:
            continue
        try:
            os.remove(stale_path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(_(
                "mowas2.err.cleanup_failed", path=stale_path,
                error=exc)) from exc

    print('=' * 60)
    return out_sub


def export_all(mesh, tgt, output_dir=None, skin_name='skin',
               texture_format='TGA', nvtt_path=None, toon_shader=True,
               export_route=None, auto_split_over_limit=False,
               split_record_limit=GAME_VERTEX_LIMIT,
               split_plan_hint=None):
    """Build one complete entity in staging, then commit it with rollback."""
    root = os.path.abspath(output_dir or OUT_DEFAULT)
    validated_name = _validate_entity_name(skin_name)
    out_sub = _entity_output_dir(root, validated_name)
    export_key = os.path.normcase(os.path.abspath(out_sub))
    if export_key in _ACTIVE_ENTITY_EXPORTS:
        raise RuntimeError(
            'Another export is already writing this entity directory: '
            + out_sub)
    _assert_output_does_not_delete_source_textures(out_sub, mesh)
    os.makedirs(os.path.dirname(root), exist_ok=True)
    _ACTIVE_ENTITY_EXPORTS.add(export_key)
    try:
        for attempt in range(2):
            stage_root = tempfile.mkdtemp(
                prefix='.%s_pipeline_stage_' % validated_name,
                dir=os.path.dirname(root))
            stage_sub = _entity_output_dir(stage_root, validated_name)
            try:
                _export_all_once(
                    mesh, tgt, output_dir=stage_root,
                    skin_name=validated_name,
                    texture_format=texture_format, nvtt_path=nvtt_path,
                    toon_shader=toon_shader, export_route=export_route,
                    auto_split_over_limit=auto_split_over_limit,
                    split_record_limit=split_record_limit,
                    split_plan_hint=split_plan_hint)

                manifest_name = '.gem2_export_manifest.json'
                owned_before = set()
                manifest_path = os.path.join(out_sub, manifest_name)
                try:
                    with open(manifest_path, 'r', encoding='utf-8') as handle:
                        manifest_data = json.load(handle)
                    if (not isinstance(manifest_data, dict)
                            or manifest_data.get('version') != 1):
                        raise ValueError('Unsupported GEM2 export manifest version')
                    for relative in manifest_data.get('files', ()):
                        if not isinstance(relative, str):
                            continue
                        normalized = os.path.normpath(relative)
                        if (os.path.isabs(normalized)
                                or normalized == os.pardir
                                or normalized.startswith(os.pardir + os.sep)):
                            continue
                        owned_before.add(os.path.normcase(normalized))
                except (OSError, TypeError, ValueError):
                    owned_before.clear()

                from .multipart_export import (
                    _commit_staged_files, _relative_files)
                generated_files = _relative_files(stage_sub)
                with open(os.path.join(stage_sub, manifest_name), 'w',
                          encoding='utf-8') as handle:
                    json.dump({'version': 1, 'files': generated_files}, handle,
                              ensure_ascii=True, indent=2)
                    handle.write('\n')

                split_pattern = re.compile(
                    re.escape(validated_name) + r'_split\d+\.ply$',
                    re.IGNORECASE)

                def stale_generated(relative):
                    if os.path.dirname(relative):
                        return (os.path.normcase(os.path.normpath(relative))
                                in owned_before)
                    filename = os.path.basename(relative)
                    return (os.path.normcase(os.path.normpath(relative))
                            in owned_before
                            or split_pattern.fullmatch(filename) is not None)

                _commit_staged_files(stage_sub, out_sub, stale_generated)
                return out_sub
            except Exception as exc:
                missing_path = _missing_export_artifact_in_exception(
                    exc, stage_sub)
                if attempt or not missing_path:
                    raise
                print('[export-retry] generated artifact disappeared during '
                      'export:', missing_path)
                print('[export-retry] rebuilding the complete entity once from '
                      'the original Blender texture sources')
            finally:
                shutil.rmtree(stage_root, ignore_errors=True)
    finally:
        _ACTIVE_ENTITY_EXPORTS.discard(export_key)


def run_full(output_dir=None, ground_z=GROUND_Z, protect_face=True,
             protect_tight=True, enable_decimate=False, skin_name='skin',
             pmx_path=None, texture_format='TGA', nvtt_path=None,
             toon_shader=True, export_route=None,
             auto_split_over_limit=False,
             split_record_limit=GAME_VERTEX_LIMIT):
    """完整移植 (对齐/绑定/减面可选/导出)。

    2026-08-17 晚: enable_decimate 默认 False —— 用户流程默认不减面
    (KK/pmx 顶点数通常 <65535, 直接导出; 减面是可选优化, 需手动开启)。
    skin_name: GOH Entity 资源名，输出 <name>/<name>.def/.mdl/.ply。
    auto_split_over_limit: GOH/MOWAS2 将最终记录无损分区到多个 PLY，优先于减面。
    split_record_limit: 每个 PLY 的自定义最终记录上限，范围 3..65535。
    """
    global _mowas2_settings_restore_depth
    if not output_dir:
        output_dir = OUT_DEFAULT
    split_record_limit = int(split_record_limit)
    if split_record_limit < 3 or split_record_limit > GAME_VERTEX_LIMIT:
        raise ValueError('Split record limit must be between 3 and 65535')
    skin_name = _validate_entity_name(skin_name)
    os.makedirs(output_dir, exist_ok=True)
    src, tgt, mesh, root = resolve_scene()
    _assert_legacy_pmx_scene(mesh, tgt)
    target_mdl = str(tgt.get('gem2_mdl_path') or '')
    effective_route = export_route or _infer_export_route(target_mdl)
    props = getattr(bpy.context.scene, 'mowas2_props', None)
    if effective_route in {'GOH', 'MOWAS2'}:
        # Route targets always use the short, bundled compatibility template.
        # Set the variant policy before repairing an older scene whose target
        # may still be the long-arm template, then validate the actual target.
        if props is not None:
            _mowas2_settings_restore_depth += 1
            try:
                props.goh_gfa_longarm = False
                if effective_route == 'MOWAS2':
                    props.toon_shader = False
            finally:
                _mowas2_settings_restore_depth -= 1
        src, tgt, mesh, root, _target_changed = _ensure_bundled_target_variant(
            src, tgt, mesh, root, pmx_path=pmx_path,
            force_short=(effective_route == 'GOH'))
    else:
        src, tgt, mesh, root, _target_changed = _ensure_bundled_target_variant(
            src, tgt, mesh, root, pmx_path=pmx_path)
    target_mdl = str(tgt.get('gem2_mdl_path') or '')
    effective_route = _validate_export_route_target(effective_route, target_mdl)
    multipart_enabled = bool(
        auto_split_over_limit and effective_route in {'GOH', 'MOWAS2'})
    if auto_split_over_limit and not multipart_enabled:
        print('[7] auto-split unavailable for route %s; decimation fallback '
              'remains route-safe' % effective_route)
    if effective_route == 'MOWAS2':
        toon_shader = False
    # 旧场景兼容：PMX 管线始终使用历史 frame-0 显示约定；若场景曾被
    # 临时 human-normalized 版本污染，从保留的 raw MDL 元数据恢复。
    if tgt is not None:
        _ensure_legacy_frame0_rest(tgt)
    print('=' * 60)
    print('GOH PMX→GEM2 pipeline v2 (compact PLY, optional safe split)')
    print('  src:', src.name, '| tgt:', tgt.name, '| mesh:', mesh.name)

    # 状态检测: mesh 是否已冻结 (freeze_mesh 后无 ARMATURE modifier / 已绑到 tgt)
    already_frozen = mesh.get('mowas2_frozen') or (
        not any(m.type == 'ARMATURE' for m in mesh.modifiers)
        and mesh.parent == tgt)
    # 冻结后修改脚部、肩宽、头颈或已应用的瞳孔选项，必须重新导入对齐，
    # 否则 UI 接受了新值但 frozen 几何不会恢复/更新。
    if already_frozen and _align_options_changed(mesh, src):
        if not pmx_path or not os.path.isfile(pmx_path):
            raise RuntimeError(_("mowas2.err.align_changed_requires_pmx"))
        print('[reimport] 冻结几何选项已修改, 重新导入并重新对齐')
        src, mesh, root = _reimport_source(pmx_path, tgt)
        already_frozen = False
    print('[pre] already_frozen:', already_frozen)

    if not already_frozen:
        # 0. snapshot
        snap = os.path.join(output_dir, 'mowas2_auto_backup.blend')
        try:
            bpy.ops.wm.save_as_mainfile(filepath=snap, copy=True)
            print('[0] snapshot:', snap)
        except Exception as e:
            print('[0] snapshot skipped:', e)

        # 1. rigids
        def _collect(o):
            out = []
            for c in o.children:
                out.extend(_collect(c))
            out.append(o)
            return out
        removed = 0
        for rn in ('rigidbodies', 'joints'):
            r = bpy.data.objects.get(rn)
            if r:
                for o in _collect(r):
                    try:
                        bpy.data.objects.remove(o, do_unlink=True)
                        removed += 1
                    except Exception:
                        pass
        print('[1] rigids removed:', removed)

        # 1.5 清理: 删除与 mesh 共享 data 的重复对象。
        #     KK 类模型 mmd_tools 导入可能产生 100+ 个共享同一 mesh data 的对象
        #     (千咲 users=149)。Shape Key 不能在这里提前清除：freeze_mesh 会先
        #     读取 evaluated 顶点再清键，从而烘焙当前眼部/表情形态。
        for o in list(bpy.data.objects):
            if o is mesh and o.type == 'MESH':
                continue
            if o.type == 'MESH' and o.data == mesh.data:
                bpy.data.objects.remove(o, do_unlink=True)
        print('[1.5] mesh data users after cleanup:', mesh.data.users)

        # 2. mirror detect (标准 MMD 源跳过: 见 align_only 注释)
        mode = detect_source_mode(tgt=tgt, src=src)
        if mode == 'mmd':
            mirrored = False
            print('[2] MMD 源 (%s): 跳过镜像判定, mirrored=False' % src.name)
        else:
            mirrored = is_source_mirrored(src, tgt)
            print('[2] mirrored:', mirrored)

        # 3. Umeyama rigid fit + ground
        align_rigid(src, tgt, mesh, root, mirrored, ground_z)
        print('[3] rigid fit done')

        # 3.25 GOH v12: GFA 骨骼级躯干链对齐 (freeze 前, 网格随骨架形变)
        #     全盘 GFA 思路: 骨架缩放/偏移 + 网格自动跟随 → 无顶点压缩/撕裂。
        if _goh_hand_split():
            n_gfa = goh_gfa_bone_align(src, tgt, mesh, source_mode=mode)
            print('[3.25] GFA bone align moved:', n_gfa)

        # 4. arm pose (T-pose -> down)
        # 2026-08-17 晚: 帧0 分支 —— 标准 MMD (A-pose/垂手) 先抬到 T-pose
        # 再走现有 align_arms, 避免非 T-pose 起始导致手陷身体/手扭。
        # (与步骤3 align_only 共用 align_arms_auto)
        pose, ang = align_arms_auto(src, tgt, mirrored, source_mode=mode)
        print('[4] arms posed (pose=%s, %.1f°)' % (pose, ang))
        print('[4.5] hands posed')

        # 5. freeze
        freeze_mesh(mesh)
        forearm_result = goh_retarget_mmd_forearm_geometry(
            mesh, src, tgt, mirrored=mirrored, source_mode=mode)
        if forearm_result.get('changed'):
            print('[5.1] MMD forearm/wrist geometry:', forearm_result['changed'])
        # GFA 后任一源分支都可能改变脚底；归一化只读取源 ankle 权重，
        # 不改变 KK/MMD 的骨骼对齐分支。
        normalize_foot_geometry(mesh, src, tgt, ground_z)
        mesh['mowas2_frozen'] = True
        mesh['mowas2_mirrored'] = mirrored
        mesh['mowas2_source_mode'] = mode
        print('[5] frozen')

    else:
        # 复用首次对齐判定的镜像方向（冻结时已写入 mesh 属性）
        mirrored = bool(mesh.get('mowas2_mirrored'))
        print('[skip] align already done, mirrored:', mirrored)

    # 冻结几何修正放在分支外：v133 对齐快照可即时重算身形、头颈和瞳孔。
    # 旧 v132 身形必须由 _align_options_changed 从 PMX 干净重建。
    body_result = goh_adjust_body_curve_geometry(
        mesh, src, tgt, mirrored=mirrored)
    if body_result.get('changed'):
        print('[5.2] body curve / foot1 spacing:', body_result['changed'])
    arm_inset_result = goh_adjust_arm_inset_geometry(
        mesh, src, tgt, mirrored=mirrored)
    if arm_inset_result.get('changed'):
        print('[5.3] rigid whole-arm inset:', arm_inset_result['changed'])
    neck_result = goh_scale_neck_follow_geometry(mesh, src)
    if neck_result.get('changed'):
        print('[5.06] neck/accessory follow:', neck_result['changed'])
    pupil_result = goh_fix_pupil_depth_geometry(mesh, src)
    if pupil_result.get('changed'):
        print('[5.07] pupil depth geometry:', pupil_result['changed'])

    # 6. casting + bind + transfer
    # run_full 可在 align_only 或上一次完整导出后再次执行；绑定完成后
    # 权重组已是目标名，重复 casting/transfer 会叠加 Armature modifier。
    vg_names = [vg.name for vg in mesh.vertex_groups]
    target_mods = [m for m in mesh.modifiers
                   if m.type == 'ARMATURE' and m.object == tgt]
    already_bound = (mesh.parent is tgt
                     and vg_names == TGT_VG_ORDER
                     and len(vg_names) == len(TGT_VG_ORDER)
                     and bool(target_mods))
    if already_bound:
        for extra in target_mods[1:]:
            mesh.modifiers.remove(extra)
        merged = 0
        print('[6] already bound: skip casting/transfer')
    else:
        merged = bone_casting(mesh, src)
        for key in ('mowas2_wrist_geometry_version',
                    'mowas2_wrist_geometry_count',
                    'mowas2_wrist_geometry_delta',
                    'mowas2_wrist_stitch_version',
                    'mowas2_wrist_stitch_count'):
            if key in mesh:
                del mesh[key]
        bind_and_transfer(mesh, tgt, mirrored)
        print('[6] casting', merged, '| bound & transferred')

    wrist_recovered = goh_recover_wrist_anchor(
        mesh, src, tgt, mirrored=mirrored)
    if wrist_recovered:
        print('[6.4] wrist anchor geometry restored:', wrist_recovered)
    wrist_weights_cleaned = goh_stitch_wrist_to_handrot(mesh, tgt)
    if wrist_weights_cleaned:
        print('[6.5] legacy wrist weights restored:', wrist_weights_cleaned)

    # 7. GOH 自动拆分优先于减面。规划器冻结最终记录后只重映射各 PLY 的
    # 局部 u16 索引；多骨/双权重以及跨文件材质都保持字节等价。其他路由仍
    # 使用单 PLY，并在已勾选时由减面接管。
    preview_hidden_mats = set()
    for material in mesh.data.materials:
        if not material:
            continue
        alpha = _material_static_alpha(material)
        if alpha is not None and alpha <= 1e-4:
            preview_hidden_mats.add(material.name)
    split_preview = None
    if multipart_enabled:
        split_preview = _plan_game_auto_split(
            mesh, tgt, skip_mats=preview_hidden_mats, log=True,
            entity_name=skin_name, record_limit=split_record_limit)
    split_will_handle_limit = bool(
        split_preview and split_preview['required']
        and split_preview['available'])
    if split_will_handle_limit:
        nf = len(mesh.data.polygons)
        nv = split_preview['total_records']
        print('[7] decimation bypassed: auto-split will preserve all', nf,
              'faces | records:', nv)
    elif enable_decimate:
        # Reduce only as far as the real compact-record u16 limit requires.
        nf = decimate_global(
            mesh, target_faces=len(mesh.data.polygons), arm=tgt,
            protect_face=protect_face, protect_tight=protect_tight,
            skip_mats=preview_hidden_mats)
        nv = _indexed_export_vertex_count(
            mesh, tgt, skip_mats=preview_hidden_mats)
        print('[7] decimated:', nf, 'faces | indexed records:', nv,
              '(limit %d)' % GAME_VERTEX_LIMIT)
        if nv > GAME_VERTEX_LIMIT:
            raise RuntimeError(_(
                "mowas2.err.decimated_vertex_limit", vertices=nv))
    else:
        nf = len(mesh.data.polygons)
        if split_preview is not None:
            nv = split_preview['total_records']
            print('[7] decimation skipped:', nf,
                  'faces | indexed records:', nv,
                  '| exporter limit:', GAME_VERTEX_LIMIT)
        else:
            nv = len(mesh.data.vertices)
            print('[7] decimation skipped:', nf, 'faces | raw verts:', nv,
                  '| indexed exporter will enforce the',
                  GAME_VERTEX_LIMIT, 'record limit')

    # 8.5 export (E6.29 抽为 export_all, 便于不重跑绑定/减面单独重做导出)
    return export_all(
        mesh, tgt, output_dir, skin_name=skin_name,
        texture_format=texture_format, nvtt_path=nvtt_path,
        toon_shader=toon_shader, export_route=effective_route,
        auto_split_over_limit=auto_split_over_limit,
        split_record_limit=split_record_limit,
        split_plan_hint=(split_preview if split_will_handle_limit else None))


class MOWAS2_OT_AutoPipeline(bpy.types.Operator):
    bl_idname = "gem2.mowas2_auto_pipeline"
    bl_label = _("mowas2.op.pipeline.label")
    bl_description = _("mowas2.op.pipeline.desc")
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        _restore_mowas2_settings(context.scene, force=False)
        _apply_export_route(context.scene.mowas2_props)
        return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        _draw_pipeline_confirmation(self.layout, context.scene.mowas2_props)

    def execute(self, context):
        try:
            _restore_mowas2_settings(context.scene, force=False)
            props = getattr(context.scene, 'mowas2_props', None)
            if props is None:
                out = run_full()
            else:
                _apply_export_route(props)
                out = run_full(
                    props.output_dir, props.ground_z,
                    protect_face=props.protect_face,
                    protect_tight=props.protect_tight,
                    enable_decimate=props.enable_decimate,
                    skin_name=props.skin_name or 'skin',
                    pmx_path=props.pmx_path,
                    texture_format=props.texture_format,
                    nvtt_path=props.nvtt_path or None,
                    toon_shader=props.toon_shader,
                    export_route=props.export_route,
                    auto_split_over_limit=props.auto_split_over_limit,
                    split_record_limit=props.split_record_limit)
            self.report({'INFO'}, _("mowas2.info.pipeline_done", dir=out))
            return {'FINISHED'}
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, _("mowas2.err.pipeline_failed", error=e))
            return {'CANCELLED'}


# ═══════════════════════════════════════════════════════════════
#  MOWAS2 自动移植面板（面向普通用户：点几下按钮完成导模）
# ═══════════════════════════════════════════════════════════════

_MOWAS2_SETTINGS_ID = 'mowas2'
_MOWAS2_PERSISTED_PROPS = (
    'pmx_path', 'mdl_path', 'output_dir', 'export_route',
    'mmd_import_preset', 'texture_format', 'nvtt_path',
    'toon_shader', 'ground_z', 'protect_face', 'protect_tight',
    'enable_decimate', 'auto_split_over_limit', 'split_record_limit',
    'skin_name',
    'goh_hand_split', 'goh_gfa_longarm',
    'goh_enlarge_head', 'goh_head_scale', 'goh_head_neck_follow',
    'goh_alpha_test', 'goh_fix_pupil_depth', 'goh_pupil_clearance',
    'goh_ik_updown_scale', 'goh_ik_updown_multiplier', 'goh_foot_scale',
    'goh_foot1_spacing', 'goh_arm_span_scale', 'goh_shoulder_scale',
    'goh_torso_ik_merge',
    'goh_iklr_keep',
    'goh_hand_clamp', 'goh_wrist_stitch', 'goh_finger_curl',
)
_MOWAS2_PRESET_PROPS = tuple(
    name for name in _MOWAS2_PERSISTED_PROPS
    if name not in {'pmx_path', 'output_dir', 'skin_name'}
)
_mowas2_settings_restore_depth = 0
_named_preset_item_cache = []


def _infer_export_route(mdl_path):
    if not mdl_path:
        return 'CUSTOM'
    current = os.path.normcase(os.path.abspath(str(mdl_path)))
    if current == os.path.normcase(os.path.abspath(MOWAS2_ROUTE_MDL)):
        return 'MOWAS2'
    if current == os.path.normcase(os.path.abspath(GOH_ROUTE_MDL)):
        return 'GOH'
    return 'CUSTOM'


def _apply_export_route(props):
    global _mowas2_settings_restore_depth
    route = str(getattr(props, 'export_route', 'CUSTOM'))
    _mowas2_settings_restore_depth += 1
    try:
        if route == 'MOWAS2':
            props.mdl_path = MOWAS2_ROUTE_MDL
            props.goh_gfa_longarm = False
            props.toon_shader = False
        elif route == 'GOH':
            props.mdl_path = GOH_ROUTE_MDL
            props.goh_gfa_longarm = False
    finally:
        _mowas2_settings_restore_depth -= 1


def _mowas2_route_updated(props, context):
    _apply_export_route(props)
    _persist_mowas2_settings(props)


def _mowas2_toon_updated(props, context):
    global _mowas2_settings_restore_depth
    if getattr(props, 'export_route', 'CUSTOM') == 'MOWAS2' \
            and bool(props.toon_shader):
        _mowas2_settings_restore_depth += 1
        try:
            props.toon_shader = False
        finally:
            _mowas2_settings_restore_depth -= 1
    _persist_mowas2_settings(props)


def _named_export_preset_items(_self, _context):
    global _named_preset_item_cache
    from .core import get_export_presets
    values = get_export_presets()
    items = [('__NONE__', 'Select a named preset', '')]
    items.extend((name, name, '') for name in sorted(values, key=str.casefold))
    _named_preset_item_cache = items
    return _named_preset_item_cache


def _capture_named_export_preset(props):
    values = {name: getattr(props, name) for name in _MOWAS2_PRESET_PROPS}
    scene = getattr(props, 'id_data', bpy.context.scene)
    snapshot = _effective_mmd_import_snapshot(scene, props)
    values['mmd_import_settings'] = snapshot['settings']
    values['mmd_import_preset_path'] = snapshot['preset_path']
    values['mmd_import_preset_sha256'] = snapshot['preset_sha256']
    values['saved_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if values.get('export_route') == 'MOWAS2':
        values['toon_shader'] = False
    return values


_MOWAS2_CONFIRM_FIELDS = (
    ('Remember settings', 'remember_settings'),
    ('Route', 'export_route'), ('MMD preset', 'mmd_import_preset'),
    ('Texture format', 'texture_format'), ('NVTT path', 'nvtt_path'),
    ('ToonShader', 'toon_shader'),
    ('Ground Z', 'ground_z'), ('Decimate', 'enable_decimate'),
    ('Auto split records', 'auto_split_over_limit'),
    ('Records per PLY', 'split_record_limit'),
    ('Protect face', 'protect_face'), ('Protect tight areas', 'protect_tight'),
    ('GOH split hands', 'goh_hand_split'),
    ('GOH GFA long arm', 'goh_gfa_longarm'),
    ('Enlarge head', 'goh_enlarge_head'), ('Head scale', 'goh_head_scale'),
    ('Head/neck follow', 'goh_head_neck_follow'),
    ('Alpha test', 'goh_alpha_test'),
    ('Fix pupil depth', 'goh_fix_pupil_depth'),
    ('Pupil clearance', 'goh_pupil_clearance'),
    ('IK up/down scale', 'goh_ik_updown_scale'),
    ('IK up/down multiplier', 'goh_ik_updown_multiplier'),
    ('Foot scale', 'goh_foot_scale'),
    ('Foot spacing', 'goh_foot1_spacing'),
    ('Arm span scale', 'goh_arm_span_scale'),
    ('Legacy shoulder scale', 'goh_shoulder_scale'),
    ('Torso IK merge', 'goh_torso_ik_merge'),
    ('IK L/R keep', 'goh_iklr_keep'),
    ('Hand clamp', 'goh_hand_clamp'),
    ('Wrist stitch', 'goh_wrist_stitch'),
    ('Finger curl', 'goh_finger_curl'),
)


def _setting_summary_value(value):
    if type(value) is bool:
        return '✓' if value else '✗'
    if isinstance(value, float):
        return '%.6g' % value
    return str(value)


def _draw_pipeline_confirmation(layout, props):
    layout.label(text='Confirm the remembered settings before running',
                 icon='QUESTION')
    paths = layout.box()
    paths.label(text='PMX: ' + str(props.pmx_path or '(scene source)'))
    paths.label(text='Target: ' + str(props.mdl_path))
    paths.label(text='Entity: ' + str(props.skin_name or 'skin'))
    paths.label(text='Output: ' + str(props.output_dir))
    flow = layout.column_flow(columns=2, align=True)
    for label, field in _MOWAS2_CONFIRM_FIELDS:
        value = getattr(props, field)
        icon = 'CHECKMARK' if value is True else ('X' if value is False else 'DOT')
        flow.label(text='%s: %s' % (label, _setting_summary_value(value)),
                   icon=icon)
    try:
        snapshot = _effective_mmd_import_snapshot(bpy.context.scene, props)
        mmd_settings = snapshot['settings']
        mmd_box = layout.box()
        mmd_box.label(text='MMD Tools immutable import snapshot', icon='IMPORT')
        mmd_box.label(text='Preset: ' + snapshot['preset'])
        mmd_box.label(text='Source: ' + (snapshot['preset_path'] or '(built-in/captured)'))
        mmd_box.label(text='SHA-256: ' + (snapshot['preset_sha256'] or '(built-in)'))
        mmd_flow = mmd_box.column_flow(columns=2, align=True)
        for field in sorted(mmd_settings, key=str.casefold):
            value = mmd_settings[field]
            icon = 'CHECKMARK' if value is True else ('X' if value is False else 'DOT')
            mmd_flow.label(
                text='%s: %s' % (field, _setting_summary_value(value)), icon=icon)
    except Exception as exc:
        layout.label(text='MMD preset unavailable: ' + str(exc), icon='ERROR')
    if props.export_route == 'MOWAS2':
        layout.label(text='MOWAS2 invariant: ToonShader forced ✗', icon='LOCKED')


def _persist_mowas2_settings(props):
    if _mowas2_settings_restore_depth:
        return
    from .core import set_export_dir, set_panel_settings
    remember = bool(getattr(props, 'remember_settings', True))
    values = {'remember_settings': remember}
    if remember:
        for name in _MOWAS2_PERSISTED_PROPS:
            values[name] = getattr(props, name)
    # Defer the write: set_panel_settings atomically persists panel + path once.
    set_export_dir(values.get('output_dir') or '', save=False)
    set_panel_settings(_MOWAS2_SETTINGS_ID, values)


def _mowas2_setting_updated(props, context):
    _persist_mowas2_settings(props)


def _mowas2_mmd_preset_updated(props, context):
    if not _mowas2_settings_restore_depth and context is not None:
        try:
            snapshot = mmd_import_preset_snapshot(props.mmd_import_preset)
            _store_mmd_import_snapshot(context.scene, snapshot)
        except Exception as exc:
            for key in (_MMD_SCENE_PRESET_KEY, _MMD_SCENE_SETTINGS_KEY,
                        _MMD_SCENE_PATH_KEY, _MMD_SCENE_SHA256_KEY):
                try:
                    del context.scene[key]
                except KeyError:
                    pass
            print('[MOWAS2] MMD preset snapshot failed:', exc)
    _persist_mowas2_settings(props)


def _preview_aligned_geometry(props, context, kind):
    _persist_mowas2_settings(props)
    if _mowas2_settings_restore_depth or context is None:
        return
    try:
        src, tgt, mesh, _root = resolve_scene()
        if not bool(mesh.get('mowas2_frozen')):
            return
        if kind in {'shoulder', 'arm', 'neck'} and mesh.parent is tgt:
            print('[preview:%s] skipped: reopen the aligned snapshot before binding'
                  % kind)
            return
        if kind == 'pupil':
            result = goh_fix_pupil_depth_geometry(mesh, src, force=True)
        elif kind == 'shoulder':
            result = goh_adjust_body_curve_geometry(
                mesh, src, tgt, mirrored=bool(mesh.get('mowas2_mirrored')),
                force=True)
        elif kind == 'arm':
            result = goh_adjust_arm_inset_geometry(
                mesh, src, tgt, mirrored=bool(mesh.get('mowas2_mirrored')),
                force=True)
        else:
            result = goh_scale_neck_follow_geometry(mesh, src, force=True)
        print('[preview:%s] %s' % (kind, result))
    except Exception as exc:
        # Property updates also occur in unrelated/imported PLY scenes. Those do
        # not have the PMX source rig needed for alignment preview.
        print('[preview:%s] skipped: %s' % (kind, exc))


def _mowas2_pupil_preview_updated(props, context):
    _preview_aligned_geometry(props, context, 'pupil')


def _mowas2_neck_preview_updated(props, context):
    _preview_aligned_geometry(props, context, 'neck')


def _mowas2_body_preview_updated(props, context):
    _preview_aligned_geometry(props, context, 'shoulder')


def _mowas2_arm_preview_updated(props, context):
    _preview_aligned_geometry(props, context, 'arm')


def _mowas2_shoulder_preview_updated(props, context):
    # Legacy hidden property: retain persistence without reapplying v3 geometry.
    _persist_mowas2_settings(props)


def _restore_mowas2_settings(scene, force=False):
    global _mowas2_settings_restore_depth
    props = getattr(scene, 'mowas2_props', None)
    if props is None:
        return
    if not force and props.settings_initialized:
        return
    from .core import get_panel_settings, get_paths
    saved = get_panel_settings(_MOWAS2_SETTINGS_ID)
    saved_remember = saved.get('remember_settings', True)
    remember = saved_remember if isinstance(saved_remember, bool) else True
    _mowas2_settings_restore_depth += 1
    try:
        props.remember_settings = remember
        if remember:
            for name in _MOWAS2_PERSISTED_PROPS:
                if name not in saved:
                    continue
                try:
                    setattr(props, name, saved[name])
                except (AttributeError, TypeError, ValueError):
                    print('[MOWAS2] ignored invalid saved setting %s=%r'
                          % (name, saved[name]))
            if 'output_dir' not in saved:
                previous_export = get_paths().get('export') or ''
                if previous_export:
                    props.output_dir = previous_export
            if 'export_route' not in saved:
                props.export_route = _infer_export_route(
                    saved.get('mdl_path', props.mdl_path))
            _apply_export_route(props)
        props.settings_initialized = True
    finally:
        _mowas2_settings_restore_depth -= 1
    _effective_mmd_import_snapshot(scene, props)


def _restore_all_mowas2_settings(force=False):
    for scene in bpy.data.scenes:
        _restore_mowas2_settings(scene, force=force)


@persistent
def _mowas2_load_post(_dummy):
    _restore_all_mowas2_settings(force=True)


@persistent
def _mowas2_blend_import_post(_context):
    _restore_all_mowas2_settings(force=False)


def _remove_mowas2_settings_handlers():
    for handlers, function_name in (
            (bpy.app.handlers.load_post, '_mowas2_load_post'),
            (bpy.app.handlers.blend_import_post, '_mowas2_blend_import_post')):
        for callback in list(handlers):
            if (getattr(callback, '__module__', None) == __name__ and
                    getattr(callback, '__name__', None) == function_name):
                handlers.remove(callback)


def _register_mowas2_settings_handlers():
    _remove_mowas2_settings_handlers()
    bpy.app.handlers.load_post.append(_mowas2_load_post)
    bpy.app.handlers.blend_import_post.append(_mowas2_blend_import_post)


class MOWAS2_SceneProps(bpy.types.PropertyGroup):
    settings_initialized: bpy.props.BoolProperty(
        default=False, options={'HIDDEN', 'SKIP_SAVE'})
    remember_settings: bpy.props.BoolProperty(
        name=_("mowas2.prop.remember_settings"),
        description=_("mowas2.prop.remember_settings.desc"),
        default=True, update=_mowas2_setting_updated)
    export_route: bpy.props.EnumProperty(
        name="Export Route",
        description="Choose the reference skeleton and route invariants",
        items=(
            ('GOH', 'GOH', 'Use samples/goh_skin.mdl'),
            ('MOWAS2', 'MOWAS2', 'Use samples/MOWAS2.mdl and disable ToonShader'),
            ('CUSTOM', 'Custom MDL', 'Keep the selected target MDL'),
        ),
        default='GOH', update=_mowas2_route_updated)
    mmd_import_preset: bpy.props.EnumProperty(
        name="MMD Import Preset",
        description="MMD Tools operator preset used for initial import and reimport",
        items=_mmd_import_preset_items,
        update=_mowas2_mmd_preset_updated)
    named_export_preset: bpy.props.EnumProperty(
        name="Named Export Preset",
        description="Saved export settings (input, entity and output paths are excluded)",
        items=_named_export_preset_items,
        options={'SKIP_SAVE'})
    pmx_path: bpy.props.StringProperty(
        name=_("mowas2.prop.pmx"), subtype='FILE_PATH',
        description=_("mowas2.prop.pmx.desc"),
        default="", update=_mowas2_setting_updated)
    mdl_path: bpy.props.StringProperty(
        name=_("mowas2.prop.mdl"), subtype='FILE_PATH',
        description=_("mowas2.prop.mdl.desc"),
        # Route selection owns the bundled GOH/MOWAS2 targets; Custom keeps any
        # explicitly selected compatible skin MDL.
        default=GOH_ROUTE_MDL,
        update=_mowas2_setting_updated)
    output_dir: bpy.props.StringProperty(
        name=_("mowas2.prop.output_dir"), subtype='DIR_PATH',
        description=_("mowas2.prop.output_dir.desc"),
        default=OUT_DEFAULT, update=_mowas2_setting_updated)
    texture_format: bpy.props.EnumProperty(
        name=_("mowas2.prop.texture_format"),
        description=_("mowas2.prop.texture_format.desc"),
        items=(
            ('TGA', _("mowas2.prop.texture_format.tga"),
             _("mowas2.prop.texture_format.tga.desc")),
            ('DDS', _("mowas2.prop.texture_format.dds"),
             _("mowas2.prop.texture_format.dds.desc")),
        ),
        default='TGA', update=_mowas2_setting_updated)
    nvtt_path: bpy.props.StringProperty(
        name=_("mowas2.prop.nvtt_path"),
        subtype='FILE_PATH',
        description=_("mowas2.prop.nvtt_path.desc"),
        default='', update=_mowas2_setting_updated)
    toon_shader: bpy.props.BoolProperty(
        name=_("mowas2.prop.toon_shader"),
        description=_("mowas2.prop.toon_shader.desc"),
        default=True, update=_mowas2_toon_updated)
    ground_z: bpy.props.FloatProperty(
        name=_("mowas2.prop.ground_z"), default=GROUND_Z,
        description=_("mowas2.prop.ground_z.desc"),
        update=_mowas2_setting_updated)
    # E6.19: 减面保护开关 (仅当减面开启时生效)。
    protect_face: bpy.props.BoolProperty(
        name=_("mowas2.prop.protect_face"), default=True,
        description=_("mowas2.prop.protect_face.desc"),
        update=_mowas2_setting_updated)
    protect_tight: bpy.props.BoolProperty(
        name=_("mowas2.prop.protect_tight"), default=True,
        description=_("mowas2.prop.protect_tight.desc"),
        update=_mowas2_setting_updated)
    # 2026-08-17 晚: 减面默认关闭 (用户流程: 导入->对齐->一键 默认不减面;
    # 面数无上限, 只须顶点 ≤65535 —— KK 系 5 万出头顶点直接可导)。
    # 需要减面时手动勾选。
    enable_decimate: bpy.props.BoolProperty(
        name=_("mowas2.prop.enable_decimate"), default=False,
        description=_("mowas2.prop.enable_decimate.desc"),
        update=_mowas2_setting_updated)
    auto_split_over_limit: bpy.props.BoolProperty(
        name=_("mowas2.prop.auto_split_over_limit"), default=False,
        description=_("mowas2.prop.auto_split_over_limit.desc"),
        update=_mowas2_setting_updated)
    split_record_limit: bpy.props.IntProperty(
        name=_("mowas2.prop.split_record_limit"),
        description=_("mowas2.prop.split_record_limit.desc"),
        default=GAME_VERTEX_LIMIT, min=3, max=GAME_VERTEX_LIMIT,
        update=_mowas2_setting_updated)
    # Backward-compatible property name; exposed as the game's Entity resource id.
    skin_name: bpy.props.StringProperty(
        name=_("mowas2.prop.skin_name"),
        description=_("mowas2.prop.skin_name.desc"),
        default='skin', maxlen=64, update=_mowas2_setting_updated)
    # GOH 版 (2026-08-18): 手部权重模式。True=GFA 分指 (palm1/palm2/palm3,
    # 匹配 GOH 原版皮肤 agit_yelan, 手部 IK/FK 对得上); False=MOWAS2 单骨。
    goh_hand_split: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_hand_split"),
        description=_("mowas2.prop.goh_hand_split.desc"),
        default=GOH_HAND_SPLIT, update=_mowas2_setting_updated)
    # v10 (2026-08-18): 长臂模板 + 源骨骼分段长度缩放。
    goh_gfa_longarm: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_gfa_longarm"),
        description=_("mowas2.prop.goh_gfa_longarm.desc"),
        default=GOH_GFA_LONGARM, update=_mowas2_setting_updated)
    # 可选的头部比例修正：默认关闭，避免改变已验证的旧导出结果。
    goh_enlarge_head: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_enlarge_head"),
        description=_("mowas2.prop.goh_enlarge_head.desc"),
        default=False, update=_mowas2_setting_updated)
    goh_head_scale: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_head_scale"),
        description=_("mowas2.prop.goh_head_scale.desc"),
        default=1.06, min=0.9, max=1.5, soft_min=1.0, soft_max=1.3,
        update=_mowas2_setting_updated)
    goh_head_neck_follow: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_head_neck_follow"),
        description=_("mowas2.prop.goh_head_neck_follow.desc"),
        default=0.0, min=0.0, max=1.0, soft_min=0.0, soft_max=1.0,
        subtype='FACTOR', update=_mowas2_neck_preview_updated)
    # GOH 原生透明材质默认 alpharef 127 + blend test；关闭后恢复
    # 旧版 blend 分类，便于用户在同一模型上做 A/B 对照。
    goh_alpha_test: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_alpha_test"),
        description=_("mowas2.prop.goh_alpha_test.desc"),
        default=GOH_ALPHA_TEST_TRANSPARENT,
        update=_mowas2_setting_updated)
    goh_fix_pupil_depth: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_fix_pupil_depth"),
        description=_("mowas2.prop.goh_fix_pupil_depth.desc"),
        default=True, update=_mowas2_pupil_preview_updated)
    goh_pupil_clearance: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_pupil_clearance"),
        description=_("mowas2.prop.goh_pupil_clearance.desc"),
        default=0.006, min=0.0, max=0.1, soft_min=0.002,
        soft_max=0.05, precision=4, update=_mowas2_pupil_preview_updated)
    # UpperBody2 的权重映射到目标 ik_updown；启用后按 GFA 自动基准
    # WidthExtraScaling_PerStep**0.5 再乘下面的可调倍率。
    goh_ik_updown_scale: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_ik_updown_scale"),
        description=_("mowas2.prop.goh_ik_updown_scale.desc"),
        default=GOH_IK_UPDOWN_SCALE, update=_mowas2_body_preview_updated)
    goh_ik_updown_multiplier: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_ik_updown_multiplier"),
        description=_("mowas2.prop.goh_ik_updown_multiplier.desc"),
        default=GOH_IK_UPDOWN_MULTIPLIER,
        min=0.8, max=1.3, soft_min=0.95, soft_max=1.15,
        precision=3, update=_mowas2_body_preview_updated)
    # 脚部尺寸倍率：脚/鞋在贴地归一化时按目标 ankle 锚点缩放，
    # 脚底始终钳在地面，不会陷地。1.0 保持旧的贴地压缩行为。
    goh_foot_scale: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_foot_scale"),
        description=_("mowas2.prop.goh_foot_scale.desc"),
        default=1.0, min=0.7, max=2.0, soft_min=0.8, soft_max=1.6,
        update=_mowas2_setting_updated)
    # foot1 腿根间距：以目标 foot1 中点为基准外移上腿根几何，并在到达
    # foot2 前渐隐；目标骨架和膝/踝枢轴保持不变。
    goh_foot1_spacing: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_foot1_spacing"),
        description=_("mowas2.prop.goh_foot1_spacing.desc"),
        default=1.0, min=0.8, max=1.3, soft_min=0.95, soft_max=1.15,
        precision=3, update=_mowas2_body_preview_updated)
    goh_arm_span_scale: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_arm_span_scale"),
        description=_("mowas2.prop.goh_arm_span_scale.desc"),
        default=1.0, min=0.75, max=1.1, soft_min=0.9, soft_max=1.05,
        precision=3, update=_mowas2_arm_preview_updated)
    # v132 兼容字段：v133 起不再应用中央躯干横向缩放。
    goh_shoulder_scale: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_shoulder_scale"),
        description=_("mowas2.prop.goh_shoulder_scale.desc"),
        default=1.0, min=0.5, max=1.3, soft_min=0.7, soft_max=1.1,
        options={'HIDDEN'}, update=_mowas2_shoulder_preview_updated)
    # 腰腹 IK 合并：ik_leftright 按保留比例并入 ik_updown，缓解大角度弯腰撕裂。
    goh_torso_ik_merge: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_torso_ik_merge"),
        description=_("mowas2.prop.goh_torso_ik_merge.desc"),
        default=False, update=_mowas2_setting_updated)
    goh_iklr_keep: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_iklr_keep"),
        description=_("mowas2.prop.goh_iklr_keep.desc"),
        default=0.5, min=0.05, max=1.0, soft_min=0.2, soft_max=1.0,
        update=_mowas2_setting_updated)
    # 手掌链钳制：部分模型手链过度拉伸导致腕掌断开；开启后按保守比值钳制。
    goh_hand_clamp: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_hand_clamp"),
        description=_("mowas2.prop.goh_hand_clamp.desc"),
        default=False, update=_mowas2_setting_updated)
    # GF2/MMD 腕锚修复：旧肩宽算法把肘/腕随世界 Y 一起缩短；原生 GFA
    # 长臂皮肤没有 hand_rot1 权重，因此只恢复真实腕枢轴/几何。
    goh_wrist_stitch: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_wrist_stitch"),
        description=_("mowas2.prop.goh_wrist_stitch.desc"),
        default=True, update=_mowas2_setting_updated)
    # 标准 MMD 源的手指内握卷曲：统一 40° 卷曲在一些模型上会外翻，
    # 默认关闭；需要内握的模型手动开启。KK/KKS 始终应用。
    goh_finger_curl: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_finger_curl"),
        description=_("mowas2.prop.goh_finger_curl.desc"),
        default=False, update=_mowas2_setting_updated)
    report: bpy.props.StringProperty(name=_("mowas2.prop.report"), default="")


class MOWAS2_OT_RefreshMMDPresetSnapshot(bpy.types.Operator):
    bl_idname = 'gem2.mowas2_refresh_mmd_preset_snapshot'
    bl_label = 'Refresh MMD Snapshot'
    bl_description = 'Re-read the selected live MMD Tools preset into the immutable scene snapshot'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.mowas2_props
        try:
            snapshot = mmd_import_preset_snapshot(props.mmd_import_preset)
            snapshot = _store_mmd_import_snapshot(context.scene, snapshot)
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report(
            {'INFO'}, 'Refreshed MMD snapshot: %s (%s)' % (
                snapshot['preset'], snapshot['preset_sha256'] or 'built-in'))
        return {'FINISHED'}


class MOWAS2_OT_SaveExportPreset(bpy.types.Operator):
    bl_idname = 'gem2.mowas2_save_export_preset'
    bl_label = 'Save Named Preset'
    bl_description = 'Save current import, route, export and adjustment settings'
    bl_options = {'REGISTER'}

    preset_name: bpy.props.StringProperty(name='Preset Name', maxlen=64)

    def invoke(self, context, event):
        selected = getattr(context.scene.mowas2_props,
                           'named_export_preset', '__NONE__')
        if selected != '__NONE__':
            self.preset_name = selected
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        from .core import save_export_preset
        name = str(self.preset_name or '').strip()
        try:
            values = _capture_named_export_preset(context.scene.mowas2_props)
            save_export_preset(name, values)
            context.scene.mowas2_props.named_export_preset = name
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Saved export preset: ' + name)
        return {'FINISHED'}


class MOWAS2_OT_LoadExportPreset(bpy.types.Operator):
    bl_idname = 'gem2.mowas2_load_export_preset'
    bl_label = 'Load Named Preset'
    bl_description = 'Apply the selected named export preset'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        global _mowas2_settings_restore_depth
        from .core import get_export_preset
        props = context.scene.mowas2_props
        name = str(props.named_export_preset)
        values = get_export_preset(name)
        if values is None:
            self.report({'ERROR'}, 'Select an existing named preset')
            return {'CANCELLED'}
        migrated_mmd_snapshot = False
        try:
            if 'mmd_import_settings' in values:
                snapshot = _validated_mmd_import_snapshot({
                    'preset': values.get('mmd_import_preset'),
                    'preset_path': values.get('mmd_import_preset_path', ''),
                    'preset_sha256': values.get('mmd_import_preset_sha256', ''),
                    'settings': values['mmd_import_settings'],
                })
            else:
                snapshot = mmd_import_preset_snapshot(
                    values.get('mmd_import_preset', _MMD_PIPELINE_PRESET))
            try:
                _validate_pipeline_mmd_invariants(snapshot['settings'])
            except ValueError as unsafe_snapshot:
                snapshot = mmd_import_preset_snapshot(_MMD_RECOMMENDED_PRESET)
                migrated_mmd_snapshot = True
                print('[MOWAS2] named preset MMD snapshot migrated:', unsafe_snapshot)
            _store_mmd_import_snapshot(context.scene, snapshot)
        except Exception as exc:
            self.report({'ERROR'}, 'Invalid MMD snapshot in preset: ' + str(exc))
            return {'CANCELLED'}
        _mowas2_settings_restore_depth += 1
        try:
            for field in _MOWAS2_PRESET_PROPS:
                if field not in values:
                    continue
                try:
                    value = values[field]
                    if migrated_mmd_snapshot and field == 'mmd_import_preset':
                        value = _MMD_RECOMMENDED_PRESET
                    setattr(props, field, value)
                except (AttributeError, TypeError, ValueError):
                    print('[MOWAS2] ignored invalid preset setting %s=%r'
                          % (field, values[field]))
            _apply_export_route(props)
        finally:
            _mowas2_settings_restore_depth -= 1
        _persist_mowas2_settings(props)
        self.report({'INFO'}, 'Loaded export preset: ' + name)
        return {'FINISHED'}


class MOWAS2_OT_DeleteExportPreset(bpy.types.Operator):
    bl_idname = 'gem2.mowas2_delete_export_preset'
    bl_label = 'Delete Named Preset'
    bl_description = 'Remove the selected named preset (does not alter current settings)'
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from .core import delete_export_preset
        props = context.scene.mowas2_props
        name = str(props.named_export_preset)
        if name == '__NONE__' or not delete_export_preset(name):
            self.report({'ERROR'}, 'Select an existing named preset')
            return {'CANCELLED'}
        props.named_export_preset = '__NONE__'
        self.report({'INFO'}, 'Deleted export preset: ' + name)
        return {'FINISHED'}


class MOWAS2_OT_ImportPMX(bpy.types.Operator):
    """步骤1：导入 PMX 源模型（mmd_tools）"""
    bl_idname = "gem2.mowas2_import_pmx"
    bl_label = _("mowas2.op.import_pmx.label")
    bl_description = _("mowas2.op.import_pmx.desc")
    bl_options = {'REGISTER', 'UNDO'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.pmx", options={'HIDDEN'})

    def invoke(self, context, event):
        _restore_mowas2_settings(context.scene, force=False)
        props = context.scene.mowas2_props
        if props.pmx_path and os.path.isfile(props.pmx_path):
            self.filepath = props.pmx_path
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            _restore_mowas2_settings(context.scene, force=False)
            if not self.filepath:
                self.report({'ERROR'}, _("mowas2.err.select_pmx"))
                return {'CANCELLED'}
            props = context.scene.mowas2_props
            snapshot = _effective_mmd_import_snapshot(context.scene, props)
            arm, mesh = import_pmx(
                self.filepath,
                preset_name=snapshot['preset'],
                preset_settings=snapshot['settings'],
                preset_path=snapshot['preset_path'],
                preset_sha256=snapshot['preset_sha256'])
            props.pmx_path = self.filepath
            props.report = _("mowas2.info.pmx_imported",
                             file=os.path.basename(self.filepath),
                             arm=arm.name, mesh=mesh.name)
            self.report({'INFO'}, props.report)
            return {'FINISHED'}
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, _("mowas2.err.pmx_failed", error=e))
            return {'CANCELLED'}


class MOWAS2_OT_HumanRestConvert(bpy.types.Operator):
    """Convert an existing GEM2 human mesh between MDL rest spaces."""
    bl_idname = 'gem2.mowas2_human_rest_convert'
    bl_label = _('mowas2.human_rest.convert.label')
    bl_description = _('mowas2.human_rest.convert.desc')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _human_rest_convert_available(context)

    mesh_name: bpy.props.StringProperty(name='Mesh', default='', options={'HIDDEN'})
    source_armature_name: bpy.props.StringProperty(
        name='Source Armature', default='', options={'HIDDEN'})
    target_armature_name: bpy.props.StringProperty(
        name='Target Armature', default='', options={'HIDDEN'})
    duplicate_mesh: bpy.props.BoolProperty(
        name=_('mowas2.human_rest.duplicate'),
        description=_('mowas2.human_rest.duplicate.desc'),
        default=False)
    exact_reverse: bpy.props.BoolProperty(
        name=_('mowas2.human_rest.exact_reverse'),
        description=_('mowas2.human_rest.exact_reverse.desc'),
        default=True)

    def invoke(self, context, event):
        _restore_mowas2_settings(context.scene, force=False)
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        props = context.scene.mowas2_props
        layout.label(text=_('mowas2.human_rest.desc'), icon='ARMATURE_DATA')
        destination_name = os.path.basename(str(props.mdl_path))
        if not destination_name:
            destination_name = _('mowas2.human_rest.destination_missing')
        layout.label(text=_('mowas2.human_rest.convert.destination',
                            name=destination_name),
                     icon='FILE_TICK' if os.path.isfile(
                         bpy.path.abspath(str(props.mdl_path))) else 'ERROR')
        layout.prop(self, 'duplicate_mesh')
        layout.prop(self, 'exact_reverse')

    def execute(self, context):
        try:
            _restore_mowas2_settings(context.scene, force=False)
            props = context.scene.mowas2_props
            _apply_export_route(props)
            report = convert_human_rest_scene(
                context.scene, props.mdl_path,
                mesh_name=self.mesh_name,
                source_armature_name=self.source_armature_name,
                target_armature_name=self.target_armature_name,
                duplicate_mesh=self.duplicate_mesh,
                exact_reverse=self.exact_reverse)
            props.report = _human_rest_module().conversion_summary(report)
            self.report({'INFO'}, props.report)
            return {'FINISHED'}
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, _(
                'mowas2.human_rest.err.convert_failed', error=str(exc)))
            return {'CANCELLED'}


def _human_rest_conversion_metadata(mesh):
    value = _human_rest_module().conversion_metadata(mesh)
    return value if isinstance(value, dict) else {}


def _human_rest_converted_mesh(scene):
    active = bpy.context.view_layer.objects.active
    all_candidates = [obj for obj in scene.objects
                      if obj.type == 'MESH'
                      and _human_rest_conversion_marked(obj)]
    if not all_candidates:
        raise RuntimeError('No converted GEM2 human mesh found')
    if any(_human_rest_same_object(active, candidate)
           for candidate in all_candidates):
        return active
    selected = [obj for obj in all_candidates if obj.select_get()]
    if len(selected) == 1:
        return selected[0]
    expected = _human_rest_path(
        getattr(getattr(scene, 'mowas2_props', None), 'mdl_path', ''))
    matching = [obj for obj in all_candidates
                if _human_rest_path(
                    _human_rest_conversion_metadata(obj).get('destination_mdl', ''))
                == expected]
    if len(matching) == 1:
        return matching[0]
    if len(selected) > 1:
        raise RuntimeError('Select exactly one converted human mesh to export')
    if len(matching) > 1:
        raise RuntimeError('Multiple converted meshes match the destination MDL')
    if len(all_candidates) == 1:
        return all_candidates[0]
    raise RuntimeError('Select exactly one converted human mesh to export')


def _human_rest_target_for_mesh(scene, mesh):
    metadata = _human_rest_conversion_metadata(mesh)
    target_name = metadata.get('destination_armature', '')
    target = next((obj for obj in scene.objects if obj.name == target_name), None)
    if target is not None and target.type == 'ARMATURE':
        if not _human_rest_target_usable(target):
            raise RuntimeError(
                'Converted mesh requires a raw-rest destination armature')
        expected = _human_rest_path(metadata.get('destination_mdl', ''))
        actual = _human_rest_armature_path(target)
        if expected and actual and expected != actual:
            raise RuntimeError('Converted mesh metadata targets a different armature MDL')
        return target
    target_mods = []
    for modifier in mesh.modifiers:
        target = modifier.object
        if (modifier.type == 'ARMATURE' and target
                and target.type == 'ARMATURE'
                and not any(_human_rest_same_object(target, item)
                            for item in target_mods)):
            target_mods.append(target)
    target_mods = [target for target in target_mods
                   if scene.objects.get(target.name) is not None]
    if len(target_mods) == 1:
        if not _human_rest_target_usable(target_mods[0]):
            raise RuntimeError(
                'Converted mesh requires a raw-rest destination armature')
        return target_mods[0]
    raise RuntimeError('Converted human mesh has no unique destination armature')


class MOWAS2_OT_HumanRestExport(bpy.types.Operator):
    """Export a mesh already converted by the human rest operator."""
    bl_idname = 'gem2.mowas2_human_rest_export'
    bl_label = _('mowas2.human_rest.converted_export.label')
    bl_description = _('mowas2.human_rest.converted_export.desc')
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _human_rest_export_available(context)

    def execute(self, context):
        try:
            _restore_mowas2_settings(context.scene, force=False)
            props = context.scene.mowas2_props
            mesh = _human_rest_converted_mesh(context.scene)
            target = _human_rest_target_for_mesh(context.scene, mesh)
            destination = str(target.get('gem2_mdl_path') or '')
            if not destination:
                raise RuntimeError('Destination armature lacks gem2_mdl_path')
            route = _infer_export_route(destination)
            out = export_all(
                mesh, target, props.output_dir,
                skin_name=props.skin_name or 'skin',
                texture_format=props.texture_format,
                nvtt_path=props.nvtt_path or None,
                toon_shader=props.toon_shader,
                export_route=route,
                auto_split_over_limit=props.auto_split_over_limit,
                split_record_limit=props.split_record_limit)
            props.report = _('mowas2.info.export_done', dir=out)
            self.report({'INFO'}, props.report)
            return {'FINISHED'}
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, _(
                'mowas2.human_rest.err.export_failed', error=str(exc)))
            return {'CANCELLED'}


class MOWAS2_OT_BuildTarget(bpy.types.Operator):
    """步骤2：从原版 .mdl 构建 GEM2 目标骨架"""
    bl_idname = "gem2.mowas2_build_target"
    bl_label = _("mowas2.op.build_target.label")
    bl_description = _("mowas2.op.build_target.desc")
    bl_options = {'REGISTER', 'UNDO'}

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.mdl", options={'HIDDEN'})

    def invoke(self, context, event):
        _restore_mowas2_settings(context.scene, force=False)
        props = context.scene.mowas2_props
        _apply_export_route(props)
        if props.mdl_path and os.path.isfile(props.mdl_path):
            self.filepath = props.mdl_path
        if props.export_route != 'CUSTOM':
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            _restore_mowas2_settings(context.scene, force=False)
            if not self.filepath or not os.path.isfile(self.filepath):
                self.report({'ERROR'}, _("mowas2.err.select_mdl"))
                return {'CANCELLED'}
            props = context.scene.mowas2_props
            props.export_route = _infer_export_route(self.filepath)
            props.mdl_path = self.filepath
            _apply_export_route(props)
            tgt = build_target_from_mdl(props.mdl_path)
            props.report = _("mowas2.info.target_built",
                             name=tgt.name, bones=len(tgt.data.bones))
            self.report({'INFO'}, props.report)
            return {'FINISHED'}
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, _("mowas2.err.target_failed", error=e))
            return {'CANCELLED'}


class MOWAS2_OT_AlignOnly(bpy.types.Operator):
    """步骤3：只对齐摆好头/身/腿（不绑骨不减面）"""
    bl_idname = "gem2.mowas2_align_only"
    bl_label = _("mowas2.op.align.label")
    bl_description = _("mowas2.op.align.desc")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            _restore_mowas2_settings(context.scene, force=False)
            props = context.scene.mowas2_props
            snap = align_only(props.output_dir, props.ground_z,
                              pmx_path=props.pmx_path)
            props.report = _("mowas2.info.align_done", snap=snap)
            self.report({'INFO'}, props.report)
            return {'FINISHED'}
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, _("mowas2.err.align_failed", error=e))
            return {'CANCELLED'}


class MOWAS2_OT_FullPipeline(bpy.types.Operator):
    """步骤4：完整管线（对齐+绑骨+权重+减面+导出）"""
    bl_idname = "gem2.mowas2_full_pipeline"
    bl_label = _("mowas2.op.full_pipeline.label")
    bl_description = _("mowas2.op.full_pipeline.desc")
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        _restore_mowas2_settings(context.scene, force=False)
        _apply_export_route(context.scene.mowas2_props)
        return context.window_manager.invoke_props_dialog(self, width=760)

    def draw(self, context):
        _draw_pipeline_confirmation(self.layout, context.scene.mowas2_props)

    def execute(self, context):
        try:
            _restore_mowas2_settings(context.scene, force=False)
            props = context.scene.mowas2_props
            _apply_export_route(props)
            out = run_full(props.output_dir, props.ground_z,
                           protect_face=props.protect_face,
                           protect_tight=props.protect_tight,
                           enable_decimate=props.enable_decimate,
                           skin_name=props.skin_name or 'skin',
                           pmx_path=props.pmx_path,
                           texture_format=props.texture_format,
                           nvtt_path=props.nvtt_path or None,
                           toon_shader=props.toon_shader,
                           export_route=props.export_route,
                           auto_split_over_limit=props.auto_split_over_limit,
                           split_record_limit=props.split_record_limit)
            props.report = _("mowas2.info.export_done", dir=out)
            self.report({'INFO'}, props.report)
            return {'FINISHED'}
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, _("mowas2.err.pipeline_failed", error=e))
            return {'CANCELLED'}


class MOWAS2_PT_Panel(bpy.types.Panel):
    bl_label = _("mowas2.panel.label")
    bl_idname = "MOWAS2_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "GOH"
    bl_order = 10
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        layout = self.layout
        _restore_mowas2_settings(context.scene, force=False)
        props = context.scene.mowas2_props

        box = layout.box()
        box.label(text=_("mowas2.panel.intro"), icon='MOD_ARMATURE')
        box.label(text=_("mowas2.panel.summary1"), icon='DOT')
        box.label(text=_("mowas2.panel.summary2"), icon='DOT')
        box.prop(props, "remember_settings", icon='PREFERENCES')
        box.prop(props, "named_export_preset", text="Preset")
        row = box.row(align=True)
        row.operator('gem2.mowas2_load_export_preset', text='Load', icon='IMPORT')
        row.operator('gem2.mowas2_save_export_preset', text='Save As', icon='ADD')
        delete_row = row.row(align=True)
        delete_row.enabled = props.named_export_preset != '__NONE__'
        delete_row.operator('gem2.mowas2_delete_export_preset', text='', icon='TRASH')

        # 场景状态
        arms = [o for o in context.scene.objects if o.type == 'ARMATURE']
        src = None
        tgt = None
        if arms:
            tgt = next((a for a in arms if a.get('gem2_world_mats')), None)
            src = next((a for a in arms if a != tgt), None) if tgt else None
        status = _("mowas2.scene.status",
                    src=src.name if src else _("mowas2.scene.not_imported"),
                    tgt=tgt.name if tgt else _("mowas2.scene.not_built"))
        box = layout.box()
        box.label(text=_("mowas2.scene.label"), icon='SCENE_DATA')
        box.label(text=status, icon='INFO')

        # Existing GEM2 human rest conversion is intentionally separate from
        # the PMX alignment workflow.
        box = layout.box()
        box.label(text=_('mowas2.human_rest.label'), icon='ARMATURE_DATA')
        box.label(text=_('mowas2.human_rest.desc'), icon='LOCKED')
        box.operator('gem2.mowas2_human_rest_convert',
                     text=_('mowas2.human_rest.convert.label'), icon='BONE_DATA')
        box.operator('gem2.mowas2_human_rest_export',
                     text=_('mowas2.human_rest.converted_export.label'),
                     icon='EXPORT')

        # 1. 导入 PMX
        box = layout.box()
        box.label(text=_("mowas2.step1.label"), icon='IMPORT')
        preset_row = box.row(align=True)
        preset_row.prop(props, "mmd_import_preset", text="MMD Preset")
        preset_row.operator(
            'gem2.mowas2_refresh_mmd_preset_snapshot', text='', icon='FILE_REFRESH')
        box.prop(props, "pmx_path", text="")
        box.operator("gem2.mowas2_import_pmx", text=_("mowas2.step1.import"),
                     icon='FILE_TICK')

        # 2. 构建目标骨架
        box = layout.box()
        box.label(text=_("mowas2.step2.label"), icon='ARMATURE_DATA')
        box.prop(props, "export_route", expand=True)
        target_row = box.row()
        target_row.enabled = props.export_route == 'CUSTOM'
        target_row.prop(props, "mdl_path", text="")
        if props.export_route != 'CUSTOM':
            box.label(text=os.path.basename(props.mdl_path), icon='FILE_TICK')
        row = box.row()
        row.operator("gem2.mowas2_build_target", text=_("mowas2.step2.build"),
                     icon='BONE_DATA')
        if tgt:
            box.label(text=_("mowas2.step2.ready", name=tgt.name, bones=len(tgt.data.bones)),
                      icon='CHECKMARK')

        # 3. 仅对齐
        box = layout.box()
        box.label(text=_("mowas2.step3.label"), icon='SNAP_ON')
        box.operator("gem2.mowas2_align_only", text=_("mowas2.step3.align"),
                     icon='RESTRICT_VIEW_OFF')

        # 4. 完整导出 (保持原布局, E6.19 保护开关移到高级区)
        box = layout.box()
        box.label(text=_("mowas2.step4.label"), icon='EXPORT')
        box.prop(props, "skin_name")
        entity_valid = True
        try:
            entity_name = _validate_entity_name(props.skin_name)
            box.label(text=_("mowas2.status.entity_output",
                             name=entity_name), icon='FILE_TICK')
        except ValueError:
            entity_valid = False
            box.label(text=_("mowas2.status.entity_invalid"), icon='ERROR')
        box.prop(props, "output_dir", text=_("mowas2.step4.output_dir"))
        box.prop(props, "texture_format", expand=True)
        if props.texture_format == 'DDS':
            box.prop(props, "nvtt_path", text="NVTT")
            try:
                _tool_kind, _tool_path = _find_nvtt_tool(props.nvtt_path or None)
                box.label(text=os.path.basename(_tool_path), icon='CHECKMARK')
            except RuntimeError:
                box.label(text=_("mowas2.status.nvtt_not_found"), icon='ERROR')
        toon_row = box.row()
        toon_row.enabled = props.export_route != 'MOWAS2'
        toon_row.prop(props, "toon_shader")
        if props.export_route == 'MOWAS2':
            box.label(text="ToonShader is always disabled for MOWAS2", icon='LOCKED')
        if props.toon_shader and props.export_route != 'MOWAS2':
            toon_root = _find_toon_shader_root()
            if toon_root:
                box.label(text=_("mowas2.status.toon_found",
                                 workshop=_TOON_SHADER_WORKSHOP_ID),
                          icon='CHECKMARK')
            else:
                box.label(text=_("mowas2.status.toon_required",
                                 workshop=_TOON_SHADER_WORKSHOP_ID),
                          icon='ERROR')
        run_row = box.row()
        run_row.enabled = entity_valid
        run_row.operator("gem2.mowas2_full_pipeline",
                         text=_("mowas2.step4.run"), icon='PLAY')

        # 已由 Blender 导入的 FBX/OBJ/glTF 等模型直接走格式无关拆分导出。
        box = layout.box()
        box.label(text=_("mowas2.generic_export.label"), icon='MESH_DATA')
        box.prop(props, "split_record_limit")
        generic_row = box.row(align=True)
        preflight = generic_row.operator(
            "gem2.multipart_preflight",
            text=_("mowas2.generic_export.preflight"), icon='VIEWZOOM')
        preflight.record_limit = props.split_record_limit
        generic_export = generic_row.operator(
            "export_scene.gem2_multipart",
            text=_("mowas2.generic_export.run"), icon='EXPORT')
        generic_export.record_limit = props.split_record_limit

        # 高级
        box = layout.box()
        box.label(text=_("mowas2.advanced.label"), icon='SETTINGS')
        box.prop(props, "ground_z")
        # GOH 版: 手部分指开关 (GFA 分指默认; 可切回 MOWAS2 单骨)
        box.prop(props, "goh_hand_split")
        # v10: 长臂模板 + 源骨骼分段缩放 (默认 True)
        box.prop(props, "goh_gfa_longarm")
        # 可选头部比例修正（默认关闭；倍率可按模型微调）
        box.prop(props, "goh_enlarge_head")
        row = box.row()
        row.enabled = bool(props.goh_enlarge_head)
        row.prop(props, "goh_head_scale")
        row = box.row()
        row.enabled = bool(props.goh_enlarge_head)
        row.prop(props, "goh_head_neck_follow", slider=True)
        # GOH 原生透明材质对照开关；GFA Eyes+/eyeblend 独立遵循 opaque 契约。
        box.prop(props, "goh_alpha_test")
        box.prop(props, "goh_fix_pupil_depth")
        row = box.row()
        row.enabled = bool(props.goh_fix_pupil_depth)
        row.prop(props, "goh_pupil_clearance")
        # UpperBody2 权重最终进入目标 ik_updown；倍率改动会在再次对齐时
        # 从原 PMX 重建，避免在冻结网格上累计缩放。
        box.prop(props, "goh_ik_updown_scale")
        row = box.row()
        row.enabled = bool(props.goh_ik_updown_scale)
        row.prop(props, "goh_ik_updown_multiplier")
        # foot1 上腿根间距与整臂间距支持冻结后即时、可逆预览。
        box.prop(props, "goh_foot1_spacing", slider=True)
        box.prop(props, "goh_arm_span_scale", slider=True)
        # 脚部尺寸可调：>1 放大脚/鞋（贴地不陷），<1 收窄。
        box.prop(props, "goh_foot_scale")
        # 腰腹 IK 合并：缓解大角度弯腰时腰腹被 ik_leftright/ik_updown 拉开。
        box.prop(props, "goh_torso_ik_merge")
        row = box.row()
        row.enabled = bool(props.goh_torso_ik_merge)
        row.prop(props, "goh_iklr_keep")
        # 手掌链钳制与 GF2 腕锚几何复位。
        box.prop(props, "goh_hand_clamp")
        box.prop(props, "goh_wrist_stitch")
        # MMD 手指内握：默认关闭（部分模型 40° 卷曲会外翻）。
        box.prop(props, "goh_finger_curl")
        # E6.19: 减面保护开关 (默认开启)
        sub = box.box()
        sub.label(text=_("mowas2.advanced.protection"), icon='MOD_DECIM')
        split_row = sub.row()
        split_row.enabled = (props.export_route in {'GOH', 'MOWAS2'})
        split_row.prop(props, "auto_split_over_limit")
        limit_row = sub.row()
        limit_row.enabled = bool(
            props.auto_split_over_limit
            and props.export_route in {'GOH', 'MOWAS2'})
        limit_row.prop(props, "split_record_limit")
        sub.prop(props, "enable_decimate")
        sub.prop(props, "protect_face")
        sub.prop(props, "protect_tight")

        # 载具 (vehicle)
        box = layout.box()
        box.label(text=_("mowas2.vehicle.label"), icon='OPTIONS')
        box.label(text=_("mowas2.vehicle.desc"), icon='DOT')
        box.operator("gem2.mowas2_import_vehicle_folder",
                     text=_("mowas2.vehicle.import"), icon='FILE_FOLDER')
        box.operator("gem2.export_vehicle_to_mowas2",
                     text=_("mowas2.vehicle.export_mowas2"), icon='EXPORT')
        box.operator("gem2.export_vehicle_to_goh",
                     text=_("mowas2.vehicle.export_goh"), icon='EXPORT')

        # GOH 动画 (2026-08-18): 从 properties.pak 提取 .anm 测试模型
        box = layout.box()
        box.label(text=_("anm.extract_goh.label"), icon='PLAY')
        box.operator("gem2.extract_goh_anm",
                     text=_("anm.extract_goh.label"), icon='FILE_ARCHIVE')

        if props.report:
            box = layout.box()
            box.label(text=_("mowas2.recent.label"), icon='INFO')
            for line in props.report.split('\n'):
                box.label(text=line, icon='DOT')


CLASSES = (MOWAS2_OT_AutoPipeline,
           MOWAS2_OT_RefreshMMDPresetSnapshot,
           MOWAS2_OT_SaveExportPreset,
           MOWAS2_OT_LoadExportPreset,
           MOWAS2_OT_DeleteExportPreset,
           MOWAS2_OT_ImportPMX,
           MOWAS2_OT_HumanRestConvert,
           MOWAS2_OT_HumanRestExport,
           MOWAS2_OT_BuildTarget,
           MOWAS2_OT_AlignOnly,
           MOWAS2_OT_FullPipeline,
           MOWAS2_PT_Panel,
           MOWAS2_SceneProps)


def register():
    # E6.19: 幂等注册 —— 先清理旧注册 (F3 Reload Scripts 不会先 unregister,
    # 直接重注册会 'already registered' 失败且场景 mowas2_props 停留旧实例,
    # 面板 draw 访问新属性抛 AttributeError → 面板后半段消失 = "一键按钮被拆")
    try:
        unregister()
    except Exception:
        pass
    registered = []
    try:
        for cls in CLASSES:
            bpy.utils.register_class(cls)
            registered.append(cls)
        bpy.types.Scene.mowas2_props = bpy.props.PointerProperty(
            type=MOWAS2_SceneProps, options={'SKIP_SAVE'})
        _register_mowas2_settings_handlers()
        # During Blender startup add-ons register while bpy.data is _RestrictData.
        # load_post restores settings once Scene data is available; manual enabling
        # and bridge registration still restore immediately.
        try:
            bpy.data.scenes
        except AttributeError:
            pass
        else:
            _restore_all_mowas2_settings(force=True)
    except Exception:
        _remove_mowas2_settings_handlers()
        try:
            del bpy.types.Scene.mowas2_props
        except Exception:
            pass
        for cls in reversed(registered):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:
                pass
        raise


def unregister():
    _remove_mowas2_settings_handlers()
    try:
        del bpy.types.Scene.mowas2_props
    except Exception:
        pass
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
