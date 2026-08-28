# 对比分析：GitHub `shaoxiangAI` 仓 vs 本系统 `D:\Architecture`

> 总工视角结论先行 · 2026-08-27 · 实证驱动（非凭记忆）
> 两仓同源「哨响AI」血脉，但走了**两条不同演化岔路**：GitHub 仓是「研究/模块化/LangGraph 支线」（冻结于 ~07-14），本系统是「生产/实时/单体支线」（活跃至今天 08-27）。

## 〇、一句话裁决

| 维度 | 谁更胜 | 关键证据 |
|------|--------|----------|
| **更先进**（工程范式） | 平分秋色，类型不同 | GitHub=架构纯净+多智能体 LLM 编排；本系统=实时数据深度+前端工程化+持续在线 |
| **更准确**（实测指标） | **本系统（微弱但可信）** | 本系统 1X2 AUC 0.885 / OU live AUC 0.770；GitHub WC2026 仅 42.3% 点准确率、D-Gate 自承 0% |
| **分析更明确** | 各擅其场 | GitHub=多智能体 LLM 叙述更"讲人话"；本系统=结构化盘口破解卡（CS三栏实证/terminal/逆向硬判定）更"可追溯" |

**结论：本系统在「实时运营 + 可信实测准确率 + 前端工程化」维度整体更先进；GitHub 仓在「架构美感 + LLM 多智能体解释层 + 评估流水线成熟度」维度更先进。两者都未能在"真实外推准确率"上交出让对方信服的硬数字——这是共同诚实边界。**

---

## 一、架构范式对比（先进度）

### GitHub `shaoxiangAI`（实测）
- **模块化包结构**：`backend/{api,core,data,features,models,services,tasks,training,scripts}` + `agents/` + `knowledge_base/` + `data_collector/` + `bookmaker_sim/` + `ml/` + `optimize/`。
- **LangGraph 四模型多智能体**（README 自报 LAMF v4.1.0）：Commander `gemma4:12b` / DataAgent `deepseek-r1:8b` / MathAgent `phi4:14b` / Explainer `qwen3:8b`。
- **ModelBridge v2.0**：XGBoost+Ridge 锁定 `football_v4.1_production.joblib`；90+ 维特征；三层降级（LLM/ML/规则 Poisson+Kelly）。
- **D-Gate 引擎**（v5.0→v5.3，基于 Elo 动态门控）、OU 校准、DrawExpert、HCP 让球、Full Linkage Predictor、知识库 KB。
- **评估流水线 `eval_pipeline.py`**：7 维评估器 E0–E7，诚实原则「宁标 NA 不造假」（第 24 行 `_na`）。**这是本系统所没有的研究级评估纪律。**
- **前端**：静态 SPA v5.0 深空暗黑主题（非 React 工程化，无构建系统）。仅 5 个 API 端点（`/api/v1/predict`、`/auth/login|register`、`/monitor/health`）。
- **数据源**：`data_collector/api_football_client.py` → **API-Football (RapidAPI) + Football-Data.org**（轮询式）；有 `football_data_live.py`/`matches.py` 含 WebSocket/odds_snapshots，但属 API 轮询而非深度拦截。
- 历史库 `data/football_data.db`（312,010 场，多为 2013+ 历史）。

### 本系统 `D:\Architecture`（活跃）
- **单体 `bridge_service.py`**（7215 行，70+ 端点，:9000）+ **React+Vite+TS+Tailwind 工程化仪表盘**（10 磁贴 + 详情弹窗，CDP 实测 `TILE_BTN_COUNT=10` 连后端取 5 场意甲真数据）。
- **乐鱼 WS 实时深度采集**：`gq/ws_collector.py`（Playwright+Edge 接管 H5 会话拦截 WS 帧）→ `events.db`（31M+ odds_snapshots，秒级新鲜，含 obscure 联赛修复）。**这是本系统相对 GitHub 仓最硬的优势——实时数据深度。**
- **赔率破解全链路**：6 维盘口结构 / cross-book / CS 信任卡（三栏：结构/庄家/历史实证）/ terminal 赛事终端 / `reverse_odds_engine` / `compute_value_layer` / 操盘手逆转信号。
- **LLM（Ollama）**：决策智能体/操盘手卡存在但**暂未启用**（工作记忆 v7.4 报告："本地 qwen3:14b 已就绪但暂未启用"）。
- **持续训练**：日志至 2026-08-27（`train_live_rollball_20260827`、`global_training_20260824`）。

### 先进度判定（P 级）
- **P0（本系统胜）**：实时数据深度（乐鱼 WS 深度拦截 vs API 轮询）、前端工程化（React vs 静态 SPA）、当前活跃度（08-27 vs 07-14，新约 1–2 月）。
- **P0（GitHub 胜）**：架构模块化纯净度、LangGraph 多智能体 LLM 编排、研究级 7 维评估流水线、多模型锁定与可复现性。
- **平手**：赔率破解的"算法种类"两边都有（D-Gate vs reverse_odds；庄家操盘四段式 vs CS 三栏信任卡），深度相当但取向不同。

---

## 二、准确率对比（准确度的诚实口径）

