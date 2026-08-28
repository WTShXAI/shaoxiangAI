"""哨响AI · 操盘手结论蒸馏层 (operator_output)
============================================
把 _live_predict 的 6 层嵌套巨响应, 蒸馏成操盘手可直接识别的【一行结论 + 三层支撑】。

设计铁律:
  - 顶层嵌套 ≤ 2 层 (card 字段皆为标量 / 长度≤3 的列表)。
  - verdict 永远是一句人话, 不藏字段。
  - evidence 最多 3 条, 每条一句。
  - 纯增量层: 不修改 _live_predict 本体, 只读其返回做蒸馏, 零回归风险。
"""
from typing import Any, Dict, List, Optional


def _confidence(operator_view: Dict, value_layer: Dict, direction: str) -> float:
    """粗略置信度 0-1: 规则命中数 + 非陷阱 + 价值层决策一致。"""
    score = 0.5
    if isinstance(operator_view, dict):
        rc = operator_view.get("rule_count") or 0
        score += min(0.20, 0.04 * rc)
        try:
            ts = float(operator_view.get("trap_score") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        score -= min(0.25, ts / 400.0)  # trap_score 0-100 -> 最多 -0.25
    if isinstance(value_layer, dict):
        dec = value_layer.get("decision")
        if dec == "BET":
            score += 0.10
        elif dec == "PASS":
            score -= 0.05
    return round(max(0.05, min(0.98, score)), 2)


def distill_operator_card(result: Dict[str, Any]) -> Dict[str, Any]:
    """从 _live_predict 完整返回蒸馏出操盘手卡片。

    Returns:
        {
          "verdict":     str   一行结论 (人话)
          "stake":       str   注码建议
          "confidence":  float 0-1 粗略置信
          "evidence":    [str,str,str] 三层支撑 (每条一句)
          "trap_score":  int|None 陷阱评分 0-100
          "decision":    str|None 价值层决策 BET/PASS/...
        }
    """
    ov = result.get("operator_view") or {}
    vl = result.get("value_layer") or {}
    direction = result.get("direction") or (ov.get("verdict") or "")
    verdict = ov.get("verdict") or direction or "无结论"
    stake = ov.get("stake_hint") or vl.get("decision_text") or ""
    trap_score = ov.get("trap_score")
    trap_verdict = ov.get("trap_verdict") or ""

    evidence: List[str] = []
    # 1) 主信号 / 方向
    if direction:
        evidence.append(f"主信号: {direction}")
    # 2) 价值层决策
    dec = vl.get("decision")
    if dec:
        best = vl.get("best_direction") or ""
        edge = vl.get("best_edge_pct")
        edge_s = f"{edge}%" if edge is not None else "--%"
        evidence.append(f"价值层: {dec} {best} (edge {edge_s})")
    # 3) 陷阱 / 风险提示
    if trap_score is not None:
        ev3 = f"陷阱评分: {trap_score}/100"
        if trap_verdict:
            ev3 += f" — {trap_verdict}"
        evidence.append(ev3)
    # 兜底凑满 3 条 (优先平局预警)
    if len(evidence) < 3:
        dp = result.get("draw_signal", {})
        if isinstance(dp, dict) and dp.get("draw_alert"):
            evidence.append("平局预警: 已配防平")
    while len(evidence) < 3:
        evidence.append("--")

    return {
        "verdict": verdict,
        "stake": stake,
        "confidence": _confidence(ov, vl, direction),
        "evidence": evidence[:3],
        "trap_score": trap_score,
        "decision": dec,
    }
