"""
哨响AI - 赔率破解模型综合分析 v1.1
====================================
对单场比赛运行全部4+1个赔率破解模型，生成综合研判报告。

模型清单:
  1. HarvestingGuard   — 收割信号检测 (HRS评分)
  2. AORE Pipeline      — 角色互换逆向推演 (庄家隐藏比分)
  3. ScoreDistSimulator — 泊松比分分布 (理论概率基础)
  4. UpsetDetector      — 冷门信号融合 (8维upset评分)
  5. OTSM StateMachine  — 赔率时序状态机 (LOCKED/ACTIVE/NOISE/DECOY)

用法:
  python odds_crack_analysis.py
"""

import sys, os, json, math
import numpy as np
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ===================== 数据: 澳大利亚 vs 土耳其 赛前欧赔 (06月14日 12:00 GMT+8) =====================

MATCH = {
    'home_team': '澳大利亚',
    'away_team': '土耳其',
    'league': '世界杯',
    'kickoff': '2026-06-14 12:00 GMT+8',
}

# 全场独赢
ODDS_1X2 = {'home': 4.95, 'draw': 3.75, 'away': 1.71}

# 全场大小球
ODDS_TOTALS_25 = {'line': 2.5, 'over': 2.08, 'under': 1.82}

# 全场让球
ODDS_AH_075 = {'line': -0.75, 'home_cover': 1.99, 'away_cover': 1.93}  # +0.5/1 from screenshot
ODDS_AH_05 = {'line': -0.5, 'home_cover': 2.26, 'away_cover': 1.71}     # +0.5 Asian

# 半场独赢
ODDS_HT_1X2 = {'home': 5.10, 'draw': 2.21, 'away': 2.47}

# 半场大小球
ODDS_HT_TOTALS = {'line': 1.0, 'over': 1.98, 'under': 1.92}

# 半场让球
ODDS_HT_AH = {'line': -0.25, 'home_cover': 1.92, 'away_cover': 1.98}

# 比分 (从滚球盘/波胆)
CS_ODDS = {
    (1,0): 3.40,  (2,0): 10.0,  (2,1): 9.90,  (3,0): 50.0,
    (3,1): 48.0,  (3,2): 80.0,  (4,0): 173,
    (0,0): None,  (1,1): 3.45,  (2,2): 18.5,  (3,3): 145, (4,4): 247,
    (0,1): None,  (0,2): None,  (1,2): 6.60,  (0,3): None,
    (1,3): 18.0,  (2,3): 47.0,
}
CS_ODDS_CLEAN = {k: v for k, v in CS_ODDS.items() if v is not None}


# ===================== 基础函数 =====================

def implied_probs_1x2(odds_dict):
    """去抽水隐含概率"""
    raw = {k: 1.0/v for k, v in odds_dict.items()}
    total = sum(raw.values())
    return {k: v/total for k, v in raw.items()}, total - 1.0  # probs, overround

def implied_probs_2outcome(odds_dict):
    """两选项去抽水"""
    raw = {k: 1.0/v for k, v in odds_dict.items()}
    total = sum(raw.values())
    return {k: v/total for k, v in raw.items()}, total - 1.0

def kelly_stake(prob, odds, bankroll=100):
    """凯利公式: 投注比例"""
    b = odds - 1.0  # 净赔率
    p = prob
    f = (p * b - (1 - p)) / b if b > 0 else 0
    return max(0, round(f * bankroll, 1))

def ev_bet(prob, odds):
    """期望值"""
    return prob * odds - 1.0

# ===================== 模型1: HarvestingGuard =====================

def run_harvesting_guard():
    """收割信号检测"""
    sys.path.insert(0, os.path.dirname(__file__))
    from bookmaker_sim.harvesting_guard import HarvestingGuard

    guard = HarvestingGuard()

    # 计算模型总进球预期 (从1X2隐含概率粗略估计)
    probs, _ = implied_probs_1x2(ODDS_1X2)
    # 简化: 用1X2概率估计 lambda
    # P(H) ~ 0.19 → λ_h ≈ 0.7, λ_a ≈ 1.5 (if away favored)
    # 从1X2: H=19%, D=25%, A=56% → estimated total λ ≈ 2.0-2.4
    model_total_lambda = 2.3  # 模型预期的全场总进球

    report = guard.scan(
        odds_1x2=ODDS_1X2,
        odds_totals=ODDS_TOTALS_25,
        odds_ah=ODDS_AH_075,
        odds_cs=CS_ODDS_CLEAN,
        league='世界杯',
        model_total_lambda=model_total_lambda,
    )

    return report


