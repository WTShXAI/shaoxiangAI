# 哨响AI - 模型系统健康度报告

> 诊断时间: 2026-05-31 15:24 | Python 3.10.11 | Windows 10

---

## 一、整体评分

| 维度 | 状态 | 评分 |
|------|------|------|
| 环境 & 依赖 | 🟢 健康 | A |
| 模型文件完整性 | 🟡 需清理 | B |
| 数据管道 | 🟢 健康 | A- |
| 特征质量 | 🟢 健康 | A |
| 模型版本一致性 | 🔴 有冲突 | D |
| 生产就绪度 | 🟡 需修复 | C+ |

**综合评分: B (75/100)**

---

## 二、模型文件清单

### 2.1 统计概览

| 类别 | 数量 | 格式 | 大小范围 |
|------|------|------|----------|
| Ridge 回归 | **16** | `.pkl` (LinearRegressionTrainer) | 2.3 KB / 全部 |
| XGBoost 分类器 | **10** | `.pkl` (LinearRegressionTrainer) | 915 - 11,299 KB |
| 集成管道 | **1** | `.joblib` (EnsembleTrainer v3.0) | 1,519 KB |
| **合计** | **27** | 3 种格式 | — |

### 2.2 最新模型详情

#### 集成管道 (生产推荐)
```
文件: football_ensemble_20260531_104049.joblib
版本: v3.0
训练时间: 2026-05-31 10:40
特征数: 19 (全部)
包含: XGBoost ✅ | Ridge ✅ | StandardScaler ✅
评估指标: 已嵌入
```

#### 最新 XGBoost (独立)
```
文件: model_xgboost_20260530_134327.pkl
特征数: 14 ⚠️ (缺少5个可选特征)
类型: LinearRegressionTrainer 字典封装
```

#### 最新 Ridge (独立)
```
文件: model_ridge_20260531_122049.pkl
特征数: 19 (全部)
类型: LinearRegressionTrainer 字典封装
```

---

## 三、🔴 关键问题: 模型加载路径冲突

### 问题描述

项目中存在 **3 种模型持久化格式**，不同模块加载不同的格式：

```
┌─────────────────────────────────────────────────┐
│  prediction_engine.py                            │
│  → EnsembleTrainer.load_pipeline()               │
│  → 期望 .joblib 格式 (19特征, 三模型集成)        │
│  → 加载成功 ✅                                   │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  api/prediction_service.py (Flask API)             │
│  → LinearRegressionTrainer.load_model()           │
│  → 期望 .pkl 格式 (14-19特征, 单模型)            │
│  → 加载最新的 .pkl XGBoost/Ridge                 │
│  → ⚠️ 未使用集成模型!                            │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│  auto_pipeline.py                                │
│  → pickle.load()                                 │
│  → 独立 .pkl 格式                                │
│  → ⚠️ 使用原始 pickle，不兼容 joblib            │
└─────────────────────────────────────────────────┘
```

### 影响分析

| 模块 | 实际加载 | 特征数 | 是否集成 | 后果 |
|------|---------|--------|---------|------|
| `prediction_engine.py` | Ensemble v3.0 | 19 | ✅ XGB+Ridge+启发式 | 正确 |
| `prediction_service.py` | 最新XGB .pkl | 14 | ❌ 单模型 | **未使用集成权重** |
| `auto_pipeline.py` | 自训练模型 | 6 | ❌ 简单模型 | 预测质量低 |

### 修复建议 (P0)

**方案**: 统一 `prediction_service.py` 使用 Ensemble pipeline

1. 在 `api/prediction_service.py` 中导入 `EnsembleTrainer`
2. 修改模型加载逻辑为 `EnsembleTrainer.load_pipeline()`
3. 确保 19 特征全部可用（当前 DB 中全部存在 ✅）

---

## 四、🟡 次要问题

### 4.1 模型文件冗余

`saved_models/` 目录积压 **27 个文件**，其中：

| 保留 | 删除 | 原因 |
|------|------|------|
| `football_ensemble_*.joblib` (1个) | — | 生产模型 |
| `model_xgboost_20260530_134327.pkl` (1个) | 其他 9 个旧XGB | 保留最新 |
| `model_ridge_20260531_122049.pkl` (1个) | 其他 15 个旧Ridge | 保留最新 |

建议清理 24 个旧文件，节省 ~36 MB。

### 4.2 训练元数据路径过期

