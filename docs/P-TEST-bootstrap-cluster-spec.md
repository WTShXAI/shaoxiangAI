# P-TEST 验证台 G2/G6 bootstrap 按 match 分簇 — 落地规格（WINDOW 备料，纯规格）

> 承接：T53 §4/Q-c（mh 检查点 3.42/场 → 朴素 iid 低估 1.85×）、T45 §Q-c（噪声带随 n 重算）。
> 证据：`scripts/audit_bootstrap_match_cluster.py` + `reports/bootstrap_match_cluster_audit.{json,md}`
> （判定 **FAIL** = 记录了结论翻转，`verdict_of` 为 fail-closed）。
> **本条为纯规格**：不改 `verification/stats.py`、不改 `gates.py`、不跑验证台、不写 `verification.db`、
> 零 `events.db` 写入、零进程操作。
> 日期：2026-09-28

---

## §1 三问答案

### Q1 `stats.py` 的 bootstrap 是否已支持分组重抽？—— 否

| 项 | 实测 |
|---|---|
| `verification/stats.py::roi_ci_bootstrap(returns, alpha=0.05, n_boot=10000)` | **无分组参数**，函数体为 `rng.integers(0, arr.size, size=(n_boot, arr.size))` 纯逐行重抽 |
| `verification/metrics.py` 调用点 | **3 行**：`:51` `roi_ci_t`、`:53` `roi_ci_bootstrap(returns)`（ROI 的 G2 判据）、`:152` `roi_ci_bootstrap(diffs)`（G6 配对差的 `paired_excess_ci_low`）|
| 具备分组能力的调用点 | **0** |
| 全仓 `def *bootstrap*` 定义 | 14 个，但**除本审计脚本外无分组参数**（`scripts/audit_mh_train_div_bypass.py::cluster_bootstrap_sd` 是聚类 SD，非区间估计，不可复用）|

**结论**：`groups` 是纯增量参数，`groups=None` 时数值必须与现状**逐位相同**（已有测试
`test_none_groups_path_is_bit_identical_to_production_stats` 守这一点）。

### Q2 现有账本是否真存在同一 `match_id` 多行？—— 不存在（但风险在门口）

| source | 行数 | 去重 match_id | 重复组 | 去重 match_date | 最大单日行数 |
|---|---|---|---|---|---|
| KNN | 2779 | 2779 | **0** | 22 | 627 |
| candles_ensemble | 181 | 181 | **0** | 3 | 104 |
| market_baseline | 6082 | 6082 | **0** | 29 | 772 |

- **同一 match 跨 source 并存**：`KNN∩market_baseline = 2153`、`KNN∩candles = 71`、
  `candles∩market_baseline = 0` → 任何「把所有 source 的行混合 iid 重抽」的口径会把同一场计两次；
  分组键必须是 **`match_id`**，不能是行号。
- **T53 的 1.85× 不会自动传播到账本**：账本 `(match_id, model_source)` 唯一，按 match 分簇 ≡ iid
  （7 个种子实测 ci_low ∈ [−0.0158, −0.0134]，与 iid −0.0147 一致）。
- **但风险确实存在**：一旦有「每场多行」的 ingester 上线（T43 的 `prematch_candles_verdict`
  逐检查点入口、mh 语料回灌），iid 立刻失真。本轮用**记账本真实数据 + 复制 3 行/场**直接量出：

| source | 模拟行数 | iid 半宽 | 按 match 分簇半宽 | **膨胀比** |
|---|---|---|---|---|
| KNN | 8337 | 0.0232 | 0.0410 | **1.77×** |
| candles_ensemble | 543 | 0.0946 | 0.1651 | **1.75×** |
| market_baseline | 18246 | 0.0143 | 0.0241 | **1.68×** |

与 T53 在 mh 语料上测得的 **1.85×**（3.42 检查点/场）同量级 → **本条给出可复用的设计效应标定：
每场 m 行 ≈ iid 半宽低估 √m 量级（m=3 → 实测 1.68–1.77×）**。

