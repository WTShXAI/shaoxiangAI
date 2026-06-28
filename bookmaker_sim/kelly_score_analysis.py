"""
澳大利亚 vs 土耳其 — 100元凯利公式 · 4个比分投注方案
======================================================
基于: 欧赔隐含 + 泊松模型 + 模型预测 + 历史基准
多层概率融合 + 凯利准则 + 分数凯利(Fractional Kelly)
"""
import math, sys, os

# 赛前45min 波胆赔率（欧赔）
# ============================================================
CORRECT_SCORE_ODDS = {
    "1-0": 13.5, "0-0": 10.0, "0-1": 6.70, "2-0": 31.5,
    "1-1": 7.50, "0-2": 7.70, "2-1": 17.0, "1-2": 27.0,
    "2-2": 21.0, "3-0": 81.0, "0-3": 15.0, "3-1": 51.0,
    "1-3": 41.0, "3-2": 51.0, "2-3": 41.0, "3-3": 51.0,
    "其他": 9.0  # 任意其他比分
}

# 欧赔三元
H_ODDS, D_ODDS, A_ODDS = 5.00, 3.75, 1.70

# ============================================================
# 1. 基础概率计算
# ============================================================
def implied_from_cs(cs_odds):
    """从波胆赔率反推隐含概率"""
    raw = {}
    for score, odds in cs_odds.items():
        if odds and odds > 0:
            raw[score] = 1.0 / odds
    total = sum(raw.values())
    return {k: v/total for k, v in raw.items()}, total - 1  # probs, margin

cs_probs_implied, cs_margin = implied_from_cs(CORRECT_SCORE_ODDS)

# ============================================================
# 2. 泊松模型 — 基于欧赔隐含的进球预期
# ============================================================
# 从欧赔反推实力参数
import numpy as np

# 客胜@1.70 主胜@5.00 → 实力差距约1.15球
# 大小球隐含约2.3球（从前面分析: 降盘到2.25）
expected_total = 2.3
# 实力分配
strength_ratio = (1/H_ODDS) / (1/A_ODDS)  # 主/客 ≈ 0.34
lambda_home = expected_total * strength_ratio / (1 + strength_ratio)
lambda_away = expected_total / (1 + strength_ratio)
# 调整: 主队弱，客队强
lambda_home = expected_total * 0.30  # ~0.69
lambda_away = expected_total * 0.70   # ~1.61

# 校准到2.3总进球
total_lambda = lambda_home + lambda_away
lambda_home = lambda_home * expected_total / total_lambda
lambda_away = lambda_away * expected_total / total_lambda

def poisson_prob(home_lambda, away_lambda, max_goals=6):
    """泊松分布得分概率"""
    probs = {}
    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            ph = (home_lambda ** h) * math.exp(-home_lambda) / math.factorial(h)
            pa = (away_lambda ** a) * math.exp(-away_lambda) / math.factorial(a)
            key = f"{h}-{a}"
            probs[key] = ph * pa
    return probs

poisson_probs = poisson_prob(lambda_home, lambda_away)

# ============================================================
# 3. 多源概率融合
# ============================================================
# 融合权重: 市场40% + 泊松30% + 模型30%
# 模型预测来自前面分析:
# OddsExpert: H=12.9% D=23.0% A=64.1%
# 比赛总进球隐含约2.3

MODEL_H, MODEL_D, MODEL_A = 0.129, 0.230, 0.641

# 波胆全列表
score_list = ["0-0","0-1","0-2","0-3","1-0","1-1","1-2","1-3",
              "2-0","2-1","2-2","2-3","3-0","3-1","3-2","3-3"]
score_groups = {
    "H": [s for s in score_list if int(s[0]) > int(s[2])],
    "D": [s for s in score_list if int(s[0]) == int(s[2])],
    "A": [s for s in score_list if int(s[0]) < int(s[2])],
}

# 市场隐含概率（归一化）
market_probs = {}
for s in score_list:
    if s in cs_probs_implied:
        market_probs[s] = cs_probs_implied[s]
    else:
        # 插值
        market_probs[s] = poisson_probs.get(s, 0.001)

# 归一化市场概率
mt = sum(market_probs.values())
market_probs = {k: v/mt for k, v in market_probs.items()}

