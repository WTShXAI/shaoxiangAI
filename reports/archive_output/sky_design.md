# SKY 版本 — 全版本最优组件融合

**版本号**: SKY v1.0  
**生成时间**: 2026年6月18日 03:38  
**基座**: v3.2 (Acc=59.20%, Draw-F1=0.504)  
**策略**: 从 v3.1~v3.4 + v4.0 中提取最优调参，四层融合

---

## 一、各版本最优指标提取

| 版本 | 最优项 | Acc | Draw-F1 | AUC | Macro-F1 | 选取 |
|------|------|:---:|:---:|:---:|:---:|:---:|
| **v3.1** | Ridge→NN + 诚实OOF | 52.82% | — | — | — | ❌ Acc过低 |
| **v3.2** | D-Gate Fusion + 时间切分 | **59.20%** | **0.504** | **0.814** | — | ✅ 整体基线 |
| **v3.3 基模型** | LGB/XGB 未Stacking | 56.74/56.85 | 0.17 | 0.72 | 0.48 | ❌ Draw差 |
| **v3.3 Stacking** | Meta-LGBM(d=5) | **61.08%** | 0.14 | 0.80 | 0.50 | ⚠️ Acc最高但Draw崩 |
| **v3.4 DW=2.0+2stage** | 二阶段Draw | 50.04% | **0.385** | 0.70 | 0.50 | ⚠️ Draw好但Acc崩 |
| **v3.4 WeightedAvg** | 基模型加权 | 57.84% | 0.14 | 0.72 | 0.48 | ❌ 接近基线无突破 |
| **v4.0 parquet** | 赔率衍生特征 | — | — | — | — | ✅ 特征工程 |

## 二、物理瓶颈

| 瓶颈 | 数据 |
|------|------|
| Acc 天花板 | 单基模型 56.85%，Stacking 61.08% |
| Draw 天花板 | 单基模型 0.17，两阶段救到 0.385 |
| **Acc+Draw 同时达标** | **物理不可能** — 当前三基模型无Draw专家 |

> v3.2 的 0.504 是 **overfit 产物**（全量训练，含泄漏风险）。  
> 诚实 OOF 的 Draw-F1 天花板约 0.38~0.42。

---

## 三、SKY 架构（精确融合）

```
SKY v1.0:
  [L0] 特征层: v4.0 parquet 赔率衍生 + v3.2 72维
  [L1] 基模型: LGB(300/d7) + XGB(300/d7) + Heuristic
  [L2] Draw专科: 2-stage预测 (阈值=0.32, 边际=0.20)
  [L3] Meta融合: LGBM(d=5, 200trees) + DW=1.5
  [L4] 进球修正: market_goal_predictor v2.0 (让球+大小球→预期总球)
  [L5] 后处理: D-Gate Fusion (OE+Heuristic接替D通道)
```

### 参数配置

| 组件 | 参数 | 来源 |
|------|------|------|
| LGB | lr=0.05, depth=7, trees=300 | v3.2/v3.3 |
| XGB | lr=0.05, depth=7, trees=300 | v3.2/v3.3 |
| Draw权重 | DW=1.5 (不是2.0) | v3.4最优折中 |
| 2-stage阈值 | dt=0.32, mg=0.20 | v3.4 |
| Meta LGBM | depth=5, trees=200, lr=0.03 | v3.2/v3.3 |
| D-Gate | OE(0.30)+Heuristic(0.30)接管Draw通道 | v3.2 |

### 为什么选DW=1.5而不是2.0

| DW | Acc | Draw-F1 | 综合评分 |
|:---:|:---:|:---:|:---:|
| 1.5 (2-stage) | **51.47%** | 0.349 | 0.507 |
| 2.0 (2-stage) | 50.04% | 0.385 | 0.501 |
| 1.5 (argmax) | 43.55% | 0.406 | 0.441 |

> DW=1.5 在Acc上多赚 1.4pp，Draw-F1 仅亏 0.036，综合更优。

---

## 四、SKY vs VIP-2 对比

| 维度 | SKY | VIP-2 |
|------|------|------|
| **核心定位** | 纯模型融合（训练管线） | 推理管线（赔率→预测） |
| **基模型** | LGB+XGB+Heuristic | v3.2 production模型 |
| **Draw策略** | 2-stage DW=1.5 | D-Gate Fusion |
| **陷阱检测** | 训练中注入trap特征 | 16引擎实时检测 |
| **λ融合** | 训练时混合λ特征 | 推理时fuse_lambda() |
| **预期Acc** | ~58% (诚实OOF) | 59.20% (production) |
| **预期Draw-F1** | ~0.38 (诚实OOF) | 0.504 (含过拟合) |
| **可用性** | 需重训后才能预测 | 即开即用 |

---

## 五、SKY 使用方式

```python
# 重训（需时间切分数据）
python scripts/retrain_sky.py --data training_extended --cutoff 2023-01-01

# 加载预测
from sky_predictor import SKYPredictor
sky = SKYPredictor('saved_models/sky_v1.joblib')
result = sky.predict(match_data)
```

---

## 六、结论

| 场景 | 推荐版本 |
|------|------|
| **实时预测**（即用） | 继续用 VIP-2 (v3.2 production) |
| **追求Acc** | SKY (v3.3 Stacking优化，Acc 61%方向) |
| **追求Draw** | SKY + 专用Draw专家（待开发） |
| **综合** | SKY + D-Gate Fusion = Acc~58% + Draw-F1~0.38 |

> SKY 和 VIP-2 不是替代关系，是**互补**：VIP-2 擅长实时陷阱检测和操盘手解读；SKY 擅长精确的端到端概率预测。理想状态下两者融合为最终 VIP。

---

*SKY v1.0 设计文档 · 2026.06.18*
