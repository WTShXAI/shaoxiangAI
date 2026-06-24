# 哨响AI — 智能足球预测系统 (LAMF v4.1.0)

> 基于 LAMF (Local AI Model Framework) 的多智能体足球预测系统

---

## 系统架构

┌──────────────────────▼──────────────────────────────────┐
│               LangGraph 多智能体工作流                     │
│  Commander(路由) → DataAgent → MathAgent → Explainer     │
│                    → Commander(汇总) → 最终决策            │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                 ModelBridge v2.0                         │
│  锁定模型 · 禁止回退 · 硬编码检测 · 审计字段 · 预测快照    │
│         XGBoost + Ridge 双模型集成                        │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│              SQLite 数据层 (data/football_data.db)        │
│         匹配数据 · 90+ 维特征 · 历史比赛记录               │
└─────────────────────────────────────────────────────────┘
```

---

## LAMF 模型矩阵

| 模型 | Agent 角色 | 核心职责 | 调用方式 |
|------|-----------|---------|---------|
| **gemma4:12b** | Commander 指挥官 | 意图理解、任务路由、结果汇总、最终决策 | `OllamaLLM(model="gemma4:12b")` |
| **deepseek-r1:8b** | DataAgent 数据分析师 | 数据获取、90+特征计算、趋势识别、异常检测 | `OllamaLLM(model="deepseek-r1:8b")` |
| **phi4:14b** | MathAgent 数学家 | 概率计算、风险评估、Kelly 准则、三层降级 | `OllamaLLM(model="phi4:14b")` |
| **qwen3:8b** | Explainer 解释器 | 中文通俗解释、用户交互、文案润色 | `OllamaLLM(model="qwen3:8b")` |

---

## 工作流

```
START → Commander(路由) → DataAgent → MathAgent → Explainer → Commander(汇总) → END
                    ↘ → Commander(汇总) → END  (简化问答时跳过专家)
```

基于 LangGraph StateGraph，支持条件路由和降级到 SimpleWorkflow。

---

## ML 模型系统

| 组件 | 说明 |
|------|------|
| **集成模型** | XGBoost + Ridge 双模型，锁定为 `football_v4.1_production.joblib` |
| **ModelBridge v2.0** | 单例模式、禁止回退、硬编码概率检测 (H=0.40/D=0.28/A=0.32)、审计字段 (_model/_version/_timestamp)、预测快照 (JSON) |
| **特征体系** | 90+ 维特征：攻防实力、近期状态、交锋记录、盘口赔率、伤病指数、市场特征等 |

---

## 三层降级策略

| 层级 | 方式 | 说明 |
|------|------|------|
| **L1** | Ollama LLM (phi4:14b) | 完整上下文数学推理，最高质量 |
| **L2** | ModelBridge ML 推理 | XGBoost+Ridge 集成，高准确率，含审计字段 |
| **L3** | 规则 Fallback | 领域知识修正 + 泊松分布 + Kelly 准则，兜底保障 |

---

## 快速启动

### 前置条件

- Python 3.10+
- [Ollama](https://ollama.com) 已安装并运行，确保以下模型已拉取:
  ```bash
  ollama pull gemma4:12b
  ollama pull deepseek-r1:8b
  ollama pull phi4:14b
  ollama pull qwen3:8b
  ```
- 模型文件: `saved_models/football_balanced_production.joblib`
- 数据库: `data/football_data.db`

### 启动

```powershell
# 创建虚拟环境并安装依赖
.\setup_env.ps1

# 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 启动后端 (开发模式)
python main.py backend --dev

