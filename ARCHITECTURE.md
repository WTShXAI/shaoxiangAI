# 哨响AI · 架构文档（预测系统）

> 最后更新：2026-09-18 · 方法：实际代码 import 图 + 进程探查 + 数据库实测
> 2026-09-18 系统转型：博彩量化 → **预测系统**。主指标 = LogLoss / Brier / 校准(ECE·斜率) / TOP1 准确率；
> ROI / CLV / 凯利已随投注决策外围归档 `archive/betting_decisions/`（判据见 docs/prediction_refactor_checklist.md）。
> 前版（2026-08-12）描述的 strategy/execution/database 执行闭环已不存在，本文档全面取代。

---

## 0. 第一性原理（2026-09-19 修订：智能化 + 实测值 + IR-32 跨庄禁令）

```
系统存在的唯一理由 =
    把赔率数据  →  变成  →  经校准、可自学习的概率预测（质量由 LogLoss 度量）
```

| # | 原理 | 实测约束（数字为 2026-09-19 实测，非占位） |
|---|------|------|
| 1 | **市场是有效的** | 1X2/OU/派生市场实证：无新信息源时，去水隐含/OIP 诚实锚即最优概率源（负结论台账 §8，M3/M4/M5-knn 三轮再证） |
| 2 | **智能化：模型从统计摘要器进化为自学习体** | ①多源特征（K线39M tick + KNN 31.2万库 + 叙事特征 15848 场）②统一库 events.db 单一训练面 ③校准闭环（每日可跑 eval→发现偏差→walkforward 修正）④新特征层 match_narrative 直接为下一代表模型供料。智能化=自学习闭环，非堆模型 |
| 3 | **概率质量 > 选边准确率（实测）** | K线集成(70d全语料): LogLoss 1.0264 优于市场、TOP1 47.5%（不追命中率）；HT锚OU 同集 76.8% vs 现任 50.2% |
| 4 | **只解释，不喊单** | 量化系统已删除（2026-09-19）：compute_value_layer/bet_core/凯利/EV/执行层/投注库全部归档 archive/quant_system_20260919/；输出仅概率+偏差说明 |
| 5 | **闭环可靠性（实测值）** | 常驻=bridge+采集器双 watchdog（巡检即拉起）；测试 41 用例全绿；API 80 条（前端活路径约 15 条，瘦身待办）；采集 23539 场/3927 万 tick，叙事覆盖 15848 场 |
| 6 | **IR-32 跨庄共识永久禁令** | 跨庄共识/跨庄edge永久禁止进入生产判定、API、前端、测试与实验；仅限自有训练/回测脚本在离线后台使用。违规=最高级事故（本条写入 IRON_RULES/AGENTS.md，并有 tests/test_no_crossbook.py 反向守卫） |

---

## 1. 真实 SSoT 地图（单一事实源）

| 模块 | 路径 | 职责 |
|------|------|------|
| **OIP 比分模型** | `pipeline/score_model.py` | 赔率隐含 Poisson：`predict_score`(比分矩阵/λ) + `deoverround`；派生市场概率源 |
| **K线集成 1X2** | `pipeline/odds_candles_predict.py` + `data/models/candles_ensemble/` | LGB+transformer 概率平均，4轮walkforward采纳 |
| **HT锚** | `pipeline/ht_anchor_predict.py` + `data/models/ht_anchor/` | 半场冻结时点 1X2+OU 方向 |
| **KNN 相似** | `pipeline/prematch_similarity.py` | football_data.db 31.2万场历史结构检索 |
| **预测产品层** | `pipeline/predict_export.py` | 逐场概率输出（1X2/xG/O2.5/BTTS/分布/偏差说明）→ `daily_predictions` 表（开赛冻结）；支持 `--backfill-days` 回填 |
| **结算原语** | `pipeline/settle.py` | 纯赛果判定（parse_score/result_1x2/settle_ou），零投注语义 |
| **校准工具** | `pipeline/calibration.py` | reliability/brier/log_loss/ece/slope |
| **模型治理** | `pipeline/model_catalog.py` | M1–M7 唯一注册表（≤7 强制校验）；旧 model_registry.json 已冻结为只读快照 |
| **偏差层(内置)** | `bridge_service._live_predict` | 模型-市场概率对照（量化系统已删除, value_layer=偏差-only） |
| **主入口** | `bridge_service.py` | FastAPI :9000，全部 API；`ShaoxiangBridge_Watchdog` 计划任务守护 |
| **采集器** | `gq/auto_collector.py` + `gq/db.py` + `gq/ws_collector.py` | 乐鱼实时采集 → events.db；四类冻结快照（赛前KNN/临场K线/开赛CS/半场） |
| **赔率双时点** | `pipeline/gq_odds_filter.py` | 初盘/中场收盘提取（2026-09-18 自根目录迁入） |
| **去水数学** | `pipeline/odds_math.py` | devig_n/devig2/devig3/devig_power 唯一实现（2026-09-19 收敛 8 副本） |
| **叙事特征** | `gq/match_narrative.py` | 平局/反超/进球干旱/热门失分特征记录 → match_narrative 表（15848 场），为下一代模型供料 |

