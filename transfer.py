"""
PMX-to-GEM2 conversion pipeline.
包含：骨骼检测、清理、对齐、权重转移、UV检查、导出。
继承自 pmx_to_gem2.py + fix_skinning.py 中的逻辑。
"""
import bpy
import math
import os
from bpy.props import (
    StringProperty, IntProperty, FloatProperty, BoolProperty,
    CollectionProperty, PointerProperty,
)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from mathutils import Matrix, Vector
from .i18n import _
_np = None
def _get_np():
    global _np
    if _np is None:
        import numpy
        _np = numpy
    return _np

# ── Bone mapping data ──────────────────────────────────────────

_BONE_MAP_FALLBACK = [
    ("ParentNode", "basis", 1.0), ("Center", "body", 1.0),
    ("UpperBody", "ik_leftright", 1.0), ("UpperBody2", "ik_updown", 1.0),
    ("Neck", "head", 1.0), ("Head", "head", 1.0),
    ("Leg_L", "foot1l", 1.0), ("Knee_L", "foot2l", 1.0), ("Ankle_L", "foot3l", 1.0),
    ("Leg_R", "foot1r", 1.0), ("Knee_R", "foot2r", 1.0), ("Ankle_R", "foot3r", 1.0),
    ("ShoulderP_L", "clavicle_left", 1.0), ("Shoulder_L", "clavicle_left", 1.0),
    ("Arm_L", "hand1l", 1.0), ("Elbow_L", "hand2l", 1.0), ("Wrist_L", "hand_rot1l", 1.0),
    ("ShoulderP_R", "clavicle_right", 1.0), ("Shoulder_R", "clavicle_right", 1.0),
    ("Arm_R", "hand1r", 1.0), ("Elbow_R", "hand2r", 1.0), ("Wrist_R", "hand_rot1r", 1.0),
    ("Thumb0_L", "palm1l", 1.0), ("Thumb1_L", "palm1l", 1.0), ("Thumb2_L", "palm2l", 1.0),
    ("IndexFinger1_L", "palm1l", 0.5), ("IndexFinger2_L", "palm2l", 1.0), ("IndexFinger3_L", "palm2l", 1.0),
    ("MiddleFinger1_L", "palm1l", 0.5), ("MiddleFinger2_L", "palm2l", 1.0), ("MiddleFinger3_L", "palm2l", 1.0),
    ("RingFinger1_L", "palm1l", 0.5), ("RingFinger2_L", "palm2l", 1.0), ("RingFinger3_L", "palm2l", 1.0),
    ("LittleFinger1_L", "palm1l", 0.5), ("LittleFinger2_L", "palm2l", 1.0), ("LittleFinger3_L", "palm2l", 1.0),
    ("Thumb0_R", "palm1r", 1.0), ("Thumb1_R", "palm1r", 1.0), ("Thumb2_R", "palm2r", 1.0),
    ("IndexFinger1_R", "palm1r", 0.5), ("IndexFinger2_R", "palm2r", 1.0), ("IndexFinger3_R", "palm2r", 1.0),
    ("MiddleFinger1_R", "palm1r", 0.5), ("MiddleFinger2_R", "palm2r", 1.0), ("MiddleFinger3_R", "palm2r", 1.0),
    ("RingFinger1_R", "palm1r", 0.5), ("RingFinger2_R", "palm2r", 1.0), ("RingFinger3_R", "palm2r", 1.0),
    ("LittleFinger1_R", "palm1r", 0.5), ("LittleFinger2_R", "palm2r", 1.0), ("LittleFinger3_R", "palm2r", 1.0),
    ("Waist", "body", 1.0), ("LowerBody", "body", 1.0),
    ("Eye_L", "head", 1.0), ("Eye_R", "head", 1.0),
    ("ArmTwist_L", "hand1l", 1.0), ("ArmTwist_R", "hand1r", 1.0),
    ("HandTwist_L", "hand2l", 1.0), ("HandTwist_R", "hand2r", 1.0),
    ("AnkleTip_L", "foot3l", 1.0), ("AnkleTip_R", "foot3r", 1.0),
    ("LegTipEX_L", "foot3l", 1.0), ("LegTipEX_R", "foot3r", 1.0),
    ("WaistCancel_L", "foot1l", 1.0), ("WaistCancel_R", "foot1r", 1.0),
    ("LegD_L", "foot2l", 1.0), ("LegD_R", "foot2r", 1.0),
    ("KneeD_L", "foot2l", 1.0), ("KneeD_R", "foot2r", 1.0),
    ("AnkleD_L", "foot3l", 1.0), ("AnkleD_R", "foot3r", 1.0),
]

BONE_MAP_TEMPLATE = _BONE_MAP_FALLBACK

_MMD_DEFAULTS = {
    "Root": "ParentNode", "Center": "Center",
    "SpineLower": "UpperBody", "SpineUpper": "UpperBody2",
    "Waist": "Waist", "LowerBody": "LowerBody",
    "Neck": "Neck", "Head": "Head", "Eye_L": "Eye_L", "Eye_R": "Eye_R",
    "Thigh_L": "Leg_L", "Knee_L": "Knee_L", "Foot_L": "Ankle_L",
    "AnkleTip_L": "AnkleTip_L", "LegTipEX_L": "LegTipEX_L",
    "LegD_L": "LegD_L", "KneeD_L": "KneeD_L", "AnkleD_L": "AnkleD_L",
    "WaistCancel_L": "WaistCancel_L",
    "Thigh_R": "Leg_R", "Knee_R": "Knee_R", "Foot_R": "Ankle_R",
    "AnkleTip_R": "AnkleTip_R", "LegTipEX_R": "LegTipEX_R",
    "LegD_R": "LegD_R", "KneeD_R": "KneeD_R", "AnkleD_R": "AnkleD_R",
    "WaistCancel_R": "WaistCancel_R",
    "Clavicle_L": "ShoulderP_L", "Shoulder_L": "Arm_L",
    "ArmTwist_L": "ArmTwist_L", "Elbow_L": "Elbow_L",
    "HandTwist_L": "HandTwist_L", "Wrist_L": "Wrist_L",
    "ShoulderC_L": "ShoulderC_L",
    "Clavicle_R": "ShoulderP_R", "Shoulder_R": "Arm_R",
    "ArmTwist_R": "ArmTwist_R", "Elbow_R": "Elbow_R",
    "HandTwist_R": "HandTwist_R", "Wrist_R": "Wrist_R",
    "ShoulderC_R": "ShoulderC_R",
    "Thumb_L1": "Thumb0_L", "Thumb_L2": "Thumb1_L", "Thumb_L3": "Thumb2_L",
    "IndexFinger_L1": "IndexFinger1_L", "IndexFinger_L2": "IndexFinger2_L", "IndexFinger_L3": "IndexFinger3_L",
    "MiddleFinger_L1": "MiddleFinger1_L", "MiddleFinger_L2": "MiddleFinger2_L", "MiddleFinger_L3": "MiddleFinger3_L",
    "RingFinger_L1": "RingFinger1_L", "RingFinger_L2": "RingFinger2_L", "RingFinger_L3": "RingFinger3_L",
    "LittleFinger_L1": "LittleFinger1_L", "LittleFinger_L2": "LittleFinger2_L", "LittleFinger_L3": "LittleFinger3_L",
    "Thumb_R1": "Thumb0_R", "Thumb_R2": "Thumb1_R", "Thumb_R3": "Thumb2_R",
    "IndexFinger_R1": "IndexFinger1_R", "IndexFinger_R2": "IndexFinger2_R", "IndexFinger_R3": "IndexFinger3_R",
    "MiddleFinger_R1": "MiddleFinger1_R", "MiddleFinger_R2": "MiddleFinger2_R", "MiddleFinger_R3": "MiddleFinger3_R",
    "RingFinger_R1": "RingFinger1_R", "RingFinger_R2": "RingFinger2_R", "RingFinger_R3": "RingFinger3_R",
    "LittleFinger_R1": "LittleFinger1_R", "LittleFinger_R2": "LittleFinger2_R", "LittleFinger_R3": "LittleFinger3_R",
}