# ===================== 模型2: AORE 逆向推演 =====================

def run_aore_manual():
    """手动运行AORE角色互换逆向推演 (不需要DB中的match_id)"""
    sys.path.insert(0, os.path.dirname(__file__))
    from bookmaker_sim.score_distribution import ScoreDistSimulator
    from bookmaker_sim.market_derivation import MarketDerivationEngine

    sim = ScoreDistSimulator()
    engine = MarketDerivationEngine(default_margin=0.06)

    # Step 1: 提取真实赔率隐含概率向量
    probs_1x2, ovr_1x2 = implied_probs_1x2(ODDS_1X2)
    probs_totals, ovr_totals = implied_probs_2outcome({
        'over': ODDS_TOTALS_25['over'],
        'under': ODDS_TOTALS_25['under']
    })
    probs_ah, ovr_ah = implied_probs_2outcome({
        'home_cover': ODDS_AH_05['home_cover'],
        'away_cover': ODDS_AH_05['away_cover']
    })

    # 构建真实赔率概率向量 [P(H), P(D), P(A), P(O2.5), P(H-cover-AH-0.5), P(BTTS), P(U1.5)]
    real_vector = np.zeros(7)
    real_vector[0] = probs_1x2['home']
    real_vector[1] = probs_1x2['draw']
    real_vector[2] = probs_1x2['away']
    real_vector[3] = probs_totals['over']
    real_vector[4] = probs_ah['home_cover']

    # 估计 BTTS 和 U1.5 (如果比分数据里有线索)
    # 从比分赔率反推 BTTS
    cs_probs_raw = {}
    for (sh, sa), odds in CS_ODDS_CLEAN.items():
        cs_probs_raw[(sh, sa)] = 1.0 / odds
    total_cs = sum(cs_probs_raw.values())
    # CS隐含的BTTS = P(双方都有进球) / total
    btts_from_cs = sum(p for (sh, sa), p in cs_probs_raw.items() if sh >= 1 and sa >= 1) / total_cs
    real_vector[5] = btts_from_cs

    u15_from_cs = sum(p for (sh, sa), p in cs_probs_raw.items() if sh + sa < 1.5) / total_cs
    real_vector[6] = u15_from_cs

    # Step 2: 搜索最优比分 (AORE核心)
    search_result = sim.search_optimal_score(real_vector, sigma_hiding=0.35, max_score=5)

    # Step 3: 对比不同sigma_hiding
    sigma_results = {}
    for sigma in [0.2, 0.35, 0.5]:
        sr = sim.search_optimal_score(real_vector, sigma_hiding=sigma, max_score=5)
        sigma_results[sigma] = sr

    # Step 4: 校准比分分布
    # 从1X2概率逆向估计lambda
    def estimate_lambda_from_probs(ph, pd, pa, max_iter=1000):
        """从H/D/A概率估计泊松lambda参数"""
        best_kl = float('inf')
        best_lams = (1.0, 1.0)
        # 网格搜索
        for lh in np.linspace(0.3, 3.0, 30):
            for la in np.linspace(0.3, 3.0, 30):
                dist = sim.dixon_coles(lh, la)
                sp = np.array([dist.prob_home_win(), dist.prob_draw(), dist.prob_away_win()])
                tp = np.array([ph, pd, pa])
                eps = 1e-10
                kl = np.sum(tp * np.log((tp + eps) / (sp + eps)))
                if kl < best_kl:
                    best_kl = kl
                    best_lams = (lh, la)
        return best_lams, best_kl

    lam_h, lam_a = estimate_lambda_from_probs(
        probs_1x2['home'], probs_1x2['draw'], probs_1x2['away']
    )[0]
    score_dist = sim.dixon_coles(lam_h, lam_a)

    # 推导公平赔率
    fair_markets = engine.derive_all_markets(score_dist)

    return {
        'real_vector': real_vector,
        'search_result': search_result,
        'sigma_results': sigma_results,
        'estimated_lambda': (lam_h, lam_a),
        'score_distribution': score_dist,
        'fair_markets': fair_markets,
        'probs_1x2': probs_1x2,
        'overrounds': {'1x2': ovr_1x2, 'totals': ovr_totals, 'ah': ovr_ah},
    }


