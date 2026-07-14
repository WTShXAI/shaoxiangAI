#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
整个系统跑一场 (World Cup 88 finished) — 端到端冒烟 + 自检 + 自己纠错
========================================================================
对 matches 全部世界杯 finished 比赛:
  - 复用 backtest 多源赔率拼接(odds表/3脚本/OCR)构造 MatchInput
  - 逐场跑 rule + optimized, 全异常捕获
  - 校验: prediction∈{H,D,A} / confidence∈[0,1] / best_score格式 / market_probs合法
  - 独立核对 _get_wc_features 返回(77维对齐自检)
输出问题清单 + 计数器。
"""
import sys, os, json, importlib.util, sqlite3, re
from pathlib import Path
from collections import defaultdict
import numpy as np

ARCH = Path(r"D:/Architecture"); PIPE = ARCH / "pipeline"; DB = ARCH / "data" / "football_data.db"
sys.path.insert(0, str(PIPE)); sys.path.insert(0, str(PIPE / "archive"))
import wc_engine as W

ZH2EN = {v: k for k, v in W._TEAM_ALIAS.items()}
def to_en(n):
    if n is None: return None
    n = n.strip(); e = ZH2EN.get(n, n); return W._canon_team(e)

# ── 赔率源(复用 backtest 逻辑) ──
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

rows = cur.execute("""SELECT match_id, match_date, home_team_name, away_team_name, home_score, away_score,
  final_result, matchday FROM matches WHERE league_name='世界杯' AND status='finished' AND final_result IS NOT NULL
  ORDER BY match_date""").fetchall()

issues = []; ran = 0; crashed = 0; feat_missing = 0; feat_bad = 0
score_re = re.compile(r'^\d+-\d+$')
for r in rows:
    h_en, a_en = W._canon_team(r['home_team_name']), W._canon_team(r['away_team_name'])
    key = (h_en, a_en)
    # 特征对齐自检(独立)
    feat = W._get_wc_features(h_en, a_en)
    if feat is None:
        feat_missing += 1
    elif not (isinstance(feat, np.ndarray) and feat.shape == (77,)):
        feat_bad += 1
        issues.append(f"[FEAT_BAD] {h_en} vs {a_en}: shape={None if feat is None else feat.shape}")
    if key not in odds_map:
        continue  # 无赔率, 引擎不支持无赔率输入, 跳过 predict
    oh, od, oa, src = odds_map[key]
    mi = W.MatchInput(home=h_en, away=a_en, odds_h=oh, odds_d=od, odds_a=oa,
                      stage=("group" if (r['matchday'] or 3) <= 3 else "knockout"), matchday=r['matchday'] or 3)
    ran += 1
    for mode in ("rule", "optimized"):
        try:
            res = W.predict(mi, mode=mode)
        except Exception as e:
            crashed += 1
            issues.append(f"[CRASH] {mode} {h_en} vs {a_en}: {type(e).__name__}: {e}")
            continue
        if res.prediction not in ('H', 'D', 'A'):
            issues.append(f"[PRED_INVALID] {mode} {h_en} vs {a_en}: pred={res.prediction!r}")
        if not (0.0 <= res.confidence <= 1.0):
            issues.append(f"[CONF_RANGE] {mode} {h_en} vs {a_en}: conf={res.confidence}")
        if not (isinstance(res.best_score, str) and score_re.match(res.best_score)):
            issues.append(f"[SCORE_FMT] {mode} {h_en} vs {a_en}: score={res.best_score!r}")
        if not isinstance(res.market_probs, dict) or set(res.market_probs) != {'H', 'D', 'A'}:
            issues.append(f"[MPROB] {mode} {h_en} vs {a_en}: mp={res.market_probs}")
        else:
            s = sum(res.market_probs.values())
            if abs(s - 1.0) > 0.05:
                issues.append(f"[MPROB_SUM] {mode} {h_en} vs {a_en}: sum={s:.3f}")
        if res.ou_recommend is None or res.hcp_recommend is None:
            issues.append(f"[REC_NONE] {mode} {h_en} vs {a_en}: ou/hcp None")

con.close()
print(f"=== 系统一场冒烟 (世界杯 finished {len(rows)} 场) ===")
print(f"有赔率跑通场次: {ran}")
print(f"崩溃: {crashed}")
print(f"特征缺失(退规则): {feat_missing}")
print(f"特征维度异常: {feat_bad}")
print(f"问题项总数: {len(issues)}")
for it in issues[:60]:
    print("  ", it)
if not issues:
    print("  ✅ 零问题")
