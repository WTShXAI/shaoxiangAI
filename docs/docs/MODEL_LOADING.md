# 模型加载指南 (LAMF v4.1.0)

> 更新: 2026-06-12 · 架构: ModelBridge v2.0 · 模型: XGBoost + Ridge 双模型锁定

---

## 一、模型文件清单

当前 `saved_models/` 目录仅保留 **4 个文件**，所有旧的 experts/、leagues/、production/ 子目录已在项目瘦身 V1.0 中移除。

| 文件 | 用途 | 备注 |
|------|------|------|
| `football_balanced_production.joblib` | **锁定生产模型** (XGBoost + Ridge 集成) | ModelBridge v2.0 强制锁定，不可替换 |
| `footballai_compressed_features.json` | 90+ 特征名列表 | 模型加载时读取特征列名 |
| `footballai_v4_latest.joblib` | v4.0 训练产物 (参考用) | 非生产路径，仅用于对比研究 |
| `model_registry_v2b.json` | 模型版本注册表 | 记录训练历史与元数据 |

### 已删除（不存在！）

以下目录和文件在瘦身中已永久移除，代码中**禁止引用**：

```
❌ saved_models/experts/          整个目录
❌ saved_models/leagues/          整个目录
❌ saved_models/production/       整个目录
❌ unified_ensemble_latest.joblib
❌ footballai_expert_latest.joblib
❌ experts/trend.joblib 等6个专家模型
❌ UnifiedPredictor
❌ SmartIntegration
```

---

## 二、ModelBridge v2.0 核心设计

文件: `agents/model_bridge.py` (526 行)

ModelBridge 是 Agent 系统访问 ML 模型的**唯一入口**，v2.0 版引入 7 项加固措施。

### 2.1 单例模式 + 线程安全

```python
from agents.model_bridge import get_model_bridge

bridge = get_model_bridge()  # 全局唯一实例，线程安全
```

- `ModelBridge.__new__` 使用双重检查锁确保全局唯一
- 首次调用 `get_model_bridge()` 自动触发 `initialize()`

### 2.2 强制锁定模型

```python
LOCKED_MODEL_FILENAME = "football_balanced_production.joblib"
```

- 默认加载 `saved_models/football_balanced_production.joblib`
- 只有在显式传入 `model_path` 参数时才允许加载其他模型（生产日志记录警告）
- 禁止自动回退到 `footballai_v4_latest.joblib` 或其他模型

### 2.3 Fail-Fast: 模型缺失时终止启动

```python
class ModelNotAvailableError(RuntimeError):
    """模型文件缺失或加载失败，系统无法启动"""
    pass
```

- 若 `football_balanced_production.joblib` 不存在，立即抛出 `ModelNotAvailableError`
- **不会静默降级**到规则 Fallback 或返回假概率
- 上游 (`main.py cmd_agent`) 捕获此异常并提示用户重新训练模型

### 2.4 硬编码概率检测

```python
HARDCODED_PROBS = {"home": 0.40, "draw": 0.28, "away": 0.32}
HARDCODED_PROB_THRESHOLD = 0.02  # ±2%
```

- 每次 `predict()` 调用后自动检测：若 H/D/A 在硬编码值的 ±2% 范围内 **且** 至少 2 个概率匹配 → 抛出 `HardcodedProbabilityError`
- 双重防线：ModelBridge 内部检测 + MathAgent 规则 Fallback 层二次检测

### 2.5 审计字段

每次预测结果附加以下字段：

| 字段 | 类型 | 示例 | 说明 |
|------|------|------|------|
| `_model` | str | `football_balanced_production.joblib` | 使用的模型文件名 |
| `_version` | str | `3.0` | 模型版本号（从 pipeline 元数据读取） |
| `_timestamp` | str | `2026-06-11T10:00:00+00:00` | ISO8601 UTC 时间戳 |
| `_feature_count` | int | `91` | 实际输入的特征数量 |

### 2.6 预测快照

每次 `predict()` 调用自动写入 `logs/predictions/prediction_YYYYMMDD_HHMMSS_<hash6>.json`：

```json
{
  "prediction": {
    "home": 0.45, "draw": 0.28, "away": 0.27,
    "_model": "football_balanced_production.joblib",
    "_version": "3.0",
    "_timestamp": "2026-06-11T10:00:00+00:00",
    "_feature_count": 90
  },
  "match_data_keys": ["home_attack_strength", "away_defense_strength", ...],
  "match_data_sample": { ... }
}
```

快照保存失败不会阻塞预测流程（仅记录警告日志）。

---

## 三、调用方式

### 基本用法