**铁律**：新代码必须消费以上 SSoT，禁止平行重造（devig 收敛进行中：`pipeline/odds_math.py`）。

---

## 2. 数据流（2026-09-18 实测）

```
采集层 gq/auto_collector.py (watchdog 守护)
  乐鱼(GQ) H5 → data/events.db (37.3 GB 主库, WAL)
    matches(23.5k) / odds_changes(38.9M行,14.5k场有1X2) / odds_snapshots(112M行)
    match_outcomes(17.8k归档) / 四类冻结快照表
        │
        ├─ 赛前: prematch_conclusion(KNN) / prematch_candles_verdict(临场≤2h K线)
        ├─ 开赛: pre_match_cs(CS冻结)
        └─ 半场: halftime_conclusion + ht_model_verdict
        │
        ▼
预测层 pipeline/predict_export.py
  市场去水基准 + K线判定 + OIP矩阵派生(O2.5/BTTS/xG, goal_scale=1.0诚实锚)
  → daily_predictions (开赛冻结; --backfill-days 历史回填, 已45天9437场)
        │
        ▼
叙事层 gq/match_narrative.py
  完赛场 → match_narrative (平局回合/追平/反超/进球后干旱/热门失分, verified 治理假0-0)
        │
        ▼
评估层 scripts/eval_prediction_calibration.py
  六路判定源 LogLoss/Brier/ECE vs 市场基准 → reports/prediction_calibration_report.{json,md}
  scripts/settle_candles_vs_knn.py (K线vs KNN 双判定对照台账)
        │
        ▼
表达层 bridge_service.py :9000 → frontend/ (React+Vite, dist 由 bridge 托管)
  /predictions 预测中心 + 比赛弹窗(模型vs市场偏差解释, 无喊单)
```

---

## 3. 指标体系（预测系统主指标）

- **多分类 LogLoss**（随机基线 ln3≈1.0986）/ **多分类 Brier** / TOP1 准确率
- **分结果校准**：reliability 分桶 + ECE + 校准斜率（≈1 为佳）
- **对照口径**：模型 vs 市场去水隐含，**同一场集**对照
- 首份基线（2026-09-18）：K线集成 LogLoss 1.0120 vs 市场 1.0341（-0.022）；O2.5(诚实锚) 0.677 vs 市场 0.696；期望进球残余偏差 -0.59 球（待联赛收缩）

---

## 4. API 端点（前端活路径，其余 65+ 端点待瘦身清单）

- **预测**：`GET /api/predictions?date=` · `GET /api/predictions/calibration`
- **分析**：`POST /api/terminal/analyze`（比赛弹窗全链路）· `POST /api/predict/ranked`
- **赛程**：`GET /api/all-fixtures` · `/api/leagues/{sport}/fixtures` · `/api/live-scores`
- **滚球**：`/api/rollball/analyze` · `/api/live-goal-probe/*` · `/api/cs/trust-card`
- **运维**：`GET /health` · `/ready`
- 遗留保活：`/predict`、`/predict/simple`、`/predict/single`（前端零调用，engine.py 已挂牌 DEPRECATED）

---

## 5. 启动与守护（2026-09-18 实测）

```
后端主服务:  bridge_service.py :9000 (start_backend.py DETACHED 拉起, 日志 logs/backend_daemon.log)
采集守护:    已移除 (2026-09-19) — 存活性由每小时 autonomous_monitor 采集停滞告警接手
bridge守护:  scripts/bridge_watchdog.py (ShaoxiangBridge_Watchdog; 日志10MB轮转)
网络监护:    scripts/network_watchdog.py (ShaoxiangAI_NetWatch)
每日复盘:    ShaoxiangAI_DailyRecheck 00:00 → scripts/recheck_analysis.py --apply
预测日更:    手动 $PY -m pipeline.predict_export --date YYYY-MM-DD (--refresh / --backfill-days N)
校准评估:    手动 $PY scripts/eval_prediction_calibration.py   (2026-09-18 起无定时自动化, 按用户要求)
前端:        dist 由 bridge 托管; 改前端后 cd frontend && npm run build
测试:        .venv/Scripts/python -m pytest tests/ -q --timeout=120
已禁用:      ShaoXiangOddsAssetDaily (指向已不存在的 pipeline/oddset_asset.py, 2026-09-18)
```

