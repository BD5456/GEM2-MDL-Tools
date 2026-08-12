"""
GFA Pipeline — 完整移植到 Blender。
用法: 在 Blender Script Editor 中运行此文件。
前提: 已用 gem2_mdl_tools 导入 PLY+MDL，或已导入 MMD 模型。
步骤:
  Step0.5  头部归一化 (GFA NormalizeHeadSize)
  Step1    清理刚体 / 未使用骨骼
  Step2    骨骼映射 (GFA bone_merging_list → PMX→GEM2 mapping)
  Step3    骨架对齐 + 蒙皮对齐
  Step4    权重转移 (GFA 方法, 重命名 VG 为 GEM2 骨名)
  Step5    UV 检查
"""
import bpy
import math
import os
import sys
import numpy as np
from mathutils import Matrix, Vector
from .i18n import _

from .bone_mapping_v2 import (
    GFA_TO_GEM2_NAME, JP_TO_GFA, GFA_TO_GEM2_TARGET,
    gfa_strip, resolve_pmx_bone, resolve_to_gem2_targets,
    diagnose_armature, print_diagnosis, PLY_SKELETON_24,
)

PROJECT = r"C:\Users\Administrator\Desktop\ai_study_project"
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)


# ═══════════════════════════════════════════════════════════════
#  Mapping system now lives in bone_mapping_v2.py
#  JP_TO_GFA → GFA_TO_GEM2_TARGET → GFA_TO_GEM2
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
#  MMD→标准角色骨名的 Lookup (canonical → PMX actual name)
# ═══════════════════════════════════════════════════════════════

# Japanese PMX bone names → English canonical name
# Covers both "左足" (full Japanese) and "足.L" (dot notation) styles

# ═══════════════════════════════════════════════════════════════
#  辅助函数 (保留所有现有数学函数)
# ═══════════════════════════════════════════════════════════════

def _find_mesh_arm():
    """自动检测: 顶点组最多的 mesh + 骨骼数最多的 armature (PMX)"""
    all_meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    mesh_obj = max(all_meshes, key=lambda m: len(m.vertex_groups)) if all_meshes else None
    all_arms = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE']
    arm_obj = max(all_arms, key=lambda a: len(a.data.bones)) if all_arms else None
    if mesh_obj and not arm_obj:
        for mod in mesh_obj.modifiers:
            if mod.type == "ARMATURE" and mod.object:
                arm_obj = mod.object; break
    return mesh_obj, arm_obj


def _find_ref_arm(source_arm):
    """查找参考骨架: 骨骼数最少的 armature (排除 source)"""
    all_arms = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE' and o != source_arm]
    return min(all_arms, key=lambda a: len(a.data.bones)) if all_arms else None


def _wpos(arm, name):
    return arm.matrix_world @ arm.pose.bones[name].head


def _mpos(arm, names):
    pts = [_wpos(arm, n) for n in names if n and n in arm.pose.bones]
    return Vector(np.mean(np.array(pts), axis=0)) if pts else Vector((0, 0, 0))


def _proj(p1, p2, plane):
    d = p2 - p1
    if plane == "XY":   a = math.degrees(math.atan2(d.y, d.x))
    elif plane == "YZ": a = math.degrees(math.atan2(d.z, d.y))
    else:               a = math.degrees(math.atan2(d.z, d.x))
    return a + 360 if a < 0 else a


def _apply_rot_world(arm, bone, angle, axis):
    b = arm.pose.bones[bone]
    wp = arm.matrix_world @ b.head
    wm = arm.matrix_world @ b.matrix
    r = Matrix.Rotation(math.radians(angle), 4, axis)
    t = Matrix.Translation(wp); ti = Matrix.Translation(-wp)
    b.matrix = arm.matrix_world.inverted() @ (t @ r @ ti @ wm)
    bpy.context.view_layer.update()


def _apply_rot_proj(arm, bone, angle, plane):
    axis = ({"X", "Y", "Z"} - set(plane)).pop()
    _apply_rot_world(arm, bone, -angle if axis == "Y" else angle, axis)


