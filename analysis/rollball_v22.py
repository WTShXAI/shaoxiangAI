# -*- coding: utf-8 -*-
"""
滚球神器 v2.2 — 跨市场规则平局识别器 + 比赛类型分类
======================================================
核心发现(2026-08-21 全量2603场AH×OU矩阵挖掘):
  高平局区: OU≤2.5(防守型) × |AH|≤0.5(势均力敌) = 平局率31% vs 基线23% (+8pp)
    - AH+0.5×OU≤2.25: 32.2% | AH-0.5×OU≤2.25: 31.0%(n=306最大样本)
    - AH-1p×OU≤2.25: 31.2%(矛盾组合: 让1球却低进球线=庄家藏东西)
  低平局区: OU≥3.25 × |AH|≥1 = 11-16% (强队碾压+高进球)

用户方向落地: "赔率能完全反应比赛类型"
  防守型(OU≤2.25) + 势均力敌(AH≈0) = 默契平局温床
  对攻型(OU≥3.0) + 浅让 = 平局中等
  碾压型(AH≥1) = 平局最低
"""
import sqlite3, math, pickle
from collections import defaultdict

DB = "data/rollball_training.db"
SEED = 20260821

def ah_b(x):
    if x is None: return "NA"
    if x == 0: return "AH0"
    if 0 < x <= 0.5: return "AH+0.5"
    if -0.5 <= x < 0: return "AH-0.5"
    if x > 0.5: return "AH+1p"
    return "AH-1p"

def ou_b(x):
    """OU分桶(2026-08-21 用户拍板锚点2.0, 数据验证判别力7.3pp vs 2.5锚3.0pp)。

    核心结构(5978场实测, 非单调):
      <2.00  超低线诱导区: 大球率53.3%(庄家低开诱小, 实际反弹) + 平局33%
      2.00-2.25 平局温床核心: 平局36.6%(全表最高) + 大球33.2%(全表最低)
      2.25-2.75 过渡区
      2.75+  中高区
    """
    if x is None: return "NA"
    if x < 2.0: return "u2.0induce"   # 超低线诱导
    if x <= 2.25: return "u2.25core"  # 平局温床核心
    if x <= 2.5: return "u2.5"
    if x <= 2.75: return "u2.75"
    if x <= 3.25: return "u3.25"
    return "u3.5p"

# ── 比赛类型(用户需求: 从赔率识别对攻/防守/默契/碾压; 锚点2.0版) ──
def match_type(ah, ou):
    if ah is None or ou is None: return "unknown"
    shallow = abs(ah) <= 0.5
    deep = abs(ah) >= 1.0
    if ou < 2.0 and shallow: return "lowline_trap"     # 超低线诱导: 大球53%(庄家低开陷阱)
    if 2.0 <= ou <= 2.25 and shallow: return "defensive_draw"  # 平局温床核心: 平局37%
    if ou <= 2.5 and abs(ah) > 0.5 and abs(ah) < 1.0: return "grind"  # 磨盘型
    if ou >= 3.0 and shallow: return "open"            # 对攻型
    if deep: return "blowout"                          # 碾压型
    if ou >= 2.75 and abs(ah) > 0.5 and abs(ah) < 1.0: return "fav_attack"  # 强攻型
    return "balanced"

# 平局先验(由矩阵实测, hold-out前全量估计)
DRAW_PRIOR = {
    ("AH+0.5","u2.25"): 0.322, ("AH-0.5","u2.75"): 0.313,
    ("AH-1p","u2.25"): 0.312, ("AH-0.5","u2.25"): 0.310,
    ("AH-1p","u2.5"): 0.272, ("AH-0.5","u2.5"): 0.263,
    ("AH+0.5","u3.25"): 0.244, ("AH-0.5","u3.5p"): 0.240,
    ("AH-0.5","u3.25"): 0.216, ("AH-1p","u2.75"): 0.214,
    ("AH+0.5","u2.5"): 0.195, ("AH-1p","u3.5p"): 0.165,
    ("AH-1p","u3.25"): 0.157, ("AH+1p","u3.25"): 0.157,
    ("AH+1p","u3.5p"): 0.109,
}
BASE_FALLBACK = 0.23