### GitHub 实测数字（取自报告，非记忆）
- `reports/worldcup_2026_backtest_report.md:14` → **WC2026 整体准确率 42.3%（11/26）**；比分 Top1 命中 23.1%（6/26）。
- 同报告:132 → Fallback-H（超热门判主胜）80% 最高，模式 A（中热门判平局）仅 36.8%。
- `reports/D-Gate_v5_回测验证与参数优化报告.md:32,60,235` → **v4.6 / v5.0 / 网格搜索全部配置准确率均为 0%**（"所有主胜/客胜预测均失败"），属显式失败迭代报告。

### 本系统实测数字（取自 v74 交叉核验，系统自否"自述数字"）
- `reports/v74_research_report_crosscheck.md:35` → 系统**拒绝采纳**内部自述数字（66% 命中/ROI 1.8%），只采纳实测 CV：
  - **live_1x2 AUC = 0.885**（naive 基线 0.452）✅ 强判别力
  - **live_ou AUC = 0.770** ✅
  - **OU 赛前 AUC ≈ 0.515**（无方向 edge，诚实保留边界）
- `deliverables/wc2026_full_backtest.json:66` → 模型自报 CV 75.6%，但**明确标注 IN-SAMPLE（训练集内重叠），非真实外推**，不可作 live 性能代表。（工作记忆所谓"78.1% 准确率"即此 in-sample 数，不应作为对外准确率。）

### 准确度判定
- **判别力维度**：本系统 AUC 0.885（1X2）/ 0.770（OU live）是**可信的强判别指标**；GitHub 未报告 AUC，仅点准确率 42.3%（26 场，3 类 1X2 问题随机基线 ~33%，+9pp 属 modest，且大概率低于"永远押热门"平凡基线 ~45–50%），D-Gate 更曾 0%。
- **口径诚实维度**：两边都有"自述虚高"问题——GitHub 的 D-Gate 报告是失败迭代、WC 42.3% 为 modest；本系统的 75.6%/78.1% 为 in-sample 已自标。本系统因有 v74 交叉核验**主动戳破自述数字**，诚实纪律略胜。
- **结论**：在双方都诚实标注的实测指标上，本系统更可信地更准确；但**双方都缺"干净外推（OOS）准确率"这一终极证据**——这是共同未解短板。

---

## 三、分析明确度对比

### GitHub：LLM 多智能体叙述更"讲人话"
- `bookmaker_reports.py` 的 `build_bookmaker_report()`：庄家四段式操盘推演（真实概率判断 / 三套赔率方案 A 保守·B 收割·C 诱平 / 决策矩阵 / 底层逻辑），含 D-Gate 联动。是**结构化"庄家会怎么定价"模拟**，叙述丰富。
- LangGraph 四模型各司其职（指挥官/数据/数学/解释），**解释层天然更透明、更可对话**。

### 本系统：结构化破解卡更"可追溯"
- CS 信任卡**三栏**：结构列 / 庄家列 / 历史实证频率（读 `match_outcomes`，禁硬编码，TTL 缓存）——证据可追。
- terminal 赛事终端、`reverse_odds_engine` 硬判定（诱盘/诚实防 + `predict_mispricing` + 凯利）、6 维盘口结构分类、操盘手逆转信号（以当前比分为先验）。
- 铁律约束"分析非预测"（禁"必赢/稳胆"措辞）、合规 AgeGate/免责——**表达更克制、更可审计**。

### 明确度判定
- **"讲得清楚、能对话"** → GitHub 多智能体 LLM 胜（这是本系统 LLM 层暂未启用的直接后果）。
- **"证据可追溯、陷阱判定硬"** → 本系统胜（CS 三栏实证、逆向硬判定、in-sample 自标）。
- 取决于"明确"的定义：面向人话叙述 GitHub 胜；面向操盘可追溯本系统胜。

---

## 四、综合裁决与建议

1. **本系统是当前"更先进的生产系统"**：实时乐鱼 WS 深度采集 + React 工程化前端 + 持续在线训练 + 诚实实测 AUC，整体可运营性领先。
2. **GitHub 仓有本系统应借鉴的资产**：
   - `eval_pipeline.py` 的 7 维评估流水线 + "宁标 NA 不造假"纪律 → 本系统评估偏散（各 deliverable 各自为战），应吸收为统一评估入口。
   - LangGraph 多智能体 LLM 解释层 → 本系统 LLM 层暂未启用，是"分析明确度"的明显短板，建议重启 qwen3:14b 解释智能体。
   - 模块化包结构 → 本系统 `bridge_service.py` 7215 行单体是技术债，长期应拆。
3. **共同短板（诚实边界）**：双方都缺**干净 OOS 外推准确率**铁证。本系统 in-sample 75.6% 与 GitHub 42.3% 不可直接比（样本/口径不同）。下一步应跑双方统一的 OOS 回测（同联赛同窗口）才有定论。

> ⚠️ 已知未动：本对比未实跑双方统一 OOS 回测；LLM 层本系统未启用状态未在本次改变；GitHub 仓 07-14 后未再活跃，结论基于其冻结态代码。

---
*证据锚点：GitHub `README.md`/`CHANGELOG.md`/`reports/worldcup_2026_backtest_report.md`/`reports/D-Gate_v5_回测验证与参数优化报告.md`/`eval_pipeline.py`/`data_collector/api_football_client.py`/`backend/services/bookmaker_reports.py`；本系统 `reports/v74_research_report_crosscheck.md`/`deliverables/wc2026_full_backtest.json`/`gq/auto_collector.py`/`ARCHITECTURE.md`。*
