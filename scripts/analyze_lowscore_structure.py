# -*- coding: utf-8 -*-
"""
哨响AI · 0-2球(under2.5 = 0/1/2球)赔率结构分析
================================================
目的: 涛哥 2026-08-03 指示 "多采集一些全场数据, 详细分析0到2球的赔率结构, 就能找到正确方向".
方法:
  A. 真值底: football_data.db historical_matches 31.2万场 total_goals -> 0/1/2/3+ 真实分布 + under1.5/2.5
  B. GQ CS波胆阶梯: op_cs 解析 -> 0-2球在波胆阶梯中的隐含占比 (renormalized) vs 实际 total<=2
  C. GQ OU: 按真实盘口线 devig -> 隐含下盘率 vs 实际 (聚焦 0-2球段 line∈{2.25,2.5,2.75})
  D. CS隐含 vs OU隐含 一致性 (结构矛盾=信号)
  E. 汇总"正确方向" = 系统性偏差所在分箱
注: GQ为单庄(乐鱼), 单庄去水无pick-edge; 此处找的是"模型校准/方向"信号, 非跨庄套利.
"""
import sqlite3, os, json, statistics as st

ROOT = "D:/Architecture"
FD = os.path.join(ROOT, "data", "football_data.db")
GQ = os.path.join(ROOT, "data", "events.db")

def conn(db):
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row; return c

def devig_ou(over, under):
    """返回 (p_over, p_under) 去水后概率"""
    if not over or not under: return None, None
    io, iu = 1.0/over, 1.0/under
    s = io + iu
    if s <= 0: return None, None
    return io/s, iu/s

def under_hit(total, line):
    """OU下盘命中: total < line (整数球, 半/四分之一线均适用)"""
    return total < line

# ============================================================
# A. 真值底: 31.2万场 0/1/2/3+ 分布
# ============================================================
print("="*70)
print("A. 全场数据真值底 (football_data.db · historical_matches)")
print("="*70)
c = conn(FD); cur = c.cursor()
cur.execute("SELECT total_goals, COUNT(*) n FROM historical_matches WHERE total_goals IS NOT NULL GROUP BY total_goals")
rows = cur.fetchall()
tot = sum(r["n"] for r in rows)
dist = {r["total_goals"]: r["n"] for r in rows}
P = lambda ks: sum(dist.get(k,0) for k in ks)/tot
print(f"总场数: {tot:,}")
print(f"  P(0球)={P([0]):.2%}  P(1球)={P([1]):.2%}  P(2球)={P([2]):.2%}  -> P(0-2球)={P([0,1,2]):.2%}")
print(f"  P(3+球)={P([3,4,5,6,7,8]):.2%}")
print(f"  真实 under1.5 (0/1球) = {P([0,1]):.2%}")
print(f"  真实 under2.5 (0/1/2球) = {P([0,1,2]):.2%}")
print(f"  真实 under3.5 (0-3球) = {P([0,1,2,3]):.2%}")
# 主要联赛 under2.5 真实率 (方向可能因联赛而异)
cur.execute("""SELECT league_name, COUNT(*) n,
  SUM(CASE WHEN (home_score+away_score)<=2 THEN 1 ELSE 0 END)*1.0/COUNT(*) u25
  FROM historical_matches WHERE home_score IS NOT NULL AND league_name IS NOT NULL
  GROUP BY league_name HAVING n>=2000 ORDER BY n DESC LIMIT 15""")
print("\n  主要联赛 under2.5 真实率 (样本>=2000):")
for r in cur.fetchall():
    print(f"    {str(r['league_name'])[:24]:24s} n={r['n']:>7,}  under2.5={r['u25']:.1%}")
c.close()

# ============================================================
# B. GQ CS波胆阶梯: 0-2球隐含占比 vs 实际
# ============================================================
print("\n" + "="*70)
print("B. GQ 波胆阶梯结构 (op_cs) — 0-2球在波胆中的隐含占比")
print("="*70)
c = conn(GQ); cur = c.cursor()
cur.execute("""SELECT home, away, score_home, score_away, op_cs, op_ou_line, op_ou_over, op_ou_under
  FROM match_outcomes
  WHERE score_home IS NOT NULL AND op_cs IS NOT NULL AND op_cs!='[]' """)
