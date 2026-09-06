# -*- coding: utf-8 -*-
"""CS(波胆)派生特征能否识别平局? 仅GQ有op_cs解析字段。"""
import sqlite3, math

con = sqlite3.connect("data/rollball_training.db"); con.row_factory=sqlite3.Row
rows=[dict(r) for r in con.execute(
  "SELECT cs_fav_odds, cs00_odds, cs_low_avg, is_draw FROM rb_matches WHERE src='gq' AND cs_fav_odds IS NOT NULL")]
con.close()

def auc(probs,labels):
    pairs=sorted(zip(probs,labels),key=lambda x:-x[0])
    pos=sum(labels); neg=len(labels)-pos
    if pos==0 or neg==0: return float('nan')
    rank=sum(i+1 for i,(p,l) in enumerate(pairs) if l)
    return (rank-pos*(pos+1)/2)/(pos*neg)

print(f"样本: {len(rows)}, 平局={sum(r['is_draw'] for r in rows)}")

# AUC: 赔率越低越像平局 -> 取负
for name,key in [("cs00(0-0赔率)","cs00_odds"),("cs_low_avg(低总球均赔)","cs_low_avg"),("cs_fav(最热比分赔率)","cs_fav_odds")]:
    d=[(r[key],r["is_draw"]) for r in rows if r[key] is not None]
    if d:
        a=auc([-x for x,_ in d],[y for _,y in d])
        print(f"[AUC] {name}: {a:.4f}")

print("\n[校准] cs00 分箱 -> 实际平局率:")
edges=[4,6,8,10,12,14,16,99]
for i in range(len(edges)-1):
    b=[r for r in rows if r["cs00_odds"] and edges[i]<=r["cs00_odds"]<edges[i+1]]
    if not b: continue
    hr=sum(r["is_draw"] for r in b)/len(b)
    print(f"  cs00[{edges[i]},{edges[i+1]}): n={len(b):4d}, 平局率={hr:.3f}")

print("\n[校准] cs_low_avg 分箱 -> 实际平局率:")
for i in range(len(edges)-1):
    b=[r for r in rows if r["cs_low_avg"] and edges[i]<=r["cs_low_avg"]<edges[i+1]]
    if not b: continue
    hr=sum(r["is_draw"] for r in b)/len(b)
    print(f"  cs_low_avg[{edges[i]},{edges[i+1]}): n={len(b):4d}, 平局率={hr:.3f}")