# ===================== 模型3: Poisson比分分布 =====================

def run_poisson_score_analysis(aore_result):
    """基于泊松分布的比分预测 & 凯利投注"""
    sys.path.insert(0, os.path.dirname(__file__))
    from bookmaker_sim.score_distribution import ScoreDistSimulator

    sim = ScoreDistSimulator()

    lam_h, lam_a = aore_result['estimated_lambda']
    dist = aore_result['score_distribution']

    # 计算各比分概率 & 对比市场赔率
    score_table = []
    for s_h in range(6):
        for s_a in range(6):
            prob = dist.prob(s_h, s_a)
            if prob < 0.001:
                continue
            fair_odds = 1.0 / prob
            market_odds = CS_ODDS.get((s_h, s_a))
            if market_odds is None:
                market_odds_str = '未开'
                ev_val = None
                kelly_val = None
            else:
                ev_val = ev_bet(prob, market_odds)
                kelly_val = kelly_stake(prob, market_odds, 100)

            score_table.append({
                'score': f"{s_h}-{s_a}",
                'prob': prob,
                'fair_odds': round(fair_odds, 2),
                'market_odds': market_odds_str if market_odds is None else market_odds,
                'ev': ev_val,
                'kelly_100': kelly_val,
            })

    score_table.sort(key=lambda x: -x['prob'])

    # 凯利推荐的4个比分
    valid_ev = [s for s in score_table if s['ev'] is not None and s['ev'] > 0]
    valid_ev.sort(key=lambda x: -x['ev'])
    kelly_recommendations = valid_ev[:4]

    # 1X2概率
    prob_h = dist.prob_home_win()
    prob_d = dist.prob_draw()
    prob_a = dist.prob_away_win()

    # Over/Under概率
    prob_over25 = dist.prob_total_over(2.5)
    prob_under25 = dist.prob_total_under(2.5)
    prob_btts = dist.prob_btts()

    return {
        'lambda_h': lam_h,
        'lambda_a': lam_a,
        'prob_h': prob_h,
        'prob_d': prob_d,
        'prob_a': prob_a,
        'prob_over25': prob_over25,
        'prob_under25': prob_under25,
        'prob_btts': prob_btts,
        'expected_total': dist.expected_total_goals(),
        'most_likely': dist.most_likely_score(),
        'top_scores': score_table[:15],
        'kelly_4': kelly_recommendations,
    }


# ===================== 模型4: UpsetDetector =====================

def run_upset_analysis():
    """冷门信号检测 (使用核心算法，不依赖DB)"""
    # 1. 赔率分歧: 市场隐含 vs 均匀分布偏差
    probs_1x2, _ = implied_probs_1x2(ODDS_1X2)
    market_favorite = max(probs_1x2, key=probs_1x2.get)
    market_favorite_prob = probs_1x2[market_favorite]

    signals = {}

    # Signal 1: 赔率背离 — 市场过度倾斜
    # Away=58.5% 意味着市场极度偏客胜
    max_prob = market_favorite_prob
    min_prob = min(probs_1x2.values())
    odds_divergence = max_prob - min_prob  # 偏差越大 = 市场越自信
    signals['odds_divergence'] = min(1.0, odds_divergence / 0.6)

    # Signal 2: 市场过度自信 — 客胜赔率<1.80 = 极度看好
    fav_odds = ODDS_1X2['away'] if market_favorite == 'away' else (
        ODDS_1X2['home'] if market_favorite == 'home' else ODDS_1X2['draw']
    )
    signals['market_overconfidence'] = min(1.0, max(0, 1.0 - (fav_odds - 1.3) / 1.2))

    # Signal 3: 高赔率方向 (>3.0) — 主胜赔率4.95说明市场认为主胜是冷门
    high_odds_count = sum(1 for v in ODDS_1X2.values() if v > 3.0)
    signals['high_odds_signal'] = high_odds_count / 3.0

    # Signal 4: 让球盘与实力不匹配
    # AH +0.75表示主队受让0.75球 → 市场认为客队明显强于主队
    probs_ah, _ = implied_probs_2outcome({
        'home_cover': ODDS_AH_075['home_cover'],
        'away_cover': ODDS_AH_075['away_cover']
    })
    ah_gap = abs(probs_ah['home_cover'] - probs_ah['away_cover'])
    signals['ah_trap'] = min(1.0, ah_gap / 0.3)

    # Signal 5: 大小球信号 — O2.5@2.08 U2.5@1.82 → 市场偏小球
    probs_totals, _ = implied_probs_2outcome({
        'over': ODDS_TOTALS_25['over'],
        'under': ODDS_TOTALS_25['under']
    })
    under_premium = probs_totals['under'] - probs_totals['over']
    signals['under_bias'] = min(1.0, under_premium / 0.3)

    # 综合 Upset Score (加权)
    weights = {
        'odds_divergence': 0.25,
        'market_overconfidence': 0.20,
        'high_odds_signal': 0.15,
        'ah_trap': 0.20,
        'under_bias': 0.20,
    }
    upset_score = sum(v * weights[k] for k, v in signals.items())
    upset_score = min(1.0, upset_score)

    # 等级
    if upset_score > 0.7:
        level = 'HIGH'
    elif upset_score > 0.4:
        level = 'MEDIUM'
    else:
        level = 'LOW'

    return {
        'upset_score': upset_score,
        'level': level,
        'signals': signals,
        'market_favorite': {'side': market_favorite, 'prob': market_favorite_prob, 'odds': fav_odds},
    }


