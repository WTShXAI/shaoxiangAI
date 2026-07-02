# QA Report: 哨响AI (FootballAI) v4.0

| Field | Value |
|-------|-------|
| **Date** | 2026-06-20 |
| **Project** | D:\Architecture |
| **Tier** | Standard |
| **Scope** | 测试套件 + 模块导入 + API冒烟 + 模型推理 + D-Gate + SSE |
| **Python** | 3.13.12 |
| **Framework** | FastAPI 0.137 + Flask 3.1 + LightGBM 4.6 |
| **Model** | football_v4.1_production.joblib |

## Health Score: 55/100

| Category | Score | Notes |
|----------|-------|-------|
| 测试套件 | 70 | 471/471通过, 但Windows编码崩溃 |
| 模块导入 | 90 | 33/34 OK, backend.main需SECRET_KEY |
| API端点 | 60 | /ws/health是WebSocket(✅正常), 但整个模型管理API模块未实现 |
| 模型推理 | 65 | UnifiedPredictor OK, SKY/VIP通道损坏, DrawExpert静默降级 |
| D-Gate | 80 | 模式A/B逻辑正确, 但仅靠降级路径(Heuristic+OE)工作, 非设计预期的3信号 |
| SSE流式 | 70 | 格式正确, 但Windows curl编码问题 |
| 架构健壮性 | 30 | 跨项目依赖, 路径污染, 3通道中2通道损坏 |

> **交叉验证更新 (2026-06-20 12:45)**: 经 gstack-product-reviewer 和 gstack-security-officer 交叉验证, 修正 ISSUE-009 (/ws/health非缺陷), 升级 ISSUE-005 (DrawExpert根因已定位), 新增 ISSUE-013 (DrawExpert静默降级). 详见下文.

---

## 1. 测试执行摘要

| 指标 | 值 |
|------|-----|
| 声称用例数 | 498 |
| 实际通过 | 471 |
| 失败 | 0 |
| 跳过 | 0 |

**结果: 471/471 通过, 0 失败** (需 `PYTHONUTF8=1` 环境变量)

**文档差异:** start.bat 声称 "498用例", 实际运行为 471 用例 (差27个).

---

## 2. 模块导入验证

| 模块 | 状态 | 备注 |
|------|------|------|
| six_layer_conversation | ✅ | SixLayerConversationEngine |
| modules.output_schema | ✅ | |
| modules.intent_classifier_v2 | ✅ | |
| modules.expert_hub_v2 | ✅ | |
| modules.expert_manager | ✅ | |
| modules.prediction_orchestrator_v4 | ✅ | |
| modules.match_analyzer | ✅ | |
| modules.knowledge_layer | ✅ | |
| modules.scenario_engine | ✅ | |
| modules.draw_upset_analyzer | ✅ | |
| modules.upset_detector | ✅ | |
| modules.feedback_loop | ✅ | |
| modules.degradation_guard | ✅ | |
| modules.image_input | ✅ | |
| modules.auto_optimizer | ✅ | |
| modules.tuning_logger | ✅ | |
| modules.p4_enhancement | ✅ | |
| modules.expert_protocol | ✅ | |
| modules.expert_registry | ✅ | |
| modules.module_router | ✅ | |
| modules.goalkeeper_model | ✅ | |
| modules.goal_timing | ✅ | |
| modules.cross_opponent | ✅ | |
| modules.scorer_tracker | ✅ | |
| modules.timespace_detector | ✅ | |
| modules.arbitrage_detector | ✅ | |
| modules.attack_efficiency | ✅ | |
| modules.baseline | ✅ | |
| modules.odds_deep_analyzer | ✅ | |
| modules.post_match_analyzer | ✅ | |
| modules.progressive_optimizer | ✅ | |
| backend.models.unified_predictor | ✅ | |
| backend.main | ❌ | SECRET_KEY验证失败 (需环境变量) |
| bookmaker_sim.bookmaker_trap_detector | ✅ | |

**导入结果: 33/34 OK, 1 FAIL**

---

## 3. API 端点冒烟测试

