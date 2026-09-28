# T57 判定词表 / Tier 表合流规格（承接 T51 R6 + T53 §2）

**状态：已落地（代码 + 测试同批提交）**。产出：
`scripts/verdict_guard_ssot.py`（SSoT）+ `scripts/audit_verification_report_freshness.py`（改）
+ `scripts/audit_mh_train_div_bypass.py`（改）+ 两处基线测试同批更新（否则套件当场红）。
全量回归 **587 passed / 0 failed**（两守卫文件 57 passed，本轮新增 4 个用例）。

---

## 1. 病根：两份词表、两份 Tier 表

| | T51 `audit_verification_report_freshness.py` | T53 `audit_mh_train_div_bypass.py` |
|---|---|---|
| 判定词表 | 要求字面量**两侧带引号** `(?:"|')(NO_EDGE\|NO EDGE\|EDGE\|INCONCLUSIVE)(?:"|')` | 放宽为裸词边界 `(?<![A-Za-z0-9_])(?:NO_EDGE\|NO EDGE\|INCONCLUSIVE\|EDGE\|BEATS MARKET\|TIE)(?![A-Za-z0-9_])` |
| 结果 | 漏掉 `mh_train_walkforward.py:118` / `mh_train_walkforward_x.py:82`（行尾无引号的 `NO EDGE (model >= market)`） | 捞到，但豁免清单（`NOISE_FILES`/`DOC_QUOTE_FILES`）**只对它自己可见** |
| 同一个文件 | `scripts/backtest_ou_signal.py` → **UNEXPECTED**（RED） | → `NOISE_KNOWN` |
| 同一个文件 | `scripts/mh_train_walkforward.py` → **UNEXPECTED**（RED） | → `EXPECTED_TIER` |

→ 两个守卫对**同一份代码给出相反判定**：T53 说"已登记"，T51 说"未登记产出体，须人工确认"。
放宽词表若只改一边，T51 会在**已知 offenders** 上永久红——守卫退化成背景噪声，
这正是 T30-D「拒绝现在就开零容忍」想避免的事。故本条的核心不是换正则，而是**合流**。

## 2. 落地形态（SSoT）

`scripts/verdict_guard_ssot.py` 定死四件事，两处调用点一律薄封装：

1. **一份词表**：`RE_VERDICT_TOKEN`（放宽版）+ `RE_VERDICT_QUOTED`（精确版，仅供低召回对照/回归用例）。
2. **一份 Tier 判定**：`classify_verdict_file()`。次序 =
   `verification/` → EMITTER_OK · 渲染白名单 → RENDER_CONSUMER · 绕过体登记册 → EXPECTED_TIER ·
   假阳性登记册 → KNOWN_FALSE_POSITIVE · `analysis/`/`sandbox/`+撞车文件 → NOISE_KNOWN ·
   `tests/`+`*audit*`/抄值文件 → DOC_META · 其余 → **UNEXPECTED（RED）**。
3. **一份带理由登记册**：`EMITTER_REGISTRY` / `FALSE_POSITIVE_REGISTRY` / `NOISE_FILE_REGISTRY` /
   `DOC_QUOTE_REGISTRY`，全部 `Dict[str, str]`，**空理由即不许登记**
   （`registry_reasons_complete()` 检出，`test_registry_reasons_cannot_be_empty` 守）。
4. **一份自避清单**：本模块含全部判定词字面量，`GUARD_SELF_EXCLUDE` 让两个扫描器按 basename 跳过。

跨审计回归（`test_both_guards_share_one_wordlist_and_one_tier_table`）静态断言两个审计脚本
**不得自带 `re.compile` 判定词正则**——与 T52 的日志解码 SSoT 同款做法。

