# GEM2 Engine Tools

> [!IMPORTANT]
> This `gem2_goh_tools` directory is the sole canonical runtime and development add-on. The sibling `gem2_mdl_tools` directory is a deprecated, inert archive retained only as the configured DSH working-directory anchor.

Blender add-on for importing, editing, converting, and exporting GEM2 Engine assets used by
**Call to Arms: Gates of Hell** and **Men of War: Assault Squad 2**.

- Current add-on version: **1.3.15**
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

### General weight projection (temporarily deprecated)

- The experimental generic surface-weight projection workflow has not been registered since version 1.2.4:
  its panel, settings, and operators are hidden from normal add-on use.
- Real-model testing showed that complete vertex coverage and plausible surface-distance statistics
  do not reliably predict acceptable shoulder, arm, and torso deformation. Reference anatomy and
  exact joint alignment remain too model-specific for a safe generic workflow.
- The implementation and regression tests remain in the source tree for possible redesign, but it is
  not part of the supported user workflow.
- For manual weighting on an existing custom armature, see Yami 3D's English YouTube tutorial
  [Weight Painting Complete Guide!! (Blender 3D)](https://www.youtube.com/watch?v=3yrwKXQbRpI).
  Its empty-group, Auto Normalize, group-locking, smoothing, and pose-testing workflow fits fixed
  GEM2 bone names better than tutorials centered on Rigify or automatic weights.

### GEM2 import and export

- Import GEM2 `PLY`, `VOL`, and `ANM` files.
- Selecting or dragging one multipart PLY automatically imports every direct sibling `VolumeView` from its MDL, with `_splitNN` filename discovery as a fallback. All skinned parts share one armature and model group.
- Import a complete PLY model directory from `File > Import > GEM2 Complete Model Folder`; multi-bone static MDLs continue through the vehicle-folder path.
- Read GEM2 `MDL` skeleton hierarchies and `MTL` material definitions.
- Export GEM2 model, collision, animation, material, and supporting entity files.
- Drag `.ply` files directly into the Blender viewport.
- Resolve and copy associated textures during import/export.
- Analyze and export selected Blender-imported PMX, FBX, OBJ, glTF, or other meshes through a source-format-independent multipart path. Active shape keys and non-armature modifiers are evaluated; Armature modifiers retain stack order against an isolated rest-pose rig.
- Use a configurable `3..65535` exact-record limit. Generated PLY files are direct sibling `VolumeView` entries on the existing attachment bone, with no `LODView` or carrier bone.
- Look up and extract only the required DDS textures from unexpanded GOH
  `resource/*.pak` archives.

### PMX/MMD to GOH pipeline

- Import PMX through `mmd_tools`.
- Detect and clean source skeletons and rigid-body layers.
- Map, align, and bake models to the supplied GOH/GFA skeleton templates.
- Transfer and normalize weights, including GOH hand, arm, leg, head, neck, and eye fixes.
- Preserve loop UV seams and imported custom loop normals, reuse only compact per-position game weights, and deduplicate exact final GEM2 records without changing Blender topology.
- Export `PLY / MDL / DEF / MTL` plus engine-compatible textures.

### Existing GOH/MOWAS2 human rest conversion

- Convert an already skinned GEM2 human mesh between GOH and MOWAS2 rest spaces from
  **GEM2 Human Rest Conversion** in the GOH panel. This path does not import PMX and does not
  call PMX alignment, GFA alignment, or weight-transfer routines.
- Select the destination route/MDL first. The operator reads raw `gem2_world_mats`,
  `gem2_parents`, and `gem2_mesh_parent` metadata, transforms vertices and shape keys by the
  weighted rest-space transfer, and rebinds the existing modifier to the destination armature.
- The normal complete-model/folder importer automatically normalizes a native human PLY armature
  once after model-space/root handling is baked. The low-level `ply_io.import_ply` API keeps the
  historical raw display unless its explicit normalization option is enabled.
- The dedicated conversion operator explicitly rebuilds both rigs from ancestor-normalized frames
  (`B^-1 * W`) before conversion. This prevents Blender from projecting a reflected MDL matrix and
  guessing an incorrect arm or foot roll. Reflected individual frames use a deterministic local-X
  correction that preserves the local-Y bone direction. Raw MDL metadata remains untouched for
  animation and export.
- The legacy PMX/GFA pipeline remains separate: its target uses the historical uniform Y-mirror
  frame-0 convention. The `mowas2_frame0_rest` marker is reserved for that path; older native
  human blends may retain it until repair. Normalized human rigs use
  `gem2_human_rest_display_rest` instead.
- Imported PLY coordinates are already in normalized model space. Skinned export first resolves
  Blender world coordinates into the armature/model frame, then peels only the destination
  `VolumeView` local attachment; it never applies a second global `mirrorY` to vertices.
- Vertex groups remain named and are not physically reordered. `gem2_skin_order` records the
  destination-specific SKIN palette order; the PLY exporter consumes it when writing weight
  slots and SKIN names (GOH and MOWAS2 orders are handled independently).
- The destination MDL metadata is never rewritten. A new conversion target is rebuilt into a
  usable normalized display rest while retaining raw metadata; a legacy `mowas2_frame0_rest` rig
  is repaired and reused when it also carries the complete raw contract, while a pure legacy PMX
  source or target is rejected rather than rewritten. An existing target is validated by its stored
  MDL path for discovery, and conversion records use raw-graph fingerprints to tolerate a copied blend's stale
  absolute source path during exact reverse. Custom loop normals,
  shape keys, modifiers, and mesh coordinates are restored together if a multi-mesh conversion
  fails.
- **Duplicate Mesh Before Conversion** keeps the source mesh visible for comparison. Enable
  **Exact Reverse** for an immediate reverse conversion so each vertex uses the inverse of its
  previously blended affine matrix, rather than a blend of inverse bone matrices.
- Use **Export Converted Human** after conversion to send the selected converted mesh through
  the normal `PLY / MDL / DEF / MTL` exporter without re-running the PMX pipeline. The PMX
  alignment buttons refuse converted meshes (and targets still referenced by them), so the two
  display contracts cannot silently overwrite one another.
- The implementation details and regression command are documented in
  [`docs/Human_Rest_Conversion.md`](docs/Human_Rest_Conversion.md).

### Vehicle tools

- Import complete vehicle folders and reconstruct their MDL hierarchy in Blender.
- Present the conventional GEM basis Y-mirror on an unapplied display root, so
  left/right vehicle parts read naturally in Blender while native MDL/PLY homes stay intact.
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

1. Download the current add-on ZIP from the GitHub Release page.
2. In Blender 5.2 LTS, open `Edit > Preferences > Add-ons`.
3. Choose `Install from Disk...` and select the downloaded ZIP.
4. Enable **GEM2 Engine Tools**.

The installation ZIP contains `gem2_goh_tools/` directly at its root. Alternatively, copy that
folder into Blender's `scripts/addons/` directory.

For PMX import, install and enable a Blender 5.2-compatible version of `mmd_tools`.
Use the shared operator preset `gem2_goh_mowas2_lossless` for both GOH and MOWAS2.
It keeps `Remove Doubles` disabled so UV, custom-normal, and weight boundaries survive import;
the GEM2 Companion panel reads the same preset, stores a SHA-256 snapshot in the scene, and
rejects incompatible presets before importing or reimporting.

## Basic usage

- Manual character weighting: keep the target GEM2 armature Rest matrices unchanged, create matching
  deform vertex groups, and refine them in Weight Paint mode while testing poses.
- Complete multipart model import: choose any referenced PLY with `File > Import > GEM2 PLY (Auto Multipart)`, or choose its directory with `File > Import > GEM2 Complete Model Folder`.
- To intentionally import only one PLY, clear **Auto-import Multipart Siblings** in the PLY file browser options.
- Other model and animation import: `File > Import > GEM2 VOL / ANM`.
- General-purpose scene export: `File > Export > FBX Model`.
- Source-format-independent GEM2 multipart export: select compatible meshes, use **Analyze** in the N-panel to inspect exact records/required parts, then choose **Export** or `File > Export > GEM2 Multipart Model (.mdl)`.
- Vehicle folder import and directional export: open the add-on's 3D View N-panel.
  Imported GEM vehicles with a standard `basis` Y-reflection are shown through an unapplied root
  display transform; you may move the root as a scene locator, but do not apply its scale before
  vehicle export. Generic FBX export may retain the deliberate negative-scale display transform.
- PMX conversion: follow the ordered steps in the PMX/GEM2 pipeline panel.

When importing PLY, the add-on searches for the corresponding MDL skeleton, MTL material,
and textures. Vehicle export retains required sidecar files and creates a backup before replacing
a MOWAS2 DEF with the stock-GOH template.

For GOH and MOWAS2 human exports, **Lossless Multipart Split** is available under
**Advanced > Vertex Limit Handling**. The exact-record threshold is configurable from `3..65535`;
keep `65535` for native hard-limit handling or lower it for deliberate multipart output. It freezes
the exact compact 40-byte records once, then packs existing triangles into `<entity>.ply` plus as
many `<entity>_splitNN.ply` files as required. Every file remains a stride-40 skinned PLY with the
complete SKIN palette and is referenced by a direct `VolumeView` on the same existing `skin` bone.
No carrier or animation bone is added. This matches
the working GOH/GFA multipart humans `agf_liufenyi` and `agf_ots14`. MOWAS2 3.262.1 was verified
in-game with an animated 85,772-record human split into 65,482 and 20,290 records; both direct views
rendered completely and followed animation. Each file stays at or below 65,535 records. A material stays whole
when possible and may span files only when necessary; local indices are remapped without changing
record bytes, topology, UVs, normals, weights, textures, or triangles. Automatic splitting takes
priority over decimation.

## Version 1.3.15 highlights

- Fix bidirectional GOH/MOWAS2 human rest conversion by rebuilding Blender bones from normalized raw
  MDL frames before assignment, including deterministic handling of reflected foot-chain frames;
  keep that display contract separate from the historical PMX/GFA frame-0 Y-mirror path.
- Preserve raw destination MDL rest metadata, vertex-group names, shape keys, custom loop normals, and
  modifier ownership; add exact blended-matrix reverse conversion and destination-aware SKIN palette export.
- Add real parsed-MDL display-rest, Pose Mode, export-space, and round-trip regression coverage.

## Version 1.3.13 highlights

- Import every MDL-referenced direct multipart PLY by selecting or dragging any one part, while reusing one armature, one model group, the original attachment-bone transform, and each part's own SKIN palette.
- Add `File > Import > GEM2 Complete Model Folder`, ignore nested LOD alternatives and MDL-unreferenced stale split files, and retain an opt-out for intentional single-file imports.
- For vehicle folders with the conventional GEM `basis` Y-reflection, put an unapplied Y display reflection
  on the shared root so left/right parts are intuitive in Blender while native MDL/PLY matrices remain
  unchanged for round-trip export.

## Version 1.3.12 highlights

- Add read-only exact-record analysis and transactional multipart GEM2 export for selected Blender-imported PMX, FBX, OBJ, glTF, and other meshes.
- Make the PMX/GOH/MOWAS2 lossless split threshold configurable from `3..65535` while preserving direct sibling `VolumeView` attachments and local u16 indices.
- Merge multiple selected meshes without mutating source geometry, materials, weights, active shape keys, modifier stacks, selection, or mode; reject mixed bound/unbound meshes and used skinned vertices without valid weights.
- Report vertices reduced to GEM2's strongest-two influence format. Stage complete generic and PMX entities before commit, roll back failures, preserve recoverable backups if rollback itself is blocked, and track PMX-owned files through a manifest so unrelated user files survive cleanup.

## Version 1.3.11 highlights

- Enable the existing lossless finalized-record splitter for MOWAS2 human exports after an in-game
  MOWAS2 3.262.1 test confirmed that multiple direct `VolumeView` entries on the original animated
  `skin` bone render together and follow animation.
- Keep the unsupported CUSTOM route disabled and retain the no-carrier-bone invariant.

## Version 1.3.10 highlights

- Detect generated texture/output artifacts that disappear during export, rebuild the complete entity
  once from the original Blender image sources, and retain the real exception if the retry also fails.
- Reject same-process attempts to write the same entity directory concurrently, preventing cleanup and
  texture conversion from racing each other.
- Add a Blender regression that reproduces a deleted `.__gem2_texture_input.tga` parent directory
  through nested exceptions and verifies both automatic recovery and re-entry exclusion.

## Version 1.3.9 highlights

- Generalize automatic splitting from one single-bone material partition to deterministic N-part
  finalized-record packing. Multi-bone and dual-weight materials are preserved, and an oversized
  single material can span PLY files without geometry changes.
- Keep face/body/eye materials in the main PLY when capacity allows and prefer hair/accessories in
  later parts; all files remain direct views on the existing `skin` bone.
- Use original Unicode material semantics during Toon conversion, preventing sanitized names such as
  `__5` (originally `目`) from being misclassified as bump eye materials.
- Add exact 65,535/65,536 boundary, immutable-record, same-material multipart, deterministic output,
  and repeated-MDL-cleanup regressions.

## Version 1.3.8 highlights

- Preserve every imported custom loop normal during compact PLY export instead of borrowing the
  normal from the first coincident vertex. This removes severe striped/faceted shading corruption on
  layered hair while still sharing stable skin weights and deduplicating identical final records.
- Revalidated the automatic split with source normals: 64,404 main records and 12,750 split records,
  preserving all 102,516 source triangles without decimation.

## Version 1.3.7 highlights

- Preserve mmd_tools/PMX `is_double_sided` on exported PLY materials so hair cards, cloth, and other
  open surfaces do not develop culling holes after export.
- Keep pupil, sclera, eye-shadow, and eyelid layers on their existing single-sided eye contract.
- Confirm automatic splitting preserves the source material triangle counts and changes no topology.

## Version 1.3.6 highlights

- Attach both skinned PLY files directly to the existing `skin` bone, matching verified GOH/GFA
  multipart characters and avoiding animation-reset behavior on newly added carrier bones.
- Migrate and remove the failed direct-`head` and sibling-carrier split structures on repeat export.
- Keep exact material planning, stale split cleanup, and independent binary validation for both files.

## Version 1.3.5 highlights

- Replaced the editor-invisible static stride-32 split with a stride-40 skinned split, but the
  separate sibling carrier was also rejected by the GOH human animation path.

## Version 1.3.4 highlights

- Added the initial opt-in automatic human PLY split experiment for the 65,535-record limit.

## Version 1.2.4 highlights

- Temporarily deprecated generic surface weight projection after real FBX tests showed that full
  coverage and surface proximity still cannot guarantee acceptable joint deformation.
- Removed the General panel, settings, and operators from normal add-on registration while retaining
  the implementation and regression suite for possible redesign.
- Added a manual weight-painting recommendation tailored to fixed-name GEM2 armatures.

## Version 1.2.3 highlights

- Added configurable post-projection weight smoothing along target mesh edges (two passes by default,
  zero to disable) before normalization and influence limiting.
- Smooths only groups transferred by General projection, preserving unrelated target groups, group
  locks, active-group state, Shape Keys, modifiers, and transactional rollback behavior.
- Added a real raw-FBX regression using `Primrose_Body_FullNude.fbx`: an unweighted duplicate keeps
  all 61 Shape Keys, receives complete GEM2 weights, is manually bound to the imported MOWAS2
  armature, and is checked in neutral and multi-joint poses against its original anatomical regions.
- In that regression, two smoothing passes reduced edges stretched over 2x from 112 to 28 and reduced
  the maximum edge-length ratio from 7.63 to 3.32 without reducing anatomical region accuracy.

## Version 1.2.2 highlights

- Tightened the non-blocking nearest-surface alignment warning from 10% to 5% of the target bounding
  box diagonal, so locally mismatched arm and hand poses are reported even when overall dimensions
  look similar.
- Added a real MOWAS2 ground-truth regression using `zaomiao`, `reimu`, and `medicgirl`: identical
  aligned geometry reproduces weights and posed deformation at floating-point precision, while a
  shifted or differently posed reference produces large, measurable weight and deformation errors.
- Clarified through regression evidence that valid donor weights alone are insufficient; reference
  and target surfaces must occupy the same pose and space before projection.

## Version 1.2.1 highlights

- Block enabled damaged Armature modifiers on either the weighted reference or the target instead
  of silently accepting weights that cannot be deformation-checked; disabled parked modifiers do
  not block projection.
- Block projection when an enabled target Armature is in Pose Position and matching weighted bones
  have non-identity pose transforms; this prevents double-deforming an already posed mesh.
- Include modifier targets, Rest/Pose state, armature transforms, and matching pose-bone matrices in
  stale-preflight protection.
- Auto Detect now ignores weighted meshes that contain an invalid Armature modifier.
- Reclassified the bundled Gondor scene as a negative regression: its `skin.001` reference has an
  Armature modifier pointing to the `skin` mesh, and its head material is weighted almost entirely
  to `foot1r` rather than `head`, so it is intentionally rejected as a reference.

## Version 1.2.0 highlights

- Added surface-only generic weight projection as the first, default-open `GOH` N-panel section.
- Added explicit source/target selection, scene-scoped detection, and read-only alignment preflight.
- Added interpolated surface, nearest-vertex, and topology-verified transfer modes.
- Added optional normalization, minimum-weight cleanup, influence limiting, distance limiting, and
  unweighted-vertex inspection.
- Added stale-preview protection, single-step undo support, transactional rollback, shared-data and
  memory guards, and complete four-language UI coverage.
- Added synthetic and Blender integration coverage for complete assignment, normalization, influence
  limits, topology preservation, and rollback; semantic quality still requires a valid reference rig.

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