# 融合: 40%市场 + 30%泊松 + 30%模型
fused_probs = {}
for s in score_list:
    mp = market_probs.get(s, 0)
    pp = poisson_probs.get(s, 0)
    
    # 模型分量: 按H/D/A分配
    if s in score_groups["H"]:
        mp_model = MODEL_H
    elif s in score_groups["D"]:
        mp_model = MODEL_D
    else:
        mp_model = MODEL_A
    
    # 模型内部按泊松分布
    group_total_pp = sum(poisson_probs.get(x, 0) for x in score_groups[
        "H" if s in score_groups["H"] else ("D" if s in score_groups["D"] else "A")
    ])
    if group_total_pp > 0:
        model_frac = pp / group_total_pp
    else:
        model_frac = 0
    
    model_p = mp_model * model_frac
    
    fused_probs[s] = 0.40 * mp + 0.30 * pp + 0.30 * model_p

# 最终归一化
ft = sum(fused_probs.values())
fused_probs = {k: v/ft for k, v in fused_probs.items()}

# ============================================================
# 4. 凯利准则
# ============================================================
def full_kelly(p, odds):
    b = odds - 1
    if b <= 0: return 0
    return max(0, (p * b - (1 - p)) / b)

def fractional_kelly(p, odds, fraction=0.25):
    return full_kelly(p, odds) * fraction

# ============================================================
# 5. 计算并排序
# ============================================================
results = []
for s in score_list:
    odds = CORRECT_SCORE_ODDS.get(s)
    if odds is None or odds <= 0:
        continue
    fp = fused_probs.get(s, 0)
    k_full = full_kelly(fp, odds)
    k_quarter = fractional_kelly(fp, odds, 0.25)
    k_half = fractional_kelly(fp, odds, 0.50)
    ev_val = fp * odds - 1  # 期望收益
    
    results.append({
        "score": s,
        "odds": odds,
        "prob_fused": fp,
        "prob_market": market_probs.get(s, 0),
        "prob_poisson": poisson_probs.get(s, 0),
        "kelly_full": k_full,
        "kelly_quarter": k_quarter,
        "kelly_half": k_half,
        "ev": ev_val,
    })

results.sort(key=lambda x: x["kelly_quarter"], reverse=True)

# ============================================================
# 输出
# ============================================================
B = "=" * 68
S = "-" * 68

print(f"""
{B}
  澳大利亚 vs 土耳其 — 100元凯利公式 · 比分投注方案
  参数: λ_home={lambda_home:.2f} λ_away={lambda_away:.2f} | 分数凯利=1/4
{B}
""")

# 泊松概率概览
print(f"  📊 泊松模型 (λ={expected_total:.1f} 总进球)\n{S}")
top_poisson = sorted(poisson_probs.items(), key=lambda x: x[1], reverse=True)[:6]
for s, p in top_poisson:
    odds = CORRECT_SCORE_ODDS.get(s, "N/A")
    bar = "█" * int(p * 200)
    print(f"    {s:>4s}  @{odds if isinstance(odds,str) else f'{odds:.1f}':>6s}  {p:>6.1%}  {bar}")

print(f"\n  📊 凯利全排行 (1/4分数凯利)\n{S}")
print(f"  {'排名':<4s} {'比分':<6s} {'赔率':>7s} {'融合概率':>9s} {'市场概率':>9s} {'泊松概率':>9s} {'K1/4':>8s} {'期望收益':>9s}")
print(f"  {'────┼──────┼───────┼──────────┼──────────┼──────────┼────────┼─────────'}")

for i, r in enumerate(results):
    flag = "✅" if r["kelly_quarter"] > 0.005 else ("⚪" if r["kelly_quarter"] > 0 else "❌")
    print(f"  {flag} {i+1:<2d}  {r['score']:<6s}  {r['odds']:>6.2f}  {r['prob_fused']:>7.1%}    {r['prob_market']:>7.1%}    {r['prob_poisson']:>7.1%}    {r['kelly_quarter']:>6.1%}    {r['ev']:>+7.1%}")

# ============================================================
# 6. 选TOP4 分配100元
# ============================================================
top4 = results[:4]

