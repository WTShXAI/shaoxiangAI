# AGENTS.md

## IR-32 跨庄共识永久禁令（2026-09-19，任何操作前必读）

跨庄共识/跨庄edge/投注占比（cross_book_edge、multibook_consensus、leyu_value_signal、
bet_split_source、compute_value_layer、bet_core）**永久禁止**进入生产判定、API、前端、
测试与实验；仅限自有训练/回测脚本离线后台对照。已随量化系统整体归档
archive/quant_system_20260919/；tests/test_no_crossbook.py 为自动守卫，见到相关字样
进入生产 import 图 = 最高级事故。

## 量化系统已删除（2026-09-19）

compute_value_layer / deep_report / bet_core / execution(早已不存在) / database.py(不存在) /
bookmaker_sim / quant_trading.db / strategy_log / beat_under_log 全部删除或归档。
bridge 的 value_layer 已改为模型-市场偏差-only。**修复 bug 时不得从 archive 引回任何量化逻辑。**

## 智能化数据底座（2026-09-19）

- **统一数据库**：训练面收敛到 events.db（historical_matches 31.2万 + odds_features 32.7万 +
  rb_matches 31.9万 已迁入；迁移脚本 scripts/migrate_unify_db.py）；KNN 生产读方已重指。
- **叙事特征**：gq/match_narrative.py → match_narrative 表 15848 场（平局回合/追平/反超/
  进球后干旱/热门失分，verified 治理假0-0：7267 verified + 369 可信改判 + 301 存疑）。
  每日 recheck_analysis 自动增量。训练一律 verified=1。
- ~~半场OU读数未条件化 bug~~ **已修复 (2026-09-19)**：采集器冻结条件化 + 历史修 210 行
  (时间轴验证)；全场口径 LL=0.5635/ECE=0.0529, 确定态 207 场 100% 命中。注意结算口径=全场总球(非下半场)；
- 采集器重启后 HT 冻结新逻辑才生效 (当前进程仍为旧读数, 待例行重启)。

## 预测产品层 (2026-09-18 改造: 博彩量化 → 预测系统)

投注决策外围 42 文件已归档 `archive/betting_decisions/` (零生产引用, 审计记录见
`docs/prediction_refactor_checklist.md`)。**系统主指标 = LogLoss / Brier / 校准(ECE), 不再是 ROI**。
新概率输出只准复用已回测模型 (铁律), 市场赔率仅作对照, 输出只解释不喊单。

```bash
PY=.venv/Scripts/python.exe
$PY -m pipeline.predict_export --date 2026-09-20      # 每日预测 → events.db daily_predictions (开赛冻结)
$PY -m pipeline.predict_export --backfill-days 45     # 历史回填 (回测口径, 只补空行; 已回填45天9437场)
$PY scripts/eval_prediction_calibration.py            # 校准评估 → reports/prediction_calibration_report.{json,md}
```

- API: `GET /api/predictions?date=` · `GET /api/predictions/calibration`; 前端「预测中心」页 (/predictions)
- 结算原语 SSoT: `pipeline/settle.py` (纯赛果判定); 投注 ROI 报表已归档, 勿再新建
- 派生市场 (O2.5/BTTS/期望进球) 用 goal_scale=1.0 诚实锚 (A/B 实证见清单§四; score_model 默认 1.2 仅波胆 top3 口径)
- **训练负结论存档** (清单§五/附, 2026-09-18): LGB/stacking/移动特征/isotonic(派生市场)/联赛收缩/KNN+K线stacking
  全部不敌现役源, 勿重复; **两例外已采纳**: K线集成升 70 天全语料 (LL 1.0264), HT锚 OU 全面胜现任
  (76.8% vs 50.2%, HT-OU 读数应以 ht_model_verdict 为准); 半场OU读数疑似上游 bug (见 reports/ht_ou_isotonic_eval.json)
- **假0-0评估口径修复 (2026-09-19, 清单§五附二)**: 断流场定格 0-0 曾污染全部结算面; 结算一律走
  `pipeline/settle.py::credible_1x2` 守卫。修复后 K线 vs 市场 ΔLL=+0.013 — "K线优于市场"为污染伪信号,
  candles 升主赛前锚点的 ≥300 场对照决策必须用守卫后台账。
- **devig SSoT**: pipeline/odds_math.py (devig_n/devig2/devig3/devig_power); 新代码禁再写本地去水
- 工程规范: 测试 `.venv/Scripts/python -m pytest tests/ -q --timeout=120` (38 用例, 2026-09-19 量化归档后); 规范化+待办见
  docs/prediction_refactor_checklist.md 与 docs/pending_cleanup_backlog.md; ARCHITECTURE.md 已重写为预测系统版
- **自主监测优化** (2026-09-19 用户指令开启): 每小时自动化 automation-025892d6 运行
  scripts/autonomous_monitor.py --cycle (bridge自愈/采集活性/预测补算+每小时refresh未开赛行应用K线判定/叙事增量/校准漂移/重训门控);
  状态 reports/monitor_status.json, 历史 monitor_history.jsonl, 日志 logs/autonomous_monitor.log;
  重训建议仅提示不自动执行 (walkforward 门禁保留)
