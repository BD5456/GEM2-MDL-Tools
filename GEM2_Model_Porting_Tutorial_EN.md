# GEM2 Engine Tools — Complete Beginner's Guide to Model Porting

**Manual and Automatic workflows for Call to Arms: Gates of Hell (GOH) and Men of War: Assault Squad 2 (MOWAS2)**

| | |
|---|---|
| **Add-on** | `gem2_goh_tools` v1.3.17 |
| **Blender** | 5.2 LTS |
| **Games** | Call to Arms: Gates of Hell (GOH / GFA), Men of War: Assault Squad 2 (MOWAS2) |
| **Audience** | A beginner who has never ported a model into a GEM2 game |
| **Goal** | Take a model from another source (MMD/PMX, FBX, OBJ, glTF, or an existing GEM2 skin) and make it work correctly inside the game |

---

## Table of Contents

1. [Read This First](#1-read-this-first)
2. [Concepts and Vocabulary](#2-concepts-and-vocabulary)
3. [Installation and First-Run Setup](#3-installation-and-first-run-setup)
4. [Choosing Your Route](#4-choosing-your-route)
5. [Route A — Automatic PMX/MMD → GOH](#5-route-a--automatic-pmxmmd--goh)
6. [Route B — Existing GEM2 Human Rest Conversion (GOH ↔ MOWAS2)](#6-route-b--existing-gem2-human-rest-conversion)
7. [Route C — Manual Porting (fully by hand)](#7-route-c--manual-porting-fully-by-hand)
8. [Route D — Generic Multipart Export (FBX / OBJ / glTF sources)](#8-route-d--generic-multipart-export)
9. [Vehicles](#9-vehicles)
10. [Validation, Packaging and In-Game Testing](#10-validation-packaging-and-in-game-testing)
11. [Troubleshooting](#11-troubleshooting)
12. [Appendix A — Panel Property Reference](#appendix-a--panel-property-reference)
13. [Appendix B — Operator and Menu Index](#appendix-b--operator-and-menu-index)
14. [Appendix C — Final Checklists](#appendix-c--final-checklists)

---

## 1. Read This First

### 1.1 What "porting" actually means

A GEM2 game does not play your Blender file. It plays a small folder of engine files:

```text
<entity_name>/
    <entity_name>.def     <- the entity entry point the game loads
    <entity_name>.mdl     <- the skeleton / hierarchy / attachment description
    <entity_name>.ply     <- the mesh (positions, normals, UVs, weights)
    <entity_name>_split01.ply   <- optional extra parts for big meshes
    <entity_name>.mtl     <- materials
    *.tga / *.dds         <- textures
```

**Porting** means producing that folder so that:

- the mesh is bound to **the game's own skeleton** (not the source model's skeleton);
- every vertex carries weights that name **real GEM2 bone names**;
- the mesh sits in the **exact coordinate space** the game expects;
- the record count per `.ply` never exceeds the engine's **65,535** limit;
- materials, UVs and textures survive the conversion.

A model that *looks* right in the Blender viewport is **not** proof that any of this is true. This tutorial teaches you to check each item.

### 1.2 The one rule that matters most

> **The target armature is the animation contract. Move the model to the skeleton. Never move, reshape, rescale, or pose the target skeleton to fit your model.**

Almost every catastrophic port failure (limbs detaching, crossed arms, feet inside the ground, animations exploding) comes from breaking this rule.

### 1.3 How to use this tutorial

- Work in **numbered checkpoint `.blend` files**. Save a new one after each major stage.
- **Use `Ctrl+Z` freely for single-step experiments** (see 1.4) — but do **not** rely on it to undo a whole multi-stage port. For that, close without saving and reopen the last checkpoint.
- Never overwrite your original source files, extracted vanilla game files, or the game's own folders.
- Do not "fix" a problem by adding a second mirror, a second Armature modifier, or another round of transforms. Go back to the checkpoint and fix the actual cause.

Suggested working layout (drive letters and names are examples — the *separation* is what matters):

```text
D:\GOH_work\my_character\
    source\          <- original, untouched
    checkpoints\     <- 00_empty.blend, 01_..., 02_...
    reference\       <- verified vanilla target files
    export_test\     <- add-on output before it goes anywhere near the game
    temp\
```

### 1.4 The experiment loop: when `Ctrl+Z` is your best friend

Beginners usually hear "don't rely on undo" and then never experiment at all. That is the wrong lesson. The real rule is about **granularity**:

| Situation | Use `Ctrl+Z`? | Why |
|---|---|---|
| You dragged a slider and don't like the result | **Yes — ideal** | One step, fully reversible |
| You moved/rotated the source while aligning | **Yes** | One step |
| You painted a bad weight stroke | **Yes** | That is what Weight Paint undo is for |
| You ran Step 3 (**Auto Align**) and the pose looks wrong | **Yes** | Alignment is a preview; undo and retry |
| You added/removed a vertex group or object by mistake | **Yes** | One step |
| You want to undo an **export** that already wrote files | **No** | Undo does not delete the files on disk |
| You reloaded scripts, changed mode several times, or reopened the file | **No** | The undo stack was cleared |
| The mesh is already **frozen** and you want to change alignment | **No** | Re-import and re-align instead (see 5.6) |
| You want to get back to "three hours ago" | **No** | Reopen the checkpoint |

So the working rhythm is:

```text
change one thing  ->  look at it  ->  not good? Ctrl+Z  ->  change it differently
                 ->  good? save a checkpoint and move on
```

**Change one thing at a time.** If you adjust six sliders and then dislike the result, you cannot tell which one caused it. Adjust, look, undo, adjust again.

Useful details:

- **`Ctrl+Z`** = undo, **`Ctrl+Shift+Z`** = redo.
- **Undo History**: `Edit > Undo History` opens a list and lets you jump back many steps at once — far faster than pressing `Ctrl+Z` fifty times.
- **Raise the undo limit before a long session**: `Edit > Preferences > System > Memory & Limits > Undo Steps`. Blender defaults to **32**, which is not enough for weight painting or parameter tuning. **128–256** is a comfortable value.
- Undo **does not** remove exported files, backups, or the `mowas2_auto_backup.blend` snapshot. Clean those up by hand if you want to.

> **The one-line version:** `Ctrl+Z` is perfect for *experiments*. Checkpoint files are for *disasters*.

### 1.5 Viewport tricks that make everything easier

| Trick | How | Why it helps |
|---|---|---|
| **Isolate one object** | Select it, press Numpad `/` (Local View); press again to exit | Inspect deformation without other meshes in the way |
| **See bones through the mesh** | Select the armature → Object Properties → `Viewport Display` → **In Front** | Essential for alignment and pose testing |
| **X-ray the mesh** | `Alt+Z` in Material Preview, or enable X-Ray on the object | See bones and internal geometry |
| **Check left/right** | Numpad `1` for front view, `Ctrl`+Numpad `1` for back | A mirrored model is obvious here — and cheap to fix early |
| **Frame what you're working on** | Numpad `.` | Stop losing the model after zooming |
| **Compare against the reference** | Keep the reference mesh visible while aligning, hide it (`Outliner` eye icon) afterwards | Confirms where target bones expect the body to be |
| **Watch the console log** | `Window > Toggle System Console` (Windows) | The pipeline prints a numbered stage log — it tells you which stage failed |
| **Preview real animation instead of re-exporting** | Extract `.anm` and import it (see 10.3) | Catches weighting problems far faster than repeated exports |

---

## 2. Concepts and Vocabulary

### 2.1 File types

| File | What it is | What you do with it |
|---|---|---|
| `.blend` | Blender scene | Save numbered checkpoint copies |
| `.pmx` | MikuMikuDance model | Source for the automatic route (needs `mmd_tools`) |
| `.fbx`, `.obj`, `.glb`, `.gltf` | Common 3D exchange formats | Source for the generic/manual routes |
| `.mdl` | GEM2 text skeleton + entity hierarchy | **Use only the one belonging to your chosen target family** |
| `.ply` | GEM2 binary mesh (positions, normals, UVs, weights) | Import as reference; written on export |
| `.vol` | GEM2 collision volume | Imported/exported with vehicles |
| `.mtl` | GEM2 material description | Check references and blend mode |
| `.anm` | GEM2 animation | Import to preview animation on your rig |
| `.def` | Entity entry point | Must name the generated MDL |
| `.tga`, `.dds` | Textures | Check real pixels and alpha |

### 2.2 Blender words

| Term | Meaning |
|---|---|
| **Mesh** | The visible surface: vertices, edges, faces |
| **Armature** | A Blender object that contains bones |
| **Target armature** | The game's own skeleton. It drives all animation. |
| **Source armature** | The skeleton that came with your imported model (temporary aid only) |
| **Armature modifier** | The link telling a mesh which armature deforms it |
| **Vertex group** | A named set of vertices with a weight, matching one bone |
| **Weight** | `0.0`–`1.0`, how strongly a bone moves a vertex |
| **Rest Position** | The skeleton's untouched neutral pose — the contract |
| **Pose Position** | A temporary test pose; does not change rest bones |
| **UV map** | 2D coordinates selecting texture pixels |
| **PLY record** | One final exported position/normal/UV/weight record. **Not always the same as one Blender vertex.** |

### 2.3 Weight and record limits — what the engine supports vs. what this add-on writes

These two limits are easy to mis-state, so here is the precise picture.

#### Bone influences per vertex

| Level | How many influences |
|---|---|
| **GEM engine format** (Best Way official spec) | **Up to 4.** The `SUBM` (GEM RTS / GEM3) `vertex_format` supports `0x0200` = 2 bones, `0x0400` = 3 bones, `0x0600` = 4 bones. The `VERT` chunk stores `bone_indices` and `bone_weights` as `uint8[4]`. |
| **What the add-on can *read*** | Up to 5 (`D3DFVF_XYZB1` … `XYZB5` are all parsed) |
| **What the add-on *writes* for a GOH human skin** | **2** |

The add-on writes GOH human skins with the vanilla compact layout:

```text
FVF 0x1118 = D3DFVF_XYZB2 | D3DFVF_LASTBETA_UBYTE4 | D3DFVF_NORMAL | D3DFVF_TEX1
stride 40  = position float[3] (12)
           + weight  float    (4)   <- one explicit float weight
           + indices uint8[4] (4)   <- LASTBETA_UBYTE4, 4 index slots
           + normal  float[3] (12)
           + uv      float[2] (8)
```

`D3DFVF_XYZB2` means "2 blending weights", so only **two** index slots are filled; the second weight is implied as `1 − w0`. Slots 3 and 4 are written as `0`.

**Practical consequence:** if a vertex has 4 influences, the exporter keeps the strongest 2 and renormalizes them. You will see:

```text
{count} vertices had more than two influences; GEM2 kept and renormalized the strongest two
```

This is normal *for this export path* — and it is why clean, simple weighting beats complex weighting here.

**Practical consequence:** if a vertex has 4 influences, the exporter keeps the strongest 2 and renormalizes them. You will see:

```text
{count} vertices had more than two influences; GEM2 kept and renormalized the strongest two
```

This is why clean, simple weighting beats complex weighting here.

#### Is 2 actually the truth? Verified against real game assets

It is reasonable to distrust a limit quoted from an add-on's own UI strings, so this was checked directly against an installed copy of **Call to Arms: Gates of Hell**. Sample scan of the real `.ply` files:

**Gates of Hell** (`resource/entity/`):

| Source | Sampled | Result |
|---|---|---|
| `humanskin.pak` (14,952 human `.ply` total) | 2,500 | **100% `XYZB2`** — FVF `0x1118` (one file at `0x1158`, i.e. the same plus vertex colour) |
| `-vehicle` (76,635 vehicle `.ply`) | 2,000 | static `XYZ` + `XYZB2` only — **zero** 3-, 4- or 5-bone |
| `entity.pak`, `inventory.pak`, `construction.pak`, `flora.pak` | 2,000 each | same — **zero** non-2-bone skinned meshes |

**Men of War: Assault Squad 2** (`resource/entity/`, both vanilla and a third-party character mod):

| Source | Sampled | Result |
|---|---|---|
| Vanilla `e1.pak`, human entries only (183 total) | 183 | **179 `XYZB2`** + 4 static — **zero** 3-, 4- or 5-bone |
| Vanilla `e1.pak` / `e2.pak` (all entries) | 1,405 / 2,000 | static + `XYZB2` — **zero** non-2-bone |
| Vanilla `c1.pak` / `c2.pak` | 2,000 each | all static (props) |
| Character mod `entity/humanskin` (808 total) | 808 | **805 `XYZB2`** + 3 static — **zero** non-2-bone |
| Character mod `entity/` (8,603 total) | 2,000 | static + `XYZB2` — **zero** non-2-bone |

> **Handy discovery:** GEM2 `.pak` archives are plain **ZIP** files (`PK\x03\x04`) with **stored** (uncompressed) entries. You can open them with Python's `zipfile` and inspect assets without extracting a 1.7 GB archive.

Two further confirmations from the actual bytes:

- Every sampled file uses the legacy **`MESH`** chunk. No `SUBM` or `SBM1` anywhere — so the 4-bone `SUBM` variant simply does not occur in GOH content.
- Dumping real human-skin vertices shows index slots **3 and 4 are always `0`**:

```text
w0 = 0.8835   indices = (6, 3, 0, 0)   -> 2 bones, w1 = 0.1165
w0 = 1.0000   indices = (1, 0, 0, 0)   -> 1 bone,  w1 = 0.0000
```

Human skins also carry `stride 56` = the 40-byte `XYZB2` body **plus a 16-byte tangent tail**, matching the add-on's own note that "skinned stride56's FVF body is 40 bytes".

**So the honest statement is:**

- **Across ~14,000 sampled `.ply` files from both GOH and MOWAS2 — vanilla and modded, human and vehicle — not a single one uses 3, 4 or 5 bone influences.** For these two games, 2 bones is not an add-on limitation; it is what every asset in the wild uses. The add-on is faithfully reproducing the vanilla layout, not taking a shortcut.
- **It is still not a law of the GEM format.** The official spec allows up to 4, and a different GEM-family title could legitimately use 3 or 4. This scan covers GOH and MOWAS2 only — earlier GEM titles (Men of War, Faces of War, Assault Squad 1) were **not** checked. Do not generalise "2 bones" to the whole engine family; if you work on another title or a different chunk layout, read its `vertex_format` / FVF bits instead of assuming.

> The add-on's reader already handles `XYZB1` through `XYZB5`, so if you ever meet a GEM asset with more influences it will *import* correctly. Only the human-skin *writer* is pinned to `XYZB2`, because that is what the game expects.

#### Records per `.ply`

One `.ply` may hold at most **65,535 records** here, because the add-on writes 16-bit indices (`INDX` chunk; the official format also defines a 32-bit `IND4` chunk, but this exporter uses 16-bit).

Records are counted **after** the exporter splits vertices by UV/normal/weight boundaries, so a 50,000-vertex Blender mesh can still exceed the limit. When it does, the add-on can **losslessly split** into `<entity>.ply` + `<entity>_split01.ply` + ... — every file is a complete, directly-attached view on the same `skin` bone. Splitting is lossless: no decimation, no geometry change.

#### Bone palette limit

A `.ply` supports at most **254 bone groups** (`bone_map` is a `uint8` and `0` is reserved for "use the mesh's global transform"). The add-on raises an error past that. A normal human character uses far fewer, but if you have merged several models or kept dozens of unused groups, this is the limit you hit.

### 2.4 Blender controls you will use

| Area | Where |
|---|---|
| 3D Viewport | Large central area |
| Outliner | Usually top-right; lists objects |
| Properties editor | Usually bottom-right; object/modifier/material settings |
| **Viewport Sidebar** | Press `N` over the 3D Viewport — **this is where the add-on panel lives** |
| Mode selector | Top-left of the 3D Viewport |

| Action | Shortcut |
|---|---|
| Toggle sidebar | `N` |
| Toggle Object / Edit Mode | `Tab` |
| Enter Pose Mode | `Ctrl+Tab` |
| Select all / deselect all | `A` / `Alt+A` |
| Front / side / top view | Numpad `1` / `3` / `7` |
| Frame selected | Numpad `.` |
| Search any command | `F3` |
| Save / Save As | `Ctrl+S` / `Ctrl+Shift+S` |

An **orange outline** means selected. In a multi-selection the brighter object is the **active** object — binding and export care about this.

---

## 3. Installation and First-Run Setup

### 3.1 Install the add-on

1. Get the add-on from either place:
   - **Steam Workshop** (app 400750, *Call to Arms: Gates of Hell*) — subscribe and it lands in your Workshop downloads folder; or
   - **GitHub Releases** — <https://github.com/BD5456/GEM2-MDL-Tools/releases> — download the `gem2_engine_tools_v*.zip` asset.

   Both contain the identical add-on folder. **Version numbers can differ between the two** — the Workshop item is usually the newer one. If a feature described here is missing, check the version shown in `Edit > Preferences > Add-ons` before assuming you did something wrong.
2. In Blender 5.2 LTS: `Edit > Preferences > Add-ons`.
3. Click **Install from Disk...** and select the ZIP.
4. Enable **GEM2 Engine Tools**.

> The ZIP contains `gem2_goh_tools/` at its root. You can instead copy that folder into
> Blender's `scripts/addons/` directory — this is the better option if you got the files
> from a Steam Workshop subscription, because Workshop content can be re-downloaded or
> deleted by Steam at any time.
>
> **Important:** a sibling folder named `gem2_mdl_tools` may exist. It is a **deprecated,
> inert archive**. Only `gem2_goh_tools` is the live add-on. Do not enable both as active
> toolsets.

### 3.2 Install `mmd_tools` (only needed for PMX sources)

The automatic route imports `.pmx` through `mmd_tools`. Install and enable a **Blender 5.2-compatible** version.

### 3.3 Set the MMD import preset (PMX users)

PMX import runs through `mmd_tools` with a fixed set of import options. The add-on needs **Remove Doubles disabled** so that UV seams, custom normals and weight boundaries survive the import.

> **Read this before you hunt for a preset file.**
> Some older notes and forum posts tell you to pick a preset called
> `gem2_goh_mowas2_lossless`. **That preset is not shipped with the add-on and is not
> publicly distributed** — it is a private authoring preset. If it does not appear in
> your dropdown, nothing is broken. Use **Pipeline defaults** instead, which is
> built in and always present.

#### Option 1 (recommended for everyone): use the built-in `Pipeline defaults`

The **MMD Preset** dropdown always contains an entry labelled **Pipeline defaults**
(internal id `__PIPELINE__`). It applies the add-on's own lossless settings directly —
no external preset file, no SHA-256 snapshot, nothing to download. **Select it and move on.**

It sets the following values, which are exactly the values the GEM2 pipeline expects:

| Setting | Value | Why it matters |
|---|---|---|
| `types` | `ARMATURE`, `MESH` | Physics, display frames and morphs are not needed; the pipeline removes rigid bodies itself |
| `scale` | `1.0` | Any other scale silently corrupts the alignment maths |
| `clean_model` | `True` | **Required.** Cleans MMD artefacts |
| `remove_doubles` | `False` | **Required.** Keeps UV seams and split vertices intact — the exporter does its own exact deduplication later |
| `import_adduv2_as_vertex_colors` | `False` | Not used by GEM2 |
| `fix_bone_order` | `True` | **Required.** GEM2 expects Japanese MMD bone order |
| `fix_ik_links` | `True` | **Required.** Correct IK chains |
| `ik_loop_factor` | `5` | Standard IK iteration count |
| `apply_bone_fixed_axis` | `False` | **Required.** Must stay off or rest poses drift |
| `rename_bones` | `True` | **Required.** Produces the readable names the bone mapper matches on |
| `use_underscore` | `True` | **Required.** Produces `Arm_L` / `Leg_L` style names — the mapper's naming contract |
| `dictionary` | `INTERNAL` | **Required.** Built-in translation dictionary, no external files |
| `bone_disp_mode` | `OCTAHEDRAL` | Visual only |
| `use_mipmap` | `True` | Texture import only |
| `sph_blend_factor` | `1.0` | Standard |
| `spa_blend_factor` | `1.0` | Standard |
| `log_level` | `INFO` | Console verbosity |
| `save_log` | `False` | Do not litter the output folder |

Rows marked **Required** are hard invariants. The add-on re-checks them before every
import and **refuses to run** if a preset violates any of them (see 11.2).

#### Option 2 (optional): create your own named preset

If you want the preset to show up by name in the dropdown, save one yourself:

1. Run `File > Import > MMD Model (.pmx/.pmd)` **once** with the values from the table above.
2. In the import dialog, click the **preset** control (the `+` / bookmark icon) and choose **Add Preset**. Name it, e.g. `gem2_goh_mowas2_lossless`.
3. Blender writes it to:

   ```text
   %APPDATA%\Blender Foundation\Blender\<version>\config\presets\operator\mmd_tools.import_model\<your name>.py
   ```

   (On Linux/macOS: `~/.config/blender/<version>/config/presets/operator/mmd_tools.import_model/`)

4. Back in the **GOH Auto Transfer** panel, click the small **refresh** icon next to **MMD Preset** so the add-on re-scans the preset folders.

The file is plain Python and must contain only `op.<setting> = <literal>` assignments
drawn from the 19 settings above. Anything else — imports, function calls, unknown
property names — is rejected as an invalid preset.

#### About the SHA-256 snapshot

When you select a **named** preset (Option 2), the panel reads it, stores a SHA-256
hash in the scene, and re-verifies it before every import and re-import. If you edit
the preset file afterwards, the snapshot no longer matches and the add-on will
**reject it** — click the refresh icon to re-capture.

`Pipeline defaults` (Option 1) has no file, so there is nothing to hash, nothing to go
stale, and nothing to refresh. **This is the main reason to prefer it.**

### 3.4 Where the panel is

1. Open a 3D Viewport.
2. Press `N` to open the sidebar.
3. Select the **GOH** tab.
4. Open the **GOH Auto Transfer** panel (it is collapsed by default — click its header).

Everything for the automatic route lives in this panel. The manual route mostly uses standard Blender tools plus the `File > Import` / `File > Export` entries this add-on adds.

---

## 4. Choosing Your Route

There are four porting routes. Pick **one** and follow it; do not mix them on the same mesh.

| Route | Use when | Difficulty |
|---|---|---|
| **A — Automatic PMX** | Source is a **PMX/MMD** character and you want a GOH/GFA or MOWAS2 soldier skin | ★☆☆ (4 clicks) |
| **B — Human Rest Conversion** | The model is **already a skinned GEM2 human** and you need to move it between GOH and MOWAS2 | ★☆☆ |
| **C — Manual** | Source is FBX/OBJ/glTF/an unsupported model, **or** the automatic result needs repair, **or** you want full control | ★★★ |
| **D — Generic Multipart Export** | You already have a Blender-imported mesh bound to a compatible GEM2 armature and just need to write GEM2 files | ★☆☆ |

Also available: **Vehicle** folder import/export (Section 9).

### Decision tree

```text
Is your source a .pmx/.pmx MMD model?
    YES -> Route A (automatic)
    NO
      |
Is it already a GEM2 human (imported from a .ply with GEM2 vertex groups)?
    YES -> Do you need it in the OTHER game (GOH <-> MOWAS2)?
              YES -> Route B (rest conversion)
              NO  -> Route D (just re-export)
    NO
      |
Does it already have a compatible GEM2 armature + correct GEM2 vertex groups?
    YES -> Route D (generic multipart export)
    NO  -> Route C (manual porting)
```

> **Beginner advice:** if your model is PMX, start with Route A. Even if you eventually need manual fixes, Route A produces a correctly-bound, correctly-weighted starting point in about two minutes — and you can inspect it to learn what "correct" looks like.

---

## 5. Route A — Automatic PMX/MMD → GOH

This is the add-on's headline workflow: **four steps, in order, top to bottom of the panel.**

```text
Step 1  Import Source Model        ->  Import PMX (mmd_tools)
Step 2  GEM2 Target Skeleton       ->  Select .mdl and Build Skeleton
Step 3  Align Head/Body/Legs       ->  Auto Align (preview only)
Step 4  Full Export                ->  Full Export (Bind + Decimate + PLY)
```

You must do them **in this order**. Step 3 is a preview; Step 4 repeats the alignment internally and then commits.

### 5.0 Before you start

- You have a `.pmx` file.
- You have `mmd_tools` installed and enabled.
- You know which **target family** you need (see 5.2).
- Blender is open on a **new empty scene**. Save it as `00_empty.blend`.

### 5.1 Step 1 — Import the source model

In the **GOH Auto Transfer** panel:

1. Find the **Step 1: Import Source Model** box.
2. Set **MMD Preset** to **Pipeline defaults** (see 3.3).
   - This entry is built into the add-on and is always present — you do not need to download or install any preset file.
   - If you created your own named preset and edited it since, click the small **refresh** icon next to the field to re-scan and update the stored snapshot.
3. Set the file field to your `.pmx`.
4. Click **Import PMX (mmd_tools)**.

What happens: the model is imported through `mmd_tools` using the add-on's lossless
import settings. Rigid bodies and joints are imported too — the pipeline removes them
later, so do not panic.

**Check:** the **Scene Status** box should now read something like:

```text
Source armature: <your model> | Target armature: (not built)
```

Save checkpoint `01_pmx_imported.blend`.

> If you get `PMX import failed`, the usual causes are: `mmd_tools` missing or not enabled
> for your Blender version, or the selected preset violates one of the required invariants
> listed in 3.3. Do not hand-import the PMX with different settings — the pipeline expects
> this exact import.

### 5.2 Step 2 — Build the GEM2 target skeleton

This is the step where you choose **which game and which animation family** your skin will use. Get it wrong and every animation will look subtly or catastrophically broken.

**Export Route** (three radio buttons):

| Route | Meaning | When to use |
|---|---|---|
| **GOH** | GOH materials and hand weights. You pick any human skeleton MDL. | Default. Your skin is for Gates of Hell. |
| **MOWAS2** | Locks to the bundled `samples/MOWAS2.mdl` and **forces Toon Shader off**. | Your skin is for Men of War: AS 2. |
| **Custom MDL** | Keeps whatever target MDL you selected. | You have a specific, verified target MDL. |

**Picking the target MDL (GOH route):**

The `samples/` folder ships reference skeletons:

| File | Family | Use it when |
|---|---|---|
| `goh_skin.mdl` | Short-arm stock GOH human | The final skin uses the stock short-arm GOH animation family |
| `goh_skin_gfa.mdl` | Long-arm GOH/GFA human | The final skin uses the GFA long-arm animation family |
| `goh_skin_armlong.mdl` | Long-arm variant | Specific long-arm work |
| `MOWAS2.mdl` | MOWAS2 human | MOWAS2 destination |
| `uma_gan_v2.mdl` | Alternative target | Specialised work |

> **Never** treat `goh_skin.mdl` and `goh_skin_gfa.mdl` as interchangeable visual aliases. They differ in forearm, hand and helper-bone rest transforms. Aligning to one and exporting with the other is a classic, hard-to-diagnose failure.

**Procedure:**

1. In the **Step 2: GEM2 Target Skeleton** box, choose the **Export Route**.
2. If the route is `GOH` or `Custom MDL`, set the **GEM2 Target Skeleton .mdl** field to your chosen `.mdl`.
   - If the route is `MOWAS2`, the field is **locked** to the bundled `MOWAS2.mdl` — this is intentional.
3. Click **Select .mdl and Build Skeleton**.

**Check:** the box should confirm:

```text
Target skeleton ready: <name> (58 bones)
```

The standard GOH human target has **58 bones**. Note also that the hierarchy includes `basis` (root), `body`, `head`, `hand1l/r`, `hand_rot1l/r`, `palm1l/r`, `foot1l/r`, `foot2l/r`, `foot3l/r`, helper/IK bones, and `skin` (the human mesh attachment bone).

Save checkpoint `02_target_built.blend`.

### 5.3 Step 3 — Align (preview only)

Click **Auto Align (preview only)** in the **Step 3: Align Head/Body/Legs** box.

Internally this performs a rigid similarity fit (rotation + translation + uniform scale — **no shearing, no non-uniform stretching**), places the model on the ground, and lowers the arms into a GOH-like pose for preview.

The console prints a stage log you can read to understand what happened:

```text
[1] rigids removed: N
[2] mirrored: False
[3] rigid fit done
[3.2] plan-C narrow target active
[4] arms posed (pose=..., N°)
[4.5] hands posed
[5] frozen
[6] casting ... | bound & transferred
[7] decimation skipped / decimated: ...
```

**Now look at the model in the viewport.** This is your chance to adjust proportions *before* anything is committed.

### 5.4 Tuning before export (Advanced box)

Open the **Advanced** box. Most options give **live, reversible preview** after alignment — drag a slider and watch the model. That is the intended workflow: **adjust → look → then Step 4.**

#### The parameter-tuning loop

This is the single most useful habit for this route, and it is exactly what `Ctrl+Z` is for:

```text
1.  Run Step 3 (Auto Align) once
2.  Change ONE slider (e.g. Foot Size)
3.  Look at the model in the viewport — orbit, check front and side views
4.  Wrong direction or wrong amount?  Ctrl+Z  (or drag the slider back)
5.  Try a different value
6.  Happy?  Leave it and move to the next slider
7.  Before Step 4, save a checkpoint: 05_tuned.blend
```

Why this works: alignment is a **preview**. Nothing is written to disk until Step 4. You can re-run **Auto Align** as many times as you like, and you can undo any single adjustment.

Practical notes:

- **Change one slider at a time.** Six sliders changed at once with a bad result teaches you nothing.
- **Some sliders are not sliders.** *Shoulder Width Factor* and *Arm Thickness* expect you to **type a number** — dragging behaves unintuitively on them.
- **Options that require re-import** (foot size, shoulder width, head scale, applied pupil options) will ask for the source PMX if you change them *after* the mesh is frozen. Keep the **PMX Model File** field filled in and this is painless. See 5.6.
- **Live-preview options** (body curve, arm inset, neck follow, pupil clearance) adjust freely after freezing — no re-import needed.
- **Don't chase perfection here.** Get proportions plausible, export, and look at it in-game. It is much easier to judge a model in the game's own lighting and animation than in the viewport.

**Body / proportions**

| Property | Default | What it does |
|---|---|---|
| **Ground Height Z** | `-0.07` | Ground plane height used to seat the feet |
| **Enlarge Head** | off | Enable before using the two head controls |
| **Head Scale** | `1.06` | Scales the head from the source head bone; does not touch the target rest skeleton |
| **Neck and Accessory Follow** | `0.0` | 0 = baseline, 1 = neck/accessories fully follow head scale |
| **Foot Size** | `1.0` | Scales feet/shoes around the target ankle; soles stay clamped to the ground |
| **Shoulder Width Mode** | `SKELETON` | `OFF` = unchanged; `GEOMETRY` = mesh inset (plan B); `SKELETON` = narrow-shoulder alignment target (plan C, recommended for anime proportions) |
| **Shoulder Width Factor** | `0.96` | `<1` narrower, `>1` wider. Type a number — it is not a slider. |
| **Hand-chain attach** | `blend` | `blend` = shoulder scales in, forearm/hand keep original GOH rest, upper arm rotates collinear (recommended); `full` = translate whole arm chain inward |
| **Clavicle/Shoulder Inset** | `1.0` | Only active in `GEOMETRY` mode. `<1` pulls shoulder-band vertices inward |
| **Whole-Arm Horizontal Spacing** | `1.0` | `<1` translates both arms toward the body centre **without shortening** them |
| **Arm Thickness** | `1.0` | Radial girth of upper arm and forearm |
| **Hip outward / Thigh outward / Calf outward** | `1.06 / 1.04 / 1.03` | Move each segment outward/inward; equal values stay continuous with no junction jump |
| **Hip / Thigh / Calf thickness** | `1.0` | Radial girth per segment; blends smoothly at junctions |

**Hands and head details**

| Property | Default | What it does |
|---|---|---|
| **GFA Hand Split** | **on** | Hand weights split into `palm1/palm2/palm3`, matching stock GOH skins so hand IK/FK lines up. Off = legacy MOWAS2 single-bone palm |
| **GFA Long-Arm Segment Scaling** | **on** | Scales source upper-arm/forearm segments to GFA long-arm proportions. It **does not** replace your chosen target skeleton |
| **Clamp Palm Chain** | off | Clamps hand-chain length ratios to 0.85–1.15 to stop wrist-to-palm separation |
| **Restore Wrist Anchor and Geometry** | **on** | Restores the real `hand_rot1` pivot and recovers wrist geometry compressed by old shoulder scaling |
| **MMD Finger Curl (40°)** | off | Applies the GFA inward finger curl. Off by default because a uniform 40° curl everts fingers on some models. KK sources always apply it |
| **Pupil Geometry Clearance Safety** | **on** | Pushes pupils out only for real penetration or near-coplanarity |
| **Pupil-Sclera Clearance** | `0.006` | Minimum gap along the eye normal and forward view |

**Torso and material behaviour**

| Property | Default | What it does |
|---|---|---|
| **Upper Torso Width (ik_updown)** | off | Scales only lateral coordinates finally mapped to `ik_updown`; rest bones stay fixed |
| **Upper Torso Lateral Factor** | `1.0` | `>1` widens the `ik_updown` region slightly |
| **Reduce Torso IK Influence** | off | Merges part of `ik_leftright` into `ik_updown` to reduce torso stretching on deep bends |
| **ik_leftright Retention** | `0.5` | Fraction of `ik_leftright` weight to keep |
| **Use Alpha Test for Transparent Materials** | **on** | GOH-style `alpharef 127` + blend test. Eyes/eyeblend keep soft alpha; constant alpha=0 layers are omitted |

**Vertex Limit Handling** (the sub-box inside Advanced)

| Property | Default | What it does |
|---|---|---|
| **Lossless Record Splitting** | off | When on, partitions finalized records across multiple PLY files. Every file keeps positions, weights, loop normals and UVs and is attached directly to the existing `skin` bone. **Takes priority over decimation** |
| **Records per PLY** | `65535` | Limit per file, range `3..65535`. Keep `65535` for native hard-limit handling; lower it to deliberately force more parts |
| **Enable Decimation** | off | Protected COLLAPSE decimation, used only when splitting is unavailable. **Leave off** unless you must reduce geometry |
| **Protect Face Details** | on | Keeps face geometry fixed during decimation |
| **Protect Tight Clothing Shells** | on | Stops tight clothing from penetrating skin during decimation |

> **Recommended starting point for a high-poly source:** turn **Lossless Record Splitting ON** and leave **Enable Decimation OFF**. You keep 100% of the geometry. Decimation is an optimisation, not a requirement — most PMX/KK models are well under the limit once records are split.

### 5.5 Step 4 — Full export

Back at the top of the panel, in the **Step 4: Full Export** box:

1. **Entity Name** — the GOH resource/entity name.
   - Rules: **1–64 characters**; ASCII letters, digits, underscores or hyphens. **The first character must be a letter or a digit** — a leading `_` or `-` is rejected.
   - Example: `agf_liufenyi`
   - The panel shows the resolved output: `Entity files: <name>.def / <name>.mdl / <name>.ply`
   - If the name is invalid the box shows an error and the export button is greyed out.
2. **Output Root** — the **parent** directory. The exporter creates an `<Entity Name>` subfolder inside it containing all model files and textures. (Default is your Desktop — change it to your working `export_test` folder.)
3. **Texture Format**:
   - **TGA (Built-in)** — recommended for beginners. 32-bit TGA, no external dependency.
   - **DDS (NVTT)** — BC1/BC1a/BC3 with mipmaps for interoperability with other GOH mods. Requires an external NVIDIA Texture Tools CLI (`nvtt_export.exe` or `nvcompress.exe`). If NVTT is not found the panel tells you.
4. **Adapt for Toon Shader** — on by default for GOH. Emits GFA ToonShaderConvert-compatible bump/full_specular materials; requires Workshop item **3565678181** to be installed and enabled in-game. Forced **off** on the MOWAS2 route.
5. Click **Full Export (Bind + Decimate + PLY)**.

A confirmation dialog appears summarising your settings. Check it, then confirm.

**What Step 4 actually does, in order:**

```text
0.  save a snapshot blend  (mowas2_auto_backup.blend in the output folder)
1.  remove rigid bodies and joints
1.5 remove duplicate objects sharing the same mesh data
2.  detect source mirroring  (standard MMD sources skip this)
3.  Umeyama rigid fit + place on ground
3.2 build narrow-shoulder alignment target (plan C, if enabled)
3.25 GFA bone alignment
4.  pose arms, 4.5 pose hands
5.  FREEZE the mesh  (bake evaluated geometry; shape keys baked here)
5.05 bind back to the original target (plan C)
5.2+ body curve, ankle junction, arm inset/thickness, shoulder inset,
     neck follow, pupil clearance
6.  bind and TRANSFER WEIGHTS to the GEM2 target, including hand, arm,
     leg, head, neck and eye fixes
7.  optional protected decimation (or bypassed when splitting handles it)
8.5 export PLY / MDL / DEF / MTL + textures, with transactional rollback
```

**Check:** the panel's **Latest Result** box reports:

```text
Export complete: <output dir>
```

Console: `GOH pipeline complete: <dir>`.

Save checkpoint `06_exported.blend`.

### 5.6 Important: about the frozen mesh

After Step 3/4 the mesh is **frozen** — its geometry has been baked and it carries a marker. Two consequences:

1. **Changing alignment options after freezing will not silently work.** If you change foot size, shoulder width, head scale or applied pupil options on an already-frozen mesh, the add-on detects the mismatch and **requires the source PMX** so it can re-import and re-align cleanly.
   - Error you may see: *"Foot-size or shoulder-width settings differ from the frozen snapshot, but the source PMX is unavailable. Select the source PMX and retry."*
   - Fix: make sure the **PMX Model File** field still points at your `.pmx`, then re-run.
2. Some options (body curve, arm inset, neck follow, pupil clearance) are designed for **live reversible preview after freezing**. Those you can adjust freely and re-export.

**If you need a fresh start:** re-import the PMX (Step 1), rebuild the target (Step 2), align (Step 3), export (Step 4). Do not try to "unfreeze" by hand.

### 5.7 What you get

```text
export_test/
    <entity_name>/
        <entity_name>.def
        <entity_name>.mdl
        <entity_name>.ply
        <entity_name>_split01.ply     <- only if the record limit required it
        <entity_name>_split02.ply
        <entity_name>.mtl
        <textures>.tga  (or .dds)
    mowas2_auto_backup.blend
```

Every PLY is a **direct `VolumeView` on the existing `skin` bone**. No carrier bone, no `LODView` wrapper. This matches verified working GOH/GFA multipart characters.

---

## 6. Route B — Existing GEM2 Human Rest Conversion

**Use this when:** you already have a **skinned GEM2 human** (imported from a `.ply`, with GEM2-named vertex groups) and you need it in the **other game** — GOH → MOWAS2 or MOWAS2 → GOH.

This is **not** a PMX importer. It does not import PMX, does not run PMX alignment, does not run GFA bone alignment, and does **not** remap weights. It only changes mesh coordinates and rebinds the existing Armature modifier to the destination armature.

### 6.1 Preconditions

- The mesh is already imported and has meaningful vertex groups named after GEM2 bones.
- Both armatures carry the metadata `gem2_world_mats`, `gem2_parents`, `gem2_mesh_parent`, `gem2_mdl_path`.
- Both armatures are in **Object mode with an identity pose**. A posed mesh is **rejected** rather than silently baking an animation pose.
- The two MDLs have matching weighted bone names and parent hierarchy.
- The destination MDL contains exactly one direct `VolumeView` attachment parent for this human skin.

### 6.2 Procedure

1. Import your existing GEM2 human (`File > Import > GEM2 Complete Model Folder` or `GEM2 PLY (Auto Multipart)`).
2. In the **GOH Auto Transfer** panel, find the **GEM2 Human Rest Conversion** box.
3. Set the **destination MDL first** — use the **Export Route** + **GEM2 Target Skeleton .mdl** controls (Step 2 box). Select the destination route/MDL **before** converting.
4. Select the mesh in the viewport.
5. Optional: enable **Duplicate Mesh Before Conversion** (default **off**) if you want the source mesh to remain visible for A/B comparison.
6. Click **Convert Human Rest Space**.
7. Then click **Export Converted Human** — this sends the converted mesh through the normal `PLY / MDL / DEF / MTL` exporter **without** re-running the PMX pipeline.

### 6.3 Reversing a conversion

- **Exact Reverse** is **on** by default. When enabled and the mesh was converted by this version, each vertex uses the inverse of its own previously blended affine matrix — giving stable A→B→A round trips.
  - (A blend of inverse bone matrices is *not* the inverse of a blend. That is why this option exists.)
- To reverse: select the converted mesh, choose the **original** route as destination, leave **Exact Reverse** enabled, and convert.

### 6.4 Good to know

- Only **Blender display bones** change. Raw MDL metadata is never rewritten — it remains the export authority for animation.
- Vertex groups keep their **names**; they are not physically reordered. The destination-specific SKIN palette order is stored in the mesh property `gem2_skin_order` and used by the exporter. GOH and MOWAS2 orders are handled independently.
- The PMX alignment buttons **refuse** converted meshes, and refuse targets still referenced by them, so the two display contracts cannot silently overwrite each other.
- A pure legacy PMX source or target without raw MDL metadata is **rejected** rather than rewritten. Vehicle and non-human imports are left untouched.

---

## 7. Route C — Manual Porting (fully by hand)

Use this when the automatic route does not support your model, or when you need to repair or fully control the result. The add-on is used only for **import, inspection, analysis and export** — the real work is ordinary Blender.

```text
prepare a clean working file
      -> bring in one verified GEM2 target armature
      -> import the source mesh
      -> clean the scene
      -> build a bone-mapping sheet
      -> align the source to the target rest pose
      -> bind the mesh to the target armature with EMPTY groups   (7.6)
      -> check the auto-created target vertex groups              (7.7)
      -> assign solid regions in Edit Mode                        (7.8)
      -> soften joints in Weight Paint                            (7.9)
      -> test target poses and repair coverage                    (7.10)
      -> check UVs, materials and textures
      -> Analyze, then export
      -> package in a separate test mod
```

> **Set your expectations.** Steps 7.1–7.5 are preparation and take minutes. **7.6–7.10 are the actual work** and will take hours on your first model. That is normal — weighting is a skill, not a button. Budget a full session for your first character and do not rush the pose testing in 7.10; it is what separates a working port from one that explodes in-game.

If you have never weighted anything before, the only three ideas that matter are:

1. **Every vertex must belong to at least one group.** Ungrouped vertices fly to the origin.
2. **A vertex should belong to as few groups as possible** — one for solid regions, two across a joint. On the human-skin export path only two influences per vertex actually survive (see 2.3).
3. **A joint needs a soft transition band** between the two bones, not a hard edge.

Everything below is detail on those three rules.

### 7.1 Choose one target arm family (do this first)

| Intended animation family | Target rest template |
|---|---|
| Short-arm stock GOH human | `samples/goh_skin.mdl` |
| Long-arm GOH/GFA human | `samples/goh_skin_gfa.mdl` |
| MOWAS2 human | A verified MOWAS2 / medicgirl target |

**Never** do these:

- align to `goh_skin.mdl` and export with `goh_skin_gfa.mdl`;
- copy weights between arm families without re-checking the rest pose;
- use a MOWAS2 target for a GOH skin;
- use a target mesh from one family with an armature from another.

Do not use a random character skin `.mdl` just because it has the same number of bones. Bone *names* alone do not prove animation compatibility.

### 7.2 Bring in a verified target skeleton

Best sources, in descending reliability:

1. A supplied/verified `.blend` containing the exact target armature and its GEM2 rest metadata. Use `File > Append` → open the target's `Collection` or `Object` folder → append only the armature. **Do not open the target file in place** and lose your source scene.
2. A complete target GEM2 model folder (`.mdl` + `.ply` + `.mtl` + textures).
3. A known-good target `.ply` with its matching `.mdl` in the same folder.

**Easiest beginner path:**

1. Start from `00_empty.blend`.
2. `File > Import > GEM2 Complete Model Folder`.
3. Select the folder containing the target MDL and its PLY files.
4. Confirm; wait for the shared armature and reference mesh.
5. Save as `01_target_reference.blend`.

> If you have only a text `.mdl` and no verified Blender target or matching PLY, **do not** create 58 bones by hand from approximate coordinates. Obtain a matching reference first. A visually similar skeleton with slightly different rest matrices will deform every animation incorrectly.

**Verify the target before touching the source:**

- `basis` is the root;
- `skin` is the human mesh attachment bone;
- bone count matches the template (normally **58**);
- the armature is not globally scaled or posed;
- the reference mesh and armature belong to the same template.

Rename the reference mesh to something unmistakable, e.g. `REF_goh_gfa_skin`. **Never rename target bones.**

Use separate collections to keep things straight:

```text
TARGET_REFERENCE   <- target armature + reference mesh
SOURCE_MODEL       <- your imported source
WORKING_EXPORT     <- the mesh you will actually export
```

> If an imported target shows an odd-looking bone tail, **do not** enter Edit Mode and drag it to make the picture nicer. Use a verified target `.blend` or a corrected importer build instead.
>
> On this target: never use `Armature > Recalculate Roll`, never move heads/tails, never change lengths, never `Apply Pose as Rest Pose`.

### 7.3 Import and clean the source

- PMX → the `mmd_tools` import entry.
- FBX / OBJ / glTF / GLB → Blender's normal importer.

Preserve what you need:

- keep the source UV map;
- keep source materials and image links;
- keep the source armature **if** it helps you pose or inspect weights;
- **avoid Remove Doubles** when it would weld UV or hard-normal seams;
- use the source format's documented scale rather than guessing a factor.

Then identify, in the Outliner:

- the visible source mesh that will become the export mesh;
- any source armature currently deforming it;
- rigid-body, joint, physics or helper objects;
- duplicate meshes created by the importer.

Open the **Modifiers** tab on the source mesh and record whether it has an Armature modifier, and **which object** it points at. A modifier named `Armature` that points at a *mesh* is invalid.

Remove or unlink (after saving a checkpoint):

- MMD rigid bodies and joint empties;
- old target armatures from previous attempts;
- scans / reconstruction meshes;
- duplicate body meshes;
- unneeded cameras and lights.

Keep intentional mesh parts separate until you have inspected their UVs and materials. **Do not join blindly** — joining merges material slots and makes it hard to tell which mesh holds the valid modifier.

### 7.4 Build a bone-mapping sheet

Before painting, write down how source anatomy maps to target groups. Source names vary between PMX, FBX and Blender rigs — the left column is **examples**, not names to type.

| Source anatomy / bone | GOH/GFA target group | Region |
|---|---|---|
| pelvis, hips, lower torso | `body` | main torso and pelvis |
| spine, chest, waist | `body` (and only a verified `ik_updown` use) | torso bend support |
| head, face, jaw | `head` | head and face |
| left thigh | `foot1l` | upper left leg |
| left calf / shin | `foot2l` | lower left leg |
| left ankle / foot | `foot3l` | left ankle and foot |
| right thigh | `foot1r` | upper right leg |
| right calf / shin | `foot2r` | lower right leg |
| right ankle / foot | `foot3r` | right ankle and foot |
| left clavicle / shoulder | `clavicle_left` | left shoulder transition |
| left upper arm | `hand1l` | left upper arm |
| left forearm | `hand2l` | left forearm |
| left wrist / hand root | `palm1l` first; `hand_rot1l` only if the target uses it | wrist/palm transition |
| left palm / finger chain | `palm1l`, `palm2l`, `palm3l` | only if the target uses the split |
| right clavicle / shoulder | `clavicle_right` | right shoulder transition |
| right upper arm | `hand1r` | right upper arm |
| right forearm | `hand2r` | right forearm |
| right wrist / hand root | `palm1r` first; `hand_rot1r` only if the target uses it | wrist/palm transition |
| right palm / finger chain | `palm1r`, `palm2r`, `palm3r` | only if the target uses the split |

> Note the naming convention: in GEM2, **`foot1/2/3` are the legs/feet** and **`hand1/2` are the arms**. This trips up everyone at least once.

To build the map without editing the target:

1. Select the target armature, keep it in **Rest Position**.
2. Inspect one target bone name at a time in Armature Data properties.
3. Select the **source** armature and enter Pose Mode only to learn what each source bone moves.
4. Write the pair down. **Do not rename the target bone to match the source.**
5. Clear the source pose with `Pose > Clear Transform > All`.

**What manual skeleton transfer means:**

```text
source anatomy + source weights
      -> map to target bone names
      -> assign/paint those target groups on the mesh
      -> keep the ORIGINAL target rest skeleton for export
```

It does **not** mean joining two armatures or copying source rest bones into the target. Target bone heads, tails, lengths, rolls, parents and rest matrices must stay **unchanged**.

### 7.5 Align the source to the target rest pose

**Use only a rigid similarity fit**: rotation + translation + **uniform** scale. No shearing, no per-axis scaling.

1. Make target bones visible: select the armature → Object Properties → `Viewport Display` → enable **In Front**.
2. Keep the target in **Rest Position**.
3. Move/rotate/scale the **source mesh** (Object Mode) so it matches the target: feet on the ground, knees at target knee height, shoulders at target clavicle height, head at target head position.
4. Align arms: match shoulder → elbow → wrist to `hand1` → `hand2` → `palm1`.
5. Check left/right **before** painting any weights. It is dramatically cheaper to fix a mirrored source now than to repaint later.

Accept the alignment only when:

- the source is not obviously stretched or squashed;
- left/right are on the correct sides;
- arms and hands land near the target chain;
- the head, hands and feet are on the correct sides and at plausible heights.

> If alignment required a non-uniform scale, **stop**. The source and target proportions are incompatible for a rigid transfer; reconsider the target family or accept manual geometry work.

**Alignment is trial and error, and that is normal.** Nobody gets it right on the first try. The loop is:

```text
move / rotate / scale  ->  look from front and side  ->  wrong? Ctrl+Z, try again
```

- `G` to move, `R` to rotate, `S` to scale. **Middle-click drag** or right-click cancels an in-progress transform; `Ctrl+Z` undoes a finished one.
- Check from **Numpad 1** (front) and **Numpad 3** (side) every time. A model can look correct from one angle and be obviously wrong from another.
- Confirm **left/right early** — it is far cheaper than discovering a mirror mistake after you have painted weights.
- Do not over-polish. Alignment only needs to be *close enough* for the weights to make sense.

Save `02_aligned.blend`.

### 7.6 Bind with empty groups

Before binding, the mesh must be **free of the source rig** and have a **clean transform**. Order matters here — do not skip ahead.

1. Save a checkpoint: `02b_before_bind.blend`.
2. Remove the source deformation link **only now**. Select the mesh → **Modifiers** tab (wrench icon) → on the Armature modifier click the small **`X`** (or the dropdown arrow → **Delete**). The mesh should now have **no** Armature modifier.
3. If the mesh is also parented to the source armature, clear that too: select the mesh → `Alt+P` → **Clear and Keep Transformation**.
4. Only now, normalise the transform: select the mesh → `Ctrl+A` → **Apply > Scale** (add **Rotation** if you rotated it).
   - **Do this after removing the source rig.** Applying a transform while a source Armature modifier is still active bakes whatever deformation it was applying.
   - A mesh with unapplied non-uniform scale deforms wrongly later, and the numbers still look fine — which makes it miserable to debug.
   - Skip only if the scale already reads exactly `1.0 / 1.0 / 1.0` in the Item / N-panel transform readout.

Then bind:

5. In the viewport: **left-click the mesh**, then **`Shift`+left-click the target armature**.
   - The order matters: the **armature must be the active object** (brightest outline). `Ctrl+P` parents to the *active* object, so clicking in the wrong order silently parents the wrong way.
6. Press `Ctrl+P` → **Armature Deform → With Empty Groups**.
   - Menu equivalent: `Object > Parent > Armature Deform > With Empty Groups`.
   - If the **Armature Deform** submenu is missing or greyed out, your active object is not an armature — re-check step 5.

**Why empty groups:** it creates the Armature modifier **and** one empty vertex group per deform bone, named exactly after the bone — with **zero weights everywhere**. Nothing is guessed. You will fill them deliberately.

**What you should see now** — check all three before continuing:

- [ ] The mesh has exactly **one** Armature modifier, and its **Object** field shows the *target* armature.
- [ ] The mesh's Vertex Groups list now contains a group for every **deform** bone.
- [ ] You see **fewer groups than the 58 bones** — this is correct and expected. The target skeleton contains helper and IK bones that are **not** deform bones, and only deform bones get a group. The exact count depends on your target MDL, so **compare against that MDL** rather than against a fixed number.

> **If you see far fewer groups than 58, that is normal** — many of the 58 bones are helper/IK bones. But if you see **zero** groups, the bind did not take effect — undo and redo step 5.

**Do not** while parenting:

- use **With Automatic Weights** (it produces plausible-looking but wrong GEM2 weights — the single most common beginner mistake on this route);
- apply the modifier;
- scale the armature;
- mirror the mesh;
- bind twice (two Armature modifiers = two active skeletons = broken export).

### 7.7 Check the target vertex groups

> **You do not need to create these by hand.** The "With Empty Groups" bind in 7.6 already created one correctly-named group per deform bone. Your job here is to **verify** them, fix any gaps, and delete the junk. Manually retyping bone names is a good way to introduce typos that silently break the port.

**Step 1 — compare the list against the target bones.**

Open **Object Data Properties** (green triangle icon) → **Vertex Groups**. You should see a group for each deform bone, typically including:

```text
body
head
clavicle_left      clavicle_right
hand1l  hand2l     hand1r  hand2r
palm1l             palm1r
foot1l  foot2l  foot3l
foot1r  foot2r  foot3r
```

You will very likely also see groups for these, because they exist on the standard GOH/GFA human skeletons. They will already have been created by the bind — the question is only whether you put **weight** in them:

```text
palm2l  palm3l     palm2r  palm3r     finger-chain palm bones.
                                      Give them weight only when using the GFA
                                      split-hand convention (GFA Hand Split on).
                                      Otherwise let the whole hand sit on palm1*.
hand_rot1l         hand_rot1r         wrist roll bone
hand3l             hand3r             extra hand segment on some targets
ik_updown  ik_leftright               torso IK helpers — weight only if your
                                      verified reference actually uses them
basis                                 root — only if the reference weights it
```

Also expect a number of groups ending in `_hide` (for example `palm2l_hide`, `palm4r_hide`) plus `ik_chain*`, `palm_ik_holder_*`, `placement`, `foresight2rot`, `left_hand`, `right_hand`, `visor`, `gun_back`, `bone03`, `bone05`, `bone06`, `bone07`. **Leave these empty.** They are helper and IK bones, not deform bones you should weight.

> **Rule of thumb:** if you cannot point to the matching bone in your reference skeleton and explain what it should move, leave the group empty. An empty group is harmless; a wrongly-weighted one is not.

**How to get a bone name right without retyping it:** select the armature, enter **Edit Mode**, click the bone, and read the name in the Item panel (or Properties → Bone → Name). Copy it from there. Never invent a name from the mapping table in 7.4.

**Step 2 — delete or quarantine source-only groups.**

Any group whose name does **not** match a target deform bone is foreign. Select it and click the **`–`** button in the Vertex Groups list.

Leftover foreign groups are a common cause of "the mesh barely moves" or "it moves with the wrong bone", because the exporter may still pick up their weights.

**Step 3 — sanity check the count.**

You should now have **only** target-named groups. The count varies by target MDL, so judge it by *reading the names*, not by matching a number. If you have dozens of leftover source groups, something went wrong earlier — go back to `02b_before_bind.blend` and check that you actually removed the source Armature modifier before binding.

### 7.8 Assign solid regions in Edit Mode

Do the heavy structure in Edit Mode; use Weight Paint **only** for softening later.

#### The one thing that trips up everyone: `Assign` adds, it does not replace

In Blender's Edit Mode, **Assign** adds the vertex to the selected group at the chosen weight. It does **not** remove it from any other group.

So if you assign the whole torso to `body` at 1.0, and then assign the thigh to `foot1l` at 1.0 **without removing it from `body` first**, those thigh vertices now belong to *both* groups at 1.0 each. After normalisation they become 0.5/0.5 — and in-game, raising the leg drags half the torso with it.

> **Rule: when moving vertices from one group to another, always `Remove` from the old group first, then `Assign` to the new one.**

#### Recommended workflow: "flood fill, then carve"

This is much faster than selecting each region from nothing, and it makes it **impossible to leave a vertex ungrouped**.

1. `Tab` into **Edit Mode** on the mesh.
2. Press `A` to select **everything**.
3. In **Object Data Properties → Vertex Groups**, select the **`body`** group, set **Weight** to `1.0`, click **Assign**.
   - Every vertex is now safely in `body`. Nothing can collapse to the origin.
4. Now carve out one region at a time:

```text
select the left thigh  ->  choose group "body"        ->  click Remove
                       ->  choose group "foot1l"      ->  click Assign
```

5. Repeat for each region:

| Region | Remove from | Assign to |
|---|---|---|
| Left thigh | `body` | `foot1l` |
| Left shin | `body` | `foot2l` |
| Left foot | `body` | `foot3l` |
| Right thigh / shin / foot | `body` | `foot1r` / `foot2r` / `foot3r` |
| Head and face | `body` | `head` |
| Neck (if separate) | `body` | `head` or `clavicle_*` per reference |
| Left upper arm | `body` (or `clavicle_left`) | `hand1l` |
| Left forearm | `hand1l` | `hand2l` |
| Left hand | `hand2l` | `palm1l` |
| Right arm chain | same pattern | `hand1r` → `hand2r` → `palm1r` |

Work **down the chain** (thigh → shin → foot). Each step carves out of the previous one, so no vertex is ever left behind.

#### Useful selection tricks

| Need | Do this |
|---|---|
| Select a box | `B`, then drag |
| Freehand lasso | `Ctrl` + drag left mouse |
| Select an edge loop around a limb | `Alt` + click the loop |
| See inside the body | `Alt+Z` (X-ray), or select outer faces and press `H` to hide them — `Alt+H` brings them back |
| Select everything currently in one group | Select the group, click **Select** in the Vertex Groups panel |
| Grow / shrink the selection | `Ctrl`+Numpad `+` / `−` |

> The `L` key selects all *connected* geometry. On a single closed body mesh that usually means **the entire model**, which is not what you want. Prefer box/lasso/loop selection.

#### Halve the work with mirroring

If your mesh is genuinely symmetric, you can do one side and mirror it:

1. Finish the **left** side groups.
2. In **Weight Paint** mode: `Weights > Mirror Vertex Group`.
3. This requires matching `l` / `r` group names and symmetric topology. It silently does nothing on asymmetric meshes — check the result instead of trusting it.

#### Find vertices with no group

This is the single most important check, and the "flood fill" method above should make it pass trivially. Verify anyway:

1. `A` to deselect, then `Select > Select All by Trait > Ungrouped`.
2. The selection should be **empty**.
3. If that menu is not available in your Blender build, switch to **Weight Paint** mode instead: ungrouped vertices render as **deep blue** (weight 0). Anything blue on a used triangle is a bug.

> Any ungrouped vertex that is part of a used triangle will collapse to the origin in-game. This is the #1 cause of "my model explodes into spikes when the animation plays".

Save `03_bound_empty_groups.blend`.

### 7.9 Soften joints in Weight Paint

1. Switch to **Weight Paint** mode (mode selector at the top-left of the 3D Viewport).
   - The mesh now shows the active group's weights as a heat map: **red = 1.0**, green = ~0.5, **blue = 0.0**.
2. **To inspect a group:** click it in the Vertex Groups list. The colours update to show that group alone. Get in the habit of clicking through every group once and watching the colours — wrong regions become obvious immediately.
3. Enable **Auto Normalize**. Find it in the **Tool** settings while in Weight Paint mode (the Options area; if the sidebar is hidden, press `N`). It makes painting one group automatically remove weight from the others, keeping each vertex's total at 1.0.
4. **Lock** every group you are not currently editing — the padlock icon in the Vertex Groups list. Locked groups cannot be painted, which prevents you from ruining finished regions with a stray stroke. Unlock one group at a time.
5. Use a **low Strength** (start around 0.2–0.3) and small radius, with smooth falloff. Press `F` to resize the brush interactively, `Shift+F` to change strength.
6. Work joint by joint: shoulder, elbow, wrist, hip, knee, ankle, neck, waist.
7. Use the **Blur** brush (or `Weights > Smooth`) to even out transitions, and **Average** to flatten a patchy area.
8. Re-check that **Auto Normalize** is still on after every pass — it can be reset by mode changes.

**How wide should a joint transition be?** As a rule of thumb, the blend zone should be roughly **half the thickness of the limb** at that joint. Too narrow and the mesh tears when bent; too wide and the joint feels rubbery and the neighbouring bone drags the mesh.

Brush cheat-sheet:

| Brush | Use it for |
|---|---|
| **Draw** (Add) | Building weight up |
| **Draw** (Subtract, hold `Ctrl`) | Taking weight away |
| **Blur** | Softening a harsh boundary — your most-used brush |
| **Average** | Flattening a patchy/noisy region |
| **Smear** | Pushing weight along the surface |

> If the brush seems to do nothing, check in order: is the group **unlocked**? Is the correct group **selected**? Are you in **Weight Paint** mode and not Vertex Paint? Is **Auto Normalize** fighting your stroke?

#### The paint → pose → undo loop

Weight painting is where `Ctrl+Z` earns its keep. The rhythm is:

```text
paint a joint  ->  Ctrl+Tab into Pose Mode  ->  rotate that bone  ->  look
   ->  tears or dents?  Ctrl+Tab back, Ctrl+Z the stroke, paint differently
   ->  looks right?  Clear Pose, move to the next joint
```

- **Pose-test after every joint**, not at the end. Finding a bad elbow after painting all 20 groups means unpicking a lot of work.
- Always clear the pose (`Pose > Clear Transform > All`) before painting again — painting on a posed mesh gives misleading results.
- **Brush Strength low, and many passes.** One heavy stroke that ruins a region costs an undo; ten light passes can be tuned.
- If undo goes too far or not far enough, use `Edit > Undo History` to jump to an exact point. This is why 1.4 recommends raising **Undo Steps** to 128–256 *before* you start painting.
- Save `04_weighted.blend` as soon as a full pass looks acceptable, so later experiments have a floor to fall back to.

Recommended external reference for the brushwork: Yami 3D, *Weight Painting Complete Guide!! (Blender 3D)* — its empty-group, Auto Normalize, group-locking, smoothing and pose-testing workflow fits fixed GEM2 bone names much better than Rigify- or auto-weight-centred tutorials.

#### Normalize and limit influences

Both commands live in the **`Weights`** menu and are only available in **Weight Paint** mode.

1. `Weights > Normalize All` — makes every vertex's weights sum to exactly 1.0.
   - Do this **after** every major painting pass, not just at the end.
2. `Weights > Limit Total` — set the limit to **2**.
   - Why 2: the human-skin layout this add-on writes (`D3DFVF_XYZB2`) only carries **two** influences per vertex, so a third or fourth influence would be silently dropped on export. Limiting to 2 in Blender means what you see in the viewport is exactly what the game will do, so you catch problems *now* instead of after export. (The engine format itself can hold up to 4 — see 2.3 — but not on this export path.)
   - With **Auto Normalize** on, painting already keeps totals near 1.0; Limit Total is the final safety net.

**Verify before moving on:**

- [ ] Click through every group — no group is empty, and no group covers the wrong body part.
- [ ] No deep-blue (0.0) vertices anywhere on a used triangle.
- [ ] `Weights > Normalize All` has been run.
- [ ] Total influences per vertex ≤ 2.

Save `04_weighted.blend`.

### 7.10 Test poses

Pose testing is how you find out whether your weights are actually correct. A model that looks perfect standing still can fall apart the moment a bone moves.

1. Select the **target armature** (not the mesh) → `Ctrl+Tab` → **Pose Mode**.
2. Click a bone and press **`R`** to rotate it. **Test to extreme angles** — at least 90°, and 120°+ for elbows and knees. Small rotations hide problems that a real animation will expose immediately.
3. Work through the list below.

| Test | Bone to rotate | What "correct" looks like |
|---|---|---|
| Elbow | `hand1l`, then `hand2l` | Forearm bends at the elbow; upper arm stays put |
| Shoulder | `hand1l` from the shoulder end | Whole arm swings; torso does not deform |
| Knee | `foot1l`, then `foot2l` | Shin bends at the knee; thigh stays put |
| Hip | `foot1l` from the hip end | Whole leg swings; pelvis barely moves |
| Ankle | `foot3l` | Foot rotates; shin stays put |
| Head | `head` | Head turns; shoulders and hair do not stretch weirdly |
| Waist | `body` | Torso bends; legs stay put |
| Wrist | `palm1l` | Hand rotates; forearm stays put |
| Both sides | repeat on `r` | Left and right behave identically |

4. Clear the pose with `Pose > Clear Transform > All` before you go back to painting.

> **Never** pose the armature to make it fit the mesh. Pose only to *test*.
>
> The model is supposed to return exactly to its rest shape when you clear the pose. If it does not, you have an unnormalised or double-assigned weight.

#### Reading the failures

| What you see when posing | Likely cause | Fix |
|---|---|---|
| A spike or a vertex shoots to the origin | Ungrouped vertices | `Select All by Trait > Ungrouped`, assign them |
| Mesh tears apart at the joint | Transition band too narrow, or the two sides of the joint have no shared weights | Blur across the joint; make sure vertices on both sides carry a little of both bones |
| A dent or pinch at the joint | Not enough weight in the crease | Add a little of both bones into the crease |
| Moving the leg drags the torso | Vertices are still in **both** groups (the `Assign` mistake from 7.8) | Select the region, `Remove` from the wrong group |
| A region does not move at all | Group name typo, or group not selected when painting | Check the name against the bone, character by character |
| Whole mesh moves as one block | Armature modifier points at the wrong object, or weights are all on one bone | Check the modifier's Object field |
| Deformation looks correct but mirrored | Left/right groups swapped | Re-check the `l`/`r` suffix on every group |

### 7.11 Check UVs, materials and textures

- **Material slots must actually be assigned to faces.** A slot that exists but is not assigned to any face disappears on export.
- Inspect the UV map without destroying seams: confirm the intended UV layer is active and seams are intact.
- Open the actual images and confirm they contain coherent pixels — not scrambling, not blank, not partially written.
- Confirm the intended alpha mode. GOH-style transparent materials use `alpharef 127` + blend test (the **Use Alpha Test for Transparent Materials** option); eye materials keep their own contract.
- Every MTL and texture reference must exist **beside** the generated entity.

> The add-on has a guard for "generated texture/output artifacts that disappear during export": if it detects that, it rebuilds the entity once from the original Blender image sources.

### 7.12 Analyze, then export

With your finished mesh selected, go to the **Imported Model Multipart Export** box in the panel (or `File > Export > GEM2 Multipart Model (.mdl)`):

1. Set **Records per PLY** (default `65535`; lower it to deliberately force more parts).
2. Click **Analyze** — a **read-only** check. It reports something like:

```text
{meshes} meshes, {triangles} triangles, {records} exact records:
{parts} PLY file(s) at limit {limit}
```

3. Read the result. If it says 1 part and records ≤ 65535, you are clear. If more parts are needed, that is fine and lossless.
4. Click **Export**.

The exporter:

- encodes two-influence GEM2 records (the `D3DFVF_XYZB2` human-skin layout — see 2.3);
- keeps loop UV seams and custom loop normals;
- deduplicates identical final records without changing Blender topology;
- writes PLY / MDL / DEF / MTL + textures;
- runs self-checks and warns if materials collapsed to a single MESH block, if duplicate triangles appeared, if the palette group count changed, or if the vertex stride is non-standard (expected **40** for skinned human skin).

> **Selection matters.** The generic exporter works on **selected** meshes. Select only the meshes belonging to this character — never the reference mesh.

### 7.13 MDL attachment and packaging

**The `skin` VolumeView rule:** every simultaneous PLY must be a **direct `VolumeView` under the original `skin` bone**. No carrier bone, no simultaneous `LODView` wrapper. Newly added carrier bones can trigger animation-reset behaviour in-game.

Inspect the generated folder:

- `.def` names the generated `.mdl`;
- `.mdl` references files that actually exist;
- `.ply` record count ≤ 65,535 per file;
- PLY palette slots and `SKIN` names use the same mapping;
- textures are present and correctly named.

Then copy the entity into a **separate test mod** — never into the game's own folders — and test it with a neutral animation plus one or two representative ones (idle, walk, fire).

---

## 8. Route D — Generic Multipart Export

**Use this when:** you already have a Blender-imported mesh (PMX, FBX, OBJ, glTF, anything) that is **already bound to a compatible GEM2 armature** with correct GEM2 vertex groups, and you only need to write GEM2 files.

This path is **source-format independent**. It does **not** retarget or make an arbitrary rig GEM2-compatible — it writes what you already have.

1. Select the compatible meshes.
2. In the panel's **Imported Model Multipart Export** box, set **Records per PLY**.
3. Click **Analyze** to inspect exact records / required parts (read-only).
4. Click **Export**, or use `File > Export > GEM2 Multipart Model (.mdl)`.

What the exporter evaluates: active shape keys and non-armature modifiers are evaluated; Armature modifiers retain stack order against an isolated rest-pose rig.

Guards to be aware of:

- Mixed bound/unbound selections are **rejected**.
- Skinned vertices without valid weights are **rejected**.
- Generated PLY files become direct sibling `VolumeView` entries on the **existing attachment bone**. No `LODView`, no carrier bone.
- You will be warned if vertices were reduced to two influences (the human-skin export limit — see 2.3).

---

## 9. Vehicles

The **Vehicle** box in the panel handles whole vehicle folders.

| Button | What it does |
|---|---|
| **Import Vehicle Folder (.mdl + .ply + .vol)** | Imports a complete vehicle folder and reconstructs its MDL hierarchy in Blender |
| **GOH to MOWAS2 (PLY + MDL + VOL)** | Reversibly hides unsupported GOH MDL sequence events |
| **MOWAS2 to GOH (PLY + MDL + VOL + Vanilla DEF)** | Restores sequence events and generates a DEF using only stock GOH references and the actual volume names found in the MDL |

Notes:

- Imported GEM vehicles with the conventional `basis` Y-reflection are shown through an **unapplied display root transform**, so left/right parts read naturally in Blender while native MDL/PLY matrices stay intact.
- You may move the root as a scene locator, but **do not apply its scale before vehicle export**. Generic FBX export may retain the deliberate negative-scale display transform.
- The GOH DEF template uses a stock **122 mm placeholder** weapon configuration and preserves the original file as `*.def.mowas2.bak`.
- These tools focus on **model and entity-format compatibility**, not gameplay/balance data. The generated GOH DEF deliberately uses verified vanilla placeholder resources.
- Generic `File > Export > FBX Model` produces FBX and automatically includes bound rigs and required parent empties.

---

## 10. Validation, Packaging and In-Game Testing

### 10.1 Re-import your own output before touching the game

This is the fastest way to catch most mistakes:

1. `File > Import > GEM2 Complete Model Folder` (or `GEM2 PLY (Auto Multipart)`).
2. Select your generated entity folder.
3. Confirm the model comes back looking like what you exported, with the armature intact.

The add-on also ships a command-line comparison utility (`diff_ply.py`) if you want to diff two PLY files byte-wise.

### 10.2 PLY facts to verify

- Record count ≤ **65,535** per file (or losslessly split).
- Skinned human stride is **40 bytes**. A non-standard stride means the engine may misparse UV/normal layout.
- Palette slots and `SKIN` names use the same mapping.
- Every simultaneous PLY is a direct `VolumeView` under the original `skin`.
- No carrier bone and no `LODView` wrapper were introduced.

### 10.3 Animation preview

You can preview real GOH animation on your rig:

1. Use **Extract GOH Animations (.anm)** in the panel to pull `.anm` files out of GOH's `properties.pak` (they live under `properties/animation/human/`). Filter by keyword: `idle`, `walk`, `fire`, `stand`. Set a limit so you do not extract all 1,599 at once.
2. `File > Import > GEM2 Engine (.anm)` to load an animation onto the active armature as an Action.
3. Scrub the timeline. This catches weighting problems far faster than exporting repeatedly.

### 10.4 Packaging

- Copy the entity folder into a **separate test mod**.
- Confirm the `.def` names the generated `.mdl`.
- Test a neutral pose plus representative animations.
- Only after it works in the test mod should you consider putting it anywhere near a real mod.

---

## 11. Troubleshooting

### 11.1 Symptom → cause → fix

| Symptom | Likely cause | Fix |
|---|---|---|
| Model is gigantic or microscopic | Source import scale | Use the source format's documented scale; re-align with uniform scale only |
| Model is inside-out / left-right swapped | Double mirroring | Re-import and mirror **once**, or not at all. Do not add a second mirror to "fix" it |
| Limbs detach when posing | Missing/incorrect vertex groups, or ungrouped vertices | Edit Mode → `Select All by Trait > Ungrouped`; re-assign regions |
| Mesh barely moves, or moves with the wrong bone | Leftover source-only vertex groups | Remove or quarantine groups not matching target bone names |
| Shoulders/arms look wrong only in animation | Wrong arm family (short vs long) | Re-import the correct matching target; never mix families |
| Elbow bends in the wrong place | `hand1`/`hand2` split wrong | Re-assign upper arm to `hand1`, forearm to `hand2` |
| Feet sink into the ground | Ground height / foot scale | Adjust **Ground Height Z** (default `-0.07`) or **Foot Size** |
| Fingers splay or evert | 40° finger curl on an unsuitable model | Turn **MMD Finger Curl** off (it is off by default) |
| Wrist separates from palm | Over-stretched hand chain | Enable **Clamp Palm Chain** |
| Hair renders with stripes or facets | Custom loop normals lost | Ensure the export path preserves imported custom loop normals (do not weld/Remove Doubles) |
| Hair cards have holes | Double-sided flag lost | Confirm `is_double_sided` survived on exported materials |
| Texture is scrambled or blank | Reused/generated image artifact | Re-export; the add-on rebuilds the entity from original image sources when it detects this |
| Pupils hidden inside the head | Eye layering | Enable **Pupil Geometry Clearance Safety**, raise **Pupil-Sclera Clearance** |
| Export fails with a 65,535 error | Too many final records | Enable **Lossless Record Splitting** (preferred) or **Enable Decimation** |
| Only part of the model appears in-game | Split PLYs not all attached, or a carrier bone was introduced | Confirm every PLY is a direct `VolumeView` on `skin` |
| Everything degenerates after "fixing" | Too many stacked transforms/mirrors | Go back to the last checkpoint |
| `Ctrl+Z` does nothing / stopped working | Mode switch, script reload or file reopen cleared the undo stack; or you hit the 32-step default | Reopen the checkpoint; raise **Undo Steps** to 128–256 (see 1.4) |
| I undid too far and lost good work | Undo went past your last save | Do **not** save. `File > Revert` reloads the last saved state; anything after that save is gone — this is why checkpoints matter |
| Slider changes nothing after export | Mesh is frozen and the option needs re-alignment | Re-point **PMX Model File** and re-run — see 5.6 |

### 11.2 Error messages

| Message | Meaning |
|---|---|
| *"Alignment failed: ..."* | Rigid fit could not proceed |
| *"Rigid alignment found only N required source bones; at least 3 are needed"* | Source skeleton lacks recognisable key bones — use the manual route |
| *"Foot-size or shoulder-width settings differ from the frozen snapshot, but the source PMX is unavailable"* | You changed alignment options after freezing — re-point the **PMX Model File** field and retry |
| *"The frozen mesh uses the bundled short-arm target while the GFA long-arm target is enabled"* | Arm family mismatch — select the source PMX so it can rebuild cleanly |
| *"The scene has no armature..."* / *"Only the source armature exists..."* | Steps out of order — do Step 1, then Step 2 |
| *"Invalid Entity name ..."* | Use 1–64 ASCII letters, digits, underscores or hyphens |
| *"Final position/weight/normal/UV indexing needs N unique vertices, exceeding the GEM2 u16 limit of 65535"* | Enable automatic splitting or decimation |
| *"DDS mode requires an external NVIDIA Texture Tools CLI"* | Provide `nvtt_export.exe` / `nvcompress.exe`, or switch to **TGA (Built-in)** |
| *"Toon Shader adaptation is enabled, but Workshop 3565678181 is incomplete or missing"* | Install/enable the Workshop item, or turn **Adapt for Toon Shader** off |
| *"The target MDL does not contain exactly one VolumeView entry"* | Destination MDL invalid for human rest conversion |
| *"The Entity output folder contains source texture ..."* | Output root collides with your source textures — choose a different output root |
| *"...collapsed to a single MESH block..."* | Material slots collapsed during export — re-export required |
| *"...duplicate triangle(s)..."* | Z-fighting / broken texture will result — fix geometry and re-export |
| *"Selected MMD preset is incompatible with the GEM2 pipeline: ..."* | Your named preset breaks one of the required invariants in 3.3. **Switch to `Pipeline defaults`.** |
| *"...Select GEM2 GOH + MOWAS2 Lossless."* | Tail of the message above. Despite the wording, you do **not** need that named preset — `Pipeline defaults` carries the identical settings and satisfies the same check. |

### 11.2.1 The "GEM2 GOH + MOWAS2 Lossless" red herring

The add-on's rejection message ends with *"Select GEM2 GOH + MOWAS2 Lossless."* That
preset is a **private authoring preset that is not distributed with the add-on**, so on a
clean install it will never appear in your dropdown.

Two consequences:

- **If you see the error:** choose **Pipeline defaults**. It is built in, always
  available, and passes the exact same invariant check.
- **If you are chasing a "missing preset" problem:** there isn't one. Do not search for,
  request, or hand-write a file called `gem2_goh_mowas2_lossless` — you gain nothing over
  `Pipeline defaults` except a name.

The only situation where a named preset is worth creating is when you maintain several
MMD import variants and want them labelled (see 3.3, Option 2).

### 11.3 Destructive actions to avoid

- Applying a modifier under an unknown parent chain.
- Applying the root scale on an imported vehicle.
- `Apply Pose as Rest Pose` on the target.
- `Armature > Recalculate Roll` on the target.
- Moving target bone heads, tails or lengths.
- Renaming target bones.
- Joining meshes blindly.
- `Remove Doubles` across UV/normal/weight seams.
- Deleting or overwriting your original source files or game files.

---

## Appendix A — Panel Property Reference

All defaults as shipped in v1.3.17.

### Top of panel

| Property | Default |
|---|---|
| **Remember Panel Settings** | on — restores last panel values and output folder in new/reopened scenes |
| **Preset** (Load / Save As / Delete) | named export presets; input, entity and output paths are excluded |

### Step 2 — target

| Property | Default |
|---|---|
| **Export Route** | `GOH` |
| **GEM2 Target Skeleton .mdl** | bundled GOH route MDL |

### Step 4 — export

| Property | Default |
|---|---|
| **Entity Name** | `skin` (1–64 ASCII letters/digits/underscores/hyphens) |
| **Output Root** | user Desktop |
| **Texture Format** | `TGA (Built-in)` |
| **NVTT Program** | empty (auto-detect) |
| **Adapt for Toon Shader** | on (forced off on MOWAS2) |

### Imported Model Multipart Export

| Property | Default |
|---|---|
| **Records per PLY** | `65535` (range `3..65535`) |

### Advanced

| Property | Default |
|---|---|
| Ground Height Z | `-0.07` |
| GFA Hand Split | **on** |
| GFA Long-Arm Segment Scaling | **on** |
| Enlarge Head | off |
| Head Scale | `1.06` |
| Neck and Accessory Follow | `0.0` |
| Use Alpha Test for Transparent Materials | on |
| Pupil Geometry Clearance Safety | **on** |
| Pupil-Sclera Clearance | `0.006` |
| Upper Torso Width (ik_updown) | off |
| Upper Torso Lateral Factor | `1.0` |
| Hip outward | `1.06` |
| Thigh outward | `1.04` |
| Calf outward | `1.03` |
| Hip / Thigh / Calf thickness | `1.0` |
| Whole-Arm Horizontal Spacing | `1.0` |
| Arm Thickness | `1.0` |
| Shoulder Width Mode | `SKELETON` |
| Shoulder Width Factor | `0.96` |
| Hand-chain attach | `blend` |
| Clavicle/Shoulder Inset | `1.0` |
| Foot Size | `1.0` |
| Reduce Torso IK Influence | off |
| ik_leftright Retention | `0.5` |
| Clamp Palm Chain | off |
| Restore Wrist Anchor and Geometry | **on** |
| MMD Finger Curl (40°) | off |

### Vertex Limit Handling

| Property | Default |
|---|---|
| Lossless Record Splitting | off |
| Records per PLY | `65535` |
| Enable Decimation | off |
| Protect Face Details | **on** |
| Protect Tight Clothing Shells | **on** |

### Human Rest Conversion

| Property | Default |
|---|---|
| Duplicate Mesh Before Conversion | off |
| Exact Reverse | **on** |

---

## Appendix B — Operator and Menu Index

### File menu — Import

| Menu entry | Purpose |
|---|---|
| **GEM2 Complete Model Folder** | Import a whole model folder with one shared rig (best for beginners) |
| **GEM2 PLY (Auto Multipart)** | Pick one PLY; every direct sibling is imported too |
| **GEM2 Engine (.vol)** | Collision volume |
| **GEM2 Engine (.anm)** | Animation |

> You can also **drag `.ply` files directly into the Blender viewport**. To intentionally import only one PLY, clear **Auto-import Multipart Siblings** in the file browser options.

### File menu — Export

| Menu entry | Purpose |
|---|---|
| **GEM2 Multipart Model (.mdl)** | Source-format-independent multipart GEM2 export |
| **FBX Model (.fbx)** | General-purpose scene export, includes bound rigs and required parent empties |
| **GEM2 Engine (.anm)** | Write a pose or Action frames as a `.anm` |

### Panel buttons (3D View → `N` → **GOH** → **GOH Auto Transfer**)

| Button | Operator |
|---|---|
| Import PMX (mmd_tools) | `gem2.mowas2_import_pmx` |
| Select .mdl and Build Skeleton | `gem2.mowas2_build_target` |
| Auto Align (preview only) | `gem2.mowas2_align_only` |
| Full Export (Bind + Decimate + PLY) | `gem2.mowas2_full_pipeline` |
| Convert Human Rest Space | `gem2.mowas2_human_rest_convert` |
| Export Converted Human | `gem2.mowas2_human_rest_export` |
| Analyze | `gem2.multipart_preflight` |
| Export (multipart) | `export_scene.gem2_multipart` |
| Import Vehicle Folder | `gem2.mowas2_import_vehicle_folder` |
| GOH to MOWAS2 / MOWAS2 to GOH | `gem2.export_vehicle_to_mowas2` / `gem2.export_vehicle_to_goh` |
| Extract GOH Animations (.anm) | `gem2.extract_goh_anm` |

Useful for scripting: press `F3` in Blender and type the operator name.

---

## Appendix C — Final Checklists

### Automatic route (A)

- [ ] `gem2_goh_tools` enabled; `mmd_tools` installed (PMX only)
- [ ] MMD preset is **Pipeline defaults** (or a self-made preset matching the 3.3 invariants)
- [ ] Correct **Export Route** (GOH / MOWAS2 / Custom)
- [ ] Target MDL matches the intended **arm family**
- [ ] Scene Status shows both a source and a target armature
- [ ] Alignment preview looks right; left/right correct; feet on ground
- [ ] Entity Name is valid ASCII (1–64)
- [ ] Output Root is a **working folder**, not the game folder
- [ ] Texture Format chosen (TGA for no dependencies)
- [ ] Toon Shader decision matches your Workshop setup
- [ ] Export completed; `Latest Result` shows the output directory
- [ ] Output re-imported and inspected
- [ ] Tested in a separate test mod

### Manual route (C)

- [ ] Working file is a copy; originals untouched
- [ ] Correct target arm family chosen
- [ ] Target armature came from a verified matching MDL/reference
- [ ] Target bone hierarchy and rest matrices **unchanged**
- [ ] `basis` and the original `skin` attachment semantics preserved
- [ ] Source aligned using rotation, translation and **uniform** scale only
- [ ] Source was **not** mirrored twice
- [ ] Mesh scale applied (`1/1/1`) **after** removing the source rig, before binding
- [ ] Bound with **With Empty Groups** (never Automatic Weights)
- [ ] Exactly **one** target Armature modifier on the export mesh
- [ ] Every used triangle vertex has at least one positive valid target weight
- [ ] `Select All by Trait > Ungrouped` returns an **empty** selection
- [ ] No vertex sits in two groups at 1.0 each (the `Assign`-without-`Remove` trap)
- [ ] `Weights > Normalize All` run; total influences per vertex is **2 or fewer**
- [ ] Target group names match target deform bones **exactly** (copied, not retyped)
- [ ] No leftover source-only vertex groups
- [ ] Head, hand and foot groups are on the correct anatomical sides
- [ ] Pose tests pass for arms, legs, head and palms **at 90°+**, both sides
- [ ] Material slots actually assigned to faces
- [ ] UV seams and intended UV map preserved
- [ ] Source images contain coherent pixels and supported alpha
- [ ] Every MTL and texture reference exists beside the entity
- [ ] Final PLY records ≤ 65,535, or losslessly split
- [ ] PLY palette slots and `SKIN` names use the same mapping
- [ ] Every simultaneous PLY is a direct `VolumeView` under the original `skin`
- [ ] No carrier bone, no `LODView` wrapper
- [ ] Generated MDL references files that actually exist
- [ ] Neutral and representative animations tested in a separate mod

---

## Final Word

A clean port is one where **the Blender scene, the target rest skeleton, the weights, the PLY records, the MDL hierarchy, the materials, the textures and the game registration all describe the same character in the same coordinate frame.**

When any one of those disagrees with the others, the game will show you something wrong — and it will usually look like a *modelling* problem when it is actually a *data* problem. Work backwards through this list and you will find it.

## References

**In the add-on folder** (these are the files you actually receive):

- `README.md` — installation, supported workflows, multipart export behaviour
- `samples/` — GOH/GFA and MOWAS2 skeleton and model templates:
  - `goh_skin.mdl` — short-arm stock GOH human
  - `goh_skin_gfa.mdl` — long-arm GOH/GFA human
  - `goh_skin_armlong.mdl` — long-arm variant
  - `MOWAS2.mdl` — MOWAS2 human
  - `uma_gan_v2.mdl` — alternative target
  - `skeleton.blend` — Blender reference scene
- `locale/` — UI translations (`en`, `ru`, `uk`, `zh`)
- `diff_ply.py` — command-line PLY comparison utility

> **Note on internal documentation.** The add-on's development tree contains a `docs/`
> folder with extra engineering notes. **That folder is not part of the distributed
> add-on**, so you will not find it after installing. Everything a user needs is in
> `README.md` and in this guide. If you ever see a reference to `docs/...`, treat it as a
> developer-only path.

**Source files:**

- `operators.py` — GEM2 import/export entries and the read-only multipart analysis operator
- `multipart_export.py` — selected-mesh export, exact-record analysis, palette writing, direct `skin` views
- `mowas2_pipeline.py` — target-frame, evaluated-freeze, texture and PMX route implementation
- `mdl_io.py` — MDL skeleton parsing and writing
- `ply_io.py` — PLY import and legacy generic export paths
- `bone_mapping_v2.py` — GOH/GEM2 target deform-bone mapping

**Official:**

- Best Way: [PLY format (.ply)](https://docs.bestway.com.ua/foundational-knowledge/gem-rts-binary-file-formats/ply-format-.ply)
- Best Way: [ANM format (.anm)](https://docs.bestway.com.ua/foundational-knowledge/gem-rts-binary-file-formats/anm-format-.anm)
- Best Way: [Vehicle model setup pipeline](https://docs.bestway.com.ua/modeling/vehicle-model-setup-pipeline)

**Credits:** 1Lt-Muhammad (export routines), Simon / VegetaBird GFA Model Weight Transfer (PMX→GEM2 and material workflow), `mmd_tools`, Best Way / 1C (GEM2 engine).

**License:** MIT
