# FootballAI v4.0 — 上线前全检审计报告

**审计日期**: 2026-06-26 12:30 CST  
**审计范围**: 安全 / 代码质量 / 功能正确性 / QA回归  
**审计方法**: 四位Agent并行 + 端到端验证  
**基准对比**: 上次全检 (2026-06-20) NO-GO → 修复后 Conditional Go → 🟢 GO

---

## 总览

| 维度 | 评分 | P0 | P1 | P2 | P3 | 结论 |
|------|------|----|----|----|----|------|
| 安全 | 2/10 | 4 | 3 | 7 | 3 | ❌ 严重 |
| 代码质量 | 4/10 | 3 | 8 | 12 | 0 | ❌ 严重 |
| 功能正确性 | 4/10 | 2 | 3 | 4 | 1 | ❌ 严重 |
| QA回归 | 78/100 | 1 | 0 | 2 | 0 | ⚠️ 注意 |

**综合判定: 🚫 NO-GO — 11项P0阻塞项，不建议上线**

---

## P0 — 上线阻塞 (11项)

### P0-1 🔴 认证系统完全禁用
- **位置**: `backend/core/security.py:101-115`, `backend/api/v1/endpoints/auth.py:28-47`, `backend/api/deps.py:15-17`
- **描述**: 三处认证代码全部标记"认证已禁用"。`get_current_user()`忽略token直接返回admin；`require_role()`放行所有角色检查；`/login`接受任意用户名密码返回admin JWT
- **影响**: 任何人可无限制访问全部API端点
- **状态**: ⚠️ 上次审计已报告，持续未修复

### P0-2 🔴 .env 硬编码真实API密钥
- **位置**: `.env:5,9,10`
- **内容**: `SECRET_KEY=FootballAI-v4.1-LocalDev-Deploy-2026-SecureKey-32chars+`、火山引擎OCR AK/SK
- **影响**: 仓库泄露 = 密钥泄露；JWT可被伪造；OCR费用可被盗用

### P0-3 🔴 26个文件硬编码 D:\AI\footballAI 路径
- **文件**: `pro_predict_kelly.py`, `start_server.bat`, `features/jepa_pipeline.py`, `pipeline/*.py` (14个), `scripts/*.py` (9个), `config/settings.yaml`, `config/jepa_v5.yaml`
- **影响**: 部署到新机器直接崩溃，模型文件跨项目依赖

### P0-4 🔴 sync engine.process() 阻塞FastAPI事件循环
- **位置**: `backend/main.py:922`, `api/chat_api.py:71`
- **描述**: SixLayerConversationEngine.process() 同步执行ML推理（SKY+VIP+D-Gate），在异步生成器内直接调用，无asyncio.to_thread()包装
- **影响**: 1个请求阻塞全部并发请求，高并发下服务不可用

### P0-5 🔴 聊天端点使用过时D-Gate v5.1而非v5.3
- **位置**: `six_layer_conversation.py:1375` — `from rules.d_gate_engine import apply_dgate`
- **UnifiedPredictor内部**: `predictors/unified_predictor.py:426` — `from rules.drawgate_v53 import apply_drawgate`
- **差异**: v5.1缺少Mode C-away、away_skepticism、group_stage_rotation、DrawExpert信号增强
- **影响**: `/chat`端点D-Gate结果与UnifiedPredictor不一致，用户看到过期判断

### P0-6 🔴 P0验证回归：阈值判型全部退化为"主胜"
- **验证脚本**: `scripts/verify_p0_production.py`
- **结果**: 26场世界杯全部预测为H（主胜），包括：沙特vs乌拉圭(H=0.13 A=0.66→pred=H)、伊拉克vs挪威(H=0.30 A=0.43→pred=H)、乌兹别克vs哥伦比亚(H=0.16 A=0.61→pred=H)
- **指标崩溃**: Acc 57.69%→50.00%, Draw F1 0.353→0, Macro F1 0.507→0.222
- **根因**: UnifiedPredictor的draw_threshold=0.32阈值逻辑存在严重bug，pD从未被选中

### P0-7 🔴 sync requests.get() 在异步端点内阻塞
- **位置**: `backend/api/v1/endpoints/matches.py:66` → `fetch_live_from_api()` → `requests.get()`
- **影响**: 同样阻塞事件循环

### P0-8 🔴 82+文件 sys.path.insert 竞态条件
- **位置**: `backend/main.py:844-848` (异步生成器内动态修改sys.path) + 80+文件模块级insert
- **影响**: 并发请求下模块导入不确定；模块遮蔽风险