- 前端已去喊单化 (价值层/决策/操盘手/天眼镜 → 概率偏差解释表达); 二期剩余见清单§五:
  `_live_predict` 摘除 value_layer、bookmaker_sim 解耦、bet_core 归档

## 电子盘口监测 (2026-09-19, 用户指令"当游戏打"落地)

- `gq/efootball_probe.py` 常驻探针 (60s/轮, euid=3020190 电子足球分区): EAFC 模拟联赛
  赛事+赔率原始 payload 全量落 **独立库 data/efootball.db** (ef_matches/ef_odds_raw),
  零接触 events.db; 主采集器当年用 _is_simulated_league 故意排除模拟域, 探针反其道采集。
- 与主采集器共用 gq/.env token (热加载继承); 日志 gq/efootball_daemon.log。
- 用途: 引擎指纹诊断 (模拟域 vs 真实域 平局率/比分分布/市场LL 对比) + 深度学习语料;
  任何输出仍走 walkforward 门禁 + IR-32。
- 相关实证 (2026-09-19): 真实域开盘≈收盘 (ΔLL -0.0039, 赔率为生成型非撮合型);
  overround 模板化 (中位 1.064); 详见会话记录与 reports/candles_70d_AB_*.json 同期产物。
- **引擎指纹首跑 (2026-09-20, n=354)**: 模拟域早盘市场 LL 1.0121 vs 真实域同口径 0.9636
  (Δ+0.0485, ~1.5σ 提示性非结论性); 引擎口味: 平局 21% vs 真实 24%, 场均球 2.93 vs 2.76。
  语料继续积累 (scripts/efootball_fingerprint.py 手动重跑), 数据集构建器
  scripts/efootball_build_dataset.py 供深度学习期; 任何模型输出仍走 walkforward 门禁。
- **模拟域结论 (2026-09-20 晚, n=2584)**: 六路探索全部完成 — 域级信号收缩至噪声(Δ+0.032≈0.9σ)、
  朴素模型败(+0.056)、联赛 one-hot 伤模型、联赛静态修正被时间切否决(+0.020)、
  GRU 多市场序列模型复现生成器定价至持平(LL 0.6056 vs 0.6072)、价格方向 85% 可预测
  (严格整场切分 0.8517, 早段 0.844 — 引擎价格过程=确定性衰减物理)。**判定: 赛果层无超额
  (攻略不存在), 价格层可预测(能力非edge)**。证据: reports/efootball_{fingerprint,sequence_eval,price_behavior}.json。
  下一决策点: 语料 5000 场复核一次价格行为稳定性; 生产模型不变。

## 双 AI 协作黑板 (2026-09-13 生产接入, 必读)

哨响(观察 AI)与执行 AI 通过共享黑板协作: `data/collab_journal.jsonl`。
**任何会话开始时**: 若黑板有 open 条目 → 先读 `sandbox/collab/AI_PATROL_BRIEF.md`
章程 → 按优先级认领处理 → 写回结果。

```bash
PY=.venv/Scripts/python.exe
$PY sandbox/collab/collab.py --journal data/collab_journal.jsonl listx --status open   # 待办
$PY sandbox/collab/collab.py --journal data/collab_journal.jsonl claim --task <ID> --by executor_zcode
$PY sandbox/collab/collab.py --journal data/collab_journal.jsonl fix   --task <ID> --note "..."
$PY sandbox/collab/collab.py --journal data/collab_journal.jsonl validate --task <ID> --ok|--reject --note "..."
$PY sandbox/collab/collab.py --journal data/collab_journal.jsonl verify    # 不变量校验
```

铁律(详见章程): 先复现→最小修复→必验证(不过则如实 reject); 禁止覆盖
is_override / git push / 无回测的模型改动 / 删生产数据。
前端"协作面板"Tab 可视化全部条目; ⚡一键检测上报可将页面异常直写黑板。

## K线集成赛前模型 (2026-09-15 采纳, 对照运行中)

Kronos 移植: 赔率K线化(pipeline/odds_candles.py) + LightGBM/transformer 双模型概率平均。
4轮walkforward验证过线(平均+3.15pp vs 旧static+traj)后采纳。
- 生产入口: `pipeline/odds_candles_predict.py` → `predict_match(con, match_key)`
- 收集器每轮对临场≤2h的scheduled场刷新判定 → `prematch_candles_verdict` 表 (开赛定格)
- 对照台账: `scripts/settle_candles_vs_knn.py` (vs KNN prematch_conclusion 逐场比对)
- 注意: 赛前任务严格剔除滚球tick(captured_at≤kickoff); 与混入滚球数据的旧85.7%口径不可比
- 待对照台账积累≥300场后, 决定是否升级为主赛前锚点 (在那之前 KNN 结论仍是展示主口径)