### Q3 分组 bootstrap 与 T45「噪声带随 n 重算」的衔接

- **T45 的阈值口径不受分簇影响**：阈值取的是 placebo **点估计**超额分布的 p95，而点估计与重抽方式
  无关（本轮复算 KNN iid p95 = 0.1312、分簇 p95 = 0.1312，逐位相同）→ 阈值表无需重算。
- **受影响的只有「CI 下限」这条判据**（G2 的 `roi_ci_low>0` 与 G6 的 `paired_excess_ci_low>0`），
  而这正是放行率的决定量。**实测结果是放水不是收紧**（见 §2 的 N3，与本规格 §4 的安全直觉相反）。

---

## §2 本轮四条新实证

**N1 estimator 校核通过（本审计存在的前提）**：Monte Carlo（k=10 簇 × 10 行，200  trials）
- 真 iid 数据：iid 覆盖率 0.905 / 分簇覆盖率 0.905（一致，无系统性偏倚）
- 真分簇数据：**iid 覆盖率 0.590 / 分簇覆盖率 0.920**（名义 0.95）
→ 数据真分簇时 iid 口径下覆盖率塌到 0.59，即「假置信」；分簇实现把它恢复回 0.92。

**N2 分簇方向不单调 —— 账本上分簇是「变窄」**：KNN 日簇 CI 半宽仅为 iid 的 **0.66×**，
candles 0.85×、market 1.06×。根因：日内心 bet 差异远大于日间差异（ICC≈0），
按日重抽等于只保留日间方差 → 区间变窄。**这不是实现 bug**（N1 覆盖），是 ICC 的表现。

**N3 ⚠ 关键反直觉结论：按日分簇会把 KNN 的 G2 从 FAIL 翻成 PASS，且稳定不可归咎于随机种子**
- iid：`roi_ci_low = −0.0147` → G2 不过（`candles` −0.1891、`market` −0.0306 同样不过）
- 按 match_date 分簇：ci_low = **+0.0012 … +0.0034（7 个种子全覆盖，摆幅仅 0.0021）**
  → 若采用分簇口径，KNN 将变成「ROI 显著为正」，**与 P0 FAILED / 三源 NO EDGE 直接冲突**。
- 这是本条给 FAIL 判定的唯一原因：**存在一条会让 EDGE 凭空出现的路径**。
- 旁证：分簇口径下 placebo 的 G6 放行率 **market_baseline 12/40 = 0.30**（T41 iid 口径为 23/100），
  即分簇**抬高**了假阳性放行率，与 T45「校正只会收紧不放水」的表述相反。

**N4 簇数过小时区间退化**：`candles_ensemble` 仅 3 个 match_date，7 个种子的 ci_low 摆幅 **0.0000**
（完全确定性）→ 区间不再是"宽"而是"死"，既不能证明也不能证否，属不可用口径。

---

## §3 落地规格（改动面最小、方向最安全的版本）

### S1 接口（`verification/stats.py`，纯增量）

```python
def roi_ci_bootstrap(returns, alpha=0.05, n_boot=10000,
                     groups: Optional[Sequence[Any]] = None) -> Tuple[float, float]:
    """groups=None → 现有逐行重抽（数值与旧版逐位相同）。
    groups 提供 → 标准分簇重抽: 抽 k 个簇(可重复), 取被抽中簇的全部行
    (= 给簇 c 的行加随机权重 W[r, c], W = 簇被抽中次数)。"""
```

- 分簇实现走**权重矩阵**而非 `(n_boot, n_rows)` 索引矩阵：后者在 22 簇 × 627 行时
  会临时分配 2 千万级 int64（本轮实测直接 OOM 级膨胀），权重形式 `W @ sums / W @ sizes` 等价且 O(n_boot·k)。
- `roi_ci_t` 同步加 `groups` 参数（t 口径不能只用于对照，否则 G6 的 t 敏感性对照会与 bootstrap 口径不对称）。
- **`metrics.py` 两处调用必须同时传 `groups`**：`:53` 传 `match_id` 列表，`:152` 传**同一批** `match_id`
  （配对差与基准 payoff 必须共享分组键，否则配对性在重抽中被破坏 —— 这是比宽度更致命的失效）。

