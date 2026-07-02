# T08 GBDT 三模型对比评估报告

## 概述

在哨响AI预测系统中集成 LightGBM 和 CatBoost，与现有 XGBoost 横向对比。

## 架构设计

```
                    ┌─────────────────────┐
                    │  GBDTDataAdapter    │
                    │  (统一数据格式适配)   │
                    └──────────┬──────────┘
                               │ TrainingBundle
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
        ┌──────────┐    ┌──────────┐    ┌──────────┐
        │ XGBoost  │    │ LightGBM │    │ CatBoost │
        │ Trainer  │    │ Trainer  │    │ Trainer  │
        └────┬─────┘    └────┬─────┘    └────┬─────┘
             │               │               │
             └───────────────┼───────────────┘
                             ▼
                  ┌─────────────────────┐
                  │ ModelComparison     │
                  │ (三模型横向对比)      │
                  └─────────────────────┘
```

## 组件清单

### 1. GBDTDataAdapter (`optimize/gbdt_adapter.py`)

统一数据预处理管线，三个模型共享同一特征工程：

| 步骤 | 操作 |
|------|------|
| 加载 | SQLite `matches JOIN match_features` |
| 缺失填充 | `DEFAULT_VALUES` 字典（19特征） |
| 异常裁剪 | 99分位数 × 1.5，对称特征取±边界 |
| 交互特征 | 6项 (ix_a1_sigma / ix_a2_lambda / ix_a3_epsilon / ix_a1_a2 / ix_rank_form / ix_power_gap) |
| 标签 | 净胜球 → 三分类 (0=主胜, 1=平局, 2=客胜) |
| 分割 | 时序 85%训练/15%验证 + 10%测试 |
| 标准化 | `StandardScaler` |

输出 `TrainingBundle` 包含: X_train/y_train/X_val/y_val/X_test/y_test/scaler/class_weights

### 2. LightGBMTrainer (`optimize/lightgbm_trainer.py`)

| 属性 | 值 |
|------|-----|
| 核心算法 | LightGBM Gradient Boosting (leaf-wise growth) |
| 分类方式 | multiclass, num_class=3 |
| 默认超参 | num_leaves=31, lr=0.05, 500轮, colsample=0.8, subsample=0.8 |
| 早停 | 50轮无改善 |
| 类别权重 | Balanced + 平局可调 |
| 概率校准 | Isotonic (可选) |
| 持久化 | Joblib |

### 3. CatBoostTrainer (`optimize/catboost_trainer.py`)

| 属性 | 值 |
|------|-----|
| 核心算法 | CatBoost (ordered boosting, symmetric trees) |
| 分类方式 | MultiClass |
| 默认超参 | depth=6, lr=0.05, 500轮, subsample=0.8 (Bernoulli) |
| 早停 | 50轮无改善 |
| 类别权重 | class_weights 列表 |
| 持久化 | Joblib |

### 4. ModelComparison (`optimize/model_comparison.py`)

三模型对比评估框架：
- 统一训练/评估/排名
- XGBoost / LightGBM / CatBoost 在**同一训练/测试集**对比
- 关键指标: 准确率, 平局F1, Brier, LogLoss, MCC
- Pairwise Cohen's Kappa 一致性分析
- 特征重要性对比 (Top-10)
- JSON 报告持久化

## 关键差异对比

| 维度 | XGBoost | LightGBM | CatBoost |
|------|---------|----------|----------|
| 树生长 | Level-wise | **Leaf-wise** | Symmetric |
| 特征处理 | 需要编码 | 自动离散化 | **内置目标编码** |
| 类别特征 | LabelEncoder | categorical_feature | 原生支持 |
| 缺失值 | 需要填充 | 自动处理 | 自动处理 |
| 过拟合控制 | reg_alpha/lambda | num_leaves/min_data | ordered boosting |
| 训练速度 | 中等 | **最快** | 较慢 |
| 概率校准 | 需要校准 | 需要校准 | **天然校准良好** |
| CPU优化 | hist | histogram | 多线程自动 |

## API 集成

`prediction_service.py` 在预测流程中（T07仲裁后）并行运行 LightGBM + CatBoost：

```json
{
  "multi_model": {
    "xgboost": {"home": 0.452, "draw": 0.248, "away": 0.300},
    "lightgbm": {"home": 0.421, "draw": 0.272, "away": 0.307},
    "catboost": {"home": 0.438, "draw": 0.255, "away": 0.307},
    "voting_avg": {"home": 0.437, "draw": 0.258, "away": 0.305}
  }
}
```

- 模型未训练时自动跳过
- 存在 ≥2 个模型时计算 voting_avg
- 所有预测概率 round 到 4 位小数

## 测试结果

**41/41 PASS** (全模块端到端测试)

## 使用指南

### 训练单个模型

```python
from optimize.gbdt_adapter import make_training_bundle
from optimize.lightgbm_trainer import LightGBMTrainer

bundle = make_training_bundle()
trainer = LightGBMTrainer.train_from_bundle(bundle)
trainer.save_model('saved_models/lightgbm_v1.joblib')
```

### 三模型对比

```python
from optimize.model_comparison import run_comparison

report = run_comparison()
# → data/gbdt_comparison_<timestamp>.json
```

### 一键运行

```bash
python -c "from optimize.model_comparison import run_comparison; run_comparison()"
```

## 新增文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `optimize/gbdt_adapter.py` | ~280 | 统一数据预处理管线 |
| `optimize/lightgbm_trainer.py` | ~300 | LightGBM 训练/预测/持久化 |
| `optimize/catboost_trainer.py` | ~300 | CatBoost 训练/预测/持久化 |
| `optimize/model_comparison.py` | ~600 | 三模型对比评估框架 |
| `api/prediction_service.py` | +90 | 多模型并行预测集成 |
| `docs/t08_gbdt_comparison.md` | 本文档 | 架构与策略文档 |
