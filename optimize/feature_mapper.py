"""
哨响AI - 特征映射兼容层 (T19)
================================
解决多代特征体系之间的映射、默认值填充、质量验证问题。

核心设计:
  1. 特征映射 — 旧名→新名双向映射, 自动补全
  2. 默认值策略 — 三级策略: 精确默认 > 统计量插补 > 零值
  3. 映射质量验证 — 覆盖率/缺失率/分布偏移检测

特征体系演进:
  V1 (19 base): a1-a6, sigma_trap, lambda_crush, epsilon_senti, ...
  V2 (market):  mkt_* 前缀, 29 个市场特征
  V3 (injury):  7 个伤病特征 (injury_index_*, attack_impact_diff, ...)
  V4 (rolling): rw_* 前缀, ~50 个滚动窗口特征
  V5 (sequence): 18 维序列特征 (DL 模型专用)

用法:
    from optimize.feature_mapper import FeatureMapper
    mapper = FeatureMapper()

    # 映射旧特征名
    new_names = mapper.map_columns(['a1', 'sigma_trap_v1'])

    # 对齐 DataFrame 到目标特征集
    df_aligned = mapper.align_dataframe(df, target='v1_extended')

    # 验证映射质量
    report = mapper.validate(df_aligned)

    # 获取兼容的 DataFrame (用于已训练模型)
    df_compat = mapper.make_compatible(df, model_feature_names=saved_model.feature_names)
"""

import logging
import warnings
from typing import Dict, List, Optional, Tuple, Set, Any
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
#  特征版本定义
# ═══════════════════════════════════════════════════════════════════

class FeatureVersion(Enum):
    """特征体系版本"""
    V1_BASE = 'v1'             # 19 基础特征 (config.yaml)
    V2_MARKET = 'v2'           # V1 + 29 市场特征
    V3_INJURY = 'v3'           # V2 + 7 伤病特征
    V4_ROLLING = 'v4'          # V3 + ~50 滚动特征
    V1_EXTENDED = 'v1_ext'     # V1 + 6 交互项 (GBDT 标配)

# ═══════════════════════════════════════════════════════════════════
#  V1 基础特征 (19 个) — 与 config.yaml / gbdt_adapter 完全一致
# ═══════════════════════════════════════════════════════════════════

V1_BASE_FEATURES: List[str] = [
    'a1', 'a2', 'a3', 'a4', 'a5', 'a6',
    'sigma_trap', 'lambda_crush', 'epsilon_senti',
    'rank_diff_factor', 'form_momentum', 'h2h_factor',
    'rank_factor', 'form_factor',
    'aerial_advantage', 'press_intensity', 'card_risk',
    'beta_dev', 'delta_fatigue',
]

V1_DEFAULTS: Dict[str, float] = {
    'a1': 0.0, 'a2': 0.5, 'a3': 0.5, 'a4': 0.0, 'a5': 0.0, 'a6': 0.0,
    'sigma_trap': 0.0, 'lambda_crush': 1.0, 'epsilon_senti': 0.5,
    'rank_diff_factor': 0.0, 'form_momentum': 0.0, 'h2h_factor': 0.0,
    'rank_factor': 0.5, 'form_factor': 0.5,
    'aerial_advantage': 1.0, 'press_intensity': 0.0, 'card_risk': 0.0,
    'beta_dev': 0.0, 'delta_fatigue': 1.0,
}

# V1 交互项 (6 个) — gbdt_adapter._build_interactions()
V1_INTERACTION_FEATURES: List[str] = [
    'ix_a1_sigma', 'ix_a2_lambda', 'ix_a3_epsilon',
    'ix_a1_a2', 'ix_rank_form', 'ix_power_gap',
]

V1_INTERACTION_FORMULAS: Dict[str, str] = {
    'ix_a1_sigma': 'a1 * sigma_trap',
    'ix_a2_lambda': '(a2 - 0.5) * (lambda_crush - 1.0)',
    'ix_a3_epsilon': '(a3 - 0.5) * (epsilon_senti - 0.5)',
    'ix_a1_a2': 'a1 * (a2 - 0.5)',
    'ix_rank_form': 'rank_diff_factor * form_momentum',
    'ix_power_gap': 'abs(0.4*a1 + 0.4*(a2-0.5) + 0.2*clip(rank_diff_factor,-1,1))',
}

V1_INTERACTION_DEFAULTS: Dict[str, float] = {
    'ix_a1_sigma': 0.0, 'ix_a2_lambda': 0.0, 'ix_a3_epsilon': 0.0,
    'ix_a1_a2': 0.0, 'ix_rank_form': 0.0, 'ix_power_gap': 0.0,
}

# 对称特征 (可正可负, 裁剪时上下界对称)
SYMMETRIC_FEATURES: Set[str] = {
    'a1', 'a4', 'a5', 'a6', 'sigma_trap',
    'rank_diff_factor', 'form_momentum', 'h2h_factor', 'beta_dev',
}

# ═══════════════════════════════════════════════════════════════════
#  V2 市场特征 (29 个) — market_features.py
# ═══════════════════════════════════════════════════════════════════

