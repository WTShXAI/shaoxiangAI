# T15 概率校准模块

## 概述

实现多种概率校准方法 (Platt Scaling, Temperature Scaling, Isotonic Regression, Beta Calibration)，提供统一对比框架和稀疏数据适应性测试。

**新增文件**: `optimize/calibration.py`

## 4 种校准方法

### 1. Platt Scaling (逻辑回归校准)

```
P_cal(y=c|x) = sigmoid(A · logit(p_c) + B)
```

- 每个类别 one-vs-rest 训练 LogisticRegression
- 输入: logit(原始概率) → 输出: 校准概率
- **优势**: 经典稳健, 适合中等样本 (100+)
- **劣势**: 假设 sigmoid 形状的校准曲线, 可能恶化已较好校准的概率

### 2. Temperature Scaling (温度缩放)

```
P_cal = softmax(logits / T)
```

- 单参数 T, 通过 NLL 最小化搜索最优温度
- T > 1: 概率更平滑 (修正过度自信)
- T < 1: 概率更尖锐 (修正欠自信)
- **优势**: 只有一个参数, 极不易过拟合, 适合小样本 (30+)
- **劣势**: 只能缩放不能改变排序, 对系统性偏差修正有限

### 3. Isotonic Regression (保序回归)

```
P_cal(x) = f(x), 其中 f 单调递增
```

- 非参数方法, 对每个类别单独训练 IsotonicRegression
- 输入: 该类原始概率 → 输出: 单调映射后的校准概率
- **优势**: 最灵活, 能拟合任意单调校准曲线, 适合大数据 (300+)
- **劣势**: 小样本易过拟合, 阶梯状输出

### 4. Beta Calibration (Beta 分布校准)

```
P_cal = u / (u + v), u = a·x^c, v = b·(1-x)^c
```

- 三参数 (a, b, c) 通过 Nelder-Mead 优化
- 比 Platt 多一个 shape 参数, 比 Isotonic 更平滑
- **优势**: 最灵活的参数方法, 平滑校准曲线
- **劣势**: 需要 scipy, 中等样本需求 (100+), 优化可能不收敛

## 核心组件

### 校准器

| 类 | 参数数 | 最小样本 | 过拟合风险 |
|---|---|---|---|
| `PlattScaler` | 2×C | 50 | 低 |
| `TemperatureScaler` | 1 | 30 | 极低 |
| `IsotonicScaler` | 非参数 | 300 | 中 |
| `BetaScaler` | 3×C | 100 | 中 |

### 评估指标

| 指标 | 公式 | 说明 |
|---|---|---|
| ECE | Σ(n_b/N)\|acc_b - conf_b\| | 期望校准误差 |
| MCE | max\|acc_b - conf_b\| | 最大校准误差 |
| Brier | (1/N)ΣΣ(p_ij - y_ij)² | 概率均方误差 |
| Log Loss | -(1/N)Σy·log(p) | 对数损失 |

### CalibratorSuite (统一框架)

```python
from optimize.calibration import CalibratorSuite

suite = CalibratorSuite()
suite.fit(y_true, raw_probs)              # 训练全部4种方法
compare_df = suite.compare()               # 对比报告
best = suite.best_method()                 # 自动选最优
calibrated = suite.predict(raw_probs)      # 用最优方法校准

# 稀疏数据适应性
sparse_df = suite.sparse_data_test()       # 不同样本量下的校准质量

# 持久化
suite.save('calibrator.joblib')
```

## 真实数据测试结果 (54,200 场, Elo 隐含概率)

### 对比报告 (20% 测试集)

| 方法 | Brier 前→后 (delta) | ECE 前→后 (delta) | LL 前→后 (delta) | 建议 |
|---|---|---|---|---|
| Isotonic | 0.606→0.584 (+0.022) | 0.040→0.054 (-0.014) | 1.030→0.982 (+0.049) | skip |
| Beta | 0.606→0.584 (+0.022) | 0.040→0.051 (-0.011) | 1.030→0.982 (+0.048) | skip |
| Temperature | 0.606→0.614 (-0.008) | 0.040→0.096 (-0.056) | 1.030→1.030 (+0.001) | skip |
| Platt | 0.606→0.609 (-0.003) | 0.040→0.108 (-0.068) | 1.030→1.014 (+0.016) | skip |

