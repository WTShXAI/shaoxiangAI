#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DrawGate 低阈值对照测试 (0.33 / 0.328 vs 当前 0.688)
保持 DRAW_GATE_MIN_IMP_D=0.10 不变, 仅扫 DRAW_GATE。
复用与 backtest_wc_90.py 一致的赔率源 + 真实赛果(final_result 唯一权威)。
输出平局召回/精确率 + 整体准确率对照。
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

ZH2EN = {v: k for k, v in W._TEAM_ALIAS.items()}
def to_en(name):
    if name is None: return None
    n = name.strip(); e = ZH2EN.get(n, n)
    return W._canon_team(e)

# ── 赔率源 (与 backtest_wc_90.py 完全一致) ──
odds_map = {}
def put(h_zh, a_zh, oh, od, oa, src):
    if not oh or not od or not oa: return
    h, a = to_en(h_zh), to_en(a_zh)
    if not h or not a: return
    key = (h, a)
    if key not in odds_map:
        odds_map[key] = (float(oh), float(od), float(oa), src)

con = sqlite3.connect(str(DB)); con.row_factory = sqlite3.Row; cur = con.cursor()
for r in cur.execute("""SELECT m.home_team_name, m.away_team_name, o.home_odds, o.draw_odds, o.away_odds
  FROM odds o JOIN matches m ON o.match_id=m.match_id
  WHERE m.league_name='世界杯'"""):
    put(r['home_team_name'], r['away_team_name'], r['home_odds'], r['draw_odds'], r['away_odds'], 'odds_table')
def load(n, p):
    s = importlib.util.spec_from_file_location(n, str(p)); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
bf = load("bf", PIPE / "wc2026_backtest_final.py")
wb = load("wb", PIPE / "worldcup2026_backtest.py")
vwc = load("vwc", PIPE / "archive" / "validate_wc2026.py")
for m in bf.COMPLETED:   put(m[0], m[1], m[2], m[3], m[4], 'bf')
for m in wb.MATCHES:     put(m[0], m[1], m[2], m[3], m[4], 'wb')
for m in vwc.WC2026:     put(m[1], m[2], m[6], m[7], m[8], 'vwc')
ocr = json.load(open(str(ARCH / "data" / "wc2026_screenshot_odds_full.json"), encoding="utf-8"))
for m in ocr:
    put(m['home'], m['away'], m['oh'], m['od'], m['oa'], 'ocr')

# ── finished + final_result 的 WC 赛果 ──
rows = cur.execute("""SELECT match_id, match_date, home_team_name, away_team_name, home_score, away_score,
  final_result, matchday FROM matches WHERE league_name='世界杯' AND status='finished' AND final_result IS NOT NULL
  ORDER BY match_date""").fetchall()

# 预构建 (match_input, actual) 列表
cases = []
for r in rows:
    h_en, a_en = W._canon_team(r['home_team_name']), W._canon_team(r['away_team_name'])
    key = (h_en, a_en)
    if key not in odds_map: continue
    oh, od, oa, src = odds_map[key]
    mi = W.MatchInput(home=h_en, away=a_en, odds_h=oh, odds_d=od, odds_a=oa,
                      stage=("group" if (r['matchday'] or 3) <= 3 else "knockout"),
                      matchday=r['matchday'] or 3)
    cases.append((mi, r['final_result']))
con.close()
N = len(cases)
print(f"[样本] 有赔率可跑 {N} 场 | 真实分布 H/D/A = "
      f"{sum(1 for _,a in cases if a=='H')}/{sum(1 for _,a in cases if a=='D')}/{sum(1 for _,a in cases if a=='A')}")

# ── 扫阈值 ──
def run(gate):
    W.DRAW_GATE = gate
    n_hit = 0
    predD = trueD = hitD = falseD = 0
    for mi, act in cases:
        ro = W.predict(mi, mode="optimized")
        if ro.prediction == act: n_hit += 1
        if act == 'D': trueD += 1
        if ro.prediction == 'D':
            predD += 1
            if act == 'D': hitD += 1
            else: falseD += 1
    acc = n_hit / N
    draw_recall = hitD / trueD if trueD else 0
    draw_prec = hitD / predD if predD else 0
    return acc, predD, trueD, hitD, falseD, draw_recall, draw_prec

print(f"\n{'DRAW_GATE':>10} | {'整体acc':>8} | {'predD':>6} {'trueD':>6} {'hitD':>6} {'falseD':>7} | {'draw_rec':>9} {'draw_prec':>10}")
print("-"*78)
for g in (0.75, 0.70, 0.688, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40, 0.35, 0.33, 0.328, 0.30):
    acc, predD, trueD, hitD, falseD, dr, dp = run(g)
    print(f"{g:>10} | {acc*100:>6.1f}% | {predD:>6} {trueD:>6} {hitD:>6} {falseD:>7} | {dr*100:>7.1f}% {dp*100:>8.1f}%")

print("\n说明: predD=判平局场数, trueD=真实平局场数, hitD=判对平局, falseD=误判平局")
print("draw_rec=平局召回率, draw_prec=平局精确率(命中平局/判平局)")