> **落地翻车实录**：SSoT 初版命名为 `scripts/_verdict_guard.py`，被 `.gitignore:142`
> 的 `_*.py` **整文件忽略**——`git status` 里连 `??` 都不显示。SSoT 不在版本控制里意味着
> 克隆/重建环境后**两个守卫同时 import 失败**，而本地完全无感。已改名为
> `scripts/verdict_guard_ssot.py`（不再以 `_` 开头），并加
> `test_ssot_module_is_tracked_by_git`（用 `git check-ignore -q` 断言 SSoT 未被忽略）。
> **同款隐患（T61 已处理）**：T52 的共享模块 `scripts/_log_codec.py` 同样被这条规则忽略
> —— T57 提出、T61 已改名 `scripts/log_codec_ssot.py` 并加 Q4 守卫（本文件成文时的
> “尚未处理” 状态至此消解）。

## 3. 基线算术（与本条预写的"2 → 4"不同，已就地修正）

预写在 backlog 的预期是 `unexpected` 从 2 变 4（在册 3 + 假阳性 1）。**实测是 0**，偏差原因：

- 预写算术假设"3 个 mh 绕过体在 T51 侧仍是 UNEXPECTED"——但它们是**已确证的真绕过体**，
  本就该进登记册（T53 本来就这么登记）。让它们保持 UNEXPECTED 等于让守卫**永久红在已知犯上**。
- 已判定假阳性 `pipeline/fusion_wdl_proto.py`（阈值字典键）同样登记为 `KNOWN_FALSE_POSITIVE`，
  而不是靠"恰好没被词表命中"蒙混过关。

故放宽词表后的**真基线 = UNEXPECTED 为空**，监控点改为「登记册是否还完整、是否还有新的自印判定」。
**零基线只在登记册仍每条有理由时成立**——空理由一旦被塞进册子，先红的会是
`test_registry_reasons_cannot_be_empty`，防止把零基线当免死金牌。

## 4. 诚实边界：放宽是用精度换召回

实测命中分布（全仓 .py，自避后）：`EMITTER 24 / RENDER 3 / NOISE 23 / DOC 67 /
EXPECTED 9 / FALSE_POSITIVE 7 / UNEXPECTED 0`。对照旧词表：`EMITTER 8 → 24`、
`DOC 4 → 67`、`NOISE 18 → 23`——**注释里的裸 `EDGE` 也命中了**。噪声面大幅上涨，
压噪声的责任全部交给 Tier 表（这正是合流的目的）。

- **不改任何既有判定结论**：三源仍 `NO EDGE / INCONCLUSIVE`，P0 FAILED 不变。
- **不影响任何生产面**：未改 `verification/`、未跑验证台、未写 `verification.db`、
  未碰 `events.db`、未挂调度、零进程操作。
- 已登记噪声 `scripts/backtest_ou_signal.py` 属 T58 讨论对象（信号词改名），
  在改名之前先以"带理由登记"方式入册，不静默豁免。

## 5. 顺手修掉的一处真 bug

`scripts/audit_mh_train_div_bypass.py::render_md()` 用 `br["missing_gates"]` 取键，
而 `gates_verdict_for_mh()` 产出的键名是 `blocking_gates` → `main()` 每次都
`KeyError` 崩在最后一步（`reports/mh_train_div_bypass_audit.md` 上次根本没写出来）。
已改；本轮重跑 `unexpected=0 live_ok=True` 正常出报告。属**脚本自身缺陷**，非生产面。

## 6. 未决

- **Q-a**：`scripts/model_g1_reach_plan.py` 是"叙述性抄值"，本次按带理由登记入 DOC_META；
  是否值得像 T58 那样改名/抽常量，待 T58 一并裁。
- **Q-b**：`analysis/` 与 `sandbox/` 靠前缀豁免，一旦出现同语义的新目录会漏；
  建议下一步把前缀豁免也换成带理由登记册（本条未做，属扩大范围）。
- **Q-c**：T51 的 `RE_VERDICT_LITERAL` 与 T53 的 `RE_VERDICT_TOKEN` 同名不同模块，
  调用方易误用；是否统一改名（如 `VERDICT_TOKEN_RE`）待定。
