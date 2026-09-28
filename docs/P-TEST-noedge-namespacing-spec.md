# P-TEST 信号词 `NO_EDGE` 与三态判定的命名隔离规格（T58，承接 T51 §2 / T53 §2 / T57）

> 状态：**只读盘点 + 规格**。本轮**未改任何生产文件**，未碰 `events.db`、未跑验证台、
> 未写 `verification.db`、未挂调度、零进程操作。
> 产出：`scripts/audit_noedge_namespacing.py` + `tests/test_audit_noedge_namespacing.py`（29 passed）
> + `reports/noedge_namespacing_audit.{json,md}`（判定 **FAIL**：唯一 RED `R5_RENAME_NEEDS_MIGRATION`）。

---

## 0 一句话结论

`NO_EDGE`（信号词）与 `NO EDGE`（三态判定）只差一个字符，但**改名的成本不在代码层，而在数据层**：
信号词已经落进 `data/watch_verdicts.json`，所以「改名」不是改字面量，而是**一次带读侧兼容的数据迁移**。
唯一零爆炸半径的改名点（`scripts/backtest_ou_signal.py`）实测**零调用方**——
**先改死代码面，活词表改名进 WINDOW**，与「不改既有结论」的纪律一致。

---

## 1 三问的答案

### Q1 信号语义出现面 / 调用方

| 层 | 命中 | 角色 |
|---|---|---|
| `analysis/`（**活生产层**，被 `bridge_service.py:83` 实时导入） | 20 | 生产者：`live_goal_probe.py`(18) / `live_score_conditional.py`(2) |
| `scripts/`（无目录前缀豁免） | 14 | ①复刻脚本 `backtest_ou_signal.py`(1，**零调用方**)；②字符串比较 `audit_chain_consistency.py`(5)；③SSoT 登记册引用 `verdict_guard_ssot.py`(5)；④审计自避 (2) |
| `tests/` | 5 | 守卫回归断言引用 |
| `frontend/src/pages/Rollball/index.tsx` | 2 | 展示层消费者：`NO_EDGE → '弱优势'` 配色映射 |
| `sandbox/collab/mock_observer.py` | 1 | 黑板异常条目（叙述面） |

- **合计 42 处命中，11 个文件，全部带理由登记**（`SIGNAL_FILE_REGISTRY`），空理由不许登记（T57 纪律）。
- **调用方**：`bridge_service.py:3765/4393` 通过 `_fu.get('signal') not in ('ALREADY_BROKEN',)`
  消费信号词族（只拦兄弟词 `ALREADY_BROKEN`，`NO_EDGE` 照常流入 `_ou_hint`）；
  前端按 `signal` 取值着色。**`scripts/backtest_ou_signal.py` 引用数 = 0**（机械证明，T58 核心事实）。
- **生产代码零「同文件撞车」**：同时含信号词与三态词的文件只有 4 个，全在审计/SSoT/测试面
  （`audit_mh_train_div_bypass.py` / `audit_verification_report_freshness.py` /
  `verdict_guard_ssot.py` / `tests/test_audit_verification_report_freshness.py`），且均已登记。
  → 撞车风险今天在**扫描器/读者层面**，不在代码层面（诚实边界：不作为"已修 bug"表述）。

### Q2 是否值得改名——**值得，但不是因为撞车**

三条**互相独立**的证据说明这个词的语义本身是错的（不只是和判定词看起来像）：

1. `sandbox/collab/mock_observer.py:27` 登记过异常 **`ANOM_NO_EDGE_high_prob`**：
   “probe 输出 NO_EDGE 但 prob≥0.70（标注语义矛盾，展示层弱优势）”。
2. `scripts/audit_chain_consistency.py:145` 的 CHK6 用 `ou.get('signal') == 'NO_EDGE'`
   专查“高置信却标无优势”，并把该检查**降级为 WARN**——同一处矛盾被第二次独立记录。
3. 前端把 `NO_EDGE` 渲染成「**弱优势**」，而字段名读作「无 edge」。

三处都指向同一件事：`NO_EDGE` 实际含义是「**当前不 Worth 下注 / 信号未过阈值**」，
不是「不存在 edge」。这与 IR-30 的 `NO EDGE`（模型在验证台上没有 edge）在**结论强度**上完全不同，
却写成同一个词——正是 IR-30「不得让读者把弱结论读成强结论」要防的 shapes。

**建议新词**：`NO_BET`（强调可行动性，与 `OVER/UNDER/ALREADY_BROKEN` 同族且直白），
备选 `NO_SIGNAL`（强调阈值未过，但语义更弱，不推荐）。

### Q3 改名成本

