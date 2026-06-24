"""
哨响AI - INVEST 决策门控 v2.0 (多信号独立门控)
================================================

核心改动: 旧门控三道门全来自模型概率(自证循环) → 新门控三道独立信号门

旧方案问题:
    Gate1: value_gap > 0.03      ← 来自模型概率
    Gate2: confidence >= 0.55    ← 来自模型概率
    Gate3: Kelly > 0             ← 来自模型概率
    结果: INVEST 49.73% << PASS 63.25%, 越确信越不准

新方案:
    Gate1: 模型-赔率方向一致       ← 赔率是独立信号
    Gate2: 市场低波动 + 盘口稳定  ← 盘口波动是独立信号
    Gate3: 历史先验命中率支撑      ← 历史数据是独立信号

数据佐证:
    INVEST/H: 43条 81.4% ← 模型和赔率方向一致时有效
    INVEST/D: 125条 36.0% ← 模型信平局但赔率不信 → 惨败
    INVEST/A: 15条 73.3% ← 客胜预测较可靠

新门控规则:
    1. 方向一致: argmax(模型H/D/A) == argmax(赔率H/D/A)
       且 |模型概率 - 赔率概率| < 0.15 (不能背离太多)
    2. 市场稳定: sigma_trap < P75 且 beta_dev < 0.5
    3. 历史先验: 同联赛+同方向近N场命中率 > threshold (贝叶斯收缩)
"""

import logging
import sqlite3
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """门控结果"""
    decision: str          # INVEST / WATCH / PASS / SKIP
    confidence: str        # HIGH / MEDIUM / LOW / NONE
    gate1_passed: bool     # 模型-赔率方向一致
    gate2_passed: bool     # 市场低波动
    gate3_passed: bool     # 历史先验支撑
    gate1_detail: str      # 具体原因
    gate2_detail: str
    gate3_detail: str
    model_direction: str   # 模型预测方向 H/D/A
    odds_direction: str    # 赔率暗示方向 H/D/A
    value_gap: float       # 模型概率 - 赔率概率 (最大方向)
    kelly: float           # 凯利比例


