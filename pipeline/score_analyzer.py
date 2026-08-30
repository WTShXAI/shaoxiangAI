"""比分分析器 (2026-08-30 拍板) — 三级判定: 定方向 / 软加权 / 观望。

定位: **你是比分分析器，不是预测器。**

每轮输出必须且仅能落在以下三级之一:

【定方向】  领先方与市场方向一致, 且置信度 ≥ 0.70
            → 输出该方向比分, 置信度正常, 不附加分歧标注。
【软加权】  领先方与市场方向冲突, 但置信度 ∈ [0.45, 0.70]
            → 方向听领先方; 概率分布整体保留, 不符合方向的比分乘
              0.05~0.10 衰减系数(禁止置零), 平局/爆冷尾巴保留 5~10%;
              标注「方向分歧, 置信度降级一档」。
【观望】    置信度 < 0.45, 或(领先方与市场冲突 且 比分胶着/剩余充裕/噪声显著)
            → 不下方向、不输出推荐比分, 仅输出原始概率分布,
              标注「观望: 信息不足以支撑方向判断」。

硬约束:
  1. 任何情况下不得对不符合方向的比分直接置零。
  2. 任何置信度 < 0.45 的场景不得定方向。

判定优先级: 先算置信度 → 再检测领先方与市场是否一致 → 由高到低匹配, 命中即停。

输出结构固定: {级别, 方向(观望为 null), 概率分布, 分歧标注(无则省略)}
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PRIOR_PATH = os.path.join(_HERE, "config", "lead_result_prior.json")

# 软加权衰减系数 (不符合方向的比分乘此系数; 禁止置零)
SOFT_ATTENUATION = 0.08
# 高置信阈值 → 定方向
CONF_HIGH = 0.70
# 低置信阈值 → 观望 (2026-08-30 滚球时点定档, 用户拍板"拉满准确率"):
#   0.55 → 81.20%/47.8% (纯准确率档, 观望近半, 操盘手宁缺勿滥)
#   0.50 → 78.42%/35.3% (平衡点) ; 0.45 → 76.62%/25.7% (偏松)
CONF_LOW = 0.55


def _band(minute: int) -> Optional[str]:
    m = int(minute or 0)
    if m <= 30:
        return "5-30"
    if m <= 55:
        return "31-55"
    if m <= 85:
        return "56-85"
    return None


def _load_prior() -> Dict:
    try:
        with open(_PRIOR_PATH, encoding="utf-8") as f:
            return json.load(f).get("table") or {}
    except Exception:
        return {}


def lead_win_prob(lead_side: str, lead_goals: int, minute: int) -> Optional[float]:
    """领先方最终获胜率 (历史统计, 领先方先验表)。"""
    if not lead_side or not lead_goals:
        return None
    b = _band(minute)
    if b is None:
        return None
    cell = _load_prior().get(f"{lead_side}|{min(abs(lead_goals), 3)}|{b}")
    if not cell or int(cell.get("n", 0)) < 30:
        return None
    # 领先方获胜率: home 领先 → home 胜率; away 领先 → away 胜率
    return float(cell.get(lead_side, 0.0))


def _dir_of(score: str) -> Optional[str]:
    p = str(score).replace(":", "-").split("-")
    try:
        h, a = int(p[0] or 0), int(p[1] or 0)
    except Exception:
        return None
    return "home" if h > a else ("away" if a > h else "draw")


def analyze_score(
    market_dir: str,           # 市场去水 argmax: home/draw/away
    market_probs: Tuple[float, float, float],  # (ph, pd, pa)
    lead_side: Optional[str],  # 当前领先方 home/away/None(赛前或平局)
    lead_goals: int,
    minute: int,
    score_dist: Optional[Dict[str, float]] = None,
) -> Dict:
    """三级判定。score_dist: {"2-1": 0.18, ...} 可选, 软加权/定方向时返回调整后的分布。"""
    ph, pd_, pa = market_probs

    # ── 1. 计算置信度 ──
    if lead_side and lead_goals > 0:
        p_lead = lead_win_prob(lead_side, lead_goals, minute)
        p_mkt = ph if lead_side == "home" else pa
        if p_lead is None:
            # 领先方先验缺失(样本不足/越界) → 置信度退化为市场最强概率, 方向听市场
            conf = max(ph, pd_, pa)
            conflict = False
            lead_side_eff = None
        else:
            conf = 1.0 - abs(p_lead - p_mkt)
            conflict = (lead_side != market_dir)
            lead_side_eff = lead_side
    else:
        # 赛前/平局: 无领先方 → 方向=市场, 置信度=市场最强概率
        conf = max(ph, pd_, pa)
        conflict = False
        lead_side_eff = None

    # 胶着/剩余充裕/噪声 判定 (用于观望的第二条件)
    drawn = (lead_goals <= 1)                     # 比分胶着(领先≤1球)
    ample_time = (int(minute or 0) <= 60)         # 剩余时间充裕

    # ── 2. 由高到低匹配, 命中即停 ──
    if lead_side is None or lead_goals <= 0:
        # 赛前/平局: 无领先方 → 方向=市场, 置信度=市场最强概率
        if conf >= CONF_LOW:
            level = "定方向"
            direction = market_dir
            note = None
        else:
            level = "观望"
            direction = None
            note = "观望: 信息不足以支撑方向判断"
    elif (not conflict) and conf >= CONF_HIGH:
        level = "定方向"
        direction = lead_side_eff or market_dir
        note = None
    elif conflict and CONF_LOW <= conf <= CONF_HIGH:
        level = "软加权"
        direction = lead_side_eff
        note = "方向分歧, 置信度降级一档"
    elif conf < CONF_LOW or (conflict and drawn) or (conflict and ample_time):
        level = "观望"
        direction = None
        note = "观望: 信息不足以支撑方向判断"
    else:
        # conf ≥ 0.70 但冲突(且不胶着/不充裕) → 高置信冲突, 仍软加权听领先方
        level = "软加权"
        direction = lead_side_eff
        note = "方向分歧, 置信度降级一档"

    # ── 3. 概率分布处理 ──
    dist = score_dist
    if dist and level == "软加权" and direction:
        dist = _soft_weight(dist, direction)
    # 定方向/观望: 返回原始分布 (定方向可在调用方做方向标注, 不衰减)

    out = {
        "级别": level,
        "方向": direction,
        "置信度": round(conf, 3),
        "概率分布": dist,
    }
    if note:
        out["分歧标注"] = note
    return out


def _soft_weight(dist: Dict[str, float], direction: str) -> Dict[str, float]:
    """软加权: 符合方向保留, 平局乘 0.08, 对手方向乘 0.05, 重新归一化。
    禁止置零(硬约束)。"""
    out = {}
    for s, p in dist.items():
        d = _dir_of(s)
        if d is None:
            out[s] = p
        elif d == direction:
            out[s] = p
        elif d == "draw":
            out[s] = p * 0.08
        else:
            out[s] = p * SOFT_ATTENUATION  # 0.05~0.10, 用 0.08 折中
    tot = sum(out.values()) or 1.0
    return {s: p / tot for s, p in out.items()}


def top_scores_from_dist(dist: Dict[str, float], n: int = 3) -> List[Dict]:
    """从概率分布取 top N (供展示)。"""
    if not dist:
        return []
    top = sorted(dist.items(), key=lambda x: -x[1])[:n]
    return [{"score": s, "prob": round(p, 4)} for s, p in top]
