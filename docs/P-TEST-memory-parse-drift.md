# P-TEST · 自动化 memory 解析漂移修复规格（T60）

> 承接：`T58` 全量回归暴露的既有基线失败
> `tests/test_snapshot_automation_health.py::test_real_memory_file_parseable`
> （断言轮次 `>= 20`，实测 9）。性质：**不是断言口径 outdated，是两个真解析 bug + 一次文件结构事故。**

---

## §0 一句话结论

断言 `>= 20` **从头到尾没有算错**——真实轮次本就远超 20，被截断的只有 9 条；
修的是解析器和那份被写坏的 `memory.md`，**断言一个字符都没改**。

## §1 四问答案

### ① `LINE_RE / TASK_RE / PASSED_RE` 对新旧两种 bullet 的实际命中差异

| 正则 | 旧行为 | 新行为 | 影响 |
|---|---|---|---|
| `LINE_RE` 时刻字段 | 只认 `12:3x` 单时刻 | 支持 `07:3x–08:0x` / `07:3x—08:0x` / `07:3x - 08:0x` / `07:3x~08:0x` | 旧版对**整行不匹配**，T57、T58（最新一轮）直接消失 |
| `TASK_RE` | `\bT(\d{2,3})\b` | 先抹掉 `T01-T29` 区间写法再匹配，并允许 `T30-B` 后缀 | 旧版把巡检轮次记成任务 `T01`，污染 `distinct_task_ids` |
| `PASSED_RE` | 无变化 | 无变化 | — |

旧样式（`- 2026-09-25 12:3x 建队列(14项) + 示范 T01(...)`）与新样式（`- 2026-09-27 22:4x **T48(...)`）
在正则层面**本来就是同一条式**——差异不在样式而在上文 D1/D2 两处结构。

### ② 是解析器 bug 还是断言口径 outdated

**是解析器 bug，且有两处独立成因：**

- **D1（文件结构事故）** `memory.md` 里出现了一段**逐字重复的第二段 header**
  （`调度/机制/D:\…/## 红线/## 执行历史`），把"顶部追加的近期轮次"与"主序列 T01–T47"切成两处。
  `parse_history()` 的语义是「遇到第一个 `## ` 就 break」，于是**在主序列之前就截断**——
  只剩顶部 9 条（其中 T57 因 D2 未计入，实测 9 条）。
  证据：删除重复块前，`## 执行历史` 在文件中出现 **2** 次；修复后 **1** 次。
- **D2（正则）** `LINE_RE` 的时刻字段只认单时刻，凡带区间的整行**整行不匹配**：
  `- 2026-09-28 07:3x–08:0x **T57(...)`（U+2013）与 `- 2026-09-28 09:1x–09:3x **T58(...)`。

两者独立：只修 D1 仍会丢 T57/T58；只修 D2 仍会被重复 header 截断。
**改断言 = 把 bug 焊进基线，禁止**（本轮未改）。

### ③ 修复后 `summarize()` 会不会跳变

会，且这是**修复的本来含义**（旧数字是错的，不是口径变了）：

| 指标 | 修复前（实测量） | 修复后 | 说明 |
|---|---|---|---|
| rounds | 9 | **59** | 恢复被截断的 50 条 |
| code_tasks | 8 | **44** | 同上 |
| total_passed | 3395 | **6097** | 各轮记录的全量回归通过数之和 |
| real_finding_rounds | 0 | **8** | 旧值 0 本身就是"根本没读到历史"的指纹 |
| queue_clear_or_inspection | 6 | **16** | 同上 |
| run_days | 2 | **4** | 09-25~09-28 |
| first_round | `2026-09-27 22:4x` | **`2026-09-25 12:3x`** | **语义修复 D3**，见下 |

**D3（字段语义）**：`first_round/last_round` 原取 `entries[0]/entries[-1]`，即**文件行序**；
而新条目是**追加到文件顶部**的 → `first_round` 会被顶到最新一轮（09-27）而不是真正的首轮（09-25）。
改为按 `(date, time_key)` 取极值。`test_summarize` 用的样本本身就是时序，不受影响。

### ④ 连带发现

`distinct_task_ids` 由 9 → **52**，其中"巡检"条目**不得**带任务号（D4）。

---

## §2 已落地改动

| 文件 | 改动 |
|---|---|
| `scripts/snapshot_automation_health.py` | `LINE_RE` 支持时刻区间；新增 `TASK_RANGE_RE` + 收紧 `TASK_RE`；`parse_history()` 改为**跨所有同名单段累积**（遇其他 `## ` 关闭、遇 `## 执行历史` 重新打开）；新增 `count_history_sections()`；`summarize()` 首/末轮改按日期取极值；`main()` 对 `sections != 1` 打印 `[WARN]` 并把 `history_sections` 写进 JSON |
| `tests/test_snapshot_automation_health.py` | 8 条 fail-closed 守卫（段数唯一 / 跨段累积 / 旧 break 语义反证 / 时刻区间 / 区间任务号 / 首末轮极值 / 最新一条必须被计入 / 无重复轮次）；原 7 条不动 |
| `scripts/audit_memory_parse_drift.py` + `tests/test_audit_memory_parse_drift.py` | 只读取证：内嵌**修复前实现** `legacy_*`，在最小复现样本上证明截断机制，在真实文档上量化"旧实现仍会丢 2 条" |
| `.workbuddy/memory/automations/…/memory.md` | 删除逐字重复的第二段 header（9 行）并合并重复的 `## 执行历史` 标题 → 段数 1；删除前已机械校验"与顶部 header 逐字相同"，**无信息损失** |
| `reports/memory_parse_drift_audit.{json,md}` | 判定 **PASS**（无 RED/AMBER） |
| `reports/automation_health.json` | 按新口径重写 |

**诚实边界**：本轮不动断言、不改任何既有判定结论、不碰 events.db、不跑验证台、零进程操作。

## §3 守卫清单（fail-closed）

| 编号 | 断言 | 防的失效 |
|---|---|---|
| G1 | 真实 memory 的 `## 执行历史` 段数 == 1 | 重复 header 把主序列切断（D1 复发） |
| G2 | `parse_history()` 跨所有同名单段累积 | 任何人改回 break 语义 |
| G3 | 带时刻区间的 bullet 命中率 100% | T57/T58 式条目被静默丢弃 |
| G4 | 首/末轮按日期取极值 | 顶部追加条目污染首轮字段 |
| G5 | `Txx-Tyy` 区间不产 task_id | 巡检轮次被记成任务号 |
| G6 | 文件末尾那条 bullet 必须被计入 | "最新一轮消失" |
| G7 | 真实 memory 无重复轮次 | 同一轮被重复计数 |

## §4 未决

- **Q-a** 修复前读数 9 只留档不复算（损坏文档副本已删）；若将来需要逐字节复现，
  需把损坏态快照存进 `archive/`。
- **Q-b** `first_round` 的语义修复会让历史看板数字"变小"（09-27→09-25）；
  若有下游消费该字段须同步（当前消费者：`reports/automation_health.json` 唯一）。
