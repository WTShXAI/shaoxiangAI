# T14 市场特征提取器

## 概述

从赔率数据中提取市场级特征，包括隐含概率体系、赔率变动率、市场信心指标、模型-市场分歧度等，集成到特征管道。

**新增文件**: `optimize/market_features.py`

## 核心架构

```
OddsAPIInterface (赔率API抽象层)
    ├── get_odds_snapshot()     → OddsSnapshot (单时间点赔率)
    ├── get_odds_history()      → List[OddsSnapshot] (赔率时间序列)
    └── get_multi_bookmaker_odds() → MultiBookmakerOdds (多博彩公司聚合)
        ├── The Odds API (需配置 API Key)
        └── DB fallback (从 odds 表按 provider 分组)

MarketFeatureExtractor (市场特征提取器)
    ├── compute_match_features()   → 29 个市场特征
    ├── generate_features_df()     → 批量生成 DataFrame
    ├── write_to_match_features()   → 写入 match_features 表
    ├── augment_bundle()            → 追加到 SequenceBundle
    ├── correlation_analysis()      → 特征相关性分析
    └── feature_importance_proxy()  → 特征重要性代理
```

## 6 大特征模块 (29 个特征)

### 模块1: 隐含概率体系 (8 features)

| 特征名 | 公式 | 说明 |
|--------|------|------|
| `mkt_implied_home` | (1/odds) × return_rate | 主胜隐含概率 |
| `mkt_implied_draw` | (1/odds) × return_rate | 平局隐含概率 |
| `mkt_implied_away` | (1/odds) × return_rate | 客胜隐含概率 |
| `mkt_fair_home` | raw_h / Σ(raw) | 去抽水公平概率-主 |
| `mkt_fair_draw` | raw_d / Σ(raw) | 去抽水公平概率-平 |
| `mkt_fair_away` | raw_a / Σ(raw) | 去抽水公平概率-客 |
| `mkt_overround` | Σ(1/odds) - 1 | 抽水率 (正=庄家利润) |
| `mkt_home_advantage` | fair_home - fair_away | 市场主场优势 |

### 模块2: 赔率变动特征 (8 features)

| 特征名 | 说明 |
|--------|------|
| `mkt_odds_drift_home` | 主胜赔率漂移 (closing - opening) |
| `mkt_odds_drift_draw` | 平局赔率漂移 |
| `mkt_odds_drift_away` | 客胜赔率漂移 |
| `mkt_drift_direction` | 变动方向一致性 (0~1) |
| `mkt_volatility` | 赔率波动率 (对数收益率 std) |
| `mkt_max_jump` | 最大单步变动 |
| `mkt_drift_magnitude` | 漂移总幅度 (√(Σdrift²)) |

> 数据来源: `odds_history` 表的时间序列；若只有单条快照，从多 provider 推断

### 模块3: 市场信心指标 (4 features)

| 特征名 | 说明 |
|--------|------|
| `mkt_bookmaker_count` | 博彩公司数量 |
| `mkt_tightness` | 赔率紧密度 (1 - mean(CV)) |
| `mkt_home_cv` | 主胜赔率变异系数 |
| `mkt_away_cv` | 客胜赔率变异系数 |

### 模块4: 模型-市场分歧 (3 features)

| 特征名 | 公式 | 说明 |
|--------|------|------|
| `mkt_divergence_home` | model_home - fair_home | 正=模型更看好主队 |
| `mkt_divergence_away` | model_away - fair_away | 正=模型更看好客队 |
| `mkt_kl_divergence` | Σ(p·log(p/q)) | KL散度近似 |

> 需要模型概率输入，无则置0

### 模块5: 市场异常信号 (4 features)

| 特征名 | 说明 |
|--------|------|
| `mkt_fav_heaviness` | 热门方偏重 (赔率最低方超过50%的部分) |
| `mkt_odds_asymmetry` | 赔率不对称度 (|home-away|/mean) |
| `mkt_draw_deviation` | 平局赔率偏离经验值(~26%) |
| `mkt_value_signal` | 价值信号 (heaviness × asymmetry) |

