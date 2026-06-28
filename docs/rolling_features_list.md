# T12 滚动窗口特征 - 新增特征列表

## 概览

新增 **50 个滚动窗口特征** (每队 50 个, 加差值共 150 列), 通过多窗口统计、方差/一致性、主客场分离、趋势分析、对手强度调整和交锋历史六大维度刻画球队状态。

**文件**: `optimize/rolling_features.py`

---

## 一、多窗口基础统计 (36 个特征)

每个窗口 (3/5/10 场) 计算以下 12 个指标:

| # | 特征名 | 说明 | 归一化 |
|---|--------|------|--------|
| 1 | `r{N}_win_pct` | 胜率 | [0, 1] |
| 2 | `r{N}_draw_pct` | 平局率 | [0, 1] |
| 3 | `r{N}_avg_gf` | 场均进球 | /3.0 → [0, 1] |
| 4 | `r{N}_avg_ga` | 场均失球 | /3.0 → [0, 1] |
| 5 | `r{N}_avg_gd` | 场均净胜球 | /6.0 → [-1, 1] |
| 6 | `r{N}_avg_pts` | 场均得分 | /3.0 → [0, 1] |
| 7 | `r{N}_cs_pct` | 零封率 | [0, 1] |
| 8 | `r{N}_btts_pct` | 双方进球率 | [0, 1] |
| 9 | `r{N}_over25_pct` | 大2.5球率 | [0, 1] |
| 10 | `r{N}_std_gf` | 进球标准差 | /3.0 → [0, 1] |
| 11 | `r{N}_avg_ga` | 失球标准差 | /3.0 → [0, 1] |
| 12 | `r{N}_std_pts` | 得分标准差 | /3.0 → [0, 1] |

窗口: **r3** (近3场, 短期), **r5** (近5场, 中期), **r10** (近10场, 长期)

### 特征重要性排名 (实测)

| 特征 | 综合排名 | 复合得分 | MI得分 | 相关系数 |
|------|---------|---------|--------|---------|
| r10_avg_gd | 11 | 0.285 | 0.055 | 0.340 |
| r10_avg_pts | 12 | 0.273 | 0.055 | 0.319 |
| r10_avg_gf | 14 | 0.236 | 0.042 | 0.308 |
| r10_win_pct | 16 | 0.226 | 0.041 | 0.299 |
| r5_avg_gd | 17 | 0.220 | 0.041 | 0.285 |
| r5_avg_pts | 23 | 0.204 | 0.040 | 0.265 |
| r3_avg_gd | 25 | 0.194 | 0.040 | 0.242 |
| r5_win_pct | 26 | 0.188 | 0.037 | 0.239 |

> 10场窗口特征普遍比3场/5场更重要，说明长期状态比短期波动更有预测力

---

## 二、主客场分离统计 (6 个特征)

仅 5 场窗口, 分别统计主场和客场表现:

| # | 特征名 | 说明 | 归一化 |
|---|--------|------|--------|
| 1 | `rhome5_win_pct` | 近5个主场胜率 | [0, 1] |
| 2 | `rhome5_avg_gf` | 近5个主场场均进球 | /3.0 |
| 3 | `rhome5_avg_ga` | 近5个主场场均失球 | /3.0 |
| 4 | `raway5_win_pct` | 近5个客场胜率 | [0, 1] |
| 5 | `raway5_avg_gf` | 近5个客场场均进球 | /3.0 |
| 6 | `raway5_avg_ga` | 近5个客场场均失球 | /3.0 |

### 特征重要性排名 (实测)

| 特征 | 综合排名 | 复合得分 |
|------|---------|---------|
| rhome5_avg_gf | 24 | 0.202 |
| raway5_avg_gf | 27 | 0.184 |
| raway5_win_pct | 28 | 0.182 |
| raway5_avg_ga | 29 | 0.169 |
| rhome5_win_pct | 31 | 0.163 |

> 主场进攻力(rhome5_avg_gf)是主客场特征中最重要的

---

## 三、趋势/加速度特征 (4 个特征)

| # | 特征名 | 说明 | 归一化 | 计算方法 |
|---|--------|------|--------|---------|
| 1 | `rtrend_pts_10` | 近10场得分趋势 | [-1, 1] | 线性回归斜率 |
| 2 | `rtrend_gf_10` | 近10场进球趋势 | [-1, 1] | 线性回归斜率 |
| 3 | `rtrend_ga_10` | 近10场失球趋势 | [-1, 1] | 线性回归斜率 |
| 4 | `rmomentum_shift` | 形式动量偏移 | [-1, 1] | 近3场均分 - 前3场均分 |

