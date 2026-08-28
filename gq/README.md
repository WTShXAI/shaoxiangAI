# GQ 自动赔率采集系统 — 完整赛事库 v3 (events.db)

## 核心决策 (2026-08-27 重构收尾)

**events.db 是唯一的完整赛事库，表结构与 GQ.db 完全对齐（同名同构）。**
所有分析流水线 reader（bridge_service / analysis/* / deploy/* 共 174 个文件）只需把
字符串 `GQ.db` → `events.db` 即可**零 SQL 改动**切换。GQ.db 保留为历史归档（只读、可回滚）。

> 为什么是"对齐 GQ 表结构"而不是"新建一组表"：旧 `event_db.py` 曾用新表名
> (`odds`/`results`/`content`/`h2h`)，与 reader 期望的 `odds_snapshots`/`match_outcomes`/
> `match_meta` 不兼容 —— 直接改 reader 会挂。最终方案改为委托 `gq.db.init_db()`
> 在 events.db 上建 GQ 全同名表，采集器/reader 共用同一套表结构。

## 表结构 (events.db = GQ 同名表 + 2 张新表)

| 表 | 来源 | 说明 |
|----|------|------|
| `matches` | GQ 同名 | 比赛主表（队名/联赛/开赛/状态/比分/分钟/last_seen/mid） |
| `odds_snapshots` | GQ 同名 | 全市场盘口快照（1X2/AH/OU/CS/角球/BTTS/OE/DNB/GOALS/WS_*） |
| `odds_changes` | GQ 同名 | 盘口变化（from→to） |
| `match_outcomes` | GQ 同名 | 赛果（score_home/away/result + ht_*） |
| `match_analysis_cache` | GQ 同名 | 赛事分析缓存（赛前 _live_predict 快照 + 赛后修正） |
| `match_meta` | GQ 同名 | 内容（前瞻/伤病/情报） |
| `prematch_conclusion` | GQ 同名 | 赛前结论 |
| `pre_match_cs` | GQ 同名 | 赛前波胆 |
| `cs_verification` | GQ 同名 | 波胆校验 |
| `interface_doc` | **新增** | 接口地址/用法说明（AI 可读，见下；10 条 seed） |
| `h2h` | **新增** | 赛果页挖掘的两队历史交锋（含 overunder 历史胜负平） |

## interface_doc (AI 可读接口说明)

`gq/event_db.py::_SEED_DOC` 内置 10 条说明，AI 软件读此表即可知道：用了哪些接口、
地址是什么、怎么鉴权、参数/返回/示例。覆盖：
WS 实时盘口流(C105) / WS 比分流(C103) / WS 事件流(C102) / HTTP 比赛列表 / HTTP 比赛结构 /
赛事内容端点 / 赛果页H2H / 单场终比分来源 / GQ.db(历史归档) / **events.db(本库)**。

## 采集器 (唯一写入方)

- `gq/ws_collector.py` — 乐鱼 WS 实时盘口流（Playwright+Edge 接管 H5 会话，gunzip C105 帧），
  复用 `gq.db` 写入层（`upsert_match` / `record_snapshot`）写 events.db。全市场盘口 + 主表 + 比分 + 状态。
- `gq/content_collector.py` — 赛事内容 HTTP 端点（前瞻/伤病/情报 + 赛果页 H2H），
  写 `match_meta` + `h2h`。
- `gq/db.py` — **SSoT 写入层**，`DB_PATH` 已切到 events.db；`init_db()` 建全部 GQ 同名表。
- `gq/event_db.py` — 完整赛事库管理：`init_event_db()`（建 GQ 同名表 + 2 新表 + seed 接口说明）、
  `backfill_all_from_gq()`（ATTACH GQ.db 合并全部表，原文件不动）、`record_h2h()`、
  `backfill_h2h_from_result_page()`（逐场挖赛果页 H2H，需 token 有效）、`stats()`。

## 核心文件

```
gq/db.py                 — ★SSoT 写入层, DB_PATH=events.db, init_db() 建 GQ 全同名表
gq/event_db.py           — 完整赛事库管理(建表/回填/接口说明/统计), 表结构对齐 GQ
gq/ws_collector.py       — 乐鱼 WS 实时盘口流采集器(写 events.db)
gq/content_collector.py  — 赛事内容(前瞻/伤病/赛果H2H) HTTP 采集(写 match_meta + h2h)
gq/auto_collector.py     — 辅助 HTTP 列表/结构/网络工具(已退役主采集, 仅工具)
gq/start_collector.py    — 启动入口(→ ws_collector -d 0)
gq/watchdog_collector.py — 看门狗(每5分钟保活, \ShaoxiangGQ_Watchdog)
gq/launcher.py           — Python 后台启动器
gq/diag_cs.py            — 波胆诊断(保留)
gq/record_image.py       — 截图入库工具(保留)
gq/_trash_2026-08-27/    — 已退役脚本归档(goals_db/cs_collector/diag_*/api_probe 等)
data/events.db           — ★完整赛事库(GQ 同名表 ×9 + interface_doc + h2h), 全系统唯一读源
data/GQ.db               — 历史归档库(原文件不动, 可回滚; backfill 来源)
```

## 历史回填

`python gq/_migrate_events_to_gq.py`（一次性）：
1. DROP 上一轮 event_db 旧表（matches/odds/results/content）
2. `init_event_db()` 建 GQ 同名表 + interface_doc(10) + h2h
3. `backfill_all_from_gq()` ATTACH GQ.db 合并全部表（原文件不动）

> 回填后 events.db 与 GQ.db 数据一致；GQ.db 留作归档不实时写。

## 使用方法

### 采集器守护模式
```bash
python gq/start_collector.py          # → ws_collector -d 0
```

### 完整赛事库统计
```bash
python -c "import gq.event_db as e; e.init_event_db(); print(e.stats())"
```

### 赛果页 H2H 回填 (需乐鱼 token 有效)
```bash
python -c "import gq.event_db as e; print(e.backfill_h2h_from_result_page())"
```

## 系统架构
```
乐鱼 H5 (Playwright+Edge 接管 WS 会话)
  ├── C105 盘口帧(gzip) → gunzip → record_snapshot → odds_snapshots   ┐
  ├── C103 比分帧        → matches(score) + match_outcomes(赛果)       │ 写
  ├── C102 事件帧        → matches(status/minute)                      │ events.db
  └── HTTP 内容端点      → match_meta(前瞻/伤病) + h2h(赛果页H2H)      ┘ (gq.db SSoT)
        ↓ (一次性 backfill_all_from_gq)
   GQ.db (历史归档, 原文件不动)
