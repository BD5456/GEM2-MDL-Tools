"""Shared Blender image discovery and collision-safe export helpers.

The GEM2 material files reference extensionless texture stems.  This module
keeps that reference separate from Blender's image datablock name and provides
one staging path for loose, packed, and generated images.
"""

import hashlib
import os
import re
import shutil
import tempfile

import bpy


SOURCE_EXTENSIONS = (
    ".png", ".tga", ".bmp", ".jpg", ".jpeg", ".dds",
    ".tif", ".tiff", ".webp", ".gif",
)

_FORMAT_EXTENSIONS = {
    "BMP": ".bmp",
    "DDS": ".dds",
    "HDR": ".hdr",
    "JPEG": ".jpg",
    "JPEG2000": ".jp2",
    "PNG": ".png",
    "TARGA": ".tga",
    "TARGA_RAW": ".tga",
    "TIFF": ".tif",
    "WEBP": ".webp",
}

_ROLE_INPUTS = {
    # Exact normalized socket names. In particular, do not let Emission Color
    # or Coat Normal masquerade as the material's diffuse/normal input.
    "diffuse": {
        "base color", "base tex", "base texture", "diffuse", "albedo",
        "color", "texture",
    },
    "bump": {"normal", "normal map", "bump", "height"},
    "specular": {
        "specular", "specular ior level", "roughness", "gloss",
    },
}

_ROLE_POSITIVE = {
    "diffuse": (
        "mmd_base_tex", "mmd base tex", "base_tex", "base tex",
        "base_color", "base color", "diffuse", "albedo", "main tex",
        "main texture", "color tex", "color texture",
    ),
    "bump": ("normal", "bump", "nrm", "height"),
    "specular": ("specular", "spec", "gloss", "rough"),
}

_ROLE_NEGATIVE = {
    "diffuse": (
        "toon", "sphere", "sph", "spa", "bump", "specular",
        "roughness", "envmap", "environment",
    ),
    "bump": ("toon", "sphere", "diffuse", "albedo", "base tex",
              "base_tex", "mmd_base_tex"),
    "specular": ("toon", "sphere", "diffuse", "albedo", "base tex",
                  "base_tex", "mmd_base_tex"),
}


class TextureStageError(RuntimeError):
    """Raised when a selected Blender image has no exportable payload."""


def _pointer(value):
    try:
        return value.as_pointer()
    except (AttributeError, RuntimeError, ReferenceError):
        return id(value)


def resolved_image_path(image):
    """Resolve a Blender image path, including ``//`` and linked libraries."""
    if image is None:
        return ""
    raw = getattr(image, "filepath_raw", "") or getattr(image, "filepath", "")
    if not raw:
        return ""
    try:
        resolved = bpy.path.abspath(raw, library=getattr(image, "library", None))
    except TypeError:
        resolved = bpy.path.abspath(raw)
    except (AttributeError, ValueError, RuntimeError):
        return ""
    return os.path.abspath(resolved) if resolved else ""


def _normalized_basename(value):
    value = str(value or "").replace("\\", "/")
    return os.path.basename(value)


def image_export_filename(image, source_path=""):
    """Return a safe, stable encoded-source filename for an image datablock."""
    raw = getattr(image, "filepath_raw", "") or getattr(image, "filepath", "")
    filename = (_normalized_basename(source_path)
                or _normalized_basename(raw)
                or _normalized_basename(getattr(image, "name", "")))

    # Blender appends .001 to duplicate datablock names after the real suffix.
    duplicate = re.match(
        r"^(.*\.(?:dds|tga|png|bmp|jpe?g|tiff?|webp|gif))\.\d{3}$",
        filename, re.IGNORECASE)
    if duplicate:
        filename = duplicate.group(1)

    stem, extension = os.path.splitext(filename)
    if extension.casefold() not in SOURCE_EXTENSIONS:
        file_format = str(getattr(image, "file_format", "") or "").upper()
        extension = _FORMAT_EXTENSIONS.get(file_format, ".png")

    # Keep the punctuation accepted by GEM2 texture names (notably @), while
    # preventing path traversal, control characters, and Unicode-only names.
    stem = re.sub(r"[^A-Za-z0-9_.@+\-]", "_", stem)
    stem = stem.strip(" .") or "texture"
    if (stem.upper() in {"CON", "PRN", "AUX", "NUL"}
            or re.fullmatch(r"(?:COM|LPT)[1-9]", stem.upper())):
        stem += "_tex"
    return stem + extension.lower()


