"""MD3 平局偏置诊断: 逐场打印决策特征, 对比正确平局 vs 误判D
重建 76 场输入 (同 run_v3_final_76.py): 源 details + 4 补充
"""
import json, sys, warnings
sys.path.insert(0, "pipeline")
import wc_engine as W
warnings.filterwarnings("ignore")

with open("deliverables/wc2026_full_backtest.json", "r", encoding="utf-8") as f:
    old = json.load(f)
details = old.get("details", [])
additions = [
    {"date":"2026-06-21","home":"西班牙","away":"沙特","oh":1.08,"od":8.80,"oa":18.0,"hcp":-2.5,"ou":3.5,"res":"H","sc":"4-0","src":"ocr_variants"},
    {"date":"2026-06-23","home":"葡萄牙","away":"乌兹别克","oh":1.22,"od":5.90,"oa":10.00,"hcp":-1.5,"ou":3.0,"res":"H","sc":"5-0","src":"ocr_variants"},
    {"date":"2026-06-27","home":"佛得角","away":"沙特","oh":2.47,"od":3.35,"oa":2.62,"hcp":0.0,"ou":2.5,"res":"D","sc":"0-0","src":"ocr_variants"},
    {"date":"2026-06-27","home":"民主刚果","away":"乌兹别克","oh":2.27,"od":3.25,"oa":2.97,"hcp":0.0,"ou":2.5,"res":"H","sc":"3-1","src":"ocr_variants"},
]
existing = {(d["home"], d["away"]) for d in details}
for a in additions:
    if (a["home"], a["away"]) not in existing:
        details.append(a)

W._load_main(); W._load_de()

rows = []
for d in details:
    h, a = d["home"], d["away"]
    oh, od, oa = d["oh"], d["od"], d["oa"]
    res = d["res"]
    mi = W.MatchInput(home=h, away=a, odds_h=oh, odds_d=od, odds_a=oa,
                      hcp=d.get("hcp") or 0.0, ou_line=d.get("ou") or 2.5)
    odds = W.parse_odds(oh, od, oa)
    form = W.analyze_form(h, a)
    ctx = W.analyze_context(mi.stage, mi.matchday, mi.r3_rotation, odds, form)
    opt = W.predict(mi, mode="optimized").prediction
    rows.append({
        "home": h, "away": a, "res": res,
        "zone": odds["zone"], "max_imp": round(max(odds["imp_h"], odds["imp_d"], odds["imp_a"]), 3),
        "gap": form["strength_gap"], "weak_atk": form["weak_attack"],
        "surv": ctx["survival_clash"], "wbs": ctx["weak_both_survival"],
        "opt": opt, "opt_ok": (opt == res),
    })

print("=== even + survival_clash + opt==D 的场次 (survival_clash->D 分支) ===")
surv_d = [r for r in rows if r["surv"] and r["gap"] == "even" and r["opt"] == "D"]
correct = [r for r in surv_d if r["res"] == "D"]
wrong = [r for r in surv_d if r["res"] != "D"]
hdr = f"{'home':<8}{'away':<8}{'res':<4}{'zone':<10}{'max_imp':<8}{'gap':<14}{'weak':<6}{'opt':<4}{'ok':<5}"
print(hdr)
print(f"--- 正确平局 ({len(correct)}) ---")
for r in correct:
    print(f"{r['home']:<8}{r['away']:<8}{r['res']:<4}{r['zone']:<10}{r['max_imp']:<8}{r['gap']:<14}{str(r['weak_atk']):<6}{r['opt']:<4}{r['opt_ok']:<5}")
print(f"--- 误判(应为非D) ({len(wrong)}) ---")
for r in wrong:
    print(f"{r['home']:<8}{r['away']:<8}{r['res']:<4}{r['zone']:<10}{r['max_imp']:<8}{r['gap']:<14}{str(r['weak_atk']):<6}{r['opt']:<4}{r['opt_ok']:<5}")

ok = sum(1 for r in rows if r["opt_ok"])
print(f"\nopt 正确: {ok}/{len(rows)}")
print(f"survival_clash->D 捕获: 正确{len(correct)} / 误判{len(wrong)}")

# 检查: 那些 correct 的, 若改为 '仅 weak_both_survival 才 D' 会丢几个?
lost_if_wbs_only = [r for r in correct if not r["wbs"]]
print(f"若改为仅 weak_both_survival 才D, 会丢失正确平局: {len(lost_if_wbs_only)}")
for r in lost_if_wbs_only:
    print(f"  丢: {r['home']} vs {r['away']} zone={r['zone']} weak_atk={r['weak_atk']}")
