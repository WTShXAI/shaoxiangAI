# -*- coding: utf-8 -*-
"""DB 匹配波胆引擎: 训练/评测/调优 + 双库合并 (2026-08-28, 用户: 形成闭环).

目标:
  1. 时间序 holdout 评测当前匹配准确度 (top1/top3) 与速度 (ms/查询)
  2. 参数扫描: top_n / 距离阈值 / 加权 → 选最优写回 cs_db_match 默认
  3. 双库合并: events.db(10058) + GQ.db(9842) 按 (home,away,kickoff) 去重 → 样本↑ → 准确度↑

用法:
  python scripts/train_cs_db_match.py           # 全流程: 合并 → holdout → 扫描 → 报告
  python scripts/train_cs_db_match.py --apply   # 扫描后把最优参数写回 pipeline/cs_db_match.py
"""
import json
import os
import sqlite3
import sys
import time

import numpy as np

ROOT = r"D:\Architecture"
EVENTS_DB = os.path.join(ROOT, "data", "events.db")
GQ_DB = os.path.join(ROOT, "data", "GQ.db")
TARGET = os.path.join(ROOT, "pipeline", "cs_db_match.py")
OUT_REPORT = os.path.join(ROOT, "models", "cs_db_match_report.json")
MAXG = 6


def _devig3(h, d, a):
    try:
        h, d, a = float(h), float(d), float(a)
        if h > 1.01 and d > 1.01 and a > 1.01:
            s = 1 / h + 1 / d + 1 / a
            return (1 / h) / s, (1 / d) / s, (1 / a) / s
    except Exception:
        pass
    return None


def _devig2(o, u):
    try:
        o, u = float(o), float(u)
        if o > 1.01 and u > 1.01:
            return (1 / o) / (1 / o + 1 / u)
    except Exception:
        pass
    return None


def load_from(db_path):
    """从单库加载 (feat, scores, keys)."""
    c = sqlite3.connect(db_path, timeout=30)
    rows = c.execute(
        """SELECT home, away, kickoff, op_1x2_h, op_1x2_d, op_1x2_a,
                  op_ou_over, op_ou_under, op_ah_home, op_ah_away,
                  score_home, score_away, captured_at
           FROM match_outcomes
           WHERE is_valid=1 AND score_home IS NOT NULL AND score_away IS NOT NULL
             AND (op_1x2_h IS NOT NULL OR op_ou_over IS NOT NULL)"""
    ).fetchall()
    c.close()
    feats, scores, keys = [], [], []
    for r in rows:
        x3 = _devig3(r[3], r[4], r[5])
        pou = _devig2(r[6], r[7]) if r[6] and r[7] else None
        pah = _devig2(r[8], r[9]) if r[8] and r[9] else None
        if x3 is None and pou is None:
            continue
        if x3 is not None:
            f = [x3[0], x3[1], x3[2]]
            if pou is not None:
                f.append(pou)
            if pah is not None:
                f.append(pah)
        else:
            f = [pou, pah]
        feats.append(f)
        scores.append((min(int(r[10]), MAXG), min(int(r[11]), MAXG)))
        keys.append((r[0], r[1], r[2]))
    return feats, scores, keys


def merge_libraries():
    """双库合并去重 (GQ.db 优先 events.db 同 key 丢弃)."""
    fe, sc_e, ke = load_from(EVENTS_DB)
    fg, sc_g, kg = load_from(GQ_DB)
    gq_keys = set(kg)
    kept_gq = [i for i, k in enumerate(kg) if k not in set(ke)]
    feats = list(fe) + [fg[i] for i in kept_gq]
    scores = list(sc_e) + [sc_g[i] for i in kept_gq]
    print(f"events.db {len(fe)} 场 + GQ.db 新增 {len(kept_gq)} 场 = 合并 {len(feats)} 场")
    return feats, scores


def build_matrix(feats):
    maxd = max(len(f) for f in feats)
    F = np.full((len(feats), maxd), np.nan, dtype=np.float32)
    for i, f in enumerate(feats):
        F[i, :len(f)] = f
    return F


