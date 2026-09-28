# P-ENG 验证台报告「时效 + 三态不变式」静态守卫规格（WINDOW 备料，纯规格）

> 承接：T38③（`audit_stale_reports.py:85` 把 `retrain_suggest` 写进报告分类判定式）
> / T42（伪活引用：`model_g1_reach_plan` 抄 `monitor_status.json` 当最新）
> / T47（`python -m verification report|ingest` 零调度方，报告必然过期）
> / T50（`monitor_status` 字段加得上去也没人看 → V8「必须有真实消费点」）
>
> 本轮任务（T51）：把「报告会过期」和「三态可能被绕过」这两件事，从**口头纪律**
> 变成**可执行、fail-closed 的静态守卫规格**。
>
> **纯规格**：不改 `verification/gates.py` / 不改 `verification/report.py` /
> 不跑验证台 / 不写 `verification.db` / 零 `events.db` 写入 / 不挂调度 / 不改生产调度。
> 配套只读审计已落盘：`scripts/audit_verification_report_freshness.py`
> + `tests/test_audit_verification_report_freshness.py`（**29 passed**）
> + 报告 `reports/verification_report_freshness_audit.{json,md}`。

---

## 1. 基线（本轮只读实测，as_of 2026-09-28 ~04:2x）

| 项 | 实测值 | 含义 |
|---|---|---|
| 报告 `generated_at` | `2026-09-25T04:26:34Z`（age ≈ 64h，mtime 09-25 12:26） | `STALE_WARN`（阈值 warn 24h / hard 168h） |
| `verification` 调度方 | **0**（T47 机械证明） | 过期是**结构性必然**，不是偶发 |
| 三态产出体（包内） | 8 处，全部在 `verification/`（`gates.py` 是唯一出口） |  today OK |
| 三态字面量（包外噪声） | `analysis/ 18 处`（`NO_EDGE` 是**盘口信号**语义，同形异义） | 全仓裸扫必被淹没 |
| 三态字面量（未分层） | **2 处**：`scripts/mh_train_div.py:67` 打印自己的 `NO EDGE` 判定；`pipeline/fusion_wdl_proto.py:363` 是阈值字典键 `"EDGE"`（假阳性样本） | 守卫要逼人分类，不能自动豁免 |
| `render_json` 字段 | 14 个，**不含** `win_rate / implied / edge_pp / mech_fav_roi / paired_excess / paired_excess_ci_low` | **EDGE 出现也无法独立复核** |
| 消费点 | 9–10 处引用 `verification_report.json`，除 1 处自述外**零时效判定** | 旧结论被当最新渲染 |
| 看板 `cls()` 映射 | 3/3 合法三态，`t-na` 兜底 | 目前**不 fail-open**；新增第四态才会静默变灰 |

**当前审计判定 = FAIL**（RED：`R2_RENDERER_CANNOT_PROVE_EDGE` + `R6_UNKNOWN_VERDICT_EMITTER`；
AMBER：`R3_REPORT_EXPIRED`）。

---

## 2. 三条真实结论（诚实口径）

### 2.1 `NO_EDGE` 是词汇撞车，不是三态漂移
`analysis/live_goal_probe.py` 等 18 处把 `NO_EDGE` 当**盘口信号**（"无优势、观望"）用，
与验证台三态**同形异义**。任何"全仓 grep EDGE 关键字"的守卫都会被这 18 处噪音吞掉。
→ 守卫必须**限定扫描面**（`verification/` 包 + 渲染消费点），包外只计量不 FAIL，
且**未分层项必须显式列成清单**（防豁免清单悄悄长大退化成无守卫）。