### S2 聚类键的选法（硬约束）

| 场景 | 聚类键 | 理由 |
|---|---|---|
| 账本每场一行（现状） | `match_id` | 唯一 → 设计效应 1，与 iid 一致（N2 反例不适用）|
| 未来每场多行入口（T43） | `match_id`（**必须**） | m=3 时低估 1.7×（§1 Q2 表）|
| 敏感性对照（不参与判定） | `match_date` | ICC≈0 时区间反而变窄，**只能当对照不能当判据** |

### S3 门禁（V1-V8 fail-closed）

| 编号 | 断言 |
|---|---|
| V1 | `groups=None` 的输出与改造前**逐位相同**（已有测试守卫）|
| V2 | `metrics.py:152` 的分组键与 `:53` 同源同长（配对性）|
| V3 | 簇数 < 30 时**禁止**用分簇 CI 作唯一判据，必须标 `CLUSTER_TOO_FEW` 并降级为对照 |
| V4 | 三源结论一致性守卫：任一 source 的 iid 与分簇口径结论不同 → 报告 FAIL（本轮 KNN 即触发）|
| V5 | G6 常数阈值（T45）改为随 `n_clusters` 记录版本号 |
| V6 | 不得出现"分簇 CI 唯一放行"的判定路径（防 N3 复现）|
| V7 | 零 `events.db` 写入、零 `verification/` 写入、零新调度方 |
| V8 | 账本 `(match_id, model_source)` 唯一不变式，由 ingest 侧静态守卫保证（下游分簇正确性的前提）|

### S4 验收 A1-A6 / 回滚 R1-R3（摘要）

- A1 全量 pytest 绿（含 `test_gates_g6_thresholds.py` 前置，T45 V1）
- A2 9042 行账本重算后三源 verdict 不变
- A3 `roi_ci_bootstrap(v)` 旧调用签名零改动
- A4 分簇 CI 在 `n_clusters>=30` 且 ICC>0 的合成数据上覆盖率 ≥ 0.90
- A5 测试 `test_g6_reproducibility.py::test_bootstrap_seed_documented` 同步（新增参数不得改默认种子）
- A6 生产面零新增写方
- 回滚 R1（去掉 `groups` 参数）R2（回退 `metrics.py` 两处传参）R3（删新增测试）均无副作用

---

## §4 强制执行顺序与诚实边界

**顺序：`S1 接口 → S2 聚类键 → V3 簇数门槛 → V4 一致性守卫 → V6 禁止单路径放行 → 其余门禁`**
（V3/V4/V6 必须在 `metrics.py` 传参**之前**就位，否则会先出现"分簇判 EDGE"的污染结论。）

**诚实边界**
1. 本条**不产生样本、不推进 G1、不产生 edge**；只问清「现有 CI 的独立性假设是否成立」。
2. 现状结论不变：KNN/candles/market 三源仍 NO EDGE / INCONCLUSIVE，与 P0 FAILED 一致
   ——本条唯一的作用是**封死一条会让 KNN 凭空变正 ROI 的路径**。
3. **分簇口径不得单独作为 G2/G6 判据**：N3/N4 实证它在簇数少时既不放宽也不收紧，而是**换答案**。
   若未来确要启用，必须与 iid 口径**双口径一致**才算通过（`gates.py:132` G6 失败无条件 NO EDGE 保持不变）。

**未决**
- Q-a 聚类键默认取 `match_id` 还是由调用方显式传（倾向显式，避免隐式默认）
- Q-b `roi_ci_t` 是否同步加分组参数（t 口径无分组时只作对照）
- Q-c V3 的 30 簇门槛是在 `metrics.py` 硬编码还是在 `stats.py` 抛异常
- Q-d T45 的 `market_baseline` EXEMPT（结构性恒 0）是否随分簇一起重审
- Q-e mh 语料是否要走本规格重新入账（涉及 T53 的 ±0.001 判定带，本轮未动）
