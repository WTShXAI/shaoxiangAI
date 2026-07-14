"""
方向#2 自主验证: 子市场剥削 — OIP Poisson 在五大联赛的子市场技能
===========================================================
数据: william_ht (五大联赛 WH 1X2 close + 实际 ft_total/h_ft/a_ft + label)
引擎: pipeline.score_model.predict_score (OIP 生产核心, 去抽水→λ→比分矩阵)
范围: 仅五大联赛 (严守"守卫/预测链路不接WC"); 子市场线(庄家O/U/波胆赔率)在
      当前DB的五大联赛中不存在 → 本验证用"实际赛果"做校准/准确率检验,
      而非"击败庄家子市场盘口"(需数据补齐, 见 ruling #3).

两个子市场技能检验(对齐"模型在子市场有真实竞争力"前提):
  T1 大小球(O/U)校准: OIP P(Over 2.5/1.5/3.5) 可靠性曲线 + Brier vs 常数基线 + 五分位lift
  T2 波胆(正确比分)准确率: OIP top1/top3 命中率 vs 朴素基线(恒猜1-1 / 2-1)

诚实边界: OIP 由收盘1X2赔率导出 → 其子市场概率是"有效市场翻译".
  若1X2有效, OIP在大小球上应良好校准(证明引擎是可靠翻译器);
  真正的"剥削"(OIP vs 庄家O/U/波胆盘口) 需五大联赛子市场赔率 → 数据缺口.
"""
import sqlite3, json, sys, math
import numpy as np
sys.path.insert(0, "D:/Architecture")
from pipeline.score_model import predict_score, deoverround

DB = "D:/Architecture/data/football_data.db"
MAJORS = ["英超", "西甲", "意甲", "德甲", "法甲"]
OUT = "D:/Architecture/deliverables/submarket_oip_validation.json"

def load():
    con = sqlite3.connect(DB); cur = con.cursor()
    rows = []
    for lg in MAJORS:
        cur.execute("""SELECT close_home_odds, close_draw_odds, close_away_odds,
                       h_ft, a_ft, ft_total, label
                       FROM william_ht
                       WHERE TRIM(league_name)=? AND label IS NOT NULL
                       AND close_home_odds>1.01 AND close_draw_odds>1.01 AND close_away_odds>1.01
                       AND h_ft IS NOT NULL AND a_ft IS NOT NULL AND ft_total IS NOT NULL
                       AND match_date IS NOT NULL""", (lg,))
        for r in cur.fetchall():
            rows.append(r)
    con.close()
    return rows