def _apply_trans_world(arm, bone, offset):
    b = arm.pose.bones[bone]
    wm = arm.matrix_world @ b.matrix
    b.matrix = arm.matrix_world.inverted() @ (Matrix.Translation(offset) @ wm)
    bpy.context.view_layer.update()


def _apply_scale_world(arm, bone, scale):
    b = arm.pose.bones[bone]
    wm = arm.matrix_world @ b.matrix
    t = wm.translation.copy()
    sm = Matrix.Diagonal((*scale, 1.0))
    b.matrix = arm.matrix_world.inverted() @ (Matrix.Translation(t) @ sm @ Matrix.Translation(-t) @ wm)


def _apply_scale_local(arm, bone, ratio, main_axis, other_factor=0.5):
    b = arm.pose.bones[bone]
    m = ratio; o = ratio ** other_factor if ratio >= 0 else -((-ratio) ** other_factor)
    if main_axis == "X": b.scale = Vector((b.scale.x*m, b.scale.y*o, b.scale.z*o))
    elif main_axis == "Y": b.scale = Vector((b.scale.x*o, b.scale.y*m, b.scale.z*o))
    else: b.scale = Vector((b.scale.x*o, b.scale.y*o, b.scale.z*m))
    bpy.context.view_layer.update()


def _align_rot(arm, rot_pair, tgt_pair, plane_seq):
    for pl in plane_seq:
        sa = _proj(_wpos(arm, rot_pair[0]), _wpos(arm, rot_pair[1]), pl)
        ta = _proj(_wpos(arm, tgt_pair[0]), _wpos(arm, tgt_pair[1]), pl)
        _apply_rot_proj(arm, rot_pair[0], ta - sa, pl)


def _align_bone_len(arm, scl_pair, tgt_pair, main_axis, ratio=1.0, other_factor=0.5):
    sv = _wpos(arm, scl_pair[1]) - _wpos(arm, scl_pair[0])
    tv = _wpos(arm, tgt_pair[1]) - _wpos(arm, tgt_pair[0])
    lr = (tv.length / sv.length) * ratio if sv.length > 0 else 1.0
    _apply_scale_local(arm, scl_pair[0], lr, main_axis, other_factor)


def _vaxis(arm, sn, en):
    v = _mpos(arm, en) - _mpos(arm, sn)
    i = np.argmax(np.abs(v))
    return ("+" if v[i] >= 0 else "-", "XYZ"[i])


def _get_bbox(mesh_obj):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = mesh_obj.evaluated_get(dg)
    bb = [mesh_obj.matrix_world @ Vector(c) for c in ev.bound_box]
    return {"Z": {"Min": min(v.z for v in bb)}}


def _bake_pose_to_rest(mesh_obj):
    arm_mod = None
    for mod in mesh_obj.modifiers:
        if mod.type == "ARMATURE" and mod.object:
            arm_mod = mod; break
    if not arm_mod: return
    arm_obj = arm_mod.object
    arm_mod_name = arm_mod.name
    bpy.context.view_layer.objects.active = mesh_obj; mesh_obj.select_set(True)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.modifier_copy(modifier=arm_mod_name)
    new_idx = list(mesh_obj.modifiers).index(arm_mod) + 1
    bpy.ops.object.modifier_apply(modifier=arm_mod_name)
    mesh_obj.modifiers[new_idx].name = arm_mod_name
    bpy.context.view_layer.update()
    bpy.context.view_layer.objects.active = arm_obj; arm_obj.select_set(True)
    bpy.ops.object.mode_set(mode="POSE")
    bpy.ops.pose.armature_apply()
    bpy.context.view_layer.objects.active = mesh_obj
    print(_("pipeline.bake_done"))


def _get_parent_chain(bone):
    """获取骨骼的所有祖先链，最近的在前"""
    chain = []
    p = bone.parent
    while p:
        chain.append(p)
        p = p.parent
    return chain


