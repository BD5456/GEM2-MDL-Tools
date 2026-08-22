"""On-demand access to packed GOH resources without unpacking whole PAK files."""
import hashlib
import os
import posixpath
import re
import shutil
import tempfile
import threading
import zipfile


_TEXTURE_EXTENSIONS = (
    '.dds', '.tga', '.png', '.bmp', '.jpg', '.jpeg', '.tif', '.tiff',
    '.ctm', '.ebm', '.tex',
)
_KNOWN_RESOURCE_ROOTS = (
    r'D:\SteamLibrary\steamapps\common\Call to Arms - Gates of Hell\resource',
    r'E:\SteamLibrary\steamapps\common\Call to Arms - Gates of Hell\resource',
    r'C:\Program Files (x86)\Steam\steamapps\common\Call to Arms - Gates of Hell\resource',
)
_ARCHIVE_INDEX_CACHE = {}
_CACHE_LOCK = threading.RLock()


def _normalize_member_reference(reference):
    if not reference:
        return ''
    value = str(reference).strip().strip('"').replace('\\', '/')
    if value.startswith('$'):
        value = value[1:]
    value = value.lstrip('/')
    if not value or re.match(r'^[A-Za-z]:', value):
        return ''
    normalized = posixpath.normpath(value)
    if (not normalized or normalized in {'.', '..'} or
            normalized.startswith('../') or '/..' in normalized):
        return ''
    return normalized


def _resource_root_from_path(path):
    if not path:
        return None
    current = os.path.abspath(path)
    if os.path.isfile(current):
        current = os.path.dirname(current)
    for _index in range(20):
        if os.path.basename(current).casefold() == 'resource':
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _resource_roots(search_paths):
    roots = []
    seen = set()
    env_root = os.environ.get('GOH_RESOURCE_ROOT')
    candidates = list(search_paths or [])
    if env_root:
        candidates.append(env_root)
    candidates.extend(_KNOWN_RESOURCE_ROOTS)
    for candidate in candidates:
        root = _resource_root_from_path(candidate)
        if root is None and os.path.basename(
                os.path.abspath(candidate)).casefold() == 'resource':
            root = os.path.abspath(candidate)
        if not root or not os.path.isdir(root):
            continue
        key = os.path.normcase(root)
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


def _member_candidates(reference):
    normalized = _normalize_member_reference(reference)
    if not normalized:
        return []
    extension = posixpath.splitext(normalized)[1].casefold()
    if extension:
        return [normalized]
    return [normalized + extension for extension in _TEXTURE_EXTENSIONS]


def _candidate_archives(resource_root, normalized_reference):
    head = normalized_reference.split('/', 1)[0].casefold()
    common = os.path.join(resource_root, 'texture', 'common')
    candidates = []
    if head.startswith('_'):
        candidates.append(os.path.join(common, head + '.pak'))
    if head == 'vehicle':
        candidates.append(os.path.join(common, 'common.pak'))
    candidates.extend((
        os.path.join(common, 'common.pak'),
        os.path.join(resource_root, 'texture', 'texture.pak'),
    ))
    result = []
    seen = set()
    for candidate in candidates:
        key = os.path.normcase(candidate)
        if key not in seen and os.path.isfile(candidate):
            seen.add(key)
            result.append(candidate)
    return result


def _archive_fingerprint(filepath):
    stat = os.stat(filepath)
    payload = '%s\0%d\0%d' % (
        os.path.normcase(os.path.abspath(filepath)),
        stat.st_size,
        stat.st_mtime_ns,
    )
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:20]


