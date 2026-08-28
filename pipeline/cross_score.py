# -*- coding: utf-8 -*-
"""
cross_score.py — 三盘交叉 + 滚球验证 的比分识别引擎 (2026-08-28 用户全局优化思路)

用户思路: 匹配数据库中的【初盘赔率结构: 大小球(OU)+让球(AH)+胜平负(1X2)】
        识别最终比分 → 再结合【滚球赔率结构】验证 → 给出适合比分。
        同时优化即时 OU 判断。

实现:
  Phase 1  初盘三盘交叉: 开盘 1X2/OU/AH 三市场去水 → build_trust_card
           (OU(P(total>line))+AH(P(home-ah>0)) 约束拟合 λ/μ) → 全比分分布 our_distribution
  Phase 2  滚球条件化: 当前比分(sh-sa)+分钟 → 泊松剩余时间缩 λ + 过滤不可能比分
  Phase 3  滚球 OU 漂移验证: 开盘线 vs 当前线 → 庄家预期总球上/下修 → 修正候选
  Phase 4  输出: 最终比分 Top3 + 三盘依据(fit_sources) + 滚球验证说明

诚实边界: 无初盘三盘(1X2 缺失) → None; 有初盘无滚球 → 退回 Phase1 分布;
         一切标注 basis(禁 CS 定价, 08-23 决策)。
"""
import json

import numpy as np


def _parse_score(score):
    if not score:
        return 0, 0
    import re
    m = re.match(r"(\d+)\s*[-:]\s*(\d+)", str(score).strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _open_odds(con, match_key):
    """开盘三市场: 1X2/OU(over,under)/AH(home,away). 1X2 缺失返回 None."""
    from analysis.live_goal_probe import (
        _open_1x2_from_snapshots, _open_total_from_snapshots, _open_ah_from_snapshots,
    )
    h, d, a = _open_1x2_from_snapshots(con, match_key)
    if not (h and d and a):
        return None
    ou_line, _ou_T = _open_total_from_snapshots(
        con, match_key, 'OU_', exclude_prefixes=['OU_1H', 'OU_2H'], ref_line=2.5)
    ah_line, ah_h, ah_a = _open_ah_from_snapshots(con, match_key)
    # OU over/under 赔率: 开盘帧 (从 odds_snapshots 最早帧取, 与 _open_total 同源)
    ou_over = ou_under = None
    if ou_line is not None:
        try:
            rows = con.execute(
                "SELECT selection, odds FROM odds_snapshots "
                "WHERE match_key=? AND market=? ORDER BY captured_at ASC LIMIT 4",
                (match_key, f"OU_{ou_line:.2f}"),
            ).fetchall()
            _d = {}
            for sel, odds in rows:
                if odds and 1.01 < odds < 1000.0:
                    _d.setdefault(sel, odds)
            ou_over = _d.get('over')
            ou_under = _d.get('under')
        except Exception:
            ou_over = ou_under = None
    return {
        'h': h, 'd': d, 'a': a,
        'ou_line': ou_line, 'ou_over': ou_over, 'ou_under': ou_under,
        'ah_line': ah_line, 'ah_home': ah_h, 'ah_away': ah_a,
    }


_EMPIRICAL = None  # 懒加载: models/cs_empirical.joblib


def _load_empirical():
    """懒加载真实赛果训练模型 (2026-08-28: cs_empirical.joblib)."""
    global _EMPIRICAL
    if _EMPIRICAL is None:
        import joblib
        import os
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "models", "cs_empirical.joblib")
        if os.path.exists(p):
            _EMPIRICAL = joblib.load(p)
    return _EMPIRICAL


