"""Conservative GOH/MOWAS2 MDL compatibility conversion.

Only syntax proven incompatible with the MOWAS2 3.262 parser is changed.
GOH animation ``events`` blocks are hidden in an MDL comment for MOWAS2 and
can be restored byte-for-byte when converting the same file back to GOH.
"""
import base64
import binascii
import codecs
import hashlib
import os
import re


TARGET_GOH = 'GOH'
TARGET_MOWAS2 = 'MOWAS2'
_MARKER = 'GEM2_GOH_COMPAT_EVENTS_B64'
_MARKER_END = 'GEM2_GOH_COMPAT_EVENTS_END'
_SEQUENCE_RE = re.compile(r'\{\s*sequence\s+"[^"]+"', re.IGNORECASE)
_EVENTS_RE = re.compile(r'\{\s*events\b', re.IGNORECASE)
_RESTORE_RE = re.compile(
    r'\r?\n[ \t]*;[ \t]*' + re.escape(_MARKER) +
    r'[ \t]+(?P<data>[A-Za-z0-9+/=]+)[ \t]*\r?\n'
    r'[ \t]*;[ \t]*' + re.escape(_MARKER_END) + r'[ \t]*\r?\n')


def _code_mask(text):
    """Mark positions outside quoted strings and semicolon comments."""
    code = bytearray(b'\x01') * len(text)
    in_string = False
    in_comment = False
    escaped = False
    for index, char in enumerate(text):
        if in_comment:
            code[index] = 0
            if char in '\r\n':
                in_comment = False
            continue
        if in_string:
            code[index] = 0
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == ';':
            code[index] = 0
            in_comment = True
        elif char == '"':
            code[index] = 0
            in_string = True
    return code


def _matching_brace(text, start):
    depth = 0
    in_string = False
    in_comment = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_comment:
            if char in '\r\n':
                in_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == ';':
            in_comment = True
        elif char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return index
    return -1


def _relative_brace_depth(text, start, end):
    depth = 0
    mask = _code_mask(text[start:end])
    for offset, char in enumerate(text[start:end]):
        if not mask[offset]:
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
    return depth


def _sequence_spans(text, mask):
    spans = []
    for match in _SEQUENCE_RE.finditer(text):
        if not mask[match.start()]:
            continue
        end = _matching_brace(text, match.start())
        if end < 0:
            raise ValueError('Unclosed MDL sequence block at byte %d'
                             % match.start())
        spans.append((match.start(), end + 1))
    return spans


def _event_spans_inside_sequences(text):
    mask = _code_mask(text)
    sequences = _sequence_spans(text, mask)
    spans = []
    warnings = []
    for match in _EVENTS_RE.finditer(text):
        if not mask[match.start()]:
            continue
        end = _matching_brace(text, match.start())
        if end < 0:
            raise ValueError('Unclosed MDL events block at byte %d'
                             % match.start())
        owner = next((span for span in sequences
                      if span[0] < match.start() and end < span[1]), None)
        if owner is None:
            warnings.append('events block outside sequence at byte %d'
                            % match.start())
            continue
        if _relative_brace_depth(text, owner[0], match.start()) != 1:
            warnings.append('nested events block left unchanged at byte %d'
                            % match.start())
            continue
        spans.append((match.start(), end + 1))
    return spans, warnings


def _live_event_count(text):
    mask = _code_mask(text)
    return sum(1 for match in _EVENTS_RE.finditer(text)
               if mask[match.start()])


def _validate_structure(text):
    depth = 0
    in_string = False
    in_comment = False
    escaped = False
    for index, char in enumerate(text):
        if in_comment:
            if char in '\r\n':
                in_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == ';':
            in_comment = True
        elif char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth < 0:
                raise ValueError('Unexpected closing MDL brace at byte %d'
                                 % index)
    if in_string:
        raise ValueError('Unclosed quoted string in MDL')
    if depth:
        raise ValueError('Unbalanced MDL braces: depth %d' % depth)


