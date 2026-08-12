# -*- coding: utf-8 -*-
"""
3-Layer Bone Mapping: PMX (Japanese) → GFA English → GEM2/MOWAS2
=================================================================
Sources:
  - GFA_Model_Weight_Transfer-main/Step3_TransferWeightFinal.py (bone_merging_list)
  - 9.mdl, sakura.mdl, 5.mdl (MOWAS2 .mdl skeletons — 58 bones each)
  - MMD模型样本/model_better2.pmx (user's PMX sample)
  - GFA_Model_Weight_Transfer-main/MMD_Input_Sample/ (GFA sample PMX)

Layer 1: JP (Japanese/Chinese/English PMX bones) → GFA canonical English
Layer 2: GFA canonical English → GEM2 target bones with weight ratios
Layer 3: .ply reference skeleton (24 weight-receiving bones) for validation
"""

# ═══════════════════════════════════════════════════════════════
#  Layer 1: PMX bone name → GFA English canonical name
#
#  Handles THREE naming conventions commonly found in PMX models:
#    a) Full-width Japanese: "左足", "右ひじ"
#    b) Dot notation:        "足.L", "ひじ.R"
#    c) English import:      "Leg_L", "Elbow_R" (via mmd_tools rename)
#  If a bone isn't found under one style, the others are tried.
# ═══════════════════════════════════════════════════════════════