def image_reference_stem(image):
    """Return the extensionless GEM2 token for a Blender image."""
    return os.path.splitext(image_export_filename(image))[0]


def _node_text(node, image=None):
    parts = [getattr(node, "name", ""), getattr(node, "label", "")]
    if image is not None:
        parts.append(getattr(image, "name", ""))
    return " ".join(str(part or "") for part in parts).casefold()


def _normalize_socket_name(name):
    value = str(name or "").replace("_", " ").replace("-", " ")
    return " ".join(value.casefold().split())


def _socket_matches(name, role):
    value = _normalize_socket_name(name)
    return value in _ROLE_INPUTS.get(role, set())


def _walk_upstream(socket, depth=0, visited=None, role="diffuse"):
    """Yield image nodes feeding a socket through node chains and groups."""
    if socket is None or not getattr(socket, "is_linked", False):
        return
    if depth > 32:
        return
    visited = set(visited or ())
    for link in getattr(socket, "links", ()):
        node = getattr(link, "from_node", None)
        if node is None:
            continue
        key = _pointer(node)
        if key in visited:
            continue
        next_visited = visited | {key}
        node_type = getattr(node, "type", "")
        if node_type == "TEX_IMAGE":
            image = getattr(node, "image", None)
            if image is not None:
                yield image, depth, getattr(link.to_socket, "name", ""), node
            continue

        if node_type == "GROUP" and getattr(node, "node_tree", None):
            # First follow the group's output socket into its internal graph.
            output_name = getattr(link.from_socket, "name", "")
            output_socket = getattr(node.node_tree.nodes.get("Group Output"),
                                    "inputs", {}).get(output_name)
            if output_socket is None:
                for output_node in getattr(node.node_tree, "nodes", ()):
                    if getattr(output_node, "type", "") != "GROUP_OUTPUT":
                        continue
                    output_socket = output_node.inputs.get(output_name)
                    if output_socket is not None:
                        break
            if output_socket is not None:
                yield from _walk_upstream(
                    output_socket, depth + 1, next_visited, role)

            # MMD shader groups also expose the source texture as an external
            # input (usually Base Tex). Follow only role-matching inputs so a
            # linked Toon Tex/Sphere Tex cannot become the diffuse candidate.
            for input_socket in getattr(node, "inputs", ()):
                if (_socket_matches(getattr(input_socket, "name", ""), role)
                        and getattr(input_socket, "is_linked", False)):
                    yield from _walk_upstream(
                        input_socket, depth + 1, next_visited, role)
            continue

        for input_socket in getattr(node, "inputs", ()):
            if getattr(input_socket, "is_linked", False):
                yield from _walk_upstream(
                    input_socket, depth + 1, next_visited, role)


def _iter_image_nodes(node_tree, seen_trees=None):
    """Yield ``(node, image, owner_tree)`` through nested node groups."""
    if node_tree is None:
        return
    seen_trees = set(seen_trees or ())
    tree_key = _pointer(node_tree)
    if tree_key in seen_trees:
        return
    seen_trees.add(tree_key)
    for node in getattr(node_tree, "nodes", ()):
        if getattr(node, "type", "") == "TEX_IMAGE":
            image = getattr(node, "image", None)
            if image is not None:
                yield node, image, node_tree
        nested = getattr(node, "node_tree", None)
        if nested is not None:
            yield from _iter_image_nodes(nested, seen_trees)


