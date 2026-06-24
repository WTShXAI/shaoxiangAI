# 哨响AI 系统架构文档

> 版本: v3.1 | 更新: 2026-05-31

---

## 1. 数据管道架构

```
┌─────────────────────┐
│  football-data.org   │  免费API，20次/分钟
│  (16联赛, 10赛季)    │  RATE_LIMIT_SECONDS=7
└─────────┬───────────┘
          │ HTTP JSON
          ▼
┌─────────────────────┐
│   data_collector/    │  pull_historical_data.py
│   main.py            │  update_latest_data.py
└─────────┬───────────┘
          │ INSERT/UPDATE
          ▼
┌─────────────────────────────────────┐
│          SQLite DB (12表)            │
│  ┌─────────┐ ┌───────┐ ┌──────┐   │
│  │ matches  │ │ teams │ │ odds │   │
│  │ (846行)  │ │       │ │(14行)│   │
│  └─────────┘ └───────┘ └──────┘   │
│  ┌──────────────┐ ┌────────────┐   │
│  │match_features│ │predictions │   │
│  │  (838行)     │ │  (38行)    │   │
│  └──────────────┘ └────────────┘   │
│  ┌─────────────┐ ┌────────┐        │
│  │model_training│ │standings│ ...  │
│  └─────────────┘ └────────┘        │
└─────────────┬───────────────────────┘
              │ SQL JOIN
              ▼
┌─────────────────────────────┐
│  features/feature_calculator │  19维特征工程
│  • 核心因子: a1~a6           │
│  • 盘口: sigma_trap, beta_dev│
│  • 基本面: lambda_crush,     │
│    delta_fatigue             │
│  • 市场: epsilon_senti       │
│  • 身体: aerial_advantage,   │
│    press_intensity           │
│  • 竞技: rank_diff_factor,   │
│    form_momentum, h2h_factor │
│  • 裁判: card_risk           │
└─────────────┬───────────────┘
              │ pandas DataFrame
              ▼
┌─────────────────────────────┐
│      预处理管道               │
│  StandardScaler()            │
│  SimpleImputer(median)       │
│  RobustScaler + clip(±3σ)   │
└─────────────┬───────────────┘
              │ scaled features
              ▼
┌─────────────────────────────────────┐
│         集成预测引擎                  │
│  ┌──────────┐ ┌───────┐ ┌────────┐ │
│  │ XGBoost  │ │ Ridge │ │启发式   │ │
│  │  50%     │ │ 30%   │ │  20%   │ │
│  └────┬─────┘ └──┬────┘ └───┬────┘ │
│       └───────────┼──────────┘      │
│                   ▼                 │
│           加权平均概率               │
│    [P(主胜), P(平局), P(客胜)]      │
└─────────────┬───────────────────────┘
              │ (match_id, prob_h/d/a, confidence)
              ▼
┌─────────────────────────────┐
│  prediction_engine.py       │
│  → predictions 表            │
│  → CSV 输出                  │
└─────────────┬───────────────┘
              │ 反馈循环
              ▼
┌─────────────────────────────┐
│  auto_pipeline.py            │
│  → Walk-Forward 回测         │
│  → 性能对比                  │
│  → 迭代训练                  │
└─────────────────────────────┘
```

---

## 2. 目录结构

```
footballAI/
├── api/                    # Flask API 服务
│   ├── prediction_service.py
│   └── routes/
├── data_collector/         # football-data.org 采集
│   └── main.py
├── database/               # 数据库管理
│   ├── db_manager.py
│   ├── enhanced_data.py
│   └── init_db.py
├── features/               # 特征工程
│   └── feature_calculator.py
├── models/                 # 模型训练器
│   └── linear_regression_trainer.py
├── modules/                # 8大公式模块
├── scripts/                # 工具脚本
├── frontend/               # 前端静态页面
│   └── index.html
├── saved_models/           # 27个模型文件
├── data/                   # SQLite DB + CSV
│   └── football_data.db
├── ensemble_trainer.py     # 集成训练 v3.0
├── prediction_engine.py    # 预测引擎 v3.0
├── auto_pipeline.py        # 自动管道
├── model_analyzer.py       # 模型分析器
├── config.yaml             # 19维特征配置
└── requirements.txt        # 依赖声明
```

---

## 4. 模型性能

| 指标 | 值 |
|------|-----|
| Walk-Forward 准确率 | 49.14% (7轮) |
| 最新准确率 | 48.13% |
| Draw Recall | 40.0% |
| Draw Precision | 28.22% |
| Brier Score | 0.2029 |
| 训练样本 | 18,218 |
| 特征维度 | 19 |

### 各联赛准确率
| 联赛 | 准确率 |
|------|--------|
| Serie A | 51.15% |
| La Liga | 50.42% |
| Premier League | 48.37% |
| Ligue 1 | 47.69% |
| Bundesliga | 47.59% |

---

## 5. 已知问题

| 优先级 | 问题 | 状态 |
|--------|------|------|
| P0 | Python 环境已修复 (3.10.11) | ✅ |
| P0 | 平局检测弱 (Recall 40%) | 🔧 待优化 |
| P1 | 模型冗余 (27个, 实际只需3个) | 🔧 待清理 |
| P1 | 置信度失校准 (高置信实际37.9%) | 🔧 待修复 |
| P2 | 缺少球员/天气/实时数据维度 | 🔧 待扩充 |