JP_TO_GFA = {
    # ── Root / Center ──
    "全ての親":   "ParentNode",
    "センター":   "Center",
    "グルーブ":   "Groove",

    # ── Spine ──
    "上半身":     "UpperBody",
    "上半身2":    "UpperBody2",
    "腰":         "Waist",
    "下半身":     "LowerBody",

    # ── Neck / Head / Face ──
    "首":         "Neck",
    "頭":         "Head",
    "左目":       "Eye_L",
    "右目":       "Eye_R",
    "目.L":       "Eye_L",
    "目.R":       "Eye_R",

    # ── Shoulders (full Japanese) ──
    "左肩":       "Shoulder_L",
    "右肩":       "Shoulder_R",
    "左肩P":      "ShoulderP_L",
    "右肩P":      "ShoulderP_R",
    "左肩C":      "ShoulderC_L",
    "右肩C":      "ShoulderC_R",

    # ── Shoulders (dot notation) ──
    "肩.L":       "Shoulder_L",
    "肩.R":       "Shoulder_R",
    "肩P.L":      "ShoulderP_L",
    "肩P.R":      "ShoulderP_R",
    "肩C.L":      "ShoulderC_L",
    "肩C.R":      "ShoulderC_R",
    "肩Solo.L":   "Shoulder_L",
    "肩Solo.R":   "Shoulder_R",

    # ── Upper Arms ──
    "左腕":       "Arm_L",
    "右腕":       "Arm_R",
    "腕.L":       "Arm_L",
    "腕.R":       "Arm_R",
    "左腕捩":     "ArmTwist_L",
    "右腕捩":     "ArmTwist_R",
    "腕捩.L":     "ArmTwist_L",
    "腕捩.R":     "ArmTwist_R",

    # ── Elbows ──
    "左ひじ":     "Elbow_L",
    "右ひじ":     "Elbow_R",
    "ひじ.L":     "Elbow_L",
    "ひじ.R":     "Elbow_R",

    # ── Wrists / Hand Twists ──
    "左手首":     "Wrist_L",
    "右手首":     "Wrist_R",
    "手首.L":     "Wrist_L",
    "手首.R":     "Wrist_R",
    "左手捩":     "HandTwist_L",
    "右手捩":     "HandTwist_R",
    "手捩.L":     "HandTwist_L",
    "手捩.R":     "HandTwist_R",

    # ── Left Fingers (full Japanese) ──
    "左親指０":   "Thumb0_L",
    "左親指１":   "Thumb1_L",
    "左親指２":   "Thumb2_L",
    "左人指１":   "IndexFinger1_L",
    "左人指２":   "IndexFinger2_L",
    "左人指３":   "IndexFinger3_L",
    "左中指１":   "MiddleFinger1_L",
    "左中指２":   "MiddleFinger2_L",
    "左中指３":   "MiddleFinger3_L",
    "左薬指１":   "RingFinger1_L",
    "左薬指２":   "RingFinger2_L",
    "左薬指３":   "RingFinger3_L",
    "左小指１":   "LittleFinger1_L",
    "左小指２":   "LittleFinger2_L",
    "左小指３":   "LittleFinger3_L",

    # ── Left Fingers (dot notation) ──
    "親指０.L":   "Thumb0_L",
    "親指１.L":   "Thumb1_L",
    "親指２.L":   "Thumb2_L",
    "人指１.L":   "IndexFinger1_L",
    "人指２.L":   "IndexFinger2_L",
    "人指３.L":   "IndexFinger3_L",
    "中指１.L":   "MiddleFinger1_L",
    "中指２.L":   "MiddleFinger2_L",
    "中指３.L":   "MiddleFinger3_L",
    "薬指１.L":   "RingFinger1_L",
    "薬指２.L":   "RingFinger2_L",
    "薬指３.L":   "RingFinger3_L",
    "小指１.L":   "LittleFinger1_L",
    "小指２.L":   "LittleFinger2_L",
    "小指３.L":   "LittleFinger3_L",

    # ── Right Fingers (full Japanese) ──
    "右親指０":   "Thumb0_R",
    "右親指１":   "Thumb1_R",
    "右親指２":   "Thumb2_R",
    "右人指１":   "IndexFinger1_R",
    "右人指２":   "IndexFinger2_R",
    "右人指３":   "IndexFinger3_R",
    "右中指１":   "MiddleFinger1_R",
    "右中指２":   "MiddleFinger2_R",
    "右中指３":   "MiddleFinger3_R",
    "右薬指１":   "RingFinger1_R",
    "右薬指２":   "RingFinger2_R",
    "右薬指３":   "RingFinger3_R",
    "右小指１":   "LittleFinger1_R",
    "右小指２":   "LittleFinger2_R",
    "右小指３":   "LittleFinger3_R",

    # ── Right Fingers (dot notation) ──
    "親指０.R":   "Thumb0_R",
    "親指１.R":   "Thumb1_R",
    "親指２.R":   "Thumb2_R",
    "人指１.R":   "IndexFinger1_R",
    "人指２.R":   "IndexFinger2_R",
    "人指３.R":   "IndexFinger3_R",
    "中指１.R":   "MiddleFinger1_R",
    "中指２.R":   "MiddleFinger2_R",
    "中指３.R":   "MiddleFinger3_R",
    "薬指１.R":   "RingFinger1_R",
    "薬指２.R":   "RingFinger2_R",
    "薬指３.R":   "RingFinger3_R",
    "小指１.R":   "LittleFinger1_R",
    "小指２.R":   "LittleFinger2_R",
    "小指３.R":   "LittleFinger3_R",

    # ── Left Leg (full Japanese) ──
    "左足":       "Leg_L",
    "左ひざ":     "Knee_L",
    "左足首":     "Ankle_L",
    "左つま先":   "AnkleTip_L",
    "左足先EX":   "LegTipEX_L",
    "左足D":      "LegD_L",
    "左ひざD":    "KneeD_L",
    "左足首D":    "AnkleD_L",

    # ── Left Leg (dot notation) ──
    "足.L":       "Leg_L",
    "ひざ.L":     "Knee_L",
    "足首.L":     "Ankle_L",
    "つま先.L":   "AnkleTip_L",
    "足先EX.L":   "LegTipEX_L",
    "足D.L":      "LegD_L",
    "ひざD.L":    "KneeD_L",
    "足首D.L":    "AnkleD_L",

    # ── Right Leg (full Japanese) ──
    "右足":       "Leg_R",
    "右ひざ":     "Knee_R",
    "右足首":     "Ankle_R",
    "右つま先":   "AnkleTip_R",
    "右足先EX":   "LegTipEX_R",
    "右足D":      "LegD_R",
    "右ひざD":    "KneeD_R",
    "右足首D":    "AnkleD_R",

    # ── Right Leg (dot notation) ──
    "足.R":       "Leg_R",
    "ひざ.R":     "Knee_R",
    "足首.R":     "Ankle_R",
    "つま先.R":   "AnkleTip_R",
    "足先EX.R":   "LegTipEX_R",
    "足D.R":      "LegD_R",
    "ひざD.R":    "KneeD_R",
    "足首D.R":    "AnkleD_R",

    # ── Waist Cancel ──
    "腰キャンセル左":   "WaistCancel_L",
    "腰キャンセル右":   "WaistCancel_R",
    "腰キャンセル.L":   "WaistCancel_L",
    "腰キャンセル.R":   "WaistCancel_R",

    # ── IK bones (not weight-bound, but referenced) ──
    "左足ＩＫ":   "Leg_IK_L",
    "右足ＩＫ":   "Leg_IK_R",
    "左つま先ＩＫ": "AnkleTip_IK_L",
    "右つま先ＩＫ": "AnkleTip_IK_R",
    "左足IK":     "Leg_IK_L",
    "右足IK":     "Leg_IK_R",
    "左つま先IK": "AnkleTip_IK_L",
    "右つま先IK": "AnkleTip_IK_R",
}

