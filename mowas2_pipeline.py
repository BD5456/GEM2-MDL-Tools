# -*- coding: utf-8 -*-
"""
MOWAS2 PMX→GEM2 自动管线 v2（2026-08 修正版，手动跑通后固化）
================================================================
把 MMD/PMX 模型自动绑骨到 GEM2(MOWAS2) 58 骨士兵骨架并导出单个 .ply。

v1 的问题（用户实测反馈 + 修复）：
  1. 旧对齐(GFA 启发式缩放链)对 T-pose 源失效 → "宽体普京"。
     → 改为 Umeyama 刚性相似变换（只旋转+平移+均匀缩放，形状零失真）
       + T-pose 手臂姿态级旋转（绕肩 pivot + roll 迭代）。
  2. 多 VolumeView 拆分导出 → 游戏内只有身体、脑袋留在地图原点：
     士兵骨架由 human_anm.ext 动画系统接管，新加的 VolumeView 载体骨
     无动画数据被重置到原点（minomet 那种多 VolumeView 只适用于静态模型）。
     → 改回单 VolumeView（原版 skin 骨），一个 .ply。
  3. 该 PMX 的 UV 逐面独立（每材质 UV 拆分系数 = 1.0：拆分顶点 = 3×面），
     单 ply 顶点 ≤65535(u16 索引硬限) ⟺ 面 ≤21845。
     → COLLAPSE 减面（面部顶点 VG 保护 + 迭代 ratio），保留原 UV/贴图/权重。
  4. 权重转移丢躯干：该 PMX 躯干权重全在 cf_s_* 补充骨上（主骨只有 0 权重
     条目）→ 必须先跑骨骼铸造（未映射骨权重上卷到最近映射祖先）。
  5. mirror_swap 把 'ik_leftright' 误换名成 'ik_leftleft'（结尾 'right' 命中
     了裸后缀规则）→ 必须用 '_left'/'_right'（带下划线）后缀判断。
  6. modifier_apply 烘焙在 parent 链(1.9×Rz90)下双重变换 →
     改用 evaluated 顶点写回法冻结姿态。
"""
import bpy
import os
import re
import struct
import math
import shutil
import subprocess
import json
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

from .i18n import _

OUT_DEFAULT = os.path.join(os.path.expanduser('~'), 'Desktop')  # 默认输出=用户桌面, 面板可改
GROUND_Z = -0.07  # 样本 skin 网格脚底高度（贴地基准）
# GOH 原生 akq_youwu/akq_ming 的透明材质均采用 alpharef 127 + blend test。
# 默认启用该语义；面板可切回 blend 做对照测试。
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
    """PNG → TGA type2 未压缩 BGRA 32bpp (desc=0x08)。

    用 bpy 解码（引擎不读 PNG，但 Blender 可读），numpy 向量化转换，不依赖 PIL。
    Blender image.pixels 顺序 = 自下而上逐行（TGA 同款原点左下），直接写。
    与参考 TGA（type2 32bpp desc=0x08）逐字节一致。

    soften=True 时对像素做 alpha 软化（见 TIGHT_ALPHA_SCALE/LIFT 注释）：
    紧身衣物贴图深黑高 alpha → 压缩 alpha 让皮肤透出。

    fill_black=True (2026-08-18 GOH): KK 系贴图透明区 RGB 纯黑, mtl 用
    {blend none} 时引擎直接画 RGB → 黑块/半透明。把 alpha<127 像素的 RGB
    用邻近不透明像素颜色填充 (迭代膨胀), 使 none 类实体材质显示正常。
    不透明像素不动 (贴图主体零改动)。

    返回 bool：贴图是否有真透明；return_profile=True 时返回包含
    has_alpha/transparent_ratio/partial_ratio/min_alpha/max_alpha 的字典，
    供未知材质区分实质镂空与少量抗锯齿边缘。
    """
    import numpy as np
    img = bpy.data.images.load(img_path, check_existing=False)
    try:
        w, h = img.size
        if w <= 0 or h <= 0:
            raise RuntimeError(_("mowas2.err.texture_bad_size",
                                 width=w, height=h))
        px = np.array(img.pixels, dtype=np.float32).reshape(h, w, 4)
        if soften:
            px[:, :, 3] = px[:, :, 3] * TIGHT_ALPHA_SCALE
            if TIGHT_ALPHA_LIFT > 0:
                lift = (1.0 - px[:, :, 3]) * (TIGHT_ALPHA_LIFT / 255.0)
                px[:, :, :3] = np.clip(px[:, :, :3] + lift[:, :, None], 0.0, 1.0)
        if fill_black:
            _fill_black_alpha(px)
        # alpha 检测与分布统计 (向量化, 一次扫描; 阈值 250/255≈0.9804)。
        # partial_ratio 用于区分 GOH 的 test 镂空与 blend 半透明边缘。
        if img.channels >= 4:
            alpha = px[:, :, 3]
            has_alpha = bool((alpha < (250.0 / 255.0)).any())
            partial = (alpha > (2.0 / 255.0)) & (alpha < (253.0 / 255.0))
            profile = {
                'has_alpha': has_alpha,
                'partial_ratio': float(partial.mean()),
                'transparent_ratio': float(
                    (alpha <= (2.0 / 255.0)).mean()),
                'opaque_ratio': float(
                    (alpha >= (253.0 / 255.0)).mean()),
                'min_alpha': float(alpha.min()),
                'max_alpha': float(alpha.max()),
            }
        else:
            has_alpha = False
            profile = {'has_alpha': False, 'partial_ratio': 0.0,
                       'transparent_ratio': 0.0, 'opaque_ratio': 1.0,
                       'min_alpha': 1.0, 'max_alpha': 1.0}
        bgra = px[:, :, [2, 1, 0, 3]]
        data = (bgra * 255.0 + 0.5).astype(np.uint8).tobytes()
        header = bytes((
            0, 0, 2,                       # idlen, colormap, type=uncompressed truecolor
            0, 0, 0, 0, 0,                 # colormap spec
            0, 0, 0, 0,                    # x/y origin
            w & 0xFF, (w >> 8) & 0xFF,
            h & 0xFF, (h >> 8) & 0xFF,
            32, 0x08,                      # 32bpp, 8 alpha bits
        ))
        with open(tga_path, 'wb') as f:
            f.write(header)
            f.write(data)
        return profile if return_profile else has_alpha
    finally:
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass


def _fill_black_alpha(px, thresh=0.5, max_iter=48):
    """KK 黑底填充: alpha < thresh 的像素 RGB ← 邻近不透明像素颜色。

    迭代膨胀: 每轮把当前透明像素的 RGB 设为 8 邻域中"上一轮已不透明"
    像素的均值。最多 max_iter 轮 (1024×1024 每轮 8 次 roll, 48 轮约
    1-2 秒); 残留 (大面积透明区中心) 取整贴图不透明像素均值兜底。
    alpha 通道不动 (none 类 mtl 引擎不读 alpha; blend/test 类不调用本函数)。
    """
    import numpy as np
    alpha = px[:, :, 3]
    mask = alpha < thresh
    if not mask.any():
        return
    rgb = px[:, :, :3].copy()
    solid = ~mask
    if not solid.any():
        # 全透明贴图: 兜底置中性灰 (几乎不会出现)
        px[:, :, :3] = 0.5
        return
    cur = mask.copy()
    n = 0
    while cur.any() and n < max_iter:
        acc = np.zeros_like(rgb)
        cnt = np.zeros(rgb.shape[:2], dtype=np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (-1, 1), (1, -1), (1, 1)):
            r = np.roll(rgb, dy, axis=0)
            r = np.roll(r, dx, axis=1)
            m = np.roll(solid, dy, axis=0)
            m = np.roll(m, dx, axis=1)
            acc += r * m[:, :, None]
            cnt += m
        ok = cur & (cnt > 0)
        if not ok.any():
            break
        ok_flat = ok
        rgb[ok_flat] = acc[ok_flat] / cnt[ok_flat][:, None]
        solid[ok_flat] = True
        cur[ok_flat] = False
        n += 1
    if cur.any():
        # 残留 (大块透明中心): 用整贴图不透明像素均值
        mean_rgb = rgb[solid].mean(axis=0) if solid.any() else np.array([0.5, 0.5, 0.5])
        rgb[cur] = mean_rgb
    px[:, :, :3] = rgb


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
                   mode='kk', alpha_profile=None, alpha_test=None):
    """统一判定一个 diffuse 贴图的最终 mtl blend 模式。

    默认采用 GOH 原生 akq_youwu 的 alpha-test 写法：
    ``alpharef 127 + blend test + alphatocoverage``，不设置 PLY 0x0002。
    面板关闭 ``透明材质使用 Alpha Test`` 后，保留旧的 blend/test 分类，
    便于对照验证；none 仍优先保护身体、鞋和盔甲实体材质。
    """
    if alpha_test is None:
        alpha_test = _goh_alpha_test_mode()
    alpha_test = bool(alpha_test)
    base = os.path.splitext(os.path.basename(diffuse or ''))[0]
    if base in exclude:
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
    if _is_force_alpha_blend(base):
        return 'test' if alpha_test else 'blend'
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


_TEXTURE_SOURCE_EXTENSIONS = ('.png', '.tga', '.bmp', '.jpg', '.jpeg', '.dds')
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
                      toon_shader=False):
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
        hair_mat = _kw_hit(base, ('hair', 'toufa', '髪', '头发',
                                  '发', '髮', 'bang', 'forelock',
                                  'maegami', 'kami', 'liuhai'))
        test_named = _kw_hit(base, ('tight', 'bodytights'))
        # Toon Shader's hair contract requires alpha-test. Do not pre-fill its
        # transparent RGB or compress it as opaque BC1 before MTL conversion.
        fill = ((hair_mat and not toon_shader)
                or (_is_force_opaque_alpha(base)
                    and not ((toon_shader and hair_mat)
                             or (_goh_alpha_test_mode() and test_named)))
                or _is_force_opaque_hard(base))
        try:
            retained_source = ext_lower == target_ext
            profile_path = temp_tga if retained_source or texture_format == 'DDS' else target_path
            profile = png_to_tga(
                source_path, profile_path, soften=soften,
                fill_black=fill, return_profile=True)
            has_alpha = bool(profile.get('has_alpha'))
            blend = _classify_mode(
                base, has_alpha, exclude=exclude, mode=mode,
                alpha_profile=profile)
            if toon_shader and hair_mat:
                blend = 'test'
            plan[base] = blend
            if retained_source:
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
            blend = 'test' if _goh_alpha_test_mode() else 'blend'
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
                            toon_shader=False):
    """内置无依赖 TGA 工作流（默认，兼容既有导出）。"""
    return _convert_textures(
        out_sub, texture_format='TGA', exclude=exclude, mode=mode,
        toon_shader=toon_shader)


def convert_textures_to_dds(out_sub, exclude=EXCLUDE_TRANSPARENT, mode='kk',
                            nvtt_path=None, toon_shader=False):
    """可选 DDS 工作流；使用用户安装的外部 NVTT，不随插件分发。"""
    return _convert_textures(
        out_sub, texture_format='DDS', exclude=exclude, mode=mode,
        nvtt_path=nvtt_path, toon_shader=toon_shader)


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


def apply_toon_shader_conversion(out_sub):
    """按 GFA ToonShaderConvert 规则将已生成的 MTL 接入 Workshop Toon Shader。

    该转换只写 MTL 引用，不复制 ONCL-C 许可覆盖的 shader/贴图资产。当前
    blend/alpharef/alphatocoverage 会被保留；只有 hair 分类按原规则强制 test，
    并补齐 GOH 所需的 alpharef 127 与 alphatocoverage。
    """
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

# ═══════════════════════════════════════════════════════════════
#  PLY 游戏原生 per-vertex 格式（经 medicgirl/skin.ply 字节级校准）
# ═══════════════════════════════════════════════════════════════
D3DFVF_XYZB2 = 0x0008
D3DFVF_NORMAL = 0x0010
D3DFVF_TEX1 = 0x0100
D3DFVF_LASTBETA_UBYTE4 = 0x1000
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
        for node in material.node_tree.nodes:
            if node.type != 'TEX_IMAGE' or not node.image or not node.image.filepath:
                continue
            source = bpy.path.abspath(node.image.filepath)
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
    """读取肩宽倍率：1.0 = 目标 hand1 肩宽（当前行为）；
    <1 时把 ShoulderC/肩线向中线收窄，匹配 GOH 原版 clavicle 肩宽。"""
    try:
        sc = bpy.context.scene
        if sc and hasattr(sc, 'mowas2_props'):
            value = float(getattr(sc.mowas2_props,
                                  'goh_shoulder_scale', 1.0))
            return max(0.8, min(1.3, value))
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


def _build_game_normals(mesh, loop_tris):
    """为 UV 拆分后的网格重建面部平滑法线，不改变几何/UV。

    UV seam split 会把同一空间位置复制成多个顶点；直接读取
    ``mesh.vertices[i].normal`` 后，这些副本只能看到自己的三角面，二次元
    脸部在光照下会出现明显的三角形锯齿。这里按材质和空间位置聚合面法线，
    用面积加权平均写入导出用的临时法线数组。只处理 face/kao 类材质，眼睛、
    头发和衣物仍沿用原有法线，避免跨部件串光。
    """
    normals = [v.normal.copy() for v in mesh.vertices]
    tol = 1e-6

    def pos_key(co):
        return (int(round(float(co.x) / tol)),
                int(round(float(co.y) / tol)),
                int(round(float(co.z) / tol)))

    accum = {}
    vertex_mats = {}
    for tri in loop_tris:
        mi = tri.material_index
        if mi >= len(mesh.materials) or not mesh.materials[mi]:
            continue
        mat_name = mesh.materials[mi].name.lower()
        is_face = any(kw in mat_name for kw in FACE_NORMAL_KW)
        for vi in tri.vertices:
            vertex_mats.setdefault(vi, set()).add(mi)
        if not is_face:
            continue
        a, b, c = (mesh.vertices[tri.vertices[0]].co,
                   mesh.vertices[tri.vertices[1]].co,
                   mesh.vertices[tri.vertices[2]].co)
        weighted = (b - a).cross(c - a)
        if weighted.length_squared <= 1e-16:
            continue
        for vi in tri.vertices:
            key = (mi, pos_key(mesh.vertices[vi].co))
            if key in accum:
                accum[key] += weighted
            else:
                accum[key] = weighted.copy()

    smoothed = 0
    for vi in range(len(mesh.vertices)):
        key_pos = pos_key(mesh.vertices[vi].co)
        for mi in vertex_mats.get(vi, ()):
            mat = mesh.materials[mi] if mi < len(mesh.materials) else None
            if not mat or not any(kw in mat.name.lower() for kw in FACE_NORMAL_KW):
                continue
            n = accum.get((mi, key_pos))
            if n is not None and n.length_squared > 1e-16:
                normals[vi] = n.normalized()
                smoothed += 1
                break
    print('[normals] face weighted smooth:', smoothed, '/', len(mesh.vertices))
    return normals


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


