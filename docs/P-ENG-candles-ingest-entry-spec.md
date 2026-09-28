# P-ENG candles ingest 入口规格（`candles_ensemble` 判定表 → 验证台账本）

> 状态：**WINDOW 备料 · 纯规格**。本周期**不实现、不运行、不碰 verification.db、不碰 events.db 写入、不重训、不碰生产调度**。
> 承接：T40（`prematch_candles_verdict` 写入链路只读审计）/ T35（walkforward 分段可行性）/ T33（ingest 门控口径对账）/ T31（C2 状态门控）。
> 纪律锚：`docs/DISCIPLINE.md` §1 诚实 / §4 数据资产保全。去水唯一入口 `pipeline.odds_math.devig_power`；结算唯一入口 `pipeline.settle.{result_1x2,credible_1x2}`。

---

## 0. 为什么需要这个入口（一句话）

`prematch_candles_verdict` 表在**活写**（1038 行，09-19 后职责迁回 `gq/ws_collector.py:783`，T40 实证，最后写入 11:06），但**没有任何路径把它送进验证台账本**——账本 `candles_ensemble` 仍停 n=181 / `match_date` 停 2026-09-18。T40 复算漏斗：**候选 1038 → 假0-0 守卫剔除 212 → 存活 782（其中新 601，已入账 181）**。本规格定义那"缺的一条腿"：`ingest_candles_verdict()` 的落地形态。

---

## 1. 事实基线（本规格的事实来源，全部来自 T33/T35/T40 只读实跑）

| 项 | 实测值 | 来源 |
|---|---|---|
| `prematch_candles_verdict` 行数 | **1051**（T40 时 1038，1h 内 +13，仍在活写） | 本轮只读复核 |
| `prematch_candles_verdict.captured_at` 语义 | **判定生成时刻**（`time.time()`），非价格 tick 时刻 | 本轮只读复核 + `gq/db.py:1582` |
| 假0-0守卫剔除 | 212（0-0 全部拦下） | T40 Q3 |
| 守卫后存活 | 782（新 601 / 已入账 181，与账本 candles 181 键吻合） | T40 Q3 |
| 候选行中存在**开赛后赔率 tick** | **776 / 1038** | T40 Q3 |
| `date_mismatch`（判定 kickoff vs daily.match_date） | 0 | T40 Q2 |
| 账本 `candles_ensemble` | n=181，最后 `match_date` 2026-09-18，一次性批量写入 `2026-09-23T16:20Z` | T35 / T31 |
| G1 缺口 | 2500 − 181 = **2319** | T34 |
| 账本世代标记列 | **无**（`devig_method` 只记去水口径） | T35/T39 |
| 账本去重键 | `(match_id, model_source)` | `verification/ledger.py:44 append()` |

---

## 2. SQL 入口（候选集）与守卫序列

### 2.1 候选集 SELECT

**库归属已实测核实（2026-09-27，只读 `mode=ro`）**：`gq/db.py:78 DB_PATH = data/events.db`，故 `prematch_candles_verdict` / `matches` / `daily_predictions` / `odds_changes` **同库，可单查询 JOIN**。本次只读复核另得两条新事实：

- 表体量 **1051 行**（T40 时 1038，**1 小时内仍 +13 → 表在持续活写**，与本轮 T40 结论"最后写入 11:06"一致）。
- **`prematch_candles_verdict.captured_at` 是「判定生成时刻」（epoch REAL，`store_prematch_candles_verdict` 写入时 `time.time()`），不是赔率 tick 时刻**。它只反映"这行最后刷新于开赛前 ≤2h"，**不可当价格时间戳用于任何去水/前视判定**。本入口的赔率时间戳一律取 `odds_changes.captured_at`。

```sql
SELECT v.match_key, v.kickoff AS v_ko, v.direction, v.probs, v.confidence,
       v.margin, v.n_ticks,
       m.score_home, m.score_away, m.kickoff AS m_ko, m.status,
       COALESCE(d.match_date, m.kickoff) AS match_date
FROM prematch_candles_verdict v
JOIN matches              m  ON m.match_key  = v.match_key
LEFT JOIN daily_predictions d ON d.match_key = v.match_key
WHERE m.status = 'finished' AND m.score_home IS NOT NULL
ORDER BY v.match_key
```

