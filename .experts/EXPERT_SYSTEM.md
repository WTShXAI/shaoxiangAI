# 哨响AI 全专家团体系（v7.1）

> **状态说明**：本文档原版标注 `v5.2.14` 且大量团队标记为「⏳ 待创建」，与磁盘实际配置严重脱节。已于 2026-07-16 据 `.experts/backup/plugindata/shaoxiang-ai/` 实际配置核对并重写。凡与本文冲突的旧笔记，以本文为准。

## 版本与范围
- **产品版本**：哨响AI v7.1（预测引擎 `pipeline/engine.py` 标称 `7.1.0`；架构真相见 `D:\Architecture\ARCHITECTURE.md`）。
- **专家包**：`shaoxiang-ai` 插件包（plugin.json 自述 v5.2，含 **34 个 agent 定义**：1 个 team-lead 入口 + 33 个 specialist）。
- **唯一对话入口**：`shaoxiang-ai-team-lead`（由 `settings.json` 注册）。

## 架构总览

```
                     ┌─────────────────────────────┐
                     │   shaoxiang-ai-team-lead     │  单一入口 (Agent)
                     │   意图分析 / 自动路由 / 总工  │
                     └──────────────┬──────────────┘
                                    │ 路由到 33 个 specialist
        ┌──────────┬──────────┬─────┼─────┬──────────┬──────────┬──────────┐
        ▼          ▼          ▼     ▼     ▼          ▼          ▼          ▼
   智囊/策略   算法专家   执行工程  质检合规  数据工程  训练部署   设计原型   记录员
   (6)        (6)       (2)      (5)      (4)       (3)       (6)       (1)
```

## 专家清单（实际 34 人，均定义于 `backup/plugindata/shaoxiang-ai/agents/`）

| # | 功能集群 | Agent ID | 人数 | 状态 |
|---|----------|----------|------|------|
| 0 | **总工入口** | shaoxiang-ai-team-lead | 1 | ✅ 已注册入口 |
| 1 | **智囊/策略/产品** | ai-strategist, ai-mathematician, ai-game-theorist, ai-architect, ai-data-scientist, ai-pm | 6 | ✅ 已定义 |
| 2 | **算法专家** | ai-algo-poisson, ai-algo-game, ai-algo-ensemble, ai-algo-temporal, ai-algo-math, ai-algo-draw | 6 | ✅ 已定义 |
| 3 | **执行/工程** | ai-code-driver, ai-devops-lead | 2 | ✅ 已定义 |
| 4 | **质检/合规 (gstack)** | ai-qa-reviewer, ai-qa-investigator, ai-qa-security, ai-qa-validator, ai-compliance | 5 | ✅ 已定义 |
| 5 | **数据工程** | ai-data-lead, ai-data-collector, ai-data-cleaner, ai-data-pipeline | 4 | ✅ 已定义 |
| 6 | **训练部署** | ai-train-trainer, ai-train-validator, ai-train-ops | 3 | ✅ 已定义 |
| 7 | **记录员** | ai-recorder | 1 | ✅ 已定义 |
| 8 | **设计原型** | ai-design-lead, ai-design-discovery, ai-design-system, ai-design-prototype, ai-design-critique, ai-design-export | 6 | ✅ 已定义 |

**总计：1 个团队入口（team-lead）+ 33 个 specialist = 34 个 agent 定义。**

## 三层闭环（概念保持）

```
回测发现 Bug → 智囊/策略分析 → 产出方案(P0-P3)
    ↓
总工(team-lead)拆解任务 → 路由到对应专家集群执行
    ↓
质检集群验收 → GO/NO-GO → 记录员归档
    ↓
回测验证 → 新问题 → 回到智囊
```

## 各集群职责

### 0. 总工入口 (shaoxiang-ai-team-lead)
- 类型: Agent（已注册为唯一对话入口，替代旧文档的 `shaoxiang-chief`）
- 职责: 意图分析、自动路由、任务拆解、总协调。

### 1. 智囊/策略/产品
- ai-strategist（首席策略官）、ai-mathematician（数学建模）、ai-game-theorist（博弈分析）、ai-architect（系统架构）、ai-data-scientist（数据科学）、ai-pm（产品官）。
- 触发: 回测发现问题时激活，产出方案。

### 2. 算法专家
- ai-algo-poisson（JEPA/Poisson）、ai-algo-game（博弈）、ai-algo-ensemble（集成）、ai-algo-temporal（时序）、ai-algo-math（数学）、ai-algo-draw（平局）。
- 触发: 预测/分析/模型/赔率/D-Gate 相关。

### 3. 执行/工程
- ai-code-driver（编码执行）、ai-devops-lead（运维部署）。

### 4. 质检/合规 (gstack)
- ai-qa-reviewer（QA 门神）、ai-qa-investigator（根因排障）、ai-qa-security（安全卫士 OWASP）、ai-qa-validator（验收）、ai-compliance（合规）。
- 触发: 上线检查 / 代码审查 / 安全审计。

### 5. 数据工程
- ai-data-lead（采集主理）、ai-data-collector（OCR/采集）、ai-data-cleaner（清洗）、ai-data-pipeline（管道）。
- 触发: 数据采集 / OCR / 质量检查。

### 6. 训练部署
- ai-train-trainer（训练师）、ai-train-validator（验证师）、ai-train-ops（运维）。
- 触发: 训练 / 回测 / 部署。

### 7. 记录员 (ai-recorder)
- 全团记忆库，归档到 `deliverables/`。（注：旧文档称 `shaoxiang-recorder`，实际 agent ID 为 `ai-recorder`。）

### 8. 设计原型
- ai-design-lead + discovery / system / prototype / critique / export（6 人标准 SOP）。
- 注: 旧文档称「系统内置 DesignEngineTeam」，实际 6 个 design agent 已包含在 `shaoxiang-ai` 插件包内（非系统内置）。

## 文件位置

```
专家包配置(权威备份):  D:\Architecture\.experts\backup\plugindata\shaoxiang-ai\
  ├─ .codebuddy-plugin/plugin.json   (34 个 agents 注册)
  ├─ settings.json                    (入口 = shaoxiang-ai-team-lead)
  ├─ agents/*.md                      (34 个 specialist 定义)
  └─ README.md
项目源码:              D:\Architecture\
归档目录:              D:\Architecture\deliverables\
```

> 旧文档指向 `C:\Users\ShXAI\.workbuddy\plugins\marketplaces\my-experts\plugins\`；若该路径已不存在，以本仓库 `D:\Architecture\.experts\backup\plugindata\shaoxiang-ai\` 为权威备份。

## 恢复指令

若 WorkBuddy 重装导致专家团丢失:
1. 查看本文件了解完整架构
2. 从 `D:\Architecture\.experts\backup\plugindata\shaoxiang-ai\` 取最新 plugin 配置
3. 使用 `expert-manager` skill 重建
4. 重建顺序: team-lead（入口）→ recorder → 各功能集群 agent
