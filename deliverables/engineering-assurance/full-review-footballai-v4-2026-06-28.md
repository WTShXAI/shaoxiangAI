# FootballAI v4.0 系统全面工程审查报告（问题清单 — 去安全/性能版）

**日期**：2026-06-28
**工作流**：综合代码审查 + 技术债评估（工作流 1 + 工作流 5）
**参与成员**：Cody（代码审查师）、Archi（架构师）、Rex（SRE工程师）、Tessa（测试专家）、Docu（技术文档师）
**说明**：已按用户要求排除安全类与性能类问题（本地部署环境）

---

## 📌 TL;DR（执行摘要）

- **整体结论**：系统核心风险在**架构碎片化**——两套系统并行运行（顶层独立脚本 vs `backend/`），God File 堆积、代码多副本、sys.path 满天飞。加上测试基础设施近乎空白，维护成本随代码量线性增长。
- **总发现**：去重合并后 **44 项**问题（🔴严重 18 项 / 🟠高 10 项 / 🟡中 10 项 / 🟢低 6 项）
- **四大风险域**：(1) 架构碎片化（两套系统并行 + God File）(2) 测试与 CI/CD 缺失 (3) 可运维性空白（无容器/无备份）(4) 文档版本脱节
- **阻塞 / 非阻塞**：🔴 **18 项阻塞**（需架构决策或优先级修复），其余为中期改进项

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| 整体评级 | 🟡 有条件通过（核心推理逻辑 OK，工程组织需大修） |
| 阻塞项数量 | 18 |
| 关键行动项 | 5 条（见下文） |
| 建议下一步 | 架构评审会 → 选定收敛方向 → P0 修复 Sprint |

---

## 🔍 审查发现（去重合并，按全局严重度排序）

### 🔴 严重问题（18 项）