def _iter_node_trees(node_tree, seen_trees=None):
    """Yield a material tree and every nested group tree exactly once."""
    if node_tree is None:
        return
    seen_trees = set(seen_trees or ())
    tree_key = _pointer(node_tree)
    if tree_key in seen_trees:
        return
    seen_trees.add(tree_key)
    yield node_tree
    for node in getattr(node_tree, "nodes", ()):
        nested = getattr(node, "node_tree", None)
        if nested is not None:
            yield from _iter_node_trees(nested, seen_trees)


def _semantic_score(node, image, role, material_name=""):
    text = _node_text(node, image)
    material_text = str(material_name or "").casefold()
    score = 0
    for token in _ROLE_POSITIVE.get(role, ()):
        if token in text:
            score += 120 if token.startswith("mmd_") else 70
    for token in _ROLE_NEGATIVE.get(role, ()):
        if token in text:
            score -= 180
    # Material names are only a weak tie breaker; a node's own role wins.
    if role == "diffuse":
        if any(token in material_text for token in ("base", "body", "cloth", "face")):
            score += 8
    return score


def material_image_candidates(material, role="diffuse"):
    """Return scored image candidates for a material role.

    The result is a list of dictionaries sorted from most to least likely.
    Traversal follows Mix/RGB/NormalMap-style inputs and also inspects nested
    groups. MMD's top-level ``mmd_base_tex``/``mmd_toon_tex`` convention is
    handled by semantic scoring when the shader group hides the final socket.
    """
    if (not material or not getattr(material, "use_nodes", False)
            or not getattr(material, "node_tree", None)):
        return []
    role = role if role in _ROLE_INPUTS else "diffuse"
    tree = material.node_tree
    candidates = {}
    order = 0

    def add(image, score, node, depth=99, path_socket="",
            role_evidence=False, nonrole_evidence=False):
        if image is None:
            return
        key = _pointer(image)
        row = candidates.get(key)
        if row is None:
            row = {
                "image": image,
                "score": 0,
                "depth": depth,
                "order": order,
                "node": node,
                "role_evidence": False,
                "nonrole_evidence": False,
            }
            candidates[key] = row
        row["score"] += int(score)
        row["depth"] = min(row["depth"], depth)
        row["role_evidence"] |= bool(role_evidence)
        row["nonrole_evidence"] |= bool(nonrole_evidence)
        if path_socket:
            path = _normalize_socket_name(path_socket)
            if _socket_matches(path, role):
                row["score"] += 80
                row["role_evidence"] = True
            if path in {"toon tex", "sphere tex", "normal", "normal map",
                        "emission color", "coat normal", "specular",
                        "roughness"}:
                row["score"] -= 120
                row["nonrole_evidence"] = True

    def scan_group_inputs(owner_tree, seen_trees=None):
        seen_trees = set(seen_trees or ())
        tree_key = _pointer(owner_tree)
        if tree_key in seen_trees:
            return
        seen_trees.add(tree_key)
        for group_node in getattr(owner_tree, "nodes", ()):
            if getattr(group_node, "type", "") != "GROUP":
                continue
            # The external link into a named group input is the most reliable
            # representation of MMD's mmd_base_tex/mmd_toon_tex shader setup.
            for input_socket in getattr(group_node, "inputs", ()):
                if not (_socket_matches(getattr(input_socket, "name", ""), role)
                        and getattr(input_socket, "is_linked", False)):
                    continue
                for image, depth, path_socket, source_node in _walk_upstream(
                        input_socket, role=role):
                    add(image, 700 - depth * 35, source_node,
                        depth, path_socket, role_evidence=True)
            nested = getattr(group_node, "node_tree", None)
            if nested is not None:
                scan_group_inputs(nested, seen_trees)

    scan_group_inputs(tree)

    principled_nodes = []
    for owner_tree in _iter_node_trees(tree):
        principled_nodes.extend(
            (node, owner_tree) for node in getattr(owner_tree, "nodes", ())
            if getattr(node, "type", "") == "BSDF_PRINCIPLED")
    for principled, _owner_tree in principled_nodes:
        for socket in getattr(principled, "inputs", ()):
            if not _socket_matches(getattr(socket, "name", ""), role):
                continue
            for image, depth, path_socket, source_node in _walk_upstream(
                    socket, role=role):
                direct_bonus = 240 if depth == 0 else 0
                add(image, 560 - depth * 35 + direct_bonus,
                    source_node, depth, path_socket, role_evidence=True)

    all_nodes = list(_iter_image_nodes(tree))
    for node, image, owner_tree in all_nodes:
        semantic = _semantic_score(
            node, image, role, getattr(material, "name", ""))
        outgoing_role = 0
        outgoing_penalty = 0
        role_link = False
        nonrole_link = False
        for link in getattr(owner_tree, "links", ()):
            if _pointer(getattr(link, "from_node", None)) != _pointer(node):
                continue
            socket_name = getattr(link.to_socket, "name", "")
            normalized_socket = _normalize_socket_name(socket_name)
            if _socket_matches(socket_name, role):
                outgoing_role = max(outgoing_role, 220)
                role_link = True
            elif normalized_socket in {
                    "toon tex", "sphere tex", "normal", "normal map",
                    "emission color", "coat normal", "specular",
                    "roughness"}:
                outgoing_penalty = max(outgoing_penalty, 100)
                nonrole_link = True
            if (getattr(link.to_node, "type", "") == "GROUP"
                    and _socket_matches(socket_name, role)):
                outgoing_role = max(outgoing_role, 180)
        add(image, semantic + outgoing_role - outgoing_penalty,
            node, 99, "", role_evidence=role_link,
            nonrole_evidence=nonrole_link)
        order += 1

    result = list(candidates.values())
    result.sort(key=lambda row: (row["score"], -row["depth"], -row["order"]),
                reverse=True)
    return result