def export_ply_game(filepath, mesh_obj, arm_obj, alpha_mats=None):
    """导出游戏原生 per-vertex ply。

    alpha_mats: 可选 set, 材质名 → 需要 MESH_FLAG_ALPHA(0x0002)。
    传 None 时用旧关键字兜底 (_material_needs_alpha_flag)。
    0x0002 位语义经 MOWAS2 13533 个 ply + GOH humanskin 122 个 ply 扫描实证:
    - blend 半透明材质 → 带 0x0002 (2b/meihong/alice flags 0x0C16/0x0C17,
      GOH 66 例 0x0C16);
    - test 镂空材质 → 不带 (GOH 304 例 0x0C15);
    - none → 不带 (0x0C14)。
    """
    mesh = mesh_obj.data
    mesh.calc_loop_triangles()
    loop_tris = mesh.loop_triangles
    game_normals = _build_game_normals(mesh, loop_tris)
    uv_layer = mesh.uv_layers.active
    if uv_layer is None:
        raise RuntimeError(_("mowas2.err.no_uv_layer"))

    # Remove only the VolumeView parent's local attachment. The ancestor basis
    # transform is shared by skeleton and mesh at runtime and must stay in MDL.
    parent_inv, parent_name = _mesh_parent_local_inverse(arm_obj)
    mesh_to_ply = parent_inv @ mesh_obj.matrix_world
    normal_to_ply = mesh_to_ply.to_3x3().inverted().transposed()
    print('[export] mesh parent:', parent_name, '| mesh_to_ply:', mesh_to_ply)

    v_weights = []
    for v in mesh.vertices:
        gs = sorted(v.groups, key=lambda g: -g.weight)[:2]
        v_weights.append([g for g in gs if g.weight > 1e-6])

    # GEM2 的 UV 是 per-vertex，而 Blender 是 per-loop。旧代码把每个 mesh
    # 顶点的最后一个 loop UV 覆盖到全部面，Faelynn 有 6408 个多 UV 顶点，
    # 会把约 18.5% 三角形拉到错误纹理位置形成条带。这里按序列化后的
    # (源顶点, UV) 建索引记录；位置/法线/权重仍从同一源顶点读取。
    export_records = []
    record_index = {}
    tris_by_mat = [[] for _ in mesh.materials]
    source_tris_by_mat = [[] for _ in mesh.materials]
    for tri in loop_tris:
        out_indices = []
        for loop_index, vertex_index in zip(tri.loops, tri.vertices):
            uv = uv_layer.data[loop_index].uv.to_tuple()
            uv_bytes = pack_ff(float(uv[0]), float(uv[1]))
            key = (int(vertex_index), uv_bytes)
            out_index = record_index.get(key)
            if out_index is None:
                out_index = len(export_records)
                record_index[key] = out_index
                export_records.append((int(vertex_index), uv))
            out_indices.append(out_index)
        tris_by_mat[tri.material_index].append(tuple(out_indices))
        source_tris_by_mat[tri.material_index].append(tri)
    if len(export_records) > 65535:
        raise RuntimeError(_(
            "mowas2.err.unique_vertex_limit",
            vertices=len(export_records)))
    print('[export] indexed vertex+UV records:', len(mesh.vertices), '->',
          len(export_records))

    with open(filepath, 'wb') as f:
        f.write(b'EPLY')
        bounds = [mesh_to_ply @ Vector(corner) for corner in mesh_obj.bound_box]
        bb0 = Vector((min(v.x for v in bounds), min(v.y for v in bounds),
                      min(v.z for v in bounds)))
        bb1 = Vector((max(v.x for v in bounds), max(v.y for v in bounds),
                      max(v.z for v in bounds)))
        f.write(b'BNDS')
        f.write(pack_fff(*bb0))
        f.write(pack_fff(*bb1))

        f.write(b'SKIN')
        f.write(pack_I(len(mesh_obj.vertex_groups)))
        for vg in mesh_obj.vertex_groups:
            nb = vg.name.encode('ascii')
            f.write(pack_B(len(nb)))
            f.write(nb)

        tri_start = 0
        for mi, mat_tris in enumerate(tris_by_mat):
            f.write(b'MESH')
            f.write(pack_I(D3DFVF_XYZB2 | D3DFVF_NORMAL | D3DFVF_TEX1
                           | D3DFVF_LASTBETA_UBYTE4))
            f.write(pack_I(tri_start))
            f.write(pack_I(len(mat_tris)))
            tri_start += len(mat_tris)
            mat_name = mesh.materials[mi].name if mesh.materials[mi] else 'mat%d' % mi
            mesh_flags = (MESH_FLAG_TWO_SIDED | MESH_FLAG_LIGHT
                          | MESH_FLAG_SKINNED | MESH_FLAG_MATERIAL
                          | MESH_FLAG_SUBSKIN)
            if alpha_mats is not None:
                needs_alpha = mat_name in alpha_mats
            else:
                needs_alpha = _material_needs_alpha_flag(mat_name)
            if needs_alpha:
                mesh_flags |= MESH_FLAG_ALPHA
            f.write(pack_I(mesh_flags))
            mtl_name = mat_name + '.mtl'
            f.write(pack_B(len(mtl_name)))
            f.write(mtl_name.encode('ascii'))
            # ── 权重索引修复(问题4: 麻花+没脑袋根因) ──
            # 游戏约定: 权重字节 = 该 MESH palette 的槽位(0=无骨),
            # palette 值 = 1 基 SKIN 列表索引。zaomiao/medicgirl/vanilla 均如此
            # (zaomiao: byte4→palette[4]=5→SKIN[4]=foot1r; byte13→basis; byte24→head)。
            # 旧写法把 0 基 SKIN 索引直接当字节 + palette 缺前导 0 → 全错位。
            # 这里写全量 palette(所有骨骼 1 基 + 前导 0), 字节 = group+1, 与 vanilla 同款。
            palette_full = [0] + [u + 1 for u in range(len(mesh_obj.vertex_groups))]
            f.write(pack_B(len(palette_full)))
            f.write(bytes(palette_full))

        f.write(b'VERT')
        f.write(pack_I(len(export_records)))
        f.write(pack_H(40))
        f.write(b'\x07\x00')
        for source_index, uv in export_records:
            v = mesh.vertices[source_index]
            pos = mesh_to_ply @ v.co
            f.write(pack_fff(pos.x, pos.y, pos.z))
            gs = v_weights[source_index]
            if not gs:
                f.write(pack_f(1.0))
                f.write(pack_BBBB(0, 0, 0, 0))
            elif len(gs) == 1:
                f.write(pack_f(1.0))
                f.write(pack_BBBB(gs[0].group + 1, 0, 0, 0))
            else:
                w0 = gs[0].weight / (gs[0].weight + gs[1].weight)
                f.write(pack_f(w0))
                f.write(pack_BBBB(gs[0].group + 1, gs[1].group + 1, 0, 0))
            n = (normal_to_ply @ game_normals[source_index]).normalized()
            f.write(pack_fff(n.x, n.y, n.z))
            u, vv = uv
            f.write(pack_f(u))
            f.write(pack_f(1.0 - vv))

        f.write(b'INDX')
        f.write(pack_I(len(loop_tris) * 3))
        for mat_tris in tris_by_mat:
            for tri in mat_tris:
                f.write(pack_HHH(tri[0], tri[2], tri[1]))


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
      h) Head Further Fixing L754-760: Neck 0.925³ 等比 + Head ×0.85
         (v12.4 只做了 Head, 本次补 Neck);
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

    # 可选 ik_updown 区域缩放：GFA 的 UpperBody2 权重最终映射到
    # 目标 ik_updown。KK 默认不执行 GF2 整体缩放，因此开启后把
    # GFA 自动基准一起作用于 UpperBody2；MMD 已在上面的原生 GFA
    # 循环应用基准，所以这里只叠加面板倍率，避免重复缩放。
    if (_goh_ik_updown_enabled() and has(src, B_UB2)
            and has(tgt, 'ik_updown')):
        user_factor = _goh_ik_updown_multiplier()
        extra_factor = user_factor if not kk_mode else gfa_ik_base * user_factor
        if abs(extra_factor - 1.0) > 1e-7:
            moved += scale_bone(B_UB2, 1.0, extra_factor, 1.0)
        src['goh_ik_updown_scale_factor'] = float(gfa_ik_base * user_factor)
        print('[gfa] ik_updown 区域缩放: UpperBody2→ik_updown '
              'GFA基准=%.3f 倍率=%.3f 实际额外=%.3f'
              % (gfa_ik_base, user_factor, extra_factor))
    else:
        src['goh_ik_updown_scale_factor'] = 1.0
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
    # 0.925/0.85 是 GF2 头颈比例修正；KK/KKS 的头骨比例来自自身 PMX，
    # 不执行这一组固定系数，避免把已经刚性拟合的头颈再次压短。
    if not kk_mode:
        if B_NCK and has(src, B_NCK):
            moved += scale_local_uniform(B_NCK, 0.925)
            bpy.context.view_layer.update()
        if B_HD and has(src, B_HD) and B_NCK and has(src, B_NCK):
            neck_pos = ww(src, B_NCK)
            hb = src.pose.bones[B_HD]
            head_pos = ww(src, B_HD)
            v = head_pos - neck_pos
            new_head = neck_pos + v * 0.85
            d = new_head - head_pos
            if d.length > 1e-5:
                wm = src.matrix_world @ hb.matrix
                hb.matrix = src.matrix_world.inverted() @ (Matrix.Translation(d) @ wm)
                moved += 1
    else:
        print('[gfa-kk] skip GF2 fixed head/neck correction')
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
        # 肩宽倍率只允许改变 ShoulderC/肩帽几何。Arm/Elbow/Wrist 是目标动画
        # 的真实枢轴，绝不能绕世界中线缩放；否则 0.90 会把腕缩入前臂 10%。
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
        # 手链目标使用真实 palm 枢轴；肩宽倍率只改变肩帽。
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
    # v14b: normalize 会移动 UpperBody 子骨 (含 ShoulderC) → 重新对齐
    # ShoulderC → hand1 y/z (GFA 段10 L644-650 语义在 normalize 后重保;
    # 否则 GF2/MMD 多级肩骨 ShoulderC 停在 z≈32.8 vs 目标 28.97, akq3 实测)。
    # 可调肩宽：GOH 原版肩峰在 clavicle（y≈±1.8），hand1 是肩外侧
    # 小三角肌带；把 ShoulderC 的 y 目标按倍率向中线收窄即可匹配原版。
    _sh_scale = _goh_shoulder_scale()
    src['mowas2_shoulder_scale'] = float(_sh_scale)
    for side, hand_t in (('L', 'hand1l'), ('R', 'hand1r')):
        _shc = next((x for x in ('ShoulderC_' + side, 'Shoulder_' + side,
                                 'ShoulderSolo_' + side)
                     if x in src.pose.bones), None)
        if not _shc or hand_t not in tgt.pose.bones:
            continue
        _tp = (tgt.matrix_world @ tgt.pose.bones[hand_t].matrix).translation
        if abs(_sh_scale - 1.0) > 1e-6:
            _tp = _tp.copy()
            _tp.y *= _sh_scale
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
    #    肩宽倍率只作用于上面的 ShoulderC/肩帽。
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


