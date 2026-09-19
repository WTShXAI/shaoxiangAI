"""
pipeline/settle.py — 赛果结算原语 (纯函数, 零依赖)
====================================================
预测系统改造 (2026-09-18): 把"赛果怎么判"从投注闭环 (roi_report, 已归档
archive/betting_decisions/) 中拆出来, 供概率评估/校准链路使用。

这里只有赛果判定原语 — 没有注码、没有 PnL、没有 ROI:
  parse_score(s)            '2-1'/'2:1' → (2, 1)
  total_goals(s)            '2-1' → 3
  line_from_sel(sel)        'under_2.5' → 2.5
  result_1x2(h, a)          比分 → 'home'|'draw'|'away'
  settle_ou(tg, line)       总进球+盘口 → 'over'|'under'|'push'|None
"""
from __future__ import annotations
import re
from typing import Optional, Tuple


def parse_score(s) -> Optional[Tuple[int, int]]:
    """'2-1' / '2:1' / '2 - 1' → (2, 1); 否则 None。"""
    if not s:
        return None
    m = re.search(r"(\d+)\s*[-:]\s*(\d+)", str(s).strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def total_goals(s) -> Optional[int]:
    p = parse_score(s)
    return (p[0] + p[1]) if p else None


def line_from_sel(sel: str) -> Optional[float]:
    """'under_2.5' → 2.5。"""
    m = re.search(r"([\d.]+)$", str(sel))
    return float(m.group(1)) if m else None


def result_1x2(score_home, score_away) -> Optional[str]:
    """全场比分 → 'home'|'draw'|'away'; 比分缺失返回 None。"""
    if score_home is None or score_away is None:
        return None
    if score_home > score_away:
        return "home"
    if score_home == score_away:
        return "draw"
    return "away"


def settle_ou(total: Optional[int], line: float) -> Optional[str]:
    """总进球 vs OU 盘口 → 'over'|'under'|'push'(走盘)|None(无法判)。"""
    if total is None or line is None:
        return None
    if total > line:
        return "over"
    if total < line:
        return "under"
    return "push"


def credible_1x2(score_home, score_away, last_odds_ts=None, kickoff_ts=None,
                 live_floor_sec: float = 95.0 * 60) -> bool:
    """假0-0守卫 (2026-09-19): status=finished 且全场 0-0, 但最后赔率 tick 早于
    kickoff+95min → 比分帧中途断流后定格的假 0-0, 该结算不可信。

    实证 (2026-09-19, daily_predictions 已结算台账抽样):
      0-0 场 78% 终盘 tick < kickoff+95min (中位 -3min, 纯断流);
      1-1 场 97% / 有胜负场 92% 持续到 ≥95min;
      通过守卫的 0-0 占已结算全集 ~6.9%, 与真实足球 0-0 频率吻合。
    非 0-0 赛果恒可信 (有进球即有真实比分帧流动); 参数缺失时保守返回 False。
    """
    if score_home is None or score_away is None:
        return False
    if score_home != 0 or score_away != 0:
        return True
    if last_odds_ts is None or kickoff_ts is None:
        return False
    try:
        return float(last_odds_ts) >= float(kickoff_ts) + live_floor_sec
    except (TypeError, ValueError):
        return False
