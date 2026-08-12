# -*- coding: utf-8 -*-
"""
Bone Diagnostics Tool — scan PMX armatures for mapping mismatches.

Usage: Run this from Blender Script Editor after importing a PMX model.
       Or import: from gem2_mdl_tools import bone_diagnostics

Checks:
  1. Which bones in the scene are mapped to GFA canonical names
  2. Which bones have no mapping (likely need manual attention)
  3. What GEM2 target bones will receive weights
  4. Gaps between the .ply reference skeleton and the mapping
"""
from .bone_mapping_v2 import (
    JP_TO_GFA, GFA_TO_GEM2_TARGET, PLY_SKELETON_24,
    resolve_pmx_bone, resolve_to_gem2_targets,
    diagnose_armature, print_diagnosis,
)
import bpy
from .i18n import _


def scan_scene():
    """Scan all armatures in the scene and print diagnostics."""
    arms = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE']
    if not arms:
        print(_("diag.no_armature"))
        return

    for arm_obj in arms:
        print(f"\n{'=' * 60}")
        print(f"Armature: {arm_obj.name} ({len(arm_obj.data.bones)} bones)")
        diag = diagnose_armature(arm_obj)
        print_diagnosis(diag)


def list_all_bone_names(arm_obj):
    """Simple list of all bone names in an armature, for manual inspection."""
    print(f"\nAll bones in '{arm_obj.name}':")
    for i, b in enumerate(arm_obj.data.bones):
        gfa = resolve_pmx_bone(b.name)
        tag = f" → {gfa}" if gfa else " (unmapped)"
        print(f"  [{i:3d}] {b.name}{tag}")


def check_vertex_group_consistency():
    """Check mesh vertex groups against armature bones."""
    mesh_obj = None
    arm_obj = None
    for o in bpy.context.scene.objects:
        if o.type == 'MESH' and mesh_obj is None:
            mesh_obj = o
        if o.type == 'ARMATURE' and arm_obj is None:
            arm_obj = o

    if not mesh_obj:
        print(_("diag.no_mesh"))
        return
    if not arm_obj:
        print(_("diag.no_armature_obj"))
        return

    bone_names = set(b.name for b in arm_obj.data.bones)
    vg_names = set(vg.name for vg in mesh_obj.vertex_groups)

    in_both = bone_names & vg_names
    vg_only = vg_names - bone_names
    bone_only = bone_names - vg_names

    print(f"\n{'=' * 60}")
    print("VERTEX GROUP / BONE CONSISTENCY CHECK")
    print(f"  Bones:        {len(bone_names)}")
    print(f"  Vertex groups:{len(vg_names)}")
    print(f"  In both:      {len(in_both)}")
    print(f"  VG only:      {len(vg_only)} (orphan vertex groups)")
    print(f"  Bone only:    {len(bone_only)} (bones with no weights)")

    if vg_only:
        print(f"\n  Orphan VGs (no matching bone):")
        for n in sorted(vg_only)[:15]:
            print(f"    - {n}")
        if len(vg_only) > 15:
            print(f"    ... and {len(vg_only) - 15} more")

    if bone_only:
        print(f"\n  Bones with no weights:")
        for n in sorted(bone_only)[:15]:
            print(f"    - {n}")
        if len(bone_only) > 15:
            print(f"    ... and {len(bone_only) - 15} more")


def check_gem2_targets():
    """Show which GEM2 target bones will receive weight from each PMX bone."""
    arms = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE']
    if not arms:
        print("No armatures")
        return

    arm_obj = arms[-1]  # largest by convention

    gem2_weights = {}  # gem2_name → total weight from all sources
    for b in arm_obj.data.bones:
        targets = resolve_to_gem2_targets(b.name)
        if targets:
            for gem2, w in targets:
                gem2_weights[gem2] = gem2_weights.get(gem2, 0) + w

    print(f"\n{'=' * 60}")
    print("GEM2 TARGET BONE WEIGHT DISTRIBUTION")
    print(f"  (from armature: {arm_obj.name})")

    for bn in PLY_SKELETON_24:
        total = gem2_weights.get(bn, 0)
        status = "OK" if total > 0 else "MISSING"
        print(f"  {bn:20s}  weight_sum={total:.2f}  [{status}]")

    # Show extra targets not in PLY skeleton
    extra = set(gem2_weights) - set(PLY_SKELETON_24)
    if extra:
        print(f"\n  Extra targets (not in .ply skeleton):")
        for bn in sorted(extra):
            print(f"    - {bn} (total weight: {gem2_weights[bn]:.2f})")


# ── Quick-run entry point ──────────────────────────────────────
if __name__ == "__main__":
    scan_scene()
    check_gem2_targets()
    check_vertex_group_consistency()
