# P-TEST — G6 零信息机械对照阈值校正落地规格（WINDOW 备料 · 纯规格）

> 生成：2026-09-27（自动化 dbda4380 · T45）
> 承接：**T41** `reports/g6_reproducibility_audit.{json,md}`（2026-09-27 04:41Z 落盘，9042 行账本，只读）
> 状态：**纯规格，不落地**。未改 `verification/gates.py` / `verification/metrics.py` / `verification/constants.py`，
> 未跑验证台，未写 `verification.db`，未重训，未碰 events.db，未碰任何生产进程。

---

## 0. 结论摘要（先读这里）

| 项 | 内容 |
|---|---|
| 待修缺陷 | G6 的零假设阈值是常数 **0**，但实测**纯噪声下配对超额的分布在多个 source 上整体右移**（market_baseline 中位数 **+0.0550**、97% 的随机策略超额 ≥ 0）→ 用 0 当阈值会把"噪声带内"的观测误当成"超额"，也把"显著低于噪声带"的观测报成"无超额" |
| 实测假阳性率 | 纯随机选边 placebo 100 次，**market_baseline 有 23/100（23.0%）同时通过 G2+G6**，名义对照 ≈2.5% |
| 校正方案 | 把阈值从常数 0 换成**同批零信息噪声带的分位数**（默认 p95），阈值随账本快照重算并带版本号缓存 |
| **方向性安全护栏（关键）** | 三个 source 的 p95 分别为 **0.1274 / 0.2966 / 0.0974，全部 > 0** → 校正**只会抬高门槛、绝不放水**，结构上不可能靠改闸门制造 EDGE |
| **启用后结论不翻转** | 观测配对超额 +0.0233 / −0.1023 / 0.0000 全部低于各自 p95，经验 p=0.46/0.65/0.97 全在带内 → 三源**仍判 NO EDGE**，与 P0 FAILED 一致 |
| 最大工程风险 | `verification/gates.py` **当前 0 个直接测试**（全仓 grep 仅命中审计脚本自身）→ 改判据等于在无回归网的情况下改唯一三态出口，必须先补测试再动代码 |
| 本轮未决 | G2 的放行率同样离谱（market_baseline 31/100），但本规格**不校正 G2**，列为 Q-d |

---

## 1. 现状判定式与缺陷定位

### 1.1 判定式（逐行引用，改动前必须逐字对齐）

| 位置 | 代码 | 语义 |
|---|---|---|
| `verification/metrics.py:137-153` | `build_bundle` 的 G6 段 | 基准 = 每行 `devig_h/d/a` 的**最短者**（并列按 `home < draw < away` 稳定取）；`mech_pay = (d[sh]-1) 命中 else -1`；`diffs = 模型 payoff − 基准 payoff`；点估计 `roi_point(diffs)`，CI 用 `roi_ci_bootstrap(diffs)` |
| `verification/gates.py:68-72` | `g6_pass = paired_excess_ci_low is not None and paired_excess_ci_low > 0.0` | **阈值 = 常数 0** |
| `verification/gates.py:132-145` | `if (not g2) or (not g3) or (not g6): → NO EDGE` | G6 失败无条件判 NO EDGE（**好性质：收紧 G6 不会被 G2 绕过**） |

去水口径 SSoT 为幂法（`pipeline/odds_math.py::devig_power`），账本 9042 行 `devig_*` 全非空、无静默 drop，
配对集 = 全集（T41 Q3 已证）。本规格只动**判定阈值**，不动去水与配对公式。

### 1.2 三个 source 的实测基线（T41，n_boot=10000）

| source | 配对行 n | 模型 ROI | 零信息买热门 ROI | 配对超额 | ci_low | ci_t_low | 严格 G6 |
|---|---|---|---|---|---|---|---|
| KNN | 2779 | +0.024836 | +0.001574 | **+0.023261** | −0.017632 | −0.017786 | FAIL |
| candles_ensemble | 181 | −0.027022 | +0.075240 | **−0.102262** | −0.279191 | −0.283946 | FAIL |
| market_baseline | 6082 | −0.006153 | −0.006153 | **0.000000** | 0.000000 | 0.000000 | FAIL |

