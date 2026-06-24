# A窗口 → B窗口：多模型对比预测指令

> **来源**: A窗口 (训练侧)  
> **接收**: B窗口 (预测侧)  
> **时间**: 2026-06-17 00:32  
> **优先级**: P0 — 等待B窗口执行后回传诊断报告

---

## 一、任务目标

用 **全部可用模型** 对以下 4 场比赛进行盲测预测，生成详细对比报告，回传 A 窗口用于诊断调参。

**⚠️ 禁止事项**: 
- **严禁查询/获取这 4 场比赛的实际结果**
- **严禁使用网络**（WebFetch/WebSearch/curl/API 调用外部数据源）
- **只允许**: 本地模型推理、本地 SQLite、本地文件读写

---

## 二、比赛数据（从赔率截图提取）

### Match #1: 澳大利亚 vs 土耳其
```
日期: 2025-06-14 12:00 (GMT+8)
全场独赢: H=4.95  D=3.75  A=1.71
全场让球: +0.5/1 @1.93  /  -0.5/1 @1.99
全场大小: 大2.5 @2.08  /  小2.5 @1.82
半场独赢: H=5.10  D=2.21  A=2.47
半场让球: +0/0.5 @1.98  /  -0/0.5 @1.92
半场大小: 大1 @1.98  /  小1 @1.92
```

### Match #2: 巴西 vs 摩洛哥
```
日期: 2025-06-14 06:00 (GMT+8)
全场独赢: H=1.70  D=3.60  A=5.30
全场让球: -0.5/1 @1.91  /  +0.5/1 @2.01
全场大小: 大2/2.5 @1.99  /  小2/2.5 @1.91
半场独赢: H=2.29  D=2.16  A=6.40
半场让球: -0/0.5 @1.81  /  +0/0.5 @2.09
半场大小: 大1 @2.12  /  小1 @1.79
```

### Match #3: 海地 vs 苏格兰
```
日期: 2025-06-14 09:00 (GMT+8)
全场独赢: H=5.90  D=4.60  A=1.49
全场让球: +1 @2.08  /  -1 @1.84
全场大小: 大2.5/3 @1.83  /  小2.5/3 @2.07
半场独赢: H=6.40  D=2.47  A=2.03
半场让球: +0.5 @1.87  /  -0.5 @2.03
半场大小: 大1/1.5 @2.12  /  小1/1.5 @1.79
```

### Match #4: 卡塔尔 vs 瑞士
```
日期: 2025-06-14 03:00 (GMT+8)
全场独赢: H=13.0  D=6.70  A=1.21
全场让球: +2 @1.85  /  -2 @2.07
全场大小: 大3 @1.99  /  小3 @1.91
半场独赢: H=13.5  D=3.05  A=1.53
半场让球: +1 @1.77  /  -1 @2.14
半场大小: 大1/1.5 @1.97  /  小1/1.5 @1.93
```

---

## 三、参与对比的模型清单

| 编号 | 版本别名 | 文件路径 | 特征维度 | 训练方式 | 备注 |
|------|---------|----------|----------|----------|------|
| M1 | **V3.2 生产** | `saved_models/football_balanced_production.joblib` | 72维 | 时间切分 OOF | 当前线上模型，6子模型+Stacking |
| M2 | **V4-Early** | `saved_models/football_ht_parquet_v4_20260616_220044.joblib` | 39维 | parquet 首训 | 数据路径修复前版本 |
| M3 | **V4-Mid** | `saved_models/football_ht_parquet_v4_20260617_000844.joblib` | 39维 | parquet 重训 | NN CUDA 崩溃版 |
| M4 | **V5-Fix** | `saved_models/football_ht_parquet_v4_20260617_001554.joblib` | 39维 | parquet 重训 | Stacking权重修复 + NN CPU降级 |

---

## 四、输出要求（必须包含以下内容）

### 4.1 每场比赛 × 每个模型的完整输出

对每场比赛、每个模型，报告：

