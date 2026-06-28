# Python 模块索引

> 自动生成于 2026-06-03 16:34:07

## 概览

### agents/ (15 个模块)

| 文件 | 说明 | 类 | 函数 |
|------|------|-----|------|
| `base_agent.py` | 哨响AI - 智能体基类 (Expert Agent Base) ========================================== 所有专业 | 1 | 0 |
| `commander_validator.py` | 哨响AI - 指挥官验证引擎 (Commander Validator) =========================================== | 1 | 0 |
| `demo.py` | 哨响AI — 智能体系统完整演示 ============================= 演示三层架构的完整流程：预测 → 反馈学习 → 第二轮预测 | 0 | 2 |
| `demo_validator.py` | 哨响AI - 指挥官验证模块完整演示 ================================= 验证功能：   1) 单个验证 → 精确率/召回率/F | 0 | 2 |
| `ensemble_agent.py` | 哨响AI - 整合智能体 (Ensemble Agent) ======================================= 第三层智能体：融合各 | 1 | 0 |
| `expert_calibrator.py` | 哨响AI — 专家校准器 (Expert Calibrator) v2.0 ========================================== | 1 | 1 |
| `expert_model_trainer.py` | 哨响AI — 专家专属模型训练器 v2.0 ================================ 为每个专家训练专属ML模型，使用领域特征子集 +  | 1 | 1 |
| `expert_selector.py` | ⚠️ 已弃用(v4.0) — ExpertSelector 专家选择器 ============================================ | 1 | 1 |
| `expert_v2.py` | 哨响AI — 增强版专家包装器 (ExpertV2) v2.0 =========================================== 在原始规 | 2 | 0 |
| `learning_agent.py` | 哨响AI - 学习智能体 (Learning Agent) ======================================= 从预测结果中持续学习 | 1 | 0 |
| `meta_feature_extractor.py` | 哨响AI — 元特征提取器 (Meta Feature Extractor) v2.0 ==================================== | 1 | 1 |
| `orchestrator.py` | 哨响AI - 指挥官智能体 (Orchestrator Agent) ============================================= | 1 | 0 |
| `scheduler.py` | 哨响AI - 调度系统 (Agent Scheduler v4.0) =========================================== 整 | 1 | 0 |
| `validator_report.py` | 哨响AI - 验证报告生成器 (Validator Report) ============================================== | 1 | 0 |
| `validator_storage.py` | 哨响AI - 验证数据持久化存储 (Validator Storage) =========================================== | 1 | 0 |

### api/ (1 个模块)

| 文件 | 说明 | 类 | 函数 |
|------|------|-----|------|
| `prediction_service.py` | (无文档字符串) | 0 | 0 |

### data_collector/ (5 个模块)

| 文件 | 说明 | 类 | 函数 |
|------|------|-----|------|
| `api_football_client.py` | 哨响AI - API-Football (RapidAPI) 客户端 v1.0 ======================================== | 1 | 1 |
| `main.py` | 哨响AI - 数据采集模块 仅使用 Football-Data.org API（真实数据） ⛔ 死命令：模拟数据功能已永久禁用 包含: 比赛、球队、积分榜、赛程 | 2 | 0 |
| `odds_history_collector.py` | 哨响AI - 赔率历史采集器 复用 Football-Data.org API 采集赔率快照，构建赔率时间序列 支撑 sigma_trap (异常波动率) 特征 | 1 | 1 |
| `the_odds_client.py` | 哨响AI - The Odds API 客户端 v1.0 ================================= 专业赔率聚合API: 跨博彩公司赔 | 1 | 2 |
| `weather_collector.py` | 哨响AI - 天气数据采集器 (Open-Meteo) 免费 API，无需 Key，无速率限制 基于经纬度查询历史/未来天气，注入特征管道 | 1 | 2 |

### optimize/ (31 个模块)

| 文件 | 说明 | 类 | 函数 |
|------|------|-----|------|
| `arbitration.py` | 哨响AI — T07 仲裁逻辑模块 (Arbitration Engine) ========================================= | 9 | 2 |
| `calibration.py` | 哨响AI - 概率校准模块 (T15) ============================== 实现多种概率校准方法，包括 Platt Scaling ( | 6 | 7 |
| `calibration_viz.py` | 哨响AI - 校准可视化与ECE监控 (T16) ===================================== 实现概率校准的可靠性图可视化、EC | 4 | 1 |
| `catboost_trainer.py` | 哨响AI - CatBoost 模型训练器 (T08) =================================== 基于 CatBoost 梯度提升 | 1 | 0 |
| `confidence_compare.py` | 哨响AI — T06 置信度对比模块 (Confidence Comparator) ===================================== | 2 | 0 |
| `data_augmentation.py` | 哨响AI - 数据增强流水线 (T10) ============================== 半监督学习：利用赔率数据生成伪标签，扩展训练数据集。   | 8 | 1 |
| `dl_models.py` | 哨响AI - 深度学习序列模型 (T09) — 增强版 ========================================= 双塔架构: 主队序列 | 16 | 4 |
| `dl_trainer.py` | 哨响AI - 深度学习训练器 (T09) — 增强版 ====================================== 时序交叉验证 + 早停 +  | 4 | 2 |
| `dynamic_weights.py` | 哨响AI — T03 动态权重系统 ================================ 基于比赛上下文特征实时调整 XGBoost / Ridge | 2 | 2 |
| `elo_ratings.py` | ╔══════════════════════════════════════════════════════════════╗ ║  T04 — ELO 评级 | 1 | 2 |
| `expert_vote.py` | 哨响AI — T06 专家投票通道集成器 (Expert Vote Integrator) ================================== | 3 | 0 |
| `feature_backfiller.py` | 哨响AI - 特征回填引擎 (T20) =========================== 从现有数据源推算5个高默认率特征的真实值，并回填到 DB。  问 | 1 | 1 |
| `feature_mapper.py` | 哨响AI - 特征映射兼容层 (T19) ================================ 解决多代特征体系之间的映射、默认值填充、质量验证问题 | 6 | 2 |
| `gbdt_adapter.py` | 哨响AI - GBDT 数据格式适配层 (T08) =================================== 为 XGBoost / LightG | 2 | 1 |
| `injury_index.py` | 哨响AI - 全队伤病评估模块 (T13) =================================== 将 goalkeeper_model 的3因 | 4 | 2 |
| `league_evaluator.py` | 哨响AI - 联赛差异化评估模块 (T18) =================================== 按联赛划分测试集，计算各联赛专属指标，识别 | 6 | 1 |
| `lightgbm_trainer.py` | 哨响AI - LightGBM 模型训练器 (T08) =================================== 基于 LightGBM 梯度提升 | 1 | 0 |
| `market_features.py` | 哨响AI - 市场特征提取器 (T14) ================================ 从赔率数据中提取市场级特征，包括隐含概率、赔率变动率 | 5 | 8 |
| `model_comparison.py` | 哨响AI - GBDT 三模型对比评估框架 (T08) ======================================== 在同一份数据上训练 X | 3 | 1 |
| `model_registry.py` | 哨响AI - 模型注册表 v2.0 ====================== 跟踪所有已训练的模型版本及其评估指标， 支持语义化版本、模型哈希校验、版本对比 | 1 | 1 |
| `phased_optimization.py` | 哨响AI - 四阶段模型优化系统 v1.0 =================================== 严格按照足球预测最佳实践，分阶段提升模型：  | 0 | 11 |
| `phased_optimization_v2.py` | 哨响AI - 四阶段模型优化系统 v2.0 =================================== 深度融合「失准数据分析」形成持续迭代闭环：  | 0 | 20 |
| `poisson_predictor.py` | 哨响AI — 泊松分布比分预测模块 (T05) ======================================  将足球赛果概率 (H/D/A)  | 1 | 4 |
| `rolling_features.py` | 哨响AI - 滚动窗口特征生成器 (T12) ==================================== 从比赛历史数据计算多窗口滚动统计特征，丰 | 4 | 3 |
| `score_prediction.py` | 哨响AI — 智能比分预测引擎 (Score Prediction Engine) ====================================== | 1 | 2 |
| `sequence_features.py` | 哨响AI - 序列特征提取器 (T09) — 增强版 ====================================== 从数据库加载比赛历史，为每场 | 2 | 2 |
| `train_optimizer.py` | 哨响AI - Optuna超参数优化器 v1.0 ============================== 基于贝叶斯优化的超参数搜索，替代原有 Param | 1 | 2 |
| `transfer_learning.py` | 哨响AI - 迁移学习框架 (T11) ============================ 1. 大型联赛预训练 (Premier League / 全联 | 6 | 2 |
| `walkforward_backtest.py` | 哨响AI - 滚动窗口回测框架 (T17) =================================== 实现 walk-forward 验证策略、按 | 7 | 2 |
| `weight_optimizer.py` | 哨响AI - 集成权重优化器 (T02) ================================ 1. 时间序列验证集划分策略（防未来信息泄露） 2. | 1 | 0 |
| `xg_generator.py` | 哨响AI — 动态预期进球生成器 (xG Generator) ============================================= 替代 | 1 | 2 |

---

## 详细文档

### `agents\base_agent.py`

> 哨响AI - 智能体基类 (Expert Agent Base)
==========================================
所有专业化智能体的基类，提供:
- 统一的预测接口 predict()
- 置信度计算
- 超时保护
- 降级策略
- 性能追踪

**类**:
- `ExpertAgent` — 专业化智能体基类

---

### `agents\commander_validator.py`

> 哨响AI - 指挥官验证引擎 (Commander Validator)
==================================================
核心验证模块：将指挥官智能体的模块选择决策与"预期标准"进行比对，
计算精确率/召回率/F1，分类误判/漏判，支持异步高并发批量验证。

验证逻辑：
- 以每个专家模块为一个"标签"（二分类：应选/不应选）
- 指挥官的 selected_modules 是"预测正类"
- 基于赛后实际结果推导的 expected_modules 是"真实正类"
- TP: 指挥官选了，且该专家实际预测正确
- FP: 指挥官选了，但该专家实际预测错误
- FN: 指挥官没选，但该专家实际预测正确（漏判）
- TN: 指挥官没选，该专家实际预测也错

**类**:
- `CommanderValidator` — 指挥官决策验证器

使用方式:
    validator = CommanderValidator()

    # 单个验证
    result = validator.validate_single(
        selecte

---

### `agents\demo.py`

> 哨响AI — 智能体系统完整演示
=============================
演示三层架构的完整流程：预测 → 反馈学习 → 第二轮预测

**函数**:
- `print_section()` — (无文档)
- `print_prediction()` — 格式化打印预测结果

---

### `agents\demo_validator.py`

> 哨响AI - 指挥官验证模块完整演示
=================================
验证功能：
  1) 单个验证 → 精确率/召回率/F1 + 误判漏判分类
  2) 异步批量验证（高并发）
  3) 持久化存储 + 阈值预警
  4) 结构化报告生成（含图表）
  5) 集成到 AgentOrchestrator 自动追踪

