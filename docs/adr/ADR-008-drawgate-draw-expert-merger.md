# ADR-008: DrawGate + DrawExpert 合并决策

**状态**: ✅ 已实施 (v5.3)

**日期**: 2026-06-27

**决策者**: 算法团队

---

## 背景

DrawGate 作为平局检测的规则引擎，DrawExpert 作为基于 ML 的平局预测模型，两者独立运行但目标相同。独立运行导致资源浪费和可能的预测冲突。

## 决策

在 v5.3 中合并 DrawGate 和 DrawExpert，将 ML 模型 `draw_expert_v1.joblib` 的输出作为 DrawGate 引擎的输入信号之一。合并后 D-F1 指标突破 0.31。

## 选项对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **DrawGate + DrawExpert 合并** | 协同决策、D-F1 提升 | 耦合度增加 |
| 保持独立 | 关注点分离 | 结果冲突、资源浪费 |
| 仅使用 DrawExpert ML | 简单 | 缺少规则知识 |

## 合并方式

```
DrawExpert (ML) → probability → DrawGate Engine → DrawScore
Rule-based signals → DrawGate Engine → DrawScore
Final: Weighted ensemble of both → Final DrawScore
```

## 后果

**正面**:
- D-F1 从 ~0.28 提升到 0.31+
- 规则知识和 ML 模型互补
- 单一输出通道简化调用方逻辑

**负面**:
- 需要维护两个模块的同步更新
- 权重分配需要持续调优
