import json, re

raw = open("leyu_settled_full.json", encoding="utf-8").read().strip()
d = json.loads(raw)
if isinstance(d, str):
    d = json.loads(d)
rows = d["rows"]

def num(s):
    s = (s or "").replace(",", "").strip()
    try: return float(s)
    except: return 0.0

parsed = []
seen = set()
for r in rows:
    if len(r) < 8: continue
    meta = r[1]
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?(\d{16,})", meta)
    dt = m.group(1) if m else "?"
    bid = m.group(2) if m else "?"
    if bid in seen: continue
    seen.add(bid)
    detail = r[3]
    stake = num(r[4]); win = num(r[5]); ret = num(r[6])
    is_win = "赢" in detail
    play = r[2]
    is_cs = "波胆" in detail
    sc = re.search(r"(\d+-\d+)\s*@([\d.]+)", detail)
    score = sc.group(1) if sc else ""
    odds = float(sc.group(2)) if sc else None
    lg = re.search(r"\[足球\]\s*([^\s]+)", detail)
    league = lg.group(1) if lg else "?"
    parsed.append(dict(dt=dt, bid=bid, play=play, is_cs=is_cs, score=score, odds=odds,
                       stake=stake, win=win, ret=ret, is_win=is_win, league=league, detail=detail))

print(f"唯一注单数: {len(parsed)}")
tot_stake = sum(p['stake'] for p in parsed)
tot_win = sum(p['win'] for p in parsed)
tot_ret = sum(p['ret'] for p in parsed)
print(f"总投注额: {tot_stake:.2f}  总输赢(净): {tot_win:.2f}  总返还: {tot_ret:.2f}")
print(f"整体ROI(净/投注): {tot_win/tot_stake*100:.1f}%")
wins = [p for p in parsed if p['is_win']]
print(f"赢单数: {len(wins)}  输单数: {len(parsed)-len(wins)}  胜率: {len(wins)/len(parsed)*100:.1f}%")

# CS bets
cs = [p for p in parsed if p['is_cs']]
cs_wins = [p for p in cs if p['is_win']]
cs_stake = sum(p['stake'] for p in cs)
cs_win = sum(p['win'] for p in cs)
print(f"\n--- 波胆(CS) ---")
print(f"波胆单数: {len(cs)}  赢: {len(cs_wins)}  胜率: {len(cs_wins)/len(cs)*100:.1f}%")
print(f"波胆总投注: {cs_stake:.2f}  波胆净输赢: {cs_win:.2f}  波胆ROI: {cs_win/cs_stake*100:.1f}%")

# Non-CS
noncs = [p for p in parsed if not p['is_cs']]
print(f"\n--- 非波胆(让球/大小/独赢等) ---")
print(f"单数: {len(noncs)}  净输赢: {sum(p['win'] for p in noncs):.2f}  ROI: {sum(p['win'] for p in noncs)/sum(p['stake'] for p in noncs)*100:.1f}%")

# 6 claimed winning bets
print(f"\n--- 用户口述6单搜索 ---")
targets = ["凯拉特","奥杜斯克","恩津加","万博","多瑙斯特","伯德布雷","宁比","哥本哈根","克拉斯诺","布良斯克","美国","比利时","5350008960852339"]
for p in parsed:
    if any(t in p['detail'] for t in targets):
        print(f"  找到: {p['dt']} {p['league']} {p['score']}@{p['odds']} stake={p['stake']} win={p['win']} {'赢' if p['is_win'] else '输'} | {p['detail'][:70]}")

# Date distribution
from collections import Counter
dc = Counter(p['dt'][:10] for p in parsed)
print(f"\n--- 按日分布(注单数) ---")
for k in sorted(dc): print(f"  {k}: {dc[k]} 注")

# CS win list (all winning CS)
print(f"\n--- 所有赢的波胆单 ---")
for p in cs_wins:
    print(f"  {p['dt']} {p['league']} {p['score']}@{p['odds']} stake={p['stake']} win={p['win']}")