# 类型先验(hold-out实测, 2026-08-21, n=781; 2.0锚版)
TYPE_DRAW_PRIOR = {
    "grind": 0.354,           # 磨盘型: 半球让+低进球
    "defensive_draw": 0.366,  # 平局温床核心: OU2.0-2.25+势均力敌(全表最高)
    "lowline_trap": 0.332,    # 超低线诱导: OU<2.0(平局也高,但主信号是大球)
    "open": 0.276,            # 对攻型: 高进球+势均力敌
    "balanced": 0.224,        # 均衡
    "fav_attack": 0.192,      # 强攻型: 半球让+高进球
    "blowout": 0.186,         # 碾压型: 深让
}
# 大球先验(5978场实测, 2026-08-21; 2.0锚非单调结构)
TYPE_OVER_PRIOR = {
    "lowline_trap": 0.533,    # 超低线诱导: 大球53%!(低开反弹)
    "defensive_draw": 0.332,  # 平局温床: 大球33%(全表最低)
    "grind": 0.390,
    "open": 0.480,
    "balanced": 0.470,
    "fav_attack": 0.490,
    "blowout": 0.430,
}

def predict_draw(ah, ou, p_d=None, use_type_prior=True):
    """平局概率: 比赛类型先验(6档, 稳健) 或 AH×OU细格子 + p_d微调。

    use_type_prior=True(推荐): 用类型档先验(每档n>=48, 比细格子抗漂移)。
    """
    if use_type_prior:
        t = match_type(ah, ou)
        prior = TYPE_DRAW_PRIOR.get(t, BASE_FALLBACK)
    else:
        key = (ah_b(ah), ou_b(ou))
        prior = DRAW_PRIOR.get(key, BASE_FALLBACK)
    if p_d is not None and 0.18 <= p_d <= 0.42:
        prior = 0.7 * prior + 0.3 * p_d
    return prior

def predict_draw_alert(ah, ou, p_d=None, threshold=0.28):
    """平局预警: 概率≥阈值 → alert。"""
    p = predict_draw(ah, ou, p_d)
    return {"prob": round(p, 3), "alert": p >= threshold,
            "zone": f"{ah_b(ah)}×{ou_b(ou)}", "type": match_type(ah, ou)}


TYPE_CN = {
    "lowline_trap": "超低线陷阱", "defensive_draw": "防守默契", "grind": "磨盘",
    "open": "对攻", "balanced": "均衡", "fav_attack": "强攻", "blowout": "碾压",
    "unknown": "未知",
}


def analyze_match(ah, ou, p_d=None, p_h=None, p_a=None):
    """一键分析: 比赛类型 + 平局/大球方向 + 简明结论(供滚球神器前端)。"""
    t = match_type(ah, ou)
    draw_p = predict_draw(ah, ou, p_d)
    over_p = TYPE_OVER_PRIOR.get(t, 0.43)
    # 大球区修正: 强弱差0.55+ × OU2.75 = 53%(规则3)
    if p_h is not None and p_a is not None and abs(ou - 2.75) <= 0.125 and max(p_h, p_a) >= 0.55:
        over_p = 0.531
    conclusion = []
    if t in ("defensive_draw", "grind"):
        conclusion.append(f"{TYPE_CN[t]}型·平局温床(历史{round(draw_p*100)}%)")
    elif t == "lowline_trap":
        conclusion.append(f"超低线陷阱·庄家低开诱小, 实际大球率53%")
    elif t == "blowout":
        conclusion.append(f"碾压型·平局率低({round(draw_p*100)}%), 分胜负格局")
    else:
        conclusion.append(f"{TYPE_CN[t]}型")
    if over_p >= 0.50:
        conclusion.append(f"大球倾向{round(over_p*100)}%")
    elif over_p <= 0.36:
        conclusion.append(f"小球倾向{round((1-over_p)*100)}%")
    return {
        "type": t, "type_cn": TYPE_CN.get(t, t),
        "draw_prob": round(draw_p, 3), "draw_alert": draw_p >= 0.28,
        "over_prob": round(over_p, 3),
        "conclusion": " · ".join(conclusion),
    }


