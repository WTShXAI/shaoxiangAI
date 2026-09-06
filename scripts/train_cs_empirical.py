# -*- coding: utf-8 -*-
"""用真实赛果训练 CS 关联比分模型 (2026-08-28, 用户: 用真实赛果训练 cs 和有关联模型).

数据: match_outcomes 自带 op_cs(CS 冻结盘) + op_1x2/op_ou/op_ah + 真实赛果 + 半场比分.
      ~6200 场 op_cs 非空 × 赛果非空 —— 完整"盘口结构→真实比分"配对训练集.

训练三层 (K→PhD 分层学习):
  L0 baseline : 直接猜"最便宜波胆" (现状, 实证命中率 ~9.4%)
  L1 实证桶   : 按 CS 最便宜波胆赔率分桶 → 桶内真实比分条件分布 (可解释, 样本充足)
  L2 ML       : sklearn HistGradientBoosting 36 类比分多分类 (全特征: CS+1X2+OU+AH+联赛)

验证: 时间序 holdout (前 70% 训练 / 后 30% 验证, 禁随机划分防泄漏).
输出: models/cs_empirical.joblib + models/cs_empirical_report.txt (命中率/分布).
"""
import json
import os
import sys
import sqlite3
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "events.db")
OUT_MODEL = os.path.join(ROOT, "models", "cs_empirical.joblib")
OUT_REPORT = os.path.join(ROOT, "models", "cs_empirical_report.txt")

MAXG = 6  # 比分截断: 0..6


def parse_score(s):
    """'3-1' / '3:1' → (3,1)"""
    try:
        if isinstance(s, str) and ("-" in s or ":" in s):
            a, b = (int(x) for x in (s.replace(":", "-").split("-")[:2]))
            return a, b
    except Exception:
        pass
    return None


def extract_features(op_cs, op_1x2_h, op_1x2_d, op_1x2_a,
                    op_ou_line, op_ou_over, op_ou_under,
                    op_ah_line, op_ah_home, op_ah_away):
    """盘口结构 → 特征向量 (CS 结构 + 1X2 去水 + OU + AH)."""
    f = {}
    # ── CS 结构 ──
    try:
        grid = json.loads(op_cs) if isinstance(op_cs, str) else op_cs
        if isinstance(grid, list):
            pairs = []
            for item in grid:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    sc, od = item[0], float(item[1])
                    p = parse_score(str(sc))
                    if p and 0 < od <= 1000:
                        pairs.append((p, od))
            pairs.sort(key=lambda x: x[1])  # 按赔率升序 = 便宜在前
            f["cs_n"] = len(pairs)
            f["cs_cheap_odds"] = pairs[0][1] if pairs else None
            f["cs_cheap_home"] = pairs[0][0][0] if pairs else None
            f["cs_cheap_away"] = pairs[0][0][1] if pairs else None
            f["cs_2nd_odds"] = pairs[1][1] if len(pairs) > 1 else None
            # CS 抽水: 隐含概率和
            imp_sum = 0.0
            for _p, _od in pairs[:5]:
                imp_sum += 1.0 / _od
            f["cs_margin5"] = round(imp_sum - 1.0, 4) if pairs else None
            # 最便宜波胆是否 1:0 / 0:1 (低进球)
            f["cs_cheap_low"] = 1 if pairs and (pairs[0][0][0] + pairs[0][0][1]) <= 1 else 0
            f["cs_cheap_home_win"] = 1 if pairs and pairs[0][0][0] > pairs[0][0][1] else 0
        else:
            f = {k: None for k in
                 ["cs_n", "cs_cheap_odds", "cs_cheap_home", "cs_cheap_away",
                  "cs_2nd_odds", "cs_margin5", "cs_cheap_low", "cs_cheap_home_win"]}
    except Exception:
        f = {k: None for k in
             ["cs_n", "cs_cheap_odds", "cs_cheap_home", "cs_cheap_away",
              "cs_2nd_odds", "cs_margin5", "cs_cheap_low", "cs_cheap_home_win"]}
    # ── 1X2 (去水隐含) ──
    try:
        h, d, a = float(op_1x2_h), float(op_1x2_d), float(op_1x2_a)
        if h > 1.01 and d > 1.01 and a > 1.01:
            s = 1 / h + 1 / d + 1 / a
            f["x_h"] = round((1 / h) / s, 4)
            f["x_d"] = round((1 / d) / s, 4)
            f["x_a"] = round((1 / a) / s, 4)
        else:
            f["x_h"] = f["x_d"] = f["x_a"] = None
    except Exception:
        f["x_h"] = f["x_d"] = f["x_a"] = None
    # ── OU ──
    try:
        ov, un = float(op_ou_over), float(op_ou_under)
        ln = float(op_ou_line) if op_ou_line not in (None, "") else 2.5
        if ov > 1.01 and un > 1.01 and ln > 0:
            f["ou_line"] = ln
            f["ou_over_p"] = round((1 / ov) / (1 / ov + 1 / un), 4)
        else:
            f["ou_line"] = f["ou_over_p"] = None
    except Exception:
        f["ou_line"] = f["ou_over_p"] = None
    # ── AH ──
    try:
        hh, aa = float(op_ah_home), float(op_ah_away)
        ln = float(op_ah_line) if op_ah_line not in (None, "") else 0.0
        if hh > 1.01 and aa > 1.01:
            f["ah_line"] = ln
            f["ah_home_p"] = round((1 / hh) / (1 / hh + 1 / aa), 4)
        else:
            f["ah_line"] = f["ah_home_p"] = None
    except Exception:
        f["ah_line"] = f["ah_home_p"] = None
    return f


