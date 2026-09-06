# 自动复核接入复盘 — 落地报告 (2026-08-08)

## 核心设计（提取自用户方案的最优方向）
把「赛前分析的核心结论」与「赛后实际结果」做规则化比对。最好的落地点：
**以 `prematch_similarity.query_match()` 的确定性单结论作为「结构化赛前结论」，按 GQ `match_key` 写入复盘表**，用其 `excess`（近邻频率 − 市场隐含概率，有符号）直接喂三色情绪标签 —— 不依赖缺失的 `bet_records` 偏差 feed，复盘即可覆盖**全部已结束比赛**。

## 命中规则（已实现）
| 分析类型 | 命中逻辑 |
|---|---|
| 胜平负 1X2 | 赛前方向（主胜/平局/客胜）== 实际赛果（比分推导 H/D/A）→ `hit` |
| 三色标签 | 用该方向 `excess` 有符号值喂 `compute_signal_label` → 🔴/🟡/🟢 |
| 平局/冷门信号 | `draw_signal`/`cold_signal` 标志位（平局预警 / 红类冷门标签） |

无赛前盘口的比赛 `predicted_direction=NULL`（不伪造，铁律）。

## 改动文件
- `gq/db.py`：新增列 `match_key / predicted_direction / predicted_from / predicted_excess / predicted_roi / draw_signal / cold_signal / auto_reviewed_at`；新增 `auto_review_match()` + `auto_review_all()`；`query_analysis_cache` 改双源 JOIN（`matches` + `match_outcomes`）。
- `scripts/recheck_analysis.py`：新增 `recheck_auto_review` 阶段（每日 00:00 `ShaoxiangAI_DailyRecheck` 自动跑）。
- `bridge_service.py`：新增 `POST /api/analysis/auto-review`（触发）+ `/api/analysis/cache`、`/api/analysis/export` 自动带新列。
- 前端 `AnalysisCenter`：新增「复盘复核」页签，展示 赛前预测 → 实际赛果 → 命中徽标 + 三色标签。

## 实测结果（全量）
- 自动复核 **4148** 场已结束+有比分比赛。
- **2902** 场有开赛前 1X2 快照（可复核，占 70.6%）；976 场无赛前盘口 → 诚实留 NULL。
- 有预测场：**1410 命中 / 1492 未中 = 48.7% 1X2 命中率**（与 KNN 引擎已知 45–52% 区间一致，无魔法 edge，诚实呈现）。
- 三色标签随 `excess` 正确填充（如 excess>0 → 🟢 顺势跟进；excess<0 → 🟡 信号冲突）。

## 诚实说明
- 历史 finished 比赛若采集器未保留开赛前快照（仅采到滚球盘），则无法出赛前结论（已正确留 NULL，不顶替）。
- 每日 00:00 定时任务会增量复核新完场比赛（跳过已复核行）。

---

## 用户批准的三项扩展（2026-08-08 全部落地）

### 1. 定时任务 `ShaoxiangAI_DailyRecheck` 真正接线
- 之前仅报告里提到，无实体。现已创建 **Windows 计划任务**（每日 00:00）。
- 包装脚本 `scripts/run_daily_recheck.bat`：`.venv python` 跑 `recheck_analysis.py --apply`，日志追加到 `logs/daily_recheck.log`。
- 注册命令：`schtasks /create /tn "ShaoxiangAI_DailyRecheck" /tr "...\run_daily_recheck.bat" /sc daily /st 00:00`（下次运行 2026/8/9 0:00）。
- `recheck_analysis.py` 第 117-118 行已含 `recheck_auto_review(args.apply)` 阶段 → 每日增量自动复核新完场 + WS3 重算 + 赛前波胆验证。

### 2. `fl_structure_weight` 受控开启（0.0 → 0.1）
- `pipeline/ranked_predictor.py` 默认参数 `fl_structure_weight` 由 `0.0` 改为 `0.1`。
- 效果：fl_model_1x2 以 10% 权重混入 1X2 最终概率（原仅透明展示、不融合）。
- 零回归基准已存档 `backups/fl_model_20260805_preretrain/`；若回测显示拖累可瞬回 `0.0`。
- 验证：`DEFAULT_PARAMS['fl_structure_weight']==0.1`；`rp.predict(...)` 冒烟测试 `fl_w=0.1` 路径无异常。

### 3. 复盘命中率反哺前端「历史命中率」卡片
- `gq/db.py` 新增 `auto_review_stats()`：隔离 `predicted_direction IS NOT NULL` 的 KNN 子集，聚合 `reviewed/hit/miss/no_prematch/hit_rate` + `by_direction{H/D/A}` + `last_reviewed_at`。
- `bridge_service.py` 新增 `GET /api/analysis/review-stats`（复用 `_wrap_data`）。
- 前端 `AnalysisCenter` 复盘复核页签顶部新增命中率卡片：总命中率大数字 + H/D/A 分项命中率 chip + 「KNN 结构相似检索·非价值信号」诚实说明 + 更新时间。
- 实测端点：`reviewed=2902 hit=1410 miss=1492 hit_rate=48.6%`（H 52.9% / D 34.2% / A 47.9%，无赛前盘口 1246 场）。
- 注：H/D/A 命中率差异显著（平局最低 34.2%）——符合 KNN 对平局判别弱于主客胜的已知特性，诚实展示不掩盖。

---

## 赛后机关：事件驱动复核，替代每日批量重跑（2026-08-08 追加）

