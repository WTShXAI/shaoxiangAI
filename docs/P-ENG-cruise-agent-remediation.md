# P-ENG · 自主巡航 Agent 修复评估（WINDOW 备料 · 纯规格）

> 类型：**纯规格文档**，本周期**不落地任何生产代码**。
> 来源任务：T30（只读盘点发现自主巡航 Agent 静默死亡）→ 本任务 T30-B 做修复评估。
> 纪律基线：`docs/DISCIPLINE.md` §1 诚实 / §9 跨庄禁区 / §4 数据资产保全 / §6 进程安全。
> 落地性质：修 `bridge_service.py` 属**生产代码**，须维护窗口 + 全量回归，不在自主巡航周期执行。

---

## 0. 结论（先给答案）

| 项 | 判定 |
|---|---|
| 故障现状 | **仍在线**。在线 bridge 进程（PID 3892/34288，启动于 2026-09-22 20:38:05）从未成功启动 cruise；cruise 缺席已 **~4 天** |
| 根因 | `bridge_service.py:1037` 向 `start_cruise(cross_book=_get_cross_book_signal)` 传参，而该函数已于 2026-09-19 随 IR-32 跨庄禁令删除 → 调用表达式求值时抛 `NameError` |
| 为何静默 | 同处 `try/except Exception as e: logger.warning(...)`（bridge_service.py:1040-1041）把启动失败降级为 WARNING；无健康检查、无告警、无回归测试 |
| 诚实影响评估 | **无 edge 损失**。cruise 只做"硬规则判定 → 本地 qwen3 叙事 → WS 广播"，护栏第 1 条明写"LLM 不参与下注决策"，不接执行链路、不写库。0 产出不污染验证台、不影响 P0 FAILED 判定 |
| 真实损失 | **产品面**：临场/滚盘"异动告警"能力自 09-19 02:44 起完全无输出，且无任何 UI 消费它 |
| 修复优先级 | **中低**（不阻断系统 GO）。但**修了也没人看**：`/api/agent/alerts`（bridge_service.py:8041）在 `frontend/src/` 与 `tests/` 中 **0 引用** |
| 建议路径 | 先做 **A 最小修复 + 监控**（低成本、零风险），再决策是否投入 **C 消费端**（前端挂载告警面板） |

---

## 1. 实证时间线（全部带证据位置）

| 时间 | 事件 | 证据位置 |
|---|---|---|
| 2026-08-28 09:30 | cruise 成功启动（手动启动期） | `logs/_manual_bridge_err.log:22,28` |
| 2026-09-19 02:44:12 | **最后一次成功启动**：`[cruise] autonomous cruise agent started`；同秒 `agent_cruise` 亦记 `interval=60s` | `logs/backend_daemon.log:23,33` |
| 2026-09-19 03:33:31 | **首次启动失败**：`name '_get_cross_book_signal' is not defined` | `logs/backend_daemon.log` |
| 2026-09-19 起 | 每次 bridge 重启均复现，累计 **19 次**失败（分布：backend_daemon.log 13 / bridge_watchdog_err.log 5 / backend_direct.log 1） | `logs/*` 全量 grep |
| 2026-09-22 20:38:06 | **最后一次失败** | `logs/bridge_watchdog_err.log:5` |
| 2026-09-22 20:38:05 | 当前在线 bridge 进程启动（PID 3892 → worker 34288），此后无重启 | `Get-CimInstance Win32_Process` |
| 2026-09-26 22:08 | watchdog 仍报 HEALTHY；cruise 缺席状态持续 | `logs/bridge_watchdog.log` 尾部 |

**关键推论**：最后 19 次失败中的最后一次（09-22 20:38:06）与当前在线进程的启动时间（09-22 20:38:05）**相差 1 秒**——即当前在线的 bridge 就是"带着 cruise 缺失"起来的，故障**未修复且仍在线**，不是历史遗留。任何一次后续 bridge 重启都会再次复现同一 NameError。

---

## 2. 故障机制（为什么能静默 7 天无人发现）

```
bridge startup (bridge_service.py:1032)
  try:
      from agent_cruise import start_cruise      # ← import 成功（agent_cruise 本身健康）
      _asyncio.create_task(start_cruise(
          broadcast=ws_manager.broadcast,
          analyze=_live_operator_card_compute,
          cross_book=_get_cross_book_signal,     # ← :1037 此处求值 → NameError
      ))
      logger.info("[cruise] ... started")        # ← 永不执行
  except Exception as e:
      logger.warning("[cruise] cruise agent start failed: {e}")   # ← :1040 降级为 WARNING
```

三层"沉默放大器"：

1. **异常被降级**：`Exception` 全捕获 + `WARNING` 级别。一个后台常驻 Agent 没起来，与"预热失败""探测跳过"同级别，看日志的人会滑过去。
2. **无存活断言**：`/health`（bridge_service.py:1240）只报 `engine` + DB 探活，不含任何 cruise 字段；`scripts/autonomous_monitor.py:48` 只测 `/health` 可达 → **可达即 OK**，cruise 缺席与"cruise 正常"在监控面上完全同形。
3. **无回归保护**：`tests/` 对 `agent_cruise` 与 `bridge_service.py:1031-1041` 启动块 **0 引用**（仅 T30 的只读审计脚本在测试里引用了这行字符串作为静态样例）。删函数时无人报警。

