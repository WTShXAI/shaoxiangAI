# 回填自动化报告 (2026-08-27 01:18)

## 一、本期回填结果
- 命令 `backfill_scores_api.py --apply` (cwd=D:/Architecture)
- 结果: **inserted=0, filled=0, skipped_friendly=60, skipped_existing=504, errors=0**
- 说明: 01:16 首跑曾 `inserted=14`(00:24 后完场带比分的非友谊场), 本次为幂等重跑, 已转 `skipped_existing`。
- VALIDATION(修复后): `null_score_cnt=0`, `score_result_inconsistent=0`, `live_only_with_result=0` ✅
- 真 backlog: 仍 1085 场"有 mid 无比分"(须外部赛果源, 本自动化职责外)。
- 未触 collector/bridge; 幂等无副作用。

## 二、关键发现: 验证器误报 593 条"不一致"(已修复)
- 01:16 首跑 VALIDATION 报 `score_result_inconsistent=593` + `live_only_with_result=6` 的 WARN。
- **根因**: `run_validation` 一致性 SQL 仅认 `'home'/'draw'/'away'` 词表; 但 `match_outcomes` 中 593 行用 `'H'/'D'/'A'`(source=`forced_status_recovery`, 同一 `archived_at` 批量写入 = collector status-score 耦合残留)。
- **全表核验**: 任一词表下 `score↔result` 一致的真冲突 = **0 条**。数据实际干净, 593 为词表不匹配误报。
- **修复**: inconsistent 查询改为词表无关(接受 `H/D/A` 与 `home/draw/away`), 并补 `score_away IS NOT NULL`。重跑验证 0 误报。
- **遗留(待拍板, 未擅自改数据)**: 593 行 `H/D/A` 与全表 `home/draw/away` 词表不统一。若有下游消费者假设单一词表, 建议归一化(593 行 `UPDATE result→home/draw/away`)。

## 三、前端"动态决策系统跳动"修复(用户反馈, 顺带修)
- **根因**: `MomentumTraderCard` 每次轮询 `fetchMomentum` 时 `setMomentumLoading(true)` → 卡片塌陷为"聚合多路信号中…"桩, 数据回来再展开。live 比赛 `last_seen` 每轮询都变 → `selected` 每轮询换新对象 → 每次轮询都塌陷/展开 = 跳动, 看不到内容。
- **修复** (`frontend/src/pages/LiveGoalProbe/index.tsx` 的 `fetchMomentum`): 仅"首次加载/切换比赛"显示加载桩; 同场刷新保留旧裁决内容原地更新, 不再塌陷。
- 已 `tsc --noEmit` 验证该文件无新类型错误。需 Vite HMR 或重新 build 生效。
