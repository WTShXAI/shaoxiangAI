"""
哨响AI — T07 仲裁逻辑模块 (Arbitration Engine)
==================================================
当集成模型与专家投票产生矛盾时，介入仲裁并输出最终裁决。

设计:
  1. 不一致规则定义: 5种冲突场景分类 + 触发条件
  2. 加权投票仲裁: 集成模型 + 各专家 + 赔率隐含概率 → 加权聚合
  3. 元学习仲裁: 基于历史场景相似度的信任权重学习
  4. 综合裁决: 输出仲裁后概率 + 置信度 + 裁决理由

用法:
    engine = ArbitrationEngine(db_path='data/football_data.db')
    result = engine.arbitrate(
        ensemble_probs={'home': 0.38, 'draw': 0.22, 'away': 0.40},
        ensemble_conf=0.72,
        expert_vote=expert_vote_result,
        confidence_comparison=confidence_comparison,
        odds={'home': 2.5, 'draw': 3.2, 'away': 2.8},
        scenario={'sigma_trap': 0.15, 'rank_diff': 0.8, 'league': '英超'},
    )
"""

import json
import logging
import time
import math
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field

from utils.constants import DEFAULT_HOME_PROB, DEFAULT_DRAW_PROB, DEFAULT_AWAY_PROB
from datetime import datetime
from collections import defaultdict
from enum import Enum

import numpy as np

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  T07-R1: 不一致场景分类
# ═══════════════════════════════════════════════════════════════

class ConflictType(Enum):
    """不一致场景枚举"""
    # 两系统预测结果不同 (home vs away, etc.)
    DIRECTION = "direction"
    # 一方高置信一方低置信 (不对称)
    CONFIDENCE_ASYMMETRY = "confidence_asymmetry"
    # 专家内部严重分裂 (consensus=split)
    INTERNAL_SPLIT = "internal_split"
    # 赔率隐含概率与两个系统都严重背离
    ODDS_CONTRARIAN = "odds_contrarian"
    # 复合冲突 (≥2 型同时触发)
    FULL_CONFLICT = "full_conflict"
    # 无冲突
    NONE = "none"


class ConflictSeverity(Enum):
    """冲突严重程度"""
    LOW = "low"          # 小分歧，可参考
    MODERATE = "moderate" # 中等分歧，谨慎处理
    HIGH = "high"        # 严重分歧，建议观望
    CRITICAL = "critical" # 极端分歧，必须人工复核


@dataclass
class ConflictAssessment:
    """不一致评估结果"""
    conflict_type: str                       # ConflictType 值
    severity: str                            # ConflictSeverity 值
    trigger_rules: List[str] = field(default_factory=list)  # 触发的规则列表
    ensemble_lean: str = ""                  # 集成模型倾向
    expert_lean: str = ""                    # 专家投票倾向
    odds_lean: str = ""                      # 赔率隐含倾向
    arbitrable: bool = True                  # 是否可自动仲裁
    reason: str = ""                         # 冲突原因描述


# ═══════════════════════════════════════════════════════════════
#  T07-R2: 决策信标 (Decision Beacon)
# ═══════════════════════════════════════════════════════════════

class DecisionPolicy(Enum):
    """最终采用的决策策略"""
    ENSEMBLE_ONLY = "ensemble_only"         # 完全依赖集成模型
    EXPERT_ONLY = "expert_only"             # 完全依赖专家投票
    WEIGHTED_FUSION = "weighted_fusion"     # 加权融合
    META_LEARNING = "meta_learning"         # 基于历史场景学习
    CONSERVATIVE_MIN = "conservative_min"   # 取保守值
    ODDS_BASELINE = "odds_baseline"         # 退回到赔率隐含概率


@dataclass
class DecisionBeacon:
    """决策信标：仲裁生成的信号建议"""
    # ── 信号 ──
    signal: str                             # BUY / SELL / HOLD / PASS
    confidence: float                       # 信标置信度 0-1
    strength: str                           # STRONG / NORMAL / WEAK / NONE

    # ── 概率 ──
    prediction: Dict[str, float]            # 仲裁后 {home, draw, away} 概率
    predicted_outcome: str                  # 最终推荐方向
    predicted_outcome_cn: str               # 中文

    # ── 溯源 ──
    policy: str                             # 采用的决策策略 (DecisionPolicy)
    policy_reason: str                      # 策略选择理由
    evidence: Dict[str, Any] = field(default_factory=dict)  # 决策依据

    # ── 元数据 ──
    execution_time_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ═══════════════════════════════════════════════════════════════
#  T07-R1: 不一致规则引擎 (Inconsistency Rules)
# ═══════════════════════════════════════════════════════════════

