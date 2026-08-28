# 僵尸场强制落标回收报告 (2026-08-27 01:05)

## 背景
`matches` 表中 `status='live'` 且 `last_seen` 超 6h 无更新（feed 死亡但未翻转/未落 `match_outcomes`）的比赛，因 watcher 停摆，终场标签永久丢失 → retrain 无法纳入。

## 执行（用户批准）
脚本 `scripts/recover_zombie_finished.py`（含备份 + DRY_RUN + 幂等守卫）。
真相口径：`matches` 末次全场比分 = 最佳可得真相；`ht_score` 留 NULL（matches.ht 已被全场覆盖污染，不传播）；`source='forced_status_recovery'` 可追溯。

## 结果
| 项 | 数量 |
|---|---|
| 僵尸场（live & 6h+） | 675 |
| 有比分 → 插标签 + 翻 finished | **593** |
| 有比分且已有标签 → 仅翻 finished | 15 |
| 无比分 → 跳过（不伪造） | 67 |
| 插标行带 in-play 快照（可进 retrain） | **593 / 593** |
| 插标行 ht 留 NULL（防污染） | 593 / 593 |

- 备份：`data/GQ.db.bak_zombie_20260826_1705`
- `match_outcomes` 总量 9828（forced 593）；`matches.finished` 10263 → **10871**
- 剩余僵尸：**67**（无全场比分，无法定标，留待人工/外部源）

## 幂等
复跑 recover：插入 0 / 翻 0。复跑 ledger verify：无副作用。

## 连带闭环（关键）
`live_ou_decode_ledger.jsonl` 中悬置 3 轮的「索尔海岸 vs 黑牛队」本轮闭环：
- 其 match_outcomes 行亦为 `forced_status_recovery`，比分 **3-0**
- 盘口 OU 2.0 → 总 3 > 2.0 = **大球打出**；模型看小 → **判错**（model_correct=false）
- 与 08-26 23:08 证据结论（最后快照 2-0、matches 3-0、总球≥3）一致
- `reconcile=done` 且 `reconcile_agrees_with_live=true`；`next_retrain_will_ingest=true`（in-play 快照存在）
- 旧错误标签（抢跑 1-0）保留于 `disputed_prior_label` 供审计

## 已知未动 / 待办
- **67 场无比分僵尸**：无真相，未强制；建议外部源补标或确认 feed 死亡原因。
- **ht 污染（1749 场 ht==全场）**：预存问题，本次仅避免传播，未修复；影响半场类特征，需单独清理。
- **watcher 停摆根因**：未查；如不停，僵尸将持续累积，建议修 live→finished 状态机（IR-06）而非依赖本回收脚本。
