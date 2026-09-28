# P-ENG 「今日预测 ?」可观测性整改规格（WINDOW 备料，纯规格）

> 任务号：**T50**（承接 T44 撞锁归因 / T48 §S4 可观测契约）
> 证据脚本：`scripts/audit_predict_refresh_observability.py` + `tests/test_audit_predict_refresh_observability.py`（18 passed）
> 只读报告：`reports/predict_refresh_observability_audit.{json,md}`
> **本文件纯规格**：不改 `scripts/autonomous_monitor.py` / 不改 `predict_export` / 不挂调度 /
> 不跑 ingest / 不写 `verification.db` / **零 events.db 写入** / 不碰生产进程。
> 落地须走维护窗口 + 全量 pytest + 回滚预案。

---

## §0 一句话

预测补算失败后，`status['predictions']` 退化成**单键 dict** `{'error': str(e)}`，
摘要行 `.get('after', '?')` 取不到就打印「今日预测 ?」——
**「补算撞锁失败」与「今日本就没有可预测场次」在状态文件与摘要行里完全同形**，
且连续撞锁没有任何计数器，单周期成功即可把「链路曾出问题」抹平。

---

## §1 现状（只读实证，2026-09-28，as_of 历史 94 周期）

| 项 | 实测值 |
|---|---|
| `reports/monitor_history.jsonl` 周期数 | 94 |
| 形态分布 | `OK 89 / DEGRADED_LOCK 5` |
| 降级率 | 5.32% |
| 锁类失败周期 | 5（全部 `database is locked`，无第二类错误） |
| 降级发生时刻 | 09-27 08:56:30 / 10:02:23 / 13:15:11 / 14:18:39 / 09-28 00:00:01 |
| **滞后规则下的连续段** | **1 段，长度 7（索引 87–93，当前仍在进行）** |
| T48 §S4 四字段历史覆盖率 | **0 / 0 / 0 / 0（94 周期中从未出现过）** |
| `narrative_added` None(失败) vs 0(无增量) | 0 vs 12 |
| 消费者扫描 | `WRITER 2`（仅 monitor 自身落盘行）/ `READER 0` / `MENTION 41` |

### N1 降级路径源码（三处，逐行定位）

```python
# scripts/autonomous_monitor.py:193-196  失败分支只留 error 单键
    try:
        status['predictions'] = refresh_predictions()
    except Exception as e:
        status['predictions'] = {'error': str(e)}          # ← 无 kind / 无计数 / 无 retry_after
        log(f'预测补算失败: {e}', 'ERROR')
# scripts/autonomous_monitor.py:219-223  摘要行把缺失的 after 印成 "?"
    log(f"周期完成 ... | 今日预测 {status.get('predictions', {}).get('after', '?')} | ...")
# scripts/autonomous_monitor.py:200-203  成功分支
    out = {'date':..., 'before':..., 'added':...,'after':...,'sources':{...}}
```

→ **`after` 只在成功分支存在**，因此失败周期必然打印 `今日预测 ?`；
而「今日无场次」走的是成功分支（`after=0`）——**同一字符串覆盖两种相反语义**。

### N2 滞后规则为什么必要（本轮核心新证据）

按「逐周期连续计数」读，上表 5 次锁失败被 09-27 12:13 那次成功切成若干段，最长仅 2；
按**滞后规则**（连续 2 个干净周期才清零，单次成功不 reset）重算，**它们连成一段长度 7**。

- 这不是算法花样，而是与 T44/T48 的实证一致：**受害者固定**（T48 N3：同一批比赛名在
  08:57 与 10:02 两轮逐条重合），说明存在持续持锁方，中间那次成功只是抖动。
- 若计数器被单周期成功清零，**「链路已恢复」的结论会被单次抖动伪造出来**，
  进而让 O2 的告警永远只在「刚好连续」时出现 → 与 T48 §S4「单调计数器不得被单周期成功清零」
  的要求直接冲突。故本规格把滞后 2 做成**硬规则**，不是可调参数。

### N3 `classify_error` 的字符串陷阱（本轮新发现，直接影响 O1 写法）

`core/error_envelope.classify_error` 是锁判定 SSoT，但它只在**异常对象本身是
`sqlite3.OperationalError`** 时才识别 `lock/busy`：

```python
classify_error(sqlite3.OperationalError('database is locked'))  # -> ('DB_LOCKED', 503)  ✅
classify_error(Exception('database is locked'))                 # -> ('INTERNAL', 500)   ❌
```

