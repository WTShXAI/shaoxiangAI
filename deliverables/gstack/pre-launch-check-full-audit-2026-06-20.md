# 哨响AI v4.0 上线前全检报告

**日期**：2026-06-20
**场景**：上线前检查（代码审查 + 功能审计 + QA测试 + 安全审计）
**参与成员**：产品官 + 安全卫士 + 质量门神 + 排障手

---

## 📌 TL;DR（执行摘要）

- 整体结论：🔴 **NO-GO — 不具备上线条件**
- 四位成员一致建议 NO-GO（安全卫士 2/10、产品官 5/10、排障手 4.5/10、质量门神 55/100 条件性 NO-GO）
- 阻塞项数量：**18 项 P0**（跨安全、代码、功能、测试、架构、ML 六个维度）
- **架构级根因（终版）**：`ensemble_trainer.py` 源码文件在整个项目中不存在（仅有 `.pyc` 缓存），23 个文件 import 它 → 后端 API 路径永久降级为 `_legacy_load()` → L2 DrawExpert + L3 Stacking 融合 + L5 NN 神经网络全部缺失 → **v4.0 六层架构在后端 API 路径中实际只有 L0 + 部分 L1 在工作**
- **DrawExpert 四层根因定案（QA+产品官四轮交叉验证终版）**：**L0 代码层**——`unified_predictor.py:565` 硬编码 `de_signal = np.array([0.33, 0.34, 0.33])`，20% 权重注入恒定 P(D)=0.34 → 每场冷启动预测被注入 0.068 恒定平局偏置，**不是模型退化而是代码写死的假信号**，让下游误以为三信号融合在工作；**L1 配置层**——`config/config.yaml:603` model_path 指向 v3.2 旧模型，升级时未更新；**L2 模型层**——DrawExpert F1=0.0，`draw_expert_v1.joblib` 文件路径（L4门控）异常被 except 吞掉；**L3 架构层**——`ensemble_trainer.py` 源码缺失（仅 .pyc），ModelBridge 依赖隐式导入顺序。三层必须同时修复。L0+L1 可几分钟内完成，立即将系统从"注入假信号"变为"透明降级"
- **三路径三模型对象（比双路径更复杂）**：冷启动路径（EnsembleTrainer 内嵌模型，72维特征）/ L4门控路径（draw_expert_v1.joblib 文件，5维赔率特征）/ ModelBridge 路径（v3.2 模型 _last_submodel_probas，N/A）——三个 DrawExpert 入口，全部失效
- **模型版本分裂（QA 新发现）**：ModelBridge 加载 v3.2（`football_balanced_production.joblib`），UnifiedPredictor 加载 v4.1（`football_v4.1_production.joblib`）——两条路径使用不同版本模型，预测结果可能不一致
- 架构级发现：三通道架构实际为单通道——SKY 和 VIP 两通道完全失效，六层引擎"多专家共识"从未生效
- 模型红旗：MCC=0（=随机猜测），DrawExpert F1=0.0，D-Gate 静默降级为二信号
- 安全补充：SECRET_KEY 明文硬编码在 `start_server.bat`，且绑定 `0.0.0.0` 所有接口
- 下一步：修复全部 P0 阻塞项后重新执行上线前检查；预估修复工作量 5-7 天

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🔴 No-Go |
| 严重度分布 | 🔴 18 / 🟠 15 / 🟡 11 / 🟢 9 |
| 关键行动项 | 18 条 P0 |
| 建议负责人 | 安全工程 + 后端工程 + ML 工程 + QA |

---

## 1. 各成员核心结论

### 🔍 产品官（代码审查）
- 核心判断：代码质量 5/10，架构设计有想法但工程规范严重不足。5 个 Critical 问题中有 3 个安全漏洞、1 个运行时崩溃 bug、1 个性能隐患。**终版追踪到 `ensemble_trainer.py` 源码缺失这一架构级根因**——23 个文件 import 它但项目中仅有 .pyc 缓存，导致后端 API 路径六层架构中 L2/L3/L5 全部缺失，v4.0 核心差异化能力从未在后端上线。
- 关键建议：最小修复路径为 C1-C5 + M7（SSL），预计 1-2 天达 Conditional Go，完整修复 Major 需额外 3-5 天。恢复 `ensemble_trainer.py` 源码到项目内是解决跨项目依赖、三通道空壳、DrawExpert 无效的统一根因修复。

### 🛡️ 安全卫士（OWASP+STRIDE 审计）
- 核心判断：安全评分 2/10（F 级），"极其危险，不具备上线条件"。认证系统被完全禁用是根因——所有 API 端点含管理操作均无保护。OCR 凭据硬编码在源码中构成密钥泄露。
- 关键建议：Sprint 0 立即恢复认证系统（F-001）、轮换并移除硬编码 OCR 凭据（F-002）、实现速率限制（F-003）、确保管理端点认证（F-004）。STRIDE 六类威胁中五类存在 Critical 级未缓解场景。

