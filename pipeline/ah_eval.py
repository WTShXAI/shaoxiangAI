"""
pipeline.ah_eval — 亚盘(让球)去水与方向判定 (SSoT, 2026-08-01 新增)

铁律: 盘口锚定默认100%跟盘. 亚盘数据来自 events.db odds_snapshots(AH_*) 或
match_outcomes(op_ah_line/op_ah_home/op_ah_away). 本模块只做「去水 + 方向」,
不另造实力模型 —— 与 ou_eval 同源思路, 禁平行重造.

输入: line(让球数, 负=主让 如 -0.5; 正=客让 如 +0.5; 0=平手),
      home_odds(主队方向赔率), away_odds(客队方向赔率)
输出: {line, p_fav, p_dog, fav_side, direction, read}
  - p_fav: 让球方(上盘)赢盘概率; p_dog: 受让方(下盘)赢盘概率
  - fav_side: 让球方是谁 ("主队" 若 line<=0)
  - direction: 被看好(赢盘概率更高)的一方 "主队"/"客队"
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def _devig(h: float, a: float) -> Tuple[float, float]:
    """双选去水. 任一赔率无效(<=1.01)则回退 0.5/0.5."""
    inv_h = 1.0 / float(h) if h and float(h) > 1.01 else 0.0
    inv_a = 1.0 / float(a) if a and float(a) > 1.01 else 0.0
    z = inv_h + inv_a
    if z <= 0:
        return 0.5, 0.5
    return inv_h / z, inv_a / z


def evaluate_ah(line, home_odds, away_odds) -> Optional[Dict[str, Any]]:
    """让球方向判定.

    约定 (与 GQ op_ah_* 一致):
      - line: 让球数. line<0 主让(主=上盘/让球方); line>0 客让(客=上盘); line=0 平手.
      - home_odds: 主队方向(上盘若主让)赔率; away_odds: 客队方向赔率.
      - 去水得 p_h/p_a = 主/客方向赢盘概率; 让球方赢盘概率 = 对应一侧.
    返回 None 表示数据无效(降级, 前端不展示让球).
    """
    if not (home_odds and away_odds and float(home_odds) > 1.01 and float(away_odds) > 1.01):
        return None
    line = float(line) if line is not None else 0.0
    p_h, p_a = _devig(float(home_odds), float(away_odds))

    # 让球方(favorite / 上盘): line<=0 主让 → 主是上盘; line>0 客让 → 客是上盘
    fav_side = "主队" if line <= 0 else "客队"
    dog_side = "客队" if line <= 0 else "主队"
    p_fav = p_h if line <= 0 else p_a
    p_dog = p_a if line <= 0 else p_h

    direction = fav_side if p_fav >= p_dog else dog_side
    best_p = max(p_fav, p_dog)
    read = f"让球{line:+g} → {direction}被看好(赢盘P={best_p:.1%})"
    return {
        "line": line,
        "p_fav": round(p_fav, 4),
        "p_dog": round(p_dog, 4),
        "fav_side": fav_side,
        "direction": direction,
        "read": read,
    }


def win_side(line: float, score_home: int, score_away: int) -> str:
    """给定终场比分 + 让球线, 返回实际赢盘方 ('主队'/'客队'/'走水').

    line 语义 (与 GQ 一致): 负=主让, 正=主受让(客让), 0=平手.
    主队让球后的净胜球 adj = (score_home - score_away) + line
      - adj > 0 → 主队赢盘
      - adj < 0 → 客队赢盘
      - adj == 0 → 走水 (整数盘退款, 回测中不计入分母)

    验算 (2026-08-01 修正, 原公式 `- line` 符号相反, 已导致回测口径错误):
      line= 0.0 主胜1-0  → adj= 1.0 → 主队 ✓
      line= 0.0 平局0-0  → adj= 0.0 → 走水 ✓
      line=-0.5 平局0-0  → adj=-0.5 → 客队 ✓ (主让半球, 平局即输盘)
      line=-0.5 主胜1-0  → adj= 0.5 → 主队 ✓
      line=-1.5 主胜1-0  → adj=-0.5 → 客队 ✓ (让1.5只赢1球=输盘)
      line=-1.5 主胜2-0  → adj= 0.5 → 主队 ✓
      line=+0.5 平局0-0  → adj= 0.5 → 主队 ✓ (主受让半球, 平局即赢盘)
      line=+1.0 客胜0-1  → adj= 0.0 → 走水 ✓

    注: 四分之一盘(±0.25/±0.75)本应半赢半输, 回测按 adj 正负记全胜/全负,
        对「命中率」口径无偏(半赢仍算方向正确), 但不等于资金 ROI.
    """
    try:
        line = float(line)
        adj = (int(score_home) - int(score_away)) + line
        if abs(adj) < 1e-9:
            return "走水"
        return "主队" if adj > 0 else "客队"
    except Exception:
        return "走水"