| 端点 | 方法 | 状态码 | 结果 |
|------|------|--------|------|
| / | GET | 200 | ✅ HTML首页 (哨响AI v4.1) |
| /api/v1/chat/health | GET | 200 | ✅ {"status":"ok","version":"v4.1"} |
| /api/v1 | GET | 200 | ✅ API根路径, 返回端点列表 |
| /metrics | GET | 200 | ✅ Prometheus指标 |
| /api/v1/docs | GET | 200 | ✅ Swagger UI |
| /api/v1/chat | POST | 200 | ✅ SSE流式 (需UTF-8客户端) |
| /api/v1/models | GET | 404 | ❌ 端点列表中有但实际未注册 |
| /ws/health | GET | 404 | ❌ WebSocket健康端点未找到 |

---

## 4. 模型推理验证

### 模型加载
- 文件: `saved_models/football_v4.1_production.joblib` (4.4MB)
- 类型: dict 包装器 (非原生模型对象)
- 内含: xgb_model (XGBClassifier), lgb_model (LGBMClassifier), draw_expert_model (DrawExpert), meta_model (LGBMClassifier, 21特征, classes=[0,1,2]), scaler, feature_names (24个)
- **依赖**: 加载需 `D:\AI\footballAI` 在 sys.path (draw_expert.py 模块)

### UnifiedPredictor.predict() 测试 (巴西 vs 阿根廷 2.10/3.30/3.60)
| 输出 | 值 |
|------|-----|
| prediction | H (主胜) |
| confidence | 0.5708 |
| H/D/A 概率 | [0.5708, 0.2705, 0.1587] |
| method | threshold(D>0.46) |
| draw_signal | 0.0 |
| trap_level | none |
| goal_prediction | home=1.71, away=0.47, total=2.18, OU=Under |

✅ **UnifiedPredictor 推理管线验证通过**

### 模型指标 vs 声称值
| 指标 | 存储值 | 声称值 | 差异 |
|------|--------|--------|------|
| accuracy | 0.678 (67.8%) | 62.43% (OOF) | +5.4% |
| f1_draw | 0.571 | 0.520 | +0.051 |
| AUC | 0.848 | 0.815 | +0.033 |
| MCC | 0.0 | 未声称 | 异常 |
| DrawExpert F1 | 0.0 | 0.520 | -0.520 |

### DrawExpert 状态
- 加载成功, 但 `p_draw` 返回 `null`
- Draw-F1 = 0.0 (vs 声称 0.520)
- v41_config: draw_expert_mult=0.25, draw_cw_mult=1.1, draw_threshold=0.46 ✅ (与声称一致)

---

## 5. D-Gate 双模式验证

### 模式A: 中等热门 (imp_H≈60%, OU≤2.5)
- 测试场景: home=1.65, draw=3.50, away=5.00
- 条件: `0.50 < max_imp ≤ 0.70 AND (ou_line=None OR ou_line ≤ 2.5)`
- max_imp = 0.555 → 满足条件
- ✅ 模式A触发, prediction覆盖为"平局"

### 模式B: 均衡赛 (draw_odds 3.0-4.5, spread<1.6, OU≤2.5)
- 测试场景: 巴西 vs 阿根廷 2.10/3.30/3.60
- 条件: `3.0 ≤ od_p ≤ 4.5 AND abs(oh_p - oa_p) < 1.6 AND (ou_line=None OR ou_line ≤ 2.5)`
- od_p=3.30 (3.0≤3.30≤4.5 ✅), spread=|2.10-3.60|=1.50 (<1.6 ✅)
- ✅ 模式B触发, prediction覆盖为"平局"
- 服务器日志确认: `[D-Gate v4.7] 模式B: od=3.30 spread=1.50 → 平局`

### D-Gate 覆盖逻辑
- 当 D-Gate 激活时: prediction 重写为"平局", risk_tags 设为 `['d_gate_B']`
- 当 D-Gate 未激活且概率全0时: risk_tags 设为 `['d_gate_junk']`
- 当 D-Gate 未激活且 d_margin<0.02: risk_tags 设为 `['d_gate_junk']`
- ✅ 覆盖逻辑验证通过

---

## 6. SSE 流式响应测试

