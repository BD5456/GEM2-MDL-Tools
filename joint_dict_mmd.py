# -*- coding: utf-8 -*-
"""
MMD Joint Dictionary: canonical_name -> actual MMD/PMX bone name
===============================================================
Map canonical joint names to the actual bone names in YOUR PMX model.
Fill in the right side with the bone names from your model.

Common MMD standard names (left column) are the canonical keys.
Right column: replace with your model's actual bone names.
Leave empty string if the bone doesn't exist in your model.
"""

MMD_JOINT_DICT = {
    # === Root / Center ===
    "Root": "ParentNode",
    "Center": "Center",

    # === Spine ===
    "SpineLower": "UpperBody",
    "SpineUpper": "UpperBody2",
    "Waist": "Waist",
    "LowerBody": "LowerBody",

    # === Neck / Head ===
    "Neck": "Neck",
    "Head": "Head",
    "Eye_L": "Eye_L",
    "Eye_R": "Eye_R",

    # === Left Leg ===
    "Thigh_L": "Leg_L",
    "Knee_L": "Knee_L",
    "Foot_L": "Ankle_L",
    "AnkleTip_L": "AnkleTip_L",
    "LegTipEX_L": "LegTipEX_L",
    "LegD_L": "LegD_L",
    "KneeD_L": "KneeD_L",
    "AnkleD_L": "AnkleD_L",
    "WaistCancel_L": "WaistCancel_L",

    # === Right Leg ===
    "Thigh_R": "Leg_R",
    "Knee_R": "Knee_R",
    "Foot_R": "Ankle_R",
    "AnkleTip_R": "AnkleTip_R",
    "LegTipEX_R": "LegTipEX_R",
    "LegD_R": "LegD_R",
    "KneeD_R": "KneeD_R",
    "AnkleD_R": "AnkleD_R",
    "WaistCancel_R": "WaistCancel_R",

    # === Left Arm ===
    "Clavicle_L": "ShoulderP_L",
    "Shoulder_L": "Arm_L",
    "ArmTwist_L": "ArmTwist_L",
    "Elbow_L": "Elbow_L",
    "HandTwist_L": "HandTwist_L",
    "Wrist_L": "Wrist_L",
    "ShoulderC_L": "ShoulderC_L",

    # === Right Arm ===
    "Clavicle_R": "ShoulderP_R",
    "Shoulder_R": "Arm_R",
    "ArmTwist_R": "ArmTwist_R",
    "Elbow_R": "Elbow_R",
    "HandTwist_R": "HandTwist_R",
    "Wrist_R": "Wrist_R",
    "ShoulderC_R": "ShoulderC_R",

    # === Left Fingers ===
    "Thumb_L1": "Thumb0_L",
    "Thumb_L2": "Thumb1_L",
    "Thumb_L3": "Thumb2_L",
    "IndexFinger_L1": "IndexFinger1_L",
    "IndexFinger_L2": "IndexFinger2_L",
    "IndexFinger_L3": "IndexFinger3_L",
    "MiddleFinger_L1": "MiddleFinger1_L",
    "MiddleFinger_L2": "MiddleFinger2_L",
    "MiddleFinger_L3": "MiddleFinger3_L",
    "RingFinger_L1": "RingFinger1_L",
    "RingFinger_L2": "RingFinger2_L",
    "RingFinger_L3": "RingFinger3_L",
    "LittleFinger_L1": "LittleFinger1_L",
    "LittleFinger_L2": "LittleFinger2_L",
    "LittleFinger_L3": "LittleFinger3_L",

    # === Right Fingers ===
    "Thumb_R1": "Thumb0_R",
    "Thumb_R2": "Thumb1_R",
    "Thumb_R3": "Thumb2_R",
    "IndexFinger_R1": "IndexFinger1_R",
    "IndexFinger_R2": "IndexFinger2_R",
    "IndexFinger_R3": "IndexFinger3_R",
    "MiddleFinger_R1": "MiddleFinger1_R",
    "MiddleFinger_R2": "MiddleFinger2_R",
    "MiddleFinger_R3": "MiddleFinger3_R",
    "RingFinger_R1": "RingFinger1_R",
    "RingFinger_R2": "RingFinger2_R",
    "RingFinger_R3": "RingFinger3_R",
    "LittleFinger_R1": "LittleFinger1_R",
    "LittleFinger_R2": "LittleFinger2_R",
    "LittleFinger_R3": "LittleFinger3_R",
}