| # | 严重度 | 域 | 问题描述 | 涉及文件/范围 | 来源成员 | 建议修复方向 |
|---|--------|------|---------|-------------|---------|------------|
| 1 | 🔴P0 | **架构—双层** | **双层架构并行运行**。顶层独立脚本系统（`predictors/`、`pipeline/`、`features/`、`rules/`）与 `backend/` 新体系通过 `sys.path` 篡改和 WSGI 桥接勉强共存。相同功能有 2-4 份副本。 | `predictors/` + `pipeline/` + `features/` + `rules/` + `backend/` | Archi #2 | 选定一个收敛方向（推荐收敛到 `backend/`），废弃顶层独立脚本 |
| 2 | 🔴P0 | **架构—God File** | **`backend/main.py` 1585 行上帝文件**。包含 FastAPI 创建、生命周期、中间件、异常处理、4 个分析报告函数、2 个 SSE 端点、WebSocket、健康检查、Flask 挂载。业务逻辑与路由层完全混合。 | `backend/main.py`（1585行） | Archi #1 / Cody #10 / Rex #12 | 按职责拆分为 `app/factory.py`、`app/routers/chat.py`、`app/services/bookmaker.py` 等 |
| 3 | 🔴P0 | **架构—管线治理** | **三条推理管线无一致性治理**。UnifiedPredictor / SKYPredictor / VIPFinalPredictor 搜索相同模型文件但权重配置完全不同，无统一接口协议/ABC，输入格式不一致。无法保证预测结果一致性。 | `predictors/unified_predictor.py` `predictors/sky/sky_predictor.py` `predictors/vip/vip_final.py` | Archi #3 | 引入 `PredictorBase` ABC；三管线收敛为策略模式；增加 `ModelGovernance` 层 |
| 4 | 🔴P0 | **架构—代码重复** | **风控引擎 D-Gate 核心代码 4 份副本**。`rules/d_gate_engine.py` + `footballai-core/` 副本 + `d_gate_v52.py`(DEPRECATED 仍被引用) + `drawgate_v53.py`（被包装）。维护成本 4 倍+不一致风险。 | `rules/d_gate_engine.py` `rules/d_gate_v52.py` `rules/drawgate_v53.py` `footballai-core/footballai/rules/d_gate_engine.py` | Archi #4 / Cody #20 | 只保留 `rules/d_gate_engine.py`(统一外观) + `drawgate_v53.py`(核心引擎)，删除其他副本 |
| 5 | 🔴P0 | **架构—特征重复** | **特征工程模块重复**。顶层 `features/` 有 `feature_aligner.py`、`feature_calculator.py` 等；`backend/features/` 有 `unified_feature_pipeline.py`、`smart_feature_compressor.py` 等。两套系统并存导致导入路径遮蔽问题。 | `features/`（顶层）+ `backend/features/` | Archi #5 | 统一特征层：以 `features/feature_aligner.py` 为唯一入口，`backend/features/` 作为插件扩展或合并 |
| 6 | 🔴P1 | **架构—sys.path** | **8+ 处 sys.path.insert 篡改**。几乎所有预测器文件独立插入自己的 `sys.path`，导致导入顺序依赖、难以调试的遮蔽问题。`main.py:843-848` 甚至需临时移除 path 再恢复。 | 遍布 `predictors/`、`backend/main.py:843-848` | Archi #6 | 使用 `pyproject.toml` 统一包结构；所有导入用绝对包导入，废弃 sys.path 模式 |
| 7 | 🔴P1 | **架构—路由碎片** | **路由在 4 个地方注册**：(1) `router.py` APIRouter、(2) `main.py` 装饰器、(3) `api/ocr.py` 通过 `importlib` 动态加载、(4) Flask WSGI 作为"黑洞"捕获所有未匹配路由。 | `backend/main.py` `backend/api/v1/router.py` `api/ocr.py` `api/chat_api.py` | Archi #7 | 所有路由统一注册到 `backend/api/v1/router.py`；废弃 `api/ocr.py` 动态加载；关闭 Flask legacy |
| 8 | 🔴P1 | **架构—配置双系统** | **两套配置系统并行**。顶层用 PyYAML + `lru_cache` 加载 YAML 配置；`backend/` 用 Pydantic `BaseSettings`。`six_layer_conversation.py` 用 YAML，FastAPI 服务用 Pydantic。 | `config/settings.py` + `backend/core/config.py` + 各处引用 | Archi #9 | 统一为 Pydantic `BaseSettings` 单一真相来源 |
| 9 | 🔴Critical | **测试—CI/CD** | **零 CI/CD 配置**。无 `.github/`、`.gitlab-ci.yml`、`Jenkinsfile`。测试只能手动触发 `test.bat`，无法防止回归代码合入主线。 | 项目根 | Tessa #1 / Rex #6 | 配置 GitHub Actions 包含 lint → 测试 → 构建流水线 |
| 10 | 🔴Critical | **测试—覆盖率** | **31/38 模块零测试（覆盖率 18%）**。缺失测试的核心模块包括：D-Gate、degradation_guard、feedback_loop、six_layer_conversation、match_analyzer 等。仅有 1 个 1843 行单文件测试。 | `tests/test_v4_modules.py`（唯一测试文件） `modules/` 目录 | Tessa #2,#3,#7 | 按风险优先级创建单元测试；D-Gate 优先创建纯函数测试；拆分单文件测试 |
| 11 | 🔴Critical | **测试—自制框架** | **使用自制 `test()` 函数而非 pytest**。无 assertion introspection、无 fixture、无 parametrize、无 skip/xfail、无 JUnit XML 输出。测试运行环境通过 `sys.path.insert` 动态加载，与实际生产环境不一致。 | `tests/test_v4_modules.py L47-56` | Tessa #6,#11 | 迁移至 pytest：使用 `assert`、`@pytest.mark.parametrize`、`conftest.py` fixtures |
| 12 | 🔴SEV1 | **SRE—可部署性** | **无容器化支持**。无 Dockerfile、docker-compose.yml 或任何容器编排配置。部署完全依赖手动 `.bat` 脚本，环境一致性无保障。 | 项目根 | Rex #5 / Docu #5 | 编写 Dockerfile；添加 docker-compose.yml 编排后端+依赖服务 |
| 13 | 🔴SEV1 | **SRE—数据持久** | **507MB SQLite 数据库无备份策略**。无 WAL 模式、无复制、无灾备。模型文件无版本化存储，`model_registry.json` 文件引用了但磁盘上不存在。 | `data/football_data.db` `saved_models/` | Rex #10,#11 | 配置定期自动备份；启用 WAL 模式；修复 model_registry.json；实施模型版本化 |
| 14 | 🔴SEV1 | **SRE—可观测性** | **日志仅输出到控制台，未落盘轮转**。`settings.yaml` 定义了 `logs/app.log` 带 10MB 轮转但未被实际使用。Metrics exporter 降级到内存模式，Prometheus 端口可能从不提供 `/metrics`。 | `backend/main.py:42-46` `utils/observability.py` `utils/metrics_exporter.py` | Rex #7,#16 | 添加 RotatingFileHandler；启用结构化日志；验证 `/metrics` 端点可用性 |
| 15 | 🔴SEV1 | **SRE—后台静默** | **Flask Bridge 后台线程静默失败**。daemon 线程中执行 Flask 初始化，线程崩溃不影响主进程但也无法被监控检测到。服务不正常但健康检查仍返回 OK。 | `backend/main.py:105-108` | Rex #15 | 后台线程增加健康心跳报告机制；在健康检查端点中报告所有组件状态 |
| 16 | 🔴SEV1 | **SRE—线程安全** | **线程不安全的进程级模型缓存**。`_model`/`_model_path`/`_model_mtime` 类级属性在多请求并发时存在竞态条件。mtime 检查的 `OSError` 被 `except pass` 静默吞掉。 | `backend/services/prediction_service.py:111-123` | Cody #9 | 使用 `threading.Lock`；mtime 失败时记录 warning 而非静默忽略 |
| 17 | 🔴SEV1 | **SRE—依赖管理** | **requirements.txt 不完整**。缺少 `asyncio`、`aiosqlite`、`prometheus-client`、`python-jose`、`passlib`、`bcrypt`、`flask`、`a2wsgi` 等实际被 import 的包。换环境部署会因缺失依赖崩溃。 | `requirements.txt` | Cody #21 | 使用 `pip freeze > requirements.txt` 重新生成完整清单；分离生产/开发依赖 |