class InconsistencyRules:
    """
    T07-R1: 不一致情况处理规则定义。

    当集成模型与专家投票产生分歧时，本模块负责：
      1. 分类冲突类型（5种场景）
      2. 评定冲突严重程度
      3. 判定是否可自动仲裁
    """

    # ── 配置阈值 ──
    CONFIDENCE_ASYMMETRY_RATIO = 2.0       # 双方置信度比值差 > 此值 → 不对称
    INTERNAL_SPLIT_MIN = 0.5               # 多数占比低于此值 → 内部分裂
    ODDS_DIVERGENCE_THRESHOLD = 0.15       # 赔率隐含概率与任一系统差 > 此值 → 背离
    MIN_EXPERTS_FOR_SPLIT = 4              # 至少N个专家才能判定内部分裂

    OUTCOME_CN = {'home': '主胜', 'draw': '平局', 'away': '客胜'}

    def assess(
        self,
        ensemble_outcome: str,
        expert_outcome: str,
        ensemble_conf: float,
        expert_conf: float,
        consensus_level: str,
        majority_pct: float,
        active_experts: int,
        odds_implied: Optional[Dict[str, float]] = None,
    ) -> ConflictAssessment:
        """
        评估不一致场景。

        Args:
            ensemble_outcome: 集成模型预测方向
            expert_outcome: 专家投票预测方向
            ensemble_conf: 集成模型置信度
            expert_conf: 专家投票置信度
            consensus_level: 专家内部一致性 (high/moderate/low/split)
            majority_pct: 多数专家占比
            active_experts: 活跃专家数
            odds_implied: 赔率隐含概率 {home, draw, away}

        Returns:
            ConflictAssessment: 结构化的冲突评估
        """
        triggers = []
        conflict_types = []
        direction_conflict = False

        # ── 规则1: 方向冲突 ──
        if ensemble_outcome != expert_outcome:
            direction_conflict = True
            triggers.append(
                f'DIRECTION: 集成模型→{self.OUTCOME_CN.get(ensemble_outcome, ensemble_outcome)}, '
                f'专家投票→{self.OUTCOME_CN.get(expert_outcome, expert_outcome)}'
            )
            conflict_types.append(ConflictType.DIRECTION)

        # ── 规则2: 置信度不对称 ──
        conf_ratio = (max(ensemble_conf, 0.01) / max(expert_conf, 0.01))
        if conf_ratio >= self.CONFIDENCE_ASYMMETRY_RATIO:
            high_side = '集成模型' if ensemble_conf > expert_conf else '专家投票'
            triggers.append(
                f'CONFIDENCE_ASYMMETRY: {high_side}置信度显著高于对方 '
                f'(比例={conf_ratio:.1f}:1)'
            )
            conflict_types.append(ConflictType.CONFIDENCE_ASYMMETRY)
        elif conf_ratio <= (1.0 / self.CONFIDENCE_ASYMMETRY_RATIO):
            high_side = '专家投票' if expert_conf > ensemble_conf else '集成模型'
            triggers.append(
                f'CONFIDENCE_ASYMMETRY(反): {high_side}置信度显著高于对方 '
                f'(比例={1/conf_ratio:.1f}:1)'
            )
            conflict_types.append(ConflictType.CONFIDENCE_ASYMMETRY)

        # ── 规则3: 专家内部分裂 ──
        if (consensus_level in ('split', 'low') and
                active_experts >= self.MIN_EXPERTS_FOR_SPLIT and
                majority_pct < self.INTERNAL_SPLIT_MIN):
            triggers.append(
                f'INTERNAL_SPLIT: 专家内部严重分裂, '
                f'一致性={consensus_level}, 多数占比={majority_pct:.0%}'
            )
            conflict_types.append(ConflictType.INTERNAL_SPLIT)

        # ── 规则4: 赔率背离 ──
        if odds_implied:
            odds_outcome = max(odds_implied, key=odds_implied.get)
            # 赔率隐含概率与两个系统都不同
            if (odds_outcome != ensemble_outcome and odds_outcome != expert_outcome):
                odds_prob = odds_implied[odds_outcome]
                ens_prob = max(odds_implied.keys(),
                               key=lambda k: abs(
                                   (odds_implied.get(k, 0) -
                                    {ensemble_outcome: ensemble_conf,
                                     expert_outcome: expert_conf}.get(k, 0.3))))
                if abs(odds_prob - max(ensemble_conf, expert_conf)) > self.ODDS_DIVERGENCE_THRESHOLD:
                    triggers.append(
                        f'ODDS_CONTRARIAN: 赔率隐含→{self.OUTCOME_CN.get(odds_outcome)}, '
                        f'偏离系统预测>{self.ODDS_DIVERGENCE_THRESHOLD:.0%}'
                    )
                    conflict_types.append(ConflictType.ODDS_CONTRARIAN)

        # ── 综合判定 ──
        if len(conflict_types) >= 2:
            final_type = ConflictType.FULL_CONFLICT
        elif conflict_types:
            final_type = conflict_types[0]
        else:
            final_type = ConflictType.NONE

        # ── 严重程度 ──
        severity = self._calc_severity(final_type, conflict_types, direction_conflict,
                                       ensemble_conf, expert_conf)

        # ── 可否自动仲裁 ──
        arbitrable = severity != ConflictSeverity.CRITICAL

        # ── 倾向分析 ──
        odds_lean = max(odds_implied, key=odds_implied.get) if odds_implied else "n/a"

        reason = '; '.join(triggers) if triggers else '两系统预测一致，无冲突'

        return ConflictAssessment(
            conflict_type=final_type.value,
            severity=severity.value,
            trigger_rules=triggers,
            ensemble_lean=ensemble_outcome,
            expert_lean=expert_outcome,
            odds_lean=odds_lean,
            arbitrable=arbitrable,
            reason=reason,
        )

    def _calc_severity(
        self,
        final_type: ConflictType,
        conflict_types: List[ConflictType],
        direction_conflict: bool,
        ensemble_conf: float,
        expert_conf: float,
    ) -> ConflictSeverity:
        """计算冲突严重程度"""
        # CRITICAL: 方向冲突 + 双方高置信 + 内部也分裂 → 三杀
        if (ConflictType.DIRECTION in conflict_types and
                ConflictType.INTERNAL_SPLIT in conflict_types and
                ensemble_conf > 0.65 and expert_conf > 0.65):
            return ConflictSeverity.CRITICAL

        # HIGH: 全冲突 + 方向不一致 (保留给2类型且含方向冲突)
        if final_type == ConflictType.FULL_CONFLICT and direction_conflict:
            return ConflictSeverity.HIGH

        # HIGH: 方向冲突 + 任一方高置信
        if direction_conflict and (ensemble_conf > 0.7 or expert_conf > 0.7):
            return ConflictSeverity.HIGH

        # HIGH: 置信度严重不对称 (>3x) + 方向冲突
        if (ConflictType.CONFIDENCE_ASYMMETRY in conflict_types and
                direction_conflict):
            ratio = max(ensemble_conf, 0.01) / max(expert_conf, 0.01)
            if ratio >= 3.0 or ratio <= 1/3.0:
                return ConflictSeverity.HIGH

        # HIGH: 多种冲突组合
        if len(conflict_types) >= 2:
            return ConflictSeverity.HIGH

        # MODERATE: 单一冲突
        if conflict_types:
            return ConflictSeverity.MODERATE

        return ConflictSeverity.LOW


