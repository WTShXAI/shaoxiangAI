"""
哨响AI - Alpha决策模块
包含: 价值缺口分析器、贝叶斯更新器、Alpha检测门
实现第二至第五章的全部逻辑
"""
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class ValueGapAnalyzer:
    """价值缺口分析器 - 第二章"""

    def __init__(self, alpha_threshold: float = 0.03):
        self.alpha_threshold = alpha_threshold

    def analyze(self, model_probs: Dict[str, float],
                odds: Dict[str, float]) -> Dict:
        """
        完整价值缺口分析
        
        Args:
            model_probs: {'home': 0.55, 'draw': 0.25, 'away': 0.20}
            odds: {'home': 1.85, 'draw': 3.50, 'away': 4.20}
        
        Returns:
            价值分析结果字典
        """
        results = {}

        # 1. 计算市场公平概率（去抽水）
        fair_probs = self._calc_fair_probabilities(odds)
        results['fair_probabilities'] = fair_probs

        # 2. 计算每个选项的价值缺口
        value_gaps = {}
        for outcome in ['home', 'draw', 'away']:
            model_p = model_probs.get(outcome, 0)
            fair_p = fair_probs.get(outcome, 0)
            value_gaps[outcome] = model_p - fair_p
        results['value_gaps'] = value_gaps

        # 3. 投资指标计算
        for outcome in ['home', 'draw', 'away']:
            model_p = model_probs.get(outcome, 0)
            odd = odds.get(outcome, 0)

            if odd > 1.0 and model_p > 0:
                # 凯利比例
                kelly = (model_p * odd - 1) / (odd - 1)
                # 预期价值（每100元）
                ev = model_p * (odd - 1) * 100 - (1 - model_p) * 100
                # 标记价值
                has_alpha = value_gaps[outcome] > self.alpha_threshold
            else:
                kelly = 0.0
                ev = 0.0
                has_alpha = False

            results[f'{outcome}_kelly'] = float(kelly)
            results[f'{outcome}_ev'] = float(ev)
            results[f'{outcome}_has_alpha'] = has_alpha

        # 4. 找出最佳投资选项
        best_outcome = max(value_gaps, key=value_gaps.get)
        best_gap = value_gaps[best_outcome]
        results['best_outcome'] = best_outcome
        results['best_value_gap'] = float(best_gap)
        results['best_kelly'] = results.get(f'{best_outcome}_kelly', 0)
        results['best_ev'] = results.get(f'{best_outcome}_ev', 0)

        # 5. 生成决策报告
        results['report'] = self._generate_report(results, model_probs, odds, fair_probs)

        return results

    def _calc_fair_probabilities(self, odds: Dict[str, float]) -> Dict[str, float]:
        """计算去抽水后的公平概率"""
        total_implied = 0
        implied = {}
        for outcome in ['home', 'draw', 'away']:
            odd = odds.get(outcome, 2.0)
            if odd > 0:
                implied[outcome] = 1.0 / odd
                total_implied += implied[outcome]
            else:
                implied[outcome] = 0.0

        # 去抽水
        fair = {}
        for outcome in ['home', 'draw', 'away']:
            fair[outcome] = implied[outcome] / total_implied if total_implied > 0 else 0.0

        return fair

    def _generate_report(self, results, model_probs, odds, fair_probs) -> str:
        """生成人类可读的决策报告"""
        lines = []
        lines.append("=" * 50)
        lines.append("价值缺口分析报告")
        lines.append("=" * 50)

        for outcome_name, key in [('主胜', 'home'), ('平局', 'draw'), ('客胜', 'away')]:
            gap = results['value_gaps'][key]
            kelly = results.get(f'{key}_kelly', 0)
            ev = results.get(f'{key}_ev', 0)
            alpha = results.get(f'{key}_has_alpha', False)
            lines.append(f"\n{outcome_name}:")
            lines.append(f"  模型概率: {model_probs.get(key, 0):.1%}")
            lines.append(f"  公平概率: {fair_probs.get(key, 0):.1%}")
            lines.append(f"  价值缺口: {gap:+.1%}")
            lines.append(f"  凯利比例: {kelly:.2%}")
            lines.append(f"  预期价值: {ev:+.1f}元/100元")
            lines.append(f"  Alpha信号: {'✓ 通过' if alpha else '✗ 未通过'}")

        best = results['best_outcome']
        outcome_cn = {'home': '主胜', 'draw': '平局', 'away': '客胜'}[best]
        lines.append(f"\n最佳机会: {outcome_cn} (价值缺口: {results['best_value_gap']:+.1%})")

        return "\n".join(lines)


