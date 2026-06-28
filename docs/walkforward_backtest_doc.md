# T17 滚动窗口回测框架

## 概述

Walk-Forward 回测框架，实现时序验证策略、按月/季度/赛季分割、滚动窗口回测引擎，
以及回测结果分析和历史性能报告生成。

## 新增文件

| 文件 | 说明 |
|---|---|
| `optimize/walkforward_backtest.py` | 滚动窗口回测核心模块 |

## 核心组件

### 1. TimeSplitter — 时间分割策略

支持按月/季度/赛季/年/自定义频率的 walk-forward 分割：

```python
from optimize.walkforward_backtest import TimeSplitter

# 季度扩展窗口
splitter = TimeSplitter(freq='quarter', window='expanding', min_train=500, min_test=50)
folds = splitter.split(df, date_col='match_date')

# 月度滑动窗口 (最近6个月训练)
splitter = TimeSplitter(freq='month', window='sliding', train_size=6, min_train=200)

# 年度扩展
splitter = TimeSplitter(freq='year', window='expanding')
```

参数说明：
- `freq`: 'month' | 'quarter' | 'season' | 'year' | 'custom'
- `window`: 'expanding' (递增窗口) | 'sliding' (固定窗口)
- `train_size`: 滑动窗口训练集大小 (按 freq 单位)
- `min_train` / `min_test`: 最小训练/测试样本数
- `gap`: 训练集和测试集之间的间隔 (避免数据泄露)

### 2. WalkForwardEngine — 回测引擎

支持两种模式：

**(A) 预计算概率模式** — DataFrame 中已有预测概率列：
```python
engine = WalkForwardEngine()
result = engine.run(df, folds,
    label_col='result_label',
    prob_cols=['home_prob', 'draw_prob', 'away_prob'])
```

**(B) 预测器工厂模式** — 每折重新训练并预测：
```python
def my_predictor(train_df, test_df):
    # 训练模型...
    return y_proba, y_true  # (n, 3), (n,)

engine = WalkForwardEngine(predictor_factory=my_predictor)
result = engine.run(df, folds, label_col='result_label')
```

可选校准 (集成 T15):
```python
engine = WalkForwardEngine(calibrate=True, calibrate_method='auto')
```

### 3. BacktestResult — 回测结果容器

```python
# 汇总指标
summary = result.summary()
# {'overall_accuracy': 0.50, 'overall_brier': 0.62, ...}

# 折级 DataFrame
fold_df = result.to_dataframe()

# 性能退化检测
alerts = result.degradation_check(window=3, threshold=0.05)
# [{'severity': 'WARNING', 'metric': 'accuracy', 'fold': 10, ...}]

# 置信区间 (Bootstrap 95%)
mean, lower, upper = result.confidence_interval('accuracy')
```

### 4. BacktestVisualizer — 可视化工具

6 类图表：

| 图表 | 方法 | 说明 |
|---|---|---|
| 滚动性能曲线 | `rolling_performance()` | 逐折 Acc/Brier/ECE/LL + 趋势线 |
| 逐折雷达图 | `fold_radar()` | 多维度折对比 |
| 指标热力图 | `metric_heatmap()` | 折 × 指标 或 折 × 联赛 |
| 策略对比 | `strategy_comparison()` | 多策略柱状图 + 逐折趋势 |
| 混淆矩阵 | `confusion_overview()` | 汇总 3×3 混淆矩阵 |
| 一键全图 | `generate_all_charts()` | 上述所有图表 |

### 5. BacktestReportBuilder — HTML 报告

自包含 HTML 报告 (图表 base64 嵌入):
- 核心指标卡片
- 退化告警
- 所有图表
- 折级明细表
- Bootstrap 置信区间

## 便捷函数

### 一键回测

```python
from optimize.walkforward_backtest import run_walkforward_backtest

result, report_path = run_walkforward_backtest(
    df,
    prob_cols=['home_prob', 'draw_prob', 'away_prob'],
    label_col='result_label',
    date_col='match_date',
    freq='quarter',
    window='expanding',
    min_train=500,
    min_test=50,
    output_dir='evaluation_results/t17',
    generate_report=True,
)
```

### 多策略对比

```python
from optimize.walkforward_backtest import run_multi_strategy_comparison

strategies = {
    'ELO': ['home_prob_elo', 'draw_prob_elo', 'away_prob_elo'],
    'Ensemble': ['home_prob_ens', 'draw_prob_ens', 'away_prob_ens'],
}
results, _ = run_multi_strategy_comparison(df, strategies, freq='quarter')
```

## 真实数据验证结果 (54,200 场)

| 指标 | 值 | 95% CI |
|---|---|---|
| Overall Accuracy | 0.5000 | [0.4912, 0.5108] |
| Overall Brier | 0.6229 | [0.6160, 0.6281] |
| Overall ECE | 0.0175 | [0.0412, 0.0526] |
| Overall MCC | 0.2196 | — |
| Folds | 40 | 2015Q4 ~ 2026Q2 |

退化检测发现 6 个告警:
- ACCURACY: 3 个 (含 1 个 CRITICAL at 2026Q2)
- BRIER: 1 个 WARNING
- ECE: 1 个 WARNING
- LOG_LOSS: 2 个 WARNING

## 依赖

- numpy, pandas, matplotlib
- sklearn (accuracy_score, log_loss, confusion_matrix, ...)
- T15 `optimize.calibration` (compute_ece, multiclass_brier, CalibratorSuite)
