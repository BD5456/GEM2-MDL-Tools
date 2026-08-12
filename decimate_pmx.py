# -*- coding: utf-8 -*-
"""
PMX Decimator v2 — Open3D QEM per-material + KD-tree UV + weight + containment.
===============================================================================
Flow:
  1. Classify materials → locked (face/eyes), reducible.
  2. (Optional) Containment culling: closest-surface + ray fallback.
  3. Per-material Open3D QEM decimate with proportional+floor budget.
  4. Rebuild mesh with per-loop UV from face-centre KD-tree matching.
  5. Restore vertex weights via KD-tree nearest-neighbor.
"""

import bpy
import numpy as np
import time
from collections import defaultdict
from mathutils import Vector
from .i18n import _

REGION_PATTERNS = [
    (["face", "head", "kao", "mayuge", "sirome", "eyeline",
      "eyelash", "eye", "tooth", "tongue", "nose", "mouth", "lip"],
     "face", 1.00),
    (["hitomi", "pupil", "eyeball", "glint", "highlight"],
     "eyes", 1.00),
    (["hair", "kami", "bang", "fringe", "pony", "braid", "ahoge"],
     "hair", 0.45),
    (["panst", "stocking", "sock", "tights", "leotard", "bodysuit",
      "swimsuit", "bikini", "underwear", "panties", "bra",
      "tight", "bodysock", "bodystocking", "spats", "legging"],
     "skin_tight", 0.65),
    (["body", "skin", "karada", "hada", "torso", "belly"],
     "body", 0.40),
    (["clothes", "top", "bottom", "skirt", "dress", "jacket", "coat",
      "pants", "shorts", "shirt", "sleeve", "collar", "tie", "ribbon",
      "shoes", "boot", "heel", "glove", "cape",
      "apron", "armor", "uniform", "school", "bunny", "suit",
      "cloth"], "clothes", 0.35),
    (["accessory", "acs", "slot", "ears", "earring", "necklace",
      "halo", "wing", "tail", "cuff", "choker", "bell",
      "hat", "cap", "crown", "bow", "seal", "badge",
      "weapon", "gun", "sword", "shield", "holster",
      "bag", "backpack"], "accessory", 0.30),
]
FALLBACK = ("other", 0.40)

INNER_KEYWORDS = [
    "inner", "naka", "underwear", "panties", "bra",
    "fundoshi", "shitagi", "hadagi",
]


def _classify(mat_name):
    low = mat_name.lower()
    for keywords, region, ratio in REGION_PATTERNS:
        for kw in keywords:
            if kw in low:
                return region, ratio
    return FALLBACK


def _is_inner(mat_name):
    low = mat_name.lower()
    for kw in INNER_KEYWORDS:
        if kw in low:
            return True
    return False


def _try_import_kdtree():
    try:
        from scipy.spatial import KDTree
        return KDTree, True
    except ImportError:
        return None, False


# ═══════════════════════════════════════════════════════════════
#  Vertex weight I/O
# ═══════════════════════════════════════════════════════════════

def _save_vertex_weights(mesh_obj):
    nv = len(mesh_obj.data.vertices)
    nvg = len(mesh_obj.vertex_groups)
    if nvg == 0:
        return None, None
    w = np.zeros((nv, nvg), dtype=np.float64)
    for v in mesh_obj.data.vertices:
        for g in v.groups:
            if g.group < nvg:
                w[v.index, g.group] = g.weight
    vg_names = [vg.name for vg in mesh_obj.vertex_groups]
    return w, vg_names


def _restore_vertex_weights(mesh_obj, old_verts, old_weights, vg_names,
                             new_verts):
    KDTree, has_kdtree = _try_import_kdtree()
    if old_weights is None or vg_names is None:
        return
    n_old = len(old_verts)
    n_new = len(new_verts)
    nvg = old_weights.shape[1]

    if has_kdtree and n_old > 0:
        tree = KDTree(old_verts)
        _u, nn = tree.query(new_verts)
    else:
        nn = np.zeros(n_new, dtype=np.int32)
        for i in range(n_new):
            dists = np.sum((old_verts - new_verts[i]) ** 2, axis=1)
            nn[i] = np.argmin(dists)

    new_w = old_weights[nn]
    for vg in list(mesh_obj.vertex_groups):
        mesh_obj.vertex_groups.remove(vg)
    vg_objs = []
    for gi in range(nvg):
        vg_objs.append(mesh_obj.vertex_groups.new(name=vg_names[gi]))
    for vi in range(n_new):
        row = new_w[vi]
        for gi in range(nvg):
            w_val = row[gi]
            if w_val > 1e-10:
                vg_objs[gi].add([vi], w_val, 'REPLACE')


