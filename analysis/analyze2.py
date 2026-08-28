import json, re
from collections import Counter

raw = open("leyu_settled_full.json", encoding="utf-8").read().strip()
d = json.loads(raw)
if isinstance(d, str): d = json.loads(d)
rows = d["rows"]

def num(s):
    s = (s or "").replace(",", "").strip()
    try: return float(s)
    except: return 0.0

parsed = []
seen = set()
for r in rows:
    if len(r) < 8: continue
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?(\d{16,})", r[1])
    bid = m.group(2) if m else "?"
    if bid in seen: continue
    seen.add(bid)
    detail = r[3]
    parsed.append(dict(
        dt=(m.group(1) if m else "?")[:10],
        bid=bid, play=r[2], is_cs="波胆" in detail,
        is_win="赢" in detail,
        stake=num(r[4]), win=num(r[5]), ret=num(r[6]),
        detail=detail,
        virt="VS-" in detail or "EAFC" in detail or "PANDA" in detail,
    ))

# full date distribution
dc = Counter(p['dt'] for p in parsed)
print("=== 按日注单数 (全347) ===")
for k in sorted(dc): print(f"  {k}: {dc[k]}")

# all leagues
lg = Counter()
for p in parsed:
    m = re.search(r"\[足球\]\s*([^\s]+)", p['detail'])
    if m: lg[m.group(1)] += 1
print(f"\n=== 涉及联赛(去重前计数, 共{len(lg)}种) 是否含用户口述联赛 ===")
for key in ["哈萨克","哈萨克斯坦","斯洛伐克","丹麦","俄罗斯","俄杯","俄甲","美洲","安哥拉","世界杯","班图"]:
    hits = [(l,c) for l,c in lg.items() if key in l]
    print(f"  [{key}]: {hits if hits else '— 无 —'}")

# virtual vs real
virt = [p for p in parsed if p['virt']]
print(f"\n=== 虚拟/电子盘(EAFC/PANDA/VS-) 注单: {len(virt)} 净输赢 {sum(p['win'] for p in virt):.2f} ===")

# targeted search for the 4 obscure slips
print(f"\n=== 精确搜索用户口述4单(哈萨克甲0-0 / 斯洛伐克U19 3-2 / 丹麦U19 3-3 / 俄杯2-0) ===")
terms = ["凯拉特","奥杜斯克","多瑙斯特","伯德布雷","宁比","哥本哈根","克拉斯诺","布良斯克","哈萨克斯坦","斯洛伐克","丹麦U19","俄罗斯杯","俄杯"]
found_any = False
for p in parsed:
    if any(t in p['detail'] for t in terms):
        found_any = True
        print(f"  {p['dt']} {p['play']} win={p['win']:.1f} | {p['detail'][:80]}")
if not found_any:
    print("  >>> 这4场在体育已结算记录中完全找不到 <<<")

# save full parsed
json.dump(parsed, open("leyu_parsed.json","w"), ensure_ascii=False)
print("\n已保存 leyu_parsed.json")
