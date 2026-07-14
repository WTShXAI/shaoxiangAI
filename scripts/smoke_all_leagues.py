#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Layer 2 — 全联赛鲁棒性压力测试 (整个系统跑一场的广度验证)
================================================================
抽样全联赛"有赔率+已完赛"真实比赛, 逐场构造 MatchInput 跑 optimized,
全异常捕获, 校验输出合法性。非WC比赛预期退化为纯规则(特征None), 不崩。
目的: 暴露 parse_odds/analyze_form/决策树/ou/hcp 在任意真实赔率与队名下的隐藏崩溃。
"""
import sys, os, sqlite3, re
from pathlib import Path
import numpy as np

ARCH = Path(r"D:/Architecture"); PIPE = ARCH / "pipeline"; DB = ARCH / "data" / "football_data.db"
sys.path.insert(0, str(PIPE))
import wc_engine as W

con = sqlite3.connect(str(DB)); con.row_factory = sqlite3.Row; cur = con.cursor()
N = 3000
cur.execute("""SELECT m.match_id, m.home_team_name, m.away_team_name, m.matchday, m.league_name,
  o.home_odds, o.draw_odds, o.away_odds
  FROM matches m JOIN odds o ON o.match_id=m.match_id
  WHERE m.status='finished' AND m.final_result IS NOT NULL
  ORDER BY random() LIMIT ?""", (N,))
rows = cur.fetchall()

score_re = re.compile(r'^\d+-\d+$')
issues = []; ran = 0; crashed = 0
leagues = {}
for r in rows:
    h = W._canon_team(r['home_team_name']); a = W._canon_team(r['away_team_name'])
    lg = r['league_name'] or '?'
    leagues[lg] = leagues.get(lg, 0) + 1
    try:
        oh = float(r['home_odds']); od = float(r['draw_odds']); oa = float(r['away_odds'])
    except Exception as e:
        issues.append(f"[ODDS_BAD] {r['match_id']} {h} vs {a}: odds={r['home_odds']}/{r['draw_odds']}/{r['away_odds']} ({e})")
        continue
    if not (oh > 1.0 and od > 1.0 and oa > 1.0):
        issues.append(f"[ODDS_RANGE] {r['match_id']} {h} vs {a}: {oh}/{od}/{oa}")
        continue
    mi = W.MatchInput(home=h, away=a, odds_h=oh, odds_d=od, odds_a=oa,
                      stage='group', matchday=(r['matchday'] or 3))
    ran += 1
    try:
        res = W.predict(mi, mode='optimized')
    except Exception as e:
        crashed += 1
        issues.append(f"[CRASH] {r['match_id']} {h} vs {a}: {type(e).__name__}: {e}")
        continue
    if res.prediction not in ('H', 'D', 'A'):
        issues.append(f"[PRED_INVALID] {r['match_id']} {h} vs {a}: pred={res.prediction!r}")
    if not (0.0 <= res.confidence <= 1.0):
        issues.append(f"[CONF_RANGE] {r['match_id']} {h} vs {a}: conf={res.confidence}")
    if not (isinstance(res.best_score, str) and score_re.match(res.best_score)):
        issues.append(f"[SCORE_FMT] {r['match_id']} {h} vs {a}: score={res.best_score!r}")
    if not isinstance(res.market_probs, dict) or set(res.market_probs) != {'H', 'D', 'A'}:
        issues.append(f"[MPROB] {r['match_id']} {h} vs {a}: mp={res.market_probs}")
    elif abs(sum(res.market_probs.values()) - 1.0) > 0.05:
        issues.append(f"[MPROB_SUM] {r['match_id']} {h} vs {a}: sum={sum(res.market_probs.values()):.3f}")
    if res.ou_recommend is None or res.hcp_recommend is None:
        issues.append(f"[REC_NONE] {r['match_id']} {h} vs {a}: ou/hcp None")

con.close()
print(f"=== Layer2 全联赛压力测试 (抽样 {N} 场, 实际跑 {ran}, 崩溃 {crashed}) ===")
print("联赛分布(top8):", dict(sorted(leagues.items(), key=lambda x: -x[1])[:8]))
print(f"问题项总数: {len(issues)}")
for it in issues[:60]:
    print("  ", it)
if not issues:
    print("  ✅ 零问题")
