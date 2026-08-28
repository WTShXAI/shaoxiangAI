# -*- coding: utf-8 -*-
"""
步骤5b: 通用模型参数重校准模板 (供下一轮优化 draw_signal/reverse_odds 套用)
=============================================================================
为下一轮"优化所有模型"立标准: 重校准+对比报告+达标才改。

本模板演示如何用 master_dataset.csv 对任意"赔率→阈值"类模型做 walk-forward 重校准。
下一轮套用时:
  1. 复制本文件, 改 MODEL_NAME / param_grid / predict_func / label_func
  2. 跑 walk-forward (train≤2022/test≥2023)
  3. 产出对比报告 (基线 vs 扫描最优)
  4. 若 test 提升 ≥+0.5pp 且 train→test衰减<0.5pp = 干净泛化 → 走 runbook 合入

示范: draw_signal 的 DRAW_ALERT 阈值重校准
  参数: DRAW_ALERT ∈ [0.20, 0.22, 0.24, 0.26, 0.28, 0.30]
  指标: 平局预测 precision/recall/F1 (test集)
"""
import csv, json, os
from collections import Counter

IN = r"D:\Architecture\data\master_dataset.csv"
OUT = r"D:\Architecture\deliverables\draw_signal_calibration_template_20260729.json"

def market_draw_prob(oh, od, oa):
    o = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/od)/o

def main():
    rows = list(csv.DictReader(open(IN, encoding="utf-8-sig")))
    # 准备数据: 隐含P平 + 是否实际平局
    data = []
    for r in rows:
        try:
            oh, od, oa = float(r["odds_home"]), float(r["odds_draw"]), float(r["odds_away"])
            if oh > 1 and od > 1 and oa > 1:
                pd = market_draw_prob(oh, od, oa)
                is_draw = 1 if r.get("result_class") == "1" else 0
                data.append({"imp_d": pd, "is_draw": is_draw, "date": str(r["match_date"])})
        except: pass
    print(f"有效样本: {len(data)}")

    train = [d for d in data if d["date"] <= "2022-12-31"]
    test = [d for d in data if d["date"] >= "2023-01-01"]
    print(f"train: {len(train)} | test: {len(test)}")

    # draw实际占比 (基线)
    draw_rate = sum(d["is_draw"] for d in test) / len(test)
    print(f"test平局实际占比: {draw_rate:.4f}")

    # 阈值网格
    thresholds = [0.20, 0.22, 0.24, 0.26, 0.28, 0.30, 0.32]
    results = {}
    for th in thresholds:
        # 用 imp_d >= th 作为"预测平局"
        for split_name, split in [("train", train), ("test", test)]:
            tp = fp = fn = tn = 0
            for d in split:
                pred = 1 if d["imp_d"] >= th else 0
                actual = d["is_draw"]
                if pred == 1 and actual == 1: tp += 1
                elif pred == 1 and actual == 0: fp += 1
                elif pred == 0 and actual == 1: fn += 1
                else: tn += 1
            prec = tp / (tp + fp) if (tp + fp) > 0 else 0
            rec = tp / (tp + fn) if (tp + fn) > 0 else 0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
            results[f"th{th}_{split_name}"] = {
                "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
                "预警场次": tp + fp, "n": len(split)
            }
        print(f"  th={th}: test P={results[f'th{th}_test']['precision']} R={results[f'th{th}_test']['recall']} F1={results[f'th{th}_test']['f1']}")

    # 找test F1最优 (但用train选阈值, 防过拟合)
    train_f1 = {th: results[f"th{th}_train"]["f1"] for th in thresholds}
    best_th_train = max(train_f1, key=train_f1.get)

    report = {
        "说明": "draw_signal DRAW_ALERT阈值重校准模板 (演示用, 下一轮各模型套用此范式)",
        "数据源": "master_dataset.csv",
        "n_train": len(train), "n_test": len(test),
        "test平局占比(基线)": round(draw_rate, 4),
        "阈值网格": thresholds,
        "完整结果": results,
        "train最优F1阈值": best_th_train,
        "当前生产阈值": 0.26,
        "test@当前0.26": results.get("th0.26_test"),
        "test@train最优": results.get(f"th{best_th_train}_test"),
        "是否改参数": False,
        "下一步": "若train最优≠0.26且test F1提升显著, 下一轮走runbook评估合入",
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(report, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n→ {OUT}")

if __name__ == "__main__":
    main()
