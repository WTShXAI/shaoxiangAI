# 哨响AI — 模型晋升 / 回滚 Runbook

> 对齐 ECC `mle-workflow` 生产方法论（prediction contract / data contract / reproducible pipeline / promotion gates / serving contract / monitoring / rollback）。
> 本文件是**生产操作手册**，所有模型变更必须走此流程。任何绕过下方"晋升铁律"的合入一律视为违规。

---

## 0. 一句话原则

**Fail-closed（默认拒绝）。** 模型未经 `test_oos_guard.py` 全绿 + OOS 审计干净，不得进入 `config/settings.yaml` 的生产路径。In-sample 幻觉（含 draw_expert 家族）**永远不得**被当作真实 edge 合入。

---

## 1. 当前生产状态（基线快照 · 2026-07-11）

### 1.1 生产模型指针（`config/settings.yaml` paths 块）
| 配置键 | 指向文件 | 角色 |
|--------|----------|------|
| `paths.v41_model` | `saved_models/football_balanced_production.joblib` | 主预测模型（v4.1 路径） |
| `paths.v32_model` | `saved_models/football_balanced_production.joblib` | 兼容路径指针 |
| `paths.draw_expert` | `saved_models/draw_expert_v1.joblib` | 平局辅助（**仅作 IN_SAMPLE_ONLY 参考，不当 edge**） |

> 说明：`football_v4.0_production` / `football_v4.1_production` / `football_ensemble_20260618` 三个 joblib 加载失败（依赖已删模块），已隔离出生产路径；现统一指向 `football_balanced_production.joblib`。

### 1.2 最近一次 OOS 审计（`deliverables/model_oos_audit_20260711.json`）
- `broken_count = 0`，`live_path_broken = []` ✅（生产路径无坏模型）
- draw_expert 家族诚实 OOS（WC LOOCV）：
  - `draw_expert_v3_focal`: ROC-AUC **0.369** (随机=0.5) → `IN_SAMPLE_ONLY`
  - `draw_expert_wc_v4`: ROC-AUC **0.288** → `IN_SAMPLE_ONLY`
  - `_backup_draw_expert_pre_redump`: ROC-AUC **0.369** → `IN_SAMPLE_ONLY`
  - **结论**：平局家族在当前 77 维特征上不可泛化，市场效率天花板已证。保留作参考，**不部署 v4、不宣称平局 edge**。

### 1.3 测试与门禁现状（晋升前必须复现）
- 全量测试：**46 passed**
- SSoT 覆盖率硬门禁（bet_core / quant_executor / api_budget ≥80%）：**80.85%** ✅
- CI 链路：`.github/workflows/ci.yml` = `lint` → `security`（密钥/环境变量扫描）→ `test`（含覆盖率门禁）→ `frontend` → `build`

---

## 2. 契约（Contracts）

### 2.1 预测契约（Prediction Contract）
- 入口：`pipeline/engine.py`（`WCEngine` / `LeagueEngine`）。
- 输出形状：`{"verdict": "主胜|平局|客胜", "probabilities": {H,D,A}, "softline_adjusted": bool, "disagreement_detected": bool}`。
- 铁律：1X2 有效市场无超越赔率 edge；唯一真实 edge = 跨庄/跨市场不平衡（soft line 价差）。argmax 缺陷已记录。

### 2.2 数据契约（Data Contract）
- 源：`D:/Architecture/data`（禁虚拟数据）。
- 时序：pre-2023 训练 / 2023+ OOF（out-of-fold）验证。
- 禁 Beta 校准、禁小样本规则主导（<5 样本 → 平）。

### 2.3 服务契约（Serving Contract）
- 模型由 `config/settings.yaml` 路径加载，**单例懒加载**；服务启动即触发 `test_oos_guard` 等价检查（production 模型必须可加载）。
- 模型文件落地目录：`saved_models/`，**必须纳入版本控制或显式归档**（曾因 untracked 在干净 clone 崩溃）。

---

## 3. 晋升流程（Promotion — fail-closed）

```
候选模型产出
   │
   ▼
[1] 重训/导出 (scripts/train_*.py 或统一训练入口)
   │  - 必须 reproducible: 固定 random_state、记录训练数据窗口、记录依赖版本
   ▼
[2] OOS 审计 (scripts/audit_all_models_oos.py)
   │  - 产出 deliverables/model_oos_audit_<date>.json
   │  - 检查: broken=0, live_path_broken=[], draw_expert 全 IN_SAMPLE_ONLY
   ▼
[3] 晋升门禁 (pytest tests/test_oos_guard.py -q)  ← CI 自动跑, 必须全绿
   │  ① 任何 *production*.joblib 必须可加载
   │  ② config 指向的模型必须可加载
   │  ③ 审计干净 + draw_expert 家族诚实 IN_SAMPLE_ONLY
   ▼
[4] 配置翻牌 (改 config/settings.yaml 的 paths.* 指针)
   │  - 旧指针保留注释, 便于秒级回滚
   ▼
[5] 本地冒烟 (启动 bridge_service → /health 显示 checks.ok=true)
   ▼
[6] CI 全绿 (lint+security+test+coverage≥80%+frontend+build)
   ▼
[7] 部署 (Docker / docker-compose, restart unless-stopped + healthcheck)
   ▼
[8] 监控观测 (见 §5) 持续 24-72h, 无质量退化才视为晋升完成
```

