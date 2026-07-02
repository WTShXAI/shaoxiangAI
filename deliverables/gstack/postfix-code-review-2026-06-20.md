# FootballAI v4.0 P0 修复后验证报告

**日期**: 2026-06-20
**审查人**: gstack-product-reviewer (代码审查)
**项目路径**: `D:\Architecture`
**审查范围**: 验证用户团队声称修复的11项非安全类P0问题 + 检查遗漏项 + 回归检测

---

## TL;DR

- **修复质量评分**: 6.5/10
- **判断**: 🟡 **Conditional Go** — 11项中8项彻底修复，3项部分修复；2项未提及的P0仍存在（模块遮蔽+MCC=0）；发现1项新回归（模型文件结构不一致）
- **核心风险**: P0-13模块包遮蔽未修复（`from modules.feedback_loop import ...` 仍失败）；P0-16 MCC=0 未修复（模型等同于随机猜测）；两个"v4.1"模型文件结构不同，UnifiedPredictor和ModelBridge加载不同物理文件

---

## 1. 逐项验证11项修复

### ✅ #6 asyncio→_asyncio NameError — 彻底修复

| 项目 | 内容 |
|------|------|
| 文件 | `backend/main.py:1120` |
| 验证 | `await _asyncio.sleep(30)` — 使用正确的导入别名 |
| 结论 | ✅ 彻底修复 |

---

### ✅ #9 D-Gate spread<1.6→<0.16 — 彻底修复

| 项目 | 内容 |
|------|------|
| 文件 | `backend/main.py:600, 619, 660` |
| 验证 | 三处均已改为 `spread < 0.16`：line 600（模式B触发条件）、line 619（分析点判断）、line 660（庄家动机模式4） |
| 结论 | ✅ 彻底修复 |

---

### ✅ #14 DrawExpert硬编码→等权 — 彻底修复

| 项目 | 内容 |
|------|------|
| 文件 | `predictors/unified_predictor.py:567-579` |
| 验证 | Line 569: `de_signal = np.array([1/3, 1/3, 1/3])` 替换原 `[0.33, 0.34, 0.33]`。新增真实DrawExpert模型使用路径（lines 570-577）+ 降级日志（lines 577, 579）。原硬编码值 P(D)=0.34 有意偏向平局（0.34>0.33），新值 1/3≈0.333 为真中性 |
| 改进点 | 1) 消除0.068→0.0667的恒定平局偏置；2) 透明降级（有日志）；3) DrawExpert可用时使用真实信号 |
| 局限 | 20%权重仍分配给中性信号，稀释XGB+LGB。更优方案是将20%重分配给XGB(0.60)+LGB(0.40)，但作为最小P0修复可接受 |
| 结论 | ✅ 彻底修复 |

---

### ⚠️ #18 config.yaml model_path v3.2→v4.1 — 部分修复

| 项目 | 内容 |
|------|------|
| 文件 | `config/config.yaml:603` |
| 验证 | Config已更新: `model_path: saved_models/football_v4.1_production.joblib`。ModelBridge `_resolve_model_path()` 正确读取此配置 |
| **问题** | UnifiedPredictor和ModelBridge加载**不同的物理文件**，虽然都叫"v4.1"但结构不同：<br>- UnifiedPredictor → `models/main/football_v4.1_production.joblib` (10.5MB, 16 keys: 有`v41_config`/`nn_scaler`，无`calibrator_suite`/`sub_models`)<br>- ModelBridge → `saved_models/football_v4.1_production.joblib` (4.5MB, 25 keys: 有`calibrator_suite`/`sub_models`/`optimal_thresholds`，无`v41_config`/`nn_scaler`)<br>**两文件key集合完全不同**，模型版本分裂从"v3.2 vs v4.1"变为"v4.1-main vs v4.1-saved" |
| 结论 | ⚠️ 部分修复 — 名称统一但物理文件仍分裂 |

---

### ✅ #11 SKY键名映射 — 彻底修复