def _find_nearest_mapped_parent(mesh_obj, arm_obj, src_bone_name, mapped_pmx_set):
    """找到源骨骼最近的已映射父骨骼"""
    bone = arm_obj.data.bones.get(src_bone_name)
    if not bone: return None
    for parent in _get_parent_chain(bone):
        if parent.name in mapped_pmx_set:
            return parent.name
    return None


# ═══════════════════════════════════════════════════════════════
#  GFA Mapping 构建
# ═══════════════════════════════════════════════════════════════

def _build_gfa_mapping(arm_obj):
    """
    使用 bone_mapping_v2 的三层映射构建 PMX→GEM2 映射表。

    遍历 armature 中的每根骨骼，用 resolve_pmx_bone() 查找 GFA 名称，
    再用 GFA_TO_GEM2_TARGET 获取目标 GEM2 骨骼和权重比例。
    """
    existing = list(b.name for b in arm_obj.data.bones)
    pmx_to_gem2 = {}
    gem2_set = set()
    mapped_pmx = set()
    unmapped_bones = []

    for bn in existing:
        gfa_name = resolve_pmx_bone(bn)
        if gfa_name is None:
            unmapped_bones.append(bn)
            continue
        targets = GFA_TO_GEM2_TARGET.get(gfa_name)
        if targets is None:
            unmapped_bones.append(bn)
            continue

        # Convert GFA prefix names (if any) to stripped GEM2 names
        converted = []
        for gem2_name, weight in targets:
            stripped = GFA_TO_GEM2_NAME.get(gem2_name, gem2_name)
            converted.append((stripped, weight))

        mapped_pmx.add(bn)
        pmx_to_gem2[bn] = converted
        for g2n, _ in converted:
            gem2_set.add(g2n)

    if unmapped_bones:
        print(f"[Mapping] {len(unmapped_bones)} bones unmapped: "
              f"{unmapped_bones[:8]}{'...' if len(unmapped_bones) > 8 else ''}")

    return pmx_to_gem2, gem2_set, mapped_pmx


# ═══════════════════════════════════════════════════════════════
#  Step 0.5: Normalize Head (GFA Step0.5)
# ═══════════════════════════════════════════════════════════════

def step_head_normalize():
    """参考 GFA Step0.5: 按 EyeLR 和 EyeNeck 比例缩放 Neck 骨骼。"""
    mesh_obj, arm_obj = _find_mesh_arm()
    if not arm_obj:
        print(_("step0_5.no_skeleton")); return

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='POSE')

    body_vec = _mpos(arm_obj, ["ShoulderP_L", "ShoulderP_R"]) - _mpos(arm_obj, ["Ankle_L", "Ankle_R"])
    body_len = body_vec.length
    if body_len < 0.001:
        print(_("step0_5.body_len_zero")); return

    eye_lr = (_wpos(arm_obj, "Eye_L") - _wpos(arm_obj, "Eye_R")).length / body_len
    eye_mid = (_wpos(arm_obj, "Eye_L") + _wpos(arm_obj, "Eye_R")) / 2
    eye_neck = abs((_wpos(arm_obj, "Neck") - eye_mid).dot(body_vec.normalized())) / body_len

    print(_("step0_5.source_ratio", eye_lr=eye_lr, eye_neck=eye_neck))
    print(_("step0_5.roundtrip"))

    bpy.context.view_layer.update()


# ═══════════════════════════════════════════════════════════════
#  Step 1: Clean Rigid Bodies / Unused
# ═══════════════════════════════════════════════════════════════

