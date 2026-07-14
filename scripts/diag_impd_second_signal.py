#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
诊断: 11场漏判平局的 imp_d 分布 + 全部非平局 imp_d 分布
目标: 设计"融合 imp_d 第二信号"的安全阈值, 在不爆增误判前提下覆盖更多真平局。
"""
import sys, os, json, importlib.util, sqlite3
from pathlib import Path
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
  FROM odds o JOIN matches m ON o.match_id=m.match_id WHERE m.league_name='世界杯'"""):
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

rows = cur.execute("""SELECT match_id, match_date, home_team_name, away_team_name, home_score, away_score,
  final_result, matchday FROM matches WHERE league_name='世界杯' AND status='finished' AND final_result IS NOT NULL
  ORDER BY match_date""").fetchall()
con.close()

W._load_main(); W._load_de()

records = []
for r in rows:
    h_en, a_en = W._canon_team(r['home_team_name']), W._canon_team(r['away_team_name'])
    key = (h_en, a_en)
    if key not in odds_map: continue
    oh, od, oa, src = odds_map[key]
    mi = W.MatchInput(home=h_en, away=a_en, odds_h=oh, odds_d=od, odds_a=oa,
                      stage=("group" if (r['matchday'] or 3) <= 3 else "knockout"), matchday=r['matchday'] or 3)
    odds = W.parse_odds(oh, od, oa)
    feats = W._get_wc_features(h_en, a_en)
    de_prob = 0.0
    if feats is not None and W._DE_LOADED:
        try:
            raw = W._DE_PKG['model'].predict_proba([feats])[0][1]
            de_prob = float(W._DE_PKG['calibrator'].predict([raw])[0])
        except Exception:
            de_prob = 0.0
    rec = dict(home=h_en, away=a_en, act=r['final_result'],
               imp_h=odds['imp_h'], imp_d=odds['imp_d'], imp_a=odds['imp_a'],
               market=odds['market'], de_prob=de_prob,
               cur_gate=(de_prob >= W.DRAW_GATE and odds['imp_d'] >= W.DRAW_GATE_MIN_IMP_D))
    records.append(rec)

# ── 分析 ──
draws = [x for x in records if x['act'] == 'D']
nond  = [x for x in records if x['act'] != 'D']

print(f"=== 18 场真实平局: 当前 DrawGate 命中情况 ===")
missed = [x for x in draws if not x['cur_gate']]
hit    = [x for x in draws if x['cur_gate']]
print(f"命中 {len(hit)} 场, 漏判 {len(missed)} 场")
print(f"\n--- 漏判的 {len(missed)} 场: imp_d / de_prob / market ---")
for x in missed:
    print(f"  {x['home']:>12} vs {x['away']:<12} imp_d={x['imp_d']:.3f} de_prob={x['de_prob']:.3f} market={x['market']}")
impd_missed = [x['imp_d'] for x in missed]
print(f"  漏判场 imp_d: min={min(impd_missed):.3f} median={np.median(impd_missed):.3f} max={max(impd_missed):.3f}")

print(f"\n=== 非平局 {len(nond)} 场 imp_d 分布 ===")
impd_nond = sorted([x['imp_d'] for x in nond])
print(f"  min={impd_nond[0]:.3f} p25={np.percentile(impd_nond,25):.3f} median={np.median(impd_nond):.3f} "
      f"p75={np.percentile(impd_nond,75):.3f} max={impd_nond[-1]:.3f}")
# 非平局中, market!=D 但 imp_d 很高的是"危险区"
nond_mkt_notd = [x for x in nond if x['market'] != 'D']
print(f"\n  非平局且 market!=D: {len(nond_mkt_notd)} 场 (这些若被 imp_d 第二信号翻成D=误判)")
for T in (0.30, 0.32, 0.34, 0.36, 0.38, 0.40, 0.42, 0.44, 0.46):
    # 第二信号: imp_d>=T 且 market!=D(即当前未被市场判D) → 判D
    # 仅统计"当前 DrawGate 未命中"的前提下额外触发
    extra_draw_hit = sum(1 for x in missed if x['imp_d'] >= T)          # 多抓到的真平局
    extra_false = sum(1 for x in nond_mkt_notd if x['imp_d'] >= T)      # 新增误判
    print(f"  imp_d>={T}: 多抓真平局={extra_draw_hit:>2}  新增误判(market!=D非平局)={extra_false:>2}")

# 更全面: 把第二信号视为"补充"(与 DrawGate OR), 看整体
print(f"\n=== 第二信号作为 DrawGate 补充 (OR) 的整体影响 ===")
for T in (0.34, 0.36, 0.38, 0.40, 0.42):
    predD = hitD = falseD = 0
    trueD = len(draws)
    for x in records:
        isD = x['cur_gate'] or (x['imp_d'] >= T and x['market'] != 'D')
        if isD:
            predD += 1
            if x['act'] == 'D': hitD += 1
            else: falseD += 1
    rec = hitD / trueD if trueD else 0
    prec = hitD / predD if predD else 0
    print(f"  imp_d>={T} (补充): 判D={predD} 命中={hitD} 误判={falseD} 召回={rec*100:.1f}% 精确={prec*100:.1f}%")