_GEM2_DEFAULTS = {
    "Root": "basis", "Body": "body", "Placement": "placement",
    "SpineLower": "ik_leftright", "SpineUpper": "ik_updown",
    "Neck": "head", "Visor": "visor", "GunBack": "gun_back",
    "Foresight": "foresight2rot",
    "Thigh_L": "foot1l", "Knee_L": "foot2l", "Foot_L": "foot3l",
    "LegExtra_L1": "bone06", "LegExtra_L2": "bone07",
    "Thigh_R": "foot1r", "Knee_R": "foot2r", "Foot_R": "foot3r",
    "LegExtra_R1": "bone03", "LegExtra_R2": "bone05",
    "Clavicle_L": "clavicle_left", "Shoulder_L": "hand1l",
    "Elbow_L": "hand2l", "Wrist_L": "hand_rot1l",
    "Hand3_L": "hand3l", "Hand_L": "left_hand",
    "Palm1_L": "palm1l", "Palm2_L": "palm2l", "Palm3_L": "palm3l",
    "Palm2_Hide_L": "palm2l_hide", "Palm3_Hide_L": "palm3l_hide",
    "Palm4_Hide_L": "palm4l_hide",
    "PalmIK_Holder02_L": "palm_ik_holder_left02",
    "PalmIK_Holder01_L": "palm_ik_holder_left01",
    "PalmIK_Holder_L": "palm_ik_holder_left",
    "Clavicle_R": "clavicle_right", "Shoulder_R": "hand1r",
    "Elbow_R": "hand2r", "Wrist_R": "hand_rot1r",
    "Hand3_R": "hand3r", "Hand_R": "right_hand",
    "Palm1_R": "palm1r", "Palm2_R": "palm2r", "Palm3_R": "palm3r",
    "Palm2_Hide_R": "palm2r_hide", "Palm3_Hide_R": "palm3r_hide",
    "Palm4_Hide_R": "palm4r_hide",
    "PalmIK_Holder02_R": "palm_ik_holder_right02",
    "PalmIK_Holder01_R": "palm_ik_holder_right01",
    "PalmIK_Holder_R": "palm_ik_holder_right",
    "IK_Chain01": "ik_chain01", "IK_Chain02": "ik_chain02",
    "IK_Chain03": "ik_chain03", "IK_Chain04": "ik_chain04",
    "IK_Chain05": "ik_chain05", "IK_Chain06": "ik_chain06",
    "IK_Chain07": "ik_chain07", "IK_Chain08": "ik_chain08",
    "Skin": "skin",
}

MMD_JOINT_DICT = _MMD_DEFAULTS
GEM2_JOINT_DICT = _GEM2_DEFAULTS

# Reverse map: canonical GFA name → actual GEM2 bone
# (used by AlignAndBake to build target joint dict)
_GEM2_REVERSE_MAP = {
    "Root": "body",  "Body": "body",
    "Placement": "placement",
    "SpineLower": "ik_leftright",
    "SpineUpper": "ik_updown",
    "Neck": "head", "Head": "head",
    "Visor": "visor", "GunBack": "gun_back",
    "Foresight": "foresight2rot",
    # Left Leg
    "Thigh_L": "foot1l", "Knee_L": "foot2l", "Foot_L": "foot3l",
    "LegExtra_L1": "bone06", "LegExtra_L2": "bone07",
    "AnkleTip_L": "foot3l", "LegTipEX_L": "foot3l",
    # Right Leg
    "Thigh_R": "foot1r", "Knee_R": "foot2r", "Foot_R": "foot3r",
    "LegExtra_R1": "bone03", "LegExtra_R2": "bone05",
    "AnkleTip_R": "foot3r", "LegTipEX_R": "foot3r",
    # Left Arm
    "Clavicle_L": "clavicle_left", "Shoulder_L": "hand1l",
    "Elbow_L": "hand2l", "Wrist_L": "hand_rot1l",
    # Right Arm
    "Clavicle_R": "clavicle_right", "Shoulder_R": "hand1r",
    "Elbow_R": "hand2r", "Wrist_R": "hand_rot1r",
}

# ── PropertyGroup ──────────────────────────────────────────────

class PMX2GEM2_BoneMapEntry(PropertyGroup):
    source_bone: StringProperty(name=_("transfer.prop.source_bone"), default="")
    target_bone: StringProperty(name=_("transfer.prop.target_bone"), default="")
    weight: FloatProperty(name=_("transfer.prop.weight"), default=1.0, min=0.0, max=1.0)


class PMX2GEM2_UL_BoneMapList(UIList):
    bl_idname = "PMX2GEM2_UL_bone_map_list"
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "source_bone", text="", emboss=False, placeholder="MMD")
            row.label(text="->", icon='FORWARD')
            row.prop(item, "target_bone", text="", emboss=False, placeholder="GEM2")
            row.prop(item, "weight", text="", emboss=False)


class PMX2GEM2_SceneProps(PropertyGroup):
    source_armature_name: StringProperty(name=_("transfer.prop.source_armature"), default="")
    target_armature_name: StringProperty(name=_("transfer.prop.target_armature"), default="")
    bone_map: CollectionProperty(type=PMX2GEM2_BoneMapEntry)
    bone_map_index: IntProperty(default=0)
    bone_report: StringProperty(default="")


# ── Helper functions ───────────────────────────────────────────

def _get_mesh_and_armature():
    mesh_obj = None
    arm_obj = None
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH" and mesh_obj is None:
            mesh_obj = obj
        if obj.type == "ARMATURE" and arm_obj is None:
            arm_obj = obj
    return mesh_obj, arm_obj


def _resolve_armature(name):
    if not name:
        return None
    obj = bpy.data.objects.get(name)
    if obj and obj.type == 'ARMATURE':
        return obj
    return None


def _resolve_armatures(props):
    src = _resolve_armature(props.source_armature_name)
    tgt = _resolve_armature(props.target_armature_name)
    if not src or not tgt:
        arms = sorted(
            [o for o in bpy.context.scene.objects if o.type == 'ARMATURE'],
            key=lambda a: len(a.data.bones),
        )
        if not src and not tgt and len(arms) >= 2:
            tgt = arms[0]; src = arms[-1]
        elif len(arms) == 1:
            if not src and not tgt:
                src = tgt = arms[0]
            elif not src:
                src = arms[0]
            else:
                tgt = arms[0]
        elif len(arms) >= 2:
            if not src:
                for a in arms:
                    if tgt is None or a != tgt: src = a; break
            if not tgt:
                for a in reversed(arms):
                    if src is None or a != src: tgt = a; break
    return src, tgt


def _get_mesh_for_armature(arm_obj):
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            for mod in obj.modifiers:
                if mod.type == "ARMATURE" and mod.object == arm_obj:
                    return obj
    return None


# ── Alignment math ─────────────────────────────────────────────

def _wpos(a, n):
    return a.matrix_world @ a.pose.bones[n].head

def _mpos(a, names):
    pts = [_wpos(a, n) for n in names if n and n in a.pose.bones]
    return Vector(np.mean(np.array(pts), axis=0)) if pts else Vector((0,0,0))

def _vmaxis(a, sn, en):
    v = _mpos(a, en) - _mpos(a, sn)
    i = np.argmax(np.abs(v))
    d = "+" if v[i] >= 0 else "-"
    return (d, "XYZ"[i]) if i < 3 else (d, "Z")

def _proj(p1, p2, pl):
    d = p2 - p1
    if pl == "XY": a = math.degrees(math.atan2(d.y, d.x))
    elif pl == "YZ": a = math.degrees(math.atan2(d.z, d.y))
    else: a = math.degrees(math.atan2(d.z, d.x))
    return a + 360 if a < 0 else a

def _apply_rotation_world(arm_obj, bone_name, angle, axis):
    bone = arm_obj.pose.bones[bone_name]
    world_pos = arm_obj.matrix_world @ bone.head
    world_mat = arm_obj.matrix_world @ bone.matrix
    t = Matrix.Translation(world_pos)
    t_inv = Matrix.Translation(-world_pos)
    rot = Matrix.Rotation(math.radians(angle), 4, axis)
    new_world = t @ rot @ t_inv @ world_mat
    bone.matrix = arm_obj.matrix_world.inverted() @ new_world
    bpy.context.view_layer.update()

def _apply_rotation_projected(arm_obj, bone_name, angle, plane):
    rot_axis_set = {"X", "Y", "Z"} - set(plane)
    if len(rot_axis_set) != 1:
        raise ValueError("Plane must be XY, YZ, or XZ")
    axis = rot_axis_set.pop()
    _apply_rotation_world(arm_obj, bone_name, -angle if axis == "Y" else angle, axis)

def _apply_translation_world(arm_obj, bone_name, offset):
    bone = arm_obj.pose.bones[bone_name]
    world_mat = arm_obj.matrix_world @ bone.matrix
    new_world = Matrix.Translation(offset) @ world_mat
    bone.matrix = arm_obj.matrix_world.inverted() @ new_world
    bpy.context.view_layer.update()

