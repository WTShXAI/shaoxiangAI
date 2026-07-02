# D-Gate 融合管线对比回测计划 v2.0

> 赵统筹 + 毕建模 · 2026-07-02 · 回测驱动融合决策

---

## 一、背景

系统存在两套并行的 D-Gate 融合管线，设计哲学互悖：

| 维度 | Path A (prediction_service.py) | Path B (UnifiedPredictor) |
|------|-------------------------------|--------------------------|
| **核心逻辑** | D通道外科替换 | DrawExpert 加法 boost |
| **公式** | D_final = D_meta×(1-gate) + D_spec×gate | probs[1] += draw_boost |
| **Gate机制** | spread驱动(0.05-0.65) + OE-Heuristic一致性调制 | drawgate_v53动态阈值 |
| **信号源** | Heuristic(0.40) + OE(0.30) + DrawExpert(0.30) | DrawExpert(0.45衰减) + drawgate_v53 |
| **H/A处理** | 保持meta-learner原始比例 | 受confidence_mult降权 |

**根本差异**: Path A "换D留HA"，Path B "抬D压强队"。窄spread比赛下，两者可能输出截然不同的概率分布。

---

## 二、对比指标（5维）

| # | 指标 | 计算 | 关键性 |
|---|------|------|--------|
| 1 | **整体Acc** | (TP+TN)/Total, argmax判型 | 基础 |
| 2 | **D-F1** | 2×P×R/(P+R), 正类=平局 | 👈 核心 |
| 3 | **MacroF1** | 三类F1均值 | 平衡 |
| 4 | **窄spread子集D-F1** | proba_spread<0.15场次 | 👈 关键(淘汰赛高发) |
| 5 | **校准度ECE** | 分10桶, |acc-conf| | 可信度 |

---

## 三、对比设计（两层）

### 对比A：全管线对比（70场）
> Path A 完整 (D-Gate Fusion) vs Path B 完整 (DrawGate v5.3 + λFusion)
> 回答：**整个融合管线谁更好**

### 对比B：DrawGate 净效应（消融实验）
> Path B-with-DrawGate vs Path B-without-DrawGate (同UnifiedPredictor，仅开关[L4])
> 回答：**DrawGate v5.3 的增量贡献**（排除 λFusion 干扰）

**关键隔离**：Path B含λFusion([L2])，而Path A不含。对比A的Δ可能部分来自λFusion而非DrawGate。对比B可分离DrawGate纯增量。

| 检验 | 用途 | 阈值 |
|------|------|------|
| **McNemar** | 两路径MacroF1差异是否显著 | p<0.05 → 显著 |
| **Wilcoxon** | 两路径D-F1逐场配对差异 | p<0.05 → 显著 |
| **Cohen's d** | 效应量（差异多大） | \|d\|>0.2→小, >0.5→中, >0.8→大 |

---

## 四、控制变量

| 共享输入 | 两端必须一致 |
|----------|-------------|
| 1X2赔率 | 从wc2026_72matches_with_odds.json读取 |
| 让球(hcp) | 从wc2026_ocr_full.json读取 |
| 大小球(OU) | 从wc2026_ocr_full.json读取 |
| 基础模型 | 两边都用 EnsembleTrainer v4.1 (saved_models/football_v4.1_production.joblib) |
| 基础proba | 两边都从 model_bridge._last_submodel_probas 取 LGB+XGB+NN 的原始三分类输出 |

**变量隔离**: 两边**只开启 D-Gate 融合层**，关闭：
- 陷阱检测 (BookmakerTrapDetector)
- 收割防护 (HarvestingGuard)  
- 贝叶斯先验 (BookmakerBayesInfer)
- JEPA 融合 (JEPA Blend Weight=0)
- D-specialist gate 的冷启动调制 (is_cold_start=False)

---

## 五、数据集

**70场有效样本**（2场缺赔率剔除：墨西哥vs南非、韩国vs捷克）
目标分布：H=34(47.2%) D=20(27.8%) A=18(25.0%) — D比例高于俱乐部联赛，对D-F1测试有利

**缺失字段填充**：
| 字段 | 策略 |
|------|------|
| 让球(handicap) | 从赔率估算：`hcp ≈ 0.85 × (odds_h-odds_a)/(odds_h+odds_a) × 2` |
| 大小球(ou_line) | 默认2.5（世界杯标准） |
| 阶段(stage) | DB league_name含"小组赛"→group，否则→knockout |

**模型对齐验证**：任选3场，确认两路径**不启用D-Gate**时输出相同概率(tol=1e-6)。

---

## 六、验收标准（毕建模决策矩阵 v2.0）

| 判定 | 条件 |
|------|------|
| **Path B 显著胜出** | D-F1 Δ≥+5pp + Acc Δ≥-2pp + McNemar p<0.01(Bonferroni) + MacroF1不显著下降 + ECE不退化>0.03 |
| **持平** | D-F1 Δ∈(-2pp,+5pp) 且 Acc Δ∈(-2pp,+2pp), 全部不显著 → 保留两套并行，等淘汰赛全结束(N~104场)再判 |
| **Path A 反超** | D-F1 Δ≤-5pp 且 Acc Δ≤-2pp → 回退到Path A，Path B需重新调参 |
| **灰色地带** | 不满足以上任一 → 输出完整分层表 + 按DrawGate模式分拆定位问题子集 + 建议调参方向 |

**Bonferroni校正**：5次检验(Acc/D-F1/MacroF1/ECE/Brier) → α=0.05/5=**0.01**

