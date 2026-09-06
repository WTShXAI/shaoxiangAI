"""
reading_methods.py — 具体「读片方法」(深度方向假说)

每个方法消费一份 OddsReport(初盘 1X2 + 可选 AH/OU), 返回 {home,draw,away} 概率。
它们都在 diagnostic_probe.py 的真 OOS 考卷上被阅卷, 目标是 > 市场基线 54.85%。

诚实预期 (与既有结论一致): 单张 opening 快照里的三个市场(1X2/AH/OU)都是同一份
"集体二诊"的不同投影, 互相融合很难真正超越市场本身 → 多数方法会落在 ~54.85%。
这正是诊断要证明的: 在"同一份报告"上做更聪明的解算 ≠ 提高自身;
真杠杆在 opening→live 漂移 / 跨庄分歧 / 独立信息(阵容新闻), 那才是下一步研究目标。

方法清单:
  - MarketConsensus   : 押隐含概率最高方 (= 市场基线, 不读片, 仅对照)
  - DrawFadeReader    : 平局被超买假说 → 压低 draw 概率 (favorite-longshot 类偏置)
  - TriangulatedReader: 1X2 + AH + OU 三市场三角融合 (内部一致性第二意见)
  - OuGoalLeanReader  : 高 over 概率 → 开放局, 向主队/平局倾斜
"""
from __future__ import annotations

from typing import Dict, Optional

from pipeline.diagnostic_probe import OddsReport, SIDES


def _devig_two(home_o: float, away_o: float) -> float:
    """双边市场去水 → 主方隐含概率。"""
    ih, ia = 1.0 / home_o, 1.0 / away_o
    return ih / (ih + ia)


def _ah_home_win_view(report: OddsReport) -> Optional[float]:
    """把 AH 盘口翻译成可对照的'主胜概率视图'。

    仅在半球线(干净映射)时可靠; 其他线做近似并标注。
    返回 None 表示 AH 不可用。
    """
    if report.ah_home_odds is None or report.ah_away_odds is None or report.ah_line is None:
        return None
    p_cover = _devig_two(report.ah_home_odds, report.ah_away_odds)  # P(主队让球覆盖)
    L = report.ah_line
    # 主队视角: 负线=让球(主强), 正线=受让(主弱)
    if L <= -0.5 and L > -1.0:        # -0.5: 覆盖 ⟺ 主胜
        return p_cover
    if L >= 0.5 and L < 1.0:          # +0.5: 覆盖 ⟺ 主胜或平 → 减回平局
        imp = report.implied
        return max(0.0, p_cover - imp["draw"])
    if L == 0.0:                      # 平手: 覆盖 ⟺ 主胜
        return p_cover
    # 其他线(-1/-1.5/+1 等): 覆盖 ≠ 主胜, 近似用 p_cover 作下界信号, 不直接返回可比视图
    return None


class MarketConsensus:
    """市场基线: 押隐含概率最高方。不读片, 仅作对照锚。"""

    name = "market_consensus"

    def predict(self, report: OddsReport) -> Dict[str, float]:
        out = {s: 0.0 for s in SIDES}
        out[report.favorite] = 1.0
        return out


class DrawFadeReader:
    """平局超买假说: 散户爱买平局把 draw 赔率压低 → draw 真实概率低于隐含。
    压低 draw 概率后归一, 预测 argmax。若真有此偏置, 应略优于市场基线。"""

    def __init__(self, fade: float = 0.85):
        self.fade = fade
        self.name = f"draw_fade_{fade}"

    def predict(self, report: OddsReport) -> Dict[str, float]:
        imp = report.implied
        d = imp["draw"] * self.fade
        h, a = imp["home"], imp["away"]
        tot = h + d + a
        return {s: v / tot for s, v in zip(SIDES, (h, d, a))}


class TriangulatedReader:
    """三市场三角融合: 把 AH 翻译成主胜视图, 与 1X2 主胜概率做等权平均,
    再叠加 OU 对 draw 的修正(高 over → 开放局 → 减 draw)。预测 argmax。

    诚实声明: 三个市场同源(同一份集体二诊), 融合大概率收敛到市场基线附近,
    但若有内部不一致(1X2 与 AH 对主队强度看法分歧大), 融合可能捕捉到一点增量。
    """

    name = "triangulated"

    def predict(self, report: OddsReport) -> Dict[str, float]:
        imp = report.implied
        h, d, a = imp["home"], imp["draw"], imp["away"]

        # AH 第二意见
        ah_view = _ah_home_win_view(report)
        if ah_view is not None:
            h = 0.5 * (h + ah_view)  # 等权平均主胜视图

        # OU 对 draw 的修正: 高 over 概率 → 进攻开放 → 减 draw
        if report.ou_over_odds is not None and report.ou_under_odds is not None:
            p_over = _devig_two(report.ou_over_odds, report.ou_under_odds)
            # p_over>0.5 减 draw, <0.5 加 draw; 幅度温和(0.15)
            d = d * (1.0 - 0.15 * (p_over - 0.5) * 2.0)

        tot = h + d + a
        return {s: v / tot for s, v in zip(SIDES, (h, d, a))}


class OuGoalLeanReader:
    """进球预期倾斜: 高 over 概率 → 主队主场进攻加成 → 向主队倾斜;
    低 over → 闷局 → 向平局/客队倾斜。幅度温和。"""

    name = "ou_goal_lean"

    def predict(self, report: OddsReport) -> Dict[str, float]:
        imp = report.implied
        h, d, a = imp["home"], imp["draw"], imp["away"]
        if report.ou_over_odds is None or report.ou_under_odds is None:
            return {s: imp[s] for s in SIDES}
        p_over = _devig_two(report.ou_over_odds, report.ou_under_odds)
        tilt = 0.1 * (p_over - 0.5) * 2.0  # [-0.1, +0.1]
        h = h * (1.0 + tilt)
        a = a * (1.0 - tilt)
        tot = h + d + a
        return {s: v / tot for s, v in zip(SIDES, (h, d, a))}


# 注册表 (供脚本遍历)
REGISTRY = {
    "market": MarketConsensus,
    "draw_fade": DrawFadeReader,
    "triangulated": TriangulatedReader,
    "ou_goal_lean": OuGoalLeanReader,
}
