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
                        score_home, score_away, mid
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
        feats, scores, mids, n_disc = [], [], [], 0
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
            mids.append(r[12] if len(r) > 12 else None)
            sh, sa = min(int(r[10]), _MAXG), min(int(r[11]), _MAXG)
            scores.append((sh, sa))
        # 统一维度: 填 NaN, 查询时按 NaN 维度排除 (带掩码)
        maxd = max(len(f) for f in feats)
        F = np.full((len(feats), maxd), np.nan, dtype=np.float32)
        for i, f in enumerate(feats):
            F[i, :len(f)] = f
        _LIB = {
            "feat": F, "scores": scores, "mids": mids, "n": len(scores),
            "n_discard_1x2less": n_disc,
            "maxd": maxd, "loaded_at": t0, "load_sec": round(time.time() - t0, 2),
        }
        _LIB_TS = time.time()
        return _LIB


# OU/AH 维距离权重 (2026-09-10): 保持等权 1.0。
# 实验记录(09月 149 场): 各权重方案差异均在 ~1 个标准误内(±3.7pp), 不过拟合;
# 关键修复是维数错位与 NaN 掩码两个真 bug(见上)。权重接口保留供后续大样本调档。
_DIM_W_OU = 1.0
_DIM_W_AH = 1.0


def db_match_scoreline(h=None, d=None, a=None, ou_line=None, ou_over=None, ou_under=None,
                       ah_line=None, ah_home=None, ah_away=None, top_n=_DEF_N,
                       max_goal=_MAXG, exclude_mid=None):
    """三盘结构 → DB 匹配 → 真实波胆分布.

    返回 {top5, n_matched, mean_dist, min_dist, basis, found} 或 None.
    """
    lib = _load_library()
    if not lib or lib["n"] < 20:
        return None
    # 查询特征 (与库同规则)
    # 2026-09-10 维数错位修复: 旧代码按"有哪些盘"拼查询向量, 有 AH 没 OU 时
    # pah 会错位到库的 pou 列 (pah↔pou 比较 = 纯噪声, 实测 TOP1 33.6%→18.8%)。
    # 现恒定 5 维槽位 [ph, pd, pa, pou, pah], 缺失盘口填 NaN → 掩码自动跳过。
    x3 = _devig3(h, d, a)
    pou = _devig2(ou_over, ou_under) if ou_over and ou_under else None
    pah = _devig2(ah_home, ah_away) if ah_home and ah_away else None
    if x3 is None and pou is None and pah is None:
        return None
    q = [float('nan')] * 5
    if x3 is not None:
        q[0], q[1], q[2] = x3[0], x3[1], x3[2]
    if pou is not None:
        q[3] = pou
    if pah is not None:
        q[4] = pah
    qd = 5
    # 2026-08-28 向量化 (原逐行 Python 循环 8533 场 ~20ms → ~0.5ms, 训练脚本实测)
    # 2026-09-10 维度权重 (链路优化实测): 5 维等权时 OU/AH 维过度收窄匹配池,
    #   TOP1 18.8% vs 仅1X2 30.9% (09月 149 场) → OU/AH 维降权为软约束,
    #   _DIM_W_OUAH 可调 (0=等价仅1X2, 1=等权5维)。
    F = lib["feat"]
    sub = F[:, :qd]
    qarr = np.array(q, dtype=np.float32)
    # 2026-09-10 NaN 掩码修复: 缺失维填 0 且从掩码剔除 — 原掩码只查库侧 NaN,
    # 查询 NaN × 库有效列 = NaN 毒化整条距离 → 该场匹配静默全灭 (审计发现)
    q_nan = np.isnan(qarr)
    qarr = np.where(q_nan, np.float32(0.0), qarr)
    mask = ~np.isnan(sub) & (~q_nan[None, :])
    wvec = np.array([1.0, 1.0, 1.0, _DIM_W_OU, _DIM_W_AH], dtype=np.float32)
    d2 = np.zeros(sub.shape[0], dtype=np.float32)
    for j in range(qd):
        col = sub[:, j]
        m = mask[:, j]
        diff = np.where(m, col - qarr[j], 0.0) * wvec[j]
        d2 += np.where(m, diff * diff, 0.0)
    dists = np.sqrt(d2)
    valid = dists < 1.0   # 距离>=1.0 视为完全不同盘 (2026-08-28 训练调优: thresh=1.0)
    if exclude_mid:
        # 2026-09-09 自身排除: 检索库含查询场自身(距离0)会泄漏真实比分到 top 邻居
        mks = lib.get('mids')
        if mks is not None:
            excl = np.array([str(m) == str(exclude_mid) for m in mks])
            valid = valid & (~excl)
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


