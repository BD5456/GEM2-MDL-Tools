# GEM2 GOH Tools (Blender Addon)

GOH（Call to Arms - Gates of Hell，战争之人：地狱之门）资源的 Blender 导入/导出插件，
基于 GEM2 引擎（与 MOWAS2 同源）。PMX/MMD 模型 → GOH 士兵皮肤（humanskin）自动管线，
目标骨架 = GFA 定制骨架（GOH 官方魔改版，58 骨，含 visor/placement/ik_chain 等）。
A Blender addon for importing/exporting GEM2 Engine assets for Call to Arms - Gates of Hell,
with a PMX/MMD → GOH soldierskin pipeline based on the GFA custom skeleton (58 bones).
Аддон Blender для импорта/экспорта ресурсов движка GEM2 для Call to Arms - Gates of Hell,
с автоматическим конвейером PMX/MMD → скин солдата GOH на базе кастомного скелета GFA (58 костей).

> 独立于 GEM2 Engine Tools (MOWAS2 版) 的 GOH 专用分支，两者互不影响。
> Standalone GOH branch, independent of the MOWAS2 edition. / Отдельная GOH-ветка, независимая от MOWAS2-версии.

---

## GOH 版说明 / GOH Edition Notes / Примечания к GOH-версии

- 目标骨架默认 = `samples/goh_skin.mdl`（GFA 定制骨架，源自 GOH mod 皮肤 agit_yelan.mdl，58 骨）。
- 输出默认 = `skin.ply / skin.mdl / skin.def`（GOH 皮肤惯例：def/mdl 用皮肤名、ply 用 skin.ply）。
- 贴图：GOH 引擎支持 TGA（mod 实测 2576 个 TGA），沿用 PNG→TGA 32bpp 无损转换。
- 透明材质分类、MMD 帧0 分支等与 MOWAS2 版一致（GEM2 引擎同源）。

---

## 更新说明 / Changelog / Журнал

### 130 版（2026-08-18）// 手部错乱/胯下/陷地 三问题修复

1. **手部错乱根因 = A1/A2 朝向约定错误**：GOH 原版皮肤 skin 骨是 **A2**
   约定（agit_yelan Orientation (0,-1,0/1,0,0/0,0,1)，goh_skin.mdl
   Matrix34 (0,1,0/-1,0,0/0,0,1) 同族）。此前误用 MOWAS2 的 A1
   (x,-z,y) → 手部顶点方向翻转（左右手 x 对调）→ 手指/手掌错乱。
   已改：`export_ply_game.skin_inv` = A2 逆 (y,-x,z)，`export_all`
   不再替换 skin 骨矩阵（保留原 A2）。raw ply 对比：左右手 x 符号与
   GOH 原版一致。
2. **手部流程简化**：移除 goh_retarget_palm（GFA 从不压缩/拉长手指，
   顶点重映射是过度工程）；goh_align_fingers 重写为 GFA AutoRotateFinger
   （局部轴探测 + 固定角度卷曲：拇指 20°/四指 25°×系数，末节 ×0.75
   + 手腕姿态角 ±35/±5/-15）。
3. **胯下两坨**：新增 `goh_crotch_fix`——骨盆中线顶点 foot1L/R 权重并入
   body（原版胯下 283 双挂顶点平衡，我们 0 双挂）。
4. **脚部陷地**：新增 `goh_final_ground`——导出前脚底拉回 -0.07
   （retarget_leg_segments 移动脚掌导致 -2.6 陷地）。
5. MOWAS2 版完全不动。

### 129 版（2026-08-18）// GOH .anm 动画支持

1. **GOH .anm 与 MOWAS2 字节级同构**（EANM/FRMS/BMAP/FRM2 + 相同 25 骨名 +
   0x04 位=左右脚镜像元数据直接应用），现有 .anm 导入/导出/播放直接支持
   GOH 动画，零格式适配。
2. **新增「提取 GOH 动画」**（面板 GOH 区 + `gem2.extract_goh_anm` 算子）：
   从 `<GOH>/resource/properties.pak`（zip）按关键词一键提取 .anm——
   GOH 人形动画 1599 个在 pak 内 `properties/animation/human/`，无需手动
   解包 4GB pak。实测：idle 143/walk 70/stand 338/fire 102/reload 84/
   sprint 10/crouch 60。