### 模块6: 赔率价值评估 (3 features)

| 特征名 | 公式 | 说明 |
|--------|------|------|
| `mkt_kelly_home` | (bp-q)/b | 凯利价值-主 |
| `mkt_kelly_away` | (bp-q)/b | 凯利价值-客 |
| `mkt_ev_home` | p(odds-1)-(1-p) | 期望值-主 |

> 需要模型概率输入，无则置0

## 赔率 API 接口设计

### 抽象层 `OddsAPIInterface`

```python
api = OddsAPIInterface()

# 从数据库获取
snapshot = api.get_odds_snapshot(match_id)
history = api.get_odds_history(match_id)
multi = api.get_multi_bookmaker_odds(match_id)

# The Odds API (需配置 THE_ODDS_API_KEY)
if api.the_odds_available:
    client = api._get_the_odds_client()
    # 跨博彩公司赔率、历史走势
```

### 数据源优先级

1. **The Odds API** (需 API Key) — 多博彩公司实时赔率 + 历史走势
2. **数据库 odds_history** — 时间序列 (覆盖: 18,334 场, 但仅 10 场有 >1 条记录)
3. **数据库 odds** — 单快照 (覆盖: 18,314 场, 2 providers)
4. **多 provider 推断** — `default`(早期) vs `retrospective_elo`(近期) 作为开盘/收盘

## 特征相关性分析结果

### 高相关性对 (|r| > 0.8): 41 对

主要来源:
- **同信息不同变换**: `implied_home ↔ fair_home` (r=1.0), `implied_draw ↔ fair_draw` (r=0.999)
- **反向关系**: `implied_home ↔ implied_away` (r=-0.927)
- **衍生关系**: `odds_asymmetry ↔ draw_deviation` (r=-0.981)

**建议**: 在 GBDT 中可保留全部 (树模型自动选择)；在 DL 中使用 `mode='diff'` 降维 (29→25)

### 特征覆盖率

| 特征 | 非零率 |
|------|--------|
| implied_home/draw/away | 99.9% |
| fair_home/draw/away | 99.9% |
| overround | 99.9% |
| odds_asymmetry | 99.6% |
| fav_heaviness | 40.4% |
| kl_divergence | 1.5% (需模型概率) |

## 管道集成

### 写入 match_features (GBDT)

```python
mfx = MarketFeatureExtractor()
df = mfx.generate_features_df()
mfx.write_to_match_features(df)
# → 自动 ALTER TABLE ADD COLUMN + UPDATE
```

### 追加 SequenceBundle (DL)

```python
mfx.augment_bundle(bundle, df, mode='diff')
# static: 19 → 44 (+25 market features)
```

## 实测数据 (18,327 场)

| 指标 | 说明 |
|------|------|
| 数据源 | odds 表 (retrospective_elo: 54,200 行, default: 146 行) |
| 覆盖率 | 99.9% (18,314/18,327 有赔率) |
| 变动数据 | 仅 10 场有 >1 条历史 (需 The Odds API 补充) |
| overround | -0.048 ± 0.005 (Elo 派生赔率无传统庄家利润) |
| home_advantage | 0.00 ± 0.10 (市场对主/客无显著偏好) |

## 用法

```python
from optimize.market_features import MarketFeatureExtractor, compute_market_features

# 单场
feat = compute_market_features(match_id=123)
print(feat['mkt_implied_home'])  # 主胜隐含概率
print(feat['mkt_overround'])      # 抽水率

# 带模型概率
feat = compute_market_features(123, model_probs={'home': 0.55, 'draw': 0.25, 'away': 0.20})
print(feat['mkt_kelly_home'])    # 凯利价值
print(feat['mkt_kl_divergence'])  # 模型-市场分歧

# 批量
mfx = MarketFeatureExtractor()
df = mfx.generate_features_df()
mfx.write_to_match_features(df)

# 相关性分析
corr = mfx.correlation_analysis(df)
imp = mfx.feature_importance_proxy(df)

# CLI
python -m optimize.market_features analyze   # 相关性分析
python -m optimize.market_features write     # 写入 match_features
```