```json
{
  "match": "澳大利亚vs土耳其",
  "model": "V3.2生产",
  "probabilities": {"H": 0.xxx, "D": 0.xxx, "A": 0.xxx},
  "prediction": "H|D|A",
  "confidence": 0.xxx,
  "prediction_mode": "fusion|model_only|odds_override|degraded",
  "degradation_level": 0,
  "sub_model_outputs": {
    "LightGBM": {"H": x, "D": x, "A": x},
    "XGBoost": {"H": x, "D": x, "A": x},
    "NeuralNet": {"H": x, "D": x, "A": x},
    "Heuristic": {"H": x, "D": x, "A": x},
    "OddsExpert": {"H": x, "D": x, "A": x}
  },
  "stacking_output": {"H": x, "D": x, "A": x},  // 仅V4/V5
  "odds_implied": {"H": x, "D": x, "A": x},
  "feature_vector_sample": "top-5 highest features with values"
}
```

### 4.2 模型间一致性分析

- 4 个模型在每场比赛上的 **预测方向是否一致**？
- 如果不一致，**分歧点在哪里**？（哪个子模型导致分歧？）
- **概率值差异幅度**（最大/最小 H/D/A 的范围）

### 4.3 异常检测

- 是否有某个模型的输出明显异常（如 D 概率 < 1% 或 > 80%）？
- degradation_level 是否 > 0？（降级触发原因）
- prediction_mode 分布（多少走 fusion / 多少走 odds_override / 多少 degraded）？

### 4.4 赔率隐含概率 vs 模型输出对比

计算每场比赛的 `odds_implied = 1/odds / sum(1/odds)`，与每个模型输出做对比：
- 模型是否比赔率更"自信"或更"保守"？
- 模型是否在某个方向上显著偏离赔率？

### 4.5 关键诊断问题（必须回答）

请逐一回答以下问题并附数据支撑：

1. **D 类别（平局）问题**：各模型给出的平局概率是多少？是否存在系统性偏低（<5%）或偏高（>50%）？
2. **盘口极端化效应**：卡塔尔vs瑞士（主13.0/客1.21，spread 极大），各模型是否退化为纯 odds 复制？override 阈值是否被触发？
3. **模型差异化程度**：M1/M2/M3/M4 的预测是否有实质性差异？如果几乎完全一样，说明什么？
4. **最值得关注的比赛**：哪场比赛的模型输出最"反直觉"（即模型高置信但赔率不支持的预测）？
5. **V5 修复效果验证**：M4(V5) 相比 M3(V4-Mid)，D 概率是否确实提升？Stacking Acc/F1 权衡是否体现？

---

## 五、技术注意事项

### V3.2 模型的预测路径
- **正确入口**: `prediction_service.py` → `predict_single()` → `ModelBridge.predict()` → `EnsembleTrainer.ensemble_predict_proba()`
- ⚠️ **不要用** `predict_single_v3()`（没人调）
- V3.2 有 72 维特征，需通过 `_prepare_features()` 构建
- V3.2 sub_models 键存的是 bool 标志，真实模型体在独立键：`xgb_model`, `lgb_model`, `odds_expert_model`, `meta_learner`
- HeuristicPredictor 是外部注入的，需要单独调用 `HeuristicPredictor.predict_proba()`

### V4/V5 模型的预测路径
- 39 维赔率衍生特征集（不同特征名！）
- 需要手动构建 meta_features：
  ```
  meta_features = [lgb_proba(3), xgb_proba(3), oe_proba(3),
                   heuristic_proba(3), nn_proba(3)] + aux_cols
  ```
- `oe`(OddsExpert) 子模型用独立的 17 维特征子集 + `oe_scaler`
- NeuralNet 用 CPU 推理（CUDA 不兼容 RTX 5070Ti）
- model_order 定义了 meta_features 中基模型概率的排列顺序
- aux_cols 包含：spread, kelly_home, kelly_draw, kelly_away, volatility, draw_odds_attract 等

### 特征构建参考
- V3.2 特征名列表: `model['feature_names']` (72个)
- V4/V5 特征名列表: `model['feature_names']` (39个)
- V4/V5 OE 子集: `model['oe_feature_names']` (17个)

---

## 六、报告格式

输出到: `output/bwindow_prediction_report_YYYYMMDD_HHmmss.json`

同时生成可读的 Markdown 摘要到: `output/bwindow_prediction_report.md`

完成后将 **Markdown 摘要的关键发现** 直接回复给 A 窗口。
