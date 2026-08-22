# GEM2 Engine Tools

Blender add-on for importing, editing, converting, and exporting GEM2 Engine assets used by
**Call to Arms: Gates of Hell** and **Men of War: Assault Squad 2**.

- Current add-on version: **1.1.1**
- Supported Blender version: **5.2 LTS**
- UI languages: **中文 / English / Русский / Українська**
- [Steam Workshop](https://steamcommunity.com/sharedfiles/filedetails/?id=3774985290)
- [GitHub Releases](https://github.com/BD5456/GEM2-MDL-Tools/releases)

中文：用于 GOH/MOWAS2 GEM2 模型、动画、碰撞体、材质和载具文件夹的 Blender
导入、编辑与导出插件，并提供 PMX/MMD 到 GOH 士兵皮肤的自动转换管线。

Русский: аддон Blender для импорта, редактирования и экспорта моделей, анимаций,
коллизий, материалов и папок техники GEM2 для GOH/MOWAS2, включая автоматический
конвейер PMX/MMD → скин солдата GOH.

Українська: додаток Blender для імпорту, редагування й експорту моделей, анімацій,
колізій, матеріалів і папок техніки GEM2 для GOH/MOWAS2, з автоматичним конвеєром
PMX/MMD → скін солдата GOH.

## Features

### GEM2 import and export

- Import GEM2 `PLY`, `VOL`, and `ANM` files.
- Read GEM2 `MDL` skeleton hierarchies and `MTL` material definitions.
- Export GEM2 model, collision, animation, material, and supporting entity files.
- Drag `.ply` files directly into the Blender viewport.
- Resolve and copy associated textures during import/export.
- Look up and extract only the required DDS textures from unexpanded GOH
  `resource/*.pak` archives.

### PMX/MMD to GOH pipeline

- Import PMX through `mmd_tools`.
- Detect and clean source skeletons and rigid-body layers.
- Map, align, and bake models to the supplied GOH/GFA skeleton templates.
- Transfer and normalize weights, including GOH hand, arm, leg, head, neck, and eye fixes.
- Repair UV seams, preserve protected materials, and apply adaptive decimation when needed.
- Export `PLY / MDL / DEF / MTL` plus engine-compatible textures.

### Vehicle tools

- Import complete vehicle folders and reconstruct their MDL hierarchy in Blender.
- Correctly handle nested LOD/`VolumeView` layouts used by turretless and other GOH vehicles.
- Export edited vehicle folders with PLY, VOL, MDL, ANM, MTL, DEF, and textures.
- Generic `File > Export` produces FBX and automatically includes bound rigs and required
  parent empties.
- Explicit directional exports:
  - **GOH → MOWAS2**: reversibly hides unsupported GOH MDL sequence events.
  - **MOWAS2 → GOH**: restores sequence events and generates a DEF using only stock GOH
    references and the actual volume names found in the MDL.
- The GOH DEF template uses a stock 122 mm placeholder weapon configuration and preserves
  the original file as `*.def.mowas2.bak`.

The directional vehicle tools focus on model and entity-format compatibility. They are not a
complete gameplay or balance-data converter; the generated GOH DEF deliberately uses verified
vanilla placeholder resources.

## Installation

1. Download `gem2_engine_tools_v1.1.1.zip` from the GitHub Release page.
2. In Blender 5.2 LTS, open `Edit > Preferences > Add-ons`.
3. Choose `Install from Disk...` and select the downloaded ZIP.
4. Enable **GEM2 Engine Tools**.

The installation ZIP contains `gem2_goh_tools/` directly at its root. Alternatively, copy that
folder into Blender's `scripts/addons/` directory.

For PMX import, install and enable a Blender 5.2-compatible version of `mmd_tools`.

## Basic usage

- Model and animation import: `File > Import > GEM2 PLY / VOL / ANM`.
- General-purpose scene export: `File > Export > FBX Model`.
- Vehicle folder import and directional export: open the add-on's 3D View N-panel.
- PMX conversion: follow the ordered steps in the PMX/GEM2 pipeline panel.

When importing PLY, the add-on searches for the corresponding MDL skeleton, MTL material,
and textures. Vehicle export retains required sidecar files and creates a backup before replacing
a MOWAS2 DEF with the stock-GOH template.

## Version 1.1.1 highlights

- Fixed PMX eye and pupil rendering/visibility problems.
- Fixed GOH vehicle imports with nested LOD/`VolumeView` structures.
- Added on-demand DDS extraction from packed GOH resources.
- Replaced the old generic bundle export with dependency-aware FBX export.
- Added explicit GOH → MOWAS2 and MOWAS2 → GOH vehicle exports.
- Added reversible GOH MDL sequence-event compatibility handling.
- Added stock-GOH DEF generation for MOWAS2 vehicle ports.
- Added complete Ukrainian localization alongside Chinese, English, and Russian.

The `m61a5` conversion was regression-tested in the GOH editor without `APP_ERROR`,
`define not found`, or unexpected-token failures.

## Package contents

- `gem2_goh_tools/` - Blender add-on package
- `locale/` - four-language UI strings
- `samples/` - GOH/GFA and MOWAS2 skeleton/model templates
- `diff_ply.py` - command-line PLY comparison utility

Development-only backups, tests, personal path settings, bytecode, and cache files are excluded
from release archives.

## Credits

- 1Lt-Muhammad - inspiration for export routines
- Simon / VegetaBird GFA Model Weight Transfer - PMX-to-GEM2 and material workflow references
- `mmd_tools`
- Best Way / 1C - GEM2 engine and original assets used for format reference
- MOWAS2 Female Soldier Mod and GFA assets used as interoperability references
- Blender-MCP and DeepSeek-assisted format investigation and regression testing

## License

[MIT License](LICENSE)