### 1.3 零假设分布的形状（本轮从 T41 JSON 复算的新增证据）

T41 落盘的 `placebo.excesses`（每 source 100 个纯随机选边 trial 的配对超额点估计）：

| source | 均值 | 中位数 | 最小 | p95 | 观测超额的经验 p | 超额 ≥ 0 占比 | 超额 ≤ 0 占比 |
|---|---|---|---|---|---|---|---|
| KNN | +0.03107 | +0.02052 | −0.06578 | **+0.12736** | 0.46 | 72% | **28%** |
| candles_ensemble | −0.00925 | −0.04838 | −0.39505 | **+0.29662** | 0.65 | 41% | **59%** |
| market_baseline | +0.05354 | +0.05496 | −0.01581 | **+0.09741** | 0.97 | **97%** | **3%** |

**三条实证含义：**

1. **零假设不以 0 为中心。** market_baseline 的随机策略中位数 **+0.0550**，只有 3% 的随机策略能拿到 ≤ 0 的超额。
   它的观测超额恰为 0 → 经验 p = 0.97，字面读法是"显著优于纯噪声"，但真实含义是"**显著差于零信息基准**"。
   常数阈值 0 把这两种截然相反的结论压成了同一个数字，**判据本身不可解释**。
2. **噪声带宽度随 n 剧烈变化。** candles 横截面波动 SD 达 0.1858（KNN 0.0523、market 0.0305），n 只有 181 → p95 高达 0.2966。
   同一判据在小样本上等于关门、在大样本上等于开门，**阈值必须随 n 走，不能写死**。
3. **偏度不是主因（诚实排除，T41 已证）。** 配对差 skew ≈ +1.0/+1.2，但 bootstrap 下尾只比 t 下尾高 +0.0003/+0.0069。
   因此本规格**不采用**"换 t 区间"这条路，只做阈值分位校正。

---

## 2. 校正口径定义（规格核心）

### 2.1 定义

设某 source 的账本配对集为 `D`（行数 `n_paired`），零信息策略族为"对每行独立均匀随机选边"的 100 个 trial
（种子 `7000+t`）。定义统计量

```
T = paired_excess_ci_low        # 决策统计量，与 gates.py 用的判据统计量为同一个
```

其零假设分布 `F0` 由 placebo 复算得到。校正后的判据：

```
G6 pass  ⟺  T > q95(F0)         # q95 = 零假设分布的上 95% 分位
```

**为什么用 `ci_low` 的分位而不是"点估计超额"的分位**：判据统计量必须与噪声带统计量同一个量。
若改成 `paired_excess >= p95(point)`，等于同时用了"点估计噪声"和"区间显著性"两次噪声校准，
判定式不可解释（T41 标题里的 `required_excess_p95` 属点估计口径，**只能当敏感性对照，不能当主口径**）。

### 2.2 ⚠ T41 的一处数据缺口（必须先补再落地）

`scripts/audit_g6_reproducibility.py::placebo_random` 只把 `g["paired_excess"]`（点估计）写入 `excesses`，
**没有记录 placebo 的 `ci_low` 分布** → 主口径所需的 `q95(F0)` 目前**不存在数字**。
故 §2.3 的脚本接口必须重新输出该字段；本规格**不替它编数字**。

### 2.3 脚本接口（`scripts/calibrate_g6_threshold.py`，本轮只定义、不实现、不运行）

```
python scripts/calibrate_g6_threshold.py \
    --db verification.db \
    --trials 100 --seed0 7000 --n-boot 1000 --quantile 0.95 \
    --out reports/g6_threshold_cache.json
```