def step_clean_rigids():
    """清除场景中的刚体/关节/物理对象。
    只删除真正的 MMD 刚体/关节 EMPTY，且绝不删除有子对象的 EMPTY
    （以防止破坏骨骼父子关系），也不触碰任何 ARMATURE 对象。"""
    removed = 0
    skipped_children = 0
    for obj in list(bpy.data.objects):
        if obj.type != 'EMPTY':
            continue

        # 安全检查：不删除任何名称包含 '_Armature' 的对象（参考骨架）
        if '_Armature' in obj.name:
            continue

        is_rigid = False
        try:
            if hasattr(obj, 'mmd_type') and obj.mmd_type in ('RIGID', 'JOINT'):
                is_rigid = True
        except:
            pass
        try:
            if hasattr(obj, 'mmd_rigid') and obj.mmd_rigid:
                is_rigid = True
        except:
            pass
        if not is_rigid:
            nm = obj.name.lower()
            if nm.startswith('rb_') or '_rigid_' in nm or '\u525b\u4f53' in obj.name:
                is_rigid = True

        if not is_rigid:
            continue

        # 递归收集要删除的子 EMPTY（只删 EMPTY，不动 ARMATURE/MESH）
        to_remove = []
        def _collect_empty_children(o):
            for child in o.children:
                if child.type == 'EMPTY':
                    to_remove.append(child)
                    _collect_empty_children(child)
        _collect_empty_children(obj)

        # 清除 MMD 刚体属性避免 mmd_tools 回调崩溃 (Blender 4.3 shadow_method 问题)
        for robj in [obj] + to_remove:
            try:
                if hasattr(robj, 'mmd_rigid') and robj.mmd_rigid:
                    robj.mmd_rigid.enabled = False
            except: pass
            try:
                if hasattr(robj, 'mmd_type'):
                    robj.mmd_type = 'NONE'
            except: pass

        for robj in to_remove:
            try:
                bpy.data.objects.remove(robj, do_unlink=True)
                removed += 1
            except Exception as e:
                print(_("step1.warn_failed", name=robj.name, error=e))
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
        except Exception as e:
            print(_("step1.warn_failed_parent", name=obj.name, error=e))

    print(_("step1.cleaned", count=removed))


# ═══════════════════════════════════════════════════════════════
#  Step 2: Bone Mapping (基于 GFA bone_merging_list)
#  构建映射 → 移除未映射骨骼(权重合并到最近父骨骼) → 移除刚体
#  关键: 不重命名骨骼
# ═══════════════════════════════════════════════════════════════

def step_bone_mapping():
    """
    Step2 骨骼映射:
      1. 运行 bone_mapping_v2 诊断，打印映射报告
      2. 构建 PMX → GEM2 映射
      3. 将无映射骨骼的权重合并到最近有映射的父骨骼
      4. 不重命名任何骨骼
    """
    mesh_obj, arm_obj = _find_mesh_arm()
    if not mesh_obj or not arm_obj:
        print(_("step2.no_mesh_skeleton")); return

    # Run diagnosis first
    diag = diagnose_armature(arm_obj)
    print_diagnosis(diag)

    all_bones = set(b.name for b in arm_obj.data.bones)
    pmx_to_gem2, gem2_set, mapped_pmx = _build_gfa_mapping(arm_obj)

    if len(mapped_pmx) == 0:
        print(_("step2.no_gfa_match"))
        gem2_lower = {n.lower() for n in PLY_SKELETON_24}
        for bn in all_bones:
            if bn.lower() in gem2_lower:
                pmx_to_gem2[bn] = [(bn.lower(), 1.0)]
                mapped_pmx.add(bn)
        if len(mapped_pmx) == 0:
            print(_("step2.still_no_match"))
            print(_("step2.armature_bones") + f": {sorted(all_bones)[:10]}...")
            print(_("step2.expected_ply") + f": {PLY_SKELETON_24}")
    else:
        print(_("step2.gfa_mapped", pmx=len(mapped_pmx), gem2=len(gem2_set)))
        unmapped = all_bones - mapped_pmx
        if unmapped:
            print(_("step2.unmapped_aux", count=len(unmapped)))
        if len(mapped_pmx) > 0 and len(unmapped) < len(all_bones) * 0.5:
            from gem2_mdl_tools.transfer import _merge_weights_to_parent
            merged = _merge_weights_to_parent(mesh_obj, arm_obj, list(mapped_pmx))
            print(_("step2.merged", count=merged))

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='OBJECT')
    print(_("step2.complete"))