`data/training_metadata.json` 引用了重命名前的路径:
```
"xgb_path": "C:\\Users\\vboxuser\\Desktop\\AI\\windows哨响AI\\..."
"ridge_path": "C:\\Users\\vboxuser\\Desktop\\AI\\windows哨响AI\\..."
```
这些文件已不存在（目录已重命名为 `footballAI`）。不影响运行，但标记为过期。

### 4.3 特征数量不一致

| 来源 | 特征数 | 
|------|--------|
| `config.yaml` | 19 |
| `ensemble_trainer.py` 实际使用 | 19 |
| `train_xgb_optimized.py` 实际使用 | **14** (缺少 5) |
| `match_features` 表 | 19 (全) |

`train_xgb_optimized.py` 使用硬编码的 `FEATURE_COLS` (19个)，但训练时 DROP 了高比例默认值特征导致只剩 14 个。实际运行时 DB 特征质量好、无特征被移除，所以 19 特征模型是从 `ensemble_trainer.py` 训练的。

### 4.4 DB 中 7 个额外列未在配置中

`match_features` 表包含 `feature_id`, `discussion_growth`, `news_impact`, `time_suppression`, `arbitrage_index`, `arbitrage_window`, `created_at` 这 7 列在 `config.yaml` 的特征列表中不存在。这是正常的——它们是 feature_calculator 计算的中间结果，不在训练特征中。

---

## 五、🟢 健康指标

### 5.1 环境

| 依赖 | 版本 | 状态 |
|------|------|------|
| Python | 3.10.11 | ✅ |
| numpy | 2.2.6 | ✅ |
| pandas | 2.3.3 | ✅ |
| scikit-learn | 1.7.2 | ✅ |
| xgboost | 3.2.0 | ✅ |
| scipy | 1.15.3 | ✅ |
| joblib | 1.5.3 | ✅ |
| flask | 3.1.3 | ✅ |
| PyYAML | 6.0.3 | ✅ |
| psutil | 7.2.2 | ✅ |

### 5.2 数据库

| 指标 | 值 | 状态 |
|------|-----|------|
| 总比赛数 | 846 | ✅ |
| 已完成 (有比分) | 800 | ✅ |
| 待预测 | 40 | ✅ |
| 特征记录 | 838 | ✅ |
| 特征覆盖率 | 104.8% | ✅ |
| 预测记录 | 38 | ✅ |
| 日期范围 | 2020-08 ~ 2026-06 | ✅ 6 年覆盖 |
| 联赛数 | 14 (英超~中超) | ✅ |

### 5.3 特征质量

| 检查项 | 结果 |
|--------|------|
| 默认值占比 > 80% | **0 个特征** ✅ |
| 缺失值占比 | < 2% 全部特征 ✅ |
| 极端异常值 | 已裁剪 ✅ |
| 核心特征分布 | A1[-1.9, 2.1], A2[0.1, 0.9], A3[0.2, 0.8] 合理 ✅ |

### 5.4 模型性能 (已知基准)

| 模型 | 指标 | 值 |
|------|------|-----|
| XGBoost | 准确率 | ~50.1% |
| Ridge | R² | ~0.25 |
| 集成 | Walk-Forward 回测 | ~49.1% |

---

## 六、修复优先级

| 优先级 | 问题 | 工作量 | 影响 |
|--------|------|--------|------|
| **P0** | prediction_service.py 未使用集成模型 | 小 | **高** - Flask API 预测不准 |
| **P1** | 清理过期模型文件 (24个) | 小 | 低 - 磁盘 + 维护 |
| **P1** | 统一 train_xgb_optimized 特征配置 | 中 | 中 - 训练一致性 |
| **P2** | 更新 training_metadata.json 路径 | 极小 | 低 |
| P3 | auto_pipeline 升级使用集成模型 | 大 | 中 |

---

## 七、数据管道端到端验证

```
✅ 步骤1: football-data.org API → data_collector → matches表 (846条)
✅ 步骤2: feature_calculator.py → match_features表 (838条, 19维)
✅ 步骤3: StandardScaler + 缺失值填充 + 异常值裁剪 (ensemble_trainer.py)
✅ 步骤4: XGBoost(50%) + Ridge(30%) + Heuristic(20%) 集成 (joblib pipeline)
✅ 步骤5: prediction_engine.py → CSV + predictions表 (38条)
⚠️ 步骤6: auto_pipeline.py → 回测 → 使用独立pickle模型 (非集成)
```

---

*报告文件: `docs/MODEL_HEALTH_REPORT.md` | JSON数据: `data/model_health_report.json`*