### 🟠 高优先级问题（10 项）

| # | 严重度 | 域 | 问题描述 | 涉及文件/范围 | 来源成员 | 建议修复方向 |
|---|--------|------|---------|-------------|---------|------------|
| 18 | 🟠High | 架构 | **6层架构 L6 从未开启**。`enable_l6=False` 在所有调用点硬编码，L6 自主优化层从未启用。`MatchContext` 设计良好但未全管线一致性使用。 | `modules/six_layer_conversation.py` `backend/main.py:921,1412` | Archi #8 | 要么完整实现 L6，要么移除；`MatchContext` 强制作为全管线唯一数据载体 |
| 19 | 🟠High | 架构 | **God File 之二**。`prediction_service.py` 1901 行中混合了模型加载、特征准备、VIP 预测、赔率引擎、陷阱检测、贝叶斯校准、收割防护、融合逻辑、比分预测、大小球分析 10+ 项职责。 | `backend/services/prediction_service.py`（1901行） | Cody #10 | 拆分为独立 service 模块（如 `report_generator.py`、`d_gate_fusion.py`） |
| 20 | 🟠High | 架构 | **变量作用域泄露**。`main.py` 末尾大量内联代码（分析卡构建、庄家报告）混合了 UI 层逻辑和领域逻辑。模块级全局变量 `_FIFA_RANKINGS` 等多个全局状态。 | `backend/main.py:206-736` | Archi #10 | 将分析卡和庄家报告迁移到 `backend/services/` |
| 21 | 🟠High | 测试 | **API 端点零测试**。14 个 API 端点（predictions、matches、training、auth、admin 等）无任何 HTTP 请求测试。当前测试仅条件式尝试 import V4PredictRequest。 | `backend/api/v1/endpoints/` | Tessa #3 | 使用 FastAPI TestClient 为每个端点创建集成测试 |
| 22 | 🟠High | 测试 | **D-Gate 风控引擎无单元测试**。`dgate_v50_classify()` 多层阈值逻辑（Mode A/B/C/Default）、S7+S1 信号计算，无任何输入输出断言验证。 | `pipeline/dgate_v50_backtest.py` | Tessa #4 | 创建 `tests/test_dgate.py`，覆盖所有分层模式和边界阈值 |
| 23 | 🟠High | 测试 | **无覆盖率测量**。无 `.coveragerc`、无 `coverage.py` 配置、无覆盖率目标。无法量化测试质量、识别死代码、设置 CI 门限。 | 项目根 | Tessa #8 | 配置 coverage.py + pytest-cov，设定 60%+ 覆盖率目标 |
| 24 | 🟠High | 文档 | **架构文档停留 v3.1**。描述 19 维特征、Flask 路由、8080 端口，实际系统 v4.1、90+ 维特征、FastAPI、8000 端口。目录结构完全过时。 | `docs/docs/ARCHITECTURE.md` | Docu #1 | 重写为 v4.1 架构描述 |
| 25 | 🟠High | 文档 | **README 文档链接全部断开**。链接指向 `docs/ARCHITECTURE.md`，实际文件在 `docs/docs/` 子目录（嵌套两层）。 | `README.md L196-203` | Docu #2 | 修复所有文档链接路径 |
| 26 | 🟠High | 文档 | **无任何 ADR（架构决策记录）**。关键决策（Flask→FastAPI、LAMF 多智能体、ModelBridge 锁定、三层降级）均无记录。 | 整个项目 | Docu #3 | 建立 `docs/adr/`，回溯记录 5-8 个关键架构决策 |
| 27 | 🟠High | 文档 | **API 参考文档过时（v3.0）**。引用端口 8080 和 Flask 路由 `/api/predict/next-match`，实际为 FastAPI `/api/v1/predict/next-match`，端口 8000。 | `docs/docs/API_REFERENCE.md` | Docu #4 | 更新为 v4.1 实际路由路径、端口、认证方式 |

