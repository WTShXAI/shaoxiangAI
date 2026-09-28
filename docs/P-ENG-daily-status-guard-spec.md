# P-ENG — `daily_predictions.status` 陈旧派生字段的防回退静态守卫规格（T49）

> 状态：**纯规格，本轮不实现、不改 `verification/ingest.py`、不碰 `events.db`、不写 `verification.db`、不碰调度。**
> 承接：`T46 §3 C 方案代价项`（C = ingest 侧门控 `d.status` → `m.status`）、`T46 验收 A2`。
> 基线数据来源：本轮只读盘点 `scripts/audit_daily_status_guard.py` + `reports/daily_status_guard_audit.{json,md}`。

---

## 0. 一句话

T46 的 C 方案把门控判据从 `d.status` 改成 `m.status`，**不新增任何 events.db 写方**，是唯一"零撞锁 + 可自愈"的路径；代价是 `d.status` 从此**语义为假**（它永远是 predict_export 抄进来的快照，无人再翻）。守卫要守的不是"现在有没有问题"，而是**"将来没人把它改回去 / 没人再拿它当判据"**。

---

## 1. 基线实证（2026-09-27，只读）

| 面 | 实测 | 含义 |
|---|---|---|
| 生产面 `d.status` SQL 判定式 | **1 处**：`verification/ingest.py:97` `WHERE d.status='finished' AND m.score_home IS NOT NULL` | 唯一真货，也是 R-A 要改的那一行 |
| `d.status` 其他命中 | 16 处总计 → `{AUDIT_LITERAL 13 / PASSTHROUGH 2 / INGEST_GATE 1}` | 13 处是审计脚本与其单测的断言字面量（见 §3 防假阳性） |
| `daily_predictions.status` UPDATE 写方 | **真写方 0**（候选 4 处全为审计/测试字面量） | 独立复核 T33 `UPDATE_WRITER = 0` |
| 文档面 | 豁免面 22（整改规格描述"将要改的行"）/ 无关面 1（`matches.status`）/ **需标注 1** | 需标注清单见 `reports/daily_status_guard_audit.md` §2 |
| 现成守卫机制 | 含守卫语义的测试 24 个，但**只有 1 个**（`test_no_crossbook.py`）带显式文件清单 + 源码正则 | T49 的守卫将是首个「显式清单 + SQL 语境判定」的静态守卫 |

**两条本轮新发现的工程缺陷（决定守卫怎么写）**：

1. **`d.status` 是歧义符号。** `frontend/src/pages/Rollball/index.tsx:616` 的 `d.status === 'scheduled'` 里 `d` 是本地比赛对象（同作用域 `d.home` / `d.away`），与 `daily_predictions` 无关。裸字符串扫描会立刻假阳性 → 判定式必须要求 **SQL 上下文**（同行出现 `WHERE` / `daily_predictions` / `JOIN matches`）。
2. **自排除清单是每个审计脚本各写一份的。** 本轮实证：`scripts/audit_daily_status_guard.py` 的文档字符串里写了一次"UPDATE + daily_predictions"的连续串（即便是为了**解释这个坑**），就直接把
   `tests/test_audit_ingest_status_gate.py::test_find_status_writers_real_repo_has_no_update_flipper`
   从 pass 打成 fail。**新增审计脚本会静默污染既有审计的 real-repo 断言。**

---

## 2. 守卫设计（G1–G4）

### G1（FAIL-closed，主断言）：生产面不得出现 `d.status` SQL 判定式

- 扫描面：`verification/`, `pipeline/`, `gq/`, `core/`, `data_collector/`, `backend/` 下 `.py`；
- 判定：`is_sql_daily_status_predicate(line)` —— 必须**同时**满足「出现 `d.status` 字面量」和「同行存在 SQL 上下文」；
- 违规即 FAIL，报 `file:line`。
- **语义是「防回退」而非「当前门禁」**：R-A 落地后本断言应转绿；将来有人把 `ingest.py:97` 改回 `d.status`，CI 立刻红。这与 T30-D 的立场一致 —— 守卫只在**语义反转后才生效**，不会在整改前误伤。

### G2（FAIL-closed）：不得出现 `daily_predictions.status` 翻写方

- 扫描面：全仓 `.py`；判定：行内出现 `UPDATE` 与 `daily_predictions` 且非审计/测试字面量，且（`status` 出现在同一语句窗口）；
- 违规即 FAIL。**注意极性**：`INSERT ... ON CONFLICT DO UPDATE SET status=excluded.status`（`predict_export.py:282`）是 **PASSTHROUGH 不是写方**（值来自 `matches.status` 快照），不得误判；`include_finished` 回填路径（`predict_export.py:269`）必须是"只补空行"，否则会覆盖前向定格行（同 T33）。
- 基线复核：真写方 = 0。任何 > 0 都意味着 A/B 方案（新增 events.db 写方）被回退引入 —— 那会与 T44 撞锁面叠加。

### G3（WARN，不 FAIL）：文档面 stale 派生字段标记

- `docs/**.md` 中把 `d.status` / `daily_predictions.status` 当"已完赛判据"的表述，须带标记 `@stale-derived-field`（行尾 / 段落首行）；
- 未标记者进**周报清单**，不当场 FAIL。理由沿用 T30-D：**一次性把 100 处注释/文档变红，会训练出绕过习惯**，反而更危险；
- 豁免面（整改规格 `P-*`、队列条目中"描述将要改的那一行"的句子）单独统计不进清单 —— 注意**只豁免句子不豁免整个文件**，否则真声明会被一起豁免掉（本轮初稿就踩了这个）。

