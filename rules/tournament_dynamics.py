#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Tournament Dynamics Override — 积分/出线形势修正 D-Gate 判型

D-Gate v5.2.13 只分析赔率结构，不感知积分榜和出线形势。
本模块在 D-Gate 输出之上叠加赛事动态修正，覆盖四种典型场景：

  R1: 已出线轮换 — 某队已出线，对手必须抢分 → 降低已出线队权重
  R2: 已淘汰送分 — 某队已淘汰，对手必须赢 → 强化对手 + 封杀Mode C平局
  R3: 双赢默契   — 两队打平都能出线 → 平局权重大幅提升
  R4: 生死战     — 两队都必须赢 → 不做修正，原样输出

MatchDay 权重:
  MD1: 0% 赛事动态 / 100% D-Gate (没有积分数据)
  MD2: 30% 赛事动态 / 70% D-Gate  (积分格局初现)
  MD3: 60% 赛事动态 / 40% D-Gate  (出线/淘汰格局清晰)

使用方式:
    from rules.tournament_dynamics import predict

    verdict, mode, d_boost, signals = predict(
        ph, pd, pa, oh, od, oa, hcp, ou, home, away, cs_other
    )
    # 自动从 StandingsProvider 获取积分数据
"""


# ═══════════════════════════════════════
# MatchDay 权重
# ═══════════════════════════════════════

def _get_matchday_weights(matchday):
    """返回 (tournament_weight, dgate_weight)"""
    if matchday <= 1:
        return 0.0, 1.0
    elif matchday == 2:
        return 0.30, 0.70
    else:
        return 0.60, 0.40


# ═══════════════════════════════════════
# 辅助判断函数
# ═══════════════════════════════════════

def _is_qualified(pts, mp):
    """判断球队是否已确保小组前2出线。

    保守策略: 6分+ → 大概率已出线。精确判断需完整小组表。
    """
    if mp >= 2 and pts >= 6:
        return True
    return False


def _is_eliminated(pts, mp, group_table, team):
    """判断球队是否已淘汰。

    条件: 0分且剩余比赛不足以追到小组第2。
    末轮(mp=2)时0分=已淘汰。
    """
    if pts == 0 and mp >= 2:
        return True
    # 更精确: 检查是否追不上前2名 (第2名积分)
    if mp >= 2 and pts >= 0:
        # 找出小组其他队的积分，算最大可追
        others = [(t, info['pts']) for t, info in group_table.items() if t != team]
        if len(others) >= 2:
            # 只需追第2名 (sorted倒数第二)，不是第1名
            sorted_pts = sorted([p for _, p in others], reverse=True)
            second_best = sorted_pts[1] if len(sorted_pts) > 1 else sorted_pts[0]
            remaining_games = 3 - mp
            max_possible = pts + remaining_games * 3
            if max_possible < second_best:
                return True
    return False


def _mutual_draw_qualifies(home, away, pts_h, pts_a, group_table):
    """判断两队打平是否都能确保小组前2出线。

    条件: 打平后双方积分都 > 第三名最大可能积分
    """
    draw_pts_h = pts_h + 1
    draw_pts_a = pts_a + 1

    other_pts = []
    for team, info in group_table.items():
        if team not in (home, away):
            other_pts.append(info.get('pts', 0))

    if len(other_pts) < 2:
        return False

    # 第三名最多能拿的分 = 当前积分 + 3
    max_third_pts = sorted(other_pts, reverse=True)[0] + 3

    return draw_pts_h > max_third_pts and draw_pts_a > max_third_pts


def _team_needs_points(pts, mp, group_table, team):
    """判断球队是否必须抢分（不稳/濒临淘汰）。"""
    if mp < 1:
        return True  # 还没踢，当然需要分
    if pts >= 6:
        return False  # 已出线

    others = [(t, info['pts']) for t, info in group_table.items() if t != team]
    if not others:
        return True

    # 如果积分 <= 第三名，需要抢分
    third_best = sorted([p for _, p in others], reverse=True)[min(1, len(others)-1)]
    if pts <= third_best:
        return True

    return False


# ═══════════════════════════════════════
# 核心修正函数
# ═══════════════════════════════════════

def apply_tournament_override(ph, pd, pa, verdict, final_mode, signals,
                               group_table, home, away, matchday=1):
    """
    根据积分榜 + 赛程阶段修正 D-Gate 输出。

    group_table: {team_name: {'pts': int, 'mp': int, 'gf': int, 'ga': int}}
    matchday: 当前比赛轮次 (1/2/3)

    返回: (ph, pd, pa, verdict_new, signals)
    """
    # ── MatchDay 权重 ──
    tw, dw = _get_matchday_weights(matchday)
    if tw == 0:
        return ph, pd, pa, verdict  # MD1: 无修正

    h_info = group_table.get(home, {})
    a_info = group_table.get(away, {})
    pts_h = h_info.get('pts', 0)
    pts_a = a_info.get('pts', 0)
    mp_h = h_info.get('mp', 0)
    mp_a = a_info.get('mp', 0)

    h_qualified = _is_qualified(pts_h, mp_h)
    a_qualified = _is_qualified(pts_a, mp_a)
    h_eliminated = _is_eliminated(pts_h, mp_h, group_table, home)
    a_eliminated = _is_eliminated(pts_a, mp_a, group_table, away)
    h_needs = _team_needs_points(pts_h, mp_h, group_table, home)
    a_needs = _team_needs_points(pts_a, mp_a, group_table, away)
    mutual_draw = _mutual_draw_qualifies(home, away, pts_h, pts_a, group_table)

    override_applied = False

    # ── R1: 已出线轮换 ──
    if h_qualified and a_needs:
        pa = pa * (1 + 0.15 * tw)
        ph = ph * (1 - 0.30 * tw)
        pd = pd * (1 + 0.10 * tw)  # 轮换增加不确定性
        _renorm = ph + pd + pa
        ph, pd, pa = ph / _renorm, pd / _renorm, pa / _renorm
        signals.append(f'R1: {home}已出线→可能轮换, {away}必须抢分')
        override_applied = True

    elif a_qualified and h_needs:
        ph = ph * (1 + 0.15 * tw)
        pa = pa * (1 - 0.30 * tw)
        pd = pd * (1 + 0.10 * tw)
        _renorm = ph + pd + pa
        ph, pd, pa = ph / _renorm, pd / _renorm, pa / _renorm
        signals.append(f'R1: {away}已出线→可能轮换, {home}必须抢分')
        override_applied = True

    # ── R2: 已淘汰送分 ──
    if h_eliminated and a_needs:
        pa = pa * (1 + 0.10 * tw)
        ph = ph * (1 - 0.20 * tw)
        _renorm = ph + pd + pa
        ph, pd, pa = ph / _renorm, pd / _renorm, pa / _renorm
        signals.append(f'R2: {home}已淘汰(0分)→动力不足')

        # R2增强: 必须抢分 vs 已淘汰 → 封杀Mode C平局
        if verdict == 'D' and matchday >= 2:
            strong_side = 'A' if pa > ph else 'H'
            signals.append(f'R2+: {away}必须赢+{home}淘汰→否决平局')
            return ph, pd, pa, strong_side

        override_applied = True

    elif a_eliminated and h_needs:
        ph = ph * (1 + 0.10 * tw)
        pa = pa * (1 - 0.20 * tw)
        _renorm = ph + pd + pa
        ph, pd, pa = ph / _renorm, pd / _renorm, pa / _renorm
        signals.append(f'R2: {away}已淘汰(0分)→动力不足')

        # R2增强: 必须抢分 vs 已淘汰 → 封杀Mode C平局
        if verdict == 'D' and matchday >= 2:
            strong_side = 'H' if ph > pa else 'A'
            signals.append(f'R2+: {home}必须赢+{away}淘汰→否决平局')
            return ph, pd, pa, strong_side

        override_applied = True

    # ── R3: 双赢默契 ──
    if mutual_draw and matchday >= 3:
        pd = pd * (1 + 0.50 * tw)
        _renorm = ph + pd + pa
        ph, pd, pa = ph / _renorm, pd / _renorm, pa / _renorm
        signals.append(f'R3: {home}vs{away}打平双方出线→默契平局可能')
        override_applied = True

    # ── 判型翻转 ──
    if override_applied:
        if pd > ph and pd > pa:
            return ph, pd, pa, 'D'
        elif ph > pa:
            return ph, pd, pa, 'H'
        else:
            return ph, pd, pa, 'A'

    return ph, pd, pa, verdict


# ═══════════════════════════════════════
# 主入口: 一键预测 (自动获取积分)
# ═══════════════════════════════════════

def predict(ph, pd, pa, oh, od, oa, hcp, ou,
            home='', away='', cs_other=None,
            group_table=None, matchday=None,
            standings_provider=None):
    """
    哨响AI 统一预测入口。

    自动流程:
      1. D-Gate v5.2.13 赔率结构判型
      2. StandingsProvider 获取积分+赛程阶段
      3. Tournament Dynamics 4规则修正
      4. MatchDay 权重自适应

    参数:
        ph/pd/pa: 隐含概率
        oh/od/oa: 原始赔率
        hcp: 让球盘口
        ou: 大小球线
        home/away: 球队名
        cs_other: 其它比分赔率
        group_table: 手动传入 (可选, 否则自动查)
        matchday: 手动传入 (可选, 否则自动查)

    返回:
        (verdict, mode, d_boost, signals)
    """
    # ── import D-Gate ──
    try:
        from rules.d_gate_v52 import dgate_v52
    except ImportError:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
        from rules.d_gate_v52 import dgate_v52

    # ── Step 1: D-Gate ──
    tournament_flag = (matchday or 1) >= 1  # 任何赛事阶段都启用杯赛模式
    verdict, final_mode, final_d, signals = dgate_v52(
        ph, pd, pa, oh, od, oa, hcp, ou, home, away, cs_other,
        tournament=tournament_flag
    )

    # ── Step 2: 获取赛事上下文 ──
    if group_table is None or matchday is None:
        if standings_provider is None:
            try:
                from rules.standings_provider import get_provider
                standings_provider = get_provider()
            except ImportError:
                pass

        if standings_provider and standings_provider.available:
            if group_table is None:
                group_table = standings_provider.get_group_table(home, away)
            if matchday is None:
                matchday = standings_provider.matchday
        else:
            group_table = group_table or {}
            matchday = matchday or 1

    # ── Step 3: 赛事动态修正 ──
    if group_table and matchday > 1:
        _, _, _, verdict_new = apply_tournament_override(
            ph, pd, pa, verdict, final_mode, signals,
            group_table, home, away, matchday
        )

        if verdict_new != verdict:
            final_mode = f'{final_mode}+Tourn'
            verdict = verdict_new

    return verdict, final_mode, final_d, signals


# ═══════════════════════════════════════
# 比分预测 (D-Gate判型联动 v5.2.14)
# ═══════════════════════════════════════

def predict_scores(ph, pd, pa, ou, hcp, verdict):
    """
    Poisson 比分推荐，联动 D-Gate 判型 + 让球盘口。

    D-Gate 判 D → λ 减半，偏向低进球
    D-Gate 判 H/A → λ = OU×1.10，按让球分配进球权重

    让球联动: hcp=-1.0 → 主队预期多进1球 → λ差距≈1.0

    返回: [(h,a,prob), ...] 前3个比分
    """
    import math

    # 判型联动: 平局时缩预期进球
    if verdict == 'D':
        lam = ou * 0.50
    else:
        lam = ou * 1.10   # 48场实测

    # 让球分配进球权重
    goal_diff = -hcp  # 主队让球为负, 转为主队预期净胜
    goal_diff = max(-3.0, min(3.0, goal_diff))  # 封顶

    if verdict == 'D':
        # 平局: 双方均衡
        lh = lam * 0.50
        la = lam * 0.50
    else:
        # 按让球拆分总λ
        lh = (lam + goal_diff) / 2
        la = (lam - goal_diff) / 2
        lh = max(0.3, lh)
        la = max(0.3, la)

    def poisson(lam_val, k):
        return (lam_val ** k) * math.exp(-lam_val) / math.factorial(k)

    scores = []
    for h in range(6):
        for a in range(6):
            if h + a > 7:
                continue
            p = poisson(lh, h) * poisson(la, a)
            scores.append((h, a, p))

    scores.sort(key=lambda x: -x[2])
    return scores[:3], lh, la


def predict_with_scores(ph, pd, pa, oh, od, oa, hcp, ou,
                         home='', away='', cs_other=None,
                         group_table=None, matchday=None,
                         standings_provider=None):
    """
    哨响AI 完整预测: 判型 + 比分 + 信号。

    返回: {
        'verdict': 'H'|'D'|'A',
        'winner': '球队名',
        'mode': 'C'|'A'|'B'|...,
        'd_boost': float,
        'signals': [...],
        'scores': [(h,a,prob), ...],
        'lambda_h': float,
        'lambda_a': float,
    }
    """
    verdict, mode, d_boost, signals = predict(
        ph, pd, pa, oh, od, oa, hcp, ou,
        home, away, cs_other,
        group_table=group_table, matchday=matchday,
        standings_provider=standings_provider
    )

    winner = '平局' if verdict == 'D' else (home if verdict == 'H' else away)
    scores, lh, la = predict_scores(ph, pd, pa, ou, hcp, verdict)

    return {
        'verdict': verdict,
        'winner': winner,
        'mode': mode,
        'd_boost': d_boost,
        'signals': signals,
        'scores': [(h, a, round(p, 4)) for h, a, p in scores],
        'lambda_h': round(lh, 2),
        'lambda_a': round(la, 2),
    }


# ═══════════════════════════════════════
# 旧接口兼容
# ═══════════════════════════════════════

def dgate_with_tournament(ph, pd, pa, oh, od, oa, hcp, ou,
                           home='', away='',
                           pts_home=0, pts_away=0, group_table=None,
                           cs_other=None, matchday=None):
    """旧接口, 内部转调新 predict()。"""
    _group_table = group_table or {}
    if pts_home > 0 and home and home not in _group_table:
        _group_table[home] = {'pts': pts_home, 'mp': 2, 'gf': 0, 'ga': 0}
    if pts_away > 0 and away and away not in _group_table:
        _group_table[away] = {'pts': pts_away, 'mp': 2, 'gf': 0, 'ga': 0}
    return predict(ph, pd, pa, oh, od, oa, hcp, ou,
                   home, away, cs_other,
                   group_table=_group_table if _group_table else None,
                   matchday=matchday)


# ═══════════════════════════════════════
# 快速测试
# ═══════════════════════════════════════

if __name__ == '__main__':
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from rules.standings_provider import StandingsProvider

    sp = StandingsProvider()
    md = sp.matchday
    print(f'StandingsProvider: source={sp.source}, matchday={md}\n')

    # 手动传入积分 (覆盖缓存的不准确数据)
    manual = {
        'A': {'墨西哥':6,'韩国':3,'捷克':3,'南非':0},
        'K': {'葡萄牙':4,'哥伦比亚':3,'民主刚果':3,'乌兹别克斯坦':1},
        'L': {'英格兰':4,'加纳':3,'克罗地亚':1,'巴拿马':1},
    }

    def make(grp):
        return {t: {'pts':p, 'mp':2, 'gf':0, 'ga':0} for t,p in manual[grp].items()}

    tests = [
        ('南非','韩国', 5.75,3.90,1.60, 0.5,2.5, make('A'), 'A'),
        ('捷克','墨西哥', 4.52,3.54,1.76, 0.5,2.5, make('A'), 'A'),
        ('哥伦比亚','葡萄牙', 3.60,3.35,2.07, 0.5,2.5, make('K'), 'K'),
        ('巴拿马','英格兰', 11.0,6.30,1.23, 1.75,3.0, make('L'), 'L'),
    ]

    for home, away, oh, od, oa, hcp, ou, gt, grp in tests:
        rh, rd, ra = 1/oh, 1/od, 1/oa
        m = rh+rd+ra
        ph_val, pd_val, pa_val = rh/m, rd/m, ra/m
        verdict, mode, d, signals = predict(
            ph_val, pd_val, pa_val, oh, od, oa, hcp, ou,
            home, away, group_table=gt, matchday=3
        )
        winner = '平局' if verdict == 'D' else (home if verdict == 'H' else away)
        print(f'{grp} {home}vs{away}: {verdict}→{winner} mode={mode}')
        for s in signals:
            if s.startswith('R') or 'Tourn' in s:
                print(f'  └ {s}')
        print()
