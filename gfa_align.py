"""
GFA Step2 增强对齐 — 适配到 Blender。
在现有 AlignAndBake 基础上增加 GFA 特有的头部/颈部精细调整。
"""
import bpy
import math
import numpy as np
from mathutils import Matrix, Vector
from .i18n import _


def _wpos(arm, name):
    return arm.matrix_world @ arm.pose.bones[name].head


def _mpos(arm, names):
    pts = [_wpos(arm, n) for n in names if n and n in arm.pose.bones]
    return Vector(np.mean(np.array(pts), axis=0)) if pts else Vector((0, 0, 0))


def _proj(p1, p2, plane):
    d = p2 - p1
    if plane == "XY":    a = math.degrees(math.atan2(d.y, d.x))
    elif plane == "YZ":  a = math.degrees(math.atan2(d.z, d.y))
    else:                a = math.degrees(math.atan2(d.z, d.x))
    return a + 360 if a < 0 else a


def _apply_rot_world(arm, bone_name, angle, axis):
    bone = arm.pose.bones[bone_name]
    wp = arm.matrix_world @ bone.head
    wm = arm.matrix_world @ bone.matrix
    t = Matrix.Translation(wp)
    ti = Matrix.Translation(-wp)
    r = Matrix.Rotation(math.radians(angle), 4, axis)
    nw = t @ r @ ti @ wm
    bone.matrix = arm.matrix_world.inverted() @ nw
    bpy.context.view_layer.update()


def _apply_rot_projected(arm, bone_name, angle, plane):
    rot_set = {"X", "Y", "Z"} - set(plane)
    if len(rot_set) != 1:
        raise ValueError("Plane must be XY, YZ, or XZ")
    axis = rot_set.pop()
    _apply_rot_world(arm, bone_name, -angle if axis == "Y" else angle, axis)


def _apply_trans_world(arm, bone_name, offset):
    bone = arm.pose.bones[bone_name]
    wm = arm.matrix_world @ bone.matrix
    nw = Matrix.Translation(offset) @ wm
    bone.matrix = arm.matrix_world.inverted() @ nw
    bpy.context.view_layer.update()


def _apply_scale_world(arm, bone_name, scale):
    bone = arm.pose.bones[bone_name]
    wm = arm.matrix_world @ bone.matrix
    trans = wm.translation.copy()
    sm = Matrix.Diagonal((*scale, 1.0))
    nw = Matrix.Translation(trans) @ sm @ Matrix.Translation(-trans) @ wm
    bone.matrix = arm.matrix_world.inverted() @ nw


def _apply_scale_local(arm, bone_name, ratio, main_axis, other_factor=0.5):
    bone = arm.pose.bones[bone_name]
    m = ratio
    o = ratio ** other_factor if ratio >= 0 else -((-ratio) ** other_factor)
    if main_axis == "X":
        bone.scale = Vector((bone.scale.x * m, bone.scale.y * o, bone.scale.z * o))
    elif main_axis == "Y":
        bone.scale = Vector((bone.scale.x * o, bone.scale.y * m, bone.scale.z * o))
    elif main_axis == "Z":
        bone.scale = Vector((bone.scale.x * o, bone.scale.y * o, bone.scale.z * m))
    bpy.context.view_layer.update()


def _align_bone_len(arm, scaling_pair, target_pair, main_axis, ratio=1.0, other_factor=0.5):
    sv = _wpos(arm, scaling_pair[1]) - _wpos(arm, scaling_pair[0])
    tv = _wpos(arm, target_pair[1]) - _wpos(arm, target_pair[0])
    lr = (tv.length / sv.length) * ratio if sv.length > 0 else 1.0
    _apply_scale_local(arm, scaling_pair[0], lr, main_axis, other_factor)


