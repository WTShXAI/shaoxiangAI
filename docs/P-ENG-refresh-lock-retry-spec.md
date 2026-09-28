# P-ENG `refresh_predictions` 撞锁退避重试落地规格（WINDOW 备料 · 纯规格）

- 任务号：**T48**（承接 T44 R1，队列 `docs/AUTONOMOUS_BACKLOG.md`）
- 状态：**纯规格**。未改 `autonomous_monitor.py` / `predict_export.py` / `match_narrative.py`，
  未挂调度、未跑 ingest、未写 `verification.db`、**零 events.db 写入**、未重试、未重启任何进程。
- 依据：`reports/predict_refresh_lock_audit.{json,md}`（T44）+ 本轮 8 条新实证（§2）。
- 纪律：对齐 `docs/DISCIPLINE.md` §1 诚实 / §4 数据资产保全 / §6 进程安全。

---

## §1 现状（逐行锚定）

`scripts/autonomous_monitor.py`：

| 行 | 现状 |
|---|---|
| :93-116 `refresh_predictions()` | **先** `_conn_ro()` 取 `before` 行数（裸 `sqlite3` `mode=ro`）；**再** `from gq.db import conn as gq_conn` → `with gq_conn(): con.executescript(TABLE_DDL); build_for_date(con, today, refresh=True, allow_candles_compute=False)`；**最后** 回到 `_conn_ro()` 取 `after` / `sources` |
| :195-199 | `try: status['predictions'] = refresh_predictions()  except Exception as e: status['predictions'] = {'error': str(e)}` → 摘要行退化成「今日预测 ?」 |
| :200-204 | 同款吞没套在 `narrative_increment()` 上 |
| :119-121 `_conn_ro()` | 裸 `mode=ro`，**无** `busy_timeout` 设置（默认 5s） |

关键结构（决定重试能放哪）：

- `gq/db.py:126-130` 写路径委托 `core.db_manager.get_manager(db_path=DB_PATH).writer()`。
- `core/db_manager.py:466-499` `writer()`：**`is_outermost` 时才 `conn.commit()`**，
  `except → conn.rollback() → raise`。即 **整个 `build_for_date` 全日循环 = 一个写事务**，
  从第一条 SELECT 到 commit 全程持 RESERVED 锁。

---

## §2 本轮新实证（相对 T44 的增量，均只读）

### N1 写事务粒度 = 全日循环（不是单次写语句）

`core/db_manager.py:483-497` 的 `if is_outermost: conn.commit()` 说明：重试若放在
`build_for_date` **内部逐场**做，在一场失败后就 retry，事务并未提交 → 重试必然复用同一被
拒绝的写上下文，**是无效功**。重试边界只有两个有效位置：①进入 `with gq_conn()` 之前的准入；
②整块外层重试（本规格推荐）。

### N2 锁失败不是廉价的放弃：失败周期 ≈ 成功周期时长

`reports/predict_refresh_lock_audit.json` 实测（2026-09-27）：

- 失败周期 `cycle_sec` = 62.6 / 63.8 / 62.3 / 68.3 s
- 成功周期 `cycle_sec` = **67.4** s（12:13 那轮）

两者同量级 → **失败的一轮同样跑完（甚至卡满）约一整轮工作量，只在 commit 阶段撞锁并整体作废**。
今日 865 场、4 次失败 ≈ **4.3 分钟纯算力作废**。这是"加重试"的直接成本约束：
**盲重试会把 4.3 分钟的作废算力再复制 3 倍**，故 §3 必须先做事务切分再谈重试。

### N3 同周期双处损伤；per-match 失败是连带不是第二故障（修正 T44 表述）

本轮重新解析 `logs/autonomous_monitor.log`（**逐行解码**utf-8→gbk→latin-1，见 T44 混合编码坑）：

- 全日志 **179 个周期**，其中 **175 个周期 per-match 失败数为 0**；
- 出现 per-match 失败的仅 **4 个周期**，计数 **8 / 16 / 34 / 56** 场；
- 这 4 个周期与 4 个 bulk `[ERROR] 预测补算失败` 周期**完全重合**（08:57 / 10:02 / 13:16 / 14:19）；
- 名单跨轮重合：同一批比赛名在 08:57 与 10:02 两轮逐条重复出现。

