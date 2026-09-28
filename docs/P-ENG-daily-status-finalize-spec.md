# P-ENG — 修掉 `daily_predictions.status` 无人翻 finished 的整改规格（T46）

> 承接：T31（C2_STATUS_GATE 判定）/ T33（`UPDATE_WRITER=0` 机械证明）/ T35（walkforward 分段）/ T43（candles ingest 入口规格）
> 性质：**WINDOW 备料，纯规格**。本文不实现、不运行、不改 `verification/ingest.py`、不跑 ingest、不写 `verification.db`、零 `events.db` 写入、不碰生产调度。
> 基线（as-of 2026-09-27 20:0x，本地时区）：全量回归 **443 passed**。

---

## §1 问题陈述（两条断点，不是一个）

T33 已机械证明「全仓不存在把 `daily_predictions.status` 翻成 `finished` 的写方」（`UPDATE_WRITER = 0`，唯一写入是 `pipeline/predict_export.py:278` 的 `INSERT ... ON CONFLICT DO UPDATE SET status=excluded.status` 一次性快照）。

**本轮新增发现（B2，本条最重要）**：除写方断点外，链条上还有**调度断点**——

- `verification/__main__.py` 的 `cmd_ingest` 是 `python -m verification ingest` 入口；
- 全仓检索显示 **monitor / `run_daily_recheck.bat` / `ShaoxiangAI_DailyRecheck` / `ShaoxiangAI_ProdGuardian` / `ShaoXiangOddsAssetDaily` 全都不调用 ingest**（`scripts/autonomous_monitor.py` 内 0 处 `ingest`；`scripts/*.py` 中仅审计/诊断脚本提到 `verification`，且均显式声明不跑 ingest）；
- 实测账本 `verification.db`：`rows=9042`、`MAX(created_at)=2026-09-25T04:26:32Z`、`MAX(match_date)=2026-09-25`、source 分布 `{KNN 2779 / candles_ensemble 181 / market_baseline 6082}` —— **与 09-25 读数逐个相同**，即账本自 09-25 起未再有任何 ingest 运行。

**诚实结论**：即便明天把 §3 的门控改成 B 口径，若没有周期性 ingest 执行方，账本照样停在 09-25，**candles_ensemble 的 G1(2500) 缺口 2319 依然一分不减**。因此整改必须是**两件一起做（R-A 写方/门控 + R-B 调度接入）**，缺一不可；本条只做备料，两者都属 WINDOW。

---

## §2 现状证据（本轮只读复算，as-of 2026-09-27 20:0x）

复算方法完全复用 T33 脚本 `scripts/audit_ingest_status_gate.py` 的只读函数（`collect_candidates` / `last_odds_map` / `evaluate_guard`），未新写查询、未跑 ingest、未写账本。

| 指标 | 数值 | 说明 |
|---|---|---|
| 门控 B 候选全集（`m.status='finished' AND m.score_home IS NOT NULL AND model_source IN (candles_ensemble, market_baseline)`） | **10027** | 与 T33 的 9830 的差异来自 09-27 当日又有比赛完赛 + 预测行补入 |
| 过 loop 守卫后存活 | **7113** | 另 **2914 行被 `credible_1x2` 假0-0守卫剔除**（`NOT_CREDIBLE`） |
| 存活中已在账本内（去重命中） | **6263** | 去重键 `(match_id, model_source)`，见 `verification/ledger.py::append` |
| **净新增（打开闸门一次性可入账）** | **850** | candles 257 / market 593 |
| 净新增 match_date 跨度 | **2026-08-31 → 2026-09-27** | 逐日：`08-31:1 / 09-18:53 / 09-19:73 / 09-20:106 / 09-21:10 / 09-22:34 / 09-24:51 / 09-25:153 / 09-26:201 / 09-27:168` |
| `daily_predictions` status 现状 | candles `{finished 224 / live 60 / scheduled 291}`；market `{finished 9156 / live 115 / scheduled 1489}` | 与 T31 相比 market 侧 finished 由 9156… 持续累积 |
| `matches.status` 现状 | `finished 27521 / live 21 / scheduled 580 / filtered 232` | 真相源在 matches 侧，daily 侧是滞后快照 |

**唯一生产读方不受影响（关键安全事实）**：`bridge_service.py:6336` 的校准端点按 `dp.match_date = ?` 过滤，**不读 `dp.status`**；前端 `frontend/src` 对 `daily_predictions` 命中 0。即翻转 status 对 API/前端零行为改变（否则整改会连带改产品面）。

---

## §3 候选写方三选一（本文核心决策项）

### 选项 A —— 在 `predict_export` 内按 `matches.status` 回写 ✗ 不可自愈

- 做法：`build_for_date` 的 upsert 后追加 `UPDATE daily_predictions SET status='finished' WHERE ...`。
- **否决理由（机械）**：前向路径 `WHERE status NOT IN ('finished',...)`（predict_export.py:258）+ `if existing and ko_ts and now > ko_ts: continue`（:273）→ **已开赛/已完赛场在结构上永远不会再路过 predict_export**；历史回填 `include_finished=True` 又是「只补空行、绝不覆盖」（:269）。⇒ A 只能人工补跑一次，**永不自愈**，等于把慢性缺陷变成一次性人工动作。