# ═══════════════════════════════════════════════════════════════
#  T07-R2: 加权投票仲裁器 (Weighted Vote Arbiter)
# ═══════════════════════════════════════════════════════════════

class WeightedVoteArbiter:
    """
    T07-R2: 加权投票仲裁算法。

    将集成模型视为一个"超级投票者"，与各专家投票一起进行加权聚合。
    额外引入赔率隐含概率作为基准锚。

    算法:
      1. 收集所有投票源: 集成模型 + 各专家ballots + 赔率隐含
      2. 对每个投票源分配权重
      3. 加权平均 → 最终概率
      4. 计算加权后置信度
    """

    # 权重配置
    ENSEMBLE_BASE_WEIGHT = 0.35      # 集成模型基础权重
    EXPERT_TOTAL_WEIGHT = 0.40       # 所有专家合计权重
    ODDS_ANCHOR_WEIGHT = 0.15        # 赔率锚定权重
    # 预留 0.10 给其他信号 (ELO, Poisson 等)

    SMOOTHING = 0.02                 # 防止零概率

    def arbitrate(
        self,
        ensemble_probs: Dict[str, float],
        ensemble_conf: float,
        expert_ballots: List[Dict],
        expert_conf: float,
        odds: Optional[Dict[str, float]] = None,
        additional_voters: Optional[List[Dict]] = None,
    ) -> Tuple[Dict[str, float], float, Dict]:
        """
        加权投票仲裁。

        Args:
            ensemble_probs: 集成模型概率
            ensemble_conf: 集成模型置信度
            expert_ballots: 各专家投票明细 (来自 ExpertVoteResult.ballots)
            expert_conf: 专家投票整体置信度
            odds: 赔率 {home, draw, away}
            additional_voters: 额外投票源 [{prediction, weight}]

        Returns:
            (arbitrated_probs, arbitrated_confidence, evidence)
        """
        voters = []  # List[(probs, weight, name)]

        # ── 投票源1: 集成模型 ──
        ensemble_weight = self.ENSEMBLE_BASE_WEIGHT * (0.5 + 0.5 * ensemble_conf)
        voters.append((ensemble_probs, ensemble_weight, 'EnsembleModel'))

        # ── 投票源2: 各专家 ──
        total_expert_weight = 0.0
        valid_ballots = [b for b in (expert_ballots or [])
                         if isinstance(b, dict) and b.get('status', '') == 'success']
        if valid_ballots:
            # 归一化专家权重
            raw_weights = [b.get('weight', 1.0) for b in valid_ballots]
            sum_raw = sum(raw_weights) if sum(raw_weights) > 0 else len(valid_ballots)
            for i, ballot in enumerate(valid_ballots):
                norm_w = raw_weights[i] / sum_raw
                exp_weight = self.EXPERT_TOTAL_WEIGHT * norm_w * (0.5 + 0.5 * ballot.get('confidence', 0.5))
                pred = ballot.get('prediction', {'home': DEFAULT_HOME_PROB, 'draw': DEFAULT_DRAW_PROB, 'away': DEFAULT_AWAY_PROB})
                voters.append((pred, exp_weight, f"Expert:{ballot.get('expert_id', '?')}"))
                total_expert_weight += exp_weight
        else:
            # 无有效专家 → 退化为只有集成模型
            total_expert_weight = 0

        # ── 投票源3: 赔率隐含概率 (锚定) ──
        if odds and all(v > 1.0 for v in odds.values()):
            implied = {
                k: (1.0 / v) for k, v in odds.items()
            }
            total_imp = sum(implied.values())
            odds_probs = {k: v / total_imp for k, v in implied.items()}
            voters.append((odds_probs, self.ODDS_ANCHOR_WEIGHT, 'OddsImplied'))

        # ── 投票源4: 额外信号 ──
        for av in (additional_voters or []):
            av_pred = av.get('prediction', {'home': DEFAULT_HOME_PROB, 'draw': DEFAULT_DRAW_PROB, 'away': DEFAULT_AWAY_PROB})
            av_weight = av.get('weight', 0.05)
            voters.append((av_pred, av_weight, av.get('name', 'ExtraVoter')))

        # ── 加权聚合 ──
        total_weight = sum(w for _, w, _ in voters)
        if total_weight <= 0:
            total_weight = 1.0

        aggregated = {'home': 0.0, 'draw': 0.0, 'away': 0.0}
        for probs, weight, _ in voters:
            w_norm = weight / total_weight
            for k in ['home', 'draw', 'away']:
                aggregated[k] += probs.get(k, 0.33) * w_norm

        # 拉普拉斯平滑 + 重归一化
        for k in aggregated:
            aggregated[k] += self.SMOOTHING
        total = sum(aggregated.values())
        aggregated = {k: v / total for k, v in aggregated.items()}

        # ── 仲裁置信度 ──
        # 1. 投票一致性 (各选民方向是否一致)
        vote_outcomes = [max(p, key=p.get) for p, _, _ in voters]
        mode_outcome = max(set(vote_outcomes), key=vote_outcomes.count)
        agreement_pct = vote_outcomes.count(mode_outcome) / len(vote_outcomes)

        # 2. 加权后最大值
        max_prob = max(aggregated.values())

        # 3. 融合置信度
        arbitrated_conf = (agreement_pct * 0.5 + max_prob * 0.3 +
                          ((ensemble_conf + expert_conf) / 2) * 0.2)

        # ── 证据 ──
        evidence = {
            'method': 'weighted_vote',
            'total_voters': len(voters),
            'voter_weights': {
                name: round(weight, 4) for _, weight, name in voters
            },
            'vote_agreement': round(agreement_pct, 4),
            'mode_outcome': mode_outcome,
            'max_prob': round(max_prob, 4),
            'voter_details': [
                {
                    'name': name,
                    'weight': round(weight, 4),
                    'probabilities': {k: round(v, 4) for k, v in probs.items()},
                    'outcome': max(probs, key=probs.get),
                }
                for probs, weight, name in voters
            ],
        }

        return aggregated, round(arbitrated_conf, 4), evidence