class BayesianUpdater:
    """贝叶斯更新器 - 第四章"""

    def __init__(self):
        self.update_history = []

    def update(self, prior_probs: Dict[str, float],
               evidence: Dict[str, Dict]) -> Dict[str, float]:
        """
        贝叶斯概率更新
        
        Args:
            prior_probs: 先验概率 {'home': 0.55, 'draw': 0.25, 'away': 0.20}
            evidence: 证据字典，每条证据包含:
                - 'likelihood': 该证据在各结果下的似然 {'home': 0.7, 'draw': 0.2, 'away': 0.1}
                - 'confidence': 证据置信度 (0-1)
                - 'description': 证据描述
        
        Returns:
            更新后的概率
        """
        current_probs = prior_probs.copy()

        for ev_name, ev_data in evidence.items():
            likelihood = ev_data.get('likelihood', {})
            confidence = ev_data.get('confidence', 0.5)
            description = ev_data.get('description', '')

            # 应用贝叶斯更新
            new_probs = {}
            total = 0
            for outcome in ['home', 'draw', 'away']:
                prior = current_probs.get(outcome, 1 / 3)
                like = likelihood.get(outcome, 1 / 3)
                # 置信度调整：高置信度证据影响更大
                adjusted_like = like * confidence + (1 - confidence) * 1.0  # 未观测到的证据视为均匀
                posterior = prior * adjusted_like
                new_probs[outcome] = posterior
                total += posterior

            # 归一化
            if total > 0:
                for outcome in new_probs:
                    new_probs[outcome] /= total

            # 记录更新历史
            self.update_history.append({
                'timestamp': datetime.now().isoformat(),
                'evidence': ev_name,
                'description': description,
                'confidence': confidence,
                'before': current_probs.copy(),
                'after': new_probs.copy()
            })

            current_probs = new_probs

        return current_probs

    def create_lineup_evidence(self, home_strength_change: float,
                                away_strength_change: float) -> Dict:
        """创建阵容变化证据"""
        likelihood = {
            'home': 0.5 + home_strength_change * 0.3,
            'draw': 0.2,
            'away': 0.5 + away_strength_change * 0.3
        }
        total = sum(likelihood.values())
        likelihood = {k: v / total for k, v in likelihood.items()}

        return {
            'lineup_change': {
                'likelihood': likelihood,
                'confidence': 0.8,
                'description': f'阵容变化: 主队强度调整{home_strength_change:+.1f}, 客队{away_strength_change:+.1f}'
            }
        }

    def create_market_evidence(self, odds_movement: float) -> Dict:
        """创建市场信号证据"""
        if odds_movement > 0:  # 赔率上升，市场看淡
            likelihood = {'home': 0.3, 'draw': 0.3, 'away': 0.4}
        else:  # 赔率下降，市场看好
            likelihood = {'home': 0.4, 'draw': 0.3, 'away': 0.3}

        return {
            'market_signal': {
                'likelihood': likelihood,
                'confidence': 0.4,
                'description': f'赔率变动: {odds_movement:+.2f}'
            }
        }

    def create_weather_evidence(self, is_rainy: bool, home_technical: bool) -> Dict:
        """创建天气因素证据"""
        if is_rainy and home_technical:
            # 大雨抑制技术流球队
            likelihood = {'home': 0.25, 'draw': 0.40, 'away': 0.35}
        elif is_rainy and not home_technical:
            likelihood = {'home': 0.40, 'draw': 0.35, 'away': 0.25}
        else:
            likelihood = {'home': 0.35, 'draw': 0.30, 'away': 0.35}

        return {
            'weather_factor': {
                'likelihood': likelihood,
                'confidence': 0.6,
                'description': f'天气因素: {"雨天" if is_rainy else "正常"}'
            }
        }

    def get_update_history(self) -> List[Dict]:
        """获取更新历史"""
        return self.update_history.copy()


