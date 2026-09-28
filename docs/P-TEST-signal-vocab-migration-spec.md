# P-TEST-T62 信号词存量快照读侧兼容 — 迁移规格（WINDOW 备料，纯规格）

> 承接 T58 §S2 / backlog T62。**本轮只做只读盘点 + 规格，不跑迁移、不写快照、不改 events.db、
> 不改生产文件、零进程操作。**
> 唯一被本轮**修改**的文件是两处**登记册**（把新审计脚本登记为「非生产者」以免活体守卫误红），
> 属守卫生效所需的台账动作，不改任何判定结论。

---

## 0. 一句话结论

改名 `NO_EDGE → NO_BET` 的成本**不在字面量也不在 JSON 快照（509 条）**，
而在 **`events.db` 的 `prediction_ledger.signal` 列（41,755 行）** —— 这是 T58 **漏掉的持久化面**；
且该列属 events.db 写入，改写须走 §4 数据资产保全 + WINDOW 停机窗口，**不属自主循环范围**。
可用的最小动作是「只改生产者 + 读侧归一化 + 存量保持原词不动」，禁止双向映射（会制造假等价）。

---

## 1. 实证摘要（2026-09-28 只读读数）

| 面 | 位置 | 规模 | 词表 |
|---|---|---|---|
| 生产者 | `analysis/live_goal_probe.py` `*_signal` | 7 值 | NO_EDGE / STRONG_BREAK / STRONG_HOLD / WEAK_TREND / ALREADY_BROKEN / SCORE_LAGGING / SETTLED_UNDER |
| 桥接 | `bridge_service.py:4790` 原样透传 `pr['full']['signal']` | — | 与生产者同 |
| 业务快照 | `data/watch_verdicts.json` | **509** 条，`ou.signal` 197/244/32/36 | 4 值（子集） |
| **events.db 列** | `prediction_ledger.signal` | **41,755 行 / 8,720 场** | NO_EDGE 19,800 · STRONG_HOLD 11,157 · STRONG_BREAK 8,190 · WEAK_TREND 2,605 · SETTLED_UNDER 3 |
| 前端取键 | `frontend/.../Rollball/index.tsx` `OU_SIG` | 5 键 | 缺 SCORE_LAGGING / SETTLED_UNDER |

- **词表三处互不一致**：生产者 ∩ 前端 = 5，`in_producer_not_frontend = {SCORE_LAGGING, SETTLED_UNDER}`，
  `in_data_not_frontend = {SETTLED_UNDER}`。**ALREADY_BROKEN 今天就已经渲染不出来**（在生产者词表、
  不在取键表）→ 与 T54「新增第四态被静默渲染」同族缺陷，已在线。
- **盯盘器不常驻（Q-a 答案）**：`data/watch_verdicts.json` mtime = **2026-09-14 00:10**（14 天未更新），
  509 条记录，`settle()` 只结算出 401 条 `final`；T58 已机械证明 `watch_live_verdicts.py` 零调用方、
  零计划任务引用。**结论：快照是人工产物，不是在线资产。**

---

## 2. 三问答案

### Q-a 盯盘器是否应常驻？若不常驻，迁移面能否缩到纯读侧？

**答案：不常驻，但「缩到纯读侧」不成立。**

- 不常驻已机械证明（零调用方 / 零计划任务 / 快照 14 天未动）。
- 但**不常驻不等于只读**——任何人手工跑一次 `snapshot()`，就会用 `v.update(now_v)`
  把存量 509 条的 `ou.signal` **整体刷成新词**。这是「迁移后第一次人工运行」的必然后果，
  规格必须预置而不是临时补救。

**落地口径**：
1. 迁移脚本只做**离线一次性改写**并显式备份（落 `data/watch_verdicts.<ts>.bak.json`），
   副产物进带理由登记册；
2. 改写范围**只限顶层 `ou.signal`**，禁止触碰 `first_*`（见 Q-b）；
3. 迁移后首次 `snapshot()` 的刷新语义须在文档中标注（否则「新旧词混杂」会被误读成数据损坏）。

### Q-b 双向映射如何与「判定首见即冻结」共存？

**① 双向映射本身应被否决。** `NO_EDGE → NO_BET` 是**重命名（同一实体的新名字）**，不是
**翻译（两种实体互译）**；反向映射 `NO_BET → NO_EDGE` 会把「此刻不下注」悄悄还原成
「不存在 edge」，与 IR-30 的结论强度直接冲突。**只允许单向 canonical + 只读旧值。**

**② 冻结语义的真实边界（实测，与文档声明不同）**：
`scripts/watch_live_verdicts.py:12` 写「判定首见即冻结，不随后续刷新改写」，但
`snapshot()` 里只有 `first_score/first_minute/first_seen/first_verdict` 是首见写入；
`ou.signal` 位于 `ou` 子字典，**每轮被 `v.update(now_v)` 覆盖**。
实测 509 条里有 **156 条 `first_score != score`**（即冻结后又刷新过）→ 该文件里确实存在
「冻结字段 + 活字段」并存的结构。

**迁移规则（必须写进迁移脚本）**：

