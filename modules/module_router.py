#!/usr/bin/env python3
"""
哨响AI — 动态模块路由器 (ModuleRouter)
=========================================
可插拔的专家适用性评分与选择系统。

设计原则:
    1. 每个专家可以注册自己的适用性评分函数 — 而非在 orchestrator 中硬编码
    2. 新专家只需注册 scorer 即可参与路由，无需修改路由代码
    3. 支持默认通用评分器作为后备

对比旧版:
    旧版 MatchAnalyzerAgent 中硬编码了10个 _score_xxx() 方法
    新版: 每个专家注册时附带 scoring 函数，路由自动发现

用法:
    router = ModuleRouter()

    # 注册评分器
    router.register_scorer('trend_analyzer', my_trend_scorer)
    router.register_scorer('new_expert', my_new_scorer)

    # 批量评分
    scores = router.score_all(match_data)
    selected = router.select(scores)
"""
import logging
from typing import Dict, List, Callable, Optional

logger = logging.getLogger(__name__)

class ModuleRouter:
    """
    动态模块路由器

    职责:
        1. 管理专家→评分函数的映射
        2. 对比赛数据执行批量适用性评分
        3. 基于阈值和优先级选择专家
        4. 确保最少/最多选择数量
    """

    # 默认选择参数
    DEFAULT_MIN_MODULES = 3
    DEFAULT_MAX_MODULES = 7
    DEFAULT_SCORE_THRESHOLD = 0.3
    DEFAULT_FALLBACK = ['trend_analyzer', 'quant_trader']

    def __init__(self, config: Dict = None):
        """
        Args:
            config: {
                'min_modules': 3,
                'max_modules': 7,
                'score_threshold': 0.3,
                'fallback_modules': ['trend_analyzer', ...],
                'per_module_thresholds': {'trend_analyzer': 0.6, ...},
            }
        """
        config = config or {}
        self._scorers: Dict[str, Callable] = {}            # expert_id → scorer函数
        self._per_module_thresholds: Dict[str, float] = {} # 每个模块的阈值覆盖

        self.min_modules = config.get('min_modules', self.DEFAULT_MIN_MODULES)
        self.max_modules = config.get('max_modules', self.DEFAULT_MAX_MODULES)
        self.score_threshold = config.get('score_threshold', self.DEFAULT_SCORE_THRESHOLD)
        self.fallback_modules = config.get('fallback_modules', self.DEFAULT_FALLBACK)

        # 应用每个模块的阈值
        for mod_id, threshold in config.get('per_module_thresholds', {}).items():
            self._per_module_thresholds[mod_id] = threshold

    # ---- 注册评分器 ----

    def register_scorer(self, expert_id: str,
                        scorer: Callable[[Dict], float],
                        threshold: float = None) -> None:
        """
        注册专家的适用性评分函数

        Args:
            expert_id: 专家ID
            scorer: 评分函数 signature: (match_features: Dict) -> float (0-1)
            threshold: 该专家的调用阈值(覆盖全局默认值)
        """
        self._scorers[expert_id] = scorer
        if threshold is not None:
            self._per_module_thresholds[expert_id] = threshold
        logger.debug(f"[Router] 注册评分器: {expert_id}")

    def unregister_scorer(self, expert_id: str) -> bool:
        """移除评分器"""
        if expert_id in self._scorers:
            del self._scorers[expert_id]
            self._per_module_thresholds.pop(expert_id, None)
            return True
        return False

    def set_threshold(self, expert_id: str, threshold: float):
        """设置单个专家的调用阈值"""
        self._per_module_thresholds[expert_id] = threshold

    # ---- 评分与选择 ----

    def score_all(self, match_data: Dict) -> Dict[str, float]:
        """
        对所有已注册专家进行适用性评分

        Args:
            match_data: 完整比赛数据(由特征提取器预处理)

        Returns:
            {expert_id: applicability_score, ...}  (0-1)
        """
        # 提取特征(对旧版兼容)
        features = match_data.get('_features', match_data)

        scores = {}
        for expert_id, scorer in self._scorers.items():
            try:
                score = scorer(features)
                score = max(0.0, min(1.0, float(score)))
                scores[expert_id] = score
            except (Exception, ValueError, KeyError, IndexError, requests.exceptions.RequestException) as e:
                logger.warning(f"[Router] 评分器 {expert_id} 异常: {e}")
                scores[expert_id] = 0.0

        return scores

    def select(self, scores: Dict[str, float],
               active_experts: List[str] = None) -> List[str]:
        """
        基于评分选择应调用的专家

        策略:
            1. 过滤: score >= 该专家的调用阈值
            2. 只选择活跃可用的专家
            3. 不够 min_modules → 补充 fallback
            4. 超过 max_modules → 取最高分

        Args:
            scores: {expert_id: score}
            active_experts: 当前活跃的专家ID列表

        Returns:
            选中的专家ID列表(按分数降序)
        """
        active_set = set(active_experts) if active_experts else None

        selected = []
        for expert_id, score in scores.items():
            # 检查可用性
            if active_set is not None and expert_id not in active_set:
                continue

            # 检查阈值
            threshold = self._per_module_thresholds.get(
                expert_id, self.score_threshold
            )
            if score >= threshold:
                selected.append((expert_id, score))

        # 按分数降序
        selected.sort(key=lambda x: x[1], reverse=True)

        # 提取ID列表
        result = [eid for eid, _ in selected]

        # 不够 → 补充 fallback
        if len(result) < self.min_modules:
            for fb in self.fallback_modules:
                if fb not in result and fb in scores:
                    if active_set is None or fb in active_set:
                        result.append(fb)
                if len(result) >= self.min_modules:
                    break

        # 太多 → 截断
        if len(result) > self.max_modules:
            result = result[:self.max_modules]

        return result

    def select_with_meta(self, scores: Dict[str, float],
                         active_experts: List[str] = None) -> List[Dict]:
        """
        选择专家并返回元数据

        Returns:
            [{expert_id, score, threshold, reason}, ...]
        """
        active_set = set(active_experts) if active_experts else None

        candidates = []
        rejected = []

        for expert_id, score in scores.items():
            if active_set is not None and expert_id not in active_set:
                rejected.append({
                    'expert_id': expert_id, 'score': score,
                    'reason': 'not_active',
                })
                continue

            threshold = self._per_module_thresholds.get(
                expert_id, self.score_threshold
            )

            if score >= threshold:
                candidates.append({
                    'expert_id': expert_id,
                    'score': round(score, 4),
                    'threshold': threshold,
                    'reason': 'selected',
                })
            else:
                rejected.append({
                    'expert_id': expert_id,
                    'score': round(score, 4),
                    'threshold': threshold,
                    'reason': f'score {score:.2f} < threshold {threshold:.2f}',
                })

        candidates.sort(key=lambda x: x['score'], reverse=True)
        result = candidates[:self.max_modules]

        # 补充 fallback
        if len(result) < self.min_modules:
            for fb in self.fallback_modules:
                if fb not in {r['expert_id'] for r in result} and fb in scores:
                    if active_set is None or fb in active_set:
                        result.append({
                            'expert_id': fb,
                            'score': round(scores[fb], 4),
                            'threshold': self.score_threshold,
                            'reason': 'fallback',
                        })
                if len(result) >= self.min_modules:
                    break

        return result

    def get_all_scorers(self) -> List[str]:
        """获取所有已注册评分器的专家ID"""
        return list(self._scorers.keys())

