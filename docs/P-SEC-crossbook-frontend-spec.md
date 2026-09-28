# P-SEC-crossbook-frontend-spec — 扩展 IR-32 守卫覆盖到前端与 dict 字段

> 状态：**纯规格，不落地守卫、不改动生产代码**。落地属 WINDOW 项（触及 `bridge_service.py` + 前端组件，需维护窗口 + 回归）。
> 触发：T06 审计（`reports/audit_frontend_deprecated_types.{json,md}`）发现活跨庄共识面板的**残留骨架**——IR-32 守卫 (`tests/test_no_crossbook.py`) 覆盖缺口。

---

## 1. 当前状态（已实证，2026-09-26 复核）

IR-32（跨庄共识永久禁令）已于 2026-09-19 落地：生产/接口/测试全禁，仅离线训练回测可用。但**残留骨架**仍在生产代码树中，且现守卫查不到：

| 位置 | 现状 | 守卫是否覆盖 |
|------|------|------|
| `bridge_service.py:6464` `_lookup_multibook_consensus()` | 函数体 `return None`（禁用），但**函数定义 + 名称未删** | ❌ 只查 `import`，不查函数定义名 |
| `bridge_service.py:7803` `"multibook_consensus": _lookup_multibook_consensus(...)` | dict 字段赋值（恒为 `None`），符号仍在生产响应字典 | ❌ 只查 `import` |
| `frontend/src/types/index.ts:588` `multibook_consensus?: MultibookConsensus \| null` | 类型字段声明 | ❌ 守卫不扫前端 |
| `frontend/.../sections.tsx:126` `export function MultiBookConsensus(...)` | 完整渲染组件（`SHARP vs RETAIL` 面板，读 `card.multibook_consensus`） | ❌ 守卫不扫前端 |
| `frontend/.../index.tsx:265` `<MultiBookConsensus card={card} />` | 组件挂载点 | ❌ 守卫不扫前端 |

**结论**：当前运行时因后端恒返回 `None`，前端 `MultiBookConsensus` 走 `if (!mb) return null` 不渲染——面板**逻辑死亡但代码存活**。风险：一旦有人把 `_lookup_multibook_consensus` 重新接通（哪怕误改），跨庄共识会**直接流向生产前端面板**，且现守卫零报警。

---

## 2. 现守卫的覆盖缺口（根因）

`tests/test_no_crossbook.py` 的判定逻辑是：

```python
BANNED = ('cross_book_edge', 'multibook_consensus', 'leyu_value_signal', ...)
# 只匹配 import 语句
if re.search(rf'^\s*(from\s+\S*{banned}\S*\s+|import\s+\S*{banned})', src, re.M):
    violations.append(...)
```

两个缺口：
1. **只扫 11 个后端 `*.py` 文件**，完全不碰 `frontend/src`（`.tsx`/`.ts`）。
2. **只识别 `import`/`from` 语句**，漏掉：
   - 后端 dict 字段赋值 `"multibook_consensus":`（非 import）
   - 函数定义名 `def _lookup_multibook_consensus`（非 import）
   - 前端组件/类型标识符 `MultiBookConsensus` / `multibook_consensus`

---

## 3. 扩展方案（目标守卫语义）

### 3.1 零容忍原则（fail-closed）
IR-32 是永久禁令红线。推荐**先清除残留骨架（§4），再让守卫对 banned 符号“任何出现即 FAIL”**。这样守卫语义最简单、最不易被绕过，不必区分“是否在用”。

### 3.2 扩展 SCAN 范围
```python
SCAN_DIRS = [
    os.path.join(ROOT, 'bridge_service.py'),         # 单文件（保持原样）
    os.path.join(ROOT, 'gq'),                          # 已有单文件 → 改为目录扫描 .py
    os.path.join(ROOT, 'pipeline'),                    # 已有单文件 → 目录扫描 .py
    os.path.join(ROOT, 'frontend', 'src'),             # 新增：前端 .ts/.tsx
]
```
扫描实现：递归 `Path.rglob('*.py')` + `rglob('*.ts')` + `rglob('*.tsx')`，跳过 `node_modules`/`dist`/`build`/`archive`。