3. 测试流程：提取（如关键词 `gun_aside`）→ 导入模型 → File>Import>GEM2 ANM
   → 播放看持枪/背枪手部 IK/FK。
4. GFA 的 GOHAnimation.py 是 Max 动画制作/编辑工具（FK 编辑 .anm），
   Blender 播放用现有 anm_io；以后做动画制作时再参考其四元数插值。

### 128 版（2026-08-18）// GFA 全流程收工确认

1. **骨架层级最终验证**：goh_skin.mdl 55 骨 vs GFA SL_GOH_SKE_Lib
   Construction 字典 45 骨逐项比对全部一致（仅根链 basis 命名差异，同构）；
   导出 mdl 的手部 IK/FK 链（palm_ik_holder→ik_chain07/08）、机械骨
   （gun_back/placement/foresight2rot/visor）层级全部正确 → 动画可正常驱动。
2. **GFA 剩余文件全部核对**：Step0/4 名字前缀（Blender 无需）、Step1 铸造
   （bone_casting 已实现）、MaterialTools（MTL/DDS/批量部署=mod 侧）、
   GOHAnimation/SKE Lib（.anm 动画制作环节，非移植）、MaxBatchScripts
   （Max 壳）——均确认无需搬运。
3. **收工**：GFA 模型移植流程全部搬运完成。剩余独立课题 = GOH .anm 动画
   制作（GOHAnimation.py 四元数 + IK/FK 链），待需要时再做。

### 127 版（2026-08-18）// GFA 全流程差距搬运（头部/肩部/手指角度/腕部姿态）

按 GFA 全流程盘点差距，本轮接入：
1. **头部归一化+微调**（`goh_normalize_head`，GFA Step0.5+Step2）：
   用 GFA 固定期望比例（EyeLR 0.04168/EyeNeck 0.10708）缩放源 Neck，
   再 Neck×0.925 + Head 0.85 压缩 → 头部大小匹配 GOH 骨架比例。
   标准 MMD 生效；KK 源无 ShoulderP/Head 位置骨自动跳过。
2. **肩部微调**（`goh_shoulder_fix`，GFA Arm Further Fixing）：左 +3° XY
   +1.25° YZ / 右 -1.25° YZ。
3. **手指角度卷曲**（`goh_align_fingers` 扩展，GFA AutoRotateFinger）：
   方向对齐后追加四指 25°×系数（Index 1.0/Middle 1.3/Ring 1.4/Little 1.375，
   末节×0.75）+ 拇指 20°。
4. **手腕姿态角**（GFA Step2 FK）：Wrist ±35° XY / ±5° YZ / -15° XZ。
5. 盘点结论：腿部旋转对齐（Umeyama 已覆盖）、Step0/4 名字前缀（Blender
   无需）、gfa_align 完整对齐（v1 教训弃用）——这三项不需要搬。
6. MOWAS2 版完全不动。

### 126 版（2026-08-18）// 腿部比例对齐 GOH 骨架

1. **腿部比例重映射**（`retarget_leg_segments`）：GOH 定制骨架大小腿比≈1:1
   （大腿 9.38/小腿 9.21），源 MMD/KK 小腿比例普遍偏长（前鬼坊 1.37）。
   此前只做整体缩放 → 绑定后踝关节错位，走路/跑步小腿脚踝拉扯。
   现沿腿轴把大腿段→foot1/foot2、小腿段→foot2/foot3 分段重映射，
   脚掌跟随新踝位 → 腿部顶点与 GOH 骨架比例一致。
2. GFA 的 NewSkeletonTransform 比例已内置于 goh_skin.mdl，无需重复；
   插件只需把源网格顶点重映射到目标骨架比例。
3. MOWAS2 版完全不动。

### 125 版（2026-08-18）// 权重形态对齐 GOH 原版皮肤（握枪/腰部/小腿）

1. **手指保留长度**（`goh_retarget_palm` TIP_EXTEND=1.35）：指根→指尖段不再完全
   压缩到 palm3，指尖超出 palm3 35% —— 手指不再被拉短，握枪仍正常。