### 格式验证
- ✅ `data: {json}\n\n` 前缀格式正确
- ✅ 事件类型: text → predict_card → done
- ✅ done 事件正确发送
- ✅ predict_card 包含 home/away/h_prob/d_prob/a_prob/prediction/d_gate_active/d_gate_mode/risk_tags

### 实际响应 (巴西 vs 阿根廷, Python UTF-8客户端)
```
predict_card: home=巴西 away=阿根廷 h=0.5708 d=0.2705 a=0.1587
              pred=平局 dgate=True/B tags=['d_gate_B']
```

### Windows curl 编码问题
- ⚠️ Windows curl 发送 GBK 编码的中文, 服务器按 UTF-8 解码失败
- 服务器日志: `Received msg='' len=0` (空消息)
- 这是客户端编码问题, 非服务器bug, 但影响 Windows 命令行测试

---

## 7. 发现的问题清单

### CRITICAL (阻塞上线)

#### ISSUE-001: SKY 预测通道键名映射错误
- **严重度**: CRITICAL
- **类别**: functional
- **描述**: SKY预测器返回 `proba_final` 键, 但 SixLayer 引擎查找 `probabilities`/`probs` 键, 导致 SKY 通道始终返回 0.000/0.000/0.000
- **位置**: `six_layer_conversation.py:822` — `sp = sky_result.get("probabilities") or sky_result.get("probs", {})`
- **SKY实际返回**: `{"proba_raw": {...}, "proba_final": {"home":0.306, "draw":0.451, "away":0.243}, "prediction": "D"}`
- **影响**: 三通道预测架构中 SKY 通道完全失效, 仅 UnifiedPredictor 通道工作
- **修复建议**: 将 `sky_result.get("probabilities")` 改为 `sky_result.get("proba_final") or sky_result.get("probabilities")`

#### ISSUE-002: VIP 预测通道依赖缺失
- **严重度**: CRITICAL
- **类别**: functional
- **描述**: VIP预测器导入 `from trap_probability_bridge import apply_trap_correction`, 但 `trap_probability_bridge.py` 仅存在于 `D:\AI\footballAI`, 不在 `D:\Architecture` 中
- **错误**: `name 'apply_trap_correction' is not defined`
- **影响**: 三通道预测架构中 VIP 通道完全失效
- **修复建议**: 将 `trap_probability_bridge.py` 复制到 `D:\Architecture` 或创建符号链接

#### ISSUE-003: 跨项目路径依赖与模块污染
- **严重度**: CRITICAL
- **类别**: functional / architecture
- **描述**: 
  - 模型加载依赖 `D:\AI\footballAI\draw_expert.py` (pickle引用)
  - VIP预测器依赖 `D:\AI\footballAI\trap_probability_bridge.py`
  - NN模型路径 `D:\AI\footballAI\saved_models\football_nn_*.pth`
  - 当 `D:\AI\footballAI` 在 sys.path 优先位置时, 其 `modules/` 包遮蔽 `D:\Architecture\modules/`, 导致 knowledge_layer/degradation_guard/feedback_loop 导入失败
- **影响**: 部署环境必须同时存在两个项目目录, 且路径顺序敏感
- **修复建议**: 将所有依赖文件合并到 `D:\Architecture`, 消除跨项目依赖

### HIGH (重大问题)

#### ISSUE-004: 测试套件 Windows 编码崩溃
- **严重度**: HIGH
- **类别**: functional
- **描述**: 测试文件使用 ✓/✗ Unicode 字符, Windows 默认 GBK 编码无法输出, 导致 `UnicodeEncodeError` 崩溃
- **复现**: `python tests/test_v4_modules.py` (不加 PYTHONUTF8=1)
- **修复建议**: 在测试文件开头添加 `sys.stdout.reconfigure(encoding='utf-8')` 或设置 `PYTHONUTF8=1`

#### ISSUE-005: DrawExpert 双路径失效 — 根因定案 (四次交叉验证, 精确到源码行)
- **严重度**: HIGH → 升级: CRITICAL (核心差异化功能失效 + 代码设计缺陷)
- **类别**: functional / architecture / code-defect
- **描述**: DrawExpert 在两条代码路径中均无法有效工作, 失效原因不同; 且冷启动路径存在硬编码假信号注入

