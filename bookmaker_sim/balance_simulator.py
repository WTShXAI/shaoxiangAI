"""
操盘手平衡模拟器 (Bookmaker Balance Simulator)
=================================================
演示庄家如何在投注窗口中动态调整赔率以平衡风险敞口、保证利润。

核心原理:
  1. 开盘: 基于泊松模型计算公平赔率，施加 8% 抽水
  2. 受注: 公众投注流入，打破平衡
  3. 调盘: 根据风险敞口调整赔率 (降价热方/拉升冷方)
  4. 平衡: 无论赛果如何，庄家都保持正收益

庄家损益公式:
  liability[i] = total_bet_on[i] × (odds[i] - 1)   # 该结果赔付
  revenue = total_bets - liability[winner]           # 实际利润
  overround = Σ(1/odds) - 1                          # 理论利润率

用法:
  python -m bookmaker_sim.balance_simulator --demo
"""
from __future__ import annotations
import math, json, logging
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class OddsState:
    """单步赔率快照"""
    step: int
    label: str           # "开盘" / "受注R1" / "调盘R1" ...
    odds_h: float
    odds_d: float
    odds_a: float
    bets_h: float        # 主胜投注量
    bets_d: float
    bets_a: float
    total_bets: float
    liability_h: float   # 如果主胜需赔付
    liability_d: float
    liability_a: float
    profit_h: float      # 如果主胜的利润
    profit_d: float
    profit_a: float
    expected_profit: float
    overround: float

@dataclass
class BalanceReport:
    """完整平衡报告"""
    match: str = ""
    lambda_h: float = 0.0
    lambda_a: float = 0.0
    target_margin: float = 0.08
    steps: List[OddsState] = field(default_factory=list)
    summary: str = ""