class AlphaDetectionGate:
    """Alpha检测门 - 第五章"""

    def __init__(self, alpha_threshold: float = 0.03, ev_threshold: float = 0.02,
                 max_invest_ratio: float = 0.05, half_kelly: bool = True):
        self.alpha_threshold = alpha_threshold
        self.ev_threshold = ev_threshold
        self.max_invest_ratio = max_invest_ratio
        self.half_kelly = half_kelly

    def screen(self, value_analysis: Dict, total_capital: float = 10000.0,
               manual_invest_amount: float = None) -> Dict:
        """
        Alpha三道门筛选
        
        Args:
            value_analysis: 价值缺口分析器的输出
            total_capital: 总资金
            manual_invest_amount: 手动指定投资金额（None=自动凯利计算）
        
        Returns:
            筛选结果
        """
        best_outcome = value_analysis.get('best_outcome', 'home')
        best_gap = value_analysis.get('best_value_gap', 0)
        best_kelly = value_analysis.get('best_kelly', 0)
        best_ev = value_analysis.get('best_ev', 0)

        gates = {
            'gate1': {'passed': False, 'details': ''},
            'gate2': {'passed': False, 'details': ''},
            'gate3': {'passed': False, 'details': ''},
        }

        # ===== 第一道门：价值检测 =====
        gate1_checks = []
        gate1_pass = True

        # 价值缺口 > 3%
        if best_gap > self.alpha_threshold:
            gate1_checks.append(f"✓ 价值缺口 {best_gap:.1%} > {self.alpha_threshold:.0%}阈值")
        else:
            gate1_checks.append(f"✗ 价值缺口 {best_gap:.1%} ≤ {self.alpha_threshold:.0%}阈值")
            gate1_pass = False

        # 概率合理性：验证模型预测的三个概率之和是否接近 1.0
        model_probs = value_analysis.get('model_probabilities', {})
        model_probs_sum = sum(model_probs.values()) if model_probs else 1.0
        if abs(model_probs_sum - 1.0) < 0.05:
            gate1_checks.append(f"✓ 概率一致性检查通过 (和={model_probs_sum:.3f})")
        else:
            gate1_checks.append(f"✗ 概率一致性检查未通过 (和={model_probs_sum:.3f})")
            gate1_pass = False

        gates['gate1']['passed'] = gate1_pass
        gates['gate1']['details'] = "\n".join(gate1_checks)

        # ===== 第二道门：收益检测 =====
        gate2_checks = []
        gate2_pass = True

        # 预期价值 > 2元/100元
        if best_ev > self.ev_threshold * 100:
            gate2_checks.append(f"✓ 预期价值 {best_ev:+.1f}元 > {self.ev_threshold * 100:.0f}元阈值")
        else:
            gate2_checks.append(f"✗ 预期价值 {best_ev:+.1f}元 ≤ {self.ev_threshold * 100:.0f}元阈值")
            gate2_pass = False

        # 凯利比例 > 0
        if best_kelly > 0:
            gate2_checks.append(f"✓ 凯利比例 {best_kelly:.2%} > 0")
        else:
            gate2_checks.append(f"✗ 凯利比例 {best_kelly:.2%} ≤ 0")
            gate2_pass = False

        gates['gate2']['passed'] = gate2_pass
        gates['gate2']['details'] = "\n".join(gate2_checks)

        # ===== 第三道门：风险控制 =====
        gate3_checks = []
        gate3_pass = True

        # 半凯利原则（手动输入优先）
        if manual_invest_amount is not None:
            actual_kelly = manual_invest_amount / total_capital if total_capital > 0 else 0
            invest_amount = manual_invest_amount
        else:
            actual_kelly = best_kelly / 2 if self.half_kelly else best_kelly
            invest_amount = total_capital * actual_kelly

        # 单次投资不超过5%（手动输入仅警告，不强制截断）
        max_allowed = total_capital * self.max_invest_ratio
        if manual_invest_amount is not None:
            if invest_amount > max_allowed:
                gate3_checks.append(f"⚠ 手动输入 {invest_amount:.0f}元 超过 {self.max_invest_ratio:.0%}上限({max_allowed:.0f}元)，已采纳手动值")
            else:
                gate3_checks.append(f"✓ 手动投资金额 {invest_amount:.0f}元")
        elif invest_amount <= max_allowed:
            gate3_checks.append(f"✓ 投资金额 {invest_amount:.0f}元 ≤ {self.max_invest_ratio:.0%}上限")
        else:
            invest_amount = max_allowed
            gate3_checks.append(f"⚠ 投资金额已限制为{self.max_invest_ratio:.0%}上限: {invest_amount:.0f}元")

        # 分散投资检查
        gates['gate3']['passed'] = gate3_pass
        gates['gate3']['details'] = "\n".join(gate3_checks)

        # ===== 决策矩阵 =====
        pass_count = sum(1 for g in gates.values() if g['passed'])
        if pass_count == 3:
            decision = 'INVEST'
            confidence = 'HIGH'
        elif pass_count == 2:
            decision = 'WATCH'
            confidence = 'MEDIUM'
        elif pass_count == 1:
            decision = 'WATCH'
            confidence = 'LOW'
        else:
            decision = 'PASS'
            confidence = 'NONE'

        outcome_cn = {'home': '主胜', 'draw': '平局', 'away': '客胜'}

        return {
            'decision': decision,
            'confidence': confidence,
            'gates': gates,
            'pass_count': pass_count,
            'best_outcome': best_outcome,
            'best_outcome_cn': outcome_cn.get(best_outcome, best_outcome),
            'investment_amount': round(invest_amount, 2),
            'kelly_percentage': round(actual_kelly * 100, 2),
            'value_gap': round(best_gap, 4),
            'expected_value': round(best_ev, 2),
            'recommendation': self._generate_recommendation(decision, confidence, best_outcome,
                                                             invest_amount, pass_count)
        }

    def _generate_recommendation(self, decision: str, confidence: str, outcome: str,
                                  amount: float, pass_count: int) -> str:
        """生成投资建议"""
        outcome_cn = {'home': '主胜', 'draw': '平局', 'away': '客胜'}[outcome]
        if decision == 'INVEST':
            return (f"【高置信度投资机会】建议投资{outcome_cn}，金额{amount:.0f}元。")
        elif decision == 'WATCH' and confidence == 'MEDIUM':
            return (f"【中等置信度机会】{outcome_cn}有投资价值，建议小仓位试探。"
                    f"通过{pass_count}/3道门，密切关注后续信息。")
        elif decision == 'WATCH' and confidence == 'LOW':
            return (f"【观察名单】{outcome_cn}仅通过{pass_count}/3道门，暂不建议投资，作为学习案例跟踪。")
        else:
            return f"【放弃投资】未通过Alpha检测门，不建议投资。"
