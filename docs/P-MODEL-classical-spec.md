# P-MODEL 古典模型接入可行性规格 — DC / Elo / 贝叶斯

> 队列任务：T07（纯规格文档，不写生产代码、不运行、不碰 events.db）
> 对齐：`docs/DISCIPLINE.md`（§1 诚实 / §9 跨庄禁区）、`docs/model_promotion_runbook.md`（walkforward 门禁、fail-closed）
> 范围：本文只描述「对照接入方案 + walkforward 门禁」，落地须走 `model_promotion_runbook` 的晋升流程。

---

## 0. 定位与红线（先读）

**本规格的目的不是找新 edge。** 项目已诚实判定：全系统无已验证可交易边缘（P0 FAILED，2026-09-17；KNN EDGE 2026-09-23 证伪）。古典模型（DC/Elo/贝叶斯）在此系统中的合理角色只有三类：

1. **校准锚（诚实基准）**：用闭式可解释模型交叉验证现役 ML 概率的校准（ECE/LogLoss），而非替代。
2. **对照基线（walkforward 门外考官）**：给现役 `unified_predictor` / `odds_candles` 一个低复杂度的可解释对照，量化"ML 相对朴素模型的真实增益"。
3. **特征贡献（非独立预测）**：把 Elo 差值、DC 期望进球 λ 作为特征喂入现役模型，而非做独立喊单引擎。

**红线（不可破）：**
- IR-30：任何"盈利/优于市场"声称必须先过 G6 零信息机械对照 + 幂法去水（`pipeline/odds_math.py::devig_power`）。比例法 `devig3/devig_n` 系统性高估热门，禁用。
- IR-32：古典模型输出**禁止**做跨庄共识、禁止喊单。三者都是单源闭式模型，不存在"跨庄"概念，但 DC/Elo 若用于"诱导器对照"须单庄内、独立信息，不得聚合多庄。
- 现役主指标 = LogLoss / Brier / ECE（校准），**不是 ROI**。ROI 仅作 G6 旁证。

---

## 1. 现状盘点（已存在模块，避免重复造轮子）

| 家族 | 现存模块 | 用途现状 | 本规格态度 |
|------|----------|----------|------------|
| Dixon-Coles | `pipeline/dc_model.py`（数学核）、`pipeline/dc_score_model.py`（比分矩阵）、`pipeline/gq_dc_oos_validation.py`（OOS 验证） | 比分分布重建，δ 相关调整；已有 OOS 验证骨架 | 复用核，补 walkforward 对照门禁 |
| Elo | `pipeline/team_strength_ranking.py`、`pipeline/league_scoring_prior.py` | 球队强度排序 / 联赛先验 | 作特征 + 对照基线 |
| 贝叶斯 | `pipeline/poisson_gbm.py`、`pipeline/open_eye_predictor.py`、`pipeline/reverse_odds_engine.py`（贝叶斯意图分类） | 泊松-GBM、反向赔率引擎 | 已有贝叶斯成分，本规格不新增贝叶斯框架，只规范"不确定性量化"输出契约 |

> 关键事实：`dc_score_model.py` 默认 `goal_scale=1.2`（仅波胆 top3 口径）；派生市场统一 `goal_scale=1.1`（守卫后 A/B，2026-09-21）。新古典模型接入须沿用该口径，不得自造 λ 缩放。

---

## 2. 三家族对照接入方案

### 2.1 Dixon-Coles（DC）— 校准锚 + 对照基线

**数学（已建于 `dc_model.py`）**
- 独立双泊松缺陷：低比分正相关系统性低估 0-0/1-1。
- DC 相关调整 `f(i,j;δ)`：δ>0 上调平局与低比分，`Z` 归一。δ=0 退化为独立泊松（嵌套，MLE 单调）。
- 嵌套性质保证：同数据上 DC 对数似然 ≥ 独立泊松。

**接入点**
- 复刻 `dc_score_model.build_scores(λh, λa, δ)` → 产出 1X2 + 波胆概率。
- λh/λa 由来：用 Elo 差值（§2.2）映射期望进球，避免重新训练攻击参数。

**对照价值（诚实）**
- 把 DC 1X2 概率与现役 ML 同场并排，算 ΔLogLoss / ΔECE。
- 判定标准：现役 ML 须 ΔLogLoss < -0.01（即 ML 显优于 DC）才维持主模型地位；若 DC 持平甚至更优 → 说明现役复杂度未换来泛化，触发降复杂度复盘（非新增 edge）。

### 2.2 Elo — 特征 + 对照基线