→ 结论：叙事侧 `database is locked` 是**主路写事务失败的连带损伤**，不是独立故障；
且受害者**跨轮固定** → 存在持续持锁方（大概率是 `ws_collector` 对同批 `matches` 行的长事务），
**不是随机争用**。对规格的含义：纯"随机退避重试"命中不了固定受害者，**必须配准入检查（§3 S1）**。

### N4 全仓零重试原语；但错误分类已有 SSoT

- `grep -rn "def retry|backoff|jitter" core/ pipeline/ gq/` = **0 命中**；
- 全仓 `except sqlite3.OperationalError` 共 15 处（scripts/9 + pipeline/7 + verification/1，
  已排除 tests 与审计脚本自指），**全部为吞没或改判码，无一重试**；
- **`core/error_envelope.classify_error()`（`core/error_envelope.py:178`）已是 SSoT 判据**：
  `:220-222` 判定 `isinstance(exc, sqlite3.OperationalError)` 且 message 含 `lock`/`busy`
  → 返回 `CODE_DB_LOCKED`（常量定义在 `:60`）+ 503。

→ R3 的 `error_kind=LOCK_CONTENTION` **必须复用 `classify_error`**，禁止在 monitor 里自写正则
（否则同一条错误在两条判据下分叉，T38 式的"抄值不重算"污染会复现）。

### N5 进程内 RLock 对跨进程撞锁无效

`writer()` 的互斥是 `threading.RLock`（**进程内**）。monitor 与 `ws_collector` 是两个进程，
RLock 完全不产生跨进程互斥。因此：

- monitor **拿到**写锁时块内重试必然成功 → 块内重试是死代码；
- monitor **拿不到**写锁时块内重试永远拿不到 → 块内重试是无效功。

唯一有效组合 = **外层整块重试 + 进入前的锁准入**。

### N6 幂等性成立，但会刷新 `generated_at`（与 T35 口径冲突）

`build_for_date(refresh=True)` 只处理未开赛行（`existing and ko_ts and now > ko_ts → continue`），
写入走 `ON CONFLICT(match_key) DO UPDATE` → 重试**不产生重复行**，幂等成立。
但 `generated_at=time.time()` 每次都变，而 **T35 已证 `daily_predictions.generated_at` 才是样本
生成时刻**（`created_at` 只是入账批次）→ 一次重试会静默改写全部已定时行的生成时刻，
使 T35/T43 的分段边界漂移。**规格硬要求：重试次数必须可观测并计入分段元数据**（§3 S4）。

### N7（附带）`gq/db.py:128-131` 只读兜底会变成写方

```python
try:
    c = sqlite3.connect(_uri, timeout=30, uri=True)   # mode=ro
except (sqlite3.Error, ValueError):
    c = sqlite3.connect(DB_PATH, timeout=30)          # ← 可写！
```

任何走 `conn(readonly=True)` 的"只读"调用方，在 URI 构造异常（如路径含特殊字符）时会**静默获得
可写连接** → 潜在撞锁放大源。本轮的 `refresh_predictions` 用的是裸 `_conn_ro()`，**未命中**该
兜底，但属同族隐患，列入 §5 清理清单（不在本规格落地范围）。

### N8 `busy_timeout` SSoT = 30000

`core/config.py:18 busy_timeout_ms: int = 30000`；`gq/db.py:135` 只读分支硬编码
`PRAGMA busy_timeout=30000`。→ **重试参数不得小于 30s 的等待语义**，否则"重试"退化成
"立刻失败立刻重跑"，把 30s 的背压变成 3 次瞬时抢锁，反而放大写压力。

---

## §3 重试设计（本规格核心）

### S1 重试参数（硬编码，禁止运行期放大）

| 参数 | 值 | 依据 |
|---|---|---|
| `attempts` | 3 | N2：失败一轮已烧满一整轮算力，超过 3 拍收益递减 |
| `base_delay` | 2.0 s | 与 `busy_timeout=30s` 同量级（N8），避免把背压变抢锁 |
| `factor` | 3.0 | 2 → 6 → 18 s |
| `jitter` | ±30% | 防止 monitor 与采集器回落同刻再撞 |
| `cap` | 120 s | 保底周期预算 |
| `retry_scope` | **整块 `with gq_conn()`** | N1/N5 |
| 判定 | `classify_error(exc)[0] == CODE_DB_LOCKED` | N4 复用 SSoT，禁止自写正则 |