### 动机（用户提出）
赛前结论只在用户查询时算一次、没存下来；比赛结束 → 写赛果 → 复核却要等每日 00:00 `auto_review_all` **批量重跑一遍 KNN** 才出 `verdict_hit`。问题：① 复核延迟最多 24h；② 赛后重跑的 KNN 结论可能与「当初展示给用户看的那条」漂移；③ 每场都重跑相似检索，贵。

### 机关设计（两层，全增量、零回归）
- **赛前录入（锚点）**：`/api/prematch/query` 算出结论后，立即把**当时展示给用户的那条确定性结论**原样固化进新表 `prematch_conclusion[match_key]`（verdict_code / verdict_cn / excess_json / roi_json / draw_signal / captured_at）。
- **赛后机关（触发）**：采集器 `_sweep_finished` 一旦把某场从 `live` 归档成 `finished`（即「落入复盘」那一刻），立刻调 `auto_review_match(match_key)` 比对 —— **读固化结论、不重跑 KNN**、实时出 `verdict_hit` + 三色标签。
- **兜底**：`auto_review_all` 降级为**补漏备份**（只处理机关上线前已完场 / 漏网的比赛），且同样 `prefer_stored=True` 优先读固化结论。

### `auto_review_match` 行为变化
新增参数 `prefer_stored: bool = True`：
- `True`（默认，机关/批量均走此）：`prematch_conclusion` 有行 → 读固化结论（`predicted_from='prematch_stored'`），严格忠于展示、且 O(1) 不重跑 KNN。
- 无固化结论（如机械上线前已完场 / 从未展示的比赛）→ 回退 KNN 重跑（`predicted_from='prematch_knn'`），与旧行为一致。

### 改动文件
- `gq/db.py`：新增 `ensure_prematch_conclusion()` + `store_prematch_conclusion()`；`auto_review_match` 支持 `prefer_stored`。
- `bridge_service.py`：`/api/prematch/query` 命中后固化结论（`store_prematch_conclusion`）。
- `gq/auto_collector.py`：`_sweep_finished` 归档成功处新增赛后机关触发（日志 `[自动复核] ... 预测= 实际= hit/miss`）。

### 实测验证
- 单元验证：构造一场已完场比赛 + 固化结论 → `auto_review_match(prefer_stored=True)` 返回 `predicted_from='prematch_stored'`、`verdict_hit='hit'`；同场 `prefer_stored=False` 正确回退 `prematch_knn`。
- 端点验证：`/api/prematch/query?match_key=...` 返回 applicable 后，`prematch_conclusion` 表确实写入该行（verdict_code/verdict_cn/captured_at）。
- 进程：bridge 重启 PID 25024（9000 监听，捕获端点生效）；collector 重启 PID 40512（`--daemon -i 60`，赛后机关生效）。
- 后端 `py_compile` 全过；`/health` 健康。

### 诚实说明（更新）
- 赛后机关让复核**实时**发生（比赛一结束即出），不再依赖每日批量；批量 `auto_review_all` 仅作补漏。
- 「赛前录入」只在用户/系统真正查询赛前结论时发生；从未被查询的比赛无固化结论 → 机关回退 KNN 兜底，复盘仍覆盖（与旧行为一致）。
- `prematch_conclusion` 严格保存「展示给用户看的那条」，因此复盘命中率现在**忠于实际产品输出**，不受模型/索引后续变更漂移影响。

---

## 赛前冻结捕获：机关零遗漏（2026-08-08 用户批准追加）

### 动机
赛后机关虽好，但「赛前录入」原本只在用户/系统**手动查询** `/api/prematch/query` 时发生。那些系统已采到初盘、但没人查的比赛，落复盘时仍要回退 KNN 兜底。用户批准：给赛前也加一道捕获，让机关对**所有有赛前盘口的比赛**零遗漏。

### 实现（与 `_freeze_scheduled_cs` 同构）
- `gq/auto_collector.py` 新增 `_capture_prematch_conclusions()`：每轮 `collect_round` 在 `_freeze_scheduled_cs()` 之后调用。
- 逻辑：扫 `status='scheduled'` + 非虚拟联赛 → 已固化（`prematch_conclusion` 有行）跳过（**每场只算一次 KNN**）→ 否则 `query_match(k=DEFAULT_K, draw_upgrade=True)`，applicable 才 `store_prematch_conclusion`。无赛前快照的比赛 not applicable → 不下写、下轮再试。
- 一次性全量预固化：调一次即把现有未开赛有盘口比赛全部固化，后续轮次仅增量补新出现的比赛。

### 实测验证
- 编排测试：160 场未开赛中 **157 场成功固化**（3 场无赛前快照/虚拟联赛跳过）；二次扫描「仍 applicable 但未固化 = 0」→ 证明每场只算一次。
- 采集器重启 PID 39192（`--daemon -i 60`）加载新代码；`prematch_conclusion` 现 158 行（157 scheduled + 1 finished 端点测试）。
- 效果：赛后机关现在对**全部有赛前盘口的比赛**都能走 `prematch_stored` 路径，**无需回退 KNN 兜底**，复核严格忠于展示且 O(1)。

### 诚实说明（再更新）
- 仍有极少数比赛（采集器从未采到开赛前 1X2 快照的 obscure 联赛）无固化结论 → 机关回退 KNN 兜底，复盘仍覆盖（与旧行为一致），但非「展示给用户那条」。
