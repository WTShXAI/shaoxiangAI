# P-TEST-calibration-coverage-spec — calibration/auto_predict 直接测试补强规格

> 来源：T20 测试覆盖缺口盘点（`reports/test_coverage_gap.{json,md}`）判定
> `calibration.py`（log_loss / ece / brier_score / build_calibration 全无直接测试触点）
> 与 `auto_predict.py`（predict_date / predict_range / plugin_predict 全无直接测试触点）为 HIGH 缺口。
> 本规格设计**直接 pytest 清单 + 诚实纪律断言（IR-30 / 去水幂法 / ECE 阈值 / G6 零信息对照）**。
>
> ⚠ 本文件为**纯规格，不落地任何测试代码**。落地须走回归门禁：`pytest tests/ -q` 全量通过 + T20 代理复测。
> 所有测试**只读**，绝不触碰 events.db / 生产库（用 FakeCursor / 临时 sqlite / monkeypatch）。

---

## 0. 设计原则

- **零 I/O 到生产库**：`build_calibration` / `fetch_*` 需要 cursor → 用 `FakeCursor`（记录 SQL、返回夹具），绝不连 events.db。
- **纯函数优先**：`log_loss / ece / brier_score / reliability / calibration_slope / devig_*` 直接喂确定性输入。
- **诚实纪律内置**：每个会"产出可信度/盈利性结论"的测试，必须带 IR-30 断言（无 edge 不宣称 / 幂法去水 / ECE 门控 / 零信息机械对照）。
- **不覆盖、不删**：仅新增 `tests/test_calibration_direct.py` 与 `tests/test_auto_predict_direct.py`；不改被测模块。

---

## 1. calibration.py 直接测试清单（→ `tests/test_calibration_direct.py`）

### 1.1 `log_loss(points)` — 4 用例
| 用例 | 输入 | 期望 |
|---|---|---|
| 空输入守卫 | `[]` | `math.isnan` True |
| 单点解析值 | `[(0.5, 1)]` | `≈ -ln(0.5) = 0.69315`（round 5 位） |
| 完美预测 | `[(0.9,1),(0.95,1),(0.1,0)]` | 显著低（< 0.2） |
| 概率裁剪 | `[(1e-20, 1)]` 经 eps 裁剪 | 有限值，不抛；值≈ `-ln(eps)` 上界 |

### 1.2 `brier_score(points)` — 4 用例
| 用例 | 输入 | 期望 |
|---|---|---|
| 空输入守卫 | `[]` | `nan` |
| 完美 | `[(1.0,1),(0.0,0)]` | `0.0` |
| 单点已知 | `[(0.5,1)]` | `(0.5-1)^2 = 0.25` |
| 裁剪+均值 | `[(2.0,1),(0.3,0)]` | p 夹到 [0,1]，均值正确 |

### 1.3 `reliability(points, nbins)` — 4 用例
| 用例 | 期望 |
|---|---|
| 空输入 | `[]` |
| 末桶含 p==1.0 | `(1.0, x)` 落在最后桶 |
| 概率裁剪 | p 越界值被夹到 [0,1] 后正确分桶 |
| 空桶跳过 | 稀疏输入不产生空桶条目；`err = emp - mean_pred` 符号正确 |

### 1.4 `ece(buckets)` / `calibration_slope(buckets)` — 4 用例
| 用例 | 期望 |
|---|---|
| ece 空 / total==0 | `nan` |
| ece 解析 | 已知桶 → `Σ(n_i/N·\|err_i\|)` 一致 |
| slope <2 点 | `None` |
| slope 已知 | 完美校准点集 → `≈1.0`；过度自信（emp 系统性低于 pred）→ `<1.0`；`denom==0` → `None` |

### 1.5 `build_calibration(cur)` 集成（FakeCursor）— 3 用例
- `FakeCursor`：实现 `.execute(sql)`（记录 SQL 串）、`.fetchall()`（按调用顺序返回预置行）。
- 用例 A：live 路径 — 注入 `bet_records` 已结算行（含百分比概率 + actual_result），断言返回 dict 含 `live.n`、`brier`、`log_loss`、`ece`、`slope`、`confidence` 分级（n≥200 high / ≥30 medium / <30 low）。
- 用例 B：historical 路径 — 注入 `odds_features`（cimp_* 和≈1，outcome∈H/D/A），断言 `historical.n`、`per_outcome` 含 H/D/A 三段、`confidence=="high"`。
- 用例 C：SQL 契约断言 — `FakeCursor` 记录到的 SQL 必须命中 `bet_records` 与 `odds_features` 两表（防止未来有人把校准悄悄改读他表/跨庄源，触 IR-32）。

### 1.6 `svg_reliability` / `render_html` — 2 用例
- `svg_reliability([])` → 返回含 `无数据` 的 `<p>`；非空 → 含 `<svg` + `<circle` + 对角线 `<line`。
- `render_html(cal)` → 含 `<!doctype html>` 与关键 stat 字段名（Brier/LogLoss/ECE）。

---

## 2. auto_predict.py 直接测试清单（→ `tests/test_auto_predict_direct.py`）

