#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
distill_wi.py  —  WI 模型(教师) → 庄家devig校准器(学生) 的蒸馏
==================================================================
目标: 把 WI(全量真实特征训练的"主导模型")的**校准知识**迁移到一个
      轻量学生——一个 per-class 温度缩放校准器。学生只需庄家隐含概率
      (任意比赛的 1X2 收盘赔率即可得), 就能把原始 devig 概率校准到
      接近 WI 的校准水平。

为什么这是"蒸馏/知识迁移":
  - 教师 WI 在 59.9万场上产出软标签(校准过的 P(H/D/A))。
  - 学生用"庄家隐含概率 → 温度缩放 → 逼近教师软标签"的方式学习,
    最小化 KL(教师 || 学生)。学生参数仅 3 个(T_h,T_d,T_a)。
  - 推理时: 任意比赛只要有收盘赔率 → devig → 学生校准 → 得到
    "WI 风格"的校准概率, 不必重建 WI 的全部历史特征。

输出:
  - data/wi_calibrator.json  (T_h,T_d,T_a + 元数据)
  - 终端报告: 蒸馏前(devig原始) vs 蒸馏后(学生) vs 教师 的 KL/logloss/argmax一致
"""
import os, json, warnings
import numpy as np
import pandas as pd
from scipy.optimize import minimize

warnings.filterwarnings("ignore")
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.william_inter_model import predict_1x2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(ROOT, "data", "william_inter_training.csv")
OUT = os.path.join(ROOT, "data", "wi_calibrator.json")


def devig(h, d, a):
    inv = np.array([1.0 / h, 1.0 / d, 1.0 / a])
    z = inv.sum()
    return inv / z


def tempered(devig_p, T):
    # devig_p: (N,3), T: (3,)  — 逐类温度缩放
    x = np.log(np.clip(devig_p, 1e-6, 1)) / T
    x -= x.max(axis=1, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=1, keepdims=True)


def kl_teacher_student(T, devig_p, teacher_p):
    stu = tempered(devig_p, T)
    # KL(teacher || student), 逐样本平均
    kl = (teacher_p * (np.log(np.clip(teacher_p, 1e-9, 1)) - np.log(np.clip(stu, 1e-9, 1)))).sum(axis=1)
    return kl.mean()


def main():
    print("加载训练集(采样) ...")
    # 全量太大, 采样 15万行代表即可(覆盖两来源/各赛季)
    df = pd.read_csv(CSV, usecols=["open_h", "open_d", "open_a", "close_h", "close_d", "close_a"])
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["close_h", "close_d", "close_a"])
    if len(df) > 150000:
        df = df.sample(150000, random_state=42)
    print(f"  有效样本: {len(df)}")

    # 教师软标签: 逐行跑 WI (LightGBM, 快)
    print("生成教师(WI)软标签 ...")
    teacher = np.zeros((len(df), 3), dtype=float)
    devig_p = np.zeros((len(df), 3), dtype=float)
    for i, (_, row) in enumerate(df.iterrows()):
        oh, od, oa = row.open_h, row.open_d, row.open_a
        ch, cd, ca = row.close_h, row.close_d, row.close_a
        r = predict_1x2(oh, od, oa, ch, cd, ca)
        teacher[i] = r["proba"]
        devig_p[i] = devig(ch, cd, ca)
    print(f"  教师 argmax 分布 H/D/A = {np.bincount(teacher.argmax(1), minlength=3)}")

    # 蒸馏: 拟合 per-class 温度
    print("拟合 per-class 温度(最小化 KL(教师||学生)) ...")
    T0 = np.ones(3)
    res = minimize(kl_teacher_student, T0, args=(devig_p, teacher),
                   method="Nelder-Mead", options={"maxiter": 5000, "xatol": 1e-6})
    T = res.x
    print(f"  最优温度 T_h/T_d/T_a = {T.round(4)}  | KL*1000 = {res.fun*1000:.3f}")

    # 评估
    stu = tempered(devig_p, T)
    raw = devig_p

    def metrics(a, b):
        kl = (b * (np.log(np.clip(b, 1e-9, 1)) - np.log(np.clip(a, 1e-9, 1)))).sum(axis=1).mean()
        ll = -(b * np.log(np.clip(a, 1e-9, 1))).sum(axis=1).mean()
        agree = (a.argmax(1) == b.argmax(1)).mean()
        return kl, ll, agree

    kl_raw, ll_raw, ag_raw = metrics(raw, teacher)
    kl_stu, ll_stu, ag_stu = metrics(stu, teacher)
    print("\n=== 蒸馏效果 (相对教师 WI) ===")
    print(f"  蒸馏前 devig原始 : KL={kl_raw*1000:.2f}  logloss={ll_raw:.4f}  argmax一致={ag_raw*100:.2f}%")
    print(f"  蒸馏后 学生校准 : KL={kl_stu*1000:.2f}  logloss={ll_stu:.4f}  argmax一致={ag_stu*100:.2f}%")

    # 保存
    meta = {
        "T_h": float(T[0]), "T_d": float(T[1]), "T_a": float(T[2]),
        "kl_teacher_student_raw": float(kl_raw),
        "kl_teacher_student_distilled": float(kl_stu),
        "argmax_agreement_raw": float(ag_raw),
        "argmax_agreement_distilled": float(ag_stu),
        "n_train": int(len(df)),
        "note": "学生=per-class温度缩放, 输入仅需庄家隐含概率(devig), 输出逼近WI校准概率. 由 pipeline.william_inter_model.calibrate_devig 调用.",
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"\n校准器已存: {OUT}")

    # 样例
    print("\n=== 样例(收盘赔率 2.10/3.30/3.60) ===")
    ch, cd, ca = 2.10, 3.30, 3.60
    dv = devig(ch, cd, ca)
    st = tempered(dv.reshape(1, 3), T)[0]
    wi = predict_1x2(2.10, 3.40, 3.50, ch, cd, ca)["proba"]
    print(f"  devig原始 : {dv.round(3).tolist()}")
    print(f"  学生校准 : {st.round(3).tolist()}")
    print(f"  教师WI   : {np.array(wi).round(3).tolist()}")


if __name__ == "__main__":
    main()