| 项目 | 内容 |
|------|------|
| 文件 | `predictors/sky/sky_predictor.py:176-182` |
| 验证 | 同时返回 `proba_final` 和 `probabilities` 键，格式为 `{'home': ..., 'draw': ..., 'away': ...}`（小写键名） |
| 引擎匹配 | `six_layer_conversation.py:822`: `sp = sky_result.get("probabilities")` ✅；line 826-828: `sp.get("home", sp.get("H", 0))` ✅ 格式匹配 |
| 结论 | ✅ 彻底修复 |

---

### ⚠️ #12 VIP键名映射 — 部分修复

| 项目 | 内容 |
|------|------|
| 文件 | `predictors/vip/vip_final.py:712-720` |
| 验证 | 同时返回 `probs` 和 `probabilities` 键，但格式为 `{'H': ..., 'D': ..., 'A': ...}`（**大写键名**） |
| 引擎匹配 | `six_layer_conversation.py:842`: `vp = vip_result.get("probabilities")` ✅ 键名匹配；lines 846-848: `vp.get("home", vp.get("H", 0))` ✅ 防御性回退使值正确进入triple字典 |
| **问题** | Line 851 日志: `vp.get('home',0)` **无大写回退** → VIP通道始终日志 `0.000/0.000/0.000`，误导调试。SKY用小写`home/draw/away`，VIP用大写`H/D/A`，格式不一致被防御性代码掩盖，是潜在bug |
| 结论 | ⚠️ 部分修复 — 功能可用但格式不一致+日志错误 |

---

### ✅ #13 补全trap_probability_bridge.py + odds_inverse_calibrator.py — 彻底修复

| 项目 | 内容 |
|------|------|
| 文件 | `predictors/components/trap_probability_bridge.py` (650 bytes), `predictors/components/odds_inverse_calibrator.py` (50KB) |
| 验证 | 两文件均已复制到项目内。`trap_probability_bridge.py` 导出 `apply_trap_correction()`。`odds_inverse_calibrator.py` 导出 `apply_goal_segment_correction()` + `OddsInverseCalibrator` 类 |
| 导入链 | VIP `vip_final.py:37-43` 有双重导入路径（`predictors.components.xxx` 优先，bare `xxx` 回退），均验证通过 |
| 结论 | ✅ 彻底修复 |

---

### ⚠️ #8 硬编码路径→环境变量 — 部分修复

| 项目 | 内容 |
|------|------|
| 文件 | `predictors/unified_predictor.py:48` |
| 验证 | Line 48: `FOOTBALLAI_ROOT = os.environ.get('FOOTBALLAI_ROOT', r"D:\AI\footballAI")` — 环境变量优先，但fallback仍为硬编码路径 |
| 残留问题 | 1) Lines 139, 142: `FOOTBALLAI_ROOT` 用作模型搜索路径（v4.1/v4.0候选）；Line 605: DrawExpert路径fallback → 若环境变量未设置且`D:\AI\footballAI`不存在，这些路径无效<br>2) 另有2个文件仍有硬编码路径未修改：<br>- `verification/predict_verifier.py:29-30`: `sys.path.insert(0, "D:/AI/footballAI")`<br>- `pipeline/dgate_optimizer.py:31`: `FOOTBALL_AI = Path("D:/AI/footballAI")` |
| 结论 | ⚠️ 部分修复 — 主入口改为环境变量但fallback+2个文件未处理 |

---

### ✅ #7 OCR requests→httpx — 彻底修复

| 项目 | 内容 |
|------|------|
| 文件 | `api/ocr.py:72-75` |
| 验证 | `import httpx; async with httpx.AsyncClient(timeout=15.0) as client: resp = await client.post(...)` — 正确替换为异步HTTP客户端 |
| 结论 | ✅ 彻底修复 |

---

### ✅ #15 测试编码修复 — 彻底修复

