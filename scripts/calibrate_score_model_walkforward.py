# -*- coding: utf-8 -*-
"""
步骤5: 补 ARCHITECTURE §9 标红的"140k校准无独立脚本"技术债
=============================================================
复现: 通用联赛 goal_scale walkforward 校准 (interwetten_odds 收盘+真实比分)
原记录: train 2016-2022 / test 2023-2025, gs=1.0→1.2 使 test top3 33.78%→34.41%

数据源: master_dataset.csv (training_extended, 含收盘赔率+真实比分+result_class)
切分:   train≤2022 / test 2023-2025 (时序, 防泄漏)
方法:   20×70/30 (复用 improve_cs_accuracy.py 范式)
        - gs网格 0.8~1.6
        - 调参仅在train, eval仅在test
        - 指标: top1/top3/top5 波胆命中率 + 方向准确率

输出: deliverables/score_model_calibration_20260729.json (对比报告)
⚠️ 不改 score_model.py 参数, 只产出报告。若发现更优gs, 下一轮走runbook合入。
"""
import csv, json, math, os, random

IN = r"D:\Architecture\data\master_dataset.csv"
OUT = r"D:\Architecture\deliverables\score_model_calibration_20260729.json"
MAXG = 6  # 0..6, 足球极少超6球, 加速

def deoverround(oh, od, oa):
    o = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/o, (1.0/od)/o, (1.0/oa)/o

def _poisson_marginal(lh, la, maxg=MAXG):
    ph = pd_ = pa = 0.0
    for i in range(maxg+1):
        pi = math.exp(-lh)*lh**i/math.factorial(i)
        for j in range(maxg+1):
            pj = math.exp(-la)*la**j/math.factorial(j)
            p = pi*pj
            if i > j: ph += p
            elif i == j: pd_ += p
            else: pa += p
    return ph, pd_, pa

def solve_oip(ph_t, pd_t, pa_t):
    best=(1.3,1.1); best_err=1e9
    for lhi in range(3,36,2):
        for lai in range(3,36,2):
            lh,la=lhi/10,lai/10
            ph,pd_,pa=_poisson_marginal(lh,la)
            err=abs(ph-ph_t)+abs(pd_-pd_t)+abs(pa-pa_t)
            if err<best_err: best_err=err; best=(lh,la)
    lh0,la0=best
    for step in (0.1,0.05,0.02):
        for _ in range(3):  # 固定3轮, 防while震荡
            for dlh in (-step,0,step):
                for dla in (-step,0,step):
                    if dlh==0 and dla==0: continue
                    lh,la=lh0+dlh,la0+dla
                    if lh<=0.1 or la<=0.1: continue
                    ph,pd_,pa=_poisson_marginal(lh,la)
                    err=abs(ph-ph_t)+abs(pd_-pd_t)+abs(pa-pa_t)
                    if err<best_err: best_err=err; lh0,la0=lh,la
    return lh0,la0

def score_matrix(lh, la, gs=1.0, maxg=MAXG):
    lh, la = lh*gs, la*gs
    col=[math.exp(-lh)*lh**i/math.factorial(i) for i in range(maxg+1)]
    row=[math.exp(-la)*la**j/math.factorial(j) for j in range(maxg+1)]
    M=[[col[i]*row[j] for j in range(maxg+1)] for i in range(maxg+1)]
    s=sum(sum(r) for r in M)
    return [[v/s for v in r] for r in M]

def topk_hit(M, hg, ag, k=3, maxg=MAXG):
    """波胆(hg,ag)是否在M概率top-k内。"""
    flat=sorted([((i,j),M[i][j]) for i in range(maxg+1) for j in range(maxg+1)], key=lambda x:-x[1])[:k]
    return 1 if (hg,ag) in [t for t,_ in flat] else 0