### 3.3 扩展 BANNED 模式
新增**符号级**匹配（不仅 import）：
```python
BANNED_SYMBOLS = (
    'cross_book_edge', 'multibook_consensus', 'leyu_value_signal',
    'bet_split_source', 'compute_value_layer', 'cross_book_alert',
    'MultiBookConsensus',          # 前端组件名
    '_lookup_multibook_consensus', # 后端禁用函数名（即使 return None）
)
```

### 3.4 匹配规则（两路）
1. **import 路**（保留现逻辑）：`^\s*(from\s+\S*banned\S*\s+|import\s+\S*banned)` —— 覆盖模块引入。
2. **符号出现路**（新增）：`re.search(rf'\b{banned}\b', src)` —— 覆盖 dict 字段赋值、函数定义、类型声明、组件名、JSX 引用。
   - 对前端 `.tsx`/`.ts`：直接整文件符号匹配（含注释/JSX 标签）。
   - 对后端：同整文件符号匹配。

### 3.5 白名单/豁免（仅限离线训练域）
`archive/` 与 `verification/` 与 `pipeline/` 内**显式标注** `@ir32-offline-allowed` 的离线回测代码不扫（现有 `verification/constants.py`、`pipeline/sharp_provider.py` 等若含 banned 符号，须有该标注或迁移）。扫描前从 `SCAN_DIRS` 排除 `archive/`。

---

## 4. 落地前置：清除残留骨架（WINDOW 项，不在本规格内执行）

要让 §3.5 零容忍守卫**干净通过**，必须先移除生产代码里现存的 banned 符号（否则守卫会立即 FAIL 自己）：

1. `bridge_service.py`：删除 `_lookup_multibook_consensus` 函数定义（6464-6466 行）与 7803 行 `"multibook_consensus":` 字段赋值；若该响应字段被前端依赖，改为**不输出该键**（前端已 `if (!mb) return null` 优雅降级，无破坏）。
2. `frontend/src/types/index.ts`：删除 `multibook_consensus?: MultibookConsensus | null`（588 行）及 `MultibookConsensus` 类型定义。
3. `frontend/.../sections.tsx`：删除 `MultiBookConsensus` 组件（125-182 行）。
4. `frontend/.../index.tsx`：删除 import（8 行）与挂载（265 行）`<MultiBookConsensus card={card} />`。
5. 回归：`pytest tests/ -q` 全绿 + `npm run build`（前端）通过 + 手动确认 MatchAnalysisModal 无报错。

> 注意：以上删除是**纯移除禁令对象**，非新增功能，不引入 edge、不碰 events.db、不杀进程，符合红线。但修改 `bridge_service.py` 与前端属生产代码，**须维护窗口 + 回归**，故列为独立 WINDOW 任务，不在本自动化周期内执行。

---

## 5. 本规格交付物（已落盘，本轮回做）

- 本文件 `docs/P-SEC-crossbook-frontend-spec.md`：覆盖缺口根因 + 扩展守卫设计（§3）+ 落地前置清除清单（§4）。
- 不修改 `tests/test_no_crossbook.py`、不修改任何生产代码（纯规格）。
- 建议后续 WINDOW 任务：按 §4 清除骨架后，按 §3 扩展守卫并跑 `pytest` 验证零容忍通过。

---

## 6. 验收门禁（落地时）

- [ ] 扩展后守卫扫描 `frontend/src` + 后端目录，命中 banned 符号即 FAIL。
- [ ] 现有生产代码经 §4 清除后，守卫全绿（零 banned 符号残留）。
- [ ] `pytest tests/ -q --timeout=120` 全量回归通过。
- [ ] 前端 `npm run build` 通过，MatchAnalysisModal 无类型/运行时错误。
- [ ] 无 events.db 写入、无进程 kill、无 schema 变更（纯代码移除）。