> Elo 隐含概率已经相当校准 (ECE=0.040), 校准后反而恶化 ECE。但 Isotonic/Beta 在 Brier 和 LL 上仍有改进。

### 全数据校准质量 (非 hold-out)

| 方法 | ECE | Brier | home_mean | draw_mean | away_mean |
|---|---|---|---|---|---|
| 原始 | 0.018 | 0.623 | 0.419 | 0.162 | 0.419 |
| Isotonic | **0.010** | **0.599** | 0.443 | 0.250 | 0.307 |
| Beta | 0.018 | 0.601 | 0.443 | 0.251 | 0.306 |
| Temperature | 0.048 | 0.623 | 0.394 | 0.212 | 0.394 |
| Platt | 0.059 | 0.624 | 0.338 | 0.338 | 0.324 |

> Isotonic 全局 ECE 最低 (0.010), 且正确修正了 draw 概率低估 (0.162→0.250)

### 稀疏数据适应性 (ECE 改进)

| 样本量 | Beta | Isotonic | Platt | Temperature |
|---|---|---|---|---|
| 50 | **+0.159** | +0.104 | +0.112 | +0.238 |
| 100 | +0.029 | +0.036 | -0.133 | +0.035 |
| 200 | -0.076 | -0.089 | -0.032 | **+0.113** |
| 500 | -0.040 | +0.004 | 0.000 | -0.008 |
| 1000 | +0.127 | +0.105 | +0.051 | +0.062 |
| 5000 | +0.017 | +0.024 | +0.015 | +0.017 |

> 极小样本 (50): Temperature 最稳健; 中等样本 (200-500): Isotonic 最优; 大样本 (1000+): Beta/Isotonic 并列

### 可靠性图 (Beta 校准前后)

| 区间 | 校准前 gap | 校准后 gap |
|---|---|---|
| 0.3-0.4 | -0.014 | +0.007 |
| 0.4-0.5 | -0.009 | +0.032 |
| 0.5-0.6 | +0.076 | +0.074 |
| 0.6-0.7 | +0.076 | +0.123 |
| 0.7-0.8 | +0.151 | +0.114 |

## 与现有 ExpertCalibrator 的关系

T15 的 `CalibratorSuite` 是 `agents/expert_calibrator.py` 的增强:

| 维度 | ExpertCalibrator (旧) | CalibratorSuite (T15) |
|---|---|---|
| 方法数 | 3 (logistic/platt/isotonic) | 4 (+temperature/beta) |
| 自动选择 | 无 | `best_method()` |
| 对比报告 | 单方法指标 | 多方法 DataFrame |
| 稀疏测试 | 无 | `sparse_data_test()` |
| 可靠性图 | 无 | `compute_reliability()` |
| 评估指标 | Log Loss + Accuracy | ECE + MCE + Brier + LL |

桥接函数: `upgrade_expert_calibrator(expert_name, db_path)` — 在 ExpertCalibrator 基础上做全方法对比

## 用法

```python
from optimize.calibration import (
    CalibratorSuite, PlattScaler, TemperatureScaler,
    IsotonicScaler, BetaScaler,
    compute_ece, calibrate_predictions, compare_calibrators,
)

# 快捷校准
cal_probs = calibrate_predictions(y_true, raw_probs, method='isotonic')

# 快捷对比
df = compare_calibrators(y_true, raw_probs)

# 完整流程
suite = CalibratorSuite()
suite.fit(y_true, raw_probs)
print(suite.compare())
print(f"最优: {suite.best_method()}")
calibrated = suite.predict(raw_probs)
sparse_results = suite.sparse_data_test()

# 单方法使用
scaler = PlattScaler(C=0.1)  # 更强正则化
scaler.fit(probs_train, y_train)
cal = scaler.predict(probs_test)
```