# ═══════════════════════════════════════════════════════════════
#  Containment test
# ═══════════════════════════════════════════════════════════════

def _fibonacci_sphere(n):
    pts = []
    phi = np.pi * (3.0 - np.sqrt(5.0))
    for i in range(n):
        y = 1.0 - (i / float(n - 1)) * 2.0
        r = np.sqrt(1.0 - y * y)
        theta = phi * i
        pts.append(Vector((float(np.cos(theta) * r), float(y),
                           float(np.sin(theta) * r))))
    return pts


def _containment_cull_body_faces(mesh_obj, orig_verts, tri_faces_all,
                                  tri_poly_all, mat_locked, mat_faces,
                                  arm_obj=None, num_rays=32, verbose=True):
    """Closest-surface + ray fallback containment.

    Core: BVH nearest-point + normal-direction + distance.
      For each body face, find the closest shell point
      (clothes+skin_tight, no hair/accessories).
      If within 1-2cm AND normals oppose → covered → remove.

    Fallback: multi-ray for loose/voluminous clothing.
    """
    from mathutils.bvhtree import BVHTree

    mesh = mesh_obj.data
    num_mats = len(mesh.materials)

    body_mats = [mi for mi in mat_locked
                 if not mat_locked[mi]
                 and _classify(mesh.materials[mi].name
                               if mi < num_mats and mesh.materials[mi]
                               else "")[0] == "body"]
    tight_mats = [mi for mi in mat_locked
                  if not mat_locked[mi]
                  and _classify(mesh.materials[mi].name
                                if mi < num_mats and mesh.materials[mi]
                                else "")[0] == "skin_tight"]
    cloth_mats = [mi for mi in mat_locked
                  if not mat_locked[mi] and mi not in body_mats
                  and mi not in tight_mats
                  and _classify(mesh.materials[mi].name
                                if mi < num_mats and mesh.materials[mi]
                                else "")[0] == "clothes"]

    if not body_mats:
        return set()
    shell_mats = tight_mats + cloth_mats
    if not shell_mats:
        return set()

    # Shell BVH
    shell_face_list = sorted({fi for mi in shell_mats
                              for fi in mat_faces.get(mi, [])
                              if fi < len(tri_faces_all)})
    if not shell_face_list:
        return set()

    shell_verts_set = set(); shell_tris = []
    for fi in shell_face_list:
        f = tri_faces_all[fi]
        shell_verts_set.update(f); shell_tris.append(f)

    unique_v = sorted(shell_verts_set)
    v_map = {ov: nv for nv, ov in enumerate(unique_v)}
    bvh = BVHTree.FromPolygons(
        [(float(orig_verts[ov][0]), float(orig_verts[ov][1]),
          float(orig_verts[ov][2])) for ov in unique_v],
        [(v_map[f[0]], v_map[f[1]], v_map[f[2]]) for f in shell_tris])

    shell_face_normals = {}
    for fi, f in zip(shell_face_list, shell_tris):
        e1 = orig_verts[f[1]] - orig_verts[f[0]]
        e2 = orig_verts[f[2]] - orig_verts[f[0]]
        n = np.cross(e1, e2); nl = np.linalg.norm(n)
        shell_face_normals[fi] = n / nl if nl > 1e-12 else np.zeros(3)

    shell_face_mat = {fi: mesh.polygons[tri_poly_all[fi]].material_index
                      for fi in shell_face_list}

    to_remove = set()
    total_body = sum(len(mat_faces.get(mi, [])) for mi in body_mats)
    removed_close = 0; removed_ray = 0

    if verbose:
        print(_("decimate.containment_header", shell=len(shell_tris), body=total_body))

    # ── Core: closest-surface + normal + per-region distance ────
    for mi in body_mats:
        for fi in mat_faces.get(mi, []):
            if fi >= len(tri_faces_all):
                continue
            fvs = tri_faces_all[fi]
            fc = (orig_verts[fvs[0]] + orig_verts[fvs[1]] + orig_verts[fvs[2]]) / 3.0
            loc, _u, idx, dist = bvh.find_nearest(
                (float(fc[0]), float(fc[1]), float(fc[2])))
            if loc is None or idx is None or idx >= len(shell_face_list):
                continue

            shell_fi = shell_face_list[idx]
            shell_mat = shell_face_mat.get(shell_fi, -1)
            max_dist = 0.010 if shell_mat in tight_mats else 0.020
            if dist > max_dist:
                continue

            e1 = orig_verts[fvs[1]] - orig_verts[fvs[0]]
            e2 = orig_verts[fvs[2]] - orig_verts[fvs[0]]
            bn = np.cross(e1, e2)
            if np.linalg.norm(bn) < 1e-12:
                continue
            bn = bn / np.linalg.norm(bn)
            sn = shell_face_normals.get(shell_fi)
            if sn is None or np.linalg.norm(sn) < 1e-12:
                continue
            if np.dot(bn, sn) < -0.3:
                to_remove.add(fi)
                removed_close += 1

    # ── Fallback: ray containment ───────────────────────────────
    rays = _fibonacci_sphere(num_rays)
    hit_threshold = int(num_rays * 0.70)

    for mi in body_mats:
        for fi in mat_faces.get(mi, []):
            if fi in to_remove or fi >= len(tri_faces_all):
                continue
            fc = Vector(((orig_verts[tri_faces_all[fi][0]] +
                          orig_verts[tri_faces_all[fi][1]] +
                          orig_verts[tri_faces_all[fi][2]]) / 3.0).tolist())
            hits = 0
            for ray_dir in rays:
                if bvh.ray_cast(fc, ray_dir, 9999.0)[0] is not None:
                    hits += 1
            if hits >= hit_threshold:
                to_remove.add(fi)
                removed_ray += 1

    if verbose and total_body > 0:
        pct = len(to_remove) / total_body * 100
        print(_("decimate.containment", removed=len(to_remove), total=total_body,
                pct=pct, close=removed_close, ray=removed_ray))

    return to_remove