- 正值 = 上升趋势, 负值 = 下滑
- `rmomentum_shift` 捕获加速/减速变化

---

## 四、对手强度调整 (2 个特征)

| # | 特征名 | 说明 | 归一化 |
|---|--------|------|--------|
| 1 | `radj_pts_5` | 对手强度校正得分 | [0, 1] |
| 2 | `rpower_score_10` | 综合实力评分 | [0, 1] |

- `radj_pts_5`: 赢强队得分更高 (对手近5场得分越高, 权重 0.5→1.5)
- `rpower_score_10`: 攻击力40% + 防守力30% + 形式30%

### 特征重要性排名 (实测)

| 特征 | 综合排名 | 复合得分 | MI得分 | 相关系数 |
|------|---------|---------|--------|---------|
| **rpower_score_10** | **9** | **0.313** | 0.075 | 0.341 |
| radj_pts_5 | 19 | 0.217 | 0.043 | 0.264 |

> `rpower_score_10` 是所有滚动特征中排名第9，仅次于 a1~a6 和排名因子

---

## 五、交锋历史 (2 个特征)

| # | 特征名 | 说明 | 归一化 |
|---|--------|------|--------|
| 1 | `rh2h_home_w5` | 近5次交锋主队胜率 | [0, 1] |
| 2 | `rh2h_avg_gd5` | 近5次交锋场均净胜球 | /3.0 → [-1, 1] |

### 特征重要性排名 (实测)

| 特征 | 综合排名 | 复合得分 | MI得分 | 相关系数 |
|------|---------|---------|--------|---------|
| rh2h_home_w5 | 13 | 0.237 | 0.053 | 0.270 |
| rh2h_avg_gd5 | 18 | 0.219 | 0.043 | 0.294 |

---

## 使用方式

### 1. 生成滚动特征 DataFrame

```python
from optimize.rolling_features import generate_rolling_features, RollingWindowConfig

# 使用默认配置 (3/5/10 窗口)
df = generate_rolling_features()

# 自定义窗口
cfg = RollingWindowConfig(windows=(5, 10))
df = generate_rolling_features(config=cfg)
```

### 2. 写入数据库

```python
from optimize.rolling_features import RollingWindowFeatureGenerator

gen = RollingWindowFeatureGenerator()
feat_df = gen.generate()
n_updated = gen.write_to_db(feat_df)
```

### 3. 增强 SequenceBundle (DL 模型)

```python
from optimize.rolling_features import RollingWindowFeatureGenerator
from optimize.sequence_features import SequenceFeatureExtractor

bundle = SequenceFeatureExtractor().extract()
gen = RollingWindowFeatureGenerator()
feat_df = gen.generate()

# 追加到 static_features (19 → 69)
bundle = gen.augment_bundle(bundle, feat_df, mode='diff')
# mode='diff': 仅差值 (+50), mode='both': 主客+差值 (+150)
```

### 4. 特征重要性分析

```python
from optimize.rolling_features import FeatureImportanceAnalyzer, analyze_feature_importance

# 一键分析
report = analyze_feature_importance(X, y, feature_names, model=gbdt)

# 详细分析
analyzer = FeatureImportanceAnalyzer()
mi_df = analyzer.mutual_information(X, y, feature_names)
corr_df = analyzer.correlation_analysis(X, y, feature_names)
perm_df = analyzer.permutation_importance(model, X, y, feature_names)
shap_df = analyzer.shap_analysis(model, X, feature_names)
report = analyzer.full_report(X, y, feature_names, model=model, include_shap=True)
```

---

## 特征重要性分析器方法

| 方法 | 说明 | 需要模型 | 输出 |
|------|------|---------|------|
| `mutual_information()` | 互信息 | 否 | MI得分 |
| `correlation_analysis()` | Spearman相关 | 否 | 相关系数+p值 |
| `permutation_importance()` | 排列重要性 | 是 | 均值+标准差 |
| `gbdt_builtin_importance()` | GBDT原生重要性 | 是(XGB/LGBM/CB) | 增益/分裂次数 |
| `shap_analysis()` | SHAP值 | 是 | 均值+标准差 |
| `full_report()` | 综合报告 | 可选 | 综合排名 |

---

## 数据统计 (实测, 17,846 样本)

| 维度 | 数值 |
|------|------|
| 每队特征数 | 50 |
| 差值特征数 (diff) | 50 |
| 总列数 (match_id + date + home/away/diff) | 152 |
| SequenceBundle 增强后 static 维度 | 19 → 69 (+50) |
| 前30名中滚动特征占比 | 18/30 (60%) |