V2_MARKET_FEATURES: List[str] = [
    'mkt_implied_home', 'mkt_implied_draw', 'mkt_implied_away',
    'mkt_fair_home', 'mkt_fair_draw', 'mkt_fair_away',
    'mkt_overround', 'mkt_home_advantage',
    'mkt_odds_drift_home', 'mkt_odds_drift_draw', 'mkt_odds_drift_away',
    'mkt_drift_direction', 'mkt_volatility', 'mkt_max_jump',
    'mkt_drift_magnitude',
    'mkt_bookmaker_count', 'mkt_tightness', 'mkt_home_cv', 'mkt_away_cv',
    'mkt_divergence_home', 'mkt_divergence_away', 'mkt_kl_divergence',
    'mkt_fav_heaviness', 'mkt_odds_asymmetry', 'mkt_draw_deviation',
    'mkt_value_signal',
    'mkt_kelly_home', 'mkt_kelly_away', 'mkt_ev_home',
]

V2_MARKET_DEFAULTS: Dict[str, float] = {
    'mkt_implied_home': 0.0, 'mkt_implied_draw': 0.0, 'mkt_implied_away': 0.0,
    'mkt_fair_home': 0.0, 'mkt_fair_draw': 0.0, 'mkt_fair_away': 0.0,
    'mkt_overround': 0.0, 'mkt_home_advantage': 0.0,
    'mkt_odds_drift_home': 0.0, 'mkt_odds_drift_draw': 0.0, 'mkt_odds_drift_away': 0.0,
    'mkt_drift_direction': 0.0, 'mkt_volatility': 0.0, 'mkt_max_jump': 0.0,
    'mkt_drift_magnitude': 0.0,
    'mkt_bookmaker_count': 0.0, 'mkt_tightness': 0.0,
    'mkt_home_cv': 0.0, 'mkt_away_cv': 0.0,
    'mkt_divergence_home': 0.0, 'mkt_divergence_away': 0.0,
    'mkt_kl_divergence': 0.0,
    'mkt_fav_heaviness': 0.0, 'mkt_odds_asymmetry': 0.0,
    'mkt_draw_deviation': 0.0, 'mkt_value_signal': 0.0,
    'mkt_kelly_home': 0.0, 'mkt_kelly_away': 0.0, 'mkt_ev_home': 0.0,
}

# ═══════════════════════════════════════════════════════════════════
#  V3 伤病特征 (7 个) — injury_index.py
# ═══════════════════════════════════════════════════════════════════

V3_INJURY_FEATURES: List[str] = [
    'injury_index_home', 'injury_index_away', 'injury_index_diff',
    'attack_impact_diff', 'defense_impact_diff',
    'squad_depth_diff', 'total_injury_asymmetry',
]

V3_INJURY_DEFAULTS: Dict[str, float] = {
    'injury_index_home': 0.0, 'injury_index_away': 0.0, 'injury_index_diff': 0.0,
    'attack_impact_diff': 0.0, 'defense_impact_diff': 0.0,
    'squad_depth_diff': 0.0, 'total_injury_asymmetry': 0.0,
}

# ═══════════════════════════════════════════════════════════════════
#  V4 滚动特征 (~50 个) — rolling_features.py (rw_ 前缀)
# ═══════════════════════════════════════════════════════════════════

def _build_v4_rolling_features() -> Tuple[List[str], Dict[str, float]]:
    """
    构建 V4 滚动特征列表和默认值。
    从 rolling_features.py 的命名规则推断。
    """
    features = []
    defaults = {}

    windows = [3, 5, 10]
    base_stats = ['win_pct', 'draw_pct', 'avg_gf', 'avg_ga', 'avg_gd',
                  'avg_pts', 'cs_rate', 'btts_rate', 'over25_rate']
    std_stats = ['std_gf', 'std_ga', 'std_pts']

    # diff 特征 (主客差值)
    for w in windows:
        for stat in base_stats:
            name = f'rw_r{w}_{stat}'
            features.append(name)
            defaults[name] = 0.0
        for stat in std_stats:
            name = f'rw_r{w}_{stat}'
            features.append(name)
            defaults[name] = 0.0

    # 主客场分离 (仅5场窗口)
    ha_stats = ['win_pct', 'avg_gf', 'avg_ga']
    for side in ['h', 'a']:
        for stat in ha_stats:
            name = f'rw_{side}_r5_{stat}'
            features.append(name)
            defaults[name] = 0.0

    # 趋势特征
    trend_feats = ['trend_pts_10', 'trend_gf_10', 'trend_ga_10', 'momentum_shift']
    for feat in trend_feats:
        name = f'rw_{feat}'
        features.append(name)
        defaults[name] = 0.0

    # 对手强度调整
    opp_feats = ['adj_pts_5', 'power_score_10']
    for feat in opp_feats:
        name = f'rw_{feat}'
        features.append(name)
        defaults[name] = 0.0

    # 交锋历史
    h2h_feats = ['h2h_home_w5', 'h2h_avg_gd5']
    for feat in h2h_feats:
        name = f'rw_{feat}'
        features.append(name)
        defaults[name] = 0.0

    return features, defaults

V4_ROLLING_FEATURES, V4_ROLLING_DEFAULTS = _build_v4_rolling_features()

# ═══════════════════════════════════════════════════════════════════
#  旧→新特征名映射表 (历史重命名)
# ═══════════════════════════════════════════════════════════════════

