"""Complete GEM2 PLY model discovery and multipart import."""

import glob
import json
import os
import re
import struct

import bpy

from .mdl_io import direct_volume_view_map
from .ply_io import (
    import_ply,
    _normalize_native_human_display,
    _NATIVE_HUMAN_DISPLAY_BONES,
)


_SPLIT_RE = re.compile(r"^(?P<base>.+?)_split(?P<index>\d+)$", re.IGNORECASE)


def _split_identity(filepath):
    stem = os.path.splitext(os.path.basename(filepath))[0]
    match = _SPLIT_RE.fullmatch(stem)
    if match:
        return match.group("base"), int(match.group("index"))
    return stem, -1


def _split_sort_key(filepath):
    base, index = _split_identity(filepath)
    return (base.casefold(), index >= 0, index, os.path.basename(filepath).casefold())


def _deduplicate_paths(paths):
    result = []
    seen = set()
    for path in paths:
        absolute = os.path.abspath(path)
        key = os.path.normcase(absolute)
        if key not in seen:
            seen.add(key)
            result.append(absolute)
    return result


def _safe_reference_path(directory, reference):
    relative = str(reference).replace("\\", os.sep).replace("/", os.sep)
    if os.path.isabs(relative):
        raise RuntimeError("MDL contains an absolute PLY reference: " + str(reference))
    candidate = os.path.abspath(os.path.join(directory, relative))
    if os.path.commonpath((directory, candidate)) != directory:
        raise RuntimeError("MDL PLY reference leaves the model folder: " + str(reference))
    return candidate


def _ply_has_skin(filepath):
    """Detect a real SKIN chunk, not the word in arbitrary binary payload."""
    with open(filepath, "rb") as handle:
        data = handle.read()
    cursor = 0
    while True:
        marker = data.find(b"SKIN", cursor)
        if marker < 0 or marker + 8 > len(data):
            return False
        try:
            count = struct.unpack_from("<I", data, marker + 4)[0]
        except struct.error:
            return False
        if 0 < count < 4096:
            pos = marker + 8
            valid = True
            for _index in range(count):
                if pos >= len(data):
                    valid = False
                    break
                length = data[pos]
                pos += 1
                if not (0 < length <= 255 and pos + length <= len(data)):
                    valid = False
                    break
                name = data[pos:pos + length]
                if not name:
                    valid = False
                    break
                try:
                    decoded = name.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = name.decode("latin-1")
                if not decoded or not all(char.isprintable() for char in decoded):
                    valid = False
                    break
                pos += length
            if valid:
                return True
        cursor = marker + 1


def _plan_is_human(plan):
    """Return true for a likely human skin import.

    A resolved MDL attachment named ``skin`` is the authoritative signal.  A
    filename fallback is accepted only for conventional ``skin*.ply`` plans;
    the armature-level normalizer still requires the complete human bone set
    and raw MDL metadata before it can mutate anything.  Vehicle plans are
    excluded before either heuristic is considered.
    """
    if str(plan.get("mode") or "").casefold() == "vehicle":
        return False
    paths = tuple(plan.get("ply_paths") or ())
    if not any(_ply_has_skin(path) for path in paths):
        return False
    attachment = str(plan.get("attachment_bone") or "").casefold()
    if attachment:
        return attachment == "skin"
    return all(
        _split_identity(path)[0].casefold() == "skin"
        for path in paths)


def _attachment_names(direct_map):
    return {str(name).casefold() for name in (direct_map or ())}


def _looks_like_human_skin(direct_map, has_skin):
    return bool(has_skin and "skin" in _attachment_names(direct_map))


def _should_import_as_vehicle(mdl_path, direct_map, has_skin):
    """Route rigid multi-bone / collision MDLs through vehicle folder import.

    Human skins stay on the skinned PLY path. Vehicles, weapons, and other
    models with multiple VolumeViews or ``{Volume}`` collision blocks need
    the vehicle importer so per-bone parts and volumes survive. Rigid meshes
    can contain coincidental ``SKIN`` bytes, so a SKIN hit only keeps the
    human path when the MDL actually attaches to ``skin``.
    """
    if _looks_like_human_skin(direct_map, has_skin):
        return False
    if len(direct_map) > 1:
        return True
    try:
        with open(mdl_path, "r", encoding="utf-8", errors="ignore") as handle:
            content = handle.read()
    except OSError:
        return False
    return bool(re.search(r'\{\s*Volume\s+"', content, re.IGNORECASE))