def _empirical_score_distribution(con, match_key, odds):
    """用真实赛果训练模型预测比分分布 (ML predict_proba + 实证桶兜底).
    特征与 scripts/train_cs_empirical.py extract_features 一致 (15 维):
    [cs_n, cs_cheap_odds, cs_cheap_home, cs_cheap_away, cs_2nd_odds, cs_margin5,
     cs_cheap_low, cs_cheap_home_win, x_h, x_d, x_a, ou_line, ou_over_p, ah_line, ah_home_p]
    返回 {score_str: prob, ...} 或 None (模型不可用/特征缺失过多).
    """
    emp = _load_empirical()
    if not emp or not emp.get("clf"):
        return None
    import json as _json
    try:
        # ── CS 初盘 (最早帧) ──
        cs_pairs = []
        rows = con.execute(
            "SELECT selection, odds FROM odds_snapshots "
            "WHERE match_key=? AND market='CS' ORDER BY captured_at ASC",
            (match_key,),
        ).fetchall()
        seen = {}
        for sel, od in rows:
            if sel and ":" in sel and od and 1.01 < od < 1000.0:
                seen.setdefault(sel, od)
        cs_pairs = [(sel, od) for sel, od in seen.items()]
        cs_pairs.sort(key=lambda x: x[1])
        # ── 特征向量 ──
        f = [None] * 15
        if cs_pairs:
            f[0] = len(cs_pairs)                      # cs_n
            f[1] = cs_pairs[0][1]                     # cs_cheap_odds
            try:
                ch, ca = (int(x) for x in cs_pairs[0][0].split(":"))
                f[2], f[3] = ch, ca                   # cheap_home/away
            except Exception:
                pass
            f[4] = cs_pairs[1][1] if len(cs_pairs) > 1 else None  # 2nd
            imp = sum(1.0 / p[1] for p in cs_pairs[:5])
            f[5] = round(imp - 1.0, 4)                # cs_margin5
            if f[2] is not None and f[3] is not None:
                f[6] = 1 if f[2] + f[3] <= 1 else 0   # cheap_low
                f[7] = 1 if f[2] > f[3] else 0        # cheap_home_win
        # 1X2 去水
        h, d, a = odds.get('h'), odds.get('d'), odds.get('a')
        if h and d and a and all(float(x) > 1.01 for x in (h, d, a)):
            s = 1 / float(h) + 1 / float(d) + 1 / float(a)
            f[8] = round((1 / float(h)) / s, 4)
            f[9] = round((1 / float(d)) / s, 4)
            f[10] = round((1 / float(a)) / s, 4)
        # OU
        if odds.get('ou_over') and odds.get('ou_under') and odds.get('ou_line'):
            ov, un = float(odds['ou_over']), float(odds['ou_under'])
            if ov > 1.01 and un > 1.01:
                f[11] = float(odds['ou_line'])
                f[12] = round((1 / ov) / (1 / ov + 1 / un), 4)
        # AH
        if odds.get('ah_home') and odds.get('ah_away'):
            hh, aa = float(odds['ah_home']), float(odds['ah_away'])
            if hh > 1.01 and aa > 1.01:
                f[13] = float(odds['ah_line']) if odds.get('ah_line') is not None else 0.0
                f[14] = round((1 / hh) / (1 / hh + 1 / aa), 4)
        if f[8] is None:  # 1X2 是核心特征, 缺失不预测
            return None
        import numpy as np
        vec = np.array([f], dtype=float)
        imp_ml = emp.get("imputer")
        clf = emp.get("clf")
        X = imp_ml.transform(vec) if imp_ml else vec
        proba = clf.predict_proba(X)[0]
        full = np.zeros((emp.get("maxg", 6) + 1) ** 2)
        for cidx, cls in enumerate(clf.classes_):
            full[cls] = proba[cidx]
        dist = {}
        for lab, p in enumerate(full):
            if p <= 0:
                continue
            hh2, aa2 = divmod(lab, emp.get("maxg", 6) + 1)
            dist[f"{hh2}:{aa2}"] = float(p)
        # 归一化 (ML 只覆盖 classes, 可能不全)
        tot = sum(dist.values())
        if tot > 0:
            return {k: v / tot for k, v in dist.items()}
        return None
    except Exception:
        return None


