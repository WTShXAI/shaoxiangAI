# LAMF 架构与 API 依赖图 (v4.1.0)

> 更新: 2026-06-12 · 架构: LangGraph + ModelBridge v2.0 · 入口: main.py

---

## 1. 运行时总览

```
                         ┌──────────────────────┐
                         │      main.py          │
                         │   (统一入口, 4子命令)   │
                         └──────┬───────────────┘
                ┌───────────────┼───────────────┬──────────────┐
                ▼               ▼               ▼              ▼
           pipeline         backend          predict         agent
        (AutoPipeline)   (uvicorn:8000)   (预测引擎)    (智能体对话)
                                │
                    ┌───────────┴──────────┐
                    ▼                      ▼
            FastAPI 路由层          LangGraph 工作流
         (backend/ 目录)         (agents/workflow.py)
                    │                      │
                    └──────────┬───────────┘
                               ▼
                    ┌─────────────────────┐
                    │   ModelBridge v2.0   │
                    │  (单例, 锁定模型)     │
                    └──────────┬──────────┘
                               ▼
                    ┌─────────────────────┐
                    │  SQLite 数据库       │
                    │  data/football_data.db│
                    └─────────────────────┘
```

---

## 2. LangGraph 工作流图

工作流定义在 `agents/workflow.py`，使用 LangGraph `StateGraph` 构建条件路由。

```
                        START
                          │
                          ▼
                 ┌────────────────┐
                 │ commander_route │  ← gemma4:12b 分析意图, 决定调用哪些 Agent
                 └───────┬────────┘
                         │
              ┌──────────┴──────────┐
              │ 需要数据?            │ 不需要
              ▼                     ▼
      ┌──────────────┐    ┌────────────────────┐
      │  data_agent   │    │ commander_synthesize│
      │ deepseek-r1   │    └──────────┬─────────┘
      └──────┬───────┘               │
             │                       │
    ┌────────┴────────┐              │
    │ 需要概率?        │ 不需要       │
    ▼                 ▼              │
┌────────────┐  ┌──────────┐        │
│ math_agent  │  │explainer │        │
│  phi4:14b   │  │ qwen3:8b │        │
└─────┬──────┘  └────┬─────┘        │
      │              │              │
  ┌───┴───┐          │              │
  │需解释? │ 不需要    │              │
  ▼       ▼          │              │
┌──────┐ ┌──────────┐│              │
│expl..│ │commander ││              │
│qwen3 │ │synthesize│◄──────────────┘
└──┬───┘ └────┬─────┘
   │          │
   └────┬─────┘
        ▼
   ┌────────┐
   │  END   │
   └────────┘
```

### 路由逻辑

| 条件函数 | 文件位置 | 逻辑 |
|---------|---------|------|
| `_route_after_commander()` | workflow.py:216 | Commander 路由分析后，`data_agent` 在 experts_to_call 中 → 进入 data_agent；否则直接汇总 |
| `_should_call_math()` | workflow.py:236 | data_agent 完成后，`math_agent` 在列表中 → 进入 math_agent；否则检查 explainer |
| `_should_call_explainer()` | workflow.py:255 | math_agent 完成后，`explainer` 在列表中 → 进入 explainer；否则直接汇总 |

---

## 3. Agent 调用关系

### 3.1 4 Agent 职责矩阵

| Agent | LLM 模型 | 源码文件 | 职责 | 输入 | 输出 |
|-------|---------|---------|------|------|------|
| **Commander** | gemma4:12b | `agents/commander.py` | 意图路由 + 结果汇总 | 用户输入文本 | `task_type`, `experts_to_call`, 最终决策 |
| **DataAgent** | deepseek-r1:8b | `agents/data_agent.py` | 数据获取 + 特征分析 | match_data + 90+ 特征 | 数据洞察、异常指标、趋势描述 |
| **MathAgent** | phi4:14b | `agents/math_agent.py` | 概率计算 + 风险评估 | 特征数据 + 分析结果 | H/D/A 概率、INVEST/WATCH/PASS |
| **Explainer** | qwen3:8b | `agents/explainer.py` | 中文解释生成 | 预测结果 + 分析洞察 | 3-5 句通俗中文解释 |

### 3.2 Commander 的两阶段职责

**阶段 1: `invoke()` → 路由**
```json
{
  "task_type": "predict",
  "assigned_agents": ["data_analyst", "math_agent"],
  "reason": "用户询问比赛结果，需先分析数据再算概率"
}
```

**阶段 2: `synthesize()` → 汇总**
```python
expert_results = {
    "data_analysis": {...},   # DataAgent 输出
    "math_analysis": {...},   # MathAgent 输出
    "explanation": {...},     # Explainer 输出
}
synthesis = commander.synthesize(expert_results)
# → { "final_prediction": {...}, "confidence": 0.85, "decision": "INVEST" }
```