### S2 伪代码（落地形态，不得改生产本轮）

```python
from core.error_envelope import classify_error, CODE_DB_LOCKED

def is_lock_error(exc) -> bool:
    try:
        return classify_error(exc)[0] == CODE_DB_LOCKED
    except Exception:
        return False

def refresh_predictions_with_retry(attempts=3, base=2.0, factor=3.0,
                                    jitter=0.3, cap=120.0):
    delay = base
    last = None
    for i in range(1, attempts + 1):
        try:
            return refresh_predictions(), 0          # 0 = 无重试
        except Exception as e:
            if not is_lock_error(e):
                raise                                 # N: 非锁异常必须 fail-fast
            last = e
        if i < attempts:
            time.sleep(min(delay, cap) * (1 + (random.random() * 2 - 1) * jitter))
            delay *= factor
    raise last
```

调用侧（`:195-199`）改为：

```python
try:
    res, retries = refresh_predictions_with_retry()
    status['predictions'] = res
    status['predictions']['lock_retries'] = retries   # S4 可观测
except Exception as e:
    kind = 'LOCK_CONTENTION' if is_lock_error(e) else 'REAL_ERROR'
    status['predictions'] = {'error': str(e), 'error_kind': kind,
                             'consecutive': _bump_consecutive(kind)}
    log(f'预测补算失败[{kind}]: {e}', 'ERROR' if kind == 'REAL_ERROR' else 'WARN')
```

### S3 前置硬约束（**先做这个，再做重试**）

> N2 已证一次失败 = 一整轮算力作废，盲重试 = 3 倍算力复制。
> 故 **A1（事务切分）是本规格的生效前提**，未完成前 `attempts` 必须保持 1（等于现状）。

- **A1 事务切分**：把 `build_for_date` 的全日单事务改为**分批提交**（每批 ≤ 50 场，
  批间 `commit()`）。切分点必须在**推理之后、写入之前**，不得把 `predict_match_full`
  拆散（否则重复推理 = 重复算力，与 N2 同病）。切分后重试粒度降到"一批"，代价从
  一整轮降到 ≤1/17。
- **A2 锁准入（命中 N3 固定受害者）**：进入 `with gq_conn()` 前，先以 `mode=ro`
  读 `PRAGMA journal_mode` 之外的最小快照成本检查 —— 若上一周期记录 `lock_retries>0`，
  本周期把首次尝试整体推迟到周期起始 +`base_delay` 之后（**不是重试内部 sleep，而是
  跨周期避让**），避免与采集器的固定写入节奏正面对撞。
- **A3 不新增 events.db 写方**：本改动只改 monitor 内的调用包装，不引入新连接类型、
  不引入新写路径（与 T47 结论一致：`verification` ingest 走另一库文件，不受影响）。

### S4 可观测字段（承接 T44 R3，并按 N6 强化）

`status['predictions']` 新增：

| 字段 | 含义 |
|---|---|
| `lock_retries` | 本周期重试次数（0 = 一次成功） |
| `error_kind` | `LOCK_CONTENTION` / `REAL_ERROR`（来自 `classify_error`） |
| `consecutive_lock_failures` | 连续撞锁周期数（**供 N3 固定受害者识别用**） |
| `lock_affected_matches` | per-match 失败名单（本轮实测 8/16/34/56，为空则记 `[]`） |

`consecutive_lock_failures >= 3` 时**必须**进 WARN 日报（现在只退化成「今日预测 ?」，
与「今日无赛」不可区分 —— T44 R3 原话，本轮 N3 证明它是固定受害者信号，不是偶发）。

---

## §4 验收门禁（fail-closed）+ 回滚