### G4（FAIL-closed，新增）：共享豁免名单 SSoT + 跨审计回归

- 把「自指排除 / 审计字面量豁免 / 注释豁免」三层收进**单一 SSoT**（建议 `scripts/_guard_exempt.py` 或 `conftest.py` 常量），各审计脚本统一 import，禁止各自复制一份；
- 新增一条跨审计回归测试：任何审计脚本若引入被他人扫描的连续字面量（如 `UPDATE` + `daily_predictions`），必须出现在豁免名单，否则 FAIL。

---

## 3. 防假阳性的三条硬约束（照抄实现要点）

1. **SQL 语境要求**（防前端本地变量）；
2. **三层排除**：① 文件级自指排除 ② 审计脚本/单测的**字面量**豁免 ③ 注释行降级（`COMMENT`）；
3. **正则必须带 `re.M`**：`SCANLIST_PAT`、`^\s*(SCAN_FILES|...)\s*[=:]` 这类**锚在行首**的正则，漏 `re.M` 会把命中数从 1 静默读成 0（本轮实测踩中，已写进测试作失效对照）。这与 T30-D 的「`\b` 前缀分界」是同一族坑：**少一个正则标志就静默漏判，且这次是反过来少报**。

---

## 4. 落地顺序与验收（V1–V8 fail-closed）

**顺序（不可颠倒）**：`R-B 调度挂载` → `R-A 门控改判 m.status` → `R-A2 补跑分段` → `G1/G2 守卫上架` → `G3 标记`。
理由：守卫在门控改判**之前**上线只会立刻变红并催生"先关守卫再改"的绕过理由；而 G1 的绿/红本身就是 R-A 是否真的落地的机械判据。

| 编号 | 断言 | 未通过处置 |
|---|---|---|
| V1 | `pytest tests/ -q` 全绿（基线 475 passed） | 停 |
| V2 | G1 真实仓库扫描结果 = `[]`（绿） | 停 |
| V3 | G2 真写方 = 0 | 停 |
| V4 | 守卫测试含 3 条失效对照（前端 `d.status` 不误抓 / 无 SQL 语境不误抓 / 审计字面量不误抓） | 停 |
| V5 | 静态检查：守卫源码中不得出现裸 `1.0/probs` 之类绕过；不得自己写正则判 DB 锁（`core/error_envelope.classify_error()` 已是 SSoT） | 停 |
| V6 | 不改 `verification/ingest.py` 之外的任何生产文件；不改 schedules | 停 |
| V7 | `models/*.joblib` mtime 不变（不触发重训） | 停 |
| V8 | 落库前后 `verification.db` 行数不变（不跑 ingest） | 停 |

**回滚**：删掉 `tests/test_daily_status_guard.py` 即完全回滚，零生产副作用（守卫不写任何文件）。

---

## 5. 测试骨架（可直接粘贴；本轮**只写规格不落地**）

```python
# tests/test_daily_status_guard.py —— 待 R-A 落地后一并启用
PROD_SCAN = ('verification', 'pipeline', 'gq', 'core', 'data_collector')

def test_g1_no_d_status_sql_predicate():
    bad = [(f, i, l.strip()) for f, i, l in iter_prod_lines()
           if is_sql_daily_status_predicate(l)]
    assert not bad, f'd.status SQL 判定式回退（应为 m.status）: {bad}'

def test_g2_no_real_status_flipper():
    code = scan_code_surface()
    real = [u for u in code['update_writers'] if not u.get('is_literal_only')]
    assert real == [], f'daily_predictions.status 出现真写方: {real}'

@pytest.mark.parametrize('line', [
    "{selPhase && (selPhase.key !== 'pre' || d.status === 'scheduled') && (",  # 前端本地变量
    "d.status,",                                                               # 无 SQL 语境
])
def test_no_false_positive(line):
    assert not is_sql_daily_status_predicate(line)
```

---

## 6. 未决（Q-a / Q-b / Q-c）

- **Q-a**：G1 扫描面是否扩到 `frontend/`？扩了就必须先把 `:616` 的本地变量改名（否则恒红）——**建议不扩**，前端本就不是 ingest 判据来源。
- **Q-b**：G2 的"语句窗口"取 6 行（`find_status_writers` 现行口径）还是"单行 + 上下文 2 行"？窗口越大越易漏，越小越易误。
- **Q-c**：共享豁免名单放 `scripts/_guard_exempt.py`（各脚本 import）还是 `conftest.py`（pytest 侧统一）？前者可被非 pytest 脚本复用，后者只能被测试用 —— **倾向前者**。

---

## 7. 红线自查

| 红线 | 状态 |
|---|---|
| IR-30 诚实 | 本文不含任何 edge/盈利声称；只谈字段语义与回退防护 |
| IR-32 跨庄禁区 | 未引入任何跨庄字段/结论 |
| §4 数据资产保全 | **零 events.db 写入**；未跑 ingest；未写 `verification.db` |
| §6 进程安全 | 未拉起、未杀任何进程 |
| WINDOW 纪律 | 仅备料，未执行 schema 变更 / 未改生产代码 / 未挂调度 |
| 售卖 | 与本议题无关，未触碰 |