def main():
    import random
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM rb_matches WHERE ou_line IS NOT NULL AND ah_line IS NOT NULL AND result IS NOT NULL")]
    con.close()
    random.seed(SEED); random.shuffle(rows)
    k = int(len(rows) * 0.7)
    train, test = rows[:k], rows[k:]

    # 在train上重估矩阵(防泄漏; 与全量估计应接近)
    mat = defaultdict(lambda: [0, 0])
    for r in train:
        key = (ah_b(r["ah_line"]), ou_b(r["ou_line"]))
        mat[key][0] += 1
        mat[key][1] += (1 if r["result"] == "D" else 0)
    # train重估的先验表(n>=40才信, 否则回退全量经验值)
    train_prior = {}
    for key, (n, d) in mat.items():
        if n >= 40:
            train_prior[key] = d / n

    # hold-out验证(类型先验由train重估, 防泄漏)
    type_stat = defaultdict(lambda: [0, 0])
    for r in train:
        t = match_type(r["ah_line"], r["ou_line"])
        type_stat[t][0] += 1
        type_stat[t][1] += (1 if r["result"] == "D" else 0)
    train_type_prior = {t: d / n for t, (n, d) in type_stat.items() if n >= 30}
    _saved = dict(TYPE_DRAW_PRIOR)
    TYPE_DRAW_PRIOR.update(train_type_prior)  # 临时换train版

    probs, labels = [], []
    by_type = defaultdict(lambda: [0, 0, 0])
    for r in test:
        p = predict_draw(r["ah_line"], r["ou_line"], r.get("p_d"))
        probs.append(p)
        is_d = 1 if r["result"] == "D" else 0
        labels.append(is_d)
        t = match_type(r["ah_line"], r["ou_line"])
        by_type[t][0] += 1
        by_type[t][1] += is_d
        # 大小验证
        by_type[t][2] += 0

    # AUC
    pairs = sorted(zip(probs, labels), key=lambda x: -x[0])
    pos = sum(labels); neg = len(labels) - pos
    rank = sum(i + 1 for i, (p, l) in enumerate(pairs) if l)
    auc_v = (rank - pos * (pos + 1) / 2) / (pos * neg) if pos and neg else float('nan')

    # 阈值精确率/召回率
    for thr in [0.26, 0.28, 0.30]:
        flagged = [(p, l) for p, l in zip(probs, labels) if p >= thr]
        if flagged:
            tp = sum(l for _, l in flagged)
            prec = tp / len(flagged)
            rec = tp / pos if pos else 0
            print(f"  阈值{thr}: 标记{len(flagged)}场 平局{tp} 精确率={prec:.3f} 召回率={rec:.3f}")

    print("=" * 70)
    print("滚球神器 v2.2(2.0锚) — 类型规则识别器 持有-out回测")
    print("=" * 70)
    print(f"test n={len(test)} 平局基线={pos/len(test):.3f}")
    print(f"AUC = {auc_v:.4f}  (v2.1的p_d: 0.4345)")

    # 大球方向验证(新): 类型先验的over预测
    ov_probs, ov_labels = [], []
    for r in test:
        if r.get("over_ou") is None: continue
        t = match_type(r["ah_line"], r["ou_line"])
        ov_probs.append(TYPE_OVER_PRIOR.get(t, 0.43))
        ov_labels.append(r["over_ou"])
    if ov_probs:
        ov_pairs = sorted(zip(ov_probs, ov_labels), key=lambda x: -x[0])
        opos = sum(ov_labels); oneg = len(ov_labels) - opos
        if opos and oneg:
            orank = sum(i + 1 for i, (p, l) in enumerate(ov_pairs) if l)
            oauc = (orank - opos * (opos + 1) / 2) / (opos * oneg)
            print(f"[大球AUC] = {oauc:.4f}  (类型先验, 2.0锚非单调结构)")

    # 按比赛类型的平局率(用户类型识别的验证)
    print("\n按比赛类型平局率:")
    for t, (n, d, _) in sorted(by_type.items(), key=lambda x: -x[1][1] / max(x[1][0], 1)):
        if n >= 20:
            print(f"  {t:<16} n={n:>4} 平局率={d/n:.3f}")

    # 保存
    model = {
        "version": "rollball_v2.2", "built": "2026-08-21",
        "draw_prior": DRAW_PRIOR, "base": BASE_FALLBACK,
        "auc_holdout": round(auc_v, 4),
        "note": "平局=AH×OU跨市场矩阵(防守+势均力敌=31%平局温床); 类型=对攻/防守默契/磨盘/碾压/强攻",
    }
    with open("analysis/rollball_v22_model.pkl", "wb") as f:
        pickle.dump(model, f)
    print("\n[SAVE] analysis/rollball_v22_model.pkl")

if __name__ == "__main__":
    main()
