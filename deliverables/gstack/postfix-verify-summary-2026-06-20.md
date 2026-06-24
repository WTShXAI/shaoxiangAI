# FootballAI v4.0 P0修复后验证报告

**日期**：2026-06-20
**场景**：P0修复后验证（代码审查 + 功能审计 + QA测试）
**参与成员**：产品官 + 排障手 + 质量门神
**验证范围**：忽略安全问题，聚焦非安全类P0修复验证

---

## 📌 TL;DR（执行摘要）

- 整体结论：🟡 **Conditional Go** — 核心 P0 修复到位，但存在 2 个未解决 P0 + 5 个新发现隐患
- 用户声称修复 11 项 P0，三位成员交叉验证：8 项彻底修复 / 3 项部分修复 / 0 项未修复
- 测试套件 471/471 全部通过
- **阻塞上线的问题**：① P0-13 模块包遮蔽仍存在（import 会失败）② 两个"v4.1"模型文件结构不一致（预测不一致风险）③ ModelBridge 配置路径错误（靠 fallback 意外工作）④ prediction_service 死代码复活（行为变化需验证）
- **强烈建议上线前修复**：DrawExpert L2 未重训（F1=0.0）、SKY 返回恒定值、NN 模型脚本缺失、D-Gate 第四处不一致

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| Go / No-Go | 🟡 Conditional Go |
| 三位成员评分 | 产品官 6.5/10 · 排障手 6.5/10 · 质量门神 82/100 |
| P0 修复验证 | ✅ 8 彻底 / ⚠️ 3 部分 / ❌ 0 未修复（11 项中） |
| 仍存在 P0 | 3 项（P0-13 模块遮蔽 + P0-16 MCC=0 + DrawExpert L2 未重训） |
| 新发现隐患 | 9 项（2 🔴 + 4 🟠 + 3 🟡） |
| 测试套件 | ✅ 471/471 通过 |
| 三通道状态 | ✅ 全部激活（但 SKY 疑似 fallback 模式） |
| 关键行动项 | 11 条 |

---

## 1. 各成员核心结论

### 🔍 产品官（代码审查）
- 核心判断：6.5/10，Conditional Go。8 项彻底修复、3 项部分修复，修复质量合格但存在回归
- 关键发现：**两个"v4.1"模型文件结构不一致**——UnifiedPredictor 加载 `models/main/` 版（10.5MB, 16 keys），ModelBridge 加载 `saved_models/` 版（4.5MB, 25 keys），key 集合完全不同。版本分裂从"v3.2 vs v4.1"演变为"v4.1-main vs v4.1-saved"
- 关键建议：上线前必须统一模型文件 + 修复模块包遮蔽

### 🔧 排障手（功能审计）
- 核心判断：6.5/10，Conditional Go。核心预测管线可用，三通道架构已激活，但 DrawExpert L2 未修复
- 关键发现：DrawExpert L2 模型层 **仍 P0**——F1=0.0（默认阈值 0.5），AUC=0.599（接近随机），模型未重训。但存在 best_f1=0.4265（阈值 0.344）未被代码使用。ModelBridge 配置路径不匹配（读 `_PROJECT_ROOT/config.yaml` 不存在，靠 fallback 意外加载 v4.1）
- 关键建议：至少使用 best_threshold=0.344 替代默认阈值 0.5 使 DrawExpert best_f1=0.4265 生效

### ✅ 质量门神（QA测试）
- 核心判断：82/100，Conditional Go。11/11 P0 修复全部验证通过，测试套件 471/471 通过
- 关键发现：SKY 通道返回恒定值——三组不同赔率返回完全相同的值（0.310/0.336/0.354），疑似 fallback 模式。VIP 在测试环境正常但生产环境 server.err.log 曾返回 0.000/0.000/0.000
- 关键建议：VIP 生产环境验证 + SKY 恒定值排查

---

## 2. 11 项 P0 修复逐项验证（三位成员交叉确认）