### P0-9 🔴 SKY/VIP/Unified 输出结构严重不一致
| 字段 | SKY | VIP | Unified |
|------|-----|-----|---------|
| 概率键名 | `home/draw/away` (小写) | `H/D/A` (大写) | `H/D/A` (大写) |
| risk_tag | 无 | 无 | 有 |
| 比分格式 | `expected_goals` | `top3_scores` | `goal_prediction` |
- **影响**: 消费端需维护3套解析逻辑，容易出错

### P0-10 🔴 三个D-Gate引擎并行运行（v5.1/v5.2/v5.3）
| 引擎 | 版本 | 使用者 |
|------|------|--------|
| `rules/d_gate_engine.py` | v5.1 | `six_layer_conversation.py`, `backend/main.py` |
| `rules/d_gate_v52.py` | v5.2 | 独立/回测 |
| `rules/drawgate_v53.py` | v5.3 | `unified_predictor.py` |
- **影响**: 三方可能对同一场比赛给出不同判断

### P0-11 🔴 models/ 目录缺关键模型文件
- **位置**: `config/settings.yaml:109` — `v32_model: "models/main/football_balanced_production.joblib"` (目录不存在)
- **config/settings.yaml:108** — `v41_model: "models/main/football_v4.1_production.joblib"` (路径无效)
- **影响**: 降级路径触发即崩溃

---

## P1 — 高优先级 (8项)

### P1-1 🟠 API端点无速率限制
- **位置**: `backend/main.py:738`, `api/chat_api.py:33`
- **描述**: `API_RATE_LIMIT: "100/minute"`已配置但从未强制执行
- **影响**: DoS攻击，OCR费用无上限消耗

### P1-2 🟠 CORS代理通配符 (*) 允许API密钥窃取
- **位置**: `tools/proxy_server.py:358-363`
- **描述**: `Access-Control-Allow-Origin: *`设置在所有响应上
- **影响**: 跨源读取含API密钥的响应

### P1-3 🟠 pickle.load 潜在RCE
- **位置**: `modules/expert_protocol.py:354`
- **描述**: 从文件反序列化pickle数据，若路径用户可控则为RCE
- **缓解**: 当前路径为内部路径，但无防护

### P1-4 🟠 SQLite连接无上下文管理器保护 (大面积)
- **位置**: `database/db_manager.py:568`, `evaluation.py:42-117`, `prediction_service.py:1268-1828`, `prediction_guard.py:791-1148` 等20+处
- **影响**: 异常时连接泄漏，高频场景导致"database is locked"

### P1-5 🟠 裸except: 吞没关键错误 (生产代码)
- **位置**: `api/chat_api.py:46`, `six_layer_conversation.py:52,331`, `modules/degradation_guard.py:277`, `report_generator_v4.py:458` 等
- **影响**: 错误静默丢失，排障不可行

### P1-6 🟠 SKY预测器缺少关键字段
- **位置**: `predictors/sky/sky_predictor.py:176-187`
- **缺失**: risk_tag, dgate_mode, draw_threshold_eff, lambda_fusion, trap_level
- **影响**: SKY预测结果无法参与风控联动

### P1-7 🟠 14个pipeline脚本硬编码项目路径
- **位置**: `pipeline/wc2026_v41_backtest.py:13-14`, `pipeline/fp_goal_analysis.py:13-14` 等
- **内容**: `ARCH_ROOT = Path(r"D:/Architecture")`, `FAI_ROOT = Path(r"D:/AI/footballAI")`
- **影响**: 部署路径变更需逐个修改

### P1-8 🟠 聊天/Unified两套risk_tag计算可能冲突
- **位置**: `backend/main.py:1059-1106` (D-Gate v5.1) vs `unified_predictor.py:426` (DrawGate v5.3)
- **影响**: 同一请求可能产生冲突的risk_tag

---

## P2 — 中等优先级 (10项)

| ID | 描述 | 位置 |
|----|------|------|
| P2-1 | DEBUG模式下自动生成SECRET_KEY | `backend/core/config.py:166-169` |
| P2-2 | Flask密钥自动回退 | `config/api_config.py:119-124` |
| P2-3 | Chat端点未使用input_validator | `backend/main.py:738-749` |
| P2-4 | DEBUG模式TrustedHost通配符* | `backend/main.py:151` |
| P2-5 | 多处__import__动态加载 | 15+处 |
| P2-6 | 静默ImportError吞没 | `bookmaker_sim/bayesian_odds_inverter.py:1131` 等多处 |
| P2-7 | settings.yaml双重配置冗余 | `config.yaml` vs `settings.yaml` |
| P2-8 | 测试套件 468/471 (3失败) | `tests/test_v4_modules.py` |
| P2-9 | Python 3.14不兼容 (缺torch/joblib) | 运行环境 |
| P2-10 | P1/P2/P3验证脚本因torch缺失无法执行 | `scripts/verify_p1_validation.py` 等 |