> `auto_predict` 的 `predict_range` 会触发 `PLUGINS`（standings_updater / odds_fetcher / tournament_dynamics 网络/DB 依赖）。
> 直接测试用 monkeypatch 隔离：patch `get_schedule` 返回确定性赛程，patch `PLUGINS` 仅留 `predict` 插件，或 patch 重插件 fn 为 no-op。

### 2.1 `plugin_predict(matches_with_odds, standings, matchday)` — 纯函数，3 用例
- 输入 2 场 `[(dt,h,a,oh,od,oa,hcp,ou), ...]`，`predict_with_scores` 不可用（自然 fallback lambda）。
- 断言：返回 `(results, standings, matchday)`；每个 result 含键 `date/home/away/odds/hcp/ou/verdict/winner/mode/scores/signals/lambda_h/lambda_a`。
- 断言：赔率→隐含概率计算正确（`ph=1/oh/(1/oh+1/od+1/oa)`），`mode` 在 fallback 时为 `"unavailable"`。

### 2.2 `predict_range` / `predict_date` — 3 用例
- monkeypatch `pipeline.auto_predict.get_schedule` → 返回 `[('6.25','A','B')]`；`PLUGINS` → 仅 `[predict]`。
- `predict_date('6.25')` 与 `predict_range('6.25','6.25')` 返回等价（结构一致、同一场）。
- `predict_range` 空赛程（`get_schedule` 返回 `[]`）→ 返回 `[]` 且不抛（对照源码 print+return [] 分支）。
- `predict_range('6.25','6.28')` 多日 → 结果按 date 分组键齐全。

### 2.3 `plugin_risk_assess(results, standings, matchday)`（顺带补，T20 未单列但同属零触点）— 3 用例
- Mode C 超级热门：odds `'1.25/5/10'` + verdict `'D'` → risks 含 `🔴 Mode C`。
- 窄 spread：`abs(ph-pa)<0.15` 且无其他风险 → 含 `🟡 窄spread`。
- 安全路径：无触发 → risks 含 `⚪ 安全`。

### 2.4 `_get_fallback_schedule` / 日期归一（纯逻辑，2 用例）
- `norm` 行为：`'6.25'` → `'2026-06-25'`（断言格式，防 2026 硬编码未来失效）。
- fallback 字典在多日区间按 `start<=date<=end` 过滤正确。

---

## 3. 诚实纪律断言（IR-30 内嵌，→ `tests/test_calibration_honesty.py`）

> 这一组是 T21 的承重部分：把 09-23 去水事故与 G6 零信息对照固化成**不可回归**的断言。

### 3.1 去水幂法断言（devig_power 抑 FLB，对照 devig3 比例法）— 3 用例
- 热门虚高场景 `odds=(1.20, 5.0, 12.0)`：断言 `devig_power(odds)[0] < devig3(*odds)[0]`
  （幂法对热门概率压制更强，正是 09-23 事故根因方向的反向护栏）。
- 幂法输出 `sum ≈ 1`（tol 1e-6）。
- 非法赔率（`<=1` / 非有限）任一 → `None`；迭代失败正确回退 `devig_n`。

### 3.2 ECE 校准阈值断言（IR-30：概率体系可信须门控）— 2 用例
- 完美校准集 → `ece≈0` 且 `slope≈1.0` → 判定"可靠"。
- 过度自信集（emp 系统性低于 pred，slope<1，ece>0.05）→ 断言**任何调用方都不得**在 `ece>阈值` 时输出"模型概率可靠"类结论（用 mock 判定函数断言门控分支）。

### 3.3 零信息机械对照不翻正断言（G6，防 09-23 复现）— 2 用例
- 构造合成样本：以**比例法(devig3)隐含概率**为"市场"，让热门以高于隐含的频次获胜（复现 09-23 比例法虚高热门）。
- 断言 A：基于 `devig3` 的"无脑买最短赔率"机械基准在该样本上 ROI>0（证明盈利是去水伪影，非 edge）。
- 断言 B：同一批样本改用 `devig_power` 口径后，机械基准 ROI 显著塌缩（含 0 或转负），证明**幂法是唯一合规度量口径**。
- 这等价于把 `docs/verification_devig_fix.md` 的核心结论固化为单元级回归护栏。

---

## 4. 落地清单（待回归门禁，本规格不执行）

1. 新建 `tests/test_calibration_direct.py`（§1，≈21 用例）
2. 新建 `tests/test_auto_predict_direct.py`（§2，≈11 用例）
3. 新建 `tests/test_calibration_honesty.py`（§3，≈7 用例）
4. 运行：`.venv/Scripts/python -m pytest tests/ -q --timeout=120` 全量须通过
5. 重跑 T20 代理：`python scripts/audit_test_coverage_gap.py` → 确认 `calibration.py` 与 `auto_predict.py` 的 HIGH 缺口从 137 移除
6. 不触 events.db、不杀进程、不改被测模块源码

## 5. 验收门禁

- [ ] 三个测试文件新增，单测全绿
- [ ] 全量 `pytest tests/ -q` 无回归（当前 129 passed 基线不降）
- [ ] T20 缺口报告中 calibration/auto_predict 标记已覆盖
- [ ] `git diff` 显示仅 tests/ 新增，无 production 代码改动
