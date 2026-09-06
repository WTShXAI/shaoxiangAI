# 哨响AI · 第一性原理架构文档

> 最后更新：2026-08-12 · 方法：第一性原理（First Principles）
> 本文档描述系统的**真实状态**。一切结论来自实际代码 import 图、进程探查与数据库实测。
> 前版（2026-07-16）已严重过时：GQ.db 当时记 2.45MB（实为 6.09GB）、football_data.db 记 523MB（实为 661MB）、且 8 月全部修复与链路训练均未录入。本次全盘核查后重写。

---

## 0. 第一性原理（系统存在的根本理由）

```
系统存在的唯一理由 =
    把赔率数据  →  变成  →  可执行的、有正期望的下注决策
```

| # | 第一性原理 | 对系统的约束 |
|---|-----------|-------------|
| 1 | **数据是唯一的资产** | 没有实时、跨庄、干净的赔率，模型/策略全是空中楼阁 |
| 2 | **1X2 市场是有效的** | edge 只存在于：①跨庄价差（soft-line 不平衡）②时序失衡（开盘→临场→滚盘漂移） |
| 3 | **模型是统计摘要器，不是智能** | OOS AUC：让球 ~0.72~0.76、平局 ~0.50（市场效率天花板）。ML 真实价值=跨赛事概率校准，不是"预测胜负" |
| 4 | **执行闭环 > 预测精度** | 行情→扫描→决策→注码→执行→风控→绩效。一个 7×24 稳定跑的闭环 >> 95% 精确但崩的模型 |
| 5 | **可靠性 > 功能数量** | 50 功能 + 崩 = 0；10 功能 + 永不停 = 10 |

### 铁律（最高指令，SSoT 于 `.workbuddy/memory/MEMORY.md`）
1. 数据有据可查；未知填 `--` 不填 0；不拿即时比分冒充半场；派生特征独立成集。
2. 盘口锚定操盘手，默认 100% 跟盘，去水分歧 ≥0.10 才降权。
4. 用户实时观测 = 地面真相；web 仅辅证。AH/OU split 取均值；负=主让。
5-7. 禁 Beta 校准/虚拟数据；命中率并排 naive 基线；评估用重复 CV + AUC + 分箱。
8-9. OU 概率分箱（完美单调）；AH 任务已下线的旧结论已被推翻（见 §9）。建模库禁收「终场 0-0 且半场缺失」假 0-0。
10. SQLite 字符串用单引号。
11-12. GQ 僵尸判定（开赛 3h 内不杀）；波胆用市场 CS 赔率为主（93.2% 覆盖），泊松回退。
13-14. 修 bug 必追问"历史数据回填了吗"（dry-run + --apply + 审计可回滚）；零方差盘口线 = 采集伪造，market_health_check 护栏。
15. AUC 突高先疑任务退化，用消融证伪。

---

## 1. 真实 SSoT 地图（单一事实源）

被 ≥3 处 import 且无竞争实现的权威模块：