# ===================== 模型5: OTSM 状态推断 =====================

def run_otsm_analysis():
    """赔率时序状态机分析 (手动模拟开→收变化)"""
    # 由于没有实时赔率时序数据，使用开赔vs收赔的简化分析
    # 开赔数据使用市场开盘赔率，收赔使用当前赔率

    # 半场独赢 → 全场独赢 可以视为"开→收"的简化代理
    ht_probs, _ = implied_probs_1x2(ODDS_HT_1X2)
    ft_probs, _ = implied_probs_1x2(ODDS_1X2)

    # 熵漂移: 从半场→全场，概率分布的熵变化
    def entropy(probs):
        eps = 1e-10
        return -sum(p * math.log(p + eps) for p in probs.values())

    h_ht = entropy(ht_probs)
    h_ft = entropy(ft_probs)
    entropy_drift = h_ft - h_ht  # 负=收敛, 正=发散

    # 方向漂移: 主胜/客胜的概率变化
    h_dir_drift = ft_probs['home'] - ht_probs['home']
    d_dir_drift = ft_probs['draw'] - ht_probs['draw']
    a_dir_drift = ft_probs['away'] - ht_probs['away']

    # 水位加速度: overround变化
    raw_ht = sum(1/v for v in ODDS_HT_1X2.values())
    raw_ft = sum(1/v for v in ODDS_1X2.values())
    ovr_ht = raw_ht - 1.0
    ovr_ft = raw_ft - 1.0
    water_accel = ovr_ft - ovr_ht  # 正=庄家增加抽水(不确信)

    # 凯利涨落: 市场对热门(客胜)的重估
    kelly_fluct = ft_probs['away'] - ht_probs['away']

    # 状态推断
    if abs(entropy_drift) < 0.02 and abs(kelly_fluct) < 0.03:
        state = 'LOCKED'
        lock_conf = 0.8
    elif abs(kelly_fluct) > 0.05 and abs(entropy_drift) > 0.03:
        state = 'ACTIVE'
        lock_conf = 0.5
    elif abs(kelly_fluct) < 0.01:
        state = 'NOISE'
        lock_conf = 0.1
    else:
        # 检查方向矛盾: 半场→全场，客胜概率上升但主胜赔率极高
        # 如果HA半场→全场变化方向一致 → 真实信号
        # 如果矛盾 → DECOY
        ht_fav = max(ht_probs, key=ht_probs.get)
        ft_fav = max(ft_probs, key=ft_probs.get)
        if ht_fav == ft_fav:
            state = 'ACTIVE'
            lock_conf = 0.5
        else:
            state = 'DECOY'
            lock_conf = 0.3

    return {
        'state': state,
        'lock_confidence': lock_conf,
        'entropy_drift': round(entropy_drift, 4),
        'water_accel': round(water_accel, 4),
        'kelly_fluctuation': round(kelly_fluct, 4),
        'home_dir_drift': round(h_dir_drift, 4),
        'draw_dir_drift': round(d_dir_drift, 4),
        'away_dir_drift': round(a_dir_drift, 4),
        'ovr_ht': round(ovr_ht, 4),
        'ovr_ft': round(ovr_ft, 4),
    }


