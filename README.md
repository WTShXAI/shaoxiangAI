# 哨响AI — 智能足球预测系统 (LAMF v6.0.0)

> 当前版本以 FastAPI 后端、ModelBridge 预测服务、规则门控和生产模型为主。

---

## 系统架构

┌─────────────────────────────────────────────────────────┐
│                  用户访问层 / 监控层                       │
│  浏览器 / API 客户端 / 调度器 / 监控系统                     │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP REST / WS
┌──────────────────────────────▼──────────────────────────────┐
│                FastAPI 后端服务 (backend/main.py)           │
│  ├─ API 路由: /api/v1/monitor, /api/v1/predict, /api/v1/auth   │
│  ├─ 业务服务: backend/services/prediction_service.py         │
│  ├─ 模型加载: agents/model_bridge.py                         │
│  ├─ 规则与门控: rules/d_gate_engine.py + bookmaker_sim/*      │
│  └─ 配置中心: backend/core/config.py (Pydantic)               │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                    预测与模型层                              │
│  - ModelBridge + EnsembleTrainer / NN / Stacking             │
│  - D-Gate 动态门控引擎                                       │
│  - Bookmaker 仿真、贝叶斯推理与陷阱检测                      │
│  - HeuristicPredictor 旧版启发式回退                         │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                      数据层                                  │
│  - SQLite: data/football_data.db                             │
│  - 生产模型: saved_models/*.joblib                           │
│  - 训练/特征配置: config/config.yaml                          │
└──────────────────────────────────────────────────────────────┘

---

## 核心组件

- `main.py` 统一入口：pipeline/backend/predict/agent
- `backend/main.py`：FastAPI 应用入口与路由注册
- `backend/api/v1/endpoints/`：REST API 端点实现
- `backend/services/prediction_service.py`：预测业务逻辑与模型桥接
- `agents/model_bridge.py`：模型加载与模型版本解析
- `rules/d_gate_engine.py`：D-Gate 规则和动态门控引擎
- `backend/core/config.py` (Pydantic)：应用配置、模型路径、开关阈值
- `config/README.md`：配置中心说明（16文件分3层）

---

## 快速启动

### 前置条件

- Python 3.10+
- Node.js 18+ (前端开发)
- 依赖安装: `pip install -e .`（基于 pyproject.toml）
- 数据库: `data/football_data.db`
- 生产模型: `saved_models/football_v4.1_production.joblib`

### 启动

```bash
pip install -e .                          # 安装Python依赖
python -m uvicorn backend.main:app --port 8000   # 启动后端
cd frontend && npm install && npm run dev        # 启动前端 (端口3000)
```

访问 API 文档:

```text
http://localhost:8000/api/v1/docs
```

### 子命令

```bash
python main.py pipeline              # 自动预测 + 回测管道
python main.py pipeline --daemon     # 守护模式
python main.py pipeline --backtest   # 仅回测
python main.py pipeline --report     # 生成报告
python main.py backend [--dev]       # 启动 FastAPI 后端
python main.py backend --port 9000   # 自定义端口
python main.py predict               # 运行预测引擎
python main.py agent                 # 交互式对话（legacy，旧版 agent 流程）
python main.py conversation          # 启动对话引擎（legacy，旧版 agent 流程）
python main.py conv --demo           # 运行交互演示（legacy）
python main.py conv -q "巴西对阿根廷" # 单次查询（legacy）
python main.py eval                  # 模型上线评估流水线
python main.py eval --quick          # 快速评估
```

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_HOST` | `0.0.0.0` | 后端监听地址 |
| `API_PORT` | `8000` | 后端端口 |
| `DEBUG` | - | 设为 `true` 启用开发模式 |
| `DATABASE_URL` | `sqlite:///data/football_data.db` | 后端数据库连接 |

---

## 项目结构

```
D:\Architecture v4.0\
├── main.py                          # 统一入口 (pipeline/backend/predict/agent)
├── backend/                         # FastAPI 后端服务
│   ├── main.py                      # 应用入口与路由注册
│   ├── api/v1/endpoints/            # REST API 端点实现
│   ├── core/                        # 配置、数据库、依赖注入
│   ├── services/                    # 业务服务层
├── config/                          # 配置文件
│   ├── config.yaml                  # 训练/特征/模型路径配置
│   ├── settings.yaml                # 全局参数与阈值开关
│   ├── settings.py                  # Python 配置加载器
├── predictors/                      # 预测引擎组件
├── pipeline/                        # 预测管道与回测脚本
├── rules/                           # D-Gate / 领域规则
├── saved_models/                    # 生产模型
│   ├── football_v4.1_production.joblib
│   ├── draw_expert_v1.joblib
│   ├── football_nn_20260616_125617.pth
├── data/                            # 数据库
│   └── football_data.db
├── docs/                            # 项目文档
├── scripts/                         # 工具脚本
├── tests/                           # 测试
├── logs/                            # 日志
└── CHANGELOG.md                     # 变更日志
```

> 前端: `frontend/` — React 18 + TypeScript + Vite，514模块构建通过，`npm run dev` 启动。

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 后端框架 | Python 3.10+ · FastAPI · uvicorn |
| 数据库 | SQLite + SQLAlchemy |
| 异步任务 | Redis + Celery |
| 模型 | XGBoost + Ridge + EnsembleTrainer |
| 规则与门控 | D-Gate / DrawGate / Bookmaker 仿真 |
| 监控 | Prometheus · RotatingFileHandler |
| 配置 | Pydantic Settings / YAML |

---

## API 接口 (25+ 路由)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/docs` | Swagger API 文档 (DEBUG模式) |
| GET | `/api/v1/monitor/health` | 健康检查 |
| GET | `/api/v1/monitor/system` | 系统资源 (CPU/内存/磁盘) |
| GET | `/api/v1/models/versions` | 模型版本列表 |
| GET | `/api/v1/models/info` | 当前活跃模型信息 |
| POST | `/api/v1/models/deploy` | 部署模型 |
| POST | `/api/v1/models/rollback` | 回滚模型 |
| POST | `/api/v1/predict/single` | 单场预测 |
| POST | `/api/v1/predict/batch` | 批量预测 |
| GET | `/api/v1/predict/next-match` | 下一场比赛预测 |
| GET | `/api/v1/predict/history` | 预测历史记录 |
| GET | `/api/v1/predict/stats` | 预测统计信息 |
| GET | `/api/v1/matches/list` | 比赛列表 |
| GET | `/api/v1/matches/scores` | 比赛比分 |
| GET | `/api/v1/training/status` | 训练状态 |
| POST | `/api/v1/training/start` | 启动训练 |
| GET | `/api/v1/features/teams/{name}` | 球队特征查询 |
| POST | `/api/v1/auth/login` | 用户登录 |
| GET | `/api/v1/auth/me` | 当前用户信息 |
| GET | `/api/v1/alerts/alerts` | 告警列表 |
| GET | `/api/v1/historical/leagues` | 联赛列表 |
| GET | `/api/v1/chat` | SSE 流式对话 |
| GET | `/api/v1/jepa/predict` | JEPA模型预测 |

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构总览 |
| [docs/MODEL_LOADING.md](docs/MODEL_LOADING.md) | ModelBridge v2.0 模型加载指南 |
| [docs/API_ARCHITECTURE.md](docs/API_ARCHITECTURE.md) | FastAPI API 架构 + 历史 LangGraph/LLM 参考 |
| [docs/archive/PROMPT_TEMPLATES.md](docs/archive/PROMPT_TEMPLATES.md) | LAMF 模型使用指南 + Prompt 模板库 (已归档) |
| [docs/STARTUP_AND_HEALTH.md](docs/STARTUP_AND_HEALTH.md) | 启动与健康检查指南 |
| [docs/archive/FORMULAS.md](docs/archive/FORMULAS.md) | 核心公式与算法 (已归档) |
| [docs/archive/MODEL_CARD.md](docs/archive/MODEL_CARD.md) | 模型卡片 (训练参数/评估指标) (已归档) |
| [docs/archive/PREDICTION_AUDIT.md](docs/archive/PREDICTION_AUDIT.md) | 预测审计与可复盘设计 (已归档) |