### ✅ 质量门神（QA测试与发布）
- 核心判断：健康评分 55/100，Conditional NO-GO。核心预测管线（UnifiedPredictor + D-Gate）功能正常可产出有效预测，但三通道架构中 SKY 和 VIP 两个通道完全失效。交叉验证后修正了 `/ws/health` 误报（WebSocket 端点正常工作），升级 `/api/v1/models` 为 HIGH（整个模型管理 API 模块未实现），定位 DrawExpert 根因（模型文件为 dict 包装器非模型对象，建议升级 CRITICAL）。**终版二次深度验证**发现 DrawExpert 双路径失效机制（Chat 路径 F1=0.0 噪声信号 / ModelBridge 路径 v3.2 模型无 draw_expert 支持恒返回 None），并修正了产品官关于 import 失败的分析（运行服务器中 import 成功，因隐式导入顺序依赖）。
- 关键建议：修复 SKY 键名映射（1 行代码）、复制 trap_probability_bridge.py 到项目内、修复 DrawExpert 模型文件或更新文档、消除跨项目依赖。统一 ModelBridge 和 UnifiedPredictor 使用 v4.1 模型消除版本分裂。移除 prediction_service.py 三信号融合死代码。D-Gate 静默降级为二信号（Heuristic+OE）而非设计的三信号，需添加告警。可降级运行（仅 UnifiedPredictor + D-Gate）。

### 🔧 排障手（调试与根因）
- 核心判断：综合健康度 4.5/10，NO-GO。最初发现 4 个阻断性问题，后交叉验证 QA 发现并升级为 6 个阻断性问题。最严重发现：三通道架构实际为单通道——SKY 键名映射错误 + VIP 函数未导入 + VIP 即使修复 NameError 仍有键名映射 bug，六层引擎"多专家共识"从未生效，只有 UnifiedPredictor 在工作。
- 关键建议：D-Gate `spread < 1.6` 在概率尺度（0~1）上永远为 True 导致模式B 无条件触发，必须改为 `spread < 0.16`。模块包遮蔽问题（footballAI modules/ 遮蔽 v4.0 modules/）需通过包结构重组解决。模型 MCC=0 是严重红旗，需 ML 团队核实。

---

## 2. 综合审查发现（去重合并后按严重度排序）

> 去重说明：安全卫士 F-002 与产品官 C1 为同一问题（OCR 密钥硬编码）；安全卫士 F-001 与产品官 C2 为同一问题（认证禁用）；安全卫士 F-017 与产品官 M7 为同一问题（SSL 禁用）；安全卫士 F-018 与排障手阻断问题4 为同一问题（硬编码路径）；安全卫士 F-019（SECRET_KEY 硬编码 start_server.bat）为独立发现；产品官 C4 NameError 与质量门神测试结果互为印证；排障手阻断问题3 与质量门神 ISSUE-004 为同一问题（测试编码崩溃）；排障手交叉验证 QA 发现并升级——三通道失效从功能问题升级为架构级阻断；QA 交叉验证修正 /ws/health 误报（WebSocket 端点正常）、升级 /api/v1/models 为 HIGH（整个模块未实现）、定位 DrawExpert 根因（dict 包装器非模型）；QA 二次深度验证发现 DrawExpert 双路径失效机制 + 模型版本分裂（v3.2 vs v4.1），修正产品官关于 import 失败的分析（运行时 import 成功，因隐式导入顺序依赖）。去重后共 53 项独立发现。

### 🔴 Critical / P0（18 项 — 阻塞上线）

> **🔴 统一根因（产品官终版追踪）**：以下 #8（硬编码路径）、#11（三通道空壳）、#14（DrawExpert 无效）均源于同一根因——`ensemble_trainer.py` 源码文件缺失。23 个文件 import 它但项目中仅有 `.pyc` 编译缓存。后端 API 路径 `model_bridge.py:105` → ModuleNotFoundError → 降级到 `_legacy_load()` → 不加载 DrawExpert/NN/Stacking → **v4.0 六层架构在后端 API 路径中实际只有 L0 + 部分 L1 工作**。这不是 bug，是**源码依赖管理彻底失败**。

| # | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|------|------|---------|------|------|
| 1 | 安全 | `backend/core/security.py:101-117` | 认证系统完全禁用，`get_current_user()` 无条件返回 admin，所有 API 端点无保护 | 恢复 JWT 验证、密码验证、角色检查、默认用户初始化 | 安全+产品 |
| 2 | 安全 | `api/ocr.py:17-18` | 火山引擎 AK/SK 硬编码在源码中，密钥泄露 | 立即轮换密钥，移至环境变量 `os.getenv()` | 安全+产品 |
| 3 | 安全 | `backend/core/config.py:67` | 速率限制配置存在但无中间件实现，全系统无限流 | 安装 slowapi，全局 100/min + 敏感端点 10/min | 安全 |
| 4 | 安全 | `backend/api/v1/endpoints/admin.py:31-93`, `training.py:38-52` | 管理端点（重启服务、触发训练、清缓存）因认证禁用可被任意访问 | 恢复认证后确保 admin 角色强制检查 | 安全 |
| 5 | 安全 | `start_server.bat:3` | **SECRET_KEY 明文硬编码**在启动脚本中，且 `--host 0.0.0.0` 绑定所有网络接口。脚本还使用 footballAI 的 venv（`d:\AI\footballAI\.venv`）非独立环境 | 移至环境变量/.env，限制绑定地址，使用项目独立 venv | 安全+QA |
| 6 | 代码 | `backend/main.py:1120` | WebSocket 健康端点 `asyncio.sleep(30)` 使用了未定义的 `asyncio`（导入别名为 `_asyncio`），NameError 导致无限错误循环 | 改为 `_asyncio.sleep(30)` | 产品 |
| 7 | 代码 | `api/ocr.py:72` | async 端点中使用同步 `requests.post()`，阻塞事件循环 15 秒 | 替换为 `httpx.AsyncClient` | 产品 |
| 8 | 代码 | `predictors/unified_predictor.py:47` 等 11 个文件 | 硬编码 `D:\AI\footballAI` 绝对路径，部署到其他机器必然失败 | 改用 `settings.PROJECT_ROOT` 或环境变量 | 产品+安全+排障 |
| 9 | 功能 | `backend/main.py:586-611` | D-Gate 模式B `spread < 1.6` 中 spread 为概率差（0~1 范围），永远为 True，模式B 无条件触发 | 改为 `spread < 0.16` 或使用赔率差 | 排障 |
| 10 | 功能 | Unified vs VIP 管线 | VIP 使用 `football_balanced_production.joblib`（5.21MB），Unified/SKY 使用 `football_v4.1_production.joblib`（4.57MB），三管线模型不一致 | 统一 config.yaml 模型路径 | 排障 |
| 11 | 架构 | `six_layer_conversation.py:822`, `sky_predictor.py:174` | **三通道架构空壳**：SKY 返回 `proba_final` 但引擎查找 `probabilities`/`probs`，SKY 通道始终返回 0.000。VIP 通道 `apply_trap_correction()`/`apply_goal_segment_correction()` 函数未导入报 NameError。VIP 即使修复 NameError 后 L697 返回 `probs` 但引擎 L842 查找 `probabilities`/`prediction` 仍有键名不匹配。六层引擎"多专家共识"从未生效，实际只有 UnifiedPredictor 单通道工作 | 修复 SKY/VIP 键名映射 + 复制 trap_probability_bridge.py 和 odds_inverse_calibrator.py 到项目内 | QA+排障 |
| 12 | 架构 | footballAI `modules/` vs v4.0 `modules/` | **模块包遮蔽**：所有预测器执行 `sys.path.insert(0, FOOTBALLAI_ROOT)` 后，footballAI 的 `modules/__init__.py` 被优先加载，遮蔽 v4.0 的 modules 包，导致 `from modules.feedback_loop import ...` 等导入失败。main.py L723-727 的 sys.path 操作试图规避但未解决此问题 | 重组包结构，消除 sys.path 操作，pip install -e . | 排障 |
| 13 | 功能 | VIP 管线依赖 | `trap_probability_bridge.py` 和 `odds_inverse_calibrator.py` 仅存在于 `D:\AI\footballAI`，VIP 通道完全失效 | 复制文件到项目内 | QA+排障 |
| 14 | ML | DrawExpert 模型 | **DrawExpert 四层根因定案（QA+产品官四轮交叉验证终版）**：**L0 代码**——`unified_predictor.py:565` 硬编码 `de_signal = np.array([0.33, 0.34, 0.33])`，line 572 融合公式 `proba = proba_xgb_raw * 0.45 + proba_lgb_raw * 0.35 + de_signal * 0.20`，20% 权重注入恒定 P(D)=0.34 → 每场冷启动预测被注入 0.068 恒定平局偏置，**不是模型退化而是代码写死的假信号**，让下游误以为三信号融合在工作，无 else 降级日志；**L1 配置**——`config/config.yaml:603` model_path 指向 v3.2 旧模型，升级时未更新，ModelBridge 加载 v3.2 无 draw_expert 子模型，`get_de_output()` 恒返回 None，`prediction_service.py:523` 三信号融合代码为死代码；**L2 模型**——DrawExpert F1=0.0，`draw_expert_v1.joblib` 文件路径（L4门控 line 371-383）异常被 except 吞掉；**L3 架构**——`ensemble_trainer.py` 源码缺失（仅 .pyc），ModelBridge import 依赖隐式导入顺序。**三路径三模型对象**：冷启动（EnsembleTrainer 内嵌模型，72维特征）/ L4门控（draw_expert_v1.joblib，5维赔率特征）/ ModelBridge（v3.2 _last_submodel_probas，N/A）——三个入口全部失效。四层必须同时修复：只修 L0→L1/L3 仍有版本分裂；只修 L1→L0 仍注入假信号；只修 L3→仍加载 v3.2；只修 L2→L0 硬编码仍在 | **L0**: line 565 改为中性值或权重重分配 + 降级日志（一行+日志，最高性价比）**L1**: 更新 config.yaml model_path 为 v4.1（一行）**L2**: 重新训练 DrawExpert 使 F1 > 0（重训管线）**L3**: 复制 ensemble_trainer.py 源码到项目内 + 显式 import（文件复制）| QA+产品 |
| 15 | 测试 | `tests/test_v4_modules.py:44` | 测试套件 457 个用例因 Windows GBK 编码崩溃（✓/✗ 字符无法输出），实际 0 个测试可验证 | 添加 `sys.stdout.reconfigure(encoding='utf-8')` | 排障+QA |
| 16 | ML | `model_registry.json` vs 文档 | **模型指标红旗**：MCC=0（=随机猜测），accuracy=62.07% vs 声称 62.43%，AUC=0.8068 vs 声称 0.815，draw_f1=0.4913 vs 声称 0.520。MCC=0 意味着模型预测与随机猜测无相关性区别 | ML 团队核实模型指标，重新评估模型质量 | 排障+QA |
| 17 | 架构 | `predictors/components/__pycache__/ensemble_trainer.cpython-311.pyc`（仅 .pyc，无 .py 源文件） | **`ensemble_trainer.py` 源码缺失——六层架构后端路径系统性降级**：23 个文件 import `ensemble_trainer`，但项目中仅有编译缓存无源文件。**QA 修正**：运行服务器中 import 实际成功（因 unified_predictor 先被导入并添加 `D:\AI\footballAI` 到 sys.path，隐式导入顺序依赖），但 ModelBridge 加载 v3.2 模型无 draw_expert 支持 → DrawExpert 仍失效。核心结论不变：v4.0 六层架构在后端 API 路径 L2/L3/L5 从未上线。此问题统一了 #8（硬编码路径）、#11（三通道空壳）、#14（DrawExpert 无效）三个独立发现 | 1. 恢复 `ensemble_trainer.py` 源码到项目内  2. model_bridge.py 改为项目内相对导入  3. 消除隐式导入顺序依赖  4. `prediction_service.py` 添加降级告警 | 产品+QA |
| 18 | 架构 | ModelBridge vs UnifiedPredictor | **模型版本分裂（QA 新发现，根因已定位）**：ModelBridge 加载 v3.2 旧模型 `football_balanced_production.joblib`（5.21MB），UnifiedPredictor 加载 v4.1 `football_v4.1_production.joblib`（4.57MB）。**根因**：`config/config.yaml:603` model_path 指向 v3.2，升级到 v4.1 时未更新配置。两条代码路径使用不同版本模型，预测结果可能不一致。隐式导入顺序依赖使 ModelBridge 的成功依赖 unified_predictor 先被导入——重构会静默断裂 | 统一 config.yaml model_path 为 v4.1（L1 修复，一行），消除版本分裂，消除隐式导入顺序依赖 | QA |

