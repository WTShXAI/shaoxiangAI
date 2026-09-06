# -*- coding: utf-8 -*-
"""odds_momentum: 赔率动量滚动窗口 + closing-line-value (报告 P2#1).

从 odds_snapshots 构造某市场(默认 OU over)的去水隐含概率时序, 计算:
  - 滚动窗口(5/15/30min)斜率: 窗口内 (p_end - p_start)/(t_end - t_start), 单位 pp/分钟。
  - 加速度: slope(5min) - slope(30min), 正=近期提速(资金加速涌入该方向)。
  - closing-line-value: 模型概率 - 终盘去水概率, 作 edge 代理(>0 即 beat closing line)。

纪律(IR-18/IR-04):
  - 这些是**弱特征/信息字段**, 绝不单独进硬判定; 盘口漂移兼具赔付管理与热度示警双重语义。
  - 无终盘快照时 closing_line_value 返回 None, 不伪造。
  - 纯函数, 无 DB 副作用, 可单测。

输入约定: snaps 为 list of (market, selection, odds, minute_at) 元组;
  OU 市场形如 'OU_2.50', selection 'over'/'under'; 1X2 市场 '1X2', selection 'home'/'draw'/'away'。
"""
from __future__ import annotations
from typing import Optional, Sequence, List, Dict, Any, Tuple


def devig_probs(odds_list: Sequence[float]) -> List[float]:
    """多结果去水隐含概率。odds_list 长度≥2, 任一≤0 返回 None(脏数据)。"""
    if not odds_list or any(o <= 0 for o in odds_list):
        return []
    inv = sum(1.0 / o for o in odds_list)
    if inv <= 0:
        return []
    return [(1.0 / o) / inv for o in odds_list]


def build_ou_series(snaps: Sequence[Tuple[str, str, float, float]],
                    line: Optional[float] = None) -> List[Tuple[float, float]]:
    """从快照构造 OU over 去水概率时序, 返回 [(minute_at, p_over_devig)] 按时间升序。

    snaps: (market, selection, odds, minute_at)。按 minute_at 聚合同分钟 over/under 配对去水。
    line: 指定盘口线(如 2.5)只取该 OU 市场; None=取所有 OU_* 中能配对者(取末位 line 多场混合不推荐, 故建议传 line)。
    """
    pairs: Dict[float, Dict[str, float]] = {}
    for market, sel, odds, minute in snaps:
        if not (isinstance(market, str) and market.startswith("OU_")):
            continue
        if line is not None:
            try:
                ml = float(market.replace("OU_", ""))
            except Exception:
                continue
            if abs(ml - line) > 1e-6:
                continue
        sel = (sel or "").lower()
        if sel not in ("over", "under"):
            continue
        bucket = pairs.setdefault(float(minute), {})
        bucket[sel] = float(odds)
    series = []
    for minute in sorted(pairs.keys()):
        b = pairs[minute]
        if "over" in b and "under" in b:
            ps = devig_probs([b["over"], b["under"]])
            if ps:
                series.append((minute, ps[0]))  # ps[0]=over de-vig prob
    return series


def _slope_in_window(series: List[Tuple[float, float]], window_min: float) -> Optional[Dict[str, float]]:
    """窗口内线性斜率。series 升序 [(t,p)]。返回 {slope_pp_per_min, p_start, p_end, t_start, t_end} 或 None。"""
    if len(series) < 2:
        return None
    t_max = series[-1][0]
    t_min = t_max - window_min
    pts = [(t, p) for (t, p) in series if t >= t_min]
    if len(pts) < 2:
        pts = series  # 窗口内不足2点, 退化为全段(避免空)
    if len(pts) < 2:
        return None
    t0, p0 = pts[0]
    t1, p1 = pts[-1]
    dt = t1 - t0
    if dt <= 0:
        return None
    slope = (p1 - p0) / dt * 100.0  # pp per minute
    return {"slope_pp_per_min": slope, "p_start": p0, "p_end": p1,
            "t_start": t0, "t_end": t1}