def resolve_material_image(material, role="diffuse"):
    """Return the best image for a material role, or ``None``."""
    candidates = material_image_candidates(material, role)
    if not candidates:
        return None
    best = candidates[0]
    # A lone toon/sphere node is not a diffuse texture. An unlabelled image
    # remains valid as a fallback (score 0), preserving simple legacy graphs.
    if best["score"] < 0:
        return None
    # An image linked only to an unrelated socket (Emission, Coat, Toon, ...)
    # is not a valid role fallback. Explicit MMD base naming is strong enough
    # to survive unusual custom shaders, while generic unrelated links fail
    # closed instead of exporting the wrong bitmap.
    if (best.get("nonrole_evidence") and not best.get("role_evidence")
            and "mmd_base_tex" not in _node_text(
                best["node"], best["image"])):
        return None
    # Non-diffuse roles must be linked or semantically named; otherwise the
    # ordinary diffuse fallback would be emitted as a bump/specular texture.
    if role != "diffuse" and best["score"] == 0:
        return None
    return best["image"]


def iter_material_images(material):
    """Return unique image datablocks used anywhere in a material tree."""
    if (not material or not getattr(material, "use_nodes", False)
            or not getattr(material, "node_tree", None)):
        return []
    result = []
    seen = set()
    for _node, image, _owner_tree in _iter_image_nodes(material.node_tree):
        key = _pointer(image)
        if key in seen:
            continue
        seen.add(key)
        result.append(image)
    return result