| # | 修复项 | 文件 | 产品官 | 排障手 | 质量门神 | 综合判定 |
|---|--------|------|--------|--------|---------|---------|
| 6 | asyncio→_asyncio NameError | backend/main.py:1120 | ✅ | ✅ | ✅ | ✅ 彻底 |
| 9 | D-Gate spread<1.6→<0.16 | backend/main.py:600,619,660 | ✅ | ✅ | ✅ | ✅ 彻底 |
| 14 | DrawExpert 硬编码→等权 | unified_predictor.py:565-569 | ✅ | ✅ | ✅ | ✅ 彻底 |
| 18 | config.yaml model_path v4.1 | config/config.yaml:603 | ⚠️ | ⚠️ | ✅ | ⚠️ 部分 |
| 11 | SKY 键名映射 | sky_predictor.py:176-182 | ✅ | ✅ | ✅ | ✅ 彻底 |
| 12 | VIP 键名映射 | vip_final.py:718-720 | ⚠️ | ✅ | ✅ | ⚠️ 部分 |
| 13 | 补全 trap_probability_bridge + odds_inverse_calibrator | predictors/components/ | ✅ | ✅ | ✅ | ✅ 彻底 |
| 8 | 硬编码路径→环境变量 | unified_predictor.py:48 | ⚠️ | ⚠️ | ✅ | ⚠️ 部分 |
| 7 | OCR requests→httpx | api/ocr.py:72 | ✅ | — | ✅ | ✅ 彻底 |
| 15 | 测试编码修复 | tests/test_v4_modules.py | ✅ | — | ✅ | ✅ 彻底 |
| 17 | ensemble_trainer.py 源码恢复 | predictors/components/ | ✅ | ✅ | ✅ | ✅ 彻底 |

### 部分修复详情

**#18 config.yaml model_path（⚠️ 部分修复）**：
- config.yaml 已更新为 v4.1 ✅
- 但 ModelBridge 读取 `_PROJECT_ROOT/config.yaml`（不存在！），实际靠 fallback 意外加载 v4.1
- 两个物理"v4.1"文件结构不同：`models/main/` 版 10.5MB/16 keys vs `saved_models/` 版 4.5MB/25 keys
- `backend/core/config.py` 默认模型名仍为 v3.2 名称

**#12 VIP 键名映射（⚠️ 部分修复）**：
- 同时返回 `probs` 和 `probabilities` ✅
- 但 `probabilities` 用大写格式 `{H, D, A}`，引擎日志用 `vp.get('home', 0)` 无回退
- 功能可用但日志误导，生产环境曾返回 0.000

**#8 硬编码路径（⚠️ 部分修复）**：
- `unified_predictor.py:48` 改为 `FOOTBALLAI_ROOT = os.environ.get(...)` ✅
- 但 fallback 仍为 `r"D:\AI\footballAI"`，sky_predictor 和 vip_final 仍有直接硬编码
- 3 个文件仍直接引用 `D:/AI/footballAI`

---

## 3. 仍存在的 P0 问题（用户未提及，确认仍存在）

| # | 严重度 | 问题 | 详情 | 来源 |
|---|--------|------|------|------|
| P0-13 | 🔴 | 模块包遮蔽 | 三个生产预测器仍执行 `sys.path.insert(1, FOOTBALLAI_ROOT)`，导致 `import modules` 加载 footballAI 的 modules/ 而非 v4.0 的。实测 `from modules.feedback_loop import ...` 失败 | 产品官+排障手 |
| P0-16 | 🟠 | 模型 MCC=0 | `model_registry.json` 中 v4.1 的 MCC=0（随机猜测级别），对比 v3.1 的 MCC=0.2903，模型质量退化 | 产品官 |
| DrawExpert L2 | 🔴 | DrawExpert 未重训 | F1=0.0（默认阈值 0.5），AUC=0.599（接近随机）。存在 best_f1=0.4265（阈值 0.344）但代码未使用 | 排障手 |

---

## 4. 新发现的问题（修复后回归 + 新隐患）

