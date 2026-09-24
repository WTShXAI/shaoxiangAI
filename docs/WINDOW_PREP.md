# WINDOW 待办规格书（维护窗口执行，本轮未落地）

> 建立于 2026-09-25，owner 据 P-SNAPSHOT-data Phase1 落地的余下缺口整理。
> **纪律红线**：本文件所有项均须独立停机/回归窗口执行，禁止在稳定运营期盲改生产。
> 执行前须老板拍板窗口时间 + 回归通过。

---

## 1. P-SNAPSHOT-data 剩余缺口（采集层扩展）

Phase1（2026-09-25 已落地）已把 `match_meta` 中**已采集**的 3 个赛前字段冻结入快照：
`preview`(4790/5029 覆盖) / `news`(781) / `injuries_home`(1223)。

**仍未可得字段（需采集层扩展，非快照表问题）：**

| 字段 | 当前状态 | 补采集方案 | 风险 |
|---|---|---|---|
| `lineup`（主+客首发） | match_meta 全 0 行；GQ 阵容端点 `getMatchLineupListPB` 标注"暂未接入" | 在 `gq/content_collector.py` 激活阵容端点解析 → 写 `match_meta.lineup_home/away`（表已存在列） | 中（端点返回 0408006=未公布时须优雅降级，不写脏值） |
| `injuries_away` | match_meta 0 行（仅 home 侧采集） | 扩展 `content_collector` 第三方情报解析覆盖客队 | 低 |
| `weather` | 全库无源 | 需新数据源（赛事 API/气象）或 GQ 情报衍生；**当前无字段** | 高（缺源） |
| `ref`（裁判） | 全库无源 | 同上 | 高（缺源） |
| `schedule_density`（赛程密度） | 全库无源 | 由 `matches.kickoff` + 同队近期赛程派生；可离线计算，不需新采集 | 低（派生即可） |

**落地顺序建议（窗口内）：**
1. 先补 `lineup`（最高价值反偷看字段，且列已就位，仅差采集器激活）→ 改写 `freeze()` 取 `match_meta.lineup_home/away`。
2. 补 `injuries_away`（同解析器扩展）。
3. `schedule_density` 走离线派生（按 team + 近 7 日场次计数），可在 freeze 时 cheap 计算，不依赖新采集。
4. `weather` / `ref` 标记 BLOCKED（缺数据源），待外部源接入后再排期。

**回滚**：采集器改动须保留原 preview/news/injuries_home 路径；lineup 解析失败不得污染既有字段。

---

## 2. P-AUDIT（odds_changes 审计链补全）

蓝图 §1 要求 tick 可追溯（source/时间/延迟/**原 JSON 哈希**/清洗 ID）。现状：
`odds_changes`(44.6M 行) 有 `captured_at`/`score_at`/`minute_at`，**缺 `raw_json_hash` + `cleaned_id`**。

**关键约束**：采集器当前只落**解析后字段**（from_odds/to_odds/…），**不保留原始 JSON**。
→ `raw_json_hash` 与 `cleaned_id` **只能前向采集**（forward-only），历史 44.6M 行无法回填。须如实标注"审计链自 <窗口日期> 起生效"。

**前向迁移规格（窗口内非破坏）：**
```sql
-- 1) 加列（SQLite ALTER，瞬时，加 NOT NULL DEFAULT '' 会触发全表重写→改用可空）
ALTER TABLE odds_changes ADD COLUMN raw_json_hash TEXT;   -- 采集时 sha256 原始 JSON
ALTER TABLE odds_changes ADD COLUMN cleaned_id   TEXT;    -- 清洗流水线批次/版本标识
```
- 采集器（`gq/ws_collector.py` 或 odds 落库处）在写入每行前计算 `raw_json_hash=sha256(raw_json_bytes)`，`cleaned_id=f"v{pipeline_ver}-{batch}"`。
- 历史行 `raw_json_hash/cleaned_id = NULL`，看板/审计查询须 `WHERE raw_json_hash IS NOT NULL` 区分。
- 索引：`CREATE INDEX IF NOT EXISTS ix_oc_hash ON odds_changes(raw_json_hash)`（窗口内建，离线可接受）。

**回滚**：新增列可 `ALTER TABLE DROP COLUMN`（SQLite 3.35+ 支持）；采集器改动须可配置开关（默认关闭→观测 1 日→开启）。

---

## 3. P-ENG（消息总线/特征存储，低紧急）

当前 bridge/采集/守护进程间靠 DB 轮询 + 文件。重架构为消息总线（如本地 ZeroMQ/Redis）收益低、风险中。
**决策**：维持现状，标注 WINDOW-LATER，不排入近期窗口（避免为架构而架构，符合"约束成累赘则无效率"原则）。

---

## 4. P-MODEL（Dixon-Coles/Elo/贝叶斯 接入对照）

蓝图 §5 要求 DC/Elo/贝叶斯接入 walkforward 门禁做对照。现役含 K线LightGBM/HT锚/static。
**决策**：不急于接入——现役模型六道关全 NO EDGE/INCONCLUSIVE，先发模型无正 edge，
新增对照模型的边际价值低且占用 OOS 样本。排期于"样本量突破 2500 且出现 G2/G6 苗头"之后。

---

## 5. B 类窗口项（见 LEGACY_ISSUES.md §B）

B1 bridge 端点瘦身 / B2 前端废弃类型 / B3 ShaoxiangVite 任务 / B5 M3/M4 复验 / B6 events.db bak 表 VACUUM / B7 零引用 db 清理。
均须独立窗口 + 回归，按"风险/价值"从 B7(低)→B1(中) 渐进。B6 涉及 37GB 库停机，排最后。

---

## 窗口执行检查单（每次窗口通用）

- [ ] 备份源库（events.db 整库 copy 到 archive/ 带时间戳）
- [ ] 变更在 `scripts/` 同目录提供 `--dry-run` / 开关默认 off
- [ ] 回归：pytest tests/ 全绿 + `python -m verification report` 三态不变
- [ ] 采集器改动后观察 24h 无脏值/无断流再默认开启
- [ ] 窗口结束写 `docs/WINDOW_PREP.md` 对应项 ✅ + 当日 memory 日志