- `match_date` 优先取 `daily_predictions.match_date`，缺失回落 `m.kickoff[:10]`（T40 实证 `date_mismatch=0`，两值一致，日期标签安全）。
- **连接姿态（§4 硬约束）**：本入口**只读**，须以 `mode=ro` 打开 events.db；**禁止用 `gq.db.conn()` 的写路径**触碰该表——虽然读同一文件，但写路径会走 `core.db_manager` 单写者与 WAL，与采集器抢锁，且违反"零写入"纪律。

### 2.2 守卫序列（**逐条对齐 `ingest_knn`**，顺序不可换）

| # | 守卫 | 实现 | 不过的处理 |
|---|---|---|---|
| G0 | 门槛行存在 | `m.status='finished' AND m.score_home IS NOT NULL` | `continue`（与 `ingest_knn:168` 同款） |
| G1 | 赛果可判 | `act = result_1x2(fsh, fsa)`；返回 `None` → `continue` | `continue` |
| G2 | **假0-0守卫** | `lodge = _last_odds_ts(con, mk)`；`ko_ts = parse_kickoff_ts(ko)`；`credible_1x2(fsh, fsa, lodge, ko_ts)` | `continue`（**212 行死在这道**，T40） |
| G3 | 判定可信 | `v.direction ∈ {home,draw,away}` 且 `probs` 三向齐全且和为 1±0.01 | `continue` |
| G4 | **判定列自洽**（本入口新增） | `max(probs, key=probs.get)` 必须 == `v.direction`；不等 → `continue`（**不信任 upsert 已覆盖的 direction，重算为准**） | `continue` |
| G5 | 赔率可得 | `odds = latest_prematch_1x2(con, mk, ko_ts)`（**必须传 `ko_ts`**） | `continue` |
| G6 | 去水 | `dv = _devig_dec_odds(odds[0], odds[1], odds[2])` → `devig_power` | `continue` |
| G7 | 去重 | `ledger.append(rec)` 内部按 `(match_id, model_source)` 判重 | 返回 0 → 不计新增 |

### 2.3 记录体（与 `ingest_daily_predictions` 同构）

```python
rec = {
    "run_id": run_id,
    "match_id": mk,
    "model_source": "candles_ensemble",   # 字符串必须逐字符一致(下游 G1/仪表盘按此分组)
    "kickoff_utc": _kickoff_utc(ko),
    "match_date": mdate or (ko[:10] if ko else ""),
    "chosen_outcome": chosen,             # = G4 重算的 max(probs)
    "predicted_prob": probs[chosen],
    "p_home": …, "p_draw": …, "p_away": …,
    "chosen_dec_odds": chosen_dec,        # 市场去水 decimal，非模型概率
    "devig_h"/"devig_d"/"devig_a": …,
    "paper_stake": PAPER_STAKE,
    "settled_home": fsh, "settled_away": fsa,
    "settled_outcome": act,
    "payoff": _compute_payoff(chosen, chosen_dec, act, PAPER_STAKE),
    "is_credible": 1,
    "created_at": _now_utc(),
    "devig_method": DEVIG_METHOD,         # "power"，常量取自 ingestion 模块
}
```
`chosen_dec` 与 G6 的四元数必须同一 `dv`：**模型出方向、市场出赔率**，两边不可互相替代（见 §3.1）。

---

## 3. 三条"禁止"（本规格的核心，反模式清单）

### 3.1 禁止把 `v.probs` 当概率绕过去水

`prematch_candles_verdict.probs` 是 **K 线集成模型的输出**（`pipeline/odds_candles_predict.predict_match` 的 `argmax` 分类分布，`gq/db.py:1553` DDL 注释即 `JSON {home,draw,away}`）。它是**模型分布，不是市场隐含概率**。若用它当天平：

- `chosen_dec_odds` 若改由 `1/probs` 反算 → 纸盘 payoff 变成"模型自己跟自己赌"，**G6 零信息机械对照立刻退化为恒 0**（T41 已实测 `market_baseline` 的 G6 结构性恒 0 就是"拿自己当自己对照"的同类病）；
- 更糟：账本与 `ingest_daily_predictions`（同一 `model_source` 的另一路）的赔率轴不一致 → 两路样本**不可并账**，G1 计数虚增。