def _roll_ou_anchor(con, match_key, minute):
    """滚球 OU 锚: 当前最新线 + 开盘线 (漂移验证). 无滚球数据返回 None."""
    try:
        rows = con.execute(
            "SELECT market, selection, odds FROM odds_snapshots "
            "WHERE match_key=? AND market LIKE 'OU_%' AND market NOT LIKE '%_1H%' "
            "AND market NOT LIKE '%_2H%' ORDER BY captured_at DESC LIMIT 30",
            (match_key,),
        ).fetchall()
        if not rows:
            return None
        latest = {}
        for mkt, sel, odds in rows:
            if odds and 1.01 < odds < 1000.0:
                latest.setdefault(mkt, {})[sel] = odds
        if not latest:
            return None
        # 取最新快照帧(第一条的 captured_at 一致组) 的最常见线
        def _line_of(mkt):
            try:
                return float(mkt.split('_')[-1])
            except Exception:
                return None
        lines = [(_line_of(mkt), v.get('over'), v.get('under')) for mkt, v in latest.items() if _line_of(mkt)]
        # 2026-08-28: 过滤 line<1.0 (OU_0.5/0.75 半场残留/已破线混入会误导"降盘漂移"显示)
        lines = [x for x in lines if x[0] is not None and x[0] >= 1.0]
        if not lines:
            return None
        lines.sort(key=lambda x: -abs(x[0] - 2.5))
        cur_line, cur_over, cur_under = lines[0]
        # 开盘线: 取最早的 OU 线
        open_rows = con.execute(
            "SELECT market FROM odds_snapshots WHERE match_key=? AND market LIKE 'OU_%' "
            "AND market NOT LIKE '%_1H%' AND market NOT LIKE '%_2H%' "
            "ORDER BY captured_at ASC LIMIT 5",
            (match_key,),
        ).fetchall()
        open_line = None
        for (mkt,) in open_rows:
            try:
                open_line = float(mkt.split('_')[-1])
                break
            except Exception:
                continue
        return {
            'open_line': open_line,
            'current_line': cur_line,
            'current_over': cur_over,
            'current_under': cur_under,
            'drift': (cur_line - open_line) if open_line is not None else None,
        }
    except Exception:
        return None


