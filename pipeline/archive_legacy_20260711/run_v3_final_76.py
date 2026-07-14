"""WC2026 v3-final backtest: 76 matches (72 original + 4 variant-fix)"""
import json, sys, warnings
sys.path.insert(0, "pipeline")
import wc_engine as W
warnings.filterwarnings("ignore")

# Load original 72
with open("deliverables/wc2026_full_backtest.json", "r", encoding="utf-8") as f:
    old = json.load(f)
details = old.get("details", [])

# Add 4 variant-fix matches (队名变体: 沙特阿拉伯->沙特, 乌兹别克斯坦->乌兹别克)
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

print("Total matches: %d" % len(details))

# Load models
W._load_main()
W._load_de()

# Predict all
results = []
am = [0, 0]  # argmax [total, ok]
ru = [0, 0]  # rule
op = [0, 0]  # optimized
by_src = {}
dp = [0, 0]  # D-pred [correct, total]

for d in details:
    try:
        h, a = d["home"], d["away"]
        oh, od, oa = d["oh"], d["od"], d["oa"]
        res = d["res"]
        src = d.get("src", "?")
        by_src.setdefault(src, [0, 0])

        # argmax
        odds = [oh, od, oa]
        ai = odds.index(min(odds))
        ap = ["H", "D", "A"][ai]
        ao = ap == res

        # rule + optimized
        mi = W.MatchInput(home=h, away=a, odds_h=oh, odds_d=od, odds_a=oa,
                          hcp=d.get("hcp") or 0.0, ou_line=d.get("ou") or 2.5)
        rp = W.predict(mi, mode="rule").prediction
        ro = rp == res
        op_p = W.predict(mi, mode="optimized").prediction
        oo = op_p == res
    except Exception:
        # 单条记录异常(缺字段/赔率为None等) → 记 ERR, 不中断整轮回测
        h = d.get("home", "?"); a = d.get("away", "?")
        ap, ao, rp, ro, op_p, oo = "ERR", False, "ERR", False, "ERR", False
        src = d.get("src", "?")
        by_src.setdefault(src, [0, 0])

    results.append({
        "date": d["date"], "home": h, "away": a,
        "res": res, "sc": d.get("sc", ""),
        "argmax": ap, "argmax_ok": ao,
        "rule": rp, "rule_ok": ro,
        "opt": op_p, "opt_ok": oo, "src": src
    })

    for arr, ok_val in [(am, ao), (ru, ro), (op, oo)]:
        arr[0] += 1
        arr[1] += int(ok_val)
    by_src[src][0] += 1
    if oo:
        by_src[src][1] += 1
    if op_p == "D":
        dp[1] += 1
        if res == "D":
            dp[0] += 1

# Output
T = len(results)
lines = []
lines.append("=" * 55)
lines.append("WC2026 v3-final BACKTEST (%d matches)" % T)
lines.append("=" * 55)
for name, arr in [("argmax", am), ("rule", ru), ("optimized", op)]:
    pct = arr[1] / arr[0] * 100 if arr[0] else 0
    lines.append("  %10s: %2d/%2d (%.1f%%)" % (name, arr[1], arr[0], pct))
if dp[1] > 0:
    lines.append("  D-recall(opt): %d/%d (%.1f%%)" % (dp[0], dp[1], dp[0]/dp[1]*100))
lines.append("")
lines.append("By source:")
for s, v in sorted(by_src.items()):
    pct = v[1] / v[0] * 100 if v[0] else 0
    lines.append("  %s: %d/%d (%.1f%%)" % (s, v[1], v[0], pct))

wrong = [r for r in results if not r["opt_ok"]]
lines.append("")
lines.append("opt WRONG (%d):" % len(wrong))
for r in wrong:
    lines.append("  %s %s vs %s: pred=%s actual=%s [%s]" % (
        r["date"], r["home"], r["away"], r["opt"], r["res"], r["src"]))

output_text = "\n".join(lines)
print(output_text)

with open("deliverables/_backtest76_output.txt", "w") as f:
    f.write(output_text)

output_json = {
    "version": "v3-final-76",
    "generated_at": "2026-07-06T20:45+08",
    "total": T,
    "summary": {
        "argmax": {"correct": am[1], "total": am[0], "acc": round(am[1]/max(am[0],1), 4)},
        "rule": {"correct": ru[1], "total": ru[0], "acc": round(ru[1]/max(ru[0],1), 4)},
        "optimized": {"correct": op[1], "total": op[0], "acc": round(op[1]/max(op[0],1), 4)},
    },
    "d_recall": {"correct": dp[0], "total": dp[1], "rate": round(dp[0]/max(dp[1],1), 4)},
    "details": results,
}
with open("deliverables/wc2026_v3_final_76.json", "w", encoding="utf-8") as f:
    json.dump(output_json, f, ensure_ascii=False, indent=2)

print("\nSaved deliverables/wc2026_v3_final_76.json")