# key: 旧名, value: 新名
LEGACY_NAME_MAP: Dict[str, str] = {
    # 旧 FeatureCalculator 可能输出的额外字段 → 标准名
    'odd_volatility': 'sigma_trap',
    'kelly_value': 'v_value',
    'implied_prob': 'p_implied',
    'handicap_dev': 'beta_dev',
    'tactical_restraint': 'lambda_crush',
    'fatigue_factor': 'delta_fatigue',
    'sentiment_bias': 'epsilon_senti',
    'whale_signal': 's_whale',
    'discussion_index': 'discussion_growth',
    'time_zone': 'time_suppression',
    'card_risk_model': 'card_risk',
    'referee_influence': 'referee_matrix',
    'asian_euro_divergence': 'arbitrage_index',

    # 伤病模块列名映射 (compute_match_features 输出名 → DB 列名)
    'home_injury_index': 'injury_index_home',
    'away_injury_index': 'injury_index_away',

    # 滚动模块旧命名 → 新命名
    'diff_r3_win_pct': 'rw_r3_win_pct',
    'home_r3_win_pct': 'rw_h_r3_win_pct',
    'away_r3_win_pct': 'rw_a_r3_win_pct',
}

# 反向映射: 新→旧 (用于向旧模型输出)
REVERSE_NAME_MAP: Dict[str, str] = {v: k for k, v in LEGACY_NAME_MAP.items()}

# ═══════════════════════════════════════════════════════════════════
#  全量特征注册表
# ═══════════════════════════════════════════════════════════════════

_ALL_FEATURE_DEFS: List[Tuple[List[str], Dict[str, float]]] = [
    (V1_BASE_FEATURES, V1_DEFAULTS),
    (V1_INTERACTION_FEATURES, V1_INTERACTION_DEFAULTS),
    (V2_MARKET_FEATURES, V2_MARKET_DEFAULTS),
    (V3_INJURY_FEATURES, V3_INJURY_DEFAULTS),
    (V4_ROLLING_FEATURES, V4_ROLLING_DEFAULTS),
]

# 全量默认值合并
ALL_DEFAULTS: Dict[str, float] = {}
for feats, defs in _ALL_FEATURE_DEFS:
    ALL_DEFAULTS.update(defs)

# 全量特征列表 (按版本顺序)
ALL_FEATURES: List[str] = []
for feats, _ in _ALL_FEATURE_DEFS:
    ALL_FEATURES.extend(feats)

# ═══════════════════════════════════════════════════════════════════
#  特征集定义
# ═══════════════════════════════════════════════════════════════════

FEATURE_SETS: Dict[str, List[str]] = {
    'v1': V1_BASE_FEATURES[:],
    'v1_ext': V1_BASE_FEATURES + V1_INTERACTION_FEATURES,
    'v2': V1_BASE_FEATURES + V1_INTERACTION_FEATURES + V2_MARKET_FEATURES,
    'v3': V1_BASE_FEATURES + V1_INTERACTION_FEATURES + V2_MARKET_FEATURES + V3_INJURY_FEATURES,
    'v4': (V1_BASE_FEATURES + V1_INTERACTION_FEATURES + V2_MARKET_FEATURES
           + V3_INJURY_FEATURES + V4_ROLLING_FEATURES),
}

# ═══════════════════════════════════════════════════════════════════
#  默认值策略
# ═══════════════════════════════════════════════════════════════════

class DefaultStrategy(Enum):
    """默认值填充策略"""
    ZERO = 'zero'               # 零值填充
    PRECISE = 'precise'         # 精确默认值 (ALL_DEFAULTS)
    STATISTICAL = 'statistical' # 统计量插补 (中位数)
    SMART = 'smart'             # 智能策略: 精确>统计>零

@dataclass
class MappingResult:
    """映射结果"""
    mapped_names: Dict[str, str]       # 旧名 → 新名
    unmapped: List[str]                 # 无法映射的列名
    extra_columns: List[str]            # 源中多余的列 (非特征)
    coverage: float                     # 映射覆盖率

@dataclass
class ValidationResult:
    """验证结果"""
    n_features: int
    n_missing: int
    missing_ratio: float
    missing_details: Dict[str, int]     # 列名 → 缺失数
    default_filled: Dict[str, int]      # 列名 → 填充数
    distribution_shifts: Dict[str, Dict] # 列名 → 偏移信息
    warnings: List[str]
    quality_score: float                # 0~1

@dataclass
class AlignmentReport:
    """对齐报告"""
    source_columns: List[str]
    target_features: List[str]
    mapped: Dict[str, str]              # 源列 → 目标特征
    renamed: Dict[str, str]             # 重命名映射
    filled_defaults: Dict[str, float]   # 默认值填充
    dropped: List[str]                  # 丢弃的列
    added: List[str]                    # 新增的列 (默认值)
    strategy: DefaultStrategy

# ═══════════════════════════════════════════════════════════════════
#  主类: FeatureMapper
# ═══════════════════════════════════════════════════════════════════

