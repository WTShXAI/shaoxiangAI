"""WC2026 诚实回测 v4:
- 修复原 harness 缺陷: 原 run_v3 没传 stage/matchday, 全部默认 group/matchday=3,
  人为最大化 survival_clash → D, 虚高 draw 捕获与准确率。
- 本版: 按日期推断真实 group matchday; R16 标记为 knockout; 真实 stage/matchday 入模。
- 同时并入 4 场真实 R16 赛果(来自 worldcup26.ir API, 非编造)。
"""
import json, sys, warnings
from datetime import datetime
sys.path.insert(0, "pipeline")
import wc_engine as W
warnings.filterwarnings("ignore")

def infer_matchday(date_str: str) -> int:
    """WC2026 小组赛: MD1≈06-11~06-16, MD2≈06-17~06-22, MD3≈06-23~06-27"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    if d <= datetime(2026, 6, 16):
        return 1
    if d <= datetime(2026, 6, 22):
        return 2
    return 3

# ── 源: 72 + 4 补充 (group) ──
with open("deliverables/wc2026_full_backtest.json", "r", encoding="utf-8") as f:
    old = json.load(f)
group = old.get("details", [])
additions = [
    {"date":"2026-06-21","home":"西班牙","away":"沙特","oh":1.08,"od":8.80,"oa":18.0,"hcp":-2.5,"ou":3.5,"res":"H","sc":"4-0","src":"ocr_variants"},
    {"date":"2026-06-23","home":"葡萄牙","away":"乌兹别克","oh":1.22,"od":5.90,"oa":10.00,"hcp":-1.5,"ou":3.0,"res":"H","sc":"5-0","src":"ocr_variants"},
    {"date":"2026-06-27","home":"佛得角","away":"沙特","oh":2.47,"od":3.35,"oa":2.62,"hcp":0.0,"ou":2.5,"res":"D","sc":"0-0","src":"ocr_variants"},
    {"date":"2026-06-27","home":"民主刚果","away":"乌兹别克","oh":2.27,"od":3.25,"oa":2.97,"hcp":0.0,"ou":2.5,"res":"H","sc":"3-1","src":"ocr_variants"},
]
existing = {(d["home"], d["away"]) for d in group}
for a in additions:
    if (a["home"], a["away"]) not in existing:
        group.append(a)

# ── R16 (knockout, 真实赛果) ──
with open("data/wc2026_r16_results.json", "r", encoding="utf-8") as f:
    r16 = json.load(f)["matched"]

all_matches = []
for d in group:
    all_matches.append({
        "date": d["date"], "home": d["home"], "away": d["away"],
        "oh": d["oh"], "od": d["od"], "oa": d["oa"],
        "hcp": d.get("hcp") or 0.0, "ou": d.get("ou") or 2.5,
        "res": d["res"], "sc": d.get("sc",""), "src": d.get("src","?"),
        "stage": "group", "matchday": infer_matchday(d["date"]),
    })
for d in r16:
    all_matches.append({
        "date": d["date"], "home": d["home"], "away": d["away"],
        "oh": d["oh"], "od": d["od"], "oa": d["oa"],
        "hcp": d.get("hcp") or 0.0, "ou": d.get("ou") or 2.5,
        "res": d["res"], "sc": d.get("sc",""), "src": d.get("src","?"),
        "stage": "knockout", "matchday": 0,
    })

print("Total matches: %d (group=%d, knockout=%d)" % (
    len(all_matches),
    sum(1 for m in all_matches if m["stage"]=="group"),
    sum(1 for m in all_matches if m["stage"]=="knockout")))

W._load_main(); W._load_de()

am = [0,0]; ru = [0,0]; op = [0,0]; dp = [0,0]
by_src = {}
results = []
for m in all_matches:
    try:
        h, a = m["home"], m["away"]
        oh, od, oa = m["oh"], m["od"], m["oa"]
        res = m["res"]; src = m["src"]
        by_src.setdefault(src, [0,0])
        # argmax
        odds = [oh, od, oa]; ai = odds.index(min(odds)); ap = ["H","D","A"][ai]; ao = ap == res
        mi = W.MatchInput(home=h, away=a, odds_h=oh, odds_d=od, odds_a=oa,
                          hcp=m["hcp"], ou_line=m["ou"], stage=m["stage"], matchday=m["matchday"])
        rp = W.predict(mi, mode="rule").prediction; ro = rp == res
        op_p = W.predict(mi, mode="optimized").prediction; oo = op_p == res
    except Exception:
        h, a = m["home"], m["away"]; ap, ao, rp, ro, op_p, oo = "ERR", False, "ERR", False, "ERR", False
        src = m["src"]; by_src.setdefault(src, [0,0])

    results.append({"date":m["date"],"home":h,"away":a,"stage":m["stage"],"matchday":m["matchday"],
                    "res":res,"sc":m["sc"],"argmax":ap,"rule":rp,"opt":op_p,
                    "argmax_ok":ao,"rule_ok":ro,"opt_ok":oo,"src":src})
    for arr, ok_val in [(am,ao),(ru,ro),(op,oo)]:
        arr[0]+=1; arr[1]+=int(ok_val)
    by_src[src][0]+=1; by_src[src][1]+=int(oo)
    if op_p=="D":
        dp[1]+=1
        if res=="D": dp[0]+=1

T = len(results)
lines = []
lines.append("="*60)
lines.append("WC2026 HONEST BACKTEST v4 (%d matches, real stage/matchday)" % T)
lines.append("="*60)
for name, arr in [("argmax",am),("rule",ru),("optimized",op)]:
    pct = arr[1]/arr[0]*100 if arr[0] else 0
    lines.append("  %10s: %2d/%2d (%.1f%%)" % (name, arr[1], arr[0], pct))
if dp[1]>0:
    lines.append("  D-recall(opt): %d/%d (%.1f%%)" % (dp[0], dp[1], dp[0]/dp[1]*100))
lines.append("")
lines.append("By source:")
for s,v in sorted(by_src.items()):
    pct = v[1]/v[0]*100 if v[0] else 0
    lines.append("  %s: %d/%d (%.1f%%)" % (s, v[1], v[0], pct))
wrong = [r for r in results if not r["opt_ok"]]
lines.append("")
lines.append("opt WRONG (%d):" % len(wrong))
for r in wrong:
    lines.append("  [%s MD%d] %s %s vs %s: pred=%s actual=%s [%s]" % (
        r["stage"], r["matchday"], r["date"], r["home"], r["away"], r["opt"], r["res"], r["src"]))
out = "\n".join(lines)
print(out)
with open("deliverables/_backtest80_honest.txt","w") as f:
    f.write(out)
out_json = {
    "version":"v4-honest-80",
    "generated_at":"2026-07-06T15:30+08",
    "note":"真实 stage/matchday 入模; 并入4场R16淘汰赛(真实API赛果)",
    "total":T,
    "summary":{"argmax":{"correct":am[1],"total":am[0],"acc":round(am[1]/max(am[0],1),4)},
               "rule":{"correct":ru[1],"total":ru[0],"acc":round(ru[1]/max(ru[0],1),4)},
               "optimized":{"correct":op[1],"total":op[0],"acc":round(op[1]/max(op[0],1),4)}},
    "d_recall":{"correct":dp[0],"total":dp[1],"rate":round(dp[0]/max(dp[1],1),4)},
    "details":results,
}
with open("deliverables/wc2026_honest_backtest_80.json","w",encoding="utf-8") as f:
    json.dump(out_json, f, ensure_ascii=False, indent=2)
print("\nSaved deliverables/wc2026_honest_backtest_80.json")
