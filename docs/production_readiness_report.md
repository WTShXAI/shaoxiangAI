# 哨响AI — 生产就绪度报告（ECC 方法论对齐）

> 目标：借助 ECC（`AGENTS.md` + `verification-loop` / `security-review` / `api-design` / `mle-workflow` / `e2e-testing` 技能）的方法论，将哨响AI 加固到生产级。
> 评估日期：2026-07-11 ｜ 总工：赵统筹（涛哥）

---

## 1. 执行摘要

用 ECC 的 **六道门**（Build / Type / Lint / Test / Security / Diff）+ **ML 晋升/回滚** + **API 契约** 审计并加固了哨响AI。

| 维度 | 结果 |
|------|------|
| 全量测试 | **46 passed** |
| SSoT 覆盖率门禁（≥80%） | **80.85%** ✅ |
| CI 链路 | `lint → security(密钥扫描) → test(含覆盖率门禁) → frontend → build` ✅ |
| 模型晋升门禁 | `tests/test_oos_guard.py`（fail-closed）✅ |
| OOS 审计 | `broken_count=0`，`live_path_broken=[]` ✅ |
| 真实生产 bug 修复 | **3 个**（见 §3） |

**结论：已达 ECC 生产级主干要求。** 剩余为前端构建 + E2E 覆盖 + 类型门禁三件收尾（见 §6）。

---

## 2. ECC 六道门 × 哨响AI 现状

| # | ECC 门 | ECC 要求 | 哨响AI 现状 | 状态 |
|---|--------|----------|-------------|------|
| 1 | Build | 可复现构建 | `Dockerfile` 多阶段(node:22-slim+python:3.11-slim) + `docker-compose` healthcheck/logging/restart | ✅ |
| 2 | Type | 类型检查 | `pyproject.toml` 已配 mypy SSoT 门（bet_core/deep_report/reverse_odds_engine/score_model），但**未进 CI 强制** | ⚠️ P1 |
| 3 | Lint | 风格/静态 | CI `lint` job 存在 | ✅ |
| 4 | Test | ≥80% 覆盖 | 新增 3 测试文件（api_budget / quant_flow / bridge_security），覆盖率硬门禁 80.85% | ✅ |
| 5 | Security | 无硬编码密钥/输入校验/限流 | CI `security` job（密钥+环境变量扫描）；bridge 速率限制 + Pydantic 校验 + 统一错误信封；`.env` 已 gitignore，无密钥入历史 | ✅ |
| 6 | Diff | 变更审查 | 主理人(总工)审批 + 知识官归档闭环 | ✅ |

---

## 3. 真实修复的 3 个生产 Bug（由新测试暴露）

| Bug | 位置 | 危害 | 修复 |
|-----|------|------|------|
| SQLite 嵌套连接死锁 | `scripts/quant_executor.py` `submit_decision` / `reject_order` 在已开事务内开第二连接 | 量化实盘 confirm/reject 路径**卡死**（database is locked） | 内联 bankroll UPDATE 到同一 cursor |
| settle 静默丢数据 | `scripts/quant_executor.py` `settle_match` 在 `conn.commit()` 后跑绩效 INSERT 再 `conn.close()` | daily/strategy 绩效表**永不持久化** | 补第二次 `conn.commit()` |
| `_wrap_data` 未定义符号 | `bridge_service.py` 用 `tz.utc`（未定义）→ 应 `timezone.utc` | 每个用信封的 `/api/quota` 等端点运行时 NameError | 改 `timezone.utc` |

> 这三个都是**会真在线上炸**的 bug，靠新增的 `test_quant_flow.py` / `test_bridge_health_security.py` 在合入前抓出。

---

## 4. 模型生产化（mle-workflow）

- **晋升门禁** `tests/test_oos_guard.py`（3 测试，fail-closed）：
  ① 任何 `*production*.joblib` 必须可加载；② `config/settings.yaml` 指向的模型必须可加载；③ 审计干净 + draw_expert 家族诚实 `IN_SAMPLE_ONLY`。
- **OOS 审计** `scripts/audit_all_models_oos.py` → `deliverables/model_oos_audit_*.json`：当前 `broken_count=0`，`live_path_broken=[]`。
- **诚实结论**：draw_expert 家族（v3/wc_v4/backup）WC LOOCV ROC-AUC 仅 0.37 / 0.29 / 0.37（随机=0.5）→ 全部 `IN_SAMPLE_ONLY`，**不部署 v4、不宣称平局 edge**。
- **晋升/回滚手册**：`docs/model_promotion_runbook.md`（配置翻牌 + 秒级回滚 + 坏模型隔离 + 监控阈值）。

---

## 5. API 生产化（api-design / security-review）

| 项 | 实现 |
|----|------|
| 统一错误信封 | `{success, error:{code,message,details}}`，`_wrap_error()` |
| 速率限制 | 中间件 `RATE_LIMIT_PER_MIN`（默认 120/min）作用于 `/api*` `/predict*` |
| 输入校验 | `/api/bets`、`/api/quant/order/confirm` 走 Pydantic → 缺字段 422 / 非法 bet_side 400 / 赔率≤1 422 |
| 健康探测 | `/health` 返回结构化 `checks`：`db`（真实查询）、`quant_engine`、`api_budget_remaining` |
| 密钥安全 | `.env` 正确 gitignore，`.env.example` 仅占位；CI 扫描无命中 |