# ================================================================
# 内置通用评分器工厂
# ================================================================
def create_default_scorer_from_meta(meta) -> Callable:
    """
    基于专家元数据的默认评分器工厂

    当专家未提供自定义评分器时，根据 input_schema 生成通用评分:
        - 检查 required 字段的可用率
        - 结合 phase 优先级
    """
    required_fields = meta.input_schema.required

    def default_scorer(features: Dict) -> float:
        if not required_fields:
            return 0.5
        available = 0
        for field in required_fields:
            current = features
            for part in field.split('.'):
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    current = None
                    break
            if current is not None:
                available += 1
        return available / len(required_fields)

    return default_scorer

# ================================================================
# 预设评分器(兼容旧版 MatchAnalyzerAgent)
# ================================================================
def build_preset_scorers() -> Dict[str, Callable]:
    """
    构建预设评分器 — 兼容旧版硬编码的 _score_xxx() 逻辑

    保留旧版评分逻辑，但以可插拔的方式提供。
    新专家可选择性覆盖。
    """
    def _score_trend(f: Dict) -> float:
        score = 0.0
        if f.get('season_stage') in ('final', 'relegation', 'championship'):
            score += 0.4
        if f.get('match_importance', 0) > 0.7:
            score += 0.3
        if f.get('is_derby'):
            score += 0.2
        if f.get('data_completeness', 0) > 0.8:
            score += 0.1
        return min(score, 1.0)

    def _score_alpha(f: Dict) -> float:
        score = 0.5
        if f.get('data_completeness', 0) > 0.9:
            score += 0.3
        if f.get('strength_diff', 0) > 0.6:
            score += 0.2
        return min(score, 1.0)

    def _score_referee(f: Dict) -> float:
        if not f.get('has_referee_data'):
            return 0.0
        score = 0.5
        if f.get('match_importance', 0) > 0.7:
            score += 0.3
        if f.get('is_derby'):
            score += 0.2
        return min(score, 1.0)

    def _score_upset(f: Dict) -> float:
        score = 0.0
        if f.get('strength_diff', 0) > 0.7:
            score += 0.4
        if f.get('odds_volatility', 0) > 0.3:
            score += 0.3
        if f.get('match_importance', 0) > 0.8:
            score += 0.2
        if f.get('data_completeness', 0) > 0.9:
            score += 0.1
        return min(score, 1.0)

    def _score_media(f: Dict) -> float:
        if not f.get('has_media_data'):
            return 0.2
        score = 0.3
        if f.get('match_importance', 0) > 0.8:
            score += 0.4
        if f.get('is_derby'):
            score += 0.2
        if f.get('season_stage') in ('final', 'relegation'):
            score += 0.1
        return min(score, 1.0)

    def _score_coach(f: Dict) -> float:
        if not f.get('has_coach_data'):
            return 0.0
        score = 0.4
        if f.get('recent_coach_change'):
            score += 0.4
        if f.get('match_importance', 0) > 0.7:
            score += 0.2
        return min(score, 1.0)

    def _score_quant(f: Dict) -> float:
        score = 0.3
        if f.get('odds_volatility', 0) > 0:
            score += 0.3
        if f.get('arbitrage_opportunity', 0) > 0.1:
            score += 0.2
        if f.get('data_completeness', 0) > 0.9:
            score += 0.2
        return min(score, 1.0)

    def _score_timespace(f: Dict) -> float:
        score = 0.0
        if f.get('travel_distance', 0) > 0.5:
            score += 0.4
        if f.get('timezone_diff', 0) > 0.3:
            score += 0.3
        return min(score, 1.0)

    def _score_arbitrage(f: Dict) -> float:
        score = 0.0
        if f.get('arbitrage_opportunity', 0) > 0.05:
            score += 0.6
        if f.get('odds_volatility', 0) > 0.2:
            score += 0.2
        if f.get('match_importance', 0) > 0.7:
            score += 0.1
        if f.get('data_completeness', 0) > 0.9:
            score += 0.1
        return min(score, 1.0)

    def _score_goal_timing(f: Dict) -> float:
        score = 0.3
        if f.get('strength_diff', 0) > 0.5:
            score += 0.3
        if f.get('data_completeness', 0) > 0.7:
            score += 0.2
        return min(score, 1.0)

    # 新增模块的默认评分器
    def _score_keeper(f: Dict) -> float:
        score = 0.3
        if f.get('has_keeper_data'):
            score += 0.4
        if f.get('match_importance', 0) > 0.7:
            score += 0.2
        if f.get('is_derby'):
            score += 0.1
        return min(score, 1.0)

    def _score_attack_efficiency(f: Dict) -> float:
        score = 0.4
        if f.get('strength_diff', 0) > 0.4:
            score += 0.3
        if f.get('data_completeness', 0) > 0.7:
            score += 0.2
        return min(score, 1.0)

    def _score_goal_keeper(f: Dict) -> float:
        score = 0.3
        if f.get('has_keeper_data'):
            score += 0.4
        if f.get('match_importance', 0) > 0.7:
            score += 0.2
        return min(score, 1.0)

    return {
        # Phase 1
        'trend_analyzer': _score_trend,
        'h2h_analyzer': _score_alpha,
        # Phase 2
        'alpha_decision': _score_alpha,
        'quant_trader': _score_quant,
        'referee_model': _score_referee,
        # Phase 3
        'keeper_goal': _score_keeper,
        'attack_efficiency': _score_attack_efficiency,
        'timespace_detector': _score_timespace,
        # Phase 4
        'arbitrage_detector': _score_arbitrage,
        'upset_detector': _score_upset,
        'media_intelligence': _score_media,
        # 其他
        'coach_tactics': _score_coach,
        'goal_timing': _score_goal_timing,
    }
