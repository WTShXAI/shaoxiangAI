# 哨响AI v4.0 P0修复后功能完整性审计报告

**日期**：2026-06-20
**审计人**：排障手（gstack-investigator）
**场景**：P0修复后功能验证
**方法**：代码审查 + 模型加载验证 + 三通道端到端测试 + 模块遮蔽实测

---

## 📌 TL;DR（执行摘要）

- **功能完整性评分**：6.5 / 10
- **判断**：🟡 **Conditional Go** — 核心预测管线可用，三通道架构已激活，但存在2个未解决P0和3个新发现隐患
- **已修复**：DrawExpert L0（代码硬编码）✅、L3（源码恢复）✅、三通道键名映射 ✅、VIP依赖补全 ✅、D-Gate概率尺度 ✅
- **未修复**：DrawExpert L2（模型未重训，F1=0.0）❌、模块包遮蔽（P0-13）❌
- **新发现**：ModelBridge配置路径不匹配（config.yaml未被读取）、NN模型架构脚本缺失（L5不可用）、backend/core/config.py默认模型名仍为v3.2

---

## 1. DrawExpert 四层根因验证

### L0 代码层 ✅ 已修复

**验证位置**：`predictors/unified_predictor.py:567-583`

**修复内容**：
- 原硬编码 `de_signal = np.array([0.33, 0.34, 0.33])` → 改为等权 `de_signal = np.array([1/3, 1/3, 1/3])`（line 569）
- 新增 `if self.trainer.draw_expert_model:` 条件判断（line 570）：DrawExpert可用时使用真实信号覆盖中性值
- 新增 `else:` 降级日志 `logger.debug("[SKY冷启动] DrawExpert模型未加载, 使用中性等权信号")`（line 579）
- 新增 `except` 降级日志 `logger.warning(f"[SKY冷启动] DrawExpert不可用, 使用中性等权: {de_err}")`（line 577）

**验证结论**：
- ✅ 等权 `[1/3, 1/3, 1/3]` 比硬编码 `[0.33, 0.34, 0.33]` 更合理——消除0.068恒定平局偏置
- ✅ 降级日志真正工作——DrawExpert不可用时走 `else` 分支记录debug日志，异常时记录warning日志
- ✅ DrawExpert可用时（v4.1模型已加载draw_expert_model）使用真实预测信号

### L1 配置层 ⚠️ 部分修复

**验证位置**：`config/config.yaml:603` + `agents/model_bridge.py:24-36`

**修复内容**：
- `config/config.yaml:603` 已更新：`model_path: saved_models/football_v4.1_production.joblib` ✅
- ModelBridge加载v4.1（通过fallback机制） ✅
- `prediction_service.py:523-540` 三信号融合代码 **未移除**，但 **不再是死代码**

**⚠️ 关键发现——配置路径不匹配**：
- ModelBridge的 `_load_config()` 读取 `_PROJECT_ROOT/config.yaml`（项目根目录），但实际配置文件在 `config/config.yaml`
- `D:/Architecture/config.yaml` **不存在**！
- 因此 `_load_config()` 返回空字典 `{}`，`_resolve_model_path()` 使用默认值 `saved_models/football_balanced_production.joblib`（v3.2路径）
- v3.2文件已被清理（不存在），fallback搜索机制在 `saved_models/` 中找到 `football_v4.1_production.joblib`
- **结论**：ModelBridge确实加载v4.1，但是 **靠fallback意外生效**，而非配置正确读取。如果未来有人在项目根目录放置 `config.yaml`，会静默覆盖行为

**prediction_service.py 死代码状态**：
- 三信号融合代码（line 523-540）未移除
- `get_de_output()` 现在返回非None值（0.298），因为EnsembleTrainer v4.1的 `_last_submodel_probas` 包含 `draw_expert` 键
- 代码从"死代码"变为"活代码"——行为已改变，三信号融合实际生效

### L2 模型层 ❌ 未修复（仍为P0）

**验证位置**：`saved_models/draw_expert_v1.joblib` + `saved_models/model_registry.json`

