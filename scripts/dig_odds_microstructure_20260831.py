# -*- coding: utf-8 -*-
"""
dig_odds_microstructure_20260831.py (rb_matches 版, 聚焦 1X2 开收漂移 @开盘价)
—— 修正: rb_matches 的 AH/OU 仅 ~4-6k 行有值, 跨市场协调改在 GQ odds_changes 挖。
本脚本只用 rb_matches 可靠的 1X2 开/收 + total_goals:
  PART A  Oracle: 每场押实际赢家 -> 证明完美预测=+EV (ROI>0, 永不错注)
  PART B  分类选边 edge 重算 @开盘价: 胜率 vs 1/开盘赔率
  PART C  1X2 漂移信号 @开盘价下注 (主压低/客压低/主压低+平升)
  PART D  主家漂移幅度分桶 @开盘价
诚实守卫: 仅 n>=300 且 ROI bootstrap CI[2.5%]>0 认 +EV。
输出: scripts/dig_odds_microstructure_out.json
"""
from __future__ import annotations
import sqlite3, json, numpy as np

DB = "data/rollball_training.db"
OUT = "scripts/dig_odds_microstructure_out.json"
TH = 0.02
N_BOOT = 2000

con = sqlite3.connect(DB)
cur = con.cursor()
rows = cur.execute(
    """
    SELECT op_h,op_d,op_a, cl_h,cl_d,cl_a, total_goals, result
    FROM rb_matches
    WHERE result IN ('H','D','A')
      AND op_h>1.01 AND op_d>1.01 AND op_a>1.01
      AND cl_h>1.01 AND cl_d>1.01 AND cl_a>1.01
    """
).fetchall()
con.close()
N = len(rows)
print(f"[load] {N} 场 (1X2 开/收齐全)")

op = np.array([[r[0],r[1],r[2]] for r in rows])
cl = np.array([[r[3],r[4],r[5]] for r in rows])
total = np.array([r[6] for r in rows], dtype=float)
res = np.array([{'H':0,'D':1,'A':2}[r[7]] for r in rows])
fav = np.argmin(op, axis=1)
fav_op = op[np.arange(N), fav]
plate = np.where(fav_op<=1.80,'hot', np.where(fav_op<=2.50,'bal','cold'))

def rel(c,o): return (c-o)/o
dH, dD, dA = rel(cl[:,0],op[:,0]), rel(cl[:,1],op[:,1]), rel(cl[:,2],op[:,2])

inv = 1.0/op; imp = inv/inv.sum(axis=1, keepdims=True)  # (N,3) 开盘隐含概率

def boot_roi(win_flag, odds, seed=7):
    rng = np.random.default_rng(seed)
    b = rng.integers(0, len(win_flag), size=(N_BOOT, len(win_flag)))
    rets = np.where(win_flag[b], odds[b]-1.0, -1.0)
    rois = rets.mean(axis=1)
    return float(rois.mean()), float(np.percentile(rois,2.5)), float(np.percentile(rois,97.5))

def ev(mask, side):
    m = mask; n=int(m.sum())
    if n<50: return {"n":n,"skip":True}
    k = side
    win = res[m]==k
    odds = op[m,k]
    wr = float(win.mean()); implied = float(imp[m,k].mean())
    edge = wr-implied
    roi = float(np.where(win, odds-1.0, -1.0).mean())
    lo=hi=np.nan
    if n>=300:
        _,lo,hi = boot_roi(win.astype(float), odds)
    return {"n":n,"win_rate":round(100*wr,2),"implied":round(100*implied,2),
            "edge_pp":round(100*edge,2),"roi_open":round(100*roi,2),
            "roi_CI":[round(100*lo,2),round(100*hi,2)] if n>=300 else None,
            "pos_ev": bool(roi>0 and (n<300 or lo>0))}