def momentum_features(snaps: Sequence[Tuple[str, str, float, float]],
                      windows: Tuple[float, ...] = (5.0, 15.0, 30.0),
                      line: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """OU 赔率动量特征。返回 None(无可用时序) 或:
    {n_points, p_first, p_last, last_minute,
     slope_5, slope_15, slope_30 (pp/min, 缺窗=None),
     accel = slope_5 - slope_30 (加速度, 正=近期加速)}。
    """
    series = build_ou_series(snaps, line=line)
    if len(series) < 2:
        return None
    out: Dict[str, Any] = {
        "n_points": len(series),
        "p_first": series[0][1],
        "p_last": series[-1][1],
        "last_minute": series[-1][0],
    }
    slopes = {}
    for w in windows:
        r = _slope_in_window(series, w)
        slopes[w] = r["slope_pp_per_min"] if r else None
        out[f"slope_{int(w)}"] = slopes[w]
    s5 = slopes.get(5.0)
    s30 = slopes.get(30.0)
    out["accel"] = (s5 - s30) if (s5 is not None and s30 is not None) else None
    return out


def closing_devig_prob(snaps: Sequence[Tuple[str, str, float, float]],
                       line: Optional[float] = None) -> Optional[float]:
    """终盘 over 去水概率 = 时序最后一点(最大 minute_at)的 over 去水概率。无→None。"""
    series = build_ou_series(snaps, line=line)
    if not series:
        return None
    return series[-1][1]


def closing_line_value(p_model: Optional[float], p_closing_devig: Optional[float]) -> Optional[float]:
    """edge 代理 = 模型概率 - 终盘去水概率。任一为 None→None(不伪造)。>0 即 beat closing line。"""
    if p_model is None or p_closing_devig is None:
        return None
    return float(p_model - p_closing_devig)


if __name__ == "__main__":
    # 自检: 构造合成 OU 快照
    # (a) over 概率持续上升(资金涌入 over) → 正斜率
    snaps_up = [(f"OU_2.50", "over", 2.0 - 0.02 * i, float(i)) for i in range(20)]
    snaps_up += [(f"OU_2.50", "under", 1.8 + 0.02 * i, float(i)) for i in range(20)]
    f_up = momentum_features(snaps_up, line=2.5)
    # (b) over 概率持续下降 → 负斜率
    snaps_dn = [(f"OU_2.50", "over", 1.8 + 0.02 * i, float(i)) for i in range(20)]
    snaps_dn += [(f"OU_2.50", "under", 2.0 - 0.02 * i, float(i)) for i in range(20)]
    f_dn = momentum_features(snaps_dn, line=2.5)
    # (c) 平盘 → 斜率≈0
    snaps_flat = [(f"OU_2.50", "over", 2.0, float(i)) for i in range(20)]
    snaps_flat += [(f"OU_2.50", "under", 2.0, float(i)) for i in range(20)]
    f_flat = momentum_features(snaps_flat, line=2.5)

    assert f_up["slope_5"] is not None and f_up["slope_5"] > 0, f"上升应正斜率, 实={f_up}"
    assert f_dn["slope_5"] is not None and f_dn["slope_5"] < 0, f"下降应负斜率, 实={f_dn}"
    assert abs(f_flat["slope_5"]) < 1e-6, f"平盘应≈0, 实={f_flat}"
    # closing-line-value
    clv = closing_line_value(0.62, closing_devig_prob(snaps_up, line=2.5))
    assert clv is not None and clv > 0, f"模型>终盘应正 CLV, 实={clv}"
    clv_none = closing_line_value(0.62, None)
    assert clv_none is None, "无终盘应 None"
    print(f"[ok] up.slope_5={f_up['slope_5']:.4f} dn.slope_5={f_dn['slope_5']:.4f} "
          f"flat.slope_5={f_flat['slope_5']:.4f} accel_up={f_up['accel']} CLV={clv:.4f}")