| 字段 | 是否冻结 | 迁移可否改写 | 理由 |
|---|---|---|---|
| `first_verdict.ou_direction` 等 `first_*` | 是 | **禁止** | 历史判定的不可变快照（2026-08-23 用户铁律） |
| `ou.signal`（顶层） | 否（每轮刷新） | 可，但须离线备份 | 非冻结，改动不影响历史判定 |
| `ht_freeze` | 半冻结（只捕一次） | 禁止 | 中场冻结语义 |

守卫已落地：`frozen_fields_are_first_sight_only()`（AST 级：首见分支外出现 `first_*` 赋值即 FAIL），
fail-closed 反向用例见 `tests/test_audit_signal_vocab_migration.py`。

**③ 方案三选一（推荐 M3）**：

| 方案 | 动作 | 风险 | 评价 |
|---|---|---|---|
| M1 只改生产者 | 生产者出新词，读侧归一化 | 存量 JSON 长期半新半旧 | 最小，但读侧归一化函数会成为常驻技术债 |
| M2 就地改写存量 | 一次性 rewrite 509 条 | 与下次 `snapshot()` 冲突 | 不推荐（无回滚价值，值可被再次覆盖） |
| **M3 canonical + legacy 双字段** | 新增 `ou.signal_v2`，`signal` 保留原值 | 字段冗余 | **推荐**：读侧只认 `signal_v2`，watch 前端双键同渲染，窗口期结束删 `signal` |

### Q-c 窗口期内前端是否双键同渲染？

**现状是「未知键整段消失」，不是变灰。** `Rollball/index.tsx:1183` 用
`{ou.signal && OU_SIG[ou.signal] && (...)}` 裸取键 + `&&` 短路：键不存在 → 整段徽章不渲染，
**连中性灰底都没有**（T54 描述的是「渲染成灰色未知」，本处更差一档）。

**落地口径**：
1. 迁移与前端必须**同一次提交**（前端先加 fallback 再切生产者，顺序不可反）；
2. fallback 分支 = 灰底 + **原词文本**（`NO_EDGE` 显示而不是隐藏），满足 T54 的 fail-closed 渲染要求；
3. 生产者 `probe_match()` 有 **LRU + TTL 20s** 缓存（`_PROBE_CACHE_TTL`），
   验收须等 TTL 过期或清缓存，否则会在「看起来没生效」上误判；
4. 与 T54（看板 fail-open 渲染）、T50 O1-O4 同窗口落地（共用唯一消费点 Rollball 页）。

---

## 3. 本轮顺带发现的两条真缺陷（本条不修，仅登记）

1. **首见 OU 结算结构性空转**：`first_verdict` 存的是扁平 `ou_direction`（字符串），
   而 `settle()` 的 `ou_win(v)` 读的是 `v['ou']['direction']` 子字典 → `first_OU` 分支恒为 `None`，
   首见冻结的 OU 判定**从未被结算过**（`first_verdict_keys = [x2, cs_top1, cs_top3, ou_direction]`）。
   属「冻结语义做了但没人用」的空转，与 T33/T36 的「数表而 G1 数账本」同形。
2. **§4 卫生**：`scripts/watch_live_verdicts.py:129` 以**默认（可写）连接**打开 events.db，
   而它只读 `score_home/score_away/status`。只读纪律要求显式 `mode=ro`。

---

## 4. 验收与回滚

**守卫 G1–G5（已落 `tests/test_audit_signal_vocab_migration.py` 共 29 用例，全绿）**

- G1 生产者词表必须覆盖数据面（快照/账本 ⊆ 生产者），孤儿词即 FAIL
- G2 前端取键必须覆盖生产者词表，缺值必须显式登记（今天缺 2 值 → 报告 FAIL）
- G3 `first_*` 只能在首见分支写入（AST 级，FAIL 带行号）
- G4 审计脚本自身必须自避（词表即被盘点对象，不自避会假红）
- G5 模块不得被 `.gitignore` 吃掉（`git check-ignore -q`，与 T61 同族）

**迁移验收 V1–V8（执行时才验，本轮不跑）**
V1 迁移为 dry-run 默认 · V2 备份文件存在且 JSON 可解析 · V3 `first_*` 逐字节不变 ·
V4 `ou.signal` 改写行数 = dry-run 声明 · V5 events.db 行数为 0 变化（证明未写库） ·
V6 前端 fallback 分支存在 · V7 生产者缓存 TTL 过期后无旧词 · V8 守卫全绿。

**回滚 R0–R2**：R0 恢复备份 JSON · R1 回退生产者（新词旧词都无消费者） · R2 删除 `signal_v2`。

---

## 5. 未决（董事长/老板定）

- **Q-1** `prediction_ledger.signal` 的 **41,755 行存量改写**是 WINDOW 项，须停机窗口批准；
  是否接受「存量不动、只改生产者 + 读侧」的零停机路径？
- **Q-2** 前端 fallback 是「灰底 + 原词」还是「双键同渲染」（M3 的 `signal_v2`）？
- **Q-3** 首见 OU 结算空转（§3.1）是否单开修复条？修它等于改 `ou_win()` 的入参契约，属真代码改动。
- **Q-4** `watch_live_verdicts.py` 的 events.db 可写连接（§3.2）是否并入 Q-3 同批修。

---

## 6. 诚实边界

本条**不产生样本、不推进 G1、不产生 edge**。三源仍全 `NO EDGE` / `INCONCLUSIVE`，
`P0 FAILED` 不变。改名与否尚未决定，本规格只是把「改名要付什么代价」算清楚。
