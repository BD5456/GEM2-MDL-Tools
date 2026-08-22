"""Generate self-contained vehicle DEF files for vanilla GOH 1.065.1.

MOWAS2 vehicle DEF files are not source-compatible with GOH. In particular,
legacy armor_heavy/armor_engine/armor_mantlet and tank_ammo calls do not exist
in GOH's vehicle property chain. This module rebuilds a conservative DEF using
only calls verified in the installed GOH properties.pak and stock vehicle DEFs.
"""

import codecs
import hashlib
import json
import os
import re
import shutil


TARGET_GOH = 'GOH'

# Verified in GOH 1.065.1 properties.pak and stock -vehicle.pak DEF files.
VANILLA_CALLS = {
    'abm_dymamic',
    'crew_4_human',
    'mobility_tank',
    'restore_ik_time',
    'tank_heavy_tier1',
    'tank_heavy_tier2',
    'tank_medium_tier1',
    'tank_medium_tier2',
    'tank_trace',
    'turret_heavy',
    'turret_medium',
}
LEGACY_MOWAS2_CALLS = {
    'abm_howitzer',
    'armor_engine',
    'armor_heavy',
    'armor_mantlet',
    'tank_ammo',
}

_CALL_RE = re.compile(r'^[ \t]*\(\s*"([^"]+)"',
                      re.IGNORECASE | re.MULTILINE)
_EXTENSION_RE = re.compile(
    r'\{\s*extension\s+"([^"]+\.mdl)"\s*\}', re.IGNORECASE)
_VOLUME_RE = re.compile(r'\{\s*volume\s+"([^"]+)"', re.IGNORECASE)
_BONE_RE = re.compile(
    r'\{\s*bone(?:\s+[A-Za-z_][\w-]*)?\s+"([^"]+)"', re.IGNORECASE)
_PLACE_RE = re.compile(r'\{\s*place\s+"([^"]+)"', re.IGNORECASE)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _decode_file(path):
    with open(path, 'rb') as handle:
        raw = handle.read()
    has_bom = raw.startswith(codecs.BOM_UTF8)
    payload = raw[len(codecs.BOM_UTF8):] if has_bom else raw
    return payload.decode('utf-8', errors='surrogateescape'), raw


def _code_mask(text):
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


def _validate_structure(text):
    mask = _code_mask(text)
    depth = 0
    for index, char in enumerate(text):
        if not mask[index]:
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth < 0:
                raise ValueError('Unexpected DEF closing brace at byte %d' % index)
    if depth:
        raise ValueError('Unbalanced DEF braces: depth %d' % depth)


def _named_blocks(text, pattern):
    mask = _code_mask(text)
    blocks = []
    for match in pattern.finditer(text):
        if not mask[match.start()]:
            continue
        end = _matching_brace(text, match.start())
        if end < 0:
            raise ValueError('Unclosed block at byte %d' % match.start())
        blocks.append((match.group(1), text[match.start():end + 1]))
    return blocks


def _float_value(text, pattern, default):
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return float(default)
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return float(default)


def _normalized_volume_name(name):
    value = re.sub(r'[^a-z0-9]', '', name.casefold())
    return value.replace('boby', 'body')


def _source_thicknesses(def_text):
    values = {}
    for name, block in _named_blocks(def_text, _VOLUME_RE):
        match = re.search(r'\{\s*thickness\s+([0-9]+(?:\.[0-9]+)?)',
                          block, re.IGNORECASE)
        if match:
            values[_normalized_volume_name(name)] = float(match.group(1))
    return values


def _default_thickness(name):
    key = name.casefold()
    if key.startswith('track'):
        return 30.0
    if key.startswith('gun') or 'mantlet' in key:
        return 50.0
    if key.startswith('turret'):
        return 80.0
    if key.startswith('engine'):
        return 40.0
    if key.startswith('body') or key.startswith('boby'):
        return 80.0
    if key in {'crew', 'inventory', 'ammo', 'fuel'}:
        return 15.0
    return 20.0