def _to_mowas2(text):
    newline = '\r\n' if '\r\n' in text else '\n'
    spans, warnings = _event_spans_inside_sequences(text)
    converted = text
    for start, end in reversed(spans):
        block = converted[start:end]
        encoded = base64.b64encode(
            block.encode('utf-8', errors='surrogateescape')).decode('ascii')
        line_start = converted.rfind('\n', 0, start) + 1
        line_prefix = converted[line_start:start]
        base_indent_match = re.match(r'[ \t]*', line_prefix)
        base_indent = base_indent_match.group(0) if base_indent_match else ''
        marker_indent = base_indent + '\t'
        replacement = (
            newline + marker_indent + '; ' + _MARKER + ' ' + encoded +
            newline + marker_indent + '; ' + _MARKER_END + newline)
        converted = converted[:start] + replacement + converted[end:]
    return converted, {
        'events_hidden': len(spans),
        'events_restored': 0,
        'markers_remaining': converted.count(_MARKER),
        'warnings': warnings,
    }


def _to_goh(text):
    restored = 0
    warnings = []

    def restore(match):
        nonlocal restored
        try:
            payload = base64.b64decode(
                match.group('data').encode('ascii'), validate=True)
            block = payload.decode('utf-8', errors='surrogateescape')
        except (binascii.Error, ValueError, UnicodeError) as exc:
            raise ValueError('Corrupt GOH events marker: %s' % exc) from exc
        _validate_structure(block)
        event_match = _EVENTS_RE.match(block)
        if (event_match is None or
                _matching_brace(block, event_match.start()) != len(block) - 1):
            raise ValueError('GOH events marker does not contain one events block')
        restored += 1
        return block

    converted = _RESTORE_RE.sub(restore, text)
    return converted, {
        'events_hidden': 0,
        'events_restored': restored,
        'markers_remaining': converted.count(_MARKER),
        'warnings': warnings,
    }


def convert_mdl_text(text, target):
    """Return ``(converted_text, report)`` for one target engine."""
    normalized_target = str(target or '').strip().upper()
    if normalized_target not in {TARGET_GOH, TARGET_MOWAS2}:
        raise ValueError('Unsupported MDL target: %s' % target)
    _validate_structure(text)
    start_markers = text.count(_MARKER)
    end_markers = text.count(_MARKER_END)
    if start_markers != end_markers:
        raise ValueError('Incomplete GOH events compatibility marker')

    if normalized_target == TARGET_MOWAS2:
        converted, details = _to_mowas2(text)
        if details['warnings'] or _live_event_count(converted):
            raise ValueError('MDL contains events outside a sequence direct child')
    else:
        converted, details = _to_goh(text)
        _spans, warnings = _event_spans_inside_sequences(converted)
        if warnings:
            raise ValueError('Restored MDL contains misplaced events blocks')
        if converted.count(_MARKER) or converted.count(_MARKER_END):
            raise ValueError('Unrestored GOH events compatibility marker')

    _validate_structure(converted)
    details.update({
        'target': normalized_target,
        'changed': converted != text,
    })
    details.setdefault('warnings', [])
    return converted, details


def convert_mdl_file(filepath, target):
    """Convert an MDL file in place and return a serializable report."""
    filepath = os.path.abspath(filepath)
    with open(filepath, 'rb') as handle:
        original_bytes = handle.read()
    has_bom = original_bytes.startswith(codecs.BOM_UTF8)
    payload = original_bytes[len(codecs.BOM_UTF8):] if has_bom else original_bytes
    text = payload.decode('utf-8', errors='surrogateescape')
    converted, report = convert_mdl_text(text, target)
    converted_bytes = converted.encode('utf-8', errors='surrogateescape')
    if has_bom:
        converted_bytes = codecs.BOM_UTF8 + converted_bytes
    if converted_bytes != original_bytes:
        temporary = filepath + '.compat.tmp'
        try:
            with open(temporary, 'wb') as handle:
                handle.write(converted_bytes)
            os.replace(temporary, filepath)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
    report.update({
        'filepath': filepath,
        'before_sha256': hashlib.sha256(original_bytes).hexdigest(),
        'after_sha256': hashlib.sha256(converted_bytes).hexdigest(),
        'bytes_before': len(original_bytes),
        'bytes_after': len(converted_bytes),
    })
    return report
