# -*- coding: utf-8 -*-
"""三盘(让球/大小球/胜平负)结合 → DB 历史匹配 → 真实波胆 (2026-08-28, 用户设计).

用户口径: "让球、大小球、胜平负结合, 从数据库中匹配波胆, 不是预测波胆" —
  用当前比赛的 1X2/OU/AH 结构做检索键, 从 match_outcomes 找"结构最相似"的历史比赛,
  统计它们的真实比分分布 → 推荐波胆. 可解释、实证、无过拟合 (检索而非模型拟合).

数据: events.db match_outcomes (10058 场, 有 op_1x2/op_ou/op_ah + 真实赛果)
  - 双盘(1X2+OU): 7678 场 | 三盘全: 6051 场 | 至少两盘: 8083 场
  特征维度按可用盘动态:
    三盘: [ph, pd, pa, P(over@ou_line), P(home_ah@ah_line)] 5 维
    缺 AH: [ph, pd, pa, P(over)] 4 维
    缺 OU: [ph, pd, pa, P(home_ah)] 4 维
    仅 1X2: [ph, pd, pa] 3 维

匹配: 欧氏距离最近 N 场 (默认 40) → 真实比分频率分布 → top5 波胆 + 相似度质量.
缓存: 索引 5 分钟 TTL (10058 场构建 <1s).
"""
import sqlite3
import threading
import time

import numpy as np

ROOT_DB = r"D:\Architecture\data\events.db"

_MAXG = 6  # 比分截断
_DEF_N = 60
_TTL = 300.0
_LOCK = threading.Lock()
_LIB = None
_LIB_TS = 0.0


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


def _load_library(force=False):
    """加载 match_outcomes 三盘结构 + 真实比分 到内存矩阵 (双库合并去重)."""
    global _LIB, _LIB_TS
    now = time.time()
    if _LIB is not None and not force and (now - _LIB_TS) < _TTL:
        return _LIB
    with _LOCK:
        if _LIB is not None and not force and (now - _LIB_TS) < _TTL:
            return _LIB
        t0 = time.time()
        SQL = """SELECT home, away, kickoff, op_1x2_h, op_1x2_d, op_1x2_a,
                        op_ou_over, op_ou_under, op_ah_home, op_ah_away,
                        score_home, score_away
                 FROM match_outcomes
                 WHERE is_valid=1 AND score_home IS NOT NULL AND score_away IS NOT NULL
                   AND (op_1x2_h IS NOT NULL OR op_ou_over IS NOT NULL)"""
        rows = []
        try:
            c = sqlite3.connect(ROOT_DB, timeout=30)
            rows += c.execute(SQL).fetchall()
            c.close()
        except Exception:
            pass
        try:
            # 2026-08-28 闭环: GQ.db 自动并入 (同结构; 当前完全重叠 0 新增, 未来自动扩样本)
            gq_c = sqlite3.connect(r"D:\Architecture\data\GQ.db", timeout=30)
            gq_rows = gq_c.execute(SQL).fetchall()
            gq_c.close()
            seen_ev = {(r[0], r[1], r[2]) for r in rows}
            for r in gq_rows:
                if (r[0], r[1], r[2]) not in seen_ev:
                    rows.append(r)
                    seen_ev.add((r[0], r[1], r[2]))
        except Exception:
            pass
        feats, scores, n_disc = [], [], 0
        for r in rows:
            x3 = _devig3(r[3], r[4], r[5])
            pou = _devig2(r[6], r[7]) if r[6] and r[7] else None
            pah = _devig2(r[8], r[9]) if r[8] and r[9] else None
            if x3 is None and pou is None:
                continue
            # 维度: 3(1X2) / 4(+OU 或 +AH) / 5(全)
            if x3 is not None:
                if pou is not None and pah is not None:
                    f = [x3[0], x3[1], x3[2], pou, pah]
                elif pou is not None:
                    f = [x3[0], x3[1], x3[2], pou]
                elif pah is not None:
                    f = [x3[0], x3[1], x3[2], pah]
                else:
                    f = [x3[0], x3[1], x3[2]]
            else:  # 无 1X2 但有 OU+AH (退化, 罕见)
                f = [pou, pah]
                n_disc += 1
            feats.append(f)
            sh, sa = min(int(r[10]), _MAXG), min(int(r[11]), _MAXG)
            scores.append((sh, sa))
        # 统一维度: 填 NaN, 查询时按 NaN 维度排除 (带掩码)
        maxd = max(len(f) for f in feats)
        F = np.full((len(feats), maxd), np.nan, dtype=np.float32)
        for i, f in enumerate(feats):
            F[i, :len(f)] = f
        _LIB = {
            "feat": F, "scores": scores, "n": len(scores),
            "n_discard_1x2less": n_disc,
            "maxd": maxd, "loaded_at": t0, "load_sec": round(time.time() - t0, 2),
        }
        _LIB_TS = time.time()
        return _LIB