def _format_number(value):
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return ('%.4f' % value).rstrip('0').rstrip('.')


def _extract_profile(def_text, mdl_text):
    volume_names = [name for name, _block in _named_blocks(mdl_text, _VOLUME_RE)]
    if not volume_names:
        raise ValueError('MDL has no Volume blocks for a GOH vehicle DEF')
    bones = {name.casefold() for name in _BONE_RE.findall(mdl_text)}
    source_thickness = _source_thicknesses(def_text)
    volumes = []
    for name in volume_names:
        thickness = source_thickness.get(
            _normalized_volume_name(name), _default_thickness(name))
        volumes.append((name, max(1.0, min(float(thickness), 2000.0))))

    mass = _float_value(def_text, r'\{\s*mass\s+([0-9.]+)', 45000)
    weight = max(1.0, mass / 1000.0)
    speed = _float_value(
        def_text, r'\{\s*MaxSpeed\s+([0-9.]+)',
        _float_value(def_text, r'\{\s*Normal\s+([0-9.]+)', 35))
    fuel = _float_value(
        def_text,
        r'\{\s*FuelBag\b.*?\{\s*volume\s+([0-9.]+)', 600)
    step = _float_value(def_text, r'\btank_trace\b.*?step\(([0-9.-]+)\)', 0.47)
    length = _float_value(def_text, r'\btank_trace\b.*?len\(([0-9.-]+)\)', 0.8)
    barrels = int(max(1, min(8, _float_value(
        def_text, r'\{\s*Barrels\s+([0-9]+)', 1))))

    places = [name for name in _PLACE_RE.findall(def_text)
              if name.casefold().startswith('gun')]
    main_place = next((name for name in places
                       if not name.casefold().startswith('gunner')), None)
    if not main_place:
        main_place = 'gun1' if 'gun1' in bones else 'gun'

    return {
        'volume_names': volume_names,
        'volumes': volumes,
        'bones': bones,
        'mass': mass,
        'weight': weight,
        'speed': max(5.0, min(speed, 120.0)),
        'reverse': max(5.0, min(speed * 0.28, 30.0)),
        'traverse': 20.0,
        'power': max(300.0, min(weight * 10.0, 8000.0)),
        'fuel': max(50.0, min(fuel, 10000.0)),
        'step': step,
        'length': length,
        'barrels': barrels,
        'main_place': main_place,
        'has_turret': 'turret' in bones,
        'has_gun_rot': 'gun_rot' in bones,
    }