def _uv_split_count_sim(me):
    """在 mesh 副本上真实执行与 uv_seam_split 相同的 UV 拆分, 返回拆分后顶点数。

    用副本仿真保证与真实 uv_seam_split 完全一致 —— KK 类模型 ~77% 的边是
    UV seam, split_edges 连锁分裂使最终顶点数 ≫ (顶点,UV) 组合数,
    (顶点,UV) 组合公式不可靠。减面循环用它做 u16 顶点硬约束的判据。
    """
    import bmesh as bm_mod
    tmp = me.copy()
    bm = bm_mod.new()
    bm.from_mesh(tmp)
    uvl = bm.loops.layers.uv.active
    if uvl is None:
        n = len(bm.verts)
    else:
        seams = [e for e in bm.edges if len(e.link_loops) == 2
                 and e.link_loops[0][uvl].uv != e.link_loops[1][uvl].uv]
        bm_mod.ops.split_edges(bm, edges=seams)
        n = len(bm.verts)
    bm.free()
    try:
        bpy.data.meshes.remove(tmp)
    except Exception:
        pass
    return n


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


def decimate_global(mesh, target_faces=21000, arm=None,
                    protect_face=True, protect_tight=True):
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
    4. 全局 COLLAPSE 迭代减到 target (u16 索引 → 总顶点 ≤65535 ⟺ 面 ≤~21845)。

    返回最终面数。适用于所有模型 (老 MMD 模型同样有效)。
    """
    import bmesh as bm_mod
    # 1. 多用户 data → 单用户 (幽灵共享引用, users 计数不可靠)
    mesh.data = mesh.data.copy()
    me = mesh.data

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
    #    E6.25: 硬约束 = 面数 ≤ target 且"真实 UV 拆分(副本仿真)"顶点 ≤ 65535
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
            est = _uv_split_count_sim(me)
            print('[decimate] faces %d <= target %d | uv-split(sim) %d (limit %d)'
                  % (f, target_faces, est, UV_LIMIT))
            if est <= UV_LIMIT:
                break
            # 面数达标但拆分后超限 → 继续微减 (每轮 ~5%)
            ratio = 0.95
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
    est_final = _uv_split_count_sim(me)
    print('[decimate] final: %d faces, uv-split(sim) %d (limit %d)'
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
    - 主体: COLLAPSE 减面到 target 余量 (u16 索引全局单块 VERT/INDX
      → 总顶点 ≤65535 ⟺ 面 ≤~21845);
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
    # E6.18: 减面前必须清 shape keys (KK/PMX 表情 morphs) —— 否则 DECIMATE
    # modifier 在带 shape key 的 mesh 上 apply 静默失败, 减面无效 (千咲 body 卡死)。
    if me.shape_keys:
        try:
            mesh.shape_key_clear()
            print('[decimate] shape keys cleared (morphs dropped)')
        except Exception:
            pass
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
    #     即使达不到 target_faces 也接受 (顶点硬限 65535 由 uv_seam_split 后检查)。
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


def uv_seam_split(mesh):
    """按 UV seam 拆分顶点（游戏 per-vertex 格式要求每顶点唯一 UV）。"""
    import bmesh as bm_mod
    me = mesh.data
    bm = bm_mod.new()
    bm.from_mesh(me)
    uvl = bm.loops.layers.uv.active
    seams = [e for e in bm.edges if len(e.link_loops) == 2
             and e.link_loops[0][uvl].uv != e.link_loops[1][uvl].uv]
    bm_mod.ops.split_edges(bm, edges=seams)
    bm.to_mesh(me)
    bm.free()
    me.update()
    return len(me.vertices)


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════
def _align_options_changed(mesh, src):
    """检测对齐期选项（脚部尺寸/肩宽倍率）是否与冻结时不同。

    脚部尺寸与肩宽倍率都作用在【对齐阶段】的网格/骨架上；网格一旦
    冻结，改这两个值不会生效（用户实测"没变化"）。返回 True 表示
    需要重新导入源模型重新对齐。
    """
    try:
        stored = mesh.get('mowas2_foot_scale')
        if stored is not None and abs(float(stored) - _goh_foot_scale()) > 1e-4:
            return True
    except Exception:
        pass
    try:
        stored = src.get('mowas2_shoulder_scale')
        if stored is not None and abs(float(stored) - _goh_shoulder_scale()) > 1e-4:
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
    src, mesh = import_pmx(pmx_path)
    root = src.parent if src.parent else src
    return src, mesh, root


def resolve_scene():
    """源=骨骼最多的骨架；目标=带 gem2_world_mats 的 GEM2 骨架；网格=绑在源上的 mesh。

    健壮化（2026 面板化）：允许 1 个骨架（只有源）+ 已冻结/未绑定网格的中间状态，
    报错信息改为面向普通用户的中文提示。"""
    arms = sorted([o for o in bpy.context.scene.objects if o.type == 'ARMATURE'],
                  key=lambda a: len(a.data.bones))
    if not arms:
        raise RuntimeError(_("mowas2.err.scene_no_armature"))
    tagged_targets = [a for a in arms if a.get('gem2_world_mats')]
    named_target = bpy.data.objects.get('skin_Armature')
    tgt = (named_target if named_target in tagged_targets
           else (tagged_targets[0] if tagged_targets else None))
    src = None
    if len(arms) >= 2:
        tgt = tgt if tgt else arms[0]
        candidates = [a for a in arms if a != tgt]
        src = max(candidates, key=lambda arm: len(arm.data.bones))
    elif len(arms) == 1:
        if tgt is None:
            raise RuntimeError(_("mowas2.err.scene_no_target"))
        src = arms[0]
    mesh = None
    if src is not None:
        for o in bpy.data.objects:
            if o.type == 'MESH':
                for mod in o.modifiers:
                    if mod.type == 'ARMATURE' and mod.object == src:
                        mesh = o
                        break
                if mesh:
                    break
    if mesh is None:
        # 已冻结/已绑定到目标的网格：找最大的 MESH（排除刚体小件）
        big = [o for o in bpy.data.objects if o.type == 'MESH'
               and len(o.data.vertices) > 500]
        if big:
            mesh = max(big, key=lambda o: len(o.data.vertices))
    if mesh is None:
        raise RuntimeError(_("mowas2.err.scene_no_mesh"))
    root = src.parent if src and src.parent else src
    return src, tgt, mesh, root


def import_pmx(filepath, types=None):
    """步骤1：mmd_tools 导入 PMX。返回 (arm_obj, mesh_obj)。"""
    types = types or {'MESH', 'ARMATURE', 'PHYSICS', 'DISPLAY', 'MORPHS'}
    bpy.ops.mmd_tools.import_model(
        filepath=filepath,
        types=types,
        scale=1.0,
        rename_bones=True,
        dictionary='INTERNAL',
        clean_model=True,
        remove_doubles=True,
        fix_ik_links=True,
    )
    arms = [o for o in bpy.data.objects if o.type == 'ARMATURE']
    arm = max(arms, key=lambda a: len(a.data.bones)) if arms else None
    big = [o for o in bpy.data.objects if o.type == 'MESH'
           and len(o.data.vertices) > 500]
    mesh = max(big, key=lambda o: len(o.data.vertices)) if big else None
    if arm is None or mesh is None:
        raise RuntimeError(_("mowas2.err.pmx_objects_missing",
                             file=os.path.basename(filepath)))
    return arm, mesh


def rebuild_frame0_rest(tgt):
    """帧0 rest 重建（E6 Step A 的轻量版）。

    .mdl 骨架 rest 在 y 上镜像（basis mirrorY = diag(1,-1,1) 空间），而 .anm 帧0
    （绑定空间，gb_com 官方人模同款）是**无镜像空间**。手动流程每次先跑 Step A 把
    skin_Armature rest 重建为帧0（foot1l y=+2.17），新流程直接用 mdl rest
    （foot1l y=-2.165）→ is_source_mirrored 误判 → 对齐 180° 消歧转反 → 前后反。

    这里对骨架整体做 Y 翻转（世界坐标左乘 diag(1,-1,1)），等价于去掉 basis mirrorY，
    验证与帧0参照偏差：body 0.0024 / hand1l 0.054 / palm3r 0.03 / foot1l 0.16（medicgirl
    与 idle_stand_1 动画文件的微小资产差异，可接受）。打上 mowas2_frame0_rest 标记，
    避免重复翻转（重复左乘会翻回去）。
    """
    if tgt.get('mowas2_frame0_rest'):
        return
    MIR = Matrix.Diagonal((1.0, -1.0, 1.0, 1.0))
    bpy.context.view_layer.objects.active = tgt
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        for eb in tgt.data.edit_bones:
            eb.matrix = MIR @ eb.matrix
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
    tgt['mowas2_frame0_rest'] = True
    print('[frame0] target rest rebuilt (Y-mirror removed):', tgt.name)


def build_target_from_mdl(mdl_path, name='skin_Armature'):
    """步骤2：从 GOH 皮肤 .mdl 构建目标骨架（GFA 定制 58 骨）。
    返回 armature 对象（带 gem2_world_mats 等属性）。
    rebuild_frame0_rest 必须保留 —— GOH 皮肤 mdl 的 rest 同样在 basis
    mirrorY 空间 (实测: 不 rebuild 时左右手反, hand_rot1l 落到 -Y 侧),
    与 MOWAS2 同款约定, 需要 Y 镜像转正。"""
    from . import mdl_io
    with open(mdl_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    root_bones, mesh_parent_name = mdl_io.parse_mdl(content)
    tgt = mdl_io.build_armature('skin', root_bones, mesh_parent_name, mdl_path)
    tgt.name = name
    rebuild_frame0_rest(tgt)   # 必需: 转正左右手 (mirrorY → 帧0 空间)
    tgt.select_set(True)
    bpy.context.view_layer.objects.active = tgt
    return tgt


def _ensure_bundled_target_variant(src, tgt, mesh, root, pmx_path=None):
    """Keep the bundled target skeleton consistent with the long-arm toggle.

    Older scenes often keep ``samples/goh_skin.mdl`` even after the long-arm
    option was enabled. That short template shares the same torso and shoulder
    with GFA but ends hand_rot1 about 2.6 units inward (agf_nijita ground truth),
    so fitting to it visibly recesses the hand into the forearm. Only replace
    the two known bundled templates; an explicitly selected custom MDL is never
    changed. Frozen meshes must be rebuilt from their PMX rather than stretched.
    """
    short_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'samples', 'goh_skin.mdl')
    long_path = GOH_DEFAULT_MDL
    desired = long_path if _goh_gfa_longarm() else short_path
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


def align_only(output_dir=None, ground_z=GROUND_Z, pmx_path=None):
    """仅对齐：把源模型整体刚性拟合到目标骨架（头/身/腿摆好、贴地、手臂垂落）。
    不绑骨、不减面、不导出 —— 供面板【步骤3 摆好头身腿】使用。
    若网格已冻结（跑过本步骤/完整管线），直接返回已保存快照，避免重复变换；
    但冻结后修改了脚部尺寸/肩宽倍率等对齐期选项时，会自动重新导入源模型
    重新对齐（否则新值不生效）。"""
    if not output_dir:
        output_dir = OUT_DEFAULT
    src, tgt, mesh, root = resolve_scene()
    src, tgt, mesh, root, _target_changed = _ensure_bundled_target_variant(
        src, tgt, mesh, root, pmx_path=pmx_path)
    # 旧场景兼容：目标骨架若缺就绪标记 → 执行 rebuild (mirrorY 转正,
    # GOH 皮肤 mdl 同款约定, 实测必需否则左右手反)。
    if tgt is not None and not tgt.get('mowas2_frame0_rest'):
        rebuild_frame0_rest(tgt)
    if mesh.get('mowas2_frozen'):
        if _align_options_changed(mesh, src):
            if not pmx_path or not os.path.isfile(pmx_path):
                raise RuntimeError(_("mowas2.err.align_changed_requires_pmx"))
            print('[reimport] 对齐选项已修改 (脚部/肩宽), 重新导入并重新对齐')
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
    forearm_result = goh_retarget_mmd_forearm_geometry(
        mesh, src, tgt, mirrored=mirrored, source_mode=mode)
    if forearm_result.get('changed'):
        print('[5.1] MMD forearm/wrist geometry:', forearm_result['changed'])
    # GFA 后任一源分支都可能改变脚底；归一化只读取源 ankle 权重，
    # 不改变 KK/MMD 的骨骼对齐分支。
    normalize_foot_geometry(mesh, src, tgt, ground_z)
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


def export_all(mesh, tgt, output_dir=None, skin_name='skin',
               texture_format='TGA', nvtt_path=None, toon_shader=True):
    """导出 ply/mdl/mtl，并按用户选择生成 TGA 或 DDS 贴图。

    skin_name: 游戏 Entity 资源名；输出 <name>/<name>.def/.mdl/.ply。

    E6.29 抽为独立函数 —— 网格已完成绑定/减面/UV拆分后, 可不重跑全流程
    单独重做导出 (bind_and_transfer 不幂等: 重复调用会叠第二个 Armature
    修改器且按 PMX 组名读不到权重 → 已处理网格上严禁重跑 run_full)。
    """
    if not output_dir:
        output_dir = OUT_DEFAULT
    skin_name = _validate_entity_name(skin_name)
    texture_format = str(texture_format or 'TGA').upper()
    if texture_format not in {'TGA', 'DDS'}:
        raise ValueError(_("mowas2.err.texture_format_unsupported",
                           format=texture_format))
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
    sanitize_materials()
    out_sub = _entity_output_dir(output_dir, skin_name)
    _assert_output_does_not_delete_source_textures(out_sub, mesh)
    os.makedirs(out_sub, exist_ok=True)
    # 清理旧材质与贴图，避免切换 TGA/DDS 后两个扩展名同时存在。
    for _f in os.listdir(out_sub):
        if _f.lower().endswith(('.mtl', '.tga', '.dds', '.png', '.bmp',
                                '.jpg', '.jpeg')):
            cleanup_path = os.path.join(out_sub, _f)
            try:
                os.remove(cleanup_path)
            except OSError as exc:
                raise RuntimeError(_(
                    "mowas2.err.cleanup_failed", path=cleanup_path,
                    error=exc)) from exc
    # PLY 在贴图处理后写入：TGA/DDS 共用的 alpha plan 决定每个 MESH 的
    # MESH_FLAG_ALPHA, 与 mtl 的 blend 重写同一套判定 (修复旧版两侧不一致:
    # 眉毛/眼线/瞳孔 mtl=blend 但 ply 无 0x0002 → 游戏里 alpha 被忽略)。

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
        content = raw.decode('gbk')
    except UnicodeDecodeError:
        content = raw.decode('utf-8', errors='replace')
    # Preserve the target MDL verbatim except for the VolumeView filename.
    # export_ply_game removes only that node's local attachment; basis/body
    # ancestor transforms remain here so mesh and skeleton receive them once.
    content, volume_count = re.subn(
        r'\{VolumeView "[^"]*"\}',
        '{VolumeView "%s.ply"}' % skin_name, content, count=1)
    if volume_count != 1:
        raise RuntimeError(_("mowas2.err.volume_view_missing"))
    with open(os.path.join(out_sub, skin_name + '.mdl'), 'wb') as f:
        f.write(content.encode('gbk', errors='replace'))
    with open(os.path.join(out_sub, skin_name + '.def'), 'w', encoding='utf-8') as f:
        f.write('{game_entity\n\t{Extension "%s.mdl"}\n}\n' % skin_name)

    # 材质循环: 写初始 mtl (blend none 占位, [8.5] 按 plan 重写) + 拷贴图。
    mat_diffuse = {}   # 材质名 -> diffuse 贴图名(去扩展名), 供 ply alpha_mats
    for mat in mesh.data.materials:
        if not mat:
            continue
        tex = None
        if mat.use_nodes and mat.node_tree:
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image and node.image.filepath:
                    absp = bpy.path.abspath(node.image.filepath)
                    if absp and os.path.isfile(absp):
                        tex = absp
                        break
        diffuse = os.path.splitext(os.path.basename(tex))[0] if tex else mat.name
        # 注意: 这里【不】调用 _tex_has_alpha —— 那会对每张贴图 bpy.data.images.load()
        # 在 GUI 下同步加载 35 张大图会慢到像卡死；alpha/blend 由 [8.5] 统一检测重写。
        mat_diffuse[mat.name] = diffuse
        with open(os.path.join(out_sub, mat.name + '.mtl'), 'w', encoding='utf-8') as f:
            f.write('{material simple\n\t{diffuse "%s"}\n\t{blend none}\n}\n' % (diffuse,))
        if tex:
            dest = os.path.join(out_sub, os.path.basename(tex))
            if not os.path.isfile(dest):
                shutil.copyfile(tex, dest)

    print('[8] exported ->', out_sub)

    # 8.5 用户可选内置 TGA 或外部 NVTT DDS；两者复用完全相同的 alpha
    # 分类、黑底填充、MTL 重写和 PLY flag 判定。
    try:
        mode = mesh.get('mowas2_source_mode') or detect_source_mode(tgt=tgt)
        if texture_format == 'DDS':
            plan = convert_textures_to_dds(
                out_sub, mode=mode, nvtt_path=nvtt_path,
                toon_shader=toon_shader)
        else:
            plan = convert_textures_to_tga(
                out_sub, mode=mode, toon_shader=toon_shader)
        print('[8.5] textures ->', texture_format, 'OK, plan:', len(plan),
              '| mode:', mode,
              '| alpha:', 'test' if _goh_alpha_test_mode() else 'blend-aware')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise RuntimeError(_(
            "mowas2.err.texture_export_failed", format=texture_format,
            error=exc)) from exc

    if toon_shader:
        apply_toon_shader_conversion(out_sub)
    else:
        print('[toon] disabled: keep material simple')

    # PLY 写入: MESH_FLAG_ALPHA 与 mtl blend 同源 (plan)。
    # 2026-08-17 晚 (GOH 对照): 0x0002 只给 blend 半透明材质; test 镂空材质
    # 不带 (GOH 304 例 test 全为 0x0C15 无 0x0002)。
    # 同一贴图可能被多个材质引用 (如 bodytights_0..6 共用一张), 由材质名映射。
    alpha_mats = {m for m, d in mat_diffuse.items() if plan.get(d) == 'blend'}
    if plan:
        print('[8.6] alpha MESH (MESH_FLAG_ALPHA):', sorted(alpha_mats))
    export_ply_game(os.path.join(out_sub, skin_name + '.ply'), mesh, tgt,
                    alpha_mats=alpha_mats)

    print('=' * 60)
    return out_sub


def run_full(output_dir=None, ground_z=GROUND_Z, protect_face=True,
             protect_tight=True, enable_decimate=False, skin_name='skin',
             pmx_path=None, texture_format='TGA', nvtt_path=None,
             toon_shader=True):
    """完整移植 (对齐/绑定/减面可选/导出)。

    2026-08-17 晚: enable_decimate 默认 False —— 用户流程默认不减面
    (KK/pmx 顶点数通常 <65535, 直接导出; 减面是可选优化, 需手动开启)。
    skin_name: GOH Entity 资源名，输出 <name>/<name>.def/.mdl/.ply。
    """
    if not output_dir:
        output_dir = OUT_DEFAULT
    skin_name = _validate_entity_name(skin_name)
    os.makedirs(output_dir, exist_ok=True)
    src, tgt, mesh, root = resolve_scene()
    src, tgt, mesh, root, _target_changed = _ensure_bundled_target_variant(
        src, tgt, mesh, root, pmx_path=pmx_path)
    # 旧场景兼容：目标骨架若缺就绪标记 → 执行 rebuild (mirrorY 转正,
    # GOH 皮肤 mdl 同款约定, 实测必需否则左右手反)。
    if tgt is not None and not tgt.get('mowas2_frame0_rest'):
        rebuild_frame0_rest(tgt)
    print('=' * 60)
    print('GOH PMX→GEM2 pipeline v2 (single-ply)')
    print('  src:', src.name, '| tgt:', tgt.name, '| mesh:', mesh.name)

    # 状态检测: mesh 是否已冻结 (freeze_mesh 后无 ARMATURE modifier / 已绑到 tgt)
    already_frozen = mesh.get('mowas2_frozen') or (
        not any(m.type == 'ARMATURE' for m in mesh.modifiers)
        and mesh.parent == tgt)
    # 冻结后修改脚部尺寸/肩宽倍率 → 冻结快照不含新值, 必须重新导入对齐
    # (否则用户改选项"没变化")。
    if already_frozen and _align_options_changed(mesh, src):
        if not pmx_path or not os.path.isfile(pmx_path):
            raise RuntimeError(_("mowas2.err.align_changed_requires_pmx"))
        print('[reimport] 对齐选项已修改 (脚部/肩宽), 重新导入并重新对齐')
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

        # 1.5 清理: 删除与 mesh 共享 data 的重复对象 + 清形态键。
        #     KK 类模型 mmd_tools 导入可能产生 100+ 个共享同一 mesh data 的对象
        #     (千咲 users=149) → 多用户 data 无法 apply modifier, DECIMATE 异常;
        #     形态键 (morphs) 会阻止 modifier apply, 且减面后无法保留 → 全删。
        for o in list(bpy.data.objects):
            if o is mesh and o.type == 'MESH':
                continue
            if o.type == 'MESH' and o.data == mesh.data:
                bpy.data.objects.remove(o, do_unlink=True)
        if mesh.data.shape_keys:
            try:
                mesh.shape_key_clear()
            except Exception:
                pass
            print('[1.5] shape keys cleared')
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

    # 7. 可选全局 COLLAPSE 减面。关闭时保留当前绑定后的原始几何，
    # 交给 indexed export 按 position/normal/UV/weight 共享顶点；不执行
    # UV seam split，避免为了旧的 loop 顶点限制而修改原模型拓扑。
    if enable_decimate:
        nf = decimate_global(mesh, target_faces=21000, arm=tgt,
                             protect_face=protect_face,
                             protect_tight=protect_tight)
        nv = uv_seam_split(mesh)
        print('[7] decimated:', nf, 'faces | uv-split:', nv,
              'verts (limit 65535)')
        if nv > 65535:
            raise RuntimeError(_(
                "mowas2.err.decimated_vertex_limit", vertices=nv))
    else:
        nf = len(mesh.data.polygons)
        nv = len(mesh.data.vertices)
        print('[7] decimation skipped:', nf, 'faces | raw verts:', nv,
              '| indexed exporter will enforce the 65535 unique-vertex limit')

    # 8.5 export (E6.29 抽为 export_all, 便于不重跑绑定/减面单独重做导出)
    return export_all(
        mesh, tgt, output_dir, skin_name=skin_name,
        texture_format=texture_format, nvtt_path=nvtt_path,
        toon_shader=toon_shader)


class MOWAS2_OT_AutoPipeline(bpy.types.Operator):
    bl_idname = "gem2.mowas2_auto_pipeline"
    bl_label = _("mowas2.op.pipeline.label")
    bl_description = _("mowas2.op.pipeline.desc")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            _restore_mowas2_settings(context.scene, force=False)
            props = getattr(context.scene, 'mowas2_props', None)
            if props is None:
                out = run_full()
            else:
                out = run_full(
                    props.output_dir, props.ground_z,
                    protect_face=props.protect_face,
                    protect_tight=props.protect_tight,
                    enable_decimate=props.enable_decimate,
                    skin_name=props.skin_name or 'skin',
                    pmx_path=props.pmx_path,
                    texture_format=props.texture_format,
                    nvtt_path=props.nvtt_path or None,
                    toon_shader=props.toon_shader)
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
    'pmx_path', 'mdl_path', 'output_dir', 'texture_format', 'nvtt_path',
    'toon_shader', 'ground_z', 'protect_face', 'protect_tight',
    'enable_decimate', 'skin_name', 'goh_hand_split', 'goh_gfa_longarm',
    'goh_enlarge_head', 'goh_head_scale', 'goh_alpha_test',
    'goh_ik_updown_scale', 'goh_ik_updown_multiplier', 'goh_foot_scale',
    'goh_shoulder_scale', 'goh_torso_ik_merge', 'goh_iklr_keep',
    'goh_hand_clamp', 'goh_wrist_stitch', 'goh_finger_curl',
)
_mowas2_settings_restore_depth = 0


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
        props.settings_initialized = True
    finally:
        _mowas2_settings_restore_depth -= 1


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
    pmx_path: bpy.props.StringProperty(
        name=_("mowas2.prop.pmx"), subtype='FILE_PATH',
        description=_("mowas2.prop.pmx.desc"),
        default="", update=_mowas2_setting_updated)
    mdl_path: bpy.props.StringProperty(
        name=_("mowas2.prop.mdl"), subtype='FILE_PATH',
        description=_("mowas2.prop.mdl.desc"),
        # GOH 版: 默认指向插件 samples 内 GFA 长臂模板 (goh_skin_gfa.mdl,
        # 前臂/手与 GOH 动画一致); 用户可自行选择任意 GFA/GOH 皮肤 .mdl。
        default=(GOH_DEFAULT_MDL if GOH_GFA_LONGARM else
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'samples', 'goh_skin.mdl')),
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
        default=True, update=_mowas2_setting_updated)
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
    # GOH 原生透明材质默认 alpharef 127 + blend test；关闭后恢复
    # 旧版 blend 分类，便于用户在同一模型上做 A/B 对照。
    goh_alpha_test: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_alpha_test"),
        description=_("mowas2.prop.goh_alpha_test.desc"),
        default=GOH_ALPHA_TEST_TRANSPARENT,
        update=_mowas2_setting_updated)
    # UpperBody2 的权重映射到目标 ik_updown；启用后按 GFA 自动基准
    # WidthExtraScaling_PerStep**0.5 再乘下面的可调倍率。
    goh_ik_updown_scale: bpy.props.BoolProperty(
        name=_("mowas2.prop.goh_ik_updown_scale"),
        description=_("mowas2.prop.goh_ik_updown_scale.desc"),
        default=GOH_IK_UPDOWN_SCALE, update=_mowas2_setting_updated)
    goh_ik_updown_multiplier: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_ik_updown_multiplier"),
        description=_("mowas2.prop.goh_ik_updown_multiplier.desc"),
        default=GOH_IK_UPDOWN_MULTIPLIER,
        min=0.5, max=1.8, soft_min=0.8, soft_max=1.3,
        update=_mowas2_setting_updated)
    # 脚部尺寸倍率：脚/鞋在贴地归一化时按目标 ankle 锚点缩放，
    # 脚底始终钳在地面，不会陷地。1.0 保持旧的贴地压缩行为。
    goh_foot_scale: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_foot_scale"),
        description=_("mowas2.prop.goh_foot_scale.desc"),
        default=1.0, min=0.7, max=2.0, soft_min=0.8, soft_max=1.6,
        update=_mowas2_setting_updated)
    # 肩宽倍率：GOH 原版肩峰在 clavicle（y≈±1.8），hand1 是肩外侧
    # 小三角肌带；<1 把肩线向中线收窄匹配原版，1.0 保持当前行为。
    goh_shoulder_scale: bpy.props.FloatProperty(
        name=_("mowas2.prop.goh_shoulder_scale"),
        description=_("mowas2.prop.goh_shoulder_scale.desc"),
        default=1.0, min=0.8, max=1.3, soft_min=0.85, soft_max=1.1,
        update=_mowas2_setting_updated)
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
            arm, mesh = import_pmx(self.filepath)
            props = context.scene.mowas2_props
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
        if props.mdl_path and os.path.isfile(props.mdl_path):
            self.filepath = props.mdl_path
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        try:
            _restore_mowas2_settings(context.scene, force=False)
            if not self.filepath or not os.path.isfile(self.filepath):
                self.report({'ERROR'}, _("mowas2.err.select_mdl"))
                return {'CANCELLED'}
            tgt = build_target_from_mdl(self.filepath)
            props = context.scene.mowas2_props
            props.mdl_path = self.filepath
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

    def execute(self, context):
        try:
            _restore_mowas2_settings(context.scene, force=False)
            props = context.scene.mowas2_props
            out = run_full(props.output_dir, props.ground_z,
                           protect_face=props.protect_face,
                           protect_tight=props.protect_tight,
                           enable_decimate=props.enable_decimate,
                           skin_name=props.skin_name or 'skin',
                           pmx_path=props.pmx_path,
                           texture_format=props.texture_format,
                           nvtt_path=props.nvtt_path or None,
                           toon_shader=props.toon_shader)
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

        # 1. 导入 PMX
        box = layout.box()
        box.label(text=_("mowas2.step1.label"), icon='IMPORT')
        box.prop(props, "pmx_path", text="")
        box.operator("gem2.mowas2_import_pmx", text=_("mowas2.step1.import"),
                     icon='FILE_TICK')

        # 2. 构建目标骨架
        box = layout.box()
        box.label(text=_("mowas2.step2.label"), icon='ARMATURE_DATA')
        box.prop(props, "mdl_path", text="")
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
        box.prop(props, "toon_shader")
        if props.toon_shader:
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
        # GOH 原生透明材质对照开关：默认 test，关闭可回到旧 blend。
        box.prop(props, "goh_alpha_test")
        # UpperBody2 权重最终进入目标 ik_updown，默认关闭以保持旧结果。
        box.prop(props, "goh_ik_updown_scale")
        row = box.row()
        row.enabled = bool(props.goh_ik_updown_scale)
        row.prop(props, "goh_ik_updown_multiplier")
        # 脚部尺寸可调：>1 放大脚/鞋（贴地不陷），<1 收窄。
        box.prop(props, "goh_foot_scale")
        # 肩宽可调：<1 匹配 GOH 原版 clavicle 肩宽，1.0 保持当前。
        box.prop(props, "goh_shoulder_scale")
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
        sub.prop(props, "enable_decimate")
        sub.prop(props, "protect_face")
        sub.prop(props, "protect_tight")

        # 载具 (vehicle)
        box = layout.box()
        box.label(text=_("mowas2.vehicle.label"), icon='OPTIONS')
        box.label(text=_("mowas2.vehicle.desc"), icon='DOT')
        box.operator("gem2.mowas2_import_vehicle_folder",
                     text=_("mowas2.vehicle.import"), icon='FILE_FOLDER')
        box.operator("gem2.mowas2_export_vehicle_folder",
                     text=_("mowas2.vehicle.export"), icon='EXPORT')

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
           MOWAS2_OT_ImportPMX,
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
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception as e:
            print('[MOWAS2] FAIL register %s: %s' % (cls.__name__, e))
    try:
        bpy.types.Scene.mowas2_props = bpy.props.PointerProperty(
            type=MOWAS2_SceneProps, options={'SKIP_SAVE'})
        _register_mowas2_settings_handlers()
        _restore_all_mowas2_settings(force=True)
    except Exception as e:
        print('[MOWAS2] FAIL scene prop/settings:', e)


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
