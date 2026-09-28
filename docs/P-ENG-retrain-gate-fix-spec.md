# P-ENG — retrain_gate 三项修复落地规格（WINDOW 备料 · 纯规格）

> 承接：T36 `scripts/audit_retrain_trigger.py`（`reports/retrain_trigger_audit.{json,md}`）实证 F1/F2/F3/F4/F5。
> 性质：**纯规格**。本轮不落地、不碰生产代码、不碰模型文件、不写任何库、不触发重训。
> 落地须走 `docs/model_promotion_runbook.md` §3 晋升流程（fail-closed）+ 全量 pytest。
> 作者：赵统筹（自主 owner 巡航）｜日期：2026-09-27

---

## 0. 一句话结论

**现在的 `retrain_gate.suggest=True` 是一个自我循环的空转信号**：它拿「已经重新定格的判定行数」（`prematch_candles_verdict`，marker 之后 599 条、表仍在活写）当重训理由，而真正决定重训有没有意义的「账本新增样本」（`candles_ensemble` 最后一条停在 2026-09-18、之后 0 行）根本不在它的算式里。三项修复的正解不是"把阈值调大"，而是**给门控接上一个独立的水位时钟**：`ts_train`（模型世代，训练结束写）与 `ts_ledger`（账本水位，ingest 结束写）分开。混用时钟或只留一个时钟，都会让 suggest 固定为 True。

---

## 1. 现状事实基线（T36 实证 + 本轮新增实证）

### 1.1 T36 已证实（不重复论证）

| 事实 | 值 | 出处 |
|---|---|---|
| 门控函数 | `scripts/autonomous_monitor.py:158 retrain_gate()` | T36 §1 |
| 计数口径 | `SELECT COUNT(*) FROM prematch_candles_verdict WHERE captured_at > COALESCE(?,0)`，`_last_train_ts()` 取 marker | T36 §1 |
| 阈值 | `n_new >= 100` → `suggest` | T36 §1 |
| drift 阈值 | `ll_delta > 0.03` 只产 `drift_alert`，**不进 suggest** | T36 §1/F1 |
| marker 写方 | **0 处写**（全仓仅 2 处定义/读取）→ 重训后门控不自愈 | T36 §3/F2 |
| 计数 vs 账本 | verdict 597（T36 时）vs 账本 candles 最后 match_date 2026-09-18、之后新入账 **0 行**，G1(2500) 缺口 2319 | T36 §5/F3 |
| 运行时消费者 | backend / frontend 命中 **0** | T36 §2/F4 |
| suggest 连续性 | 自 2026-09-24 09:54:35 起连续 True，85 周期仅翻转 2 次 | T36 §4/F5 |

### 1.2 本轮新增实证（T37 现场复核，均为只读）

**N1 — 账本 SSoT 三库并存，其中一个 0 表空壳。** 实测：

| 路径 | 行数 | mtime | 判定 |
|---|---|---|---|
| `D:\Architecture\verification.db` | 9042 | 2026-09-25 12:26 | **SSoT**（`verification/schema.py:14 VERIFICATION_DB`；`__main__.py:32` 默认 `--db`） |
| `D:\Architecture\verification_v2.db` | 8930 | 2026-09-24 00:21 | 口径重建期冻结副本，**0 处代码引用、不再写入** |
| `D:\Architecture\verification\verification.db` | **0 张表** | 2026-09-24 14:50 | **空壳**：文件存在、无表，`verification/` 包实际不指向它 |

风险：任何 `**/verification*.db` 的 glob 式盘点（含自动化）都会把空壳库当成"账本为 0 行"，与 SSoT 得出相反结论。落地前必须先定路径白名单。
账本当前按 source 分布：`KNN 2779 / candles_ensemble 181 / market_baseline 6082`；`run_id` 共 5 个，最大单批 `741b8eb4…` 8930 行（`created_at` 2026-09-23T16:19:46Z）。

**N2 — marker `ts` 与账本 `created_at` 是两个时钟域，直接比较必然永久 True。**
marker 文件里 `ts = 1789756639.121332`，是 `time.time()` 的**本地 epoch**（换算本地 2026-09-19 02:37:19，而文件 mtime 为 04:34:56 —— 两者不同源，T36 表格把它们并列易被误读）。账本 `created_at` 是 UTC ISO `%Y-%m-%dT%H:%M:%SZ`。实测「`created_at > marker_ts`」= **9042 = 全表**，因为整本账本是本轮批量回填的产物，天然晚于 09-19 的训练时刻。
→ 若按 T36 §7 的字面建议（"门控计数改为按 ledger 新增行"）直接实现，会把 `suggest` 从"当前 True"变成"**永远 True**"，比现状更坏。

