# 改造清单: 博彩量化系统 → 预测系统 (2026-09-18 执行)

> 目标: 把"投注决策层"摘掉, 把"概率建模层"留下并做校准。
> 数据 → 特征 → 模型 → **校准概率 → 预测产品/看板** (原来: … → 找价值 → 仓位/下注)。
> 主指标从 ROI / yield / CLV 换成 **LogLoss / Brier / 校准误差(ECE·斜率) / TOP1 准确率**。
> 执行铁律遵守 AGENTS.md: 新概率输出只准复用已回测模型, **不做无回测的混采/新模型**。

---

## 一、保留 (概率建模层, 原样复用)

| 模块 | 角色 |
|---|---|
| `gq/` 采集器全家 + `data/events.db` | 数据真相源 (四类冻结快照体系) |
| `pipeline/score_model.py` | OIP 赔率隐含 Poisson — 比分矩阵/λ SSoT (预测派生市场的基座) |
| `pipeline/odds_candles_predict.py` + `data/models/candles_ensemble/` | K线集成 1X2 (4轮walkforward采纳) |
| `pipeline/ht_anchor_predict.py` | HT锚 (1X2 + OU 方向概率) |
| `pipeline/prematch_similarity.py` (KNN) | 赛前相似度结论 (历史主口径) |
| `pipeline/cs_db_match.py` | 波胆 DB 检索分布 |
| `analysis/live_goal_probe.py` | 滚球概率仪 (生产耦合深, 原样保留) |
| `pipeline/calibration.py` | 可靠性图/Brier/LogLoss/ECE 工具 (依赖已改指 settle) |
| `pipeline/model_catalog.py` (M1–M7) | 模型治理目录 |

## 二、删除/归档 (投注决策外围, 42 文件 → `archive/betting_decisions/`)

**判据**: 搬移前逐模块 grep 审计, 只归档**零生产 import 边**的文件 (只被命令行手动运行、
产出 reports/ 或独立台账、单向依赖生产 SSoT)。

- **EV/价值验证族**: `scripts/p0_2_softline_roi_curve.py`, `p0_3_value_layer_ev_curve.py`,
  `p0_9_model_ev.py`, `p0_9b_independent_ev.py`, `test_p0_3_divergence_gate.py`, `test_softline_decision_closure.py`
- **CLV 跟踪族**: `scripts/p0_clv_probe.py`, `p0_clv_clean.py`, `validate_drift_clv.py`, `validate_drift_clv_oos.py`
- **Paper 纸盘族**: `scripts/p0_paper_monitor.py`, `paper_track_{poisson_ou,ou_fusion,ou_lowline}_20260831.py`,
  `analyze_poisson_ou_decay_20260902.py`, `p0_crossmarket_coord_papertrack.py`, `open_eye_forward_monitor.py`,
  `prospective_score_eval.py`, `build_paper_report.py`, `autopilot.py`, `analysis/h1_paper_trading.py`
- **下注台账/结算/ROI 族**: `pipeline/roi_report.py`, `pipeline/cs_ev_decision.py`,
  `scripts/backfill_bet_records.py`, `bet_record_sync.py`, `strategy_ledger.py`, `daily_settlement.py`,
  `roi_report.py`, `settlement_roi.py`, `evaluate_user_bets.py`, `ingest_user_bets.py`, `arb_roi_sim.py`,
  `analysis/bet_variance_sim{,2}.py`, `parse_bets.py`, `extract_bets.js`, `user_bet_analysis_20260813.py`
- **冷门买彩研究族**: `analysis/cold_door_full_analysis.py`, `cold_door_cs_model.py`, `cs_value_model.py`,
  `recalibrate_cs00.py`, `verify_cs_ev.py`
- **CI 决策闸门测试**: `tests/test_p0_3_divergence_gate.py`, `tests/test_softline_decision_closure.py`
  (`.github/workflows/ci.yml` 对应两步已同步移除)
- 数据表未删: `bet_records` / `submarket_bets` / `strategy_log` 等仍留库中 (禁删生产数据)。

