import json, re
from datetime import datetime, timedelta

path = r"D:\Architecture\analysis\leyu_parsed.json"
data = json.load(open(path, encoding="utf-8"))

def outcome(b):
    d = b.get("detail", "")
    if "输" in d:
        return "L"
    if "赢" in d:
        return "W"
    if "退" in d or "和局" in d or "取消" in d:
        return "V"  # void/refund
    return "?"

def parse_dt(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None

# enrich
for b in data:
    b["oc"] = outcome(b)
    b["d"] = parse_dt(b["dt"])

valid = [b for b in data if b["d"]]
max_dt = max(b["d"] for b in valid)
min_dt = min(b["d"] for b in valid)
print(f"数据范围: {min_dt.date()} ~ {max_dt.date()} | 总注单数: {len(data)}")

# window = last 3 calendar days inclusive
win_start = max_dt - timedelta(days=2)
print(f"『最近3天』窗口: {win_start.date()} ~ {max_dt.date()}")
print("="*70)

def seg_stats(label, bets):
    n = len(bets)
    if n == 0:
        print(f"{label}: 0 笔"); return
    stake = sum(b["stake"] for b in bets)
    net = sum(b["win"] for b in bets)
    w = sum(1 for b in bets if b["oc"] == "W")
    l = sum(1 for b in bets if b["oc"] == "L")
    v = sum(1 for b in bets if b["oc"] == "V")
    roi = net / stake * 100 if stake else 0
    wr = w / (w + l) * 100 if (w + l) else 0
    print(f"{label}")
    print(f"  笔数={n} | 投注={stake:.2f} | 净={net:+.2f} | ROI={roi:+.1f}% | 胜率={wr:.1f}% (W{w}/L{l}/V{v})")

# Inform vs blind: user says only last-3-days CS bets are 'serious'
informed = [b for b in valid if b["is_cs"] and b["d"] >= win_start]
blind    = [b for b in valid if not (b["is_cs"] and b["d"] >= win_start)]

print("\n【按你的声明切分】")
seg_stats("A. 最近3天 CS单 (你说认真的)", informed)
seg_stats("B. 其余全部 (你说之前瞎买)", blind)

print("\n【补充：最近3天 ALL 玩法 vs 更早 ALL】")
seg_stats("C. 最近3天 全部玩法", [b for b in valid if b["d"] >= win_start])
seg_stats("D. 更早 全部玩法", [b for b in valid if b["d"] < win_start])

print("\n【CS 整体 vs 非CS 整体】")
seg_stats("E. 全部 CS单", [b for b in valid if b["is_cs"]])
seg_stats("F. 全部 非CS单", [b for b in valid if not b["is_cs"]])

# list the informed CS bets in detail
print("\n" + "="*70)
print("最近3天 CS单 明细:")
for b in sorted(informed, key=lambda x: x["dt"]):
    print(f"  {b['dt']} | {b['play']} | stake={b['stake']:.1f} net={b['win']:+.1f} | {b['oc']} | {b['detail'][:60]}")