**验证结果**：
- `draw_expert_v1.joblib` 文件存在（97KB），为dict包装：`{model: LGBMClassifier, feature_names, eval_metrics, ...}`
- **F1仍然为0.0**（`eval_metrics.f1 = 0.0`，默认阈值0.5）
- `best_f1 = 0.4265`（最优阈值0.344）——模型有一定判别力但很弱
- `auc = 0.599`——仅略优于随机猜测（0.5）
- **DrawExpert未被重新训练**——模型文件与修复前相同

**影响分析**：
- L0修复缓解了影响：系统不再注入硬编码0.34偏置，改为使用DrawExpert的真实（弱）信号
- 模型确实能输出非零概率（测试值0.298），`predict_proba` 工作正常
- 但模型质量差（F1=0.0, AUC=0.599），DrawExpert信号对预测的贡献有限
- **结论：L2未完成，仍为P0问题，但严重度从"注入假信号"降级为"使用弱信号"**

### L3 架构层 ✅ 已修复（附条件）

**验证位置**：`predictors/components/ensemble_trainer.py` + import测试

**验证结果**：
- `ensemble_trainer.py` 源码已恢复：**2509行**，125KB ✅（与声称的2509行一致）
- `from ensemble_trainer import EnsembleTrainer` 导入成功 ✅
- `EnsembleTrainer.load_pipeline('saved_models/football_v4.1_production.joblib')` 加载成功 ✅
- 72维特征 ✅，模型版本4.1 ✅
- `draw_expert_model` 已加载（`<draw_expert.DrawExpert object>`）✅
- `xgb_model`、`lgb_model` 均存在 ✅
- `_last_submodel_probas` 包含6个子模型：`['lgb', 'xgb', 'heuristic', 'odds_expert', 'nn', 'draw_expert']` ✅

**⚠️ 附带问题——NN模型（L5）不可用**：
- `nn_model` 属性为None/False
- `load_nn_model()` 尝试从 `predictors/components/scripts/train_neural_net.py` 加载模型架构定义
- **`train_neural_net.py` 在整个项目中不存在**（Glob搜索0结果）
- `.pth` 文件存在（759KB）但无法加载——有权重无架构定义
- **L5神经网络层实际未工作**

**隐式导入顺序依赖**：
- `sys.path.insert(0, predictors/components)` 确保ensemble_trainer从项目内加载 ✅
- `sys.path.insert(1, FOOTBALLAI_ROOT)` 仍在——ensemble_trainer不再依赖footballAI路径，但其他modules.*导入仍依赖

---

## 2. 三通道架构验证

### SKY 通道 ✅ 已修复

**验证位置**：`predictors/sky/sky_predictor.py:176-182` + `six_layer_conversation.py:822`

**修复内容**：
- `sky_predictor.py:182`: `result['probabilities'] = result['proba_final']` —— 同时返回两种键名
- six_layer引擎 line 822: `sky_result.get("probabilities") or sky_result.get("probs", {})` —— 能正确读取

**端到端测试**（Brazil vs Argentina, 赔率2.5/3.2/2.8）：
```
SKY proba_final: {'home': 0.310, 'draw': 0.336, 'away': 0.354}
SKY probabilities: {'home': 0.310, 'draw': 0.336, 'away': 0.354}
SKY prediction: A
```
✅ 键名映射正确，概率非零，预测有效

### VIP 通道 ✅ 已修复

**验证位置**：`predictors/vip/vip_final.py:712-720` + `six_layer_conversation.py:842`

**修复内容**：
- `vip_final.py:719-720`: 同时返回 `probs` 和 `probabilities` 两种键名
- `trap_probability_bridge.py`（650B）和 `odds_inverse_calibrator.py`（50KB）已复制到 `predictors/components/` ✅
- VIP导入有三重fallback：项目路径 → footballAI路径 → stub函数（line 37-48）
- 无NameError

**端到端测试**（Brazil vs Argentina, 赔率2.5/3.2/2.8）：
```
VIP probs: {'H': 0.3719, 'D': 0.2801, 'A': 0.3479}
VIP probabilities: {'H': 0.3719, 'D': 0.2801, 'A': 0.3479}
```
✅ 键名映射正确，概率非零，无NameError

### Unified 通道 ✅ 正常工作

**验证位置**：`predictors/unified_predictor.py`