### 🟠 High / P1（15 项 — 上线前应修复）

| # | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|------|------|---------|------|------|
| 17 | 安全 | `backend/main.py:109-115` | CORS `allow_credentials=True` + `allow_methods=["*"]` + `allow_headers=["*"]` 危险组合 | 限制为实际使用的方法和头 | 安全 |
| 18 | 安全 | `api/ocr.py:51-105` | OCR 端点无文件大小限制（对比 predict/image 有 10MB） | 添加 10MB 限制 | 安全 |
| 19 | 安全 | 项目根目录 | 无 `.gitignore` 文件，密钥/配置/数据库可能泄露到版本控制 | 创建 `.gitignore`，清理 git 历史 | 安全 |
| 20 | 安全 | `backend/main.py:138-141` 等 | DEBUG 模式下错误信息泄露 `str(exc)` + 请求路径 + HTML 堆栈跟踪 | 生产环境统一返回通用错误消息 | 安全 |
| 21 | 安全 | `backend/main.py:708-958` | SSE Chat 端点无认证、无消息大小限制 | 添加认证，限制消息最大 5000 字符 | 安全 |
| 22 | 安全 | `backend/api/v1/endpoints/matches.py:70` | SSL 证书验证禁用 `verify=False` + 主动抑制 InsecureRequestWarning | 移除 `verify=False`，恢复默认 SSL 验证 | 安全+产品 |
| 23 | 代码 | `backend/main.py:708-958` | `chat_endpoint` 函数 250 行，混合请求解析/引擎调用/D-Gate/陷阱检测/SSE 输出 | 拆分为独立函数 | 产品 |
| 24 | 代码 | 全项目 65+ 文件 | sys.path 操作泛滥，请求处理中动态修改 sys.path 存在并发竞态 | 组织为正规 Python 包，pip install -e . | 产品+排障 |
| 25 | 代码 | `backend/models/unified_predictor.py:271,352,366` | DEPRECATED 文件中引用未导入的 `sqlite3.Error`/`sqlalchemy.exc.SQLAlchemyError` | 删除该文件或添加 import | 产品 |
| 26 | 代码 | D-Gate 三处实现 | D-Gate 双模式逻辑在 `_build_analysis_card`、`chat_endpoint`、`six_layer_conversation` 三处独立实现且不一致 | 抽取为 `modules/d_gate_detector.py` | 产品+排障 |
| 27 | 功能 | DrawExpert 静默降级 | D-Gate 因 DrawExpert 失效仅靠 Heuristic+OE 双信号工作，非设计预期的三信号融合。**静默降级无任何告警**，用户看到"三信号融合"承诺但实际仅二信号 | 添加 DrawExpert 不可用告警，或修正文档承诺 | QA |
| 28 | 功能 | 模型指标 | 存储 accuracy=67.8% vs 声称 OOF=62.43%，DrawExpert F1=0.0 vs 声称 0.520，MCC=0.0 | 核实模型指标，更新文档 | QA |
| 29 | 功能 | D-Gate ou_line 处理 | `_build_analysis_card` 要求 ou_line 已知且 ≤2.5，`chat_endpoint` 在 ou_line 未知时也触发，逻辑相反 | 统一 ou_line None 处理 | 排障 |
| 30 | 功能 | 冷启动降级 | chat_endpoint 检查全零概率，UnifiedPredictor fallback 返回非零概率，降级路径可能失效 | 统一冷启动检测条件 | 排障 |
| 31 | 功能 | `backend/api/v1/endpoints/` | **模型管理 API 模块未实现**：`docs/OPERATIONS.md` 文档了 6 个子端点（versions/best/compare/deploy/rollback/auto-promote），但 endpoints/ 下无 models 相关 router。路由检查确认 `/api/v1` 下仅 3 个业务端点 | 实现模型管理 API 或从文档移除承诺 | QA |

