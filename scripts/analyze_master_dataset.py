# -*- coding: utf-8 -*-
"""
步骤4: 31万行全量宽表特征深度分析
==================================
从 master_dataset.csv (31.5万行) 挖特征分布、标签平衡、信号强度、时间稳定性。
输出 data/master_feature_analysis.json
"""
import csv, json, math
from collections import Counter, defaultdict

IN = r"D:\Architecture\data\master_dataset.csv"
OUT = r"D:\Architecture\data\master_feature_analysis.json"

def nums(rows, key):
    out=[]
    for r in rows:
        v=r.get(key,"")
        if v=="" or v is None: continue
        try: out.append(float(v))
        except: pass
    return out

def dist(vals):
    if not vals: return {"n":0}
    vals=sorted(vals); n=len(vals)
    mean=sum(vals)/n
    var=sum((x-mean)**2 for x in vals)/n
    return {"n":n,"min":round(vals[0],3),"max":round(vals[-1],3),
            "mean":round(mean,3),"median":round(vals[n//2],3),
            "p25":round(vals[n//4],3),"p75":round(vals[3*n//4],3),
            "stdev":round(var**0.5,3)}

def main():
    rows=list(csv.DictReader(open(IN,encoding="utf-8-sig")))
    print(f"加载 {len(rows)} 行")
    R={"总行数":len(rows)}

    # ══ 1. 标签分布 ══
    R["标签分布"]={
        "1X2(result_class)":dict(Counter(r.get("result_class") for r in rows)),
        "半场赛果(ht_label)":dict(Counter(r.get("ht_label") for r in rows if r.get("ht_label"))),
    }
    # 总进球分布
    totals=[int(float(r["te_total_goals"])) for r in rows if r.get("te_total_goals") and r["te_total_goals"]!=""]
    tc=Counter(totals)
    R["标签分布"]["全场总进球"]=dict(sorted(tc.items()))
    R["标签分布"]["大球占比(>2.5)"]=round(sum(1 for t in totals if t>2.5)/len(totals),4) if totals else 0

    # ══ 2. 赔率特征分布 ══
    R["赔率特征分布"]={}
    R["赔率特征分布"]["收盘主赔odds_home"]=dist(nums(rows,"odds_home"))
    R["赔率特征分布"]["收盘平赔odds_draw"]=dist(nums(rows,"odds_draw"))
    R["赔率特征分布"]["抽水率odds_overround"]=dist(nums(rows,"odds_overround"))
    R["赔率特征分布"]["隐含P平odds_imp_d"]=dist(nums(rows,"odds_imp_d"))
    R["赔率特征分布"]["drift_magnitude"]=dist(nums(rows,"drift_magnitude"))

    # ══ 3. drift信号 (开收盘赔率变化) — 与赛果的关系 ══
    drift_signal={}
    for ddir in ("0","1","-1"):
        sub=[r for r in rows if r.get("drift_direction")==ddir and r.get("result_class")]
        if sub:
            rc=Counter(r["result_class"] for r in sub)
            n=len(sub)
            drift_signal[f"drift方向{ddir}"]= {
                "n":n,
                "主胜率":round(rc.get("0",0)/n,3),
                "平局率":round(rc.get("1",0)/n,3),
                "客胜率":round(rc.get("2",0)/n,3),
            }
    R["drift信号(开→收盘变化方向 vs 赛果)"]=drift_signal

    # ══ 4. 半场vs全场关系 (18.7万有半场的子集) ══
    ht_rows=[r for r in rows if r.get("ht_label")]
    if ht_rows:
        ht_full={}
        for hl in ("0","1","2"):
            sub=[r for r in ht_rows if r.get("ht_label")==hl and r.get("result_class")]
            if sub:
                rc=Counter(r["result_class"] for r in sub)
                n=len(sub)
                hl_name={"0":"半场主领先","1":"半场平","2":"半场客领先"}[hl]
                ht_full[hl_name]={
                    "n":n,
                    "最终主胜率":round(rc.get("0",0)/n,3),
                    "最终平局率":round(rc.get("1",0)/n,3),
                    "最终客胜率":round(rc.get("2",0)/n,3),
                }
        R["半场→全场赛果转换"]=ht_full

    # ══ 5. 隐含P(平) vs 实际平局 (draw_signal核心验证) ══
    imp_d_vals=[]
    for r in rows:
        v=r.get("odds_imp_d","")
        if v:
            try:
                imp_d_vals.append((float(v), r.get("result_class")=="1"))
            except: pass
    if imp_d_vals:
        # 按隐含P(平)分5档, 看实际平局率
        imp_d_vals.sort()
        bins=[]
        bsize=len(imp_d_vals)//5
        for i in range(5):
            chunk=imp_d_vals[i*bsize:(i+1)*bsize if i<4 else len(imp_d_vals)]
            actual_draw=sum(1 for _,d in chunk if d)/len(chunk)
            mean_imp=sum(v for v,_ in chunk)/len(chunk)
            bins.append({"隐含P平档":f"{round(mean_imp,3)}","n":len(chunk),"实际平局率":round(actual_draw,3),"校准比":round(actual_draw/mean_imp,3) if mean_imp else 0})
        R["平局校准(隐含P平 vs 实际平局率)"]=bins

    # ══ 6. 时间稳定性 ══
    years=Counter(str(r.get("match_date",""))[:4] for r in rows)
    R["时间分布(按年)"]=dict(sorted(years.items()))
    # 抽水率是否随时间漂移
    by_year=defaultdict(list)
    for r in rows:
        y=str(r.get("match_date",""))[:4]
        v=r.get("odds_overround","")
        if y and v:
            try: by_year[y].append(float(v))
            except: pass
    R["抽水率时间漂移"]={y:round(sum(v)/len(v),3) for y,v in sorted(by_year.items())}

    json.dump(R,open(OUT,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
    print(f"→ {OUT}")
    # 关键结论
    print("\n=== 关键结论 ===")
    print(f"1X2标签: {R['标签分布']['1X2(result_class)']}")
    if "drift信号(开→收盘变化方向 vs 赛果)" in R:
        for k,v in R["drift信号(开→收盘变化方向 vs 赛果)"].items():
            print(f"  {k}: 主{v['主胜率']} 平{v['平局率']} 客{v['客胜率']}")
    if "半场→全场赛果转换" in R:
        print("半场→全场:")
        for k,v in R["半场→全场赛果转换"].items():
            print(f"  {k}(n={v['n']}): 终主胜{v['最终主胜率']} 终平{v['最终平局率']} 终客胜{v['最终客胜率']}")
    if "平局校准(隐含P平 vs 实际平局率)" in R:
        print("平局校准(隐含 vs 实际):")
        for b in R["平局校准(隐含P平 vs 实际平局率)"]:
            print(f"  隐含{b['隐含P平档']} → 实际{b['实际平局率']} (校准比{b['校准比']})")

if __name__=="__main__":
    main()