输出 `g6_threshold_cache.json` 的**必填键（缺一键即 fail-closed，退回 strict 并告警）**：

| 键 | 类型 | 说明 |
|---|---|---|
| `source` | str | model_source |
| `n_paired` | int | 参与标定的配对行数 |
| `quantile` | float | 0.95 |
| `threshold_ci_low` | float | **主口径**：零假设 ci_low 分位 |
| `threshold_point_p95` | float | 敏感性口径（T41 数字，可选但须存在） |
| `null_median` | float | 零假设中位数 |
| `trials` / `seed0` / `n_boot` | int | 复现参数 |
| `generated_at` | str | UTC ISO8601（本地时钟，与账本 `created_at` 同域） |

缓存读取的 fail-closed 矩阵：

| 条件 | 行为 |
|---|---|
| 文件缺失 / JSON 损坏 | 退回 `strict`，报告标 `g6_mode=strict` |
| 缺任一必填键 | 退回 `strict` |
| `threshold_ci_low <= 0` | **退回 strict + WARN**（说明配对结构与零假设不符，不能当"放水"用） |
| `n_paired < G6_CALIB_MIN_N` | 退回 strict |
| 报告产物里 `g6.mode != cache.g6_mode` | 拒绝产出（防止 T38/T42 式的"抄值"污染） |

### 2.4 何时重标定

阈值随 `n_paired` 变化（≈ 1/√n）。规则：**每次生成验证台报告前必须重算**；
缓存命中判定用 `(source, n_paired, seed0, trials, quantile, n_boot)` 全键比对，任一变化即重算。
缓存内容必须一并写进报告产物（`g6.threshold / g6.mode / g6.calibrated_at / g6.noise_n`），
否则下游又会踩 T38 的"抄值"坑。

---

## 3. 代码修改点（逐文件、逐行级）

### 3.1 `verification/constants.py` — 新增常量

```python
G6_MODE: str = "strict"        # strict | calibrated; 环境变量 SX_G6_MODE 可覆盖(WINDOW 内)
G6_NOISE_QUANTILE: float = 0.95
G6_PLACEBO_TRIALS: int = 100   # 与 T41 对齐; <100 不启用
G6_PLACEBO_SEED0: int = 7000
G6_PLACEBO_N_BOOT: int = 1000
G6_CALIB_MIN_N: int = 100      # 配对行少于此值不启用校准
```

（需同步加入 `__all__`。）

### 3.2 `verification/metrics.py` — `MetricsBundle` 加字段

```python
g6_mode: str = "strict"
g6_threshold: Optional[float] = None    # 校准阈值(主口径 = 零假设 ci_low 分位)
g6_noise_n: Optional[int] = None        # 参与标定的配对行数
g6_null_median: Optional[float] = None
g6_empirical_p: Optional[float] = None  # 观测超额在零假设分布中的经验 p
g6_pass_strict: Optional[bool] = None   # 未加校准前的原始 G6 判据, 供回归对比
```

`build_bundle` 的 G6 段末尾（`metrics.py:153` 之后）追加：

```python
bundle.g6_pass_strict = bool(bundle.paired_excess_ci_low is not None
                             and bundle.paired_excess_ci_low > 0.0)
if _g6_mode() == "calibrated" and len(diffs) >= G6_CALIB_MIN_N:
    rec = _g6_calibrate(rows, diffs, ...)     # 见 §2.3, 固定种子, 绝不写库
    bundle.g6_threshold = rec["threshold_ci_low"]
    bundle.g6_noise_n = rec["n_paired"]
    bundle.g6_null_median = rec["null_median"]
    bundle.g6_empirical_p = rec["empirical_p"]
    bundle.g6_mode = "calibrated"
```

不许做的事（**与 T43 同族的"禁止三条"**）：
- 禁止把 `devig_*` 换成任何非幂法口径；
- 禁止在标定脚本里碰 `verification.db` 以外的写路径、禁止写任何库；
- 禁止把 `chosen_outcome` 反算成赔率再配对（会让 G6 退化成"自己跟自己赌"，T41 §market_baseline 已演示该病）。

