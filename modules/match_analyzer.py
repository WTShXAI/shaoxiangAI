"""
哨响AI v4.0 — 全维度比赛分析器 (Match Analyzer)
===================================================
整合: 让球 + 大小球 + 射手榜 + 阵容 + 操盘手操作模拟

数据源:
  - SP数据库 betting_markets (让球/大小球实时赔率)
  - SP数据库 handicap_labels (历史让球标签)
  - modules/scorer_tracker.py (射手榜, Sporting News)
  - bookmaker_sim/balance_simulator.py (操盘手平衡)

核心能力:
  1. 让球覆盖/穿盘概率计算
  2. 大小球预期
  3. 射手榜攻击力对比
  4. 操盘手视角反推: 如果你是庄家, 如何平衡窗口

作者: Architecture v4.0
日期: 2026-06-19
"""
from __future__ import annotations
import sqlite3, logging, math
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SP_DB = 'D:/AI/SP/data/sp_data.db'

# ═══════════════════════════════════════════════════════════════
# 1. 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class HandicapAnalysis:
    """让球分析"""
    line: float = 0.0                 # 让球线 (正=主队让球)
    home_cover_prob: float = 0.0      # 主队穿盘概率
    away_cover_prob: float = 0.0      # 客队穿盘概率
    push_prob: float = 0.0            # 走水概率
    recommendation: str = ""          # 推荐方向
    confidence: str = ""              # 置信度
    historical_cover_rate: float = 0.5  # 历史同盘口穿盘率

@dataclass
class OverUnderAnalysis:
    """大小球分析"""
    line: float = 2.5                 # 大小球线
    over_prob: float = 0.0            # 大球概率
    under_prob: float = 0.0           # 小球概率
    expected_goals: float = 0.0       # 预期总进球
    recommendation: str = ""          # 推荐方向

@dataclass
class BookmakerOperation:
    """操盘手操作模拟"""
    initial_odds: Dict[str, float] = field(default_factory=dict)
    current_liability: Dict[str, float] = field(default_factory=dict)
    suggested_adjustment: str = ""    # 建议调整方向
    risk_zones: List[str] = field(default_factory=list)  # 风险敞口
    guaranteed_profit: float = 0.0    # 保证利润率
    trap_detected: bool = False

@dataclass
class FullMatchReport:
    """完整比赛分析报告"""
    home: str
    away: str
    league: str
    odds_1x2: Dict[str, float]
    handicap: HandicapAnalysis
    over_under: OverUnderAnalysis
    scorer_compare: Dict
    bookmaker_op: BookmakerOperation
    lineup_note: str = ""

# ═══════════════════════════════════════════════════════════════
# 2. 核心分析器
# ═══════════════════════════════════════════════════════════════

