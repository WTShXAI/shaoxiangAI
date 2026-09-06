import json, re
from collections import defaultdict

parsed = json.load(open("leyu_parsed.json", encoding="utf-8"))

# 1) search specific bet ID from user's WC screenshot
print("=== 搜索美国vs比利时 bet ID 5350008960852339 ===")
hit = [p for p in parsed if "5350008960852339" in p['detail']]
print("  找到:", len(hit), [p['detail'][:60] for p in hit] if hit else "— 未找到 —")

# 2) per-day P&L
day = defaultdict(lambda: [0.0,0.0,0])  # stake, win, n
for p in parsed:
    day[p['dt']][0]+=p['stake']; day[p['dt']][1]+=p['win']; day[p['dt']][2]+=1
print("\n=== 每日 P&L (注数 / 投注 / 净赢) ===")
for k in sorted(day):
    s,w,n=day[k]
    print(f"  {k}: n={n:3d} stake={s:7.1f} net={w:8.2f} ROI={w/s*100:6.1f}%")

# 3) best 2-consecutive-day window by net
days=sorted(day)
net={d:day[d][1] for d in days}
best=(-1e9,None)
for i in range(len(days)-1):
    tw=net[days[i]]+net[days[i+1]]
    if tw>best[0]: best=(tw,(days[i],days[i+1]))
print(f"\n最佳连续2日净赢: {best[0]:.2f} @ {best[1]}")
print(f"整体净赢: {sum(net.values()):.2f}  整体ROI: {sum(p['win'] for p in parsed)/sum(p['stake'] for p in parsed)*100:.1f}%")

# 4) biggest single wins
wins=sorted([p for p in parsed if p['win']>0], key=lambda x:-x['win'])
print(f"\n=== Top 10 单笔赢钱 ===")
for p in wins[:10]:
    print(f"  {p['dt']} net=+{p['win']:.1f} stake={p['stake']:.1f} | {p['detail'][:70]}")