### 3.1 晋升铁律（不可谈判）
1. **未过 `test_oos_guard` 不进生产路径。**
2. **In-sample 指标（含 draw_expert 家族 ROC<0.5）不得写进对外文档当作 edge。**
3. **配置翻牌前必须先在本地 `/health` 验证模型可加载、不抛异常。**
4. **模型文件必须可复现**：记录训练窗口 + 依赖版本（sklearn 1.9.0 与 1.6.1 反序列化不兼容已踩坑）。

---

## 4. 回滚流程（Rollback — 秒级）

触发条件（任一即回滚）：
- `test_oos_guard` 在 CI 或本地变红；
- `/health` 的 `checks` 出现 `ok=false`（db / quant_engine / api_budget 任一失败）；
- 监控发现预测质量或量化绩效 24h 内显著退化（命中率/ROI 跌破基线 -2σ）；
- 线上出现 `database is locked`、量化 confirm 路径超时等生产 bug。

### 4.1 配置回滚（最常用）
```bash
# 1. 还原 config/settings.yaml 的 paths.* 到上一稳定指针（git 历史里有旧注释）
git show HEAD~1:config/settings.yaml > config/settings.yaml   # 或手动改回注释中的旧路径

# 2. 重启服务
docker compose restart bridge        # 或 Windows: 重启 start_g3_daemon.bat
```
> 因为旧指针在翻牌时保留了注释，回滚=改一行路径，无需重新训练。

### 4.2 代码回滚
```bash
git revert <bad_commit>              # 或 git checkout <last_good_tag> -- .
# 重跑 CI 确认全绿
pytest tests/ -q --timeout=120
```

### 4.3 坏模型隔离（防御性）
若发现某 `*production*.joblib` 无法加载，立即移出生产目录而非删除：
```bash
mkdir -p _broken_quarantine_$(date +%Y%m%d)
mv saved_models/<bad_model>.joblib _broken_quarantine_$(date +%Y%m%d)/
# 将 config 指针改指 football_balanced_production.joblib（已知可加载）
```
> 历史教训：曾 3 个 production 模型加载失败却仍在配置路径，CI 门禁上线后才拦截。

---

## 5. 监控（Monitoring）

### 5.1 系统监控（System）
- `/health` 端点返回 `checks`：`db`（真实 sqlite 查询）、`quant_engine`、`api_budget_remaining`。任何 `ok=false` 即告警。
- Docker healthcheck 已配；`restart: unless-stopped` + json-file 日志轮转（50m×5）。
- 速率限制：`RATE_LIMIT_PER_MIN`（默认 120/min）对 `/api*` `/predict*` 生效。

### 5.2 质量监控（Quality / ML）
- 量化绩效：`quant_trading.db` 的 daily_performance / strategy_performance 表（**注意 settle 后必须二次 commit，曾有静默丢数据 bug，已修**）。
- 预测质量：定期重跑 `scripts/audit_all_models_oos.py`，对比 `deliverables/model_oos_audit_*.json` 的 ROC/PR 是否退化。
- API 预算：`api_budget` 日 cap=300 / hard_floor=500，剩余低于 50 告警（DailyCollector 已接）。

### 5.3 告警阈值建议
| 指标 | 告警线 | 动作 |
|------|--------|------|
| `/health` ok | =false | 立即查 checks 明细 |
| SSoT 覆盖率 | <80% | 阻断 merge（CI 已强制） |
| 量化 ROI（24h） | 跌破基线 -2σ | 暂停自动决策，人工复核 |
| API 剩余配额 | <50 | 降频/暂停拉取 |

---

## 6. 命名与归档规范

- 生产模型：`<role>_production.joblib`（如 `football_balanced_production.joblib`）。
- 候选/实验：`multi_*_<YYYYMMDD>_<HHMMSS>.joblib`、`draw_expert_vN_focal.joblib`。
- 审计产物：`deliverables/model_oos_audit_<YYYYMMDD>.json`（保留历史，便于退化溯源）。
- 复盘纪要：任务完成 → 知识官归档 `~/哨响AI/档案馆/`（含 Credit/Token 记录）。

---

## 7. 反模式（禁止清单）

- ❌ 把 in-sample 准确率（如 draw_expert v3 的 0.742 背下值）当 OOS edge 对外宣称。
- ❌ 在 `wc_engine.py` 平行加 `bridge` 已实现的软线逻辑（曾造死代码+NameError，已回退）。
- ❌ 量化资金路径内开**第二个 sqlite 连接**（嵌套连接死锁，live confirm 路径曾卡死，已内联修复）。
- ❌ 模型文件留 untracked（干净 clone/CI 崩溃）。
- ❌ 关掉 `ENABLE_ML_MARKET_OVERRIDE` 护栏却让 ML 静默覆盖 argmax（sklearn 1.9.0 兼容坑，已用护栏禁用覆盖）。

---

## 8. 一键校验清单（晋升前本地自测）

```bash
cd /d/Architecture
# 1. OOS 审计干净
.venv/Scripts/python.exe scripts/audit_all_models_oos.py
# 2. 晋升门禁全绿
.venv/Scripts/python.exe -m pytest tests/test_oos_guard.py -q
# 3. 全量 + 覆盖率
.venv/Scripts/python.exe -m pytest tests/ -q --cov=scripts.bet_core --cov=scripts.quant_executor \
  --cov=pipeline.collectors.api_budget --cov-fail-under=80 --timeout=120
# 4. 服务健康
.venv/Scripts/python.exe -c "import bridge_service; print('import OK')"
# 5. /health 冒烟 (启动后 curl http://localhost:9000/health)
```

全部绿 → 可翻牌 `config/settings.yaml` → 提交 → CI → 部署 → 监控 24-72h。