---

## 七、输出格式

```
═══════════════════════════════════════════════
  D-Gate 融合管线对比回测 — WC2026 70场
═══════════════════════════════════════════════

指标               Path A          Path B          Δ(B-A)    p-value   判定
─────────────────────────────────────────────────────────────
Acc (全量)        0.XXX           0.XXX          +0.0XX    0.XXX     B/持平
D-F1 (全量)       0.XXX           0.XXX          +0.0XX    0.XXX     B/持平
MacroF1 (全量)    0.XXX           0.XXX          +0.0XX    0.XXX     B/持平
D-F1 (窄spread)   0.XXX           0.XXX          +0.0XX    0.XXX     B/持平
ECE (全量)        0.XXX           0.XXX          -0.0XX      —       B/持平

─────────────────────────────────────────────────────────────
窄spread子集 (~25场):
  Path A D-F1: 0.XXX  (命中 X/25, 召回 X/Y)
  Path B D-F1: 0.XXX  (命中 X/25, 召回 X/Y)

淘汰赛子集 (~18场):
  Path A D-F1: 0.XXX
  Path B D-F1: 0.XXX

⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘
⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘⫘

推荐: [Path A / Path B / 持平 / 混合]
理由: ...

下一步: [选项1(消重复) / 选项2(合并) / 选项3(三合一)]
```

---

## 八、执行流程

```
Step 1: 数据准备 (验证师, 30min)
  ├─ 从 wc2026_72matches_with_odds.json 加载72场比赛
  ├─ 从 wc2026_ocr_full.json 加载让球+大小球
  └─ 验证所有必需字段齐全

Step 2: 路径隔离 (钱代驾, 1h)
  ├─ 编写 wrapper: PathA.predict(match) → D-Gate融合后的三分类概率
  │   └─ 仅开启 D-specialist gate, 关闭所有其他增强
  ├─ 编写 wrapper: PathB.predict(match) → DrawGate融合后的三分类概率
  │   └─ 仅开启 DrawGate v5.3, 关闭陷阱/收割/贝叶斯/JEPA
  └─ 验证两边输入完全相同

Step 3: 批量回测 (验证师, 30min)
  ├─ 循环72场, 每场同时喂给两个wrapper
  ├─ 记录: match_id, true_label, probaA[H/D/A], probaB[H/D/A], spread
  └─ 输出 JSON: backtest_results.json

Step 4: 指标计算 (毕建模, 1h)
  ├─ sklearn: accuracy_score, f1_score(D), classification_report
  ├─ 窄spread子集提取 + 分类计算
  ├─ 淘汰赛子集提取 + 分类计算
  ├─ ECE: calibration_curve + 10-bin
  └─ McNemar: statsmodels contingency_table

Step 5: 专家评议 (何执策+杜博弈, 30min)
  ├─ 解读对比结果
  ├─ 判断谁胜出
  └─ 输出：融合方向推荐 + 下一步行动计划

总计: ~3.5h
```

---

## 九、代码骨架

```python
# backtest_dg_fusion.py
import json, numpy as np
from sklearn.metrics import accuracy_score, f1_score

# Step 1: 加载数据
matches = json.load(open('data/wc2026_72matches_with_odds.json'))

# Step 2: Wrapper
def path_a_predict(match):
    """prediction_service.py 的 D-specialist gate (仅D-Gate部分)"""
    # ... 从原始proba开始, 执行 D-specialist gate 逻辑
    # d_spec = 0.40*d_heur + 0.30*d_oe + 0.30*de_pdraw
    # d_final = d_prob*(1-d_gate) + d_spec*d_gate
    return h_final, d_final, a_final

def path_b_predict(match):
    """UnifiedPredictor 的 DrawGate v5.3 (仅DrawGate部分)"""
    # ... 从原始proba开始, 执行 DrawGate 逻辑
    # draw_boost = dg['draw_boost']
    # final_probs[1] += draw_boost
    return final_probs

# Step 3: 批量回测
results = []
for m in matches:
    true_label = 1 if m['hs'] == m['aws'] else (0 if m['hs'] > m['aws'] else 2)
    pa = path_a_predict(m)
    pb = path_b_predict(m)
    results.append({
        'match': f"{m['home']}vs{m['away']}",
        'true': true_label,
        'pa': pa, 'pb': pb,
        'spread': abs(1/m['1x2_home'] - 1/m['1x2_away'])  # 隐含概率spread
    })

# Step 4: 指标
y_true = [r['true'] for r in results]
y_pred_a = [np.argmax(r['pa']) for r in results]
y_pred_b = [np.argmax(r['pb']) for r in results]

print(f"Path A Acc: {accuracy_score(y_true, y_pred_a):.3f}")
print(f"Path B Acc: {accuracy_score(y_true, y_pred_b):.3f}")
print(f"Path A D-F1: {f1_score(y_true, y_pred_a, labels=[1], average='macro'):.3f}")
print(f"Path B D-F1: {f1_score(y_true, y_pred_b, labels=[1], average='macro'):.3f}")
```

---

## 十、风险与回退

| 风险 | 缓解 |
|------|------|
| EnsembleTrainer 模型文件不可用 | 回退到 DB 中已有的 proba 字段 |
| HeuristicPredictor 不可用(Path A依赖) | 跳过，仅对比 OE+DrawExpert 部分 |
| drawgate_v53 有 module import 问题 | 使用 draw_expert.py 桥接(已修复) |
| 两边输出完全一致(实际上等价) | → 直接判"持平"，选选项1消重复 |