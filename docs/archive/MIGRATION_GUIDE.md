# 迁移指南: LinearRegressionTrainer → EnsembleTrainer

> **生效日期**: 2026-06-02  
> **弃用截止**: 2026-09-01（截止后旧模型接口将移除）  
> **目标**: 安全、平滑地从旧版单模型训练器迁移到新版集成训练器

---

## 变化概览

| 维度 | LinearRegressionTrainer (旧) | EnsembleTrainer (新) |
|------|---------------------------|----------------------|
| 特征数量 | 3-9 个 | 26+ 个（含交互特征） |
| 模型架构 | 单模型 (Linear/Ridge/Lasso/XGBoost) | 集成 XGBoost + Ridge + 启发式 |
| 概率输出 | 间接 (goal_diff→softmax) | 直接概率 (calibrated) |
| 概率校准 | 无 | Isotonic/Platt/None |
| 序列化格式 | `.pkl` (pickle) | `.joblib` (含 scaler/config/eval) |
| 时序切分 | 随机切分 | 时间序列切分 |
| 自定义类别权重 | 有限 | draw_weight + class_weight 全面 |
| 测试准确率 | ~41% | ~45% (v3.1 no_cal+no_heu) |
| 客胜召回率 | ~15% | ~33% |

---

## 逐步迁移

### 步骤1: 模型加载

```python
# ❌ 旧方式
from models.linear_regression_trainer import LinearRegressionTrainer
model = LinearRegressionTrainer.load("old_model.pkl")

# ✅ 新方式
from ensemble_trainer import EnsembleTrainer
model = EnsembleTrainer.load("saved_models/football_ensemble_latest.joblib")
```

### 步骤2: 预测接口

```python
# ❌ 旧方式 — 返回 goal_diff 数值
goal_diff = model.predict(features)
result = "H" if goal_diff > 0.5 else ("D" if abs(goal_diff) <= 0.5 else "A")

# ✅ 新方式 — 直接返回概率
proba = model.ensemble_predict_proba(features)
pred_idx = proba.argmax()
result = ["H", "D", "A"][pred_idx]
confidence = proba[pred_idx]
```

### 步骤3: 训练

```python
# ❌ 旧方式
trainer = LinearRegressionTrainer(model_type='ridge', alpha=1.0)
trainer.train(X_train, y_train)  # 需要手动构造 X, y

# ✅ 新方式
trainer = EnsembleTrainer()
trainer.train(training_data)  # 传入 DataFrame，自动特征工程+时序切分
```

### 步骤4: 评估

```python
# ❌ 旧方式
predictions = model.predict(X_test)
mse = mean_squared_error(y_test, predictions)

# ✅ 新方式
X_test, y_test = trainer.prepare_features(test_data)
proba = trainer.ensemble_predict_proba(X_test)
# 自动计算 accuracy, ECE, 各类召回率等
```

---

## 兼容性包装器

在弃用截止前，提供兼容层帮助平滑过渡：

```python
# scripts/migrate_from_legacy.py 提供兼容包装
from scripts.migrate_from_legacy import LegacyCompatWrapper

# 包装旧模型为兼容接口
compat = LegacyCompatWrapper("old_model.pkl")
proba = compat.ensemble_predict_proba(features)  # 旧模型模拟新接口
```

---

## 自动迁移工具

使用 `scripts/migrate_from_legacy.py` 自动迁移：

```bash
# 迁移旧模型文件
python scripts/migrate_from_legacy.py --model old_model.pkl --output migrated/

# 迁移旧训练数据
python scripts/migrate_from_legacy.py --data old_training_data.csv --output migrated/
```

---

## 常见问题

**Q: 旧模型还能用吗？**  
A: ⚠️ 自 2026-06-02 起，每次实例化 `LinearRegressionTrainer` 都会显示弃用警告。功能继续可用至 2026-09-01，之后正式移除。

**Q: 如何抑制弃用警告？**  
```python
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="linear_regression_trainer")
```

**Q: 新模型比旧模型快/慢吗？**  
A: 训练时间略长（特征更多），但预测速度相当（~1-2ms/场）。新模型准确率高约4个百分点。

**Q: 需要重新训练吗？**  
A: 是的。旧 .pkl 文件无法直接加载为新格式，需要用新数据重新训练或使用自动迁移脚本。
