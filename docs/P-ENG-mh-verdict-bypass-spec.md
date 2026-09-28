# P-ENG — mh 训练脚本「绕过 gates.verdict」真修复评估 + 静态守卫规格（T53）

> 承接 T51 R6（`scripts/mh_train_div.py:67` 自印三态判定）。
> 性质：**纯只读盘点 + 规格**；不改 `verification/`、不跑训练脚本、不写任何库、不碰调度、零进程操作。
> 证据：`reports/mh_train_div_bypass_audit.{json,md}`（由 `scripts/audit_mh_train_div_bypass.py` 生成）。

---

## 0 一页答案

| 问题 | 答案 |
|---|---|
| ① 语义差在哪 | mh 的判定 = **单门禁 G3**（`ΔLL<0`，硬阈值 ±0.001，**无置信区间、无样本量约束**）；`gates.verdict` = **六门禁合取** `G1∧G2∧G3∧G4∧G5∧G6`，且 `EDGE` 必须与 ROI CI 下限、**零信息机械对照**同时成立。符号约定两者同向（负 = 模型更优），`vs_market_ll` 是唯一 1:1 可映射字段。 |
| ① 改走 gates 会不会改变结论 | **会，且只翻转一支**：`BEATS MARKET`（mh 的唯一正面 sounding 标签）在 gates 下只会变成 `NO EDGE`（缺 G2/G4/G5/G6）；`TIE` / `NO EDGE` 两支结论不变。→ 这是**修 bug 而非改口径**，因为翻转方向是收紧。 |
| ② 复用 gates 缺什么 bundle | 缺 **8 个字段**：`roi_point / roi_ci_low / roi_ci_high / roi_method` + `mech_fav_roi / paired_excess / paired_excess_ci_low` + `model_source`。现成 1 个（`vs_market_ll`），可算 7 个（`n/log_loss/brier/ece/slope/accuracy/direction_binomial_p`）。 |
| ③ 守卫失败阈值语义 | 不是「命中数 ≤ N」，而是 **集合相等**：白名单外出现任何判定字面量 = FAIL（fail-closed）。已登记项**消失**（退役未登记）另算 AMBER，不允许静默消失。 |

**判定：`FAIL`**（4 条 RED）。本条**不产生样本、不推进 G1、不产生 edge**。

---

## 1 事实基线（本轮实测，全部只读）

| 项 | 数值 |
|---|---|
| mh 数据集 | `data/mh_dataset_x.csv` 6000 行 / **1628 场** / 检查点 {0,45,60,75,85}；标签列 `ridx∈{0,1,2}`（已核验为终场 1X2，**不是行号**，见 §7 反证） |
| 判定产出体 | **3 个**（不是 T51 说的 1 个）：`mh_train_div.py:67`、`mh_train_walkforward.py:118`、`mh_train_walkforward_x.py:82` |
| walk-forward 折切分 | cut 0.60/0.80/0.90 → 测试集 652/326/163 场 → 2262/1106/534 **样本** |
| 聚合 | 样本 **n=3902** / 场次 **m=1141** / 每场检查点 **3.42** / market LL **0.8069** |
| 噪声底（按场次聚类 bootstrap） | SD = 0.01941 / 0.02868 / 0.03770（三折） |
| mh 判定带 | `±0.001`，即 **最窄折处噪声的 0.0265×σ** |
| 去水口径 | `mh_build_dataset.fair_probs` = **比例法**（`1/odds` 归一 = devig_n/devig3），无幂法 |

---

## 2 Q1 产出体盘点与 T51 漏检勘误

**勘误（对 T51 的修正）**：T51 只把 `mh_train_div.py:67` 列为「未分层产出体候选」，实际上 **mh 家族三个文件**都在自印判定，而其中两个被 T51 漏检。

漏检根因（可机械复现）：T51 的正则要求字面量**两侧带引号**
`RE_VERDICT_LITERAL = (?:"|')(NO_EDGE|NO EDGE|EDGE|INCONCLUSIVE)(?:"|')`，
而：

- `mh_train_walkforward.py:118` → `... else ('NO EDGE (model >= market)')}")` 尾部无引号;
- `mh_train_walkforward_x.py:82` → 同一形状。

