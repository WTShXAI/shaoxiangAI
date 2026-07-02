# 哨响AI 系统架构文档

> 版本: v4.1.0 | 更新: 2026-06-29
> 说明: 当前主线运行时为 FastAPI 后端 + ModelBridge/规则预测体系。历史 LLM/LangGraph 多智能体设计仅作参考，不是后端部署必需。

---

## 1. 当前主线架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      用户访问层                                   │
│  API 客户端 / 监控系统 / 调度器                                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP REST
┌──────────────────────────▼──────────────────────────────────────┐
│                FastAPI 后端服务 (backend/main.py)                 │
│  ├─ 注册路由: backend/api/v1/router.py                            │
│  ├─ 端点实现: backend/api/v1/endpoints/                           │
│  ├─ 文档: /api/v1/docs /api/v1/openapi.json                       │
│  └─ 中间件: CORS / TrustedHost / request_id / 全局异常处理         │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                      预测与模型层                                 │
│  - backend/services/prediction_service.py                         │
│  - agents/model_bridge.py                                          │
│  - rules/d_gate_engine.py + rules/drawgate_v53.py                 │
│  - saved_models/football_v4.1_production.joblib                    │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                          数据层                                   │
│  - SQLite: data/football_data.db                                  │
│  - 训练/特征配置: config/config.yaml                              │
│  - 特征计算: features/ / rules/ / data_collector/                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. 目录结构

```
D:\Architecture v4.0\
├── main.py                  # 统一入口 (pipeline/backend/predict/agent)
├── start.py                 # 容器化启动入口
├── config/                  # 配置 (settings.yaml)
├── agents/                  # 模型加载与启发式回退
│   ├── model_bridge.py      # ModelBridge: 生产模型加载与审计
│   ├── heuristic_predictor.py # 旧版启发式回退逻辑
│   └── __init__.py
├── backend/                 # FastAPI 后端服务 (端口 8000)
│   ├── main.py              # 应用入口和路由注册
│   ├── api/v1/              # API v1 路由
│   │   ├── endpoints/       # 端点实现 (12 组路由)
│   │   └── router.py        # 主路由聚合
│   ├── routers/             # 迁移路由 (chat/fixtures/jepa/misc)
│   ├── services/            # 业务逻辑层
│   └── core/                # 配置、数据库、依赖注入
│       ├── config.py        # Pydantic Settings (统一配置 v2)
│       └── database.py      # SQLAlchemy 引擎
├── predictors/              # 预测引擎
├── pipeline/                # 预测管道
│   ├── full_linkage_predictor.py  # 全链路联动预测 (~81KB)
│   ├── auto_pipeline.py     # 自动预测管道
│   ├── knockout_predictor.py # 淘汰赛预测
│   ├── handicap_pipeline.py # 盘口分析管道
│   └── ...                  # 其他管道
├── rules/                   # D-Gate 引擎和领域规则
│   ├── d_gate_engine.py     # D-Gate v5 核心引擎
│   ├── d_gate_utils.py      # D-Gate 工具函数
│   ├── d_gate_v52.py        # D-Gate v5.2 实现 (待清理)
│   ├── drawgate_v53.py      # DrawGate v5.3
│   ├── domain_rules.py      # 领域规则
│   ├── multi_signal_engine.py # 多信号引擎
│   ├── tournament_dynamics.py # 赛事阶段动态
│   ├── sp_core.py           # SP 核心
│   └── football_kb.yaml     # 知识库
├── features/                # 特征计算 (90+ 维)
│   └── feature_calculator.py
├── saved_models/            # 生产模型
│   ├── football_v4.1_production.joblib  # 锁定生产模型
│   ├── draw_expert_v1.joblib            # DrawExpert 模型
│   ├── football_nn_20260616_125617.pth  # 神经网络模型
│   └── model_registry_v2b.json         # 模型注册表
├── data/                    # 数据
│   └── football_data.db     # SQLite 主库
├── docs/                    # 项目文档
│   ├── ARCHITECTURE.md      # 本文档
│   ├── API_REFERENCE.md     # API 参考
│   ├── adr/                 # 架构决策记录
│   ├── CHANGELOG.md         # 变更日志
│   └── ...                  # 其他文档
├── scripts/                 # 工具脚本
├── tests/                   # 测试
├── logs/                    # 日志
└── CHANGELOG.md             # 变更日志 (根目录)
```