### 2.2 IR-30 的 EDGE 字段缺口是**渲染面**的，不是数据面的
今天三源是 `NO EDGE / NO EDGE / INCONCLUSIVE`，数据面检查**空洞成立**（没有 EDGE 可查）。
但 `verification/report.py::render_json` 根本不写 `mech_fav_roi / paired_excess /
paired_excess_ci_low`（这三个 `MetricsBundle` 里**算出来了却丢弃**），也不写胜率/隐含/edge_pp。
→ **一旦将来 gates 判出 EDGE，JSON 里没有任何数字可供任何人复核**，
G6 只能靠 `reasons` 里的一句中文描述"证明"。这正是 09-23 去水事故的形状：
结论无法被复核 = 结论不可信。所以 G2 必须在**静态渲染面**强制，而不是只在数据面查。

### 2.3 过期是结构性的，与时间无关
T47 已机械证明 `python -m verification report|ingest` 零调度方且
`cmd_report` 内部还夹着 `ingest_new`。因此"报告时效闸门"单独的收益是零——
闸门只会更响地报同一个根因。**修复顺序必须是 ①先有调度方 ②再上时效闸门**，
否则 G4 一上线就是常亮红灯，次日被人加 `allow_ignore` 关掉。

---

## 3. 守卫设计（G1–G5）+ 强制顺序

| # | 守卫 | 判定式（可在 CI / `tests/` 落） | 严重度 | 验证方式 |
|---|---|---|---|---|
| **G1** | 三态枚举不变式 | 进入 `verification_report.json` 的每个 `models[*].verdict` 必须 ∈ `{EDGE, NO EDGE, INCONCLUSIVE}` | 违规 = RED | 读报告 + 静态扫 `verification/` 包，除 `gates.verdict` 外不得直接产出三态字面量 |
| **G2** | IR-30 EDGE 可复核 | 只要 `render_json` 可能产出 `verdict="EDGE"`，就必须同时能写出 `win_rate / implied / edge_pp` 与 G6 三元组（缺任一即 FAIL，EDGE 不得进入报告） | 缺字段 = RED | **静态面**（本轮唯一可强制层）+ 数据面双检（数据面 today 空洞成立，不得因此放行） |
| **G3** | 时效标记必填 | 报告须带 `freshness{generated_at, age_hours, status}`；`status ∈ {FRESH, STALE_WARN, STALE_HARD}`；任何消费点渲染 verdict 前必须读 `status` 并显式标注 | 缺 `generated_at` = RED（不可判定）；仅过期 = AMBER | 静态扫消费点 + 报告结构断言 |
| **G4** | 调度存在性 | `python -m verification report|ingest` 必须有 ≥1 个调度方（`SCHEDULED_CLI ≥ 1`）。**无调度方则 G3 必然长期亮 AMBER，须显式写明根因而非伪装成数据问题** | 无调度方 = AMBER + 阻断 G3 升级为 FAIL 前必须先修调度 | 复用 T47 的 `SCHEDULED_CLI` 判定式 |
| **G5** | 下游抄值防污染 | 消费点禁止"抄 verdict 但不带 `generated_at`"（沿用 T38③ / T42 的伪活引用与抄值链） | 抄值且无时效 = RED | 静态扫 `verdict` 读取点 |

**强制落地顺序（反序则必然返工）**

```
G4 调度接入（T47 §Q4 方案 A，monitor 末尾 subprocess timeout=240）
   ↓  只有先有真实更新，G3 的 AMBER 才是"可恢复的告警"
G2 渲染面补 IR-30 字段 + 数据面双检
   ↓  先让 EDGE 可复核，再谈 EDGE 的语义
G1 三态枚举静态守卫（失败阈值语义 = 防回退：R-A 后转绿，改回即红）
   ↓
G3 时效标记 + 消费点渲染标注（≥1 个真实消费点，否则同 T50 V8 无人看）
   ↓
G5 抄值防污染（最后做：会把既有多份报告判成失效面，需先通知下游）
```