# ═══════════════════════════════════════════════════════════════
#  T07-R2: 元学习仲裁器 (Meta-Learning Arbiter)
# ═══════════════════════════════════════════════════════════════

class MetaLearningArbiter:
    """
    T07-R2: 元学习仲裁算法。

    基于历史预测准确率，为不同"场景"学习集成模型 vs 专家投票的信任权重。

    场景特征:
      - sigma_trap (赔率诱空/诱多信号)
      - rank_diff_factor (排名差距)
      - h2h_factor (交锋历史)
      - confidence_ratio (双方置信度比)
      - odds_range (赔率区间)
      - consensus_level (专家一致性)
      - league_tier (联赛等级)

    方法:
      1. 将特征空间离散化为有限数量的"场景桶"
      2. 每个桶维护: [ensemble_correct, expert_correct, total_predictions]
      3. 仲裁时查找最近邻场景桶的准确率 → 动态调整权重
      4. 结果回传后更新准确率

    场景桶: 基于 3 个主特征离散化:
      - sigma_trap: [-1, -0.3, 0, 0.3, 1] → 4 区间
      - confidence_ratio: [0.5, 0.8, 1.25, 2.0] → 5 区间
      - consensus_encoded: [split=0, low=1, moderate=2, high=3] → 4 区间
      总计: 4 × 5 × 4 = 80 个场景桶
    """

    # 场景桶边界
    SIGMA_BINS = [-float('inf'), -0.3, 0.0, 0.3, float('inf')]   # 4 区间
    CONF_RATIO_BINS = [-float('inf'), 0.5, 0.8, 1.25, 2.0, float('inf')]  # 5 区间
    CONSENSUS_MAP = {'none': 0, 'split': 0, 'low': 1, 'moderate': 2, 'high': 3, 'n/a': 1}  # 4 区间

    MIN_SAMPLES_FOR_TRUST = 5           # 场景桶最少样本数才信任

    # 先验信任权重 (无历史数据时)
    PRIOR_ENSEMBLE_WEIGHT = 0.55
    PRIOR_EXPERT_WEIGHT = 0.45

    def __init__(self, db_path: str = 'data/football_data.db'):
        self._db_path = db_path
        # 场景桶: key=(sigma_bin, conf_bin, consensus_bin) → stats
        self._scenario_buckets: Dict[Tuple[int, int, int], Dict] = {}
        self._load_historical_accuracy()

    # ═══════════════════════════════════════════════════════════
    #  场景桶索引
    # ═══════════════════════════════════════════════════════════

    def _bucket_key(self, scenario: Dict) -> Tuple[int, int, int]:
        """将场景特征映射到桶索引"""
        sigma = float(scenario.get('sigma_trap', 0.0))
        conf_ratio = float(scenario.get('confidence_ratio', 1.0))
        consensus = str(scenario.get('consensus_level', 'moderate'))

        sigma_idx = np.digitize(sigma, self.SIGMA_BINS) - 1
        sigma_idx = max(0, min(sigma_idx, len(self.SIGMA_BINS) - 2))

        conf_idx = np.digitize(conf_ratio, self.CONF_RATIO_BINS) - 1
        conf_idx = max(0, min(conf_idx, len(self.CONF_RATIO_BINS) - 2))

        consensus_idx = self.CONSENSUS_MAP.get(consensus, 1)

        return (sigma_idx, conf_idx, consensus_idx)

    def _load_historical_accuracy(self):
        """从数据库加载历史预测准确率，构建场景桶统计"""
        try:
            import sqlite3
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 查询已验证的预测 + 关联特征
            cur.execute("""
                SELECT p.home_prob, p.draw_prob, p.away_prob,
                       p.prediction_type, p.agent_analysis,
                       f.sigma_trap,
                       COALESCE(m.home_score, -1) as actual_home,
                       COALESCE(m.away_score, -1) as actual_away
                FROM predictions p
                LEFT JOIN features f ON f.match_id = p.match_id
                LEFT JOIN matches m ON m.match_id = p.match_id
                WHERE m.home_score IS NOT NULL
                  AND m.away_score IS NOT NULL
                ORDER BY p.prediction_time DESC
                LIMIT 1000
            """)
            rows = cur.fetchall()
            conn.close()

            if not rows:
                logger.info("[MetaLearn] 无历史验证数据，使用先验权重")
                return

            loaded = 0
            for row in rows:
                try:
                    actual_home = int(row['actual_home'])
                    actual_away = int(row['actual_away'])

                    # 实际结果
                    if actual_home > actual_away:
                        actual_outcome = 'home'
                    elif actual_home < actual_away:
                        actual_outcome = 'away'
                    else:
                        actual_outcome = 'draw'

                    # 集成模型预测
                    ensemble_probs = {
                        'home': float(row['home_prob'] or 0.33),
                        'draw': float(row['draw_prob'] or DEFAULT_DRAW_PROB),
                        'away': float(row['away_prob'] or 0.33),
                    }
                    ensemble_outcome = max(ensemble_probs, key=ensemble_probs.get)

                    # 专家投票预测 (从 agent_analysis JSON 提取)
                    expert_outcome = None
                    agent_data = row['agent_analysis']
                    if agent_data:
                        agent = json.loads(agent_data) if isinstance(agent_data, str) else agent_data
                        expert_pred = agent.get('prediction', 'draw')
                        expert_outcome = str(expert_pred) if expert_pred else None

                    sigma_trap = float(row['sigma_trap'] or 0.0)

                    # 简化场景桶 (只用 sigma_trap)
                    sigma_idx = max(0, min(np.digitize(sigma_trap, self.SIGMA_BINS) - 1,
                                           len(self.SIGMA_BINS) - 2))

                    # 使用简化桶 key
                    for cb in range(len(self.CONF_RATIO_BINS) - 1):
                        for csb in range(4):
                            bucket_key = (sigma_idx, cb, csb)
                            if bucket_key not in self._scenario_buckets:
                                self._scenario_buckets[bucket_key] = {
                                    'ensemble_correct': 0, 'expert_correct': 0, 'total': 0
                                }

                    # 仅记录到匹配的桶 (所有 conf_ratio/consensus 桶共享同一 sigma 数据)
                    for cb in range(len(self.CONF_RATIO_BINS) - 1):
                        for csb in range(4):
                            bk = (sigma_idx, cb, csb)
                            self._scenario_buckets[bk]['total'] += 1
                            if ensemble_outcome == actual_outcome:
                                self._scenario_buckets[bk]['ensemble_correct'] += 1
                            if expert_outcome and expert_outcome == actual_outcome:
                                self._scenario_buckets[bk]['expert_correct'] += 1

                    loaded += 1
                except (Exception, KeyError, IndexError):
                    continue

            logger.info(f"[MetaLearn] 从数据库加载 {loaded} 条历史记录，"
                        f"{len(self._scenario_buckets)} 个场景桶")

        except (Exception, KeyError, IndexError) as e:
            logger.warning(f"[MetaLearn] 历史数据加载失败(非致命): {e}")

    # ═══════════════════════════════════════════════════════════
    #  元学习权重推断
    # ═══════════════════════════════════════════════════════════

    def infer_weights(
        self,
        scenario: Dict,
    ) -> Tuple[float, float, Dict]:
        """
        基于历史相似场景推断信任权重。

        Args:
            scenario: 场景特征 {'sigma_trap', 'confidence_ratio',
                                'consensus_level', 'odds_range', ...}

        Returns:
            (ensemble_weight, expert_weight, evidence)
        """
        bucket_key = self._bucket_key(scenario)
        stats = self._scenario_buckets.get(bucket_key)

        evidence = {
            'method': 'meta_learning',
            'bucket_key': bucket_key,
            'scenario_features': scenario,
        }

        if stats is None or stats['total'] < self.MIN_SAMPLES_FOR_TRUST:
            # 不足样本 → 回退到先验
            evidence['source'] = 'prior'
            evidence['reason'] = f"场景桶样本不足 "
            evidence['samples'] = stats['total'] if stats else 0
            evidence['ensemble_weight'] = self.PRIOR_ENSEMBLE_WEIGHT
            evidence['expert_weight'] = self.PRIOR_EXPERT_WEIGHT
            return self.PRIOR_ENSEMBLE_WEIGHT, self.PRIOR_EXPERT_WEIGHT, evidence

        # 计算准确率
        ens_acc = stats['ensemble_correct'] / max(stats['total'], 1)
        exp_acc = stats['expert_correct'] / max(stats['total'], 1)

        # Laplace 平滑
        ens_acc_smooth = (stats['ensemble_correct'] + 1) / (stats['total'] + 3)
        exp_acc_smooth = (stats['expert_correct'] + 1) / (stats['total'] + 3)

        # 归一化为权重
        total_acc = ens_acc_smooth + exp_acc_smooth
        if total_acc <= 0:
            ens_weight = self.PRIOR_ENSEMBLE_WEIGHT
            exp_weight = self.PRIOR_EXPERT_WEIGHT
        else:
            ens_weight = ens_acc_smooth / total_acc
            exp_weight = exp_acc_smooth / total_acc

        evidence['source'] = 'historical'
        evidence['reason'] = (
            f"场景桶 #{bucket_key}: {stats['total']}样本, "
            f"集成准确率={ens_acc:.1%}, 专家准确率={exp_acc:.1%}"
        )
        evidence['samples'] = stats['total']
        evidence['ensemble_accuracy'] = round(ens_acc, 4)
        evidence['expert_accuracy'] = round(exp_acc, 4)
        evidence['ensemble_weight'] = round(ens_weight, 4)
        evidence['expert_weight'] = round(exp_weight, 4)

        return round(ens_weight, 4), round(exp_weight, 4), evidence

    # ═══════════════════════════════════════════════════════════
    #  结果反馈 (在线学习)
    # ═══════════════════════════════════════════════════════════

    def record_outcome(
        self,
        scenario: Dict,
        ensemble_correct: bool,
        expert_correct: bool,
    ):
        """
        记录预测结果，更新场景桶统计（在线学习）。

        Args:
            scenario: 场景特征 (同 infer_weights)
            ensemble_correct: 集成模型是否正确
            expert_correct: 专家投票是否正确
        """
        bucket_key = self._bucket_key(scenario)
        if bucket_key not in self._scenario_buckets:
            self._scenario_buckets[bucket_key] = {
                'ensemble_correct': 0, 'expert_correct': 0, 'total': 0
            }
        self._scenario_buckets[bucket_key]['total'] += 1
        if ensemble_correct:
            self._scenario_buckets[bucket_key]['ensemble_correct'] += 1
        if expert_correct:
            self._scenario_buckets[bucket_key]['expert_correct'] += 1

        stats = self._scenario_buckets[bucket_key]
        logger.debug(
            f"[MetaLearn] 桶#{bucket_key} 更新: "
            f"total={stats['total']}, ens_acc={stats['ensemble_correct']/stats['total']:.2%}"
        )