**函数**:
- `print_separator()` — (无文档)
- `simulate_match()` — 构造模拟比赛数据

---

### `agents\ensemble_agent.py`

> 哨响AI - 整合智能体 (Ensemble Agent)
=======================================
第三层智能体：融合各专家预测，加权投票，生成最终预测。

**类**:
- `EnsembleAgent` — 整合智能体：融合全部分析模块的预测 (v4.0: 全专家参与，取消选择器)

---

### `agents\expert_calibrator.py`

> 哨响AI — 专家校准器 (Expert Calibrator) v2.0
==============================================
为每个规则型专家训练概率校准器, 将原始规则输出
映射为经过历史数据验证的校准概率。

核心原理:
    1. 回溯所有历史比赛, 记录每个专家的原始预测概率
    2. 以实际结果为标签, 训练 LogisticRegression 校准器
    3. 校准器学习修正系统性偏差 (如某专家总是高估主胜)

用法:
    calibrator = ExpertCalibrator('trend_analyzer')
    calibrator.run_all_matches(db)           # 收集原始预测 vs 真实结果
    calibrator.train()                       # 训练校准器
    calibrated_probs = calibrator.predict(raw_probs)  # 校准单场
    calibrator.save('calibrators/trend_v2.joblib')

**类**:
- `ExpertCalibrator` — 通用专家校准器 — 为任意规则型专家训练概率映射

方法:
    collect_predictions(db) → 回溯历史, 收集原始预测向量 + 真实标签
    train(method='logistic') → 训练校准模型


**函数**:
- `train_all_calibrators()` — 为所有(或指定)专家批量训练校准器

Returns:
    {expert_name: training_results, ...}

---

### `agents\expert_model_trainer.py`

> 哨响AI — 专家专属模型训练器 v2.0
================================
为每个专家训练专属ML模型，使用领域特征子集 + 全量18k历史数据。

与 ExpertCalibrator 不同:
    Calibrator: 校准专家原始规则输出的概率
    ModelTrainer: 用领域特征直接训练分类器, 完全替代规则

每个专家的特征子集:
    trend_analyzer:    排名/形态/交锋 (rank_diff,form_momentum,h2h_factor...)
    quant_trader:      赔率/盘口 (sigma_trap,beta_dev,lambda_crush,a1-a6...)
    referee_model:     纪律/对抗 (card_risk,press_intensity...)
    coach_tactics:     战术/身体 (aerial_advantage,press_intensity,delta_fatigue...)
    upset_detector:    实力差距/波动 (sigma_trap,a1,rank_diff_factor...)
    media_intelligence: 舆情 (epsilon_senti,discussion_growth,news_impact...)
    timespace_detector: 时空 (time_suppression,delta_fatigue...)
    arbitrage_detector: 套利 (arbitrage_index,arbitrage_window,sigma_trap...)
    goal_timing:        节奏 (a3,a4,a5,a6...)
    alpha_decision:     全部特征 (综合)

**类**:
- `ExpertModelTrainer` — 专家专属模型训练器

为每个专家:
    1. 从数据库加载特征子集 X + 标签 y
    2. 标准化
    3. 训练 LogisticRegression (小模型, 不易过拟合)
    4. 评估
    5. 保存

**函数**:
- `train_all_expert_models()` — 批量训练所有专家专属模型

---

### `agents\expert_selector.py`

> ⚠️ 已弃用(v4.0) — ExpertSelector 专家选择器
============================================
v4.0 起全专家始终参与预测，不再使用选择器过滤。
此文件保留以兼容旧脚本和模型文件加载。

原功能:
    1. 为每个专家训练 XGBoost/LGBM 二分类器
    2. 预测每个专家在特定比赛中的可靠性
    3. 注入到 EnsembleAgent 进行上下文感知权重调整

迁移: 直接使用 EnsembleAgent.integrate_predictions()，所有专家自动参与。

**类**:
- `ExpertSelector` — 专家选择器 — 学习"在什么比赛上下文中, 哪些专家最可靠"

训练目标:
    对每场比赛和每个专家, 训练一个二分类模型:
    输入 = 比赛元特征向量 (20维)
    输出 = 该专家是否会预测正确? (0/1)

预测:


**函数**:
- `prepare_training_data_from_db()` — 从数据库一站式生成 ExpertSelector 训练数据 (v2.0: 使用ML模型快速推理)

流程:
    1. 加载所有已完成比赛的 match_features
    2. 使用训练好的 ExpertModelTrainer 

---

### `agents\expert_v2.py`

> 哨响AI — 增强版专家包装器 (ExpertV2) v2.0
===========================================
在原始规则专家之上, 自动注入概率校准器, 输出校准后概率。

用法:
    expert_v2 = ExpertV2('trend_analyzer')
    expert_v2.ensure_calibrator('calibrators/')  # 确保校准器已加载
    result = expert_v2.predict(match_data, context)

**类**:
- `ExpertV2` — 增强版专家 — 自动集成校准器的专家包装器

对比原始专家:
    原始: raw_probs = expert._run_analysis(data)
        → 硬编码规则, 输出接近均匀分布

    V2:   raw_p
- `ExpertV2Factory` — ExpertV2 工厂 — 批量创建增强版专家

---

### `agents\learning_agent.py`

> 哨响AI - 学习智能体 (Learning Agent)
=======================================
从预测结果中持续学习，优化调度策略和权重分配。

**类**:
- `LearningAgent` — 学习智能体：追踪预测表现，优化调度策略

---

### `agents\meta_feature_extractor.py`

> 哨响AI — 元特征提取器 (Meta Feature Extractor) v2.0
===================================================
从比赛原始特征中提取"元特征"，用于 ExpertSelector 训练。

元特征 = 比赛上下文 + 专家预测特征，让集成器能够:
    1. 理解哪些类型的比赛适合哪些专家
    2. 动态调整专家权重
    3. 识别专家预测一致性/分歧度

**类**:
- `MetaFeatureExtractor` — 元特征提取器 — 为 ExpertSelector 提供上下文感知输入