def _vehicle_plan(directory, mdl_path, direct_map):
    resolved = _resolve_map(directory, direct_map, require_all=True)
    all_paths = _deduplicate_paths(
        path for paths in resolved.values() for path in paths)
    return {
        "mode": "vehicle",
        "source": "mdl",
        "directory": directory,
        "mdl_path": mdl_path,
        "attachment_bone": None,
        "ply_paths": all_paths,
    }


def _read_direct_map(mdl_path):
    with open(mdl_path, "r", encoding="utf-8", errors="ignore") as handle:
        content = handle.read()
    return {
        bone: [reference for reference in references
               if os.path.splitext(reference)[1].casefold() == ".ply"]
        for bone, references in direct_volume_view_map(content).items()
        if any(os.path.splitext(reference)[1].casefold() == ".ply"
               for reference in references)
    }


def _resolve_map(directory, references_by_bone, require_all):
    resolved = {}
    missing = []
    for bone, references in references_by_bone.items():
        paths = []
        for reference in references:
            path = _safe_reference_path(directory, reference)
            if os.path.isfile(path):
                paths.append(path)
            else:
                missing.append(reference)
        if paths:
            resolved[bone] = _deduplicate_paths(paths)
    if require_all and missing:
        raise FileNotFoundError(
            "MDL references missing PLY parts: " + ", ".join(missing))
    return resolved


def _mdl_candidates(filepath):
    directory = os.path.dirname(os.path.abspath(filepath))
    selected_stem = os.path.splitext(os.path.basename(filepath))[0]
    base_stem, _index = _split_identity(filepath)
    ordered = [
        os.path.join(directory, base_stem + ".mdl"),
        os.path.join(directory, selected_stem + ".mdl"),
        os.path.join(directory, "skin.mdl"),
    ]
    ordered.extend(sorted(glob.glob(os.path.join(directory, "*.mdl"))))
    return [path for path in _deduplicate_paths(ordered) if os.path.isfile(path)]


def _fallback_file_plan(filepath):
    directory = os.path.dirname(os.path.abspath(filepath))
    base_stem, _selected_index = _split_identity(filepath)
    paths = []
    for candidate in glob.glob(os.path.join(directory, "*.ply")):
        candidate_base, _index = _split_identity(candidate)
        if candidate_base.casefold() == base_stem.casefold():
            paths.append(candidate)
    paths = sorted(_deduplicate_paths(paths), key=_split_sort_key)
    return {
        "mode": "multipart" if len(paths) > 1 else "single",
        "source": "filename" if len(paths) > 1 else "single",
        "directory": directory,
        "mdl_path": None,
        "attachment_bone": None,
        "ply_paths": paths or [os.path.abspath(filepath)],
    }


def discover_ply_model(filepath):
    """Plan a complete import from one selected PLY without touching Blender data."""
    filepath = os.path.abspath(filepath)
    if not os.path.isfile(filepath):
        raise FileNotFoundError("PLY file does not exist: " + filepath)
    if os.path.splitext(filepath)[1].casefold() != ".ply":
        raise ValueError("Expected a .ply file: " + filepath)

    selected_name = os.path.basename(filepath).casefold()
    directory = os.path.dirname(filepath)
    for mdl_path in _mdl_candidates(filepath):
        direct_map = _read_direct_map(mdl_path)
        matching_bones = [
            bone for bone, references in direct_map.items()
            if any(os.path.basename(reference.replace("\\", "/")).casefold()
                   == selected_name for reference in references)
        ]
        if not matching_bones:
            continue
        if len(matching_bones) != 1:
            raise RuntimeError(
                "Selected PLY is directly referenced by multiple MDL bones: "
                + ", ".join(matching_bones))

        attachment_bone = matching_bones[0]
        selected_resolved = _resolve_map(
            directory, {attachment_bone: direct_map[attachment_bone]},
            require_all=True)
        selected_parts = selected_resolved.get(attachment_bone, [])
        if not selected_parts:
            raise RuntimeError("Selected MDL bone has no importable PLY views")

        if _should_import_as_vehicle(
                mdl_path, direct_map, _ply_has_skin(filepath)):
            return _vehicle_plan(directory, mdl_path, direct_map)
        return {
            "mode": "multipart" if len(selected_parts) > 1 else "single",
            "source": "mdl",
            "directory": directory,
            "mdl_path": mdl_path,
            "attachment_bone": attachment_bone,
            "ply_paths": selected_parts,
        }
    return _fallback_file_plan(filepath)


