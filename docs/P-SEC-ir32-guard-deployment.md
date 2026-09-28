# P-SEC-ir32-guard-deployment — IR-32 守卫落地部署序列（纯规格）

> 状态：**纯规格**。本文件不修改 `tests/test_no_crossbook.py`、不触碰守卫、不改动生产代码、不碰 events.db、不杀进程。
> 落地属 **WINDOW 项**（涉及 `bridge_service.py` / 前端 / 计划任务调度 / 守卫测试）。
> 上游依据：T06（前端类型审计）→ T15（前端守卫规格）→ T30（跨庄活跃面盘点）→ **T30-C（守卫覆盖缺口量化，本文件全部数字来自 `reports/crossbook_guard_gap_audit.json`，生成于 2026-09-26 23:34:41）**。

---

## 1. 决策基线（T30-C 实测，不可凭感觉拍脑袋）

| 指标 | 实测值 | 含义 |
|------|--------|------|
| 现守卫命中数 | **0**（全绿通过） | 现守卫 `tests/test_no_crossbook.py` 只扫 11 个后端 `.py` 的 **`import` 行**，今天任何跨庄残留都抓不到 |
| 扩展后命中总数 | **108**（去重） | 全部为"扩展前看不见"的新增命中 |
| 其中**运行时有效** | **8 处** | 真正在生产响应/渲染路径上 |
| 其中**注释噪声** | **100 处** | 零容忍口径下会造成 100% 红灯 |
| 其中**测试自指** | **70 处** | 守卫自身与其审计脚本的自检样本 |
| 生产面规格盲区 | **0 处** | `core/` `data_collector/` `agent_cruise/` 实测零残留 → T15 §3.2 的遗漏属**理论风险，当前无害** |

**运行时有效 8 处全清单**（按风险降序）：

| 风险 | 位置 | 命中形态 | 代码 |
|------|------|----------|------|
**去重后生产运行时命中 = 6 处**（上表 8 行里，`bridge_service.py:7803` 被 DICT_FIELD 与 STRING 两条规则重复计一次；`tests/...live_surface.py:110` 属测试面）。

| **critical** | `bridge_service.py:7803` | DICT_FIELD | `"multibook_consensus": _lookup_multibook_consensus(req.home, req.away),  # 多庄 sharp/retail 共识` |
| **high** | `frontend/.../MatchAnalysisModal/sections.tsx:127` | FIELD_ACCESS | `const mb = card.multibook_consensus` |
| **high** | `pipeline/ranked_params.json:7` | STRING | `"priority_override": "cross_book_edge"`（**运行时会被 `load_params()` 读入**） |
| medium | `frontend/.../sections.tsx:126` | COMPONENT | `export function MultiBookConsensus(...)` |
| medium | `frontend/.../index.tsx:8` | STRING | `MultiBookConsensus, OperatorView, ...`（import 语句） |
| medium | `frontend/.../index.tsx:265` | JSX_MOUNT | `<MultiBookConsensus card={card} />` |
| medium | `bridge_service.py:7803` | STRING | 同上 7803 行（函数名同时被匹配） |
| medium | `tests/test_audit_crossbook_live_surface.py:110` | STRING | 审计脚本自身（测试面，非生产） |

### 1.1 本轮新增实证（部署前必须知道的两件事）

**① `pipeline/ranked_params.json` 是被 `load_params()` 真实读取的配置文件**（`pipeline/ranked_predictor.py:84-95`：JSON 覆盖 `DEFAULT_PARAMS`）。文件第 7 行 `"priority_override": "cross_book_edge"` 是**活的跨庄键名**。当前 `grep -rn priority_override --glob=*.py` 全仓仅 1 命中（即 `ranked_predictor.py:53` 的默认值声明），说明**该键当前无任何消费方**（默认逻辑已改走 `ensemble_*`），属"活的死配置"。
→ 部署含义：**守卫必须扫 `.json`**，否则这个唯一"运行时被读"的跨庄符号会从任何基于扩展名的扫描面里漏掉。

**② `_get_cross_book_signal` 是 IR-32 事故的直接符号**，但仍**不在 BANNED 列表里**：它导致 `bridge_service.py:1037` 向 `start_cruise()` 传已删函数 → `NameError` → 自主巡航 Agent 自 2026-09-19 02:44 起静默死亡，2026-09-22 重启的在线 bridge 仍带着这个失败启动（T30 / T30-B）。
→ 部署含义：banned 名单不能只列"还在用的名字"，必须补一张**已删除符号的残留名单（RESIDUE_SYMBOLS）**，否则同类删除事故永远不会被守卫拦住。

---

## 2. 为什么"现在就开零容忍"是错的

T15 §3.1 主张"零容忍：banned 符号任何出现即 FAIL"。按 T30-C 实测，**照做立刻得到 108 条 FAIL，其中 100 条在注释里、70 条在测试里**。后果是可预见的：

1. 红灯密度 ≈ 100 条/次，人类面对 100 条噪声只会做一件事——找办法关掉它（`-k "not crossbook"`、`--deselect`、`@pytest.mark.skip`）。
2. 一旦绕过习惯形成，**那 8 处运行时残留会在同一批绿灯里继续存在**，红线保护反而比今天更弱。
3. 更糟的是：绕过会**掩盖真正的回归信号**，让守卫从"红线探测器"退化为"橡皮图章"。

