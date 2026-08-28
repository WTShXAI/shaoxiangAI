"""
cs_ev_engine.py — 滚球(实时)波胆 +EV 检测器  (哨响AI P0-1)
============================================================
解决用户实战痛点: 他在滚球中"比分盒注"(同场押多条精确比分)但命中率仅 7%,
靠感觉押高赔冷门。本模块把赛前 CS 0-0 错价检测器扩展到 **任意 live 比分 / 任意目标比分**,
用泊松"剩余时间"模型算"该比分成为最终赛果的公平概率", 与 CS 盘隐含概率比较 → +EV 信号。

核心函数:
  league_goal_rate(db)                 # 从 events.db 算各级联赛场均进球(主/客)
  live_score_distribution(...)         # 给定 live 比分+剩余时间, 输出最终比分概率分布
  cs_value_flag(...)                   # 单目标比分 +EV 判定
  rank_ev_scores(...)                 # 枚举候选比分, 返回按 +EV 排序的清单
  ReverseOddsEngine.cs_value_flag(...) # 接入逆向引擎的统一入口

模型 (诚实声明):
  - 剩余进球 ~ Poisson(λ_rem), λ_rem = 联赛场均 × (剩余分钟/90), 主客按联赛主客率拆分。
  - 朴素假设: 剩余时间进球率均匀, 不建模"领先后收缩防守"(obscure 联赛该假设本就脆弱, 见铁律)。
  - 这是"纪律化+EV 过滤器", 非预测神器; 实盘前必须 paper-trading 验证。
"""
from __future__ import annotations
import os
import sqlite3
import math
import logging
from typing import Dict, List, Tuple, Optional

import numpy as np

logger = logging.getLogger(__name__)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GQ_DB = os.path.join(ROOT, "data", "events.db")


# ── 泊松 pmf (不依赖 scipy, 自给自足) ──
def _poisson_pmf(lam: float, k_max: int = 12) -> np.ndarray:
    """返回 P(X=0..k_max) 的数组, 已归一(尾部截断误差<1e-6)。"""
    if lam <= 0:
        out = np.zeros(k_max + 1)
        out[0] = 1.0
        return out
    logf = 0.0
    out = np.zeros(k_max + 1)
    out[0] = math.exp(-lam)
    for k in range(1, k_max + 1):
        logf += math.log(k)
        out[k] = math.exp(-lam + k * math.log(lam) - logf)
    s = out.sum()
    if s > 0:
        out /= s
    return out


# ── 联赛场均进球率缓存 ──
_RATE_CACHE: Dict[str, Tuple[float, float]] = {}   # league_key -> (home_rate, away_rate)
_GLOBAL_RATE: Optional[Tuple[float, float]] = None


def _league_key(league: str) -> str:
    return (league or "").strip().lower()


def league_goal_rate(db_path: str = GQ_DB,
                     league: Optional[str] = None) -> Tuple[float, float]:
    """返回 (home_rate_per90, away_rate_per90)。
    给定 league 则返回该联赛主/客场均进球; 否则返回全局。
    全局率从 events.db 已完赛场次的场均总进球按 55/45 主客拆分(经验)。"""
    global _GLOBAL_RATE
    if league:
        key = _league_key(league)
        if key in _RATE_CACHE:
            return _RATE_CACHE[key]
    if _GLOBAL_RATE is None:
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT home_score, away_score FROM match_outcomes "
                "WHERE score_home IS NOT NULL AND score_away IS NOT NULL").fetchall()
            conn.close()
            if rows:
                tot = np.array([h + a for h, a in rows], dtype=float)
                avg = float(tot.mean()) if tot.size else 2.6
            else:
                avg = 2.6
        except sqlite3.Error:
            avg = 2.6
        _GLOBAL_RATE = (avg * 0.55, avg * 0.45)   # 主客经验拆分
        logger.info(f"[cs_ev] 全局场均进球 {avg:.3f} -> 主{_GLOBAL_RATE[0]:.3f}/客{_GLOBAL_RATE[1]:.3f}")
    if league:
        # 尝试该联赛专属率
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT home_score, away_score FROM match_outcomes "
                "WHERE score_home IS NOT NULL AND score_away IS NOT NULL AND lower(league)=?",
                (league.lower(),)).fetchall()
            conn.close()
            if len(rows) >= 20:
                tot = np.array([h + a for h, a in rows], dtype=float)
                avg = float(tot.mean())
                r = (avg * 0.55, avg * 0.45)
                _RATE_CACHE[_league_key(league)] = r
                return r
        except sqlite3.Error:
            pass
        # 回退全局
        return _GLOBAL_RATE
    return _GLOBAL_RATE


def live_score_distribution(cur_h: int, cur_a: int, minutes_played: float,
                            rate_home: float, rate_away: float,
                            max_goals: int = 8) -> Dict[Tuple[int, int], float]:
    """给定 live 比分与已踢分钟数, 输出最终比分(主,客)概率分布。

    Args:
        cur_h, cur_a   : 当前(实时)比分
        minutes_played : 已进行的分钟数 (0~120)
        rate_home/away : 该联赛每 90 分钟主/客期望进球
    Returns:
        {(h,a): prob}  仅含 prob>0 的项
    """
    remaining = max(0.0, 90.0 - minutes_played)
    if remaining <= 0:
        # 已终场: 当前比分即最终
        return {(int(cur_h), int(cur_a)): 1.0}
    frac = remaining / 90.0
    lam_h = rate_home * frac
    lam_a = rate_away * frac
    pmf_h = _poisson_pmf(lam_h, max_goals)
    pmf_a = _poisson_pmf(lam_a, max_goals)
    dist: Dict[Tuple[int, int], float] = {}
    for i in range(len(pmf_h)):
        if pmf_h[i] <= 0:
            continue
        for j in range(len(pmf_a)):
            if pmf_a[j] <= 0:
                continue
            p = pmf_h[i] * pmf_a[j]
            if p > 1e-9:
                dist[(int(cur_h) + i, int(cur_a) + j)] = p
    return dist


