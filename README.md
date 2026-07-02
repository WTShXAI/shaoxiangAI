# 哨响AI — 智能足球预测系统 (LAMF v6.0.0)

> 当前版本以 **FastAPI 后端 + React 前端 + WebSocket 实时推送** 为核心，支持比赛数据分析、实时比分、模型管理、系统监控等功能。

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        用户浏览器                                 │
│              React 18 + TypeScript + Vite (localhost:3000)       │
│          Axios (/api/v1/*)   WebSocket (/ws/realtime)            │
└───────────────────────────────┬─────────────────────────────────┘
                                │ Vite Proxy
                    ┌───────────┴───────────┐
                    │  /api → localhost:9000  │
                    │  /ws  → localhost:9000  │
                    └───────────┬───────────┘
┌───────────────────────────────▼─────────────────────────────────┐
│                FastAPI 后端服务 (serve.py → port 9000)          │
│  ├─ API 路由: /api/v1/monitor, /api/v1/predict, /api/v1/auth    │
│  ├─ WebSocket: /ws/realtime (实时比分/比赛更新)                   │
│  ├─ 业务服务: backend/services/prediction_service.py             │
│  ├─ 模型加载: agents/model_bridge.py                             │
│  ├─ 规则与门控: rules/d_gate_engine.py + bookmaker_sim/*        │
│  └─ 配置中心: backend/core/config.py (Pydantic)                  │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│                    预测与模型层                                  │
│  - ModelBridge + EnsembleTrainer / NN / Stacking                  │
│  - D-Gate 动态门控引擎                                           │
│  - Bookmaker 仿真、贝叶斯推理与陷阱检测                          │
│  - JEPA Lite 后融合 + KNN-Hybrid (draw 专精)                    │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│                      数据层                                      │
│  - SQLite: data/football_data.db / database/football_core.db    │
│  - 生产模型: saved_models/*.joblib / *.pt                        │
│  - 训练/特征配置: config/config.yaml                              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 快速启动

### 前置条件

- Python 3.10+
- Node.js 18+ (前端开发)
- 虚拟环境: `.venv\Scripts\python.exe` (项目自带)

### 一键启动 (推荐)

双击 `启动哨响AI.bat`，自动完成：

1. 启动后端服务 (port 9000)
2. 启动前端开发服务器 (port 3000)
3. 8 秒后自动打开浏览器 `http://localhost:3000`

### 手动启动

```bash
# 后端
.venv\Scripts\python.exe serve.py --port 9000

# 前端 (新终端)
cd frontend && npm install && npm run dev
```

访问:

| 服务 | 地址 |
|------|------|
| 前端页面 | http://localhost:3000 |
| API 文档 | http://localhost:9000/api/v1/docs |
| Prometheus 指标 | http://localhost:9091 |

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_HOST` | `0.0.0.0` | 后端监听地址 |
| `API_PORT` | `9000` | 后端端口 |
| `DEBUG` | - | 设为 `true` 启用开发模式 |
| `JEPA_BLEND_WEIGHT` | `0.08` | JEPA 后融合权重 (0=关闭) |
| `THRESHOLD_BIAS_H/D/A` | `0.02 / 0.10 / -0.04` | 分类阈值偏置 |
| `CUDA_VISIBLE_DEVICES` | - | GPU 设备选择 |

### 子命令

```bash
python main.py pipeline              # 自动预测 + 回测管道
python main.py pipeline --daemon     # 守护模式
python main.py pipeline --backtest   # 仅回测
python main.py pipeline --report     # 生成报告
python main.py backend [--dev]       # 启动 FastAPI 后端
python main.py backend --port 9000   # 自定义端口
python main.py predict               # 运行预测引擎
python main.py agent                 # 交互式对话 (legacy)
python main.py conversation          # 启动对话引擎 (legacy)
python main.py conv --demo           # 运行交互演示 (legacy)
python main.py conv -q "巴西对阿根廷" # 单次查询 (legacy)
python main.py eval                  # 模型上线评估流水线
python main.py eval --quick          # 快速评估
```

---

## 前端概览

现代 **React 18 + TypeScript + Vite** 单页应用，位于 `frontend/` 目录。

| 技术 | 用途 |
|------|------|
| React 18 | UI 框架 |
| TypeScript 5.5 | 类型安全 |
| Vite 5 | 构建工具 + HMR |
| Zustand | 全局状态管理 |
| TanStack React Query | 服务端数据缓存 |
| Framer Motion | 页面动画 |
| TailwindCSS 3 + PostCSS | 样式 |
| ECharts | 图表可视化 |
| Axios | HTTP 请求 |

### 前端页面

| 页面 | 路由 | 功能 |
|------|------|------|
| 数据探索 | `/data-explorer` | 比赛列表查询、搜索/筛选、WebSocket 实时更新 |
| 预测大厅 | `/prediction` | 比赛预测、概率展示 |
| 模型管理 | `/models` | 模型版本、部署/回滚 |
| 系统监控 | `/monitor` | 系统健康、告警管理、Prometheus 指标 |

---

## 项目结构

```
D:\Architecture\
├── 启动哨响AI.bat               # 一键启动脚本 (双击运行)
├── serve.py                     # 后端启动器 (隔离包冲突)
├── main.py                      # 统一入口 (pipeline/backend/predict/agent)
├── bridge_service.py            # 模型桥接服务
├── v5_server.py                 # JEPA v5.0 KNN-Hybrid Server
├── .venv/                       # Python 虚拟环境
├── .vscode/
│   └── settings.json            # IDE 配置 (.py 关联 Python 模式)
│
├── backend/                     # FastAPI 后端服务
│   ├── main.py                  # 应用入口与路由注册
│   ├── api/v1/endpoints/        # REST API 端点实现
│   │   ├── monitor.py           # 监控/健康检查
│   │   ├── predictions.py       # 预测接口
│   │   ├── matches.py           # 比赛数据
│   │   ├── chat_routes.py       # SSE 流式对话
│   │   └── ...
│   ├── core/                    # 配置、数据库、依赖注入
│   ├── services/                # 业务服务层
│   └── requirements.txt
│
├── frontend/                    # React 18 + TypeScript + Vite 前端
│   ├── src/
│   │   ├── pages/
│   │   │   ├── DataExplorer/    # 数据探索 (WebSocket 实时)
│   │   │   ├── PredictionHall/  # 预测大厅
│   │   │   ├── ModelManagement/ # 模型管理
│   │   │   └── SystemMonitor/   # 系统监控
│   │   ├── services/api.ts      # Axios API 封装
│   │   ├── types/               # TypeScript 类型定义
│   │   └── vite-env.d.ts        # Vite 类型声明
│   ├── vite.config.ts           # Vite 配置 (代理 /api → 9000)
│   └── package.json
│
├── config/                      # 配置文件
│   ├── config.yaml              # 训练/特征/模型路径配置
│   ├── settings.yaml            # 全局参数与阈值开关
│   └── api_config.py            # API 配置加载器
│
├── agents/                      # AI Agent 模块
├── predictors/                  # 预测引擎组件
├── pipeline/                    # 预测管道与回测脚本
├── rules/                       # D-Gate / 领域规则
├── bookmaker_sim/              # 博彩仿真
├── models/                      # PyTorch 模型定义
├── saved_models/               # 生产模型文件
│   ├── football_v4.1_production.joblib
│   ├── draw_expert_v1.joblib
│   └── football_nn_*.pth
│
├── data/                        # 数据库与缓存
│   ├── football_data.db
│   └── api_cache/              # API 请求缓存
│
├── docs/                        # 项目文档
├── scripts/                     # 工具脚本
├── tests/                       # 测试
└── logs/                        # 运行日志
```

---

## API 接口一览 (25+ 路由)

### 监控 & 健康

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/monitor/health` | 健康检查 |
| GET | `/api/v1/monitor/system` | 系统资源 (CPU/内存/磁盘) |
| GET | `/api/v1/monitor/metrics/summary` | 指标摘要 (请求量/错误率) |
| GET | `/api/v1/monitor/model-health` | 模型健康状态 |

### 预测

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/predict/single` | 单场预测 |
| POST | `/api/v1/predict/batch` | 批量预测 |
| GET | `/api/v1/predict/next-match` | 下一场比赛预测 |
| GET | `/api/v1/predict/history` | 预测历史记录 |
| GET | `/api/v1/predict/stats` | 预测统计信息 |

### 比赛 & 数据

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/matches/list` | 比赛列表 |
| GET | `/api/v1/matches/scores` | 比赛比分 |
| GET | `/api/v1/historical/leagues` | 联赛列表 |
| GET | `/api/v1/historical/{code}/matches` | 联赛历史比赛 |

### 模型管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/models/versions` | 模型版本列表 |
| GET | `/api/v1/models/info` | 当前活跃模型信息 |
| POST | `/api/v1/models/deploy` | 部署模型 |
| POST | `/api/v1/models/rollback` | 回滚模型 |

### 告警 & 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/alerts/alerts` | 告警列表 |
| POST | `/api/v1/auth/login` | 用户登录 |
| GET | `/api/v1/auth/me` | 当前用户信息 |

### 其他

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/docs` | Swagger API 文档 (DEBUG 模式) |
| GET | `/api/v1/chat` | SSE 流式对话 |
| GET | `/api/v1/jepa/predict` | JEPA 模型预测 |
| WS | `/ws/realtime` | 实时比分 / 比赛更新推送 |
| GET | `/api/v1/features/teams/{name}` | 球队特征查询 |
| GET | `/api/v1/training/status` | 训练状态 |
| POST | `/api/v1/training/start` | 启动训练 |

---

## 实时 WebSocket 推送

数据探索页面通过 WebSocket (`/ws/realtime`) 接收实时数据更新。

**后端推送格式:**

```json
// 单场比赛更新
{ "type": "match_update", "match": { "id": "...", "homeScore": 2, ... } }

// 全量列表
{ "type": "matches_list", "matches": [...] }
```

前端收到 `match_update` 后自动合并到现有数据集中触发重渲染，无需刷新页面。

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 后端框架 | Python 3.10+ · FastAPI · uvicorn |
| 前端框架 | React 18 + TypeScript 5.5 + Vite 5 |
| 数据库 | SQLite + SQLAlchemy |
| 模型 | XGBoost + Ridge + EnsembleTrainer + JEPA Lite |
| 规则与门控 | D-Gate / DrawGate / Bookmaker 仿真 |
| 实时通信 | WebSocket (前端自动重连) |
| 监控 | Prometheus · RotatingFileHandler |
| 配置 | Pydantic Settings / YAML |
| 状态管理 | Zustand · TanStack React Query |
| 样式 | TailwindCSS 3 + Framer Motion |

---

## 文档索引

| 文档 | 说明 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 系统架构总览 |
| [docs/MODEL_LOADING.md](docs/MODEL_LOADING.md) | ModelBridge v2.0 模型加载指南 |
| [docs/API_ARCHITECTURE.md](docs/API_ARCHITECTURE.md) | FastAPI API 架构 |
| [docs/STARTUP_AND_HEALTH.md](docs/STARTUP_AND_HEALTH.md) | 启动与健康检查指南 |
| [docs/archive/PROMPT_TEMPLATES.md](docs/archive/PROMPT_TEMPLATES.md) | Prompt 模板库 (已归档) |
| [docs/archive/FORMULAS.md](docs/archive/FORMULAS.md) | 核心公式与算法 (已归档) |
| [docs/archive/MODEL_CARD.md](docs/archive/MODEL_CARD.md) | 模型卡片 (已归档) |
| [docs/archive/PREDICTION_AUDIT.md](docs/archive/PREDICTION_AUDIT.md) | 预测审计设计 (已归档) |