| 编号 | 断言 | 失败即拒绝 |
|---|---|---|
| V1 | `is_lock_error` 对 `OperationalError("database is locked")` 为真，对 `ValueError` 为假 | 拒绝 |
| V2 | 非锁异常 **不被重试**（单测：连续 3 次抛 `RuntimeError` 只调用 1 次） | 拒绝 |
| V3 | 退避序列单调不减且 `<= cap`（2×1.3=2.6 / 7.8×0.7=5.46 … 上限 120） | 拒绝 |
| V4 | 重试前后 `daily_predictions` **行数不减少**（幂等，N6） | 拒绝 |
| V5 | 重试后 `daily_predictions` **既有行主键集合不变**（只覆盖不新增，N6） | 拒绝 |
| V6 | A1 切分后单批写事务 `<= 5s`（打点计时，写入 `cycle_sec` 拆分字段） | 拒绝 |
| V7 | `lock_retries` 字段缺失时老解析器按 absence 处理（向后兼容，T44 R3） | 拒绝 |
| V8 | 全量 `pytest` 绿；`tests/test_refresh_lock_retry.py` 为纯 mock，**零生产 I/O、零 events.db 写入** | 拒绝 |

回滚：

| 编号 | 动作 | 代价 |
|---|---|---|
| R1 | 恢复单次 `refresh_predictions()` 调用 | 失败率回到原基线（4/96 ≈ 4.2%） |
| R2 | 去掉 `lock_retries`/`error_kind` 字段 | 字段可缺省，V7 保证兼容 |
| R3 | 关掉 A2 跨周期避让开关 | 回到固定受害者状态，无数据副作用 |
| R4 | 回退 A1 事务切分（若出现批次内部分写入） | 单次回滚窗口内可能丢 ≤1 批（50 场），次日自动补齐（refresh 幂等） |

---

## §5 执行顺序与前置（WINDOW 停机窗口）

强制顺序：**A1（事务切分）→ S1/S2 重试 → A2 避让 → S4 可观测 → R3/R4 可观测**。
理由：N2 已证失败一轮 = 一整轮算力；不先切分就开重试，等于把 N2 的损失乘 3。

前置（与既有任务的关系）：

1. **T47**（ingest 挂 monitor 末尾）与本文**无锁冲突** —— `verification` ingest 只读
   events.db 但写 `verification.db`（不同库文件），顺序建议
   `predictions（本文） → ingest（T47）`：predictions 先落库，ingest 才能读到新 daily 行。
2. **T44 R2**（读写面分离）是本规格 A1 的上位目标，本规格只取其中"事务粒度"一半，
   不触碰 long-lived writer 改造（跨模块，风险更高，留给 R2 本体）。
3. `gq/db.py:128-131` 只读兜底（N7）建议并入 T44 R2 同窗口清理，本规格不落地。

---

## §6 未决（人工拍板）

- **Q-a**：A1 批次大小取 50（今日 865 场 ≈ 17 批）还是 100（≈9 批）？批次越小越省算力，
  但批间 commit 越多、撞锁次数越多（每次 commit 都要重新抢写锁）。**推荐 50**。
- **Q-b**：A2 的跨周期避让准确率无法离线验证（需在生产上跑 24h 观察
  `consecutive_lock_failures`），是否接受"先上线观察 1 天再决定是否常驻"？
- **Q-c**：`lock_affected_matches` 名单直接进 `monitor_status.json` 会不会让下游
  报告（T38 已发现抄值不重算问题）再次把瞬时状态当结论？建议只存**计数**、名单另存
  `reports/monitor_lock_victims.jsonl`。
- **Q-d**：是否顺手修 `gq/match_narrative.py:310` 每轮 `executescript(TABLE_DDL)`
  （DDL 抢锁 + 无变更仍执行）？它不属本规格范围，但 N3 的固定受害者很可能与它同源。

---

## §7 红线自查

| 红线 | 结论 |
|---|---|
| IR-30 诚实 | 本规格不含任何 edge/盈利主张；全部数字来自只读日志/源码/既有报告 |
| IR-32 跨庄禁区 | 不引入任何跨庄字段；`daily_predictions` 写入列集合不变 |
| §4 数据资产保全 | 零 events.db 写入：未跑 ingest、未改 schema、未 VACUUM、未落库 |
| §6 进程安全 | 未杀任何进程；未重启 monitor；重试参数经 V2 保证非锁异常不重试 |
| 售卖 | 未涉及；Package B 售卖仍须人工 + 法务双签 |
| WINDOW | 纯备料：未改任何生产文件、未挂调度、未运行重试路径 |