| 项目 | 内容 |
|------|------|
| 文件 | `tests/test_v4_modules.py:19-22` |
| 验证 | `sys.stdout.reconfigure(encoding='utf-8')` + `sys.stderr.reconfigure(encoding='utf-8')`。实际运行: **471/471 通过, 0 失败** |
| 结论 | ✅ 彻底修复 |

---

### ✅ #17 ensemble_trainer.py源码恢复 — 彻底修复

| 项目 | 内容 |
|------|------|
| 文件 | `predictors/components/ensemble_trainer.py` |
| 验证 | 文件存在，2509行真实源码（非反编译）。`EnsembleTrainer` 类含 `load_pipeline`, `predict_match`, `ensemble_predict_proba`, `predict_dual_path` 等方法。导入验证通过 |
| 结论 | ✅ 彻底修复 |

---

## 2. 修复验证汇总

| # | 修复项 | 状态 | 说明 |
|---|--------|------|------|
| 6 | asyncio→_asyncio | ✅彻底 | Line 1120 确认 |
| 9 | D-Gate spread<0.16 | ✅彻底 | 三处均确认 |
| 14 | DrawExpert等权 | ✅彻底 | 1/3等权+降级日志+真实信号路径 |
| 18 | config.yaml v4.1 | ⚠️部分 | 名称统一但两物理文件结构不同 |
| 11 | SKY键名映射 | ✅彻底 | probabilities键+小写格式匹配引擎 |
| 12 | VIP键名映射 | ⚠️部分 | probabilities键存在但大写格式+日志错误 |
| 13 | 组件文件补全 | ✅彻底 | 两文件存在，导入链验证通过 |
| 8 | 硬编码路径 | ⚠️部分 | 环境变量优先但fallback+2文件未处理 |
| 7 | OCR httpx | ✅彻底 | AsyncClient 确认 |
| 15 | 测试编码 | ✅彻底 | 471/471通过 |
| 17 | ensemble_trainer源码 | ✅彻底 | 2509行真实源码，导入通过 |

**统计**: ✅彻底修复 8项 / ⚠️部分修复 3项 / ❌未修复 0项

---

## 3. 仍存在的非安全P0问题

### ❌ P0-13 模块包遮蔽 — 未修复

**严重度**: P0（阻断 `from modules.xxx import ...` 导入）

**现状**: `unified_predictor.py:53` 执行 `sys.path.insert(1, FOOTBALLAI_ROOT)`，导致 `D:\AI\footballAI\modules\__init__.py` 优先于 `D:\Architecture\modules\__init__.py` 被加载。

**实测验证**:
```
import modules
→ modules package path: D:\AI/footballAI\modules\__init__.py  ← 错误来源

from modules.feedback_loop import FeedbackLoop
→ FAILED: No module named 'modules.feedback_loop'  ← footballAI没有此文件

from modules.degradation_guard import DegradationGuard
→ FAILED: No module named 'modules.degradation_guard'  ← footballAI没有此文件
```

**影响**: v4.0 modules/ 中有 `feedback_loop.py`, `degradation_guard.py`, `cross_opponent.py`, `image_input.py`, `knowledge_layer.py`, `match_analyzer.py` 等文件在 footballAI modules/ 中不存在。当这些模块被导入时将失败。`main.py:723-727, 1026-1029` 有 sys.path 重排序的 workaround 代码，但治标不治本。

**修复建议**: 重组包结构，消除 `sys.path.insert(1, FOOTBALLAI_ROOT)`。将外部依赖改为显式 import 或 pip install -e .。

---

### ❌ P0-16 模型MCC=0 — 未修复

**严重度**: P0（模型质量红旗）

**现状**: 两个 model_registry.json 中当前版本的 MCC 均为 0：

| 文件 | 当前版本 | MCC | accuracy | draw_f1 |
|------|---------|-----|----------|---------|
| `models/model_registry.json` | v0003 | **0** | 60.0% | 0.55 |
| `saved_models/model_registry.json` | 4.1 | **0** | 62.07% | 0.4913 |