def cs_value_flag(cur_h: int, cur_a: int, minutes_played: float,
                 target_score: str, cs_odds: float,
                 rate_home: float, rate_away: float,
                 thresh: float = 0.02) -> Dict:
    """单目标比分的 +EV 判定。

    target_score: '2-1' / '0-0' / '3-0' 等 (主-客)
    cs_odds     : 该比分的 CS 实时赔率
    返回 {target, fair_prob, implied_prob, divergence, is_ev, ev_edge}
    """
    try:
        th, ta = (int(x) for x in target_score.split("-"))
    except Exception:
        return {"error": f"invalid target_score {target_score}"}
    if cs_odds <= 1.0:
        return {"error": "cs_odds must be > 1"}
    dist = live_score_distribution(cur_h, cur_a, minutes_played, rate_home, rate_away)
    fair = dist.get((th, ta), 0.0)
    implied = 1.0 / cs_odds
    div = fair - implied
    return {
        "target": target_score,
        "live": f"{cur_h}-{cur_a}@{int(minutes_played)}'",
        "fair_prob": round(float(fair), 4),
        "implied_prob": round(float(implied), 4),
        "divergence": round(float(div), 4),
        "is_ev": bool(div >= thresh),
        "ev_edge": round(float(div * cs_odds), 4),   # 若押1单位, 期望净收益
    }


def rank_ev_scores(cur_h: int, cur_a: int, minutes_played: float,
                   cs_market: Dict[str, float], rate_home: float, rate_away: float,
                   thresh: float = 0.02, top_n: int = 8) -> List[Dict]:
    """枚举 CS 市场多条比分, 返回按 divergence 降序的 +EV 清单。
    cs_market: {'2-1': 7.9, '0-0': 11.0, ...} 实时赔率
    """
    out = []
    for sc, odds in cs_market.items():
        r = cs_value_flag(cur_h, cur_a, minutes_played, sc, odds, rate_home, rate_away, thresh)
        if "error" not in r:
            out.append(r)
    out = [x for x in out if x["is_ev"]]
    out.sort(key=lambda x: -x["divergence"])
    return out[:top_n]


# ════════════════════════════════════════════════════════════════
# 集成到 ReverseOddsEngine
# ════════════════════════════════════════════════════════════════
def attach_to_engine(engine) -> None:
    """把 cs_value_flag 方法挂到 ReverseOddsEngine 实例/类 (避免循环 import)。

    用法: from pipeline.cs_ev_engine import attach_to_engine; attach_to_engine(engine)
    或 ReverseOddsEngine 已在模块加载时 import 本模块并绑定 (见 reverse_odds_engine.py 底部)。
    """
    from pipeline.reverse_odds_engine import ReverseOddsEngine

    def _cs_value_flag(self, cur_h, cur_a, minutes_played, target_score, cs_odds,
                       league=None, thresh=0.02):
        rh, ra = league_goal_rate(league=league)
        return cs_value_flag(cur_h, cur_a, minutes_played, target_score, cs_odds, rh, ra, thresh)

    def _rank_ev_scores(self, cur_h, cur_a, minutes_played, cs_market,
                        league=None, thresh=0.02, top_n=8):
        rh, ra = league_goal_rate(league=league)
        return rank_ev_scores(cur_h, cur_a, minutes_played, cs_market, rh, ra, thresh, top_n)

    ReverseOddsEngine.cs_value_flag = _cs_value_flag
    ReverseOddsEngine.rank_ev_scores = _rank_ev_scores
    logger.info("[cs_ev] 已挂载 cs_value_flag / rank_ev_scores 到 ReverseOddsEngine")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # 自测: 乌兹别克超 铁尔米兹 vs 吉扎克, 赛前(0-0, 0'), 目标 3-1@29
    rh, ra = league_goal_rate(league="乌兹别克超")
    print(f"乌兹别克超 主/客率: {rh:.3f}/{ra:.3f}")
    r = cs_value_flag(0, 0, 0, "3-1", 29.0, rh, ra, thresh=0.02)
    print("赛前 3-1@29:", r)
    # 滚球示例: 1-0 (77'), 目标 2-1@7.9 (格鲁吉亚丙)
    rh2, ra2 = league_goal_rate(league="格鲁吉亚丙")
    r2 = cs_value_flag(1, 0, 77, "2-1", 7.9, rh2, ra2, thresh=0.02)
    print("77' 1-0 目标 2-1@7.9:", r2)
    # 枚举
    market = {"0-0": 11.0, "1-0": 8.5, "1-1": 7.0, "2-1": 7.9, "2-0": 12.0, "0-1": 13.0}
    print("赛前 0-0 枚举 +EV:", rank_ev_scores(0, 0, 0, market, league="乌兹别克超"))