def main():
    rows = load()
    print(f"[load] 5-major rows with close 1X2 + actual score: {len(rows)}")

    over25_pred = []; over25_act = []
    over15_pred = []; over15_act = []
    over35_pred = []; over35_act = []
    cs_top1 = 0; cs_top3 = 0; cs_n = 0
    naive_11 = 0; naive_21 = 0; naive_n = 0
    cons_err = 0.0
    for (ch, cd, ca, h_ft, a_ft, ft_total, lab) in rows:
        try:
            r = predict_score("H", "A", ch, cd, ca)
        except Exception:
            continue
        M = r["matrix"]; mg = M.shape[0]-1  # max_goal
        # OIP 1X2 recovery vs deoverround (consistency)
        ph, pd, pa = deoverround(ch, cd, ca)
        cons_err = max(cons_err, abs(r["p_h"]-ph), abs(r["p_d"]-pd), abs(r["p_a"]-pa))
        # totals
        ov25 = float(sum(M[i,j] for i in range(mg+1) for j in range(mg+1) if i+j>=3))
        ov15 = float(sum(M[i,j] for i in range(mg+1) for j in range(mg+1) if i+j>=2))
        ov35 = float(sum(M[i,j] for i in range(mg+1) for j in range(mg+1) if i+j>=4))
        over25_pred.append(ov25); over25_act.append(1 if (h_ft+a_ft)>2 else 0)
        over15_pred.append(ov15); over15_act.append(1 if (h_ft+a_ft)>1 else 0)
        over35_pred.append(ov35); over35_act.append(1 if (h_ft+a_ft)>3 else 0)
        # correct score
        flat = M.flatten()
        order = np.argsort(-flat)[:3]
        top1 = divmod(int(np.argmax(flat)), mg+1)
        top3 = [divmod(int(k), mg+1) for k in order]
        cs_n += 1
        if (h_ft, a_ft) == top1: cs_top1 += 1
        if (h_ft, a_ft) in top3: cs_top3 += 1
        naive_n += 1
        if h_ft==1 and a_ft==1: naive_11 += 1
        if h_ft==2 and a_ft==1: naive_21 += 1

    n = len(over25_act)
    def brier(preds, acts):
        return float(np.mean((np.array(preds)-np.array(acts))**2))
    base25 = float(np.mean(over25_act)); base15 = float(np.mean(over15_act)); base35 = float(np.mean(over35_act))
    t1 = {
        "over25_base_rate": base25,
        "oip_brier_over25": brier(over25_pred, over25_act),
        "naive_brier_over25": brier([base25]*n, over25_act),
        "over15_base_rate": base15,
        "oip_brier_over15": brier(over15_pred, over15_act),
        "naive_brier_over15": brier([base15]*n, over15_act),
        "over35_base_rate": base35,
        "oip_brier_over35": brier(over35_pred, over35_act),
        "naive_brier_over35": brier([base35]*n, over35_act),
    }
    # reliability deciles for Over 2.5
    preds = np.array(over25_pred); acts = np.array(over25_act)
    edges = np.quantile(preds, np.linspace(0,1,11))
    rel = []
    for b in range(10):
        m = (preds>=edges[b]) & (preds<edges[b+1] if b<9 else preds<=edges[b+1])
        if m.sum()>0:
            rel.append({"pred_bin": round(float((edges[b]+edges[b+1])/2),3),
                        "emp_rate": round(float(acts[m].mean()),3), "n": int(m.sum())})
    t1["reliability_over25_deciles"] = rel
    # quintile lift
    q = np.quantile(preds, [0.2,0.4,0.6,0.8])
    topq = preds>=q[3]; botq = preds<q[0]
    t1["top_quintile_emp_over_rate"] = round(float(acts[topq].mean()),3) if topq.sum() else None
    t1["bottom_quintile_emp_over_rate"] = round(float(acts[botq].mean()),3) if botq.sum() else None

    t2 = {
        "oip_top1_hit": round(cs_top1/cs_n, 4),
        "oip_top3_hit": round(cs_top3/cs_n, 4),
        "naive_11_hit": round(naive_11/naive_n, 4),
        "naive_21_hit": round(naive_21/naive_n, 4),
        "n": cs_n,
    }
    consistency = {"max_1x2_recovery_abs_err": round(float(cons_err), 6)}

    result = {
        "scope": "5-major WH 1X2 close -> OIP sub-market validation vs ACTUALS (no bookmaker O/U/score odds for 5-major in DB)",
        "n_total": n,
        "T1_totals_OU": t1,
        "T2_correct_score": t2,
        "consistency": consistency,
        "data_gap": "五大联赛庄家O/U/波胆盘口赔率缺失 -> '击败子市场盘口'剥削检验需数据补齐 (betting_markets仅68场WC pinnacle, 按WC限制排除)",
    }
    v=[]
    v.append(f"大小球OIP Brier(Over2.5)={t1['oip_brier_over25']:.4f} vs 常数基线{t1['naive_brier_over25']:.4f} (低=好, Δ={t1['oip_brier_over25']-t1['naive_brier_over25']:+.4f})")
    if t1["top_quintile_emp_over_rate"] and t1["bottom_quintile_emp_over_rate"]:
        v.append(f"Over2.5 五分位: 顶档实际{100*t1['top_quintile_emp_over_rate']:.1f}% vs 底档{100*t1['bottom_quintile_emp_over_rate']:.1f}% (基线{base25*100:.1f}%)")
    v.append(f"波胆: OIP top1={t2['oip_top1_hit']*100:.1f}% / top3={t2['oip_top3_hit']*100:.1f}% vs 朴素1-1={t2['naive_11_hit']*100:.1f}% / 2-1={t2['naive_21_hit']*100:.1f}%")
    result["verdict_notes"] = v
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__=="__main__":
    main()