2. **躯干权重对齐原版**：GOH 原版皮肤 ik_leftright 几乎不用（0-3.5%），躯干旋转由
   ik_updown 主导。分指模式把 ik_leftright 并入 ik_updown（保留 5% 余量）→
   腰部 body↔ik_updown 平滑过渡，不再出现大块 ik_leftright 区。
3. **脚踝权重对齐原版**：原版 foot2↔foot3 混合仅 3-11% 窄过渡；消除 GFA 表
   Ankle 0.5/0.5 产生的 40% 五五开顶点（按 z 高度收窄过渡带）→ 小腿动作正常。
4. MOWAS2 版完全不动；映射表保持 GFA 原样（形态归一在 bind 后处理）。

### 124 版（2026-08-18）// 标准 MMD 源兼容修复

1. **手指骨名兼容**（`_finger_bones`）：KK/KKS 有完整 3 节手指
   （IndexFinger1/2/3 + Thumb0/1/2），标准 MMD/崩3（如前鬼坊天狗）只有 2 节
   （IndexFinger1/2 + Thumb0/1，无 Finger3/Thumb2）。此前 `goh_retarget_palm`
   硬编码 `MiddleFinger3_L` → 标准 MMD 崩溃 KeyError。现统一降级解析
   （指根→指尖取存在的节），`goh_align_fingers`/`pose_hands`/`goh_retarget_palm`
   全部兼容 2 节与 3 节模型。
2. 验证：前鬼坊天狗完整管线跑通（分指+手指伸展+导出），KK 源不受影响。

### 123 版（2026-08-18）// GFA 手部二次精修（IK/FK 对不上根因修复）

1. **SKIN 骨顺序 = GOH 原版/GFA 顺序**：`hand_rot1l → head → … → palm1r/2r/3r → palm1l/2l/3l`
   （此前沿用 MOWAS2 顺序 head 在最后，现导出 SKIN 块与 GOH 原版皮肤逐字节一致）。
2. **手部顶点沿 palm 链伸展**（`goh_retarget_palm`）：此前 `lift_hand` 把手部质心拉向
   palm1 掌心骨 → 手指缩回掌心（顶点方向 len 0.076 vs 原版 1.224）→ 动画驱动 palm2/palm3
   时手指不跟随（IK/FK 不如原版）。分指模式弃用 lift_hand，改沿源手指方向按链段比例
   重映射到目标 palm 链（掌心保持、掌中→指根拉伸、指根→指尖压缩），手形不变。
   实测顶点方向 len 0.076→0.773，距 palm3 1.789→1.131（与原版同量级）。
3. **Thumb0/1 严格按 GFA**（拇指 100% palm1）；`transfer.py` 旧模板表手部同步 GFA 分指。
4. MOWAS2 版（gem2_mdl_tools）仍完全不动；GOH 版「手部分指(GFA)」开关可回退单骨。

### 122 版（2026-08-18）// GFA 手部精修

1. **手部权重恢复 GFA 分指**（GOH 默认开启，开关「手部分指(GFA)」在高级区）：
   GOH 原版皮肤（agit_yelan/agf_feitusa）手部是 palm1/palm2 分指混合权重（0.53/0.47，
   93% 顶点多骨），此前插件继承 MOWAS2 的「手部强制 palm1 单骨」把手指焊接成一块，
   导致 GOH 动画驱动 palm2/palm3 时手指不跟随 → 手部 IK/FK 对不上。
   现按 GFA 权重表（Step3_TransferWeightFinal）逐条移植 palm1/palm2/palm3 分指
   （Finger1: 0.50/0.425/0.075 … Finger3: 0.50/0.39/0.11，拇指 0.975/0.95/0.9+0.1），
   并修正 GFA 原表 R 侧两处笔误（Finger1_R 第三项→Palm3R；Thumb0_R 对称 palm1r/palm2r）。
2. **GOH 手指 FK 对齐**（`goh_align_fingers`）：四指指根→目标 palm1→palm2 方向、
   中节→palm2→palm3 方向（cap 40°）、拇指→palm1 侧向（cap 20°），使源张开直指
   朝向 GOH 半握 rest，配合分指权重手指才能正确卷曲。