def _apply_scale_world(arm_obj, bone_name, scale):
    bone = arm_obj.pose.bones[bone_name]
    world_mat = arm_obj.matrix_world @ bone.matrix
    translation = world_mat.translation.copy()
    scale_mat = Matrix.Diagonal((*scale, 1.0))
    new_world = (
        Matrix.Translation(translation) @
        scale_mat @
        Matrix.Translation(-translation) @
        world_mat
    )
    bone.matrix = arm_obj.matrix_world.inverted() @ new_world

def _apply_scale_local(arm_obj, bone_name, scale_ratio, main_axis, other_factor=0.5):
    bone = arm_obj.pose.bones[bone_name]
    main = scale_ratio
    other = scale_ratio ** other_factor if scale_ratio >= 0 else -((-scale_ratio) ** other_factor)
    if main_axis == "X":
        bone.scale = Vector((bone.scale.x * main, bone.scale.y * other, bone.scale.z * other))
    elif main_axis == "Y":
        bone.scale = Vector((bone.scale.x * other, bone.scale.y * main, bone.scale.z * other))
    elif main_axis == "Z":
        bone.scale = Vector((bone.scale.x * other, bone.scale.y * other, bone.scale.z * main))
    bpy.context.view_layer.update()

def _align_bone_rotation(arm_obj, rotating_pair, target_pair, plane_seq):
    for plane in plane_seq:
        src_rot = _proj(_wpos(arm_obj, rotating_pair[0]), _wpos(arm_obj, rotating_pair[1]), plane)
        tgt_rot = _proj(_wpos(arm_obj, target_pair[0]), _wpos(arm_obj, target_pair[1]), plane)
        diff = tgt_rot - src_rot
        _apply_rotation_projected(arm_obj, rotating_pair[0], diff, plane)

def _align_bone_length(arm_obj, scaling_pair, target_pair, main_axis, ratio=1.0, other_factor=0.5):
    sv = _wpos(arm_obj, scaling_pair[1]) - _wpos(arm_obj, scaling_pair[0])
    tv = _wpos(arm_obj, target_pair[1]) - _wpos(arm_obj, target_pair[0])
    length_ratio = (tv.length / sv.length) * ratio if sv.length > 0 else 1.0
    _apply_scale_local(arm_obj, scaling_pair[0], length_ratio, main_axis, other_factor)

