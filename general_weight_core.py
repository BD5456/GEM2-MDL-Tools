"""Pure helpers for generic surface-projected vertex weights."""

from __future__ import annotations

import hashlib
import json


def clean_weight_vector(weights, min_weight=0.0001, limit_influences=True,
                        max_influences=4, normalize=True):
    """Prune and optionally normalize one sparse ``(group, weight)`` vector.

    No fallback group is invented. An empty input or a vector fully removed by
    the threshold stays empty so the Blender integration can report it.
    """
    threshold = max(0.0, float(min_weight))
    merged = {}
    for name, raw_weight in weights:
        weight = max(0.0, float(raw_weight))
        if weight <= 0.0 or weight < threshold:
            continue
        merged[str(name)] = merged.get(str(name), 0.0) + weight
    ordered = sorted(merged.items(), key=lambda item: (-item[1], item[0]))
    if limit_influences:
        ordered = ordered[:max(1, int(max_influences))]
    total = sum(weight for _name, weight in ordered)
    if normalize and total > 0.0:
        ordered = [(name, weight / total) for name, weight in ordered]
    return ordered


def weighted_coverage(weighted_vertices, total_vertices):
    total = max(0, int(total_vertices))
    weighted = max(0, min(total, int(weighted_vertices)))
    return (weighted / total) if total else 0.0


def stable_signature(payload):
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
