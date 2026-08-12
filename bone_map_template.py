# -*- coding: utf-8 -*-
"""
Bone Mapping Template: MMD (PMX) bones -> GEM2 target bones
=============================================================
Format: (source_bone_name, target_bone_name, weight)

source_bone_name: MMD/PMX bone name (as imported by mmd_tools)
target_bone_name: GEM2 target skeleton bone name
weight: distribution weight (1.0 = full transfer)

Fill in the list below. Expected ~58-60 entries.
Common MMD bone names (from mmd_tools import):
  Root: ParentNode, Center
  Body: UpperBody, UpperBody2, Neck, Head
  Arms: ShoulderP_L/R, Shoulder_L/R, Arm_L/R, ArmTwist_L/R,
        Elbow_L/R, HandTwist_L/R, Wrist_L/R
  Fingers: Thumb0_L/R, Thumb1_L/R, Thumb2_L/R,
           IndexFinger1_L/R, IndexFinger2_L/R, IndexFinger3_L/R,
           MiddleFinger1~3_L/R, RingFinger1~3_L/R, LittleFinger1~3_L/R
  Legs: Leg_L/R, Knee_L/R, Ankle_L/R, AnkleTip_L/R, LegTipEX_L/R
  D-bones: LegD_L/R, KneeD_L/R, AnkleD_L/R
  Other: Waist, LowerBody, Eye_L/R, WaistCancel_L/R

Common GEM2 bone names (from sample PLY):
  basis, body, foot1l, foot2l, foot3l, foot1r, foot2r, foot3r,
  ik_leftright, ik_updown, head, visor, gun_back,
  clavicle_left, clavicle_right, hand1l, hand2l, hand_rot1l,
  palm1l, palm2l, palm3l, hand1r, hand2r, hand_rot1r,
  palm1r, palm2r, palm3r, placement, foresight2rot,
  palm_ik_holder_left, palm_ik_holder_right,
  bone03, bone05, bone06, bone07, ik_chain01~08, skin

NOTE: Replace the entries below with your actual bone mapping!
"""

BONE_MAP_TEMPLATE = [
    # === Root / Body ===
    ("ParentNode", "basis", 1.0),
    ("Center", "body", 1.0),
    ("UpperBody", "ik_leftright", 1.0),
    ("UpperBody2", "ik_updown", 1.0),
    ("Neck", "head", 1.0),
    ("Head", "head", 1.0),

    # === Left Leg ===
    ("Leg_L", "foot1l", 1.0),
    ("Knee_L", "foot2l", 1.0),
    ("Ankle_L", "foot3l", 1.0),

    # === Right Leg ===
    ("Leg_R", "foot1r", 1.0),
    ("Knee_R", "foot2r", 1.0),
    ("Ankle_R", "foot3r", 1.0),

    # === Left Arm ===
    ("ShoulderP_L", "clavicle_left", 1.0),
    ("Shoulder_L", "clavicle_left", 1.0),
    ("Arm_L", "hand1l", 1.0),
    ("Elbow_L", "hand2l", 1.0),
    ("Wrist_L", "hand_rot1l", 1.0),

    # === Right Arm ===
    ("ShoulderP_R", "clavicle_right", 1.0),
    ("Shoulder_R", "clavicle_right", 1.0),
    ("Arm_R", "hand1r", 1.0),
    ("Elbow_R", "hand2r", 1.0),
    ("Wrist_R", "hand_rot1r", 1.0),

    # === Left Fingers (example mapping to palm bones) ===
    ("Thumb0_L", "palm1l", 1.0),
    ("Thumb1_L", "palm1l", 1.0),
    ("Thumb2_L", "palm2l", 1.0),
    ("IndexFinger1_L", "palm1l", 0.5),
    ("IndexFinger2_L", "palm2l", 1.0),
    ("IndexFinger3_L", "palm2l", 1.0),
    ("MiddleFinger1_L", "palm1l", 0.5),
    ("MiddleFinger2_L", "palm2l", 1.0),
    ("MiddleFinger3_L", "palm2l", 1.0),
    ("RingFinger1_L", "palm1l", 0.5),
    ("RingFinger2_L", "palm2l", 1.0),
    ("RingFinger3_L", "palm2l", 1.0),
    ("LittleFinger1_L", "palm1l", 0.5),
    ("LittleFinger2_L", "palm2l", 1.0),
    ("LittleFinger3_L", "palm2l", 1.0),

    # === Right Fingers (example mapping to palm bones) ===
    ("Thumb0_R", "palm1r", 1.0),
    ("Thumb1_R", "palm1r", 1.0),
    ("Thumb2_R", "palm2r", 1.0),
    ("IndexFinger1_R", "palm1r", 0.5),
    ("IndexFinger2_R", "palm2r", 1.0),
    ("IndexFinger3_R", "palm2r", 1.0),
    ("MiddleFinger1_R", "palm1r", 0.5),
    ("MiddleFinger2_R", "palm2r", 1.0),
    ("MiddleFinger3_R", "palm2r", 1.0),
    ("RingFinger1_R", "palm1r", 0.5),
    ("RingFinger2_R", "palm2r", 1.0),
    ("RingFinger3_R", "palm2r", 1.0),
    ("LittleFinger1_R", "palm1r", 0.5),
    ("LittleFinger2_R", "palm2r", 1.0),
    ("LittleFinger3_R", "palm2r", 1.0),

    # === Extra bones (merge to nearest parent) ===
    ("Waist", "body", 1.0),
    ("LowerBody", "body", 1.0),
    ("Eye_L", "head", 1.0),
    ("Eye_R", "head", 1.0),
    ("ArmTwist_L", "hand1l", 1.0),
    ("ArmTwist_R", "hand1r", 1.0),
    ("HandTwist_L", "hand2l", 1.0),
    ("HandTwist_R", "hand2r", 1.0),
    ("AnkleTip_L", "foot3l", 1.0),
    ("AnkleTip_R", "foot3r", 1.0),
    ("LegTipEX_L", "foot3l", 1.0),
    ("LegTipEX_R", "foot3r", 1.0),
    ("WaistCancel_L", "foot1l", 1.0),
    ("WaistCancel_R", "foot1r", 1.0),
    ("LegD_L", "foot2l", 1.0),
    ("LegD_R", "foot2r", 1.0),
    ("KneeD_L", "foot2l", 1.0),
    ("KneeD_R", "foot2r", 1.0),
    ("AnkleD_L", "foot3l", 1.0),
    ("AnkleD_R", "foot3r", 1.0),
]