- **路径A — Chat端点冷启动 (UnifiedPredictor, 活跃路径)**:
  - `six_layer_conversation.py:263` → `predictors/unified_predictor.py` → `EnsembleTrainer` (from `D:\AI\footballAI`)
  - `unified_predictor.py:47-50` 显式添加 `D:\AI\footballAI` 到 sys.path, 使 ensemble_trainer 可导入
  - **DE=0.34 的精确来源 — 硬编码常量, 非模型输出**:
    `unified_predictor.py:565`:
    ```python
    de_signal = np.array([0.33, 0.34, 0.33])  # ← 硬编码默认值
    if self.trainer.draw_expert_model:
        de_p = self.trainer.draw_expert_model.predict_proba(X)
        if de_p.shape[1] == 2:  # 二分类覆盖条件
            de_d = float(de_p[0, 1])
            de_signal = np.array([(1-de_d)*0.5, de_d, (1-de_d)*0.5])
    ```
    日志 `DE=0.34` = `de_signal[1]` (line 574). 两种不可区分的场景:
    - 场景A: `predict_proba` 返回非二分类 → `shape[1] != 2` → if 失败 → 保持硬编码 [0.33, 0.34, 0.33]
    - 场景B: `predict_proba` 返回 P(draw)≈0.34 → 计算 = [(1-0.34)*0.5, 0.34, (1-0.34)*0.5] = [0.33, 0.34, 0.33] — 与硬编码数学相同
  - **融合公式 (line 572)**: `proba = proba_xgb_raw * 0.45 + proba_lgb_raw * 0.35 + de_signal * 0.20`
    DE 的 20% 权重注入恒定 P(D)=0.34 → 每场冷启动被注入 0.34 × 0.20 = **0.068 恒定平局偏置**
  - **代码设计缺陷**: 硬编码 [0.33, 0.34, 0.33] 有微妙平局偏置 (0.34 > 0.33), 应为中性 [1/3, 1/3, 1/3] 或权重重分配; 当前设计让下游以为三信号融合在工作, 无 else 降级日志

- **路径A-L4 — Chat端点 L4 门控 (独立加载路径)**:
  - `unified_predictor.py:371-383` → `_get_draw_expert_signal()` (line 587-610)
  - 从 `draw_expert_v1.joblib` 文件独立加载 `DrawExpert` 对象 (与冷启动路径的 `self.trainer.draw_expert_model` 是**不同模型对象**)
  - 日志 `DrawExpert 已加载: ...draw_expert_v1.joblib (Draw-F1=0.0)` (line 74/100/126/167) — 模型加载但 F1=0.0
  - `_get_draw_expert_signal` 返回 `de.predict_proba(feats)[1]` — 若模型有效则产出真实信号, 若无效则异常被 except 吞掉, draw_signal 保持 0.0

- **路径B — ModelBridge (prediction_service.py, 代码存在但路由未注册)**:
  - `agents/model_bridge.py:105` → `from ensemble_trainer import EnsembleTrainer`
  - model_bridge.py 自身不添加 `D:\AI\footballAI` 到 sys.path, **但运行时 import 成功** (unified_predictor 先导入并添加了路径 — 隐式导入顺序依赖)
  - server.err.log:83-86 确认: `ModelBridge: 模型就绪: football_balanced_production.joblib | 72特征 | NN=✓`
  - ModelBridge 加载 v3.2 模型, v3.2 无 draw_expert 子模型 → `get_de_output()` 返回 None
  - `prediction_service.py:523-530` 的三信号融合代码**永远不会执行** (de_pdraw 始终为 None), 恒降级到二信号

