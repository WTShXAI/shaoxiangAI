"""敏感性：不同冷门赔率区间下，100元(10单)随机买彩达成净赚≥900的概率"""
import random, statistics
random.seed(7)
N = 300_000
STAKE, N_BETS, MARGIN = 10, 10, 1.10

def sim_band(lo, hi):
    out = 0.0
    for _ in range(N_BETS):
        o = lo * (hi/lo) ** random.random()
        p = 1.0 / (o * MARGIN)
        if random.random() < p:
            out += STAKE * o
    return out - N_BETS*STAKE

print(f"{'赔率区间':16s} {'期望净回报':>12s} {'中位数':>10s} {'净赚≥900概率':>14s} {'净赚≥1000概率':>14s}")
for lo, hi, label in [
    (5, 40, "原始假设(5-40)"),
    (6, 12, "温和冷门(6-12)"),
    (8, 20, "中等冷门(8-20)"),
    (10, 30, "偏极端(10-30)"),
    (15, 40, "大冷门(15-40)"),
]:
    band = [sim_band(lo, hi) for _ in range(N)]
    exp = statistics.mean(band)
    med = statistics.median(band)
    p900 = sum(1 for x in band if x >= 900)/N*100
    p1000 = sum(1 for x in band if x >= 1000)/N*100
    print(f"{label:16s} {exp:+12.2f} {med:+10.2f} {p900:13.2f}% {p1000:13.2f}%")
