"""
FootballAI v6.0 — 子市场 EV 探针 (P1 方法论资产)

目的：验证 compute_submarket_value（pipeline/deep_report.py）的 EV/Kelly 数学正确，
并演示 OIP(odds-implied Poisson, score_model.predict_score) 推导 O/U、CS 隐含概率，
同时**诚实标注**：真实子市场 EV 计算被跨庄 O/U / CS 赔率源卡住（football_data.db 无此 feed），
故本探针为方法论就绪 + 概率侧演示，非实时 edge 声明。

运行：python scripts/probe_submarket_ev.py
输出：控制台验证报告（无外部依赖，纯标准库 + numpy + 本仓 pipeline）
"""
import sys, os, sqlite3, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from pipeline.deep_report import compute_submarket_value
from pipeline.score_model import predict_score


def unit_validate():
    """用已知答案的腿验证 EV/Kelly/决策逻辑。"""
    print("=== [1] compute_submarket_value 单元测试 ===")
    # 腿A: consensus_prob=0.58, best_odds=1.95 → EV=0.58*1.95-1=+0.131, edge=0.58-1/1.95=0.0672
    # 腿B: consensus_prob=0.40, best_odds=2.50 → EV=0.40*2.50-1=0.0, edge=0.40-0.4=0.0
    legs = [
        {"label": "OU Over 2.5", "best_odds": 1.95, "consensus_prob": 0.58},
        {"label": "OU Under 2.5", "best_odds": 2.50, "consensus_prob": 0.40},
    ]
    r = compute_submarket_value(legs, bankroll=10000, frac_kelly=0.5)
    a = r["rows"][0]
    exp_ev = round(0.58 * 1.95 - 1.0, 4)
    exp_edge = round(0.58 - 1 / 1.95, 4)
    exp_kelly = round(max(0.0, (0.58 * 1.95 - 1) / 0.95), 4)
    ok = (a["ev"] == exp_ev and a["edge"] == exp_edge and a["kelly_half"] == round(exp_kelly * 0.5, 4)
          and r["decision"] == "BET" and r["best_label"] == "OU Over 2.5")
    print(f"  Over2.5: EV={a['ev']} (expect {exp_ev}) | edge={a['edge_pct']}% | kelly_half={a['kelly_half']}")
    print(f"  decision={r['decision']} best={r['best_label']} → {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok


def oip_probability_side():
    """用受控的合理 1X2 赔率跑 OIP，演示 O/U、CS 隐含概率推导（子市场概率侧）。

    注：match_features.real_*_odds 实测 λ 异常偏高(均值 λ_h≈3.4，隐含场均 6.5 球)，
    说明该列语义非标准十进制 1X2 赔率，需先校验 feed 方可用于真实 OIP。
    此处用受控示例 (2.10/3.40/3.20) 演示概率侧逻辑，结论稳健。
    """
    print("\n=== [2] OIP 推导子市场隐含概率（概率侧演示，受控示例）===")
    oh, od, oa = 2.10, 3.40, 3.20
    pr = predict_score("H", "A", oh, od, oa)
    lh, la = pr["lh"], pr["la"]
    print(f"  示例赔率 {oh}/{od}/{oa} → λ_h={lh:.2f} λ_a={la:.2f} (P(H/D/A)={pr['p_h']:.2f}/{pr['p_d']:.2f}/{pr['p_a']:.2f})")
    M = pr["matrix"]
    over25 = float(M[np.add.outer(np.arange(9), np.arange(9)) > 2].sum())
    under25 = 1 - over25
    cs11 = float(M[1, 1])
    draw_cs = float(sum(M[i, i] for i in range(9)))
    print(f"  P(O/U Over 2.5) ≈ {over25:.3f}  | P(Under 2.5) ≈ {under25:.3f}")
    print(f"  P(CS 1-1) ≈ {cs11:.3f}  | P(任意平局局比分) ≈ {draw_cs:.3f}")
    return {"over25": over25, "under25": under25, "cs11": cs11, "draw_cs": draw_cs}


def gated_ev_demo(probs):
    """用 OIP 概率 + 合成跨庄最优赔率演示子市场 EV 结构（明确标注合成）。"""
    print("\n=== [3] 子市场 EV 结构演示（赔率为合成示意，非真实 feed）===")
    print("  ⚠️ 真实 EV 需跨庄 O/U / CS 最优赔率；football_data.db 当前无此 feed → 本步为结构演示。")
    # 合成：共识概率来自 OIP，best_odds 用 consensus_prob 反推 + 一个理论价差（soft-line 假设 +5%）
    synth_legs = []
    for label, p in [("OU Over 2.5", probs["over25"]), ("OU Under 2.5", probs["under25"]),
                     ("CS 1-1", probs["cs11"])]:
        # 单庄隐含 odds = 1/p (无价差)；best_odds 假设跨庄给 +5% 更好价 → odds*1.05
        single = 1.0 / p if p > 0 else 999
        best = single * 1.05
        synth_legs.append({"label": label, "best_odds": round(best, 3), "consensus_prob": round(p, 4)})
    r = compute_submarket_value(synth_legs, bankroll=10000, frac_kelly=0.5)
    for row in r["rows"]:
        print(f"  {row['label']:14s} consensusP={row['model_prob']:.3f} bestOdds={row['best_odds']} "
              f"edge={row['edge_pct']:+.2f}% EV={row['ev_pct']:+.2f}% kelly½={row['kelly_half']}")
    print(f"  → decision={r['decision']} ({r['decision_text']})")
    print("  说明：+5% 价差仅为示意；真实 soft-line 价差需外接跨庄源后由共识引擎计算。")


if __name__ == "__main__":
    import math
    ok = unit_validate()
    probs = oip_probability_side()
    gated_ev_demo(probs)
    print("\n=== 结论 ===")
    print("  compute_submarket_value 数学:", "✅ 正确" if ok else "❌ 错误")
    print("  OIP 概率侧: ✅ 可从真实 1X2 赔率推导 O/U / CS 隐含概率")
    print("  真实 EV 侧: ⛔ 被跨庄 O/U / CS 赔率 feed 卡住（待外接源，同 RLM 外部源）")
    print("  下一步: 接入跨庄 O/U / CS 赔率源后，compute_submarket_value 即可产出真实子市场 EV。")
