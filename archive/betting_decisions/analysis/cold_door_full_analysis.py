"""
冷门波胆全量分析（1822 命中 / 5536 完赛）  v3 最终版
====================================================
关键事实（已诊断）：
  * pre_odds_json 网格含 4-4 以上比分 -> SCORES 扩到 0-0..5-5（覆盖99.4%真实比分）。
  * hit(=1,1822) = 真实比分落在网格内；out_grid=3714 含5+大比分与网格缺该比分两类。
  * 干净 FLB 只在 in_grid 子集算（剔除网格截断混淆）。
  * 近10场进球从 GQ.matches 取（队名一致），覆盖低(~1%)如实标注。

全部特征赛前，零终场泄露。产出 score-level 数据集供建模。
"""
import sqlite3, json, re, math
import numpy as np
import pandas as pd
from collections import defaultdict

GQ = "D:/Architecture/data/events.db"
SCORES = [f"{h}-{a}" for h in range(6) for a in range(6)]   # 0-0..5-5
SCORE_SET = set(SCORES)
SCORE_RE = re.compile(r"^\d+-\d+$")

def devig_cs(grid):
    inv = {s: 1.0/o for s,o in grid.items() if s in SCORE_SET
           and isinstance(o,(int,float)) and o>1.0}
    tot = sum(inv.values())
    return {s:p/tot for s,p in inv.items()} if tot>0 else None

def parse_dt(s):
    if not s: return None
    s=str(s)[:10].replace("-","")
    try: return float(s)
    except: return None

# ---------------------------------------------------------------- 1) cs_verification
con=sqlite3.connect(GQ); cur=con.cursor()
cur.execute("""SELECT match_key,home,away,league,kickoff,actual_score,
                       favorite_score,favorite_odds,actual_odds,actual_implied,hit,pre_odds_json
                FROM cs_verification WHERE pre_odds_json IS NOT NULL""")
rows=cur.fetchall(); con.close()
print(f"[load] usable rows = {len(rows)}")

recs=[]; match_meta=[]
for mk,home,away,lg,ko,sc,fs,fo,ao,ai,hit,pj in rows:
    try: grid=json.loads(pj)
    except: continue
    dev=devig_cs(grid)
    if not dev: continue
    actual=sc.strip() if sc else None
    in_grid = 1 if (actual in dev) else 0
    exp_tot=sum((int(s[0])+int(s[2]))*p for s,p in dev.items())
    for s in SCORES:
        if s not in grid: continue
        recs.append({
            "match_key":mk,"home":home,"away":away,"league":lg or "",
            "kickoff":ko or "","score":s,"odds":float(grid[s]),
            "mkt_implied":dev.get(s),"exp_tot_goals":round(exp_tot,3),
            "log_odds":math.log(float(grid[s])),
            "hit":1 if actual==s else 0,
            "in_grid_match":in_grid,
            "is_fav_score":1 if (fs and fs==s) else 0,
        })
    match_meta.append({"match_key":mk,"actual":actual,"in_grid":in_grid,
                       "fav_odds":fo,"act_odds":ao,"act_implied":ai})
df=pd.DataFrame(recs)
meta=pd.DataFrame(match_meta)
print(f"[load] score-level rows = {len(df)}  matches = {df.match_key.nunique()}")
print(f"        in_grid matches = {int(meta.in_grid.sum())}  out_grid = {len(meta)-int(meta.in_grid.sum())}")

# ---------------------------------------------------------------- 2) FLB 校准（仅 in_grid 子集，逐场质量）
print("\n=== FLB 校准（仅真实比分在网格内的 1822 场，逐场质量）===")
sub=df[df["in_grid_match"]==1]
pred_mass=defaultdict(float); emp_mass=defaultdict(float); nm=0
for mk,g in sub.groupby("match_key"):
    nm+=1
    for b,lo,hi in [("A:<5",1,5),("B:5-8",5,8),("C:8-12",8,12),("D:12-20",12,20),("E:20-40",20,40),("F:>=40",40,1e9)]:
        sg=g[(g["odds"]>=lo)&(g["odds"]<hi)]
        if len(sg)==0: continue
        pred_mass[b]+=sg["mkt_implied"].sum()
        emp_mass[b]+=1.0 if (sg["hit"]==1).any() else 0.0
flb=[]
for b in ["A:<5","B:5-8","C:8-12","D:12-20","E:20-40","F:>=40"]:
    pm=pred_mass[b]/nm; em=emp_mass[b]/nm
    flb.append({"bucket":b,"pred_mass":round(pm,4),"emp_mass":round(em,4),
                "ratio":round(em/pm,3) if pm>0 else None})
    print(f"  {b:8s} 预测质量={pm:.4f} 实证质量={em:.4f} ratio={em/pm:.3f}")
print("  ratio<1 = 该桶被市场高估（FLB）。长赔方桶应更低。")

# ---------------------------------------------------------------- 3) 近10场进球（GQ.matches）
print("\n=== 球队近10场进球（GQ.matches 一致队名）===")
con=sqlite3.connect(GQ); cur=con.cursor()
cur.execute("""SELECT home,away,league,kickoff,score_home,score_away FROM matches
                WHERE score_home IS NOT NULL AND score_away IS NOT NULL AND kickoff IS NOT NULL""")
mm=cur.fetchall(); con.close()
tm=defaultdict(list)
def norm(s): return re.sub(r"\s+","",s or "").lower()
for ht,at,lg,ko,hs,aws in mm:
    d=parse_dt(ko)
    if d is None: continue
    tg=int(hs)+int(aws)
    tm[(norm(ht),norm(lg))].append((d,tg)); tm[(norm(at),norm(lg))].append((d,tg))
for k in tm: tm[k].sort()
def last10(team,league,kickoff):
    lst=tm.get((norm(team),norm(league)))
    if not lst: return np.nan
    kd=parse_dt(kickoff)
    if kd is None: return np.nan
    past=[tg for d,tg in lst if d<kd]
    return float(np.mean(past[-10:])) if len(past)>=3 else np.nan
df["home_l10"]=df.apply(lambda r:last10(r["home"],r["league"],r["kickoff"]),axis=1)
df["away_l10"]=df.apply(lambda r:last10(r["away"],r["league"],r["kickoff"]),axis=1)
cov=df["home_l10"].notna()&df["away_l10"].notna()
print(f"  覆盖率 = {cov.mean()*100:.1f}% ({cov.sum()}/{len(df)})")
if cov.sum()>100:
    try:
        from sklearn.metrics import roc_auc_score
        s2=df[cov].copy()
        auc=roc_auc_score(s2["hit"].values,(s2["home_l10"]+s2["away_l10"]).values)
        print(f"  [区分力] 双方近10均球 预测 hit 的 AUC = {auc:.3f} (0.5=无区分)")
    except Exception as e: print("  AUC err",e)

# ---------------------------------------------------------------- 4) 保存
df.to_csv("analysis/cold_door_scorelevel.csv",index=False)
with open("analysis/cold_door_hit_stats.json","w",encoding="utf-8") as f:
    json.dump({
        "n_matches":int(df.match_key.nunique()),
        "n_in_grid":int(meta.in_grid.sum()),
        "n_score_rows":int(len(df)),
        "flb_in_grid":flb,
        "last10_coverage_pct":round(float(cov.mean()*100),1),
        "note":"FLB仅在真实比分在网格内的子集计算；近10场覆盖率低，本数据集不可作稳定特征",
    },f,ensure_ascii=False,indent=2)
print("\n[saved] analysis/cold_door_scorelevel.csv + cold_door_hit_stats.json")