**影响**: MCC=0 意味着模型预测与实际结果零相关，等同于随机猜测。对比 v3.1 的 MCC=0.2903，当前模型质量反而退化。

**修复建议**: ML团队核实MCC=0根因（可能是评估bug、数据泄露、或模型确实无效），重新评估模型质量，更新文档指标。

---

### ⚠️ P0-8 硬编码路径 — 部分修复（详见上方 #8）

**残留**:
1. `unified_predictor.py:48` — fallback `r"D:\AI\footballAI"` 仍硬编码
2. `unified_predictor.py:139,142,605` — 使用 `FOOTBALLAI_ROOT` 作为模型/DrawExpert搜索路径
3. `verification/predict_verifier.py:29-30` — `sys.path.insert(0, "D:/AI/footballAI")`
4. `pipeline/dgate_optimizer.py:31` — `FOOTBALL_AI = Path("D:/AI/footballAI")`

---

## 4. 新发现问题（修复引入的回归/新发现）

### 🆕 REG-1: 两个"v4.1"模型文件结构不一致

**严重度**: High（模型版本分裂的新形式）

**发现**: UnifiedPredictor 和 ModelBridge 加载不同的 v4.1 模型文件：

| 路径 | 大小 | Keys数 | 独有Keys |
|------|------|--------|----------|
| `models/main/football_v4.1_production.joblib` | 10.5MB | 16 | `v41_config`, `nn_scaler` |
| `saved_models/football_v4.1_production.joblib` | 4.5MB | 25 | `calibrator_suite`, `meta_model`, `sub_models`, `optimal_thresholds`, `train_timestamp`, `version`, `global_d_rate`, `*_info` |

**根因**: UnifiedPredictor 的 `_load_model()` 搜索候选列表（lines 136-142）优先 `models/main/`，而 ModelBridge 的 `_resolve_model_path()` 从 config 读取 `saved_models/` 路径。

**影响**: 两条代码路径使用结构不同的模型文件，预测结果可能不一致。原 P0-18 的"v3.2 vs v4.1"版本分裂变为"v4.1-main vs v4.1-saved"文件分裂。

**修复建议**: 统一为一个模型文件，删除冗余副本，或在 config 中明确指定两个路径使用同一文件。

---

### 🆕 REG-2: VIP通道日志始终显示0.000

**严重度**: Medium（功能可用但调试误导）

**发现**: `six_layer_conversation.py:851`:
```python
logger.info(f"[SixLayer] VIP: {vp.get('home',0):.3f}/{vp.get('draw',0):.3f}/{vp.get('away',0):.3f} → {top_v}")
```
VIP 返回 `{'H': ..., 'D': ..., 'A': ...}`（大写键），但日志用 `vp.get('home',0)`（小写键，无回退），始终返回 0。

**对比**: SKY 日志 line 831 同样用 `sp.get('home',0)`，但 SKY 返回小写键 `{'home': ..., 'draw': ..., 'away': ...}`，所以 SKY 日志正确。

**修复建议**: VIP 的 `probabilities` 值改为小写键格式 `{'home': ..., 'draw': ..., 'away': ...}`，与 SKY 统一。或日志行添加大写回退。

---

### 🆕 REG-3: 模型加载隐式依赖 draw_expert 模块

**严重度**: Low（仅影响加载路径）

**发现**: `models/main/football_v4.1_production.joblib` 加载时需要 `draw_expert` 模块可导入（pickle 反序列化需要）。若 `predictors/components/` 不在 sys.path 中，模型加载失败。

**当前状态**: `unified_predictor.py:52` 的 `sys.path.insert(0, ...)` 确保 components/ 在路径中，但这是隐式依赖。若其他入口（如 ModelBridge）在未设置 sys.path 的情况下加载此文件，将失败。

**修复建议**: 确保 `draw_expert.py` 可从标准路径导入，或在 `ensemble_trainer.py` 中注册 `sys.modules` 别名。