| # | 严重度 | 类别 | 问题描述 | 来源 |
|---|--------|------|---------|------|
| NEW-1 | 🔴 | 模型 | 两个"v4.1"模型文件结构不一致——UnifiedPredictor 加载 `models/main/` 版（10.5MB, 16 keys），ModelBridge 加载 `saved_models/` 版（4.5MB, 25 keys），预测不一致风险 | 产品官 |
| NEW-2 | 🟠 | 功能 | SKY 通道返回恒定值——三组不同赔率返回完全相同的值（0.310/0.336/0.354），疑似 fallback 模式而非真实预测 | 质量门神 |
| NEW-3 | 🟠 | 架构 | NN 模型架构脚本 `train_neural_net.py` 缺失——L5 神经网络层从未工作 | 排障手 |
| NEW-4 | 🟠 | 配置 | ModelBridge 配置路径不匹配——读取 `_PROJECT_ROOT/config.yaml`（不存在），靠 fallback 意外加载 v4.1，重构会静默断裂 | 排障手 |
| NEW-5 | 🟡 | 配置 | `backend/core/config.py` 默认模型名仍为 v3.2 名称 | 排障手 |
| NEW-6 | 🟡 | 清理 | `models/` 目录仍有 18MB 未清理（models/main/ 10.5MB + models/multi_market/ 6.1MB） | 质量门神 |
| NEW-7 | 🟡 | 日志 | VIP 返回大写键 `{H,D,A}` 但引擎日志用 `vp.get('home',0)` 无回退，功能可用但日志误导 | 产品官 |
| NEW-8 | 🟠 | 代码 | **prediction_service 死代码复活（修复引入）**——`get_de_output()` 原恒返回 None（死代码），v4.1 模型含 draw_expert 键使其返回实际值 (~0.298)，D-specialist 融合公式 `0.30 * de_pdraw` 现在影响预测结果——行为变化需验证 | 产品官+排障手 |
| NEW-9 | 🟠 | 代码 | **D-Gate 第四处实现不一致**——`chat_endpoint` line 823 用 `abs(oh_p - oa_p) < 1.6`（赔率差），与 `_build_analysis_card` 的 `spread < 0.16`（概率差）是不同指标。#9 修复仅改了 3 处，第 4 处未触及 | 产品官 |

---

## 5. DrawExpert 四层根因修复状态（终版）

| 层次 | 修复前 | 修复后 | 状态 | 备注 |
|------|--------|--------|------|------|
| **L0 代码** | 硬编码 `[0.33,0.34,0.33]` | 等权 `[1/3,1/3,1/3]` + 降级日志 | ✅ 已修复 | 消除 0.068 恒定平局偏置 |
| **L1 配置** | config.yaml 指向 v3.2 | config.yaml 指向 v4.1 | ⚠️ 部分 | ModelBridge 配置路径不匹配，靠 fallback |
| **L2 模型** | F1=0.0, 输出常数 0.34 | F1=0.0, AUC=0.599 | ❌ 未修复 | 模型未重训，但 best_f1=0.4265（阈值 0.344）未使用 |
| **L3 架构** | ensemble_trainer.py 缺失 | 2509 行源码恢复 | ✅ 已修复 | import 正常，隐式依赖消除 |

**关键建议**：L2 虽未重训，但可立即使用 `best_threshold=0.344` 替代默认阈值 0.5，使 DrawExpert best_f1=0.4265 生效——这是性价比最高的临时方案。

---

## 6. 三通道架构验证

| 通道 | 修复前 | 修复后 | 端到端测试 | 遗留问题 |
|------|--------|--------|-----------|---------|
| **Unified** | ✅ 正常 | ✅ 正常 | H=40%/D=35%/A=26% | 无 |
| **SKY** | ❌ 返回 0.000 | ✅ 返回非零 | H=31%/D=34%/A=35% | ⚠️ 三组不同赔率返回恒定值，疑似 fallback |
| **VIP** | ❌ NameError | ✅ 无 NameError | H=37%/D=28%/A=35% | ⚠️ 生产环境曾返回 0.000，键名格式大写 |

---

## 7. 清理验证

| 项目 | 用户声称 | 实际验证 |
|------|---------|---------|
| saved_models/ | 6 个模型文件，10.5MB | ✅ 6 个文件 + 1 scaler，~11.2MB |
| v3.2/v4.0 旧模型 | 已删除 | ✅ 确认无旧模型 |
| 死代码 unified_predictor.py | 已删除 | ✅ 确认已删除 |
| models/ 目录 | 未提及 | ⚠️ 仍有 18MB 未清理 |
| __pycache__ | 已清理 | ✅ 确认已清理 |

---

## ✅ 行动清单