- **DE=0.34 常数输出 — 4场比赛决定性证据**:
  server.err.log 中 4 场不同比赛的 DrawExpert 输出完全相同, 而其他子模型输出差异巨大:
  | 比赛 | XGB (H/D) | LGB (H/D) | DE | final |
  |------|-----------|-----------|-----|-------|
  | 荷兰 vs 日本 | 0.62/0.25 | 0.42/0.45 | **0.34** | 0.491/0.335/0.174 |
  | 澳大利亚 vs 土耳其 | 0.18/0.28 | 0.10/0.42 | **0.34** | 0.182/0.339/0.478 |
  | 西班牙 vs 佛得角 | 0.84/0.14 | 0.77/0.22 | **0.34** | 0.714/0.208/0.078 |
  | (第4场 12:37:52) | 0.56/0.31 | 0.41/0.45 | **0.34** | 0.458/0.367/0.175 |
  XGB 范围 0.18–0.84, LGB 范围 0.10–0.77 — 子模型正常工作. DE 恒为 0.34 — 硬编码常量或退化模型的输出, **常数偏置, 非有效信号**.

- **四层根因分析 (四次交叉验证最终版)**:
  | 层次 | 问题 | 根因 | 修复 | 工作量 |
  |------|------|------|------|--------|
  | **L0: 代码** | DE=0.34 硬编码假信号注入 | `unified_predictor.py:565` 硬编码 [0.33, 0.34, 0.33], 无 else 降级日志 | 改为中性 [1/3,1/3,1/3] 或权重重分配 + 降级日志 | 一行 + 日志 |
  | **L1: 配置** | ModelBridge 加载 v3.2 而非 v4.1 | `config/config.yaml:603` model_path 指向 v3.2, 升级时未更新 | 改为 `football_v4.1_production.joblib` | 一行 |
  | **L2: 模型** | DrawExpert F1=0.0, 输出常数 0.34 | 模型训练数据与推理特征不匹配, 或模型损坏 | 重新训练 DrawExpert | 重训管线 |
  | **L3: 架构** | ensemble_trainer.py 源码不在项目内 | 仅 .pyc 缓存, 依赖 D:\AI\footballAI; import 依赖隐式导入顺序 | 复制源码到项目内 + 显式 import | 文件复制 |
  四层需同时修复. L0 是代码设计缺陷 (独立于模型有效性), 即使模型修复后硬编码默认值仍应改为中性.

- **架构级问题**:
  1. `ensemble_trainer.py` 源码不在项目中 (仅 .pyc 缓存), 实际依赖 `D:\AI\footballAI` 的源文件
  2. ModelBridge 的成功依赖 unified_predictor 先被导入 (隐式导入顺序依赖) — 重构导入顺序会静默断裂
  3. ModelBridge 加载 v3.2 旧模型, UnifiedPredictor 加载 v4.1 新模型 — 两条路径使用不同版本模型
  4. DrawExpert F1=0.0 — 模型从未有效训练或训练数据与推理特征不匹配
  5. prediction_service.py 的三信号融合代码 (line 525-530) 是死代码
  6. **冷启动路径 line 565 硬编码假信号注入** — 代码设计缺陷, 让下游误以为三信号融合在工作
  7. **两个 DrawExpert 模型对象** — 冷启动路径用 EnsembleTrainer 内嵌模型, L4路径从文件独立加载, 可能有不同训练来源

- **影响链**: DrawExpert 是 v4.0 六层架构 L2 (平局专科层) 的核心组件. 失效意味着:
  - Chat路径冷启动: DrawExpert 注入硬编码常数 0.34 (20%权重), 系统性抬高平局概率 0.068
  - Chat路径L4门控: DrawExpert 从文件加载但 F1=0.0, draw_signal 可能恒为 0.0 (异常被 except 吞掉)
  - 预测API路径: DrawExpert 返回 None, 三信号融合恒降级为二信号
  - 用户看到"三信号融合"的承诺, 实际: Chat路径=硬编码假信号, API路径=二信号降级
- **修复建议**: 
  1. **L0 代码**: `unified_predictor.py:565` 改为 `de_signal = np.array([1/3, 1/3, 1/3])`; 更好: DE不可用时权重重分配 `proba = proba_xgb_raw * 0.5625 + proba_lgb_raw * 0.4375`; 添加 else 降级日志
  2. **L1 配置**: 将 `config/config.yaml:603` 的 model_path 改为 `football_v4.1_production.joblib` (一行修复)
  3. **L2 模型**: 重新训练 DrawExpert 使 F1 > 0 (当前输出常数 0.34, 系统性抬高平局概率)
  4. **L3 架构**: 将 `ensemble_trainer.py` 源码从 `D:\AI\footballAI` 复制到项目内, 消除跨项目依赖和隐式导入顺序依赖
  5. 统一 ModelBridge 和 UnifiedPredictor 使用 v4.1 模型
  6. 在 API 响应中标注 DrawExpert 可用状态和 F1 值, 而非静默降级
  7. 移除 prediction_service.py 中的死代码 (三信号融合分支)