def _sha256_file(filepath):
    digest = hashlib.sha256()
    with open(filepath, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _image_size(image):
    try:
        width, height = image.size
        return int(width), int(height)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return 0, 0


def image_alpha_profile(image, sample_limit=4096):
    """Sample a Blender image's alpha channel before staging/conversion.

    The generic and multipart exporters need the same semantic alpha decision as
    the PMX pipeline, but their source image may be packed and pathless. Reading
    the datablock keeps that decision independent of the external filepath.
    Returns ``None`` only when Blender cannot expose a usable pixel buffer.
    """
    import numpy as np

    width, height = _image_size(image)
    if width <= 0 or height <= 0:
        return None
    pixel_count = width * height
    try:
        raw = np.asarray(image.pixels, dtype=np.float32)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None
    if raw.size == pixel_count * 3 or raw.size == pixel_count:
        return {
            "known": True,
            "has_alpha": False,
            "partial_ratio": 0.0,
            "transparent_ratio": 0.0,
            "opaque_ratio": 1.0,
            "min_alpha": 1.0,
            "max_alpha": 1.0,
        }
    if raw.size != pixel_count * 4:
        return None
    alpha = raw[3::4]
    if alpha.size != pixel_count:
        return None
    sample_limit = max(1, int(sample_limit))
    if alpha.size > sample_limit:
        step = max(1, (alpha.size + sample_limit - 1) // sample_limit)
        sample = alpha[::step]
        sample = np.concatenate((sample, alpha[:1], alpha[-1:]))
    else:
        sample = alpha
    return {
        "known": True,
        "has_alpha": bool((sample < (250.0 / 255.0)).any()),
        "partial_ratio": float(((sample > (2.0 / 255.0))
                                 & (sample < (253.0 / 255.0))).mean()),
        "transparent_ratio": float((sample <= (2.0 / 255.0)).mean()),
        "opaque_ratio": float((sample >= (253.0 / 255.0)).mean()),
        "min_alpha": float(sample.min()),
        "max_alpha": float(sample.max()),
    }


def _save_image_as_png(image, filepath):
    width, height = _image_size(image)
    if width <= 0 or height <= 0:
        raise TextureStageError(
            "Image %r has no pixel buffer" % getattr(image, "name", "<unnamed>"))
    old_format = getattr(image, "file_format", None)
    old_raw = getattr(image, "filepath_raw", None)
    try:
        if hasattr(image, "file_format"):
            image.file_format = "PNG"
        try:
            image.save(filepath=filepath)
        except TypeError:
            image.save(filepath)
    finally:
        if old_format is not None:
            try:
                image.file_format = old_format
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
        if old_raw is not None:
            try:
                image.filepath_raw = old_raw
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
    if not os.path.isfile(filepath) or os.path.getsize(filepath) <= 0:
        raise TextureStageError(
            "Blender could not serialize image %r" % getattr(image, "name", "<unnamed>"))


def fill_black_alpha(pixels, thresh=0.5, max_iter=48):
    """Fill transparent black RGB from neighbouring solid pixels in-place."""
    import numpy as np

    alpha = pixels[:, :, 3]
    mask = alpha < thresh
    if not mask.any():
        return
    rgb = pixels[:, :, :3].copy()
    solid = ~mask
    if not solid.any():
        pixels[:, :, :3] = 0.5
        return
    pending = mask.copy()
    iterations = 0
    while pending.any() and iterations < max_iter:
        accumulated = np.zeros_like(rgb)
        count = np.zeros(rgb.shape[:2], dtype=np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (-1, 1), (1, -1), (1, 1)):
            neighbour_rgb = np.roll(np.roll(rgb, dy, axis=0), dx, axis=1)
            neighbour_solid = np.roll(
                np.roll(solid, dy, axis=0), dx, axis=1)
            accumulated += neighbour_rgb * neighbour_solid[:, :, None]
            count += neighbour_solid
        writable = pending & (count > 0)
        if not writable.any():
            break
        rgb[writable] = accumulated[writable] / count[writable][:, None]
        solid[writable] = True
        pending[writable] = False
        iterations += 1
    if pending.any():
        fallback = (rgb[solid].mean(axis=0)
                    if solid.any() else np.array([0.5, 0.5, 0.5]))
        rgb[pending] = fallback
    pixels[:, :, :3] = rgb


def image_file_to_tga(source_path, target_path, soften=False,
                      fill_black=False, return_profile=False,
                      alpha_scale=1.0, alpha_lift=0.0):
    """Decode an image with Blender and write GEM2 type-2 BGRA32 TGA.

    Blender may expose RGB, RGBA, grayscale, or malformed channel counts
    depending on the source file. Normalize every decoded buffer to RGBA before
    analysis and output so JPEG/BMP/TGA sources follow the same contract.
    """
    import numpy as np

    image = bpy.data.images.load(source_path, check_existing=False)
    try:
        width, height = _image_size(image)
        if width <= 0 or height <= 0:
            raise TextureStageError(
                "Texture %r has invalid size %dx%d" %
                (os.path.basename(source_path), width, height))
        raw = np.array(image.pixels, dtype=np.float32)
        pixel_count = width * height
        channel_count = int(getattr(image, "channels", 0) or 0)
        if pixel_count <= 0 or raw.size == 0:
            raise TextureStageError(
                "Texture %r has no decoded pixels" % os.path.basename(source_path))
        if raw.size == pixel_count * 4:
            pixels = raw.reshape(height, width, 4).copy()
            had_alpha = channel_count >= 4
        elif raw.size == pixel_count * 3:
            rgb = raw.reshape(height, width, 3)
            pixels = np.ones((height, width, 4), dtype=np.float32)
            pixels[:, :, :3] = rgb
            had_alpha = False
        elif raw.size == pixel_count:
            gray = raw.reshape(height, width)
            pixels = np.ones((height, width, 4), dtype=np.float32)
            pixels[:, :, :3] = gray[:, :, None]
            had_alpha = False
        else:
            raise TextureStageError(
                "Texture %r has unsupported %d-value pixel buffer" %
                (os.path.basename(source_path), raw.size))

        if soften:
            pixels[:, :, 3] *= float(alpha_scale)
            if alpha_lift:
                lift = ((1.0 - pixels[:, :, 3])
                        * (float(alpha_lift) / 255.0))
                pixels[:, :, :3] = np.clip(
                    pixels[:, :, :3] + lift[:, :, None], 0.0, 1.0)
        if fill_black:
            fill_black_alpha(pixels)

        alpha = pixels[:, :, 3]
        if had_alpha:
            has_alpha = bool((alpha < (250.0 / 255.0)).any())
            partial = (alpha > (2.0 / 255.0)) & (alpha < (253.0 / 255.0))
            profile = {
                "has_alpha": has_alpha,
                "partial_ratio": float(partial.mean()),
                "transparent_ratio": float((alpha <= (2.0 / 255.0)).mean()),
                "opaque_ratio": float((alpha >= (253.0 / 255.0)).mean()),
                "min_alpha": float(alpha.min()),
                "max_alpha": float(alpha.max()),
            }
        else:
            profile = {
                "has_alpha": False,
                "partial_ratio": 0.0,
                "transparent_ratio": 0.0,
                "opaque_ratio": 1.0,
                "min_alpha": 1.0,
                "max_alpha": 1.0,
            }

        bgra = np.clip(pixels[:, :, [2, 1, 0, 3]], 0.0, 1.0)
        payload = (bgra * 255.0 + 0.5).astype(np.uint8).tobytes()
        header = bytes((
            0, 0, 2,
            0, 0, 0, 0, 0,
            0, 0, 0, 0,
            width & 0xFF, (width >> 8) & 0xFF,
            height & 0xFF, (height >> 8) & 0xFF,
            32, 0x08,
        ))
        with open(target_path, "wb") as handle:
            handle.write(header)
            handle.write(payload)
        return profile if return_profile else bool(profile["has_alpha"])
    finally:
        try:
            bpy.data.images.remove(image)
        except (AttributeError, RuntimeError, ValueError):
            pass


def _write_atomic(destination, source_path=None, payload=None):
    parent = os.path.dirname(destination)
    fd, temporary = tempfile.mkstemp(
        prefix=".__gem2_texture_", suffix=".tmp", dir=parent)
    os.close(fd)
    try:
        if source_path is not None:
            shutil.copy2(source_path, temporary)
        else:
            with open(temporary, "wb") as handle:
                handle.write(payload or b"")
        os.replace(temporary, destination)
    finally:
        if os.path.isfile(temporary):
            os.remove(temporary)


class TextureStager:
    """Stage images with collision-safe extensionless GEM2 texture stems."""

    def __init__(self, output_dir):
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        self._by_image = {}
        self._by_source = {}
        self._by_digest = {}
        # GEM2 MTL references omit file extensions, so stem uniqueness is the
        # real invariant. A PNG and TGA named ``foo`` must never coexist.
        self._by_stem = {}

    def _disk_paths_for_stem(self, stem):
        key = stem.casefold()
        result = []
        try:
            filenames = os.listdir(self.output_dir)
        except OSError:
            return result
        allowed = set(SOURCE_EXTENSIONS) | {".tga"}
        for filename in filenames:
            base, extension = os.path.splitext(filename)
            if (base.casefold() == key
                    and extension.casefold() in allowed):
                candidate = os.path.join(self.output_dir, filename)
                if os.path.isfile(candidate):
                    result.append(candidate)
        return result

    def _allocate(self, filename, digest):
        preferred_stem, extension = os.path.splitext(filename)
        ordinal = 1
        while True:
            stem = (preferred_stem if ordinal == 1 else
                    "%s_%d" % (preferred_stem, ordinal))
            key = stem.casefold()
            planned = self._by_stem.get(key)
            if planned is not None:
                if planned["digest"] == digest:
                    return planned, None
                ordinal += 1
                continue

            disk_paths = self._disk_paths_for_stem(stem)
            candidate_name = stem + extension
            candidate_path = os.path.join(self.output_dir, candidate_name)
            # Reuse an unconverted source left by an interrupted export only
            # when it is the exact payload and sole texture for this stem.
            if len(disk_paths) == 1 and os.path.normcase(
                    disk_paths[0]) == os.path.normcase(candidate_path):
                try:
                    if _sha256_file(candidate_path) == digest:
                        ref = {
                            "filename": candidate_name,
                            "stem": stem,
                            "path": candidate_path,
                            "digest": digest,
                        }
                        self._by_stem[key] = ref
                        self._by_digest[digest] = ref
                        return ref, None
                except OSError:
                    pass
            if not disk_paths and not os.path.exists(candidate_path):
                return None, candidate_name
            ordinal += 1

    def stage(self, image, source_label="", strict=True):
        """Stage one image and return ``{filename, stem, path, digest}``."""
        if image is None:
            return None
        image_key = _pointer(image)
        existing = self._by_image.get(image_key)
        if existing is not None:
            return existing

        source_path = resolved_image_path(image)
        packed = getattr(image, "packed_file", None)
        packed_data = None
        if packed is not None:
            try:
                packed_data = bytes(packed.data)
            except (AttributeError, TypeError, ValueError, RuntimeError):
                packed_data = None

        temporary_source = None
        try:
            # Packed pixels are authoritative even when Blender retains an
            # existing external filepath. The file on disk may be stale or a
            # different revision of the same image.
            if packed_data:
                filename = image_export_filename(image, source_path)
                digest = hashlib.sha256(packed_data).hexdigest()
                writer = ("bytes", packed_data)
                source_key = None
            elif source_path and os.path.isfile(source_path):
                filename = image_export_filename(image, source_path)
                digest = _sha256_file(source_path)
                writer = ("file", source_path)
                source_key = os.path.normcase(os.path.abspath(source_path))
                old = self._by_source.get(source_key)
                if old is not None:
                    self._by_image[image_key] = old
                    return old
            else:
                filename = image_export_filename(image)
                fd, temporary_source = tempfile.mkstemp(
                    prefix=".__gem2_image_", suffix=".png",
                    dir=self.output_dir)
                os.close(fd)
                _save_image_as_png(image, temporary_source)
                filename = os.path.splitext(filename)[0] + ".png"
                digest = _sha256_file(temporary_source)
                writer = ("file", temporary_source)
                source_key = None
        except (OSError, RuntimeError, ValueError, TypeError,
                AttributeError, ReferenceError, TextureStageError) as exc:
            if temporary_source and os.path.isfile(temporary_source):
                os.remove(temporary_source)
            if not strict:
                print("[GEM2] texture skipped: %s (%s)" % (
                    source_label or getattr(image, "name", "<unnamed>"), exc))
                return None
            raise TextureStageError(
                "Cannot stage texture %r: %s" % (
                    source_label or getattr(image, "name", "<unnamed>"), exc)) from exc

        try:
            by_digest = self._by_digest.get(digest)
            if by_digest is not None:
                ref = by_digest
            else:
                ref, allocated_name = self._allocate(filename, digest)
                if ref is None:
                    destination = os.path.join(self.output_dir, allocated_name)
                    if writer[0] == "file":
                        _write_atomic(destination, source_path=writer[1])
                    else:
                        _write_atomic(destination, payload=writer[1])
                    ref = {
                        "filename": allocated_name,
                        "stem": os.path.splitext(allocated_name)[0],
                        "path": destination,
                        "digest": digest,
                    }
                    self._by_stem[ref["stem"].casefold()] = ref
                    self._by_digest[digest] = ref
            self._by_image[image_key] = ref
            if source_path and os.path.isfile(source_path):
                self._by_source[os.path.normcase(
                    os.path.abspath(source_path))] = ref
            return ref
        finally:
            if temporary_source and os.path.isfile(temporary_source):
                os.remove(temporary_source)

    def stage_material(self, material, source_label=""):
        """Stage diffuse/bump/specular role images for one material."""
        refs = {}
        for role in ("diffuse", "bump", "specular"):
            image = resolve_material_image(material, role)
            if image is None:
                continue
            label = "%s:%s" % (
                source_label or getattr(material, "name", "material"), role)
            refs[role] = self.stage(image, source_label=label)
        return refs


def convert_staged_to_tga(refs, fill_black_default=False):
    """Normalize staged images to GEM2's 32-bit type-2 TGA representation.

    ``TextureStager`` has already reserved unique stems. Conversion preserves
    those stems, so every MTL reference remains tied to exactly one TGA file.
    A ref can set ``fill_black`` to True only for a consumer that is known to
    be opaque. The conservative default preserves alpha/RGB for blend and
    alpha-test rendering when no semantic classifier was run.
    """
    unique = {}
    for ref in (refs or ()):
        if ref is None:
            continue
        unique[os.path.normcase(os.path.abspath(ref["path"]))] = ref
    for ref in unique.values():
        source = ref["path"]
        stem = ref["stem"]
        fill_black = bool(ref.get("fill_black", fill_black_default))
        if os.path.splitext(source)[1].casefold() == ".tga":
            temporary = source + ".__gem2_normalized.tga"
            try:
                image_file_to_tga(
                    source, temporary, soften=False, fill_black=fill_black)
                os.replace(temporary, source)
            finally:
                if os.path.isfile(temporary):
                    os.remove(temporary)
            ref["filename"] = stem + ".tga"
            ref["path"] = source
            continue
        target = os.path.join(os.path.dirname(source), stem + ".tga")
        if os.path.exists(target):
            raise TextureStageError(
                "Reserved texture target already exists: %s" % target)
        try:
            image_file_to_tga(
                source, target, soften=False, fill_black=fill_black)
        except BaseException:
            if os.path.isfile(target):
                os.remove(target)
            raise
        if os.path.isfile(source):
            os.remove(source)
        ref["filename"] = stem + ".tga"
        ref["path"] = target
    return refs