# ═══════════════════════════════════════════════════════════════
#  Main decimate function
# ═══════════════════════════════════════════════════════════════

def decimate(mesh_obj, arm_obj=None, max_faces=21845, verbose=True,
             occlusion_cull=True):
    import open3d as o3d
    KDTree, has_kdtree = _try_import_kdtree()

    t0 = time.time()
    mesh = mesh_obj.data
    n_initial_v = len(mesh.vertices)
    n_initial_f = len(mesh.polygons)

    if mesh.shape_keys:
        mesh_obj.shape_key_clear()
    if n_initial_f <= max_faces:
        return n_initial_v, n_initial_v, 0.0

    # ── 0. Save weights & read original data ────────────
    old_vg_weights, old_vg_names = _save_vertex_weights(mesh_obj)
    orig_verts = np.array([v.co.copy() for v in mesh.vertices], dtype=np.float64)

    uv_layer_name = "UVMap"
    uv_arr = None
    if mesh.uv_layers.active:
        uv_layer_name = mesh.uv_layers.active.name
        n_uv = len(mesh.uv_layers.active.data)
        uv_arr = np.empty((n_uv, 2), dtype=np.float64)
        mesh.uv_layers.active.data.foreach_get("uv", uv_arr.ravel())

    tri_face_vi = []
    tri_poly_idx = []
    tri_loop_idx = []
    for pi, p in enumerate(mesh.polygons):
        if len(p.loop_indices) == 3:
            tri_face_vi.append(tuple(mesh.loops[li].vertex_index
                                     for li in p.loop_indices))
            tri_poly_idx.append(pi)
            tri_loop_idx.append(tuple(p.loop_indices))
    tri_faces_all = np.array(tri_face_vi, dtype=np.int32)
    tri_poly_all = np.array(tri_poly_idx, dtype=np.int32)

    num_mats = len(mesh.materials)

    # ── 1. Classify materials ──────────────────────────
    mat_locked = {}
    mat_is_inner = {}
    mat_ratio = {}
    mat_faces = defaultdict(list)
    for fi, pi in enumerate(tri_poly_all):
        mi = mesh.polygons[pi].material_index
        mat_faces[mi].append(fi)
        if mi not in mat_locked:
            mat_name = mesh.materials[mi].name if mi < num_mats and mesh.materials[mi] else ""
            region, kr = _classify(mat_name)
            mat_locked[mi] = kr >= 1.0
            mat_is_inner[mi] = _is_inner(mat_name)
            mat_ratio[mi] = kr

    n_locked = sum(len(mat_faces[mi]) for mi in mat_faces if mat_locked.get(mi, False))
    n_reducible = n_initial_f - n_locked

    if verbose:
        locked_names = [mesh.materials[mi].name if mi < num_mats and mesh.materials[mi] else "?"
                        for mi in mat_locked if mat_locked[mi]]
        red_names = [mesh.materials[mi].name if mi < num_mats and mesh.materials[mi] else "?"
                     for mi in mat_locked if not mat_locked[mi]]
        print(_("decimate.status", name=mesh_obj.name, v=n_initial_v, f=n_initial_f,
                mats=num_mats))
        print(_("decimate.locked_reducible", locked=n_locked, reducible=n_reducible,
                locked_names=locked_names, reducible_names=red_names))

    if n_reducible <= 0:
        return n_initial_v, n_initial_v, 0.0

    # ── 2. Containment culling ────────────────────────
    occluded_faces = set()
    if occlusion_cull:
        occluded_faces = _containment_cull_body_faces(
            mesh_obj, orig_verts, tri_faces_all, tri_poly_all,
            mat_locked, mat_faces, arm_obj=arm_obj,
            num_rays=32, verbose=verbose)

    if occluded_faces:
        for mi in list(mat_faces.keys()):
            mat_faces[mi] = [fi for fi in mat_faces[mi]
                             if fi not in occluded_faces]
        n_reducible = sum(len(mat_faces[mi]) for mi in mat_faces
                          if not mat_locked.get(mi, False))

    # ── 3. Build budget ────────────────────────────────
    reducible_mats = sorted(mi for mi in mat_locked if not mat_locked[mi])
    reducible_groups_data = []
    n_total_red = 0
    for mi in reducible_mats:
        nf = len(mat_faces[mi])
        if nf < 4:
            continue
        reducible_groups_data.append((mi, nf))
        n_total_red += nf

    if n_total_red <= 0:
        if occluded_faces:
            pass  # proceed to rebuild
        else:
            return n_initial_v, n_initial_v, 0.0

    available_budget = max_faces - n_locked
    budgets = {}
    if n_total_red > available_budget:
        reducible_groups_data.sort(key=lambda x: x[1])
        remaining_budget = available_budget
        remaining_faces = n_total_red

        for mi, nf in reducible_groups_data:
            if mat_is_inner.get(mi, False):
                target = max(4, int(nf * 0.10))
            else:
                proportional = int(nf * (remaining_budget / remaining_faces)) if remaining_faces > 0 else 4
                kr = mat_ratio.get(mi, 0.35)
                floor_ratio = kr * 1.5
                floor = max(4, min(400, int(nf * min(floor_ratio, 0.95))))
                target = max(floor, proportional)
            target = min(target, nf)
            target = min(target, remaining_budget)
            target = max(4, target)
            budgets[mi] = target
            remaining_budget -= target
            remaining_faces -= nf

    # ── 4. Decimate each reducible group ───────────────
    decimated_groups_store = []

    for mi, nf in reducible_groups_data:
        tri_fi_list = mat_faces[mi]
        if len(tri_fi_list) < 4:
            continue

        target = budgets.get(mi, nf)
        sub_face_vi = tri_faces_all[tri_fi_list]
        used_v = np.unique(sub_face_vi.ravel())
        v_old2new = {ov: nv for nv, ov in enumerate(used_v)}
        sub_verts = orig_verts[used_v]
        sub_faces = np.array([[v_old2new[vi] for vi in f]
                               for f in sub_face_vi], dtype=np.int32)

        if target >= nf:
            decimated_groups_store.append({
                'material_index': mi, 'verts': sub_verts,
                'faces': sub_faces, 'tri_fi_list': tri_fi_list,
                'used_v': used_v, 'sub_verts': sub_verts,
            })
            mat_name = mesh.materials[mi].name if mi < num_mats and mesh.materials[mi] else "?"
            if verbose:
                print(_("decimate.keep_all", name=mat_name, before=nf, budget=target))
            continue

        mat_name = mesh.materials[mi].name if mi < num_mats and mesh.materials[mi] else "?"
        if verbose:
            ratio = (1 - target / max(1, nf)) * 100
            print(_("decimate.reduction_target", name=mat_name, v=len(sub_verts), f=nf, target=target, pct=ratio))

        m = o3d.geometry.TriangleMesh()
        m.vertices = o3d.utility.Vector3dVector(sub_verts)
        m.triangles = o3d.utility.Vector3iVector(sub_faces)
        m.remove_duplicated_vertices()
        m.remove_degenerate_triangles()
        if len(m.triangles) < 4:
            continue

        s = m.simplify_quadric_decimation(target)
        dec_v = np.asarray(s.vertices, dtype=np.float64)
        dec_f = np.asarray(s.triangles, dtype=np.int32)

        decimated_groups_store.append({
            'material_index': mi, 'verts': dec_v,
            'faces': dec_f, 'tri_fi_list': tri_fi_list,
            'used_v': used_v, 'sub_verts': sub_verts,
        })

        if verbose:
            print(_("decimate.result", v=len(dec_v), f=len(dec_f)))

    if not decimated_groups_store:
        return n_initial_v, n_initial_v, 0.0

    # ── 5. Build final mesh arrays ─────────────────────
    all_v = []; all_f = []; all_m = []; all_uv_loops = []
    vidx = {}

    def _add_v(x, y, z, tag):
        key = (round(x, 6), round(y, 6), round(z, 6), tag)
        if key in vidx:
            return vidx[key]
        idx = len(all_v)
        vidx[key] = idx
        all_v.append((x, y, z))
        return idx

    # Phase A: Locked faces
    for mi in sorted(mat_locked):
        if not mat_locked[mi]:
            continue
        for fi in mat_faces[mi]:
            fvs = tri_faces_all[fi]
            poly = mesh.polygons[tri_poly_all[fi]]
            face_idx = []
            for ci in range(3):
                vi = fvs[ci]; v = mesh.vertices[vi].co
                li = poly.loop_indices[ci]
                gvi = _add_v(v.x, v.y, v.z, "locked")
                face_idx.append(gvi)
                u, vv = (0.0, 0.0)
                if uv_arr is not None and li < len(uv_arr):
                    u, vv = uv_arr[li]
                all_uv_loops.append((u, vv))
            all_f.append(face_idx); all_m.append(mi)

    # Phase B: Decimated faces — face-centre UV matching
    if has_kdtree:
        for group in decimated_groups_store:
            mi = group['material_index']
            dv = group['verts']; df = group['faces']
            tri_fi_list = group['tri_fi_list']

            orig_fc_arr = np.array([
                (orig_verts[f[0]] + orig_verts[f[1]] + orig_verts[f[2]]) / 3.0
                for f in tri_faces_all[tri_fi_list]], dtype=np.float64)
            fc_tree = KDTree(orig_fc_arr)

            for face in df:
                vi0, vi1, vi2 = int(face[0]), int(face[1]), int(face[2])
                if vi0 >= len(dv) or vi1 >= len(dv) or vi2 >= len(dv):
                    continue
                idx0 = len(all_v)
                all_v.append((float(dv[vi0, 0]), float(dv[vi0, 1]), float(dv[vi0, 2])))
                all_v.append((float(dv[vi1, 0]), float(dv[vi1, 1]), float(dv[vi1, 2])))
                all_v.append((float(dv[vi2, 0]), float(dv[vi2, 1]), float(dv[vi2, 2])))
                all_f.append((idx0, idx0 + 1, idx0 + 2)); all_m.append(mi)
                dec_fc = (dv[vi0] + dv[vi1] + dv[vi2]) / 3.0
                _u, nn_fi = fc_tree.query(dec_fc)
                best_si = tri_fi_list[nn_fi]; lis = tri_loop_idx[best_si]
                for ci in range(3):
                    li = lis[ci]; u, vv = 0.0, 0.0
                    if uv_arr is not None and li < len(uv_arr):
                        u, vv = uv_arr[li]
                    all_uv_loops.append((u, vv))
    else:
        for group in decimated_groups_store:
            mi = group['material_index']
            dv = group['verts']; df = group['faces']
            tri_fi_list = group['tri_fi_list']
            for face in df:
                vi0, vi1, vi2 = int(face[0]), int(face[1]), int(face[2])
                if vi0 >= len(dv) or vi1 >= len(dv) or vi2 >= len(dv):
                    continue
                idx0 = len(all_v)
                all_v.append((float(dv[vi0, 0]), float(dv[vi0, 1]), float(dv[vi0, 2])))
                all_v.append((float(dv[vi1, 0]), float(dv[vi1, 1]), float(dv[vi1, 2])))
                all_v.append((float(dv[vi2, 0]), float(dv[vi2, 1]), float(dv[vi2, 2])))
                all_f.append((idx0, idx0 + 1, idx0 + 2)); all_m.append(mi)
                dec_fc = (dv[vi0] + dv[vi1] + dv[vi2]) / 3.0
                best_dist = float('inf'); best_si = tri_fi_list[0] if tri_fi_list else 0
                for si in tri_fi_list:
                    fvs = tri_faces_all[si]
                    orig_fc = (orig_verts[fvs[0]] + orig_verts[fvs[1]] + orig_verts[fvs[2]]) / 3.0
                    d = np.sum((dec_fc - orig_fc) ** 2)
                    if d < best_dist:
                        best_dist = d; best_si = si
                lis = tri_loop_idx[best_si]
                for ci in range(3):
                    li = lis[ci]; u, vv = 0.0, 0.0
                    if uv_arr is not None and li < len(uv_arr):
                        u, vv = uv_arr[li]
                    all_uv_loops.append((u, vv))

    if not all_f:
        return n_initial_v, n_initial_v, 0.0

    # ── 6. Emergency reduction ──────────────────────────
    if len(all_f) > max_faces:
        excess = len(all_f) - max_faces
        if verbose:
            print(_("decimate.still_over", excess=excess, max_faces=max_faces))
        dec_indices = [i for i, mi in enumerate(all_m)
                       if not mat_locked.get(mi, False)]
        if dec_indices:
            np.random.seed(42)
            to_drop = set(np.random.choice(
                dec_indices, size=min(excess, len(dec_indices)),
                replace=False))
            keep = [i for i in range(len(all_f)) if i not in to_drop]
            all_f = [all_f[i] for i in keep]
            all_m = [all_m[i] for i in keep]
            all_uv_loops = [all_uv_loops[i * 3 + ci]
                            for i in keep for ci in range(3)]

    # ── 7. Write to mesh ────────────────────────────────
    if verbose:
        print(_("decimate.writing", v=len(all_v), f=len(all_f)))

    mesh.clear_geometry()
    mesh.from_pydata(all_v, [], all_f)

    if not mesh.uv_layers:
        mesh.uv_layers.new(name=uv_layer_name)
    uv_layer = mesh.uv_layers.active
    uv_layer.name = uv_layer_name
    uv_flat = [c for (u, v) in all_uv_loops for c in (u, v)]
    uv_layer.data.foreach_set("uv", uv_flat)

    for pi, poly in enumerate(mesh.polygons):
        if pi < len(all_m):
            poly.material_index = all_m[pi]

    mesh.update(); mesh.validate()

    # ── 8. Restore vertex weights ───────────────────────
    new_verts_arr = np.array(all_v, dtype=np.float64)
    _restore_vertex_weights(mesh_obj, orig_verts, old_vg_weights,
                            old_vg_names, new_verts_arr)

    n_final_v = len(mesh.vertices); n_final_f = len(mesh.polygons)
    elapsed = time.time() - t0

    if verbose:
        red = (1 - n_final_v / n_initial_v) * 100
        print(_("decimate.done", init_v=n_initial_v, init_f=n_initial_f,
                final_v=n_final_v, final_f=n_final_f, pct=red, elapsed=elapsed))

    return n_initial_v, n_final_v, (1 - n_final_v / n_initial_v) * 100