两类元特征:
    A. 比赛上下文特征 (match_*): 从 match_data / match_features 提取
    B. 专家交互特征 (ex

**函数**:
- `extract_meta_features_for_training()` — 批量提取元特征 (用于训练 ExpertSelector)

Returns:
    shape (n_matches, 20) numpy array

---

### `agents\orchestrator.py`

> 哨响AI - 指挥官智能体 (Orchestrator Agent)
=============================================
第一层智能体：分析比赛特征，决定调用哪些模块。
为每个模块计算适用性分数，动态选择最适合的分析专家。

**类**:
- `MatchAnalyzerAgent` — 指挥官智能体：分析比赛特征，分配任务给专业智能体

---

### `agents\scheduler.py`

> 哨响AI - 调度系统 (Agent Scheduler v4.0)
===========================================
整个智能体系统的统一入口。

v4.0 变更:
    - 取消专家选择器：全部10专家始终参与预测
    - 整合器返回全部专家贡献数据
    - 简化为单一架构路径

v2.0 (保留兼容):
    - ExpertHub 集成: 可插拔多专家架构
    - 渐进式优化: COLD_START → TRAINING → ACTIVE

架构:
    指挥官分析 → 全部专家并行执行 → 整合器加权融合 → 学习反馈

使用方式:
    orchestrator = AgentOrchestrator()
    result = orchestrator.predict(match_data)

**类**:
- `AgentOrchestrator` — 智能体调度器：整个智能体系统的统一入口

v2.0 新增:
    - use_hub=True 启用 ExpertHub (可插拔架构)
    - use_hub=False 保留旧版三层架构 (向后兼容)
    - register

---

### `agents\validator_report.py`

> 哨响AI - 验证报告生成器 (Validator Report)
==============================================
基于验证数据生成结构化报告，包含：
- 准确率趋势图（宏观F1/精确率/召回率）
- 各专家性能雷达图
- 错误类型分布饼图
- 场景准确率对比柱状图
- 低准确率场景详细分析
- 告警摘要

**类**:
- `ValidatorReport` — 验证报告生成器

---

### `agents\validator_storage.py`

> 哨响AI - 验证数据持久化存储 (Validator Storage)
===================================================
SQLite 持久化层：存储指挥官决策验证记录、错误案例、趋势快照。
支持高并发写入（WAL模式）、按时间/场景索引查询。

**类**:
- `ValidatorStorage` — 验证数据 SQLite 持久化存储

---

### `api\prediction_service.py`

> (无文档字符串)

---

### `data_collector\api_football_client.py`

> 哨响AI - API-Football (RapidAPI) 客户端 v1.0
=============================================
数据覆盖: 170+联赛, 球员伤病/阵容/统计数据/赔率
免费额度: 100 req/day (RapidAPI免费层)
API文档: https://www.api-football.com/documentation-v3

核心能力:
- /fixtures/lineups — 首发阵容
- /players/sidelined — 伤病/禁赛球员 (按球队/联赛)
- /teams/statistics — 球队赛季统计数据
- /fixtures/events — 比赛事件(进球/黄牌/换人)
- /odds — 赔率数据
- /fixtures/headtohead — 历史交锋

**类**:
- `ApiFootballCollector` — API-Football (RapidAPI) 数据采集器

**函数**:
- `get_api_football_collector()` — 获取 API-Football 采集器实例

---

### `data_collector\main.py`

> 哨响AI - 数据采集模块
仅使用 Football-Data.org API（真实数据）
⛔ 死命令：模拟数据功能已永久禁用
包含: 比赛、球队、积分榜、赛程、实时比分、竞赛列表、API测试
v2.1: 移除模拟数据，全部走真实API

**类**:
- `APICache` — 简单的内存缓存，带TTL过期机制
- `FootballDataCollector` — Football-Data.org 数据采集器 v2.0

---

### `data_collector\odds_history_collector.py`

> 哨响AI - 赔率历史采集器
复用 Football-Data.org API 采集赔率快照，构建赔率时间序列
支撑 sigma_trap (异常波动率) 特征计算

**类**:
- `OddsHistoryCollector` — 赔率历史采集器 — 从现有 Football-Data.org 采集器拉取并追踪赔率变化

**函数**:
- `seed_odds_history_from_odds()` — 从现有 odds 表快照补偿 odds_history (向后兼容)
为已有赔率的比赛创建至少一条历史记录

---

### `data_collector\the_odds_client.py`

> 哨响AI - The Odds API 客户端 v1.0
=================================
专业赔率聚合API: 跨博彩公司赔率对比、赔率走势历史、套利检测
免费额度: 500 req/month
API文档: https://the-odds-api.com/liveapi/guides/v4/

核心能力:
- /sports/{sport}/odds — 当前赔率 (含多家博彩公司)
- /sports/{sport}/odds-history — 历史赔率走势
- /sports — 支持的运动/联赛列表

**类**:
- `TheOddsCollector` — The Odds API 赔率数据采集器

**函数**:
- `similar_team_name()` — 球队名模糊匹配（去掉 FC, AFC 等后缀后比较）
- `get_odds_collector()` — 获取 The Odds API 采集器实例

---

### `data_collector\weather_collector.py`

> 哨响AI - 天气数据采集器 (Open-Meteo)
免费 API，无需 Key，无速率限制
基于经纬度查询历史/未来天气，注入特征管道

**类**:
- `WeatherCollector` — Open-Meteo 天气数据采集器

**函数**:
- `get_weather_for_match()` — 为一场比赛获取天气
- `get_stadium_coords()` — 从数据库读取球队→球场坐标映射

---

### `optimize\arbitration.py`

> 哨响AI — T07 仲裁逻辑模块 (Arbitration Engine)
==================================================
当集成模型与专家投票产生矛盾时，介入仲裁并输出最终裁决。

设计:
  1. 不一致规则定义: 5种冲突场景分类 + 触发条件
  2. 加权投票仲裁: 集成模型 + 各专家 + 赔率隐含概率 → 加权聚合
  3. 元学习仲裁: 基于历史场景相似度的信任权重学习
  4. 综合裁决: 输出仲裁后概率 + 置信度 + 裁决理由

用法:
    engine = ArbitrationEngine(db_path='data/football_data.db')
    result = engine.arbitrate(
        ensemble_probs={'home': 0.38, 'draw': 0.22, 'away': 0.40},
        ensemble_conf=0.72,
        expert_vote=expert_vote_result,
        confidence_comparison=confidence_comparison,
        odds={'home': 2.5, 'draw': 3.2, 'away': 2.8},
        scenario={'sigma_trap': 0.15, 'rank_diff': 0.8, 'league': '英超'},
    )

**类**:
- `ConflictType` — 不一致场景枚举
- `ConflictSeverity` — 冲突严重程度
- `ConflictAssessment` — 不一致评估结果
- `DecisionPolicy` — 最终采用的决策策略
- `DecisionBeacon` — 决策信标：仲裁生成的信号建议
- `InconsistencyRules` — T07-R1: 不一致情况处理规则定义。

当集成模型与专家投票产生分歧时，本模块负责：
  1. 分类冲突类型（5种场景）
  2. 评定冲突严重程度
  3. 判定是否可自动仲裁
- `WeightedVoteArbiter` — T07-R2: 加权投票仲裁算法。

将集成模型视为一个"超级投票者"，与各专家投票一起进行加权聚合。
额外引入赔率隐含概率作为基准锚。

算法:
  1. 收集所有投票源: 集成模型 + 各专家ballots + 赔率隐含
  2. 对每
- `MetaLearningArbiter` — T07-R2: 元学习仲裁算法。

基于历史预测准确率，为不同"场景"学习集成模型 vs 专家投票的信任权重。

场景特征:
  - sigma_trap (赔率诱空/诱多信号)
  - rank_diff_factor (排名差距)
  
- `ArbitrationEngine` — T07: 仲裁引擎主模块。

整合不一致规则检测 + 加权投票 + 元学习，输出最终决策信标。

调用流程:
  1. InconsistencyRules.assess() → 冲突评估
  2. 无冲突 → 直接使用融合结果 (跳过仲裁

**函数**:
- `get_arbitration_engine()` — 全局懒加载仲裁引擎
- `quick_arbitrate()` — 快速仲裁（静态方法）

---

### `optimize\calibration.py`

> 哨响AI - 概率校准模块 (T15)
==============================
实现多种概率校准方法，包括 Platt Scaling (温度缩放 + 逻辑回归)、
Isotonic Regression、Beta Calibration，提供统一对比框架和稀疏数据适应性。

核心设计:
  1. Platt Scaling — 逻辑回归校准 (经典 sigmoid 映射)
  2. Temperature Scaling — 单参数温度缩放 (深度学习常用)
  3. Isotonic Regression — 非参数单调校准
  4. Beta Calibration — 三参数 Beta 分布校准
  5. 多方法对比框架 — Brier Score / ECE / Log Loss / 可靠性图
  6. 稀疏数据适应 — 正则化 + 交叉验证 + 最小样本检测

输出:
  - 校准后概率 (home/draw/away)
  - 校准评估报告 (对比表 + 可靠性数据)
  - 持久化校准器 (joblib)

用法:
    from optimize.calibration import CalibratorSuite
    suite = CalibratorSuite()
    suite.fit(y_true, raw_probs)
    report = suite.compare()
    calibrated = suite.predict(raw_probs, method='platt')

**类**:
- `PlattScaler` — Platt Scaling — 经典逻辑回归校准

原理:
    对每个类别 c, 训练二分类 LogisticRegression:
    P(y=c|x) = 1 / (1 + exp(A * s_c + B))
    其中 s_
- `TemperatureScaler` — Temperature Scaling — 单参数温度缩放

原理:
    P_cal = softmax(logits / T)
    T > 1 → 概率更平滑 (过度自信修正)
    T < 1 → 概率更尖锐 (欠自信修正)

- `IsotonicScaler` — Isotonic Regression — 非参数单调校准

原理:
    对每个类别, 将原始概率映射为单调递增的校准概率
    适合: 大数据集, 非线性校准曲线

注意: 小样本 (<300) 容易过拟合, 建议使用 Platt/
- `BetaScaler` — Beta Calibration — 三参数 Beta 分布校准

原理:
    P_cal = F_Beta(a * x^c / (a * x^c + b * (1-x)^c))
    三参数 (a, b, c) 比 Platt (2
- `CalibrationReport` — 校准评估报告
- `CalibratorSuite` — 校准套件 — 多方法训练 + 对比 + 选择

用法:
    suite = CalibratorSuite()
    suite.fit(y_true, raw_probs)
    report = suite.compare()


**函数**:
- `compute_ece()` — Expected Calibration Error (ECE)
ECE = Σ (n_b/N) |acc_b - conf_b|
- `compute_mce()` — Maximum Calibration Error (MCE)
- `compute_reliability()` — 计算可靠性图数据
- `multiclass_brier()` — 多分类 Brier Score
BS = (1/N) Σ Σ (p_ij - y_ij)^2
- `calibrate_predictions()` — 快捷校准
- `compare_calibrators()` — 快捷对比
- `upgrade_expert_calibrator()` — 为 ExpertCalibrator 提供 T15 增强对比

在 ExpertCalibrator 基础上, 使用 CalibratorSuite 做全方法对比,
自动选择最优校准方法。

Args:
    expert_name: 专

---

### `optimize\calibration_viz.py`

> 哨响AI - 校准可视化与ECE监控 (T16)
=====================================
实现概率校准的可靠性图可视化、ECE 追踪监控、多方法对比图表，
以及 HTML 评估报告生成。

核心组件:
  1. CalibrationVisualizer — 校准可视化工具
     - reliability_diagram: 可靠性图 (per-class + overall)
     - before_after_comparison: 校准前后对比图
     - multi_method_comparison: 多方法对比柱状图
     - confidence_histogram: 置信度分布直方图
     - class_wise_reliability: 分类别可靠性图
  2. ECEMonitor — ECE 时间序列监控
     - track: 记录评估点
     - trend_chart: 趋势图
     - alert_check: 异常检测
  3. CalibrationReportBuilder — HTML 评估报告生成
     - 从 CalibratorSuite / ExpertCalibrator 生成完整报告
     - 嵌入图表 + 表格 + 建议

依赖:
  - matplotlib (Agg 后端, 无 GUI)
  - T15 calibration.py 的 compute_ece / compute_reliability / CalibratorSuite

用法:
    from optimize.calibration_viz import CalibrationVisualizer
    viz = CalibrationVisualizer(output_dir='reports/calibration')
    viz.reliability_diagram(y_true, raw_probs, calibrated_probs)
    viz.before_after_comparison(suite)
    viz.generate_html_report(suite)

**类**:
- `CalibrationVisualizer` — 校准可视化工具 — 生成可靠性图、ECE 监控、对比图

所有图表保存为 PNG, 返回文件路径。
- `ECETrackPoint` — ECE 追踪点
- `ECEMonitor` — ECE 时间序列监控器

用法:
    monitor = ECEMonitor()
    monitor.track(y_true, probs, method='platt')
    monitor.track(y_true, p
- `CalibrationReportBuilder` — 校准评估 HTML 报告生成器

用法:
    builder = CalibrationReportBuilder(output_dir='reports/calibration')
    builder.generate(suite

**函数**:
- `visualize_expert_calibration()` — 为指定专家生成校准可视化

Args:
    expert_name: 专家名
    db_path: 数据库路径
    output_dir: 输出目录

Returns:
    {chart_name: file_path}

---

### `optimize\catboost_trainer.py`

> 哨响AI - CatBoost 模型训练器 (T08)
===================================
基于 CatBoost 梯度提升决策树，产出多分类概率预测。

特性：
- 与 XGBoost 共享统一 GBDTDataAdapter 数据格式
- Ordered Target Encoding 天然防过拟合
- Symmetric Trees 快速推理
- 内置 Early Stopping + Overfitting Detector
- 概率校准 (Isotonic)
- Joblib 持久化

用法:
    trainer = CatBoostTrainer()
    bundle = make_training_bundle()
    eval_result = trainer.train(bundle)
    proba = trainer.predict_proba(X_test)
    trainer.save_model('catboost_v1.joblib')

**类**:
- `CatBoostTrainer` — CatBoost 足球预测模型训练器。
对外 API 与 XGBoost / LightGBM 端统一。

---

### `optimize\confidence_compare.py`

> 哨响AI — T06 置信度对比模块 (Confidence Comparator)
======================================================
对集成模型预测与专家投票两套系统的置信度进行对比分析。

设计:
  1. 预测向量对齐: 余弦相似度测量两个系统预测方向的一致性
  2. 置信度交叉验证: 两个置信度值的加权融合
  3. 分歧检测: 超过阈值则标记 CONFLICT
  4. 综合评级: HIGH_AGREE / MODERATE / LOW_CONFIDENCE / CONFLICT
  5. 建议输出: 根据对比结果给出操作建议

用法:
    comparator = ConfidenceComparator()
    result = comparator.compare(
        ensemble_probs={'home': 0.45, 'draw': 0.28, 'away': 0.27},
        ensemble_conf=0.72,
        expert_probs={'home': 0.50, 'draw': 0.25, 'away': 0.25},
        expert_conf=0.81,
    )

**类**:
- `ConfidenceComparison` — 置信度对比分析结果
- `ConfidenceComparator` — 置信度对比器。

对比集成模型 (EnsembleTrainer) 和专家投票 (ExpertVoteIntegrator)
两套系统的预测结果与置信度，生成综合分析报告。

核心算法:
  1. 余弦相似度 → prediction_si

---

### `optimize\data_augmentation.py`

> 哨响AI - 数据增强流水线 (T10)
==============================
半监督学习：利用赔率数据生成伪标签，扩展训练数据集。

核心组件:
  1. OddsPseudoLabeler   - 从赔率隐含概率生成伪标签 + 置信度评分
  2. ConfidenceFilter    - 多维度置信度筛选阈值策略
  3. DataAugmentor       - 序列级数据增强 (噪声注入/特征扰动/时序裁剪)
  4. AugmentationPipeline - 一站式流水线: 伪标签 + 增强 + 合并

设计思路:
  - 赔率隐含概率反映市场共识，可信度高于随机预测
  - 置信度 = 综合赔率优势 × 返还率 × 隐含概率方差
  - 伪标签用于扩展训练集，真标签数据保持不变
  - 数据增强仅作用于序列特征，不改变静态特征语义

**类**:
- `PseudoLabeledSample` — 一条伪标签样本
- `OddsPseudoLabeler` — 从赔率隐含概率生成伪标签。

策略:
  - 将赔率转换为隐含概率: p_i = 1/odds_i / sum(1/odds_j)
  - 取最大概率方向为伪标签
  - 计算综合置信度分数 (见 ConfidenceScore)

数据来
- `FilterConfig` — 置信度筛选配置
- `ConfidenceFilter` — 多维度置信度筛选策略。

筛选规则:
  1. 全局最低置信度过滤
  2. 各类别分别阈值 (平局更严格)
  3. 返还率过滤
  4. 总量控制 (不超过训练集的 max_pseudo_ratio)
  5. 类别平衡 (避免伪标签加
- `SequenceAugmentor` — 序列级数据增强策略。

方法:
  1. Gaussian Noise    - 高斯噪声注入序列特征
  2. Feature Dropout    - 随机置零部分特征维度
  3. Temporal Crop      - 随机裁剪序
- `PseudoFeatureBuilder` — 为伪标签样本构建序列特征和静态特征。

利用赔率信息构造:
  - 静态特征: 赔率隐含概率 + 赔率差异 + 返还率 + 原match_features
  - 序列特征: 与真标签样本相同的方式从历史构建

对于没有历史数据的比赛, 使
- `AugmentationConfig` — 数据增强流水线配置
- `AugmentationPipeline` — 一站式数据增强流水线。

流程:
  1. 加载已有真标签的 SequenceBundle
  2. 从赔率数据生成伪标签
  3. 置信度筛选
  4. 构建伪标签样本特征
  5. 序列级数据增强 (噪声/dropout/swap/cr

**函数**:
- `augment_training_data()` — 一站式数据增强便捷函数。

Args:
    bundle: 原始 SequenceBundle (None 则从数据库提取)
    db_path: 数据库路径
    min_confidence: 伪标签最低置信度
    max

---

### `optimize\dl_models.py`

> 哨响AI - 深度学习序列模型 (T09) — 增强版
=========================================
双塔架构: 主队序列塔 + 客队序列塔 + 静态特征塔 → 拼接 → 全连接 → 3分类

模型:
  1. MatchGRU       - 双向 GRU + Attention 处理时序序列
  2. MatchCNN       - 1D 多尺度卷积处理序列模式
  3. MatchTransformer - 自注意力 Transformer 编码器
  4. MatchEnsemble  - GRU + CNN + Transformer 联合推理

增强:
  - 序列掩码 (padding mask)
  - 多头注意力
  - Focal Loss (类别不平衡)
  - 标签平滑
  - 特征交互层 (主客队差异编码)

用法:
    from optimize.dl_models import MatchGRU, MatchCNN, MatchTransformer, MatchEnsemble
    model = MatchGRU(seq_feat_dim=18, static_dim=19)

**类**:
- `ResidualFC` — 带残差连接的全连接块
- `SequenceEncoder` — 序列编码器基类：将 (B, L, D) 序列编码为 (B, H) 向量
- `GRUEncoder` — 双向 GRU 序列编码器。
(B, L, D) → GRU → concat(last_forward, last_backward) → (B, 2*H)
- `CNNEncoder` — 多层 1D 卷积 + Global Max Pooling 编码器。
(B, L, D) → Conv1d×N → GMP → (B, C)
- `AttentionPooling` — 自注意力池化: 学习序列各时间步的权重
- `AttnGRUEncoder` — GRU + Attention Pooling (替代 last state)
- `MatchGRU` — GRU 双塔预测模型。

架构:
  home_seq ──► GRUEncoder ──┐
                            ├── concat ──► FC ──► softmax(3)
  away_seq ─
- `MatchCNN` — 1D CNN 双塔预测模型。

架构:
  home_seq ──► CNNEncoder ──┐
                            ├── concat ──► FC ──► softmax(3)
  away_se
- `TransformerEncoder` — 自注意力 Transformer 编码器。
(B, L, D) → PositionalEncoding → TransformerEncoderLayer×N → AttentionPool → (B, H)

支持 padding ma
- `InteractionLayer` — 主客队编码差异交互。
计算 home - away, home * away, |home - away| 等交互特征。
- `MatchTransformer` — Transformer 双塔预测模型。

架构:
  home_seq ──► TransformerEncoder ──┐
                                      ├── InteractionLaye
- `MatchEnsemble` — GRU + CNN + Transformer 三塔融合模型。

架构:
  home_seq ──┬──► GRUEncoder ──► h_gru ──┐
             ├──► CNNEncoder  ──► h_cnn 
- `FocalLoss` — Focal Loss: 解决类别不平衡问题。
FL(p_t) = -alpha_t * (1 - p_t)^gamma * log(p_t)

gamma > 0 减少易分类样本的权重，关注难分类样本。
alpha 平衡各类别的权重。
- `LabelSmoothingCrossEntropy` — 标签平滑交叉熵。
防止模型过度自信，提高泛化能力。
- `_StubClass` — 万能占位类：构造和属性访问均返回自身
- `_StubModule` — torch.nn.Module 占位基类

**函数**:
- `save_dl_model()` — 保存 PyTorch 模型 (state_dict + 元数据)
- `load_dl_model()` — 加载 PyTorch 模型 (支持所有模型类型)
- `is_torch_available()` — (无文档)
- `get_device()` — (无文档)

---

### `optimize\dl_trainer.py`

> 哨响AI - 深度学习训练器 (T09) — 增强版
======================================
时序交叉验证 + 早停 + 学习率调度 + 分类权重平衡

增强:
  - Mixup 数据增强 (序列数据适配)
  - SWA (Stochastic Weight Averaging)
  - Focal Loss / Label Smoothing 支持
  - 序列掩码支持
  - 概率校准评估 (ECE, 可靠性图)
  - 多模型对比 (GRU / CNN / Transformer / Ensemble)
  - 超参数搜索辅助

**类**:
- `DLTrainConfig` — 深度学习训练超参数
- `DLTrainer` — 深度学习训练器 — 增强版。

封装训练/验证/测试完整流程，支持:
- 早停和 LR 调度
- Mixup 数据增强
- SWA 权重平均
- Focal Loss / Label Smoothing
- 序列掩码
- 概率校准 (ECE
- `_StubClass` — (无文档)
- `_StubModule` — (无文档)

**函数**:
- `create_trainer()` — 创建训练器 (带默认配置)
- `train_and_compare()` — 一站式训练+对比：提取序列 → 训练多种模型 → 返回对比结果。

Args:
    models: 要训练的模型列表, 可选 ['gru', 'cnn', 'transformer', 'ensemble']
            默

---

### `optimize\dynamic_weights.py`

> 哨响AI — T03 动态权重系统
================================
基于比赛上下文特征实时调整 XGBoost / Ridge / Heuristic 集成权重。

设计思路:
  - 静态权重 = 全局最优（Optuna 搜索）
  - 动态调整 = 在静态权重基础上，根据比赛特征施加 ± 修正
  - 最终权重 = normalize(w_base × (1 + Σ adjustment_factors))
  - 严格裁剪 + 归一化，防止极端情况

调整因子:
  1. 盘口波动 (sigma_trap)      — 高波动 → 降低 XGBoost, 提升启发式
  2. 实力差 (rank_diff_factor)   — 实力悬殊 → 提升 XGBoost (数据驱动更准)
  3. 交锋历史 (h2h_factor)       — H2H 明确 → 提升启发式
  4. 盘口偏差 (beta_dev)         — 市场异常 → 降低 Ridge
  5. 模型一致性 (a4)             — 背离时 → 偏向最稳定模型 (XGBoost)
  6. 联赛类型 (league)           — 不同联赛不同基础权重

用法:
    calc = DynamicWeightCalculator(config_dict)
    weights = calc.compute(features, league_name='Premier League')
    # weights = {'xgboost': 0.72, 'ridge': 0.04, 'heuristic': 0.24}

**类**:
- `WeightAdjustment` — 单次权重调整的完整记录
- `DynamicWeightCalculator` — 动态权重计算器

根据比赛上下文特征，在静态最优权重基础上施加调整。

调整算法:
  α_i = Σ (k_j × context_factor_j)  for each model i
  w_i = w_base_i × (1 + c

**函数**:
- `get_calculator()` — 获取全局单例 DynamicWeightCalculator (延迟初始化)
- `reset_calculator()` — 重置全局单例 (用于测试)

---

### `optimize\elo_ratings.py`

> ╔══════════════════════════════════════════════════════════════╗
║  T04 — ELO 评级系统 (Elo Rating System)                      ║
║  v1.0 — 2026-06-01                                           ║
║                                                              ║
║  功能:                                                       ║
║  1. 基础 ELO 计算 (K=32, 主场+100)                            ║
║  2. K 因子动态调整 (联赛/比分差/赛季阶段)                       ║
║  3. 评级 → 胜平负概率映射 (含平局模型)                         ║
║  4. 批量历史回测 + 序列化保存                                  ║
╚══════════════════════════════════════════════════════════════╝

**类**:
- `EloRatingSystem` — 足球 ELO 评级系统。

核心参数 (可从 config.yaml 覆盖):
- k_base: 基础 K 因子 (默认 32)
- home_advantage: ELO 主场加分 (默认 100)
- init_rating: 新球队

**函数**:
- `get_elo_system()` — 获取全局 ELO 实例 (懒加载单例)
- `reset_global_elo()` — 重置全局 ELO 实例

---

### `optimize\expert_vote.py`

> 哨响AI — T06 专家投票通道集成器 (Expert Vote Integrator)
==========================================================
并行调度多个专家智能体进行投票，生成结构化投票结果。

设计:
  - 复用 AgentOrchestrator (Hub 模式) 作为投票引擎
  - 并行执行各专家（内置在 Orchestrator 中），收集投票结果
  - 计算投票一致性、多数意见、加权置信度
  - 自动注入专家性能跟踪 (F1权重 + 上下文规则)

用法:
    integrator = ExpertVoteIntegrator()
    vote_result = integrator.vote(match_data, timeout=8.0)

**类**:
- `ExpertBallot` — 单个专家的投票
- `ExpertVoteResult` — 专家投票综合结果
- `ExpertVoteIntegrator` — 专家投票通道集成器。

功能:
  1. 并行调度 10+ 专家智能体进行投票
  2. 加权融合各专家预测（通过 EnsembleAgent）
  3. 分析投票一致性
  4. 输出结构化投票结果

---

### `optimize\feature_backfiller.py`

> 哨响AI - 特征回填引擎 (T20)
===========================
从现有数据源推算5个高默认率特征的真实值，并回填到 DB。

问题根因:
  pull_historical_data.compute_features() 对5个特征写死默认值:
    - aerial_advantage = 1.0 (默认均势)
    - press_intensity  = 0.0 (默认无逼抢)
    - card_risk        = 0.0 (默认无风险)
    - beta_dev         = 0.0 (默认无盘口偏差)
    - delta_fatigue    = 1.0 (默认无疲劳)

  因为这些特征需要原始数据(头球、逼抢、裁判、亚盘、赛程密度)，
  而 football-data.org 免费 API 不提供这些数据。

解决方案: 从现有数据源推算近似值:
  1. beta_dev:      从赔率隐含差 + 排名差推算理论让球 vs 赔率隐含让球
  2. delta_fatigue: 从赛程密度推算 (同队7天内多赛 → 高疲劳)
  3. aerial_advantage: 从球队历史进球分布估算 (大球率+进球差 → 空中优势)
  4. press_intensity:  从失球+零封率反向推算 (低失球+高零封 → 高逼抢)
  5. card_risk:        从联赛+球队纪律统计推算 (大球率+进球 → 纪律代理)

用法:
    from optimize.feature_backfiller import FeatureBackfiller
    filler = FeatureBackfiller(db_path='data/football_data.db')
    stats = filler.backfill_all()

**类**:
- `FeatureBackfiller` — 特征回填引擎 — 从现有数据推算5个高默认率特征的真实值

**函数**:
- `backfill_features()` — 一键回填5个高默认率特征

---

### `optimize\feature_mapper.py`

> 哨响AI - 特征映射兼容层 (T19)
================================
解决多代特征体系之间的映射、默认值填充、质量验证问题。

核心设计:
  1. 特征映射 — 旧名→新名双向映射, 自动补全
  2. 默认值策略 — 三级策略: 精确默认 > 统计量插补 > 零值
  3. 映射质量验证 — 覆盖率/缺失率/分布偏移检测

特征体系演进:
  V1 (19 base): a1-a6, sigma_trap, lambda_crush, epsilon_senti, ...
  V2 (market):  mkt_* 前缀, 29 个市场特征
  V3 (injury):  7 个伤病特征 (injury_index_*, attack_impact_diff, ...)
  V4 (rolling): rw_* 前缀, ~50 个滚动窗口特征
  V5 (sequence): 18 维序列特征 (DL 模型专用)

用法:
    from optimize.feature_mapper import FeatureMapper
    mapper = FeatureMapper()

    # 映射旧特征名
    new_names = mapper.map_columns(['a1', 'sigma_trap_v1'])

    # 对齐 DataFrame 到目标特征集
    df_aligned = mapper.align_dataframe(df, target='v1_extended')

    # 验证映射质量
    report = mapper.validate(df_aligned)

    # 获取兼容的 DataFrame (用于已训练模型)
    df_compat = mapper.make_compatible(df, model_feature_names=saved_model.feature_names)

**类**:
- `FeatureVersion` — 特征体系版本
- `DefaultStrategy` — 默认值填充策略
- `MappingResult` — 映射结果
- `ValidationResult` — 验证结果
- `AlignmentReport` — 对齐报告
- `FeatureMapper` — 特征映射兼容层

职责:
  1. 旧名→新名映射 (LEGACY_NAME_MAP + 智能推断)
  2. DataFrame 对齐到目标特征集 (缺失列填充, 多余列移除)
  3. 默认值策略选择与填充
  4. 映射质量验证 (覆

**函数**:
- `align_for_model()` — 一站式: 将 DataFrame 对齐到已训练模型的特征列表。

Args:
    df: 源 DataFrame
    model_feature_names: 模型训练时的特征列表
    strategy: 'smart' | '
- `check_feature_alignment()` — 一站式: 检查特征映射质量

---

### `optimize\gbdt_adapter.py`

> 哨响AI - GBDT 数据格式适配层 (T08)
===================================
为 XGBoost / LightGBM / CatBoost 提供统一的数据预处理器。
所有模型共享同一套特征工程 & 数据预处理管线。

职责：
1. 从数据库加载训练数据 (matches JOIN match_features)
2. 特征预处理 (缺失填充 / 异常值裁剪 / 交互项生成)
3. 标签构建 (净胜球 → 3分类)
4. 训练/验证集时序分割
5. 标准化

**类**:
- `TrainingBundle` — 统一训练数据包：三个模型共享相同的 split + scaler
- `GBDTDataAdapter` — 统一的 GBDT 数据适配器。
对外暴露一组 prep_* 方法，各模型只需调用即可获得标准化数据。

**函数**:
- `make_training_bundle()` — 一键创建标准训练数据包

---

### `optimize\injury_index.py`

> 哨响AI - 全队伤病评估模块 (T13)
===================================
将 goalkeeper_model 的3因子模型扩展到全队11个位置，设计伤病影响量化指标，
并集成到特征管道 (match_features + SequenceBundle)。

核心设计:
  1. 位置重要性权重 — GK/CB/CM/ST 各位置缺阵对球队影响不同
  2. 球员质量降级 — 替补 vs 主力 的实力差距
  3. 伤病恢复因子 — 伤愈复出后的状态折扣
  4. 阵容深度指数 — 板凳厚度对抗伤病的能力
  5. 累积伤病指数 — 多人缺阵的非线性叠加效应
  6. 交锋伤病差 — 双方伤病影响的对比

输出特征:
  - 直接写入 match_features 表 (injury_index, squad_depth, injury_impact_diff 等)
  - 可追加到 SequenceBundle.static_features 供 DL 模型使用

数据来源:
  - 显式伤病数据: TeamInjuryReport (手动/API 输入)
  - 隐式代理数据: form_trends 表中出场数异常 / media_intelligence NLP
  - 球队基础数据: teams 表 (rating, attack/defense_strength)

用法:
    from optimize.injury_index import TeamInjuryModel
    model = TeamInjuryModel()
    features = model.compute_match_features(home_team, away_team, match_date)
    # features → dict of injury-related features

**类**:
- `PositionProfile` — 位置画像 — 定义缺阵影响
- `PlayerInjuryInfo` — 球员伤病信息
- `TeamInjuryReport` — 球队伤病报告 — 某场比赛前某队的伤病情况
- `TeamInjuryModel` — 全队伤病评估模型

将 goalkeeper_model 的3因子 (training_load, injury_recovery, pressure)
扩展到全队11个位置，计算：

1. 位置加权缺阵指数 (Position-Weigh

**函数**:
- `compute_injury_features()` — 便捷函数: 计算单场伤病特征

可以直接传入 TeamInjuryReport, 也可以只传球队名+日期 (自动从DB构建)
- `generate_injury_features()` — 便捷函数: 为所有比赛生成伤病特征

---

### `optimize\league_evaluator.py`

> 哨响AI - 联赛差异化评估模块 (T18)
===================================
按联赛划分测试集，计算各联赛专属指标，识别模型薄弱环节并给出针对性改进建议。

核心组件:
  1. LeagueMetrics — 单联赛评估指标数据类
  2. WeakSpot — 薄弱环节识别
  3. LeagueEvaluator — 联赛差异化评估器
     - 接受 BacktestResult + 原始 DataFrame (含 league_name 列)
     - 按联赛分割，计算完整指标集
     - 自动识别薄弱环节 (低于全局均值 + 阈值)
     - 生成针对性改进建议
  4. LeagueEvaluationResult — 评估结果容器
  5. LeagueVisualizer — 联赛级可视化 (6 类图表)
  6. LeagueReportBuilder — 自包含 HTML 报告

依赖:
  - numpy, pandas, matplotlib
  - T15 calibration.py (compute_ece, multiclass_brier)
  - T17 walkforward_backtest.py (BacktestResult, FoldMetrics)

用法:
    from optimize.league_evaluator import LeagueEvaluator

    evaluator = LeagueEvaluator()
    league_result = evaluator.evaluate(backtest_result, df, league_col='league_name')
    league_result.summary()
    league_result.weak_spots
    league_result.suggestions

**类**:
- `LeagueMetrics` — 单联赛评估指标
- `WeakSpot` — 识别的薄弱环节
- `LeagueEvaluator` — 联赛差异化评估器 — 按联赛划分测试集并计算各联赛专属指标。

Parameters
----------
min_samples : int
    每个联赛最少样本数，低于此数的联赛被合并为 "Other"
delta_threshol
- `LeagueEvaluationResult` — 联赛差异化评估结果
- `LeagueVisualizer` — 联赛差异化评估可视化工具。

Parameters
----------
output_dir : str
    图表输出目录
dpi : int
    图表分辨率
- `LeagueReportBuilder` — 联赛差异化评估 HTML 报告生成器。
生成自包含的 HTML 文件 (图表 base64 嵌入)。

**函数**:
- `run_league_evaluation()` — 一键运行联赛差异化评估 + 可视化 + HTML 报告。

Parameters
----------
result : BacktestResult
    T17 回测结果
df : pd.DataFrame
    原始数据 (需含 

---

### `optimize\lightgbm_trainer.py`

> 哨响AI - LightGBM 模型训练器 (T08)
===================================
基于 LightGBM 梯度提升决策树，产出多分类概率预测。

特性：
- 与 XGBoost 共享统一的 GBDTDataAdapter 数据格式
- Early Stopping + Bagging 防过拟合
- 平衡类别权重 + 平局专项优化
- 概率校准 (Isotonic)
- Joblib 持久化

用法:
    trainer = LightGBMTrainer()
    bundle = make_training_bundle()
    eval_result = trainer.train(bundle)
    proba = trainer.predict_proba(X_test)  # → (n, 3)
    trainer.save_model('lightgbm_v1.joblib')

**类**:
- `LightGBMTrainer` — LightGBM 足球预测模型训练器。
对外 API 与 XGBoost 端统一: train() / predict_proba() / predict() / save() / load()

---

### `optimize\market_features.py`

> 哨响AI - 市场特征提取器 (T14)
================================
从赔率数据中提取市场级特征，包括隐含概率、赔率变动率、市场信心指标、
模型-市场分歧度等，集成到特征管道 (match_features + SequenceBundle)。

核心设计:
  1. 隐含概率体系 — 1X2 隐含概率 + 去抽水公平概率
  2. 赔率变动特征 — 开盘→收盘漂移、变动速率、方向一致性
  3. 市场信心指标 — 返还率/抽水率、赔率紧密度、博彩公司分歧
  4. 模型-市场分歧 — 隐含概率 vs 模型预测偏离度
  5. 市场异常信号 — 热门方偏重、赔率突变、套利窗口
  6. 赔率价值评估 — 凯利价值、期望值、价值缺口

输出特征:
  - 直接写入 match_features 表 (mkt_implied_home_prob, mkt_odds_drift 等)
  - 可追加到 SequenceBundle.static_features 供 DL 模型使用

数据来源:
  - odds 表: 1X2 赔率 + 返还率 (retrospective_elo / default)
  - odds_history 表: 赔率时间序列 (开盘/收盘区分)
  - The Odds API: 多博彩公司赔率对比 (需配置 API Key)
  - match_features 表: 已有 sigma_trap / p_implied 等

用法:
    from optimize.market_features import MarketFeatureExtractor
    mfx = MarketFeatureExtractor()
    features = mfx.compute_match_features(match_id=123)
    # features → dict of market features

    # 批量生成并写入
    df = mfx.generate_features_df()
    mfx.write_to_match_features(df)

**类**:
- `OddsSnapshot` — 单时间点赔率快照
- `MultiBookmakerOdds` — 多博彩公司赔率聚合
- `OddsMovement` — 赔率变动特征
- `OddsAPIInterface` — 赔率 API 抽象接口 — 支持多数据源切换
当前实现: DB (从数据库读取)
预留: The Odds API, API-Football
- `MarketFeatureExtractor` — 市场特征提取器 — 从赔率数据提取深层市场信号

特征维度:
  1. 隐含概率体系 (5 features)
  2. 赔率变动特征 (8 features)
  3. 市场信心指标 (4 features)
  4. 模型-市场分歧 (

**函数**:
- `implied_prob()` — 从赔率计算隐含概率 (含返还率修正)
implied = (1/odds) * return_rate
- `fair_prob()` — 去抽水公平概率 — 将3个隐含概率归一化
fair = raw / (raw_h + raw_d + raw_a)
- `overround()` — 抽水率 (overround) = Σ(1/odds) - 1
正值 = 庄家利润; 接近0 = 公平市场; 负值 = 套利可能
- `kelly_value()` — 凯利价值: f* = (bp - q) / b
b = odds - 1 (净赔率), p = model_prob, q = 1 - p
返回: 正值=有价值投注, 负值=无价值
- `expected_value()` — 期望值: EV = p * (odds - 1) - (1 - p)
- `compute_odds_movement()` — 从赔率时间序列计算变动特征

若只有1条记录: drift=0, volatility=0
若有2+条: opening=第1条, closing=最后1条
- `compute_market_features()` — 单场快捷计算
- `generate_market_features()` — 批量生成

---

### `optimize\model_comparison.py`

> 哨响AI - GBDT 三模型对比评估框架 (T08)
========================================
在同一份数据上训练 XGBoost / LightGBM / CatBoost 并横向对比。

功能：
1. 统一训练 → 统一评估 → 生成对比报告
2. 按联赛拆分评估
3. 预测一致性分析（三模型投票）
4. 特征重要性对比
5. 训练时间 / 推理速度对比

**类**:
- `ModelEvalSummary` — 单个模型评估摘要
- `ComparisonReport` — 完整对比报告
- `ModelComparison` — 三模型对比评估器。
统一训练 XGBoost / LightGBM / CatBoost 并在相同测试集上评估。

**函数**:
- `run_comparison()` — 一键运行三模型对比

---

### `optimize\model_registry.py`

> 哨响AI - 模型注册表 v2.0
======================
跟踪所有已训练的模型版本及其评估指标，
支持语义化版本、模型哈希校验、版本对比、A/B 测试、回滚、部署标记。

新功能 (v2.0):
    - 语义化版本 (semver) 支持
    - 模型 SHA256 哈希 (完整性校验)
    - compare_versions() 版本对比
    - auto-promote() 自动晋升最优模型
    - 增强 CLI (compare, register, history)

使用:
    registry = ModelRegistry()
    registry.register(model_path, metrics, semver='3.1.0')  # 注册新模型
    registry.list_models(status='active')                     # 列出活跃模型
    registry.deploy(model_id)                                 # 部署到生产
    registry.compare_versions('v0001', 'v0002')               # 版本对比
    registry.get_best_by_metric('accuracy')                   # 获取最佳模型

**类**:
- `ModelRegistry` — 模型版本注册表 — 持久化为 JSON

v2.0 新特性:
- 语义化版本 (semver)
- SHA256 模型哈希 (完整性校验)
- 版本对比 (compare_versions)
- 自动晋升 (auto_promote)

**函数**:
- `get_registry()` — 获取全局注册表单例

---

### `optimize\phased_optimization.py`

> 哨响AI - 四阶段模型优化系统 v1.0
===================================
严格按照足球预测最佳实践，分阶段提升模型：
  阶段1: 数据与基线 → 严格时序划分 + 多特征选择 + LR/RF基线
  阶段2: 特征工程与模型升级 → 高级特征 + XGBoost/LightGBM超参调优
  阶段3: 集成与校准 → Platt/Isotonic校准 + Stacking集成 + 联赛子模型
  阶段4: 反馈闭环 → 自动化流水线 + 评估报告生成

核心原则:
  - 严格按时间划分 (70/15/15) 模拟真实预测
  - 目标: 平衡的准确率 (非偏向某一类) + 可靠概率 (低ECE/低Brier)
  - 多维度评估: 总体Acc + 类级Recall/F1 + ECE + Brier + 联赛级
  - 避免过拟合: 正则化 + 早停 + 特征选择

**函数**:
- `compute_ece()` — 计算 Expected Calibration Error (10 bins).
- `compute_reliability_data()` — 计算可靠性曲线数据。
- `multiclass_brier()` — 多分类 Brier Score (各类平均).
- `load_training_data()` — 从数据库加载所有有比分+特征数据，按时间排序。
- `strict_time_split()` — 严格按时序划分数据集 —— 核心原则。
训练集: 2015-2021 → 验证集: 2022-2023 → 测试集: 2024-2026
模拟真实预测场景：用过去数据训练，预测未来。
- `prepare_features_common()` — 在全部三个数据集上统一准备特征: 去除共同问题列，统一填充+标准化。
确保train/val/test特征维度一致。
- `phase1_baseline()` — 阶段1: 构建稳健基线
- 特征选择 (互信息 + 随机森林重要性)
- 训练 Logistic Regression 和 Random Forest
- 在测试集上评估基线指标
- `phase2_advanced_models()` — 阶段2: 高级模型训练
- XGBoost + LightGBM, 带早停和超参优化
- 交互特征 (可选)
- 验证集调优
- 联赛级错误分析
- `phase3_ensemble_calibration()` — 阶段3: 概率校准 + 加权集成
- Platt Scaling (逻辑回归校准)
- Isotonic 校准
- 多模型集成 (LR + RF + XGB + [LGB])
- 权重搜索 (验证集 Brier 最小化)
- `phase4_report()` — 阶段4: 生成评估报告 + 自动化配置建议
- `main()` — (无文档)

---

### `optimize\phased_optimization_v2.py`

> 哨响AI - 四阶段模型优化系统 v2.0
===================================
深度融合「失准数据分析」形成持续迭代闭环：
  训练 → 预测 → 错误归因 → 特征/模型改进 → 再训练

阶段1: 数据与基线 → 严格时序划分 + 经典特征 + LR/RF基线 + 首次错误分析
阶段2: 特征工程与模型升级 → 全特征集 + XGB/LGB超参调优 + 深度错误归因  
阶段3: 集成与校准 → 分组校准(联赛+实力差) + Stacking集成 + 平局约束
阶段4: 反馈闭环 → 自动化流水线 + 四节报告 + 风险提示

核心改进(v1→v2):
  - 每次训练后立即进行错误聚类分析
  - 错误归因标签: 冷门/实力差/联赛特化/战术克制/数据噪声
  - 分组建模: 联赛子模型 + 实力差子模型
  - 平局专项: class_weight + draw_features + 平局集成约束
  - 四节报告模板: 亚盘/比分/胜平负/综合分析

**函数**:
- `compute_ece()` — Expected Calibration Error
- `compute_brier()` — 多类Brier Score (one-vs-rest平均)
- `class_distribution()` — 返回类别分布
- `tag_prediction_errors()` — 对每场比赛标注错误类型标签。
返回带标签的DataFrame。
- `error_analysis_report()` — 生成结构化错误分析报告
- `load_data()` — 加载完整数据集: matches + match_features + odds
- `time_split()` — 严格按时间划分: 前70%训练, 中间15%验证, 最后15%测试
- `build_phase1_features()` — 第一阶段: 经典足球特征
- 近期5场: 场均进球、失球、积分、胜率
- 主客场能力差
- 历史交锋(H2H)胜率
- 排名差
- `build_phase2_feature_set()` — 第二阶段: 基于错误分析的增强特征集
- 引入市场赔率特征(mkt_*)
- 引入更长期窗口(r10)
- 引入伤病/情绪/动量特征
- 针对错误类型添加交互特征
- `prepare_features()` — 统一特征预处理: 缺失值填充 + 标准化 + 裁剪
- `evaluate_model()` — 全面评估模型
- `train_baseline_models()` — 训练LR和RF基线模型
- `train_advanced_models()` — 训练XGBoost和LightGBM (超参调优)
- `train_league_models()` — 为每个联赛训练子模型
- `train_strength_models()` — 按实力差分组训练
- `calibrate_models()` — 对每个模型进行Platt+Isotonic校准，选择最优
- `optimize_ensemble_weights()` — 网格搜索最优集成权重 (带平局约束)
- `analyze_feature_importance()` — 分析特征重要性，提出改进建议
- `generate_match_reports()` — 生成样本比赛的预测报告 (四节模板):
一、亚盘分析 | 二、比分预测 | 三、胜平负预测 | 四、综合分析
- `main()` — (无文档)

---

### `optimize\poisson_predictor.py`

> 哨响AI — 泊松分布比分预测模块 (T05)
======================================

将足球赛果概率 (H/D/A) 通过泊松模型转化为:
  1. 比分概率矩阵 (0-0 ~ 6-6, 49种组合)
  2. 泊松一致性 H/D/A 概率 (反向聚合，提供替代视角)
  3. Top-K 最可能比分预测

核心逻辑 (源自 predictions.ts):
  - 从胜率反推主/客预期进球 λ
  - 独立泊松 PMF 乘积 → 比分概率
  - 比分概率聚合 → H/D/A 概率
  - 输出最高概率比分 (覆盖主胜/平/客胜各至少1个)

数学推导:
  P(score=h-a) = Poisson(h|λ_h) × Poisson(a|λ_a)
  P(H) = Σ_{h>a} P(h-a)
  P(D) = Σ_{h=a} P(h-a)
  P(A) = Σ_{h<a} P(h-a)

**类**:
- `PoissonPredictor` — 泊松分布比分预测器 (T05)。

用途:
  1. 将已有的 H/D/A 概率通过泊松模型重新映射，得到「泊松一致性概率」
  2. 生成最可能的比分预测
  3. 作为启发式模型的替代信号源

使用示例:
  pp = PoissonP

**函数**:
- `expected_goals_from_probs()` — 从 H/D/A 概率反推主/客预期进球 λ。

推导逻辑:
  - home_share = home_prob / (home_prob + away_prob)
    忽略平局，只看胜负倾向
  - λ_total ≈ base_la
- `score_matrix()` — 生成完整比分概率矩阵。

Returns:
    proba: (max_goals+1) × (max_goals+1) 矩阵
           proba[h][a] = P(主队进h球 ∧ 客队进a球)
- `score_to_outcome_probs()` — 比分概率矩阵 → H/D/A 概率。

对角线 (h=a) → 平局
上三角 (h>a) → 主胜
下三角 (h<a) → 客胜

Returns:
    (home_prob, draw_prob, away_prob)
- `top_score_predictions()` — 生成 Top-K 最可能的比分预测，确保覆盖主胜/平/客胜。

Args:
    lambda_h: 主队预期进球
    lambda_a: 客队预期进球
    top_k: 返回比分数量 (默认3)
    max_goals: 最

---

### `optimize\rolling_features.py`

> 哨响AI - 滚动窗口特征生成器 (T12)
====================================
从比赛历史数据计算多窗口滚动统计特征，丰富模型输入。

核心设计：
  1. 多窗口统计 (3/5/10场) — 捕获短期/中期/长期状态
  2. 方差/一致性指标 — 度量球队稳定性
  3. 主客场分离统计 — 捕获主场优势
  4. 趋势/加速度特征 — 识别上升/下滑
  5. 对手强度调整 — 校正赛程难度偏差
  6. 特征重要性分析 — 评估特征贡献

输出特征可直接：
  - 写入 match_features 表供 GBDT 模型使用
  - 追加到 SequenceBundle.static_features 供 DL 模型使用

**类**:
- `RollingWindowConfig` — 滚动窗口特征配置
- `RollingWindowFeatureGenerator` — 滚动窗口特征生成器。

从数据库加载比赛历史，按球队维护滑动窗口，
为每场比赛计算主客队的滚动统计特征。
- `FeatureImportanceAnalyzer` — 特征重要性分析器。

支持多种方法：
1. 排列重要性 (Permutation Importance) — 模型无关
2. 互信息 (Mutual Information) — 统计方法
3. 相关性分析 (Correlation) — 
- `_SklearnModelWrapper` — 将 PyTorch 模型包装为 sklearn 兼容接口

**函数**:
- `get_rolling_feature_names()` — 返回所有滚动窗口特征名称（按固定顺序）
- `generate_rolling_features()` — 一键生成滚动窗口特征
- `analyze_feature_importance()` — 一键特征重要性分析

---

### `optimize\score_prediction.py`

> 哨响AI — 智能比分预测引擎 (Score Prediction Engine)
=====================================================
替代 prediction_service.py 中的 _generate_dual_score_predictions()。

核心改进：
    1. 动态 xG 驱动：每个比分概率由 home_xG/away_xG 的泊松分布独立计算
    2. 无硬编码频率表：完全基于数学概率 + 联赛基线
    3. 冷门深度集成：冷门信号 → 调整 xG → 重新计算所有比分概率
    4. 场场不同的比分推荐：比赛专属扰动确保差异化

流程：
    赔率/概率 → XGGenerator.generate_xg()
         ↓
    (home_xG, away_xG)
         ↓
    Poisson PMF → 比分概率矩阵(36种)
         ↓
    排序 + Top-3 (保证不同赛果)
         ↓
    冷门检测 → xG 调整 → 重新排序
         ↓
    输出: {primary, secondary, tertiary, upset_info, warnings}

日期: 2026-06-02

**类**:
- `ScorePredictionEngine` — 智能比分预测引擎

纯数学驱动：xG → Poisson → 比分概率 → Top-3 推荐

**函数**:
- `get_score_engine()` — (无文档)
- `predict_scores()` — 便捷函数：一键预测比分

---

### `optimize\sequence_features.py`

> 哨响AI - 序列特征提取器 (T09) — 增强版
======================================
从数据库加载比赛历史，为每场比赛构建主队/客队的近期N场序列特征。

核心思路：
  - 按日期排序，逐队维护"滚动窗口"历史
  - 每场比赛提取两支球队各自的最近 K 场比赛作为序列
  - 输出标准化的 PyTorch 张量 + 静态特征

序列每步特征 (18维):
  1.  goals_for        - 该队进球
  2.  goals_against    - 该队失球
  3.  result_points    - 赛果分数 (3=胜, 1=平, 0=负)
  4.  is_home          - 是否主场 (1/0)
  5.  goal_diff        - 净胜球
  6.  clean_sheet      - 零封 (1/0)
  7.  over_25_flag     - 大于2.5球 (1/0)
  8.  btts_flag        - 双方进球 (1/0)
  9.  days_since       - 距本场比赛天数 / 30 (归一化)
 10.  opp_form_proxy   - 对手近期场均分 / 3 (强度代理)
 ── 增强特征 ──
 11.  rolling_pts_3    - 近3场场均得分 / 3 (短期状态)
 12.  rolling_gd_3     - 近3场场均净胜球 / 3 (短期攻防)
 13.  rolling_gf_5     - 近5场场均进球 / 3 (攻击力)
 14.  rolling_ga_5     - 近5场场均失球 / 3 (防守力)
 15.  win_streak       - 连胜场次 / 5 (归一化, 负为连败)
 16.  form_momentum    - 近5场得分加权和(越近权重越高) / 15
 17.  xg_proxy         - 进球×射门期望代理 (攻击质量)
 18.  comeback_flag    - 逆转/被逆转标志 (-1/0/1)

**类**:
- `SequenceBundle` — 序列特征数据包：可直接输入 PyTorch 模型
- `SequenceFeatureExtractor` — 序列特征提取器。

从 SQLite 数据库加载所有完赛比赛，
按球队维护历史窗口，为每场比赛提取序列特征。

**函数**:
- `split_temporal()` — 按时序分割：前 (1-test-val) 训练, 中 val 验证, 后 test 测试。
修改 bundle 的 train/val/test_indices。
- `temporal_cv_splits()` — 前向链式交叉验证：每折扩大训练集，测试下一段时间。

Returns:
    [(train_indices, test_indices), ...] 共 n_splits 折

---

### `optimize\train_optimizer.py`

> 哨响AI - Optuna超参数优化器 v1.0
==============================
基于贝叶斯优化的超参数搜索，替代原有 ParameterGrid 网格搜索。

特点:
- 自动搜索 XGBoost + Ridge + 集成权重的最优组合
- 支持多目标优化(准确率 + 平局F1 + Brier Score)
- 时序交叉验证防止数据泄露
- 支持早停(pruning)减少搜索时间
- 自动特征选择(基于重要性)
- 输出完整的优化报告

使用方法:
    python optimize/train_optimizer.py          # 默认100次试验
    python optimize/train_optimizer.py --n 200   # 200次试验
    python optimize/train_optimizer.py --no-optuna  # 回退到默认参数训练

**类**:
- `OptunaOptimizer` — Optuna 驱动的超参数优化器

**函数**:
- `optimize_model()` — (无文档)
- `main()` — (无文档)

---

### `optimize\transfer_learning.py`

> 哨响AI - 迁移学习框架 (T11)
============================
1. 大型联赛预训练 (Premier League / 全联赛)
2. 小联赛/目标联赛微调策略
3. 领域适应评估

核心思路:
  - Phase 1: 在数据量大的源联赛上预训练模型, 学习通用足球模式
  - Phase 2: 冻结/部分冻结编码器, 仅微调分类头或全模型
  - Phase 3: 评估源域→目标域的迁移效果, 计算领域差距

微调策略:
  - head_only:    仅训练分类头, 编码器完全冻结
  - gradual:      渐进解冻 (先训分类头, 再逐步解冻编码器层)
  - discriminative: 判别式学习率 (浅层小lr, 深层大lr)
  - full:         全模型微调 (小学习率)

用法:
    from optimize.transfer_learning import TransferLearningManager, FineTuningConfig
    mgr = TransferLearningManager(db_path='data/football_data.db')
    result = mgr.pretrain_and_finetune(
        source_leagues=['Premier League', 'La Liga'],
        target_league='Ligue 1',
    )

**类**:
- `LeagueAwareSplitter` — 按联赛分割 SequenceBundle。

支持两种模式:
  1. 按联赛名过滤: 提取指定联赛的子集
  2. 按联赛大小分组: 大联赛 → 源域, 小联赛 → 目标域
- `FineTuningConfig` — 微调策略配置
- `LayerFreezer` — 模型层冻结/解冻工具。

支持:
  - 按模块名模式冻结
  - 渐进解冻 (从分类头→编码器)
  - 判别式学习率分组
- `DiscriminativeLR` — 判别式学习率: 编码器用小lr, 分类头用大lr。
越深的层学习率越大 (接近分类头)。

用法:
    param_groups = DiscriminativeLR.build_param_groups(model, base_lr=
- `DomainGapMeasurer` — 计算源域和目标域之间的领域差距。

方法:
  1. 特征分布距离 (MMD - Maximum Mean Discrepancy)
  2. 标签分布差异 (KL散度)
  3. 统计特征差异 (均值/方差偏移)
- `TransferLearningManager` — 迁移学习管理器: 预训练 + 微调一站式。

工作流:
  1. 在源联赛上预训练模型
  2. 在目标联赛上微调
  3. 评估迁移效果 (vs 从零训练基线)

**函数**:
- `transfer_learn()` — 一站式迁移学习。

Args:
    source_leagues: 源联赛列表 (用于预训练)
    target_league: 目标联赛 (用于微调)
    strategy: 微调策略
    model: 模型类型

Ret
- `evaluate_transfer()` — 仅评估领域差距 (不训练模型)。

用于快速判断迁移学习是否有价值。

---

### `optimize\walkforward_backtest.py`

> 哨响AI - 滚动窗口回测框架 (T17)
===================================
实现 walk-forward 验证策略、按月/季度时间分割、滚动窗口回测引擎，
以及回测结果分析和历史性能报告生成。

核心组件:
  1. TimeSplitter — 时间分割策略
     - expanding: 扩展窗口 (训练集不断扩大)
     - sliding: 滑动窗口 (固定训练窗口大小)
     - 支持月/季度/赛季/自定义频率
  2. WalkForwardEngine — 滚动窗口回测引擎
     - 逐折训练+预测
     - 支持任意预测器 (ELO/Ensemble/Expert/自定义)
     - 与 T15/T16 校准系统集成
  3. BacktestResult — 回测结果容器
     - 逐折/汇总指标 (Accuracy/Brier/ECE/LogLoss/MCC)
     - 置信区间 & 性能退化检测
     - 按联赛/时段分解
  4. BacktestVisualizer — 回测可视化
     - 滚动性能曲线
     - 逐折雷达图
     - 退化热力图
  5. BacktestReportBuilder — HTML 历史性能报告

依赖:
  - numpy, pandas, matplotlib
  - T15 calibration.py (compute_ece, multiclass_brier)
  - T16 calibration_viz.py (ECEMonitor, CalibrationVisualizer)

用法:
    from optimize.walkforward_backtest import TimeSplitter, WalkForwardEngine

    splitter = TimeSplitter(freq='quarter')
    folds = splitter.split(df, date_col='match_date')

    engine = WalkForwardEngine(predictor_factory=my_predictor)
    result = engine.run(df, folds, label_col='result_label', prob_cols=['home_prob','draw_prob','away_prob'])
    result.summary()

**类**:
- `TimeFold` — 单折训练/测试数据索引
- `TimeSplitter` — 时序数据分割器 — 支持月/季度/赛季/自定义频率的 walk-forward 分割。

Parameters
----------
freq : str
    分割频率: 'month' | 'quarter' | 'season' |
- `FoldMetrics` — 单折回测指标
- `WalkForwardEngine` — 滚动窗口回测引擎。

支持两种模式:
1. 预计算概率模式: DataFrame 中已有预测概率列
2. 预测器工厂模式: 每折重新训练并预测

Parameters
----------
predictor_factory : calla
- `BacktestResult` — 回测结果 — 包含逐折和汇总指标
- `BacktestVisualizer` — 回测结果可视化工具。

Parameters
----------
output_dir : str
    图表输出目录
dpi : int
    图表分辨率
- `BacktestReportBuilder` — 回测结果 HTML 报告生成器。
生成自包含的 HTML 文件 (图表 base64 嵌入)。

**函数**:
- `run_walkforward_backtest()` — 一键运行 walk-forward 回测 + 可视化 + HTML 报告。

Parameters
----------
df : pd.DataFrame
date_col : str
label_col : str
prob_cols 
- `run_multi_strategy_comparison()` — 多策略对比回测。

Parameters
----------
strategies : dict
    {strategy_name: prob_cols_list} 每个策略的概率列名列表

Returns
-------
(resu

---

### `optimize\weight_optimizer.py`

> 哨响AI - 集成权重优化器 (T02)
================================
1. 时间序列验证集划分策略（防未来信息泄露）
2. Optuna 贝叶斯搜索最优集成权重
3. 网格搜索基线对比
4. 全面评估指标 + 可视化报告

**类**:
- `WeightOptimizer` — 集成权重优化器 — 仅优化 Ensemble 融合权重（不重新训练模型）

---

### `optimize\xg_generator.py`

> 哨响AI — 动态预期进球生成器 (xG Generator)
=============================================
替代固定 goal_diff 公式，基于赔率/概率/实力数据动态计算每场比赛的
home_xG (主队预期进球) 和 away_xG (客队预期进球)。

设计原则：
    1. 赔率驱动总进球期望：主胜/平局/客胜概率 → 总进球基线
    2. 实力差分配进球：rating差/攻防数据 → 主客进球拆分
    3. 联赛感知：不同联赛的进球特征不同 (德甲 3.12 vs 西甲 2.59)
    4. 比赛不确定性：±0.15 小扰动避免同质化

核心流程：
    赔率 → 隐含概率 → 总进球期望(回归模型)
          ↓
    rating差 + 攻防特征 → 主客分配比例
          ↓
    home_xG, away_xG → 泊松比分矩阵

日期: 2026-06-02

**类**:
- `XGGenerator` — 动态 xG 生成引擎

为每场比赛生成独一无二的 home_xG 和 away_xG，
完全替代固定的 goal_diff → λ 公式。

**函数**:
- `get_xg_generator()` — 获取全局 XGGenerator 单例
- `generate_xg()` — 便捷函数：生成 home_xG, away_xG

---
