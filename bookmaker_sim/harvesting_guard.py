"""
哨响AI — 庄家收割防护墙 (HarvestingGuard) v1.0
===============================================

核心功能:
  1. 赛前赔率异常扫描 (Odds Anomaly Scanner)
  2. 收割风险评分 (Harvesting Risk Score, HRS)  
  3. 尾端风险调整 (Tail-Risk Adjustment)
  4. 联赛级基线校准 (League Baseline Calibrator)

设计目标:
  防止模型在"庄家知情收割"场景下给出过度自信的错误预测。
  美国 vs 巴拉圭 4-1 的教训: 赛前 U2@2.85 溢价13% 是明确收割信号,
  但模型未识别, 预测 2-1 严重低估了真实赛果。

三层防护:
  L1 — 赔率指纹扫描: 检测单个盘口的异常溢价
  L2 — 交叉盘口一致性: 检测多个盘口间的矛盾信号  
  L3 — 尾端风险调整: 当收割信号高时, 扩大预测分布尾部

用法:
  guard = HarvestingGuard()
  report = guard.scan(odds_1x2={'home': 2.10, 'draw': 3.20, 'away': 3.75},
                      odds_totals={'line': 2.0, 'over': 1.88, 'under': 2.85},
                      league='世界杯')
  if report.hrs > 0.5:
      adjusted = guard.adjust_prediction(base_probs, report)
"""

import sys, os, logging, sqlite3
import numpy as np
from typing import Dict, Tuple, Optional, List, Any
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
# 数据类
# ══════════════════════════════════════════════════

@dataclass
class OddsAnomaly:
    """单个盘口异常"""
    market: str           # '1x2' | 'totals' | 'ah' | 'cs'
    dimension: str        # 'premium' | 'suppression' | 'gap' | 'inconsistency'
    severity: float       # 0-1
    description: str
    fair_value: float = 0.0
    actual_value: float = 0.0
    premium_pct: float = 0.0

@dataclass
class HarvestingReport:
    """收割风险报告"""
    # ── 综合评分 ──
    hrs: float = 0.0              # Harvesting Risk Score (0-1)
    confidence: float = 0.0       # 检测置信度
    risk_level: str = "LOW"       # LOW | MEDIUM | HIGH | CRITICAL
    
    # ── 分维度信号 ──
    signal_1x2: float = 0.0
    signal_totals: float = 0.0
    signal_ah: float = 0.0
    signal_cs: float = 0.0
    signal_cross_market: float = 0.0  # 交叉盘口矛盾
    
    # ── 异常列表 ──
    anomalies: List[OddsAnomaly] = field(default_factory=list)
    
    # ── 资金流向推断 ──
    baited_direction: str = ""      # 引诱方向
    suppressed_direction: str = ""  # 抑制方向 (真实方向?)
    
    # ── 尾端风险 ──
    tail_risk_factor: float = 0.0   # 尾端风险系数 (0-1)
    extreme_score_prob: float = 0.0 # 极端比分概率 (总进球>=4)
    
    # ── 建议 ──
    recommendation: str = ""
    actionable: bool = False

@dataclass
class AdjustedPrediction:
    """尾端调整后的预测"""
    base_probs: Dict[str, float]      # 原始 H/D/A
    adjusted_probs: Dict[str, float]  # 调整后 H/D/A
    confidence_discount: float        # 置信度折扣
    tail_scenarios: List[Dict]        # 尾端场景
    adjustment_note: str

# ══════════════════════════════════════════════════
# 联赛基线
# ══════════════════════════════════════════════════

# 各联赛的历史统计 (从 odds_features 300K+ 数据中提炼)
LEAGUE_BASELINES = {
    'default': {
        'avg_total_goals': 2.75, 'home_win_rate': 0.45, 'draw_rate': 0.26,
        'avg_overround': 0.065, 'totals_std': 0.12,
        'extreme_score_rate': 0.08,  # 总进球>=4 的概率
    },
    '英超': {
        'avg_total_goals': 2.85, 'home_win_rate': 0.44, 'draw_rate': 0.25,
        'avg_overround': 0.058, 'totals_std': 0.10,
        'extreme_score_rate': 0.10,
    },
    '西甲': {
        'avg_total_goals': 2.60, 'home_win_rate': 0.47, 'draw_rate': 0.26,
        'avg_overround': 0.060, 'totals_std': 0.11,
        'extreme_score_rate': 0.07,
    },
    '世界杯': {
        'avg_total_goals': 2.55, 'home_win_rate': 0.43, 'draw_rate': 0.28,
        'avg_overround': 0.055, 'totals_std': 0.09,
        'extreme_score_rate': 0.06,  # 世界杯通常更保守
    },
}

# 极端比分阈值
EXTREME_SCORE_THRESHOLD = 4  # 总进球 >= 4 视为极端

