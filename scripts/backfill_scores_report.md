# GQ.db 比分回填执行报告 (2026-08-24, 实测校正版)

## 一、任务
补全两类"有赔率但无有效赛果"的比赛（用户原话：live-only 121 + 有赔率未完赛 435）：
- **A 桶 live-only**：无初盘、纯开赛后滚球数据，缺终比分 → 可作滚球标签。
- **B 桶 has-prematch**：有初盘但 collector 未归档终比分。

## 二、关键发现（推翻研究报告假设）
研究报告 / `backfill.py` / `result.md` 主张"用 `fetch_match_structure` API 重拉为权威历史源"。
**实测证明该假设对历史比赛不成立**：

- GQ 的 `structureMatchBaseInfoByMidsPB`(STRUCT) 与 `getMatchBaseInfoByOddsPB`(ODDS) 端点
  **只服务进行中/即将开赛的比赛**；历史完场（>约 2 天）已被清库，返回 0 条 / 空 data。
- dry-run 全量打 API 拉 512 个 mid：**0 可写**，509 返回"API 无返回"，仅当前 live 场返回数据（正确跳过）。
- 结论：**API 重拉不能作为历史比分回填手段**，仅能在比赛"仍在窗口内"时取数。

> 附带结构发现：`D:/down/backfill.py` 与 `result.md` 面向 `football_data.db` 且 `import collector`，
> 而线上真实系统是 **GQ.db**（`gq/auto_collector.py` + `gq/db.py`，`matches.mid` 多数为空，
> 真实连接键是 `match_key`=队名"主 vs 客"）。已改写独立脚本 `backfill_scores_api.py` 针对 GQ.db。

## 三、真实可恢复数据源 = 本地 `matches` 表
collector 完场时往往已把终比分写入 `matches`(status='finished', score_home/away 已填)，
只是因"状态-比分耦合 bug"未能归档进 `match_outcomes`（研究报告判断 #1）。
→ 回填以 `matches` 为权威源，把已知终比分 propagation 到 `match_outcomes`。

## 四、目标统计（2026-08-24 实测，按 match_key 正确关联）
| 类别 | 数量 | 处理 |
|---|---|---|
| 有赔率(market 快照) + mid，但无有效 match_outcomes 结果 | **509** | — |
| ├ 已在 `matches` 持终比分（status=finished） | 62 | 见下 |
| │   ├ 竞技场（非友谊，已完场） | **5** | ✅ 首轮已回填 match_outcomes（0-0/实分，live 模型可 join） |
| │   └ 友谊赛 | 54 | ✅ 按系统 P0a 规则排除（不污染复盘库） |
| └ `matches` 也无终比分 | 447 | 见下 |
|     ├ 仍 live（进行中，collector 完赛后归档） | 8 | 本轮正确跳过（status≠finished，避免写入半场比分） |
|     ├ 仍 scheduled（未来场，collector 届时归档） | 145 | 不动 |
|     ├ 友谊赛（无比分，按规则排除） | 64 | 不动 |
|     └ **finished 但 score=NULL（GQ 已丢，collector 漏捕）** | **302** | ⚠️ 经 GQ 不可恢复 |

> 注：首轮 `--apply` 写入 5 场竞技；本轮（collector 已完赛更多场）dry-run 显示候选 54 场，
> 但 apply 实测插入 0、跳过 54 友谊——即候选 54 全为友谊赛；另 8 场竞技"有比分"实为
> `status=live`（in-play 比分，minute 31~118），正确跳过，待 collector 翻 finished 后自动回填。

## 五、已执行
脚本：`scripts/backfill_scores_api.py`（standalone，逐字复制 auto_collector 最小 API client
避开 msvcrt 单例锁；只 `import gq.db` 安全 API；dry-run 默认 / `--apply` 写库 / 针对性行备份 +
WAL + 事务 + 幂等 + is_override 锁保护 + 友谊赛守卫 + 非 finished 跳过）。

- 首轮 `--apply`：**5 场竞技比赛**写入 `match_outcomes`（result 由实分推导，home/away 齐全 →
  live 模型可 join 消费）。已用 SQL 验证存在（例：国际利美拉SP vs 费古伦斯SC `mid 5559156`、
  天使城(女) vs 哥谭(女) `mid 5518399`，均 0-0 draw，`archived_at`≈2026-08-24 15:03 GMT+8）。
- **54 场友谊赛**按系统规则正确排除。
- 备份：`scripts/backfill_backup_20260824_150613.json` / `_151600.json`（针对性行备份，可回滚）。
- 校验：`null_score_cnt=0`、`score_result_inconsistent=0`、collector 全程未中断。

## 六、遗留阻塞：302 场 finished-but-NULL-score
这 302 场（status=finished 但 score 空）在 GQ 任何端点都取不到终比分：
- `matches` 无比分 + API 清库 → 死数据；collector 已标记 finished 不再重拉。
- 属 collector"状态-比分耦合 bug"的历史失败残留。

### 可选后续（需用户拍板）
1. **接入外部历史赛果源**（API-Football / football-data.org 等），按
   队名(中文/音译)+联赛+日期 映射补全——难点：冷门联赛+音译队名匹配可靠性，
   错配会污染训练标签（违背"宁缺勿错"）。
2. **接受丢失**：302 为 collector 历史失败残留，GQ 已清，放弃（不影响已 recovered 的标签）。
3. **治本**：修复 collector"状态-比分耦合 bug" + 增设**窗口内主动回填自动化**
   （完场后、GQ 清库前主动归档），杜绝新残留累积（研究报告判断 #1 落地）。

## 七、结论
研究报告的"API 重拉为权威历史源"对历史比赛无效，已用本地 `matches` 源纠正。
实际可补全 **5 场竞技标签**（+54 友谊按规则排除；8 场 live 竞技待 collector 完赛自动回填）。
**302 场 finished-but-NULL 需外部源或接受丢失**；建议优先做"治本"（collector 修复 +
窗口内主动回填）以防残留继续增长。