3. MOWAS2 版（gem2_mdl_tools）完全不动；GOH 版可通过开关回退「MOWAS2 单骨」兼容。

### 121 测试版更新说明 / Update 121 Test Build / Тестовое обновление 121

### 中文

1. 修复 alpha 贴图的透明区域显示黑色：`panst`、袜子等 alpha 材质对应的 PLY MESH 段现在会写入 `MESH_FLAG_ALPHA`。
2. `bodytights` 紧身衣继续按 `{blend none}` 做不透明实验；头发、鞋靴和腿甲也保持 `{blend none}`。
3. `panst` 继续使用 `{blend blend}`，袜子等镂空材质使用 `{blend test}`。

### English

1. Fixed alpha textures showing black in transparent areas: PLY MESH sections for alpha materials such as `panst` and socks now include `MESH_FLAG_ALPHA`.
2. `bodytights` remains `{blend none}` for the opaque-material test; hair, shoes/boots, and leg armor remain `{blend none}`.
3. `panst` continues to use `{blend blend}`, while cutout materials such as socks use `{blend test}`.

### Русский

1. Исправлено отображение чёрного в прозрачных областях alpha-текстур: секции MESH PLY для alpha-материалов, например `panst` и носков, теперь получают `MESH_FLAG_ALPHA`.
2. `bodytights` остаётся `{blend none}` для проверки непрозрачного материала; волосы, обувь и поножи также остаются `{blend none}`.
3. `panst` продолжает использовать `{blend blend}`, а материалы с вырезами, например носки, используют `{blend test}`.

## 115 更新说明 / Update 115 / Обновление 115

### 中文

1. 更新载具导入/导出功能（位于 N 键菜单；初版，尚未测试）。
2. 更新 `.anm` 动画导入系统；动画制作与导出暂未测试。
3. 更新 `.pmx` 模型一键绑骨导出功能。目前只测试了 KK/KKS（恋活导出的 `.pmx`）；标准 `.pmx` 尚未适配。该功能仍为初版，后续还会继续优化减面效果。

### English

1. Updated vehicle import/export (available in the N-panel; initial version, not tested yet).
2. Updated the `.anm` animation import system; animation authoring and export are not tested yet.
3. Updated one-click rig-and-export for `.pmx` models. Currently tested only with KK/KKS (PMX exported from Koikatsu); standard `.pmx` files are not adapted yet. This is still an initial version, and decimation quality will be improved in future updates.

### Русский

1. Обновлён импорт/экспорт транспорта (доступен в боковой панели N; первая версия, пока не протестировано).
2. Обновлена система импорта анимаций `.anm`; создание и экспорт анимаций пока не тестировались.
3. Обновлена функция привязки к скелету и экспорта `.pmx` в один клик. Сейчас проверено только на KK/KKS (PMX из Koikatsu); стандартные `.pmx` пока не адаптированы. Это первая версия, качество децимации будет улучшаться в следующих обновлениях.

---

## 功能特性 / Features / Возможности

**导入导出 / Import / Export**
- 导入 GEM2 PLY / VOL；导出 GEM2 PLY / MDL / MTL
- 材质导出两种模式：Simple（漫反射贴图）/ Full Bump（含 roughness、specular 等细节贴图）
- Import GEM2 PLY / VOL; export GEM2 PLY / MDL / MTL
- Two material export modes: Simple (diffuse only) and Full Bump (roughness, specular and other detail maps)

**PMX → GEM2 转换管线 / PMX-to-GEM2 pipeline / Конвейер PMX → GEM2**
- 骨骼检测与清理（Rigid Bodies）
- 骨骼映射（含 GFA 模板，可加载/自动填充/手动编辑）
- 骨骼对齐与烘焙（含头骨归一化）
- 权重转移（自动合并辅助骨骼权重，清理非 ASCII 顶点组）
- UV 修复、自动减面（QEM，适配 65535 顶点 / 21845 三角形引擎上限）
- Bone detection and cleanup, bone mapping (with GFA templates), alignment and bake, weight transfer, UV fixing, automatic QEM decimation (within engine limits: 65535 vertices / 21845 triangles)