---

## 3. 技术栈

| 类别 | 技术 | 版本 |
|------|------|------|
| 后端框架 | **FastAPI** + uvicorn | v5.0 |
| 数据库 | **SQLite** (SQLAlchemy ORM) | — |
| ML 模型 | **XGBoost** + **Ridge** (ModelBridge v2.0) | — |
| 预测引擎 | **D-Gate** (动态门控) | **v5.3** |
| 平局预测 | **DrawGate** + **DrawExpert** (合并) | v5.3 |
| 全链路联动 | **Full Linkage Predictor** | — |
| LLM | **Ollama** 本地部署 | 历史/可选，非当前 backend 运行时必需 |
| 工作流 | **LangGraph** StateGraph | 历史/可选，主要用于旧版 agent 流程 |
| 前端 | 纯静态 HTML+CSS+JS SPA | v5.0 |
| 消息队列 | **Celery** + Redis | — |
| ML 追踪 | **MLflow** | — |
| 监控 | **Prometheus** | — |

---

## 4. 核心组件

### 4.1 FastAPI 后端 (端口 8000)

- 异步 I/O，自动 OpenAPI 文档
- Pydantic 模型校验
- 路由分组: `api/v1/endpoints/` (12 组) + `routers/` (5 组)
- 全局异常处理、CORS、TrustedHost 中间件
- 兼容层: Flask WSGI via `a2wsgi`

### 4.2 D-Gate v5.3 引擎

五层门控架构:
1. **L0 - Knowledge**: 知识库规则
2. **L1 - Signal**: 信号提取（DrawGate、HCP、OU、Poisson、Elo）
3. **L2 - Gate**: 动态门控决策
4. **L3 - Barrier**: 场景屏障（赛事阶段、联赛类型）
5. **L4 - Degradation**: 降级策略

### 4.3 ModelBridge v2.0

- 单例模式，锁定 `football_v4.1_production.joblib`
- 禁止运行时回退
- 硬编码概率检测 (H=0.40/D=0.28/A=0.32)
- 审计字段：`_model`, `_version`, `_timestamp`
- 预测快照 (JSON)

### 4.4 三层降级策略

| 层级 | 方式 | 适用场景 |
|------|------|---------|
| L1 | Ollama LLM (phi4:14b) | 完整上下文推理，最高质量 |
| L2 | ModelBridge ML 推理 | XGBoost+Ridge 集成，高准确率 |
| L3 | 规则 Fallback | 领域知识 + 泊松 + Kelly，兜底保障 |

---

## 5. 版本体系

| 维度 | 版本 | 说明 |
|------|------|------|
| 产品版本 | **v4.1.0** | 整体项目发布版本 |
| D-Gate 引擎 | **v5.3** | 动态门控引擎版本 |
| ModelBridge | **v2.0** | 模型锁定与审计框架 |
| LAMF | **v4.1.0** | 多智能体框架版本 |
| API | **v1** | REST API 版本 |
| 前端 | **v5.0** | SPA 前端版本 |

---

## 6. 数据流

```
API 请求 → FastAPI 路由 → UnifiedPredictor
  → D-Gate v5.3 (DrawGate + HCP + OU + Poisson + Elo)
  → LINKAGE_MATRIX 协同 → 概率输出
  → ModelBridge / LLM / 规则 (降级)
  → 审计字段注入 → JSON 响应
```

---

## 7. 已知问题

| 优先级 | 问题 | 状态 |
|--------|------|------|
| P0 | D-Gate v5.2 遗留代码待清理 (`rules/d_gate_v52.py`) | 🔧 待处理 |
| P0 | Containerization - Dockerfile + compose | ✅ 已完成 |
| P1 | Prometheus `/metrics` 重复注册 | 🔧 待修复 |
| P1 | 日志落盘 - RotatingFileHandler | 🔧 待处理 |
| P2 | Alembic 数据库迁移初始化 | 🔧 待处理 |