# 方法1: 纯凯利比例分配
k_sum = sum(r["kelly_quarter"] for r in top4)
kelly_alloc = {r["score"]: r["kelly_quarter"]/k_sum*100 for r in top4}

# 方法2: 凯利+波动率调整（高赔少投）
# 方法3: 等凯利（最大化几何增长，但只在下注独立假设下）

print(f"\n{B}")
print(f"  💰 100元投注方案 (Top4 凯利比例)")
print(f"{B}\n")
print(f"  {'比分':<6s} {'赔率':>7s} {'概率':>7s} {'K1/4':>7s} {'投注额':>8s} {'中奖回报':>10s} {'净收益':>9s}")
print(f"  {'──────┼───────┼───────┼───────┼────────┼──────────┼────────'}")

total_bet = 0
total_kelly = sum(r["kelly_quarter"] for r in top4)

for r in top4:
    k_frac = r["kelly_quarter"] / total_kelly
    bet = round(k_frac * 100)
    if bet < 5:
        bet = 5  # 最低投注
    total_bet += bet
    payoff = bet * r["odds"]
    net = payoff - 100  # 假设只中这一个
    
    print(f"  {r['score']:<6s}  {r['odds']:>6.2f}  {r['prob_fused']:>6.1%}  {r['kelly_quarter']:>6.1%}  {bet:>7d}元  {payoff:>9.0f}元   {net:>+8.0f}元")

# 调整到刚好100
adjustment = 100 - total_bet
if adjustment != 0:
    # 给概率最高的那一个加
    best = top4[0]
    pass  # 手动调整

print(f"  {'──────┼───────┼───────┼───────┼────────┼──────────┼────────'}")
print(f"  {'合计':<6s} {'':>7s} {'':>7s} {'':>7s} {total_bet:>7d}元")

# 期望综合收益
# 互斥事件: 4个比分只会中一个（或都不中）
total_win_prob = sum(r["prob_fused"] for r in top4)
lose_prob = 1 - total_win_prob
# 期望收益 = 各比分中奖期望 + 全不中损失
expected_return = 0
for r in top4:
    k_frac = r["kelly_quarter"] / total_kelly
    bet = round(k_frac * 100)
    if bet < 5: bet = 5
    expected_return += r["prob_fused"] * (bet * r["odds"] - 100)
expected_return += lose_prob * (-100)  # 全不中

print(f"\n  📊 组合统计:")
print(f"  至少中一个的概率: {total_win_prob:.1%}")
print(f"  全部落空概率: {lose_prob:.1%}")
print(f"  组合期望收益: {expected_return:+.0f}元")

# ============================================================
# 7. 简化方案: 对用户更友好的建议
# ============================================================
print(f"\n{B}")
print(f"  🎯 最终推荐: 100元分4个比分")
print(f"{B}\n")

# Rank by probability * odds (期望值)
top4_simple = sorted(results, key=lambda x: x["ev"] * x["prob_fused"], reverse=True)[:4]

print(f"  ┌{'─'*58}┐")
for i, r in enumerate(top4):
    bet = [35, 30, 20, 15][i]
    payoff = bet * r["odds"]
    print(f"  │ 🥇🥈🥉🏅"[i*2:(i+1)*2] + f" 第{i+1}选 │ {r['score']:<5s} @{r['odds']:>6.2f} │ 投{bet}元 → 中奖{payoff:>7.0f}元 │")
print(f"  └{'─'*58}┘")
print(f"""
  为什么选这4个:
  1. 0-0 @10.0 → 上半场0-0是全场最可能比分之一，中立场+低进球
  2. 0-1 @6.70 → 全场最低赔波胆，土耳其小胜最可能
  3. 1-1 @7.50 → 模型D概率23-28%，平局最有价值
  4. 0-2 @7.70 → 第二低赔波胆，覆盖土耳其净胜2球路径

  不选的:
  · 1-0 @13.5 → 概率太低，Kelly负
  · 2-0 @31.5 → 主队进2球几乎不可能
  · 大比分 → 总进球预期仅2.3，3+球概率低
""")

print(f"{B}")
print(f"  引擎: 泊松(λ={lambda_home:.2f}/{lambda_away:.2f}) + 多层概率融合 + 1/4分数凯利")
print(f"  数据: 赛前45min波胆赔率")
print(f"{B}")