| 成本面 | 实测 | 结论 |
|---|---|---|
| 代码字面量 | 42 处 / 11 文件，横跨 4 层 | `analysis/`(20) + `scripts/`(14) + `tests/`(5) + `frontend`(2) |
| **持久化携带** | **`data/watch_verdicts.json` 是业务快照**，内含 `"signal": "NO_EDGE"` | 🔴 ** RED：必须做数据迁移或读侧双词兼容 ** |
| 间接写入方 | `scripts/watch_live_verdicts.py`（不含字面量，只搬运上游值落盘） | 改生产者不会让**存量快照**复活 |
| 字符串比较站点 | 79 处（scripts 35 / analysis 22 / other 14 / bridge 8） | 改名后这些比较点**静默失效**且无测试会红 |
| 对 `analysis/` 的影响 | **勘误：不是历史脚本**——`analysis/live_goal_probe.py` 是 `bridge_service.py:83` 实时导入的生产层 | 改名 = 改生产面 |

**成本等级：高（需 WINDOW + 迁移）**，但存在一条**零成本先行动作**：
把 `scripts/backtest_ou_signal.py`（零调用方、唯一在 `scripts/` 根目录触发撞车的复刻脚本）
改成 `NO_BET`——改它 0 爆炸半径，且能直接**拆掉 T57 为它建的 `NOISE_FILE_REGISTRY` 豁免条目**，
让守卫基线从「靠豁免压噪声」回到「靠登记册治理」。

---

## 2 整改规格

### S1 分两步（强制顺序，不可合并）

| 步 | 动作 | 范围 | 风险 | 前置 |
|---|---|---|---|---|
| **N1（本窗口可做）** | `scripts/backtest_ou_signal.py` 内信号词改 `NO_BET`；同步把 T57 `NOISE_FILE_REGISTRY` 里的该条目**退役**（退役 = 从登记册删除而非标注释，见 T53「退役未登记会 AMBER」的口径） | 1 文件 | 零（零调用方，零消费方） | 无 |
| **N2（WINDOW）** | 活词表改名 `NO_EDGE → NO_BET`，跨 analysis→bridge→frontend 三层，含存量快照读侧兼容 | 42 处 / 11 文件 | 高（生产面 + 数据迁移） | WINDOW 停机窗口 + 迁移脚本（可写不跑）+ 回归 + 回滚表 |

### S2 N2 的必要条件（缺一不可，否则不许开工）

1. **迁移脚本先写不跑**：`scripts/migrate_signal_vocab.py`，双向映射（老词读兼容 + 新词写），
   支持回放存量 `data/watch_verdicts.json` 而不覆盖未冻结条目。
2. **读侧双词兼容**：所有 `.get('signal') == ...` 比较点必须同时接受新旧两词（窗口期内），
   全量 79 处比较点逐一登记，禁止只改一半。
3. **前端两词同渲染**：`Rollball/index.tsx` 的 map 必须同时含两键，否则窗口期内新词会落
   「未知」样式（与 T54 的 fail-open 渲染同形）。
4. **不产生任何 edge 结论变化**：改名只改字符串，不改变任何判定；三源仍 `NO EDGE` / `INCONCLUSIVE`，
   P0 `FAILED` 不变；报名时需按 IR-30 附胜率/隐含/edge_pp + 零信息机械对照（当前无一源达标）。

### S3 守卫（并入现有静态守卫，不新增第五套词表）

- G1 登记册完整性：`SIGNAL_FILE_REGISTRY` 每条必须有非空理由（已由
  `test_registry_reasons_are_all_non_empty` 守）。
- G2 未登记命中 = RED（`R1_SIGNAL_TOKEN_UNREGISTERED`），防豁免清单静默长大。
- G3 同文件撞车未登记 = RED（`R2_SAME_FILE_COLLISION`）；已登记 = INFO。
- G4 fail-closed：本审计任一检查未产出结果即 RED（沿用 T52“静默空结果”教训）。
- G5 三态行判定**必须丢弃信号拼写**（`verdict_lines_of()`）——本轮实测：
  直接复用 SSoT 词表会把 `live_goal_probe.py` 一次性算成 18 行假阳性。

### S4 验收 / 回滚

- A1 `pytest tests/ -q` 全绿；A2 活体审计 `verdict` 可判定且 `R1/R2/R3` 为 0 基线；
  A3 全仓信号词命中面数字与本报告一致（± 新增文件）；A4 N1 后 T57 登记册该条目已退役。
- 回滚 = 改回字面量 + 恢复登记册条目，零副作用（审计脚本不写生产）。

---

## 3 未决

- **Q-a** `scripts/watch_live_verdicts.py` 是否常驻调度？（T58 实测它零调用方/零计划任务引用，
  与 `backtest_ou_signal.py` 同型；若它才是真正把词写进磁盘的一方，N2 的迁移面还要再算一次。）
- **Q-b** 新词取 `NO_BET` 还是 `NO_SIGNAL`？（倾向 `NO_BET`，与“不下注”本义一致。）
- **Q-c** 79 处比较点是否全部纳入 N2 清单，还是先只盯 bridge 的 8 处？（倾向全量，否则静默失效面仍在。）

---

## 4 诚实边界

本轮**不产生样本、不推进 G1、不产生 edge**；三源仍全 `NO EDGE` / `INCONCLUSIVE`，P0 `FAILED` 不变。
报告指向的「语义矛盾」是**既有系统的标记问题**，不是新发现的亏损，也不构成任何可交易边缘。