#### ISSUE-006: 模型指标与声称值不符
- **严重度**: HIGH
- **类别**: content
- **描述**: 
  - 存储accuracy=67.8% vs 声称OOF=62.43% (+5.4%)
  - 存储f1_draw=0.571 vs 声称0.520 (+0.051)
  - 存储AUC=0.848 vs 声称0.815 (+0.033)
  - MCC=0.0 (异常)
- **影响**: 上线评估基准不准确, 可能导致错误的质量判断
- **修复建议**: 核实指标来源 (训练集 vs OOF), 更新文档

### MEDIUM (中等问题)

#### ISSUE-007: backend.main 无 SECRET_KEY 无法启动 — 含安全风险
- **严重度**: MEDIUM (安全维度: CRITICAL)
- **类别**: functional / security
- **描述**: Settings 验证 SECRET_KEY ≥ 32字符, 无 .env 文件, 必须通过环境变量设置
- **安全风险 (交叉验证 — gstack-security-officer F-019)**: 
  - SECRET_KEY 明文硬编码在 `start_server.bat:3`: `set SECRET_KEY=FootballAI-v5.0-DGate-Production-2026-06-20-SecureKey`
  - 同一脚本 `start_server.bat:6` 使用 `--host 0.0.0.0` 绑定所有网络接口
  - 结合认证已禁用 (F-001), 攻击者获取此明文密钥后可伪造任意 JWT token
  - OWASP 分类: A02 Cryptographic Failures + A05 Security Misconfiguration
- **额外发现**: `start_server.bat:6` 使用 `d:\AI\footballAI\.venv\Scripts\python.exe` (footballAI的venv), 而非独立venv
- **影响**: 新环境部署需手动设置 SECRET_KEY; 明文密钥已泄露在脚本中
- **修复建议**: 创建 .env 文件或 .env.example 模板; 通过环境变量或 secrets manager 注入 SECRET_KEY; 轮换已泄露密钥

#### ISSUE-008: Flask legacy 文件缺失 — 含安全风险
- **严重度**: MEDIUM
- **类别**: functional / security
- **描述**: `archive/prediction_service_flask_legacy.py` 文件不存在, Flask WSGI 挂载失败
- **安全风险 (交叉验证 — gstack-security-officer F-021)**: 
  - Flask WSGI 回退路径设计用于处理 `/api/*` 下的 legacy 路由
  - 文件缺失导致这些路由静默失败, 可能存在安全控制覆盖的盲区
  - 某些 legacy 端点可能预期有 Flask 层的认证/验证, 现在这些验证被跳过
  - OWASP 分类: A04 Insecure Design
- **影响**: Flask 兼容层不可用, legacy 路由安全控制可能存在盲区
- **日志**: `[错误] Flask WSGI 挂载失败: [Errno 2] No such file or directory`
- **修复建议**: 恢复缺失的 archive 文件, 或彻底移除 Flask bridge 代码并确认所有 legacy 路由已被 FastAPI 原生路由替代且具备等效安全控制

#### ISSUE-009: 整个模型管理 API 模块未实现 (文档承诺的完整功能缺失)
- **严重度**: MEDIUM → 升级: HIGH (交叉验证 — gstack-product-reviewer)
- **类别**: functional
- **描述**: 
  - ~~`/ws/health` 返回 404~~ → **已修正**: `/ws/health` 是 WebSocket 端点 (非HTTP GET), 经 `ws://` 协议验证正常返回 health_update 消息. 原测试方法有误, 非端点缺失.
  - `/api/v1/models` 返回 404 → **确认且范围更大**: 不只是一个端点缺失, 而是**整个模型管理 API 模块未实现**
  - `docs/OPERATIONS.md` 文档了 6 个子端点: `/api/v1/models/versions`, `/best`, `/compare`, `/deploy`, `/rollback`, `/auto-promote`
  - `backend/api/v1/endpoints/` 下无 models 相关 router 文件
  - 路由检查确认: `/api/v1` 下仅有 `/chat`, `/chat/health`, `/predict/image` 三个业务端点