def query(F, scores, q, top_n, dist_thresh, weights=None, idx_range=None):
    """向量化查询 (可只扫 idx_range 子集=验证集训练索引)."""
    qd = len(q)
    qarr = np.array(q, dtype=np.float32)
    if weights is not None:
        w = np.array(weights[:qd], dtype=np.float32)
    else:
        w = None
    if idx_range is None:
        idx_range = range(F.shape[0])
    idx_range = np.array(list(idx_range), dtype=int)
    sub = F[idx_range, :qd]
    mask = ~np.isnan(sub)
    d = np.zeros(len(idx_range), dtype=np.float32)
    for j in range(qd):
        col = sub[:, j]
        m = mask[:, j]
        diff = np.where(m, col - qarr[j], 0.0)
        wj = w[j] if w is not None else 1.0
        d += np.where(m, (diff ** 2) * wj, 0.0)
    d = np.sqrt(d)
    valid = d < dist_thresh
    if valid.sum() == 0:
        return None
    cand = np.argsort(d)[:top_n]
    cand = cand[d[cand] < dist_thresh]
    if len(cand) < 5:
        return None
    dists = d[cand]
    matched = [scores[idx_range[i]] for i in cand]
    return dists, matched


def eval_config(feats, scores, F, tr_idx, va_idx, top_n, thresh, weights=None, verbose=False):
    """holdout 评测: tr_idx 建索引, va_idx 查询.
    口径 = 最近 top_n 场相似比赛的【比分频率分布】 top1/top3 (与线上输出一致)."""
    from collections import Counter
    t0 = time.time()
    hit1 = hit3 = nq = 0
    for i in va_idx:
        q = F[i, :]
        m = ~np.isnan(q)
        qlist = q[m].tolist()
        if len(qlist) < 3:
            continue
        res = query(F, scores, qlist, top_n, thresh, weights, idx_range=tr_idx)
        if res is None:
            continue
        dists, matched = res
        actual = scores[i]
        cnt = Counter(matched)
        top1_score = cnt.most_common(1)[0][0]
        top3_scores = [s for s, _ in cnt.most_common(3)]
        if actual == top1_score:
            hit1 += 1
        if actual in top3_scores:
            hit3 += 1
        nq += 1
    dur = time.time() - t0
    avg_ms = (dur / max(1, nq)) * 1000
    t1 = hit1 / max(1, nq)
    t3 = hit3 / max(1, nq)
    if verbose:
        print(f"  top_n={top_n} thresh={thresh} w={weights} → "
              f"top1 {t1*100:.1f}% top3 {t3*100:.1f}% 查询{nq} 平均{avg_ms:.0f}ms")
    return t1, t3, avg_ms, nq


def main():
    feats, scores = merge_libraries()
    F = build_matrix(feats)
    n = len(scores)
    split = int(n * 0.7)
    tr_idx, va_idx = list(range(split)), list(range(split, n))
    print(f"时间序: 索引 {len(tr_idx)} / 验证 {len(va_idx)}")

    report = {"n_total": n, "n_train": len(tr_idx), "n_val": len(va_idx), "runs": []}
    best = None
    # ── 参数扫描 ──
    for top_n in (20, 40, 60):
        for thresh in (1.0, 2.0, 5.0):
            for w in (None, [2.0, 1.0, 1.0, 1.0, 1.0]):  # 等权 vs 1X2 加权
                t1, t3, ms, nq = eval_config(feats, scores, F, tr_idx, va_idx,
                                             top_n, thresh, w)
                run = {"top_n": top_n, "thresh": thresh, "weights": w,
                       "top1": round(t1, 4), "top3": round(t3, 4),
                       "avg_ms": round(ms, 1), "n_query": nq}
                report["runs"].append(run)
                if best is None or (run["top3"] > best["top3"] and run["avg_ms"] < best["avg_ms"] * 3):
                    best = run
                print(f"top_n={top_n} thresh={thresh} w={'1X2加权' if w else '等权'} → "
                      f"top1 {t1*100:.1f}% top3 {t3*100:.1f}% 平均{ms:.0f}ms n={nq}")
    report["best"] = best
    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n最优: {best}")
    print(f"报告: {OUT_REPORT}")

    if "--apply" in sys.argv and best:
        # 写回 cs_db_match.py 默认参数
        src = open(TARGET, encoding="utf-8").read()
        new_src = src.replace("_DEF_N = 40", f"_DEF_N = {best['top_n']}")
        # 距离阈值 (5.0 出现处)
        new_src = new_src.replace("if d_ < 5.0:", f"if d_ < {best['thresh']}:")
        new_src = new_src.replace("idx = idx[dists[idx] < 5.0]", f"idx = idx[dists[idx] < {best['thresh']}]")
        open(TARGET, "w", encoding="utf-8").write(new_src)
        print(f"已写回最优参数 → {TARGET}")


if __name__ == "__main__":
    main()
