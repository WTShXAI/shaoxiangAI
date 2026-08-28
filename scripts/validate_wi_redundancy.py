#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
validate_wi_redundancy.py — 验证 WI 集成中 wi_student 是否冗余 (优化删减前验证)
==========================================================================
目的: 在动刀删减前, 用硬数字确认:
  1. wi_teacher(教师, LightGBM) 与 wi_student(蒸馏学生, 温度缩放) 是否共线/冗余
  2. 把 wi_student 移出集成(权重回收到 teacher+devig)对最终概率/排名的影响
输出量化结论, 供"先行验证"决策.
"""
import sys, os
sys.path.insert(0, "D:/Architecture")
import numpy as np
from pipeline.william_inter_model import predict_1x2, calibrate_devig
from pipeline.model_ensemble import blend_1x2


def devig(h, d, a):
    inv = [1.0 / h, 1.0 / d, 1.0 / a]
    z = sum(inv)
    return [x / z for x in inv]


# 真实盘口样本(覆盖不同市场状态: 主强/客强/均势/深盘/平手)
SAMPLES = [
    (2.10, 3.40, 3.50), (1.95, 3.30, 4.20), (1.50, 4.20, 6.50), (2.80, 3.20, 2.50),
    (3.40, 3.10, 2.10), (1.30, 5.00, 9.00), (4.50, 3.80, 1.70), (2.30, 3.10, 3.20),
    (1.70, 3.60, 4.80), (2.00, 3.50, 3.60), (2.60, 3.30, 2.70), (1.85, 3.40, 4.50),
    (5.00, 4.00, 1.60), (1.40, 4.50, 7.50), (3.00, 3.40, 2.20),
]

teacher_vs_student_agree = 0
max_t_s_diff = 0.0
flip_with_vs_without = 0
max_shift = 0.0
n = len(SAMPLES)

for h, d, a in SAMPLES:
    t = predict_1x2(h, d, a, h, d, a)["proba"]          # wi_teacher
    inv = devig(h, d, a)
    s = calibrate_devig(*inv)                            # wi_student
    dv = inv                                             # devig_raw

    if np.argmax(t) == np.argmax(s):
        teacher_vs_student_agree += 1
    max_t_s_diff = max(max_t_s_diff, max(abs(x - y) for x, y in zip(t, s)))

    # 当前: 含 student (0.55/0.30/0.15)
    w_cur = {"wi_teacher": 0.55, "wi_student": 0.30, "devig_raw": 0.15}
    comp_cur = {"wi_teacher": t, "wi_student": s, "devig_raw": dv}
    b_cur, _ = blend_1x2(comp_cur, w_cur)

    # 优化后: 去 student, 权重回收到 teacher (0.85/0.15)
    w_new = {"wi_teacher": 0.85, "devig_raw": 0.15}
    comp_new = {"wi_teacher": t, "devig_raw": dv}
    b_new, _ = blend_1x2(comp_new, w_new)

    if np.argmax(b_cur) != np.argmax(b_new):
        flip_with_vs_without += 1
    max_shift = max(max_shift, max(abs(x - y) for x, y in zip(b_cur, b_new)))

print("=== WI 集成冗余度验证 ===")
print(f"样本数 = {n}")
print(f"teacher vs student argmax 一致率 = {teacher_vs_student_agree}/{n} = {teacher_vs_student_agree / n:.1%}")
print(f"teacher vs student 最大概率差    = {max_t_s_diff:.4f} ({max_t_s_diff * 100:.2f}pp)")
print(f"含/不含 student 排名翻转数       = {flip_with_vs_without}/{n}")
print(f"含/不含 student 最大概率偏移     = {max_shift:.4f} ({max_shift * 100:.2f}pp)")
print("结论: 若一致率≥95% 且偏移<0.5pp, 则 wi_student 在集成中冗余, 可安全移出热路径.")