### 3.3 `verification/gates.py` — `Gates.check` 的 G6 分支

```python
g6_pass = bundle.g6_pass_strict                       # 保留原语义, 方向性不变
if self.g6_mode == "calibrated":
    g6_pass = g6_pass and (bundle.g6_threshold is not None)
                      and (bundle.paired_excess_ci_low > bundle.g6_threshold)
```

`Gates.__init__` 增加 `g6_mode: str = G6_MODE`, `g6_quantile: float = G6_NOISE_QUANTILE`。

### 3.4 `verification/gates.py::verdict` — 文案必须能区分三种失败

现状 `gates.py:140-144` 对 G6 失败的统一措辞是"表观 ROI 由去水偏差/热门倾向驱动"。
**T41 已证该措辞对 `market_baseline` 事实错误**（它恒等于最短 devig 边，配对差恒 0，是"拿自己当自己的对照"）。
故三态出口必须分开写：

| 情形 | 措辞 |
|---|---|
| strict 未过 | 原措辞（去水偏差/热门倾向驱动） |
| calibrated 未过，且 source == `market_baseline` | **EXEMPT**："该 source 的选边恒等于零信息基准，配对差结构性为 0，不适用机械对照判负；其意义是'市场基线无独立信息'，须单列说明而非判负" |
| calibrated 未过，其他 source | "配对超额 CI 下限未超过本批零信息噪声带 p95={threshold}（观测配对超额={paired_excess}，经验 p={p}）→ 不构成零信息对照之上的超额" |

### 3.5 `verification/report.py` — 产物必须带阈值指纹

新增 `bundle.g6_mode / g6_threshold / g6_calibrated_at / g6.noise_n` 到报告 JSON，
并在 `--check-threshold` 模式下对缓存做 fail-closed 比对。

---

## 4. 诚实约束（写进规格，防止"改了闸门就有 edge"的误读）

### 4.1 启用后结论不翻转（本轮已算，不是承诺）

| source | 观测配对超额 | 阈值 = p95 | 观测是否达阈值 | 经验 p | 校正后 G6 |
|---|---|---|---|---|---|
| KNN | +0.023261 | 0.127356 | 否 | 0.46 | **FAIL** |
| candles_ensemble | −0.102262 | 0.296622 | 否 | 0.65 | **FAIL** |
| market_baseline | 0.000000 | 0.097410 | 否 | 0.97 | **FAIL** |

三源全部仍 FAIL → `verdict()` 输出全部仍为 `NO EDGE`，与 P0 干净判定（FAILED）一致。
**规格正文必须原样保留这张表**，让任何人改闸门前先看到"收紧后结论不变"。

### 4.2 方向性安全护栏（结构性保证）

- 所有已实测 p95 **> 0** → 校正只会抬高门槛。
- 理论上 q95 是右尾分位数，只要零假设分布非退化就 > 0；仍加 fail-closed 断言 `threshold > 0`，
  否则退回 strict 并 WARN（说明配对结构与零假设不符，需人工介入而非静默放水）。
- `gates.py:132` 的 `if (not g2) or (not g3) or (not g6): → NO EDGE` 保持不变
  → **收紧 G6 无法通过"只过 G2"绕过**。

### 4.3 反向误读警告（三条，写进验收）

1. 阈值校正**不是**为了让某个模型通过，而是让"通过"这个字有意义；
2. 若将来某 source 的观测超额越过 p95，正确解读是"该模型可能真有零信息之上的超额"，
   但仍须**独立复算**（换种子、换 trials、换 n_boot）后才可报，不能一次通过即宣称；
3. 阈值随 `n_paired` 变，样本增长后旧缓存不可复用 —— 缓存里必须带 `n_paired`，否则报告不可复现。

---

## 5. 回归门禁与回滚（fail-closed）