class MatchAnalyzer:
    """全维度比赛分析器"""

    def __init__(self):
        self._conn = None
        self._scorer = None

    @property
    def conn(self):
        if self._conn is None:
            self._conn = sqlite3.connect(SP_DB)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    @property  
    def scorer(self):
        if self._scorer is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'scorer_tracker',
                'D:/Architecture v4.0/modules/scorer_tracker.py'
            )
            stmod = importlib.util.module_from_spec(spec)
            import sys
            sys.modules['scorer_tracker'] = stmod
            spec.loader.exec_module(stmod)
            self._scorer = stmod.get_scorer_tracker()
        return self._scorer

    def analyze(self, home: str, away: str, league: str,
                odds: Dict[str, float]) -> FullMatchReport:
        """执行全维度分析"""
        
        # ── 让球分析 ──
        handicap = self._analyze_handicap(home, away, odds)
        
        # ── 大小球分析 ──
        ou = self._analyze_over_under(home, away, odds)
        
        # ── 射手对比 ──
        scorer = self.scorer.compare_attack(home, away)
        
        # ── 操盘手操作模拟 ──
        bookmaker = self._simulate_bookmaker_operation(home, away, odds)
        
        return FullMatchReport(
            home=home, away=away, league=league,
            odds_1x2=odds,
            handicap=handicap,
            over_under=ou,
            scorer_compare=scorer,
            bookmaker_op=bookmaker,
            lineup_note="阵容数据需从SP/worldcup_match_live接入"
        )

    # ═══════════════════════════════════════════════════════════
    # 让球分析
    # ═══════════════════════════════════════════════════════════

    def _analyze_handicap(self, home: str, away: str,
                          odds: Dict[str, float]) -> HandicapAnalysis:
        """让球盘口分析"""
        ha = HandicapAnalysis()
        
        # 从赔率反推让球线
        oh, od, oa = odds.get('home', 2.5), odds.get('draw', 3.2), odds.get('away', 2.8)
        inv_sum = 1/oh + 1/od + 1/oa
        ph, pd, pa = (1/oh)/inv_sum, (1/od)/inv_sum, (1/oa)/inv_sum
        
        # 实力差 → 让球线 (简化公式)
        spread = ph - pa
        if spread > 0.40:
            ha.line = -1.5  # 主队让1.5球
        elif spread > 0.25:
            ha.line = -1.0
        elif spread > 0.15:
            ha.line = -0.75
        elif spread > 0.08:
            ha.line = -0.5
        elif spread > 0.03:
            ha.line = -0.25
        elif spread > -0.03:
            ha.line = 0.0   # 平手盘
        elif spread > -0.08:
            ha.line = 0.25
        elif spread > -0.15:
            ha.line = 0.5
        elif spread > -0.25:
            ha.line = 0.75
        elif spread > -0.40:
            ha.line = 1.0
        else:
            ha.line = 1.5   # 客队让1.5球

        # 穿盘概率 (基于历史同盘口统计)
        try:
            row = self.conn.execute(
                'SELECT home_cover_rate, away_cover_rate, push_rate, n_samples '
                'FROM handicap_depth_profile WHERE handicap_bin = ?',
                [f'{ha.line:+.2f}']
            ).fetchone()
            if row and row['n_samples'] > 10:
                ha.home_cover_prob = row['home_cover_rate']
                ha.away_cover_prob = row['away_cover_rate']
                ha.push_prob = row['push_rate']
                ha.historical_cover_rate = row['home_cover_rate']
        except Exception:
            # 默认: 让球方55%穿盘
            pass
        
        # 如果没有历史数据, 用模型估算
        if ha.home_cover_prob == 0:
            if ha.line < 0:  # 主队让球
                ha.home_cover_prob = 0.48
                ha.away_cover_prob = 0.42
                ha.push_prob = 0.10
            elif ha.line > 0:  # 客队让球
                ha.home_cover_prob = 0.42
                ha.away_cover_prob = 0.48
                ha.push_prob = 0.10
            else:
                ha.home_cover_prob = 0.45
                ha.away_cover_prob = 0.45
                ha.push_prob = 0.10

        # 推荐
        if ha.home_cover_prob > 0.50:
            ha.recommendation = f"主队穿盘({ha.line:+.2f})"
            ha.confidence = "medium" if ha.home_cover_prob > 0.55 else "low"
        elif ha.away_cover_prob > 0.50:
            ha.recommendation = f"客队穿盘({-ha.line:+.2f})"
            ha.confidence = "medium" if ha.away_cover_prob > 0.55 else "low"
        else:
            ha.recommendation = "让球盘无明确方向"
            ha.confidence = "low"

        return ha

    # ═══════════════════════════════════════════════════════════
    # 大小球分析
    # ═══════════════════════════════════════════════════════════

    def _analyze_over_under(self, home: str, away: str,
                            odds: Dict[str, float]) -> OverUnderAnalysis:
        """大小球分析"""
        oua = OverUnderAnalysis()
        
        oh, od, oa = odds.get('home', 2.5), odds.get('draw', 3.2), odds.get('away', 2.8)
        inv_sum = 1/oh + 1/od + 1/oa
        ph, pd, pa = (1/oh)/inv_sum, (1/od)/inv_sum, (1/oa)/inv_sum
        
        # 从赔率反推预期进球 (泊松反推)
        lambda_h = self._prob_to_lambda(ph)
        lambda_a = self._prob_to_lambda(pa)
        total_lambda = lambda_h + lambda_a
        
        oua.expected_goals = total_lambda
        oua.line = 2.5  # 默认
        
        # 大小球概率
        over_prob = 1.0 - self._poisson_cdf(2, total_lambda)  # P(≥3球)
        oua.over_prob = over_prob
        oua.under_prob = 1.0 - over_prob
        
        if over_prob > 0.55:
            oua.recommendation = f"大球{oua.line} ({over_prob:.0%})"
        elif over_prob < 0.45:
            oua.recommendation = f"小球{oua.line} ({(1-over_prob):.0%})"
        else:
            oua.recommendation = f"大小球{oua.line}均衡"
        
        return oua

    def _prob_to_lambda(self, p: float) -> float:
        """概率 → 泊松λ (粗略反推)"""
        if p <= 0:
            return 0.3
        return max(0.2, -math.log(max(0.001, 1 - p)) * 0.7)

    def _poisson_cdf(self, k: int, lam: float) -> float:
        """泊松累计分布 P(X ≤ k)"""
        cdf = 0
        term = math.exp(-lam)
        for i in range(k + 1):
            cdf += term
            term *= lam / (i + 1)
        return cdf

    # ═══════════════════════════════════════════════════════════
    # 操盘手操作模拟
    # ═══════════════════════════════════════════════════════════

    def _simulate_bookmaker_operation(self, home: str, away: str,
                                       odds: Dict[str, float]) -> BookmakerOperation:
        """操盘手视角: 如果你在窗口操作赔率, 你会怎么调?"""
        bo = BookmakerOperation(initial_odds=odds.copy())
        
        oh, od, oa = odds.get('home', 2.5), odds.get('draw', 3.2), odds.get('away', 2.8)
        inv_sum = 1/oh + 1/od + 1/oa
        
        # ── 1. 计算当前窗口盈亏矩阵 (假设¥100K池) ──
        pool = 100000
        # 公众投注倾向 (主场偏好多5%)
        bet_h = pool * ((1/oh)/inv_sum * 0.55)
        bet_d = pool * ((1/od)/inv_sum * 0.45)
        bet_a = pool * ((1/oa)/inv_sum * 0.45)
        total_bets = bet_h + bet_d + bet_a
        # 归一化
        bet_h = bet_h / (bet_h+bet_d+bet_a) * pool
        bet_d = bet_d / (bet_h+bet_d+bet_a) * pool
        bet_a = bet_a / (bet_h+bet_d+bet_a) * pool
        
        # 庄家负债
        liab_h = bet_h * (oh - 1)
        liab_d = bet_d * (od - 1)
        liab_a = bet_a * (oa - 1)
        
        # 三种结果下的利润
        profit_h = total_bets - liab_h
        profit_d = total_bets - liab_d
        profit_a = total_bets - liab_a
        
        min_profit = min(profit_h, profit_d, profit_a)
        bo.guaranteed_profit = min_profit / total_bets
        
        bo.current_liability = {
            'H': round(liab_h), 'D': round(liab_d), 'A': round(liab_a),
        }
        
        # ── 2. 识别风险敞口 ──
        max_liability = max(liab_h, liab_d, liab_a)
        if liab_h > max_liability * 0.8:
            bo.risk_zones.append(f"主胜赔付风险高: ¥{liab_h:,.0f}")
        if liab_d > max_liability * 0.8:
            bo.risk_zones.append(f"平局赔付风险高: ¥{liab_d:,.0f}")
        if liab_a > max_liability * 0.8:
            bo.risk_zones.append(f"客胜赔付风险高: ¥{liab_a:,.0f}")
        
        # ── 3. 操盘手操作建议 ──
        overround = inv_sum - 1
        if min_profit < pool * 0.02:  # 利润率<2%
            bo.trap_detected = True
            # 找出最危险的结果, 建议降价
            worst = max([('H', liab_h), ('D', liab_d), ('A', liab_a)], key=lambda x: x[1])
            bo.suggested_adjustment = (
                f"⚠️ 利润率仅{bo.guaranteed_profit:.1%}, 建议降价{worst[0]}端(当前赔付¥{worst[1]:,.0f})。"
                f"同时拉升非热门端吸引资金。"
            )
        elif overround > 0.10:
            bo.suggested_adjustment = f"抽水偏高({overround:.1%}), 可微降吸引投注"
        else:
            bo.suggested_adjustment = f"窗口平衡, 利润保证{bo.guaranteed_profit:.1%}, 维持现赔率"
        
        return bo

    def format_report(self, report: FullMatchReport) -> str:
        """格式化为6层AI报告兼容输出"""
        lines = []
        
        # ── 让球 ──
        ha = report.handicap
        line_str = f'主{ha.line:+.2f}' if ha.line < 0 else (f'客{ha.line:+.2f}' if ha.line > 0 else '平手')
        lines.append(f"\n{'─' * 40}")
        lines.append(f"⚽ 让球盘分析 (AH)")
        lines.append(f"  盘口: {line_str}")
        lines.append(f"  穿盘: 主{ha.home_cover_prob:.0%} / 客{ha.away_cover_prob:.0%} / 走水{ha.push_prob:.0%}")
        lines.append(f"  推荐: {ha.recommendation} [{ha.confidence}]")
        
        # ── 大小球 ──
        ou = report.over_under
        lines.append(f"\n{'─' * 40}")
        lines.append(f"🥅 大小球分析 (O/U)")
        lines.append(f"  盘口: {ou.line}球")
        lines.append(f"  预期进球: {ou.expected_goals:.2f}")
        lines.append(f"  大球: {ou.over_prob:.0%} / 小球: {ou.under_prob:.0%}")
        lines.append(f"  推荐: {ou.recommendation}")
        
        # ── 射手 ──
        sc = report.scorer_compare
        lines.append(f"\n{'─' * 40}")
        lines.append(f"🎯 射手榜对比")
        lines.append(f"  {sc.get('home_summary', report.home)}")
        lines.append(f"  {sc.get('away_summary', report.away)}")
        lines.append(f"  优势: {sc.get('advantage', 'N/A')}")
        
        # ── 操盘手操作 ──
        bo = report.bookmaker_op
        lines.append(f"\n{'─' * 40}")
        lines.append(f"🏦 操盘手操作模拟")
        lines.append(f"  假设资金池: ¥100,000")
        lines.append(f"  负债矩阵: H=¥{bo.current_liability.get('H',0):,.0f} D=¥{bo.current_liability.get('D',0):,.0f} A=¥{bo.current_liability.get('A',0):,.0f}")
        lines.append(f"  保证利润: {bo.guaranteed_profit:.1%}")
        if bo.risk_zones:
            for rz in bo.risk_zones:
                lines.append(f"  ⚠ {rz}")
        lines.append(f"  操盘建议: {bo.suggested_adjustment}")
        if bo.trap_detected:
            lines.append(f"  🔴 陷阱检测: 薄利窗口, 庄家可能诱盘")
        
        # ── 阵容 ──
        lines.append(f"\n{'─' * 40}")
        lines.append(f"👥 首发阵容: {report.lineup_note}")
        
        return "\n".join(lines)

# 单例
_analyzer: Optional[MatchAnalyzer] = None

def get_match_analyzer() -> MatchAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = MatchAnalyzer()
    return _analyzer