# Also add English mmd_tools-imported names as self-mapping
# (mmd_tools with "Rename Bones To English" already produces these)
_ENGLISH_SELF = [
    "ParentNode", "Center", "Groove",
    "UpperBody", "UpperBody2", "Waist", "LowerBody",
    "Neck", "Head", "Eye_L", "Eye_R",
    "ShoulderP_L", "ShoulderP_R", "Shoulder_L", "Shoulder_R",
    "ShoulderC_L", "ShoulderC_R",
    "Arm_L", "Arm_R", "ArmTwist_L", "ArmTwist_R",
    "Elbow_L", "Elbow_R", "HandTwist_L", "HandTwist_R",
    "Wrist_L", "Wrist_R",
    "Thumb0_L", "Thumb1_L", "Thumb2_L",
    "IndexFinger1_L", "IndexFinger2_L", "IndexFinger3_L",
    "MiddleFinger1_L", "MiddleFinger2_L", "MiddleFinger3_L",
    "RingFinger1_L", "RingFinger2_L", "RingFinger3_L",
    "LittleFinger1_L", "LittleFinger2_L", "LittleFinger3_L",
    "Thumb0_R", "Thumb1_R", "Thumb2_R",
    "IndexFinger1_R", "IndexFinger2_R", "IndexFinger3_R",
    "MiddleFinger1_R", "MiddleFinger2_R", "MiddleFinger3_R",
    "RingFinger1_R", "RingFinger2_R", "RingFinger3_R",
    "LittleFinger1_R", "LittleFinger2_R", "LittleFinger3_R",
    "Leg_L", "Knee_L", "Ankle_L", "AnkleTip_L", "LegTipEX_L",
    "LegD_L", "KneeD_L", "AnkleD_L", "WaistCancel_L",
    "Leg_R", "Knee_R", "Ankle_R", "AnkleTip_R", "LegTipEX_R",
    "LegD_R", "KneeD_R", "AnkleD_R", "WaistCancel_R",
]
for _en in _ENGLISH_SELF:
    JP_TO_GFA[_en] = _en


# ═══════════════════════════════════════════════════════════════
#  Layer 1b: Koikatsu / Illusion bone names (cf_j_ / cf_d_ / cf_s_)
#
#  Koikatsu models (KKPMX export) use cf_ prefix:
#    cf_j_* = joint bones (main chain, must be mapped)
#    cf_d_* = secondary deformation bones (merge to nearest joint)
#    cf_s_* = supplementary / IK target bones
# ═══════════════════════════════════════════════════════════════