def _fallback_folder_plan(directory):
    paths = sorted(glob.glob(os.path.join(directory, "*.ply")),
                   key=_split_sort_key)
    if not paths:
        raise FileNotFoundError("Folder contains no PLY files: " + directory)
    groups = {}
    for path in paths:
        base, _index = _split_identity(path)
        groups.setdefault(base.casefold(), []).append(path)
    if len(groups) != 1:
        raise RuntimeError(
            "Folder contains multiple PLY model groups and no unambiguous MDL: "
            + ", ".join(sorted(groups)))
    parts = _deduplicate_paths(next(iter(groups.values())))
    return {
        "mode": "multipart" if len(parts) > 1 else "single",
        "source": "filename",
        "directory": directory,
        "mdl_path": None,
        "attachment_bone": None,
        "ply_paths": parts,
    }


def discover_ply_folder(directory):
    """Plan a complete model-folder import without creating Blender data."""
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        raise NotADirectoryError("Model folder does not exist: " + directory)

    candidates = []
    for mdl_path in sorted(glob.glob(os.path.join(directory, "*.mdl"))):
        direct_map = _read_direct_map(mdl_path)
        if direct_map:
            candidates.append((mdl_path, direct_map))
    if not candidates:
        return _fallback_folder_plan(directory)
    if len(candidates) != 1:
        raise RuntimeError(
            "Folder contains multiple MDL files with PLY views: "
            + ", ".join(os.path.basename(item[0]) for item in candidates))

    mdl_path, direct_map = candidates[0]
    resolved = _resolve_map(directory, direct_map, require_all=True)
    if not resolved:
        return _fallback_folder_plan(directory)
    all_paths = _deduplicate_paths(
        path for paths in resolved.values() for path in paths)
    has_skin = any(_ply_has_skin(path) for path in all_paths)
    if (has_skin and len(resolved) > 1
            and "skin" in _attachment_names(resolved)):
        raise RuntimeError(
            "Folder mixes skinned PLY parts across multiple attachment bones; "
            "select a PLY on the desired multipart character bone instead")
    if _should_import_as_vehicle(mdl_path, resolved, has_skin):
        return {
            "mode": "vehicle",
            "source": "mdl",
            "directory": directory,
            "mdl_path": mdl_path,
            "attachment_bone": None,
            "ply_paths": all_paths,
        }

    attachment_bone, paths = next(iter(resolved.items()))
    return {
        "mode": "multipart" if len(paths) > 1 else "single",
        "source": "mdl",
        "directory": directory,
        "mdl_path": mdl_path,
        "attachment_bone": attachment_bone,
        "ply_paths": paths,
    }


def _snapshot_datablocks():
    return {
        name: {block.as_pointer() for block in getattr(bpy.data, name)}
        for name in ("objects", "meshes", "armatures", "materials", "images")
    }


def _remove_new_datablocks(snapshot):
    for name in ("objects", "meshes", "armatures", "materials", "images"):
        collection = getattr(bpy.data, name)
        for block in list(collection):
            if block.as_pointer() in snapshot[name]:
                continue
            try:
                if name == "objects":
                    collection.remove(block, do_unlink=True)
                elif block.users == 0:
                    collection.remove(block)
            except (ReferenceError, RuntimeError):
                pass


def _restore_selection(selected, active):
    for obj in bpy.context.view_layer.objects:
        try:
            obj.select_set(False)
        except (AttributeError, ReferenceError):
            pass
    for obj in selected:
        if obj and obj.name in bpy.context.view_layer.objects:
            obj.select_set(True)
    if active and active.name in bpy.context.view_layer.objects:
        bpy.context.view_layer.objects.active = active


def _select_imported(objects, armature):
    for obj in bpy.context.view_layer.objects:
        try:
            obj.select_set(False)
        except ReferenceError:
            pass
    for obj in objects:
        obj.select_set(True)
    if armature is not None:
        armature.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]