class HarvestingGuard:
    """
    庄家收割防护墙
    
    三层防护:
      L1: 赔率指纹扫描 → 检测单盘口异常溢价
      L2: 交叉盘口一致性 → 检测多盘口间矛盾
      L3: 尾端风险调整 → 扩大预测分布尾部
    """
    
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'football_data.db'
        )
        self._league_cache: Dict[str, Dict] = {}
        self._anomaly_thresholds = {
            'premium': 0.10,      # 10% 溢价 = 可疑
            'extreme_premium': 0.20,  # 20% 溢价 = 极度可疑
            'suppression': 0.08,  # 8% 抑制 = 可疑
            'cross_market_gap': 0.12,  # 12% 交叉矛盾
        }
    
    # ═══════════════════════════════════════════════
    # 主入口: 赛前扫描
    # ═══════════════════════════════════════════════
    
    def scan(self,
             odds_1x2: Optional[Dict[str, float]] = None,
             odds_totals: Optional[Dict[str, float]] = None,
             odds_ah: Optional[Dict[str, float]] = None,
             odds_cs: Optional[Dict[Tuple[int, int], float]] = None,
             league: Optional[str] = None,
             model_total_lambda: Optional[float] = None) -> HarvestingReport:
        """
        赛前赔率扫描 — 检测收割信号
        
        Args:
            odds_1x2: {'home': 2.10, 'draw': 3.20, 'away': 3.75}
            odds_totals: {'line': 2.0, 'over': 1.88, 'under': 2.85}
            odds_ah: {'line': -0.5, 'home_cover': 2.12, 'away_cover': 1.81}
            odds_cs: {(1,0): 5.90, (0,0): 7.80, ...}
            league: 联赛名 (用于基线对比)
            model_total_lambda: 模型预测的总进球期望值 (λ_h + λ_a)
               — 关键参数! 用模型自己的预期来检测偏差
                如果提供, _scan_totals 会用此 λ 计算真实 U/O 概率
                如果不提供, 回退到联赛基线 (可能误判)
        
        Returns:
            HarvestingReport 完整风险报告
        """
        report = HarvestingReport()
        league_baseline = self._get_league_baseline(league)
        
        # ── L1: 各维度独立扫描 ──
        if odds_1x2:
            report.signal_1x2, anomalies_1x2 = self._scan_1x2(odds_1x2, league_baseline)
            report.anomalies.extend(anomalies_1x2)
        
        if odds_totals:
            report.signal_totals, anomalies_totals = self._scan_totals(
                odds_totals, league_baseline, model_total_lambda
            )
            report.anomalies.extend(anomalies_totals)
        
        if odds_ah:
            report.signal_ah, anomalies_ah = self._scan_ah(odds_ah, odds_1x2, league_baseline)
            report.anomalies.extend(anomalies_ah)
        
        if odds_cs:
            report.signal_cs, anomalies_cs = self._scan_cs(odds_cs, league_baseline)
            report.anomalies.extend(anomalies_cs)
        
        # ── L2: 交叉盘口一致性 ──
        if odds_1x2 and (odds_totals or odds_ah):
            report.signal_cross_market = self._scan_cross_market(
                odds_1x2, odds_totals, odds_ah, league_baseline
            )
        
        # ── 综合 HRS ──
        report.hrs, report.confidence = self._compute_hrs(report)
        
        # ── 风险等级 ──
        report.risk_level = self._classify_risk(report.hrs, report.confidence)
        
        # ── 资金流向推断 ──
        report.baited_direction, report.suppressed_direction = \
            self._infer_money_flow(odds_1x2, odds_totals, odds_ah)
        
        # ── 尾端风险 ──
        report.tail_risk_factor = self._compute_tail_risk(report, odds_totals, league_baseline)
        report.extreme_score_prob = self._estimate_extreme_prob(
            report, odds_1x2, odds_totals, league_baseline
        )
        
        # ── 建议 ──
        report.recommendation, report.actionable = self._generate_recommendation(report)
        
        return report
    
    # ═══════════════════════════════════════════════
    # L1: 单盘口扫描
    # ═══════════════════════════════════════════════
    
    def _scan_1x2(self, odds: Dict[str, float], baseline: Dict) -> Tuple[float, List[OddsAnomaly]]:
        """
        1X2 赔率扫描
        
        检测:
          1. 是否有异常溢价 (某个方向的赔率被故意抬高以引诱)
          2. 是否有异常抑制 (真实方向的赔率被压低)
          3. overround 是否异常
        """
        anomalies = []
        h, d, a = odds.get('home', 0), odds.get('draw', 0), odds.get('away', 0)
        if h <= 0 or d <= 0 or a <= 0:
            return 0.0, []
        
        # 去抽水 → 隐含概率
        imp_h = 1.0 / h
        imp_d = 1.0 / d
        imp_a = 1.0 / a
        total_imp = imp_h + imp_d + imp_a
        
        fair_h = imp_h / total_imp
        fair_d = imp_d / total_imp
        fair_a = imp_a / total_imp
        
        # 公平赔率
        fair_h_odds = 1.0 / fair_h
        fair_d_odds = 1.0 / fair_d
        fair_a_odds = 1.0 / fair_a
        
        # overround 检测
        overround = total_imp - 1.0
        expected_ovr = baseline.get('avg_overround', 0.065)
        
        signal_score = 0.0
        
        # 检测1: 赔率溢价 (高于公平赔率 = 引诱)
        premiums = {
            'Home': (h - fair_h_odds) / fair_h_odds,
            'Draw': (d - fair_d_odds) / fair_d_odds,
            'Away': (a - fair_a_odds) / fair_a_odds,
        }
        
        max_premium_dir = max(premiums, key=premiums.get)
        max_premium = premiums[max_premium_dir]
        
        if max_premium > self._anomaly_thresholds['premium']:
            severity = min(1.0, max_premium / 0.25)  # 25%溢价 = 满分
            anomalies.append(OddsAnomaly(
                market='1x2',
                dimension='premium',
                severity=severity,
                description=f'{max_premium_dir}赔率溢价{max_premium*100:.0f}%，疑似引诱投注',
                fair_value=round({'Home': fair_h_odds, 'Draw': fair_d_odds, 'Away': fair_a_odds}[max_premium_dir], 2),
                actual_value=round({'Home': h, 'Draw': d, 'Away': a}[max_premium_dir], 2),
                premium_pct=round(max_premium * 100, 1),
            ))
            signal_score = max(signal_score, severity)
        
        # 检测2: 极端溢价
        if max_premium > self._anomaly_thresholds['extreme_premium']:
            signal_score = max(signal_score, 0.8)
        
        # 检测3: 赔率抑制 (低于公平赔率 = 庄家压低真实方向)
        min_premium_dir = min(premiums, key=premiums.get)
        min_premium = premiums[min_premium_dir]
        
        if min_premium < -self._anomaly_thresholds['suppression']:
            severity = min(1.0, abs(min_premium) / 0.15)
            anomalies.append(OddsAnomaly(
                market='1x2',
                dimension='suppression',
                severity=severity,
                description=f'{min_premium_dir}赔率被抑制{abs(min_premium)*100:.0f}%，可能是真实方向',
                fair_value=round({'Home': fair_h_odds, 'Draw': fair_d_odds, 'Away': fair_a_odds}[min_premium_dir], 2),
                actual_value=round({'Home': h, 'Draw': d, 'Away': a}[min_premium_dir], 2),
                premium_pct=round(min_premium * 100, 1),
            ))
            signal_score = max(signal_score, severity * 0.7)  # 抑制信号稍弱
        
        # 检测4: 异常 overround (>2x league avg)
        if overround > expected_ovr * 1.8:
            severity = min(1.0, (overround / expected_ovr - 1) / 1.5)
            anomalies.append(OddsAnomaly(
                market='1x2',
                dimension='premium',
                severity=severity,
                description=f'抽水率异常偏高: {overround*100:.1f}% (联赛均值 {expected_ovr*100:.1f}%)',
            ))
            signal_score = max(signal_score, severity * 0.5)
        
        return min(1.0, signal_score), anomalies
    
    def _scan_totals(self, odds: Dict[str, float], baseline: Dict,
                    model_total_lambda: Optional[float] = None) -> Tuple[float, List[OddsAnomaly]]:
        """
        大小球赔率扫描  (v1.2: 支持 model_total_lambda)
        
        核心逻辑:
          - 如果提供了 model_total_lambda: 用模型的预期进球计算"真实"概率
          - 如果不提供: 只做 overround 检测 + 交叉盘口检测 (不做基线对比)
          
        美国 vs 巴拉圭 (有 model_total_lambda=3.5, 因为模型预测会有大球):
          - 真实 P(U2) = P(Poisson(3.5) <= 2) = 32%
          - 实际隐含 P(U2) = 35% → 基本一致, 不是饵
          - 但如果 model_total_lambda=2.8 (模型预期保守):
          - 真实 P(U2) = P(Poisson(2.8) <= 2) = 47%
          - 实际隐含 P(U2) = 35% → 偏差 -12% = 饵!
        """
        anomalies = []
        line = odds.get('line', 2.5)
        over_odds = odds.get('over', 0)
        under_odds = odds.get('under', 0)
        
        if over_odds <= 0 or under_odds <= 0:
            return 0.0, []
        
        # ── 实际隐含概率 (去抽水) ──
        imp_o = 1.0 / over_odds
        imp_u = 1.0 / under_odds
        total_imp = imp_o + imp_u
        actual_u_imp = imp_u / total_imp
        actual_o_imp = imp_o / total_imp
        
        signal_score = 0.0
        
        # ── 检测1: 用模型预期对比 (最可靠) ──
        if model_total_lambda is not None and model_total_lambda > 0:
            from math import exp, factorial
            lam = model_total_lambda
            
            def poisson_cdf(k, lam):
                return sum((lam ** i) * exp(-lam) / factorial(i) for i in range(k + 1))
            
            true_u_prob = poisson_cdf(int(line), lam)
            true_o_prob = 1.0 - true_u_prob
            
            u_bias = actual_u_imp - true_u_prob
            o_bias = actual_o_imp - true_o_prob
            
            BAIT_THRESHOLD = -0.10  # 模型预期比实际低 10% 以上 = 饵
            
            if u_bias < BAIT_THRESHOLD:
                severity = min(1.0, abs(u_bias) / 0.25)
                fair_u_odds = 1.0 / true_u_prob
                anomalies.append(OddsAnomaly(
                    market='totals',
                    dimension='premium',
                    severity=severity,
                    description=f'U{line}疑似饵(模型对比): 实际{actual_u_imp:.1%} '
                                f'vs 模型预期{true_u_prob:.1%} (偏差{u_bias*100:.0f}%)',
                    fair_value=round(fair_u_odds, 2),
                    actual_value=round(under_odds, 2),
                    premium_pct=round((under_odds / fair_u_odds - 1) * 100, 1),
                ))
                signal_score = max(signal_score, severity)
            
            if o_bias < BAIT_THRESHOLD:
                severity = min(1.0, abs(o_bias) / 0.25)
                fair_o_odds = 1.0 / true_o_prob
                anomalies.append(OddsAnomaly(
                    market='totals',
                    dimension='premium',
                    severity=severity,
                    description=f'O{line}疑似饵(模型对比): 实际{actual_o_imp:.1%} '
                                f'vs 模型预期{true_o_prob:.1%} (偏差{o_bias*100:.0f}%)',
                    fair_value=round(fair_o_odds, 2),
                    actual_value=round(over_odds, 2),
                    premium_pct=round((over_odds / fair_o_odds - 1) * 100, 1),
                ))
                signal_score = max(signal_score, severity)
        
        # ── 检测2: 异常高额抽水 ──
        overround = total_imp - 1.0
        expected_ovr = baseline.get('avg_overround', 0.065) * 2
        if overround > expected_ovr * 1.8:
            severity = min(1.0, (overround / expected_ovr - 1) / 2.0)
            anomalies.append(OddsAnomaly(
                market='totals',
                dimension='premium',
                severity=severity * 0.4,
                description=f'大小球抽水率异常: {overround*100:.1f}% '
                            f'(联赛均值~{expected_ovr*100:.1f}%)',
            ))
            signal_score = max(signal_score, severity * 0.3)
        
        return min(1.0, signal_score), anomalies
    
    def _scan_ah(self, odds: Dict[str, float], 
                  odds_1x2: Optional[Dict] = None,
                  baseline: Optional[Dict] = None) -> Tuple[float, List[OddsAnomaly]]:
        """亚盘赔率扫描"""
        anomalies = []
        line = odds.get('line', 0)
        home_odds = odds.get('home_cover', 0)
        away_odds = odds.get('away_cover', 0)
        
        if home_odds <= 0 or away_odds <= 0:
            return 0.0, []
        
        # 去抽水
        imp_h = 1.0 / home_odds
        imp_a = 1.0 / away_odds
        total_imp = imp_h + imp_a
        
        fair_h_prob = imp_h / total_imp
        fair_a_prob = imp_a / total_imp
        
        fair_h_odds = 1.0 / fair_h_prob
        fair_a_odds = 1.0 / fair_a_prob
        
        home_premium = (home_odds - fair_h_odds) / fair_h_odds
        away_premium = (away_odds - fair_a_odds) / fair_a_odds
        
        signal_score = 0.0
        
        if max(home_premium, away_premium) > self._anomaly_thresholds['premium']:
            dir_name = '主队' if home_premium > away_premium else '客队'
            premium_val = max(home_premium, away_premium)
            severity = min(1.0, premium_val / 0.25)
            anomalies.append(OddsAnomaly(
                market='ah',
                dimension='premium',
                severity=severity,
                description=f'AH{line:+}: {dir_name}覆盖赔率溢价{premium_val*100:.0f}%',
                premium_pct=round(premium_val * 100, 1),
            ))
            signal_score = max(signal_score, severity)
        
        # 交叉检测: AH 方向 vs 1X2 方向是否一致
        if odds_1x2:
            imp_1x2_h = 1.0 / odds_1x2.get('home', 1)
            imp_1x2_d = 1.0 / odds_1x2.get('draw', 1)
            imp_1x2_a = 1.0 / odds_1x2.get('away', 1)
            total_1x2 = imp_1x2_h + imp_1x2_d + imp_1x2_a
            prob_1x2_h = imp_1x2_h / total_1x2
            
            # 1X2 主胜概率 vs AH 主队覆盖概率
            prob_diff = abs(prob_1x2_h - fair_h_prob)
            if prob_diff > 0.08:
                severity = min(1.0, prob_diff / 0.15)
                anomalies.append(OddsAnomaly(
                    market='ah',
                    dimension='inconsistency',
                    severity=severity,
                    description=f'AH与1X2方向不一致: 1X2主胜概率{prob_1x2_h:.1%} '
                                f'vs AH主覆盖概率{fair_h_prob:.1%} (差{prob_diff*100:.0f}%)',
                ))
                signal_score = max(signal_score, severity * 0.6)
        
        return min(1.0, signal_score), anomalies
    
    def _scan_cs(self, odds: Dict[Tuple[int, int], float], 
                  baseline: Dict) -> Tuple[float, List[OddsAnomaly]]:
        """比分赔率扫描 — 检测覆盖缺口"""
        anomalies = []
        
        if not odds:
            return 0.0, []
        
        # 计算覆盖的比分范围
        covered_scores = set(odds.keys())
        max_goal_h = max(s[0] for s in covered_scores) if covered_scores else 0
        max_goal_a = max(s[1] for s in covered_scores) if covered_scores else 0
        
        signal_score = 0.0
        
        # 检测1: 覆盖范围不足 (大比分区域缺失)
        if max_goal_h < 4 or max_goal_a < 3:
            severity = 0.5 if max_goal_h < 4 else 0.3
            anomalies.append(OddsAnomaly(
                market='cs',
                dimension='gap',
                severity=severity,
                description=f'比分赔率覆盖不足: 仅覆盖到{max_goal_h}-{max_goal_a}，'
                            f'大比分区域(4-1等)未开赔 → 信息不对称',
            ))
            signal_score = max(signal_score, severity)
        
        # 检测2: 极端比分溢价 (如果4-0在CS中但赔率极高)
        # 找最大隐含概率 vs 最小隐含概率的比值
        all_imps = {k: 1.0/v for k, v in odds.items()}
        total_imp = sum(all_imps.values())
        
        if total_imp > 0:
            probs = {k: v/total_imp for k, v in all_imps.items()}
            max_prob = max(probs.values())
            min_prob = min(probs.values())
            
            # 如果最可能比分和最不可能比分的概率比 > 50:1
            if max_prob / max(min_prob, 1e-8) > 50:
                anomalies.append(OddsAnomaly(
                    market='cs',
                    dimension='premium',
                    severity=0.3,
                    description=f'CS概率分布极度不均 (max/min={max_prob/min_prob:.0f}:1)',
                ))
                signal_score = max(signal_score, 0.3)
        
        # 检测3: 联赛极端比分概率 vs CS覆盖
        league_extreme_rate = baseline.get('extreme_score_rate', 0.08)
        # CS中 总进球>=4 的比分占比
        extreme_in_cs = sum(1 for (h, a) in covered_scores if h + a >= 4)
        total_covered = len(covered_scores)
        cs_extreme_rate = extreme_in_cs / max(total_covered, 1)
        
        if cs_extreme_rate < league_extreme_rate * 0.5 and total_covered > 5:
            severity = 0.4
            anomalies.append(OddsAnomaly(
                market='cs',
                dimension='gap',
                severity=severity,
                description=f'CS中大比分覆盖率偏低: {cs_extreme_rate:.0%} '
                            f'vs 联赛均值{league_extreme_rate:.0%}',
            ))
            signal_score = max(signal_score, severity)
        
        return min(1.0, signal_score), anomalies
    
    # ═══════════════════════════════════════════════
    # L2: 交叉盘口一致性
    # ═══════════════════════════════════════════════
    
    def _scan_cross_market(self, odds_1x2: Dict, odds_totals: Optional[Dict],
                            odds_ah: Optional[Dict], baseline: Dict) -> float:
        """
        交叉盘口一致性检测
        
        核心思想: 如果庄家诚实地给赔率, 1X2 / AH / Totals 应该自洽。
        如果出现矛盾 → 庄家在某些盘口上不诚实 → 收割信号。
        
        美国 vs 巴拉圭:
          1X2 → Home=2.10 (主胜概率46%)  
          Totals → U2@2.85 (小球极度引诱) 
          矛盾: 主胜概率高 + 小球引诱 → 暗示"主胜但小球" → 实际大球
        
        如果 1X2 和 Totals 讲的是两个不同的故事 → cross_market_gap 高
        """
        signal = 0.0
        contradictions = 0
        
        # 1X2 隐含概率
        h, d, a = odds_1x2.get('home', 0), odds_1x2.get('draw', 0), odds_1x2.get('away', 0)
        imp_h = 1.0 / h if h > 0 else 0
        imp_d = 1.0 / d if d > 0 else 0
        imp_a = 1.0 / a if a > 0 else 0
        total = imp_h + imp_d + imp_a
        prob_h = imp_h / total if total > 0 else 0.33
        
        # ── 1X2 vs Totals 一致性 ──
        if odds_totals:
            line = odds_totals.get('line', 2.5)
            over_odds = odds_totals.get('over', 0)
            under_odds = odds_totals.get('under', 0)
            
            if over_odds > 0 and under_odds > 0:
                imp_o = 1.0 / over_odds
                imp_u = 1.0 / under_odds
                prob_over = imp_o / (imp_o + imp_u)
                
                # 检测: 1X2 强烈偏主胜 但 Totals 偏小球 → 矛盾
                # (主胜通常伴随更多进球)
                if prob_h > 0.48 and prob_over < 0.45:
                    gap = prob_h - prob_over
                    contradictions += 1
                    signal = max(signal, min(1.0, gap / 0.20))
                
                # 检测: 1X2 均衡 但 Totals Under 极度溢价 → 矛盾
                max_prob = max(prob_h, 1 - prob_h - (imp_d/total if total > 0 else 0.26), 1 - prob_h)
                if max_prob < 0.48:  # 无明显热门
                    fair_u_odds = 1.0 / (imp_u / (imp_o + imp_u))
                    under_premium = (under_odds - fair_u_odds) / fair_u_odds if fair_u_odds > 0 else 0
                    if under_premium > 0.12:  # Under溢价但无热门 ← 矛盾
                        contradictions += 1
                        signal = max(signal, min(1.0, under_premium / 0.25))
        
        # ── 1X2 vs AH 一致性 ──
        if odds_ah:
            ah_line = odds_ah.get('line', 0)
            
            # 从 1X2 推断隐含的预期进球差
            # 简化: prob_h 高 → AH line 应该更负 (主让更多)
            implied_handicap = (prob_h - 0.33) * 1.5  # 粗略映射
            
            if abs(ah_line - implied_handicap) > 0.35:
                contradictions += 1
                signal = max(signal, min(1.0, abs(ah_line - implied_handicap) / 0.7))
        
        # 有多个矛盾 → 信号更强
        if contradictions >= 2:
            signal = min(1.0, signal * 1.3)
        
        return signal
    
    # ═══════════════════════════════════════════════
    # HRS 计算
    # ═══════════════════════════════════════════════
    
    def _compute_hrs(self, report: HarvestingReport) -> Tuple[float, float]:
        """
        综合收割风险评分
        
        HRS = 加权组合 of:
          - signal_totals (最高权重, 0.40) — 大小球是收割的核心工具
          - signal_1x2 (0.25)
          - signal_cross_market (0.15) 
          - signal_ah (0.10)
          - signal_cs (0.10)
        
        置信度 = 有效维度的比例
        """
        weights = {
            'totals': 0.40,
            '1x2': 0.25,
            'cross_market': 0.15,
            'ah': 0.10,
            'cs': 0.10,
        }
        
        signals = {
            'totals': report.signal_totals,
            '1x2': report.signal_1x2,
            'cross_market': report.signal_cross_market,
            'ah': report.signal_ah,
            'cs': report.signal_cs,
        }
        
        # 加权平均
        weighted_sum = 0.0
        active_weight = 0.0
        
        for dim, signal in signals.items():
            if signal > 0:
                w = weights[dim]
                weighted_sum += signal * w
                active_weight += w
        
        # 还有异常的维度也算 (即使信号值=0但可能有些是N/A)
        anomaly_dims = set(a.market for a in report.anomalies)
        for dim in anomaly_dims:
            dim_key = {'1x2': '1x2', 'totals': 'totals', 'ah': 'ah', 'cs': 'cs'}.get(dim)
            if dim_key and signals.get(dim_key, 0) == 0:
                active_weight += weights.get(dim_key, 0) * 0.3  # 异常但信号弱给部分权重
        
        if active_weight > 0:
            hrs = weighted_sum / active_weight
            # 有异常时给加成
            if len(report.anomalies) >= 2:
                hrs = min(1.0, hrs * 1.15)
            if len(report.anomalies) >= 3:
                hrs = min(1.0, hrs * 1.10)
        else:
            hrs = 0.0
        
        # 置信度
        total_possible_dims = 4  # 1x2, totals, ah, cs
        active_dims = sum(1 for s in [report.signal_1x2 > 0, report.signal_totals > 0,
                                       report.signal_ah > 0, report.signal_cs > 0])
        confidence = min(1.0, active_dims / total_possible_dims + 0.1)
        
        return round(hrs, 4), round(confidence, 4)
    
    def _classify_risk(self, hrs: float, confidence: float) -> str:
        """风险等级分类"""
        if hrs >= 0.75 and confidence >= 0.5:
            return "CRITICAL"
        elif hrs >= 0.55:
            return "HIGH"
        elif hrs >= 0.30:
            return "MEDIUM"
        else:
            return "LOW"
    
    # ═══════════════════════════════════════════════
    # 资金流向推断
    # ═══════════════════════════════════════════════
    
    def _infer_money_flow(self, odds_1x2: Optional[Dict],
                           odds_totals: Optional[Dict],
                           odds_ah: Optional[Dict]) -> Tuple[str, str]:
        """推断庄家赔率设计的意图"""
        baited = []
        suppressed = []
        
        if odds_1x2:
            h, d, a = odds_1x2.get('home', 0), odds_1x2.get('draw', 0), odds_1x2.get('away', 0)
            if h > 0:
                # 最高赔率 = 最引诱
                max_dir = max([('主胜', h), ('平局', d), ('客胜', a)], key=lambda x: x[1])
                min_dir = min([('主胜', h), ('平局', d), ('客胜', a)], key=lambda x: x[1])
                baited.append(f'1X2-{max_dir[0]}@{max_dir[1]:.2f}')
                suppressed.append(f'1X2-{min_dir[0]}@{min_dir[1]:.2f}')
        
        if odds_totals:
            over = odds_totals.get('over', 0)
            under = odds_totals.get('under', 0)
            line = odds_totals.get('line', 2.5)
            if under > over:
                baited.append(f'U{line}@{under:.2f} (溢价)')
            else:
                baited.append(f'O{line}@{over:.2f} (溢价)')
        
        if odds_ah:
            hc = odds_ah.get('home_cover', 0)
            ac = odds_ah.get('away_cover', 0)
            line = odds_ah.get('line', 0)
            if ac > hc:
                baited.append(f'AH客队覆盖@{ac:.2f}')
            else:
                baited.append(f'AH主队覆盖@{hc:.2f}')
        
        return '; '.join(baited) if baited else '无足够数据', \
               '; '.join(suppressed) if suppressed else '无足够数据'
    
    # ═══════════════════════════════════════════════
    # L3: 尾端风险调整
    # ═══════════════════════════════════════════════
    
    def _compute_tail_risk(self, report: HarvestingReport,
                            odds_totals: Optional[Dict],
                            baseline: Dict) -> float:
        """
        尾端风险系数
        
        当 Totals Under 溢价高 + 其他信号时, 
        实际进球数可能远超预期 (如 4-1)。
        """
        tail_risk = 0.0
        factors = 0
        
        # Factor 1: Totals Under 溢价 (最直接)
        if report.signal_totals > 0.4:
            tail_risk += report.signal_totals * 0.7
            factors += 1
        
        # Factor 2: CS 覆盖缺口
        if report.signal_cs > 0.3:
            tail_risk += report.signal_cs * 0.5
            factors += 1
        
        # Factor 3: 交叉盘口矛盾
        if report.signal_cross_market > 0.4:
            tail_risk += report.signal_cross_market * 0.4
            factors += 1
        
        # Factor 4: 1X2 有溢价 (引诱输家)
        if report.signal_1x2 > 0.4:
            tail_risk += report.signal_1x2 * 0.3
            factors += 1
        
        if factors > 0:
            tail_risk = min(1.0, tail_risk / max(1, factors - 1))
        
        return round(tail_risk, 4)
    
    def _estimate_extreme_prob(self, report: HarvestingReport,
                                odds_1x2: Optional[Dict],
                                odds_totals: Optional[Dict],
                                baseline: Dict) -> float:
        """
        估算极端比分概率 (总进球 >= 4)
        
        当收割信号存在时, 极端比分的概率被上调。
        base = 联赛 baseline extreme_rate
        adjusted = base * (1 + tail_risk_factor * 3)
        """
        base_extreme = baseline.get('extreme_score_rate', 0.08)
        tail_factor = report.tail_risk_factor
        
        # 尾端风险直接放大极端概率
        adjusted = base_extreme * (1 + tail_factor * 3.0)
        
        # Totals溢价直接贡献
        if report.signal_totals > 0.5:
            adjusted += 0.05  # 额外+5%
        
        return min(0.40, adjusted)  # 上限 40%
    
    # ═══════════════════════════════════════════════
    # 预测调整
    # ═══════════════════════════════════════════════
    
    def adjust_prediction(self,
                          base_probs: Dict[str, float],
                          report: HarvestingReport) -> AdjustedPrediction:
        """
        根据收割风险报告调整预测
        
        调整策略:
          1. 当 HRS > 0.5: 降低预测置信度
          2. 当 tail_risk > 0.4: 扩大平局概率 (极端比分通常伴随不确定性)
          3. 生成尾端场景: 展示可能的极端比分
        
        Args:
            base_probs: {'H': 0.45, 'D': 0.28, 'A': 0.27}
            report: HarvestingReport
        
        Returns:
            AdjustedPrediction
        """
        h, d, a = base_probs.get('H', 0.33), base_probs.get('D', 0.33), base_probs.get('A', 0.34)
        
        hrs = report.hrs
        tail = report.tail_risk_factor
        
        adjusted = {'H': h, 'D': d, 'A': a}
        confidence_discount = 0.0
        scenarios = []
        note_parts = []
        
        # ── 无风险: 返回原始预测 ──
        if hrs < 0.30:
            return AdjustedPrediction(
                base_probs=base_probs,
                adjusted_probs=adjusted,
                confidence_discount=0.0,
                tail_scenarios=[],
                adjustment_note='无收割信号, 预测未调整',
            )
        
        # ── MEDIUM 风险 (0.30-0.55): 轻微调整 ──
        if 0.30 <= hrs < 0.55:
            # 降低置信度 10-20%
            confidence_discount = 0.10 + (hrs - 0.30) * 0.4
            # 轻微增加平局概率
            d_boost = (hrs - 0.30) * 0.05
            adjusted['H'] = h * (1 - d_boost/2)
            adjusted['D'] = d + d_boost
            adjusted['A'] = a * (1 - d_boost/2)
            note_parts.append(f'MEDIUM收割风险(HRS={hrs:.2f}), 置信度-{confidence_discount:.0%}')
        
        # ── HIGH 风险 (0.55-0.75): 显著调整 ──
        elif 0.55 <= hrs < 0.75:
            confidence_discount = 0.25 + (hrs - 0.55) * 0.5
            # 平滑所有概率向 1/3 (增加不确定性)
            alpha = (hrs - 0.55) * 0.4  # 向均匀分布混合
            adjusted['H'] = h * (1 - alpha) + 0.333 * alpha
            adjusted['D'] = d * (1 - alpha) + 0.333 * alpha
            adjusted['A'] = a * (1 - alpha) + 0.333 * alpha
            note_parts.append(f'HIGH收割风险(HRS={hrs:.2f}), 显著降低置信度')
            
            # 生成尾端场景
            if tail > 0.3:
                scenarios = self._generate_tail_scenarios(report)
        
        # ── CRITICAL 风险 (>0.75): 重度调整 ──
        else:
            confidence_discount = 0.50 + min(0.30, (hrs - 0.75) * 0.5)
            alpha = 0.50  # 强混合均匀分布
            adjusted['H'] = h * (1 - alpha) + 0.333 * alpha
            adjusted['D'] = d * (1 - alpha) + 0.333 * alpha
            adjusted['A'] = a * (1 - alpha) + 0.333 * alpha
            note_parts.append(f'CRITICAL收割风险(HRS={hrs:.2f})! 预测极不可靠, 建议观望')
            scenarios = self._generate_tail_scenarios(report)
        
        # 归一化
        total = adjusted['H'] + adjusted['D'] + adjusted['A']
        if total > 0:
            adjusted = {k: round(v/total, 4) for k, v in adjusted.items()}
        
        # ── 附加尾端风险说明 ──
        if tail > 0.3:
            note_parts.append(f'尾端风险={tail:.2f}, 极端比分概率={report.extreme_score_prob:.0%}')
        
        # ── 附加异常说明 ──
        for anomaly in report.anomalies[:3]:  # top 3
            note_parts.append(f'[{anomaly.market}] {anomaly.description[:60]}')
        
        return AdjustedPrediction(
            base_probs={'H': round(h, 4), 'D': round(d, 4), 'A': round(a, 4)},
            adjusted_probs=adjusted,
            confidence_discount=round(confidence_discount, 4),
            tail_scenarios=scenarios,
            adjustment_note=' | '.join(note_parts),
        )
    
    def _generate_tail_scenarios(self, report: HarvestingReport) -> List[Dict]:
        """生成尾端场景 — 可能的极端比分"""
        scenarios = []
        
        if report.baited_direction and 'U' in report.baited_direction:
            # 如果有 Under 引诱 → 可能出大球
            scenarios.append({
                'score': '2-2 / 3-2 / 4-1',
                'type': '大球场景 (总进球≥4)',
                'probability_estimate': f'{report.extreme_score_prob:.0%}',
                'rationale': f'U赔率溢价, 尾端风险={report.tail_risk_factor:.2f}',
            })
        
        if report.suppressed_direction and '1X2' in report.suppressed_direction:
            # 被抑制的方向可能才是真实的
            suppressed_team = report.suppressed_direction.split('-')[1] if '-' in report.suppressed_direction else ''
            if suppressed_team:
                scenarios.append({
                    'score': f'如果{suppressed_team}方向正确',
                    'type': '庄家抑制方向 (可能是真实方向)',
                    'probability_estimate': '需结合盘口判断',
                    'rationale': f'{suppressed_team}赔率被刻意压低',
                })
        
        # 极端比分提醒
        scenarios.append({
            'score': '4-0 / 4-1 / 3-3 等极端比分',
            'type': '尾端风险提醒',
            'probability_estimate': f'{report.extreme_score_prob:.0%} (vs 联赛均值 ~6-8%)',
            'rationale': '当收割信号高时, 极端比分概率被上调至联赛均值的2-3倍',
        })
        
        return scenarios
    
    # ═══════════════════════════════════════════════
    # 建议生成
    # ═══════════════════════════════════════════════
    
    def _generate_recommendation(self, report: HarvestingReport) -> Tuple[str, bool]:
        """生成操作建议"""
        if report.risk_level == "CRITICAL":
            return (
                f"🚫 严重收割信号 (HRS={report.hrs:.2f})! "
                f"多个盘口存在异常溢价，建议回避此场比赛。"
                f"如必须预测，只参考方向不参考比分幅度。",
                True
            )
        elif report.risk_level == "HIGH":
            return (
                f"⚠️ 强收割信号 (HRS={report.hrs:.2f})! "
                f"模型预测可信度降低{25 if report.hrs > 0.6 else 15}%。"
                f"建议: 1)关注尾端风险 2)降低投注规模 3)考虑观望",
                True
            )
        elif report.risk_level == "MEDIUM":
            return (
                f"⚡ 中等收割信号 (HRS={report.hrs:.2f})。"
                f"模型预测可用但需谨慎。关注异常盘口。",
                False
            )
        else:
            return "✅ 未检测到明显收割信号，预测可信度正常。", False
    
    # ═══════════════════════════════════════════════
    # 联赛基线
    # ═══════════════════════════════════════════════
    
    def _get_league_baseline(self, league: Optional[str]) -> Dict:
        """获取联赛基线 (优先数据库, 回退到内置值)"""
        cache_key = league or 'default'
        if cache_key in self._league_cache:
            return self._league_cache[cache_key]
        
        # 尝试从数据库校准
        db_baseline = self._calibrate_from_db(league)
        if db_baseline:
            self._league_cache[cache_key] = db_baseline
            return db_baseline
        
        # 回退到内置基线
        baseline = LEAGUE_BASELINES.get(league, LEAGUE_BASELINES['default']).copy()
        self._league_cache[cache_key] = baseline
        return baseline
    
    def _calibrate_from_db(self, league: Optional[str]) -> Optional[Dict]:
        """从 odds_features 表校准联赛基线"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            if league:
                c.execute("""
                    SELECT 
                        AVG(home_score + away_score) as avg_goals,
                        AVG(CASE WHEN home_score > away_score THEN 1.0 ELSE 0.0 END) as hwr,
                        AVG(CASE WHEN home_score = away_score THEN 1.0 ELSE 0.0 END) as dr,
                        AVG(CASE WHEN home_score + away_score >= 4 THEN 1.0 ELSE 0.0 END) as extreme_rate,
                        AVG(overround) as avg_ovr,
                        COUNT(*) as n
                    FROM odds_features
                    WHERE league = ? AND home_score IS NOT NULL
                """, (league,))
            else:
                c.execute("""
                    SELECT 
                        AVG(home_score + away_score) as avg_goals,
                        AVG(CASE WHEN home_score > away_score THEN 1.0 ELSE 0.0 END) as hwr,
                        AVG(CASE WHEN home_score = away_score THEN 1.0 ELSE 0.0 END) as dr,
                        AVG(CASE WHEN home_score + away_score >= 4 THEN 1.0 ELSE 0.0 END) as extreme_rate,
                        AVG(overround) as avg_ovr,
                        COUNT(*) as n
                    FROM odds_features
                    WHERE home_score IS NOT NULL
                """)
            
            row = c.fetchone()
            conn.close()
            
            if row and row[5] > 10:
                return {
                    'avg_total_goals': round(row[0], 2) if row[0] else 2.75,
                    'home_win_rate': round(row[1], 4) if row[1] else 0.45,
                    'draw_rate': round(row[2], 4) if row[2] else 0.26,
                    'extreme_score_rate': round(row[3], 4) if row[3] else 0.08,
                    'avg_overround': round(row[4], 4) if row[4] else 0.065,
                    'totals_std': 0.10,
                    'sample_size': row[5],
                }
            
        except (Exception, KeyError, IndexError) as e:
            logger.debug(f"联赛基线DB校准失败 ({league}): {e}")
        
        return None
    
    # ═══════════════════════════════════════════════
    # 便捷: 从数据库匹配中提取赔率
    # ═══════════════════════════════════════════════
    
    def scan_from_db(self, home_team: str, away_team: str,
                      league: Optional[str] = None) -> Optional[HarvestingReport]:
        """
        从数据库提取赔率并扫描
        
        Args:
            home_team: 主队名
            away_team: 客队名
            league: 联赛名
        """
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            # 查找最新赔率
            c.execute("""
                SELECT o.home_odds, o.draw_odds, o.away_odds, m.league_name
                FROM odds o
                JOIN matches m ON o.match_id = m.match_id
                WHERE m.home_team_name = ? AND m.away_team_name = ?
                ORDER BY o.created_at DESC LIMIT 1
            """, (home_team, away_team))
            
            row = c.fetchone()
            conn.close()
            
            if row:
                odds_1x2 = {'home': row[0], 'draw': row[1], 'away': row[2]}
                league = league or row[3]
                return self.scan(odds_1x2=odds_1x2, league=league)
            
        except (Exception, KeyError, IndexError) as e:
            logger.warning(f"从DB扫描失败 ({home_team} vs {away_team}): {e}")
        
        return None

# ══════════════════════════════════════════════════
# 便捷函数
# ══════════════════════════════════════════════════

def create_guard(db_path: Optional[str] = None) -> HarvestingGuard:
    return HarvestingGuard(db_path)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    guard = HarvestingGuard()
    
    # ── 测试1: 美国 vs 巴拉圭 (已知收割案例) ──
    print("=" * 70)
    print("  测试1: 美国 vs 巴拉圭 — 已知收割案例")
    print("=" * 70)
    
    report = guard.scan(
        odds_1x2={'home': 2.10, 'draw': 3.20, 'away': 3.75},
        odds_totals={'line': 2.0, 'over': 1.88, 'under': 2.85},
        odds_ah={'line': -0.5, 'home_cover': 2.12, 'away_cover': 1.81},
        odds_cs={
            (1,0): 5.90, (0,0): 7.80, (0,1): 8.80,
            (2,0): 8.90, (1,1): 6.70, (0,2): 19.5,
            (2,1): 10.0, (2,2): 22.0, (1,2): 14.5,
            (3,0): 13.0, (3,1): 34.0, (0,3): 30.0,
            (3,2): 135.0, (2,3): 50.0, (1,3): 50.0,
        },
        league='世界杯',
        model_total_lambda=3.8,  # 模型预期大球 (关键参数!)
    )
    
    print(f"\n  HRS: {report.hrs:.3f} | 风险等级: {report.risk_level}")
    print(f"  置信度: {report.confidence:.2f}")
    print(f"  尾端风险: {report.tail_risk_factor:.3f}")
    print(f"  极端比分概率: {report.extreme_score_prob:.1%}")
    print(f"\n  分维度信号:")
    print(f"    1X2:      {report.signal_1x2:.3f}")
    print(f"    Totals:   {report.signal_totals:.3f}")
    print(f"    AH:       {report.signal_ah:.3f}")
    print(f"    CS:       {report.signal_cs:.3f}")
    print(f"    CrossMkt: {report.signal_cross_market:.3f}")
    print(f"\n  异常 ({len(report.anomalies)}个):")
    for a in report.anomalies:
        print(f"    [{a.market}] {a.description}")
    print(f"\n  引诱方向: {report.baited_direction}")
    print(f"  抑制方向: {report.suppressed_direction}")
    print(f"\n  建议: {report.recommendation}")
    
    # ── 测试2: 预测调整 ──
    print(f"\n  {'='*50}")
    print(f"  预测调整演示")
    print(f"  {'='*50}")
    
    base = {'H': 0.36, 'D': 0.35, 'A': 0.29}
    adjusted = guard.adjust_prediction(base, report)
    print(f"  原始预测: H={base['H']:.3f} D={base['D']:.3f} A={base['A']:.3f}")
    print(f"  调整预测: H={adjusted.adjusted_probs['H']:.3f} D={adjusted.adjusted_probs['D']:.3f} A={adjusted.adjusted_probs['A']:.3f}")
    print(f"  置信度折扣: {adjusted.confidence_discount:.0%}")
    print(f"  调整说明: {adjusted.adjustment_note}")
    if adjusted.tail_scenarios:
        print(f"  尾端场景:")
        for s in adjusted.tail_scenarios:
            print(f"    → {s['score']} [{s['type']}]")
    
    # ── 测试3: 正常比赛 (低风险) ──
    print(f"\n{'='*70}")
    print(f"  测试2: 利物浦 vs 伯恩茅斯 — 正常比赛")
    print(f"{'='*70}")
    
    report2 = guard.scan(
        odds_1x2={'home': 1.40, 'draw': 5.00, 'away': 7.50},
        odds_totals={'line': 3.0, 'over': 1.85, 'under': 1.95},
        league='英超',
        model_total_lambda=3.2,  # 利物浦强队，预期进球高
    )
    
    print(f"\n  HRS: {report2.hrs:.3f} | 风险等级: {report2.risk_level}")
    print(f"  异常: {len(report2.anomalies)}个")
    print(f"  建议: {report2.recommendation}")
    
    base2 = {'H': 0.65, 'D': 0.20, 'A': 0.15}
    adj2 = guard.adjust_prediction(base2, report2)
    print(f"  原始: H={base2['H']:.3f} → 调整: H={adj2.adjusted_probs['H']:.3f}")
    print(f"  调整说明: {adj2.adjustment_note}")