**端到端测试**（Brazil vs Argentina, 赔率2.5/3.2/2.8）：
```
Unified probabilities: {'H': 0.3956, 'D': 0.3470, 'A': 0.2574}
```
✅ 加载v4.1模型，DrawExpert信号参与冷启动融合，概率有效

### 三通道共识验证

三通道均返回有效概率，six_layer引擎能正确读取所有三个通道结果。三通道架构从"单通道空壳"恢复为"三通道共识"。

---

## 3. D-Gate 双模式验证

### 三处修改 ✅ 已修复

**验证位置**：`backend/main.py:600, 619, 660`

| 行号 | 修复前 | 修复后 | 状态 |
|------|--------|--------|------|
| 600 | `spread < 1.6` | `spread < 0.16` | ✅ |
| 619 | `spread < 1.6` | `spread < 0.16` | ✅ |
| 660 | `spread < 1.6` | `spread < 0.16` | ✅ |

**`spread` 变量定义验证**（line 305, 536）：
```python
spread = abs(imp_h - imp_a)  # 概率差，0~1范围
```
✅ `spread` 确实是概率差（0~1范围），`< 0.16` 阈值正确

**模式触发条件验证**：
- **模式A**（line 596-598）：`0.50 < max_imp <= 0.70 and ou_line <= 2.5` —— 中等热门翻车检测 ✅
- **模式B**（line 600-604）：`spread < 0.16 and 3.0 <= od <= 4.5 and ou_line <= 2.5` —— 均衡赛事平局检测 ✅
- 模式B不再无条件触发——需要同时满足概率差<0.16 + 平赔区间 + 低大小球 ✅

### ⚠️ 第三处D-Gate实现不一致（P1，预存问题）

**验证位置**：`backend/main.py:823`（chat_endpoint函数）

```python
abs(oh_p - oa_p) < 1.6  # oh_p/oa_p 是赔率值，非概率
```

此处使用 **赔率差**（非概率差），阈值1.6对赔率差是合理的。但与 `_build_analysis_card` 的概率差逻辑不一致。这是预存的P1问题（#26 D-Gate三处实现不一致），非本次修复引入。

### ⚠️ 验证脚本残留旧值

`verification/predict_verifier.py:161` 仍有 `spread < 1.6`——这是验证脚本非生产代码，不影响运行时。

---

## 4. 模块包遮蔽问题（P0-13）❌ 仍存在

**验证方法**：实测 `sys.path` 插入后 `import modules` 的加载来源

**验证结果**：
```python
sys.path.insert(0, 'D:/Architecture/predictors/components')
sys.path.insert(1, 'D:/AI/footballAI')
import modules
# 结果: modules loaded from D:\AI/footballAI\modules\__init__.py  ← footballAI的modules包！
#       NOT from D:\Architecture\modules\__init__.py
```

**遮蔽确认**：
- footballAI `modules/__init__.py` 存在 ✅
- v4.0 `modules/__init__.py` 存在 ✅
- `sys.path.insert(1, FOOTBALLAI_ROOT)` 使footballAI的modules包优先加载 ❌

**受影响文件**（仍执行 `sys.path.insert(1, FOOTBALLAI_ROOT)`）：
| 文件 | 行号 | 状态 |
|------|------|------|
| `predictors/unified_predictor.py` | 53 | ❌ 仍存在 |
| `predictors/sky/sky_predictor.py` | 27 | ❌ 仍存在 |
| `predictors/vip/vip_final.py` | 30 | ❌ 仍存在 |

**缓解措施**（已实施但未根治）：
- `sys.path.insert(0, predictors/components)` 确保 `ensemble_trainer` 从项目内加载
- `FOOTBALLAI_ROOT` 改为 `os.environ.get('FOOTBALLAI_ROOT', ...)` 支持环境变量覆盖
- 但硬编码默认值 `r"D:\AI\footballAI"` 仍在

**结论**：模块遮蔽问题 **未解决**。`from modules.xxx import yyy` 仍可能加载footballAI的modules包而非v4.0的。推荐方案（pip install -e .）未实施。

---

## 5. 模型版本一致性验证

### ModelBridge vs UnifiedPredictor