### 选项 B —— 独立 finalize 步骤（新增写方）△ 可自愈但新增撞锁面

- 做法：新增 `pipeline/daily_status_finalize.py::finalize_finished(con)`，谓词
  `UPDATE daily_predictions SET status='finished' WHERE status<>'finished' AND match_key IN (SELECT match_key FROM matches WHERE status='finished' AND score_home IS NOT NULL)`；
  由 monitor 周期调用或独立计划任务。
- 优点：保留 `d.status` 语义；ingest 侧零改动（门控仍 `d.status='finished'`，语义重新变真）。
- **代价（必须计入）**：这是**第 N 个 `events.db` 写方**。T44 已实证 monitor 的 `refresh_predictions()`（`autonomous_monitor.py:93-116`，走 `gq.db.conn()` 写连接）与采集器撞锁，今日 4/17 周期失败。finalize 若与 refresh_predictions 同周期并行 → 新增一类 `database is locked` 面。

### 选项 C —— ingest 侧反向判定（门控改 `m.status`）✓ **推荐**

- 做法：`verification/ingest.py:97` 的 `WHERE d.status='finished'` → `WHERE m.status='finished'`（与同文件 `ingest_knn:168` 的 `m.status='finished'` 口径**统一**，T33 ① 已指出这是「值同表不同源」的口径不一致）。
- 优点：**不新增任何写方 → 零撞锁面**；**自愈**（每轮 ingest 自动纳入已完赛场）；对 KNN 侧口径先例对齐；`bridge`/前端零影响（§2 末）。
- 代价：`d.status` 退化为「陈旧派生字段」，未来读者若拿它当「已完赛」判据会再踩同一坑 → **必须同步改注释/文档并在 T46 验收项里加一条静态守卫**。
- 存量一次性补入的 850 行按 §5 分段处理，不可直接并账。

### 择优结论

**推荐 C 为主路径（零写方、零撞锁、自愈），B 作为备选**（若未来需要 `status` 表达「预测可用性」而非「已完赛」，独立的 finalize 更好）。**无论选哪个，R-B（调度接入）都必须同窗口落地**，否则 §1 的 B2 断点照旧。

---

## §4 幂等性与并发安全（三选一方案通用要求）

1. **幂等谓词**：任何 `UPDATE` 必须自带 `AND status<>'finished'`，重复执行行数为 0（验收 A3）。禁用 `UPDATE ... SET status='finished'` 无谓词写法。
2. **不碰已翻行**：已翻行再跑不产生变化；`predict_export` 的 upsert 若对同一 match_key 重跑会写入 `excluded.status`（= `matches.status`，已完赛场为 `'finished'`）→ 与 finalize 结果**同值**，不构成回退。但 **`include_finished` 历史回填路径必须保持只补空行（predict_export.py:269），否则会覆盖前向定格行**（与 T33「前向行结构上永不入账」同一处设计）。
3. **并发/撞锁**：
   - C 方案无新写方 → 撞锁面不增加；
   - B 方案（若采纳）必须：① 与 `refresh_predictions()` **同一连接、同一周期串行**（复用 monitor 的单进程写连接，不另开进程）；② 套用 T44 的 R1 抖动退避（仅 `OperationalError` 重试 3 拍：base 2s / factor 3 / jitter ±30% / cap 120s），非锁异常 fail-fast；③ 失败时写 `error_kind=LOCK_CONTENTION`，**不得静默吞掉**（现状是退化成「今日预测 ?」）。
4. **账本去重兜底**：即使 status 语义错乱导致重复入账，`ledger.append` 的 `(match_id, model_source)` 去重也会拦住（本轮实测去重命中 6263 行，即历史上已有 6263 行被正确拦下）——这是最后一道保险，但**它拦不住「应入而未入」**，故不能替代门控修复。

---

## §5 「不能顺手打开的闸门」——打开后净新增的 850 行如何使用

一旦门控放宽（C 或 B），一次性会灌入 **850 行**净新增（candles 257 / market 593）。**这些行不可照单计入 G1(2500)**，理由与边界：

1. **跨 walkforward 切代窗口**：净新增日期跨度 2026-08-31 → 09-27，其中 ≥09-18 的行共 849 行；按 T35 的切代锚点（09-19 转型 / 09-20 假0-0守卫 / 09-21 goal_scale 1.1 / **09-24 幂法** / 09-25 注册表），**≥09-24 之后的只有 573 行（candles 208 / market 365）**，其余属跨迭代窗口的历史行。
2. **09-24 幂法线是最高的不可并账边界**（T43）：`devig_method` 在 09-24 由比例法切幂法，**G1 分子只应取该线之后段**。按此口径，一次性补入对 candles 的**当期可执行增量仅 208 行**（`181 + 208 = 389`，距 2500 仍缺 2111）。
3. **`candles_ensemble` 的样本源仍受 T40 约束**：`prematch_candles_verdict` 是旁路入口（可净增 601 行），与 daily 侧是两条腿；补入 daily 侧不改变「K线判定当轮落不了日表」（T44 撞锁）的问题。
4. **诚实表述纪律（§1 诚实底线）**：任何关于「补入后 G1 何时达标」的说法**必须注明是按哪条分段口径**，且不得把一次性补入当作稳态速率（T34 已实证「一次性批量写入会被伪装成稳态速率」）。