### 🟡 Medium / P2（11 项）

| # | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|------|------|---------|------|------|
| 32 | 安全 | `compute_otsm_features.py` 等 | 管道脚本 f-string SQL 拼接（列名非用户输入但模式不安全） | 参数化查询或白名单校验 | 安全 |
| 33 | 安全 | `api/ocr.py:53`, `backend/main.py:975` | 文件上传仅校验 Content-Type 不校验 magic bytes | 使用 python-magic 验证文件头 | 安全 |
| 34 | 安全 | 全局 | 无安全审计日志，管理操作以通用 admin 身份记录 | 恢复认证后记录安全事件含真实用户身份 | 安全 |
| 35 | 安全 | Flask legacy | Flask legacy 文件缺失（`archive/prediction_service_flask_legacy.py`），可能导致 legacy 路由安全控制盲区 | 确认 legacy 路由是否需要保留，补全或移除 | 安全+QA |
| 36 | 代码 | 6+ 文件 | bare `except:` 捕获所有异常含 SystemExit/KeyboardInterrupt | 替换为 `except Exception:` | 产品+排障 |
| 37 | 代码 | `predictors/` 4 个文件 | λ融合函数 80 行四重复制 | 抽取为共享模块 | 排障 |
| 38 | 代码 | `backend/main.py` 10+ 处 | 赔率反推逻辑 `inv_sum = 1/oh + 1/od + 1/oa` 重复 10+ 次 | 抽取为工具函数 | 排障 |
| 39 | 代码 | 9 个函数超 150 行 | `_generate_report` 348行、`VIPFinalPredictor.predict` 341行、`chat_endpoint` 250行等 | 拆分为小函数 | 排障 |
| 40 | 代码 | `predictors/vip_predictor.py` + `vip_2_predictor.py` | ~60KB 疑似死代码，已被 vip_final.py 替代 | 删除或归档 | 排障 |
| 41 | 代码 | 多处 | 版本号不一致：main.py "v5.0"、config.py "4.1.0"、conversation "v4.0"、predictor "v4.1" | 统一使用 settings.APP_VERSION | 产品 |
| 42 | 测试 | `backend.main` 导入 | 需 SECRET_KEY 环境变量（≥32字符），无 .env 文件 | 创建 .env.example 模板 | QA |

> ✅ **已修正**：`/ws/health` 原报告为 404 缺陷，经交叉验证为 WebSocket 端点（非 HTTP GET），`ws://localhost:8000/ws/health` 正常返回 `{"type":"health_update","health":"watching","trend":"stable"}`，已从问题清单移除。