---

## 5. 特别关注点回答

### Q1: VIP键名映射格式一致性

**回答**: ❌ 不一致。SKY 返回 `{'home': ..., 'draw': ..., 'away': ...}`（小写），VIP 返回 `{'H': ..., 'D': ..., 'A': ...}`（大写）。引擎 `six_layer_conversation.py` 的 triple 字典赋值（lines 846-848）使用防御性 `.get("home", .get("H", 0))` 双重回退，因此**功能上可用**。但日志行（line 851）缺少回退，VIP 始终日志 0.000。建议统一为小写格式。

### Q2: DrawExpert L0修复后的逻辑

**回答**: 等权 `[1/3, 1/3, 1/3]` 比硬编码 `[0.33, 0.34, 0.33]` **更好但非最优**：
- ✅ 消除了有意平局偏置（0.34→0.333）
- ✅ 透明降级（有日志）
- ✅ DrawExpert可用时使用真实信号
- ⚠️ 20%权重仍给中性信号，稀释XGB+LGB。更优方案：DE不可用时重分配为 XGB 0.60 + LGB 0.40

### Q3: ensemble_trainer.py恢复后import是否正常

**回答**: ✅ 核心import链验证通过：
- `from ensemble_trainer import EnsembleTrainer` — OK
- `from trap_probability_bridge import apply_trap_correction` — OK
- `from odds_inverse_calibrator import apply_goal_segment_correction` — OK
- `EnsembleTrainer` 有 `load_pipeline`, `predict_match`, `ensemble_predict_proba` 等方法
- ⚠️ 但 `from modules.feedback_loop import ...` 仍因模块遮蔽失败（P0-13未修复）

### Q4: 清理后模型文件是否有必要模型被误删

**回答**: ⚠️ 存在疑虑。两个"v4.1"文件结构不同（16 keys vs 25 keys），4.5MB版本缺少 `v41_config` 和 `nn_scaler`，10.5MB版本缺少 `calibrator_suite` 和 `optimal_thresholds`。需ML团队确认哪个是完整版本。`draw_expert_v1.joblib` 在 `saved_models/` 和 `models/draw_expert/` 各有一份（相同97KB），未误删。NN模型 `football_nn_*.pth` (758KB) 仍在。

---

## 6. 修复质量评分

| 维度 | 评分 | 说明 |
|------|------|------|
| 修复完整性 | 7/10 | 11项中8项彻底，3项部分。未提及的P0-13/P0-16仍存在 |
| 修复深度 | 6/10 | 多数修复到位但部分为表面修补（路径fallback、VIP格式、模型文件分裂） |
| 回归控制 | 6/10 | 发现3个新问题（模型文件不一致、VIP日志、隐式依赖） |
| 代码质量 | 7/10 | DrawExpert修复有日志有真实信号路径，质量较好；VIP修复格式不一致 |
| 测试验证 | 8/10 | 471/471测试通过，编码问题彻底解决 |
| **综合** | **6.5/10** | |

---

## 7. Go / No-Go 判断

### 🟡 Conditional Go

**条件**:

1. **必须修复（上线前）**:
   - P0-13 模块包遮蔽 — `from modules.feedback_loop import ...` 仍失败，影响运行时稳定性。建议将 `sys.path.insert(1, FOOTBALLAI_ROOT)` 改为按需导入或移除
   - REG-1 模型文件统一 — 确认哪个v4.1文件是完整版本，删除冗余副本，统一UnifiedPredictor和ModelBridge加载同一文件

2. **强烈建议（上线前）**:
   - P0-16 MCC=0 — 至少需要ML团队给出解释（评估bug vs 模型无效），并在文档中注明
   - VIP键名格式统一 — 将 `{'H', 'D', 'A'}` 改为 `{'home', 'draw', 'away'}`，修复日志