# ═══════════════════════════════════════════════════════════════════════════
# 统一 CS 推荐 (2026-08-30 SSoT, 治"前端比分分歧")
#
# 评估结论 (5798 场干净赛前样本 + 48 场滚盘 HT 重放, 详见当日会话):
#   赛前:  DB三盘匹配 top1 15.8%/top3 55.3%  >  0.6DB+0.4结构(13.7/34.8)  >  结构λμ(11.7/30.2)
#   滚盘:  DB+比分过滤 top1 12.5% 最佳(过滤后候选不足拖累 top3);
#          cross_score 条件化 top3 35.4% 最佳(平移分布提供近比分候选)。
# 统一 = DB 核心 + 滚球比分过滤 + 平移补位(候选不足时以当前比分平移 DB 分布补齐 top3)。
# 赛前/滚盘单一真相源: 合理比分卡主推 / CS信任卡DB栏 / 终场读数回退 全部消费本函数。
# ═══════════════════════════════════════════════════════════════════════════
def _unified_impl(h=None, d=None, a=None, ou_line=None, ou_over=None, ou_under=None,
                      ah_line=None, ah_home=None, ah_away=None,
                      current_score='', current_minute=0, top_n=_DEF_N, ou_hint=None):
    """统一波胆推荐 (SSoT)。赛前 = DB三盘匹配 top5; 滚球 = 过滤低于当前比分 + 平移补位。

    ou_hint: (line, direction) 可选 — 全场破蛋卡(probe full)的 OU 判定, 仅 live_odds
    数据源有效。应用为软约束: 与 OU 方向矛盾的总球降权 ×0.15(不置零, 保留尾部),
    保证 CS 推荐与 OU 推荐不互相矛盾(实测 阿尔德什尔 77' 1-1: OU 推大4.25 需总球≥5,
    CS 却推 3-1 总球4 — 买大4.25 必输的自相矛盾)。

    返回 {found, mode('pre'|'roll'), top5[{score('i-j'), prob, n}], n_matched, mean_dist,
          basis, live_filter} 或 None。比分 key 统一 '-' 格式。"""
    m = db_match_scoreline(h=h, d=d, a=a, ou_line=ou_line, ou_over=ou_over, ou_under=ou_under,
                           ah_line=ah_line, ah_home=ah_home, ah_away=ah_away, top_n=top_n)
    if not m or not m.get('found'):
        return None
    top5 = [{'score': t['score'].replace(':', '-'), 'prob': t['prob'], 'n': t.get('n')}
            for t in m['top5']]

    sh = sa = None
    minute = int(current_minute or 0)
    for _sep in ('-', ':'):
        if current_score and _sep in str(current_score):
            try:
                _h, _a = str(current_score).split(_sep, 1)
                sh, sa = int(_h), int(_a)
            except Exception:
                sh = sa = None
            break

    # 赛前模式: 原样返回
    if sh is None or minute <= 0:
        return {**{k: v for k, v in m.items() if k != 'top5'},
                'mode': 'pre', 'top5': top5, 'score': top5[0]['score'] if top5 else None,
                'basis': 'SSoT·赛前: ' + m['basis']}

    # 滚球模式: 过滤不可能比分
    kept = [t for t in top5
            if int(t['score'].split('-')[0]) >= sh and int(t['score'].split('-')[1]) >= sa]
    live_filter = f"已过滤低于当前比分 {sh}-{sa} 的候选"
    if len(kept) >= 3:
        return {**{k: v for k, v in m.items() if k != 'top5'},
                'mode': 'roll', 'top5': kept, 'score': kept[0]['score'],
                'live_filter': live_filter,
                'basis': f"SSoT·滚球: DB三盘匹配 {m['n_matched']} 场(均距 {m['mean_dist']}) 真实波胆, {live_filter}"}
    # 平移补位: 以当前比分为基的邻近候选(当前比分/+1球变体, 按剩余时间衰减)填满 top3。
    # 不把 DB 比分直接叠加当前比分(会过度加球); DB 分布此时主要贡献方向背景。
    _rem = max(0.1, 1.0 - minute / 100.0)
    pads = [(f'{sh}-{sa}', 0.6), (f'{sh+1}-{sa}', 0.25 * _rem * 2), (f'{sh}-{sa+1}', 0.2 * _rem * 2)]
    merged = {t['score']: t['prob'] for t in kept}
    for k, w in pads:
        if k not in merged:
            merged[k] = w * max(0.05, (top5[0]['prob'] if top5 else 0.1))
    ranked = sorted(merged.items(), key=lambda x: -x[1])[:5]
    out5 = [{'score': s, 'prob': round(p, 4)} for s, p in ranked]
    return {**{k: v for k, v in m.items() if k != 'top5'},
            'mode': 'roll', 'top5': out5, 'score': out5[0]['score'] if out5 else None,
            'live_filter': live_filter + ' + 平移补位(DB分布⊕当前比分)',
            'basis': f"SSoT·滚球: DB匹配 {m['n_matched']} 场, {live_filter}; 候选不足→当前比分平移补位"}