### 🟢 Low / P3（9 项）

| # | 类别 | 位置 | 问题描述 | 建议 | 来源 |
|---|------|------|---------|------|------|
| 43 | 安全 | `backend/main.py:1098-1122` | WebSocket 健康端点无认证 | 添加 token query 参数验证 | 安全 |
| 44 | 安全 | 全局 | 无安全 HTTP 头（CSP/HSTS/X-Frame-Options 等） | 添加安全头中间件 | 安全 |
| 45 | 安全 | `backend/main.py:717` | 日志中用户输入未消毒，可能日志注入 | 移除换行符等控制字符 | 安全 |
| 46 | 安全 | `backend/main.py:100-102` | Swagger/OpenAPI 文档在生产暴露 | 生产设置 docs_url=None | 安全 |
| 47 | 代码 | 多处 | 大量魔法数字未外部化（D-Gate 阈值、margin 常量等） | 移至 config/settings.yaml | 产品 |
| 48 | 代码 | `six_layer_conversation.py:604,646` | `explain_keywords` 重复定义且内容略有不同 | 删除重复定义 | 产品 |
| 49 | 代码 | `predictors/unified_predictor.py:42` | `warnings.filterwarnings('ignore')` 全局禁用警告 | 针对性过滤 FutureWarning | 产品 |
| 50 | 代码 | 3 处 | `logging.basicConfig()` 多次调用，只有首次生效 | 仅入口文件调用一次 | 产品 |
| 51 | 代码 | `predictors/unified_predictor.py` | 三线预测器串行调用，可用 asyncio.gather 并行化 | 性能优化 | 产品 |

---

## ✅ 行动清单