# ===================== 综合分析 & 报告生成 =====================

def generate_report():
    """生成综合分析报告"""
    print("=" * 70)
    print("  哨响AI — 赔率破解模型综合分析")
    print(f"  比赛: {MATCH['home_team']} vs {MATCH['away_team']}")
    print(f"  时间: {MATCH['kickoff']}  联赛: {MATCH['league']}")
    print("=" * 70)

    # ── 基础赔率分析 ──
    print("\n" + "─" * 50)
    print("  【0】基础赔率 & 市场隐含概率")
    print("─" * 50)

    # 1X2
    probs_1x2, ovr_1x2 = implied_probs_1x2(ODDS_1X2)
    print(f"\n  全场独赢: H={ODDS_1X2['home']:.2f} D={ODDS_1X2['draw']:.2f} A={ODDS_1X2['away']:.2f}")
    print(f"  隐含概率: H={probs_1x2['home']:.1%} D={probs_1x2['draw']:.1%} A={probs_1x2['away']:.1%}")
    print(f"  抽水率: {ovr_1x2:.2%}")

    probs_totals, ovr_totals = implied_probs_2outcome({
        'over': ODDS_TOTALS_25['over'], 'under': ODDS_TOTALS_25['under']
    })
    print(f"\n  大小球2.5: O={ODDS_TOTALS_25['over']:.2f} U={ODDS_TOTALS_25['under']:.2f}")
    print(f"  隐含: O={probs_totals['over']:.1%} U={probs_totals['under']:.1%} 抽水={ovr_totals:.2%}")

    probs_ah, ovr_ah = implied_probs_2outcome({
        'home_cover': ODDS_AH_05['home_cover'], 'away_cover': ODDS_AH_05['away_cover']
    })
    print(f"\n  亚盘+0.5: 主={ODDS_AH_05['home_cover']:.2f} 客={ODDS_AH_05['away_cover']:.2f}")
    print(f"  隐含: 主覆盖={probs_ah['home_cover']:.1%} 客覆盖={probs_ah['away_cover']:.1%}")

    # ── 模型1: HarvestingGuard ──
    print("\n" + "─" * 50)
    print("  【1】HarvestingGuard — 收割信号检测")
    print("─" * 50)

    hg_report = run_harvesting_guard()
    print(f"\n  HRS (收割风险评分): {hg_report.hrs:.3f}")
    print(f"  风险等级: {hg_report.risk_level}")
    print(f"  检测置信度: {hg_report.confidence:.2f}")
    print(f"\n  分维度信号:")
    print(f"    1X2:      {hg_report.signal_1x2:.3f}")
    print(f"    Totals:   {hg_report.signal_totals:.3f}")
    print(f"    AH:       {hg_report.signal_ah:.3f}")
    print(f"    CS:       {hg_report.signal_cs:.3f}")
    print(f"    交叉盘口: {hg_report.signal_cross_market:.3f}")
    print(f"\n  异常信号 ({len(hg_report.anomalies)}个):")
    for a in hg_report.anomalies:
        print(f"    [{a.market}] {a.description}")
    print(f"\n  引诱方向: {hg_report.baited_direction}")
    print(f"  抑制方向: {hg_report.suppressed_direction}")
    print(f"  尾端风险: {hg_report.tail_risk_factor:.3f}")
    print(f"  极端比分概率: {hg_report.extreme_score_prob:.1%}")
    print(f"\n  建议: {hg_report.recommendation}")

    # ── 模型2: AORE 逆向推演 ──
    print("\n" + "─" * 50)
    print("  【2】AORE — 角色互换逆向推演")
    print("─" * 50)

    aore = run_aore_manual()
    sr = aore['search_result']
    print(f"\n  估计 lambda: 主={aore['estimated_lambda'][0]:.2f} 客={aore['estimated_lambda'][1]:.2f}")
    print(f"\n  逆向推演最可能比分: {sr['best_score_h']}-{sr['best_score_a']}")
    print(f"  KL散度: {sr['best_anomaly']:.6f}")
    print(f"\n  Top 5 候选比分 (庄家隐藏预期):")
    for i, r in enumerate(sr['top5']):
        marker = ''
        if i == 0:
            marker = ' <-- 最匹配'
        print(f"    {i+1}. {r['score_h']}-{r['score_a']}: KL={r['kl_divergence']:.6f}{marker}")

    print(f"\n  不同sigma_hiding下的一致性:")
    for sigma, res in aore['sigma_results'].items():
        best = res['top5'][0]
        print(f"    sigma={sigma}: {best['score_h']}-{best['score_a']} (KL={best['kl_divergence']:.6f})")

    # ── 模型3: Poisson比分分布 ──
    print("\n" + "─" * 50)
    print("  【3】Poisson比分分布 & 凯利投注")
    print("─" * 50)

    poisson = run_poisson_score_analysis(aore)
    print(f"\n  Poisson参数: lambda_h={poisson['lambda_h']:.2f} lambda_a={poisson['lambda_a']:.2f}")
    print(f"  预期总进球: {poisson['expected_total']:.2f}")
    print(f"\n  1X2概率:")
    print(f"    主胜(H): {poisson['prob_h']:.1%}")
    print(f"    平局(D): {poisson['prob_d']:.1%}")
    print(f"    客胜(A): {poisson['prob_a']:.1%}")
    print(f"\n  大小球:")
    print(f"    Over 2.5:  {poisson['prob_over25']:.1%} (市场隐含={probs_totals['over']:.1%})")
    print(f"    Under 2.5: {poisson['prob_under25']:.1%} (市场隐含={probs_totals['under']:.1%})")
    print(f"    BTTS:      {poisson['prob_btts']:.1%}")
    best_sh, best_sa, best_p = poisson['most_likely']
    print(f"\n  最可能比分: {best_sh}-{best_sa} (p={best_p:.2%})")

    print(f"\n  Top 15 比分概率 & 市场对比:")
    print(f"  {'比分':>6}  {'概率':>7}  {'公平赔率':>8}  {'市场赔率':>8}  {'EV':>7}  {'凯利(100)':>9}")
    print(f"  {'─'*57}")
    for s in poisson['top_scores']:
        ev_str = f"{s['ev']:+.1%}" if s['ev'] is not None else 'N/A'
        kl_str = f"{s['kelly_100']:.0f}元" if s['kelly_100'] is not None else 'N/A'
        odds_str = f"{s['market_odds']}" if isinstance(s['market_odds'], str) else f"{s['market_odds']:.2f}"
        print(f"  {s['score']:>6}  {s['prob']:>6.2%}  {s['fair_odds']:>8.2f}  {odds_str:>8}  {ev_str:>7}  {kl_str:>9}")

    print(f"\n  凯利推荐的4个比分投注 (100元分配):")
    total_kelly = sum(s['kelly_100'] for s in poisson['kelly_4'] if s['kelly_100'])
    for i, s in enumerate(poisson['kelly_4']):
        if s['kelly_100'] is not None:
            print(f"    {i+1}. {s['score']} @{s['market_odds']:.2f}: "
                  f"概率={s['prob']:.2%}, EV={s['ev']:+.1%}, 投{s['kelly_100']:.0f}元")

    # ── 模型4: UpsetDetector ──
    print("\n" + "─" * 50)
    print("  【4】UpsetDetector — 冷门信号融合")
    print("─" * 50)

    upset = run_upset_analysis()
    print(f"\n  Upset Score: {upset['upset_score']:.3f} ({upset['level']})")
    print(f"  市场最看好: {upset['market_favorite']['side']} "
          f"(概率={upset['market_favorite']['prob']:.1%}, 赔率={upset['market_favorite']['odds']:.2f})")
    print(f"\n  分维度信号:")
    for k, v in upset['signals'].items():
        print(f"    {k}: {v:.3f}")

    # ── 模型5: OTSM ──
    print("\n" + "─" * 50)
    print("  【5】OTSM — 赔率时序状态机")
    print("─" * 50)

    otsm = run_otsm_analysis()
    print(f"\n  当前状态: {otsm['state']}")
    print(f"  锁定置信度: {otsm['lock_confidence']:.2f}")
    print(f"  熵漂移: {otsm['entropy_drift']:.4f} (负=收敛)")
    print(f"  水位加速度: {otsm['water_accel']:.4f} (正=抽水增加)")
    print(f"  凯利涨落: {otsm['kelly_fluctuation']:.4f}")
    print(f"\n  方向漂移 (半场→全场):")
    print(f"    主胜: {otsm['home_dir_drift']:+.1%}")
    print(f"    平局: {otsm['draw_dir_drift']:+.1%}")
    print(f"    客胜: {otsm['away_dir_drift']:+.1%}")
    print(f"\n  抽水率: 半场={otsm['ovr_ht']:.2%} → 全场={otsm['ovr_ft']:.2%}")

    # ── 综合研判 ──
    print("\n" + "=" * 70)
    print("  【综合研判】")
    print("=" * 70)

    # 各模型信号汇总
    verdicts = []

    # HG信号
    if hg_report.risk_level in ('HIGH', 'CRITICAL'):
        verdicts.append(f"[HG] 收割风险={hg_report.risk_level} (HRS={hg_report.hrs:.2f})")
    elif hg_report.risk_level == 'MEDIUM':
        verdicts.append(f"[HG] 中等收割信号 (HRS={hg_report.hrs:.2f})")

    # AORE
    aore_score = f"{sr['best_score_h']}-{sr['best_score_a']}"
    verdicts.append(f"[AORE] 逆向推演比分={aore_score} (KL={sr['best_anomaly']:.4f})")

    # Poisson vs Market
    model_fav = 'H' if poisson['prob_h'] > max(poisson['prob_d'], poisson['prob_a']) else \
                ('D' if poisson['prob_d'] > max(poisson['prob_h'], poisson['prob_a']) else 'A')
    market_fav = 'H' if probs_1x2['home'] > max(probs_1x2['draw'], probs_1x2['away']) else \
                 ('D' if probs_1x2['draw'] > max(probs_1x2['home'], probs_1x2['away']) else 'A')

    if model_fav != market_fav:
        verdicts.append(f"[Poisson] 方向分歧: 模型偏{model_fav} vs 市场偏{market_fav}")
    else:
        verdicts.append(f"[Poisson] 方向一致: {model_fav} ({poisson['prob_h']:.0%}/{poisson['prob_d']:.0%}/{poisson['prob_a']:.0%})")

    # Upset
    if upset['upset_score'] > 0.4:
        verdicts.append(f"[Upset] 冷门信号={upset['level']} (score={upset['upset_score']:.2f})")
    else:
        verdicts.append(f"[Upset] 无明显冷门信号 (score={upset['upset_score']:.2f})")

    # OTSM
    verdicts.append(f"[OTSM] 状态={otsm['state']} (锁定置信度={otsm['lock_confidence']:.2f})")

    # 关键矛盾
    print("\n  各模型核心发现:")
    for i, v in enumerate(verdicts, 1):
        print(f"    {i}. {v}")

    # 核心矛盾
    print(f"\n  >>> 核心矛盾 <<<")
    print(f"  市场赔率说: 客胜概率 {probs_1x2['away']:.0%} (A@1.71)")
    print(f"  泊松模型说: 客胜概率 {poisson['prob_a']:.0%}")
    print(f"  AORE推演说: {aore_score} (庄家隐藏意图)")
    gap = abs(probs_1x2['away'] - poisson['prob_a'])
    print(f"  市场-模型偏差: {gap:.1%}")
    if gap > 0.10:
        print(f"  >>> 严重分歧! 市场可能过度倾斜客胜 <<<")

    # Kelly行动建议
    print(f"\n  >>> 行动建议 <<<")
    if poisson['kelly_4']:
        print(f"  Kelly推荐4个比分投注 (100元):")
        for i, s in enumerate(poisson['kelly_4']):
            print(f"    {i+1}. {s['score']} @{s['market_odds']:.2f} → 投{s['kelly_100']:.0f}元 (EV={s['ev']:+.1%})")
    else:
        print(f"  无正向EV比分投注机会")

    # 风险提示
    if hg_report.risk_level in ('HIGH', 'CRITICAL'):
        print(f"\n  ⚠️ 收割风险={hg_report.risk_level}: 建议减仓或观望")
    if otsm['state'] == 'DECOY':
        print(f"  ⚠️ OTSM状态=DECOY: 赔率可能是诱饵")

    print(f"\n{'='*70}")
    print(f"  分析完成 — 时间: 2026-06-14 13:14")
    print(f"{'='*70}")

    # 返回完整数据供后续使用
    return {
        'harvesting_guard': hg_report,
        'aore': aore,
        'poisson': poisson,
        'upset': upset,
        'otsm': otsm,
        'verdicts': verdicts,
    }


if __name__ == '__main__':
    generate_report()