# ═══════════════════════════════════════════════════════════════
#  T07: 仲裁引擎 (Arbitration Engine) - 主入口
# ═══════════════════════════════════════════════════════════════

class ArbitrationEngine:
    """
    T07: 仲裁引擎主模块。

    整合不一致规则检测 + 加权投票 + 元学习，输出最终决策信标。

    调用流程:
      1. InconsistencyRules.assess() → 冲突评估
      2. 无冲突 → 直接使用融合结果 (跳过仲裁)
      3. 有冲突 → 选择策略:
         a. 低/中等冲突 → WeightedVoteArbiter
         b. 高/严重冲突 + 有历史数据 → MetaLearningArbiter
         c. 严重冲突 + 无历史数据 → ConservativeMin
      4. 生成 DecisionBeacon

    用法:
        engine = ArbitrationEngine()
        beacon = engine.arbitrate(...)
    """

    STRATEGY_ORDER = [
        'weighted_vote',     # 默认：加权投票
        'meta_learning',     # 有历史数据时：元学习
        'conservative_min', # 兜底：保守取小
    ]

    def __init__(self, db_path: str = 'data/football_data.db'):
        self.rules = InconsistencyRules()
        self.weighted_voter = WeightedVoteArbiter()
        self.meta_learner = MetaLearningArbiter(db_path=db_path)

        # 便捷引用
        self.OUTCOME_CN = {'home': '主胜', 'draw': '平局', 'away': '客胜'}

    def arbitrate(
        self,
        ensemble_probs: Dict[str, float],
        ensemble_conf: float,
        expert_vote_result: Any,          # ExpertVoteResult (或 dict)
        confidence_comparison: Any = None,  # ConfidenceComparison (或 dict)
        odds: Optional[Dict[str, float]] = None,
        scenario: Optional[Dict] = None,
    ) -> DecisionBeacon:
        """
        执行仲裁。

        Args:
            ensemble_probs: 集成模型概率 {home, draw, away}
            ensemble_conf: 集成模型置信度
            expert_vote_result: ExpertVoteResult dataclass 或 dict
            confidence_comparison: ConfidenceComparison dataclass 或 dict
            odds: 赔率 {home, draw, away}
            scenario: 场景特征 {sigma_trap, rank_diff, league, ...}

        Returns:
            DecisionBeacon: 决策信标
        """
        start = time.perf_counter()
        scenario = scenario or {}

        # ── 提取专家投票信息 ──
        try:
            expert_prediction = expert_vote_result.prediction
            expert_outcome = expert_vote_result.predicted_outcome
            expert_conf = expert_vote_result.confidence
            expert_ballots = expert_vote_result.ballots
            consensus_level = getattr(expert_vote_result, 'consensus_level', 'moderate')
            majority_pct = getattr(expert_vote_result, 'majority_pct', 0.6)
            active_experts = getattr(expert_vote_result, 'active_experts', 0)
        except AttributeError:
            # 兼容 dict
            expert_prediction = expert_vote_result.get('prediction',
                                                       {'home': DEFAULT_HOME_PROB, 'draw': DEFAULT_DRAW_PROB, 'away': DEFAULT_AWAY_PROB})
            expert_outcome = expert_vote_result.get('predicted_outcome', 'draw')
            expert_conf = expert_vote_result.get('confidence', 0.5)
            expert_ballots = expert_vote_result.get('ballots', [])
            consensus_level = expert_vote_result.get('consensus_level', 'moderate')
            majority_pct = expert_vote_result.get('majority_pct', 0.6)
            active_experts = expert_vote_result.get('active_experts', 0)

        ensemble_outcome = max(ensemble_probs, key=ensemble_probs.get)

        # ── 计算赔率隐含概率 ──
        odds_implied = None
        if odds and all(v > 1.0 for v in odds.values()):
            implied = {k: 1.0 / v for k, v in odds.items()}
            total = sum(implied.values())
            odds_implied = {k: v / total for k, v in implied.items()}

        # ── 步骤1: 冲突评估 ──
        assessment = self.rules.assess(
            ensemble_outcome=ensemble_outcome,
            expert_outcome=expert_outcome,
            ensemble_conf=ensemble_conf,
            expert_conf=expert_conf,
            consensus_level=consensus_level,
            majority_pct=majority_pct,
            active_experts=active_experts,
            odds_implied=odds_implied,
        )

        evidence = {
            'conflict_assessment': {
                'type': assessment.conflict_type,
                'severity': assessment.severity,
                'rules': assessment.trigger_rules,
                'arbitrable': assessment.arbitrable,
                'reason': assessment.reason,
            },
        }

        # ── 步骤2: 无冲突 → 直接返回 (不仲裁) ──
        if assessment.conflict_type == ConflictType.NONE.value:
            elapsed = (time.perf_counter() - start) * 1000
            return DecisionBeacon(
                signal='HOLD',
                confidence=ensemble_conf,
                strength='NORMAL',
                prediction=ensemble_probs,
                predicted_outcome=ensemble_outcome,
                predicted_outcome_cn=self.OUTCOME_CN.get(ensemble_outcome, ensemble_outcome),
                policy=DecisionPolicy.ENSEMBLE_ONLY.value,
                policy_reason='两系统预测一致，无需仲裁',
                evidence=evidence,
                execution_time_ms=round(elapsed, 1),
            )

        # ── 步骤3: 选择仲裁策略 ──
        severity = assessment.severity

        # 3a. 尝试元学习 (高冲突 + 有历史数据)
        if severity in ('high', 'critical'):
            ml_scenario = {
                'sigma_trap': float(scenario.get('sigma_trap', 0.0)),
                'confidence_ratio': (
                    max(ensemble_conf, 0.01) / max(expert_conf, 0.01)
                ),
                'consensus_level': consensus_level,
                'rank_diff': float(scenario.get('rank_diff', 0.0)),
            }
            ens_weight, exp_weight, ml_evidence = self.meta_learner.infer_weights(ml_scenario)
            evidence['meta_learning'] = ml_evidence

            if ml_evidence.get('source') == 'historical':
                # 元学习有足够样本 → 使用学习到的权重融合
                policy = DecisionPolicy.META_LEARNING
                final_probs = {
                    k: ensemble_probs.get(k, 0.33) * ens_weight +
                    expert_prediction.get(k, 0.33) * exp_weight + 0.005
                    for k in ['home', 'draw', 'away']
                }
                total = sum(final_probs.values())
                final_probs = {k: v / total for k, v in final_probs.items()}

                final_conf = ensemble_conf * ens_weight + expert_conf * exp_weight
                policy_reason = (
                    f'元学习: 集成权重={ens_weight:.3f}(准确率{ml_evidence.get("ensemble_accuracy", 0):.1%}), '
                    f'专家权重={exp_weight:.3f}(准确率{ml_evidence.get("expert_accuracy", 0):.1%}), '
                    f'基于{ml_evidence.get("samples", 0)}条历史数据'
                )
            else:
                # 元学习样本不足 → 回退到加权投票
                policy = DecisionPolicy.WEIGHTED_FUSION
                final_probs, final_conf, wv_evidence = self.weighted_voter.arbitrate(
                    ensemble_probs, ensemble_conf,
                    expert_ballots, expert_conf,
                    odds=odds,
                )
                evidence['weighted_vote'] = wv_evidence
                policy_reason = '元学习样本不足，回退至加权投票'
        else:
            # 3b. 低/中等冲突 → 加权投票
            policy = DecisionPolicy.WEIGHTED_FUSION
            final_probs, final_conf, wv_evidence = self.weighted_voter.arbitrate(
                ensemble_probs, ensemble_conf,
                expert_ballots, expert_conf,
                odds=odds,
            )
            evidence['weighted_vote'] = wv_evidence
            policy_reason = f'{assessment.severity.upper()} 级别冲突，采用加权投票仲裁'

        # ── 步骤4: 保守兜底 ──
        # 极端严重冲突 → 降低置信度
        if severity == 'critical':
            final_conf *= 0.7
            policy_reason += ' (CRITICAL冲突 → 置信度×0.7)'

        final_outcome = max(final_probs, key=final_probs.get)

        # ── 步骤5: 生成信号 ──
        signal, strength = self._generate_signal(
            policy, final_conf, severity, assessment
        )

        elapsed = (time.perf_counter() - start) * 1000

        logger.info(
            f"[Arbitration] 完成: 冲突={assessment.conflict_type}/{severity}, "
            f"策略={policy.value}, 结果={self.OUTCOME_CN.get(final_outcome)}"
            f"(conf={final_conf:.3f}), 信号={signal}/{strength}, "
            f"耗时={elapsed:.0f}ms"
        )

        return DecisionBeacon(
            signal=signal,
            confidence=round(final_conf, 4),
            strength=strength,
            prediction={k: round(v, 4) for k, v in final_probs.items()},
            predicted_outcome=final_outcome,
            predicted_outcome_cn=self.OUTCOME_CN.get(final_outcome, final_outcome),
            policy=policy.value,
            policy_reason=policy_reason,
            evidence=evidence,
            execution_time_ms=round(elapsed, 1),
        )

    # ═══════════════════════════════════════════════════════════
    #  信号生成
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _generate_signal(
        policy: DecisionPolicy,
        confidence: float,
        severity: str,
        assessment: ConflictAssessment,
    ) -> Tuple[str, str]:
        """生成 BUY/SELL/HOLD/PASS 信号和强度"""
        if severity == 'critical':
            return ('PASS', 'NONE')

        if confidence >= 0.75 and not assessment.trigger_rules:
            return ('BUY', 'STRONG')
        elif confidence >= 0.65:
            return ('HOLD', 'NORMAL')
        elif confidence >= 0.50:
            return ('HOLD', 'WEAK')
        elif severity == 'high':
            return ('PASS', 'NONE')
        else:
            return ('HOLD', 'WEAK')


# ═══════════════════════════════════════════════════════════════
#  API 便捷函数
# ═══════════════════════════════════════════════════════════════

_global_arbitration_engine: Optional[ArbitrationEngine] = None


def get_arbitration_engine(db_path: str = 'data/football_data.db') -> ArbitrationEngine:
    """全局懒加载仲裁引擎"""
    global _global_arbitration_engine
    if _global_arbitration_engine is None:
        _global_arbitration_engine = ArbitrationEngine(db_path=db_path)
    return _global_arbitration_engine


def quick_arbitrate(
    ensemble_probs: Dict[str, float],
    ensemble_conf: float,
    expert_vote_result: Any,
    confidence_comparison: Any = None,
    odds: Optional[Dict[str, float]] = None,
    scenario: Optional[Dict] = None,
) -> DecisionBeacon:
    """快速仲裁（静态方法）"""
    engine = get_arbitration_engine()
    return engine.arbitrate(
        ensemble_probs=ensemble_probs,
        ensemble_conf=ensemble_conf,
        expert_vote_result=expert_vote_result,
        confidence_comparison=confidence_comparison,
        odds=odds,
        scenario=scenario,
    )
