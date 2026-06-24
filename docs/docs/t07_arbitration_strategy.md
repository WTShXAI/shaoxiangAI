# T07 仲裁逻辑 — 不一致处理策略文档

## 1. 概述

当集成模型（XGB+Ridge+Heuristic）与专家投票系统产生分歧时，仲裁模块介入，综合多源信号输出最终决策信标。

**核心原则**：
- 无分歧不收 — 方向一致时不仲裁，直接使用集成模型结果
- 分级处置 — 按严重程度采用不同策略
- 可解释 — 每个决策都有完整溯源证据

---

## 2. 不一致场景分类 (5种)

| 类型 | 触发条件 | 含义 |
|------|---------|------|
| **DIRECTION** | 集成模型与专家投票预测方向不同 | 最核心的冲突，也是最常见的 |
| **CONFIDENCE_ASYMMETRY** | 双方置信度比值 ≥ 2:1 | 一方远超另一方，信号可靠度存疑 |
| **INTERNAL_SPLIT** | 专家内部一致性=split/low + 多数占比<50% | 专家内部无法形成共识 |
| **ODDS_CONTRARIAN** | 赔率隐含概率与两系统预测都偏差>15% | 市场与分析师观点背离 |
| **FULL_CONFLICT** | ≥2 种类型同时触发 | 多维度不一致，信号矛盾加剧 |

### 2.1 严重程度评级

```
                    ┌──────────┐
                    │   无冲突  │ → 跳过仲裁
                    └──────────┘
                          │
          ┌───────┬───────┼───────┬───────┐
          ▼       ▼               ▼       ▼
       LOW    MODERATE          HIGH   CRITICAL
      (无冲突) (单一冲突)    (多冲突/高置信差) (三杀)
```

| 严重程度 | 条件 | 示例 |
|---------|------|------|
| **LOW** | 方向一致，无其他触发 | 双系统都选"主胜" |
| **MODERATE** | 单一冲突类型 | 仅方向不一致，但双方置信都不高 |
| **HIGH** | 多冲突 OR 方向冲突+高置信 | 方向冲突 + 置信度≥0.7 |
| **CRITICAL** | 方向冲突 + 专家内部分裂 + 双方高置信 | 三杀：最危险的信号 |

---

## 3. 仲裁算法

### 3.1 加权投票仲裁 (WeightedVoteArbiter)

**适用场景**: MODERATE/HIGH 级别冲突（默认策略）

**投票源及权重**:

| 投票源 | 基础权重 | 说明 |
|--------|---------|------|
| 集成模型 | 35% | 主力模型，置信度越高权重越大 |
| 各专家 | 40% (合计) | 按ExpertSelect权重 + 个体置信度动态分配 |
| 赔率锚定 | 15% | 市场隐含概率作为基准 |
| 预留 | 10% | ELO/Poisson等额外信号 |

**公式**:
```
final_prob = Σ(voter_prob_i × weight_i) / Σ(weight_i)
arb_conf = agreement_pct × 0.5 + max_prob × 0.3 + avg_sys_conf × 0.2
```

### 3.2 元学习仲裁 (MetaLearningArbiter)

**适用场景**: HIGH/CRITICAL 级别冲突 + 有充足历史数据

**场景桶设计**:
- 3维特征离散化 → 全排列 4×5×4 = 80 个场景桶
  1. **sigma_trap** (赔率诱多/诱空): 4区间 [-inf, -0.3, 0, 0.3, +inf]
  2. **confidence_ratio** (双方置信比): 5区间 [-inf, 0.5, 0.8, 1.25, 2.0, +inf]
  3. **consensus_level** (专家一致性): 4区间 [split, low, moderate, high]

**学习机制**:
- 每个场景桶维护: `{ensemble_correct, expert_correct, total}`
- 新预测结果确认后：`record_outcome()` 更新统计
- 仲裁时：查找场景桶准确率 → 动态分配信任权重
- 样本不足(<5条) → 回退到先验权重 (55%/45%)

---

## 4. 仲裁流程

```
/api/predict 响应构建
    │
    ├─ confidence_comparison.tier == CONFLICT?
    │       │
    │       ├─ NO → 跳过仲裁 (直接返回集成模型结果)
    │       │
    │       └─ YES → ArbitrationEngine.arbitrate()
    │                   │
    │                   ├─ 1. InconsistencyRules.assess() → 冲突分类+严重度
    │                   │
    │                   ├─ 2. 选择策略:
    │                   │    ├─ LOW/MODERATE → WeightedVoteArbiter
    │                   │    ├─ HIGH/CRITICAL → MetaLearningArbiter
    │                   │    │    └─ 样本不足 → 回退 WeightedVote
    │                   │    └─ CRITICAL → 置信度 ×0.7
    │                   │
    │                   └─ 3. 生成 DecisionBeacon
    │                        ├─ signal: BUY/HOLD/PASS
    │                        ├─ strength: STRONG/NORMAL/WEAK/NONE
    │                        ├─ prediction: 仲裁后概率
    │                        └─ evidence: 完整决策依据
```

---

## 5. 决策信标 (DecisionBeacon)

| 信号 | 强度 | 含义 | 建议 |
|------|------|------|------|
| **BUY** | STRONG | 双重验证通过，高度可信 | 重点关注 |
| **HOLD** | NORMAL | 仲裁后方向明确 | 可参考，结合基本面 |
| **HOLD** | WEAK | 仲裁结果不理想 | 小仓位/观望 |
| **PASS** | NONE | 严重冲突，不可仲裁 | 暂停投注 |

---

## 6. API 响应新增字段

```json
{
  "arbitration": {
    "signal": "HOLD",
    "confidence": 0.638,
    "strength": "WEAK",
    "prediction": {"home": 0.432, "draw": 0.272, "away": 0.296},
    "predicted_outcome": "home",
    "predicted_outcome_cn": "主胜",
    "policy": "weighted_fusion",
    "policy_reason": "...",
    "evidence": {
      "conflict_assessment": {
        "type": "direction",
        "severity": "high",
        "rules": ["DIRECTION: 集成模型→客胜, 专家投票→主胜"],
        "arbitrable": true,
        "reason": "DIRECTION: ..."
      },
      "weighted_vote": {
        "total_voters": 8,
        "vote_agreement": 0.75,
        "mode_outcome": "home",
        "voter_details": [...]
      },
      "meta_learning": {
        "source": "prior",
        "reason": "场景桶样本不足",
        "ensemble_weight": 0.55,
        "expert_weight": 0.45
      }
    },
    "execution_time_ms": 0.5
  }
}
```

`arbitration` 仅在冲突发生时出现，无冲突时为 `null`。

---

## 7. 文件清单

| 文件 | 职责 |
|------|------|
| `optimize/arbitration.py` | 仲裁引擎完整实现 (~500行) |
| `api/prediction_service.py` | 主流程集成 (+45行) |
| `scripts/test_t07.py` | 端到端测试 (34项) |
| `docs/t07_arbitration_strategy.md` | 本文档 |