_KK_BONE_MAP = {
    # ── Root / Hips / Spine ──
    "cf_j_root":      "ParentNode",
    "cf_j_hips":      "Center",
    "cf_j_spine01":   "Waist",
    "cf_j_spine02":   "UpperBody",
    "cf_j_spine03":   "UpperBody2",

    # ── Neck / Head ──
    "cf_j_neck":      "Neck",
    "cf_j_head":      "Head",

    # ── Eyes ──
    "cf_j_eye_L":     "Eye_L",
    "cf_j_eye_R":     "Eye_R",

    # ── Left Arm ──
    "cf_j_shoulder_L":  "ShoulderP_L",
    "cf_s_shoulder01_L": "ShoulderP_L",
    "cf_s_shoulder02_L": "Shoulder_L",
    "cf_d_shoulder_L": "Shoulder_L",
    "cf_j_arm_L":      "Arm_L",
    "cf_d_arm_L":      "ArmTwist_L",
    "cf_j_elbo_L":     "Elbow_L",      # Koikatsu: "elbo" = elbow
    "cf_d_elbo_L":     "Elbow_L",
    "cf_j_wrist_L":    "Wrist_L",
    "cf_s_hand_L":     "Wrist_L",

    # ── Right Arm ──
    "cf_j_shoulder_R":  "ShoulderP_R",
    "cf_s_shoulder01_R": "ShoulderP_R",
    "cf_s_shoulder02_R": "Shoulder_R",
    "cf_d_shoulder_R": "Shoulder_R",
    "cf_j_arm_R":      "Arm_R",
    "cf_d_arm_R":      "ArmTwist_R",
    "cf_j_elbo_R":     "Elbow_R",
    "cf_d_elbo_R":     "Elbow_R",
    "cf_j_wrist_R":    "Wrist_R",
    "cf_s_hand_R":     "Wrist_R",

    # ── Left Fingers (Koikatsu uses 01/02/03, MMD uses 1/2/3, Thumb 0/1/2) ──
    "cf_j_thumb01_L":   "Thumb0_L",
    "cf_j_thumb02_L":   "Thumb1_L",
    "cf_j_thumb03_L":   "Thumb2_L",
    "cf_j_index01_L":   "IndexFinger1_L",
    "cf_j_index02_L":   "IndexFinger2_L",
    "cf_j_index03_L":   "IndexFinger3_L",
    "cf_j_middle01_L":  "MiddleFinger1_L",
    "cf_j_middle02_L":  "MiddleFinger2_L",
    "cf_j_middle03_L":  "MiddleFinger3_L",
    "cf_j_ring01_L":    "RingFinger1_L",
    "cf_j_ring02_L":    "RingFinger2_L",
    "cf_j_ring03_L":    "RingFinger3_L",
    "cf_j_little01_L":  "LittleFinger1_L",
    "cf_j_little02_L":  "LittleFinger2_L",
    "cf_j_little03_L":  "LittleFinger3_L",

    # ── Right Fingers ──
    "cf_j_thumb01_R":   "Thumb0_R",
    "cf_j_thumb02_R":   "Thumb1_R",
    "cf_j_thumb03_R":   "Thumb2_R",
    "cf_j_index01_R":   "IndexFinger1_R",
    "cf_j_index02_R":   "IndexFinger2_R",
    "cf_j_index03_R":   "IndexFinger3_R",
    "cf_j_middle01_R":  "MiddleFinger1_R",
    "cf_j_middle02_R":  "MiddleFinger2_R",
    "cf_j_middle03_R":  "MiddleFinger3_R",
    "cf_j_ring01_R":    "RingFinger1_R",
    "cf_j_ring02_R":    "RingFinger2_R",
    "cf_j_ring03_R":    "RingFinger3_R",
    "cf_j_little01_R":  "LittleFinger1_R",
    "cf_j_little02_R":  "LittleFinger2_R",
    "cf_j_little03_R":  "LittleFinger3_R",

    # ── Left Leg ──
    "cf_j_leg_L":     "Leg_L",
    "cf_d_leg_L":     "LegD_L",
    "cf_j_knee_L":    "Knee_L",
    "cf_d_knee_L":    "KneeD_L",
    "cf_j_ankle_L":   "Ankle_L",
    "cf_d_ankle_L":   "AnkleD_L",
    "cf_j_foot_L":    "AnkleTip_L",

    # ── Right Leg ──
    "cf_j_leg_R":     "Leg_R",
    "cf_d_leg_R":     "LegD_R",
    "cf_j_knee_R":    "Knee_R",
    "cf_d_knee_R":    "KneeD_R",
    "cf_j_ankle_R":   "Ankle_R",
    "cf_d_ankle_R":   "AnkleD_R",
    "cf_j_foot_R":    "AnkleTip_R",

    # ── Breast / Chest (merge to spine) ──
    "cf_d_bust00":    "UpperBody2",
    "cf_s_bust00_L":  "UpperBody2",
    "cf_s_bust00_R":  "UpperBody2",
    "cf_j_bust00":    "UpperBody2",

    # ── IK targets (not weight-mapped, but listed for completeness) ──
    "cf_j_leg_ik_L":    "Leg_IK_L",
    "cf_j_leg_ik_R":    "Leg_IK_R",
    "cf_j_foot_ik_L":   "AnkleTip_IK_L",
    "cf_j_foot_ik_R":   "AnkleTip_IK_R",
    "cf_j_hand_ik_L":   "Wrist_L",
    "cf_j_hand_ik_R":   "Wrist_R",
}

# Add to main dict
for kk_name, gfa_name in _KK_BONE_MAP.items():
    if kk_name not in JP_TO_GFA:
        JP_TO_GFA[kk_name] = gfa_name


# ═══════════════════════════════════════════════════════════════
#  Layer 2: GFA English canonical → [(GEM2 target, weight), ...]
#
#  This is the COMPLETE bone_merging_list from
#  GFA_Model_Weight_Transfer-main/Step3_TransferWeightFinal.py
#  adapted to stripped GEM2 names (no GFA_MWT_SKE_ prefix).
#
#  Weight split rules (from GFA project):
#    - Upper leg: full weight on foot1 (thigh)
#    - Lower leg: full weight on foot2  (calf)
#    - Ankle/foot: 50/50 split foot2/foot3
#    - Spine: UpperBody→ik_leftright, UpperBody2→ik_updown
#    - Neck/Head/Eyes: all → head
#    - Upper arm: full weight on hand1
#    - Forearm: full weight on hand2 (with handTwist→palm1 bleed)
#    - Wrist/fingers: distributed across palm1-3
# ═══════════════════════════════════════════════════════════════