# PART A oracle
print("\n[PART A] Oracle: 每场押实际赢家 (证完美预测=+EV)")
ora_op = op[np.arange(N), res]; ora_cl = cl[np.arange(N), res]
ora_roi_op = float((ora_op-1.0).mean()); ora_roi_cl = float((ora_cl-1.0).mean())
print(f"  押赢家@开盘 ROI = {ora_roi_op*100:+.2f}%   @收盘 ROI = {ora_roi_cl*100:+.2f}%  (均>0)")
part_a = {"oracle_roi_open":round(100*ora_roi_op,2),"oracle_roi_close":round(100*ora_roi_cl,2)}

# PART B plate edge @opening
print("\n[PART B] 分类选边 edge @开盘价 (胜率 vs 1/开盘赔率)")
part_b = {}
for pc in ['hot','bal','cold']:
    m=plate==pc; n=int(m.sum()); k=fav[m]
    win=res[m]==k; odds=op[m][np.arange(n),k]
    wr=float(win.mean()); impl=float(imp[m][np.arange(n),k].mean()); edge=wr-impl
    roi=float(np.where(win,odds-1.0,-1.0).mean())
    part_b[pc]={"n":n,"win_rate":round(100*wr,2),"implied":round(100*impl,2),
                "edge_pp":round(100*edge,2),"roi_open":round(100*roi,2)}
    print(f"  {pc}: 胜率{wr*100:.1f}% vs 隐含{impl*100:.1f}%  edge={edge*100:+.2f}pp  ROI开盘={roi*100:+.2f}%")

# PART C 1X2 drift @opening
print("\n[PART C] 1X2 漂移信号 @开盘价下注")
sig_c = [
 ("主家压低→押H@开盘", dH<=-TH, 0),
 ("客家压低→押A@开盘", dA<=-TH, 2),
 ("平局升(>2%)→押H@开盘", (dD>=TH)&(dH<=-TH*0.5), 0),
 ("主压低+平升(强主)→押H@开盘", (dH<=-TH)&(dD>=TH), 0),
 ("主升(>2%)+客压低→押A@开盘", (dH>=TH)&(dA<=-TH), 2),
]
part_c = {}
for name,mask,k in sig_c:
    r=ev(mask,k); part_c[name]=r
    if r.get("skip"): print(f"  {name}: n={r['n']} 不足"); continue
    tag="✅+EV" if r["pos_ev"] else "❌"
    print(f"  {name}: n={r['n']} 胜率{r['win_rate']}% 隐含{r['implied']}% edge={r['edge_pp']:+}pp ROI={r['roi_open']:+}% {tag}")

# PART D drift magnitude
print("\n[PART D] 主家漂移幅度分桶 (押H@开盘)")
part_d = {}
for lab,lo,hi in [("微(>-0.5%)",-0.005,None),("小(-0.5~-2%)",-0.02,-0.005),
                  ("中(-2~-5%)",-0.05,-0.02),("大(<-5%)",None,-0.05)]:
    m = (dH<=hi) if (lo is None) else ((dH<=lo)&(dH>hi)) if (hi is not None) else (dH<=lo)
    if lo is None and hi is None: m=dH<=-TH
    r=ev(m,0); part_d[lab]=r
    if not r.get("skip"):
        print(f"  主家{lab}: n={r['n']} 胜率{r['win_rate']}% 隐含{r['implied']}% edge={r['edge_pp']:+}pp ROI={r['roi_open']:+}%")

pos=[k for k,v in part_c.items() if isinstance(v,dict) and v.get("pos_ev")]
print(f"\n[诚实校验] rb_matches 显著+EV信号 = {len(pos)} / {len(sig_c)}")
print("  ", pos if pos else "无 → 1X2 开盘价下漂移信号仍被抽水吸收 (需去 GQ 挖跨市场tick)")

out={"meta":{"db":DB,"n":N,"th":TH,
     "note":"rb_matches 仅 1X2开收可靠; AH/OU 仅~5k行, 跨市场协调见 GQ odds_changes 深挖"},
     "part_a_oracle":part_a,"part_b_plate_edge":part_b,
     "part_c_1x2_drift":part_c,"part_d_drift_mag":part_d,"pos_ev_signals":pos}
with open(OUT,"w",encoding="utf-8") as f: json.dump(out,f,ensure_ascii=False,indent=2)
print(f"\n[done] 写出 {OUT}")
