# T16 校准可视化与ECE监控

## 模块概述

`optimize/calibration_viz.py` — 概率校准的可视化工具集，包含可靠性图生成、ECE 时间序列监控、多方法对比图表和 HTML 评估报告。

依赖 T15 `calibration.py` 的核心计算函数。

## 核心组件

### 1. CalibrationVisualizer — 校准可视化工具

| 方法 | 说明 | 输出 |
|------|------|------|
| `reliability_diagram()` | 可靠性图 (Raw vs Calibrated) | PNG |
| `before_after_comparison()` | 校准前后 ECE/Brier/LL 对比柱状图 | PNG |
| `multi_method_comparison()` | 多方法改进量柱状图 + 雷达图 | PNG |
| `confidence_histogram()` | 置信度分布直方图 | PNG |
| `class_wise_reliability()` | Home/Draw/Away 分类别可靠性图 | PNG |
| `sparse_data_chart()` | 稀疏数据适应性曲线 | PNG |
| `generate_all_charts()` | 一键生成全部 6 张图表 | Dict[name, path] |

**可靠性图设计**:
- 横轴: 置信度 (Confidence), 纵轴: 准确率 (Accuracy)
- 对角虚线: 完美校准
- 绿色/红色柱: 过自信/欠自信 gap
- 标注: ECE 值 + 每分箱样本量

### 2. ECEMonitor — ECE 时间序列监控

| 方法 | 说明 |
|------|------|
| `track(y_true, probs, method, label)` | 记录评估点 |
| `track_from_report(report, method)` | 从 CalibrationReport 记录 |
| `trend_chart(metric)` | ECE/Brier/LL 趋势图 |
| `alert_check()` | 异常检测 (ECE超阈值 + 退化) |
| `save_history()/load_history()` | JSON 持久化 |

**告警机制**:
- **WARNING**: ECE > 0.08 (默认阈值)
- **CRITICAL**: 同方法相邻评估点 ECE 退化 > 0.02

### 3. CalibrationReportBuilder — HTML 评估报告

| 方法 | 说明 |
|------|------|
| `generate(suite, y_true, raw_probs)` | 从 CalibratorSuite 生成完整 HTML 报告 |
| `generate_from_data(y_true, raw_probs)` | 从原始数据一键生成 (含训练) |

**报告内容**:
- 核心指标卡片 (最优 ECE, Brier, 样本量)
- 告警状态 (ECE 良好/偏高/过高)
- 可靠性图 + 校准前后对比图
- 分类别可靠性图
- 方法对比详情表格
- 可靠性分箱数据
- 稀疏数据适应性图
- 图表以 base64 嵌入 HTML (自包含)

### 4. ExpertCalibrator 桥接

```python
from optimize.calibration_viz import visualize_expert_calibration
charts = visualize_expert_calibration('trend_analyzer')
# 自动: 收集数据 → CalibratorSuite 训练 → 生成图表 + HTML 报告
```

## 用法示例

### 基础可靠性图

```python
from optimize.calibration_viz import CalibrationVisualizer
viz = CalibrationVisualizer(output_dir='reports/calibration')
viz.reliability_diagram(y_true, raw_probs, calibrated_probs)
```

### 一键全图 + HTML 报告

```python
from optimize.calibration import CalibratorSuite
from optimize.calibration_viz import CalibrationVisualizer, CalibrationReportBuilder

suite = CalibratorSuite()
suite.fit(y_true, raw_probs)

# 图表
viz = CalibrationVisualizer()
charts = viz.generate_all_charts(y_true, raw_probs, suite)

# HTML 报告
builder = CalibrationReportBuilder()
html_path = builder.generate(suite, y_true, raw_probs)
```

### ECE 监控

```python
from optimize.calibration_viz import ECEMonitor

monitor = ECEMonitor(alert_threshold=0.08)
monitor.track(y_true, probs, method='isotonic', label='retrained')
monitor.trend_chart()
alerts = monitor.alert_check()
monitor.save_history()
```

## 54,200 场真实数据测试结果

| 指标 | 值 |
|------|-----|
| 数据量 | 54,200 场 Elo 隐含概率 |
| 图表生成 | 6 张 PNG 全部成功 |
| HTML 报告 | 自包含, 可直接浏览 |
| 分类 ECE | Home: +0.02, Draw: +0.09, Away: -0.09 |
| ECE 告警 | 2 个 (ECE > 0.08 的方法) |
| 稀疏数据图 | 7 样本量 × 4 方法 |

## 输出目录

```
reports/calibration/
├── reliability_*.png          # 可靠性图
├── before_after_*.png         # 校准前后对比
├── method_comparison_*.png    # 多方法对比
├── confidence_hist_*.png      # 置信度分布
├── class_reliability_*.png   # 分类别可靠性
├── sparse_data_*.png          # 稀疏数据适应性
├── ece_trend_*.png            # ECE 趋势图
├── ece_monitor_history.json   # ECE 监控历史
└── calibration_report_*.html  # 完整评估报告
```