**另**：`agent_cruise.py:21`（模块 docstring 内）仍写着 `cross_book = _get_cross_book_signal`。经 AST 核验（模块 docstring 位于 2–22 行），该处**只在文档串里，不产生运行时 NameError**——属"文档悬空引用"，应在方案 B 中一并清掉以免误导。

---

## 3. 影响面（诚实口径）

| 维度 | 评估 | 依据 |
|---|---|---|
| 交易 edge | **零损失** | cruise 不产概率、不下注；护栏第 1 条"LLM 不参与下注决策"，决策源为 value-layer 硬规则 |
| 数据资产 | **零写入** | `agent_cruise.py` 只读 events.db（扫描），产出仅内存 deque(maxlen=200) + WS 广播；对 events.db **零写入**，不触 §4 |
| 验证台 / G1-G6 | **不受影响** | 不进 `verification.db`，样本累积（KNN 2779 / market 6082 / candles_ensemble 181）与 cruise 无关 |
| 校准漂移 / retrain_gate | **不受影响** | `retrain_gate.suggest=true`（new_verdicts 540 / ll_delta 越 0.03 线）由 `autonomous_monitor` 独立判定，与 cruise 无关 |
| 已发布结论 | **无变化** | P0 诚实终判定 FAILED、三态全 NO EDGE/INCONCLUSIVE 与 cruise 无因果关系 |
| 产品完整性 | **有缺失** | 临场异动告警自 09-19 02:44 起 0 产出；即便修好，**前端也没有消费方** |

**诚实声明**：本节判定"零损失"基于"cruise 从未产生任何可交易输出"这一事实（0 产出 > 0 可计损失）。若未来为 cruise 接入执行或展示，损失口径须重新评估。

---

## 4. 修复方案（三选一，均为生产代码改动 → WINDOW）

### 方案 A · 最小修复（推荐先做，风险最低）

目标：让 cruise 以**合规无跨庄**形态重新上线。

1. `bridge_service.py:1037` 删除 `cross_book=_get_cross_book_signal,` 整行。
2. 依赖已满足：`start_cruise(broadcast, analyze, cross_book=None, ...)`（`agent_cruise.py:222`）**默认即 None**，且 `None` 路径完全安全——
   - `_run_round` 167-177 行 `if cross_book:` 直接跳过跨庄分支；
   - `_narrate`/`_template_narrate`（87-130 行）在 `cross_book` 为空时不拼接跨庄话术；
   - 告警的 `reasons` 仍由 `value-layer {decision}` + `trap {trap}` 两条硬规则驱动。
   → **运行期行为与 IR-32 完全兼容**，不需要改动 `agent_cruise.py` 任何一行。
3. 优先级护栏（防复发）：在 `agent_cruise._narrate` 的 LLM prompt 载荷里，**永久移除 `"cross_book": cross_book`（117 行）**——即使将来有人把跨庄信号接回来，LLM 叙事也不得触碰跨庄内容。

改动面：`bridge_service.py` 1 行删除 + `agent_cruise.py` 1 行删除。回滚成本：double。

### 方案 B · 彻底修复（清残骸，WINDOW 内一次做完）

在 A 基础上，清除全部跨庄残留并补守卫：

| 项 | 位置 | 动作 |
|---|---|---|
| 悬空文档引用 | `agent_cruise.py:21` docstring | 改为 `cross_book = None（2026-09-19 起永久禁用, IR-32）` |
| 跨庄叙事分支 | `agent_cruise.py:87-100, 167-177` | 删除 `n_soft_lines` / `max_spread_pp` 相关拼接与阈值判断 |
| 告警载荷 | `agent_cruise.py:198` | `"cross_book": cb` 一并移除 |
| 启动块 | `bridge_service.py:1031-1041` | 重写为显式 `cross_book=None` + ERROR 级失败 + 全局状态位 |
| 防复发守卫 | `tests/` 新增 | 静态断言：`bridge_service.py` 与 `agent_cruise.py` 中**不得出现** `_get_cross_book_signal` / `cross_book` 符号（fail-closed） |

### 方案 C · 只修监控，代码不动（零风险折中）

不改 bridge/agent 代码，仅把"cruise 是否起来"变成可观测：

- `/health` 增加 `"cruise": {"started": bool, "last_round_ts": ..., "rounds": n}`；
- `scripts/autonomous_monitor.py:48` 附近增加 cruise 断言：`started=False` 或 `last_round_ts` 超 5 分钟 → 计为告警项（现状 monitor_status 里 `bridge: ok` 不会因此变色）。

### 方案对比

| | 改动面 | 风险 | 能恢复告警 | 能防复发 | 建议 |
|---|---|---|---|---|---|
| A | 2 行 | 极低 | ✅ | ❌（还会再犯） | **立即排 WINDOW** |
| B | ~6 处 | 中（生产代码 + 全量回归） | ✅ | ✅ | A 之后同窗口做 |
| C | 2 处（非 cruise 主链路） | 低 | ❌（仍无告警产出） | ✅（可观测） | 可与 A 同窗口 |