def unified_scoreline(h=None, d=None, a=None, ou_line=None, ou_over=None, ou_under=None,
                      ah_line=None, ah_home=None, ah_away=None,
                      current_score='', current_minute=0, top_n=_DEF_N, ou_hint=None,
                      winner_hint=None):
    """SSoT 统一波胆推荐 (外层包装: OU 软约束 → winner 方向对齐 → 方向仲裁末级)。

    2026-09-10 固定管线: impl → OU 软约束 → winner_hint 对齐 → arbitrate 仲裁。
    仲裁必须最后: 保证输出的 direction 与 top1 比分类别恒一致, "方向卡 主胜 vs
    首选比分 0-0(平)"这类串联矛盾从结构上不可能出现。
    winner_hint: 'home'|'draw'|'away' — 滚球 lead-prior 方向(76%+ 实证)传入后,
    非该类候选 ×0.15 软降权且 direction 强制对齐, 消灭滚球态方向卡与首选比分
    的类别分歧(链路审计 CHK1)。"""
    out = _unified_impl(h=h, d=d, a=a, ou_line=ou_line, ou_over=ou_over, ou_under=ou_under,
                        ah_line=ah_line, ah_home=ah_home, ah_away=ah_away,
                        current_score=current_score, current_minute=current_minute, top_n=top_n)
    if not out or not out.get('found'):
        return out
    force = None
    if ou_hint or winner_hint:
        out, force = _apply_constraints_stage(out, current_score, ou_hint, winner_hint)
    return _finalize_arbitrate(out, force_dir=force)


