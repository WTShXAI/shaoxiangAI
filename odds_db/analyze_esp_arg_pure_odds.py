# -*- coding: utf-8 -*-
"""西班牙vs阿根廷 纯赔率结构分析 (不含漂移叙事/基本面, 纯数学盘口反推)
读最新 drift 快照的 1X2 + 全场波胆, 用生产级 deoverround + Poisson 反推 λ,
对比竞彩实际定价找出: 庄家压低(价值/软线) vs 抬高(不看好) 的位置。
"""
import json, math, importlib.util

spec = importlib.util.spec_from_file_location("score_model", "pipeline/score_model.py")
sm = importlib.util.module_from_spec(spec); spec.loader.exec_module(sm)
deoverround = sm.deoverround
solve_oip = sm.solve_oip

data = json.load(open("odds_db/cs_esp_arg_20260719_190841_drift.json", encoding="utf-8"))
oh, od, oa = data["1x2"]["home"], data["1x2"]["draw"], data["1x2"]["away"]
ph, pd, pa = deoverround(oh, od, oa)
lh, la = solve_oip(ph, pd, pa)
print(f"=== 1X2 去水 & Poisson λ 反推 ===")
print(f"去水平局: H={ph:.3f} D={pd:.3f} A={pa:.3f}")
print(f"反推 λ: λh={lh:.3f}(西班牙)  λa={la:.3f}(阿根廷)  λ总={lh+la:.3f}")

cs = data["cs_odds_full"]

def pmf(k, l):
    if l <= 0 or k < 0: return 0.0
    return math.exp(-l) * (l ** k) / math.factorial(k)

scores = [(h, a) for h in range(0, 6) for a in range(0, 6)]
theory_prob = {f"{h}:{a}": pmf(h, lh) * pmf(a, la) for h, a in scores}

# 波胆市场去水率
inv_sum = sum(1.0 / cs[f"{h}:{a}"] for h, a in scores if f"{h}:{a}" in cs)
margin = 1.0 / inv_sum
print(f"\n=== 波胆市场 ===")
print(f"总含抽水隐含概率 = {inv_sum:.3f}  → 市场抽水率 ≈ {1-margin:.1%}")

rows = []
for h, a in scores:
    key = f"{h}:{a}"
    if key not in cs: continue
    odds = cs[key]
    impl_dew = (1.0 / odds) * margin
    tp = theory_prob[key]
    vr = impl_dew / tp if tp > 0 else 0
    rows.append((key, odds, impl_dew, tp, vr))

# 按 value_ratio 降序 (庄家压低/便宜 在前)
rows.sort(key=lambda r: -r[4])
print(f"\n{'比分':5} {'赔率':>7} {'实际去水P':>9} {'理论P':>8} {'价值比':>7}  判定")
for key, odds, impl, tp, vr in rows:
    tag = "便宜(庄家压低/软线)" if vr > 1.10 else ("贵(庄家抬高/不看好)" if vr < 0.90 else "合理")
    print(f"{key:5} {odds:7.2f} {impl:9.4f} {tp:8.4f} {vr:7.2f}  {tag}")

# 三目标比分专项
print(f"\n=== 用户目标三比分 纯赔率定位 ===")
for key in ["1:2", "2:3", "1:3"]:
    if key in cs:
        odds = cs[key]
        impl = (1.0 / odds) * margin
        tp = theory_prob[key]
        vr = impl / tp
        print(f"{key}: 实际去水P={impl:.4f} 理论P={tp:.4f} 价值比={vr:.2f} "
              f"({'便宜' if vr>1.1 else '贵' if vr<0.9 else '合理'})")

# AH / OU 静态读盘
print(f"\n=== AH / OU 静态结构 ===")
print(f"AH -0.5/+0.5: 主1.93 / 客1.99 → 主让平半, 主略优但水位接近(差0.06)")
print(f"OU 2/2.5: 大2.00 / 小1.90 → 小球低水(庄家倾向小球)")
print(f"1X2: 主2.23最低 < 平3.15 < 客3.45 → 庄家略看好西班牙不败")

# 保存
out = {
    "lambda_h": lh, "lambda_a": la,
    "market_margin_cs": margin,
    "value_table": [{"score": r[0], "odds": r[1], "implied_dew": r[2],
                     "theory": r[3], "value_ratio": r[4]} for r in rows],
}
json.dump(out, open("odds_db/analyze_esp_arg_pure_odds.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2, default=str)
print("\nSAVED odds_db/analyze_esp_arg_pure_odds.json")