cs_rows = cur.fetchall()
c.close()

def cs_implied_under25(op_cs):
    try:
        lst = json.loads(op_cs)
    except Exception:
        return None
    if not isinstance(lst, list) or not lst: return None
    imp = {}  # score -> 1/odds
    for item in lst:
        if not isinstance(item, list) or len(item) < 2: continue
        sc, od = item[0], item[1]
        try: od = float(od)
        except Exception: continue
        if od <= 1.01: continue
        # 解析比分
        parts = str(sc).split("-")
        if len(parts) != 2: continue
        try: g0, g1 = int(parts[0]), int(parts[1])
        except Exception: continue
        imp[sc] = 1.0/od
    if not imp: return None
    tot_imp = sum(imp.values())
    if tot_imp <= 0: return None
    low = sum(v for k,v in imp.items() if (int(k.split('-')[0])+int(k.split('-')[1])) <= 2)
    return low/tot_imp, tot_imp

cs_pairs = []  # (implied_under25, actual_under25_bool)
cs_low_scores = []  # 每场 0-2球占比
for r in cs_rows:
    res = cs_implied_under25(r["op_cs"])
    if res is None: continue
    share, tot_imp = res
    if tot_imp < 0.15: continue  # 波胆阶梯太短(只有几个盘)跳过
    tg = r["score_home"] + r["score_away"]
    cs_pairs.append((share, 1 if tg <= 2 else 0))
    cs_low_scores.append(share)

n_cs = len(cs_pairs)
if n_cs:
    act_rate = sum(b for _,b in cs_pairs)/n_cs
    mean_imp = st.mean(cs_low_scores)
    print(f"有效场数(波胆阶梯完整): {n_cs}")
    print(f"  波胆隐含 0-2球占比 均值 = {mean_imp:.1%}")
    print(f"  实际 0-2球 发生率     = {act_rate:.1%}")
    print(f"  整体偏差 (实际-隐含)   = {act_rate-mean_imp:+.1%}  "
          f"{'-> 庄家低估0-2球(买小方向)' if act_rate>mean_imp else '-> 庄家高估0-2球(买大方向)'}")
    # 分箱
    print("\n  按波胆隐含0-2占比分箱 (找系统偏差):")
    bins = [(0.30,0.40),(0.40,0.50),(0.50,0.60),(0.60,0.70),(0.70,1.01)]
    for lo,hi in bins:
        seg = [(imp,b) for imp,b in cs_pairs if lo<=imp<hi]
        if not seg: 
            print(f"    [{lo:.0%},{hi:.0%}) : (无样本)"); continue
        m = st.mean([imp for imp,_ in seg]); a = sum(b for _,b in seg)/len(seg)
        print(f"    [{lo:.0%},{hi:.0%}) n={len(seg):>4}  隐含={m:.1%}  实际={a:.1%}  偏差={a-m:+.1%}")

# ============================================================
# C. GQ OU: 按真实线 devig, 聚焦 0-2球段 (line 2.25/2.5/2.75 -> total<=2)
# ============================================================
print("\n" + "="*70)
print("C. GQ OU结构 — 按真实盘口线 (聚焦 0-2球段 line∈{2.25,2.5,2.75})")
print("="*70)
c = conn(GQ); cur = c.cursor()
cur.execute("""SELECT score_home, score_away, op_ou_line, op_ou_over, op_ou_under
  FROM match_outcomes
  WHERE score_home IS NOT NULL AND op_ou_line IS NOT NULL
    AND op_ou_over IS NOT NULL AND op_ou_under IS NOT NULL""")
ou_rows = cur.fetchall()
c.close()

seg_lines = [2.25, 2.5, 2.75]  # 这些线 under = total<=2 (即0-2球)
ou_pairs = []
for r in ou_rows:
    line = r["op_ou_line"]
    po, pu = devig_ou(r["op_ou_over"], r["op_ou_under"])
    if po is None: continue
    tg = r["score_home"] + r["score_away"]
    if line in seg_lines:
        ou_pairs.append((pu, 1 if under_hit(tg, line) else 0, line))
    else:
        # 也记录其他线, 用于看整体结构
        pass