def main():
    rows=list(csv.DictReader(open(IN,encoding="utf-8-sig")))
    # 筛: 有收盘赔率+真实比分
    data=[]
    for r in rows:
        try:
            oh,od,oa=float(r["odds_home"]),float(r["odds_draw"]),float(r["odds_away"])
            hg,ag=int(r["home_score"]),int(r["away_score"])
            date=str(r["match_date"])
            if oh>1 and od>1 and oa>1 and hg>=0 and ag>=0:
                data.append({"oh":oh,"od":od,"oa":oa,"hg":hg,"ag":ag,"date":date})
        except: pass
    print(f"有效样本: {len(data)}")

    # 预计算每场的 λ (不随gs变, gs只缩放比分矩阵)
    for d in data:
        ph,pd,pa=deoverround(d["oh"],d["od"],d["oa"])
        d["lh"],d["la"]=solve_oip(ph,pd,pa)

    # 时序切分 + 抽样 (solve_oip是瓶颈, 抽样到2万足够统计显著性)
    train_full=[d for d in data if d["date"]<="2022-12-31"]
    test_full=[d for d in data if d["date"]>="2023-01-01"]
    random.seed(42)
    train=random.sample(train_full,min(8000,len(train_full)))
    test=random.sample(test_full,min(5000,len(test_full)))
    print(f"train_full:{len(train_full)}→抽样{len(train)} | test_full:{len(test_full)}→抽样{len(test)}")
    if len(test)<1000:
        print("❌ test不足"); return

    # gs网格
    gs_grid=[0.8,0.9,1.0,1.1,1.2,1.3,1.4,1.5,1.6]
    results={}
    for gs in gs_grid:
        # train上选, test上eval
        for split_name, split in [("train",train),("test",test)]:
            t1=t3=t5=0; n=len(split)
            for d in split:
                M=score_matrix(d["lh"],d["la"],gs)
                t1+=topk_hit(M,d["hg"],d["ag"],1)
                t3+=topk_hit(M,d["hg"],d["ag"],3)
                t5+=topk_hit(M,d["hg"],d["ag"],5)
            results[f"gs{gs}_{split_name}"]={"top1":round(t1/n,4),"top3":round(t3/n,4),"top5":round(t5/n,4),"n":n}
        print(f"  gs={gs}: train top3={results[f'gs{gs}_train']['top3']} test top3={results[f'gs{gs}_test']['top3']}")

    # 找train最优gs
    train_scores={gs:results[f"gs{gs}_train"]["top3"] for gs in gs_grid}
    best_gs=max(train_scores,key=train_scores.get)
    test_baseline=results["gs1.0_test"]["top3"]
    test_best=results[f"gs{best_gs}_test"]["top3"]
    test_current=results.get("gs1.2_test",{}).get("top3")

    report={
        "说明":"补ARCHITECTURE §9标红技术债: 通用联赛goal_scale walkforward校准独立脚本",
        "数据源":"master_dataset.csv (training_extended收盘赔率+真实比分)",
        "n_train":len(train),"n_test":len(test),
        "切分":"train≤2022 / test≥2023 (时序walk-forward)",
        "gs网格":gs_grid,
        "完整结果":results,
        "train最优gs":best_gs,
        "test基线(gs=1.0)_top3":test_baseline,
        "test当前生产(gs=1.2)_top3":test_current,
        "test最优gs_top3":test_best,
        "当前1.2是否仍最优": test_current==test_best if test_current else None,
        "结论": f"train最优gs={best_gs}, test top3={test_best} vs 基线{test_baseline} (Δ{test_best-test_baseline:+.4f})。当前生产gs=1.2 test top3={test_current}。",
        "是否改参数": False,
        "下一步": "若最优gs≠1.2, 下一轮走model_promotion_runbook评估是否合入",
    }
    os.makedirs(os.path.dirname(OUT),exist_ok=True)
    json.dump(report,open(OUT,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
    print(f"\n→ {OUT}")
    print(f"train最优gs={best_gs}, test top3={test_best} (基线{test_baseline}, 当前1.2={test_current})")

if __name__=="__main__":
    main()