| 路径 | 加载模型 | 机制 | 状态 |
|------|---------|------|------|
| UnifiedPredictor | `football_v4.1_production.joblib` | 候选路径搜索，项目内优先 | ✅ 正确 |
| ModelBridge | `football_v4.1_production.joblib` | config读取失败→默认v3.2路径→文件不存在→fallback搜索 | ⚠️ 意外生效 |

**⚠️ ModelBridge配置路径问题**：
- `_load_config()` 读取 `_PROJECT_ROOT/config.yaml` —— 该文件不存在
- 实际配置在 `config/config.yaml` —— 未被ModelBridge读取
- ModelBridge通过fallback机制加载v4.1（v3.2文件已删除，fallback找到v4.1）
- 两条路径最终都加载v4.1，但ModelBridge的路径是 **脆弱的意外生效**

### model_registry.json 指标

| 指标 | 值 | 状态 |
|------|-----|------|
| version | 4.1 | ✅ |
| accuracy | 0.6207 | — |
| auc | 0.8068 | — |
| **mcc** | **0** | ❌ **仍为0（=随机猜测），未核实** |
| draw_f1 | 0.4913 | —（主模型draw F1，非DrawExpert） |

**MCC=0 仍未解决**（P0-16），需ML团队核实。

### backend/core/config.py 默认模型名

`backend/core/config.py:60`: `DEFAULT_MODEL_NAME: str = "football_balanced_production.joblib"` —— 仍为v3.2名称，未更新为v4.1。虽然当前不影响运行（ModelBridge不直接使用此值），但为潜在隐患。

---

## 6. 清理后模型文件完整性验证

**saved_models/ 目录**：12MB，10个文件

| 文件 | 大小 | 必要性 | 状态 |
|------|------|--------|------|
| `football_v4.1_production.joblib` | 4.5MB | 主模型 | ✅ 存在 |
| `draw_expert_v1.joblib` | 97KB | DrawExpert | ✅ 存在 |
| `football_nn_20260616_125617.pth` | 759KB | NN模型 | ⚠️ 存在但无法加载（缺架构脚本） |
| `multi_ah_handicap_20260618_195326.joblib` | 4.2MB | 亚盘模型 | ✅ 存在 |
| `multi_goals_total_20260618_195328.joblib` | 1.6MB | 进球模型 | ✅ 存在 |
| `multi_ou_totals_20260618_195327.joblib` | 527KB | 大小球模型 | ✅ 存在 |
| `draw_expert_oof.npy` | 69KB | DE训练数据 | ✅ 存在 |
| `draw_expert_oof_indices.npy` | 69KB | DE训练数据 | ✅ 存在 |
| `draw_expert_scaler.joblib` | 3.7KB | DE缩放器 | ✅ 存在 |
| `model_registry.json` | 2.2KB | 注册表 | ✅ 存在 |

**结论**：必要模型文件齐全 ✅。用户声称"6个模型文件/10.5MB"，实际10个文件/12MB（含支撑文件），基本一致。

---

## 7. 新发现问题

### 🔴 NEW-1: NN模型架构脚本缺失（L5不可用）

- `train_neural_net.py` 在整个项目中不存在（Glob搜索0结果）
- `ensemble_trainer.py:922` 依赖此文件加载NN模型架构
- `.pth` 权重文件存在但无法加载——有权重无架构
- **影响**：L5神经网络层从未工作，`_last_submodel_probas['nn']` 可能为None
- **严重度**：P1（系统可降级运行，但v4.0声称的六层架构实际只有五层）

### 🟠 NEW-2: ModelBridge配置路径不匹配

- ModelBridge读取 `_PROJECT_ROOT/config.yaml`，实际配置在 `config/config.yaml`
- config修复（L1）未被ModelBridge实际读取
- v4.1加载靠fallback意外生效
- **影响**：配置修改不生效，未来可能静默断裂
- **严重度**：P1

### 🟡 NEW-3: backend/core/config.py默认模型名过时

- `DEFAULT_MODEL_NAME` 仍为 `football_balanced_production.joblib`（v3.2）
- 虽当前不影响运行，但为潜在隐患
- **严重度**：P2

---

## 8. 功能完整性评分矩阵