| # | 行动 | 负责方 | 紧急度 | 期望完成 |
|---|------|--------|--------|---------|
| 1 | **修复 P0-13 模块包遮蔽**：移除三个预测器中的 `sys.path.insert(1, FOOTBALLAI_ROOT)`，或实施 `pip install -e .` 使项目内 modules 优先。临时方案：设置 `FOOTBALLAI_ROOT` 环境变量 | 后端工程 | P0 | 上线前 |
| 2 | **统一模型文件**：确认 UnifiedPredictor 和 ModelBridge 加载同一个 v4.1 模型文件，消除"v4.1-main vs v4.1-saved"分裂 | ML 工程 | P0 | 上线前 |
| 3 | **DrawExpert L2 临时方案**：将默认阈值从 0.5 改为 best_threshold=0.344，使 best_f1=0.4265 生效（一行修改） | ML 工程 | P0 | 上线前 |
| 4 | **ModelBridge 配置路径修复**：修复 `agents/model_bridge.py:24` 的 `_PROJECT_ROOT/config.yaml` 路径指向 `config/config.yaml`，消除 fallback 意外生效 | 后端工程 | P0 | 上线前 |
| 5 | **prediction_service 行为变化验证**：`get_de_output()` 返回实际值 (~0.298) 后，D-specialist 融合公式 `0.30 * de_pdraw` 开始影响预测结果，需验证行为是否符合预期 | ML 工程 | P1 | Day 2 |
| 6 | **D-Gate 第四处修复**：`chat_endpoint` line 823 的 `abs(oh_p - oa_p) < 1.6`（赔率差）与 `_build_analysis_card` 的 `spread < 0.16`（概率差）指标不一致，需统一 | 后端工程 | P1 | Day 2 |
| 7 | **SKY 恒定值排查**：检查 SKY 通道是否走了 fallback 路径，特征构建是否正常 | ML 工程 | P1 | Day 2 |
| 8 | **VIP 生产环境验证**：在生产 venv 中确认 VIP 不返回 0.000，统一键名格式为小写 | 后端工程 | P1 | Day 2 |
| 9 | **清理 models/ 目录**：删除 models/main/ 和 models/multi_market/ 中的冗余模型（18MB） | 运维 | P2 | Day 3 |
| 10 | **补全 train_neural_net.py**：恢复 NN 模型架构脚本，使 L5 神经网络层可用 | ML 工程 | P2 | Day 3 |
| 11 | **更新 backend/core/config.py**：默认模型名从 v3.2 更新为 v4.1 | 后端工程 | P2 | Day 3 |

---

## ⚠️ 待完善 / 已知局限

- 本报告忽略所有安全问题（认证禁用、密钥硬编码、速率限制、SSL 等），安全问题需另行处理
- DrawExpert L2 重训是长期方案，本次验证仅确认临时方案（阈值调整）可行
- SKY 恒定值问题需进一步排查是否为冷启动预期行为
- VIP 生产环境 0.000 问题需在真实生产 venv 中验证
- 三位成员均未覆盖性能测试和压力测试

---

## 📚 成员产出索引

- **gstack-product-reviewer（产品官）**：代码审查报告，8 项彻底修复 / 3 项部分修复 / 2 个回归问题 / 2 个遗留 P0。评分 6.5/10。**交叉验证追加**：确认排障手 5 项功能审计发现全部成立，新增 prediction_service 死代码复活（NEW-8）和 D-Gate 第四处不一致（NEW-9），上线前必须修复项从 2 项增至 4 项。报告：`deliverables/gstack/postfix-code-review-2026-06-20.md`
- **gstack-investigator（排障手）**：功能审计报告，DrawExpert 四层根因修复状态确认（L0✅/L1⚠️/L2❌/L3✅），三通道全部激活，3 个新发现。评分 6.5/10。报告：`deliverables/gstack/postfix-functional-audit-2026-06-20.md`
- **gstack-qa-lead（质量门神）**：QA 测试报告，471/471 通过，11/11 P0 验证通过，D-Gate 三场景验证通过，8 项非阻塞问题。评分 82/100。报告：`deliverables/gstack/postfix-qa-test-2026-06-20.md`

---

> 本报告由软件工坊 AI 协作生成，关键决策请由工程负责人复核。
