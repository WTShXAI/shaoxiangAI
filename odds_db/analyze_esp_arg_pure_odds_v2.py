# -*- coding: utf-8 -*-
"""西阿纯赔率 v2: 用生产级λ(西班牙高) + 竞彩初盘全量波胆(完整客胜列) 重算价值表
纠正 v1 之前倒挂分析手算λ方向错误。
"""
import json, math, importlib.util

spec = importlib.util.spec_from_file_location("score_model", "pipeline/score_model.py")
sm = importlib.util.module_from_spec(spec); spec.loader.exec_module(sm)
deoverround = sm.deoverround
solve_oip = sm.solve_oip

# λ 从 drift 快照 1X2 反推 (生产级, 西班牙热门)
drift = json.load(open("odds_db/cs_esp_arg_20260719_190841_drift.json", encoding="utf-8"))
oh, od, oa = drift["1x2"]["home"], drift["1x2"]["draw"], drift["1x2"]["away"]
ph, pd, pa = deoverround(oh, od, oa)
lh, la = solve_oip(ph, pd, pa)
print(f"λh={lh:.3f}(西班牙) λa={la:.3f}(阿根廷)  [主胜{oh}最低→西班牙热门, 正确]")

# 竞彩初盘全量波胆 (完整客胜列, 含 1:3/2:3/1:4)
init = json.load(open("odds_db/cs_manual_20260719_182150.json", encoding="utf-8"))
cs = init["cs_odds"]

def pmf(k, l):
    if l <= 0 or k < 0: return 0.0
    return math.exp(-l) * (l ** k) / math.factorial(k)

scores = [(h, a) for h in range(0, 6) for a in range(0, 6)]
theory_prob = {f"{h}:{a}": pmf(h, lh) * pmf(a, la) for h, a in scores}

inv_sum = sum(1.0 / cs[f"{h}:{a}"] for h, a in scores if f"{h}:{a}" in cs)
margin = 1.0 / inv_sum
print(f"波胆市场抽水率 ≈ {1-margin:.1%}\n")

rows = []
for h, a in scores:
    key = f"{h}:{a}"
    if key not in cs: continue
    odds = cs[key]
    impl = (1.0 / odds) * margin
    tp = theory_prob[key]
    vr = impl / tp if tp > 0 else 0
    rows.append((key, odds, impl, tp, vr))

rows.sort(key=lambda r: -r[4])
print(f"{'比分':5} {'赔率':>7} {'实际去水P':>9} {'理论P':>8} {'价值比':>7}  判定")
for key, odds, impl, tp, vr in rows:
    tag = "便宜(庄家压低)" if vr > 1.10 else ("贵(庄家抬高)" if vr < 0.90 else "合理")
    print(f"{key:5} {odds:7.2f} {impl:9.4f} {tp:8.4f} {vr:7.2f}  {tag}")

print(f"\n=== 目标三比分 ===")
for key in ["1:2", "2:3", "1:3"]:
    if key in cs:
        odds = cs[key]; impl = (1.0/odds)*margin; tp = theory_prob[key]; vr = impl/tp
        print(f"{key} @ {odds}: 价值比={vr:.2f} {'便宜' if vr>1.1 else '贵' if vr<0.9 else '合理'} "
              f"(实际去水P={impl:.4f} vs 理论={tp:.4f})")
