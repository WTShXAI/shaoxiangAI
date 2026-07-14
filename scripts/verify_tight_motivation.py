#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
验证 "胶着(imp_d 紧贴热门<=0.12) + 战意边缘(MD3生死)" 选择性翻D规则
能否覆盖 DrawGate 漏判的 11 场平局, 且不爆增误判。
对照: 当前 DrawGate(0.688) 仅 7/18。
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

GAP = 0.12   # 平局紧贴热门阈值
recs = []
for r in rows:
    h_en, a_en = W._canon_team(r['home_team_name']), W._canon_team(r['away_team_name'])
    key = (h_en, a_en)
    if key not in odds_map: continue
    oh, od, oa, src = odds_map[key]
    md = r['matchday'] or 3
    stage = "group" if md <= 3 else "knockout"
    mi = W.MatchInput(home=h_en, away=a_en, odds_h=oh, odds_d=od, odds_a=oa, stage=stage, matchday=md)
    odds = W.parse_odds(oh, od, oa)
    feats = W._get_wc_features(h_en, a_en)
    de_prob = 0.0
    if feats is not None and W._DE_LOADED:
        try:
            raw = W._DE_PKG['model'].predict_proba([feats])[0][1]
            de_prob = float(W._DE_PKG['calibrator'].predict([raw])[0])
        except Exception:
            de_prob = 0.0
    form = W.analyze_form(h_en, a_en)
    ctx = W.analyze_context(stage, md, False, odds, form)
    fav = max(odds['imp_h'], odds['imp_a'])         # 热门隐含概率
    tight = (fav - odds['imp_d']) <= GAP            # 平局紧贴热门
    md3 = (stage == 'group' and md == 3)            # 小组生死战
    survival = bool(ctx.get('survival_clash'))      # 均势淘汰赛/生死
    recs.append(dict(home=h_en, away=a_en, act=r['final_result'],
                     imp_h=odds['imp_h'], imp_d=odds['imp_d'], imp_a=odds['imp_a'],
                     market=odds['market'], de_prob=de_prob, md=md, stage=stage,
                     tight=tight, md3=md3, survival=survival,
                     gate=(de_prob >= W.DRAW_GATE and odds['imp_d'] >= W.DRAW_GATE_MIN_IMP_D)))

draws = [x for x in recs if x['act'] == 'D']
nond  = [x for x in recs if x['act'] != 'D']
missed = [x for x in draws if not x['gate']]

print(f"=== 11 场漏判平局: 是否满足'胶着+战意' ===")
for x in missed:
    flag = "胶着" if x['tight'] else "   "
    mflag = "MD3" if x['md3'] else f"MD{x['md']}"
    sflag = "生死" if x['survival'] else "   "
    print(f"  {x['home']:>12} vs {x['away']:<12} gap={ (max(x['imp_h'],x['imp_a'])-x['imp_d']):.3f} {flag} {mflag} {sflag}")

print(f"\n=== 选择性规则捕获统计 ===")
# 规则A: 胶着 + MD3
ruleA_hit = [x for x in missed if x['tight'] and x['md3']]
ruleA_fp  = [x for x in nond if x['market']!='D' and x['tight'] and x['md3']]
print(f"规则A(胶着+MD3): 漏判平局中命中 {len(ruleA_hit)} 场; 非平局误判 +{len(ruleA_fp)} 场")
# 规则B: 胶着 + (MD3 或 survival)
ruleB_hit = [x for x in missed if x['tight'] and (x['md3'] or x['survival'])]
ruleB_fp  = [x for x in nond if x['market']!='D' and x['tight'] and (x['md3'] or x['survival'])]
print(f"规则B(胶着+MD3/生死): 漏判平局中命中 {len(ruleB_hit)} 场; 非平局误判 +{len(ruleB_fp)} 场")

print(f"\n=== 整体影响 (DrawGate OR 规则B) ===")
# 当前 DrawGate 基准
g_hit = sum(1 for x in draws if x['gate'])
g_predD = sum(1 for x in recs if x['gate'])
g_fp = g_predD - g_hit
print(f"仅 DrawGate: 判D={g_predD} 命中={g_hit} 误判={g_fp} 召回={g_hit/len(draws)*100:.1f}% 精确={g_hit/g_predD*100:.1f}%")

new_predD = new_hit = new_fp = 0
for x in recs:
    isD = x['gate'] or (x['market']!='D' and x['tight'] and (x['md3'] or x['survival']))
    if isD:
        new_predD += 1
        if x['act']=='D': new_hit += 1
        else: new_fp += 1
print(f"DrawGate+规则B: 判D={new_predD} 命中={new_hit} 误判={new_fp} 召回={new_hit/len(draws)*100:.1f}% 精确={new_hit/new_predD*100:.1f}%")

# 整体准确率(optimized=市场argmax, DrawGate/规则B 覆盖)
def acc_with(extra_fn):
    hit = 0
    for x in recs:
        # 复刻 optimized: 默认市场argmax, 除非 gate 或 extra 触发判D
        pred = 'D' if (x['gate'] or extra_fn(x)) else x['market']
        if pred == x['act']: hit += 1
    return hit / len(recs)
acc_base = acc_with(lambda x: False)
acc_ruleB = acc_with(lambda x: (x['market']!='D' and x['tight'] and (x['md3'] or x['survival'])))
print(f"\n整体准确率: 仅DrawGate={acc_base*100:.1f}%  DrawGate+规则B={acc_ruleB*100:.1f}%")

# ── GAP 扫描: 找甜点 ──
print(f"\n=== GAP(胶着阈值) 扫描 (规则: 胶着 + MD3/生死 + market!=D → 翻D, OR DrawGate) ===")
print(f"{'GAP':>6} | {'判D':>4} {'命中':>4} {'误判':>4} | {'draw召回':>8} {'draw精确':>9} | {'整体acc':>7}")
print("-"*60)
for g in (0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.25):
    predD = hitD = fp = 0
    for x in recs:
        isD = x['gate'] or (x['market']!='D' and (max(x['imp_h'],x['imp_a'])-x['imp_d'])<=g and (x['md3'] or x['survival']))
        if isD:
            predD += 1
            if x['act']=='D': hitD += 1
            else: fp += 1
    rec = hitD/len(draws)
    prec = hitD/predD if predD else 0
    acc = acc_with(lambda x,g=g: (x['market']!='D' and (max(x['imp_h'],x['imp_a'])-x['imp_d'])<=g and (x['md3'] or x['survival'])))
    print(f"{g:>6} | {predD:>4} {hitD:>4} {fp:>4} | {rec*100:>6.1f}% {prec*100:>7.1f}% | {acc*100:>5.1f}%")