3. **可上线后跟进**:
   - P0-8 残留硬编码路径 — 设置环境变量可 workaround，但应彻底清理
   - REG-3 模型加载隐式依赖 — 当前路径设置覆盖，但应显式化
   - DrawExpert权重重分配 — 当前等权可接受，但有优化空间

**判断依据**: 核心预测管线（UnifiedPredictor + D-Gate）功能正常，471测试全通过，ensemble_trainer源码恢复使六层架构后端路径不再系统性降级。但模块遮蔽（P0-13）会导致运行时import失败，模型文件分裂（REG-1）会导致预测不一致，这两个问题必须在上线前解决。

---

## 附录: 验证方法

| 验证项 | 方法 |
|--------|------|
| 代码修改 | 直接读取文件确认行号和内容 |
| 导入链 | Python脚本模拟sys.path设置后执行import |
| 模块遮蔽 | `import modules; print(modules.__file__)` 确认加载路径 |
| 模型文件 | `joblib.load()` 后比较keys和类型 |
| 测试套件 | `python tests/test_v4_modules.py` 直接运行 |
| 模型注册表 | 读取JSON确认MCC值 |
| 硬编码路径 | Grep全项目搜索 `D:\AI\footballAI` 和 `FOOTBALLAI_ROOT` |

---

## 附录A: 交叉验证 — gstack-investigator 功能审计发现确认

以下5项发现由 gstack-investigator 在功能审计中发现，与本代码审查有重叠区域，经逐项验证确认：

### ✅ 发现1: ModelBridge配置路径不匹配 (新发现P1)

**验证**: 确认。`agents/model_bridge.py:24` 的 `_load_config()` 读取 `_PROJECT_ROOT/config.yaml`，但项目根目录**不存在** config.yaml（仅有 `config/config.yaml`）。Glob 搜索确认: 全项目仅 `config/config.yaml` 一个配置文件。

**影响**: `_load_config()` 的 `os.path.isfile(cfg_path)` 判定始终为 False，返回空dict。`_resolve_model_path()` 中的 `cfg.get('model', {}).get('model_path', ...)` 使用默认值 `saved_models/football_balanced_production.joblib`（v3.2名称），但 `_find_best_model()` 的兜底搜索（`saved_models/` 下找含'production'的.joblib）意外找到 `football_v4.1_production.joblib`。

**结论**: P0-18的config修复（`config/config.yaml:603` 改为v4.1）**实际上没有被ModelBridge读取**。v4.1加载是fallback机制的意外结果，非配置驱动。这是一个新发现的P1问题。

---

### ✅ 发现2: prediction_service.py三信号融合代码复活

**验证**: 确认。`backend/services/prediction_service.py:523-540` 的 `de_pdraw = model.get_de_output()` 代码仍在。`agents/model_bridge.py:280-288` 的 `get_de_output()` 方法检查 `self._trainer._last_submodel_probas` 中是否有 `draw_expert` 键。

**行为变化**: v4.1模型文件包含 `draw_expert_model` 键（两份模型文件均有），EnsembleTrainer加载后 `_last_submodel_probas` 会包含 `draw_expert` 输出。原审计认为 `get_de_output()` 恒返回None（因旧模型无draw_expert），但v4.1模型使此代码路径**从死代码变为活代码**，`de_pdraw` 现在返回实际值（如0.298）。

**风险**: 这段代码在修复前从未执行过真实逻辑，现在突然激活，可能引入未经验证的行为变化。D-specialist融合公式 `d_spec = 0.40 * d_heur + 0.30 * d_oe + 0.30 * de_pdraw` 中的 `de_pdraw` 值现在会影响预测结果。

---

### ✅ 发现3: NN模型架构脚本缺失 (新发现P1)

**验证**: 确认。`predictors/components/ensemble_trainer.py:921-922` 的 `load_nn_model()` 尝试从 `predictors/components/scripts/train_neural_net.py` 动态导入 `FootballNN` 类。Glob搜索 `**/train_neural_net*` 和 `**/*neural_net*` 全项目**零匹配**。`predictors/components/scripts/` 目录不存在。