| 模块 | 路径 | 职责 | 状态 |
|------|------|------|------|
| **注码核心** | `scripts/bet_core.py` | 半凯利注码 `decide_dir`/`safe_stake`/`kelly_fraction` | ✅ |
| **赔率破解** | `pipeline/reverse_odds_engine.py` | 庄家意图解码 `ReverseOddsEngine.analyze_multi` | ✅ |
| **预测核心（旧链路）** | `pipeline/engine.py` | `create_engine`（标称 v7.1 规则管线）。前端 `/api/terminal/analyze` **不经此**（见 §2 双链路） | ✅ |
| **OIP 波胆** | `pipeline/score_model.py` | `_live_predict` 实际调用（`predict_score` OIP Poisson，前端唯一活路径） | ✅（生产） |
| **投注库** | `database.py` | SQLite 资金曲线/风控/报表（→ `data/bets.db`） | ✅ |
| **价值层** | `pipeline/compute_value_layer.py` | `compute_value_layer()` 纯函数 | ✅ |
| **策略层+组合层** | `pipeline/strategy.py` | 多策略注册 + 组合聚合 → BetPlan | ✅ |
| **执行层+手动确认闸** | `pipeline/execution.py` | 消费 BetPlan；`ManualConfirmationGate` 绝不无确认打出 | ✅ |
| **主入口** | `bridge_service.py` | FastAPI 服务（端口 9000），暴露全部 API（见 §4） | ✅（运行主场） |
| **采集器** | `gq/auto_collector.py` + `gq/launcher.py` | 乐鱼(GQ)实时赔率采集守护进程 | ✅（运行主场） |
| **采集 DB 层** | `gq/db.py` | `is_virtual_league`(227) 虚拟盘拦截 · `is_override`(206-410) 人工纠偏锁 · `record_match_outcome`(571) 归档 · `result="home/draw/away"`(723-727) 规范 · CS 三表(1514/1594/1623) | ✅ |
| **模型注册表** | `saved_models/model_registry.json` | 版本/chains/active 指针 | ⚠️ 含重复空壳条目（见 §7） |
| **联赛链路训练** | `scripts/league_train_pipeline.py` | 剔世界杯/友谊赛，训 1X2+OU+AH+DrawExpert | ✅（8-12 落地） |
| **WC2026 链路训练** | `scripts/wc2026_train_pipeline.py` | 读 `wc2026_merged.json`，GroupKFold 防泄漏 | ✅（8-12 落地） |
| **WC2026 合并** | `scripts/merge_wc2026_odds.py` | 软件 `wc_all_matches` ∩ 截图盘口，队名规范化 | ✅（8-12 落地） |
| **回档** | `scripts/backfill_outcomes.py` | 漏档补 `match_outcomes`（dry-run + --apply + 审计） | ✅（8-12 用） |
| **重启守护** | `restart_bridge.py` | Popen DETACHED 自举，单实例收口 | ✅（8-10 用） |

**铁律**：所有新代码必须消费以上 SSoT，禁止平行重造。

---

## 2. 真实数据流（2026-08-12 实测）

```
┌─────────────────────────────────────────────────────────────────┐
│ 采集层  gq/auto_collector.py (launcher.py 守护, 60s 轮询)         │
│   乐鱼(GQ) H5 实时赔率 → GQ.db (6.09 GB, 主采集库)               │
│   - matches(权威, mid NULL) / match_outcomes(归档, mid 数字)      │
│   - odds_snapshots(盘口/赔率, market=1X2/AH/OU/CS)               │
│   - pre_match_cs(赛前波胆冻结) / cs_verification(赛果验证)        │
│   - analysis_cache(复盘) / 虚拟盘 is_virtual 拦截                  │
└───────────────────────────┬─────────────────────────────────────┘
                             │  odds + 终场赛果
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 历史库  football_data.db (661 MB)                                │
│   wc_all_matches(edition='2026' 136场, 含1X2临盘赔率+真实赛果)    │
│   interwetten_odds / william_ht / live_odds_raw 等赔率表          │
└───────────────────────────┬─────────────────────────────────────┘
                             │ 特征
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 特征库  shaoxiang_feature_library.db (3275场)                    │
│   labeled_1x2=3275 / ou=2413 / ah=0；x1_h/d/a 去水概率(和≡1)     │
│   xspread 跨市场价差（真 edge 来源）                              │
└───────────────────────────┬─────────────────────────────────────┘
                             │ 训练
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 训练层  scripts/{league,wc2026}_train_pipeline.py                │
│   派生 ~12 维赔率特征 → Stacking(LGB+XGB→LR) + DrawExpert + OU   │
│   GroupKFold(按对阵分组) 防泄漏；并排 naive 基线                  │
│   落盘 data/*.joblib → 注册 model_registry (chains: league/wc)   │
└───────────────────────────┬─────────────────────────────────────┘
                             │ 推理
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 预测层  (⚠️ 多链路并存, 见 §4)                                   │
│   链路A(旧 /predict* 端点): create_engine → wc_main_v1 + draw    │
│   链路B(生产 /api/terminal/analyze, 前端唯一活路径):              │
│        bridge._live_predict → score_model.predict_score (OIP)     │
│   链路C(8月新增 /api/predict/live, /api/predict/ranked):          │
│        ranked_predictor / model_dispatcher 编排                   │
└───────────────────────────┬─────────────────────────────────────┘
                             │ 预测 + 赔率
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│ 价值层  compute_value_layer → 执行层 execution → 前端 Vite+PWA    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. 执行闭环（量化系统）

```
实时赔率 ──→ 全市场扫描 ──→ 价值层打分(compute_value_layer) ──→ 策略层+组合层(strategy.py) ──→ BetPlan
   │                                                                                    │
   │                                                                          bet_core 算注码(半凯利,单注封顶10%)
   │                                                                                    │
   └─────────────────── 执行层(execution.py) ─────────────────┐                        │
      ┌─ 模拟盘(sim): 自动执行+自动结算 → database(equity)     │                        │
      └─ 真实盘(real): ManualConfirmationGate → 确认才落库      │                        │
                                                                  ▼                        │
                                                            绩效归因(equity/sharpe/DD) ◄┘
