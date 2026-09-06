# 哨响AI 专家团备份说明

## 备份内容
此目录保存 WorkBuddy 专家团（`shaoxiang-ai` 插件包）的完整配置备份。

## 当前状态 (2026-07-16)
专家包 `shaoxiang-ai`（plugin.json v5.2）：**34 个 agent 定义**（1 个 team-lead 入口 + 33 个 specialist），全部已落地。

| 功能集群 | Agent ID | 人数 | 状态 |
|----------|----------|------|------|
| 总工入口 | shaoxiang-ai-team-lead | 1 | ✅ 已注册入口 |
| 智囊/策略/产品 | ai-strategist, ai-mathematician, ai-game-theorist, ai-architect, ai-data-scientist, ai-pm | 6 | ✅ |
| 算法专家 | ai-algo-poisson, ai-algo-game, ai-algo-ensemble, ai-algo-temporal, ai-algo-math, ai-algo-draw | 6 | ✅ |
| 执行/工程 | ai-code-driver, ai-devops-lead | 2 | ✅ |
| 质检/合规 | ai-qa-reviewer, ai-qa-investigator, ai-qa-security, ai-qa-validator, ai-compliance | 5 | ✅ |
| 数据工程 | ai-data-lead, ai-data-collector, ai-data-cleaner, ai-data-pipeline | 4 | ✅ |
| 训练部署 | ai-train-trainer, ai-train-validator, ai-train-ops | 3 | ✅ |
| 记录员 | ai-recorder | 1 | ✅ |
| 设计原型 | ai-design-lead, ai-design-discovery, ai-design-system, ai-design-prototype, ai-design-critique, ai-design-export | 6 | ✅ |

> 产品版本：哨响AI v7.1（引擎标称 7.1.0）。完整架构见 `EXPERT_SYSTEM.md`。

## 恢复步骤
1. 确认 WorkBuddy 已启动
2. 使用 `expert-manager` skill 重建专家团（优先从 `plugindata/shaoxiang-ai/` 取配置）
3. plugin 原始文件在 `plugindata/shaoxiang-ai/` 子目录（含 `agents/`、`settings.json`、`.codebuddy-plugin/plugin.json`）

## 更新记录
- 2026-06-25: 初始创建（仅总工 + 记录员口径）
- 2026-07-16: 据磁盘实际配置同步为 34-agent 全量状态（v7.1 / shaoxiang-ai v5.2），修正入口 ID 与路径
