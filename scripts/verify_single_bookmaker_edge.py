"""
实证检验文章核心前提: "用模型概率对比单一庄家隐含概率 -> 系统性 +EV"。

方法 (走 SSoT 干净数据):
1. 加载 clean_outcomes (剔虚拟/截断, 用 build_opening_lines 重建主盘线)
2. 取 1X2 原始赔率 -> 去水还原隐含概率 (文章公式: 1/odds, 归一化)
3. 测校准度: 隐含概率分箱 vs 实际胜率
4. 测 "总是买最高隐含概率方" 的 ROI (含 margin)
5. 测隐含概率对赛果的 AUC (区分力)
结论: 若校准良好 + 买 favorites ROI 为负 -> 单庄偏差不可系统性获利, 真 edge 仅来自跨庄软线价差(项目保留通道)
"""
import sqlite3, sys, math
sys.path.insert(0, "D:/Architecture")
from pipeline.clean_outcomes import load_clean_outcomes
from pipeline.opening_line import build_opening_lines

DB = "data/events.db"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()


def load_1x2_clean():
    """clean_outcomes (DataFrame, 已剔虚拟/截断) 取 1X2 原始赔率 + 赛果."""
    df = load_clean_outcomes()  # pandas DataFrame, 列含 op_1x2_h/d/a, score_*
    need = ["mid", "op_1x2_h", "op_1x2_d", "op_1x2_a", "score_home", "score_away"]
    df = df[df["score_home"].notna() & df["score_away"].notna()]
    df = df[df["op_1x2_h"].notna() & df["op_1x2_d"].notna() & df["op_1x2_a"].notna()]
    out = []
    for _, r in df.iterrows():
        h, d, a = float(r["op_1x2_h"]), float(r["op_1x2_d"]), float(r["op_1x2_a"])
        sh, sa = int(r["score_home"]), int(r["score_away"])
        if h <= 1.01 or d <= 1.01 or a <= 1.01:
            continue
        out.append({"h": h, "d": d, "a": a, "sh": sh, "sa": sa})
    return out


def devig(h, d, a):
    ih, id_, ia = 1.0 / h, 1.0 / d, 1.0 / a
    s = ih + id_ + ia
    return ih / s, id_ / s, ia / s, s  # 归一化概率 + overround


def main():
    rows = load_1x2_clean()
    print(f"[数据] 干净 1X2 样本: {len(rows)} 场")

    # 去水
    probs = []
    for r in rows:
        ph, pd, pa, over = devig(r["h"], r["d"], r["a"])
        res = 0 if r["sh"] > r["sa"] else (1 if r["sh"] == r["sa"] else 2)
        probs.append((ph, pd, pa, over, res, r))

    # 1) 校准度: home 隐含概率分 10 箱
    print("\n=== 校准度 (home 隐含概率 vs 实际主胜率) ===")
    bins = [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 1.01]
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i + 1]
        grp = [p for p in probs if lo <= p[0] < hi]
        if len(grp) < 15:
            continue
        act = sum(1 for p in grp if p[4] == 0) / len(grp)
        center = (lo + hi) / 2
        print(f"  隐含 {lo:.0%}-{hi:.0%}: n={len(grp):4}  实际主胜 {act:.1%}  (偏差 {act-center:+.1%})")

    # 2) 买 favorite ROI (含 margin)
    print("\n=== 总是买最高隐含概率方 (含 margin) ROI ===")
    stake = 1.0
    pnl = 0.0
    n_bet = 0
    for p in probs:
        ph, pd, pa, over, res, r = p
        imp = [ph, pd, pa]
        side = imp.index(max(imp))  # 0/1/2
        odds = [r["h"], r["d"], r["a"]][side]
        n_bet += 1
        if side == res:
            pnl += (odds - 1) * stake
        else:
            pnl -= stake
    roi = pnl / (n_bet * stake)
    print(f"  下注 {n_bet} 场, 总ROI = {roi:+.2%}  (盲投≈ -overround)")

    # 3) 区分力 AUC (home prob 预测主胜)
    print("\n=== 隐含概率区分力 (AUC) ===")
    y = [1 if p[4] == 0 else 0 for p in probs]
    s = [p[0] for p in probs]
    # 手动 AUC (Mann-Whitney)
    pos = [s[i] for i in range(len(s)) if y[i] == 1]
    neg = [s[i] for i in range(len(s)) if y[i] == 0]
    if pos and neg:
        cnt = sum(1 for a in pos for b in neg if a > b)
        cnt += 0.5 * sum(1 for a in pos for b in neg if a == b)
        auc = cnt / (len(pos) * len(neg))
        print(f"  home 概率预测主胜 AUC = {auc:.4f}  (0.5=随机, 1.0=完美)")

    # 4) overround 分布
    import statistics
    overs = [p[3] for p in probs]
    print(f"\n=== overround (抽水) ===")
    print(f"  均值 {statistics.mean(overs):.4f}  中位 {statistics.median(overs):.4f}  "
          f"-> 平均抽水 {(statistics.mean(overs)-1)*100:.2f}%")

    print("\n=== 结论 ===")
    print("  若校准良好(实际≈隐含) 且 买favorite ROI≈-抽水 -> 单庄隐含概率已高度校准,")
    print("  margin 吃掉所有 '模型vs单庄' 偏差. 模型vs单庄自共识系统性 +EV 不成立, 真 edge 仅来自")
    print("  跨庄/跨市场软线价差 (项目保留通道).")


if __name__ == "__main__":
    main()
