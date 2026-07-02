# 哨响AI — 公式与算法说明

> **版本**: v4.1.0 | **更新**: 2026-06-11  
> **覆盖**: 特征工程公式 · ML 集成模型 · 领域知识修正 · 三层降级策略 · 泊松比分预测

---

## 目录

1. [系统架构概要](#1-系统架构概要)
2. [特征工程公式](#2-特征工程公式)
3. [ML 集成模型](#3-ml-集成模型)
4. [领域知识修正引擎](#4-领域知识修正引擎)
5. [三层降级策略](#5-三层降级策略)
6. [风险评估与 Kelly 准则](#6-风险评估与-kelly-准则)
7. [泊松分布比分预测](#7-泊松分布比分预测)
8. [硬编码概率检测](#8-硬编码概率检测)
9. [公式索引表](#9-公式索引表)

---

## 1. 系统架构概要

```
用户输入 → CommanderAgent (gemma4:12b) → 路由决策
                                            │
                    ┌───────────────────────┼───────────────────────┐
                    ▼                       ▼                       ▼
             DataAnalystAgent         MathAgent              ExplainerAgent
             (deepseek-r1:8b)        (phi4:14b)              (qwen3:8b)
                    │                  │    │                      │
                    │          ┌───────┘    └───────┐              │
                    │          ▼                    ▼              │
                    │   ModelBridge v2.0    DomainKB 修正          │
                    │   (XGBoost+Ridge)    (derby/top6/injury)     │
                    │          │                                   │
                    └──────────┼───────────────────────────────────┘
                               ▼
                       CommanderAgent 汇总 → 最终决策
```

### 技术栈

| 组件 | 技术选型 |
|------|---------|
| 数据库 | SQLite (`data/football_data.db`) |
| Web 框架 | FastAPI (端口 8000)，入口 `python main.py backend` |
| 前端 | 纯静态 HTML+JS+CSS SPA (v5.0 深色主题) |
| ML 框架 | XGBoost + Ridge 双模型集成 (scikit-learn + xgboost) |
| Agent 框架 | LangGraph (Commander → Data → Math → Explainer 有向图) |
| LLM 推理 | Ollama (gemma4:12b / phi4:14b / deepseek-r1:8b / qwen3:8b) |
| 模型版本 | `football_v4.1_production.joblib` (强制锁定) |

---

## 2. 特征工程公式

> 实现位置: `features/feature_calculator.py` (FeatureCalculator 类, 396 行)

系统基于 **4 大类输入数据** 计算特征：盘口赔率、赛事基本面、市场情绪、时空断裂带。最终聚合为 **A1–A6 六个核心因子**，作为 ML 模型的输入。

### 2.1 模块1: 盘口动态特征 (4 公式)

#### 公式 2.1.1 — 异常波动率 σ_trap

$$\sigma_{trap} = \overline{|\ln(\frac{o_t}{o_{t-1}})|} \cdot \frac{24}{\max(V_{avg},\ 1)}$$

- `o_t`: t 时刻赔率
- `V_avg`: 平均成交量
- 物理含义：赔率对数收益率的均值，经成交量缩放。高波动 = 市场分歧大。

```python
def calc_odd_volatility(odds_series, avg_volume):
    log_returns = [abs(log(odds[i] / odds[i-1])) for i in range(1, len(odds))]
    return mean(log_returns) * (24.0 / max(avg_volume, 1))
```

---

#### 公式 2.1.2 — 凯利-赔率价值方程 V_value

$$V_{value} = \frac{P_{model} \cdot O - 1}{O - 1} - 0.05 \cdot r$$

- `P_model`: 模型概率
- `O`: 当前赔率
- `r`: 庄家利润率 (~6%)
- 物理含义：用 Kelly 公式度量赔率中隐含的价值。正值 = 存在正期望。

---

#### 公式 2.1.3 — 隐含胜率转换 P_implied

$$P_{implied} = \frac{1}{O} \cdot (1 - r)$$

- `r`: 庄家利润率
- 物理含义：从赔率反推庄家隐含概率，去除利润率偏差。

---

#### 公式 2.1.4 — 盘口偏差指数 β_dev

$$\beta_{dev} = |H_{theoretical} - H_{actual}|$$

- `H_theoretical`: 理论盘口（基于实力差推算）
- `H_actual`: 实际开盘盘口
- 物理含义：市场定价与理论值的偏差，大偏差 = 潜在价值机会。

---

### 2.2 模块2: 赛事基本面特征 (5 公式)

#### 公式 2.2.1 — 战术克制系数 λ_crush

$$\lambda_{crush} = \frac{S_{home}}{S_{away}} \cdot (1 - e^{-H_{suppression}})$$

- `S_home / S_away`: 主/客队实力评分
- `H_suppression`: 历史交锋压制程度 [0, 1]
- 物理含义：综合考虑实力比和历史压制效应的克制系数。>1 = 主队克制客队。

---

#### 公式 2.2.2 — 体能衰减因子 δ_fatigue

$$\delta_{fatigue} = e^{-0.05 \cdot I_{midweek}}$$

- `I_midweek`: 周中比赛强度 (场次 × 强度系数)
- 物理含义：指数衰减模型量化一周双赛的体能消耗。

---

#### 公式 2.2.3 — 75分钟体能定律 fitness_75

$$f_{75} = \min\left(\max\left(0,\ \frac{G^{75+}_{home}}{\max(G^{75+}_{away\_conceded},\ 1)} \cdot \min\left(\frac{R_{days}}{7},\ 1.5\right)\right),\ 2.0\right)$$

- `G^{75+}_{home}`: 主队 75 分钟后进球数
- `G^{75+}_{away_conceded}`: 客队 75 分钟后失球数
- `R_days`: 休息天数
- 物理含义：75分钟是体能拐点，主队 Terminal 进球 / 客队 Terminal 失球比例 × 休息因子。

---

#### 公式 2.2.4 — 防空有效性指数 α_aerial

$$\alpha_{aerial} = \frac{W_{attacker\_aerial}}{W_{defender\_aerial}}$$

- 比值 >1 = 进攻方制空优势，适合高空球战术。

---

#### 公式 2.2.5 — 逼抢对抗公式 π_press

$$\pi_{press} = \frac{C_{press}}{P_{success}}$$

- `C_press`: 主队逼抢次数
- `P_success`: 客队传球成功率
- 物理含义：高强度逼抢 vs 传控能力的直接对抗。

---

### 2.3 模块3: 市场情绪特征 (4 公式)

#### 公式 2.3.1 — 情绪偏差因子 ε_senti

$$\epsilon_{senti} = S_{nlp} \cdot \left(1 - \frac{2}{1 + e^{-(G_{discuss} - 0.5)}}\right)$$

- `S_nlp`: NLP 情感得分 [0, 1]
- `G_discuss`: 讨论增长率
- 物理含义：Sigmoid 激活的情绪偏差，极端舆情 → 强偏差。

---

#### 公式 2.3.2 — 大户博弈信号 S_whale

$$S_{whale} = \begin{cases} -1.0 & \text{欧赔大单押主 + 亚盘大单押客（对冲）}\\ 1.0 & \text{otherwise} \end{cases}$$

- 物理含义：检测跨市场对冲行为。机构对冲 = 看衰主队。

---

#### 公式 2.3.3 — 舆情增长指数 g_discuss

$$g_{discuss} = \frac{G_{home} - G_{away}}{T_{discuss}}$$

- 物理含义：主客队讨论热度差 / 总讨论量，反映舆论倾斜方向。

---

#### 公式 2.3.4 — 新闻冲击系数 γ_news

$$\gamma_{news} = 1.0 + \frac{N_{breaking}}{10} \cdot P_{sentiment}$$

- 物理含义：突发新闻的冲击力 = 新闻数量 × 情感极性。

---

### 2.4 模块4: 时空断裂带 (2 公式)

#### 公式 2.4.1 — 时段压制系数 δ_t

$$\delta_t = \frac{G^{t}_{home}}{G^{t}_{away\_conceded}} \cdot e^{-0.1 \cdot |t - t_{opt}|}$$

- `t_opt`: 最优进攻时段 (默认 60')
- 物理含义：特定时段的进球/失球比 × 时间衰减。在最优时段附近达到峰值。

---

#### 公式 2.4.2 — 基准时段 μ_τ

$$\mu_{\tau} = \arg\max_{t \in \{0,15,30,45,60,75,90\}} \frac{G^{t}_{home}}{G^{t}_{away\_conceded}}$$

- 扫描全部时间窗口，找到主队相对进球优势最大的时段。

---

### 2.5 模块5: 裁判影响 (2 公式)

#### 公式 2.5.1 — 红黄牌风险模型 R_card

$$R_{card} = \frac{1}{1 + e^{-(C_{avg} \cdot (1 + 0.2 \cdot \pi_{press}) - 3)}}$$

- `C_avg`: 裁判场均出牌数
- Logistic 函数映射到 [0, 1]，高值 = 高概率出牌。

---

#### 公式 2.5.2 — 裁判影响矩阵

$$M_{ref} = \text{clip}_{[0,1]}\left(0.6 \cdot B_{home} + 0.2 \cdot (1 - S_{strict}) + 0.2 \cdot R_{var}\right)$$

- `B_home`: 主场偏向
- `S_strict`: 执法严格度
- `R_var`: VAR 介入率
- 综合标量：越接近 1 = 对主队越有利。

---

### 2.6 模块6: 跨市场套利 (2 公式)

#### 公式 2.6.1 — 亚欧背离指数 α_arb

$$\alpha_{arb} = |\frac{P_{asian}}{P_{euro}} - 1| \cdot R_{volume}$$

- 亚盘隐含概率 vs 欧赔隐含概率的偏离度 × 成交量比。

---

#### 公式 2.6.2 — 套利时间窗口 W_arb

$$W_{arb} = \frac{D_{odds}}{V_{avg}}$$

- 赔率差异 / 平均成交量。大盘活水 → 套利窗口大。

---

### 2.7 模块7: 融合补偿 (4 公式)

#### 公式 2.7.1 — 三维动态融合 P_fusion

$$P_{fusion} = \gamma \cdot (\alpha \cdot P_{odds} + (1-\alpha) \cdot \sigma_{trap}) + (1-\gamma) \cdot \lambda_{crush} + \beta \cdot \epsilon_{senti}$$

- `γ = 0.7, α = 0.6, β = 0.15` (可调超参)
- 三维融合：
  - **市场维度** (70%): 赔率隐含概率 + 波动调整
  - **基本面维度** (30%): 战术克制系数
  - **情绪修正** (15%): 舆情偏差
- 最终 clip 到 [0, 1]。

---

#### 公式 2.7.2 — 冷门补偿机制 P_final

$$P_{final} = P_{fusion} \times \begin{cases} 1.25 & \sigma_{trap} > 0.15 \land S_{whale} = -1 \\ 0.8 & \lambda_{crush} > 2.0 \\ 1.0 & \text{otherwise} \end{cases}$$

- **异常波动 + 对冲信号** → 冷门预警，概率放大 1.25×
- **极端战术克制** → 克制方概率被压缩，仅 0.8× (避免过度自信)

---

#### 公式 2.7.3 — 战意补偿因子 ΔP

$$\Delta P = \text{clip}_{[-0.15, 0.15]}\left(0.03 \cdot R_{diff} \cdot (1 + \max(0, |R_{diff}| - 12) \cdot 0.02)\right)$$

- 基本层: 每差 1 名 +3% 概率
- 增强层: 差 >12 名(保级队 vs 争冠队) → 非线性增强

---

#### 公式 2.7.4 — 时空断裂补偿 δ_QF

$$\delta_{QF} = \overline{\{\delta_t \mid \delta_t > 1.8\}}$$

- 筛选压制系数 >1.8 的时段，取均值。只关注显著断裂点。

---

### 2.8 核心因子 A1–A6

所有模块特征最终聚合为 6 个核心因子，输入 ML 模型：

| 因子 | 名称 | 公式 | 范围 | 含义 |
|------|------|------|------|------|
| **A1** | 盘口价值因子 | `V_value × (1 + σ_trap)` | [-0.5, 0.5] | 赔率价值 × 波动加成 |
| **A2** | 基本面优势因子 | `(λ_crush + f_75 + π_press + rank_f + form_f) / 5` | [0, 1] | 战术+体能+逼抢+排名+状态 |
| **A3** | 市场情绪因子 | `(ε_senti + γ_news + S_whale_norm) / 3` | [0, 1] | 情绪+新闻+大户三方综合 |
| **A4** | 盘口-基本面协同 | `A1 × A2` | [-0.5, 0.5] | 二者同向=强信号，背离=弱信号 |
| **A5** | 波动调整信号 | `A1 / (1 + |σ_trap|)` | [-0.5, 0.5] | 高波动→信号衰减 |
| **A6** | 市场分歧度 | `|A3 - 0.5| × 2 × sign(A1)` | [-0.5, 0.5] | 情绪偏离中性 × 盘口方向 |

> **注意**: 上述 A1-A6 是特征工程层的核心因子。ML 模型 (XGBoost+Ridge) 实际使用的特征维度为 **90+ 维**，包含这些因子及其交叉项、滚动窗口统计等衍生特征。

---

## 3. ML 集成模型

> 实现位置: `agents/model_bridge.py` (ModelBridge v2.0), `ensemble_trainer.py` (EnsembleTrainer)

### 3.1 模型架构

```
输入特征 (90+ 维)
      │
      ├──────────────────────┬──────────────────────┐
      ▼                      ▼                      ▼
  XGBoost               Ridge Regression       StandardScaler
  (梯度提升树)            (岭回归)                (特征标准化)
      │                      │
      └──────────┬───────────┘
                 ▼
         Soft Voting (概率平均)
                 │
                 ▼
         输出: [P(home), P(draw), P(away)]
```

### 3.2 集成方法 — 加权软投票

$$P(c) = w_{xgb} \cdot P_{xgb}(c) + w_{ridge} \cdot P_{ridge}(c)$$

- `w_xgb = 0.5, w_ridge = 0.5` (等权平均)
- 每个基模型输出三维概率向量 [home, draw, away]
- 加权平均后 L1 归一化

### 3.3 模型锁定机制 (v2.0)

- **强制锁定**: 仅加载 `saved_models/football_v4.1_production.joblib`
- **禁止回退**: 不自动回退到 `footballai_v4_latest.joblib` 或其他模型
- **缺失终止**: 模型文件不存在 → `ModelNotAvailableError` → 系统拒绝启动 (Fail-Fast)
- **格式兼容**: 同时支持 EnsembleTrainer 标准格式和 V4 多 seed 格式

### 3.4 预测输出格式

每次预测输出附带审计字段：

```json
{
    "home": 0.4521,
    "draw": 0.2817,
    "away": 0.2662,
    "_model": "football_v4.1_production.joblib",
    "_version": "4.1.0",
    "_timestamp": "2026-06-11T10:00:00+00:00",
    "_feature_count": 93
}
```

### 3.5 预测快照

每次预测自动写入 `logs/predictions/prediction_YYYYMMDD_HHMMSS_XXXXXX.json`，包含：
- 完整预测结果（含审计字段）
- 输入特征键名清单 (前 10 个采样)
- 可选的领域知识修正记录 (`knowledge_base`)

---

## 4. 领域知识修正引擎

> 实现位置: `rules/domain_rules.py` + `rules/football_kb.yaml`

ML 模型输出原始概率后，应用三条足球领域知识规则进行修正：

### 规则 1: 德比检测

$$P_{draw} \mathrel{+}= 0.03, \quad P_{home} \mathrel{-}= 0.015, \quad P_{away} \mathrel{-}= 0.015$$

- 触发条件：对阵双方在知识库 `derbies` 列表中
- 足球常识：德比战平局率显著高于常规比赛

### 规则 2: Top6 主场优势

$$P_{home} \mathrel{\times}= F_{home\_advantage}$$

- 触发条件：主队在知识库 `teams` 中标记为 `is_top6` + 确认主场作战
- `F_home_advantage`: 各队配置的加成系数（通常 1.05–1.15）

### 规则 3: 关键球员伤停

$$P_{home} \mathrel{-}= \sum_{pos \in injured} Penalty(pos)$$

- 触发条件：`injured_positions` 中存在知识库定义的 `key_positions`
- 惩罚系数示例：GK=0.08, CB=0.05, ST=0.07 (可在 `football_kb.yaml` 中配置)

### 强制归一化

修正后对所有概率做 L1 归一化：

$$P'(c) = \frac{P(c)}{\sum_{c} P(c)}, \quad c \in \{home, draw, away\}$$

### 审计追踪

修正结果附带元数据：

```json
{
    "home": 0.4321,
    "draw": 0.3117,
    "away": 0.2562,
    "_kb_applied": ["derby_boost", "top6_home_advantage"],
    "_kb_raw_ml": {"home": 0.4521, "draw": 0.2817, "away": 0.2662}
}
```

---

## 5. 三层降级策略

> 实现位置: `agents/math_agent.py` (MathAgent.invoke)

系统在概率计算上采用严格的三层降级链：

```
Level 1: Ollama LLM (phi4:14b)
    │ 自然语言推理 + 概率输出
    │ 失败 ↓ (连接超时 / 模型未安装)
    ▼
Level 2: ModelBridge v2.0 (XGBoost+Ridge)
    │ 真实 ML 集成模型预测
    │ → DomainKB 修正
    │ 失败 ↓ (模型文件缺失 → Fail-Fast 终止)
    ▼
Level 3: 规则计算 (最后兜底)
    │ 硬编码基础值 + 特征微调
    │ → 硬编码概率检测 (Fail-Fast)
    │ → 仅作保底，概率不可靠
```

### Level 2 详细流程

```
ModelBridge.predict(match_data)
    │
    ├── 构建特征向量 (从 match_data 提取 90+ 维)
    ├── EnsembleTrainer.predict_batch([feat])
    │     ├── StandardScaler.transform
    │     ├── XGBoost.predict_proba  → P_xgb
    │     ├── Ridge.predict_proba    → P_ridge
    │     └── Soft Voting: (P_xgb + P_ridge) / 2
    │
    ├── 硬编码概率检测 (H≈0.40/D≈0.28/A≈0.32)
    ├── 附加审计字段
    ├── 预测快照写入 logs/predictions/
    │
    └── DomainKB 修正 (derby/top6/injury)
```

### Level 3 规则降级公式

**基础概率**: H=0.40, D=0.28, A=0.32

**特征微调**:

$$P_{home} = 0.40 + 0.35 \cdot A_1 + 0.15 \cdot (A_2 - 0.5) + 0.12 \cdot (A_3 - 0.5) + 0.10 \cdot H2H + 0.08 \cdot Form + 0.12 \cdot Rank$$

$$P_{away} = 0.32 - 0.25 \cdot A_1 - 0.08 \cdot H2H$$

- 调整后做 L1 归一化
- **硬编码检测**: 若最终概率在 H=0.40/D=0.28/A=0.32 的 ±0.02 内匹配 ≥2 个 → `RuntimeError` 终止 (Fail-Fast)

### 置信度计算

$$conf = \min(0.85,\ base + 0.4 \cdot \max(P) + 0.015 \cdot N_{features})$$

- `base`: LLM路径=0.45, ML路径=0.45, 规则路径=0.35
- `max(P)`: 最大概率值
- `N_features`: 可用特征数量

---

## 6. 风险评估与 Kelly 准则

> 实现位置: `agents/math_agent.py` (_assess_risk_with_rules / _detect_value_bets)

### 6.1 风险等级判定

$$RiskLevel = \begin{cases} LOW & \max(P) \geq 0.50 \\ MEDIUM & \max(P) \geq 0.38 \\ HIGH & otherwise \end{cases}$$

### 6.2 隐含概率与优势

$$P_{implied} = \frac{1}{O}$$

$$Edge = P_{model} - P_{implied}$$

- `O`: 赔率 (如 2.10)
- `Edge > 0`: 模型认为市场低估该结果

### 6.3 Kelly 仓位公式

$$K = \frac{P_{model} \cdot O - 1}{O - 1} = \frac{Edge}{O - 1}$$

- `K > 0`: 存在正期望，建议仓位 = K × 本金
- `K ≤ 0`: 无投注价值

### 6.4 价值投注检测阈值

- 触发条件: `Edge > 0.05` (5% 以上的概率优势)
- 输出: 价值投注列表 + 仓位建议

---

## 7. 泊松分布比分预测

> 实现位置: `agents/math_agent.py` (_poisson_predict)

### 7.1 进球期望估算

$$\lambda_{home} = \frac{2.72 \cdot 1.08}{2} + 0.5 \cdot A_1 + 0.3 \cdot A_5$$

$$\lambda_{away} = \frac{2.72}{2} - 0.3 \cdot A_1 - 0.2 \cdot A_5$$

- `BASE_LAMBDA = 2.72`: 场均总进球 (英超历史均值)
- `HOME_ADVANTAGE = 1.08`: 历史主场加成系数
- `A_1`: 盘口价值因子 (影响进球预期)
- `A_5`: 波动调整信号因子
- 最终限制: λ ∈ [0.3, 4.0]

### 7.2 泊松概率质量函数

$$P(k; \lambda) = \frac{\lambda^k \cdot e^{-\lambda}}{k!}$$

### 7.3 比分概率计算

$$P(score = i:j) = P(i; \lambda_{home}) \cdot P(j; \lambda_{away})$$

- `i, j ∈ [0, 6]` — 共 49 种比分组合
- 按概率降序取 Top 5

---

## 8. 硬编码概率检测

> 实现位置: `agents/model_bridge.py` (_check_hardcoded_probability) + `agents/math_agent.py` (_check_hardcoded_probability_in_rules)

### 8.1 检测目标

| 检测值 | Home | Draw | Away |
|--------|------|------|------|
| 硬编码目标 | 0.40 | 0.28 | 0.32 |
| 容差范围 | ±0.02 | ±0.02 | ±0.02 |

### 8.2 双重防线

```
防线 1: ModelBridge._check_hardcoded_probability()
    │ ML 预测后立即检测
    │ 匹配 ≥2 → HardcodedProbabilityError
    ▼
防线 2: MathAgent._check_hardcoded_probability_in_rules()
    │ 规则 Fallback 概率归一化后检测
    │ 匹配 ≥2 → RuntimeError (Fail-Fast)
```

### 8.3 设计理由

硬编码值 (H=0.40/D=0.28/A=0.32) 是规则 Fallback 的初始基础值。如果特征微调未产生有效偏移，说明：
- 特征数据缺失或全部为默认值
- 预测实际基于硬编码而非真实推理

此时产出概率不可靠，必须终止而非给出虚假预测。

---

## 9. 公式索引表

| 编号 | 公式名 | 符号 | 章节 | 实现文件 |
|------|--------|------|------|---------|
| 2.1.1 | 异常波动率 | σ_trap | 2.1 | feature_calculator.py |
| 2.1.2 | 凯利赔率价值 | V_value | 2.1 | feature_calculator.py |
| 2.1.3 | 隐含胜率转换 | P_implied | 2.1 | feature_calculator.py |
| 2.1.4 | 盘口偏差指数 | β_dev | 2.1 | feature_calculator.py |
| 2.2.1 | 战术克制系数 | λ_crush | 2.2 | feature_calculator.py |
| 2.2.2 | 体能衰减因子 | δ_fatigue | 2.2 | feature_calculator.py |
| 2.2.3 | 75分钟体能定律 | f_75 | 2.2 | feature_calculator.py |
| 2.2.4 | 防空有效性 | α_aerial | 2.2 | feature_calculator.py |
| 2.2.5 | 逼抢对抗公式 | π_press | 2.2 | feature_calculator.py |
| 2.3.1 | 情绪偏差因子 | ε_senti | 2.3 | feature_calculator.py |
| 2.3.2 | 大户博弈信号 | S_whale | 2.3 | feature_calculator.py |
| 2.3.3 | 舆情增长指数 | g_discuss | 2.3 | feature_calculator.py |
| 2.3.4 | 新闻冲击系数 | γ_news | 2.3 | feature_calculator.py |
| 2.4.1 | 时段压制系数 | δ_t | 2.4 | feature_calculator.py |
| 2.4.2 | 基准时段 | μ_τ | 2.4 | feature_calculator.py |
| 2.5.1 | 红黄牌风险 | R_card | 2.5 | feature_calculator.py |
| 2.5.2 | 裁判影响矩阵 | M_ref | 2.5 | feature_calculator.py |
| 2.6.1 | 亚欧背离指数 | α_arb | 2.6 | feature_calculator.py |
| 2.6.2 | 套利时间窗口 | W_arb | 2.6 | feature_calculator.py |
| 2.7.1 | 三维动态融合 | P_fusion | 2.7 | feature_calculator.py |
| 2.7.2 | 冷门补偿 | P_final | 2.7 | feature_calculator.py |
| 2.7.3 | 战意补偿 | ΔP | 2.7 | feature_calculator.py |
| 2.7.4 | 时空断裂补偿 | δ_QF | 2.7 | feature_calculator.py |
| 3.2 | 加权软投票 | P(c) | 3.2 | model_bridge.py |
| 5.3 | 规则概率微调 | — | 5 | math_agent.py |
| 6.3 | Kelly 仓位 | K | 6.3 | math_agent.py |
| 7.1 | 进球期望 | λ_home/away | 7.1 | math_agent.py |
| 7.2 | 泊松 PMF | P(k;λ) | 7.2 | math_agent.py |

---

> **维护约定**: 本文件描述 v4.1.0 架构的实际公式与算法。任何公式变更应同步更新对应的实现代码文件与本文档。
> **代码映射**: 公式编号 `X.Y.Z` 对应 `features/feature_calculator.py` 中的 `calc_*` 方法或 `agents/math_agent.py` 中的方法。