**N3 — verdict 表仍在活写，而账本仍在停写。**
`prematch_candles_verdict` 现有 1029 行、marker 之后 599（T36 为 597，两日内 +2），`max(captured_at)=1790463910.3`（2026-09-26 附近）。即：采集侧定格判定持续产出，但走 `daily_predictions.status` 门控的入账链路（T31 C2 / T33 已机械证明 **UPDATE_WRITER=0**）仍把其中绝大多数挡在验证台之外。**重训的原料没有增加，只有判定噪音在增加。**

---

## 2. 三项修复规格

> 命名：`R1` marker 回写 / `R2` 计数改按账本 / `R3` drift 与 suggest 的关系。
> **执行顺序强制：`R2` 必须最先落地**，其次是 `R1`，最后是 `R3`。理由见 §2.4。

### 2.1 R1 — 训练结束时自动回写 marker（修 F2）

**问题**：`finalize_candles_ensemble.py` 训练产出 `data/models/candles_ensemble/{lgb_candles_static.joblib, transformer.pt, meta.json}`，但从不回写 `reports/last_candles_train_marker.json`；因此重训后 `new_verdicts_since_train` 不归零，suggest 永久 True（唯一一次 True→False 发生在 marker 落盘那 39 秒内）。

**改动点**：`scripts/finalize_candles_ensemble.py`，在第 3 步写 `meta.json` 之后新增第 4 步 `write_train_marker()`。

```python
def write_train_marker(ledger_watermark: float | None = None) -> dict:
    """训练完成后回写门控水位. ledger_watermark: 传入当前账本 max(created_at) 的 epoch,
    使门控计数从 '训练后的新样本' 起算, 而非从 '训练时刻' 起算."""
```

marker JSON 结构（**字段扩展，旧字段兼容**）：

| 键 | 类型 | 语义 | 写入方 |
|---|---|---|---|
| `ts` | float | `time.time()` 本地 epoch，**保留**（向后兼容 `_last_train_ts()`） | R1 训练结束 |
| `ts_train` | float（epoch） | 模型**世代**时刻，用于 walkforward 分段（承接 T35③） | R1 |
| `ts_ledger` | float（UTC epoch） | 账本**水位**（最后一次 ingest 的账本 max `created_at` 的 UTC epoch） | R2 的 ingest 钩子 |
| `run_id` | str | 本次训练/入账批次号 | R1/R2 |
| `n_matches` / `window_days` | int | 与 `meta.json` 同源，供门控判断是否样本不足 | R1 |

**前置门禁（落地前必须全绿）**
1. `scripts/finalize_candles_ensemble.py` 单独跑不写生产库 —— 训练本身属重操作，**不在本规格范围内**，仅要求其新增写 marker 的分支可被 `--dry-run` 跳过并在日志留痕。
2. `.venv/Scripts/python.exe -m pytest tests/ -q --timeout=120` 全绿（基线见 §4）。
3. 新增 `tests/test_train_marker_writeback.py`：临时 tmp 目录 + monkeypatch 路径，**零生产 I/O**。

**验收断言（fail-closed，缺一不通过）**
- A1 训练结束后 marker 中 `ts_train` **严格大于** 本次训练前的 `ts`；
- A2 marker 可被 `_last_train_ts()` 正常反序列化（结构兼容，不抛 `KeyError`）；
- A3 标记写入后再次调用门控，`new_verdicts_since_train` 相对写入前**下降**（即水位确实前进）；
- A4 marker 文件写入失败时**不静默吞没**：必须 `log(...)` 且以非 0 退出码结束训练（禁止 `try/except: pass`，与 T36 observed 的 09-19 静默失败同型）；
- A5 写入 marker 前**不得**删除旧文件（回滚需要），采用 `os.replace` 原子写。

**回滚**：`git checkout -- scripts/finalize_candles_ensemble.py` 即可；marker 是纯派生产物，回滚后只影响门控水位精度，无副作用。

### 2.2 R2 — 门控计数改按账本新增行（修 F3，critical）

**问题**：门控用 verdict 行数怂恿重训，但 verdict 只代表"又定格了 N 场判定"，不代表"账本多了 N 个可信样本"。当前两者已解耦到极端：599 条新判定 vs 账本新增 **0** 行。此时重训只是把同批 in-sample 重新拟合（T36 F3）。

**改动点**：`scripts/autonomous_monitor.py` 的 `retrain_gate()` 与 `_last_train_ts()`。