def _align_rot(arm, rotating_pair, target_pair, plane_seq):
    for pl in plane_seq:
        sa = _proj(_wpos(arm, rotating_pair[0]), _wpos(arm, rotating_pair[1]), pl)
        ta = _proj(_wpos(arm, target_pair[0]), _wpos(arm, target_pair[1]), pl)
        diff = ta - sa
        _apply_rot_projected(arm, rotating_pair[0], diff, pl)


def _vaxis(arm, sn, en):
    v = _mpos(arm, en) - _mpos(arm, sn)
    i = np.argmax(np.abs(v))
    d = "+" if v[i] >= 0 else "-"
    return (d, "XYZ"[i])


def _get_bbox(mesh_obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = mesh_obj.evaluated_get(depsgraph)
    bbox = [mesh_obj.matrix_world @ Vector(c) for c in eval_obj.bound_box]
    return {
        "X": {"Min": min(v.x for v in bbox), "Max": max(v.x for v in bbox)},
        "Y": {"Min": min(v.y for v in bbox), "Max": max(v.y for v in bbox)},
        "Z": {"Min": min(v.z for v in bbox), "Max": max(v.z for v in bbox)},
    }


# ═══════════════════════════════════════════════════════════════
#  主对齐函数 — 含 GFA 头部增强
# ═══════════════════════════════════════════════════════════════

def gfa_align(src_arm, tgt_arm, mesh_obj,
              src_jd=None, tgt_jd=None):
    """
    GFA Step2 完整对齐，重点增强头部/颈部精度。

    src_jd / tgt_jd: joint dicts (MMD_JOINT_DICT / GEM2_JOINT_DICT)
    若为 None 则使用 transfer.py 中的默认值。
    """
    import sys
    project = r"C:\Users\Administrator\Desktop\ai_study_project"
    if project not in sys.path:
        sys.path.insert(0, project)
    from gem2_mdl_tools.transfer import MMD_JOINT_DICT, GEM2_JOINT_DICT
    if src_jd is None:
        src_jd = MMD_JOINT_DICT
    if tgt_jd is None:
        tgt_jd = GEM2_JOINT_DICT

    # 验证必要 joint
    required = [
        "Root", "Thigh_L", "Thigh_R", "Knee_L", "Knee_R",
        "Foot_L", "Foot_R", "SpineLower", "SpineUpper", "Neck",
        "Clavicle_L", "Clavicle_R", "Shoulder_L", "Shoulder_R",
        "Elbow_L", "Elbow_R", "Wrist_L", "Wrist_R",
    ]
    missing = []
    for j in required:
        sn = src_jd.get(j, ""); tn = tgt_jd.get(j, "")
        if not sn or sn not in src_arm.pose.bones:
            missing.append("SRC:" + j)
        if not tn or tn not in tgt_arm.pose.bones:
            missing.append("TGT:" + j)
    if missing:
        raise ValueError(_("gfa.missing_joints", joints=", ".join(missing[:8])))

    bpy.context.view_layer.objects.active = src_arm
    bpy.ops.object.mode_set(mode='POSE')
    for bone in src_arm.pose.bones:
        for c in list(bone.constraints):
            bone.constraints.remove(c)

    # ── PASS 1: 轴对齐 ──
    shd, sha = _vaxis(src_arm,
                      [src_jd["Foot_L"], src_jd["Foot_R"]],
                      [src_jd["Shoulder_L"], src_jd["Shoulder_R"]])
    thd, tha = _vaxis(tgt_arm,
                      [tgt_jd["Foot_L"], tgt_jd["Foot_R"]],
                      [tgt_jd["Shoulder_L"], tgt_jd["Shoulder_R"]])
    if sha != tha:
        ra = ({"X", "Y", "Z"} - {sha, tha}).pop()
        _apply_rot_world(src_arm, src_jd["Root"], 90, ra)
        shd, sha = _vaxis(src_arm,
                          [src_jd["Foot_L"], src_jd["Foot_R"]],
                          [src_jd["Shoulder_L"], src_jd["Shoulder_R"]])
    if shd != thd:
        ra = ({"X", "Y", "Z"} - {sha, tha}).pop()
        _apply_rot_world(src_arm, src_jd["Root"], 180, ra)
    ssd, ssa = _vaxis(src_arm,
                      [src_jd["Shoulder_L"]], [src_jd["Shoulder_R"]])
    tsd, tsa = _vaxis(tgt_arm,
                      [tgt_jd["Shoulder_L"]], [tgt_jd["Shoulder_R"]])
    if ssa != tsa:
        _apply_rot_world(src_arm, src_jd["Root"], 90, sha)
    if ssd != tsd:
        _apply_rot_world(src_arm, src_jd["Root"], 180, sha)

    # ── PASS 2: 腿初步对齐 ──
    ls = ["YZ", "XZ"]
    _align_rot(src_arm, [src_jd["Thigh_L"], src_jd["Knee_L"]],
               [tgt_jd["Thigh_L"], tgt_jd["Knee_L"]], ls)
    _align_rot(src_arm, [src_jd["Thigh_R"], src_jd["Knee_R"]],
               [tgt_jd["Thigh_R"], tgt_jd["Knee_R"]], ls)
    _align_rot(src_arm, [src_jd["Knee_L"], src_jd["Foot_L"]],
               [tgt_jd["Knee_L"], tgt_jd["Foot_L"]], ls)
    _align_rot(src_arm, [src_jd["Knee_R"], src_jd["Foot_R"]],
               [tgt_jd["Knee_R"], tgt_jd["Foot_R"]], ls)

    # ── PASS 3: 整体缩放 (肩高 + BBox) ──
    tgt_sh_z = _mpos(tgt_arm, [tgt_jd["Shoulder_L"], tgt_jd["Shoulder_R"]]).z
    bbox_min = _get_bbox(mesh_obj)["Z"]["Min"]
    src_sh_z = _mpos(src_arm, [src_jd["Shoulder_L"], src_jd["Shoulder_R"]]).z
    if src_sh_z - bbox_min > 0.001:
        ovs = (tgt_sh_z - 0) / (src_sh_z - bbox_min)
        src_arm.pose.bones[src_jd["Root"]].scale *= ovs
        bpy.context.view_layer.update()

    # ── PASS 4: 身体方向对齐 ──
    src_bd = _proj(
        _mpos(src_arm, [src_jd["Foot_L"], src_jd["Foot_R"]]),
        _mpos(src_arm, [src_jd["Shoulder_L"], src_jd["Shoulder_R"],
                         src_jd["Neck"]]), "XZ")
    tgt_bd = _proj(
        _mpos(tgt_arm, [tgt_jd["Foot_L"], tgt_jd["Foot_R"]]),
        _mpos(tgt_arm, [tgt_jd["Shoulder_L"], tgt_jd["Shoulder_R"],
                         tgt_jd["Neck"]]), "XZ")
    bd = tgt_bd - src_bd
    _apply_rot_projected(src_arm, src_jd["Root"], bd, "XZ")
    _apply_rot_projected(src_arm, src_jd["Thigh_L"], -bd, "XZ")
    _apply_rot_projected(src_arm, src_jd["Thigh_R"], -bd, "XZ")
    _apply_trans_world(src_arm, src_jd["Root"],
        _mpos(tgt_arm, [tgt_jd["Thigh_L"], tgt_jd["Thigh_R"]]) -
        _mpos(src_arm, [src_jd["Thigh_L"], src_jd["Thigh_R"]]))

    # ── PASS 5: 肩部缩放 ──
    src_sh2 = _mpos(src_arm, [src_jd["Shoulder_L"], src_jd["Shoulder_R"]]).z
    src_th2 = _mpos(src_arm, [src_jd["Thigh_L"], src_jd["Thigh_R"]]).z
    tgt_sh2 = _mpos(tgt_arm, [tgt_jd["Shoulder_L"], tgt_jd["Shoulder_R"]]).z
    tgt_th2 = _mpos(tgt_arm, [tgt_jd["Thigh_L"], tgt_jd["Thigh_R"]]).z
    uh = (tgt_sh2 - tgt_th2) / (src_sh2 - src_th2) if abs(src_sh2 - src_th2) > 0.001 else 1.0
    ssw = (_mpos(src_arm, [src_jd["Shoulder_L"]]) - _mpos(src_arm, [src_jd["Shoulder_R"]])).length
    tsw = (_mpos(tgt_arm, [tgt_jd["Shoulder_L"]]) - _mpos(tgt_arm, [tgt_jd["Shoulder_R"]])).length
    uw = tsw / ssw if ssw > 0.001 else 1.0
    _apply_scale_world(src_arm, src_jd["Root"], Vector(((uw*uh)**0.5, uw, uh)))
    _apply_trans_world(src_arm, src_jd["Root"],
        _mpos(tgt_arm, [tgt_jd["Thigh_L"], tgt_jd["Thigh_R"]]) -
        _mpos(src_arm, [src_jd["Thigh_L"], src_jd["Thigh_R"]]))

    # ── PASS 6: ★ GFA 头部增强 — 脊柱方向 + 肩/颈偏移分布 ──
    su = _proj(_mpos(src_arm, [src_jd["SpineLower"]]),
               _mpos(src_arm, [src_jd["Shoulder_L"], src_jd["Shoulder_R"],
                                src_jd["Neck"]]), "XZ")
    tu = _proj(_mpos(tgt_arm, [tgt_jd["SpineLower"]]),
               _mpos(tgt_arm, [tgt_jd["Shoulder_L"], tgt_jd["Shoulder_R"],
                                tgt_jd["Neck"]]), "XZ")
    ud = (tu - su) * 0.67
    _apply_rot_projected(src_arm, src_jd["SpineLower"], ud, "XZ")
    _apply_rot_projected(src_arm, src_jd["Shoulder_L"], -ud, "XZ")
    _apply_rot_projected(src_arm, src_jd["Shoulder_R"], -ud, "XZ")
    _apply_rot_projected(src_arm, src_jd["Neck"], -ud, "XZ")

    # 肩/颈偏移分布 (GFA 风格)
    so = (_mpos(tgt_arm, [tgt_jd["Shoulder_L"], tgt_jd["Shoulder_R"],
                           tgt_jd["Neck"]]) -
          _mpos(src_arm, [src_jd["Shoulder_L"], src_jd["Shoulder_R"],
                           src_jd["Neck"]]))
    _apply_trans_world(src_arm, src_jd["SpineLower"], so * 0.1)
    _apply_trans_world(src_arm, src_jd["SpineUpper"], so * 0.4)
    _apply_trans_world(src_arm, src_jd["Clavicle_L"], so * 0.5)
    _apply_trans_world(src_arm, src_jd["Clavicle_R"], so * 0.5)
    _apply_trans_world(src_arm, src_jd["Neck"], so * 0.5)  # ★ 颈部关键

    ls_off = (_mpos(tgt_arm, [tgt_jd["Shoulder_L"]]) -
              _mpos(src_arm, [src_jd["Shoulder_L"]]))
    _apply_trans_world(src_arm, src_jd["Clavicle_L"], ls_off * 0.6)
    _apply_trans_world(src_arm, src_jd["Shoulder_L"], ls_off * 0.4)

    rs_off = (_mpos(tgt_arm, [tgt_jd["Shoulder_R"]]) -
              _mpos(src_arm, [src_jd["Shoulder_R"]]))
    _apply_trans_world(src_arm, src_jd["Clavicle_R"], rs_off * 0.6)
    _apply_trans_world(src_arm, src_jd["Shoulder_R"], rs_off * 0.4)

    # ★ 颈部单独偏移 (GFA 特有: Neck get its own offset)
    n_off = (_mpos(tgt_arm, [tgt_jd["Neck"]]) -
             _mpos(src_arm, [src_jd["Neck"]]))
    _apply_trans_world(src_arm, src_jd["Neck"], n_off * 0.4)

    # ── PASS 7: 腿部骨长 ──
    _align_bone_len(src_arm, [src_jd["Thigh_L"], src_jd["Knee_L"]],
                    [tgt_jd["Thigh_L"], tgt_jd["Knee_L"]], "Y", other_factor=1.0/3)
    _align_bone_len(src_arm, [src_jd["Thigh_R"], src_jd["Knee_R"]],
                    [tgt_jd["Thigh_R"], tgt_jd["Knee_R"]], "Y", other_factor=1.0/3)
    keh = _mpos(src_arm, [src_jd["Knee_L"], src_jd["Knee_R"]]).z
    kch = keh - _get_bbox(mesh_obj)["Z"]["Min"]
    if kch > 0.001:
        lsc = keh / kch
        _apply_scale_local(src_arm, src_jd["Knee_L"], lsc, "Y", other_factor=1.0/3)
        _apply_scale_local(src_arm, src_jd["Knee_R"], lsc, "Y", other_factor=1.0/3)
    _align_rot(src_arm, [src_jd["Thigh_L"], src_jd["Foot_L"]],
               [tgt_jd["Thigh_L"], tgt_jd["Foot_L"]], ls)
    _align_rot(src_arm, [src_jd["Thigh_R"], src_jd["Foot_R"]],
               [tgt_jd["Thigh_R"], tgt_jd["Foot_R"]], ls)

    # ── PASS 8: 手臂对齐 ──
    ars = ["XY", "YZ"]
    _align_bone_len(src_arm, [src_jd["Shoulder_L"], src_jd["Elbow_L"]],
                    [tgt_jd["Shoulder_L"], tgt_jd["Elbow_L"]], "Y")
    _align_bone_len(src_arm, [src_jd["Shoulder_R"], src_jd["Elbow_R"]],
                    [tgt_jd["Shoulder_R"], tgt_jd["Elbow_R"]], "Y")
    _align_rot(src_arm, [src_jd["Shoulder_L"], src_jd["Elbow_L"]],
               [tgt_jd["Shoulder_L"], tgt_jd["Elbow_L"]], ars)
    _align_rot(src_arm, [src_jd["Shoulder_R"], src_jd["Elbow_R"]],
               [tgt_jd["Shoulder_R"], tgt_jd["Elbow_R"]], ars)
    _align_bone_len(src_arm, [src_jd["Elbow_L"], src_jd["Wrist_L"]],
                    [tgt_jd["Elbow_L"], tgt_jd["Wrist_L"]], "Y")
    _align_bone_len(src_arm, [src_jd["Elbow_R"], src_jd["Wrist_R"]],
                    [tgt_jd["Elbow_R"], tgt_jd["Wrist_R"]], "Y")
    _align_rot(src_arm, [src_jd["Elbow_L"], src_jd["Wrist_L"]],
               [tgt_jd["Elbow_L"], tgt_jd["Wrist_L"]], ars)
    _align_rot(src_arm, [src_jd["Elbow_R"], src_jd["Wrist_R"]],
               [tgt_jd["Elbow_R"], tgt_jd["Wrist_R"]], ars)
    _align_rot(src_arm, [src_jd["Shoulder_L"], src_jd["Wrist_L"]],
               [tgt_jd["Shoulder_L"], tgt_jd["Wrist_L"]], ars)
    _align_rot(src_arm, [src_jd["Shoulder_R"], src_jd["Wrist_R"]],
               [tgt_jd["Shoulder_R"], tgt_jd["Wrist_R"]], ars)

    bpy.context.view_layer.update()
    print(_("gfa_align.done"))
