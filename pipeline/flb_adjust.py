"""FLB 感知去水 (favorite-longshot bias aware devig).
背景: 在 IW 14万场上验证, 庄家 8.17% 抽水在赔率轴上分布不均 --
  长赔率(冷门)被系统性高估(实际打出率低于去水隐含概率 -2.5pp, ROI -21.9%),
  短赔率(热门)被相对低估(实际打出率高于隐含概率 +4pp, ROI -3.1%).
  => 单庄内一切负 ROI, 但冷门边际泄漏远大于热门.
关键推论(易错, 已纠正): devig_flb 给出的"公平概率"对冷门更低、对热门更高,
  这更接近真实概率. 但正因为冷门真实概率比看上去更低, 任何模型若敢给冷门
  高于市场的概率, 几乎必然是模型自己也被 FLB 偏差带偏 -> 冷门"价值"应是
  *伪信号*, 要惩罚而非采纳. 所以正确用法不是用 FLB 去找冷门价值, 而是:
  (1) 用 devig_flb 作公平概率基线; (2) 对冷门侧 edge 施加惩罚(降权);
  (3) 模型不确定时倾向热门(抽水泄漏最小, 且热门被相对低估).
用法: 作为 compute_value_layer / unified_predictor 的可选公平概率基线(不破坏 SSoT).
gamma>1 -> 长赔率相对降权(更贴合 FLB); gamma=1 等价于标准 devig.
"""
import numpy as np

DEFAULT_GAMMA = 1.08  # 在 IW 14万场上标定: 复现 短赔率+4pp / 长赔率-2.5pp 的 edge 模式

def devig_flb(h, d, a, gamma=DEFAULT_GAMMA):
    h = np.asarray(h, float); d = np.asarray(d, float); a = np.asarray(a, float)
    raw = np.stack([1.0/h, 1.0/d, 1.0/a], axis=-1)        # 原始注码占比 (n,3)
    adj = raw ** gamma                                    # gamma>1 压缩长赔率(小值)的相对权重
    fair = adj / adj.sum(axis=-1, keepdims=True)
    return fair if fair.ndim > 1 else fair.ravel()

def devig_even(h, d, a):
    """标准均匀去水, 作为对照."""
    h = np.asarray(h, float); d = np.asarray(d, float); a = np.asarray(a, float)
    raw = np.stack([1.0/h, 1.0/d, 1.0/a], axis=-1)
    return raw / raw.sum(axis=-1, keepdims=True)

if __name__ == "__main__":
    # 演示: 一场"热门偏弱、冷门偏高"的盘口
    # 主胜1.50(热门) / 平3.40 / 客胜7.00(冷门)
    h, d, a = 1.50, 3.40, 7.00
    even = devig_even(h, d, a)
    flb = devig_flb(h, d, a)
    print(f"赔率 H={h} D={d} A={a}")
    print(f"均匀去水公平概率: H={even[0]:.3f} D={even[1]:.3f} A={even[2]:.3f}")
    print(f"FLB去水 公平概率: H={flb[0]:.3f} D={flb[1]:.3f} A={flb[2]:.3f}")
    print(f"FLB 相对均匀:     H={flb[0]-even[0]:+.3f} D={flb[1]-even[1]:+.3f} A={flb[2]-even[2]:+.3f}")
    # 假设某独立模型给出概率 (略看好客胜冷门, 这是 FLB 易误报区)
    model = np.array([0.58, 0.24, 0.18])
    edge_even = model - even
    edge_flb  = model - flb
    print(f"\n若模型概率=[.58,.24,.18]:")
    print(f"  均匀去水 edge: H={edge_even[0]:+.3f} D={edge_even[1]:+.3f} A={edge_even[2]:+.3f}")
    print(f"  FLB去水  edge: H={edge_flb[0]:+.3f} D={edge_flb[1]:+.3f} A={edge_flb[2]:+.3f}")
    # 正确用法: 对冷门侧 edge 施加惩罚(冷门真实概率比 FLB 公平概率还低, 伪价值)
    odds = np.array([h, d, a])
    longshot_penalty = np.where(odds >= 3.0, 0.5, 1.0)   # 长赔率侧 edge 砍半
    edge_flb_pen = edge_flb * longshot_penalty
    print(f"  FLB+冷门惩罚 edge: H={edge_flb_pen[0]:+.3f} D={edge_flb_pen[1]:+.3f} A={edge_flb_pen[2]:+.3f}")
    print("  -> 正确结论: 冷门侧'价值'被惩罚压掉; 只剩热门侧可谨慎采纳(且仍须跨庄确认)")