---

## 6. 数据资产（2026-09-18 实测）

| 文件 | 大小 | 内容 |
|------|------|------|
| `data/events.db` | **37.3 GB** | 主库：采集+赛果+四类冻结快照+daily_predictions+prediction_ledger；含两个亿级 bak 表（odds_snapshots_bak 41.7M行 / odds_changes_bak 12.1M行，留维护窗口处理） |
| `data/GQ.db` | 6.2 GB | events.db 的 09-01 冻结子集（11,495 场全 ⊂ events.db），仅历史归档 |
| `data/football_data.db` | 724 MB | historical_matches 31.2万（KNN库）/ odds_features 32.6万 |
| `data/rollball_training.db` | 61 MB | rb_matches 31.9万（滚球训练集，date 止 2026-08-20，同步已停） |
| `archive/db_exports_20260918/` | — | 已删投注台账的 CSV 导出（strategy_log/beat_under_log 等 6 表） |

---

## 7. 已知缺口 / 待办（2026-09-19 更新）

- **【最高优先级 bug】半场OU读数未条件化**：halftime_conclusion.ou_prob 在 486 场"冻结时 OVER 已数学确定"的场次平均读数仅 0.556（应为≈1.0），0/609 测试场反映确定性 — 上游采集器 HT 冻结写入的是未条件化概率（证据 reports/ht_ou_isotonic_eval.json）。缓解：M6 HTAnchor OU 已全面可用；isotonic 校准器已存档。修复需重设计冻结读数语义
- ~~二期：_live_predict 摘除 compute_value_layer、bet_core 归档、bookmaker_sim 解耦~~ **已完成（2026-09-19 量化系统删除）**；去水收敛完成（pipeline/odds_math.py）
- **端点瘦身**：65 个前端零调用端点 + 前端废弃类型（清单见 docs/prediction_refactor_checklist.md §七）
- **模型升级路线图**（按序）：HT锚OU分任务(+16.6pp 待同集复验) → K线全语料重训(3223→~3900)+draw校准 → KNN+K线 stacking(sweep 11518 场就绪) → 期望进球联赛收缩(-0.59) → 半场OU校准(ECE 0.39)
- **数据杠杆**：采集赛前提频（赛前≥20 tick 场次仅 32% → 目标 60%+）
- **CI**: mypy 门禁未启用；events.db 亿级 bak 表清理需停机窗口 + VACUUM

---

## 8. 负结论台账（勿重试，除非前提变化）

| 结论 | 证据 |
|------|------|
| 市场效率：1X2/OU/派生市场均无超越赔率的稳定信息 | v6 框架；p0_10 独立路径 LL 1.0117 vs 市场 0.9411；派生市场 LGB/stacking/isotonic 三轮全输 OIP 锚（2026-09-18） |
| Dixon-Coles 小样本过拟合 | OOS LL 3.85 vs OIP 2.83 |
| XGB Poisson λ 不敌 OIP | OU@2.5 AUC 0.584 < 0.70 gate，已封存 |
| Kronos 预训练迁移无增益 | probe 与随机初始化差 0.1~1.0pp = 噪音（reports/kronos_transfer_falsified_20260916.md） |
| 多通道K线 E6 | 1X2 +0.1/+0.3pp、OU 低于多数类；重开条件=语料×10 |
| 微观结构"读盘"信号 | R1 6,000场三假设全证伪 |
| goal_scale 语义分离 | 1.2 仅波胆 top3 口径；期望/大小球必须用 1.0 诚实锚（A/B n=8194） |

**重试前提**：新信息源（xG/伤停/第二庄家）或语料×10。

---

## 9. 技术栈
- **后端**：Python 3.12 (.venv) + FastAPI + uvicorn；SQLite (WAL, 单写者 gq/db.py)
- **建模**：LightGBM / XGBoost / sklearn / PyTorch(CPU, transformer) / scipy(OIP 求解)
- **前端**：React 18 + TypeScript + Vite + Tailwind（dist 由 bridge 托管）
- **采集**：乐鱼(GQ) H5（auto_collector + watchdog）；CI：GitHub Actions（pytest+tsc+vite build）

---

*本文档为预测系统的唯一权威架构描述。与本文档矛盾的旧文档/旧假设，以本文档为准。
规范化执行记录与模型升级路线图：`docs/prediction_refactor_checklist.md`；铁律：`docs/IRON_RULES.md`。*