**审计后特意保留** (原计划归档, 发现生产耦合):
`pipeline/prediction_ledger.py` (live_goal_probe 运行时写入),
`analysis/h1_fav_undervalue_detector.py` (reverse_odds_engine 挂载),
`analysis/cold_door_model.py` (bridge 启动加载), `bookmaker_sim/` (data_collector/aore_pipeline 引用)。

## 三、重构 (本轮已执行 ✅)

| 项 | 产出 |
|---|---|
| 结算原语与投注 PnL 解耦 | `pipeline/settle.py` (纯赛果判定: parse_score / result_1x2 / settle_ou), `pipeline/calibration.py` 改指向它 |
| **预测产品层** | `pipeline/predict_export.py` — 单场/按日生成概率输出 (1X2 + 期望进球 + O2.5 + BTTS + 总进球分布 + top比分 + 市场对照 + 偏差说明), 落 `events.db daily_predictions` (开赛冻结); 模型源显式标注 `candles_ensemble` / `market_baseline` |
| **评估换轨** | `scripts/eval_prediction_calibration.py` — 六路判定源 (K线/KNN/半场/HT锚/预测表/市场大样本) 的 LogLoss·Brier·ECE·斜率, K线 vs 市场同场集对照; 报告落 `reports/prediction_calibration_report.{json,md}` |
| API (additive) | `GET /api/predictions?date=` · `GET /api/predictions/calibration` |
| 前端 | 「预测中心」页 `/predictions` (概率条/期望进球/派生市场/市场对照/偏差说明, 页脚"非投注建议") |

**首份校准报告要点 (2026-09-18)**: K线集成 n=211, LogLoss 1.0117 **优于市场同场集 1.0350 (−0.0233)**,
但 TOP1 44.1% 低于市场 48.8% — 概率质量更好、argmax 选边更保守, 这正是 LogLoss 口径才能暴露的事实;
半场冻结 1X2 n=2113 LogLoss 0.9952; HT锚 (小样本 n=29) LogLoss 0.8378; 半场 OU 校准偏弱 (ECE 0.39) 待修。

## 四、迭代二 (2026-09-18 当日完成 ✅)

1. **历史回填**: `predict_export --backfill-days N` (回测口径: 含已完赛场, 只补空行绝不覆盖,
   依然只用赛前tick) — 已回填 45 天 **9437 场**。派生市场校准不用等前向积累。
2. **派生市场校准进评估**: eval 脚本新增 O2.5 / BTTS / 期望总进球分箱 / 市场同盘口对照。
3. **goal_scale A/B 实证** (`scratch_ab_goalscale.py`, n=8194): score_model 默认 1.2 是波胆
   top3 的 λ 放大, 用于期望/大小球输出系统性高估 ~1 球。产品层派生市场改用诚实锚 **1.0**:
   O2.5 LogLoss 0.757→**0.677** (优于市场同盘口对照 0.696), ECE 0.209→**0.085**;
   BTTS ECE 0.159→**0.084**; 期望进球偏差 -1.13→**-0.59**。score_model 本体默认值不动。
   剩余 ~0.6 球偏差来自市场对本库冷门联赛的让水, 留作按联赛收缩的后续课题。
4. **每日自动化**: `run_daily_recheck.bat` (00:00) 追加 predict_export --refresh + 校准评估。
5. **前端去喊单化** (清单原二期#2, 提前完成): 比赛弹窗 价值层卡→「模型 vs 市场」(概率对照+
   偏差pp, 去掉 EV/凯利列)、决策卡→「模型倾向+偏差解释」、操盘手卡→「综合结论·一句话读数」
   (BET/PASS → 分歧显著/与市场一致); 后端 operator_output 蒸馏文案同步 (stake 恒空串,
   evidence 去 BET 语义); 开盘天眼镜 → 「模型 vs 市场」表达。**字段/接口零变更**, 纯表达层。

## 五、迭代三: 派生市场模型训练 (2026-09-18, 三轮全不采纳 — 负结论存档)

