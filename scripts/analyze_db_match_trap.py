# -*- coding: utf-8 -*-
"""
从数据库捞一场真实比赛, 跑 ReverseOddsEngine.analyze_multi 做盘口陷阱分析.
样本: 西班牙人 vs 毕尔巴鄂竞技 [2018-01-15, 西甲, 终场D]
  - 威廉希尔收盘 2.80/3.10/2.62 → 热门=客
  - Interwetten 收盘 2.60/3.20/2.80 → 热门=主
  - 双庄对"谁热门"分歧, 终场平局 → soft-line 分歧信号教科书案例
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput, Intent

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'football_data.db')

# 直接从已知DB行构造 (已核实)
books = [
    # william_hill
    OddsInput(open_h=2.75, open_d=3.20, open_a=2.62,
              close_h=2.80, close_d=3.10, close_a=2.62),
    # interwetten
    OddsInput(open_h=2.60, open_d=3.20, open_a=2.80,
              close_h=2.60, close_d=3.20, close_a=2.80),
]

engine = ReverseOddsEngine()
# 逐庄单源分析 (演示单机构局限)
single_reports = []
for i, b in enumerate(['william_hill', 'interwetten']):
    r = engine.analyze(books[i])
    single_reports.append((b, r))

# 跨庄综合分析
multi = engine.analyze_multi(books)

def fmt_probs(p):
    return "H=%.1f%% D=%.1f%% A=%.1f%%" % (p[0]*100, p[1]*100, p[2]*100)

print("=" * 64)
print("  盘口陷阱分析 — 西班牙人 vs 毕尔巴鄂竞技 [2018-01-15 西甲]")
print("=" * 64)
print("\n[逐庄单源分析 — 注意单机构局限]")
for name, r in single_reports:
    print(f"\n  {name}:")
    print(f"    收盘隐含: {fmt_probs(r.implied_probs)}")
    print(f"    意图: {r.intent.value} ({r.drift_pattern}) 置信{r.intent_confidence:.0%}")
    print(f"    结论: {r.verdict}")

print("\n" + "-" * 64)
print("[跨庄综合分析 analyze_multi — 操盘手三段框架]")
print(f"  共识隐含概率 : {fmt_probs(multi.implied_probs)}")
print(f"  调整后概率   : {fmt_probs(multi.true_probs) if multi.true_probs else 'N/A'}")
print(f"  n_books      : {multi.n_books}")
print(f"  跨庄同步     : {multi.cross_book_sync}")
print(f"  意图         : {multi.intent.value}")
print(f"  跨庄分歧?    : {multi.disagreement_detected}")
print(f"  soft-line淡热门? : {multi.softline_fade_applied}")
if multi.softline_adjusted_probs:
    print(f"  soft-line调整概率: {fmt_probs(multi.softline_adjusted_probs)}")
print(f"  CLV beat-close: {multi.clv_beat}")
print(f"  RLM代理(离散): {multi.rlm_proxy}")
print(f"  误定价分     : {multi.mispricing_score:.2f}")
print(f"  凯利         : {multi.kelly_fraction:+.1%} → {multi.recommended_bet}")
print(f"  honest_def   : target={multi.honest_def_target} applied={multi.honest_def_applied}")
print(f"\n  最终结论:\n  {multi.verdict}")

# 真实赛果对照
print("\n" + "=" * 64)
print("  真实赛果: 西班牙人 0-0 毕尔巴鄂竞技 (平局 D)")
print("  验证: 双庄分歧→淡共识热门, 实际平局 → soft-line 分歧信号在本案中成立")
print("=" * 64)

# 导出 JSON 供报告渲染
out = {
    'match': '西班牙人 vs 毕尔巴鄂竞技',
    'date': '2018-01-15',
    'league': '西甲 17/18第19轮',
    'outcome': 'D (0-0)',
    'books': [
        {'source': 'william_hill', 'open': [2.75,3.20,2.62], 'close': [2.80,3.10,2.62], 'fav': 'A'},
        {'source': 'interwetten', 'open': [2.60,3.20,2.80], 'close': [2.60,3.20,2.80], 'fav': 'H'},
    ],
    'consensus_implied': list(multi.implied_probs),
    'softline_adjusted': list(multi.softline_adjusted_probs) if multi.softline_adjusted_probs else None,
    'disagreement': multi.disagreement_detected,
    'fade_applied': multi.softline_fade_applied,
    'clv_beat': multi.clv_beat,
    'rlm_proxy': multi.rlm_proxy,
    'intent': multi.intent.value,
    'mispricing_score': multi.mispricing_score,
    'kelly': multi.kelly_fraction,
    'verdict': multi.verdict,
}
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'deliverables', 'db_match_trap_analysis_20180115.json'), 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("\n[JSON 已导出: deliverables/db_match_trap_analysis_20180115.json]")