**硬约束：`chosen_dec_odds` 只能来自 `_devig_dec_odds(latest_prematch_1x2(...))`，唯一入口 `devig_power`。** 代码层面把 `1.0 / probs[...]` 写成字面量应被 CI 拒绝（§5 G-A3）。

### 3.2 禁止用「最新赔率」取价 —— 必须 `captured_at <= kickoff`

T40 实证：**候选 776/1038 行存在开赛后的赔率 tick**。若取 `SELECT MAX(captured_at) FROM odds_changes WHERE match_key=?`（这正是 `_last_odds_ts()` 的语义，用于**结算可信度**的），会把**滚球价当赛前价** → 前视（lookahead）伪 edge：用开赛后才知道的赔率给赛前判定定价，ROI 必然虚高，且**回测里看不出来**（赔率本身是"未来才知道"的列，不是标签泄漏）。

**硬约束：赔率一律走 `latest_prematch_1x2(con, mk, ko_ts)`——其内部已 `if float(cap) <= ko_ts` 过滤（predict_export.py:64）。禁止自写 `MAX(captured_at)` 取价。** 判据：`_last_odds_ts()` 归守卫用（G2），`latest_prematch_1x2()` 归定价用，**两者语义不同、不可互换**。

### 3.3 禁止绕开守卫直接 `INSERT INTO verification_ledger`

T40 Q3 已证：**守卫一道也绕不过**（无法只换表名就入账）。任何"先写后洗"的写法（先写账本再补守卫字段）会使 G1 分母被污染。

---

## 4. walkforward 分段切法（不可并账）

补入区间 **2026-09-18 → 2026-09-27（9 天）** 落在**多个口径迭代窗口内**。切代锚点（T35 实测提交）：

| 段 | 起点提交 | 日期 | 口径变更 | 处理 |
|---|---|---|---|---|
| S-pre | — | ≤ 09-18 | 账本 n=181 全部（含 `2026-09-23T16:20Z` 一次性回填） | 独立计数，不与后段并账 |
| S1 | `0997ced0` | 2026-09-18 | 结算守卫类 | 边界段 |
| S2 | `f98c5e45` | 2026-09-19 | 架构转型 + **假0-0守卫**（`credible_1x2`） | **不可与 S1/S3 并账** |
| S3 | `87207afc` | 2026-09-20 | 假0-0结算守卫 + BTTS 对照 | 与 S2 同类，可合段 |
| S4 | `f270c84f` | 2026-09-21 | `goal_scale 1.0 → 1.1` 守卫后翻转（AGENTS.md 记载 A/B 4/4 切分过线） | **不可与前后并账** |
| S5 | `642b8b40` | 2026-09-24 | **去水口径事故根治**（比例法 → `devig_power`），KNN 伪 EDGE 翻转 NO EDGE | **最高优先级边界**：09-24 前/后绝对不可并账 |
| S6 | `c8758ac9` | 2026-09-25 | 模型注册表补全（模型版本锁定） | 模型代边界（T35 判定：补入区间内由同代模型产出，**不破坏时序**） |

**执行规则**：

1. 一次性补跑 **必须按段分别计数**：`n_g1_segment = 197`（= 782 存活 − 181 已入账，粗口径，实际按段切分后各段净增量须逐段跑出）。G1 判定以 **S5 之后段（S5 起）** 的累计为主口径，S-pre 与 09-24 前的行数**只作对照不进 G1 分子**。
2. **模型世代锚**：训练固化标记 `2026-09-18 finalize days=70`（`verification/models` marker）。补入区间内的判定由同代模型产出 → **本入口的补跑不破坏 walkforward 时序**（T35 结论）。触发重训后，**S6 起重新起算**（T34 ST 情景）。
3. **账本无世代列**（T39 实证）：本入口**不加列**（属 WINDOW）。分段靠 `created_at` + git 提交对齐（T39 已证 `created_at` 与 git 同一时钟域，可作近似切代）。**代价（诚实标注）：同一次入账内的多次重训行永久不可分。**
4. **禁止"补跑并一次性并账"**：这会凭空把 G1 缺口 2319 填掉一部分而口径混杂，属**以记账替代验证**。

