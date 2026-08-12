# GEM2 Engine Tools (Blender Addon)

GEM2 引擎（Men of War: Assault Squad 2）资源的 Blender 导入/导出插件。
A Blender addon for importing and exporting GEM2 Engine assets (Men of War: Assault Squad 2).
Аддон Blender для импорта и экспорта ресурсов движка GEM2 (Men of War: Assault Squad 2).

> Steam Workshop 同款内容 / Same content as the Steam Workshop release / Та же версия, что и в Steam Workshop.

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

命令行对比：`python diff_ply.py <original.ply> <exported.ply>`

---

## 已知限制 / Known Limitations / Ограничения

- 引擎面数上限：单网格 65535 顶点 / 21845 三角形，超出时插件会自动减面
- 车辆/载具的自动拆件导出尚在计划中（当前可手动导入 .ply 后逐个组装）
- 建议：模型需在 Blender 内手动清理后再进入管线
- Engine limits: 65535 vertices / 21845 triangles per mesh (auto-decimated otherwise); automatic vehicle part splitting is planned; manual cleanup before the pipeline is recommended.

---

## 许可 / License / Лицензия

[MIT License](LICENSE)