落地（真正的 WINDOW 执行）前必须先绿：

| 编号 | 门禁 | 期望 |
|---|---|---|
| V1 | 全量 pytest（当前基线 **443 passed**）+ 新增 `tests/test_gates_g6_thresholds.py` | 不退化。**`verification/gates.py` 当前 0 直接测试，此测试是本规格的前置条件，不是可选项** |
| V2 | 9042 行账本 strict / calibrated **双路逐行 verdict 对账** | 变化行数 = **0**（三源均因 ci_low ≤ 0 已 FAIL，抬高阈值不改变 FAIL） |
| V3 | 方向性护栏断言 | 无任何 source 从 NO EDGE / INCONCLUSIVE 翻到 EDGE |
| V4 | strict 模式回归 | `Gates.check` 全部 `passed` 与 `detail` 字符串与 `git HEAD` 逐位相同 |
| V5 | 字段完整性 | `g6_mode / g6_threshold / g6_noise_n / g6_null_median / g6_empirical_p` 无配对行时须显式 `null`（不能静默省略） |
| V6 | 缓存键完整性 | 六必填键齐全且 `threshold_ci_low > 0`（见 §2.3 矩阵） |
| V7 | 阈值漂移可见性 | 阈值变化时 `reports/verification_report.json` 对应字段必须变化，diff 可见（禁止跨快照抄值） |

回滚：

| 编号 | 动作 |
|---|---|
| R1 | 环境变量/配置切回 `SX_G6_MODE=strict`（无需发版） |
| R2 | 删除 `reports/g6_threshold_cache.json`（缓存缺失自动退回 strict，无需发版） |
| R3 | `git revert` 本次 gates/metrics/constants/report 改动 |
| R4 | 复原验证台报告产物（本报告为只读产物，不覆盖历史三态） |

---

## 6. 未决问题（须人工决策，本轮不拍板）

- **Q-a 主口径选择**：本文推荐 `ci_low` 分位（§2.1）；`paired_excess` 点估计分位（T41 口径）只作敏感性。
  落地前须实算一次 `threshold_ci_low` 并确认 §4.1 的不翻转表仍成立。
- **Q-b `market_baseline` EXEMPT**：是把它移出 G6 判定面，还是保留但单独标注？属产品决策，本轮不改结构。
- **Q-c 阈值缓存位置**：放 `reports/`（会被下游抄值，T38/T42 前车之鉴）还是独立路径？
  建议独立，但牵扯 T37 的"账本路径白名单"前置项 P1。
- **Q-d ⚠ G2 同样失真**：placebo 中 `market_baseline` 单独过 G2 达 **31/100（31.0%）**，
  **比 G6 的 23/100 还离谱**。本规格只校正 G6；若只修 G6 而 G2 仍按常数 0 判定，
  "零信息策略靠 G2 放行"的路径仍在（虽然 G6 失败仍会把它压成 NO EDGE）。
  **强烈建议把 G2 的噪声带校正列为独立任务，与本条同窗口落地。**
- **Q-e 标定成本**：100 trials × 1000 bootstrap × 3 source ≈ 可接受，但须在报告生成器里做缓存，
  否则每个周期重算会拖慢自动化。

---

## 7. 红线自查

| 红线 | 本轮状态 |
|---|---|
| IR-30 诚实（无 edge 不宣称盈利） | 维持；本规格全部数字来自只读审计，结论仍为 NO EDGE |
| IR-32 跨庄禁区 | 未触碰跨庄面 |
| §4 数据资产保全 | 未写 `events.db`；未开写连接 |
| §6 不杀进程 | 未碰任何进程 |
| 售卖人工+法务双签 | 未涉及 |
| WINDOW 项 | 本条为 WINDOW 备料，仅定义脚本接口，**未实现未运行** |

零写入：未改 gates/metrics/constants、未跑验证台、未写 `verification.db`、未重训、未碰生产。