```

---

## 4. 当前 API 端点（bridge_service.py 实测，2026-08-12）

**核心端点**（按触发范围分组）：
- **预测**：`POST /predict`、`/predict/simple`、`/predict/single`（旧链路A）；`POST /api/terminal/analyze`（链路B，前端活路径）；`POST /api/predict/live`、`POST /api/predict/ranked`（链路C）；`GET /api/live/wc`
- **价值/信号**：`POST /api/leyu/value-signal`（8-12 修复 405→200）、`GET /api/cross-book/signals`、`GET /api/cross-book/lookup`、`GET /api/water-signals`、`GET /api/template-deviation`
- **实时比分**：`GET /api/live-scores`、`/api/live-score/{mid}`、`/api/live-update/{mid}`（**缺失 `GET /api/matches/state`**，见 §7 缺口#1）
- **赛程/赛果**：`GET /api/all-fixtures`、`/api/leagues`、`/api/leagues/{sport}/fixtures`、`/api/match-results`、`/api/auto-results`、`/api/timeline/*`
- **复盘/CS**：`GET /api/analysis/cache`、`/api/analysis/scan`、`/api/cs/pre-match`、`/api/cs/verification`（CS 端点实测正常）
- **组合/执行**：`POST /api/portfolio`、`POST /api/execute/confirm`、`GET /api/execute/pending`、`POST /api/execute/settle`
- **运维**：`GET /health`、`/ready`、`GET /api/data-growth/stats`、`/api/quota`、`/api/report/*`

> ⚠️ 共 ~55 个端点，远超 2026-07-16 文档记录的"双链路"描述。文档当时未记录 8 月新增的 `/api/leyu/value-signal`、`/api/predict/live`、`/api/predict/ranked`、`/api/cross-book/*`、`/api/template-deviation`、`/api/water-signals` 等。

---

## 5. 启动方式（2026-08-12 真实入口）

```
后端主服务:   bridge_service.py  (端口 9000, 单实例, 已收口)
启动命令:     .venv/Scripts/python.exe bridge_service.py --port 9000
采集守护:     .venv/Scripts/python.exe gq/launcher.py  (后台守护, 60s 轮询)
重启(生产):   restart_bridge.py  (Popen DETACHED 自举, 单实例收口)
前端开发:     cd frontend && npm run dev  (端口 3000, 代理 /api → 9000)
前端生产:     cd frontend && npm run build  (dist/ 后端托管)
乐鱼 token:   gq/.env 的 GQ_REQUEST_ID (gitignore, 不入库)
```

> **双实例已收口（2026-08-10）**：原 4 进程（9000×2 + 9100×2）乱象，经批准整合为单实例 9000（restart_bridge.py 启动）；9100 已关闭不可达。无 supervisor 自动重启（计划任务仅 DailyRecheck + OddsAssetDaily）。

---

## 6. 数据资产清单（诚实版，2026-08-12 实测）

| 文件 | 实测大小 | 状态 / 内容 |
|------|---------|------------|
| `data/GQ.db` | **6.09 GB** | ✅ 乐鱼实时赔率主采集库（matches/match_outcomes/odds_snapshots/pre_match_cs/cs_verification/analysis_cache） |
| `data/football_data.db` | **661 MB** | ✅ 历史赛果+赔率（wc_all_matches 2026 共 136 场带临盘赔率+赛果；interwetten_odds 等） |
| `data/shaoxiang_feature_library.db` | ~978 KB | ✅ 特征库 3275 场（labeled_1x2=3275/ou=2413/ah=0），x1 去水概率 + xspread 跨市场价差 |
| `data/worldcup_screenshots.db` | ~319 KB | ✅ 70 张世界杯赔率截图 OCR（含盘口细节，无赛果） |
| `data/hist_feature_matrix.db` | ~50 MB | ✅ 历史特征矩阵 |
| `data/bets.db` | 81 KB | ✅ 投注记录 |
| `data/quant_trading.db` | 110 KB | ✅ 量化交易 |
| `data/leisu_odds.db` | 7 MB | ✅ 雷速赔率 |
| `data/electronic_poll_*.db` (20+) | 各 3-4 MB | ⚠️ 乐鱼轮询实验库（散落，待清理） |
| `data/live_poll_*.db` | 153 MB | ⚠️ 实时轮询实验库（散落） |
| `data/_verify_sandbox.db` | 355 MB | ⚠️ 验证沙箱（可清理） |
| 根目录同名 `GQ.db`/`football_data.db` (0字节) | 0 | ❌ 占位（真库在 data/） |
| `data/wc2026_*.db`/`leisu_live.db`/`live_scores.db` 等 | 0 | ❌ 空占位 |

**注意**：根目录下有大量临时/实验文件（`_bzy_*.py`、`_diag_*.png`、`_zcode_deleted_manifest_*.txt`(1.4MB)、各类 `_*.py`/`_*.txt`），属历史调试残留，非系统组成部分。

---

## 7. 已知缺口 / 待修（2026-08-12 盘点）

| # | 缺口 | 真相 | 优先级 |
|---|------|------|-------|
| 1 | ~~`GET /api/matches/state` 缺失~~ | **已修复(2026-08-13)**：补端点复用 enrich_match_state + 同源 feed，实测 200 返回 2655 场(2566 finished/40 live/47 scheduled/2 unknown)，WS1 重合并生效 | — |
| 2 | ~~DT 推理维度不匹配~~ | **已修复**：fl_model_1x2=37维，fl_predictor 改用 extract_features(N_FEAT=37) 动态维度对齐，predict_from_odds 返回有效归一化概率，dt_vote 生效（记忆旧"22维喂30维"数字均过时） | — |
| 3 | ~~`league_scoring_prior` 中文匹配 bug~~ | **已修复**：实测英超/法甲/西甲/德甲/意甲/中超 exact 命中 prior_n=1300~1670；巴西甲/欧冠聚合命中；仅 WC2026 特殊长联赛名会误匹墨西哥杯(权重极低可忽略) | — |
| 4 | ~~model_registry 脏数据~~ | **已修复(2026-08-13)**：去重 9→4 版本（删 5 个 wc_v1 重复副本 + 1 个 league_v1 缺AUC 副本），保留 `6.0-rule`/`wc_v1`(legacy 指针锚)/`league_v1`(完整AUC)/`wc2026_v1`；所有指针解析 OK 无悬空；备份 `model_registry.json.bak_20260813_*` | — |
| 5 | **WC2026 链路 AH 未训** | `wc_all_matches` 无一致 AH 盘口线；截图 AH 线变参 → 暂不训 AH | P2 |
| 7 | ~~`kickoff=epoch 0` 漏档风险~~ | **已修复(2026-08-13)**：`recheck_analysis.py` 加 `recheck_missing_outcomes` 阶段（每日 00:00 `ShaoxiangAI_DailyRecheck` 自动扫描 `finished`+非虚拟+无 `match_outcomes` 应归档缺档并补档，复用 `record_match_outcome` 幂等）。**关键 bug 修正**：必须限定 `status='finished'`，否则 live 场实时比分被误当终场锁死。当前实时库补 20 场 finished 漏档（写20/失败0），复查归零；备份 `GQ.db.bak_20260813_085629`。根因：采集器归档**持续漏档**（非仅 kickoff=0），护栏每日兜底 | — |

---

## 8. 模型迭代诚实基线（截至 2026-08-12）

### 三条链路现状
| 链路 | 训练数据 | 关键指标 | 结论 |
|------|---------|---------|------|
| **league_v1** (8-12) | 剔世界杯310+友谊259 → 1X2/2999, OU/2469, AH/1274 | 1X2 AUC **0.6445**(acc 0.5245 vs 0.4565 +6.8pp) · **AH AUC 0.7602 强 edge** · OU AUC 0.5456≈基线(无 edge) | AH 有真信号；1X2/OU 从赔率拿不到稳定 edge |
| **wc2026_v1** (8-12) | `wc2026_merged.json` 115 场(GroupKFold) | 1X2 acc **0.5739** vs 押最热方 **0.6000**（−2.6pp）· AUC 0.6825 · OU AUC 0.5046≈随机 | **ML 打不过市场**，WC 主链路应锚定赔率，ML 仅次级叠加 |
| **wc_v1** (旧, legacy active 指针) | ~116 场早期 WC | wc_main_v1 + DrawExpert_v3_focal 双 ML | registry 已去重(5→1 副本)；指针仍指向它但无磁盘文件(孤儿)，未激活生产，符合既有"WC 锚定赔率"决定 |


### 波胆(OIP Poisson) 校准（2026-07-18 walkforward，仍有效）
- 通用联赛 `goal_scale=1.2`（test OOS top3 34.41%）；WC 独立 `1.35`
- 天花板确凿：最常用 1-1 实际占 11.9%，模型 top1=12.9% 已贴近上限

---

## 9. 技术栈
- **后端**：Python 3.11/3.13（.venv）+ FastAPI + uvicorn
- **预测**：XGBoost / LightGBM / PyTorch（score_model OIP）
- **前端**：React 18 + TypeScript + Vite 5 + Tailwind + ECharts + framer-motion
- **数据**：SQLite（GQ.db / football_data.db / shaoxiang_feature_library.db / bets.db）
- **采集**：乐鱼(GQ) H5（auto_collector + launcher 守护）；历史 The Odds API / interwetten / william
- **CI**：GitHub Actions（flake8 + pytest + tsc + vite build）

---

## 10. 优化路线图（按第一性原理排序）

### P0 — 系统必须能启动且文档真实
- [x] 本架构文档重写（2026-08-12，取代 7-16 过时版）
- [x] 入口统一为 `bridge_service.py`（单实例 9000）
- [x] 双实例收口（restart_bridge.py）

### P1 — 消除 SSoT 名实不符 + 关键 bug
- [x] `compute_value_layer` 提取为独立模块
- [x] 事件循环冻结修复（to_thread + LRU 缓存，2026-08-06）
- [x] 虚拟盘根拦截 `is_virtual_league`（8-07）
- [x] 赛前 CS 归档 + 赛果验证（8-08）
- [x] `kickoff=0` 漏档 backfill 57 场（8-12）
- [x] **修 `/api/matches/state` 缺失**（缺口#1，2026-08-13 实测生效）
- [x] **修 DT 维度不匹配**（缺口#2，实测已生效，记忆旧数字过时）
- [x] **修 `league_scoring_prior` 中文匹配**（缺口#3，实测已生效）

### P2 — 注册表与数据清理
- [x] 清理 model_registry 空壳/重复条目（缺口#4，9→4 版本，2026-08-13）
- [ ] 清理散落实验 db（electronic_poll_*/live_poll_*/_verify_sandbox）
- [ ] 清理根目录临时 `_*.py`/`_*.png`/`_*.txt`
- [ ] 接入跨庄共识(尖庄源)点亮 +EV（缺口#6，待数据）

### P3 — 可靠性加固
- [x] `kickoff=0` 漏档自动检测（缺口#7，已做护栏 8-13：每日 00:00 自动扫描 finished 漏档并补档）
- [ ] CI 加 mypy 门禁（SSoT 模块）
- [ ] 前端三页合并（TradingHub）

---

*本文档由赵统筹（总工）按第一性原理维护。任何与本文档矛盾的旧文档/旧假设，以本文档为准。规则铁律详见于 `.workbuddy/memory/MEMORY.md`。*
