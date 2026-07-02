# T13 全队伤病评估模块

## 概述

将 `goalkeeper_model` 的3因子模型扩展到全队11个位置，设计伤病影响量化指标，并集成到特征管道。

## 新增文件

| 文件 | 说明 |
|------|------|
| `optimize/injury_index.py` | 全队伤病评估模块 |

## 核心组件

### 1. 位置画像 (`PositionProfile`)

定义每个位置的缺阵影响权重：

| 位置 | 代码 | 缺阵重要性 | 可替代性 | 进攻影响 | 防守影响 |
|------|------|-----------|---------|---------|---------|
| 门将 | GK | 0.95 | 0.20 | 0.05 | 0.95 |
| 中后卫 | CB | 0.85 | 0.40 | 0.10 | 0.80 |
| 后腰 | DM | 0.75 | 0.45 | 0.30 | 0.60 |
| 前锋 | ST | 0.80 | 0.40 | 0.85 | 0.05 |
| 前腰 | AM | 0.65 | 0.45 | 0.70 | 0.10 |
| 中场 | CM | 0.70 | 0.50 | 0.50 | 0.35 |
| 边后卫 | FB | 0.55 | 0.65 | 0.40 | 0.30 |
| 边锋 | W | 0.50 | 0.55 | 0.65 | 0.05 |

### 2. 球员伤病信息 (`PlayerInjuryInfo`)

- `injury_status`: fit / doubtful / out / long_term / returning
- `quality_rating`: 球员实力评分 (0-1)
- `is_starter`: 是否主力
- `severity_score`: 伤病严重程度 (doubtful=0.3, out=0.7, long_term=1.0)

### 3. 球队伤病报告 (`TeamInjuryReport`)

- `players`: 球员伤病列表
- `squad_rating` / `bench_rating`: 主力/替补实力
- `media_injury_alert` / `media_injury_impact`: 媒体情报
- 自动计算: `n_injured`, `n_starters_out`, `n_doubtful`

### 4. 全队伤病评估模型 (`TeamInjuryModel`)

#### 5 个量化指标

| 指标 | 计算方法 | 输出范围 |
|------|---------|---------|
| **位置加权缺阵指数 (PWAI)** | Σ(位置重要性 × 不可替代性 × 严重程度) | 0-1 |
| **球员质量降级** | Σ(主力评分 - 替补评分) / 归一化 | 0-1 |
| **复出状态折扣** | 1 - min(1, 复出场次/5) × 位置权重 | 0-1 |
| **阵容深度指数** | bench_rating/squad_rating × 伤病惩罚 | 0-1 |
| **累积伤病指数** | PWAI^(1/(1+0.1×(n-1))) — 非线性叠加 | 0-1 |

#### 比赛级输出特征 (19个)

| 特征名 | 说明 |
|--------|------|
| `home_injury_index` | 主队综合伤病指数 |
| `away_injury_index` | 客队综合伤病指数 |
| `injury_index_diff` | 伤病差 (正值=主队更健康) |
| `home_attack_impact` | 主队进攻端伤病影响 |
| `away_attack_impact` | 客队进攻端伤病影响 |
| `attack_impact_diff` | 进攻伤病差 (正值=主队优势) |
| `home_defense_impact` | 主队防守端伤病影响 |
| `away_defense_impact` | 客队防守端伤病影响 |
| `defense_impact_diff` | 防守伤病差 (正值=主队优势) |
| `home_squad_depth` | 主队阵容深度 |
| `away_squad_depth` | 客队阵容深度 |
| `squad_depth_diff` | 深度差 (正值=主队更厚) |
| `home_quality_deg` | 主队实力降级 |
| `away_quality_deg` | 客队实力降级 |
| `quality_deg_diff` | 降级差 (正值=主队优势) |
| `home_recovery_disc` | 主队复出折扣 |
| `away_recovery_disc` | 客队复出折扣 |
| `recovery_disc_diff` | 折扣差 |
| `total_injury_asymmetry` | 综合伤病不对称性 (加权汇总) |