if ou_pairs:
    # 总体
    act = sum(b for _,b,_ in ou_pairs)/len(ou_pairs)
    imp = st.mean([i for i,_,_ in ou_pairs])
    print(f"0-2球段总场数 (line 2.25/2.5/2.75): {len(ou_pairs)}")
    print(f"  OU隐含 下盘(0-2球) 均值 = {imp:.1%}")
    print(f"  实际 0-2球 发生率     = {act:.1%}")
    print(f"  整体偏差 (实际-隐含)   = {act-imp:+.1%}")
    print("\n  分线:")
    for ln in seg_lines:
        seg = [(i,b) for i,b,_ in ou_pairs if _==ln] if False else [(i,b) for i,b,l in ou_pairs if l==ln]
        if not seg: continue
        m = st.mean([i for i,_ in seg]); a = sum(b for _,b in seg)/len(seg)
        print(f"    line {ln}: n={len(seg):>4}  隐含下盘={m:.1%}  实际={a:.1%}  偏差={a-m:+.1%}")
    print("\n  按OU隐含下盘率分箱:")
    bins = [(0.30,0.45),(0.45,0.55),(0.55,0.65),(0.65,0.80)]
    for lo,hi in bins:
        seg = [(i,b) for i,b,_ in ou_pairs if lo<=i<hi]
        if not seg:
            print(f"    [{lo:.0%},{hi:.0%}) : (无样本)"); continue
        m = st.mean([i for i,_ in seg]); a = sum(b for _,b in seg)/len(seg)
        print(f"    [{lo:.0%},{hi:.0%}) n={len(seg):>4}  隐含={m:.1%}  实际={a:.1%}  偏差={a-m:+.1%}")

# ============================================================
# D. CS隐含 vs OU隐含 一致性
# ============================================================
print("\n" + "="*70)
print("D. CS隐含 vs OU隐含 一致性 (结构矛盾信号)")
print("="*70)
c = conn(GQ); cur = c.cursor()
cur.execute("""SELECT score_home, score_away, op_cs, op_ou_line, op_ou_over, op_ou_under
  FROM match_outcomes
  WHERE score_home IS NOT NULL AND op_cs IS NOT NULL AND op_cs!='[]'
    AND op_ou_line IN (2.25,2.5,2.75) AND op_ou_over IS NOT NULL AND op_ou_under IS NOT NULL""")
both = cur.fetchall()
c.close()
diffs = []
for r in both:
    csres = cs_implied_under25(r["op_cs"])
    if csres is None: continue
    cs_share, _ = csres
    po, pu = devig_ou(r["op_ou_over"], r["op_ou_under"])
    if pu is None: continue
    diffs.append(cs_share - pu)
if diffs:
    print(f"同时有CS+OU(0-2段)场数: {len(diffs)}")
    print(f"  CS隐含- OU隐含 均值差 = {st.mean(diffs):+.1%}")
    print(f"  |差|>10pp 的场数 = {sum(1 for d in diffs if abs(d)>0.10)} ({sum(1 for d in diffs if abs(d)>0.10)/len(diffs):.1%})")
    print("  -> 两口径对0-2球定价分歧大 => 结构矛盾, 可作模型不确定/谨慎信号")

# ============================================================
# E. 正确方向汇总
# ============================================================
print("\n" + "="*70)
print("E. 正确方向 (系统偏差汇总)")
print("="*70)
print("  1) 真值底: under2.5 全局≈50% (coin-flip), 但联赛间差异大(见A表) — 方向须按联赛校准, 不可全局一刀切.")
print("  2) GQ单庄CS/OP两口径对0-2球的隐含 vs 实际偏差(见B/C) 即本批采集数据的系统方向.")
print("  3) 单庄无跨庄edge; 上述偏差用于: ① 校准 ou_eval.ou_confidence 闸门 ② 按联赛修正 league_ou_prob.")
print("  4) 下一步'多采集': 让GQ采集器持续累积 match_outcomes (当前2206场, 带OU1276/CS1472);")
print("     若要历史级OU赔率结构, 需补主流联赛历史OU盘口源(当前历史库OU赔率极缺).")
print("="*70)