**建议组合**：C 先上（可与监控同窗口，零 cruise 链路风险）→ A 同窗口 → B 下次窗口清理。

---

## 5. 监控方案（fail-closed，替代"Warning 吞掉"）

现状缺陷：`/health` 报可达，但报不出"后台常驻任务是否活着"。

1. **状态位**：bridge startup 块用模块级 `_CRUISE_STATE = {"started": False, "last_round_ts": None}`；`start_cruise` 成功后置 `True`；`_run_round` 每轮换 ts。`start_cruise` 失败时**置 False 并 `logger.error`**，不得只 WARNING。
2. **健康检查**：`/health` 增 `checks["cruise"]`；`started=False` 时 `ok=False` → 整体 `degraded`（而非 `healthy`）。
3. **外部断言**：`scripts/autonomous_monitor.py` 在现有 `/health` 探活后追加 cruise 断言，`autonomous_monitor --cycle` 会把结果写进 `reports/monitor_status.json`，从而让"后台 Agent 死了"进入老板可见的状态面。
4. **禁止项**：`prod_guardian.py` 只负责"拉起/保活"，**不得**因 cruise 状态异常去重启 bridge（重启会打断采集与飞轮；§6 只拉起不杀，且重启不是 cruise 的解药——重启必然复现同一 NameError，A 修好前重启 = 白重启）。

---

## 6. 红线对照

| 红线 | 本方案满足情况 |
|---|---|
| §1 诚实 | 本节所有影响评估基于"cruise 0 产出"实测事实；不宣称"修好就有了 edge"——修的只是产品完整性 |
| §9 跨庄禁区（IR-32） | A/B 方案**彻底断掉** cruise 侧跨庄信号来源；并把 `_narrate` prompt 载荷中的 `cross_book` 移除，**杜绝 LLM 叙事回流跨庄内容**。B 方案加静态守卫防符号复活 |
| §4 数据资产保全 | 全部改动为代码，不涉及任何 DB 写入；cruise 只读 events.db 且方案不改其读法 |
| §6 进程安全 | 不杀任何进程；明确禁止 guardian 因 cruise 异常重启 bridge |
| AI 不擅售 | 不涉及 |

---

## 7. WINDOW 执行前置清单（落地时才需要，本周期不执行）

- [ ] 前置 1：`bridge_service.py:1031-1041` 改动需**维护窗口**（当前在线 bridge 自 09-22 运行，重启会短暂中断前端/WS）。
- [ ] 前置 2：执行后必跑全量回归 `tests/`（当前基线 **231 passed**），新增 cruise 守卫用例须回车。
- [ ] 前置 3：重启 bridge 后核对日志出现 `[cruise] autonomous cruise agent started (interval=60s)`，且 60s 内出现 `[cruise] round error` 之外的正常轮次日志（无目标时静默属正常，`_scan_targets()` 空返回 0 不算故障）。
- [ ] 前置 4：若同时上方案 C，`/health` 契约变化需同步 `scripts/autonomous_monitor.py` 解析处（避免解析异常把 bridge 报成 dead）。
- [ ] 回滚：回滚 bridge_service.py 1 行 + agent_cruise.py 1 行即可回到当前状态，无需数据回滚。

---

## 8. 验收口径（如何知道真修好了）

| 指标 | 当前 | 修好后 |
|---|---|---|
| 启动日志 | 19 次 `cruise agent start failed` | `[cruise] autonomous cruise agent started (interval=60s)` |
| `/health` | 无 cruise 字段 | `checks.cruise.started=true` |
| 告警产出 | 0（自 09-19 02:44 起） | 临场/滚盘达阈值场才推；**无阈值场不推属正常**，不得用"推了几条"当验收指标 |
| 前端可见 | 无消费方 | 挂载 `/api/agent/alerts` 后看板才有意义（方案 C 之外的事项，须产品决策） |

---

## 9. 明确不做（本周期及建议不做）

- 不做"恢复跨庄信号"，IR-32 永久禁令不因本任务松动。
- 不为 cruise 重跑验证台、不重训模型、不写 verification.db。
- 不为 cruise 引入执行链路（下注/建仓），护栏第 1 条保持。
- 不建议在无人看板前投入 C（前端挂载）——先决策"这个告警谁看"。

---

## 附：证据复现命令

```bash
PY=.venv/Scripts/python.exe
# 失败日志全量
grep -rn "_get_cross_book_signal" logs/            # 19 处
grep -rn "_get_cross_book_signal" bridge_service.py agent_cruise.py
# 成功日志（最后一次）
grep -rn "cruise] autonomous cruise agent started" logs/
# 在线 bridge 启动时间（只读）
Get-CimInstance Win32_Process -Filter "Name like 'python%'"   # 看 bridge_service.py 的 CreationDate
# 消费者盘点（前端/测试）
grep -rn "agent/alerts\|agentAlerts" frontend/src tests/
```