### P0 — 阻塞项（必须修复后方可上线）

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 1 | 恢复认证系统：JWT 验证 + 密码验证 + 角色检查 + 默认用户初始化 | 后端工程 | P0 | Day 1 |
| 2 | 轮换火山引擎 AK/SK，移至环境变量，清理 git 历史 | 安全工程 | P0 | Day 1 |
| 3 | 实现 slowapi 速率限制中间件（全局 100/min + 敏感端点 10/min） | 后端工程 | P0 | Day 1 |
| 4 | 移除 `start_server.bat` 中硬编码的 SECRET_KEY，移至 .env；限制绑定地址；使用项目独立 venv | 安全工程 | P0 | Day 1 |
| 5 | 修复 `asyncio` → `_asyncio` NameError（backend/main.py:1120） | 后端工程 | P0 | Day 1 |
| 6 | `requests.post` → `httpx.AsyncClient`（api/ocr.py:72） | 后端工程 | P0 | Day 1 |
| 7 | 移除所有 `D:\AI\footballAI` 硬编码路径，改用环境变量/相对路径 | 后端工程 | P0 | Day 2 |
| 8 | 修复 D-Gate 模式B `spread < 1.6` → `spread < 0.16` 或改用赔率差 | 后端工程 | P0 | Day 1 |
| 9 | 统一三管线模型路径，VIP 使用 `football_v4.1_production.joblib` | 后端工程 | P0 | Day 1 |
| 10 | 修复 SKY 通道键名映射 `proba_final` → `probabilities`（1 行代码） | 后端工程 | P0 | Day 1 |
| 11 | 复制 `trap_probability_bridge.py` + `odds_inverse_calibrator.py` 到项目内，消除 VIP 通道 NameError | 后端工程 | P0 | Day 1 |
| 12 | 修复 VIP 通道键名映射 `probs` → `probabilities`（L697 vs L842） | 后端工程 | P0 | Day 1 |
| 13 | 重组包结构消除模块遮蔽（footballAI modules/ vs v4.0 modules/），pip install -e . | 后端工程 | P0 | Day 2 |
| 14 | **DrawExpert L0 修复（最高性价比）**：`unified_predictor.py:565` 硬编码 `de_signal = np.array([0.33, 0.34, 0.33])` 改为中性值（如 `[0.33, 0.34, 0.33]` → 等权 `[1/3, 1/3, 1/3]` 或权重重分配给 XGB+LGB）+ 添加 else 降级日志。立即消除 0.068 恒定平局偏置，将系统从"注入假信号"变为"透明降级"。几分钟内完成 | 后端工程 | P0 | Day 1 |
| 15 | **DrawExpert L2 修复**：重新训练 DrawExpert 模型消除常数 0.34 偏置（根因已定位为 `unified_predictor.py:565` 硬编码 + `draw_expert_v1.joblib` F1=0.0 异常被 except 吞掉），使 F1 > 0 | ML 工程 | P0 | Day 2 |
| 15 | 修复测试套件编码崩溃，添加 `PYTHONUTF8=1` 或 reconfigure stdout | QA | P0 | Day 1 |
| 16 | 核实模型 MCC=0 根因，重新评估模型质量，更新文档指标 | ML 工程 | P0 | Day 2 |
| 17 | **DrawExpert L3 修复**：恢复 `ensemble_trainer.py` 源码到项目内（从 `D:\AI\footballAI\` 复制或 .pyc 反编译），model_bridge.py 改为项目内相对导入，消除隐式导入顺序依赖，`_legacy_load()` 添加 DrawExpert 加载逻辑，`prediction_service.py` 添加降级告警。此修复统一解决 #7（硬编码路径）、#10-12（三通道空壳）、#14（DrawExpert 无效） | 后端工程 + ML 工程 | P0 | Day 2-3 |
| 18 | **DrawExpert L1 修复 + 模型版本统一**：更新 `config/config.yaml:603` model_path 从 v3.2（`football_balanced_production.joblib`）切换为 v4.1（`football_v4.1_production.joblib`），消除版本分裂（一行配置）；移除 `prediction_service.py` 三信号融合死代码（de_pdraw 恒为 None） | 后端工程 + ML 工程 | P0 | Day 1 |

### P1 — 上线前应修复

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 17 | 修复 CORS 配置（限制 methods/headers） | 后端工程 | P1 | Day 3 |
| 18 | OCR 端点添加 10MB 文件大小限制 | 后端工程 | P1 | Day 3 |
| 19 | 创建 `.gitignore`（.env, *.db, __pycache__/, saved_models/, data/） | 安全工程 | P1 | Day 2 |
| 20 | 移除 SSL `verify=False`，恢复证书验证 | 后端工程 | P1 | Day 2 |
| 21 | 生产环境关闭 Swagger/Redoc，统一通用错误消息 | 后端工程 | P1 | Day 3 |
| 22 | 统一 D-Gate 三处实现为独立模块 `modules/d_gate_detector.py` | 后端工程 | P1 | Day 3 |
| 23 | 创建 `.env.example` 模板，文档化必需环境变量 | DevOps | P1 | Day 2 |
| 24 | 添加 DrawExpert 不可用告警机制，或修正"三信号融合"文档承诺 | 后端工程 | P1 | Day 3 |
| 25 | 实现模型管理 API 模块（6 子端点）或从 OPERATIONS.md 移除承诺 | 后端工程 | P1 | Day 4 |

---

## ⚠️ 待完善 / 已知局限

- **依赖 CVE 检查未完成**：项目无 lock 文件，FastAPI/SQLAlchemy/jose 等关键库版本未交叉验证 CVE（安全卫士 A06 置信度 5/10）
- **性能压测未执行**：QA 以功能冒烟测试为主，未进行并发压测和内存泄漏检测
- **模型准确率争议**：存储 accuracy=62.07% vs 文档声称 OOF=62.43%，MCC=0（=随机猜测），DrawExpert F1=0.4913 vs 声称 0.520，需 ML 团队核实
- **🔴 架构级根因（终版）**：`ensemble_trainer.py` 源码缺失（仅有 .pyc 缓存），23 个文件 import 它。**QA 修正**：运行服务器中 import 实际成功（隐式导入顺序依赖——unified_predictor 先被导入并添加 `D:\AI\footballAI` 到 sys.path），但 ModelBridge 加载 v3.2 模型无 draw_expert 支持 → 后端 API 路径 L2 DrawExpert + L3 Stacking + L5 NN 全部缺失。v4.0 六层架构在后端 API 路径中实际只有 L0 + 部分 L1 工作。统一了硬编码路径、三通道空壳、DrawExpert 无效三个独立发现
- **🔴 DrawExpert 四层根因定案（QA+产品官四轮交叉验证终版）**：**L0 代码**——`unified_predictor.py:565` 硬编码 `de_signal = np.array([0.33, 0.34, 0.33])`，line 572 融合 `proba = proba_xgb_raw * 0.45 + proba_lgb_raw * 0.35 + de_signal * 0.20`，20% 权重注入恒定 P(D)=0.34 → 每场冷启动预测被注入 0.068 恒定平局偏置，**不是模型退化而是代码写死的假信号**，让下游误以为三信号融合在工作，无 else 降级日志；**L1 配置**——`config.yaml:603` 指向 v3.2，升级时未更新，ModelBridge 加载 v3.2 无 draw_expert 支持；**L2 模型**——DrawExpert F1=0.0，`draw_expert_v1.joblib`（L4门控）异常被 except 吞掉；**L3 架构**——`ensemble_trainer.py` 源码缺失，ModelBridge 依赖隐式导入顺序。**三路径三模型对象**：冷启动（EnsembleTrainer 内嵌模型，72维特征）/ L4门控（draw_expert_v1.joblib，5维赔率特征）/ ModelBridge（v3.2 _last_submodel_probas，N/A）——三个入口全部失效。四层必须同时修复。L0+L1 可几分钟内完成，立即将系统从"注入假信号"变为"透明降级"
- **🔴 模型版本分裂（QA 新发现）**：ModelBridge 加载 v3.2（`football_balanced_production.joblib`, 5.21MB），UnifiedPredictor 加载 v4.1（`football_v4.1_production.joblib`, 4.57MB）。两条代码路径使用不同版本模型，预测结果可能不一致。隐式导入顺序依赖使重构会静默断裂
- **三通道架构空壳**：SKY 和 VIP 两通道完全失效，六层引擎"多专家共识"从未生效，实际只有 UnifiedPredictor 单通道工作
- **模型管理 API 未实现**：OPERATIONS.md 文档承诺 6 个子端点但代码中无 router 实现
- **VIPFinalPredictor 完整路径未验证**：因 `trap_probability_bridge.py` 缺失，VIP 通道端到端功能未验证
- **Docker 部署验证未执行**：本次检查基于本地启动，Docker 容器化部署未验证
- **start_server.bat 使用 footballAI venv**：`d:\AI\footballAI\.venv\Scripts\python.exe` 非项目独立环境，进一步加深跨项目耦合

---

## 📚 成员产出索引

- **gstack-product-reviewer（产品官）原始产出**：5 Critical + 8 Major + 7 Minor + 3 Info，代码质量评分 5/10，含精确行号和修复代码示例。终版追踪到 `ensemble_trainer.py` 源码缺失这一架构级根因，统一了硬编码路径、三通道空壳、DrawExpert 无效三个独立发现
- **gstack-security-officer（安全卫士）原始产出**：18 项安全发现（4C/4H/5M/5L），STRIDE 六类威胁建模 + OWASP Top 10 逐项检查，安全评分 2/10
- **gstack-qa-lead（质量门神）原始产出**：471/471 测试通过（需 UTF-8），33/34 模块导入 OK，API 冒烟 5/7 通过，模型推理 + D-Gate + SSE 验证通过。交叉验证后修正 /ws/health 误报（WebSocket 端点正常）、升级 /api/v1/models 为 HIGH（整个模块未实现）、定位 DrawExpert 根因（dict 包装器非模型对象，建议 CRITICAL）、新增 DrawExpert 静默降级告警缺失。**终版二次深度验证**：发现 DrawExpert 双路径失效机制（Chat 路径 F1=0.0 噪声 / ModelBridge 路径 v3.2 无 draw_expert 恒返回 None）、模型版本分裂（v3.2 vs v4.1）、修正产品官 import 失败分析（运行时 import 成功，隐式导入顺序依赖）、prediction_service.py 三信号融合为死代码。**终版四层根因定案**（与产品官四轮交叉验证）：L0 代码（`unified_predictor.py:565` 硬编码 `de_signal = [0.33, 0.34, 0.33]`，20% 权重注入恒定 0.068 平局偏置，假信号非模型退化）+ L1 配置（config.yaml:603 指向 v3.2）+ L2 模型（F1=0.0，draw_expert_v1.joblib 异常被 except 吞掉）+ L3 架构（ensemble_trainer.py 源码缺失），三路径三模型对象全部失效，四层必须同时修复。报告已落盘 `reports/QA_REPORT_v4.0.md`
- **gstack-investigator（排障手）原始产出**：功能完整性矩阵 13 模块审计，代码健康度 5 维度评分（重复 4/10、硬编码 3/10、死代码 5/10、复杂度 3/10、依赖 3/10），综合 4.5/10。交叉验证 QA 发现后升级为 6 个阻断性问题：三通道架构空壳（SKY+VIP 双失效）、模块包遮蔽、模型 MCC=0 红旗。最终交叉验证 QA 补充后确认 DrawExpert 模型无效根因

---

## 回滚预案

若强行上线后出现问题，按以下步骤回滚：

1. **停止服务**：`taskkill /F /PID <pid>` 或 `docker-compose down`
2. **回退模型**：`football_v4.1_production.joblib` → `football_v4.0` 版本（saved_models/ 内有两版本备份）
3. **恢复数据库**：`data/football_data.db.backup` → `data/football_data.db`（531MB / 33426 场比赛）
4. **重启服务**：`start_server.bat`（需设置 SECRET_KEY + PYTHONPATH 环境变量）
5. **验证**：`GET /api/v1/chat/health` → 200；`POST /api/v1/chat` → SSE 概率非零
6. **降级运行**：仅启用 UnifiedPredictor + D-Gate，禁用 SKY 和 VIP 通道。注意：冷启动路径受 `unified_predictor.py:565` 硬编码常数 0.34 偏置影响（20%权重×0.34=0.068 恒定平局偏置），平局预测系统性偏高

---

> 本报告由软件工坊 AI 协作生成（产品官 + 安全卫士 + 质量门神 + 排障手），关键决策请由工程负责人复核。
