"""
哨响AI — 定价模板反演结果分析器
快速从引擎产出生成结构化洞察。
"""
import os, json, sys
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IN_DIR = os.path.join(ROOT, "data", "pricing_template")

def load():
    with open(os.path.join(IN_DIR, "pricing_template_report.json"), encoding="utf-8") as f:
        report = json.load(f)
    df = pd.read_csv(os.path.join(IN_DIR, "triplet_table.csv"))
    return report, df

def analyze(report, df):
    print("=" * 70)
    print("哨响AI 定价模板反演 — 核心洞察")
    print("=" * 70)

    # 1. DC 模型
    dc = report["dc_model"]
    print(f"\n1. Dixon-Coles 公平概率模型")
    print(f"   ρ = {dc['rho']:.4f}  (接近0 → 低分依赖弱，独立泊松近似有效)")
    print(f"   主场优势 = {dc['home_adv']:.3f} 球")
    print(f"   截距 = {dc['intercept']:.3f} (log-λ 基线)")
    print(f"   训练: {dc['n_train']} 场, {dc['n_teams']} 队")

    # 2. Margin 模板
    mt = report["margin_template"]
    print(f"\n2. 庄家 Margin 模板 (GQ)")
    print(f"   log(market_p) = {mt['a']:.4f} * log(fair_p) + {mt['b']:.4f}")
    print(f"   R² = {mt['r2']:.4f} → {'强规律，模板清晰' if mt['r2'] > 0.7 else '噪声大，模板不稳定'}")
    a = mt['a']
    print(f"   a={a:.4f}: {'favorite bias (热门低margin)' if a < 1.0 else 'longshot bias (冷门低margin)' if a > 1.0 else '均匀margin'}")

    # 3. 偏差分析
    print(f"\n3. 偏差分布 (DC fair - Market implied)")
    for col, label in [("op_dev_h", "主胜"), ("op_dev_d", "平局"), ("op_dev_a", "客胜")]:
        if col in df.columns:
            valid = df[col].dropna()
            print(f"   {label}: mean={valid.mean():.4f}, std={valid.std():.4f}, "
                  f"|dev|>5%={ (abs(valid)>0.05).sum() }/{len(valid)}")

    # 4. 偏差分层 vs 实际命中率
    print(f"\n4. 偏差分层 → 实际命中率关系 (核心指标)")
    analysis = report.get("analysis", {})
    for outcome in ["H", "D", "A"]:
        layers = analysis.get(f"dev_stratification_{outcome}", [])
        if layers:
            print(f"\n   {outcome} (主胜/平局/客胜):")
            for l in layers:
                if l["n"] > 0:
                    print(f"     {l['bin']:>12s}: n={l['n']}, "
                          f"实际命中={l.get('actual_win_rate',0):.3f}, "
                          f"DC概率={l.get('dc_prob_mean',0):.3f}, "
                          f"差={l.get('dc_vs_actual',0):.3f}")

    # 5. 跨市场信号
    print(f"\n5. 跨市场一致性")
    print(f"   总信号: {analysis.get('total_cross_signals', 0)}")
    print(f"   有信号场次: {analysis.get('matches_with_signals', 0)}")

    # 6. 大偏差场次的 ROI 预估
    print(f"\n6. 大偏差场次 (>|5%|) 统计")
    for outcome in ["H", "D", "A"]:
        n = analysis.get(f"large_dev_{outcome}_n", 0)
        actual = analysis.get(f"large_dev_{outcome}_actual", 0)
        dc_p = analysis.get(f"large_dev_{outcome}_dc", 0)
        print(f"   {outcome}: n={n}, 实际命中={actual:.3f}, DC概率={dc_p:.3f}, "
              f"{'DC校准良好' if abs(actual-dc_p) < 0.05 else 'DC高估' if dc_p > actual else 'DC低估'}")

    # 7. Beat Closing
    print(f"\n7. Beat Closing % (模拟：在收盘前用DC概率下注)")
    for outcome in ["H", "D", "A"]:
        pct = analysis.get(f"beat_closing_{outcome}_pct", 0)
        print(f"   {outcome}: {pct:.1%}")

    # 8. 联赛分层
    league_rmse = report.get("analysis", {}).get("league_rmse_top10", {})
    # (loaded later)

    print("\n" + "=" * 70)
    print("战术建议")
    print("=" * 70)

    # 基于数据分析的战术建议
    print("""
1. DC 模型作为公平概率基线: ρ≈{rho:.4f}, 接近独立泊松.
   → 低分比赛 (0-0/1-1) 不需要单独 DC 修正, cs_calibration 的 +40% 因子足够.

2. Margin 模板 R²={r2:.3f}: 
   → {r2_msg}

3. 偏差信号: 当 DC_p - market_implied > 0.05 时, 
   → 正偏差组实际命中率 vs DC概率 的差值是关键.
   → 如果正偏差组实际 > DC → 模型保守, edge真实存在.
   → 如果正偏差组实际 ≈ DC → 模型校准良好.
   → 如果正偏差组实际 < DC → 模型高估, 需降权.

4. 跨市场信号: {sig_count} 个, 覆盖 {sig_match} 场.
   → 如果信号多的比赛实际命中率偏离随机, 说明套利信号有预测力.

5. Beat Closing: 如果 Beat Closing % > 50% 且显著, 
   → 说明 DC 模型信息优于市场收盘, 正 CLV 可盈利.
""".format(
        rho=dc['rho'],
        r2=mt['r2'],
        r2_msg="模板规律强, 偏离模板就是信号" if mt['r2'] > 0.7 else "模板噪声大, 需要多庄对比提高信噪比",
        sig_count=analysis.get('total_cross_signals', 0),
        sig_match=analysis.get('matches_with_signals', 0),
    ))


if __name__ == "__main__":
    report, df = load()
    analyze(report, df)
