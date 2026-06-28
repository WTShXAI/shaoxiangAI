# ADR-005: LAMF 多智能体工作流设计

**状态**: ✅ 已实施

**日期**: 2026-06-20

**决策者**: 架构团队

---

## 背景

预测系统需要处理复杂的足球赛事分析，涉及数据获取、特征计算、概率评估和结果解释等多个环节。单一模型难以同时满足所有需求。

## 决策

采用 LAMF (Local AI Model Framework) 多智能体架构，基于 LangGraph StateGraph 编排四个专用 LLM Agent：
- Commander (gemma4:12b): 意图理解、任务路由、结果汇总
- DataAgent (deepseek-r1:8b): 数据获取、特征计算
- MathAgent (phi4:14b): 概率计算、三层降级
- Explainer (qwen3:8b): 中文解释、文案润色

## 选项对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **LAMF 多智能体** | 专业化分工、可扩展 Agent、支持降级 | 依赖本地 Ollama、硬件要求高 |
| 单一 LLM | 部署简单 | 上下文混淆、无法专注 |
| 纯 ML 无 LLM | 确定性高 | 缺少解释能力和灵活性 |

## 后果

**正面**:
- 每个 Agent 专注特定领域，输出质量高
- 支持条件路由和降级 (SimpleWorkflow)
- 可扩展新 Agent 类型

**负面**:
- 需要 Ollama 运行 4 个本地模型（~40GB VRAM）
- Agent 间通信增加延迟
- 依赖本地 GPU 资源