---

## P3 — 低优先级 (4项)

| ID | 描述 | 位置 |
|----|------|------|
| P3-1 | 图片上传仅检查Content-Type未检查魔数 | `backend/main.py:1352-1356` |
| P3-2 | 临时文件delete=False崩溃时残留 | `backend/main.py:1370-1372` |
| P3-3 | 无HTTPS/SSL强制执行 | `start.py:89-91` |
| P3-4 | Mode A阈值 v5.1(0.28) vs v5.3(0.24) 有意分歧 | `d_gate_engine.py` vs `drawgate_v53.py` |

---

## QA回归测试

| 指标 | 值 | 判定 |
|------|-----|------|
| 测试总数 | 471 | — |
| 通过 | 468 | ✅ |
| 失败 | 3 | ⚠️ |
| 通过率 | 99.4% | ✅ |
| 核心模块导入 | 9/9 | ✅ |
| 配置文件有效 | 4/4 (UTF-8) | ✅ |
| 模型文件加载 | torch缺失无法加载 | ⚠️ |
| P0验证回测 | Acc 50% (崩溃) | ❌ |

**3个失败测试**:
1. "YAML loaded" — pyyaml可能未安装于测试Python环境
2. "64+ total terms — got 34" — 术语表不完整
3. "lookup 抽水" — 术语查询失败

---

## 与上次审计对比 (2026-06-20)

| 上次发现 | 当前状态 |
|----------|----------|
| SSL verify=False | ✅ 已修复 |
| CORS allow_methods="*" | ✅ 已修复 |
| asyncio NameError | ✅ 已修复 |
| D-Gate spread<1.6 bug | ✅ 已修复 |
| 三管线模型不一致 (VIP用balanced) | ✅ 已修复 |
| DrawExpert恒定0.331 | ✅ 已修复 |
| SKY NN路径错误 | ✅ 已修复 |
| model_bridge.py TypeError | ✅ 已修复 |
| GBK测试崩溃 | ✅ 已修复 |
| **认证禁用** | ❌ **未修复** (re-reported) |
| **硬编码D:\AI\footballAI** | ❌ **部分修复，26个遗留** |
| **无限流** | ❌ **未修复** (re-reported) |
| **SECRET_KEY硬编码** | ❌ **未修复** (re-reported) |
| **sys.path泛滥** | ❌ **未修复** (82+文件) |

---

## 修复路线图

### 即刻阻塞 — P0必须在24小时内修复

1. **P0-6 (最高优先级)**: 修复UnifiedPredictor阈值判型bug — pD从未被选中，全部退化为H
2. **P0-5**: 统一D-Gate到v5.3 — 废弃v5.1/v5.2，全系统使用drawgate_v53
3. **P0-1/P0-2**: 恢复认证 + 轮换所有密钥
4. **P0-3**: 创建PathConfig统一管理路径，消除26个硬编码引用
5. **P0-4**: 所有ML推理用asyncio.to_thread()包装
6. **P0-9**: 统一输出schema (全部使用大写H/D/A + risk_tag + goal_prediction)
7. **P0-11**: 修复settings.yaml无效模型路径

### 上线前 — P1必须修复
8. 部署速率限制中间件
9. 缩小CORS代理到特定来源
10. 审计pickle.load路径
11. SQLite连接全部改用上下文管理器
12. 裸except改为至少logging.error

### 上线后 — P2逐个解决
13-22. 清理import错误处理、统一配置系统、补全测试覆盖率

---

## 审计结论

| 维度 | 判定 |
|------|------|
| 安全性 | ❌ NO-GO — 零认证 + 密钥泄露 + 无限流 |
| 稳定性 | ❌ NO-GO — 事件循环阻塞 + SQLite泄漏 + 路径崩溃 |
| 功能完整性 | ❌ NO-GO — D-Gate三版本混乱 + 输出不兼容 + P0回归 |
| QA质量 | ⚠️ CONDITIONAL — 468/471通过但P0验证崩溃 |

**最终判定: 🚫 NO-GO**

系统存在11项P0阻塞项，其中最严重的是P0-6（阈值判型全塌）和P0-5（D-Gate三版本并行），导致核心预测功能无法正常工作。建议在P0项全部修复后再进行第二轮全检。

---

*报告生成: WorkBuddy 全检Agent | 2026-06-26 12:30 CST*
*审计方法: 安全Agent + 代码质量Agent + 功能Agent + QA直接验证*