# ═══════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════

def _find_mesh_and_arm():
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    arms = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE']
    if not meshes:
        return None, None
    m = max(meshes, key=lambda x: len(x.vertex_groups))
    a = None
    for mod in m.modifiers:
        if mod.type == 'ARMATURE' and mod.object:
            a = mod.object; break
    if a is None and arms:
        a = max(arms, key=lambda x: len(x.data.bones))
    return m, a


def decimate_to_target(mesh_obj, arm_obj=None, max_verts=65535,
                       max_faces=21845, verbose=True):
    return decimate(mesh_obj, arm_obj, max_faces, verbose)


def decimate_if_needed(mesh_obj, arm_obj=None, max_verts=65535,
                       max_faces=21845, verbose=True):
    n, f = len(mesh_obj.data.vertices), len(mesh_obj.data.polygons)
    if f <= max_faces:
        return n
    _u, fin, _u2 = decimate(mesh_obj, arm_obj, max_faces, verbose)
    return fin


def check_decimate_needed(mesh_obj, max_verts=65535, max_faces=21845):
    n, f = len(mesh_obj.data.vertices), len(mesh_obj.data.polygons)
    return n, f, max(0, f - max_faces), ""


def run(max_faces=21845):
    mesh_obj, _u = _find_mesh_and_arm()
    if mesh_obj:
        decimate(mesh_obj, None, max_faces)


if __name__ == "__main__":
    run()