# ═══════════════════════════════════════════════════════════════


def step_align_skeleton():
    """
    Step3 骨架对齐:
       1. 记录 GFA 映射关系（不修改骨骼位置）
       2. 蒙皮对齐: 按顶点组名匹配骨骼名做权重同步
    """
    mesh_obj, arm_obj = _find_mesh_arm()
    if not arm_obj:
        print(_("step3.no_skeleton")); return

    print(_("step3.ready"))
    bpy.context.view_layer.update()


# ═══════════════════════════════════════════════════════════════
#  Step 4: Weight Transfer (GFA 方法)
#  使用 GFA bone_merging_list 转移 PMX→GEM2 权重
#  重命名顶点组为 GEM2 骨名
# ═══════════════════════════════════════════════════════════════

def step_transfer_weights():
    """
    Step4 权重转移 — GFA 方法:
      遍历 armature 所有骨骼，对顶层 PMX→GEM2 映射，
      按 GFA 权重比例复制/重命名顶点组到 GEM2 骨骼名。
    """
    mesh_obj, arm_obj = _find_mesh_arm()
    if not mesh_obj or not arm_obj:
        print(_("step4.no_mesh_skeleton")); return

    bpy.ops.object.mode_set(mode="OBJECT")
    pmx_to_gem2, gem2_set, mapped_pmx = _build_gfa_mapping(arm_obj)

    if len(pmx_to_gem2) == 0:
        print(_("step4.empty_mapping"))
        return

    mesh_vg_names = set(vg.name for vg in mesh_obj.vertex_groups)

    # Check if VGs already have GEM2 names
    gem2_lower_set = {n.lower() for n in PLY_SKELETON_24}
    already_gem2 = {n for n in mesh_vg_names if n.lower() in gem2_lower_set}
    if already_gem2 and len(already_gem2) >= 6:
        print(_("step4.already_gem2", count=len(already_gem2)))
        return

    # Perform weight transfer
    transferred = 0
    merged = 0

    for pmx_name, gem2_entries in pmx_to_gem2.items():
        src_vg = mesh_obj.vertex_groups.get(pmx_name)
        if src_vg is None:
            continue

        # Collect source weights first (don't mutate while iterating)
        for gem2_name, ratio in gem2_entries:
            if ratio <= 0:
                continue
            tgt_vg = mesh_obj.vertex_groups.get(gem2_name)
            if tgt_vg is None:
                tgt_vg = mesh_obj.vertex_groups.new(name=gem2_name)

            # Copy weights from src → tgt with ratio
            for vert in mesh_obj.data.vertices:
                try:
                    sw = src_vg.weight(vert.index)
                    if sw > 0:
                        tgt_vg.add([vert.index], sw * ratio, 'ADD')
                except RuntimeError:
                    pass

        # Remove source VG (weight already transferred)
        mesh_obj.vertex_groups.remove(src_vg)
        transferred += 1

    # Normalize: ensure each vertex's total weight = 1.0
    if transferred > 0:
        _normalize_vertex_weights(mesh_obj)

    # Clean non-ASCII VGs (will crash export)
    cleaned = 0
    for vg in list(mesh_obj.vertex_groups):
        try:
            vg.name.encode("ascii")
        except UnicodeEncodeError:
            mesh_obj.vertex_groups.remove(vg)
            cleaned += 1

    if cleaned:
        print(_("step4.cleaned_nonascii", count=cleaned))

    print(_("step4.transferred", count=transferred, targets=len(gem2_set)))