#### 不对称性加权公式

```
total_injury_asymmetry = 0.35 × injury_index_diff
                       + 0.25 × attack_impact_diff
                       + 0.25 × defense_impact_diff
                       + 0.15 × squad_depth_diff
```

### 5. 数据来源 (代理模式)

当前数据库无独立伤病表，使用代理策略：

| 代理数据 | 来源 | 逻辑 |
|---------|------|------|
| 球队评分 | `teams` 表 | squad_rating / bench_rating |
| 进球骤降 | `form_trends` 表 | 近5场 vs 前5场进球差 > 0.8 → 攻击手疑似缺阵 |
| 失球骤增 | `form_trends` 表 | 近5场 vs 前5场失球差 > 0.8 → 后卫疑似缺阵 |
| 零封率低 | `form_trends` 表 | 近3场零封 < 10% + 场均失球 > 1.5 → 门将疑似状态不佳 |
| 媒体NLP | `media_intelligence` | injury_alert → 0.15 加权 |

### 6. 门将模型协同 (`integrate_keeper_risk`)

将 `KeeperRiskModel` 的评估融合到全队伤病指数：

- 高风险门将 (risk < 0.7): 伤病指数 +0.3 × (0.7 - risk)
- 低风险门将 (risk > 0.85): 伤病指数 -0.1 × (risk - 0.85)
- 防守端影响同步调整

## 使用方式

```python
# 1. 手动构建伤病报告
from optimize.injury_index import TeamInjuryModel, TeamInjuryReport, PlayerInjuryInfo

model = TeamInjuryModel()
home_report = TeamInjuryReport(
    team='利物浦', squad_rating=0.87, bench_rating=0.75,
    players=[
        PlayerInjuryInfo(name='Salah', team='利物浦', position='W',
                        is_starter=True, quality_rating=0.92, injury_status='doubtful'),
    ]
)
features = model.compute_team_injury_index(home_report)

# 2. 比赛级特征
away_report = TeamInjuryReport(team='曼城', squad_rating=0.90, bench_rating=0.82, players=[])
match_features = model.compute_match_features(home_report, away_report)

# 3. 从数据库代理构建
report = model.build_injury_report_from_db('利物浦', '2025-01-15')

# 4. 与门将模型协同
from modules.goalkeeper_model import KeeperRiskModel
keeper_model = KeeperRiskModel()
keeper_eval = keeper_model.evaluate("Alisson", {'games_since_injury': 2})
fused = model.integrate_keeper_risk('利物浦', keeper_eval, injury_index)

# 5. 批量生成并写入 match_features
df = model.generate_features_df()
model.write_to_match_features(features_df=df)

# 6. 追加到 SequenceBundle (DL 模型)
model.augment_bundle(bundle, df, mode='diff')

# 7. 便捷函数
from optimize.injury_index import compute_injury_features, generate_injury_features
features = compute_injury_features(home_team='利物浦', away_team='曼城', match_date='2025-01-15')
```

## 实测结果

| 指标 | 值 |
|------|---|
| 样本数 | 18,224 场 |
| home_injury_index | mean=0.025, std=0.038 |
| away_injury_index | mean=0.027, std=0.040 |
| injury_index_diff | mean=0.002, std=0.057 |
| attack_impact_diff | mean=0.001, std=0.020 |
| defense_impact_diff | mean=0.001, std=0.031 |
| total_injury_asymmetry | mean=0.001, std=0.038 |

> **注**: 当前为代理模式，伤病信号密度低。接入正式伤病API后特征方差会显著增大。

## 扩展计划

1. 接入 football-data.org 的 `injuries` 端点 → 结构化伤病数据
2. 建立 `injuries` 数据库表 → 持久化存储
3. `media_intelligence` NLP 命名实体识别 → 自动提取球员名+伤病类型
4. 历史伤病频率建模 → 球员伤病倾向评估
