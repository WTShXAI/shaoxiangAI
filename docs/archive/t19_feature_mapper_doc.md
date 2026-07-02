# T19 特征映射兼容层

## 概述

解决多代特征体系之间的映射、默认值填充、质量验证问题。确保旧模型/旧数据与新特征体系之间无缝衔接。

## 核心模块

**文件**: `optimize/feature_mapper.py` (~1080行)

## 特征体系演进

| 版本 | 特征数 | 前缀/范围 | 来源 |
|------|--------|-----------|------|
| V1 | 19 | a1-a6, sigma_trap, lambda_crush... | feature_calculator.py |
| V1_EXT | 25 | + ix_a1_sigma 等6个交互项 | gbdt_adapter.py |
| V2 | 54 | + mkt_* 29个市场特征 | market_features.py |
| V3 | 61 | + 7个伤病特征 | injury_index.py |
| V4 | 111 | + rw_* ~50个滚动特征 | rolling_features.py |

## FeatureMapper 类 API

### 1. 列名映射

```python
mapper = FeatureMapper()

# 单列映射
new_name = mapper.map_column('odd_volatility')  # → 'sigma_trap'

# 批量映射
result = mapper.map_columns(['odd_volatility', 'a1', 'fatigue_factor', 'match_id'])
# result.mapped_names = {'odd_volatility': 'sigma_trap', 'fatigue_factor': 'delta_fatigue'}
# result.extra_columns = ['match_id']
# result.coverage = 1.0
```

映射策略:
- 精确匹配: `LEGACY_NAME_MAP` 中的旧名→新名
- 后缀剥离: `_v1`, `_v2`, `_old`, `_raw` 等后缀自动去除
- 变体匹配: `-`↔`_`, 大小写不敏感
- 前缀识别: `rw_`, `mkt_`, `ix_` 自动识别为新命名

### 2. DataFrame 对齐

```python
# 对齐到指定特征集
df_aligned, report = mapper.align_dataframe(df, target='v1_ext')

# 对齐到自定义特征列表
df_aligned, report = mapper.align_dataframe(df, target_features=['a1', 'a2', 'ix_a1_sigma'])

# 对齐到已保存模型的特征列表
df_compat = mapper.make_compatible(df, model_feature_names=saved_model.feature_names)
```

对齐流程:
1. 清理源DF重复列 (`match_id` 出现两次等)
2. 列名映射 (旧→新), 冲突时标准名优先
3. 缺失列填充默认值
4. 交互项自动计算 (如果基础列存在)
5. 多余列移除
6. 列顺序对齐

### 3. 默认值策略

```python
class DefaultStrategy(Enum):
    ZERO = 'zero'              # 零值填充
    PRECISE = 'precise'        # 精确默认值 (ALL_DEFAULTS)
    STATISTICAL = 'statistical' # 统计量插补 (中位数)
    SMART = 'smart'            # 智能策略: 精确>统计>零
```

SMART策略优先级:
1. PRECISE: 从 `ALL_DEFAULTS` 获取精确默认值
2. STATISTICAL: 从训练集中位数插补 (需 `precompute_medians()`)
3. ZERO: 零值兜底

```python
# 预计算中位数 (在训练集上)
mapper.precompute_medians(df_train)

# 获取默认值报告
report_df = mapper.get_default_report()
```

### 4. 映射质量验证

```python
result = mapper.validate(df, reference_df=df_train, expected_features=FEATURE_SETS['v1_ext'])
# result.quality_score  → 0~1
# result.missing_details → 缺失详情
# result.default_filled → 默认值比例
# result.distribution_shifts → KS分布偏移
# result.warnings → 告警列表
```

质量评分公式:
```
quality = 0.4 × coverage + 0.4 × (1 - missing_ratio) + 0.2 - shift_penalty
```

告警类型:
- `[HIGH_MISSING]`: 缺失率 > 50%
- `[HIGH_DEFAULT]`: 默认值比例 > 90%
- `[DISTRIBUTION_SHIFT]`: KS统计量 > 0.15
- `[MISSING_COLUMNS]`: 目标特征不存在

### 5. DB兼容性检查

```python
compat = mapper.check_db_compatibility('data/football_data.db')
# compat.compatibility_score → 0.524
# compat.missing_from_db → 滚动特征等
```

## 旧名→新名映射表

| 旧名 | 新名 | 说明 |
|------|------|------|
| odd_volatility | sigma_trap | 异常波动率 |
| kelly_value | v_value | 凯利价值 |
| implied_prob | p_implied | 隐含概率 |
| handicap_dev | beta_dev | 盘口偏差 |
| tactical_restraint | lambda_crush | 战术克制 |
| fatigue_factor | delta_fatigue | 体能衰减 |
| sentiment_bias | epsilon_senti | 情绪偏差 |
| whale_signal | s_whale | 大户信号 |
| discussion_index | discussion_growth | 舆情增长 |
| home_injury_index | injury_index_home | 主队伤病 |
| away_injury_index | injury_index_away | 客队伤病 |
| diff_r3_win_pct | rw_r3_win_pct | 滚动3场胜率 |

## 端到端验证结果 (18218场真实数据)

| 测试 | 结果 | 说明 |
|------|------|------|
| V1对齐 | PASS | 19/19特征, 0列填充 |
| V1_EXT交互项 | PASS | 6项自动计算, 38.9%非零 |
| V2市场特征 | PASS | 29列, 100%非零 |
| 质量验证 | PASS | Quality=1.0, 5个高默认值告警 |
| 模型兼容 | PASS | 列名/顺序完全匹配, 无NaN |
| DB兼容 | PASS | Score=0.524 (V4滚动特征未入库) |
| 默认值分析 | PASS | 5个特征99.8%为默认值 |

### 高默认值特征 (需要真实数据源)

- `aerial_advantage` (99.8%) — 防空有效性, 需射门数据
- `press_intensity` (99.8%) — 逼抢强度, 需逼抢数据
- `card_risk` (99.8%) — 红黄牌风险, 需裁判数据
- `beta_dev` (99.8%) — 盘口偏差, 需亚盘数据
- `delta_fatigue` (99.8%) — 体能衰减, 需赛程数据

## 便捷函数

```python
from optimize.feature_mapper import align_for_model, check_feature_alignment

# 一站式: 对齐到已训练模型
df_compat = align_for_model(df, model.feature_names, strategy='smart')

# 一站式: 检查映射质量
result = check_feature_alignment(df, reference=df_train, target='v1_ext')
```
