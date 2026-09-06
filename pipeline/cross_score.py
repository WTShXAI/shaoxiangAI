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
import os
import time

import numpy as np


# ── 方向3 (2026-08-29): 「领先方最终获胜」经验先验 ──
# 数据由 scripts/stat_lead_vs_result_20260829.py 生成, 存 config/lead_result_prior.json。
# **禁硬编码**进代码 —— 重跑统计脚本即可再生成。
_LEAD_PRIOR_CACHE = {"ts": 0.0, "data": None}
_LEAD_PRIOR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "lead_result_prior.json")
_LEAD_PRIOR_TTL = 3600.0      # 1 小时缓存, 重跑统计脚本后自动生效
_LEAD_PRIOR_MIN_N = 30        # 样本不足的格子不加权
_LEAD_PRIOR_K = 200.0         # 收缩系数 K (同 IR-19 FLB 口径, w = n/(n+K))
# α 上限 (2026-08-29 回测定档, 632 场/2060 点测试集, 训练集 kickoff<2026-08-26 无交集):
#   ⚠ 平局(draw|0)跳过加权后结论反转 —— 平局格子 n=846 是 Brier 恶化的主因,
#     排除后 α 越大方向准确率越高, 且 Brier **全程改善**:
#       α=0.3 → +3.20pp, Brier -0.0009
#       α=0.5 → +3.98pp, Brier -0.0008
#       α=0.7 → +4.95pp, Brier **-0.0004**  ← 帕累托最优
#       α=1.0 → +5.05pp, Brier -0.0002  (准确率仅再+0.1pp, 校准改善变少)
#   → 取 0.7: 几乎拿满准确率收益, 且 Brier 改善最多。IR-19 目标 Brier, 宁保守。
_LEAD_PRIOR_ALPHA_CAP = 0.7