```python
def retrain_gate() -> dict:
    n_new_verdict = ...        # 保留原计数, 仅作为诊断字段输出, 不再决定 suggest
    n_new_ledger  = ledger_rows_since(_ledger_watermark_ts())
    return {
        'new_verdicts_since_train': n_new_verdict,   # 保留, 降级为参考
        'new_ledger_rows_since_watermark': n_new_ledger,
        'ledger_watermark_ts': ...,
        'suggest': n_new_ledger >= LEDGER_TRIGGER_N,   # 阈值另定, 见下
    }
```

**阈值 `LEDGER_TRIGGER_N` 取值建议与理由**：现阈值 100 是按 verdict 流的节奏定的；账本流受 ingest 门控（T31 C2）卡死，稳态速率不足 1 行/天（T34：现状外推 candles 速率 0）。若沿用 100，门控在门控修复前**永远 False**（比现在更沉默，反而掩盖问题）；若沿用 1，门控一旦修复就会因 T33 的一次性补账（净新增仅 461 行）立刻爆 True。
→ **建议 `LEDGER_TRIGGER_N = 500`（约 G1 2500 的 1/5）且只统计 `candles_ensemble`**（`market_baseline`/`KNN` 是参照系不是重训对象）。并按 T35 结论：跨 walkforward 分段的一次性补账**不并入阈值**（补跑须分段，见 T33）。

**前置门禁**
1. 账本水位必须用 UTC epoch 而不是 marker 的本地 `ts`（N2）——仓库内统一一个 `to_epoch_utc(created_at)` 转换函数，禁止在门控里做字符串比较。
2. `verification/ingest.py` 的入口增加水位推进钩子（写 `ts_ledger`）。**注意**：这是写 `verification.db` 的路径，属正常写入面（非 events.db），但仍必须**只追加、不改历史行**。
3. 全量 pytest 绿；新增 `tests/test_retrain_gate_ledger.py`（FakeCursor / 临时库，不连生产 `verification.db`）。

**验收断言**
- B1 门控读取的账本库路径必须来自 `verification/schema.py::VERIFICATION_DB` 单一常量（**禁止** write 时 `verification.db`、盘点 glob 时 `verification_v2.db` 或空壳库，见 N1）；
- B2 水位为 0 时（无 marker）门控返回 `None`-态而非 `False`，避免"从未训练过"被误读为"不需要重训"；
- B3 用同一份临时账本，水位设在前半 / 后半两处，两次 `suggest` 必须发生翻转（证明计数真的在动，而不是恒 True/恒 False）；
- B4 门控返回的 `new_verdicts_since_train` 仍计算但**不参与 suggest**（防止回归时被改回主判据）；
- B5 门控对 `verification.db` 以 `mode=ro` + `PRAGMA query_only` 打开（与既有审计脚本一致）。

**回滚**：`retrain_gate()` 恢复 `suggest = n_new_verdicts >= 100` 一行即可，水位文件可留作诊断。

### 2.3 R3 — drift 与 suggest 的关系（修 F1）

**问题**：T17 的 `INVALIDATED_BY_RETRAIN` 分类把「`retrain_gate.suggest=True`」和「`drift_alert=True`（`ll_delta=0.03231 > 0.03`）」当成同一件事（89 份报告因此被判失效）。但代码里两者互不喂养：drift 只在 `calibration_check()` 产 `drift_alert`，suggest 只看行数。

**处置：二选一，不允许维持现状（现状是"文档口径混用"的温床）。**

- **方案 A（推荐，小步）**：显式声明二者无关。在 `docs/DISCIPLINE.md` 与 `AGENTS.md` 各加一句统一措辞——
  > `retrain_gate.suggest` 是**样本充足性**信号（账本新增行数 ≥ 阈值）；`calibration.drift_alert` 是**校准劣化**信号（LL delta > 0.03）。二者**不互相触发**，报告失效判据只引用 `suggest`（样本面），不引用 `drift_alert`；T17 的 `INVALIDATED_BY_RETRAIN` 分类须同步只以 `suggest` 为准。
  同时把 `INVALIDATED_BY_RETRAIN` 的判定式从「suggest or drift_alert」改为「suggest」。
- **方案 B（大步）**：把 drift 真正接进 suggest（`suggest = ledger_new >= N or drift_alert`）。**须先回答"drift 严重到什么程度值得停服/重训"**——当前 `log_loss_delta=0.03231` 相对 2026-09-19 守卫后的真值基线 `+0.013` 只有 2.5 倍，样本 n 又只有 181，属噪声量级；B 方案必须先有 G1 达标样本才有意义（否则就是 T36 F3 的同批 in-sample 重拟合）。

