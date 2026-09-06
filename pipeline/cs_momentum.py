"""
cs_momentum.py — CS波胆跟庄策略模块
======================================
分析 CS 波胆赔率变动，输出跟庄信号:

  ▼ 赔率下降 = 庄家认为该结果更可能发生 → "跟庄信号"
  ▲ 赔率上升 = 庄家看衰 → "看衰"
  同向盘口 → 庄家共识
  矛盾信号 → 庄家分歧

输出信号:
  FOLLOW — 多盘同向收敛, 跟庄
  FADE   — 多盘反向发散, 反庄
  WATCH  — 信号模糊, 观望

三色盘口信号 (color_signal):
  GREEN  — 下降 > 3pp
  AMBER  — 变化在 ±3pp 内
  RED    — 上升 > 3pp
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 三色阈值 ──
GREEN_THRESHOLD_PP = 3.0   # 下降 >3pp = 绿色 (看好)
RED_THRESHOLD_PP = 3.0     # 上升 >3pp = 红色 (看衰)


class CSMomentumTracker:
    """CS 波胆跟庄策略追踪器。

    分析波胆赔率变动 (initial_odds → current_odds)，
    输出跟庄/反庄/观望信号。

    用法:
        tracker = CSMomentumTracker()
        signal = tracker.analyze_cs_movement(
            initial_odds={"1-1": 7.60, "1-0": 8.40, ...},
            current_odds={"1-1": 8.00, "1-0": 8.50, ...},
            live_score=(0, 1),
        )
        # signal = {"cs_follow_signal": "FADE", "signals": [...], ...}
    """

    # ── 颜色信号 ──

    @staticmethod
    def color_signal(prob_change_pp: float) -> str:
        """将概率变化 (百分点) 映射为三色信号。

        Args:
            prob_change_pp: 概率变化, 单位百分点 (已乘100).
                           负值=下降(庄家看好), 正值=上升(庄家看衰).

        Returns:
            "GREEN"  下降 > 3pp (庄家看好)
            "AMBER"  ±3pp 内 (中性)
            "RED"    上升 > 3pp (庄家看衰)
        """
        # 修正(2026-08-12 反推复盘): 与图例"▼赔率下降=庄家看好"及
        # _prob_change_pp(正值=概率上升=赔率下降=看好)语义统一 ——
        # 原实现符号整体反转(把赔率下降错判为RED/看衰), 导致2-1现赔1.76暴跌
        # 被误标看衰. 修正: 概率上升(赔率降)=GREEN看好; 概率下降(赔率升)=RED看衰.
        if prob_change_pp > GREEN_THRESHOLD_PP:
            return "GREEN"
        elif prob_change_pp < -RED_THRESHOLD_PP:
            return "RED"
        return "AMBER"

    # ── 赔率→概率转换 ──

    @staticmethod
    def _odds_to_prob(odds: float) -> float:
        """赔率转隐含概率 (无 overround 修正, 用于变动对比)。"""
        if odds <= 0:
            return 0.0
        return 1.0 / odds

    @staticmethod
    def _prob_change_pp(initial_odds: float, current_odds: float) -> float:
        """计算概率变化的百分点 (current_prob - initial_prob) * 100。

        正值 = 概率上升 (庄家看好, 赔率下降)
        负值 = 概率下降 (庄家看衰, 赔率上升)
        """
        ip = 1.0 / initial_odds if initial_odds > 1 else 0
        cp = 1.0 / current_odds if current_odds > 1 else 0
        if ip == 0:
            return 0.0
        return (cp - ip) * 100

    @staticmethod
    def _odds_change_abs(initial_odds: float, current_odds: float) -> float:
        """赔率绝对变动量。"""
        return current_odds - initial_odds

    # ── 主力法 ──

    def analyze_cs_movement(
        self,
        initial_odds: Dict[str, float],
        current_odds: Dict[str, float],
        live_score: Optional[Tuple[int, int]] = None,
    ) -> Dict:
        """分析 CS 波胆赔率变动，输出跟庄信号。

        Args:
            initial_odds:  初盘波胆赔率 {"1-1": 7.60, "1-0": 8.40, ...}
            current_odds:  当前波胆赔率 {"1-1": 8.00, "1-0": 8.50, ...}
            live_score:    当前比分 (home_goals, away_goals), 可选

        Returns:
            {
                "cs_follow_signal": "FOLLOW" | "FADE" | "WATCH",
                "decision_text": str,          # 可读解释
                "signals": [...],               # 逐行信号
                "green_count": int,             # 绿色盘口数 (看好)
                "red_count": int,               # 红色盘口数 (看衰)
                "amber_count": int,             # 中性盘口数
                "divergence_detected": bool,    # 是否检测到庄家分歧
                "dominant_color": str,          # 主导颜色
                "odds_score": int,              # 综合评分 (-100 ~ +100)
            }
        """
        scores = []
        signals_detail = []
        green_count = 0
        red_count = 0
        amber_count = 0

        # 只分析两个数据源都有的比分
        common_scores = set(initial_odds.keys()) & set(current_odds.keys())
        if not common_scores:
            return {
                "cs_follow_signal": "WATCH",
                "decision_text": "无共同波胆数据, 无法分析",
                "signals": [],
                "green_count": 0, "red_count": 0, "amber_count": 0,
                "divergence_detected": False,
                "dominant_color": "AMBER",
                "odds_score": 0,
            }

        for score_key in sorted(common_scores,
                                key=lambda k: self._prob_change_pp(
                                    initial_odds[k], current_odds[k])):
            init_o = initial_odds[score_key]
            curr_o = current_odds[score_key]
            pp = self._prob_change_pp(init_o, curr_o)
            color = self.color_signal(pp)
            delta_odds = self._odds_change_abs(init_o, curr_o)

            if color == "GREEN":
                green_count += 1
            elif color == "RED":
                red_count += 1
            else:
                amber_count += 1

            # 信号方向
            if color == "GREEN":
                direction = "看好"
            elif color == "RED":
                direction = "看衰"
            else:
                direction = "中性"

            signals_detail.append({
                "score": score_key,
                "initial_odds": round(init_o, 2),
                "current_odds": round(curr_o, 2),
                "delta_odds": round(delta_odds, 2),
                "prob_change_pp": round(pp, 2),
                "color": color,
                "direction": direction,
            })

        # ── 同向/矛盾判断 ──
        total = len(signals_detail)
        green_ratio = green_count / total if total > 0 else 0
        red_ratio = red_count / total if total > 0 else 0

        # 是否有矛盾信号: 同时存在绿色和红色 (≥2个) → 庄家分歧
        divergence_detected = (green_count >= 2 and red_count >= 2)

        # 综合评分: 绿色+1, 红色-1, 乘以相对比例
        if total > 0:
            raw_score = (green_count - red_count) / total
        else:
            raw_score = 0
        odds_score = int(round(raw_score * 100))

        # ── 判定主信号 ──
        if divergence_detected:
            # 矛盾和但整体偏绿 → WATCH (有分歧时不急着跟)
            signal = "WATCH"
            if green_count > red_count * 2:
                desc = f"绿色 {green_count} vs 红色 {red_count}: 偏向看好但存在分歧, 建议观望"
            elif red_count > green_count * 2:
                desc = f"红色 {red_count} vs 绿色 {green_count}: 偏向看衰但存在分歧, 建议观望"
            else:
                desc = f"绿色 {green_count} vs 红色 {red_count}: 庄家分歧明显, 不建议入场"
        elif green_count >= total * 0.5:
            # 超过半数是绿色 → 跟庄
            signal = "FOLLOW"
            desc = f"绿色 {green_count}/{total} 盘口被看好, 庄家共识跟庄"
        elif red_count >= total * 0.5:
            # 超过半数是红色 → 反庄
            signal = "FADE"
            desc = f"红色 {red_count}/{total} 盘口被看衰, 庄家共识反庄"
        else:
            signal = "WATCH"
            desc = f"绿{green_count}/红{red_count}/中性{amber_count}: 信号模糊, 建议观望"

        # ── 主导颜色 ──
        if green_count > red_count and green_count > amber_count:
            dominant_color = "GREEN"
        elif red_count > green_count and red_count > amber_count:
            dominant_color = "RED"
        else:
            dominant_color = "AMBER"

        # ── 比分上下文 ──
        if live_score:
            h, a = live_score
            desc = f"[{h}-{a}] {desc}"

        return {
            "cs_follow_signal": signal,
            "decision_text": desc,
            "signals": signals_detail,
            "green_count": green_count,
            "red_count": red_count,
            "amber_count": amber_count,
            "divergence_detected": divergence_detected,
            "dominant_color": dominant_color,
            "odds_score": odds_score,
        }

    # ── 批量分析 (用于飞轮自动扫描) ──

    def batch_analyze(
        self,
        matches: List[Dict],
    ) -> List[Dict]:
        """批量分析多场比赛的 CS 跟庄信号。

        Args:
            matches: [{"home": str, "away": str, "initial_odds": {...},
                       "current_odds": {...}, "live_score": (h,a)}, ...]

        Returns:
            每场比赛追加 cs_follow_signal 等字段的副本列表
        """
        results = []
        for m in matches:
            try:
                analysis = self.analyze_cs_movement(
                    initial_odds=m.get("initial_odds", {}),
                    current_odds=m.get("current_odds", {}),
                    live_score=m.get("live_score"),
                )
                result = dict(m)
                result.update(analysis)
                results.append(result)
            except Exception as e:
                logger.warning(f"[CSMomentum] 分析失败 {m.get('home','?')} vs "
                               f"{m.get('away','?')}: {e}")
                result = dict(m)
                result["cs_follow_signal"] = "WATCH"
                result["decision_text"] = f"分析异常: {e}"
                results.append(result)
        return results


# ── 便捷函数 ──

def analyze_cs_odds_move(
    initial: Dict[str, float],
    current: Dict[str, float],
    score: Optional[Tuple[int, int]] = None,
) -> Dict:
    """快捷函数: 分析 CS 波胆赔率变动。"""
    tracker = CSMomentumTracker()
    return tracker.analyze_cs_movement(initial, current, score)