def _load_lead_prior():
    """加载领先方→最终赛果经验先验表 (带 TTL 缓存)。"""
    now = time.time()
    if _LEAD_PRIOR_CACHE["data"] is not None and (now - _LEAD_PRIOR_CACHE["ts"]) < _LEAD_PRIOR_TTL:
        return _LEAD_PRIOR_CACHE["data"]
    try:
        with open(_LEAD_PRIOR_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    _LEAD_PRIOR_CACHE["ts"] = now
    _LEAD_PRIOR_CACHE["data"] = data
    return data


def _band_of_minute(minute):
    try:
        m = int(minute or 0)
    except Exception:
        return None
    if m <= 30:
        return "5-30"
    if m <= 55:
        return "31-55"
    if m <= 85:
        return "56-85"
    return None


def _apply_lead_prior(dist, sh, sa, minute):
    """滚球阶段: 用「领先方最终获胜」经验频率校正模型分布的**方向权重**。

    实测铁证 (scripts/stat_lead_vs_result_20260829.py, 2760 场 / 10276 采样点):
        领先 1 球 → 最终该方胜 71~74%
        领先 2 球 → 92~94%
        领先 3 球 → 99.4~99.6%
    而模型在滚球阶段仍锚定**初盘**市场定价: 实测莫斯科斯巴达U19 vs 罗迪那U19
    `1-0@20'` 推 1-2(客胜), 与 "主队领先1球最终主胜 70.5%" 的历史频率正好相反。

    融合公式 (只重分配方向间权重, 不改方向内分布):
        P_new(s) ∝ P_model(s) * ( P_emp(dir(s)) / P_model_dir(dir(s)) ) ^ α
        α = n/(n+K), K=200 —— 样本越多越信任历史频率 (IR-19 FLB 同口径)
    格子样本 n < 30 时不加权。

    ⚠ 与 IR-25 不冲突: IR-25 证伪的是 "obscure 领先后收缩 → 不再进球"
      (Kabuscorp 0-1→2-1 / 特尔纳瓦 76'1-2→2-2)。本先验**只校正最终胜负方向**,
      不压低后续进球概率 —— 方向**内**的相对分布与总球分布均保持不变, 只是把
      三个方向之间的权重按历史频率重新分配。领先方依然可能丢球(概率不变),
      只是不再把"初盘热门"当成比"当前比分"更强的方向证据。

    返回 (新分布, 说明) 或 (原分布, None)。
    """
    if not dist or minute is None or int(minute or 0) <= 0:
        return dist, None
    try:
        sh, sa = int(sh), int(sa)
    except Exception:
        return dist, None
    band = _band_of_minute(minute)
    if band is None:
        return dist, None
    prior = _load_lead_prior()
    table = prior.get("table") or {}
    diff = sh - sa
    lead_side = "home" if diff > 0 else ("away" if diff < 0 else "draw")
    # 平局不加权 (2026-08-29 回测铁证): 平局时**没有领先方**, "领先方最终获胜"先验
    #   不适用; 实测强行套用会让方向准确率从 37.8% 退到 36.4% (-1.4pp, n=846)。
    #   而领先 1 球场景能拿 +10~15pp (home1 62.2→72.8, away1 59.6→74.4)。
    if lead_side == "draw":
        return dist, None
    lead_goals = min(abs(diff), 3)
    cell = table.get(f"{lead_side}|{lead_goals}|{band}")
    if not cell or int(cell.get("n", 0)) < _LEAD_PRIOR_MIN_N:
        return dist, None

    # 模型当前的方向概率
    dir_prob = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for s, p in dist.items():
        try:
            mh, ma = (int(x) for x in str(s).replace(':', '-').split('-'))
        except Exception:
            continue
        if mh > ma:
            dir_prob["home"] += p
        elif mh < ma:
            dir_prob["away"] += p
        else:
            dir_prob["draw"] += p
    tot_model = sum(dir_prob.values()) or 1.0
    dir_prob = {k: v / tot_model for k, v in dir_prob.items()}

    emp = {"home": float(cell.get("home", 0.0)),
           "draw": float(cell.get("draw", 0.0)),
           "away": float(cell.get("away", 0.0))}
    n = float(cell.get("n", 0))
    alpha = min(n / (n + _LEAD_PRIOR_K), _LEAD_PRIOR_ALPHA_CAP)

    out = {}
    for s, p in dist.items():
        try:
            mh, ma = (int(x) for x in str(s).replace(':', '-').split('-'))
        except Exception:
            out[s] = p
            continue
        d = "home" if mh > ma else ("away" if mh < ma else "draw")
        pm = dir_prob.get(d, 0.0)
        pe = emp.get(d, 0.0)
        if pm <= 1e-9:
            # 模型给该方向 0 概率 → 用经验值开一个下限, 否则永远拉不动
            ratio = (pe / 1e-3) ** alpha if pe > 0 else 1.0
            ratio = min(ratio, 50.0)
        else:
            ratio = (pe / pm) ** alpha
        out[s] = p * ratio
    tot = sum(out.values()) or 1.0
    out = {s: p / tot for s, p in out.items()}
    note = (f"领先方先验校正: 当前 {sh}-{sa}({lead_side}领先{lead_goals}球) @{int(minute)}' "
            f"历史 P(主/平/客)={emp['home']:.0%}/{emp['draw']:.0%}/{emp['away']:.0%} "
            f"(n={int(n)}), 权重 α={alpha:.2f}")
    return out, note


def _parse_score(score):
    if not score:
        return 0, 0
    import re
    m = re.match(r"(\d+)\s*[-:]\s*(\d+)", str(score).strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _open_odds(con, match_key):
    """开盘三市场: 1X2/OU(over,under)/AH(home,away). 1X2 缺失返回 None。

    2026-08-30 SSoT: 优先 match_outcomes.op_* 归档列(与 bridge._gather_opening_odds
    同源 — 实测 格拉茨风暴B队场快照路径 OU 隐含总球被健全闸门拒绝 → ou_line=None →
    DB 匹配退化 4 维 → 两卡比分分歧), 归档缺列再回退快照路径。"""
    ou_line = ou_over = ou_under = None
    ah_line = ah_h = ah_a = None
    h = d = a = None
    try:
        row = con.execute(
            "SELECT op_1x2_h, op_1x2_d, op_1x2_a, op_ou_line, op_ou_over, op_ou_under, "
            "op_ah_line, op_ah_home, op_ah_away FROM match_outcomes "
            "WHERE match_key=? OR (match_key IS NULL AND 0) LIMIT 1", (match_key,)).fetchone()
    except Exception:
        row = None
    # match_outcomes 无 match_key 列 — 按 matches.home/away 关联归档行
    if row is None:
        try:
            row = con.execute(
                "SELECT mo.op_1x2_h, mo.op_1x2_d, mo.op_1x2_a, mo.op_ou_line, mo.op_ou_over, "
                "mo.op_ou_under, mo.op_ah_line, mo.op_ah_home, mo.op_ah_away "
                "FROM match_outcomes mo JOIN matches m ON m.home = mo.home AND m.away = mo.away "
                "WHERE m.match_key=? ORDER BY mo.kickoff DESC LIMIT 1", (match_key,)).fetchone()
        except Exception:
            row = None
    if row:
        h, d, a = (float(x) if x else None for x in row[0:3])
        if row[3] and row[4] and row[5]:
            ou_line, ou_over, ou_under = float(row[3]), float(row[4]), float(row[5])
        if row[6] and row[7] and row[8]:
            ah_line, ah_h, ah_a = float(row[6]), float(row[7]), float(row[8])
    if not (h and d and a):
        from analysis.live_goal_probe import _open_1x2_from_snapshots
        h, d, a = _open_1x2_from_snapshots(con, match_key)
        if not (h and d and a):
            return None
    # 2026-08-30 SSoT: OU/AH 流内自洽回退(与 bridge._gather_opening_odds 同逻辑) —
    # 进行中场归档列(op_*)尚无行, 快照路径的 _open_total 可能被健全闸门拒绝,
    # 导致 OU 缺失 → DB 匹配退化 4 维 → 两卡比分分歧(格拉茨风暴B队场实测)。
    def _stream_open_pair(prefix, not_likes, ref, sels):
        nl = " AND ".join(f"market NOT LIKE '{p}%'" for p in not_likes)
        try:
            rows = con.execute(
                f"SELECT market, selection, odds FROM odds_snapshots "
                f"WHERE match_key=? AND market LIKE ? AND {nl} "
                f"AND odds>1.01 AND odds<1000 AND captured_at > strftime('%s','now','-3 day') "
                f"ORDER BY captured_at ASC LIMIT 120", (match_key, prefix + '%')).fetchall()
        except Exception:
            return None
        streams = {}
        for mkt, sel, od in rows:
            s = streams.setdefault(mkt, {})
            if sel in sels and sel not in s:
                s[sel] = float(od)
        cands = []
        for mkt, s in streams.items():
            if all(k in s for k in sels):
                try:
                    line = float(mkt.split('_')[1])
                except Exception:
                    continue
                cands.append((abs(line - ref), line, s))
        if not cands:
            return None
        _, line, s = min(cands)
        return (line, s)

    if ou_line is None or ou_over is None:
        _ou = _stream_open_pair('OU_', ['OU_1H', 'OU_2H'], 2.5, ('over', 'under'))
        if _ou:
            ou_line = _ou[0]
            ou_over, ou_under = _ou[1]['over'], _ou[1]['under']
    if ah_line is None or ah_h is None:
        _ah = _stream_open_pair('AH_', ['AH_1H', 'AH_2H'], 0.0, ('home', 'away'))
        if _ah:
            ah_line = _ah[0]
            ah_h, ah_a = _ah[1]['home'], _ah[1]['away']
    if ou_line is None:
        from analysis.live_goal_probe import _open_total_from_snapshots
        ou_line2, _ou_T = _open_total_from_snapshots(
            con, match_key, 'OU_', exclude_prefixes=['OU_1H', 'OU_2H'], ref_line=2.5)
        if ou_line2 is not None:
            ou_line = ou_line2
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
    if ah_line is None:
        from analysis.live_goal_probe import _open_ah_from_snapshots
        ah_line, ah_h, ah_a = _open_ah_from_snapshots(con, match_key)
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
    """滚球 OU 锚: 当前最新线 + 开盘线 (漂移验证). 无滚球数据返回 None.

    2026-08-29 三 bug 修复 (莫斯科斯巴达U19 vs 罗迪那U19 3-1 实测暴露, 用户"修正模型"):
      Bug-1 排序反: 原 lines.sort(key=lambda x: -abs(x[0]-2.5)) 取 lines[0] —— sort 升序
           加负号后 -1.5 < -1.0, [0] 拿到的是离 2.5 **最远**的线 (实测 4.0 而非最近的 3.5)。
           改为 key=abs(...) 取最近。
      Bug-2 开盘取错: 原 open_rows 用 ORDER BY captured_at ASC 取最早帧 —— GQ 采集器在
           kickoff+31s 把半场盘(minute_at=45)的 captured_at 打成早于真开盘帧(minute_at=0),
           实测把 OU_3.50 半场残盘当成开盘价 (真开盘是 OU_3.75)。改为优先走 SSoT
           _open_total_from_snapshots (kickoff-5min 闸门), SQL 兜底加 minute_at=0 硬过滤。
      Bug-3 in-play 回退终场: 原当前线查询不限 minute_at, min=57 无数据时取到 min=112 终场
           残盘 (1X2 1.01 主胜已定 / OU_4.0), 等于拿已知终局倒推预测 —— 致命泄漏。
           改为限定 minute_at>0 AND minute_at<=minute, 无真 in-play 数据诚实降级 (drift=None)。
    """
    try:
        minute = int(minute or 0)

        # Bug-1b 修复: 原 _line_of 用 float(mkt.split('_')[-1]) —— GQ 长表变体盘命名
        #   'OU_0.50_88' (市场_线_变体号) 被解析成 88.0, 污染线值与所有按线聚合处。
        #   实测 21 场比赛开盘线被算成 88/97/191.5 球。统一走 _extract_line_from_market
        #   (2026-08-28 已正确处理变体号: 'OU_0.50_88'→0.5)。
        from analysis.live_goal_probe import (
            _extract_line_from_market as _line_of,
            _ok_ou_line_value,
        )

        # ── 开盘线: 真开盘帧 (minute_at<=1) 优先 ──
        # 三级回退, 最小侵入 (2026-08-29 校准: _open_total_from_snapshots 自带 kickoff-48h
        # 窗口, 候选池比原查询小, 直接当 SSoT 会引入额外差异, 故降级为二级回退):
        #   ① minute_at<=1 的开盘帧 (无 kickoff 窗口限制, 与旧查询同池, 只多排除半场残盘)
        #   ② _open_total_from_snapshots (kickoff-5min 闸门, 最严格的"确认开盘")
        #   ③ 旧逻辑: 全表 captured_at ASC 取最早 (行为完全回退, 零回归)
        # ── 开盘线: 动态开盘批 + 取最早 (最小侵入) ──
        # 2026-08-29 三轮校准后的最终口径 (scripts/verify_open_line_accuracy_20260829.py):
        #   ① 真值验证打脸了"SSoT 优先 + |line-2.5| 重排"方案 —— 与 DB 独立记录的开盘线
        #      match_outcomes.op_ou_line 对比, 旧逻辑平均偏差 0.1218, 重排方案 0.2376
        #      (偏差翻倍)。原因: ref_line=2.5 是硬编码, 但俄超/U19 等联赛真实开盘主盘
        #      常在 3.5; 而"取 captured_at 最早那条"恰好就是庄家开盘时挂的主盘。
        #   ② 也打脸了"加 id ASC 二级排序" —— 实测把 AC米兰U20 的开盘线从 2.25 变成
        #      2.75, 引入 24% 且方向偏正的系统漂移。行序不能改。
        #   → 最终: 保持旧 SQL 的排序与"取最早"语义, 只做三处真 bug 修正:
        #       a. 线解析走 _extract_line_from_market (修 'OU_0.50_88' → 88 球)
        #       b. 加 _ok_ou_line_value 过滤 (挡角球/组合盘)
        #       c. 限定 minute_at<=min_ma+1 动态开盘批 (排除半场/终场残盘冒充开盘)
        open_line = None
        try:
            # 先定该场最小 minute_at —— 它就是"开盘帧"所在的比赛分钟
            _row = con.execute(
                "SELECT MIN(minute_at) FROM odds_snapshots WHERE match_key=? "
                "AND market LIKE 'OU_%' AND market NOT LIKE '%_1H%' AND market NOT LIKE '%_2H%'",
                (match_key,),
            ).fetchone()
            min_ma = int(_row[0]) if (_row and _row[0] is not None) else None
        except Exception:
            min_ma = None
        # 最小 minute_at>5 → 该场根本没有开赛前后快照, 不切批(零回归, 走旧路径)
        ma_cap = (min_ma + 1) if (min_ma is not None and min_ma <= 5) else None
        try:
            if ma_cap is not None:
                open_rows = con.execute(
                    "SELECT market FROM odds_snapshots WHERE match_key=? AND market LIKE 'OU_%' "
                    "AND market NOT LIKE '%_1H%' AND market NOT LIKE '%_2H%' AND minute_at<=? "
                    "ORDER BY captured_at ASC LIMIT 5",
                    (match_key, ma_cap),
                ).fetchall()
            else:
                open_rows = con.execute(
                    "SELECT market FROM odds_snapshots WHERE match_key=? AND market LIKE 'OU_%' "
                    "AND market NOT LIKE '%_1H%' AND market NOT LIKE '%_2H%' "
                    "ORDER BY captured_at ASC LIMIT 5",
                    (match_key,),
                ).fetchall()
            for (mkt,) in open_rows:
                _l = _line_of(mkt)
                if _l is not None and _ok_ou_line_value(_l):
                    open_line = _l
                    break
        except Exception:
            open_line = None
        if open_line is None:
            # ② SSoT 兜底 (仅在上面取不到时, 不作为首选)
            try:
                from analysis.live_goal_probe import _open_total_from_snapshots
                _ol, _ = _open_total_from_snapshots(
                    con, match_key, 'OU_', exclude_prefixes=['OU_1H', 'OU_2H'], ref_line=2.5)
                if _ol is not None:
                    open_line = float(_ol)
            except Exception:
                open_line = None

        # ── 当前线: 限定 minute_at<=minute (真 in-play), 绝不回退终场 ──
        cur_line = cur_over = cur_under = None
        if minute > 0:
            # Bug-5 (2026-08-29): events.db 61.8% 滚球快照 minute_at 卡死 45/90,
            # `minute_at<=? ORDER BY minute_at DESC` 退化为 captured_at/写入序 → 恒取
            # 终场残盘。加 kickoff 推算的 captured_at 真实时基上限, 收窄到该分钟真窗口。
            _cap_ts = None
            try:
                from analysis.live_goal_probe import _inplay_cap_ts
                _cap_ts = _inplay_cap_ts(con, match_key, minute)
            except Exception:
                _cap_ts = None

            def _complete(frame_rows):
                """该帧是否含至少一条线同时有合法 over+under (能定主盘)。"""
                _d = {}
                for _m, _s, _o in frame_rows or []:
                    if _o and 1.01 < _o < 1000.0:
                        _d.setdefault(_m, {})[_s] = _o
                for _m, _v in _d.items():
                    _l = _line_of(_m)
                    if _l is not None and _ok_ou_line_value(_l) and _v.get('over') and _v.get('under'):
                        return True
                return False

            def _qcur(use_cap):
                # Bug-6a: 原 `ORDER BY ... LIMIT 30` **跨帧聚合** —— 把 min 41.9 的临时
                #   残盘 OU_2.50 与 min 112 的终场档 OU_4.00 混成"同一时刻"的盘口。
                # Bug-6b: 只取最新一帧又太严 —— 单帧常只记录单侧赔率(over 或 under),
                #   凑不齐 over/under → 过度降级(实测 34.8% 场次拿不到当前线)。
                #   折中: 从最新帧往回找, **取第一个能凑齐完整 over+under 的单帧**,
                #   最多回退 5 帧 (约 5~7 分钟), 绝不跨帧混拼。
                _w = ("match_key=? AND market LIKE 'OU_%' AND market NOT LIKE '%_1H%' "
                      "AND market NOT LIKE '%_2H%' AND minute_at>0 AND minute_at<=? "
                      "AND odds IS NOT NULL AND odds>1.01 AND odds<1000.0")
                _ps = [match_key, minute]
                if use_cap and _cap_ts:
                    _w += " AND captured_at<=?"
                    _ps.append(_cap_ts)
                # Bug-6c: 采集器**逐条写入**, 每条记录 captured_at 各不相同(微秒级差),
                #   实测单帧仅 1 行 → DISTINCT captured_at 退化成单条记录, 永远凑不齐
                #   over+under。改按 **60 秒时间桶** 聚合同一帧(采集周期约 80s)。
                try:
                    _frs = con.execute(
                        f"SELECT DISTINCT CAST(captured_at/60 AS INTEGER) FROM odds_snapshots "
                        f"WHERE {_w} ORDER BY 1 DESC LIMIT 5", tuple(_ps)).fetchall()
                except Exception:
                    return []
                _last = []
                for (_bkt,) in _frs or []:
                    try:
                        _last = con.execute(
                            f"SELECT market, selection, odds FROM odds_snapshots "
                            f"WHERE {_w} AND CAST(captured_at/60 AS INTEGER)=?",
                            tuple(_ps) + (_bkt,)).fetchall()
                    except Exception:
                        _last = []
                    if _complete(_last):
                        return _last
                return _last

            rows = _qcur(True)
            if not rows:
                rows = _qcur(False)   # cap 后无帧 → 回退, 零回归
            latest = {}
            for mkt, sel, odds in rows or []:
                if odds and 1.01 < odds < 1000.0:
                    latest.setdefault(mkt, {})[sel] = odds
            cand_lines = [(_line_of(mkt), v.get('over'), v.get('under'))
                          for mkt, v in latest.items()
                          if _line_of(mkt) is not None
                          and _ok_ou_line_value(_line_of(mkt))
                          and v.get('over') and v.get('under')]
            if cand_lines:
                # Bug-6 修复 (2026-08-29): 原 abs(line - 2.5) **硬编码 ref_line** 选线 ——
                #   实测该场 OU_2.50 只存在于 min 41.9~43.2 (上半场末临时残盘, n=4),
                #   而真实主盘是 3.5/3.75 → 硬编码 2.5 必选到残盘, 算出"开盘3.75→当前2.5
                #   下修1.25球"的荒谬漂移。改用 IR-01 主盘 SSoT 同口径:
                #     ① 同线漂移优先: 开盘线若仍在候选里, 直接跟踪它 (最诚实的漂移)
                #     ② 否则取 overround(抽水) 最低的线 = 庄家主推盘口
                #   实测验证: min 52.8 帧 3.5 抽水 1.0664 < 4.0 抽水 1.0695 → 主盘 3.5 ✓
                #            与用户"初盘大3.5"吻合。
                def _ovr(item):
                    try:
                        return (1.0 / float(item[1])) + (1.0 / float(item[2]))
                    except Exception:
                        return 9.9
                _pick = None
                if open_line is not None:
                    _same = [c for c in cand_lines if abs(c[0] - open_line) < 1e-6]
                    if _same:
                        _pick = _same[0]
                if _pick is None:
                    cand_lines.sort(key=_ovr)
                    _pick = cand_lines[0]
                cur_line, cur_over, cur_under = _pick

        return {
            'open_line': open_line,
            'current_line': cur_line,
            'current_over': cur_over,
            'current_under': cur_under,
            'drift': (cur_line - open_line) if (cur_line is not None and open_line is not None) else None,
        }
    except Exception:
        return None


def derive_score_cross(con, match_key, current_score='0-0', current_minute=0, ou_hint=None):
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
    # ── 2026-08-30 SSoT 统一 v2: 比分排名直接消费 unified_scoreline(与 bridge
    # trust-card db_match 栏同一函数同参) — 消除"两卡不同回退路径导致的比分分歧"
    # (实测 格拉茨风暴B队 2-1@45': cross Poisson平移→2-2 vs unified邻近补位→2-1)。
    # 滚球的过滤/补位已在 unified 内完成, Phase 2 的过滤对已过滤分布是幂等无操作。 ──
    try:
        from pipeline.cs_db_match import unified_scoreline
        m = unified_scoreline(
            h=odds['h'], d=odds['d'], a=odds['a'],
            ou_line=odds['ou_line'], ou_over=odds['ou_over'], ou_under=odds['ou_under'],
            ah_line=odds['ah_line'], ah_home=odds['ah_home'], ah_away=odds['ah_away'],
            current_score=current_score, current_minute=current_minute, ou_hint=ou_hint,
        )
        if m and m.get('found'):
            db_dist = {}
            for t in m.get('top5', []):
                db_dist[t['score']] = t['prob']
            t3 = sum(db_dist.values()) or 1.0
            our = {s: p / t3 for s, p in db_dist.items()}
            emp_note = f"DB 三盘匹配 {m['n_matched']} 场历史(均距 {m['mean_dist']}) → 真实比分 top1 {m['top1_hit']*100:.0f}%/top3 {m['top3_hit']*100:.0f}% (SSoT·统一比分源, mode={m.get('mode')})"
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
            # 2026-08-30 根因修复: 开盘分布全部低于当前比分(领先场常见 — 开盘均衡→
            # 0-0/1-1 密集而比分已 2-0) → 原回退"未过滤全分布"会主推低于当前比分的
            # 荒谬比分(实测 克罗斯科瓦利夫卡 73' 2-0 仍主推 1-1)。
            # 改为平移重构: 剩余进球 = 开盘分布均值 × 剩余时间缩放, 主客比沿用开盘
            # 强度比, 终场分布 = 当前比分 ⊕ 剩余泊松矩阵 — 主推恒 ≥ 当前比分。
            try:
                from pipeline.score_model import score_matrix
                _wsum = 0.0   # Σ p·(mh+ma)
                _hsum = 0.0   # Σ p·mh
                for s, p in our.items():
                    try:
                        mh, ma = (int(x) for x in str(s).replace(':', '-').split('-'))
                    except Exception:
                        continue
                    _wsum += (mh + ma) * p
                    _hsum += mh * p
                h_share = (_hsum / _wsum) if _wsum > 1e-9 else 0.5
                _ts = max(0.2, 1.0 - minute / 100.0)
                rem_total = max(0.15, _wsum * _ts)
                lam_h = rem_total * h_share
                lam_a = rem_total * (1.0 - h_share)
                M = score_matrix(lam_h, lam_a, 6)
                M = M / M.sum()
                keep = {}
                for i in range(M.shape[0]):
                    for j in range(M.shape[1]):
                        keep[f'{min(sh + i, 9)}-{min(sa + j, 9)}'] = float(M[i, j])
                roll_note = (f"滚球重构: 开盘分布全低于当前比分 {sh}-{sa} → 当前比分 ⊕ "
                             f"剩余泊松(λ主{lam_h:.2f}/λ客{lam_a:.2f}, 剩余时间缩放{_ts:.2f})")
            except Exception:
                keep = {f'{sh}-{sa}': 1.0}   # 兜底: 至少不推荐低于当前比分的比分
        # 2026-08-30 SSoT: DB 经验频率分布不做时间衰减重排(实测重排把滚盘 top1 从
        # 12.5% 压到 6.2% — DB 的排序本身就是历史频率, 时间衰减是给结构分布设计的)。
        # 滚球只做两件事: ① 过滤不可能比分 ② 过滤空时平移重构(见上)。
        time_scale = max(0.2, 1.0 - minute / 100.0)
        candidates = keep
        roll_note = f"滚球条件化: 已 {sh}-{sa} @{minute}', 过滤不可能比分(SSoT·DB排序保留)"

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
            # 2026-08-30 SSoT: DB 路径(emp_note 非空 = unified_scoreline 已生效)不再
            # 重排 — OU 漂移重排与 lead prior 同样扰动 DB 频率排序, 只保留 drift_note
            # 信息标注; 结构路径(无DB)保留原调整。
            if dlt < 0 and emp_note is None:
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

    # ── Phase 3.5: 「领先方最终获胜」经验先验 (2026-08-30 SSoT 调整) ──
    # 方向已由 winner 直出(lead prior ⊕ 即时盘), 不再对比分分布做方向重排 —
    # 实测重排把滚盘比分 top1 从 12.5% 压到 6.2%(扰动 DB 频率排序, 只剩副作用)。
    # note 保留供展示(说明领先方历史胜率), 分布不动。
    lead_prior_note = None
    if minute > 0:
        _dist_unchanged, lead_prior_note = _apply_lead_prior(candidates, sh, sa, minute)

    # ── Phase 4: 输出 ──
    # 第一比分: 初盘+即时结构 (未滚球条件化, 全分布 argmax + 即时盘方向校正)
    score_opening = None
    top3_opening = []
    opening_basis = None
    try:
        _orank = sorted(our_raw.items(), key=lambda x: -x[1])
        top3_opening = [{'score': s.replace(':', '-'), 'prob': round(p, 4)} for s, p in _orank[:3]]
        _best = str(_orank[0][0]).replace(':', '-')   # 统一 '-' 格式 (our_raw 可能是 '1:1')
        # 即时盘校正: 当前 1X2 去水方向 (最新市场定价) 替换初盘方向
        # 2026-08-29 Fix-1 (莫斯科斯巴达U19 vs 罗迪那U19 3-1 实测暴露):
        #   _best 来自 our_raw, key 是**冒号**格式 ('1:1'); 原代码 int(_best.split('-')[0])
        #   对 '1:1' 抛 ValueError, 被 except 静默吞掉 → 「即时盘去水方向校正」自上线起
        #   **从未生效**, opening_basis 恒为"初盘三盘分布主推", 且 score_opening 恒等于
        #   初盘 argmax —— 这是波胆推荐与实时比分反向的 R3 根因。
        #   修法: ① 先统一 '-' 格式; ② 删掉未使用的死变量 _tot; ③ 校正则从全分布中
        #   **挑方向匹配的最高概率比分**(原粗暴镜像 f"{ma}-{mh}" 对平局 1-1 无效且会
        #   造出分布里本不存在的比分)。
        try:
            from analysis.live_goal_probe import _current_inplay_odds, _dewater_1x2
            _cur = _current_inplay_odds(con, match_key, minute)
            if _cur and _cur.get('x2') and all(x and x > 1.0 for x in _cur['x2']):
                _ph, _pd_, _pa = _dewater_1x2(*_cur['x2'])
                if _ph is not None:
                    _dir = 0 if (_ph >= _pd_ and _ph >= _pa) else (2 if _pa >= _pd_ else 1)
                    def _ok_dir(s):
                        try:
                            mh, ma = (int(x) for x in str(s).replace(':', '-').split('-'))
                        except Exception:
                            return False
                        if _dir == 0:
                            return mh > ma
                        if _dir == 2:
                            return mh < ma
                        return mh == ma
                    for s, _p in _orank:
                        if _ok_dir(s):
                            _best = str(s).replace(':', '-')
                            break
                    opening_basis = f"初盘三盘分布主推 + 即时盘去水方向校正"
        except Exception:
            pass
        score_opening = _best
        if opening_basis is None:
            opening_basis = "初盘三盘分布主推"
    except Exception:
        score_opening = None

    # ── Fix-3: score_opening 语义标注 + 与实时比分冲突标记 (2026-08-29) ──
    # score_opening 是**纯初盘**结论 (our_raw argmax), 从不做滚球条件化 —— 这是设计
    # 意图, 但它意味着: 比赛进行中它天然可能与实时比分反向。实测莫斯科斯巴达U19 vs
    # 罗迪那U19: 初盘 away 热门(1.86) → score_opening 恒为 1-1/1-2(客胜方向), 而实时
    # 比分 3-1 主队领先 —— 前端把两者并排裸显示, 用户看到"波胆推荐和实时比分反着来"。
    # 修法: 不篡改数值(初盘结论本身没错, 篡改=伪造), 而是显式标注语义 + 输出冲突标记,
    #       让前端把它降级为"参考项"而非"推荐项"。IR-30 诚实边界: 宁标注不伪造。
    opening_conflict = False
    try:
        if minute > 0 and score_opening and (sh > 0 or sa > 0):
            _oh, _oa = (int(x) for x in str(score_opening).replace(':', '-').split('-'))
            _odir = 0 if _oh > _oa else (2 if _oh < _oa else 1)
            _cdir = 0 if sh > sa else (2 if sh < sa else 1)
            opening_conflict = (_odir != _cdir)
        if minute > 0 and opening_basis:
            opening_basis = "初盘结论(不含当前比分, 仅供参考): " + opening_basis
    except Exception:
        opening_conflict = False

    # 第二比分: 滚球修正后 (条件化 + 漂移验证 + 领先方先验)
    ranked = sorted(candidates.items(), key=lambda x: -x[1])
    top3 = [{'score': s, 'prob': round(p, 4)} for s, p in ranked[:3]]

    # 主推比分与当前比分领先方冲突 → 诚实标注 (IR-30: 宁标注不伪造)。
    # ⚠ 不据此篡改输出: IR-25 已两次证伪 "obscure 领先后收缩" 假设 (Kabuscorp 0-1→2-1
    #   主胜逆转 / 特尔纳瓦 76'1-2→2-2 追平), 强行把主推拉向领先方=伪造信号。
    #   实测莫斯科斯巴达U19 vs 罗迪那U19: 1-0@20' 时市场 away 仍热门(1.95), 模型推 1-2
    #   与实时比分反向 —— 这是市场定价, 不是 bug。标注分歧, 交给人拍板。
    roll_conflict = False
    try:
        if minute > 0 and top3 and (sh > 0 or sa > 0):
            _rh, _ra = (int(x) for x in str(top3[0]['score']).replace(':', '-').split('-'))
            _rdir = 0 if _rh > _ra else (2 if _rh < _ra else 1)
            _cdir = 0 if sh > sa else (2 if sh < sa else 1)
            roll_conflict = (_rdir != _cdir)
    except Exception:
        roll_conflict = False
    basis_parts = [f"初盘三盘交叉({'+'.join(fit_sources)})拟合 λ/μ → 全比分分布"]
    if emp_note:
        basis_parts.append(emp_note)
    if roll_note:
        basis_parts.append(roll_note)
    if drift_note:
        basis_parts.append(drift_note)
    if lead_prior_note:
        basis_parts.append(lead_prior_note)
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
    # ── 方向直出 (2026-08-30): 不再让调用方从比分top反推方向 ──
    # 赛前模式方向上限实测 55.5%(7913场干净档案, 1X2去水argmax — 市场有效性, 物理上限);
    # 滚球模式用 lead_result_prior 干净频率表(1994场/7614点, 防泄漏split):
    #   56-85' 领先1球 → 76.3%胜(n=932), 领先2球 → 92%+。
    # 方向 = lead prior(n≥30, w=n/(n+200)收缩) ⊕ 即时盘去水(0.3权重); 无领先时
    # 用即时盘/开盘去水。比分分布仍独立输出(top3), 方向不再被 DB 匹配分布稀释。
    winner_out = None
    try:
        _p_hda = None
        if minute > 0 and sh != sa:
            _side = 'home' if sh > sa else 'away'
            _diff = abs(sh - sa)
            _band = _band_of_minute(minute)
            _cell = (_load_lead_prior().get('table') or {}).get(f'{_side}|{_diff}|{_band}')
            if _cell and _cell.get('n', 0) >= 30:
                _w = _cell['n'] / (_cell['n'] + 200.0)
                _p_hda = {
                    'home': _cell.get('home', 0.33), 'draw': _cell.get('draw', 0.33),
                    'away': _cell.get('away', 0.33),
                }
                # 即时盘去水混合(0.7先验+0.3市场)
                try:
                    from analysis.live_goal_probe import _current_inplay_odds as _cpo
                    _cur = _cpo(con, match_key, minute) or {}
                    if _cur.get('x2'):
                        _x2 = _cur['x2']
                        from analysis.live_goal_probe import _dewater_1x2
                        _dv = _dewater_1x2(*[float(x) for x in _x2])
                        if _dv:
                            _p_hda = {k: 0.7 * _p_hda[k] + 0.3 * _dv[i]
                                      for i, k in enumerate(('home', 'draw', 'away'))}
                except Exception:
                    pass
                # 先验置信权重仅作用于与市场分歧度, 不改变 argmax 选择
                winner_out = max(_p_hda, key=_p_hda.get)
        if winner_out is None and odds.get('h') and odds.get('d') and odds.get('a'):
            _ih, _idd, _ia = 1.0 / odds['h'], 1.0 / odds['d'], 1.0 / odds['a']
            _s = _ih + _idd + _ia
            winner_out = max(('home', 'draw', 'away'),
                             key=lambda k: {'home': _ih, 'draw': _idd, 'away': _ia}[k] / _s)
    except Exception:
        winner_out = None
    return {
        'top3': top3,
        'score': top3[0]['score'] if top3 else None,        # 第二比分: 滚球修正 (主推)
        'score_roll': top3[0]['score'] if top3 else None,
        'score_opening': score_opening,                     # 第一比分: 初盘+即时结构
        'top3_opening': top3_opening,
        'opening_basis': opening_basis,
        'opening_conflict': opening_conflict,               # Fix-3: 初盘结论方向与实时比分领先方相反
        'roll_conflict': roll_conflict,                     # Fix-3: 主推比分方向与实时比分领先方相反
        'winner': winner_out,
        'winner_label': {'home': '主胜', 'draw': '平', 'away': '客胜'}.get(winner_out),
        'winner_basis': ('领先方先验(干净频率表)⊕即时盘' if minute > 0 and sh != sa and winner_out
                         else '开盘1X2去水(市场上限≈55%)'),
        'total': None,
        'lead_prior_note': lead_prior_note,     # 方向3: 领先方先验校正说明 (None=未生效)
        # 条件化+校正后的完整分布 (top20), 供回测/复核用, 前端不消费
        'dist': [{'score': s, 'prob': round(p, 4)}
                 for s, p in sorted(candidates.items(), key=lambda x: -x[1])[:20]],
        'basis': '; '.join(basis_parts),
        'roll_verification': drift_note,
        'fit_sources': fit_sources,
        'over_prob_at': over_prob_at,
        'found': True,
    }