def db_match_scoreline(h=None, d=None, a=None, ou_line=None, ou_over=None, ou_under=None,
                       ah_line=None, ah_home=None, ah_away=None, top_n=_DEF_N,
                       max_goal=_MAXG):
    """三盘结构 → DB 匹配 → 真实波胆分布.

    返回 {top5, n_matched, mean_dist, min_dist, basis, found} 或 None.
    """
    lib = _load_library()
    if not lib or lib["n"] < 20:
        return None
    # 查询特征 (与库同规则)
    x3 = _devig3(h, d, a)
    pou = _devig2(ou_over, ou_under) if ou_over and ou_under else None
    pah = _devig2(ah_home, ah_away) if ah_home and ah_away else None
    if x3 is None and pou is None:
        return None
    q = []
    if x3 is not None:
        q += [x3[0], x3[1], x3[2]]
        if pou is not None:
            q.append(pou)
        if pah is not None:
            q.append(pah)
    else:
        q += [pou, pah]
    qd = len(q)
    # 2026-08-28 向量化 (原逐行 Python 循环 8533 场 ~20ms → ~0.5ms, 训练脚本实测)
    F = lib["feat"]
    sub = F[:, :qd]
    qarr = np.array(q, dtype=np.float32)
    mask = ~np.isnan(sub)
    d2 = np.zeros(sub.shape[0], dtype=np.float32)
    for j in range(qd):
        col = sub[:, j]
        m = mask[:, j]
        diff = np.where(m, col - qarr[j], 0.0)
        d2 += np.where(m, diff * diff, 0.0)
    dists = np.sqrt(d2)
    valid = dists < 1.0   # 距离>=1.0 视为完全不同盘 (2026-08-28 训练调优: thresh=1.0)
    dists[~valid] = np.inf
    idx = np.argsort(dists)[:top_n]
    idx = idx[np.isfinite(dists[idx])]
    if len(idx) < 5:
        return None
    n = len(idx)
    mean_d = float(dists[idx].mean())
    # 真实比分分布
    dist = {}
    for i in idx:
        sh, sa = lib["scores"][i]
        key = f"{sh}:{sa}"
        dist[key] = dist.get(key, 0.0) + 1.0
    tot = sum(dist.values())
    dist = {k: v / tot for k, v in dist.items()}
    ranked = sorted(dist.items(), key=lambda x: -x[1])
    top5 = [{"score": k, "prob": round(v, 4), "n": int(round(v * tot))} for k, v in ranked[:5]]
    top1 = sum(1 for i in idx if f"{lib['scores'][i][0]}:{lib['scores'][i][1]}" == ranked[0][0])
    top3 = sum(1 for i in idx if f"{lib['scores'][i][0]}:{lib['scores'][i][1]}" in
               {k for k, _ in ranked[:3]})
    basis = (f"DB 匹配 {n} 场历史(三盘结构 1X2/OU/AH 去水, 欧氏距离均 {mean_d:.3f}), "
             f"真实比分 top1 命中 {top1}/{n} ({top1/n*100:.0f}%) top3 {top3}/{n} ({top3/n*100:.0f}%)")
    return {
        "top5": top5,
        "score": top5[0]["score"] if top5 else None,
        "n_matched": n,
        "mean_dist": round(mean_d, 4),
        "top1_hit": round(top1 / n, 4),
        "top3_hit": round(top3 / n, 4),
        "basis": basis,
        "found": True,
    }


def clear_cache():
    global _LIB, _LIB_TS
    with _LOCK:
        _LIB = None
        _LIB_TS = 0.0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, r"D:\Architecture")
    lib = _load_library(force=True)
    print(f"索引: {lib['n']} 场 (载入 {lib['load_sec']}s)")
    # 测试: 1X2 1.22/5.1/9.4 (米克斯克 主胜极强) + OU 2.5 + AH 0
    r = db_match_scoreline(h=1.22, d=5.1, a=9.4, ou_line=2.5, ou_over=1.85, ou_under=1.95,
                           ah_line=0.0, ah_home=1.9, ah_away=1.9)
    if r:
        print(f"匹配 {r['n_matched']} 场, 均距 {r['mean_dist']}")
        for t in r['top5']:
            print(f"  {t['score']}  {t['prob']*100:.1f}%  (n={t['n']})")
        print(f"top1命中 {r['top1_hit']*100:.0f}% / top3 {r['top3_hit']*100:.0f}%")
        print(f"basis: {r['basis']}")