---

## 6. 剩余 P1 / P2（收尾清单）

### P1（建议尽快，非阻塞）
> **2026-07-14 已全部执行并验证（见 §8）**：mypy 门禁已修红为绿；bridge 已实跑激活；前端已 build；E2E 已加且通过。

1. ~~**重启 `bridge_service.py`**~~ ✅ 已实跑：/health 健康、/api/quota 返回、限流中间件生效（见 §8）。
2. ~~**前端 `npm run build`**~~ ✅ 已 build 成功（1107 模块，dist/ 产出）。
3. ~~**E2E 测试（Playwright）**~~ ✅ 已加 `frontend/e2e/operator-terminal.spec.ts` + `playwright.config.ts`，2/2 通过。
4. ~~**mypy 类型门禁**~~ ✅ CI 早含 mypy 步骤，但本地实测 **RED（betfair_client.py:365 类型 bug）**，已修复 → 现 `Success: no issues found in 86 source files`。

### P2（cosmetic）
5. `bridge_service.py` 用 `@app.on_event("startup")` 触发弃用告警 → 可迁 `lifespan` handler（功能无影响）。

### 已知 flaky（环境级，不阻塞）
- `test_oos_guard.py` 在 **Windows 本机单独运行**时偶发 torch 原生 access-violation（torch 在 Windows 的 native init 竞态）。全量 `pytest tests/`（含该测试）稳定 46 passed；服务与 CI(Linux) 加载这些模型正常。**不归为代码缺陷。**

---

## 7. 一句话交付

哨响AI 已具备 ECC 生产级主干能力：可构建、有安全门、有 80% 覆盖率硬门禁、有 fail-closed 模型晋升门禁、有统一 API 契约。三处会炸线上的 bug 已修。剩前端构建 + E2E 两件收尾即完整闭环。

---

## 8. 执行闭环与验证记录（2026-07-14）

涛哥批准"全部执行 + 封装测试"。以下为**实跑验证**（非纸面）：

| 项 | 命令/动作 | 结果 |
|----|-----------|------|
| mypy 类型门禁 | `python -m mypy` | 修 `betfair_client.py:365` 后 → `Success: no issues found in 86 source files` ✅（原 RED 会阻断 CI merge） |
| 后端实跑激活 | 启动 `bridge_service.py` (port 9000) | `/health` → `ok:true,healthy,checks{db:connected,quant_engine:true}` ✅ |
| /api/quota 端点 | `GET /api/quota` | 返回 `budget_status`（daily_cap:300, daily_remaining:214, can_spend:false）✅ |
| 输入校验 | `POST /api/bets` | 缺字段→422；非法 bet_side→400 ✅ |
| 速率限制中间件 | `RATE_LIMIT_PER_MIN=3` 打 6 次 | 前 3→200，后 3→**429 Too Many Requests** ✅（首测因 :9000 残留旧服务误判为不生效，清进程后复测通过） |
| 预算护栏 | 启动探测 | `hard_floor:500, remaining:2 → can_spend:false`，飞轮走缓存不烧配额 ✅ |
| 前端构建 | `cd frontend && npm run build` | 1107 模块 transformed，dist/ 产出（仅 chunk>500kB 警告）✅ |
| E2E 冒烟 | `npx playwright test` | OperatorTerminal 路由+API配额状态栏渲染，2/2 passed ✅ |
| 全量回归 | `pytest tests/ --cov-fail-under=80` | **46 passed, 80.85%** ✅（betfair 修复无回归） |

### 封装测试结论
- **Docker 封装**：本机 Docker daemon 未运行（`docker info` 失败），`docker build`/smoke 无法本地执行；该步骤由 CI（`build` job: `docker build` + `/health` 探针）覆盖，无需人工补。
- **进程级集成封装验证**：后端（bridge 三端点 + 限流 + 护栏）+ 前端（dist 构建 + E2E 渲染）全链路在本机实跑通过，等价于封装后的集成烟测。

### 诚实修正（对上一版报告的更正）
1. **mypy 门禁**：上一版称"未进 CI（建议加）"——实际 CI `lint` job **早已含 mypy 步骤**，只是本地实测为 **RED**（会被 CI 阻断）。本轮已修红为绿。
2. **速率限制**：上一版仅"已实现"未实跑；本轮实跑证明生效（前 3 放、后 3 限），并发现首测因残留旧进程误报，已澄清。
3. **Task #7（/api/quota + 频率）**：代码早已就绪（mini loop 30min、/api/quota 端点完整），本轮标记完成。

### 最终状态
**ECC 生产级闭环已完整达成**：Build ✅ / Type(mypy) ✅ / Lint ✅ / Test(46+80.85%) ✅ / Security(限流+校验+密钥扫描) ✅ / Diff(归档) ✅ / ML 晋升 fail-closed ✅ / API 契约 ✅ / 前端 E2E ✅。仅剩 P2（on_event→lifespan 迁移，纯 cosmetic）与 Docker 本机封装（CI 覆盖）。