训练脚本: `scripts/train_derived_markets.py` (walkforward: kickoff升序 + TimeSeriesSplit(5) expanding,
n=8194, 采纳线 ΔLL≤-0.010 且 ≥4/5 折不劣)。基线 B1 = OIP 诚实锚矩阵 (goal_scale=1.0)。

| 轮 | 方案 | O2.5 ΔLL | BTTS ΔLL | 结论 |
|---|---|---|---|---|
| v1 | LGB: 去水1X2+OU锚+overround+联赛场均(expanding口径) | +0.052 | +0.039 | 不采纳 (1/5 折不劣) |
| v2 | v1 + B1概率stacking + 开盘→临场移动量 (ph/p_over) | +0.049 | +0.036 | 不采纳 (1/5 折不劣) |
| 校准 | B1 + walkforward isotonic (`scratch_calib_oip.py`) | +0.073 | +0.023 | 不过线 (ECE 0.113→0.085 变好, 但 LogLoss 恶化) |

**结论**: OIP 诚实锚就是本库宇宙派生市场的最优概率源 — 市场效率在 O2.5/BTTS 上同样成立,
与 1X2 v6 框架"主市场无超越赔率信息优势"结论一致。等渗只改善分桶校准不改善 LogScore,
不满足 LogLoss 优先的采纳线。**未来重试前提**: 数据源变化 (接入 GQ.db 11495 场 /
rollball_training 31.9万场扩样本) 或新信息源 (xG/伤停), 否则勿重复同类尝试。

## 六、二期剩余 (未执行)

1. **`_live_predict` 摘除 compute_value_layer**: 8600 行生产服务的热路径, 需 None 兜底 + 全端点回归后切换。
3. **`bookmaker_sim/` 解耦后归档**: 先把 `odds_handicap_converter` 迁到 pipeline/ (data_collector 依赖它)。
4. **凯利/注码 SSoT (`scripts/bet_core.py`) 归档**: 等 1 完成后自然失联; `tests/test_bet_core.py` 同步处理。
5. **candles 升级主赛前锚点决策**: 对照台账 ≥300 场后按 AGENTS.md 既有约定评估 (当前 231 场)。
6. **去水(devig) ≥5 处平行副本收敛** 到单一 SSoT。
7. **期望进球按联赛收缩**: 诚实锚后残余 ~0.6 球系统性偏差 (市场让水), 需 walkforward 验证的联赛系数。
8. **半场 OU 校准修复**: halftime_conclusion ECE 0.39, 待样本增厚后做分桶修正。

## 七、日常使用

```bash
PY=.venv/Scripts/python.exe
$PY -m pipeline.predict_export --date 2026-09-20      # 每日预测 (采集器K线判定到点后可重跑 --refresh)
$PY scripts/eval_prediction_calibration.py            # 校准评估 (完赛后跑, 建议每日/每周)
$PY scripts/settle_candles_vs_knn.py --days 7         # 既有双判定对照台账 (保留)
```

页面: 侧边栏「预测中心」; API: `/api/predictions` / `/api/predictions/calibration`。

---

## 附: 规范化 + 模型升级迭代 (2026-09-18 晚, 全面检查轮)

### 规范化已执行
- **P0**: ci.yml 三处硬断修复 (pytest-timeout/quant_executor/G3) + venv 装 pytest → **本地 79 测试全绿**;
  禁用孤儿计划任务 ShaoXiangOddsAssetDaily; 清 logs/ 1.7GB (四个大件) + bridge_watchdog 日志 10MB 轮转
  + backend_daemon.log 改道 logs/; events.db 删 6 张垃圾表 (零行×2 + 小 bak×2 + 投注台账×2, 数据先导出
  archive/db_exports_20260918/), 亿级 bak×2 留维护窗口