def build_vanilla_goh_def(def_text, mdl_text, mdl_filename):
    """Return generated DEF text and a JSON-serializable profile report."""
    _validate_structure(mdl_text)
    if def_text:
        _validate_structure(def_text)
    profile = _extract_profile(def_text, mdl_text)
    heavy = profile['weight'] >= 40.0
    very_heavy = profile['weight'] >= 55.0
    target = 'tank_heavy' if heavy else 'tank_medium'
    tier = ('tank_heavy_tier2' if very_heavy else
            'tank_heavy_tier1' if heavy else 'tank_medium_tier2')
    turret_call = 'turret_heavy' if heavy else 'turret_medium'
    include = '/properties/tank.ext' if profile['has_turret'] else '/properties/tank_assaultgun.ext'
    selection = ('/properties/selection/vehicle_big.inc'
                 if profile['weight'] >= 55.0 else
                 '/properties/selection/vehicle.inc')
    props = ('"heavy" "vision_lev05" "muzzle_122mm_128mm_sides" '
             '"diesel" "detect_tank_heavy" "sq_armor_super_heavy"')
    if not profile['has_turret']:
        props = '"td" "heavy" "-turret" "vision_lev04" "sq_at_gun"'

    lines = [
        '; Generated by GEM2 Engine Tools from a legacy MOWAS2 vehicle.',
        '; Calls and placeholder armament use stock GOH 1.065.1 resources only.',
        '{game_entity',
        '\t(include "%s")' % include,
        '\t(include "%s" scale(1.0))' % selection,
        '\t{props %s}' % props,
        '\t{Extension "%s"}' % mdl_filename,
        '\t{PatherId "%s"}' % ('tiger2' if heavy else 'panzer3'),
        '\t{targetclass "%s"}' % target,
        '\t{targetSelector "%s"}' % target,
        '\t{collider "tank.heavy"}',
        '',
        '\t("%s")' % tier,
        '',
    ]
    for name, thickness in profile['volumes']:
        lines.extend([
            '\t{Volume "%s"' % name,
            '\t\t{thickness %s}' % _format_number(thickness),
            '\t}',
        ])
    lines.extend([
        '',
        '\t("crew_4_human")',
        '',
        '\t{extender "inventory"',
        '\t\t{box',
        '\t\t\t{item "bulletrus_122_l48 aphebc" 40}',
        '\t\t\t{item "bulletrus_122_l48 he" 30}',
        '\t\t\t{item "ammo hmgun_usa" 1200}',
        '\t\t\t{item "satchel_charge_rus" 1}',
        '\t\t}',
        '\t}',
        '',
        '\t{Weaponry',
        '\t\t("restore_ik_time")',
        '\t\t{place "%s"' % profile['main_place'],
        '\t\t\t{RestoreIKAfterAim}',
        '\t\t\t{weapon "122mm_d25" filling "bulletrus_122_l48 aphebc" 1}',
    ])
    if profile['barrels'] > 1:
        lines.append('\t\t\t{Barrels %d}' % profile['barrels'])
    lines.extend([
        '\t\t\t{gunner "gunner"}',
        '\t\t\t{charger "charger"}',
        '\t\t\t("abm_dymamic" zeroing(3.5) dispersion(0.20))',
        '\t\t}',
        '\t}',
        '',
        '\t{mass %s}' % _format_number(profile['mass']),
        '\t{Chassis',
        '\t\t("tank_trace" fx("tracks_big") step(%s) len(%s))' % (
            _format_number(profile['step']), _format_number(profile['length'])),
        '\t\t("mobility_tank"',
        '\t\t\tspeed(%s)' % _format_number(profile['speed']),
        '\t\t\treverse(%s)' % _format_number(profile['reverse']),
        '\t\t\ttraverse(%s)' % _format_number(profile['traverse']),
        '\t\t\tweight(%s)' % _format_number(profile['weight']),
        '\t\t\tpower(%s)' % _format_number(profile['power']),
        '\t\t\ttrack(3.0)',
        '\t\t\tfuel(%s)' % _format_number(profile['fuel']),
        '\t\t\ttype(diesel)',
        '\t\t\trange(100)',
        '\t\t)',
        '\t}',
    ])
    if profile['has_gun_rot']:
        lines.extend([
            '',
            '\t{bone "gun_rot"',
            '\t\t{limits -8 20}',
            '\t\t{speed2 4}',
            '\t}',
        ])
    if profile['has_turret']:
        lines.extend([
            '\t{bone "turret"',
            '\t\t("%s" power_traverse(20))' % turret_call,
            '\t}',
        ])
    lines.extend(['}', ''])
    generated = '\n'.join(lines)
    report = validate_vanilla_goh_def(generated, mdl_text, mdl_filename)
    report.update({
        'mass': profile['mass'],
        'weight': profile['weight'],
        'main_place': profile['main_place'],
        'barrels': profile['barrels'],
        'has_turret': profile['has_turret'],
        'volume_thickness': {
            name: thickness for name, thickness in profile['volumes']
        },
    })
    return generated, report


