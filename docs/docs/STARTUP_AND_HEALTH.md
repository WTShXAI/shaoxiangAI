# 启动与健康检查指南 (LAMF v4.1.0)

> 更新: 2026-06-12 · 统一入口: `main.py` · 跨平台 (Windows / macOS / Linux)

---

## 一、前置条件

| 项 | 要求 | 验证命令 |
|----|------|---------|
| Python | 3.10+ | `python --version` |
| Ollama | 已安装并运行 | `curl http://localhost:11434/api/tags` |
| LLM 模型 | gemma4:12b, deepseek-r1:8b, phi4:14b, qwen3:8b | `ollama list` |
| 生产模型 | `saved_models/football_balanced_production.joblib` | `ls saved_models/` (或 `dir saved_models\`) |
| 数据库 | `data/football_data.db` | `ls data/` |

### 拉取 LLM 模型

```bash
ollama pull gemma4:12b
ollama pull deepseek-r1:8b
ollama pull phi4:14b
ollama pull qwen3:8b
```

---

## 二、安装依赖

### Windows

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 三、启动后端

```bash
# 开发模式（推荐，含热重载 + 免 JWT）
python main.py backend --dev

# 生产模式
python main.py backend

# 自定义端口
python main.py backend --port 9000
```

| 说明 | 值 |
|------|-----|
| 监听地址 | `http://0.0.0.0:8000` |
| Swagger 文档 | `http://localhost:8000/docs` |
| 开发模式 | `--dev` 启用热重载 + DEBUG 模式免 JWT 验证 |

### 其他子命令

```bash
python main.py pipeline              # 自动预测 + 回测管道
python main.py pipeline --daemon     # 守护模式 (定时执行)
python main.py pipeline --backtest   # 仅回测
python main.py pipeline --report     # 准确率报告
python main.py predict               # 单次预测引擎
python main.py agent                 # 交互式智能体对话
```

---

## 四、健康检查

| 端点 | 说明 | 期望 |
|------|------|------|
| `GET /` | 根路径 | 服务信息 (JSON) |
| `GET /docs` | Swagger UI | API 文档页面 |
| `GET /api/v1/monitor/health` | 健康检查 | `{"status": "ok"}` |

### 快速验证

```bash
# 服务是否启动
curl http://localhost:8000/

# 健康检查
curl http://localhost:8000/api/v1/monitor/health

# 模型是否正常加载 (命令行)
python -c "from agents.model_bridge import get_model_bridge; b = get_model_bridge(); print('OK:', b.model_name, '| 特征数:', len(b.feature_names))"
```

---

## 五、故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| `ModuleNotFoundError: fastapi` | 未安装依赖 | `pip install -r requirements.txt` |
| `ModelNotAvailableError` | `saved_models/football_balanced_production.joblib` 缺失 | 重新训练模型或从备份恢复 |
| `HardcodedProbabilityError` | 检测到硬编码概率 H=0.40/D=0.28/A=0.32 | 检查特征是否正确计算、模型是否正常加载 |
| Ollama 连接失败 | Ollama 服务未启动 | 启动 Ollama: `ollama serve` |
| LLM 模型未找到 | 未拉取对应模型 | `ollama pull <model>` |
| 端口被占用 | 8000 端口冲突 | `python main.py backend --port 9000` |
| `401 Unauthorized` | 生产模式需要 JWT 认证 | 使用 `--dev` 模式或先登录获取 token |
| `FileNotFoundError: football_kb.yaml` | DomainKB 文件缺失 | 确保 `rules/football_kb.yaml` 存在 (predict_match 已做 try-except 防护) |
| 预测结果始终相同 | 特征缺失值由默认值填充 | 检查数据源和 `config.yaml` 中的 `default_values` |

---

## 六、环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_HOST` | `0.0.0.0` | FastAPI 监听地址 |
| `API_PORT` | `8000` | FastAPI 端口 |
| `DEBUG` | - | 设为 `true` 启用开发模式 (免 JWT + 详细日志) |

### 配置示例 (`.env`)

```bash
DEBUG=true
API_PORT=8000
API_HOST=0.0.0.0
```

---

## 七、停止服务

按 `Ctrl+C` 即可停止 uvicorn。无状态服务，无需优雅关闭。

---

## 八、完整启动步骤

1. **确保 Ollama 运行**
   ```bash
   ollama serve
   ```

2. **确保模型文件存在**
   ```bash
   ls saved_models/football_balanced_production.joblib
   ```

3. **激活虚拟环境**
   - Windows: `.venv\Scripts\activate`
   - macOS/Linux: `source .venv/bin/activate`

4. **启动后端**
   ```bash
   python main.py backend --dev
   ```

5. **验证**
   ```bash
   curl http://localhost:8000/api/v1/monitor/health
   # → {"status": "ok"}
   ```

---

## 九、环境依赖总览

| 类别 | 组件 | 是否必须 |
|------|------|---------|
| Python 包 | fastapi, uvicorn, joblib, numpy, scikit-learn, xgboost | ✅ 必须 |
| Python 包 | langgraph, langchain-ollama | ✅ 必须 (Agent 工作流) |
| Python 包 | yaml, requests, pandas | ✅ 必须 |
| 外部服务 | Ollama (http://localhost:11434) | ✅ 必须 (LLM 推理) |
| 外部服务 | Celery, Redis, MLflow, Grafana, Prometheus | ❌ 不需要 (已移除) |
| 数据库 | MySQL, PostgreSQL | ❌ 不需要 (仅 SQLite) |
| 前端构建 | Node.js, npm, Vue CLI | ❌ 不需要 (纯静态 SPA) |
