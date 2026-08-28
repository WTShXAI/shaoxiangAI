"""
pipeline.inplay_ou — 半场条件大小球预测器 (编码用户经验性结构特征)

用户经验论点 (涛哥, 2026-08-03): 半场比分 1:1 + 下半场时间窗口充足 → 大3.5 高度可打出.
核心不是 EV, 而是"结构 + 剩余时间窗口"命题: 已知半场状态时, 全场总进球条件概率
显著高于无条件基线. 这不是"赌价值", 是"赌已验证的结构".

数据基础: data/ht_conditional_ou.json
  由 football_data.db.matches (1829 场含半场+全场比分) 计算:
  HT 状态 → 各 OU 线条件大球率.
  例: HT 1:1 → 大3.5 = 45.0% (无条件 30.1%, 结构性 +15pp) ← 与用户现场判断一致.

predict(ht_home, ht_away, line, minutes_remaining=45.0, price=None):
  1. 查 (HT状态, line) 经验条件大球率
  2. 小样本经验贝叶斯收缩 (K=30) 向该线无条件率靠拢, 防极端 n 抖动
  3. 时间窗口衰减: minutes_remaining<45 时, 对"超出基线的结构性 lift"按 剩余/45 比例衰减
     (结构优势=开放局+剩余时间, 随时钟缩减; HT 调用=全 lift, FT 调用=退回基线)
  4. 返回 over_prob + under_prob + confidence + (price 给定时) 公平赔率/正EV标记

注意: 这是 IN-PLAY 信号 — 调用时半场比分已知(中场或中场后). 赛前不知 HT 则退回无条件基线.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "ht_conditional_ou.json",
)
_K_SMOOTH = 30.0  # 经验贝叶斯先验强度 (小样本向无条件基线收缩)

_cache = None


def _load() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        with open(_DATA_PATH, encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def _ht_key(h, a) -> str:
    return f"{int(h)}:{int(a)}"


def all_lines_for(ht_home, ht_away) -> Dict[str, Any]:
    """返回某半场比分在所有 OU 线上的条件大球率 (供展示)."""
    data = _load()
    ht = _ht_key(ht_home, ht_away)
    state = next((s for s in data["ht_states"] if s["ht"] == ht), None)
    if state is None:
        return {"ht": ht, "n": 0, "found": False, "lines": {}}
    return {"ht": ht, "n": state["n"], "found": True, "lines": state["lines"]}


def predict(ht_home, ht_away, line,
            minutes_remaining: float = 45.0,
            price: Optional[float] = None) -> Dict[str, Any]:
    """半场条件大小球预测.

    ht_home/ht_away: 半场主/客进球数 (整数)
    line: OU 盘口线 (如 3.5)
    minutes_remaining: 剩余比赛分钟 (45=刚中场, 0=临近终场). 影响时间窗口衰减.
    price: 大球赔率 (可选, 给定则返回公平赔率/EV 供参考, 不用于主判定)
    """
    data = _load()
    line_s = str(float(line))
    uncon = data["unconditional_over_rate"].get(line_s)
    ht = _ht_key(ht_home, ht_away)
    state = next((s for s in data["ht_states"] if s["ht"] == ht), None)

    if uncon is None:
        return {"error": f"line {line} 无基线数据", "over_prob": None, "under_prob": None}

    # HT 状态在训练库无样本 → 退回无条件基线 + 低把握
    if state is None or line_s not in state["lines"]:
        return {
            "ht": ht, "line": line, "n": (state["n"] if state else 0),
            "over_prob": round(uncon, 4), "under_prob": round(1 - uncon, 4),
            "confidence": "低", "source": "unconditional_baseline",
            "minutes_remaining": minutes_remaining,
        }

    cond = state["lines"][line_s]["over_rate"]
    n = state["n"]

    # 1. 经验贝叶斯收缩 (小样本防抖)
    smoothed = (n * cond + _K_SMOOTH * uncon) / (n + _K_SMOOTH)

    # 2. 时间窗口衰减 (仅中场后深处调用生效)
    if minutes_remaining < 45.0:
        frac = max(0.0, min(1.0, minutes_remaining / 45.0))
        lift = smoothed - uncon
        smoothed = uncon + lift * frac

    over = max(0.001, min(0.999, smoothed))

    # 把握度 (按样本量)
    if n >= 100:
        conf = "高"
    elif n >= 40:
        conf = "中"
    else:
        conf = "低"

    out = {
        "ht": ht, "line": line, "n": n,
        "over_prob": round(over, 4), "under_prob": round(1 - over, 4),
        "conditional_rate": round(cond, 4), "unconditional_rate": round(uncon, 4),
        "lift_pp": round((cond - uncon) * 100, 1),
        "confidence": conf, "source": "ht_conditional",
        "minutes_remaining": minutes_remaining,
    }
    if price is not None:
        fair = 1.0 / over if over > 0 else 0
        out["fair_odds"] = round(fair, 3)
        out["market_odds"] = price
        out["ev"] = round(over * price - 1.0, 4)
        out["positive_ev"] = (over * price) > 1.0
    return out


if __name__ == "__main__":
    print("=== 半场条件大小球 自测 ===")
    cases = [
        (1, 1, 3.5, 45.0, 1.92),   # 用户论点: 1:1 + 大3.5 @1.92
        (2, 1, 3.5, 45.0, None),
        (1, 2, 3.5, 45.0, None),
        (0, 0, 3.5, 45.0, None),
        (1, 1, 3.5, 20.0, 1.92),   # 同一场 70' 仍 1:1, 时间窗口衰减
        (1, 1, 2.5, 45.0, None),
    ]
    for h, a, ln, mr, pr in cases:
        r = predict(h, a, ln, mr, pr)
        print(f"HT {h}:{a} 大{ln} | 剩{mr}min -> 大球 {r['over_prob']} "
              f"(条件{r.get('conditional_rate')}, 基线{r.get('unconditional_rate')}, "
              f"lift {r.get('lift_pp')}pp, 把握{r['confidence']})"
              + (f" | 赔率{pr}→公平{r['fair_odds']} EV{r['ev']}" if pr else ""))
