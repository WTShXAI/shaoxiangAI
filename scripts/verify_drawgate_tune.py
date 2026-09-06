#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聚焦验证 DrawGate(GATE=0.688) 在多个 imp_d 下限下的真实表现"""
import sys, os, json, importlib.util, sqlite3
from pathlib import Path
import numpy as np
ARCH = Path(r"D:/Architecture"); PIPE = ARCH / "pipeline"; DB = ARCH / "data" / "football_data.db"
sys.path.insert(0, str(PIPE)); sys.path.insert(0, str(PIPE / "archive"))
import wc_engine as W
ZH2EN = {v: k for k, v in W._TEAM_ALIAS.items()}
def to_en(n):
    if n is None: return None
    return W._canon_team(ZH2EN.get(n.strip(), n.strip()))
odds_map = {}
def put(h_zh, a_zh, oh, od, oa, src):
    if not oh or not od or not oa: return
    h, a = to_en(h_zh), to_en(a_zh)
    if not h or not a: return
    k = (h, a)
    if k not in odds_map: odds_map[k] = (float(oh), float(od), float(oa), src)
con = sqlite3.connect(str(DB)); con.row_factory = sqlite3.Row; cur = con.cursor()
for r in cur.execute("""SELECT m.home_team_name,m.away_team_name,o.home_odds,o.draw_odds,o.away_odds
  FROM odds o JOIN matches m ON o.match_id=m.match_id WHERE m.league_name='世界杯'"""):
    put(r['home_team_name'], r['away_team_name'], r['home_odds'], r['draw_odds'], r['away_odds'], 'odds')
def load(n,p):
    s=importlib.util.spec_from_file_location(n,str(p)); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
bf=load("bf",PIPE/"wc2026_backtest_final.py"); wb=load("wb",PIPE/"worldcup2026_backtest.py"); vwc=load("vwc",PIPE/"archive"/"validate_wc2026.py")
for m in bf.COMPLETED: put(m[0],m[1],m[2],m[3],m[4],'bf')
for m in wb.MATCHES: put(m[0],m[1],m[2],m[3],m[4],'wb')
for m in vwc.WC2026: put(m[1],m[2],m[6],m[7],m[8],'vwc')
ocr=json.load(open(str(ARCH/"data"/"wc2026_screenshot_odds_full.json"),encoding="utf-8"))
for m in ocr: put(m['home'],m['away'],m['oh'],m['od'],m['oa'],'ocr')
W._load_main(); W._load_de()
rows=cur.execute("""SELECT match_id,match_date,home_team_name,away_team_name,home_score,away_score,final_result
  FROM matches WHERE league_name='世界杯' AND status='finished' AND final_result IS NOT NULL ORDER BY match_date""").fetchall()
diag=[]
for r in rows:
    h,w=W._canon_team(r['home_team_name']),W._canon_team(r['away_team_name'])
    k=(h,w)
    if k not in odds_map: continue
    oh,od,oa,src=odds_map[k]
    odds=W.parse_odds(oh,od,oa)
    feats=W._get_wc_features(h,w)
    de_prob=0.0
    if feats is not None:
        de_raw=float(W._DE_PKG['model'].predict_proba([feats])[0][1])
        de_prob=float(W._DE_PKG['calibrator'].predict([de_raw])[0])
    diag.append(dict(match_id=r['match_id'],home=h,away=w,de_prob=de_prob,imp_d=odds['imp_d'],
                     mkt=odds['market'],actual=r['final_result'],is_draw=(r['final_result']=='D')))
n=len(diag); ndraw=sum(1 for d in diag if d['is_draw'])
mkt_acc=sum(1 for d in diag if d['mkt']==d['actual'])/n*100
print(f"n={n} draws={ndraw} market_baseline={mkt_acc:.1f}%")
GATE=0.688
print(f"\nGATE={GATE} 扫描 imp_d 下限:")
print(f"{'LOW':>6} {'判D':>4} {'D命中':>5} {'D精确%':>7} {'整体acc%':>9} {'vs市场':>7}")
for LOW in [0.0,0.05,0.10,0.15,0.18]:
    preds=[]
    for d in diag:
        pred='D' if (d['de_prob']>=GATE and d['imp_d']>=LOW) else d['mkt']
        preds.append(pred==d['actual'])
    acc=np.mean(preds)*100
    dp=[d for d in diag if d['de_prob']>=GATE and d['imp_d']>=LOW]
    dh=sum(1 for d in dp if d['is_draw'])
    prec=dh/len(dp)*100 if dp else 0
    print(f"{LOW:6.2f} {len(dp):4d} {dh:5d} {prec:6.1f}% {acc:8.1f}% {acc-mkt_acc:+6.1f}%")
con.close()