**影响**: `load_nn_model()` 在line 923 `spec_from_file_location` 处会抛异常（文件不存在），被 line 940 的 except 捕获，返回 False。NN子模型永远不可用，L5神经网络层从未工作。`.pth` 权重文件（758KB）存在但无法加载。

**结论**: 这是预存问题，非本次修复引入。但意味着ensemble的L5层（神经网络）始终降级，stacking仅用 XGB+LGB 两路。

---

### ✅ 发现4: D-Gate第三处实现不一致 (预存P1)

**验证**: 确认。存在两处D-Gate模式B实现，使用**不同指标**：

| 位置 | 代码 | 指标 | 阈值 |
|------|------|------|------|
| `_build_analysis_card` line 600 | `spread < 0.16` | 概率差 `abs(imp_h - imp_a)` | 0.16 (概率空间) |
| `chat_endpoint` line 823 | `abs(oh_p - oa_p) < 1.6` | 赔率差（原始赔率相减） | 1.6 (赔率空间) |

`oh_p`/`oa_p` 是原始赔率（line 808: `odds.get('home',2), odds.get('away',3.5)`），不是概率。赔率差1.6 ≈ 概率差约0.10-0.15（取决于赔率水平），所以 chat_endpoint 的阈值实际上**比** _build_analysis_card **更宽松**，会触发更多平局判定。

**结论**: 预存P1不一致，非本次修复引入。但#9修复仅改了 `_build_analysis_card` 中的三处，chat_endpoint 的第四处未触及。

---

### ✅ 发现5: backend/core/config.py默认模型名仍为v3.2

**验证**: 确认。`backend/core/config.py:60`: `DEFAULT_MODEL_NAME: str = "football_balanced_production.joblib"` — 这是v3.2模型名，未更新为v4.1。

**影响**: 若有代码使用 `Settings.DEFAULT_MODEL_NAME` 构建模型路径，将指向不存在的 `saved_models/football_balanced_production.joblib`。需确认哪些代码引用此设置。

**结论**: 与发现1（ModelBridge配置路径问题）共同构成"配置不一致"风险组：config.yaml更新了但未被读取，core/config.py未更新，ModelBridge靠fallback意外工作。

---

## 附录B: 综合风险评估更新

基于交叉验证，更新后的上线前必须修复项：

| 优先级 | 问题 | 来源 | 状态 |
|--------|------|------|------|
| P0 | P0-13 模块包遮蔽 | 本审查 | ❌未修复 |
| P0 | REG-1 模型文件结构不一致 | 本审查 | 新发现 |
| P1 | ModelBridge配置路径错误 (config.yaml未被读取) | investigator发现1 | 新发现 |
| P1 | NN模型架构脚本缺失 (L5从未工作) | investigator发现3 | 预存 |
| P1 | D-Gate第四处实现不一致 (chat_endpoint) | investigator发现4 | 预存 |
| P1 | prediction_service死代码复活 (行为变化) | investigator发现2 | 修复引入 |
| P1 | P0-16 MCC=0 | 本审查 | ❌未修复 |
| P2 | VIP键名格式不一致+日志错误 | 本审查 | ⚠️部分修复 |
| P2 | core/config.py默认模型名v3.2 | investigator发现5 | 预存 |
| P2 | P0-8 硬编码路径残留 | 本审查 | ⚠️部分修复 |

**更新后判断**: 仍为 🟡 Conditional Go，但条件项从2项增至4项（新增ModelBridge配置路径+prediction_service行为变化验证）。

---

> 本报告由 gstack-product-reviewer 基于代码级验证生成。所有判断均有文件行号或实测输出支撑。附录A为与 gstack-investigator 功能审计发现的交叉验证。建议工程负责人复核 P0-13、REG-1、ModelBridge配置路径、prediction_service行为变化的修复方案。