class BookmakerBalanceSimulator:
    """
    操盘手平衡模拟器

    模拟庄家从开盘到临场的完整调盘过程:
      1. 泊松模型 → 公平赔率
      2. 施加抽水 → 开盘赔率
      3. 公众投注 → 风险敞口出现
      4. 动态调盘 → 平衡敞口
      5. 赛前状态 → 确认利润

    庄家目标: 无论赛果，利润 > 0
    """

    def __init__(self, default_margin: float = 0.08):
        self.default_margin = default_margin

    def simulate(self,
                 lambda_h: float = 1.6,
                 lambda_a: float = 1.1,
                 total_pool: float = 100000.0,
                 home_bias: float = 0.45,
                 draw_bias: float = 0.20,
                 away_bias: float = 0.35,
                 steps: int = 4,
                 match: str = "") -> BalanceReport:
        """
        运行平衡模拟

        Args:
            lambda_h: 主队泊松强度
            lambda_a: 客队泊松强度
            total_pool: 总投注池
            home_bias: 公众偏向主胜的比例
            draw_bias: 公众偏向平局的比例
            away_bias: 公众偏向客胜的比例
            steps: 模拟轮数
            match: 比赛名称
        """
        report = BalanceReport(
            match=match, lambda_h=lambda_h, lambda_a=lambda_a,
        )

        # ── Step 0: 计算公平概率 ──
        p_h, p_d, p_a = self._poisson_probabilities(lambda_h, lambda_a)

        # ── Step 1: 开盘 ──
        fair_odds_h = 1.0 / p_h
        fair_odds_d = 1.0 / p_d
        fair_odds_a = 1.0 / p_a

        # 施加 8% 抽水 (非均匀: 平局多抽 3%)
        margin = self.default_margin
        m_h = margin * 0.35     # 主胜抽 35% of total margin
        m_d = margin * 0.40     # 平局抽 40% (庄家对平局最谨慎)
        m_a = margin * 0.25     # 客胜抽 25%

        open_h = fair_odds_h / (1 + m_h)
        open_d = fair_odds_d / (1 + m_d)
        open_a = fair_odds_a / (1 + m_a)

        # ── 受注模拟 ──
        remaining = total_pool
        current_h, current_d, current_a = open_h, open_d, open_a

        for step in range(steps + 1):
            # 当前投注分布 (受赔率影响: 赔率越低越吸引投注)
            chunk = remaining / (steps + 1 - step) if step < steps else 0
            remaining -= chunk

            # 赔率影响: 高赔率 → 高吸引力 → 多投注
            inv_h = 1.0 / current_h
            inv_d = 1.0 / current_d
            inv_a = 1.0 / current_a
            inv_sum = inv_h + inv_d + inv_a

            # 资金分配 = 赔率吸引力 × 公众偏好
            factor_h = (inv_h / inv_sum) * 0.6 + home_bias * 0.4
            factor_d = (inv_d / inv_sum) * 0.6 + draw_bias * 0.4
            factor_a = (inv_a / inv_sum) * 0.6 + away_bias * 0.4
            f_sum = factor_h + factor_d + factor_a

            bet_h = chunk * (factor_h / f_sum) if step < steps else 0
            bet_d = chunk * (factor_d / f_sum) if step < steps else 0
            bet_a = chunk * (factor_a / f_sum) if step < steps else 0

            # 累积投注
            if step == 0:
                cum_h, cum_d, cum_a = 0, 0, 0
            else:
                prev = report.steps[-1]
                cum_h = prev.bets_h + bet_h
                cum_d = prev.bets_d + bet_d
                cum_a = prev.bets_a + bet_a

            total = cum_h + cum_d + cum_a

            # 计算负债和利润
            liab_h = cum_h * (current_h - 1) if cum_h > 0 else 0
            liab_d = cum_d * (current_d - 1) if cum_d > 0 else 0
            liab_a = cum_a * (current_a - 1) if cum_a > 0 else 0

            profit_h = total - liab_h
            profit_d = total - liab_d
            profit_a = total - liab_a

            expected_profit = profit_h * p_h + profit_d * p_d + profit_a * p_a

            overround = (1/current_h + 1/current_d + 1/current_a) - 1

            label = "开盘" if step == 0 else f"受注R{step}" if step < steps else "临场"
            report.steps.append(OddsState(
                step=step, label=label,
                odds_h=current_h, odds_d=current_d, odds_a=current_a,
                bets_h=cum_h, bets_d=cum_d, bets_a=cum_a,
                total_bets=total,
                liability_h=liab_h, liability_d=liab_d, liability_a=liab_a,
                profit_h=profit_h, profit_d=profit_d, profit_a=profit_a,
                expected_profit=expected_profit,
                overround=overround,
            ))

            # ── 调盘 (除最后一步) ──
            if step < steps:
                # 计算当前风险敞口
                # 如果某结果负债过高 → 降价 (降低吸引力)
                worst_case = min(profit_h, profit_d, profit_a)
                if worst_case < 0:
                    # 找出最危险的结果
                    dangers = []
                    if profit_h < 0:
                        dangers.append(('H', abs(profit_h)))
                    if profit_d < 0:
                        dangers.append(('D', abs(profit_d)))
                    if profit_a < 0:
                        dangers.append(('A', abs(profit_a)))
                    dangers.sort(key=lambda x: x[1], reverse=True)

                    for danger, severity in dangers:
                        if danger == 'H':
                            current_h *= 0.92  # 降价 8%
                            # 拉升平局和客胜以吸引资金
                            current_d *= 1.03
                            current_a *= 1.03
                        elif danger == 'D':
                            current_d *= 0.90
                            current_h *= 1.02
                            current_a *= 1.02
                        elif danger == 'A':
                            current_a *= 0.92
                            current_h *= 1.03
                            current_d *= 1.03
                else:
                    # 已平衡，微调
                    current_h *= 0.99
                    current_d *= 0.99
                    current_a *= 0.99

        # ── 生成总结 ──
        final_state = report.steps[-1]
        min_profit = min(final_state.profit_h, final_state.profit_d, final_state.profit_a)

        report.summary = (
            f"初始资金池: ¥{total_pool:,.0f} | "
            f"最终投注: ¥{final_state.total_bets:,.0f} | "
            f"抽水率: {final_state.overround:.1%} | "
            f"最差利润: ¥{min_profit:,.0f}"
            f"{' ✅ 保证盈利' if min_profit > 0 else ' ⚠️ 仍有风险'}"
        )

        return report

    def _poisson_probabilities(self, lh: float, la: float) -> Tuple[float, float, float]:
        """泊松比分分布 → 三分类概率"""
        max_g = 6
        hw = dw = aw = 0.0

        for hg in range(max_g + 1):
            for ag in range(max_g + 1):
                prob = self._poisson_pmf(hg, lh) * self._poisson_pmf(ag, la)
                # Dixon-Coles 低比分修正
                if hg == 0 and ag == 0:
                    prob *= (1 + 0.05)
                elif hg == 0 and ag == 1:
                    prob *= (1 - 0.05)
                elif hg == 1 and ag == 0:
                    prob *= (1 - 0.05)
                elif hg == 1 and ag == 1:
                    prob *= (1 + 0.03)

                if hg > ag:
                    hw += prob
                elif hg == ag:
                    dw += prob
                else:
                    aw += prob

        total = hw + dw + aw
        return hw / total, dw / total, aw / total

    def _poisson_pmf(self, k: int, lam: float) -> float:
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        return math.exp(-lam) * (lam ** k) / math.factorial(k)

    def format_table(self, report: BalanceReport) -> str:
        """生成平衡过程表格"""
        lines = []
        lines.append(f"")
        lines.append(f"  ╔{'═' * 70}╗")
        lines.append(f"  ║  操盘手平衡模拟: {report.match:<40}║")
        lines.append(f"  ║  λ_H={report.lambda_h:.2f}  λ_A={report.lambda_a:.2f}  |  目标抽水率={report.target_margin:.0%}                           ║")
        lines.append(f"  ╠{'═' * 70}╣")
        lines.append(f"  ║ {'阶段':<9} {'主胜赔':>6} {'平局赔':>6} {'客胜赔':>6} {'主胜投注':>10} {'平局投注':>10} {'客胜投注':>10} ║")
        lines.append(f"  ╠{'─' * 70}╣")

        for s in report.steps:
            lines.append(
                f"  ║ {s.label:<9} {s.odds_h:>5.2f}  {s.odds_d:>5.2f}  {s.odds_a:>5.2f}  "
                f"¥{s.bets_h:>8,.0f}  ¥{s.bets_d:>8,.0f}  ¥{s.bets_a:>8,.0f} ║"
            )

        lines.append(f"  ╠{'═' * 70}╣")
        lines.append(f"  ║ {'← 如果赛果为':<18} {'庄家赔付':>14} {'庄家利润':>14} {'利润率':>14} ║")
        lines.append(f"  ╠{'─' * 70}╣")

        final = report.steps[-1]
        for label, liab, prof in [("主胜(H)", final.liability_h, final.profit_h),
                                   ("平局(D)", final.liability_d, final.profit_d),
                                   ("客胜(A)", final.liability_a, final.profit_a)]:
            rate = prof / final.total_bets * 100 if final.total_bets > 0 else 0
            lines.append(
                f"  ║ {label:<18} ¥{liab:>12,.0f}  ¥{prof:>12,.0f}  {rate:>11.1f}% {' ✅' if prof > 0 else ' ❌'} ║"
            )

        lines.append(f"  ╚{'═' * 70}╝")
        lines.append(f"")
        lines.append(f"  📌 结论: {report.summary}")

        return "\n".join(lines)

    def format_analysis(self, report: BalanceReport) -> str:
        """生成操盘分析解读"""
        final = report.steps[-1]
        initial = report.steps[0]
        p_h, p_d, p_a = self._poisson_probabilities(report.lambda_h, report.lambda_a)

        min_profit = min(final.profit_h, final.profit_d, final.profit_a)
        max_profit = max(final.profit_h, final.profit_d, final.profit_a)

        odds_change_h = (final.odds_h - initial.odds_h) / initial.odds_h * 100
        odds_change_d = (final.odds_d - initial.odds_d) / initial.odds_d * 100
        odds_change_a = (final.odds_a - initial.odds_a) / initial.odds_a * 100

        analysis = f"""
  📈 操盘分析

  1. 真实概率 (泊松模型)
     主胜={p_h:.1%}  平局={p_d:.1%}  客胜={p_a:.1%}

  2. 赔率变化 (开盘 → 临场)
     主胜: {initial.odds_h:.2f} → {final.odds_h:.2f} ({odds_change_h:+.1f}%)
     平局: {initial.odds_d:.2f} → {final.odds_d:.2f} ({odds_change_d:+.1f}%)
     客胜: {initial.odds_a:.2f} → {final.odds_a:.2f} ({odds_change_a:+.1f}%)

  3. 投注分布 (临场)
     主胜=¥{final.bets_h:,.0f}({final.bets_h/final.total_bets*100:.0f}%)\t平局=¥{final.bets_d:,.0f}({final.bets_d/final.total_bets*100:.0f}%)\t客胜=¥{final.bets_a:,.0f}({final.bets_a/final.total_bets*100:.0f}%)

  4. 庄家盈亏矩阵
     主胜: 收入¥{final.total_bets:,.0f} - 赔付¥{final.liability_h:,.0f} = ¥{final.profit_h:,.0f}
     平局: 收入¥{final.total_bets:,.0f} - 赔付¥{final.liability_d:,.0f} = ¥{final.profit_d:,.0f}
     客胜: 收入¥{final.total_bets:,.0f} - 赔付¥{final.liability_a:,.0f} = ¥{final.profit_a:,.0f}

  5. 风险评估
     最坏情况: ¥{min_profit:,.0f} | 最好情况: ¥{max_profit:,.0f}
     利润率区间: [{min_profit/final.total_bets*100:.1f}% ~ {max_profit/final.total_bets*100:.1f}%]
     保证盈利: {'✅ 是' if min_profit > 0 else '❌ 否'}
"""
        return analysis

# ═══════════════════════════════════════════════════════════════
# 便捷入口
# ═══════════════════════════════════════════════════════════════

def run_demo(match: str = "巴西 vs 阿根廷 (世界杯)",
             lambda_h: float = 1.60,
             lambda_a: float = 1.10,
             total_pool: float = 100000.0) -> str:
    """运行演示, 返回完整文本输出"""
    sim = BookmakerBalanceSimulator()
    report = sim.simulate(
        lambda_h=lambda_h, lambda_a=lambda_a,
        total_pool=total_pool,
        home_bias=0.48, draw_bias=0.18, away_bias=0.34,
        steps=4, match=match,
    )
    table = sim.format_table(report)
    analysis = sim.format_analysis(report)
    return table + analysis

if __name__ == "__main__":
    print(run_demo())
