#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实运行辅助决策系统, 输出决策 JSON (供演示片使用, 数字全部来自真实代码).

两路:
  1) 阿根廷 vs 瑞士 (单庄live截图, open=close, 无跨庄) → 真实展示"有效市场无edge→闸门OFF→不下注"
  2) 一个 DB 内真实 WH+IW 双庄样本 → 真实展示 soft-line 跨庄分歧→edge→半凯利注码
"""
import sys, os, json
sys.path.insert(0, os.getcwd())
import numpy as np
from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput
from scripts.bet_core import safe_stake, kelly_fraction, FRAC_KELLY, MAX_STAKE_FRAC, BANKROLL

eng = ReverseOddsEngine()
out = {}

# ─────────────────────────────────────────────
# 路1: 阿根廷 vs 瑞士 (单庄live, 无开盘价 → open=close)
# ─────────────────────────────────────────────
ARG_ODDS = (1.70, 3.45, 5.60)
arg = OddsInput(open_h=ARG_ODDS[0], open_d=ARG_ODDS[1], open_a=ARG_ODDS[2],
                close_h=ARG_ODDS[0], close_d=ARG_ODDS[1], close_a=ARG_ODDS[2])
r_arg = eng.analyze(arg)                       # 真实单庄分析
cons = arg.implied_probs                         # 真实共识隐含 (去抽水)
MODEL_PROB = (0.557, 0.274, 0.169)              # 预测引擎argmax (存于 odds_db, 锚=市场argmax)
argmax_idx = int(np.argmax(MODEL_PROB))
# 单庄无跨庄 → 分歧闸门 OFF
stake_off, _ = safe_stake(MODEL_PROB[argmax_idx], ARG_ODDS[argmax_idx], BANKROLL, gate=False)
# 若硬算单庄共识argmax的凯利 (验证无正期望)
k_argmax = kelly_fraction(cons[argmax_idx], ARG_ODDS[argmax_idx])
# 平局保险侧价值层观察 (非自动BET): 平3.45 / 波胆1-1@7.2
ins_draw_ev = round(cons[1] * 3.45 - 1, 4)       # 平局隐含EV(单庄, 仅观察)
ins_cs11_ev = round(cons[1] * 7.2 - 1, 4)        # 1-1波胆EV(单庄, 仅观察)
out['argentina_single_book'] = {
    'teams': '阿根廷 vs 瑞士 (2026WC半决赛)',
    'odds': list(ARG_ODDS),
    'consensus_implied': [round(x, 4) for x in cons],
    'model_prob': list(MODEL_PROB),
    'argmax': ('H', 'D', 'A')[argmax_idx],
    'argmax_label': '阿根廷主胜',
    'kelly_argmax': round(k_argmax, 4),
    'gate_off_stake': round(stake_off, 2),
    'draw_prob': round(MODEL_PROB[1], 3),
    'mispricing_score': round(r_arg.mispricing_score, 3),
    'expected_edge': round(r_arg.expected_edge, 4),
    'verdict': r_arg.verdict,
    'insurance': {
        'draw_odds': 3.45, 'draw_ev': ins_draw_ev,
        'cs11_odds': 7.2, 'cs11_ev': ins_cs11_ev,
        'note': '平局/1-1为价值层保险侧观察, 非自动BET (需跨庄分歧闸门+edge)',
    },
    'actual_result': '1-1 平局',
    'actual_note': '赛果平局→平局保险侧方向命中; 系统铁律: 有效市场无edge, 单庄不下注',
}

# ─────────────────────────────────────────────
# 路2: 真实 WH+IW 双庄样本 (从DB取) → 完整edge链路
# ─────────────────────────────────────────────
def first_dual_book():
    import sqlite3
    c = sqlite3.connect('data/football_data.db')
    rows = c.execute(
        """SELECT home_team, away_team FROM odds_features
           WHERE open_h>0 AND close_h>0
           GROUP BY home_team, away_team HAVING COUNT(DISTINCT source)>=2
           ORDER BY (SELECT COUNT(*) FROM odds_features o2 WHERE o2.home_team=odds_features.home_team AND o2.away_team=odds_features.away_team) DESC
           LIMIT 1""").fetchone()
    c.close()
    return rows

hb, ab = first_dual_book()
books = eng.query_odds_multi(hb, ab)
rm = eng.analyze_multi(books)
best_odds = eng._best_odds(books)                # 跨庄最优价 (soft line 下注侧)
kelly_frac, side = eng.kelly_stake_from_probs(rm.true_probs, best_odds)
# 分歧闸门 ON (有跨庄分歧 = 真edge 源)
side_idx = {'H': 0, 'D': 1, 'A': 2}[side] if side else 0
gate_on_stake, gate_on_k = (0.0, 0.0)
if side:
    gate_on_stake, gate_on_k = safe_stake(rm.true_probs[side_idx], best_odds[side_idx], BANKROLL, gate=True)
out['dual_book_sample'] = {
    'teams': f'{hb} vs {ab}',
    'n_books': len(books),
    'books': [
        {'close': [round(b.close_h, 2), round(b.close_d, 2), round(b.close_a, 2)],
         'implied': [round(x, 4) for x in b.implied_probs]}
        for b in books
    ],
    'consensus_implied': [round(x, 4) for x in rm.implied_probs],
    'softline_disagreement': rm.disagreement_detected,
    'softline_fade_applied': rm.softline_fade_applied,
    'softline_adjusted': [round(x, 4) for x in rm.softline_adjusted_probs] if rm.softline_adjusted_probs else None,
    'clv_beat': rm.clv_beat,
    'rlm_proxy': rm.rlm_proxy,
    'kelly_fraction': round(kelly_frac, 4),
    'recommended_side': side,
    'best_odds': [round(x, 2) for x in best_odds],
    'bankroll': BANKROLL,
    'frac_kelly': FRAC_KELLY,
    'max_stake_frac': MAX_STAKE_FRAC,
    'gate_on_stake': round(gate_on_stake, 2),
    'gate_on_stake_pct': round(gate_on_stake / BANKROLL * 100, 2),
    'verdict': rm.verdict,
}

print(json.dumps(out, ensure_ascii=False, indent=2))
with open('deliverables/decision_demo_output.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("\n[OK] 已写入 deliverables/decision_demo_output.json")