def _archive_index(filepath):
    stat = os.stat(filepath)
    cache_key = (
        os.path.normcase(os.path.abspath(filepath)),
        stat.st_size,
        stat.st_mtime_ns,
    )
    with _CACHE_LOCK:
        cached = _ARCHIVE_INDEX_CACHE.get(cache_key)
        if cached is not None:
            return cached
    with zipfile.ZipFile(filepath, 'r') as archive:
        index = {
            info.filename.replace('\\', '/').lstrip('/').casefold(): info
            for info in archive.infolist()
            if not info.is_dir()
        }
    with _CACHE_LOCK:
        stale = [key for key in _ARCHIVE_INDEX_CACHE
                 if key[0] == cache_key[0] and key != cache_key]
        for key in stale:
            _ARCHIVE_INDEX_CACHE.pop(key, None)
        _ARCHIVE_INDEX_CACHE[cache_key] = index
    return index


def _find_packed_member(reference, search_paths):
    member_candidates = _member_candidates(reference)
    if not member_candidates:
        return None
    normalized = member_candidates[0]
    for resource_root in _resource_roots(search_paths):
        for archive_path in _candidate_archives(resource_root, normalized):
            try:
                index = _archive_index(archive_path)
            except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
                continue
            for candidate in member_candidates:
                info = index.get(candidate.casefold())
                if info is not None:
                    return archive_path, info, index
    return None


def _cache_root():
    local = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
    return os.path.join(local, 'gem2_goh_tools', 'asset-cache')


def _cached_member_path(archive_path, info):
    relative = _normalize_member_reference(info.filename)
    if not relative:
        raise RuntimeError('Unsafe PAK member path: %s' % info.filename)
    root = os.path.join(_cache_root(), _archive_fingerprint(archive_path))
    destination = os.path.abspath(os.path.join(
        root, *relative.split('/')))
    if os.path.commonpath((root, destination)) != os.path.abspath(root):
        raise RuntimeError('PAK member escapes texture cache: %s' % info.filename)
    return destination


def _extract_member(archive_path, info):
    destination = _cached_member_path(archive_path, info)
    if os.path.isfile(destination) and os.path.getsize(destination) == info.file_size:
        return destination
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = destination + '.tmp.%d.%d' % (
        os.getpid(), threading.get_ident())
    try:
        with zipfile.ZipFile(archive_path, 'r') as archive:
            with archive.open(info, 'r') as source, open(temporary, 'wb') as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
        if os.path.getsize(temporary) != info.file_size:
            raise RuntimeError('Incomplete PAK extraction: %s' % info.filename)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    print('[PAK] extracted %s <- %s' % (info.filename, archive_path))
    return destination


def resolve_packed_texture(reference, search_paths=()):
    """Resolve one virtual texture reference to an on-demand cache file."""
    found = _find_packed_member(reference, search_paths)
    if found is None:
        return None
    archive_path, info, _index = found
    try:
        return _extract_member(archive_path, info)
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None


def packed_texture_variants(reference, search_paths=()):
    """Extract ``#variant`` siblings for a packed texture reference."""
    found = _find_packed_member(reference, search_paths)
    if found is None:
        return []
    archive_path, info, index = found
    member = info.filename.replace('\\', '/').lstrip('/')
    stem, extension = posixpath.splitext(member)
    base_stem = stem.split('#', 1)[0]
    prefix = (base_stem + '#').casefold()
    extension_key = extension.casefold()
    variants = []
    seen = set()
    for key, candidate in sorted(index.items()):
        candidate_name = candidate.filename.replace('\\', '/').lstrip('/')
        candidate_stem, candidate_extension = posixpath.splitext(candidate_name)
        if (candidate_stem.casefold().startswith(prefix) and
                candidate_extension.casefold() == extension_key):
            identity = candidate.filename.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            try:
                variants.append(_extract_member(archive_path, candidate))
            except (OSError, RuntimeError, zipfile.BadZipFile,
                    zipfile.LargeZipFile):
                continue
    return variants


def is_packed_cache_path(filepath):
    if not filepath:
        return False
    try:
        return os.path.commonpath((
            os.path.abspath(_cache_root()),
            os.path.abspath(filepath),
        )) == os.path.abspath(_cache_root())
    except ValueError:
        return False


def clear_pak_caches():
    """Release in-process central-directory indexes."""
    with _CACHE_LOCK:
        _ARCHIVE_INDEX_CACHE.clear()