本轮改用**放宽版**词表（去引号约束、词边界替代）：

```
(?<![A-Za-z0-9_])(?:NO_EDGE|NO EDGE|INCONCLUSIVE|EDGE|BEATS MARKET|TIE)(?![A-Za-z0-9_])
```

**词汇撞车继续扩大**：本轮新发现 `scripts/backtest_ou_signal.py:39` 的 `NO_EDGE` 是**信号分类词**
（`backend_signal()` 返回 `OVER/UNDER/NO_EDGE`，与 `analysis/live_goal_probe.py` 同族语义 =「不下注」），
位于 `scripts/` 而非 `analysis/`，因此 T51 的按目录前缀噪声豁免**覆盖不到**——
这是「词汇撞车」从目录豁免演进为**必须逐文件登记**的理由。

---

## 3 Q2 语义差与 bundle 缺口

### 3.1 语义差表

| 维度 | mh 自印判定 | `Gates.verdict` |
|---|---|---|
| 输入 | 两个 LL 的差 `model_ll − market_ll` | `MetricsBundle`（16 字段） |
| 阈值 | 硬编码 `±0.001` | G3 用 `< 0`，其余门禁各有阈值（ECE/斜率/p 值/CI） |
| 显著性 | **无**（判定带比噪声细 1–2 个数量级，见 §5） | G2/G5/G6 均带置信区间或检验 p 值 |
| 样本量 | 不做约束（脚本只打印 `total test samples`，未过线判断） | G1 `n ≥ 2500`（默认 `MIN_SAMPLE`），**不足直接 INCONCLUSIVE** |
| 组合逻辑 | **单门禁**结论 = 判定 | **六门禁合取**：`EDGE` 需 G2∧G3∧G4∧G5∧G6 全绿 |
| 反面解释 | `NO EDGE` = 模型**劣于**市场 | `NO EDGE` = 样本足但无统计边缘 / 伪影 |
| 出口唯一性 | 3 个脚本各写各的 | `gates.verdict` 是唯一三态出口（`verification/` 包内 8 处字面量均在 `gates.py`/`report.py`） |

### 3.2 会不会改变现有结论（确定性映射，纯函数可测）

| mh 标签 | mh 含义 | `gates.verdict` | 结论是否翻转 |
|---|---|---|---|
| `BEATS MARKET` | ΔLL < −0.001（模型优于市场） | **NO EDGE**（缺 G2/G4/G5/G6） | **是（收紧）** |
| `TIE` | \|ΔLL\| ≤ 0.001 | NO EDGE（G3 不成立：0 不小于 0） | 否 |
| `NO EDGE` | ΔLL > 0.001（模型劣于市场） | NO EDGE（G3 不成立） | 否 |

→ **只有唯一一支的标签与 gates 不一致，且方向是收紧**。因此「改走 gates」应定性为
**修 bug（口径自洽化）**，不是「改口径」（不是把标准放宽或收紧到另一套数值）。

### 3.3 复用 gates 需要的 bundle（缺 8 个）

| 字段 | 状态 | 需要的改造 |
|---|---|---|
| `n` | 可算 | 直接填 3902（**但口径有问题，见 §4**） |
| `vs_market_ll` | **现成** | 与 mh 的 `d` 同义；注意两边都来自同一份比例法 `imp`，**差值自洽但绝对偏差照旧** |
| `log_loss/brier/ece/slope/accuracy/direction_binomial_p` | 可算 | 需概率行喂 `calibration_bridge`；`n` 的口径同 G1 问题 |
| `roi_point/roi_ci_low/roi_ci_high/roi_method` | **缺** | mh 完全没有 payoff 概念 → 必须新建「(比赛, 检查点) → 选边 + 结算 payoff」的纸盘逐行，且**去水口径须换幂法** |
| `mech_fav_roi/paired_excess/paired_excess_ci_low` | **缺** | 需在同批比赛上先算「无脑买最短赔率」的机械 ROI 再取配对差 + bootstrap CI |
| `model_source` | **缺** | 且 `verification/constants.py::MODEL_SOURCES` 只登记 `candles_ensemble/market_baseline/KNN` → **即使造出 bundle，`report._build_bundles` 也永远不会拾取它**（需加源或走旁路入口） |

