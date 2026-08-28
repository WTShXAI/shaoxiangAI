"""
哨响AI · 波胆冷门买彩方差模拟
目的：量化"100块变1000块"在纯随机买冷门波胆下偶然发生的概率，
      以此判断"中冷门"是能力(edge)还是方差红利。
假设(保守/贴近真实)：
  - 每单押一个冷门波胆，赔率 o 在对数均匀区间 [5, 40] 采样(冷门典型范围)
  - 庄家 margin ≈ 10%，去水后隐含概率 p = 1/(o*1.1)
  - 真实发生概率≈隐含概率(我们实测单庄隐含概率校准 ±6%)
  - 每单独立伯努利试验，10单(=100块/10块一单)，重复 N 次
"""
import random, statistics

random.seed(20260813)
N = 300_000
STAKE = 10
N_BETS = 10
MARGIN = 1.10

def sample_odds():
    # 对数均匀 5~40
    return 5 * (40/5) ** random.random()

def simulate_once():
    total_in = N_BETS * STAKE
    total_out = 0
    wins = 0
    for _ in range(N_BETS):
        o = sample_odds()
        p = 1.0 / (o * MARGIN)
        if random.random() < p:
            wins += 1
            total_out += STAKE * o  # 含本金回收
    net = total_out - total_in
    return net, wins

nets, wins_list = [], []
for _ in range(N):
    net, w = simulate_once()
    nets.append(net)
    wins_list.append(w)

# 阈值：用户"赢了1000块"两种解读
thr_net_1000 = sum(1 for x in nets if x >= 1000) / N   # 净赚1000 (总回收1100)
thr_net_900  = sum(1 for x in nets if x >= 900)  / N   # 总回收1000 (净赚900)
thr_pos      = sum(1 for x in nets if x > 0)    / N
mean_net     = statistics.mean(nets)
median_net   = statistics.median(nets)
max_net      = max(nets)

print(f"模拟次数            : {N:,}")
print(f"每单投入/单数/总投入 : {STAKE}元 × {N_BETS}单 = {STAKE*N_BETS}元")
print(f"庄家margin          : {(MARGIN-1)*100:.0f}%")
print(f"理论期望净回报/轮   : {mean_net:+.2f}元 (应为负，庄家优势)")
print(f"中位数净回报/轮     : {median_net:+.2f}元")
print(f"单次模拟最大净回报   : {max_net:+.2f}元")
print("-" * 50)
print(f"随机下净赚>0 的概率      : {thr_pos*100:5.2f}%")
print(f"随机下净赚≥900(总回收1000): {thr_net_900*100:5.2f}%")
print(f"随机下净赚≥1000(总回收1100): {thr_net_1000*100:5.2f}%")
print("-" * 50)
# 如果"感觉"真的有效：命中率翻倍后的期望
print("若真实命中率=隐含概率的 2 倍(假设有edge)：")
def simulate_edge(mult):
    total_in = N_BETS * STAKE
    total_out = 0
    for _ in range(N_BETS):
        o = sample_odds()
        p = min(1.0, 1.0/(o*MARGIN) * mult)
        if random.random() < p:
            total_out += STAKE * o
    return total_out - total_in
edge_nets = [simulate_edge(2.0) for _ in range(N)]
print(f"  期望净回报/轮 : {statistics.mean(edge_nets):+.2f}元")
print(f"  净赚≥1000概率 : {sum(1 for x in edge_nets if x>=1000)/N*100:5.2f}%")