⇒ 验收/使用建议：**先按段记账，不入全局 G1 分子**；`model_g1_reach_plan` 的 S1 情景需把补入行的分段归属写进前提（T38① 已指出该报告现有伪前提）。

---

## §6 落地步骤（WINDOW，需停机窗口 + 全量回归）

**顺序强制：R-B（调度接入）→ R-A（门控/写方）→ R-A2（补跑一次）→ 验收 → 观察。**
（先做 R-A 而 R-B 未上，等于只把门修好却不跑 ingest，账本依旧不动。）

- **R-B 调度接入**：在 `scripts/autonomous_monitor.py` 末尾增加 `ingest_step()`（复用现有写连接，串行，`OperationalError` 走 T44 R1 退避，成功/失败写 `monitor_status['ingest']={rows, ts, error_kind}`），或新增独立计划任务（后者需写新 `.bat` + `schtasks`，属生产调度变更，须老板确认）。
- **R-A 门控（C 方案）**：`verification/ingest.py:97` 单行 `d.status` → `m.status`；同步改该行注释 + `docs/prediction_refactor_checklist.md` 相关条目；新增静态守卫断言 ingest 入口不再出现 `d.status=`（防回退）。
- **R-A2 补跑**：`python -m verification ingest --dry-run` 先看预计新增，再实跑（属**写 verification.db 的显式动作**，须在窗口内由人工执行，AI 不自动跑）。
- **回溯补 status（仅 B 方案需要）**：一次性 `finalize` 全仓历史行。

---

## §7 验收门禁（fail-closed）

| 编号 | 断言 | 失败处置 |
|---|---|---|
| A1 | 全量 pytest 绿（当前基线 443） | 停 |
| A2 | 门控改后 `ingest_daily_predictions` 的 WHERE 不再含 `d.status=`（静态扫描 + 单元测试） | 停 |
| A3 | 幂等：连续执行 finalize 两次，第二次影响行数 = 0 | 停（禁止无谓词 UPDATE） |
| A4 | 净新增行数与本文 §2 复算一致（850 ± 数据自然增长），且**分段归属表**（≥09-24 为 573）与 §5 相符 | 停 + 人工复核 |
| A5 | `bridge` 校准端点（bridge_service.py:6336）返回不变（对同一 date 抽样 20 场比对 payload/LL） | 停 |
| A6 | 落库/补跑**不得触发重训**：`models/*.joblib` mtime 不变（沿用 T43 A6） | 停 |
| A7 | 监控可见：`monitor_status.json` 新增 `ingest` 字段且有 ts；连续 3 个周期 `rows>0`（稳态自愈证据） | 停（否则视为 B2 断点未修） |
| A8 | 账本 `MAX(created_at)` 前进、source 计数按分段变化（candles 181 → 389，且 181 之后新行 match_date ≥ 09-24） | 停 |

**回滚**：C 方案 = 改回 `d.status='finished'`（单行 + 注释）；B 方案 = `UPDATE daily_predictions SET status=? WHERE match_key=?` 逐行还原 / 或停止 finalize 任务即从周期性写方列表移除（账本为 append-only，回滚不影响既有行）。**绝不 `DELETE` 账本行、绝不 VACUUM、绝不动 events.db。**

---

## §8 红线自查

- §4 数据资产保全：本文零 `events.db` 写入；补跑属人工窗口动作，脚本可写但不自动跑。
- IR-30 诚实：§5 明确「补入不等于达标」，不把 850 行当 G1 分子，不宣称速率。
- IR-32 跨庄禁区：本条不涉及跨庄/共识/喊单，无需 T30 系列守卫。
- §6 进程安全：不杀进程；新增的 monitor 内串行步骤不得并发拉起。
- 售卖须人工+法务双签：不涉及。
- WINDOW 项：本条为备料规格，未执行。

## §9 未决（需老板/人工拍板）

- **Q-a**：C（零写方）还是 B（保留 status 语义）？—— 本文推荐 C，但 B 若采纳须先解决 T44 撞锁。
- **Q-b**：ingest 调度挂 monitor 周期（改代码，推荐）还是新增计划任务（改生产调度，须批准）？
- **Q-c**：850 行一次性补入是「按段记账」还是「直接进 G1 分子」？—— 本文强烈建议按段（§5），最终口径须与 T35/T43 切代表对齐后由人工确认。
- **Q-d**：是否同步给 `ingest_knn` 加一条「不得依赖 daily status」的注释级约定，避免后续有人改回？
