#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DrawExpert 平局盲区诊断
=======================
目标: 回答"DrawExpert 到底有没有真实平局信号", 为攻克DE定阈值。
方法: 复用 backtest_wc_90 的赔率源, 直接截获引擎内部 de_prob(校准后平局概率)
      和赔率隐含平局概率 imp_d, 对照真实 final_result。
不依赖 predict() 最终输出(护栏OFF会覆盖成市场argmax)。
输出: 校准曲线 + 门控阈值扫描(模拟DrawGate对整体准确率/平局命中的影响)
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

# ── 赔率源(同 backtest_wc_90) ──
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
for m in bf.COMPLETED: put(m[0], m[1], m[2], m[3], m[4], 'bf')
for m in wb.MATCHES: put(m[0], m[1], m[2], m[3], m[4], 'wb')
for m in vwc.WC2026: put(m[1], m[2], m[6], m[7], m[8], 'vwc')
ocr = json.load(open(str(ARCH / "data" / "wc2026_screenshot_odds_full.json"), encoding="utf-8"))
for m in ocr: put(m['home'], m['away'], m['oh'], m['od'], m['oa'], 'ocr')

W._load_main(); W._load_de()
if not W._DE_LOADED:
    print("FATAL: DrawExpert 未加载"); sys.exit(1)

rows = cur.execute("""SELECT match_id, match_date, home_team_name, away_team_name, home_score, away_score,
  final_result, matchday FROM matches WHERE league_name='世界杯' AND status='finished' AND final_result IS NOT NULL
  ORDER BY match_date""").fetchall()

diag = []
for r in rows:
    h_en, a_en = W._canon_team(r['home_team_name']), W._canon_team(r['away_team_name'])
    key = (h_en, a_en)
    if key not in odds_map: continue
    oh, od, oa, src = odds_map[key]
    odds = W.parse_odds(oh, od, oa)
    feats = W._get_wc_features(h_en, a_en)
    de_prob = 0.0; de_raw = 0.0
    if feats is not None:
        de_raw = float(W._DE_PKG['model'].predict_proba([feats])[0][1])
        de_prob = float(W._DE_PKG['calibrator'].predict([de_raw])[0])
    mkt = odds['market']
    diag.append(dict(
        match_id=r['match_id'], home=h_en, away=a_en,
        de_prob=de_prob, de_raw=de_raw,
        imp_d=odds['imp_d'], imp_h=odds['imp_h'], imp_a=odds['imp_a'],
        mkt=mkt, actual=r['final_result'], is_draw=(r['final_result'] == 'D'),
        de_signal=(de_prob >= W._DE_PKG['threshold']),
    ))

n = len(diag)
n_draw = sum(1 for d in diag if d['is_draw'])
print(f"\n[诊断样本] 有赔率可预测场: {n} | 真实平局: {n_draw} ({n_draw/n*100:.1f}%)")
print(f"[DrawExpert] threshold(包内)={W._DE_PKG['threshold']:.3f}")

# 1) de_prob 分布: 平局 vs 非平局
draw_p = [d['de_prob'] for d in diag if d['is_draw']]
nond_p = [d['de_prob'] for d in diag if not d['is_draw']]
print(f"\n[de_prob 分布] 平局场均值={np.mean(draw_p):.3f} 中位数={np.median(draw_p):.3f} max={np.max(draw_p):.3f}")
print(f"              非平局场均值={np.mean(nond_p):.3f} 中位数={np.median(nond_p):.3f} max={np.max(nond_p):.3f}")
print(f"              全样本均值={np.mean([d['de_prob'] for d in diag]):.3f}")

# 2) 校准 decile
print(f"\n[校准曲线] de_prob区间 -> 平局占比(真平局/落入该区间) | 落入场次")
edges = np.linspace(0, 1, 11)
for i in range(10):
    lo, hi = edges[i], edges[i+1]
    bucket = [d for d in diag if lo <= d['de_prob'] < hi]
    if not bucket:
        print(f"  [{lo:.1f},{hi:.1f}) : 空")
        continue
    nd = sum(1 for d in bucket if d['is_draw'])
    print(f"  [{lo:.1f},{hi:.1f}) : {nd/len(bucket)*100:5.1f}%  落入{len(bucket):2d}场")

# 3) 当前 de_signal(threshold=0.688) 表现
sig = [d for d in diag if d['de_signal']]
sig_draw = sum(1 for d in sig if d['is_draw'])
print(f"\n[当前 de_signal @ {W._DE_PKG['threshold']:.3f}] 触发{sig_draw}/{len(sig)}场为真平局 (precision={sig_draw/len(sig)*100:.1f}% 若全判D)")
print(f"  真实平局中被 de_signal 覆盖: {sig_draw}/{n_draw} (recall={sig_draw/n_draw*100:.1f}%)")

# 4) DrawGate 扫描: de_prob>=G 且 imp_d>=LOW -> 判D, 否则走市场argmax
print(f"\n[DrawGate 扫描] 策略: de_prob>=G & imp_d>=LOW -> 判D; 否则=市场argmax(mkt)")
print(f"{'GATE':>6} {'LOW_imp_d':>9} {'判D数':>6} {'D命中':>5} {'D精确%':>7} {'整体acc%':>8} {'vs市场':>7}")
market_acc = sum(1 for d in diag if d['mkt'] == d['actual']) / n * 100
for G in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.688]:
    for LOW in [0.0, 0.18, 0.22]:
        preds = []
        for d in diag:
            if d['de_prob'] >= G and d['imp_d'] >= LOW:
                pred = 'D'
            else:
                pred = d['mkt']
            preds.append(pred == d['actual'])
        acc = np.mean(preds) * 100
        d_pred = [d for d in diag if d['de_prob'] >= G and d['imp_d'] >= LOW]
        d_hit = sum(1 for d in d_pred if d['is_draw'])
        prec = d_hit / len(d_pred) * 100 if d_pred else 0
        print(f"{G:6.3f} {LOW:9.2f} {len(d_pred):6d} {d_hit:5d} {prec:6.1f}% {acc:7.1f}% {acc-market_acc:+6.1f}%")

# 5) 门控翻错代价: 门控判D但原市场判对的场
print(f"\n[翻错代价示例] GATE=0.45, LOW=0.18 下被门控翻成D、但市场原本判对的场:")
G, LOW = 0.45, 0.18
for d in diag:
    gate_d = (d['de_prob'] >= G and d['imp_d'] >= LOW)
    if gate_d and d['mkt'] != d['actual'] and d['actual'] != 'D':
        print(f"  {d['match_id']} {d['home']} vs {d['away']} | de_prob={d['de_prob']:.3f} imp_d={d['imp_d']:.3f} | 市场判{d['mkt']} 实际{d['actual']}({d['actual']=='D'})")
con.close()