**顺序硬约束**：`MODEL_SOURCES` 或旁路入口（Q-d）→ devig 换幂法重建数据集 → 纸盘逐行 → G6 三元组 → 才谈得上 G1/G2。

### 3.4 附带阻塞：去水口径

`mh_build_dataset.fair_probs()` 用比例法（1/odds 归一）。按 §去水口径铁律，
**用它反算赔率搭 G6 机械基准会复刻 09-23 事故**（机械基准被高估 → `paired_excess` 失真）。
但 `vs_market_ll` 一侧市场基线同源同口径，**差值是自洽的**，可直接映射。
→ G6 不可与 G3 同批落地，必须分开（同 T51 §Q-b 的立场：先只强制 G6 三元组）。

---

## 4 Q3 G1 计数口径二难（本轮新发现的结构性缺陷）

同一批数据、同一套折切分，**两种计数必有一边失真**：

| 计数口径 | n | G1（≥2500） | 问题 |
|---|---|---|---|
| 按**样本**（脚本口径） | 3902 | ✅ 过 | 同一场 3.42 个检查点共用**同一个终场结局**；若当 3.42 注下单，ROI 点不变但方差被摊薄 → 朴素 iid 置信区间**低估 1.85×**（√3.42） |
| 按**场次**（独立结局单元） | 1141 | ❌ 不过 | 这才是结算层的真实独立单元（G2/G6 的 bootstrap 必须按此重抽） |

含义：**mh 这条路径在任一口径下都拿不到 gates 认可的 EDGE**——按样本会误放行 G1，按场次连门都进不去。
这也对 `verification` 自身提出要求：`verification/ledger` 的 `n` 是「已结算注数」，
其聚类结构（同场多注）必须在 G2/G6 的 bootstrap 里按 match 分簇重抽，否则跨模型复用会系统性低估不确定性。

---

## 5 Q4 判定带 vs 噪声底（为什么 ±0.001 不算证据）

市场基线的聚类 bootstrap SD（按 match 整簇重抽，600 次，seed 20260928）：

| 折 cut | 测试场次 | SD(market LL) | ±0.001 / SD |
|---|---|---|---|
| 0.60 | 652 | 0.01941 | 0.0515 |
| 0.80 | 326 | 0.02868 | 0.0349 |
| 0.90 | 163 | 0.03770 | **0.0265** |

→ 判定带只有噪声的 **2.6%–5.2%**，即 **±0.001 远在噪声尺度以内**：处于带内的结果由抽样噪声决定，
跨过/跨不过这条线的标签翻转不携带任何信息。这与 T41/T45 对 G6 常数阈值 0 的批评**同形**，
说明「LL 差阈值」这一类判据都需要随 n 与聚类结构重算的噪声带（F0）而不是固定常数。

诚实边界：此处 SD 是**市场基线侧**的噪声（ΔLLM 的另一半来自模型侧，量级相当或更大），
故 0.0265×σ 是**下界**而非精确值。

---

## 6 静态守卫规格（G1–G4）

**设计立场**：守卫的目标不是「让 mh 变绿」，而是**防回退**——任何人把判定词改回自印，必须当场变红。

| 守卫 | 内容 | 失败阈值语义 |
|---|---|---|
| **G1 未登记产出体** | 全仓 `.py` 放宽版词表扫描；白名单外任何命中 | **集合相等**：`unexpected_count == 0` 才通过。不是「命中数 ≤ N」—— 漏检的代价是「绕过门控的结论被当成系统结论」，故 fail-closed |
| **G2 在册项存活** | `EXPECTED_EMITTERS` 三项必须存在且仍含判定字面量 | 存在性缺失 = **AMBER**（退役未登记，须补登记行）；字面量消失 = AMBER（防止改花样绕过） |
| **G3 逃逸通道** | 任一项改为 `import verification.gates` / 调用 `Gates().verdict` | 自动转 PASS，**不需要手工加豁免**（豁免只服务于「暂时不动」，见 Q-b） |
| **G4 跨审计回归** | T51 的 `scan_verdict_literal_sources()` 基线必须仍为 2 文件 | 漂移即 FAIL；新写文件含判定词必须走 self-exclude 或登记 |

**失败语义的三个硬规则**（防止守卫自己失效）：