- **影响**: 文档描述了一个完整的模型版本管理系统 (部署/回滚/自动提升), 但代码不存在. 这是文档承诺的完整功能模块未实现, 不是遗漏一个端点.
- **修复建议**: 实现模型管理 API 模块, 或从文档中移除未实现功能的描述

#### ~~ISSUE-009-original: /ws/health 返回 404~~ → 已修正: 非缺陷
- **修正说明**: `/ws/health` 注册为 `APIWebSocketRoute` (backend/main.py:1098), 需 `ws://` 协议握手. HTTP GET 返回 404 是 FastAPI 正常行为.
- **验证**: `ws://localhost:8000/ws/health` 连接成功, 返回 `{"type":"health_update","health":"watching","performance":{...},"trend":"stable"}`
- **状态**: ✅ 端点正常工作, 原 QA 报告为误报 (测试方法不当)

#### ISSUE-010: CUDA 兼容性警告
- **严重度**: MEDIUM
- **类别**: performance
- **描述**: RTX 5070 Ti (sm_120) 不兼容当前 PyTorch 安装 (支持至 sm_90)
- **影响**: GPU 加速可能不可用, 训练降级到 CPU
- **日志**: `NVIDIA GeForce RTX 5070 Ti with CUDA capability sm_120 is not compatible`

### LOW (轻微问题)

#### ISSUE-011: 测试用例数文档不符
- **严重度**: LOW
- **类别**: content
- **描述**: start.bat 声称 "498用例", 实际 471 用例

#### ISSUE-012: 无 VERSION 文件
- **严重度**: LOW
- **类别**: content
- **描述**: 项目根目录无 VERSION 文件

#### ISSUE-013: DrawExpert 双路径降级 — 三信号融合承诺与实际不符 (二次交叉验证更新)
- **严重度**: HIGH
- **类别**: functional / ux / architecture
- **描述**: D-Gate 模式A/B 门控逻辑实现健全, 但 DrawExpert 在两条路径中均无法有效贡献 (ISSUE-005):
  - **Chat路径冷启动 (UnifiedPredictor)**: DrawExpert 注入硬编码常数 0.34 (line 565, 20%权重), 系统性抬高平局概率 0.068 — 不是模型输出, 是代码假信号
  - **Chat路径L4门控**: DrawExpert 从 draw_expert_v1.joblib 文件独立加载, F1=0.0, draw_signal 可能恒为 0.0 (异常被 except 吞掉)
  - **预测API路径 (ModelBridge)**: DrawExpert 返回 None — 三信号融合代码是死代码, 恒降级为二信号. 但此路径路由未注册, 当前不影响用户
- **影响**: 
  - 用户/API 消费者看到的是"三信号融合 (Heuristic + OE + DrawExpert)"的承诺
  - Chat路径实际: 二有效信号 + 一噪声信号 (F1=0.0 的 DrawExpert)
  - 预测API路径实际: 二信号 (DrawExpert 完全缺失)
  - 静默降级无任何告警或状态标记 — 用户无法知道 DrawExpert 不可用或无效
  - D-Gate "正常工作"是因为降级路径设计得当, 但这是**降级后的正常, 不是设计预期中的正常**
- **修复建议**: 
  1. 修复 DrawExpert 模型使 F1 > 0 (ISSUE-005)
  2. 在 API 响应中明确标注 DrawExpert 可用状态和 F1 值 (degraded/ok)
  3. 在日志中记录降级事件, 而非静默跳过
  4. 移除 prediction_service.py 中的死代码 (永远不执行的三信号融合分支)

---

## 8. Go/No-Go 决策建议

### 决策: ⚠️ CONDITIONAL NO-GO

**理由:**

核心预测管线 (UnifiedPredictor + D-Gate) 功能正常, 可以产出有效的 H/D/A 概率和平局覆盖预测。但存在以下阻塞项:

1. **SKY 通道失效** (ISSUE-001) — 键名映射错误导致始终返回 0.000, 三通道架构退化为单通道
2. **VIP 通道失效** (ISSUE-002) — 跨项目依赖缺失, 通道完全不可用
3. **DrawExpert 四层失效** (ISSUE-005, 四次交叉验证) — L0代码硬编码假信号(line565) / L1配置指向v3.2 / L2模型输出常数0.34(F1=0.0) / L3源码不在项目内; 系统性抬高平局概率0.068
4. **跨项目依赖** (ISSUE-003) — 部署必须同时存在两个项目目录, 路径顺序敏感; ensemble_trainer.py 源码缺失是又一具体表现
5. **DrawExpert 静默降级** (ISSUE-013, 交叉验证新增) — D-Gate 仅靠降级路径工作; Chat路径注入常数0.34偏置; API路径DrawExpert完全缺失
6. **模型管理 API 模块未实现** (ISSUE-009, 交叉验证升级) — 6个文档化端点全部缺失

**上线条件 (修复后可 GO):**
- [ ] 修复 SKY 通道键名映射 (ISSUE-001) — 改动1行代码
- [ ] 复制 trap_probability_bridge.py 到项目内 (ISSUE-002)
- [ ] L0: 修复 unified_predictor.py:565 硬编码 — 改为中性值或权重重分配 + 降级日志 (ISSUE-005, 一行)
- [ ] L1: 更新 config.yaml model_path 为 v4.1 (ISSUE-005, 一行)
- [ ] L2: 重新训练 DrawExpert 使 F1 > 0, 消除常数0.34偏置 (ISSUE-005)
- [ ] L3: 复制 ensemble_trainer.py 到项目内 (ISSUE-005, ISSUE-003)
- [ ] 消除或文档化跨项目依赖 (ISSUE-003)
- [ ] 从文档移除未实现的模型管理 API 描述, 或实现该模块 (ISSUE-009)

**当前可降级运行:**
- 仅 UnifiedPredictor 通道 + D-Gate (降级模式) 即可提供基本预测功能
- 471/471 单元测试通过
- API 核心端点 (/, /chat, /chat/health, /metrics, /ws/health) 正常
- ⚠️ 注意: 降级模式下平局预测受 DrawExpert 硬编码常数 0.34 偏置影响 (20%权重×0.34=0.068恒定偏置), 系统性偏高; DrawExpert 差异化能力为零

---

## 9. 回滚预案

### 回滚触发条件
- 预测准确率显著下降 (>10%)
- API 服务不可用 (>5分钟)
- D-Gate 误触发率异常升高

### 回滚步骤

1. **停止当前服务**
   ```bash
   taskkill /PID <pid> /F  # 找到占用8000端口的进程
   ```

2. **回退模型版本**
   ```bash
   # 修改 start_server.bat 或模型注册表
   # 将 football_v4.1_production.joblib 替换为 football_v4.0_production.joblib
   # 或修改 config/settings.yaml 中的模型路径
   ```

3. **重启服务**
   ```bash
   # 使用 start_server.bat 重启
   # 或手动: set SECRET_KEY=<key> && python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
   ```

4. **验证回滚**
   - GET /api/v1/chat/health → 确认 200
   - POST /api/v1/chat → 确认 SSE 正常
   - 检查 predict_card 概率非零

### 备份状态
- 当前运行服务 PID: 8084 (端口 8000)
- 模型备份: saved_models/ 目录包含 v4.0 和 v4.1 两个版本
- 数据库: data/football_data.db (531MB, 33426场比赛)

---

## 附录: 测试环境信息

| 项目 | 值 |
|------|-----|
| OS | Windows |
| Python | 3.13.12 |
| FastAPI | 0.137.2 |
| Flask | 3.1.3 |
| LightGBM | 4.6.0 |
| NumPy | 2.4.6 |
| Pandas | 3.0.3 |
| scikit-learn | 1.9.0 |
| GPU | RTX 5070 Ti (16GB, sm_120 — PyTorch不兼容) |
| CPU | 24逻辑核心 (12物理核心) |
| 内存 | 64GB |
| 数据库 | SQLite, 531MB, 34表, 33426场比赛 |