def _apply_constraints_stage(out, current_score, ou_hint, winner_hint):
    """OU + winner 双约束联合求解 (2026-09-10, 取代分级降权).

    分级降权的缺陷(审计实测): OU 段先降权违规者, winner 段再把方向外候选 ×0.15 —
    两个 ×0.15 复合后, "OU 合规但方向外"候选反而输给 "OU 违规但方向内"候选,
    且 OU 补位条件(全部 top3 违规)在混合 top3 下永不触发。

    联合求解: 每个候选一次乘齐两组惩罚
        p' = p × (方向外?0.15:1) × (违反OU?0.15:1)
    再做方向感知补位(need~need+2 全拆分, 方向合规拆分免罚), 排序后一次性生效。
    返回 (out, force_dir) — force_dir 供末级仲裁锁定方向。
    """
    try:
        if not out or not out.get('found'):
            return out, None
        top5 = out.get('top5') or []
        if not top5:
            return out, None
        try:
            _h, _a = str(current_score).replace(':', '-').split('-')
            sh, sa = int(_h), int(_a)
        except Exception:
            sh = sa = None
        import math as _math
        if winner_hint in ('home', 'draw', 'away'):
            wf = lambda cls: 1.0 if cls == winner_hint else 0.15
        else:
            wf = None
        ou_dir = ou_ifloor = None
        if ou_hint:
            try:
                ou_dir = str(ou_hint[1]).upper()
                _line = float(ou_hint[0])
                if ou_dir in ('OVER', 'UNDER') and 0.5 <= _line <= 10.0:
                    ou_ifloor = _math.floor(_line + 1e-9)
                else:
                    ou_dir = None
            except Exception:
                ou_dir = None

        def ou_bad(tot):
            return (ou_dir == 'OVER' and tot <= ou_ifloor) or (ou_dir == 'UNDER' and tot > ou_ifloor)

        demoted_ou, adj = [], []
        _need = 1   # 方向代表合成的默认加球量(OU 补位块内会被覆盖)
        for t in top5:
            p = float(t.get('prob') or 0.0)
            cls = _outcome_of(t.get('score'))
            try:
                tot = int(t['score'].split('-')[0]) + int(t['score'].split('-')[1])
            except Exception:
                tot = None
            f = 1.0
            if wf and wf(cls) < 1.0:
                f *= 0.15
            if ou_dir and tot is not None and ou_bad(tot):
                f *= 0.15
                demoted_ou.append(t['score'])
            adj.append({'score': t['score'], 'prob': round(p * f, 6), '_p': p})
        if not demoted_ou and not wf:
            return out, None
        # ---- 方向感知补位: OU 合规候选权重过低时, 生成 need~need+2 全拆分 ----
        if ou_dir == 'OVER' and sh is not None:
            _target = ou_ifloor + 1
            _need = max(0, _target - (sh + sa))
            # 触发口径 = 同时满足 OU + 方向两约束 (只满足 OU 不够 — winner 段会再压它)
            best_sat = max([t['prob'] for t in adj
                            if (not ou_bad(int(t['score'].split('-')[0]) + int(t['score'].split('-')[1])))
                            and (not wf or _outcome_of(t['score']) == winner_hint)] or [0.0])
            best_any = max([t.get('_p', t['prob']) for t in adj] or [0.0])
            if _need > 0 and best_sat < 0.30 * best_any:
                _ref_p = max([t.get('_p', 0.0) for t in adj] or [0.05])
                _wsum = _hsum = 0.0
                for t in top5:
                    try:
                        mh, ma = (int(x) for x in t['score'].split('-'))
                        _wsum += (mh + ma) * float(t.get('prob') or 0.0)
                        _hsum += mh * float(t.get('prob') or 0.0)
                    except Exception:
                        continue
                h_share = (_hsum / _wsum) if _wsum > 1e-9 else 0.5
                added = {}
                for _g in range(_need, _need + 3):
                    for extra_h in range(_g + 1):
                        extra_a = _g - extra_h
                        key = f'{min(sh + extra_h, 9)}-{min(sa + extra_a, 9)}'
                        w = 0.5 + (h_share - 0.5) * (extra_h - extra_a) / max(1, _g)
                        wgt = max(0.05, w * _ref_p)
                        if winner_hint and _outcome_of(key) != winner_hint:
                            wgt *= 0.15
                        if wgt > added.get(key, 0):
                            added[key] = wgt
                merged = {t['score']: t['prob'] for t in adj}
                for k, p in added.items():
                    if merged.get(k, 0) < p:
                        merged[k] = p
                adj = [{'score': k, 'prob': round(p, 6)} for k, p in merged.items()]
        # 方向代表候选合成 (2026-09-10): winner 类在池中缺席时补一个该类代表 —
        # 否则 force 分区无从落地, 方向卡与首选比分再次矛盾 (审计 CHK1 实测 2 例)。
        if (winner_hint and sh is not None
                and not any(_outcome_of(t['score']) == winner_hint for t in adj)):
            _ref_p2 = max([t.get('_p', 0.0) for t in adj] or [0.05])
            if winner_hint == 'draw':
                key = f'{sh}-{sa}' if sh == sa else f'{min(sh + 1, 9)}-{min(sa + 1, 9)}'
                wgt = 0.8 * _ref_p2
            elif winner_hint == 'home':
                key = f'{min(sh + max(1, _need), 9)}-{sa}'
                wgt = 0.6 * _ref_p2
            else:
                key = f'{sh}-{min(sa + max(1, _need), 9)}'
                wgt = 0.6 * _ref_p2
            adj.append({'score': key, 'prob': round(wgt, 6)})
        adj.sort(key=lambda t: -float(t.get('prob') or 0.0))
        out = dict(out)
        out['top5'] = [{'score': t['score'], 'prob': t['prob']} for t in adj[:5]]
        out['score'] = out['top5'][0]['score']
        force = None
        if winner_hint in ('home', 'draw', 'away') and wf:
            tot = sum(float(t.get('prob') or 0.0) for t in out['top5']) or 1.0
            fm = sum(float(t.get('prob') or 0.0) for t in out['top5']
                     if _outcome_of(t.get('score')) == winner_hint) / tot
            force = winner_hint
            _wlabel = {'home': '主胜', 'draw': '平', 'away': '客胜'}[winner_hint]
            out['direction'] = {'winner': winner_hint, 'label': _wlabel, 'prob': round(fm, 3),
                                'basis': '领先方先验⊕即时盘(方向对齐池重排)'}
            out['winner_align'] = f"已对齐方向{_wlabel}: 非该类候选联合降权"
            out['basis'] = (out.get('basis') or '') + '; ' + out['winner_align']
        if demoted_ou:
            out['ou_align'] = (f"已对齐 OU 推荐 {'大' if ou_dir == 'OVER' else '小'}"
                               f"{float(ou_hint[0]):g}: 矛盾总球降权({','.join(demoted_ou)})")
            out['basis'] = (out.get('basis') or '') + '; ' + out['ou_align']
        return out, force
    except Exception:
        return out, None