**门禁（fail-closed，任一红项或缺项即 FAIL，不因其它项绿而放行）**：
`V1` 三态越界必须 FAIL · `V2` 渲染缺 IR-30 字段必须 FAIL · `V3` 缺 `generated_at` 必须 FAIL ·
`V4` 未分层三态字面量必须 FAIL（本轮基线 2 处，须人工分类后清零）·
`V5` 消费点零时效判定必须 WARN（不静默）· `V6` G4 无调度方必须显式列出根因 ·
`V7` 零写入（不改 `verification.db`、不跑 ingest、零 `events.db` 写入）·
`V8` 与 T50 同窗口：O1–O4 与 G1–G5 必须配真实消费点。

**回滚**：删除本轮新增测试即可，零副作用（未改任何生产文件）。

---

## 4. 验收（A1–A7）

| # | 断言 | fail-closed? |
|---|---|---|
| A1 | `python -m pytest tests/ -q` 全绿，且本守卫测试 ≥ 25 条 | 是 |
| A2 | 在临时报告上注入 `"verdict": "NO_EDGE"` → 守卫必须 FAIL（防守卫自己失效） | 是 |
| A3 | 在临时 `render_json` 副本删除 `paired_excess_ci_low` → G2 必须 FAIL | 是 |
| A4 | 把 `mh_train_div.py` 的一个 `NO EDGE` 打印改为走 `gates.verdict` → `unexpected_count` 必须降到 1 | 是 |
| A5 | 活体：真实报告三态全部在枚举内（today 成立，无越界） | 是 |
| A6 | 活体：若报告 age > warn，`findings` 必须含 `R3_REPORT_EXPIRED`，**且** `amber` 中必须带「零调度方」根因文案 | 是 |
| A7 | 零写入自查：审计前后 `verification.db` 行数与 `models/*.joblib` mtime 均不变 | 是 |

---

## 5. 未决（人工拍板前不得开工）

- **Q-a 阈值**：`warn=24h / hard=168h` 是本轮建议值。若报告改为「每次 ledger 有新增才生成」，
  阈值应改为「相对上次 ledger 水位」而非固定小时数。
- **Q-b G2 字段口径**：`win_rate / implied / edge_pp` 三个字段在现有 `MetricsBundle` 里
  **一个都不存在**（只有 `roi/accuracy/ci_*`）。要么新增三个指标（属模型层改动，WINDOW），
  要么退而求其次只强制 **G6 三元组**（`MetricsBundle` 已有，只是没写进 JSON）——
  **强烈建议先做后者**，成本从"改模型"降到"改渲染"。
- **Q-c 未分层 2 处处置**：`mh_train_div.py:67` 应改为复用 `gates.verdict`（真修复）；
  `fusion_wdl_proto.py:363` 是阈值键，应把它重命名或移入豁免名单（须留证据，禁止静默）。
- **Q-d G3 消费点范围**：本轮实测 9–10 处引用里只有 1 处带 `age` 字样。是否要求
  **所有**引用点都渲染时效标注，还是只要求生产渲染面（`build_dashboard.py`）？
- **Q-e 与 T50 的顺序**：T50 给 monitor 加 `error_kind`/滞后计数器，本条给报告加时效标记，
  两者都依赖"有人看"，建议同窗口落地（T50 的 V8 与本条 G3 互为准入条件）。

---

## 6. 红线自查

| 红线 | 本轮状态 |
|---|---|
| IR-30 诚实 | 审计结论全程标注「估算/可复核边界」；EDGE 声称必须配 G6 对照，本规格把它写成**结构性前置**而非可选项 |
| IR-32 跨庄禁区 | 未触碰跨庄面；扫描面不含跨庄符号 |
| §4 数据资产保全 | 零 `events.db` 写入；未跑 ingest；未写 `verification.db` |
| §6 进程安全 | 零进程操作 |
| 售卖双签 | 未涉及售卖 |
| WINDOW | 仅备料，未执行任何 schema / 生产改动 |

**诚实边界**：本条只把「报告会过期 + EDGE 不可复核」这两处结构性缺口**变成可度量、可拦截**，
**不产生任何样本、不推进 G1、不产生 edge**。要真正让报告不再过期，属 T47 的调度范围；
要真正让 EDGE 可复核，属 Q-b（模型/渲染层 WINDOW）。