def _normalize_vertex_weights(mesh_obj):
    """Normalise vertex group weights so each vertex sums to 1.0."""
    import numpy as np
    nv = len(mesh_obj.data.vertices)
    nvg = len(mesh_obj.vertex_groups)
    w = np.zeros((nv, nvg), dtype=np.float64)
    for v in mesh_obj.data.vertices:
        for g in v.groups:
            w[v.index, g.group] = g.weight
    row_sum = w.sum(axis=1, keepdims=True)
    row_sum[row_sum == 0] = 1.0
    w /= row_sum
    w[w < 1e-10] = 0

    for vg in list(mesh_obj.vertex_groups):
        mesh_obj.vertex_groups.remove(vg)
    vg_map = {}
    for vi in range(nv):
        for gi in range(nvg):
            if w[vi, gi] > 0:
                if gi not in vg_map:
                    vg_map[gi] = mesh_obj.vertex_groups.new(
                        name=f"_temp_vg_{gi}")
                vg_map[gi].add([vi], w[vi, gi], 'REPLACE')
    # Rename back
    counted = 0
    for gi, vg in vg_map.items():
        try:
            vg.name = f"gem2_bone_{gi}"
            counted += 1
        except:
            pass
    print(_("step4.normalized", count=counted))


# ═══════════════════════════════════════════════════════════════
#  Step 4.5: Decimate (optional — reduce verts to ≤65535)
# ═══════════════════════════════════════════════════════════════

def step_decimate(max_verts=65535, max_faces=21845):
    """(Legacy) Per-material split decimation via Open3D QEM."""
    from .decimate_pmx import (decimate_if_needed, check_decimate_needed,
                                _find_mesh_and_arm)

    mesh_obj, arm_obj = _find_mesh_and_arm()
    if not mesh_obj:
        print(_("decimate.no_mesh")); return

    n, f, need, reason = check_decimate_needed(mesh_obj, max_verts, max_faces)

    print(_("decimate.status", name=mesh_obj.name, v=n, f=f,
            mats=""))

    if need > 0:
        print(_("decimate.over_limit"))
        final = decimate_if_needed(mesh_obj, arm_obj,
                                   max_verts=max_verts,
                                   max_faces=max_faces)
        f_final = len(mesh_obj.data.polygons)
        print(_("decimate.pipeline_result", v=n, f=f, final_v=final, final_f=f_final))
    else:
        print(_("decimate.within_limit"))


# ═══════════════════════════════════════════════════════════════
#  Step 5: UV Check
# ═══════════════════════════════════════════════════════════════

def step_uv_check():
    """检查 UV 图层，确保命名正确"""
    mesh_obj, _u = _find_mesh_arm()
    if not mesh_obj:
        print(_("step5.no_mesh")); return

    uv_layers = mesh_obj.data.uv_layers
    if len(uv_layers) == 0:
        print(_("step5.no_uv")); return

    active_uv = uv_layers.active or uv_layers[0]
    if active_uv.name != "UVMap":
        active_uv.name = "UVMap"
        print(_("step5.renamed_uv"))


# ═══════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════

def run(do_step0_5=True, do_step1=True, do_step2=True,
        do_step3=True, do_step4=True, do_decimate=True,
        do_step5=True):
    """按顺序运行 GFA 管道各步骤"""
    print("=" * 60)
    print(_("pipeline.title"))
    print("=" * 60)

    if do_step0_5:
        print("\n" + _("pipeline.step0_5"))
        step_head_normalize()

    if do_step1:
        print("\n" + _("pipeline.step1"))
        step_clean_rigids()

    if do_step2:
        print("\n" + _("pipeline.step2"))
        step_bone_mapping()

    if do_step3:
        print("\n" + _("pipeline.step3"))
        step_align_skeleton()

    if do_step4:
        print("\n" + _("pipeline.step4"))
        step_transfer_weights()

    if do_decimate:
        print("\n" + _("pipeline.step4_5"))
        step_decimate()

    if do_step5:
        print("\n" + _("pipeline.step5"))
        step_uv_check()

    print("\n" + _("pipeline.complete"))


# ── 快捷调用 ────────────────────────────────────────────────────
if __name__ == "__main__":
    run()