def _finalize_arbitrate(out, force_dir=None):
    """末级方向仲裁出口 (2026-09-10): 必须在 OU/winner 约束重排之后调用。

    保证 unified_scoreline 输出的 direction 与 top1 比分类别恒一致 —
    "方向卡 主胜" 与 "首选比分 0-0(平)" 这类串联矛盾从结构上不可能出现。
    force_dir: winner_hint 生效时锁定方向 — 只做稳定分区(该类在前), 不改判。"""
    try:
        if not (out and out.get('found')):
            return out
        top5 = out.get('top5') or []
        if force_dir in ('home', 'draw', 'away') and any(_outcome_of(t.get('score')) == force_dir for t in top5):
            ordered = sorted(top5, key=lambda t: (_outcome_of(t.get('score')) != force_dir,
                                                  -float(t.get('prob') or 0.0)))
            tot = sum(float(t.get('prob') or 0.0) for t in top5) or 1.0
            fm = sum(float(t.get('prob') or 0.0) for t in top5 if _outcome_of(t.get('score')) == force_dir) / tot
            out['top5'] = ordered
            out['score'] = ordered[0]['score']
            out['direction'] = {
                'winner': force_dir,
                'label': {'home': '主胜', 'draw': '平', 'away': '客胜'}[force_dir],
                'prob': round(fm, 3),
                'basis': '领先方先验⊕即时盘(方向对齐池重排)',
            }
            return out
        _dir, _ordered = arbitrate_direction(top5)
        if _dir and _ordered:
            out['top5'] = _ordered
            out['score'] = _ordered[0]['score']
            out['direction'] = _dir
    except Exception:
        pass
    return out


def _outcome_of(score: str) -> str:
    """比分 → 胜负平类 ('home'|'draw'|'away'); 解析失败返回 'draw'。"""
    try:
        mh, ma = (int(x) for x in str(score).replace(':', '-').split('-')[:2])
    except Exception:
        return 'draw'
    return 'home' if mh > ma else ('draw' if mh == ma else 'away')


def arbitrate_direction(top5):
    """方向仲裁 v3 (2026-09-10, 干净样本实证后简化):

    匹配器维数错位/NaN掩码双 bug 修复后, 干净样本(109 场非库内)实证:
      方向=TOP1类别 76.1%  >>  方向=池多数 59.6%  (旧分支②换首选倒贴 9pp TOP1)
    故方向恒取 TOP1 比分的类别, 置信度 = 该类质量占比; 不再换首选、不再改判。
    不变量: 输出的 direction 与 top1 比分类别恒一致(结构上不可能矛盾)。
    (滚球态 winner_hint 对齐由 _apply_constraints_stage/_finalize_arbitrate
     的 force 路径负责, 与本函数独立。)
    """
    try:
        if not top5:
            return None, top5
        tot = 0.0
        mass = {'home': 0.0, 'draw': 0.0, 'away': 0.0}
        for t in top5:
            p = float(t.get('prob') or 0.0)
            tot += p
            mass[_outcome_of(t.get('score'))] += p
        if tot <= 1e-9:
            return None, top5
        mass = {k: v / tot for k, v in mass.items()}
        t1_out = _outcome_of(top5[0].get('score'))
        direction = {
            'winner': t1_out,
            'label': {'home': '主胜', 'draw': '平', 'away': '客胜'}[t1_out],
            'prob': round(mass[t1_out], 3),
            'basis': '比分池仲裁(方向=首选比分类别, 干净样本实证)',
        }
        return direction, top5
    except Exception:
        return None, top5
