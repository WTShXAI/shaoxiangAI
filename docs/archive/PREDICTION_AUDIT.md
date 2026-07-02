# 哨响AI 预测审计说明 (Prediction Audit)

## 概述

哨响AI v2.0 引入了完整的预测审计机制，每次模型预测都会自动生成审计快照，确保预测结果可追溯、可验证。

## 审计日志格式

### 快照位置
```
logs/predictions/prediction_YYYYMMDD_HHMMSS_<hash6>.json
```

### 快照结构
```json
{
  "prediction": {
    "home": 0.4512,
    "draw": 0.2817,
    "away": 0.2671,
    "_model": "football_v4.1_production.joblib",
    "_version": "3.0",
    "_timestamp": "2026-06-11T10:30:00+00:00",
    "_feature_count": 93
  },
  "match_data_keys": ["home_team_name", "away_team_name", "league", ...],
  "match_data_sample": {
    "home_team_name": "Arsenal",
    "away_team_name": "Chelsea",
    "league": "Premier League"
  }
}
```

## 审计字段含义

| 字段 | 类型 | 含义 |
|------|------|------|
| `_model` | string | 使用的模型文件名，固定为 `football_v4.1_production.joblib` |
| `_version` | string | 模型版本号，如 `3.0` 或 `production` |
| `_timestamp` | string | ISO8601 格式的预测时间戳（UTC） |
| `_feature_count` | int | 模型要求的输入特征数量 |
| `home` | float | 主队胜概率 (0-1) |
| `draw` | float | 平局概率 (0-1) |
| `away` | float | 客队胜概率 (0-1) |

## 如何追溯一次预测

### 5 步复盘流程

```
1️⃣ 找到快照文件
   → 进入 logs/predictions/ 目录
   → 按文件名中的日期时间定位目标预测

2️⃣ 查看预测结果
   → 打开 .json 文件
   → 检查 prediction.home/draw/away 的概率值

3️⃣ 验证模型来源
   → 确认 _model 字段为 "football_v4.1_production.joblib"
   → 确认 _version 字段与模型版本一致
   → 如果 _model 为空或不匹配，说明预测可能有问题

4️⃣ 检查时间一致性
   → 确认 _timestamp 是合理的预测时间
   → 如果时间异常（如未来时间或过于久远），可能存在异常

5️⃣ 检查概率合理性
   → 三个概率之和应等于 1.0（±0.001）
   → 任何概率不应为 0 或 1（完全确定）
   → 如果 H≈0.40/D≈0.28/A≈0.32，说明是硬编码值（系统应自动检测并抛异常）
```

## 异常检测规则

### 🚫 硬编码概率检测
系统会自动检测以下硬编码概率模式：
- H = 0.40 ± 0.02
- D = 0.28 ± 0.02
- A = 0.32 ± 0.02

如果检测到 ≥2 个概率匹配上述模式，系统抛出 `HardcodedProbabilityError` 异常并终止预测。

**背景**: v1.0 版本中曾发现 math_agent.py 的 fallback 硬编码值为 H=0.40/D=0.28/A=0.32，导致所有预测结果相同。v2.0 已修复此问题并加入自动检测。

### 🚫 模型缺失检测
- 如果 `football_v4.1_production.joblib` 不存在，系统抛出 `ModelNotAvailableError` 并拒绝启动
- 不允许自动回退到其他模型文件

## 特征校验

### config/feature_schema.json
系统在 `config/feature_schema.json` 中定义了完整的特征校验 schema：
- 每个特征的名称、类型、取值范围、默认值
- 预测前可根据此 schema 进行特征完整性校验

### 校验规则
1. 缺失特征 → 使用默认值填充（记录到快照的 `features_default_filled` 字段）
2. 特征值超出范围 → 使用默认值替换
3. 特征类型异常 → 使用默认值替换

## 数据保留策略

| 数据类型 | 保留期限 | 轮转策略 |
|---------|---------|---------|
| 预测快照 | 90 天 | 超过 90 天的快照自动清理 |
| 预测日志 | 30 天 | 归档到 Archive/ 目录 |
| 审计报告 | 永久 | 不自动清理 |

## 给非技术人员的说明

### 怎么看预测是不是"真的"？

1. **看 `_model` 字段**：如果是 `football_v4.1_production.joblib`，说明是真实模型输出的
2. **看概率值**：如果三个概率都是"整整齐齐"的 0.40/0.28/0.32，那大概率是假的（系统会自动检测）
3. **看 `_timestamp`**：检查预测时间是否合理
4. **看快照文件**：每次预测都会在 `logs/predictions/` 里留一个 JSON 文件，可以事后查阅

### 预测结果怎么理解？

- `home: 0.45` → 主队赢的概率约 45%
- `draw: 0.28` → 平局的概率约 28%
- `away: 0.27` → 客队赢的概率约 27%
- 概率最高的那个就是模型认为最可能的结果

---

> 本文档由工程保障团队 AI 协作生成，最后更新: 2026-06-11