**诚实结论：这不是"更严格"，这是"把红线埋进噪声里"。** 零容忍的正确顺序是**先减法（清骨架）再加法（开零容忍）**。下面两套序列即按此原则设计。

---

## 3. 部署序列

### 方案 A — 先清骨架，再开零容忍（推荐；两窗口）

| 阶段 | 步骤 | 产出 | 验证门禁 |
|------|------|------|----------|
| A0（**本自动化周期内可做的准备工作**） | 生成残留清单 `reports/ir32_residue_inventory.{json,md}`（复刻 T30-C 扫描逻辑，只读，不改守卫） | 清单落盘 | 无代码变更，不改生产 |
| A1 **WINDOW-1（清骨架）** | 按 `P-SEC-crossbook-frontend-spec.md` §4 逐条删除：① `bridge_service.py` 6464-6466 函数定义 + 7803 行字段赋值（该键前端已 `if (!mb) return null` 优雅降级，删键零破坏）② `types/index.ts:588` 字段 + `MultibookConsensus` 类型 ③ `sections.tsx:126-182` 组件 ④ `index.tsx:8` import + `:265` 挂载 ⑤ `ranked_params.json:7` 的 `priority_override` 键（停掉"活的死配置"） | 运行时有效命中 8 → **0** | `pytest tests/ -q` 全绿 + 前端 `npm run build` 通过 + 人工确认 MatchAnalysisModal 正常 |
| A2 **WINDOW-1 同窗口** | 清注释噪声：`pipeline/evaluation/cross_book_backtest.py`(10)、`full_chain_backtest.py`(4)、`model_ensemble.py`(3)、`ranked_predictor.py`(3)、`sharp_provider.py`(4)、`odds_math.py`(2)、`flb_adjust.py`(1)、`unified_predictor.py`(1)、`bridge_service.py`(2) —— 按 §4 豁免标注协议改写为 `@ir32-allow-comment` 行尾标注，或直接删掉过时描述 | 注释噪声 100 → 可控 | 同上 + 逐文件确认语义未被曲解（注释只删不改逻辑） |
| A3 **WINDOW-2（开零容忍）** | 按 T15 §3.2-3.4 + 本文 §4-§6 扩展守卫：`SCAN_DIRS` 扩到 `gq/`、`pipeline/`、`frontend/src`、补 `core/` `data_collector/` `agent_cruise/`；扩展名覆盖 `.py .json .ts .tsx`；BANNED 增补符号（见 §5）；规则 Tier-1/Tier-2（见 §4） | 零容忍守卫 | 全量 `pytest` 全绿；故意注入一个 `multibook_consensus` dict 字段 → 守卫 FAIL（fail-closed 实证） |

**A 的回滚点**：A1/A2 可整批 `git revert`（纯代码删除，无数据面改动）；A3 回滚 = 还原 `tests/test_no_crossbook.py` 单文件，无副作用。

### 方案 B — 分批开（不删代码，风险更低但周期更长）

若不接受"删骨架"（例如前端改动需要更长时间回归），退而求其次：

| 批次 | 开什么 | 命中数 | 说明 |
|------|--------|--------|------|
| B-1 | 仅 **Tier-1 runtime kind**（DICT_FIELD / FIELD_ACCESS / JSX_MOUNT / COMPONENT / 运行时 STRING）FAIL | **6**（§1 去重后的生产运行时全量；排除测试面 1 处 + 7803 行 STRING 规则重复计 1 次） | 立刻抓住真正的生产通路，注释与测试不参与 |
| B-2 | 排入 `tests/**` 到**观察模式**（命中打印不 FAIL，统计进报告） | 70 | 积累基线，稳定后再转 FAIL |
| B-3 | 排入注释面到**观察模式 + 豁免白名单**（`pipeline/`、`bridge_service.py` 注释） | 100 | 与 A2 并行：一边白名单一边随代码演进自然减少 |
| B-4 | 观察期零新增 → 转 FAIL | — | 触发条件见下 |

**B 的放行准则（必须写死，防止"永远观察"）**：B-2/B-3 连续 **14 天**观察期内新增命中数为 0，且 CI 耗时增加 < 2s → 自动转 FAIL；否则升格 A1/A2 的清骨架工作，不再延期。

---

## 4. 注释豁免标注协议（`@ir32-allow-comment`）

零容忍下必须给"说明禁令历史"的注释一个**显式的、可审计的**生存通道，否则只有两条路：删注释（丢知识）或关守卫（丢红线）。

```python
# @ir32-allow-comment  # 历史 IR-32 说明，仅注释，非代码
```
- 位置：**行尾**，与代码同一行；跨行 docstring 则放在**首行**并同时标注后续行（或整段加同一标注）。
- 语义：该行命中**降级为 WARN**，不计入 FAIL；仍计入 `reports/` 的噪声统计，供 A2 清理进度跟踪。
- 审计：新增豁免必须**说明理由**（禁则在标注后加短句原因）；守卫自身提供 `--list-exemptions` 导出当前全部豁免点供季度复盘。
- 反绕过：豁免只能由**注释行**触发；字符串字面量、dict 键、标识符**一律不豁免**（这是 8 处运行时命中全部落网的关键）。