1. **「没检查」不算「通过了」**：扫描未执行（异常/缺文件）→ 结论 FAIL 或 AMBER，不得静默 PASS。
2. **白名单只能逐条登记 + 写理由**：`NOISE_FILES` / `DOC_QUOTE_FILES` 每条必须带理由字符串，
   空理由不允许登记；新文件**永不**自动豁免。
3. **跨审计污染回归**：本仓库已经发生过两次「新脚本的注释把旧审计结论打翻」
   （T47 的 `SCHEDULED_CLI` 0→1；T49 的自排除字面量），故 G4 把「旧审计基线不漂移」本身变成断言。

---

## 7 验收与回滚

**A1** `scripts/audit_mh_train_div_bypass.py --check` 退出码 0（零未登记产出体且无 RED）；
**A2** `tests/test_audit_mh_train_div_bypass.py` 全绿（本轮 23 passed）；
**A3** 把 `mh_train_div.py:67` 改走 `Gates().verdict` → G1 保持绿（`unexpected_count` 不变）而 G2 转 AMBER
「判定字面量已消失，须显式登记为已修复」——**修复必须留下痕迹**，不允许静默消失（同 T51 §A4 的「只减不增」精神）；
**A4** 回退测试：往 `scripts/` 新增一个含 `NO EDGE` 字面的脚本 → G1 必须 FAIL（**防回退主用例**）；
**A5** 全量回归不退化（本轮 534 → 557 passed）；
**A6** G6 相关改造**不得**与 G3 同批提交（去水口径不同，见 §3.4）；
**A7** 任何 `MODEL_SOURCES` / devig 改动须走 WINDOW 停机窗口 + 回归（红线 §4/§6）。

**回滚**：本条只新增脚本/测试/规格三件套，回滚 = 删除三者，零副作用；不删历史报告。

**反证记录（避免后人重走弯路）**：已核验 `ridx` 是**终场 1X2 标签**（`mh_build_dataset.py:96`
`ridx = 0 if sh_f>sa_f else (2 if sa_f>sh_f else 1)`），**不是行号**——`LGBMClassifier` 的标签用法正确。
本轮曾把「标签用行号」列为候选根因，静态反证排除。

---

## 8 未决（Q-a…Q-d）

- **Q-a 三个 mh 文件一起改，还是只改 `mh_train_div.py`？** 三份语义与阈值完全相同，
  只改一个会让另外两个从「已登记绕过体」变成「未登记产出体」→ 守卫当场红。建议**同批改**。
- **Q-b 是「改为调 gates」还是「删掉判定词、只留数字」？** 若走 G3 的豁免通道（§G3），
  脚本仍打印自己的 ΔLL 但不再打三态标签；若彻底删除，需保留数字输出否则结论不可见。
  倾向前者（改标签、保留数字），因为 mh 的数字仍是唯一的历史证据。
- **Q-c G1 计数口径**：`verification/ledger` 的行是否允许「同一 match 多行」？
  若允许，bootstrap 必须按 match 分簇（本轮实测设计效应 3.42）；若不允许，则只能 cp=0 单.checkpoint 入账。
- **Q-d 旁路入口**：给 mh 造 bundle 后是通过扩 `MODEL_SOURCES` 还是走 `report` 之外的独立入口？
  扩注册表会让 `report._build_bundles` 对新源执行 `fetch_credible`（账本无该 source → 空），
  属无害但易误判；独立入口更干净，需与 T55（CLI 调度）同窗。

---

## 9 红线自检

| 红线 | 本轮状态 |
|---|---|
| IR-30 诚实 | 三支标签的 gates 映射为确定性结论；**不宣称任何 edge**；噪声底与计数口径均给出负面证据 |
| IR-32 跨庄禁区 | 未触碰跨庄面；本条不新增任何投注/共识输出 |
| §4 数据资产保全 | 只读 `data/mh_dataset_x.csv`（纯 CSV）；**零 events.db 写入、零 verification.db 写入** |
| §6 进程安全 | 零进程操作；不 import 任何训练入口（脚本只做 CSV/文本静态分析） |
| WINDOW | 全部整改项（G6 三元组 / devig 换幂法 / MODEL_SOURCES）只列规格，**未执行** |
| 售卖 | 未涉及 |