---

## 5. 验收门禁（fail-closed，任一不过则**不落库**）

| 门 | 断言 | 失败动作 |
|---|---|---|
| G-A1 | 全量 `pytest` 绿（基线 **422 passed** @ 2026-09-27，T42 实测） | 不合并 |
| G-A2 | 默认 `--dry-run`：只算不写；`--apply` 才写账本；**未显式 `--apply` 时账本行数不变** | 不落库 |
| G-A3 | 静态扫描：出现 `1.0 / probs`（反算 odds）或自写 `MAX(captured_at)` 取价 → FAIL | 不落库 |
| G-A4 | 账本去重键不冲突：dry-run 报"新增 N / 去重 M"，断言 `N+M == 782 − 已入账`，且 `(match_id,model_source)` 重名 0 | 不落库 |
| G-A5 | 落库前后 `verification.db` 行数差 == N（`append()` 幂等，重跑 N=0） | 回滚 |
| G-A6 | **本入口不得触发重训**：跑完 `retrain_gate` 的 `suggest` 与跑前一致；`models/*.joblib` mtime 不变 | 立即中止+回滚 |
| G-A7 | 前视自检：随机抽 20 条落库记录，断言 `devig_*` 对应的 `captured_at <= kickoff_utc + 60s` | 回滚 |

**IR-30 诚实附加**：本入口上线后**不得**宣称任何盈利。账本新增行的 G6 判定（零信息机械对照）须与 T41 同口径复核；若 `ci_low <= 0` 一律判 NO EDGE，报告措辞照抄 T41 的 EXEMPT 例外（对结构性恒 0 的机械基准单列，不判负）。

---

## 6. 回滚表

| 步骤 | 动作 | 前置检查 | 回滚方式 | 影响面 |
|---|---|---|---|---|
| R0 | dry-run 只算 | G-A2 | 无（零写） | 无 |
| R1 | 落库 N 行 | G-A4/A5 | `DELETE FROM verification_ledger WHERE run_id=?`（run_id 入账时写入） | 仅验证台账本，`verification.db` 非生产资产；**不回滚** events.db |
| R2 | 若误触重训 | G-A6 | 立即中止；marker 由 R1 路径回写（T37 R1） | marker 文件 `last_candles_train_marker.json` |
| R3 | 报告发布 | — | 删/标注 `reports/candles_verdict_ingest.{json,md}` | 仅 reports/ |

**回滚负向清单（绝不做）**：`DELETE FROM verification_ledger` 之外**绝不动 events.db**；绝不用 `VACUUM`；绝不降权限重跑旧口径。

---

## 7. 本规格明确**不做**的事（本周期边界）

- 不改 `verification/ingest.py` / `ledger.py` / `schema.py`（schema 增列属 WINDOW，T39 建议四列）。
- 不跑 ingest、不写 `verification.db`、不重训、不碰生产进程、不改 `gq/` 任何文件。
- 不解决上游缺陷本身：**`daily_predictions.status` 无人翻 finished（T33 机械证明 UPDATE_WRITER=0）仍在** → 即便本入口上线，`ingest_daily_predictions` 一路的 `candles_ensemble`/`market_baseline` 仍停 09-18。**本入口是旁路，不是根治**。
- 不宣称修完就GE1。补入样本是否推进 G1 取决于 G1 按段计数口径采纳与否（T34 S1 情景 37.0/天 → 2026-11-29，为**估算非承诺**）。

---

## 8. 落地前置（执行本规格前须先满足）

1. **P1** G-A3 静态扫描进 CI（可与既有测试同批，fail-closed）。
2. **P2** G1 分段计数口径由 owner 拍板：S5（09-24 幂法）是否设为 G1 起点。
3. **P3** 停机窗口（schema/迁移类**不适用本入口**；但 `verification.db` 落库与 `retrain_gate` 读写并存，建议排入 WINDOW 并行窗口）。
4. **P4** 全量 pytest 基线锁 422 passed。