## 5. 符号表补强（两族）

现 BANNED 为 7 条字符串，扩展后须分两族管理，理由不同：

**族 1｜活跨庄符号（BANNED）** — 任何真实使用都应立即 FAIL
```
cross_book_edge  multibook_consensus  cross_book_alert
leyu_value_signal  bet_split_source  multibook  cross_book
```

**族 2｜IR-32 已删除符号（RESIDUE_SYMBOLS，新增建议）** — 名字本身已随禁令消失，残留即"有人还在引用已删代码"，正是 09-19 cruise 事故的形状
```
_get_cross_book_signal       # ← T30/T30-B 实证：致命 NameError， cruise Agent 静默死亡 4 天
_lookup_multibook_consensus  # ← bridge_service.py:6464 函数定义体恒 return None
sharp_consensus_for_match    # ← 原 import 者已随模块删除（pipeline/multibook_consensus.py 已不存在）
compute_value_layer  market_implied  from scripts.bet_core
```

**边界协议（修 T30-C 指出的整词漏判）**
- **IMPORT 规则**：保留现有 `from/import` 行内**子串**匹配（`\S*{sym}\S*`）——过宽只会造成假阳性，属安全方向。
- **SYMBOL 规则**：改用 `(?<![A-Za-z0-9]){sym}\b`（**前缀按"非字母数字"分界，不用 `\b`**）。
  - 例：`def _lookup_multibook_consensus(...)` 用 `\b` 前缀时 `_` 是词字符 → **不匹配（漏判）**；用 `(?<![A-Za-z0-9])` → **匹配（抓到）**。
  - 例：`my_cross_book_edge` 不会被精确命中——这是**期望行为**：精确符号才 FAIL，其他形状走族 1 的子串规则兜底。
- **Tier-1 / Tier-2**：族 1 命中 → 立即 FAIL；族 2 命中 → FAIL，除非该行是**定义/声明**（`def X`、`X = ...`）且同行无调用/传参（此时报 WARN + 指引清理，避免把"已删函数的空壳"当成"复活"）。

## 6. SCAN 面补齐与优先级

| 优先级 | 扫描面 | 实测命中 | 理由 |
|--------|--------|----------|------|
| P0 | `bridge_service.py`（单文件） | 4 | 唯一 critical 在此 |
| P0 | `frontend/src`（`.ts/.tsx`） | 5（运行时有效 4 + 注释 1） | 组件渲染面；现守卫完全不扫 |
| P0 | 扩展名含 **`.json`** | 1 | `ranked_params.json` 是运行时被读的配置（§1.1①） |
| P1 | `pipeline/`（目录化，现仅 3 个单文件） | 29（几乎全注释） | A2 清理主战场 |
| P1 | `tests/` **观察模式** | 70 | 自指噪声，先观察后 FAIL |
| P2 | `gq/` `core/` `data_collector/` `agent_cruise/` | 0 | T30-C 实测零残留，但 `agent_cruise.py` 正是 09-19 事故的藏身处 → 属**防御纵深**，代价仅为几个文件的扫描开销，优先级低但不能永久不扫 |

**排除面（写死）**：`archive/` `node_modules/` `dist/` `build/` `.git/` `verification/`（离线回测域，如需保留须逐文件加 `@ir32-offline-allowed`）。

## 7. 验收门禁（落地时逐条核销）

- [ ] A1 完成后，运行时有效命中 **8 → 0**（`reports/ir32_residue_inventory.json` 可复算）。
- [ ] A2 完成后，注释噪声中**已处置项 100% 带 `@ir32-allow-comment` 或已删除**。
- [ ] A3 后全量 `pytest tests/ -q --timeout=120` 全绿。
- [ ] **fail-closed 实证**：临时在 `frontend/src` 与 `pipeline/` 各注入一处 `multibook_consensus` 的 dict 字段 → 守卫必须 FAIL；删除注入 → 恢复绿。
- [ ] 族 2 补强实证：注入 `cross_book=_get_cross_book_signal,` → 守卫必须 FAIL（防 09-19 事故重演）。
- [ ] 前端 `npm run build` 通过，MatchAnalysisModal 无类型/运行时错误。
- [ ] 零 events.db 写入、零进程 kill、零 schema 变更、零 VACUUM。

## 8. 本规格交付与本轮边界

- 本文件：`docs/P-SEC-ir32-guard-deployment.md`（部署序列 A/B + 豁免协议 + 符号表补强 + 扫描面优先级 + 验收门禁）。
- **未做**：未改守卫、未改生产代码、未删任何符号、未跑扫描写入、未碰 events.db、未碰调度、未重启任何服务。
- **建议 WINDOW 任务**：A0（生成 `ir32_residue_inventory`，只读可在本自动化周期内完成）→ A1（清骨架，须窗口+回归）→ A3（开零容忍）。