**方案**
- 复用 `team_strength_ranking` 的 Elo 序列，取赛前 30 场滚动 Elo 差 Δelo。
- 期望进球映射：`λh = base * exp(k*Δelo)`，k 由历史回归定（只读 events.db matches 拟合，写隔离库不回 events.db）。
- 输出契约：`{home_elo, away_elo, delta_elo, lambda_home, lambda_away}` JSON，供 §2.1 与现役模型共用。

**对照价值**
- Elo 作为**零特征工程的下界基线**：若现役 ML 相对 Elo-only 无 ΔLogLoss 增益，则 ML 投入存疑（诚实诊断，非盈利声称）。

### 2.3 贝叶斯（不确定性量化）— 仅作输出契约升级

**方案（不新造贝叶斯框架）**
- 在现役模型输出加 `confidence_interval`（由 `poisson_gbm` / `reverse_odds_engine` 已有的后验离散度派生）。
- 输出契约新增字段：`probabilities.{H,D,A}` + `ci_low` / `ci_high` / `entropy`。
- 用途：高熵（接近 1/3）场次自动降级展示，不进入任何自动决策。

**禁止**
- 不得用贝叶斯后验做"+EV 点选"——那会绕过 G6 零信息对照，触 IR-30。

---

## 3. walkforward 门禁（接入必过）

对齐 `docs/model_promotion_runbook.md` §3，古典模型任何"进入生产路径"动作须：

```
[1] 候选产出 (scripts/ 新模块, 纯函数, 可单测)
   │   - reproducible: 固定 random_state, 记录训练/拟合窗口
   ▼
[2] OOS 审计 (复用 scripts/audit_all_models_oos.py 框架, 加 classical 区块)
   │   - broken=0, 古典模型标 RESEARCH_DCLASS (served 但未过 walkforward 不标 active)
   ▼
[3] walkforward 对照门禁 (本规格新增, 见 §4)
   │   - 4 切分严格整场切分, 古典 vs 现役 ΔLogLoss 必须稳定 (非单切分偶然)
   ▼
[4] 晋升门禁 pytest tests/test_oos_guard.py -q 全绿 (fail-closed)
   ▼
[5] 配置翻牌 (config/settings.yaml paths.*) + /health 冒烟
   ▼
[6] 监控 24-72h 无退化
```

**WINDOW 项（须维护窗口，本规格不执行）：** 任何把古典模型写入 events.db、改 schema、或接回 `freeze` 的动作都归类为 WINDOW，仅备料（写迁移脚本 + dry-run），由人工在停机窗口运行。

---

## 4. 对照判定标准（诚实口径）

| 指标 | 现役基线 | 古典模型准入线 | 决策 |
|------|----------|----------------|------|
| ΔLogLoss（古典 vs 现役，n≥2500） | — | 古典须 ≥ 0（即不显著更差）才可作对照基线 | 若古典显著更优 → 降级现役复盘 |
| ΔECE（校准） | 现役 ECE≤0.06 门禁 | 古典 ECE 用于标定现役校准漂移 | 仅诊断 |
| 样本量 | G1 ≥2500 | 同 G1 | 不足不判 |
| G6 零信息对照 | 模型 ROI 须过 | 古典 ROI 同理须过 | 不过 = NO EDGE |

**铁律**：古典模型不得带来"新 edge"预期。本规格所有的对照结论都先假设 NO EDGE，只有 walkforward + G6 双过才升级为"可解释增益"诊断意见，且仍**不喊单**。

---

## 5. 验收门禁（落地时）

- [ ] 三家族模块均可被 `pytest` 加载（导入不抛异常）。
- [ ] 对照脚本输出隔离 JSON（`reports/classical_vs_production_<date>.json`），不写 events.db。
- [ ] `test_oos_guard.py` 全绿（古典模型注册为 RESEARCH_DCLASS，不进 active 生产路径）。
- [ ] 任何对外文档只含校准/对照指标，不含未经 G6 的 ROI edge 声称。

---

## 6. 回滚

- 古典模型仅作对照，不进 `paths.*` 生产指针 → 回滚=删对照产出文件，零风险。
- 若误入生产路径：按 `model_promotion_runbook.md` §4.1 还原 `config/settings.yaml` 指针。

---

## 7. 下一步建议（owner 自主追加）

- T07.1：写 `scripts/classical_baseline_compare.py`（只读 events.db，输出隔离 JSON），接 §4 门禁。
- T07.2：把 Elo Δelo 特征并入 `indep_features_gq` 校验（独立信息增广，路径②已证 NO EDGE，仅作特征贡献复核）。
- 本规格不触发任何生产改动。
