"""
GFA Step0.5 NormalizeHeadSize — 适配到 Blender。
从目标参考骨架计算头部比例，并按参考比例缩放源骨架的 Neck 骨骼。
"""
import bpy
import math
from mathutils import Vector
from .i18n import _


def _wpos(arm_obj, bone_name):
    """骨骼世界坐标"""
    return arm_obj.matrix_world @ arm_obj.pose.bones[bone_name].head


def _mean_pos(arm_obj, names):
    pts = [_wpos(arm_obj, n) for n in names if n and n in arm_obj.pose.bones]
    if not pts:
        return Vector((0, 0, 0))
    return sum(pts, Vector((0, 0, 0))) / len(pts)


def compute_head_ratios(arm_obj, eye_l="Eye_L", eye_r="Eye_R",
                         neck="Neck", shoulder_l="ShoulderP_L",
                         shoulder_r="ShoulderP_R", ankle_l="Ankle_L",
                         ankle_r="Ankle_R"):
    """
    从骨架计算 EyeLateralRatio 和 EyeNeckRatio。
    返回: (eye_lr_ratio, eye_neck_ratio)
    """
    eye_l_pos = _wpos(arm_obj, eye_l)
    eye_r_pos = _wpos(arm_obj, eye_r)
    neck_pos = _wpos(arm_obj, neck)
    shoulder_mid = _mean_pos(arm_obj, [shoulder_l, shoulder_r])
    ankle_mid = _mean_pos(arm_obj, [ankle_l, ankle_r])

    body_vec = shoulder_mid - ankle_mid
    body_len = body_vec.length
    if body_len < 0.001:
        return 0, 0

    eye_lr = (eye_l_pos - eye_r_pos).length
    eye_lr_ratio = eye_lr / body_len

    eye_mid = (eye_l_pos + eye_r_pos) / 2.0
    eye_to_neck = neck_pos - eye_mid
    proj_len = abs(eye_to_neck.dot(body_vec.normalized()))
    eye_neck_ratio = proj_len / body_len

    return eye_lr_ratio, eye_neck_ratio


def normalize_head_size(src_arm_obj, ref_arm_obj,
                        neck_src="Neck",
                        eye_l_ref="Eye_L", eye_r_ref="Eye_R",
                        neck_ref="Neck",
                        shoulder_l_ref="ShoulderP_L", shoulder_r_ref="ShoulderP_R",
                        ankle_l_ref="Ankle_L", ankle_r_ref="Ankle_R"):
    """
    按参考骨架的头部比例缩放源骨架的 Neck，从而匹配头大小。

    参考 GFA Step0.5:
      RatioOffset = (RatioOffsetByEyeLR * RatioOffsetByEyeNeck) ** 0.5
    若比例 > 1 → 放大 Neck；< 1 → 缩小 Neck。

    返回实际应用的缩放因子。
    """
    # 确保在 POSE 模式
    bpy.context.view_layer.objects.active = src_arm_obj
    bpy.ops.object.mode_set(mode='POSE')

    ref_lr, ref_neck = compute_head_ratios(
        ref_arm_obj, eye_l_ref, eye_r_ref, neck_ref,
        shoulder_l_ref, shoulder_r_ref, ankle_l_ref, ankle_r_ref)

    src_lr, src_neck = compute_head_ratios(
        src_arm_obj, eye_l_ref, eye_r_ref, neck_src,
        shoulder_l_ref, shoulder_r_ref, ankle_l_ref, ankle_r_ref)

    if ref_lr == 0 or ref_neck == 0 or src_lr == 0 or src_neck == 0:
        print(_("head_normalize.skip_zero"))
        return 1.0

    offset_lr = ref_lr / src_lr
    offset_neck = ref_neck / src_neck
    ratio_offset = math.sqrt(offset_lr * offset_neck)

    # 缩放 Neck 骨骼 (pose bone scale)
    neck_bone = src_arm_obj.pose.bones.get(neck_src)
    if neck_bone:
        neck_bone.scale = neck_bone.scale * ratio_offset
        print(_("head_normalize.scaled", ratio=ratio_offset,
                src_lr=src_lr, ref_lr=ref_lr,
                src_neck=src_neck, ref_neck=ref_neck))
    else:
        print(_("head_normalize.bone_missing", name=neck_src))

    bpy.context.view_layer.update()
    return ratio_offset