GFA_TO_GEM2_TARGET = {
    # ── Body / Root ──
    "ControlNode":           [("body", 1.0)],
    "ParentNode":            [("body", 1.0)],
    "Center":                [("body", 1.0)],
    "Groove":                [("body", 1.0)],
    "_shadow_WaistCancel_L": [("body", 1.0)],
    "_shadow_WaistCancel_R": [("body", 1.0)],
    "Waist":                 [("body", 1.0)],
    "LowerBody":             [("body", 1.0)],

    # ── Left Leg ──
    "WaistCancel_L": [("foot1l", 1.0)],
    "Leg_L":         [("foot1l", 1.0)],
    "LegD_L":        [("foot1l", 1.0)],
    "Knee_L":        [("foot2l", 1.0)],
    "KneeD_L":       [("foot2l", 1.0)],
    "Ankle_L":       [("foot2l", 0.5), ("foot3l", 0.5)],
    "AnkleD_L":      [("foot2l", 0.5), ("foot3l", 0.5)],
    "AnkleTip_L":    [("foot3l", 1.0)],
    "LegTipEX_L":    [("foot3l", 1.0)],

    # ── Right Leg ──
    "WaistCancel_R": [("foot1r", 1.0)],
    "Leg_R":         [("foot1r", 1.0)],
    "LegD_R":        [("foot1r", 1.0)],
    "Knee_R":        [("foot2r", 1.0)],
    "KneeD_R":       [("foot2r", 1.0)],
    "Ankle_R":       [("foot2r", 0.5), ("foot3r", 0.5)],
    "AnkleD_R":      [("foot2r", 0.5), ("foot3r", 0.5)],
    "AnkleTip_R":    [("foot3r", 1.0)],
    "LegTipEX_R":    [("foot3r", 1.0)],

    # ── Spine ──
    "UpperBody":  [("ik_leftright", 1.0)],
    "UpperBody2": [("ik_updown", 1.0)],

    # ── Neck / Head / Eyes ──
    "Neck":  [("head", 1.0)],
    "Head":  [("head", 1.0)],
    "Eye_L": [("head", 1.0)],
    "Eye_R": [("head", 1.0)],

    # ── Left Shoulder / Clavicle ──
    "ShoulderP_L": [("clavicle_left", 1.0)],
    "Shoulder_L":  [("clavicle_left", 1.0)],

    # ── Right Shoulder / Clavicle ──
    "ShoulderP_R": [("clavicle_right", 1.0)],
    "Shoulder_R":  [("clavicle_right", 1.0)],

    # ── Left Upper Arm ──
    "ShoulderC_L": [("hand1l", 1.0)],
    "Arm_L":       [("hand1l", 1.0)],
    "ArmTwist_L":  [("hand1l", 1.0)],

    # ── Right Upper Arm ──
    "ShoulderC_R": [("hand1r", 1.0)],
    "Arm_R":       [("hand1r", 1.0)],
    "ArmTwist_R":  [("hand1r", 1.0)],

    # ── Left Forearm ──
    "Elbow_L":      [("hand2l", 1.0)],
    "HandTwist_L":  [("hand2l", 0.9), ("palm1l", 0.1)],

    # ── Right Forearm ──
    "Elbow_R":      [("hand2r", 1.0)],
    "HandTwist_R":  [("hand2r", 0.9), ("palm1r", 0.1)],

    # ── Left Wrist / Hand ── (merged, was [("palm1l", 0.975), ("palm1l", 0.025)])
    "Wrist_L":           [("palm1l", 1.0)],
    "Thumb0_L":          [("palm1l", 1.0)],
    "Thumb1_L":          [("palm1l", 1.0)],
    "Thumb2_L":          [("palm1l", 0.9),  ("palm2l", 0.1)],
    # Finger knuckle 1: mostly palm1, some palm2, trace palm3
    "IndexFinger1_L":    [("palm1l", 0.50), ("palm2l", 0.425), ("palm3l", 0.075)],
    "MiddleFinger1_L":   [("palm1l", 0.50), ("palm2l", 0.425), ("palm3l", 0.075)],
    "RingFinger1_L":     [("palm1l", 0.50), ("palm2l", 0.425), ("palm3l", 0.075)],
    "LittleFinger1_L":   [("palm1l", 0.50), ("palm2l", 0.425), ("palm3l", 0.075)],
    # Finger knuckle 2: shifting toward palm2/palm3
    "IndexFinger2_L":    [("palm1l", 0.50), ("palm2l", 0.35),  ("palm3l", 0.15)],
    "MiddleFinger2_L":   [("palm1l", 0.50), ("palm2l", 0.35),  ("palm3l", 0.15)],
    "RingFinger2_L":     [("palm1l", 0.50), ("palm2l", 0.35),  ("palm3l", 0.15)],
    "LittleFinger2_L":   [("palm1l", 0.50), ("palm2l", 0.35),  ("palm3l", 0.15)],
    # Finger tips: progressive shift toward palm3
    "IndexFinger3_L":    [("palm1l", 0.50), ("palm2l", 0.36),  ("palm3l", 0.14)],
    "MiddleFinger3_L":   [("palm1l", 0.50), ("palm2l", 0.37),  ("palm3l", 0.13)],
    "RingFinger3_L":     [("palm1l", 0.50), ("palm2l", 0.38),  ("palm3l", 0.12)],
    "LittleFinger3_L":   [("palm1l", 0.50), ("palm2l", 0.39),  ("palm3l", 0.11)],

    # ── Right Wrist / Hand ── (merged duplicates)
    "Wrist_R":           [("palm1r", 1.0)],
    "Thumb0_R":          [("palm1r", 1.0)],
    "Thumb1_R":          [("palm1r", 1.0)],
    "Thumb2_R":          [("palm1r", 0.9),  ("palm2r", 0.1)],
    "IndexFinger1_R":    [("palm1r", 0.50), ("palm2r", 0.425), ("palm3r", 0.075)],
    "MiddleFinger1_R":   [("palm1r", 0.50), ("palm2r", 0.425), ("palm3r", 0.075)],
    "RingFinger1_R":     [("palm1r", 0.50), ("palm2r", 0.425), ("palm3r", 0.075)],
    "LittleFinger1_R":   [("palm1r", 0.50), ("palm2r", 0.425), ("palm3r", 0.075)],
    "IndexFinger2_R":    [("palm1r", 0.50), ("palm2r", 0.35),  ("palm3r", 0.15)],
    "MiddleFinger2_R":   [("palm1r", 0.50), ("palm2r", 0.35),  ("palm3r", 0.15)],
    "RingFinger2_R":     [("palm1r", 0.50), ("palm2r", 0.35),  ("palm3r", 0.15)],
    "LittleFinger2_R":   [("palm1r", 0.50), ("palm2r", 0.35),  ("palm3r", 0.15)],
    "IndexFinger3_R":    [("palm1r", 0.50), ("palm2r", 0.36),  ("palm3r", 0.14)],
    "MiddleFinger3_R":   [("palm1r", 0.50), ("palm2r", 0.37),  ("palm3r", 0.13)],
    "RingFinger3_R":     [("palm1r", 0.50), ("palm2r", 0.38),  ("palm3r", 0.12)],
    "LittleFinger3_R":   [("palm1r", 0.50), ("palm2r", 0.39),  ("palm3r", 0.11)],
}