# 访问 API 文档
# http://localhost:8000/docs
```

### 子命令

```bash
python main.py pipeline              # 自动预测 + 回测管道
python main.py pipeline --daemon     # 守护模式 (定时执行)
python main.py pipeline --backtest   # 仅回测
python main.py pipeline --report     # 准确率报告
python main.py backend [--dev]       # FastAPI 后端 (默认 :8000)
python main.py backend --port 9000   # 自定义端口
python main.py predict               # 单次预测引擎
python main.py agent                 # 交互式智能体对话
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_HOST` | `0.0.0.0` | 后端监听地址 |
| `API_PORT` | `8000` | 后端端口 |
| `DEBUG` | - | 设为 `true` 启用开发模式 (免 JWT) |

---

## 项目结构

```
footballAI/
├── main.py                  # 统一入口 (pipeline/backend/predict/agent)
├── config.yaml              # 特征/权重/默认值配置
├── requirements.txt         # Python 运行时依赖
├── agents/                  # LAMF 多智能体
│   ├── commander.py         # Commander: gemma4:12b 意图路由+汇总
│   ├── data_agent.py        # DataAgent: deepseek-r1:8b 数据分析
│   ├── math_agent.py        # MathAgent: phi4:14b 概率计算+三层降级
│   ├── explainer.py         # Explainer: qwen3:8b 中文解释
│   ├── model_bridge.py      # ModelBridge v2.0: 模型锁定+硬编码检测
│   ├── workflow.py          # LangGraph 工作流编排
│   ├── scheduler.py         # Agent 调度器
│   ├── state.py             # Agent 状态定义
│   └── nodes.py             # 工作流节点
├── backend/                 # FastAPI 后端服务
├── frontend/                # 纯静态 SPA 前端 (v5.0 深空暗黑主题)
├── features/                # 特征计算引擎 (90+ 维)
│   └── feature_calculator.py
├── rules/                   # 领域知识规则
│   ├── domain_rules.py
│   └── football_kb.yaml
├── saved_models/            # 生产模型 (仅 4 文件)
│   ├── football_balanced_production.joblib  # 锁定生产模型
│   ├── footballai_compressed_features.json  # 特征名列表
│   ├── footballai_v4_latest.joblib          # v4 训练产物 (参考)
│   └── model_registry_v2b.json              # 模型注册表
├── data/                    # 数据
│   └── football_data.db     # SQLite 主库
├── scripts/                 # 工具脚本 (93 个, 6 个已废弃)
├── docs/                    # 项目文档
├── logs/                    # 日志
├── output/                  # 输出
└── tests/                   # 测试
```

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 后端框架 | Python 3.10+ · FastAPI · uvicorn |
| 数据库 | SQLite (data/football_data.db) |
| 前端 | 纯静态 HTML+CSS+JS SPA (v5.0 深空暗黑主题) |
| ML 模型 | XGBoost + Ridge (ModelBridge v2.0 锁定) |
| LLM | Ollama 本地部署 (gemma4 / deepseek-r1 / phi4 / qwen3) |
| 工作流 | LangGraph StateGraph (含 SimpleWorkflow 降级) |
| 预测管道 | auto_pipeline.py (AutoPipeline) |

---

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 服务信息 |
| GET | `/docs` | Swagger API 文档 |
| GET | `/api/v1/monitor/health` | 健康检查 |
| POST | `/api/v1/predict` | 单场预测 |
| POST | `/api/v1/auth/login` | 用户登录 |
| POST | `/api/v1/auth/register` | 用户注册 |

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构总览 |
| [docs/MODEL_LOADING.md](docs/MODEL_LOADING.md) | ModelBridge v2.0 模型加载指南 |
| [docs/API_ARCHITECTURE.md](docs/API_ARCHITECTURE.md) | LangGraph 工作流与 API 架构 |
| [docs/PROMPT_TEMPLATES.md](docs/PROMPT_TEMPLATES.md) | LAMF 模型使用指南 + Prompt 模板库 |
| [docs/STARTUP_AND_HEALTH.md](docs/STARTUP_AND_HEALTH.md) | 启动与健康检查指南 |
| [docs/FORMULAS.md](docs/FORMULAS.md) | 核心公式与算法 |
| [docs/MODEL_CARD.md](docs/MODEL_CARD.md) | 模型卡片 (训练参数/评估指标) |
| [docs/PREDICTION_AUDIT.md](docs/PREDICTION_AUDIT.md) | 预测审计与可复盘设计 |