### 🟡 中优先级问题（10 项）

| # | 严重度 | 域 | 问题描述 | 涉及文件/范围 | 来源成员 | 建议修复方向 |
|---|--------|------|---------|-------------|---------|------------|
| 28 | 🟡Med | 代码 | **numpy 类型未转换**。多处 ML 代码将 `numpy.int64`/`numpy.float64` 直接存 DB 或返回 JSON，Pydantic v2 无法自动序列化，靠外层 `default=str` 勉强绕过。 | `scripts/train_jepa.py` `models/jepa_losses.py` 等多处 | Cody #12 | 模型输出处统一调用 `.item()` 转换；或编写自定义 JSONEncoder |
| 29 | 🟡Med | 代码 | **15+ 处宽泛 `except Exception: pass`**。静默隐藏 `KeyError`、`TypeError`、`MemoryError` 等需要处理的异常，导致静默降级到错误路径。 | `prediction_service.py:714-724` `model_bridge.py:35` 等多处 | Cody #13 | 替换为具体异常类型；`except Exception` 至少 `logger.exception()` |
| 30 | 🟡Med | 代码 | **时区无关的 `datetime.now()`**。多处使用无时区的 `datetime.now()` 而非 `datetime.now(timezone.utc)`，DB 时间戳跨时区歧义。 | `backend/main.py` `prediction_service.py` `db_manager.py` 等多处 | Cody #14 | 统一使用 UTC：`datetime.now(timezone.utc)` |
| 31 | 🟡Med | 代码 | **批量预测端点遗漏异常分支**。只捕获了 `ValueError`/`KeyError`/`FileNotFoundError`，忽略了 `TypeError`/`AttributeError`/`RequestException`。 | `backend/api/v1/endpoints/predictions.py:138-154` | Cody #16 | 添加更全面的异常捕获或统一 `except Exception` + logger |
| 32 | 🟡Med | 架构 | **命名风格混合**。文件命名混合英文+中文拼音；函数签名混合驼峰+下划线；版本号在文件名中表明接口不稳定。 | 全项目 | Archi #11 | 统一为 PEP 8 下划线命名；用 git tag 代替文件名版本号 |
| 33 | 🟡Med | 架构 | **错误响应格式不统一**。SSE 端点 `{"type":"error"}`、JSON 端点 `{"success":false}`、全局异常 `{"detail":"..."}`，三种格式并存。 | `backend/main.py:738-1200` | Archi #12 | 统一错误响应格式 `{"error":{"code":"...","message":"..."}}` |
| 34 | 🟡Med | 架构 | **OCR 端点通过 importlib 动态加载**。绕过了正常包管理系统，IDE 无法检测、类型检查无效。 | `backend/main.py:184-197` `api/ocr.py` | Archi #13 | 将 OCR 路由作为正式模块加入 `router.py` |
| 35 | 🟡Med | 架构 | **Flask Legacy 仍挂载**。FastAPI 启动时通过 `WSGIMiddleware` 挂载 Flask 应用作为"黑洞"后备。旧后端从未真正下线。 | `backend/flask_bridge.py` `backend/main.py:1563-1573` | Archi #14 | 确认无流量依赖后完全移除 Flask WSGI 挂载 |
| 36 | 🟡Med | 架构 | **同级模块包名遮蔽**。`backend/features/` 在 sys.path 中遮蔽顶层 `features/`，需手动插入项目根路径绕过。 | `backend/api/v1/endpoints/features.py:14-19` | Archi #17 | 重命名其中一个包（如 `backend/features/`→`backend/feature_engineering/`） |
| 37 | 🟡Med | 文档 | **4 个目录碎片化 + 无入职指南 + 无 CHANGELOG**。文档分散在 `docs/docs/`(36)、`docs/`(2)、`knowledge_base/`(6)、`instructions/`(1)，无统一索引。 | 全项目 | Docu #6,#9,#14 | 在 README 建立完整文档索引 Map；创建 `ONBOARDING.md` 和 `CHANGELOG.md` |