# ═══════════════════════════════════════════════════════════════
#  Layer 2b: GFA prefixed name → GEM2 stripped name
#  GFA_MWT_SKE_Body → body, etc.
# ═══════════════════════════════════════════════════════════════

GFA_PREFIX = "GFA_MWT_SKE_"

GFA_TO_GEM2_NAME = {
    "GFA_MWT_SKE_Body":           "body",
    "GFA_MWT_SKE_foot1L":         "foot1l",
    "GFA_MWT_SKE_foot2L":         "foot2l",
    "GFA_MWT_SKE_foot3L":         "foot3l",
    "GFA_MWT_SKE_foot1R":         "foot1r",
    "GFA_MWT_SKE_foot2R":         "foot2r",
    "GFA_MWT_SKE_foot3R":         "foot3r",
    "GFA_MWT_SKE_IK_LeftRight":   "ik_leftright",
    "GFA_MWT_SKE_IK_UpDown":      "ik_updown",
    "GFA_MWT_SKE_Head":           "head",
    "GFA_MWT_SKE_Clavicle_left":  "clavicle_left",
    "GFA_MWT_SKE_Hand1L":         "hand1l",
    "GFA_MWT_SKE_Hand2L":         "hand2l",
    "GFA_MWT_SKE_Palm1L":         "palm1l",
    "GFA_MWT_SKE_Palm2L":         "palm2l",
    "GFA_MWT_SKE_Palm3L":         "palm3l",
    "GFA_MWT_SKE_Clavicle_right": "clavicle_right",
    "GFA_MWT_SKE_Hand1R":         "hand1r",
    "GFA_MWT_SKE_Hand2R":         "hand2r",
    "GFA_MWT_SKE_Palm1R":         "palm1r",
    "GFA_MWT_SKE_Palm2R":         "palm2r",
    "GFA_MWT_SKE_Palm3R":         "palm3r",
}