```

## 待办 / 状态

1. ✅ **bridge_service 重启（已完成 2026-08-27）**：已手术刀重启(IR-10, `scripts/restart_bridge.py --wait-health`)，
   新进程 pid=30604(venv) 派生 Python312 worker pid=39140 持有 :9000，health 200。
   **已验证读 events.db**：`gq.db.DB_PATH=events.db`；`interface_doc` 仅 events.db 有(GQ.db 无此表)，
   bridge 端点正常出数(match_outcomes=9842 / matches=11507)。venv→Python312 父子是 IR-09 shim 常态, 非双写。
2. **H2H 回填**：token 过期期间 `backfill_h2h_from_result_page` 返 0，换 token 后跑补全。
3. 迁移脚本 `_migrate_events_to_gq.py` 已修正为 `backfill_all_from_gq(recreate=True)` (修复 GQ 表结构漂移回填),
   保留作回滚/重迁参考。

### ⚠️ 已知坑: GQ.db 表结构漂移 (2026-08-27 发现并修复)
GQ.db 经多次 ALTER 已漂移, 比 gq.db.init_db() 规范结构多出列: `matches.tag` / `match_outcomes.is_virtual` /
`match_analysis_cache` 多 20 个分析列。原 `backfill_all_from_gq()` 用 `INSERT...SELECT *`(按列位置),
遇列数不符整表静默失败(matches/match_outcomes/match_analysis_cache 曾因此全空)。
修复: backfill 改读 GQ 实际 DDL 重建 events.db 对应表(`recreate=True`), 再 SELECT * 完整拷贝。
建议后续把漂移列补回 gq.db.init_db() 以根除(属独立优化, 未做)。