def _get_bbox(mesh_obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = mesh_obj.evaluated_get(depsgraph)
    bbox = [mesh_obj.matrix_world @ Vector(c) for c in eval_obj.bound_box]
    return {
        "X": {"Min": min(v.x for v in bbox), "Max": max(v.x for v in bbox)},
        "Y": {"Min": min(v.y for v in bbox), "Max": max(v.y for v in bbox)},
        "Z": {"Min": min(v.z for v in bbox), "Max": max(v.z for v in bbox)},
    }

def _apply_pose_to_rest(mesh_obj):
    initial_active = bpy.context.active_object
    initial_mode = bpy.context.object.mode if bpy.context.object else "OBJECT"
    for mod in mesh_obj.modifiers:
        if mod.type == "ARMATURE" and mod.object:
            arm_mod = mod; break
    else:
        raise ValueError("No armature modifier found on mesh")
    arm_obj = arm_mod.object
    arm_mod_name = arm_mod.name
    bpy.context.view_layer.objects.active = mesh_obj
    mesh_obj.select_set(True)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.modifier_copy(modifier=arm_mod_name)
    new_mod_idx = list(mesh_obj.modifiers).index(arm_mod) + 1
    new_mod = mesh_obj.modifiers[new_mod_idx]
    bpy.ops.object.make_single_user(object=True, obdata=True)
    bpy.ops.object.modifier_apply(modifier=arm_mod_name)
    new_mod.name = arm_mod_name
    bpy.context.view_layer.update()
    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    bpy.ops.object.mode_set(mode="POSE")
    bpy.ops.pose.armature_apply()
    bpy.context.view_layer.update()
    bpy.context.view_layer.objects.active = initial_active
    bpy.ops.object.mode_set(mode=initial_mode)


# ── Weight merge helper ────────────────────────────────────────

def _merge_weights_to_parent(mesh_obj, arm_obj, bone_name_target_list):
    mesh = mesh_obj.data
    num_verts = len(mesh.vertices)
    vg_name_to_id = {}
    for vg in mesh_obj.vertex_groups:
        vg_name_to_id[vg.name] = vg.index
    current_w = _get_np().zeros((num_verts, len(mesh_obj.vertex_groups)), dtype=float)
    for vert in mesh.vertices:
        for g in vert.groups:
            current_w[vert.index, g.group] = g.weight
    target_set = set(bone_name_target_list)
    bone_to_merge_parent = {}
    for b in arm_obj.data.bones:
        if b.name in target_set: continue
        parent = b.parent
        while parent and parent.name not in target_set:
            parent = parent.parent
        if parent and parent.name in target_set:
            bone_to_merge_parent[b.name] = parent.name
    merged = set()
    for src_name, parent_name in bone_to_merge_parent.items():
        src_idx = vg_name_to_id.get(src_name)
        parent_idx = vg_name_to_id.get(parent_name)
        if src_idx is not None and parent_idx is not None:
            col = current_w[:, src_idx]
            mask = col > 1e-10
            if mask.any():
                current_w[mask, parent_idx] += col[mask]
            current_w[:, src_idx] = 0
            merged.add(src_name)
    row_sums = _get_np().sum(current_w, axis=-1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    current_w /= row_sums
    current_w[current_w < 1e-10] = 0
    vg_id_to_name = {v: k for k, v in vg_name_to_id.items()}
    for vg in list(mesh_obj.vertex_groups):
        mesh_obj.vertex_groups.remove(vg)
    for vg_id in range(current_w.shape[1]):
        col = current_w[:, vg_id]
        mask = col >= 1e-10
        if mask.any():
            name = vg_id_to_name.get(vg_id, "")
            if name:
                new_vg = mesh_obj.vertex_groups.new(name=name)
                for vert_id in _get_np().where(mask)[0]:
                    new_vg.add([int(vert_id)], col[vert_id], "REPLACE")
    return len(merged)


# ═══════════════════════════════════════════════════════════════
#  Operators
# ═══════════════════════════════════════════════════════════════

def _auto_rotate_finger(arm_obj, finger_chain, rot_axis, rot_ang):
    dir_sign, axis_name = rot_axis
    for bone_name in finger_chain:
        if dir_sign == "+":
            _apply_rotation_world(arm_obj, bone_name, rot_ang, axis_name)
            _apply_rotation_world(arm_obj, bone_name, rot_ang, axis_name)
        elif dir_sign == "-":
            _apply_rotation_world(arm_obj, bone_name, -rot_ang, axis_name)

def _get_vector_main_axis(arm_obj, start_names, end_names):
    vec = _mpos(arm_obj, end_names) - _mpos(arm_obj, start_names)
    max_idx = np.argmax(np.abs(vec))
    if max_idx >= len(vec):
        return ("+", "Z")
    return ("+", "XYZ"[max_idx]) if vec[max_idx] >= 0 else ("-", "XYZ"[max_idx])

def _get_projected_rotation(arm_obj, bone1, bone2, plane):
    return _proj(_wpos(arm_obj, bone1), _wpos(arm_obj, bone2), plane)


class PMX2GEM2_OT_DetectBones(Operator):
    bl_idname = "pmx2gem2.detect_bones"
    bl_label = _("transfer.op.detect_bones")

    def execute(self, context):
        props = context.scene.pmx2gem2_props
        src, tgt = _resolve_armatures(props)
        if not src or not tgt:
            self.report({"ERROR"}, _("transfer.err.select_two_armatures"))
            return {"CANCELLED"}

        src_bones = set(b.name for b in src.data.bones)
        tgt_bones = set(b.name for b in tgt.data.bones)
        common = src_bones & tgt_bones
        only_src = src_bones - tgt_bones
        only_tgt = tgt_bones - src_bones

        lines = [
            _("various.source") + f": {len(src_bones)}",
            _("various.target") + f": {len(tgt_bones)}",
            _("various.common") + f": {len(common)}",
            _("various.source_only") + f": {len(only_src)}",
            _("various.target_only") + f": {len(only_tgt)}",
        ]
        props.bone_report = "\n".join(lines)
        self.report({"INFO"}, _("transfer.info.detect", src=len(src_bones), tgt=len(tgt_bones), common=len(common)))
        return {"FINISHED"}


class PMX2GEM2_OT_CleanRigidBodies(Operator):
    bl_idname = "pmx2gem2.clean_rigids"
    bl_label = _("transfer.op.clean_rigids")

    def execute(self, context):
        removed = 0
        for obj in list(bpy.data.objects):
            if obj.type != 'EMPTY': continue
            is_rigid = False
            try:
                if hasattr(obj, 'mmd_type') and obj.mmd_type in ('RIGID', 'JOINT'):
                    is_rigid = True
            except: pass
            try:
                if hasattr(obj, 'mmd_rigid') and obj.mmd_rigid:
                    is_rigid = True
            except: pass
            if not is_rigid:
                nm = obj.name.lower()
                if nm.startswith('rb_') or '_rigid_' in nm or '剛体' in obj.name:
                    is_rigid = True
            if is_rigid:
                try:
                    bpy.data.objects.remove(obj, do_unlink=True)
                    removed += 1
                except: pass
        self.report({"INFO"}, _("transfer.info.removed_rigids", count=removed))
        return {"FINISHED"}


class PMX2GEM2_OT_AlignAndBake(Operator):
    bl_idname = "pmx2gem2.align_and_bake"
    bl_label = _("transfer.op.align_bake")

    def execute(self, context):
        props = context.scene.pmx2gem2_props
        src, tgt = _resolve_armatures(props)
        if not src or not tgt:
            self.report({"ERROR"}, _("transfer.err.select_two_armatures"))
            return {"CANCELLED"}

        mesh_obj = _get_mesh_for_armature(src)
        if not mesh_obj:
            self.report({"ERROR"}, _("transfer.err.no_mesh_bound"))
            return {"CANCELLED"}

        from .bone_mapping_v2 import (
            resolve_pmx_bone, JP_TO_GFA,
            PLY_SKELETON_24, GFA_TO_GEM2_TARGET,
            diagnose_armature, print_diagnosis,
        )

        # Resolve source bone names: PMX→GFA
        src_bones = {b.name for b in src.data.bones}
        pmx_to_gfa = {}
        for bn in src_bones:
            gfa = resolve_pmx_bone(bn)
            if gfa:
                pmx_to_gfa[bn] = gfa

        print(f"[Align] Resolved {len(pmx_to_gfa)}/{len(src_bones)} PMX bone names")
        if len(pmx_to_gfa) < 10:
            print(f"[Align] WARNING: Very few bones mapped. PMX sample names:")
            for bn in sorted(src_bones)[:15]:
                print(f"  - {bn}")

        # Build joint lookup dicts from resolved bones
        # canonical → actual PMX bone name
        src_jd = {}
        for px_name, gfa_name in pmx_to_gfa.items():
            src_jd[gfa_name] = px_name

        # Add English self-names as fallbacks
        for bn in src_bones:
            if bn not in src_jd.values() and bn in src_bones:
                for k in ["ParentNode", "Center", "UpperBody", "UpperBody2",
                           "Waist", "LowerBody", "Neck", "Head",
                           "Eye_L", "Eye_R"]:
                    if bn == k:
                        src_jd[k] = bn

        # tgt_jd: canonical → GEM2 bone name
        tgt_jd = {}
        tgt_bone_set = set(b.name for b in tgt.data.bones)
        for gfa_en, gem2_entries in GFA_TO_GEM2_TARGET.items():
            for gem2_name, _ in gem2_entries:
                if gem2_name in tgt_bone_set:
                    # Find the canonical name for this gem2 target
                    for cn, gn in _GEM2_REVERSE_MAP.items():
                        if gn == gem2_name:
                            tgt_jd[cn] = gem2_name
                            break
                    else:
                        # Use the gem2 name directly
                        tgt_jd[gfa_en] = gem2_name

        print(f"[Align] source joint dict: {len(src_jd)} entries")
        print(f"[Align] target joint dict: {len(tgt_jd)} entries")

        # 添加 GEM2 参考骨骼
        need_add = tgt_bone_set - src_bones
        if need_add:
            mode0 = bpy.context.object.mode if bpy.context.object else "OBJECT"
            bpy.context.view_layer.objects.active = tgt
            bpy.ops.object.mode_set(mode="EDIT")
            bone_data = {}
            for b in tgt.data.edit_bones:
                if b.name in need_add:
                    bone_data[b.name] = {
                        "head": b.head.copy(), "tail": b.tail.copy(),
                        "mx": b.matrix.copy(),
                        "p": b.parent.name if b.parent and b.parent.name in tgt_bone_set else None,
                    }
            bpy.ops.object.mode_set(mode="OBJECT")
            bpy.context.view_layer.objects.active = src
            bpy.ops.object.mode_set(mode="EDIT")
            for nm, d in bone_data.items():
                eb = src.data.edit_bones.new(nm)
                eb.head = d["head"]; eb.tail = d["tail"]; eb.matrix = d["mx"]
            for nm, d in bone_data.items():
                if d["p"] and d["p"] in src.data.edit_bones:
                    src.data.edit_bones[nm].parent = src.data.edit_bones[d["p"]]
            bpy.ops.object.mode_set(mode=mode0)

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
            if not sn or sn not in src.pose.bones: missing.append("SRC:"+j)
            if not tn or tn not in src.pose.bones: missing.append("TGT:"+j)
        if missing:
            print(f"[Align] Missing joints: {missing}")
            self.report({"ERROR"}, _("transfer.err.missing_joints", joints=", ".join(missing[:6])))
            return {"CANCELLED"}

        # 进入 POSE 模式对齐
        initial_active = bpy.context.active_object
        bpy.context.view_layer.objects.active = src
        bpy.ops.object.mode_set(mode="EDIT")
        for jn in ["Shoulder_L","Shoulder_R","Neck","Thigh_L","Thigh_R",
                     "Foot_L","Foot_R","Wrist_L","Wrist_R"]:
            bn = src_jd.get(jn, "")
            if bn and bn in src.data.edit_bones:
                src.data.edit_bones[bn].inherit_scale = "AVERAGE"
        bpy.ops.object.mode_set(mode="POSE")
        for bone in src.pose.bones:
            for c in list(bone.constraints):
                bone.constraints.remove(c)

        # === UMA 对齐逻辑 ===
        shd, sha = _vmaxis(src, [src_jd["Foot_L"],src_jd["Foot_R"]], [src_jd["Shoulder_L"],src_jd["Shoulder_R"]])
        thd, tha = _vmaxis(tgt, [tgt_jd["Foot_L"],tgt_jd["Foot_R"]], [tgt_jd["Shoulder_L"],tgt_jd["Shoulder_R"]])
        if sha != tha:
            ra = ({"X","Y","Z"}-{sha,tha}).pop()
            _apply_rotation_world(src, src_jd["Root"], 90, ra)
            shd, sha = _vmaxis(src, [src_jd["Foot_L"],src_jd["Foot_R"]], [src_jd["Shoulder_L"],src_jd["Shoulder_R"]])
        if shd != thd:
            ra = ({"X","Y","Z"}-{sha,tha}).pop()
            _apply_rotation_world(src, src_jd["Root"], 180, ra)
        ssd, ssa = _vmaxis(src, [src_jd["Shoulder_L"]], [src_jd["Shoulder_R"]])
        tsd, tsa = _vmaxis(tgt, [tgt_jd["Shoulder_L"]], [tgt_jd["Shoulder_R"]])
        if ssa != tsa:
            _apply_rotation_world(src, src_jd["Root"], 90, sha)
        if ssd != tsd:
            _apply_rotation_world(src, src_jd["Root"], 180, sha)

        # Full Body Pre-Alignment
        ls = ["YZ","XZ"]
        _align_bone_rotation(src, [src_jd["Thigh_L"],src_jd["Knee_L"]], [tgt_jd["Thigh_L"],tgt_jd["Knee_L"]], ls)
        _align_bone_rotation(src, [src_jd["Thigh_R"],src_jd["Knee_R"]], [tgt_jd["Thigh_R"],tgt_jd["Knee_R"]], ls)
        _align_bone_rotation(src, [src_jd["Knee_L"],src_jd["Foot_L"]], [tgt_jd["Knee_L"],tgt_jd["Foot_L"]], ls)
        _align_bone_rotation(src, [src_jd["Knee_R"],src_jd["Foot_R"]], [tgt_jd["Knee_R"],tgt_jd["Foot_R"]], ls)

        tgt_sh_z = _mpos(tgt, [tgt_jd["Shoulder_L"],tgt_jd["Shoulder_R"]]).z
        bbox_min = _get_bbox(mesh_obj)["Z"]["Min"]
        src_sh_z = _mpos(src, [src_jd["Shoulder_L"],src_jd["Shoulder_R"]]).z
        if src_sh_z - bbox_min > 0.001:
            ovs = (tgt_sh_z - 0) / (src_sh_z - bbox_min)
            src.pose.bones[src_jd["Root"]].scale *= ovs
            bpy.context.view_layer.update()

        src_bd = _proj(_mpos(src, [src_jd["Foot_L"],src_jd["Foot_R"]]),
                       _mpos(src, [src_jd["Shoulder_L"],src_jd["Shoulder_R"],src_jd["Neck"]]), "XZ")
        tgt_bd = _proj(_mpos(tgt, [tgt_jd["Foot_L"],tgt_jd["Foot_R"]]),
                       _mpos(tgt, [tgt_jd["Shoulder_L"],tgt_jd["Shoulder_R"],tgt_jd["Neck"]]), "XZ")
        bd = tgt_bd - src_bd
        _apply_rotation_projected(src, src_jd["Root"], bd, "XZ")
        _apply_rotation_projected(src, src_jd["Thigh_L"], -bd, "XZ")
        _apply_rotation_projected(src, src_jd["Thigh_R"], -bd, "XZ")
        _apply_translation_world(src, src_jd["Root"],
            _mpos(tgt, [tgt_jd["Thigh_L"],tgt_jd["Thigh_R"]]) - _mpos(src, [src_jd["Thigh_L"],src_jd["Thigh_R"]]))

        # Shoulder Alignment
        src_sh2 = _mpos(src, [src_jd["Shoulder_L"],src_jd["Shoulder_R"]]).z
        src_th2 = _mpos(src, [src_jd["Thigh_L"],src_jd["Thigh_R"]]).z
        tgt_sh2 = _mpos(tgt, [tgt_jd["Shoulder_L"],tgt_jd["Shoulder_R"]]).z
        tgt_th2 = _mpos(tgt, [tgt_jd["Thigh_L"],tgt_jd["Thigh_R"]]).z
        uh = (tgt_sh2 - tgt_th2) / (src_sh2 - src_th2) if abs(src_sh2 - src_th2) > 0.001 else 1.0
        ssw = (_mpos(src, [src_jd["Shoulder_L"]]) - _mpos(src, [src_jd["Shoulder_R"]])).length
        tsw = (_mpos(tgt, [tgt_jd["Shoulder_L"]]) - _mpos(tgt, [tgt_jd["Shoulder_R"]])).length
        uw = tsw / ssw if ssw > 0.001 else 1.0
        _apply_scale_world(src, src_jd["Root"], Vector(((uw*uh)**0.5, uw, uh)))
        _apply_translation_world(src, src_jd["Root"],
            _mpos(tgt, [tgt_jd["Thigh_L"],tgt_jd["Thigh_R"]]) - _mpos(src, [src_jd["Thigh_L"],src_jd["Thigh_R"]]))

        su = _proj(_mpos(src, [src_jd["SpineLower"]]),
                   _mpos(src, [src_jd["Shoulder_L"],src_jd["Shoulder_R"],src_jd["Neck"]]), "XZ")
        tu = _proj(_mpos(tgt, [tgt_jd["SpineLower"]]),
                   _mpos(tgt, [tgt_jd["Shoulder_L"],tgt_jd["Shoulder_R"],tgt_jd["Neck"]]), "XZ")
        ud = (tu - su) * 0.67
        _apply_rotation_projected(src, src_jd["SpineLower"], ud, "XZ")
        _apply_rotation_projected(src, src_jd["Shoulder_L"], -ud, "XZ")
        _apply_rotation_projected(src, src_jd["Shoulder_R"], -ud, "XZ")
        _apply_rotation_projected(src, src_jd["Neck"], -ud, "XZ")

        so = _mpos(tgt, [tgt_jd["Shoulder_L"],tgt_jd["Shoulder_R"],tgt_jd["Neck"]]) - _mpos(src, [src_jd["Shoulder_L"],src_jd["Shoulder_R"],src_jd["Neck"]])
        _apply_translation_world(src, src_jd["SpineLower"], so * 0.1)
        _apply_translation_world(src, src_jd["SpineUpper"], so * 0.4)
        _apply_translation_world(src, src_jd["Clavicle_L"], so * 0.5)
        _apply_translation_world(src, src_jd["Clavicle_R"], so * 0.5)
        _apply_translation_world(src, src_jd["Neck"], so * 0.5)
        ls_off = _mpos(tgt, [tgt_jd["Shoulder_L"]]) - _mpos(src, [src_jd["Shoulder_L"]])
        _apply_translation_world(src, src_jd["Clavicle_L"], ls_off * 0.6)
        _apply_translation_world(src, src_jd["Shoulder_L"], ls_off * 0.4)
        rs_off = _mpos(tgt, [tgt_jd["Shoulder_R"]]) - _mpos(src, [src_jd["Shoulder_R"]])
        _apply_translation_world(src, src_jd["Clavicle_R"], rs_off * 0.6)
        _apply_translation_world(src, src_jd["Shoulder_R"], rs_off * 0.4)
        n_off = _mpos(tgt, [tgt_jd["Neck"]]) - _mpos(src, [src_jd["Neck"]])
        _apply_translation_world(src, src_jd["Neck"], n_off * 0.4)

        # Leg Alignment
        _align_bone_length(src, [src_jd["Thigh_L"],src_jd["Knee_L"]], [tgt_jd["Thigh_L"],tgt_jd["Knee_L"]], "Y", other_factor=1.0/3)
        _align_bone_length(src, [src_jd["Thigh_R"],src_jd["Knee_R"]], [tgt_jd["Thigh_R"],tgt_jd["Knee_R"]], "Y", other_factor=1.0/3)
        keh = _mpos(src, [src_jd["Knee_L"],src_jd["Knee_R"]]).z
        kch = keh - _get_bbox(mesh_obj)["Z"]["Min"]
        if kch > 0.001:
            lsc = keh / kch
            _apply_scale_local(src, src_jd["Knee_L"], lsc, "Y", other_factor=1.0/3)
            _apply_scale_local(src, src_jd["Knee_R"], lsc, "Y", other_factor=1.0/3)
        _align_bone_rotation(src, [src_jd["Thigh_L"],src_jd["Foot_L"]], [tgt_jd["Thigh_L"],tgt_jd["Foot_L"]], ls)
        _align_bone_rotation(src, [src_jd["Thigh_R"],src_jd["Foot_R"]], [tgt_jd["Thigh_R"],tgt_jd["Foot_R"]], ls)

        # Arm Alignment
        ars = ["XY","YZ"]
        _align_bone_length(src, [src_jd["Shoulder_L"],src_jd["Elbow_L"]], [tgt_jd["Shoulder_L"],tgt_jd["Elbow_L"]], "Y")
        _align_bone_length(src, [src_jd["Shoulder_R"],src_jd["Elbow_R"]], [tgt_jd["Shoulder_R"],tgt_jd["Elbow_R"]], "Y")
        _align_bone_rotation(src, [src_jd["Shoulder_L"],src_jd["Elbow_L"]], [tgt_jd["Shoulder_L"],tgt_jd["Elbow_L"]], ars)
        _align_bone_rotation(src, [src_jd["Shoulder_R"],src_jd["Elbow_R"]], [tgt_jd["Shoulder_R"],tgt_jd["Elbow_R"]], ars)
        _align_bone_length(src, [src_jd["Elbow_L"],src_jd["Wrist_L"]], [tgt_jd["Elbow_L"],tgt_jd["Wrist_L"]], "Y")
        _align_bone_length(src, [src_jd["Elbow_R"],src_jd["Wrist_R"]], [tgt_jd["Elbow_R"],tgt_jd["Wrist_R"]], "Y")
        _align_bone_rotation(src, [src_jd["Elbow_L"],src_jd["Wrist_L"]], [tgt_jd["Elbow_L"],tgt_jd["Wrist_L"]], ars)
        _align_bone_rotation(src, [src_jd["Elbow_R"],src_jd["Wrist_R"]], [tgt_jd["Elbow_R"],tgt_jd["Wrist_R"]], ars)
        _align_bone_rotation(src, [src_jd["Shoulder_L"],src_jd["Wrist_L"]], [tgt_jd["Shoulder_L"],tgt_jd["Wrist_L"]], ars)
        _align_bone_rotation(src, [src_jd["Shoulder_R"],src_jd["Wrist_R"]], [tgt_jd["Shoulder_R"],tgt_jd["Wrist_R"]], ars)
        _apply_rotation_projected(src, src_jd["Shoulder_L"], 2.0, "YZ")
        _apply_rotation_projected(src, src_jd["Shoulder_R"], -2.0, "YZ")

        # Bake pose → rest
        _apply_pose_to_rest(mesh_obj)
        bpy.context.view_layer.objects.active = initial_active

        # Auto-fill bone mapping from resolved joints
        if len(props.bone_map) == 0:
            src_bone_set = set(b.name for b in src.data.bones)
            for cname, gem2_name in tgt_jd.items():
                mmd_name = src_jd.get(cname, "")
                if mmd_name and mmd_name in src_bone_set and gem2_name:
                    entry = props.bone_map.add()
                    entry.source_bone = mmd_name
                    entry.target_bone = gem2_name
                    entry.weight = 1.0

        self.report({"INFO"}, _("transfer.info.aligned", joints=len(required), refs=len(need_add)))
        return {"FINISHED"}


# ── Bone Map Management ────────────────────────────────────────

class PMX2GEM2_OT_BoneMapAdd(Operator):
    bl_idname = "pmx2gem2.bone_map_add"; bl_label = _("transfer.op.add")
    def execute(self, context):
        props = context.scene.pmx2gem2_props
        entry = props.bone_map.add()
        entry.source_bone = ""; entry.target_bone = ""; entry.weight = 1.0
        props.bone_map_index = len(props.bone_map) - 1
        return {"FINISHED"}

class PMX2GEM2_OT_BoneMapRemove(Operator):
    bl_idname = "pmx2gem2.bone_map_remove"; bl_label = _("transfer.op.remove")
    def execute(self, context):
        props = context.scene.pmx2gem2_props
        if props.bone_map:
            props.bone_map.remove(props.bone_map_index)
            props.bone_map_index = max(0, props.bone_map_index - 1)
        return {"FINISHED"}

class PMX2GEM2_OT_BoneMapClear(Operator):
    bl_idname = "pmx2gem2.bone_map_clear"; bl_label = _("transfer.op.clear")
    def execute(self, context):
        context.scene.pmx2gem2_props.bone_map.clear()
        return {"FINISHED"}

class PMX2GEM2_OT_BoneMapLoadTemplate(Operator):
    bl_idname = "pmx2gem2.bone_map_load_template"; bl_label = _("transfer.op.load_template")
    def execute(self, context):
        props = context.scene.pmx2gem2_props
        props.bone_map.clear()
        for src, tgt, w in BONE_MAP_TEMPLATE:
            entry = props.bone_map.add()
            entry.source_bone = src; entry.target_bone = tgt; entry.weight = w
        self.report({"INFO"}, _("transfer.info.loaded_template", count=len(BONE_MAP_TEMPLATE)))
        return {"FINISHED"}

class PMX2GEM2_OT_BoneMapAutoFill(Operator):
    bl_idname = "pmx2gem2.bone_map_auto_fill"; bl_label = _("transfer.op.auto_fill")
    def execute(self, context):
        props = context.scene.pmx2gem2_props
        src, tgt = _resolve_armatures(props)
        if not src or not tgt:
            self.report({"ERROR"}, _("transfer.err.select_both_armatures")); return {"CANCELLED"}
        src_bones = sorted(b.name for b in src.data.bones)
        tgt_bones = sorted(b.name for b in tgt.data.bones)
        props.bone_map.clear()
        matched = set()
        for sb in src_bones:
            if sb in tgt_bones:
                entry = props.bone_map.add()
                entry.source_bone = sb; entry.target_bone = sb; entry.weight = 1.0
                matched.add(sb)
        for sb in src_bones:
            if sb not in matched:
                entry = props.bone_map.add()
                entry.source_bone = sb; entry.target_bone = ""; entry.weight = 1.0
        self.report({"INFO"}, _("transfer.info.auto_fill", matched=len(matched), manual=len(src_bones)-len(matched)))
        return {"FINISHED"}


class PMX2GEM2_OT_RemoveExtraBones(Operator):
    bl_idname = "pmx2gem2.remove_extra_bones"; bl_label = _("transfer.op.remove_extra")
    def execute(self, context):
        props = context.scene.pmx2gem2_props
        src, _u = _resolve_armatures(props)
        if not src:
            self.report({"ERROR"}, _("transfer.err.src_not_found")); return {"CANCELLED"}
        mapped_mmd = set()
        for entry in props.bone_map:
            if entry.source_bone: mapped_mmd.add(entry.source_bone)
        if not mapped_mmd:
            self.report({"ERROR"}, _("transfer.err.bone_map_empty")); return {"CANCELLED"}
        tgt_bone_set = set()
        tgt = _resolve_armature(props.target_armature_name)
        if tgt and tgt.type == 'ARMATURE':
            tgt_bone_set = set(b.name for b in tgt.data.bones)
        to_keep = mapped_mmd | tgt_bone_set
        removed = 0
        initial = bpy.context.active_object
        bpy.context.view_layer.objects.active = src
        bpy.ops.object.mode_set(mode="EDIT")
        for bone in list(src.data.edit_bones):
            if bone.name not in to_keep:
                src.data.edit_bones.remove(bone)
                removed += 1
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = initial
        self.report({"INFO"}, _("transfer.info.removed_bones", count=removed))
        return {"FINISHED"}


class PMX2GEM2_OT_TransferWeights(Operator):
    bl_idname = "pmx2gem2.transfer_weights"; bl_label = _("transfer.op.transfer_weights")
    def execute(self, context):
        props = context.scene.pmx2gem2_props
        src, tgt = _resolve_armatures(props)
        if not src or not tgt:
            self.report({"ERROR"}, _("transfer.err.select_both_armatures")); return {"CANCELLED"}
        if len(props.bone_map) == 0:
            self.report({"ERROR"}, _("transfer.err.bone_map_empty")); return {"CANCELLED"}
        mesh_obj = _get_mesh_for_armature(src)
        if not mesh_obj:
            self.report({"ERROR"}, _("transfer.err.no_mesh_bound")); return {"CANCELLED"}

        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        mapping = {}
        for entry in props.bone_map:
            if entry.source_bone and entry.target_bone:
                if entry.target_bone not in mapping:
                    mapping[entry.target_bone] = []
                mapping[entry.target_bone].append((entry.source_bone, entry.weight))

        renamed = 0
        for gem2_name, src_list in mapping.items():
            total_src_weight = sum(w for _, w in src_list)
            for mmd_name, weight in src_list:
                vg = mesh_obj.vertex_groups.get(mmd_name)
                if vg is None: continue
                norm_w = weight / total_src_weight if total_src_weight > 0 else 1.0
                tgt_vg = mesh_obj.vertex_groups.get(gem2_name)
                if tgt_vg is not None and tgt_vg != vg:
                    for vert in mesh_obj.data.vertices:
                        try:
                            sw = vg.weight(vert.index)
                            if sw > 0:
                                tgt_vg.add([vert.index], sw * norm_w, 'ADD')
                        except: pass
                    mesh_obj.vertex_groups.remove(vg)
                elif tgt_vg is None:
                    vg.name = gem2_name
                renamed += 1

        # 清理零权重 VG
        for vg in list(mesh_obj.vertex_groups):
            if vg.name not in mapping:
                has_weight = False
                for vert in mesh_obj.data.vertices:
                    try:
                        if vg.weight(vert.index) > 0: has_weight = True; break
                    except: pass
                if not has_weight:
                    mesh_obj.vertex_groups.remove(vg)

        # 删除已映射的 MMD 骨骼
        bpy.context.view_layer.objects.active = src
        bpy.ops.object.mode_set(mode="EDIT")
        mmd_to_remove = set()
        for gem2_name, src_list in mapping.items():
            for mmd_name, _ in src_list:
                if mmd_name != gem2_name and mmd_name in src.data.edit_bones:
                    mmd_to_remove.add(mmd_name)
        removed_bones = 0
        for bone_name in list(mmd_to_remove):
            bone = src.data.edit_bones.get(bone_name)
            if bone:
                for child in list(bone.children):
                    child.parent = bone.parent
                src.data.edit_bones.remove(bone)
                removed_bones += 1
        bpy.ops.object.mode_set(mode="OBJECT")

        self.report({"INFO"}, _("transfer.info.transferred", vg=renamed, bones=removed_bones))
        return {"FINISHED"}


class PMX2GEM2_OT_FixUV(Operator):
    bl_idname = "pmx2gem2.fix_uv"; bl_label = _("transfer.op.fix_uv")
    def execute(self, context):
        mesh_obj, _u = _get_mesh_and_armature()
        if not mesh_obj:
            self.report({"ERROR"}, _("transfer.err.no_mesh")); return {"CANCELLED"}
        mesh = mesh_obj.data
        uv_layers = mesh.uv_layers
        if len(uv_layers) == 0:
            self.report({"ERROR"}, _("transfer.err.no_uv")); return {"CANCELLED"}
        active_uv = uv_layers.active or uv_layers[0]
        if active_uv.name != "UVMap":
            active_uv.name = "UVMap"
        self.report({"INFO"}, _("transfer.info.uv_check", count=len(uv_layers)))
        return {"FINISHED"}


class PMX2GEM2_OT_ExportGEM2(Operator):
    bl_idname = "pmx2gem2.export_gem2"; bl_label = _("transfer.op.export_gem2")
    directory: StringProperty(subtype='DIR_PATH')
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self); return {"RUNNING_MODAL"}
    def execute(self, context):
        from . import gem2_export
        if not self.directory:
            self.report({"ERROR"}, _("transfer.err.select_dir")); return {"CANCELLED"}
        return gem2_export.gem2_export(self.directory, self)


class PMX2GEM2_OT_RunMapping(Operator):
    bl_idname = "pmx2gem2.run_mapping"; bl_label = _("transfer.op.run_mapping")
    def execute(self, context):
        from .Pipeline import step_bone_mapping
        step_bone_mapping(); return {"FINISHED"}

class PMX2GEM2_OT_RunAlign(Operator):
    bl_idname = "pmx2gem2.run_align"; bl_label = _("transfer.op.run_align")
    def execute(self, context):
        from .Pipeline import step_align_skeleton
        step_align_skeleton(); return {"FINISHED"}

class PMX2GEM2_OT_RunTransfer(Operator):
    bl_idname = "pmx2gem2.run_transfer"; bl_label = _("transfer.op.run_transfer")
    def execute(self, context):
        from .Pipeline import step_transfer_weights
        step_transfer_weights(); return {"FINISHED"}

class PMX2GEM2_OT_RunUV(Operator):
    bl_idname = "pmx2gem2.run_uv"; bl_label = _("transfer.op.run_uv")
    def execute(self, context):
        from .Pipeline import step_uv_check
        step_uv_check(); return {"FINISHED"}

class PMX2GEM2_OT_RunDecimate(Operator):
    bl_idname = "pmx2gem2.run_decimate"; bl_label = _("transfer.op.run_decimate")
    def execute(self, context):
        from .Pipeline import step_decimate
        step_decimate(); return {"FINISHED"}


# ── Panel ──────────────────────────────────────────────────────

class PMX2GEM2_PT_Panel(Panel):
    bl_label = _("transfer.panel.label")
    bl_idname = "PMX2GEM2_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PMX2GEM2"

    def draw(self, context):
        layout = self.layout
        props = context.scene.pmx2gem2_props

        box = layout.box()
        box.label(text=_("transfer.panel.scene_status"), icon='SCENE_DATA')
        mesh_count = len([o for o in bpy.data.objects if o.type == 'MESH'])
        arm_count = len([o for o in bpy.data.objects if o.type == 'ARMATURE'])
        box.label(text=_("transfer.panel.meshes", count1=mesh_count, count2=arm_count))

        box = layout.box()
        box.label(text=_("transfer.panel.step1_detect"), icon='VIEWZOOM')
        box.operator("pmx2gem2.detect_bones", text=_("transfer.op.detect_bones"))
        if props.bone_report:
            for line in props.bone_report.split("\n")[:10]:
                box.label(text=line, icon='DOT')

        box = layout.box()
        box.label(text=_("transfer.panel.step2_clean"), icon='SORTALPHA')
        box.operator("pmx2gem2.clean_rigids", text=_("transfer.op.clean_rigids"))

        box = layout.box()
        box.label(text=_("transfer.panel.gfa_pipeline"), icon='SCRIPT')
        row = box.row(align=True)
        row.operator("pmx2gem2.run_mapping", text=_("transfer.btn.mapping"))
        row.operator("pmx2gem2.run_align", text=_("transfer.btn.align"))
        row = box.row(align=True)
        row.operator("pmx2gem2.run_transfer", text=_("transfer.btn.transfer"))
        row.operator("pmx2gem2.run_uv", text=_("transfer.btn.uv"))
        row = box.row(align=True)
        row.operator("pmx2gem2.run_decimate", text=_("transfer.btn.decimate"), icon='MOD_DECIM')
        row.operator("pmx2gem2.export_gem2", text=_("transfer.btn.export"), icon='EXPORT')

        box = layout.box()
        box.label(text="High→LowPoly Bake", icon='TEXTURE_DATA')
        box.operator("gem2.native_decimate_bake", text="Decimate + Bake (Native)",
                     icon='RENDER_STILL')
        box.label(text="5-phase: backup → decimate → UV → bake → cleanup",
                  icon='DOT')

        box = layout.box()
        box.label(text=_("transfer.panel.bone_map_editor"), icon='LINKED')
        row = box.row()
        row.template_list("PMX2GEM2_UL_bone_map_list", "",
                          props, "bone_map", props, "bone_map_index", rows=4)
        row = box.row(align=True)
        row.operator("pmx2gem2.bone_map_add", text=_("transfer.op.add"))
        row.operator("pmx2gem2.bone_map_remove", text=_("transfer.op.remove"))
        row.operator("pmx2gem2.bone_map_clear", text=_("transfer.op.clear"))
        row = box.row(align=True)
        row.operator("pmx2gem2.bone_map_load_template", text=_("transfer.op.load_template"))
        row.operator("pmx2gem2.bone_map_auto_fill", text=_("transfer.op.auto_fill"))

        box = layout.box()
        box.label(text=_("transfer.panel.export_section"), icon='EXPORT')
        box.operator("pmx2gem2.export_gem2", text=_("transfer.btn.export_full"))


# ── Skinning fix operators (from fix_skinning.py) ──────────────

def _parse_mdl_bones_world(mdl_path):
    """解析 .mdl 文件，返回 (world_mats_dict, mesh_parent_name)"""
    from .core import row34_to_blender, ori_to_blender, find_matching_brace
    import re
    if not os.path.isfile(mdl_path):
        return None, None
    with open(mdl_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    def _parse_node(text):
        inner = text.strip()
        if inner.startswith("{"): inner = inner[1:].strip()
        if inner.endswith("}"): inner = inner[:-1].strip()
        nm = re.search(r"bone\s+(?:revolute\s+)?\x22([^\x22]+)\x22", inner)
        if not nm: return None
        name = nm.group(1)
        children = []
        remaining = inner
        pre_text = ""
        while True:
            cs = remaining.find("{bone")
            if cs == -1:
                pre_text += remaining; break
            pre_text += remaining[:cs]
            ce = find_matching_brace(remaining, cs)
            if ce == -1: break
            child = _parse_node(remaining[cs:ce+1])
            if child: children.append(child)
            remaining = remaining[ce+1:]
        matrix = None; position = None; orientation = None
        m34_m = re.search(r"\{Matrix34\s*([^}]*)\}", pre_text)
        if m34_m:
            vals = re.findall(r"[-+]?\d*\.?\d+(?:e[+-]?\d+)?", m34_m.group(1))
            if len(vals) >= 12:
                matrix = [list(map(float, vals[i*3:(i+1)*3])) for i in range(4)]
        pos_m = re.search(r"Position\s+([-\d.e+-]+)\s+([-\d.e+-]+)\s+([-\d.e+-]+)", pre_text)
        if pos_m: position = (float(pos_m.group(1)), float(pos_m.group(2)), float(pos_m.group(3)))
        ori_m = re.search(r"\{Orientation\s*([^}]*)\}", pre_text, re.DOTALL)
        if ori_m:
            vals = re.findall(r"[-+]?\d*\.?\d+(?:e[+-]?\d+)?", ori_m.group(1))
            if len(vals) >= 9:
                orientation = [list(map(float, vals[i*3:(i+1)*3])) for i in range(3)]
        if not ori_m:
            ori_m = re.search(
                r"Orientation\s+([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+"
                r"([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)\s+"
                r"([-\d.e+]+)\s+([-\d.e+]+)\s+([-\d.e+]+)", pre_text)
            if ori_m:
                vals = [float(ori_m.group(i)) for i in range(1, 10)]
                orientation = [vals[i*3:(i+1)*3] for i in range(3)]
        has_volumeview = "{VolumeView" in pre_text
        return {"name": name, "matrix": matrix, "position": position,
                "orientation": orientation, "children": children,
                "has_volumeview": has_volumeview}

    skel_start = content.find("{Skeleton")
    if skel_start == -1: return None, None
    skel_end = find_matching_brace(content, skel_start)
    if skel_end == -1: return None, None
    skeleton_text = content[skel_start+1:skel_end]
    root_bones = []
    remaining = skeleton_text
    while True:
        bs = remaining.find("{bone")
        if bs == -1: break
        be = find_matching_brace(remaining, bs)
        if be == -1: break
        bone = _parse_node(remaining[bs:be+1])
        if bone: root_bones.append(bone)
        remaining = remaining[be+1:]
    if not root_bones: return None, None

    flat = {}
    mesh_parent = None
    def _flatten(node, parent_name=None):
        nonlocal mesh_parent
        local_mat = Matrix.Identity(4)
        if node["matrix"]:
            local_mat = row34_to_blender(node["matrix"])
        elif node["position"] and node["orientation"]:
            local_mat = ori_to_blender(node["orientation"])
            local_mat.translation = Vector(node["position"])
        elif node["orientation"]:
            local_mat = ori_to_blender(node["orientation"])
        elif node["position"]:
            local_mat = Matrix.Translation(Vector(node["position"]))
        flat[node["name"]] = {"local": local_mat, "parent": parent_name}
        if node.get("has_volumeview"):
            mesh_parent = node["name"]
        for child in node["children"]:
            _flatten(child, node["name"])
    for bone in root_bones:
        _flatten(bone, None)

    world_mats = {}
    def _compute_world(name):
        if name in world_mats: return world_mats[name]
        info = flat[name]
        parent = info["parent"]
        if parent and parent in flat:
            world = _compute_world(parent) @ info["local"]
        else:
            world = info["local"].copy()
        world_mats[name] = world
        return world
    for name in flat:
        _compute_world(name)
    return world_mats, mesh_parent


def _find_mdl_file_standalone(ply_filepath):
    """智能查找与 PLY 对应的 MDL 文件"""
    import glob
    base_dir = os.path.dirname(ply_filepath)
    mesh_name = os.path.splitext(os.path.basename(ply_filepath))[0]
    candidates = [
        os.path.join(base_dir, mesh_name + ".mdl"),
        os.path.join(base_dir, "skin.mdl"),
    ]
    for p in candidates:
        if os.path.isfile(p): return p
    all_mdls = glob.glob(os.path.join(base_dir, "*.mdl"))
    if len(all_mdls) == 1: return all_mdls[0]
    return None


def _apply_mesh_world_transform(mesh_obj, arm_obj, world_mats, mesh_parent_name):
    """用 mesh_parent 骨骼的世界矩阵将顶点从 LOCAL 变换到 WORLD"""
    mesh = mesh_obj.data
    if not mesh_parent_name or mesh_parent_name not in world_mats:
        print(f"[FixSkin] mesh_parent '{mesh_parent_name}' not in world_mats")
        return False
    mesh_world = world_mats[mesh_parent_name]
    for v in mesh.vertices:
        v.co = mesh_world @ v.co
    mesh.update()
    return True


class GEM2_OT_FixSkinning(Operator):
    bl_idname = "gem2.fix_skinning"
    bl_label = _("skinning.fix_label")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj and obj.type == "MESH":
            for mod in obj.modifiers:
                if mod.type == "ARMATURE" and mod.object:
                    return True
        return False

    def execute(self, context):
        mesh_obj = context.active_object
        arm_obj = None
        for mod in mesh_obj.modifiers:
            if mod.type == "ARMATURE" and mod.object:
                arm_obj = mod.object; break
        if not arm_obj:
            self.report({"ERROR"}, _("skinning.no_armature_mod")); return {"CANCELLED"}

        mdl_path = None
        if mesh_obj.get("gem2_ply_source"):
            mdl_path = _find_mdl_file_standalone(mesh_obj["gem2_ply_source"])
        if not mdl_path:
            self.report({"ERROR"}, _("skinning.no_mdl")); return {"CANCELLED"}

        world_mats, mesh_parent_name = _parse_mdl_bones_world(mdl_path)
        if not world_mats:
            self.report({"ERROR"}, _("skinning.parse_mdl_failed", path=mdl_path)); return {"CANCELLED"}

        if _apply_mesh_world_transform(mesh_obj, arm_obj, world_mats, mesh_parent_name):
            self.report({"INFO"}, _("skinning.fix_done"))
        else:
            self.report({"ERROR"}, _("skinning.fix_failed")); return {"CANCELLED"}
        return {"FINISHED"}


class GEM2_OT_FixSkinningFromFile(Operator):
    bl_idname = "gem2.fix_skinning_from_file"
    bl_label = _("skinning.fix_file_label")
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if obj and obj.type == "MESH":
            for mod in obj.modifiers:
                if mod.type == "ARMATURE" and mod.object:
                    return True
        return False

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        mesh_obj = context.active_object
        arm_obj = None
        for mod in mesh_obj.modifiers:
            if mod.type == "ARMATURE" and mod.object:
                arm_obj = mod.object; break
        if not arm_obj:
            self.report({"ERROR"}, _("skinning.no_armature_mod")); return {"CANCELLED"}
        if not os.path.isfile(self.filepath):
            self.report({"ERROR"}, _("skinning.mdl_not_found", path=self.filepath)); return {"CANCELLED"}

        world_mats, mesh_parent_name = _parse_mdl_bones_world(self.filepath)
        if not world_mats:
            self.report({"ERROR"}, _("skinning.parse_mdl_failed", path=self.filepath)); return {"CANCELLED"}

        if _apply_mesh_world_transform(mesh_obj, arm_obj, world_mats, mesh_parent_name):
            self.report({"INFO"}, _("skinning.fix_done"))
        else:
            self.report({"ERROR"}, _("skinning.fix_failed")); return {"CANCELLED"}
        return {"FINISHED"}


class GEM2_PT_Panel(Panel):
    bl_label = _("skinning.panel_label")
    bl_idname = "GEM2_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GEM2 Tools"

    def draw(self, context):
        layout = self.layout
        layout.label(text=_("skinning.fix_mismatch"), icon="ARMATURE_DATA")
        layout.operator("gem2.fix_skinning", text=_("skinning.auto_fix"))
        layout.operator("gem2.fix_skinning_from_file", text=_("skinning.fix_with_file"))
        box = layout.box()
        box.label(text=_("skinning.how_to"), icon="INFO")
        box.label(text=_("skinning.step1"))
        box.label(text=_("skinning.step2"))
        box.label(text=_("skinning.step3"))
        box.label(text=_("skinning.step4"))
        box.label(text=_("skinning.step5"))


# ── Registration ───────────────────────────────────────────────

TRANSFER_CLASSES = (
    PMX2GEM2_BoneMapEntry,
    PMX2GEM2_UL_BoneMapList,
    PMX2GEM2_SceneProps,
    PMX2GEM2_PT_Panel,
    PMX2GEM2_OT_DetectBones,
    PMX2GEM2_OT_CleanRigidBodies,
    PMX2GEM2_OT_RunMapping,
    PMX2GEM2_OT_RunAlign,
    PMX2GEM2_OT_RunTransfer,
    PMX2GEM2_OT_RunUV,
    PMX2GEM2_OT_RunDecimate,
    PMX2GEM2_OT_BoneMapAdd,
    PMX2GEM2_OT_BoneMapRemove,
    PMX2GEM2_OT_BoneMapClear,
    PMX2GEM2_OT_BoneMapLoadTemplate,
    PMX2GEM2_OT_BoneMapAutoFill,
    PMX2GEM2_OT_ExportGEM2,
    GEM2_OT_FixSkinning,
    GEM2_OT_FixSkinningFromFile,
    GEM2_PT_Panel,
)


def register():
    for cls in TRANSFER_CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception as e:
            print(f"[GEM2.transfer] FAIL register {cls.__name__}: {e}")
            import traceback; traceback.print_exc()
    try:
        bpy.types.Scene.pmx2gem2_props = PointerProperty(type=PMX2GEM2_SceneProps)
    except Exception as e:
        print(f"[GEM2.transfer] FAIL scene prop: {e}")


def unregister():
    del bpy.types.Scene.pmx2gem2_props
    for cls in reversed(TRANSFER_CLASSES):
        bpy.utils.unregister_class(cls)