### 3.3 MathAgent 三层降级

```
MathAgent.invoke()
  │
  ├── L1: _analyze_with_llm()      → Ollama phi4:14b 数学推理
  ├── L2: _analyze_with_bridge()   → ModelBridge ML 推理 (XGBoost+Ridge)
  └── L3: _analyze_with_rules()    → 领域知识 + 泊松分布 + Kelly 准则
```

详见 `docs/MODEL_LOADING.md` 第四章。

---

## 4. FastAPI 路由表

后端入口: `main.py backend [--dev] [--port 9000]`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 服务信息 |
| GET | `/docs` | Swagger API 文档 |
| GET | `/api/v1/monitor/health` | 健康检查 → `{"status":"ok"}` |
| POST | `/api/v1/predict` | 单场预测 |
| POST | `/api/v1/auth/login` | 用户登录 |
| POST | `/api/v1/auth/register` | 用户注册 |

> 注意: 精确的路由表以 `backend/` 目录下实际注册的路由为准，上述为常见端点。

---

## 5. 统一入口命令

`main.py` 是项目的唯一入口点，所有功能通过子命令调用：

| 命令 | 说明 | 关键参数 |
|------|------|---------|
| `python main.py pipeline` | 运行自动预测+回测管道 | `--daemon`, `--backtest`, `--report`, `--interval` |
| `python main.py backend` | 启动 FastAPI 后端服务 | `--dev`, `--port 9000` |
| `python main.py predict` | 启动预测引擎 | 需要已加载的模型 |
| `python main.py agent` | 运行智能体对话 | 交互式命令行界面 |

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_HOST` | `0.0.0.0` | FastAPI 监听地址 |
| `API_PORT` | `8000` | FastAPI 端口 |
| `DEBUG` | - | 设为 `true` 启用开发模式 (免 JWT) |

---

## 6. 数据流 (一次完整预测)

```
1. 用户输入 "预测曼联 vs 利物浦"
         │
2. main.py agent → AgentWorkflow.invoke({"input": "预测曼联 vs 利物浦"})
         │
3. Commander.invoke() → gemma4:12b → task_type="predict", experts=["data_agent","math_agent","explainer"]
         │
4. DataAgent.invoke() → deepseek-r1:8b → 从 SQLite 提取两队历史数据 + 计算90+特征
         │
5. MathAgent.invoke() → 依次尝试:
   ├── phi4:14b LLM 概率推理
   ├── ModelBridge.predict() → XGBoost+Ridge 预测
   └── 规则 Fallback → 领域知识修正 + 泊松分布
         │
6. Explainer.invoke() → qwen3:8b → 生成中文解释
         │
7. Commander.synthesize() → gemma4:12b → 汇总 → 最终决策 INVEST/WATCH/PASS
         │
8. 返回结构化回答:
   📊 预测结果: 主胜
      主胜: 52.3% | 平局: 25.1% | 客胜: 22.6%
      置信度: 85% | 决策: INVEST
   💡 分析: 曼联近期主场表现强势...
   📌 要点: 1. 主场优势明显 2. 利物浦客场疲软
   🎯 建议: 可以考虑小注主胜
```

---

## 7. 配置与环境

| 配置源 | 用途 | 示例 |
|--------|------|------|
| `config.yaml` | 特征列名、集成权重、默认值、数据参数 | `data.default_values`, `features.columns` |
| `.env` | API 密钥、端口、调试开关 | `API_PORT=8000`, `DEBUG=true` |
| `rules/domain_rules.py` | 领域知识规则 (德比加成/Top6修正/伤停惩罚) | `DERBY_BOOST = 0.05` |
| `rules/football_kb.yaml` | DomainKB 知识库 | 球队实力评分、历史交锋权重 |

---

## 8. 架构演进 (v3.1 → v4.1.0)

| 组件 | v3.1 (旧) | v4.1.0 (当前) |
|------|----------|---------------|
| Agent 数量 | 10 专家 + Orchestrator | 4 Agent (Commander/Data/Math/Explainer) |
| 模型推理 | A/B/C 三条路径 + UnifiedPredictor | ModelBridge v2.0 单一路径，锁定模型 |
| 模型文件 | 27 个 (Ridge×16, XGBoost×10, 集成×1) | 4 个 |
| Web 框架 | Flask (端口 5000) | FastAPI + uvicorn (端口 8000) |
| 前端 | Vue 3 + Element Plus | 纯静态 SPA (v5.0 深空暗黑主题) |
| 工作流 | AgentOrchestrator 顺序编排 | LangGraph StateGraph 条件路由 |
| 降级 | 静默降级到默认概率 | Fail-Fast + 三层显式降级 |
| 入口 | `run_backend.py` / `python -m api.prediction_service` | `python main.py backend` |