**前置门禁**：A 方案只需文档 + T17 判定式改动 + 一条守卫测试；B 方案须 §4 全量门禁 + walkforward 过线（对齐 runbook §3）。

**验收断言**
- C1 `INVALIDATED_BY_RETRAIN` 分类结果相对改动前后若变化，必须在报告里显式列出差异行数（不允许静默重分类）；
- C2 新增守卫测试断言 `drift_alert=True 且 suggest=False` 的组合**不是**"报告失效"（修复 T17 的过度判失效）；
- C3 若选 B，`suggest` 变为三态：`ready_retrain` / `insufficient_sample`（账本不足且 drift 高，明确**不重训**）/ `ok`，三态在 `monitor_status.json` 可见。

### 2.4 执行顺序为什么是 R2 → R1 → R3

- 若先做 R1 而不做 R2：水位只由训练推进，训练不发生 → 水位不动 → suggest 永久 True（N2 已证明 `created_at > marker_ts` 恒真）。
- 若先做 R2 并直接复用 marker 的 `ts`：全表 9042 行晚于 marker → suggest **永远 True**（比现状更坏）。R2 必须自带 `ts_ledger` 水位。
- 因此：`R2`（含 `ts_ledger` 水位与路径白名单）→ `R1`（训练回写世代）→ `R3`（口径统一）。R3 独立可最后做。

---

## 3. 落地前置清单（WINDOW，本轮不执行）

| # | 前置 | 说明 | 阻塞对象 |
|---|---|---|---|
| P1 | 账本路径白名单 | 定死 `verification.db` 为唯一 SSoT；`verification_v2.db` 冻结、空壳库移入 `archive/`（**不得删**，属数据资产保全 §4） | R2 B1 |
| P2 | walkforward 分段可判 | 账本无世代列（T35③），重训后无法切代 → 增列属 schema 变更，**必须停机窗口** | R1/R2 语义正确性 |
| P3 | ingest 门控修复（T31 C2 / T33） | 不改门控，账本水位永远推不动，"重训先于样本"的死锁不解除 | R2 有效性 |
| P4 | 全量 pytest 基线锁定 | 见 §4 | 全部 |

**关键依赖（诚实标注）**：R2 的收益**完全依赖 P3**。`T33` 已机械证明全仓 `UPDATE_WRITER=0`、改口径净新增仅 461 行且跨 9 天迭代窗口。**所以"修好 retrain_gate"本身不解锁 G1；它只是让门控以后不再撒谎。** 真正的解锁点是 ingest 门控 + 按 walkforward 分段补账（T34 推演：补跑 2026-11-03 可达、不补跑 NEVER）。

---

## 4. 门禁与验收基线

```bash
cd /d/Architecture
PY=.venv/Scripts/python.exe
$PY -m pytest tests/ -q --timeout=120            # 全量回归; 落地前先记录基线数
$PY -m pytest tests/test_oos_guard.py -q          # 晋升门禁 (runbook §3[3])
$PY scripts/eval_prediction_calibration.py        # 校准评估 (drift 口径)
```
- 全量回归当前基线：**340 passed**（2026-09-27，含 T36 新增 24 条）。新增测试须使总数单调上升。
- 落地的任一 enhancements 若使总数下降，一律 fail-closed 回退。

---

## 5. 回滚总表

| 修复 | 回滚动作 | 副作用 |
|---|---|---|
| R1 | `git checkout -- scripts/finalize_candles_ensemble.py` | marker 停留在上次写入，只影响水位精度，无功能影响 |
| R2 | `retrain_gate()` 恢复 `suggest = n_new_verdicts >= 100` | suggest 回到"慢性 True"状态（F5），不新增风险 |
| R3 | 文档回退 + T17 判定式回退到「suggest or drift_alert」 | 无（纯分类口径） |
| P1 | 空壳库若不移则**保持现状**，只加路径白名单 | 移库须走归档而非删除（§4 数据资产保全） |

---

## 6. 本轮边界声明

本规格**纯文档**：未改任何生产代码、未跑 finalize、未触发重训、未写 `verification.db` / `events.db`、未碰 `config/settings.yaml`、未动计划任务、未杀任何进程。§1.2 的 N1/N2/N3 均为 `mode=ro` 只读查询结果（账本 9042 行 / verdict 1029 行的计数为 2026-09-27 07:5x 快照，会随时间变化，引用时须重新测量）。