而 monitor 的 `log(f'预测补算失败: {e}')` 写的是 `str(e)`。因此
**O1 落地时必须把原始异常对象 `e` 传给 `classify_error`，不能传 `str(e)`**，
否则 `error_kind` 会退化成 `REAL_ERROR`，把撞锁误报成代码错误——这是"加了可观测反而更误导"
的典型路径。本脚本的 `classify_error_kind()` 已按「异常对象走 SSoT、字符串走 SSoT+同义词兜底」
两级口径实现并写进测试。

### N4 可观测字段无处落地（结构性）

`status` 每周期**整体覆盖写**到 `monitor_status.json`、`HISTORY` 以行追加；
`reports/monitor_status.json` 现存 `predictions` 键仅
`{date, before, added, after, sources}`（成功路径）或 `{error}`（失败路径）。
没有专用计数器字段、没有历史窗口，也没有任何「上一周期」对比位置。
→ 计数器只能**从 `monitor_history.jsonl` 离线重算**（`hysteresis_streaks()` 已实现），
  因此 O2 **不需要新增任何持久化状态**，这是本规格能低成本落地的关键。

### N5 消费点缺失（诚实前置）

扫描 41 处提及中，只有 monitor 自己 2 处是写；**没有任何生产代码以 `open(...,'r')` 读取
`monitor_status.json`**（审计脚本走 glob、文档/记忆为叙述引用，按 T42 口径不计消费方）。
backend / frontend 命中 **0**。→ **字段加得上去也无人看**；
O1–O4 必须与**一个真实消费点**（看板 / 日报 / 告警通道）同窗口落地，否则只是把失败从
「?」变成「字段里有 but 没人读」。这与 T08「无消息总线」、T38「suggest 恒 True 无人消费」
是同一类结构问题。

---

## §2 缺陷清单

| ID | 缺陷 | 证据 | 严重度 |
|---|---|---|---|
| D1 | 失败块只有 `error` 字符串，无法分辨锁失败/真错误（只能子串猜测） | N1 / 历史 5 次降级全部为 `database is locked` | HIGH |
| D2 | 摘要行 `?` 语义二义（失败 ≡ 无场次） | N1 `:222` | HIGH |
| D3 | 无连续撞锁计数器，单次成功即抹平 | N2 | HIGH |
| D4 | `status` 每周期整体覆盖，无历史窗口 | N4 | MEDIUM |
| D5 | `narrative_added` 用 `None` 表示失败（防御性；历史 0 次触发，但与 D2 同型） | 报告 `narrative` 段 | LOW |

---

## §3 整改设计（O1–O4，与 T48 §S4 对齐）

### 字段契约

`status['predictions']` 统一带 `outcome` 判别键，失败时补充：

| 字段 | 取值 | 来源 |
|---|---|---|
| `outcome` | `ok` / `no_matches` / `failed` | 三态互斥（O3） |
| `error` | 字符串（保留） | 现状 |
| `error_kind` | `LOCK_CONTENTION` / `REAL_ERROR` | `core.error_envelope.classify_error`（**传异常对象**，见 N3） |
| `consecutive_lock_failures` | int，**滞后 2 清零** | O2 / T48 §S4 |
| `lock_affected_matches` | list[str] 或 `[]` | T48 §S4（`lock_retries>0` 时才有值） |
| `retry_after` | float 秒（本轮退避总时长） | 与 T48 S1 参数表同量级 |

`outcome='no_matches'` 只在「今日 `matches` 行数为 0 且补算成功」时输出，
用于把「无场次」从 `?` 中彻底摘出去。

### O1 predictions 失败块补齐 error_kind

- **改什么**：`:195-196` 改为
  `status['predictions'] = {'error': str(e), 'error_kind': kind, 'consecutive': n, 'retry_after': sec, 'outcome': 'failed'}`，
  `kind` 由 `classify_error(e)[0]` 得到（`e` 为原始异常对象）。
- **为什么**：D1；`error_kind` 是下游一切判定的前提（告警分级、是否重试、是否牵连叙事）。
- **门禁**：G1 `error_kind ∈ {LOCK_CONTENTION, REAL_ERROR}`；G2 成功块**不得**出现 `error` 键
  （防下游用 presence 反推健康）；G3 摘要行改为打印 kind 而非裸 `?`。
- **回滚**：字段可缺省，老解析器按 absence 处理。

### O2 连续撞锁滞后计数器 + 阈值告警

- **改什么**：新增 `consecutive_lock_failures`（滞后 2 清零）；`>= 3` 时 `log WARN`
  并在 `status` 内写独立键 `lock_alert: true`；**字段与告警键分离**（否则"计数存在"会被当成"已告警"）。
- **为什么**：D3 / N2 —— 实测单次成功不该抹平，受害者固定说明抖动不是恢复。
- **门禁**：G1 连续 2 个干净周期才清零（`hysteresis_streaks` 已实现并有单测）；
  G2 计数器可完全由 `monitor_history.jsonl` 离线重算 → **无需新增持久化状态**；
  G3 `lock_alert` 与 `consecutive` 同时可查。