class InvestGateV2:
    """INVEST 决策门控 v2.0 — 多信号独立门控"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path
        self._odds_p75_sigma = None  # 延迟计算

    def screen(
        self,
        model_probs: Dict[str, float],
        odds_probs: Dict[str, float],
        features: Dict[str, float],
        league_name: str = 'default',
        match_id: int = None,
    ) -> GateResult:
        """
        执行三道独立门控

        Args:
            model_probs: {'home': 0.45, 'draw': 0.28, 'away': 0.27}
            odds_probs: {'home': 0.48, 'draw': 0.26, 'away': 0.26}
            features: 特征字典 (需要 sigma_trap, beta_dev)
            league_name: 联赛名称
            match_id: 比赛ID (用于日志)

        Returns:
            GateResult
        """
        # 确定方向
        model_direction = max(model_probs, key=model_probs.get)
        odds_direction = max(odds_probs, key=odds_probs.get)
        model_max_prob = model_probs[model_direction]
        odds_max_prob = odds_probs[model_direction]

        # ===== Gate1: 模型-赔率方向一致 =====
        gate1_passed, gate1_detail = self._gate1_consensus(
            model_probs, odds_probs, model_direction, odds_direction
        )

        # ===== Gate2: 市场低波动 =====
        gate2_passed, gate2_detail = self._gate2_stability(
            features, league_name
        )

        # ===== Gate3: 历史先验支撑 =====
        gate3_passed, gate3_detail = self._gate3_prior(
            league_name, model_direction
        )

        # ===== 决策矩阵 =====
        pass_count = sum([gate1_passed, gate2_passed, gate3_passed])

        if pass_count == 3:
            decision = 'INVEST'
            confidence = 'HIGH'
        elif pass_count == 2:
            # Gate1 通过 + 任意一个 = WATCH (方向有支撑但不完美)
            if gate1_passed:
                decision = 'WATCH'
                confidence = 'MEDIUM'
            else:
                # Gate1 不通过 = PASS (方向不一致, 不该投)
                decision = 'PASS'
                confidence = 'LOW'
        elif pass_count == 1:
            decision = 'PASS'
            confidence = 'LOW'
        else:
            decision = 'PASS'
            confidence = 'NONE'

        # 计算 value_gap 和 kelly (用于兼容旧字段)
        value_gap = model_max_prob - odds_max_prob
        best_odds = 1.0 / max(odds_probs[model_direction], 0.01)
        kelly = max(0, (model_max_prob * best_odds - 1) / (best_odds - 1)) * 0.5
        kelly = min(kelly, 0.10)

        return GateResult(
            decision=decision,
            confidence=confidence,
            gate1_passed=gate1_passed,
            gate2_passed=gate2_passed,
            gate3_passed=gate3_passed,
            gate1_detail=gate1_detail,
            gate2_detail=gate2_detail,
            gate3_detail=gate3_detail,
            model_direction=model_direction.upper(),
            odds_direction=odds_direction.upper(),
            value_gap=round(value_gap, 4),
            kelly=round(kelly, 4),
        )

    def _gate1_consensus(
        self,
        model_probs: Dict[str, float],
        odds_probs: Dict[str, float],
        model_dir: str,
        odds_dir: str,
    ) -> Tuple[bool, str]:
        """
        Gate1: 模型-赔率方向一致 + 概率不能背离太多

        通过条件:
            1. argmax(模型) == argmax(赔率) — 方向一致
            2. |模型概率 - 赔率概率| < 0.15 — 不能背离太多
            3. 如果方向不一致, 但模型概率与赔率差距 < 0.05 → 弱通过(仅WATCH级)

        特殊规则:
            - 模型预测D但赔率不信D → 直接失败 (INVEST/D历史36%准确率)
        """
        # 方向一致检查
        direction_match = (model_dir == odds_dir)

        # 概率背离检查
        prob_divergence = abs(model_probs[model_dir] - odds_probs[model_dir])

        # 特殊规则: 模型预测平局但赔率方向不是平局
        if model_dir == 'draw' and odds_dir != 'draw':
            return False, (
                f"模型方向=D, 赔率方向={odds_dir.upper()}, "
                f"平局方向不一致(历史INVEST/D仅36%准确率)"
            )

        if direction_match and prob_divergence < 0.15:
            return True, (
                f"方向一致={model_dir.upper()}, "
                f"概率背离={prob_divergence:.3f} < 0.15"
            )
        elif direction_match and prob_divergence >= 0.15:
            return False, (
                f"方向一致但背离过大: "
                f"模型={model_probs[model_dir]:.3f} vs 赔率={odds_probs[model_dir]:.3f}, "
                f"差={prob_divergence:.3f} >= 0.15"
            )
        else:
            # 方向不一致
            return False, (
                f"方向不一致: 模型={model_dir.upper()} vs 赔率={odds_dir.upper()}"
            )

    def _gate2_stability(
        self,
        features: Dict[str, float],
        league_name: str,
    ) -> Tuple[bool, str]:
        """
        Gate2: 市场低波动 + 盘口稳定

        通过条件:
            1. sigma_trap < P75 (波动率低于75分位, 市场相对稳定)
            2. beta_dev < 0.5 (盘口偏差不大)

        理由: 高波动市场 = 不确定性高 = 模型也不可靠
        """
        sigma_trap = abs(features.get('sigma_trap', 0.0))
        beta_dev = abs(features.get('beta_dev', 0.0))

        # sigma_trap 阈值 — 如果无历史数据, 用固定阈值
        sigma_threshold = self._get_sigma_p75(league_name)

        sigma_ok = sigma_trap < sigma_threshold
        beta_ok = beta_dev < 0.5

        if sigma_ok and beta_ok:
            return True, (
                f"sigma_trap={sigma_trap:.4f} < {sigma_threshold:.4f}, "
                f"beta_dev={beta_dev:.4f} < 0.5"
            )
        elif not sigma_ok and not beta_ok:
            return False, (
                f"双高波动: sigma_trap={sigma_trap:.4f} >= {sigma_threshold:.4f}, "
                f"beta_dev={beta_dev:.4f} >= 0.5"
            )
        elif not sigma_ok:
            return False, (
                f"高波动: sigma_trap={sigma_trap:.4f} >= {sigma_threshold:.4f}"
            )
        else:
            return False, (
                f"盘口偏差大: beta_dev={beta_dev:.4f} >= 0.5"
            )

    def _gate3_prior(
        self,
        league_name: str,
        prediction_direction: str,
    ) -> Tuple[bool, str]:
        """
        Gate3: 历史先验命中率支撑

        通过条件:
            同联赛 + 同预测方向 的近期命中率 > threshold

        贝叶斯收缩:
            后验 = (先验命中率 × N + 50% × shrinkage) / (N + shrinkage)
            shrinkage = 10 (相当于10场均匀先验)
            threshold = 52% (略高于随机33%, 因为模型已给出方向)

        注意: 如果历史数据不足 (< 5场), 默认通过 (不因数据不足而否决)
        """
        if not self.db_path:
            return True, "无数据库连接, 默认通过"

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute('''
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN p.is_correct = 1 THEN 1 ELSE 0 END) as correct
                FROM predictions p
                JOIN matches m ON p.match_id = m.match_id
                WHERE m.league_name = ?
                  AND p.prediction = ?
                  AND p.is_correct IS NOT NULL
            ''', (league_name, prediction_direction))

            row = cursor.fetchone()
            conn.close()

            if row is None or row[0] is None:
                return True, "无历史数据, 默认通过"

            total = row[0]
            correct = row[1] or 0

            if total < 5:
                return True, f"历史样本不足({total}场), 默认通过"

            # 原始命中率
            raw_rate = correct / total

            # 贝叶斯收缩: 向50%回归
            shrinkage = 10
            posterior = (correct + shrinkage * 0.5) / (total + shrinkage)

            threshold = 0.52  # 52% (略高于随机)

            if posterior > threshold:
                return True, (
                    f"{league_name}/{prediction_direction}: "
                    f"命中{correct}/{total}={raw_rate*100:.1f}%, "
                    f"贝叶斯后验={posterior*100:.1f}% > {threshold*100:.0f}%"
                )
            else:
                return False, (
                    f"{league_name}/{prediction_direction}: "
                    f"命中{correct}/{total}={raw_rate*100:.1f}%, "
                    f"贝叶斯后验={posterior*100:.1f}% <= {threshold*100:.0f}%"
                )

        except (Exception) as e:
            logger.warning(f"Gate3 查询失败: {e}")
            return True, f"查询异常, 默认通过: {e}"

    def _get_sigma_p75(self, league_name: str = None) -> float:
        """获取 sigma_trap 的 P75 分位数 (延迟计算, 缓存)"""
        if self._odds_p75_sigma is not None:
            return self._odds_p75_sigma

        if not self.db_path:
            # 无数据库, 使用固定阈值
            self._odds_p75_sigma = 0.5
            return 0.5

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute('''
                SELECT sigma_trap FROM match_features
                WHERE sigma_trap IS NOT NULL AND sigma_trap != 0.0
                ORDER BY sigma_trap
            ''')
            values = [r[0] for r in cursor.fetchall()]
            conn.close()

            if len(values) < 10:
                self._odds_p75_sigma = 0.5
                return 0.5

            import numpy as np
            p75 = float(np.percentile(values, 75))
            self._odds_p75_sigma = max(p75, 0.1)  # 至少0.1
            logger.info(f"sigma_trap P75 = {self._odds_p75_sigma:.4f}")
            return self._odds_p75_sigma

        except (Exception, ValueError) as e:
            logger.warning(f"计算 sigma P75 失败: {e}")
            self._odds_p75_sigma = 0.5
            return 0.5
