#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WC2026 全量回测 (88 场 finished, 真实赔率 + 真实赛果)
=====================================================
赔率源: odds 表(已由 scripts/etl_wc_odds_to_table.py 固化, 合并 bf/wb/vwc 脚本 + OCR json, 按 match_id 直取)
赛果: matches表 final_result (唯一权威, 禁止虚拟)
预测: wc_engine.predict(mode='rule' | 'optimized')
  - optimized 在护栏OFF(默认)下最终判决=赔率隐含概率argmax -> 即市场基线, 非模型增益
  - 比分 best_score 为硬编码模板(2-0/0-2/1-1...), 非学习模型 -> 命中率低属设计限制
诚实标注: 88场 match_features(93/98)与 wc_main_v1 训练重叠 -> 属 in-sample, 数字偏乐观
输出: deliverables/backtest_wc_90.json + 同目录 .md
"""
import sys, os, json, importlib.util, sqlite3
from pathlib import Path
from collections import defaultdict
import numpy as np

ARCH = Path(r"D:/Architecture")
PIPE = ARCH / "pipeline"
DB = ARCH / "data" / "football_data.db"
sys.path.insert(0, str(PIPE)); sys.path.insert(0, str(PIPE / "archive"))
import wc_engine as W

# ── 1. 赔率源: 直接读 odds 表(已由 scripts/etl_wc_odds_to_table.py 固化 WC 赔率) ──
con = sqlite3.connect(str(DB)); con.row_factory = sqlite3.Row; cur = con.cursor()
odds_by_id = {}   # match_id -> (oh, od, oa, provider)
for r in cur.execute("""SELECT m.match_id, o.home_odds, o.draw_odds, o.away_odds, o.provider
  FROM odds o JOIN matches m ON o.match_id=m.match_id
  WHERE m.league_name='世界杯'"""):
    odds_by_id[r['match_id']] = (float(r['home_odds']), float(r['draw_odds']),
                                  float(r['away_odds']), r['provider'])
print(f"[赔率源] odds 表内 WC 比赛赔率: {len(odds_by_id)} 场")

# ── 2. 88 场 finished WC ──
rows = cur.execute("""SELECT match_id, match_date, home_team_name, away_team_name, home_score, away_score,
  final_result, matchday FROM matches WHERE league_name='世界杯' AND status='finished' AND final_result IS NOT NULL
  ORDER BY match_date""").fetchall()
print(f"[DB] finished+fr WC: {len(rows)} 场")

# ── 3. 跑预测 ──
def parse_score(s):
    try:
        h, a = s.split('-'); return int(h), int(a)
    except: return None, None

recs = []
skip_no_odds = 0
for r in rows:
    if r['match_id'] not in odds_by_id:
        skip_no_odds += 1
        continue
    oh, od, oa, src = odds_by_id[r['match_id']]
    h_en, a_en = W._canon_team(r['home_team_name']), W._canon_team(r['away_team_name'])
    mi = W.MatchInput(home=h_en, away=a_en, odds_h=oh, odds_d=od, odds_a=oa,
                      stage=("group" if (r['matchday'] or 3) <= 3 else "knockout"),
                      matchday=r['matchday'] or 3)
    rr = W.predict(mi, mode="rule")
    ro = W.predict(mi, mode="optimized")
    # 市场基线(用引擎权威 parse_odds, 平局tie-break与optimized一致)
    mkt = W.parse_odds(oh, od, oa)['market']
    act = r['final_result']
    ph, pa = parse_score(rr.best_score); ah, aa = r['home_score'], r['away_score']
    opt_ph, opt_pa = parse_score(ro.best_score)
    recs.append(dict(
        match_id=r['match_id'], date=r['match_date'], home=h_en, away=a_en,
        actual_result=act, actual_score=f"{ah}-{aa}", odds_src=src,
        rule_pred=rr.prediction, opt_pred=ro.prediction, market_pred=mkt,
        rule_score=rr.best_score, opt_score=ro.best_score,
        hit_rule=(rr.prediction == act), hit_opt=(ro.prediction == act), hit_mkt=(mkt == act),
        score_exact_rule=(ph == ah and pa == aa),
        score_exact_opt=(opt_ph == ah and opt_pa == aa),
        score_tol_rule=(ph is not None and abs(ph-ah) <= 1 and abs(pa-aa) <= 1),
        score_tol_opt=(opt_ph is not None and abs(opt_ph-ah) <= 1 and abs(opt_pa-aa) <= 1),
    ))

def rate(key):
    return sum(1 for x in recs if x[key]) / len(recs) if recs else 0

n = len(recs)
summary = dict(
    total_finished=len(rows), with_odds=n, skipped_no_odds=skip_no_odds,
    rule_acc=round(rate('hit_rule'), 4),
    optimized_acc=round(rate('hit_opt'), 4),
    market_baseline_acc=round(rate('hit_mkt'), 4),
    score_exact_rule=round(rate('score_exact_rule'), 4),
    score_exact_opt=round(rate('score_exact_opt'), 4),
    score_tol_rule=round(rate('score_tol_rule'), 4),
    score_tol_opt=round(rate('score_tol_opt'), 4),
    pred_dist=dict(
        rule=dict(H=sum(1 for x in recs if x['rule_pred']=='H'),
                  D=sum(1 for x in recs if x['rule_pred']=='D'),
                  A=sum(1 for x in recs if x['rule_pred']=='A')),
        opt=dict(H=sum(1 for x in recs if x['opt_pred']=='H'),
                 D=sum(1 for x in recs if x['opt_pred']=='D'),
                 A=sum(1 for x in recs if x['opt_pred']=='A')),
        actual=dict(H=sum(1 for x in recs if x['actual_result']=='H'),
                    D=sum(1 for x in recs if x['actual_result']=='D'),
                    A=sum(1 for x in recs if x['actual_result']=='A')),
    ),
    caveat="88场 match_features 与 wc_main_v1 训练重叠(in-sample); optimized 护栏OFF=市场argmax; 比分为硬编码模板非学习模型",
)
out = dict(summary=summary, records=recs)
Path(ARCH / "deliverables").mkdir(exist_ok=True)
json.dump(out, open(str(ARCH / "deliverables" / "backtest_wc_90.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

print(f"\n=== 回测结果 ({n} 场有赔率, {skip_no_odds} 场无赔率跳过) ===")
print(f"rule 准确率       : {summary['rule_acc']*100:.1f}%")
print(f"optimized 准确率  : {summary['optimized_acc']*100:.1f}%")
print(f"市场基线(argmax)  : {summary['market_baseline_acc']*100:.1f}%")
print(f"比分精确命中(rule): {summary['score_exact_rule']*100:.1f}%")
print(f"比分精确命中(opt) : {summary['score_exact_opt']*100:.1f}%")
print(f"比分±1容差(rule)  : {summary['score_tol_rule']*100:.1f}%")
print(f"比分±1容差(opt)   : {summary['score_tol_opt']*100:.1f}%")
print("actual dist:", summary['pred_dist']['actual'])
con.close()