```python
from agents.model_bridge import get_model_bridge

bridge = get_model_bridge()
result = bridge.predict(match_data)

# 返回示例:
# {
#     "home": 0.45, "draw": 0.28, "away": 0.27,
#     "_model": "football_balanced_production.joblib",
#     "_version": "3.0",
#     "_timestamp": "2026-06-11T10:00:00+00:00",
#     "_feature_count": 90
# }
```

### 在 Agent 中使用

`MathAgent` (`agents/math_agent.py`) 是 ModelBridge 的主要消费者：

```python
# MathAgent._analyze_with_bridge() 内部实现
bridge = get_model_bridge()
if bridge._available:
    result = bridge.predict(match_data)
    return result  # 含审计字段
```

### 属性查询

```python
bridge.available       # bool — 模型是否可用
bridge.feature_names   # List[str] — 90+ 特征名
bridge.model_name      # str — 当前模型文件名
bridge.model_version   # str — 模型版本号
```

---

## 四、三层降级策略

文件: `agents/math_agent.py`

MathAgent 的 `invoke()` 方法按优先级依次尝试：

| 层级 | 方法 | 入口 | 方式 | 触发条件 |
|------|------|------|------|---------|
| **L1** | `_analyze_with_llm()` | MathAgent.invoke() | Ollama phi4:14b 数学推理 | 默认首选 |
| **L2** | `_analyze_with_bridge()` | MathAgent.invoke() | ModelBridge ML 推理 (XGBoost+Ridge) | L1 失败 / Ollama 不可用 |
| **L3** | `_analyze_with_rules()` | MathAgent.invoke() | 领域知识修正 + 泊松分布 + Kelly 准则 | L1+L2 均失败 |

```
用户请求
  │
  ▼
MathAgent.invoke()
  │
  ├── L1: try phi4:14b LLM 推理 ── 成功 → 返回概率+评估
  │         │ 失败
  ├── L2: try ModelBridge.predict() ── 成功 → 返回概率+审计字段
  │         │ 失败
  └── L3: 规则 Fallback (领域知识+泊松+Kelly) ── 必须成功 (兜底)
```

### Fail-Fast 约束

- L1 中 ModelBridge 不可用 → 直接 `raise RuntimeError("Fail-Fast: 模型不可用")`
- L2 失败时**不会**尝试加载其他模型文件做回退
- L3 中的规则 Fallback 若产生硬编码概率 (H=0.40/D=0.28/A=0.32)，会被双重检测拦截

---

## 五、模型格式兼容

ModelBridge v2.0 支持两种格式（仅用于兼容，生产推荐格式 1）：

### 格式 1: EnsembleTrainer 标准格式 (推荐)

```python
pipeline = {
    'xgb_model': XGBClassifier,
    'ridge_model': RidgeClassifier,
    'scaler': StandardScaler,
    'feature_names': [...],
    'eval_metrics': {...},
    'version': '3.0',
    'timestamp': '...',
    'config': {...}
}
```

- `_load_ensemble_trainer_format()` 加载
- `_predict_with_trainer()` 预测（调用 `EnsembleTrainer.predict_batch()`）
- 日志: `✅ EnsembleTrainer 格式模型加载成功 (锁定)`

### 格式 2: V4 多 Seed 集成格式 (兼容)

```python
pipeline = {
    'models': [XGBoost*N],
    'scalers': [StandardScaler*N],
    'calibrators': [IsotonicRegression*N],
    'selected_features': [...],
    'n_seeds': N
}
```

- `_load_v4_format()` 加载（带警告日志）
- `_predict_with_v4()` 预测（多模型平均 + 概率校准 + 归一化）
- 日志: `⚠️ 加载的模型是 V4 多 seed 格式，不是推荐的 EnsembleTrainer 格式`

---

## 六、故障处理

| 现象 | 原因 | 处理 |
|------|------|------|
| `ModelNotAvailableError` | `saved_models/football_balanced_production.joblib` 缺失 | 重新训练模型或从备份恢复 |
| `HardcodedProbabilityError` | 预测结果为硬编码值 H=0.40/D=0.28/A=0.32 | 检查特征是否正确计算、模型是否正常加载 |
| `get_model_bridge()` 返回 None | 模型加载过程中抛出异常 | 检查日志，确认模型文件完整性 |
| 预测结果始终相同 | 特征缺失值全部由默认值填充 | 检查 `config.yaml` 中的 `default_values`，确认数据源正常 |

### 快速自检

```bash
# 验证 ModelBridge 能否正常加载
python -c "from agents.model_bridge import get_model_bridge; b = get_model_bridge(); print('OK:', b.model_name, '| 特征数:', len(b.feature_names))"

# 预期输出:
# OK: football_balanced_production.joblib | 特征数: 90
```