### 🟢 低优先级问题（6 项）

| # | 严重度 | 域 | 问题描述 | 涉及文件/范围 | 来源成员 | 建议修复方向 |
|---|--------|------|---------|-------------|---------|------------|
| 38 | 🟢Low | 代码 | **大量存档代码留在仓库中**。`archive_backup/root_py/`(40+文件)、`pipeline/archive/`(15脚本) 等增加认知负载和代码库体积。 | `archive_backup/` `pipeline/archive/` `deliverables/archive/` | Cody #17 / Archi #16 | 清理归档目录；重要存档移至 git tag |
| 39 | 🟢Low | 代码 | **硬编码赛后分析脚本残留**。`eu_odds_analysis.py` 硬编码欧赔和相对路径 `joblib.load()`。分析原型代码残留在产品目录。 | `bookmaker_sim/eu_odds_analysis.py` | Cody #18 | 移至 `archive/` 或改造为通用工具 |
| 40 | 🟢Low | 代码 | **绕过 ModelBridge 的 joblib.load 直接调用**。两个文件虽有 DEPRECATED 注释但仍位于活跃目录可能被无意导入。 | `backend/models/footballai_enhanced.py` `backend/models/smart_integration.py` | Cody #19 | 移至 `archive/` 或在头部加 `raise DeprecationWarning` 防误用 |
| 41 | 🟢Low | 代码 | **目录职责不清晰**。`predictors/components/`、`agents/`、`knowledge_base/`、`modules/` 之间无清晰"领域/应用/基础设施"分层。 | `predictors/components/` `modules/` `agents/` | Archi #15 | 按 DDD 分层：`domain/`/`application/`/`infrastructure/`/`interfaces/` |
| 42 | 🟢Low | 文档 | **D-Gate v6.0 架构文档在 Python 三重引号字符串中**，非纯 Markdown，难以 diff/review。 | 多处（`V60_ARCHITECTURE` 变量） | Docu #13 | 将设计文档从 Python 字符串提取为独立 Markdown 文件 |
| 43 | 🟢Low | 运维 | **`.venv_314_backup/` 残留 + Prometheus 端口 9090 冲突**。旧 venv 未清理；`PROMETHEUS_PORT=9090` 与 Prometheus Server 默认端口冲突。 | 项目根 `backend/core/config.py:101` | Rex #18,#19 | 清理旧 venv；Prometheus 端口改 9091 |

---

## 🏗️ 架构影响评估（综合）

**核心矛盾**：系统经历了"原型→生产"的两次演变（Flask→FastAPI，单文件→模块化），但两次转换都未完成。当前「顶层独立脚本」和「backend/」两套体系通过 sys.path 篡改和 WSGI 桥接勉强共存，工程债务快速积累。

**四大风险域**：

```
🔴 架构碎片    ──── 两套系统并行 + 8+ 处 sys.path 篡改 + God File * 2
🔴 代码重复    ──── D-Gate 4 副本 + 特征工程 2 套 + 预测管线 3 条
🔴 测试缺失    ──── 零 CI/CD + 18% 模块覆盖 + 自制测试框架
🟠 可运维性    ──── 无容器 + 无备份 + 日志不落盘 + 依赖不完整 + 线程不安全
🟠 文档脱节    ──── 架构文档 v3.1（实际 v4.1）+ 链接全断开 + 无 ADR
```