**其他 / Others**
- GFA 管线面板（GFA mod 专用的完整移植流程）
- 纹理烘焙（Bake）、蒙皮修复（自动 / 指定 MDL 文件）
- 命令行工具：`diff_ply.py`（对比原始与导出的 PLY）、`bone_diagnostics.py`
- 附带参考骨架模板 `samples/skeleton.blend`
- 界面支持中文 / English / Русский（Blender 语言跟随）
- GFA pipeline panel, texture baking, skinning fix (auto / from MDL file), CLI utilities (`diff_ply.py`, `bone_diagnostics.py`), reference skeleton (`samples/skeleton.blend`), trilingual UI (Chinese / English / Russian)

---

## 安装 / Installation / Установка

- Blender 4.3+
- `Edit > Preferences > Add-ons > Install...`，选择本插件的压缩包（zip 根目录包含 `gem2_mdl_tools/`），启用 "GEM2 Engine Tools"
- 或将文件夹复制到 `scripts/addons/` 后启用
- Blender: Edit > Preferences > Add-ons > Install… → select the addon zip (with `gem2_mdl_tools/` at the zip root) → enable "GEM2 Engine Tools". Alternatively copy the folder into `scripts/addons/`.

## 使用 / Usage / Использование

菜单：`File > Import/Export > GEM2 PLY`；PMX 转换见侧边栏 "PMX 转 GEM2" / GFA 管线面板。
Menus: `File > Import/Export > GEM2 PLY`; the PMX-to-GEM2 conversion lives in the side panel, and the GFA pipeline has its own panel.

### MOWAS2 一键自动导模（N 键侧边栏 → 「MOWAS2」面板）

> N 键侧边栏**只保留「MOWAS2」一个面板**（旧 PMX2GEM2 / GEM2 Tools 面板已从 UI 移除，
> 底层算子仍保留可供脚本调用）。面向普通用户：**点几下按钮**完成
> MMD/PMX → MOWAS2(GEM2) 单模型移植（对齐→绑骨→权重→减面→导出 .ply/.mdl/.def/.mtl + TGA 贴图）。

四步流程（按顺序点即可）：

1. **导入 PMX** — 选择源模型（如 `model_better2.pmx`），用 mmd_tools 自动导入（scale=1.0、rename_bones、INTERNAL 字典、清模型/去重/修 IK）。
2. **构建 GEM2 目标骨架** — 选择原版 .mdl（默认 `medicgirl.mdl`；也可用 `human.mdl` / `5.mdl`），自动解析并建立 58 骨目标骨架（含 `gem2_world_mats` 等属性）。
3. **摆好头/身/腿（仅对齐）** — Umeyama 刚性拟合 + 贴地 + T-pose 手臂垂落 + 手腕落位，冻结网格。可在视口查看效果（保存 `mowas2_aligned.blend` 快照）。
4. **一键完整导出** — 骨骼铸造 → 权重转移（镜像换名 + 手部单骨化）→ 手臂分段重映射 → 手腕权重重分配 → 全局 COLLAPSE 减面（面部保护，≤21845 面）→ UV-seam 拆分 → 单 .ply 导出 + 替换原版 .mdl 的 VolumeView + .def + .mtl（按 alpha 自动 `{blend blend}`）+ **PNG→TGA**（引擎不读 PNG）。

场景状态实时显示；已对齐/已冻结状态自动识别，重复点击不会二次变换。
详细管线说明见 `mowas2_pipeline.py` 模块注释与项目记忆 `问题_4_MOWAS2_E6_18_自动管线交接.md`。

命令行对比：`python diff_ply.py <original.ply> <exported.ply>`

---

## 已知限制 / Known Limitations / Ограничения

- 引擎面数上限：单网格 65535 顶点 / 21845 三角形，超出时插件会自动减面
- 载具导入/按骨自动拆分导出为初版，尚未完成完整测试
- 建议：模型需在 Blender 内手动清理后再进入管线
- Engine limits: 65535 vertices / 21845 triangles per mesh (auto-decimated otherwise); vehicle folder import and automatic bone splitting are initial, untested features; manual cleanup before the pipeline is recommended.

---

## 许可 / License / Лицензия

[MIT License](LICENSE)