| # | 验证项 | 状态 | 评分 | 说明 |
|---|--------|------|------|------|
| 1 | DrawExpert L0 代码层 | ✅ 已修复 | 10/10 | 硬编码→等权+降级日志 |
| 2 | DrawExpert L1 配置层 | ⚠️ 部分修复 | 5/10 | config已更新但ModelBridge未读取，靠fallback |
| 3 | DrawExpert L2 模型层 | ❌ 未修复 | 2/10 | F1=0.0，模型未重训，但有弱信号(AUC=0.599) |
| 4 | DrawExpert L3 架构层 | ✅ 已修复 | 8/10 | 源码恢复+import正常，但NN模型不可用 |
| 5 | SKY 通道 | ✅ 已修复 | 10/10 | 键名映射+端到端验证通过 |
| 6 | VIP 通道 | ✅ 已修复 | 10/10 | 键名映射+依赖补全+端到端验证通过 |
| 7 | Unified 通道 | ✅ 正常 | 10/10 | v4.1模型加载+预测有效 |
| 8 | D-Gate 双模式 | ✅ 已修复 | 8/10 | 三处修复正确，但第三处实现不一致 |
| 9 | 模块包遮蔽 | ❌ 仍存在 | 2/10 | sys.path操作未消除，遮蔽实测确认 |
| 10 | 模型版本一致性 | ⚠️ 部分 | 5/10 | 两条路径都加载v4.1，但ModelBridge靠fallback |
| 11 | 模型文件完整性 | ✅ 完整 | 9/10 | 必要模型齐全，NN架构脚本缺失 |

**加权综合评分**：**6.5 / 10**

---

## 9. Go / Conditional Go / No-Go 判断

### 🟡 Conditional Go

**理由**：

**可以上线的条件**（已满足）：
1. ✅ 三通道架构已激活——SKY、VIP、Unified三通道端到端验证通过
2. ✅ 核心预测管线可用——UnifiedPredictor + D-Gate产出有效预测
3. ✅ DrawExpert L0修复——不再注入硬编码假信号，改为透明降级
4. ✅ D-Gate概率尺度修复——模式B不再无条件触发
5. ✅ ensemble_trainer源码恢复——import和模型加载正常

**必须在上线前解决的条件**（未满足）：
1. ❌ **DrawExpert L2模型未重训**——F1=0.0（默认阈值），AUC=0.599（接近随机）。L0修复将影响从"注入假信号"降级为"使用弱信号"，但模型质量仍然差。**建议**：至少使用 `best_threshold=0.344` 替代默认阈值0.5，使best_f1=0.4265生效
2. ❌ **模块包遮蔽仍存在**——`from modules.xxx` 可能加载footballAI的模块。**缓解**：设置 `FOOTBALLAI_ROOT` 环境变量指向项目自身

**建议上线后立即处理**：
1. 🔴 修复ModelBridge配置路径（`config.yaml` → `config/config.yaml`）
2. 🔴 补全 `train_neural_net.py` 或修改 `load_nn_model()` 使用内联架构定义
3. 🟠 核实MCC=0根因（model_registry.json）
4. 🟠 更新 `backend/core/config.py` 默认模型名
5. 🟡 统一D-Gate三处实现

---

## 10. 特别标注

### ⚠️ DrawExpert L2（模型重训）—— 未完成，仍为P0

- `draw_expert_v1.joblib` 未重新训练
- F1=0.0（默认阈值0.5），AUC=0.599（接近随机）
- 模型有 `best_f1=0.4265`（阈值0.344）但代码未使用最优阈值
- **影响**：DrawExpert信号对预测贡献有限，但L0修复确保系统透明降级而非注入假信号
- **建议**：重训DrawExpert使F1>0，或至少在代码中使用 `best_threshold` 替代默认阈值

### ⚠️ 模块遮蔽问题 —— 仍存在

- 三个生产预测器仍执行 `sys.path.insert(1, FOOTBALLAI_ROOT)`
- 实测确认 `import modules` 加载footballAI的modules包
- 推荐方案（pip install -e .）未实施
- **缓解**：设置环境变量 `FOOTBALLAI_ROOT=D:\Architecture` 可临时消除遮蔽

---

> 本报告由排障手（gstack-investigator）通过代码审查+模型加载验证+三通道端到端测试+模块遮蔽实测生成。所有结论基于实际工具输出，非推测。