def validate_vanilla_goh_def(def_text, mdl_text, mdl_filename):
    _validate_structure(def_text)
    if not re.search(r'\{\s*game_entity\b', def_text, re.IGNORECASE):
        raise ValueError('Generated DEF has no game_entity root')
    extensions = _EXTENSION_RE.findall(def_text)
    if extensions != [mdl_filename]:
        raise ValueError('Generated DEF Extension does not match %s: %r'
                         % (mdl_filename, extensions))

    calls = {name.casefold() for name in _CALL_RE.findall(def_text)}
    legacy = sorted(calls & LEGACY_MOWAS2_CALLS)
    if legacy:
        raise ValueError('Generated DEF still calls MOWAS2 defines: %s'
                         % ', '.join(legacy))
    unknown = sorted(calls - VANILLA_CALLS)
    if unknown:
        raise ValueError('Generated DEF calls unverified GOH defines: %s'
                         % ', '.join(unknown))

    mdl_volumes = [name for name, _block in _named_blocks(mdl_text, _VOLUME_RE)]
    def_volumes = [name for name, _block in _named_blocks(def_text, _VOLUME_RE)]
    if def_volumes != mdl_volumes:
        raise ValueError('Generated DEF volume list does not match MDL')
    return {
        'target': TARGET_GOH,
        'generator': 'GOH_VANILLA_1_065_1',
        'extension': mdl_filename,
        'calls': sorted(calls),
        'volumes': def_volumes,
        'legacy_calls': legacy,
        'warnings': [],
    }


def prepare_vanilla_goh_def(source_dir, mdl_path):
    """Validate inputs and build a GOH DEF plan without writing output."""
    mdl_filename = os.path.basename(mdl_path)
    entity_name = os.path.splitext(mdl_filename)[0]
    source_def = os.path.join(source_dir, entity_name + '.def')
    if not os.path.isfile(source_def):
        candidates = sorted(
            os.path.join(source_dir, name)
            for name in os.listdir(source_dir)
            if name.lower().endswith('.def'))
        source_def = candidates[0] if candidates else None

    def_text = ''
    source_bytes = b''
    if source_def:
        def_text, source_bytes = _decode_file(source_def)
    mdl_text, _mdl_bytes = _decode_file(mdl_path)
    generated, report = build_vanilla_goh_def(
        def_text, mdl_text, mdl_filename)
    generated_bytes = generated.encode('utf-8', errors='surrogateescape')
    return {
        'entity_name': entity_name,
        'source_def': source_def,
        'source_bytes': source_bytes,
        'generated_bytes': generated_bytes,
        'report': report,
    }


def write_vanilla_goh_def(plan, output_dir):
    """Write a prepared GOH DEF and preserve its MOWAS2 source as .bak."""
    os.makedirs(output_dir, exist_ok=True)
    output_def = os.path.join(output_dir, plan['entity_name'] + '.def')
    generated_bytes = plan['generated_bytes']

    # Back up first so this helper also remains safe when source and output
    # directories happen to be identical outside the normal vehicle operator.
    source_def = plan['source_def']
    backup_path = None
    if source_def:
        backup_path = output_def + '.mowas2.bak'
        shutil.copy2(source_def, backup_path)

    temporary = output_def + '.tmp'
    try:
        with open(temporary, 'wb') as handle:
            handle.write(generated_bytes)
        os.replace(temporary, output_def)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    report = dict(plan['report'])
    source_bytes = plan['source_bytes']
    report.update({
        'source_def': source_def,
        'output_def': output_def,
        'backup_def': backup_path,
        'source_sha256': _sha256(source_bytes) if source_bytes else None,
        'output_sha256': _sha256(generated_bytes),
        'changed': generated_bytes != source_bytes,
    })
    return report


def export_vanilla_goh_def(source_dir, output_dir, mdl_path):
    """Generate the output DEF and preserve the source DEF as a .bak file."""
    plan = prepare_vanilla_goh_def(source_dir, mdl_path)
    return write_vanilla_goh_def(plan, output_dir)


def report_json(report):
    return json.dumps(report, ensure_ascii=False, sort_keys=True)
