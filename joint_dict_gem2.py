# -*- coding: utf-8 -*-
"""
GEM2 Joint Dictionary: canonical_name -> GEM2 target bone name
===============================================================
Map canonical joint names to the actual bone names in the GEM2 target skeleton.
Based on 9.mdl (58 bones). Edit the right column if your target skeleton differs.

Canonical names are used by the skeleton alignment algorithm (UMA GOH.py).
"""

GEM2_JOINT_DICT = {
    # === Root / Body ===
    "Root": "basis",
    "Body": "body",
    "Placement": "placement",

    # === Spine ===
    "SpineLower": "ik_leftright",
    "SpineUpper": "ik_updown",

    # === Neck / Head ===
    "Neck": "head",
    "Visor": "visor",
    "GunBack": "gun_back",
    "Foresight": "foresight2rot",

    # === Left Leg ===
    "Thigh_L": "foot1l",
    "Knee_L": "foot2l",
    "Foot_L": "foot3l",
    "LegExtra_L1": "bone06",
    "LegExtra_L2": "bone07",

    # === Right Leg ===
    "Thigh_R": "foot1r",
    "Knee_R": "foot2r",
    "Foot_R": "foot3r",
    "LegExtra_R1": "bone03",
    "LegExtra_R2": "bone05",

    # === Left Arm ===
    "Clavicle_L": "clavicle_left",
    "Shoulder_L": "hand1l",
    "Elbow_L": "hand2l",
    "Wrist_L": "hand_rot1l",
    "Hand3_L": "hand3l",
    "Hand_L": "left_hand",
    "Palm1_L": "palm1l",
    "Palm2_L": "palm2l",
    "Palm3_L": "palm3l",
    "Palm2_Hide_L": "palm2l_hide",
    "Palm3_Hide_L": "palm3l_hide",
    "Palm4_Hide_L": "palm4l_hide",
    "PalmIK_Holder02_L": "palm_ik_holder_left02",
    "PalmIK_Holder01_L": "palm_ik_holder_left01",
    "PalmIK_Holder_L": "palm_ik_holder_left",

    # === Right Arm ===
    "Clavicle_R": "clavicle_right",
    "Shoulder_R": "hand1r",
    "Elbow_R": "hand2r",
    "Wrist_R": "hand_rot1r",
    "Hand3_R": "hand3r",
    "Hand_R": "right_hand",
    "Palm1_R": "palm1r",
    "Palm2_R": "palm2r",
    "Palm3_R": "palm3r",
    "Palm2_Hide_R": "palm2r_hide",
    "Palm3_Hide_R": "palm3r_hide",
    "Palm4_Hide_R": "palm4r_hide",
    "PalmIK_Holder02_R": "palm_ik_holder_right02",
    "PalmIK_Holder01_R": "palm_ik_holder_right01",
    "PalmIK_Holder_R": "palm_ik_holder_right",

    # === IK Chains ===
    "IK_Chain01": "ik_chain01",
    "IK_Chain02": "ik_chain02",
    "IK_Chain03": "ik_chain03",
    "IK_Chain04": "ik_chain04",
    "IK_Chain05": "ik_chain05",
    "IK_Chain06": "ik_chain06",
    "IK_Chain07": "ik_chain07",
    "IK_Chain08": "ik_chain08",

    # === Skin ===
    "Skin": "skin",
}