def gfa_strip(gfa_name):
    """Strip GFA prefix and lowercase → GEM2 name."""
    if gfa_name.startswith(GFA_PREFIX):
        return gfa_name[len(GFA_PREFIX):].lower()
    return gfa_name.lower()


# ═══════════════════════════════════════════════════════════════
#  Layer 3: .ply reference skeleton (24 weight-receiving bones)
#
#  Extracted from actual MOWAS2 .mdl files (9.mdl, sakura.mdl, etc.)
#  These are the bones that receive vertex weights in .ply SKIN block.
#  Other bones (bone03-07, visor, gun_back, placement, foresight2rot,
#  palm_ik_holder_*, ik_chain*, hand3*, *_hide, left_hand/right_hand)
#  are mechanical/IK/helper bones that do NOT receive skin weights.
# ═══════════════════════════════════════════════════════════════

PLY_SKELETON_24 = [
    # Core / Body
    "body",
    # Left Leg (thigh → calf → foot)
    "foot1l", "foot2l", "foot3l",
    # Right Leg
    "foot1r", "foot2r", "foot3r",
    # Spine
    "ik_leftright", "ik_updown",
    # Head
    "head",
    # Left Arm (clavicle → upper → forearm → wrist)
    "clavicle_left", "hand1l", "hand2l", "hand_rot1l",
    # Left Hand (palm segments)
    "palm1l", "palm2l", "palm3l",
    # Right Arm
    "clavicle_right", "hand1r", "hand2r", "hand_rot1r",
    # Right Hand
    "palm1r", "palm2r", "palm3r",
]

# Bone name mapping for .mdl-internal → GEM2 weight targets
# hand_rot1l/hand_rot1r appear in .mdl skeletons but in GFA weight mapping
# they correspond to Wrist bones → palm1 (not hand_rot1 directly).
# hand_rot1 is the wrist twist bone in the .mdl which corresponds to
# where palm1 begins in the GFA mapping scheme.

PLY_BONE_SET = set(PLY_SKELETON_24)


# ═══════════════════════════════════════════════════════════════
#  Convenience: build the JP → [(gem2_name, weight)] lookup
# ═══════════════════════════════════════════════════════════════

def japanese_to_gem2(jp_names):
    """
    Convert a list of Japanese PMX bone names to (gem2_name, weight) tuples.
    Uses JP_TO_GFA → GFA_TO_GEM2_TARGET chain.
    Returns a dict: {gem2_name: total_weight}
    """
    result = {}
    for jp in jp_names:
        gfa_name = JP_TO_GFA.get(jp)
        if gfa_name is None:
            continue
        targets = GFA_TO_GEM2_TARGET.get(gfa_name, [])
        for gem2_name, weight in targets:
            result[gem2_name] = result.get(gem2_name, 0.0) + weight
    return result


def english_to_gem2(gfa_names):
    """
    Convert a list of English GFA bone names to (gem2_name, weight) tuples.
    Returns a dict: {gem2_name: total_weight}
    """
    result = {}
    for name in gfa_names:
        targets = GFA_TO_GEM2_TARGET.get(name, [])
        for gem2_name, weight in targets:
            result[gem2_name] = result.get(gem2_name, 0.0) + weight
    return result


# ═══════════════════════════════════════════════════════════════
#  Bone name lookup: try JP → GFA, also try direct EN match
# ═══════════════════════════════════════════════════════════════

def resolve_pmx_bone(pmx_name):
    """
    Given a PMX bone name (any style), return the GFA canonical name.
    Tries JP→GFA lookup first, then checks if name is already GFA English.
    Returns None if unmapped.
    """
    if pmx_name in JP_TO_GFA:
        return JP_TO_GFA[pmx_name]
    # Fallback: try without special chars (some imports add prefixes like "pmx_")
    lower = pmx_name.lower()
    for en_name in _ENGLISH_SELF:
        if lower == en_name.lower():
            return en_name
    return None