- **P1**: 根目录 19 个一次性文件 → archive/root_debris_20260918/; gq_checkpoint → scripts/ops/;
  gq_odds_filter/gq_drift_adapter 迁 pipeline/ (5 处引用同步); 死代码归档 (model_dispatcher/
  model_registry/2×.retired/scripts 旧 dc_score_model 副本) + pattern_matcher 注册桥加护栏 + engine.py 挂牌;
  pyproject packages 19→6 真实目录; requirements 删 9 项零使用依赖; model_registry.json 冻结 + 3 训练脚本停写闸;
  **ARCHITECTURE.md 全文重写为预测系统架构** + IRON_RULES IR-03/IR-10 勘误;
  **pipeline/odds_math.py 立为 devig SSoT** (8 处生产副本已委托, 修复 train_odds_trajectory_model 双 devig3 调用即崩);
  投注台账封账 (strategy_log/beat_under_log 已删表+导出, 4 脚本+2 bat 归档, /api/strategy/today 优雅空返回)
- **P2 待办**: docs/pending_cleanup_backlog.md (端点瘦身 65 端点/前端废类型/Vite 反模式/模型治理欠账/数据清理候选)

### 模型升级迭代结果 (全部 walkforward, 判定线预声明)
| # | 升级项 | 结果 | 数字 |
|---|---|---|---|
| M1 | HT锚 OU 分状态重验 | **全面胜出, 建议全状态采用** | 同集 walkforward: HT锚 76.8% vs 现任 50.2% (主领 76.2/51.0, 平 77.9/47.2, 客领 74.7/54.5, n=1233); meta 原记录 +16.6pp 得到加强; 客领先担忧证伪 |
| M2 | K线集成全语料重训 | **采纳并已固化** | 70d(n=3825) vs 26d(n=3219) 同协议: TOP1 47.5% vs 46.8%, **LL 1.0264 vs 1.0343**; data/models/candles_ensemble 已重训固化 (meta 含窗口对照); 采集器下次重启生效 |
| M3 | KNN+K线 stacking | 不采纳 | n=199 (不足200线); stacking LL 1.0080 vs K线单路 1.0034; KNN excess 信号无增益 |
| M4 | 期望进球联赛收缩 | 不采纳 + **重要发现** | 收缩系数不可迁移: 训练窗估 s 后测试窗 bias 从 -0.23 过冲到 +0.40~+0.51, O2.5 LL 0.676→0.732/0.758; 残余偏差非平稳, 收缩=追噪声 |
| M5 | 半场OU等渗校准 | **过线候选, 待查透** | isotonic: LL 0.6945→0.1968, ECE 0.448→0.094 (n=2029); 铁证=测试集 609 场中 0 场 raw 读数反映冻结时确定性 → 上游读数疑似 bug; 校准器存档 models/ht_ou_isotonic_20260918.joblib, 无下游消费方暂不接线 |

训练脚本新增: scripts/verify_ht_anchor_by_state.py · eval_knn_candles_stack.py · eval_league_shrink.py;
train_odds_candles_model.py 输出新增 LogLoss 主指标; reports/ 下 5 份新报告。

---

## 附2: 训练迭代 (2026-09-19, 自主优化轮)

| # | 项目 | 结果 | 数字 |
|---|---|---|---|
| T1 | K线 draw 校准 (α 缩放) | **不采纳** | 2/5 折有真增益(-0.02~-0.06) 但 3/5 折 α*=1.0 — 非平稳, 静态 α 缺乏跨折稳定性 (reports/draw_calibration_tune.json) |
| T2 | 70d 采纳多种子稳健性 | **确认稳定** | seed42/7/13 集成 LL 1.0264/1.0326/1.0193 (均值 1.0261 ± 0.007, 均不劣于 26d 基线 1.0343) |
| T3 | 半场OU读数条件化 | **根因修复+历史修复+验证闭环** | 根因: 冻结读数未条件化比分(486场确定态均值0.556); 修复: 采集器冻结条件化 + 历史修210行(时间轴验证); 全场正确口径修复后 LL=0.5635/ECE=0.0529, 确定态207场100%命中; M5 isotonic 增益证实为伪影(口径错+未条件化叠加), 校准器作废 |
| T4 | IR-32 落地守卫 | 完成 | tests/test_no_crossbook.py 反向守卫上线即抓到 2 处残留并清除 |

训练脚本新增: scripts/tune_draw_calibration.py · repair_ht_ou_conditioned.py。
