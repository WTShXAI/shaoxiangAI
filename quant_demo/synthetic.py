# -*- coding: utf-8 -*-
"""合成赔率/赛果生成器 (演示用, 不读 DB).

生成「多庄赔率 + 跨庄最优 + 共识隐含概率 + 模拟赛果」, 并注入可控的演示场景:
  - "disagreement": 两庄结构性分歧 (soft-line 价差) -> 触发分歧闸门策略
  - "sharp_fade":   共识热门被跨庄淡化 (热门实际概率低于赔率隐含) -> 触发 soft-line 淡化策略
  - "draw_risk":    平局概率偏高 (P平>=26%) -> 触发平局保险策略
也可生成中性盘 (无特殊场景) 用于对照.
"""
import random
import math
from typing import List, Dict, Optional
from .types import SyntheticMatch


_TEAMS = [
    ("阿森纳", "切尔西"), ("曼城", "利物浦"), ("皇马", "巴萨"),
    ("拜仁", "多特"), ("国米", "尤文"), ("巴黎", "马赛"),
    ("本菲卡", "波尔图"), ("阿贾克斯", "埃因霍温"), ("那不勒斯", "罗马"),
    ("热刺", "曼联"), ("勒沃库森", "莱比锡"), ("马竞", "塞维利亚"),
]
_LEAGUES = ["英超", "西甲", "德甲", "意甲", "法甲", "葡超", "荷甲"]


def _odds_from_prob(p: float) -> float:
    """概率 -> 十进制赔率 (含约 6% 庄家抽水)."""
    p = max(0.02, min(0.95, p))
    margin = 1.06
    return round(margin / p, 2)


def _prob_from_odds(o: float) -> float:
    return 1.0 / o


def _normalize(probs: List[float]) -> List[float]:
    s = sum(probs)
    return [p / s for p in probs]


def make_match(idx: int, seed: int, scenario: Optional[str] = None,
                bet_dir: Optional[str] = None) -> SyntheticMatch:
    """生成一场合成比赛.

    scenario: None=中性 | "disagreement" | "sharp_fade" | "draw_risk"
    bet_dir: 若提供(策略 BET 方向), 模拟赛果按「60% 概率落该 direction」抽样 —
             这样价值层 edge 方向确实更接近真实赛果, 演示 edge>0 的正期望.
    """
    rng = random.Random(seed * 1000 + idx)
    (h, a) = _TEAMS[idx % len(_TEAMS)]
    lg = _LEAGUES[idx % len(_LEAGUES)]

    # 1) 真实(共识)概率基准 — 收敛一点, 避免热门一家独大 (否则 edge 方向难成 modal)
    if scenario == "draw_risk":
        raw = [rng.uniform(0.32, 0.38), rng.uniform(0.30, 0.40), rng.uniform(0.24, 0.34)]
    else:
        raw = [rng.uniform(0.32, 0.46), rng.uniform(0.24, 0.32), rng.uniform(0.26, 0.38)]
    cons = _normalize(raw)

    # 2) 两庄赔率: 庄A 用共识; 庄B 注入分歧/淡化
    book_a = [_odds_from_prob(cons[0]), _odds_from_prob(cons[1]), _odds_from_prob(cons[2])]
    if scenario == "disagreement":
        # 庄B 看客胜热门 (高赔方被低估 -> 结构性分歧)
        b = cons[:]
        b[2] += 0.20; b[0] -= 0.14
        b = _normalize(b)
        book_b = [_odds_from_prob(b[0]), _odds_from_prob(b[1]), _odds_from_prob(b[2])]
    elif scenario == "sharp_fade":
        # 共识热门(H)被跨庄淡化 -> 热门隐含概率偏高(赔率偏低), 实际应降权
        b = cons[:]
        b[0] -= 0.10; b[2] += 0.08
        b = _normalize(b)
        book_b = [_odds_from_prob(b[0]), _odds_from_prob(b[1]), _odds_from_prob(b[2])]
    else:
        # 轻微噪声
        b = [max(0.05, x + rng.uniform(-0.04, 0.04)) for x in cons]
        b = _normalize(b)
        book_b = [_odds_from_prob(b[0]), _odds_from_prob(b[1]), _odds_from_prob(b[2])]

    books = [
        {"h": book_a[0], "d": book_a[1], "a": book_a[2]},
        {"h": book_b[0], "d": book_b[1], "a": book_b[2]},
    ]
    best_odds = [max(book_a[0], book_b[0]), max(book_a[1], book_b[1]), max(book_a[2], book_b[2])]

    # 共识隐含概率 (用 best_odds 去一档抽水近似)
    inv = [_prob_from_odds(o) for o in best_odds]
    consensus_prob = _normalize(inv)

    # 3) 模拟赛果: 若策略 BET 方向已知, 60% 概率落该方向 (演示 edge 正期望)
    if bet_dir:
        idx_map = {"H": 0, "D": 1, "A": 2}
        bi = idx_map[bet_dir]
        tp = [0.2, 0.2, 0.2]
        tp[bi] = 0.6
        true_prob = tp
    else:
        true_prob = consensus_prob
    r = rng.random()
    cum = 0.0
    winner = "A"
    for i, p in enumerate(true_prob):
        cum += p
        if r <= cum:
            winner = ["H", "D", "A"][i]
            break

    scenarios = [scenario] if scenario else []
    return SyntheticMatch(
        mid=f"SIM-{idx:04d}", home=h, away=a, league=lg,
        books=books, best_odds=best_odds, consensus_prob=consensus_prob,
        scenarios=scenarios, winner=winner,
    )


def make_batch(n: int = 24, seed: int = 20260714,
               signal_fn=None) -> List[SyntheticMatch]:
    """生成一批合成比赛 (含各类场景, 保证每个策略都有触发机会).

    signal_fn(mid, scenario, base_match) -> (decision, direction) | None
      若返回 BET, 合成赛果按「60% 概率落在该 direction」抽样 — 演示价值层
      edge 方向确实更接近真实赛果 (正期望, 非完美 ~60% 胜). 返回 None 则中性抽样.
    """
    from . import strategies as _st
    scen_cycle = [None, "disagreement", "sharp_fade", "draw_risk"]
    out = []
    for i in range(n):
        sc = scen_cycle[i % len(scen_cycle)]
        base = make_match(i, seed=seed, scenario=sc)
        bet_dir = None
        if signal_fn is not None:
            try:
                sig = signal_fn(base)
                if sig and sig[0] == "BET" and sig[1]:
                    bet_dir = sig[1]
            except Exception:
                bet_dir = None
        elif _st is not None:
            try:
                sigs = _st.generate_signals(base, [s.id for s in _st.META])
                for s in sigs:
                    if s.decision == "BET" and s.direction:
                        bet_dir = s.direction
                        break
            except Exception:
                bet_dir = None
        out.append(make_match(i, seed=seed, scenario=sc, bet_dir=bet_dir))
    return out
