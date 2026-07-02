# 哨响AI 入职指南

> 适用于新加入团队的工程师，涵盖项目结构、环境搭建、编码规范和启动指南。

---

## 目录

1. [项目概览](#1-项目概览)
2. [目录结构](#2-目录结构)
3. [环境搭建](#3-环境搭建)
4. [启动指南](#4-启动指南)
5. [编码规范](#5-编码规范)
6. [Git 工作流](#6-git-工作流)

---

## 1. 项目概览

**哨响AI (LAMF v4.1.0)** 是一个基于多智能体和机器学习模型的足球预测系统。核心能力：

- **多智能体工作流**: LangGraph 编排 4 个 LLM Agent（Commander、DataAgent、MathAgent、Explainer）
- **D-Gate v5.3 引擎**: 动态门控决策引擎，集成 DrawGate、HCP、OU、Poisson 等多种信号
- **ML 模型集成**: XGBoost + Ridge 双模型锁定生产
- **FastAPI 后端**: RESTful API，端口 8000，v1 前缀
- **全链路联动管道**: 统一预测入口，三层降级策略 (LLM → ML → 规则)

### 版本体系

| 版本 | 说明 |
|------|------|
| 产品版本 v4.1.0 | 当前产品发布版本 |
| D-Gate 引擎 v5.3 | 动态门控引擎版本 |
| ModelBridge v2.0 | 模型锁定与审计框架 |

---

## 2. 目录结构

```
D:\Architecture\
├── main.py                  # 统一入口 (pipeline/backend/predict/agent)
├── start.py                 # 容器化启动入口
├── config/                  # 配置文件
├── agents/                  # LAMF 多智能体 (Commander/DataAgent/MathAgent/Explainer)
│   ├── commander.py         # 意图路由+汇总
│   ├── data_agent.py        # 数据分析+特征计算
│   ├── math_agent.py        # 概率计算+三层降级
│   ├── explainer.py         # 中文解释
│   ├── model_bridge.py      # ModelBridge v2.0
│   ├── workflow.py          # LangGraph 工作流
│   └── scheduler.py         # Agent 调度器
├── backend/                 # FastAPI 后端服务
│   ├── api/v1/endpoints/    # API 路由 (v1 前缀)
│   │   ├── predictions.py   # 预测端点
│   │   ├── matches.py       # 比赛数据端点
│   │   ├── models.py        # 模型管理端点
│   │   ├── training.py      # 训练端点
│   │   ├── features.py      # 特征端点
│   │   ├── evaluation.py    # 评估端点
│   │   ├── alerts.py        # 告警端点
│   │   ├── admin.py         # 管理端点
│   │   ├── ab_test.py       # A/B 测试端点
│   │   ├── historical_data.py # 历史数据端点
│   │   └── monitor.py       # 监控端点
│   └── routers/             # 附加路由 (chat/fixtures/jepa/misc)
├── predictors/              # 预测引擎
├── pipeline/                # 预测管道
│   ├── full_linkage_predictor.py  # 全链路联动预测
│   ├── auto_pipeline.py     # 自动预测管道
│   └── knockout_predictor.py # 淘汰赛预测
├── rules/                   # 领域规则引擎
│   ├── d_gate_engine.py     # D-Gate 核心引擎
│   ├── d_gate_v52.py        # D-Gate v5.2 实现
│   ├── drawgate_v53.py      # DrawGate v5.3
│   └── domain_rules.py      # 领域规则
├── features/                # 特征工程 (90+ 维)
├── data_collector/          # 数据采集
├── database/                # 数据库管理
├── saved_models/            # 生产模型
├── data/                    # 数据文件
├── scripts/                 # 工具脚本
├── docs/                    # 文档
│   ├── adr/                 # 架构决策记录
│   ├── ARCHITECTURE.md      # 系统架构
│   ├── API_REFERENCE.md     # API 参考
│   └── ...                  # 其他文档
├── tests/                   # 测试
├── logs/                    # 日志
└── CHANGELOG.md             # 变更日志
```

---

## 3. 环境搭建

### 前置条件

- **Python 3.10+**（推荐 3.10.11）
- **Ollama**（可选，LLM Agent 需要）
  - [下载安装 Ollama](https://ollama.com)
  - 拉取所需模型:
    ```bash
    ollama pull gemma4:12b
    ollama pull deepseek-r1:8b
    ollama pull phi4:14b
    ollama pull qwen3:8b
    ```
- **Git** 用于版本管理

### 步骤

```bash
# 1. 克隆仓库
git clone <仓库地址>
cd Architecture

# 2. 创建虚拟环境
python -m venv .venv

# 3. 激活虚拟环境
# Windows:
.venv\Scripts\activate
# Linux/Mac:
source .venv/bin/activate

# 4. 安装依赖
pip install -r requirements.txt

# 5. 验证安装
python main.py --help
```

---

## 4. 启动指南

### 开发模式

```bash
# 启动 FastAPI 后端（开发模式，自动重载）
python main.py backend --dev

# 访问 API 文档
# http://localhost:8000/docs

# 自定义端口
python main.py backend --port 9000
```

### 预测管道

```bash
# 单次预测 + 回测
python main.py pipeline

# 守护模式（定时执行）
python main.py pipeline --daemon

# 仅回测
python main.py pipeline --backtest

# 准确率报告
python main.py pipeline --report
```

### 其他命令

```bash
python main.py predict   # 单次预测引擎
python main.py agent     # 交互式智能体对话
```

### Docker 部署

```bash
docker compose up -d     # 启动所有服务
docker compose logs -f   # 查看日志
```

---

## 5. 编码规范

### Python

- **Python 3.10+** 类型注解是强制的
- 使用 `pydantic` 模型进行数据校验
- 遵循 PEP 8 风格指南
- 使用 `black` 格式化（行宽 100）
- 使用 `isort` 管理导入顺序

### 命名规范

| 类型 | 规范 | 示例 |
|------|------|------|
| 模块/包 | snake_case | `full_linkage_predictor.py` |
| 类 | PascalCase | `UnifiedPredictor` |
| 函数/方法 | snake_case | `predict_match()` |
| 变量 | snake_case | `match_id` |
| 常量 | UPPER_SNAKE_CASE | `DEFAULT_PORT` |
| 私有 | `_` 前缀 | `_internal_helper()` |

### 文档注释

- 公共 API 使用 Google 风格 docstring
- 模块级文档放在文件头部
- 复杂逻辑需要添加行内注释

### 错误处理

- 使用自定义异常类 (继承 `Exception`)
- FastAPI 端点使用 HTTPException
- 关键路径使用 try/except 包裹

### 测试

- 新增功能需包含单元测试
- 测试文件位于 `tests/` 目录
- 使用 `pytest` 运行测试
- 测试命名: `test_<功能名>.py`

---

## 6. Git 工作流

### 分支策略

- `main`: 生产分支，保持稳定
- `feature/*`: 功能开发分支
- `fix/*`: 修复分支
- `release/*`: 发布分支

### Commit 规范

```
<类型>: <简短描述>

可选详细说明
```

类型前缀:
- `feat`: 新功能
- `fix`: 修复
- `docs`: 文档更新
- `refactor`: 重构
- `test`: 测试
- `chore`: 杂项

### 工作流程

1. 从 `main` 创建功能分支
2. 开发并本地测试
3. 提交代码（遵循 commit 规范）
4. 创建 Pull Request
5. 通过 CI 和 Code Review 后合并到 `main`

---

> 如有问题，联系团队架构师或查阅 `docs/` 目录下的详细文档。
