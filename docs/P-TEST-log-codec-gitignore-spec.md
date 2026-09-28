# T61 共享 SSoT 模块被 `.gitignore` 静默吃掉 —— 改名 + 整类守卫（纯代码，不改解析行为）

> 承接：T57 ⑤b（预置条目）、T52（共享解码器 SSoT 建立）、T44（首个踩坑方）。
> 性质：**纯代码低风险** —— 只改名与同步引用，不改动任何解码行为、不碰生产数据、零进程操作。
> 判定：PASS（Q1 重复解码器 0 / Q2 整文件解码 0 / Q3 活体 OK / Q4 未登记 0）。

---

## 1. 问题（可复现，非推测）

```bash
git check-ignore -v scripts/_log_codec.py     # → .gitignore:142:_*.py   scripts/_log_codec.py
```

`.gitignore:142` 的 `_*.py` 会把**以 `_` 开头的共享模块整文件忽略**：`git status` 里连
`??` 都不显示。T57 已因此把 `scripts/_verdict_guard.py` 改名 `verdict_guard_ssot.py` 并加了
`test_ssot_module_is_tracked_by_git`；**T52 的 `scripts/_log_codec.py` 同款未处理** ——
一旦重建/克隆环境，两个守卫同时 `import` 失败，而本地工作区**看不出任何异常**。

实测全仓 `scripts/` + `tests/` 下被忽略的 `.py` 共 **2** 个：

| 文件 | 状态 | 处置 |
|---|---|---|
| `scripts/_log_codec.py` | 共享 SSoT，被两个审计脚本 import | **本条改名** `scripts/log_codec_ssot.py` |
| `scripts/_analyze_live_ou_margin.py` | 一次性只读分析脚本（2026-08-24，零引用、无生产调用方） | **登记带理由**，不入库 |

## 2. 处置

1. **改名** `scripts/_log_codec.py` → `scripts/log_codec_ssot.py`（对齐 T57 的 `_` 规避命名）。
2. **同步引用** 3 处：`scripts/audit_log_codec_ssot.py`（import + 2 处 basename 自避 + 报告 `ssot` 字段）、
   `scripts/audit_predict_refresh_lock.py`（import + docstring）、`tests/test_audit_log_codec_ssot.py`。
3. **新增 Q4 整类守卫**（`scripts/audit_log_codec_ssot.py::scan_git_ignored_python`）：
   用 `git check-ignore --stdin` 一次批查 `scripts/` + `tests/` 全部 `.py`，命中项**必须进
   `GITIGNORED_PY_REGISTRY` 并写理由**，空理由不许登记（与 T57 同立场：防「自动豁免」把守卫掏空）。
   未做检查 = FAIL（`build_findings` 缺省值 `<NOT RUN>`）。
4. **测试** 7 条新用例（T4 段）：SSoT 必须被 git 索引 / 模块名不得以 `_` 开头 / 无静默忽略 /
   扫描面闭合自检 / 登记理由非空 / `build_findings` fail-closed / `iter_python_files` 双根覆盖。

## 3. 为什么选「改名」而不是「改 gitignore」

`.gitignore:138-141` 的 `_*.py` 与 `_*.log` / `_*.png` / `_*.db` / `_*.xml` 同属「一次性产物」规则族；
加一条 `!scripts/*.py` 反向规则会把未来所有 `_foo.py` 产物一并放行，**扩大而非消除歧义**。
改名把风险挡在命名约定里，再由 Q4 守卫保证「任何被忽略的 .py 都必须显式登记」——
**这是一次性产物规则与共享模块的唯一显式边界**。

## 4. 本轮实测（诚实读数的三条）

- **N1 `git check-ignore --stdin` 必须走 bytes 入参**：`subprocess.run(..., text=True, input="a\nb\n")`
  实测返回空 stdout 且 rc=1 → 整份扫描变「0 个被忽略文件」**假绿**；bytes 入参正常。
  该失效模式与 T52 的「字节当路径喂 `os.path.exists` 得假零」同形 —— 又一处「静默返回空 = 看起来健康」。
  已写进代码注释与 `test_scan_git_ignored_python_sees_the_known_case` 防守卫自身退化。
- **N2 fail-closed 自证**：临时落 `scripts/_tmp_guard_probe.py` → 扫描立即报
  `unregistered: ['scripts/_tmp_guard_probe.py']`；删除后恢复。守卫不是恒绿装饰。
- **N3 活体回归随日志增长**：`logs/autonomous_monitor.log` 由 T52 读数的 767 行 → 本轮 **800 行**
  （含中文 798 行，整文件 UTF-8 解码仍失败 `whole_file_utf8_works=false`），混合编码事实不变。

## 5. 验收

- 单文件：`tests/test_audit_log_codec_ssot.py` **19 passed**。
- 全量回归：**641 passed / 0 failures / 0 errors**（junit-xml 复核）。
- 报告：`reports/log_codec_ssot_audit.{json,md}`（verdict **PASS**，Q4 `ignored_total=1 / unregistered=0`）。

## 6. 诚实边界与未决

- **不产生样本、不推进 G1、不产生 edge**；三源仍全 `NO EDGE` / `INCONCLUSIVE`，P0 `FAILED` 不变。
- 未碰 `events.db`、未跑验证台、未写 `verification.db`、零进程操作；只改自有 `scripts/` `tests/` `docs/` `reports/`。
- **未决 Q-a**：`scripts/_analyze_live_ou_margin.py` 是否应补 `git add`（一次性脚本，入库价值低，
  但它在 D 盘非备份盘 → 丢了的代价是真丢失；建议由老板决定是否入库）。
- **未决 Q-b**：Q4 扫描面是否扩展到 `pipeline/`、`analysis/`、仓库根（当前仅 `scripts/` + `tests/`；
  这两处不含 `_`-开头共享模块，扩展面收益待评估）。
