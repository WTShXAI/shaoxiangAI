# 计划任务对账建议（WINDOW 备料，不自动改生产调度）

> 生成：2026-09-26，自动化 dbda4380 执行 T16（纯规格/建议，供老板决策）。
> 来源：`scripts/check_env_health.py`（T13 落地，只读 schtasks + pip + db，pytest 通过）实测结果 `reports/env_health_check.json`。
> **红线**：本文件只给"建议注册/启用清单 + 风险"，**绝不**执行 `schtasks /change /enable` 等生产改动；改动须老板决策 + 维护窗口。

---

## 1. 真实发现（来自 T13 实测）

`check_env_health.py` 跑出 **FAIL×1 / WARN×0**，唯一 FAIL 如下：

| 任务 | 期望 | 实测 | 状态 |
|---|---|---|---|
| `ShaoxiangBridge_Watchdog` | 启用 | Running | PASS |
| `ShaoxiangAI_ProdGuardian` | 启用 | Running | PASS |
| `ShaoxiangAI_NetWatch` | 启用 | Ready | PASS |
| `ShaoxiangVite` | 启用 | Running | PASS |
| `ShaoxiangAI_DailyRecheck` | 启用 | Ready | PASS |
| `ShaoXiangOddsAssetDaily` | 启用 | **Disabled** | **FAIL** |
| `ShaoxiangSandbox2301` | 停用 | Disabled | PASS |

两项与现行指南 `环境复现指南.md §7.1` 不一致：

- **发现①** — 指南 §7.1 仍列 `ShaoxiangGQ_Watchdog`（保活 ws_collector 乐鱼采集），但该任务已于 **2026-09-24 退役**，`ws_collector` / GQ token 自愈改由 `ShaoxiangAI_ProdGuardian` 接管（见 prod_guardian 记忆：断流自愈 SSoT）。指南与 `check_env_health.py` 的 EXPECTED_TASKS 均已更新为 `ShaoxiangAI_ProdGuardian`，**仅 §7.1 表格文本未同步**。
- **发现②** — `ShaoXiangOddsAssetDaily`（"数据资产日报"）当前状态=**Disabled**，但校验器期望=启用。即"指南期望启用 vs 生产实际停用"的反差，**该任务是否仍需要、启用后会做什么，需老板确认**（见 §3 风险）。

---

## 2. 建议清单（待老板拍板）

### A. 文档同步（低风险，纯文本，可直接做）
- 更新 `环境复现指南.md §7.1` 表格：删除 `ShaoxiangGQ_Watchdog` 一行，新增 `ShaoxiangAI_ProdGuardian`（保活 ws_collector + GQ token 自愈）行。
- §7.2 示例代码里 `GQ_Watchdog` 的启用/停用注释同步改为 `ShaoxiangAI_ProdGuardian`。
- **执行方**：AI 可直接改该 md（不碰调度）。可排入下一个文档类 backlog 项。

### B. `ShaoXiangOddsAssetDaily` 启用决策（中风险，需老板确认后由窗口执行）
- 决策项：**启用 / 保持停用 / 退役删除**。
- 若启用，需先确认该任务指向的脚本与产物（"数据资产日报"具体生成什么、写入何处、是否触碰 events.db / 是否对外），确认无红线冲突后再 `schtasks /change /tn "ShaoXiangOddsAssetDaily" /enable`。

---

## 3. 风险与前提

| 项 | 风险 | 缓解 |
|---|---|---|
| A 文档同步 | 极低（不碰调度、不碰库） | 直接改，无需窗口 |
| B 启用 OddsAssetDaily | 中：未知脚本可能写库/产外部件/触发对外推送（wecom 850003 仍失效，推送会静默丢失）；与 §4 数据保全 / IR-30 诚实口径可能冲突 | **先读该任务 action 指向的脚本与产物**，确认不写 events.db、不喊单、不跨庄；再于维护窗口 `schtasks /change /enable`，启用后跑一次 dry 观察日志 |
| B 若实质已无用 | 低：长期 Disabled 属无害残留 | 若确认废弃，建议显式 `schtasks /delete` 并清理 §7.1 条目，消除"幽灵期望" |

---

## 4. 不做的边界（本周期）

- **不**执行任何 `schtasks /change /enable|/disable|/run` —— 属生产调度改动，须人工 + 窗口。
- **不**删除/重建任何计划任务。
- **不**触碰 events.db / GQ.db / football_data.db。

---

## 5. 落地前置（建议追加 backlog）

- [ ] **B1** 读 `ShaoXiangOddsAssetDaily` 的 action 脚本与产物，确认红线合规性，输出《OddsAssetDaily 合规诊断》供 B 决策。
- [ ] **B2** 按 §2-A 更新 `环境复现指南.md §7.1` 表格（删除 GQ_Watchdog / 增 ProdGuardian）。
- [ ] **B3**（老板拍板后）在维护窗口执行 B 决策项，启用后 dry 观察一轮。