def _import_ply_batch(plan, normalize_human_display=True):
    paths = list(plan["ply_paths"])
    normalize_human = bool(
        normalize_human_display and _plan_is_human(plan))
    if not paths:
        raise RuntimeError("Multipart import plan contains no PLY files")

    before = _snapshot_datablocks()
    original_selected = list(bpy.context.selected_objects)
    original_active = bpy.context.view_layer.objects.active
    imported = []
    try:
        primary, armature, group = import_ply(
            paths[0], mdl_path_override=plan.get("mdl_path"),
            mesh_parent_override=plan.get("attachment_bone"),
            normalize_human_display=False)
        imported.append(primary)

        # Normalize exactly once, before any later multipart mesh is attached to
        # the armature.  ``has_skin`` is a plan-level fact here: a later part
        # may be the first file carrying the SKIN chunk.
        normalization_report = None
        if normalize_human:
            raw_metadata = bool(
                armature and armature.get("gem2_world_mats")
                and armature.get("gem2_parents"))
            human_bones = bool(
                armature and _NATIVE_HUMAN_DISPLAY_BONES.issubset(
                    {bone.name for bone in armature.data.bones}))
            normalization_report = _normalize_native_human_display(
                armature,
                plan.get("attachment_bone")
                or (armature.get("gem2_mesh_parent") if armature else ""),
                True,
                False,
                strict=raw_metadata and human_bones)
            if (normalization_report is None and raw_metadata
                    and human_bones):
                raise RuntimeError(
                    "Human display-rest normalization did not produce a "
                    "poseable armature: " + str(armature.name))
            if (normalization_report is None and raw_metadata
                    and not human_bones):
                print('[human] candidate plan has no complete human bone set; '
                      'keeping raw display:', armature.name)
        for path in paths[1:]:
            obj, _armature, _group = import_ply(
                path,
                skip_armature=armature is None,
                create_helpers=False,
                existing_armature=armature,
                existing_group=group,
                reference_object=primary,
                mdl_path_override=plan.get("mdl_path"),
                mesh_parent_override=plan.get("attachment_bone"),
                normalize_human_display=False,
            )
            imported.append(obj)

        part_names = [os.path.basename(path) for path in paths]
        group["gem2_multipart_model"] = len(paths) > 1
        group["gem2_multipart_parts"] = json.dumps(part_names)
        group["gem2_multipart_mdl"] = str(plan.get("mdl_path") or "")
        group["gem2_multipart_attachment_bone"] = str(
            plan.get("attachment_bone") or "")
        for index, obj in enumerate(imported):
            obj["gem2_multipart_index"] = index
            obj["gem2_multipart_count"] = len(imported)
            obj["gem2_multipart_group"] = group.name
        _select_imported(imported, armature)
        return {
            **plan,
            "objects": imported,
            "armature": armature,
            "group": group,
            "part_count": len(imported),
            "human_display_normalized": normalization_report is not None,
            "human_display_normalization_report": normalization_report,
        }
    except Exception:
        try:
            if (bpy.context.view_layer.objects.active is not None
                    and bpy.context.view_layer.objects.active.mode != "OBJECT"):
                bpy.ops.object.mode_set(mode="OBJECT")
        except (AttributeError, RuntimeError):
            pass
        _remove_new_datablocks(before)
        _restore_selection(original_selected, original_active)
        raise


def _import_vehicle(plan):
    from .vehicle_io import import_vehicle_folder
    root = import_vehicle_folder(plan["directory"])
    return {
        **plan,
        "objects": [root],
        "armature": bpy.data.objects.get(
            str(root.get("gem2_vehicle_armature") or "")),
        "group": root,
        "part_count": len(plan["ply_paths"]),
    }


def import_model_from_ply(filepath, auto_multipart=True,
                           normalize_human_display=True):
    """Import one PLY or its complete MDL/split sibling model.

    Complete human models are normalized by default; pass ``False`` only for
    low-level raw-space inspection.
    """
    if auto_multipart:
        plan = discover_ply_model(filepath)
    else:
        absolute = os.path.abspath(filepath)
        plan = {
            "mode": "single",
            "source": "single",
            "directory": os.path.dirname(absolute),
            "mdl_path": None,
            "attachment_bone": None,
            "ply_paths": [absolute],
        }
        # A user may intentionally disable sibling/multipart discovery while
        # importing a conventional human ``skin.ply``.  Preserve the same
        # display-rest repair when the file itself proves it is skinned; the
        # low-level importer still remains raw unless this high-level option is
        # enabled.
        if (_split_identity(absolute)[0].casefold() == "skin"
                and _ply_has_skin(absolute)):
            plan["attachment_bone"] = "skin"
    if plan["mode"] == "vehicle":
        return _import_vehicle(plan)
    return _import_ply_batch(
        plan, normalize_human_display=normalize_human_display)


def import_model_folder(directory, normalize_human_display=True):
    """Import a complete GEM2 model folder using MDL ownership when available.

    Complete human models are normalized by default; vehicle and rigid plans
    never enter the human display-rest path.
    """
    plan = discover_ply_folder(directory)
    if plan["mode"] == "vehicle":
        return _import_vehicle(plan)
    return _import_ply_batch(
        plan, normalize_human_display=normalize_human_display)