def resolve_to_gem2_targets(pmx_name):
    """Full chain: PMX name → [(GEM2 target, weight), ...]"""
    gfa_name = resolve_pmx_bone(pmx_name)
    if gfa_name is None:
        return None
    return GFA_TO_GEM2_TARGET.get(gfa_name)


# ═══════════════════════════════════════════════════════════════
#  Diagnostics: scan armature bones, report mapping coverage
# ═══════════════════════════════════════════════════════════════

def diagnose_armature(arm_obj):
    """
    Scan an armature and return a diagnostic report.
    Use this to check bone name mapping before weight transfer.

    Returns dict with:
      - total_bones
      - mapped: bone name → (gfa_name, [(gem2, weight), ...])
      - unmapped: list of bone names with no mapping
      - gem2_targets: set of GEM2 bones that will receive weights
      - coverage_pct
    """
    bone_names = sorted(b.name for b in arm_obj.data.bones)
    total = len(bone_names)

    mapped = {}
    unmapped = []
    gem2_targets = set()

    for bn in bone_names:
        gfa_name = resolve_pmx_bone(bn)
        if gfa_name is None:
            unmapped.append(bn)
            continue
        targets = GFA_TO_GEM2_TARGET.get(gfa_name)
        if targets is None:
            unmapped.append(bn)
            continue
        mapped[bn] = (gfa_name, targets)
        for gem2_name, _ in targets:
            gem2_targets.add(gem2_name)

    coverage = (len(mapped) / total * 100) if total > 0 else 0

    return {
        "total_bones": total,
        "mapped": mapped,
        "unmapped": unmapped,
        "gem2_targets": sorted(gem2_targets),
        "coverage_pct": coverage,
        "missing_ply": sorted(PLY_BONE_SET - gem2_targets),
    }


def print_diagnosis(diag):
    """Pretty-print a diagnosis report."""
    print("=" * 60)
    print(f"BONE MAPPING DIAGNOSIS")
    print(f"  Total bones:  {diag['total_bones']}")
    print(f"  Mapped:       {len(diag['mapped'])} ({diag['coverage_pct']:.1f}%)")
    print(f"  Unmapped:     {len(diag['unmapped'])}")
    print(f"  GEM2 targets: {len(diag['gem2_targets'])}/{len(PLY_SKELETON_24)}")
    print("")

    if diag["unmapped"]:
        print(f"UNMAPPED BONES ({len(diag['unmapped'])}):")
        for bn in diag["unmapped"][:20]:
            print(f"  - {bn}")
        if len(diag["unmapped"]) > 20:
            print(f"  ... and {len(diag['unmapped']) - 20} more")
        print("")

    if diag["missing_ply"]:
        print(f"MISSING FROM .PLY SKELETON ({len(diag['missing_ply'])}):")
        for bn in diag["missing_ply"]:
            print(f"  - {bn}")
        print("")

    if diag["mapped"]:
        print(f"SAMPLE MAPPED (first 10):")
        for bn, (gfa, targets) in list(diag["mapped"].items())[:10]:
            tgt_str = ", ".join(f"{n}:{w:.2f}" for n, w in targets)
            print(f"  {bn}  →  {gfa}  →  [{tgt_str}]")
        print("=" * 60)


# ═══════════════════════════════════════════════════════════════
#  For compatibility with old code
# ═══════════════════════════════════════════════════════════════

# Build set of all GEM2 targets
GEM2_TARGETS = set()
for targets in GFA_TO_GEM2_TARGET.values():
    for gem2_name, _ in targets:
        GEM2_TARGETS.add(gem2_name)

# Report gaps (for debugging)
_non_ply = GEM2_TARGETS - PLY_BONE_SET
_unmapped_ply = PLY_BONE_SET - GEM2_TARGETS


# ═══════════════════════════════════════════════════════════════
#  Self-test
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"[bone_mapping_v2] Loaded at {__file__}")
    print(f"  JP_TO_GFA:            {len(JP_TO_GFA)} entries")
    print(f"  GFA_TO_GEM2_TARGET:   {len(GFA_TO_GEM2_TARGET)} entries")
    print(f"  GFA_TO_GEM2_NAME:     {len(GFA_TO_GEM2_NAME)} entries")
    print(f"  PLY skeleton (24):    {len(PLY_SKELETON_24)} bones")
    print(f"  GEM2 targets total:   {len(GEM2_TARGETS)} unique")
    if _non_ply:
        print(f"  [WARN] Targets NOT in .ply:  {sorted(_non_ply)}")
    if _unmapped_ply:
        print(f"  [WARN] .ply bones unmapped:  {sorted(_unmapped_ply)}")
