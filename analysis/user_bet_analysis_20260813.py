"""
用户 2026-08-12~13 波胆投注实际数据还原与分析
数据来源：5张截图 + 口述1场未截图
方法：用赔率反推庄家隐含概率(扣10% margin)，算实际ROI/EV/随机全中概率
"""
import math, random, statistics
random.seed(20260813)

# 截图中5场
bets = [
    {"league": "哈萨克斯坦甲级", "match": "凯拉特斯科斯塔尔 vs 奥杜斯克学院", "score": "0-0", "odds": 42.0, "stake": 6.50, "type": "赛前"},
    {"league": "安哥拉班图",     "match": "恩津加乌尼奥 vs 万博英雄",         "score": "0-2", "odds": 12.0, "stake": 10.00, "type": "滚球"},
    {"league": "斯洛伐克U19",   "match": "多瑙斯特雷达U19 vs 伯德布雷佐夫U19", "score": "3-2", "odds": 17.5, "stake": 9.36, "type": "赛前"},
    {"league": "丹麦U19",       "match": "宁比U19 vs 哥本哈根U19",           "score": "3-3", "odds": 34.0, "stake": 10.00, "type": "赛前"},
    {"league": "俄罗斯杯资格赛", "match": "克拉斯诺亚兹纳米亚 vs 布良斯克戴拿模", "score": "2-0", "odds": 17.0, "stake": 7.00, "type": "赛前"},
]

# 口述未截图的1场
extra_bet = {"league": "未知", "match": "口述场", "score": "1-4", "odds": 56.0, "stake": 60.00, "type": "赛前/滚球未知"}

MARGIN = 1.10

def analyze(bet_list, label):
    total_in = sum(b["stake"] for b in bet_list)
    total_out = sum(b["stake"] * b["odds"] for b in bet_list)
    net = total_out - total_in
    roi = net / total_in if total_in else 0
    # 庄家去水隐含概率
    joint_prob = 1.0
    print(f"\n=== {label} (共{len(bet_list)}单) ===")
    print(f"{'联赛':12s} {'比分':6s} {'赔率':>6s} {'投入':>8s} {'返还':>10s} {'隐含概率':>10s}")
    for b in bet_list:
        p = 1.0 / (b["odds"] * MARGIN)
        joint_prob *= p
        out = b["stake"] * b["odds"]
        print(f"{b['league']:12s} {b['score']:6s} {b['odds']:6.2f} {b['stake']:8.2f} {out:10.2f} {p*100:9.4f}%")
    # 理论期望(假设庄家校准)
    expected_out = total_in / MARGIN
    expected_net = expected_out - total_in
    print(f"\n总投入: {total_in:.2f} 元 | 总返还: {total_out:.2f} 元 | 净赚: {net:+.2f} 元")
    print(f"实际 ROI: {roi*100:+.2f}% | 理论期望净赚: {expected_net:+.2f} 元 (庄家优势)")
    print(f"随机下这{bet_list.__len__()}单全中的联合概率(去水后): {joint_prob*100:.6f}% ({joint_prob:.2e})")
    return total_in, total_out, net, joint_prob

# 分析只展示5单
a_in, a_out, a_net, a_joint = analyze(bets, "截图5场")

# 分析6场(含口述56倍)
all_bets = bets + [extra_bet]
b_in, b_out, b_net, b_joint = analyze(all_bets, "截图5场+口述1场")

# 关键问题：用户自述"100块赢1000块"与展示数据的关系
print("\n=== 口径对照 ===")
print(f"仅展示5单: 投入 {a_in:.2f}, 净赚 {a_net:.2f} -> 和自述的'100块赢1000'投入口径不一致")
print(f"6场合计  : 投入 {b_in:.2f}, 净赚 {b_net:.2f} -> 投入102.86, 净赚约4273, 远超自述")
print(f"若自述'100块总投入'为真，则还有约 {100 - a_in:.2f} 元(≈{100-a_in:.0f}块)的下注未展示/未说明结果")

# 蒙特卡洛：如果这5单是"随机精选"，重复买很多次，出现全中/高净赚的概率
print("\n=== 蒙特卡洛：重复这5单组合10万次 ===")
def sim_same_five():
    net = 0
    wins = 0
    for b in bets:
        p = 1.0 / (b["odds"] * MARGIN)
        if random.random() < p:
            wins += 1
            net += b["stake"] * b["odds"] - b["stake"]
        else:
            net -= b["stake"]
    return net, wins

results = [sim_same_five() for _ in range(100_000)]
max_net = max(r[0] for r in results)
mean_net = statistics.mean(r[0] for r in results)
all_five_rate = sum(1 for r in results if r[1] == 5) / 100_000
print(f"期望净回报/轮: {mean_net:+.2f} 元")
print(f"随机下5单全中  : {all_five_rate*100:.4f}%")
print(f"随机下最大净赚  : {max_net:+.2f} 元")

# 敏感性：如果那100块里包含 X 笔未中10元投注，真实战绩如何？
print("\n=== 反推：若你2天真实总投入=100元，其余未展示部分亏掉，则真实净赚 ===")
for extra_losers in [0, 1, 2, 3, 4, 5, 6]:
    extra_stake = extra_losers * 10
    if a_in + extra_stake <= 100:
        true_net = a_net - extra_stake
        true_roi = true_net / 100
        print(f"  未展示 {extra_losers} 笔10元未中: 真实净赚 {true_net:+.2f}元, 真实ROI {true_roi*100:+.2f}%")

print("\n=== 结论前提 ===")
print("以上概率均基于'赔率隐含概率=真实概率'且各场独立。")
print("小联赛波胆庄家margin可能>10%，真实概率可能更低；若如此，随机达成概率只会更小。")
print("但：'只展示中奖单'会让任何战绩看起来都像神迹——必须把全部下注(包括未中的)列出来才算真实EV。")