def main():
    os.makedirs(os.path.join(ROOT, "models"), exist_ok=True)
    con = sqlite3.connect(DB)
    rows = con.execute(
        """SELECT home, away, league, kickoff, score_home, score_away,
                  op_1x2_h, op_1x2_d, op_1x2_a, op_ou_line, op_ou_over, op_ou_under,
                  op_ah_line, op_ah_home, op_ah_away, op_cs, captured_at
           FROM match_outcomes
           WHERE is_valid=1 AND score_home IS NOT NULL AND score_away IS NOT NULL
             AND op_cs IS NOT NULL AND op_cs != ''
           ORDER BY captured_at""").fetchall()
    con.close()
    print(f"op_cs×赛果配对: {len(rows)} 场")

    X, y, meta = [], [], []
    for r in rows:
        home, away, league, kickoff, sh, sa = r[0], r[1], r[2], r[3], r[4], r[5]
        feats = extract_features(r[15], r[6], r[7], r[8], r[9], r[10], r[11],
                                 r[12], r[13], r[14])
        shc, sac = min(sh, MAXG), min(sa, MAXG)
        label = shc * (MAXG + 1) + sac  # 0..48
        # 特征向量 (None → nan, 训练时处理)
        vec = [
            feats.get("cs_n"), feats.get("cs_cheap_odds"), feats.get("cs_cheap_home"),
            feats.get("cs_cheap_away"), feats.get("cs_2nd_odds"), feats.get("cs_margin5"),
            feats.get("cs_cheap_low"), feats.get("cs_cheap_home_win"),
            feats.get("x_h"), feats.get("x_d"), feats.get("x_a"),
            feats.get("ou_line"), feats.get("ou_over_p"),
            feats.get("ah_line"), feats.get("ah_home_p"),
        ]
        X.append(vec)
        y.append(label)
        meta.append((home, away, league, sh, sa, feats.get("cs_cheap_odds"),
                     feats.get("cs_cheap_home"), feats.get("cs_cheap_away")))
    X = np.array(X, dtype=float)
    y = np.array(y, dtype=int)
    print(f"有效特征矩阵: {X.shape}")

    # ── 时间序划分 (前 70% 训练 / 后 30% 验证) ──
    n = len(y)
    split = int(n * 0.7)
    X_tr, y_tr, X_va, y_va = X[:split], y[:split], X[split:], y[split:]
    print(f"训练 {len(y_tr)} / 验证 {len(y_va)}")

    def topk_hit(probs, yt, k):
        hits = 0
        for p, lab in zip(probs, yt):
            top = np.argsort(-p)[:k]
            if lab in top:
                hits += 1
        return hits / len(yt)

    lines = []
    def rep(s):
        print(s)
        lines.append(s)

    rep(f"== 时间序 holdout: 训练 {len(y_tr)} / 验证 {len(y_va)} ==")

    # ── L0 baseline: 直接猜最便宜波胆 (现状) ──
    base_hits_1 = base_hits_3 = 0
    for i, (m, lab) in enumerate(zip(meta[split:], y_va)):
        cheap_h, cheap_a = m[5] or -1, m[6] or -1
        if cheap_h is None or cheap_a is None:
            continue
        pred = min(cheap_h, MAXG) * (MAXG + 1) + min(cheap_a, MAXG)
        if pred == lab:
            base_hits_1 += 1
        # top3: 便宜波胆 + 2nd (若同场)
        base_hits_3 += 1  # 简化: 只算 top1
    rep(f"L0 最便宜波胆 top1: {base_hits_1}/{len(y_va)} = {base_hits_1/len(y_va)*100:.1f}%")

    # ── L1 实证桶: 按最便宜波胆赔率分桶 → 桶内真实比分分布 ──
    buckets = {}
    for i, (vec, lab) in enumerate(zip(X_tr, y_tr)):
        odds = vec[1]  # cs_cheap_odds
        if np.isnan(odds):
            continue
        b = 0 if odds < 1.5 else (1 if odds < 3 else (2 if odds < 6 else 3))
        buckets.setdefault(b, []).append(lab)
    bucket_dist = {}
    for b, labs in buckets.items():
        dist = np.bincount(labs, minlength=(MAXG + 1) ** 2).astype(float)
        dist /= dist.sum()
        bucket_dist[b] = dist
    emp_hits_1 = emp_hits_3 = 0
    for vec, lab in zip(X_va, y_va):
        odds = vec[1]
        if np.isnan(odds) or not bucket_dist:
            continue
        b = 0 if odds < 1.5 else (1 if odds < 3 else (2 if odds < 6 else 3))
        dist = bucket_dist.get(b)
        if dist is None:
            continue
        top1 = int(np.argmax(dist))
        top3 = np.argsort(-dist)[:3]
        if top1 == lab:
            emp_hits_1 += 1
        if lab in top3:
            emp_hits_3 += 1
    rep(f"L1 实证桶 top1: {emp_hits_1}/{len(y_va)} = {emp_hits_1/len(y_va)*100:.1f}% | "
        f"top3: {emp_hits_3}/{len(y_va)} = {emp_hits_3/len(y_va)*100:.1f}%")
    rep(f"   桶分布: " + ", ".join(f"b{k}:n={len(v)}" for k, v in sorted(buckets.items())))

    # ── L2 ML: HistGradientBoosting 多分类 ──
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.impute import SimpleImputer
        imp = SimpleImputer(strategy="median")
        X_tr_i = imp.fit_transform(X_tr)
        X_va_i = imp.transform(X_va)
        clf = HistGradientBoostingClassifier(
            max_iter=200, max_depth=5, learning_rate=0.08,
            min_samples_leaf=20, random_state=42)
        t0 = time.time()
        clf.fit(X_tr_i, y_tr)
        p_va = clf.predict_proba(X_va_i)
        # 对齐类别
        full = np.zeros((len(y_va), (MAXG + 1) ** 2))
        for cidx, cls in enumerate(clf.classes_):
            full[:, cls] = p_va[:, cidx]
        ml_hits_1 = sum(1 for p, lab in zip(full, y_va) if np.argmax(p) == lab)
        ml_hits_3 = sum(1 for p, lab in zip(full, y_va) if lab in np.argsort(-p)[:3])
        rep(f"L2 ML(HistGB) top1: {ml_hits_1}/{len(y_va)} = {ml_hits_1/len(y_va)*100:.1f}% | "
            f"top3: {ml_hits_3}/{len(y_va)} = {ml_hits_3/len(y_va)*100:.1f}% | "
            f"训练耗时 {time.time()-t0:.1f}s")
        # 保存模型 + imputer + 桶
        import joblib
        payload = {"bucket_dist": bucket_dist, "clf": clf, "imputer": imp,
                   "classes": clf.classes_, "maxg": MAXG, "trained_at": time.time()}
        joblib.dump(payload, OUT_MODEL)
        rep(f"模型已存: {OUT_MODEL}")
    except Exception as e:
        rep(f"L2 ML 失败: {e}")
        import joblib
        payload = {"bucket_dist": bucket_dist, "clf": None, "imputer": None,
                   "classes": None, "maxg": MAXG, "trained_at": time.time()}
        joblib.dump(payload, OUT_MODEL)

    # 保存报告
    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