- **回滚**：不写计数键即回退，无数据副作用。

### O3 摘要行三态可区分

- **改什么**：摘要模板改为 `今日预测 {after}{/无场次 | /失败:{kind}}`；
  `no_matches` 仅在今日 `matches` 计数为 0 且补算成功时输出。
- **为什么**：D2 —— 现在人工巡检无法判断「要不要跟」。
- **门禁**：G1 `outcome ∈ {ok, no_matches, failed}` 且三者互斥；G2 不新增 events.db 写方。
- **回滚**：回退旧模板，无迁移。

### O4 `narrative_added` 的 None/0 歧义

- **改什么**：叙事失败记 `{'error':..., 'error_kind':...}` 而非 `None`；无增量仍记 `0`。
- **为什么**：D5（与 D2 同型，历史 0 次触发 = 防御性修复，非当期缺陷）。
- **门禁**：G1 成功路径不得出现 `None`；G2 旧历史行仍可解析。
- **回滚**：字段可缺省。

### 落地顺序（强制）

```
T48 A1（事务切分 ≤50 场）  ← 重试与计数的生效前提
        ↓
O1（error_kind）           ← 计数与告警的前提
        ↓
O2（滞后计数器 + 告警键）
        ↓
O3（摘要三态）  ← 与「一个真实消费点」同窗口
        ↓
O4
```

> T48 N2 已证：失败周期同样烧满一整轮算力（失败↔周期结束间隔 ≈ `busy_timeout` 30s），
> 在 `A1` 之前"重试 + 计数"只是把失败重复三次。**计数可观测 ≠ 修复**，但不可观测的失败
> 连"要不要修"都决定不了——这是本规格存在的理由。

---

## §4 验收门禁（fail-closed）

| ID | 断言 | 不通过的处理 |
|---|---|---|
| V1 | 失败块必含 `error_kind` 且 ∈ 定义集 | FAIL，禁止上线 |
| V2 | 成功块不得含 `error` 键 | FAIL |
| V3 | `consecutive_lock_failures` 滞后 2 清零（单测 + 历史重算一致性） | FAIL |
| V4 | 历史 94 周期重算段数与 `predict_refresh_observability_audit.json` 一致 | FAIL（口径漂移） |
| V5 | `outcome` 三态互斥且摘要行打印 outcome | FAIL |
| V6 | 本改动**零 events.db 写入**（连接数/写语句增量 = 0） | FAIL |
| V7 | 不新增 events.db 写方（写方白名单不变） | FAIL |
| V8 | **至少一个真实消费点**读取新字段（否则 O1–O2 视为未闭环） | FAIL |
| V9 | 全量 pytest 绿（基线以落地前为准，允许只增不减） | FAIL |

---

## §5 回滚

| 项 | 方式 | 副作用 |
|---|---|---|
| O1/O2/O3/O4 | 删除新增字段，恢复旧 dict 与旧摘要模板 | 无（字段可缺省） |
| 新增键 `lock_alert` | 删除键 | 无 |
| 与 T48 重试叠加 | 关掉重试开关即回退（`attempts=1` 等于现状） | 无 |

---

## §6 诚实边界与未决

- **未决 Q-a**：消费点选哪个（前端看板 / 每日日报 / 告警通道）——**必须先有这个决定**，
  否则 O1–O2 只是把失败写进没人读的文件（N5 已证）。
- **未决 Q-b**：`lock_affected_matches` 是否直接进 `monitor_status.json`
  （T48 §Q-c 已提出：名单进快照会让下游抄值，倾向只存计数）。
- **未决 Q-c**：滞后 2 是硬规则还是可调常数（本规格按硬规则写，因 N2 已证受害者固定；
  若后续出现「抖动型」锁失败需重新标定）。
- **未决 Q-d**：历史 94 周期中 `narrative_added` 从未为 `None`，O4 的收益目前是防御性的，
  不是当期缺陷修复——不应在报告中表述为"已修 bug"。
- 本报告全部数字来自只读证据；落地后结论**不得**被表述为"撞锁已修复"——
  O1–O4 只让失败可见，**不减少撞锁**（减少撞锁属 T48 重试与 T44 R2/R4 的范畴）。

## §7 红线自查

- IR-30：本规格不触碰任何概率/赔率口径，不产生盈利声称；无 edge 不被宣称。
- IR-32：不引入任何跨庄字段，`lock_affected_matches` 只记 match_key，不含跨庄共识。
- §4 数据保全：零 events.db 写入（V6 守卫）。
- §6 进程安全：不改生产进程、不杀进程、不重启任何服务。
- 售卖：本规格不涉及售卖动作。