def derive_score_cross(con, match_key, current_score='0-0', current_minute=0):
    """三盘交叉 + 滚球验证 比分识别. 返回 dict 或 None."""
    odds = _open_odds(con, match_key)
    if odds is None:
        return None
    try:
        from pipeline.cs_trust_model import build_trust_card
        card = build_trust_card(
            cs_grid=None,
            h=odds['h'], d=odds['d'], a=odds['a'],
            ou_line=odds['ou_line'], ou_over=odds['ou_over'], ou_under=odds['ou_under'],
            ah_line=odds['ah_line'], ah_home=odds['ah_home'], ah_away=odds['ah_away'],
            con=con,
        )
    except Exception:
        card = {}
    our = card.get('our_distribution') or {}
    if not our:
        return None
    fit_sources = card.get('fit_sources') or ['1X2']

    # ── Phase 3.5: DB 历史匹配 + 实证模型混合 (2026-08-28, 用户: 三盘结合从数据库匹配波胆) ──
    # 用户口径: "让球/大小球/胜平负结合, 从 DB 匹配历史相似比赛的真实波胆, 非预测".
    # 优先级: DB 匹配(0.6) > 实证ML(0.25) > 结构(0.15) — DB 匹配实测 top1 22%/top3 45%.
    # 此块在 Phase 2 之前: 混合结果走滚球条件化/漂移验证.
    our_raw = dict(our)   # 纯结构 (score_opening 用)
    # 统一比分 key 为 '-' 格式 (build_trust_card 可能出 '2:0', db_match 出 '2-0')
    our = {k.replace(':', '-'): v for k, v in our.items()}
    emp_note = None
    try:
        from pipeline.cs_db_match import db_match_scoreline
        m = db_match_scoreline(
            h=odds['h'], d=odds['d'], a=odds['a'],
            ou_line=odds['ou_line'], ou_over=odds['ou_over'], ou_under=odds['ou_under'],
            ah_line=odds['ah_line'], ah_home=odds['ah_home'], ah_away=odds['ah_away'],
        )
        if m and m.get('found'):
            db_dist = {}
            for t in m.get('top5', []):
                db_dist[t['score'].replace(':', '-')] = t['prob']
            # DB 匹配(0.6) + 结构(0.4) 混合
            merged = {}
            for s, p in our.items():
                merged[s] = 0.6 * db_dist.get(s, 0.0) + 0.4 * p
            for s, p in db_dist.items():
                merged.setdefault(s, 0.6 * p)
            t3 = sum(merged.values()) or 1.0
            merged = {s: p / t3 for s, p in merged.items()}
            our = merged
            emp_note = f"DB 三盘匹配 {m['n_matched']} 场历史(均距 {m['mean_dist']}) → 真实比分 top1 {m['top1_hit']*100:.0f}%/top3 {m['top3_hit']*100:.0f}%"
    except Exception:
        emp_note = None
    # 实证 ML 次级混合 (DB 匹配不可用时增强)
    try:
        emp_dist = _empirical_score_distribution(con, match_key, odds)
        if emp_dist and emp_note is None:
            merged = {}
            for s, p in our.items():
                merged[s] = 0.5 * p + 0.5 * emp_dist.get(s, 0.0)
            for s, p in emp_dist.items():
                merged.setdefault(s, 0.5 * p)
            t3 = sum(merged.values()) or 1.0
            merged = {s: p / t3 for s, p in merged.items()}
            our = merged
            emp_note = f"真实赛果实证模型混合(0.5结构+0.5实证, 9984场训练)"
    except Exception:
        pass

    # ── Phase 2: 滚球条件化 ──
    sh, sa = _parse_score(current_score)
    minute = int(current_minute or 0)
    roll_note = None
    candidates = dict(our)
    if minute > 0:
        # 过滤不可能比分 (已进球必须达成; 当前比分本身保留)
        keep = {}
        for s, p in candidates.items():
            try:
                mh, ma = (int(x) for x in str(s).replace(':', '-').split('-'))
            except Exception:
                continue
            if mh < sh or ma < sa:
                continue
            keep[s] = p
        if not keep:
            keep = dict(our)  # 过滤空则退回全分布
        # 剩余时间缩 λ: 已有比分占权重, 总球预期下调
        time_scale = max(0.2, 1.0 - minute / 100.0)
        base_total = sum((int(s.split('-')[0]) + int(s.split('-')[1])) * p
                         for s, p in keep.items() if '-' in s and all(x.isdigit() for x in s.split('-')))
        candidates = keep
        # 已进球 = 剩余进球的先验, 用剩余时间缩放来压低高比分
        if time_scale < 1.0:
            scaled = {}
            for s, p in candidates.items():
                mh, ma = (int(x) for x in str(s).replace(':', '-').split('-'))
                extra = (mh - sh) + (ma - sa)   # 还需进几球
                adj = p * (time_scale ** max(0, extra - 1))   # 多进一球概率按时间衰减
                scaled[s] = adj
            tot = sum(scaled.values()) or 1.0
            candidates = {s: p / tot for s, p in scaled.items()}
        roll_note = f"滚球条件化: 已 {sh}-{sa} @{minute}', 剩余时间缩放 {time_scale:.2f}, 过滤不可能比分"

    # ── Phase 3: 滚球 OU 漂移验证 ──
    drift = _roll_ou_anchor(con, match_key, minute)
    drift_note = None
    if drift and drift.get('open_line') and drift.get('current_line'):
        dlt = drift['current_line'] - drift['open_line']
        if abs(dlt) >= 0.25:
            direction = '下修' if dlt < 0 else '上修'
            drift_note = (f"滚球 OU 验证: 开盘线 {drift['open_line']} → 当前 {drift['current_line']}"
                          f" (庄家预期总球{direction} {abs(dlt):.2f} 球)")
            # 下修 → 大比分概率压低 (权重乘小系数)
            if dlt < 0:
                tot = sum(candidates.values()) or 1.0
                adj2 = {}
                for s, p in candidates.items():
                    mh, ma = (int(x) for x in str(s).replace(':', '-').split('-'))
                    if (mh + ma) >= 4:
                        adj2[s] = p * 0.7   # 大比分在降线下概率下修
                    else:
                        adj2[s] = p
                t2 = sum(adj2.values()) or 1.0
                candidates = {s: p / t2 for s, p in adj2.items()}

    # ── Phase 4: 输出 ──
    # 第一比分: 初盘+即时结构 (未滚球条件化, 全分布 argmax + 即时盘方向校正)
    score_opening = None
    top3_opening = []
    opening_basis = None
    try:
        _orank = sorted(our_raw.items(), key=lambda x: -x[1])
        top3_opening = [{'score': s, 'prob': round(p, 4)} for s, p in _orank[:3]]
        _best = _orank[0][0]
        # 即时盘校正: 当前 1X2 去水方向 (最新市场定价) 替换初盘方向
        try:
            from analysis.live_goal_probe import _current_inplay_odds, _dewater_1x2
            _cur = _current_inplay_odds(con, match_key, minute)
            if _cur and _cur.get('x2') and all(x and x > 1.0 for x in _cur['x2']):
                _ph, _pd_, _pa = _dewater_1x2(*_cur['x2'])
                if _ph is not None:
                    _dir = 0 if _ph >= _pd_ and _ph >= _pa else (2 if _pa >= _pd_ else 1)
                    _tot = int(_best.split('-')[0]) + int(_best.split('-')[1])
                    _mk = _best.replace(':', '-')
                    _mh, _ma = (int(x) for x in _mk.split('-'))
                    if _dir == 0 and _mh < _ma:
                        _best = f"{_ma}-{_mh}"   # 即时盘转向主胜 → 翻转
                    elif _dir == 2 and _mh > _ma:
                        _best = f"{_ma}-{_mh}"
                    opening_basis = f"初盘三盘分布主推 + 即时盘去水方向校正"
        except Exception:
            pass
        score_opening = _best.replace(':', '-')
        if opening_basis is None:
            opening_basis = "初盘三盘分布主推"
    except Exception:
        score_opening = None

    # 第二比分: 滚球修正后 (条件化 + 漂移验证)
    ranked = sorted(candidates.items(), key=lambda x: -x[1])
    top3 = [{'score': s, 'prob': round(p, 4)} for s, p in ranked[:3]]
    basis_parts = [f"初盘三盘交叉({'+'.join(fit_sources)})拟合 λ/μ → 全比分分布"]
    if emp_note:
        basis_parts.append(emp_note)
    if roll_note:
        basis_parts.append(roll_note)
    if drift_note:
        basis_parts.append(drift_note)
    basis_parts.append("禁 CS 定价 (08-23 决策)")
    # 2026-08-28: 初盘三盘分布 → P(总球 > line) 先验 (供即时 OU 判断做强先验)
    over_prob_at = {}
    try:
        for s, p in candidates.items():
            mh, ma = (int(x) for x in str(s).replace(':', '-').split('-'))
            for line in (0.5, 1.5, 2.5, 3.5):
                if (mh + ma) > line:
                    over_prob_at[line] = over_prob_at.get(line, 0.0) + p
        over_prob_at = {k: round(v, 4) for k, v in over_prob_at.items()}
    except Exception:
        over_prob_at = {}
    return {
        'top3': top3,
        'score': top3[0]['score'] if top3 else None,        # 第二比分: 滚球修正 (主推)
        'score_roll': top3[0]['score'] if top3 else None,
        'score_opening': score_opening,                     # 第一比分: 初盘+即时结构
        'top3_opening': top3_opening,
        'opening_basis': opening_basis,
        'winner_label': None,  # 由调用方补充
        'total': None,
        'basis': '; '.join(basis_parts),
        'roll_verification': drift_note,
        'fit_sources': fit_sources,
        'over_prob_at': over_prob_at,
        'found': True,
    }