**建议演进路径**：架构收敛 → 代码归一 → 测试基建 → 可运维性加固 → 文档补齐

---

## ✅ 行动清单（按优先级排序）

| # | 行动 | 负责角色 | 紧急度 | 预期完成 |
|---|------|---------|--------|---------|
| 1 | **架构评审会**：选定收敛方向（推荐 `backend/`），决议废弃/保留/迁移顶层独立脚本 | 工程负责人+架构师 | P0 | 本周 |
| 2 | **拆 God File**：`backend/main.py` 1585 行按职责拆为 factory/router/services 三层 | 架构师+后端 | P0 | 1周 |
| 3 | **统一 D-Gate 代码**：保留 `rules/d_gate_engine.py` + `drawgate_v53.py`，删其他副本 | 架构师 | P0 | 2天 |
| 4 | **统一特征层**：以 `features/feature_aligner.py` 为唯一入口，合并 `backend/features/` | 架构师+ML | P0 | 1周 |
| 5 | **废除 sys.path 模式**：配置 `pyproject.toml`，所有导入改用绝对包导入 | 架构师+后端 | P1 | 1周 |
| 6 | **配置 GitHub Actions CI**：包含 lint → 测试(junit.xml) → 覆盖率报告 | 测试+SRE | P1 | 3天 |
| 7 | **统一测试框架到 pytest**：迁移 `test_v4_modules.py` 到 pytest，拆分多文件 | 测试 | P1 | 1周 |
| 8 | **创建 D-Gate 单元测试**：覆盖 Mode A/B/C/Default、边界阈值、极端赔率场景 | 测试+ML | P1 | 3天 |
| 9 | **Docker 容器化**：编写 Dockerfile + docker-compose.yml（后端+数据库） | SRE | P1 | 3天 |
| 10 | **配置日志轮转**：启用 RotatingFileHandler + 结构化日志 JSON 格式 | 后端+SRE | P1 | 2天 |
| 11 | **配置数据库备份**：cron 定时 `sqlite3 .backup` + 启用 WAL 模式 | SRE | P1 | 1天 |
| 12 | **重写架构文档至 v4.1** + 修复 README 链接路径 | 技术文档师 | P1 | 2天 |
| 13 | **建立 ADR 记录**：回溯 5-8 个关键架构决策到 `docs/adr/` | 架构师+文档 | P2 | 1周 |
| 14 | **修正时区 + numpy 类型**：全项目统一 UTC 和 `.item()` | 后端+ML | P2 | 2天 |
| 15 | **处理宽泛异常捕获**：替换为具体异常类型 + 至少 `logger.exception()` | 后端 | P2 | 3天 |
| 16 | **配置模型版本化**：修复 `model_registry.json`；链接 MLflow | ML | P2 | 1周 |
| 17 | **清理归档代码**：`archive_backup/`、`pipeline/archive/`、废弃 venv | 工程 | P3 | 按需 |

---

## ⚠️ 待完善 / 已知局限

- **审查方式**：纯静态分析，未进行运行时验证或压力测试。
- **未覆盖领域**：前端（项目中无 `frontend/` 目录，`static/` 为空）。
- **依赖扫描**：未执行 `pip-audit` 或 Snyk 扫描，仅基于代码中 import 的分析。
- **数据库审查**：未深入分析 507MB SQLite 数据库的 schema 设计、查询性能、索引覆盖。
- **模型层面**：模型准确率、过拟合、特征重要性等 ML 专项审查未包含在此工程审查范围内。

---

## 📚 数据来源 & 成员产出索引

- **Cody（代码审查师）**：21 项 → 去安全/性能后保留 14 项（6🟡+3🟢 等）
- **Archi（架构师）**：18 项 → 全部保留（10🔴+6🟡+2🟢）
- **Rex（SRE工程师）**：19 项 → 去安全后保留 12 项（7🔴+...）
- **Tessa（测试专家）**：12 项 → 全部保留（5🔴+4🟠+3🟡）
- **Docu（技术文档师）**：15 项 → 全部保留（3🔴+5🟠+4🟡+3🟢）

---

> 本报告由工程保障团队 AI 协作生成（Cody + Archi + Rex + Tessa + Docu），已按用户要求排除安全与性能类问题。关键决策请由人类工程负责人复核。