class FeatureMapper:
    """
    特征映射兼容层

    职责:
      1. 旧名→新名映射 (LEGACY_NAME_MAP + 智能推断)
      2. DataFrame 对齐到目标特征集 (缺失列填充, 多余列移除)
      3. 默认值策略选择与填充
      4. 映射质量验证 (覆盖率/缺失率/分布偏移)
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path
        self._median_cache: Dict[str, float] = {}

    # ── 1. 特征名映射 ──────────────────────────────────────────

    def map_column(self, name: str) -> str:
        """将单个列名映射到标准名"""
        # 1. 精确匹配
        if name in LEGACY_NAME_MAP:
            return LEGACY_NAME_MAP[name]

        # 2. 已经是标准名
        if name in ALL_DEFAULTS or name in V1_INTERACTION_DEFAULTS:
            return name

        # 3. 模糊匹配: 去除常见后缀/前缀
        candidates = []
        suffixes_to_strip = ['_v1', '_v2', '_old', '_raw', '_orig', '_calc']
        for suffix in suffixes_to_strip:
            if name.endswith(suffix):
                stripped = name[:-len(suffix)]
                if stripped in LEGACY_NAME_MAP:
                    return LEGACY_NAME_MAP[stripped]
                if stripped in ALL_DEFAULTS:
                    return stripped
                candidates.append(stripped)

        # 4. 模糊匹配: 下划线变体
        name_variants = [
            name.replace('-', '_'),
            name.replace(' ', '_'),
            name.lower(),
        ]
        for variant in name_variants:
            if variant in LEGACY_NAME_MAP:
                return LEGACY_NAME_MAP[variant]
            if variant in ALL_DEFAULTS:
                return variant

        # 5. 前缀匹配 (rw_ → 滚动特征, mkt_ → 市场特征)
        for prefix in ('rw_', 'mkt_', 'ix_'):
            if name.startswith(prefix):
                # 已经是新命名规范
                return name

        return name  # 无法映射, 返回原名

    def map_columns(self, names: List[str]) -> MappingResult:
        """批量映射列名"""
        mapped = {}
        unmapped = []
        extra = []

        for name in names:
            new_name = self.map_column(name)
            if new_name == name and name not in ALL_DEFAULTS:
                # 非特征列 (如 match_id, match_date)
                extra.append(name)
            else:
                mapped[name] = new_name

        # 检查映射后是否有重名
        target_names = list(mapped.values())
        duplicates = [n for n in set(target_names) if target_names.count(n) > 1]
        if duplicates:
            for dup in duplicates:
                sources = [k for k, v in mapped.items() if v == dup]
                logger.warning(f"映射重名: {sources} → {dup}, 保留第一个")

        # 覆盖率
        feature_names = [n for n in names if n in ALL_DEFAULTS or n in LEGACY_NAME_MAP]
        coverage = len(mapped) / len(feature_names) if feature_names else 1.0

        return MappingResult(
            mapped_names=mapped,
            unmapped=unmapped,
            extra_columns=extra,
            coverage=coverage,
        )

    # ── 2. DataFrame 对齐 ─────────────────────────────────────

    def align_dataframe(
        self,
        df: pd.DataFrame,
        target: str = 'v1_ext',
        target_features: Optional[List[str]] = None,
        strategy: DefaultStrategy = DefaultStrategy.SMART,
        compute_interactions: bool = True,
    ) -> Tuple[pd.DataFrame, AlignmentReport]:
        """
        对齐 DataFrame 到目标特征集。

        Args:
            df: 源 DataFrame (可能包含旧名、多余列、缺失列)
            target: 特征集名称 ('v1', 'v1_ext', 'v2', 'v3', 'v4')
            target_features: 显式目标特征列表 (覆盖 target)
            strategy: 默认值填充策略
            compute_interactions: 是否自动计算交互项

        Returns:
            (aligned_df, alignment_report)
        """
        if target_features is not None:
            expected = list(target_features)
        else:
            expected = FEATURE_SETS.get(target, FEATURE_SETS['v1_ext'])[:]

        # Step 0: 清理源 DataFrame 中的重复列名
        if df.columns.duplicated().any():
            dup_cols = df.columns[df.columns.duplicated()].tolist()
            logger.debug(f"源 DataFrame 含重复列: {dup_cols}, 保留首个")
            df = df.loc[:, ~df.columns.duplicated()]

        # Step 1: 列名映射 (旧→新), 处理重名冲突
        rename_map = {}
        for col in df.columns:
            mapped = self.map_column(col)
            if mapped != col:
                rename_map[col] = mapped

        # 检查重名: 旧名映射后的新名可能已存在
        new_names_after_rename = {}
        for col in df.columns:
            new_name = rename_map.get(col, col)
            new_names_after_rename.setdefault(new_name, []).append(col)

        # 对重名列: 优先保留标准名, 丢弃旧名映射
        drop_cols = set()
        for new_name, sources in new_names_after_rename.items():
            if len(sources) > 1:
                # 标准名优先, 其余丢弃
                has_standard = new_name in sources
                for src in sources:
                    if has_standard and src != new_name:
                        drop_cols.add(src)
                        logger.debug(f"重名列冲突, 丢弃旧名列: {src} → {new_name}")
                    elif not has_standard and src != sources[0]:
                        drop_cols.add(src)

        df_step1 = df.drop(columns=drop_cols, errors='ignore')
        df_renamed = df_step1.rename(columns=rename_map)

        # Step 2: 识别已有/缺失/多余列
        existing = set(df_renamed.columns)
        needed = set(expected)
        missing = needed - existing
        extra_in_df = existing - needed - {'match_id', 'match_date', 'home_team_name',
                                           'away_team_name', 'league_name', 'home_score',
                                           'away_score', 'final_result'}

        # Step 3: 默认值填充缺失列
        filled_defaults = {}
        for feat in sorted(missing):
            # 如果是交互项, 尝试计算
            if compute_interactions and feat in V1_INTERACTION_FEATURES:
                computed = self._compute_interaction(df_renamed, feat)
                if computed is not None:
                    df_renamed[feat] = computed
                    continue

            # 按策略填充默认值
            default_val = self._get_default(feat, df_renamed, strategy)
            df_renamed[feat] = default_val
            filled_defaults[feat] = default_val

        # Step 4: 选择目标列 (保留关键元数据列)
        meta_cols = [c for c in df_renamed.columns
                     if c in {'match_id', 'match_date', 'home_team_name',
                              'away_team_name', 'league_name', 'home_score',
                              'away_score', 'final_result'}]
        output_cols = [c for c in expected if c in df_renamed.columns]
        df_aligned = df_renamed[meta_cols + output_cols].copy()

        # Step 5: 确保列顺序与目标一致
        final_cols = meta_cols + [c for c in expected if c in df_aligned.columns]
        df_aligned = df_aligned.reindex(columns=final_cols)

        report = AlignmentReport(
            source_columns=list(df.columns),
            target_features=expected,
            mapped=rename_map,
            renamed=rename_map,
            filled_defaults=filled_defaults,
            dropped=list(extra_in_df),
            added=list(missing),
            strategy=strategy,
        )

        return df_aligned, report

    def make_compatible(
        self,
        df: pd.DataFrame,
        model_feature_names: List[str],
        strategy: DefaultStrategy = DefaultStrategy.SMART,
    ) -> pd.DataFrame:
        """
        使 DataFrame 与已保存模型兼容。

        自动处理:
          - 列名映射 (旧→新)
          - 缺失列填充默认值
          - 多余列移除
          - 列顺序对齐

        Args:
            df: 源 DataFrame
            model_feature_names: 已保存模型的特征列表
            strategy: 默认值策略

        Returns:
            兼容的 DataFrame (列名/顺序与模型一致)
        """
        df_aligned, _ = self.align_dataframe(
            df, target_features=model_feature_names, strategy=strategy,
            compute_interactions=True,
        )
        # 确保严格按模型特征顺序排列
        feature_cols = [c for c in model_feature_names if c in df_aligned.columns]
        return df_aligned[feature_cols]

    # ── 3. 默认值策略 ─────────────────────────────────────────

    def _get_default(
        self,
        feature: str,
        df: Optional[pd.DataFrame] = None,
        strategy: DefaultStrategy = DefaultStrategy.SMART,
    ) -> float:
        """
        获取特征默认值。

        策略优先级 (SMART):
          1. PRECISE: 精确默认值 (ALL_DEFAULTS)
          2. STATISTICAL: 统计量插补 (中位数)
          3. ZERO: 零值兜底
        """
        if strategy == DefaultStrategy.ZERO:
            return 0.0

        if strategy in (DefaultStrategy.PRECISE, DefaultStrategy.SMART):
            if feature in ALL_DEFAULTS:
                return ALL_DEFAULTS[feature]

        if strategy in (DefaultStrategy.STATISTICAL, DefaultStrategy.SMART):
            # 尝试从 DataFrame 计算中位数
            if df is not None and feature in df.columns:
                median = df[feature].dropna().median()
                if pd.notna(median):
                    self._median_cache[feature] = float(median)
                    return float(median)
            # 使用缓存的中位数
            if feature in self._median_cache:
                return self._median_cache[feature]

        # 兜底: 零值
        if strategy == DefaultStrategy.SMART and feature in ALL_DEFAULTS:
            return ALL_DEFAULTS[feature]

        return 0.0

    def precompute_medians(self, df: pd.DataFrame, features: Optional[List[str]] = None):
        """
        预计算特征中位数, 用于后续默认值填充。

        Args:
            df: 训练集 DataFrame
            features: 指定特征列表 (None=全部)
        """
        target = features or [c for c in df.columns if c in ALL_DEFAULTS]
        for feat in target:
            if feat in df.columns:
                median = df[feat].dropna().median()
                if pd.notna(median):
                    self._median_cache[feat] = float(median)

        logger.info(f"预计算 {len(self._median_cache)} 个特征中位数")

    def get_default_report(self, features: Optional[List[str]] = None) -> pd.DataFrame:
        """获取默认值对照表"""
        target = features or list(ALL_DEFAULTS.keys())
        rows = []
        for feat in target:
            precise = ALL_DEFAULTS.get(feat, None)
            median = self._median_cache.get(feat, None)
            rows.append({
                'feature': feat,
                'precise_default': precise,
                'median_default': round(median, 4) if median is not None else None,
                'symmetric': feat in SYMMETRIC_FEATURES,
                'version': self._feature_version(feat),
            })
        return pd.DataFrame(rows)

    # ── 4. 映射质量验证 ───────────────────────────────────────

    def validate(
        self,
        df: pd.DataFrame,
        reference_df: Optional[pd.DataFrame] = None,
        expected_features: Optional[List[str]] = None,
        distribution_threshold: float = 0.15,
    ) -> ValidationResult:
        """
        验证映射质量。

        检查:
          1. 特征覆盖率 (有多少目标特征存在)
          2. 缺失率 (NaN/默认值比例)
          3. 分布偏移 (与参考数据集对比, KS检验)
          4. 常量特征 (全为默认值的特征)

        Args:
            df: 待验证的 DataFrame
            reference_df: 参考数据集 (用于分布对比)
            expected_features: 期望特征列表
            distribution_threshold: 分布偏移阈值 (KS statistic)

        Returns:
            ValidationResult
        """
        expected = expected_features or list(ALL_DEFAULTS.keys())

        # 覆盖率
        existing = [f for f in expected if f in df.columns]
        missing_cols = [f for f in expected if f not in df.columns]

        # 缺失率
        missing_details = {}
        default_filled = {}
        feature_cols = [c for c in df.columns if c in ALL_DEFAULTS or c in expected]

        for col in feature_cols:
            n_missing = int(df[col].isna().sum())
            if n_missing > 0:
                missing_details[col] = n_missing

            # 默认值比例
            default_val = ALL_DEFAULTS.get(col, None)
            if default_val is not None:
                n_default = int((df[col] == default_val).sum())
                if n_default > 0:
                    default_filled[col] = n_default

        # 总缺失
        n_total_missing = sum(missing_details.values())
        n_total_cells = len(df) * len(feature_cols) if feature_cols else 1
        missing_ratio = n_total_missing / n_total_cells

        # 分布偏移检测
        distribution_shifts = {}
        if reference_df is not None:
            from scipy import stats as scipy_stats
            common_cols = [c for c in feature_cols if c in reference_df.columns]
            for col in common_cols:
                src_vals = df[col].dropna().values
                ref_vals = reference_df[col].dropna().values
                if len(src_vals) > 30 and len(ref_vals) > 30:
                    try:
                        ks_stat, ks_p = scipy_stats.ks_2samp(src_vals, ref_vals)
                        if ks_stat > distribution_threshold:
                            distribution_shifts[col] = {
                                'ks_statistic': round(ks_stat, 4),
                                'p_value': round(ks_p, 6),
                                'shift': 'significant' if ks_p < 0.05 else 'marginal',
                                'src_mean': round(float(np.mean(src_vals)), 4),
                                'ref_mean': round(float(np.mean(ref_vals)), 4),
                            }
                    except (Exception, ValueError, KeyError, IndexError):
                        pass

        # 告警
        warnings_list = []

        # 高缺失率告警
        for col, n_miss in sorted(missing_details.items(), key=lambda x: -x[1]):
            ratio = n_miss / len(df)
            if ratio > 0.5:
                warnings_list.append(f"[HIGH_MISSING] {col}: {ratio:.1%} 缺失")

        # 高默认值比例告警
        for col, n_default in sorted(default_filled.items(), key=lambda x: -x[1]):
            ratio = n_default / len(df)
            if ratio > 0.9:
                warnings_list.append(f"[HIGH_DEFAULT] {col}: {ratio:.1%} 为默认值")

        # 分布偏移告警
        for col, info in distribution_shifts.items():
            warnings_list.append(
                f"[DISTRIBUTION_SHIFT] {col}: KS={info['ks_statistic']:.3f} "
                f"(src_mean={info['src_mean']}, ref_mean={info['ref_mean']})"
            )

        # 缺失列告警
        if missing_cols:
            warnings_list.append(
                f"[MISSING_COLUMNS] {len(missing_cols)} 个目标特征不存在: "
                f"{missing_cols[:5]}{'...' if len(missing_cols) > 5 else ''}"
            )

        # 质量评分 (0~1)
        coverage_score = len(existing) / len(expected) if expected else 1.0
        missing_score = 1.0 - min(missing_ratio, 1.0)
        shift_penalty = min(len(distribution_shifts) * 0.05, 0.3)
        quality_score = max(0.0, min(1.0,
            0.4 * coverage_score + 0.4 * missing_score + 0.2 - shift_penalty
        ))

        return ValidationResult(
            n_features=len(feature_cols),
            n_missing=n_total_missing,
            missing_ratio=round(missing_ratio, 4),
            missing_details=missing_details,
            default_filled=default_filled,
            distribution_shifts=distribution_shifts,
            warnings=warnings_list,
            quality_score=round(quality_score, 3),
        )

    # ── 5. 交互项计算 ─────────────────────────────────────────

    def _compute_interaction(
        self, df: pd.DataFrame, feat: str
    ) -> Optional[pd.Series]:
        """尝试从已有列计算交互项"""
        try:
            if feat == 'ix_a1_sigma' and 'a1' in df.columns and 'sigma_trap' in df.columns:
                return df['a1'] * df['sigma_trap']
            elif feat == 'ix_a2_lambda' and 'a2' in df.columns and 'lambda_crush' in df.columns:
                return (df['a2'] - 0.5) * (df['lambda_crush'] - 1.0)
            elif feat == 'ix_a3_epsilon' and 'a3' in df.columns and 'epsilon_senti' in df.columns:
                return (df['a3'] - 0.5) * (df['epsilon_senti'] - 0.5)
            elif feat == 'ix_a1_a2' and 'a1' in df.columns and 'a2' in df.columns:
                return df['a1'] * (df['a2'] - 0.5)
            elif feat == 'ix_rank_form' and 'rank_diff_factor' in df.columns and 'form_momentum' in df.columns:
                return df['rank_diff_factor'] * df['form_momentum']
            elif feat == 'ix_power_gap' and 'a1' in df.columns and 'a2' in df.columns and 'rank_diff_factor' in df.columns:
                return np.abs(
                    0.4 * df['a1'] + 0.4 * (df['a2'] - 0.5) +
                    0.2 * np.clip(df['rank_diff_factor'], -1, 1)
                )
        except (Exception, KeyError, IndexError):
            pass
        return None

    def compute_all_interactions(self, df: pd.DataFrame) -> pd.DataFrame:
        """为 DataFrame 计算所有交互项"""
        df_out = df.copy()
        for feat in V1_INTERACTION_FEATURES:
            if feat not in df_out.columns:
                series = self._compute_interaction(df_out, feat)
                if series is not None:
                    df_out[feat] = series
                else:
                    df_out[feat] = V1_INTERACTION_DEFAULTS.get(feat, 0.0)
        return df_out

    # ── 6. 辅助方法 ───────────────────────────────────────────

    @staticmethod
    def _feature_version(feature: str) -> str:
        """判断特征属于哪个版本"""
        if feature in V1_BASE_FEATURES:
            return 'v1'
        if feature in V1_INTERACTION_FEATURES:
            return 'v1_ext'
        if feature.startswith('mkt_'):
            return 'v2'
        if feature in V3_INJURY_FEATURES:
            return 'v3'
        if feature.startswith('rw_'):
            return 'v4'
        return 'unknown'

    def get_feature_info(self, feature: str) -> Dict[str, Any]:
        """获取特征详细信息"""
        return {
            'name': feature,
            'version': self._feature_version(feature),
            'default': ALL_DEFAULTS.get(feature, None),
            'median': self._median_cache.get(feature, None),
            'symmetric': feature in SYMMETRIC_FEATURES,
            'legacy_names': [k for k, v in LEGACY_NAME_MAP.items() if v == feature],
            'is_interaction': feature in V1_INTERACTION_FEATURES,
            'interaction_formula': V1_INTERACTION_FORMULAS.get(feature, None),
        }

    def summary(self) -> pd.DataFrame:
        """特征体系总览"""
        rows = []
        for version, features in FEATURE_SETS.items():
            rows.append({
                'version': version,
                'n_features': len(features),
                'features': ', '.join(features[:5]) + ('...' if len(features) > 5 else ''),
            })
        return pd.DataFrame(rows)

    def check_db_compatibility(self, db_path: str = None) -> Dict[str, Any]:
        """
        检查数据库列与特征体系的兼容性。

        Returns:
            包含 DB 列名、已映射、未映射信息的字典
        """
        import sqlite3
        path = db_path or self.db_path
        if not path:
            return {'error': 'No db_path specified'}

        conn = sqlite3.connect(path)
        cursor = conn.execute("PRAGMA table_info(match_features)")
        db_cols = set(row[1] for row in cursor.fetchall())
        conn.close()

        # 检查每个目标特征是否在 DB 中
        # 交互项不在 DB 中 (运行时计算), 排除
        storable_features = set(ALL_FEATURES) - set(V1_INTERACTION_FEATURES)
        all_expected = storable_features
        in_db = storable_features & db_cols
        not_in_db = storable_features - db_cols
        extra_in_db = db_cols - all_expected - {'feature_id', 'match_id', 'created_at',
                                                  'weather_modifier'}

        # 检查旧名映射
        legacy_in_db = set()
        for old_name, new_name in LEGACY_NAME_MAP.items():
            if old_name in db_cols:
                legacy_in_db.add(old_name)

        return {
            'db_columns': sorted(db_cols),
            'expected_in_db': sorted(in_db),
            'missing_from_db': sorted(not_in_db),
            'extra_in_db': sorted(extra_in_db),
            'legacy_names_in_db': sorted(legacy_in_db),
            'compatibility_score': round(len(in_db) / len(all_expected), 3) if all_expected else 0.0,
        }

# ═══════════════════════════════════════════════════════════════════
#  便捷函数
# ═══════════════════════════════════════════════════════════════════

def align_for_model(
    df: pd.DataFrame,
    model_feature_names: List[str],
    strategy: str = 'smart',
) -> pd.DataFrame:
    """
    一站式: 将 DataFrame 对齐到已训练模型的特征列表。

    Args:
        df: 源 DataFrame
        model_feature_names: 模型训练时的特征列表
        strategy: 'smart' | 'precise' | 'statistical' | 'zero'

    Returns:
        对齐后的 DataFrame (列名/顺序/完整性)
    """
    mapper = FeatureMapper()
    strat = DefaultStrategy(strategy)
    return mapper.make_compatible(df, model_feature_names, strat)

def check_feature_alignment(
    df: pd.DataFrame,
    reference: Optional[pd.DataFrame] = None,
    target: str = 'v1_ext',
) -> ValidationResult:
    """一站式: 检查特征映射质量"""
    mapper = FeatureMapper()
    expected = FEATURE_SETS.get(target, FEATURE_SETS['v1_ext'])
    return mapper.validate(df, reference, expected)

# ═══════════════════════════════════════════════════════════════════
#  自测
# ═══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import sys
    import os
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    print('=' * 60)
    print('T19 特征映射兼容层 — 自测')
    print('=' * 60)

    mapper = FeatureMapper()
    passed = 0
    failed = 0

    # ── Test 1: 列名映射 ──
    print('\n--- Test 1: 列名映射 ---')
    test_cases = [
        ('odd_volatility', 'sigma_trap'),
        ('kelly_value', 'v_value'),
        ('fatigue_factor', 'delta_fatigue'),
        ('a1', 'a1'),  # 已是标准名
        ('home_injury_index', 'injury_index_home'),
        ('sigma_trap_v1', 'sigma_trap'),
        ('mkt_overround', 'mkt_overround'),  # 已是新名
    ]
    for old, expected in test_cases:
        result = mapper.map_column(old)
        ok = result == expected
        passed += ok
        failed += not ok
        print(f'  {"PASS" if ok else "FAIL"}: {old} → {result} (expected: {expected})')

    # ── Test 2: 批量映射 ──
    print('\n--- Test 2: 批量映射 ---')
    result = mapper.map_columns(['odd_volatility', 'a1', 'fatigue_factor', 'match_id'])
    ok = result.coverage == 1.0 and 'match_id' in result.extra_columns
    passed += ok
    failed += not ok
    print(f'  {"PASS" if ok else "FAIL"}: coverage={result.coverage}, extra={result.extra_columns}')

    # ── Test 3: DataFrame 对齐 ──
    print('\n--- Test 3: DataFrame 对齐 ---')
    df_test = pd.DataFrame({
        'match_id': [1, 2],
        'a1': [0.1, -0.2],
        'sigma_trap': [0.05, 0.08],
        'lambda_crush': [1.2, 0.9],
        'odd_volatility': [0.03, 0.04],  # 旧名
    })
    df_aligned, report = mapper.align_dataframe(df_test, target='v1')
    ok = 'a1' in df_aligned.columns and 'sigma_trap' in df_aligned.columns
    passed += ok
    failed += not ok
    print(f'  {"PASS" if ok else "FAIL"}: aligned columns={list(df_aligned.columns)}')
    print(f'  renamed={report.renamed}, filled={list(report.filled_defaults.keys())[:5]}...')

    # ── Test 4: 交互项计算 ──
    print('\n--- Test 4: 交互项自动计算 ---')
    df_with_ix = pd.DataFrame({
        'a1': [0.1, -0.2],
        'a2': [0.6, 0.4],
        'sigma_trap': [0.05, 0.08],
        'lambda_crush': [1.2, 0.9],
        'epsilon_senti': [0.6, 0.4],
        'rank_diff_factor': [0.3, -0.1],
        'form_momentum': [0.2, -0.1],
    })
    df_ix = mapper.compute_all_interactions(df_with_ix)
    ok = 'ix_a1_sigma' in df_ix.columns and 'ix_power_gap' in df_ix.columns
    passed += ok
    failed += not ok
    print(f'  {"PASS" if ok else "FAIL"}: ix_a1_sigma={df_ix["ix_a1_sigma"].values}, '
          f'ix_power_gap={df_ix["ix_power_gap"].values}')

    # ── Test 5: 模型兼容 ──
    print('\n--- Test 5: 模型兼容 (make_compatible) ---')
    model_features = ['a1', 'a2', 'a3', 'sigma_trap', 'ix_a1_sigma', 'ix_power_gap']
    df_compat = mapper.make_compatible(df_test, model_features)
    ok = list(df_compat.columns) == model_features and len(df_compat) == 2
    passed += ok
    failed += not ok
    print(f'  {"PASS" if ok else "FAIL"}: columns={list(df_compat.columns)}')

    # ── Test 6: 默认值策略 ──
    print('\n--- Test 6: 默认值策略 ---')
    val_zero = mapper._get_default('a1', strategy=DefaultStrategy.ZERO)
    val_precise = mapper._get_default('a1', strategy=DefaultStrategy.PRECISE)
    val_smart = mapper._get_default('a2', strategy=DefaultStrategy.SMART)
    ok = val_zero == 0.0 and val_precise == 0.0 and val_smart == 0.5
    passed += ok
    failed += not ok
    print(f'  {"PASS" if ok else "FAIL"}: zero={val_zero}, precise={val_precise}, smart={val_smart}')

    # ── Test 7: 验证 ──
    print('\n--- Test 7: 验证 ---')
    df_validate = pd.DataFrame({
        'a1': [0.0] * 100 + [0.1] * 10,
        'a2': [0.5] * 95 + [0.6] * 15,
        'sigma_trap': [0.0] * 100 + [0.05] * 10,
    })
    vr = mapper.validate(df_validate, expected_features=['a1', 'a2', 'sigma_trap'])
    ok = vr.quality_score > 0 and len(vr.warnings) > 0  # a1 90%+ 为默认值
    passed += ok
    failed += not ok
    print(f'  {"PASS" if ok else "FAIL"}: quality={vr.quality_score}, warnings={vr.warnings}')

    # ── Test 8: 特征版本识别 ──
    print('\n--- Test 8: 特征版本识别 ---')
    versions = {
        'a1': 'v1', 'mkt_overround': 'v2', 'injury_index_diff': 'v3',
        'rw_r3_win_pct': 'v4', 'ix_a1_sigma': 'v1_ext',
    }
    all_ok = True
    for feat, expected_v in versions.items():
        actual = mapper._feature_version(feat)
        ok = actual == expected_v
        passed += ok
        failed += not ok
        all_ok = all_ok and ok
        print(f'  {"PASS" if ok else "FAIL"}: {feat} → {actual} (expected: {expected_v})')

    # ── Test 9: DB 兼容性检查 ──
    print('\n--- Test 9: DB 兼容性检查 ---')
    db_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'football_data.db')
    if os.path.exists(db_path):
        compat = mapper.check_db_compatibility(db_path)
        ok = compat['compatibility_score'] > 0.5
        passed += ok
        failed += not ok
        print(f'  {"PASS" if ok else "FAIL"}: compatibility_score={compat["compatibility_score"]}')
        print(f'  missing_from_db={compat["missing_from_db"][:5]}...')
    else:
        print('  SKIP: DB not found')

    # ── Test 10: 特征集总览 ──
    print('\n--- Test 10: 特征集总览 ---')
    summary = mapper.summary()
    ok = len(summary) == 5  # v1, v1_ext, v2, v3, v4
    passed += ok
    failed += not ok
    print(summary.to_string(index=False))
    print(f'  {"PASS" if ok else "FAIL"}')

    # ── 总结 ──
    print(f'\n{"=" * 60}')
    print(f'自测结果: {passed} PASSED, {failed} FAILED')
    print(f'{"=" * 60}')

    if failed > 0:
        sys.exit(1)
