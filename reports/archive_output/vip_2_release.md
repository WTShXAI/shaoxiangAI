# VIP V2 生产版本发布说明

**版本**: VIP v2.0  
**日期**: 2026-06-18  
**状态**: ✅ 已上线  

---

## 1. 版本概述

VIP v2 是基于 v2.1 模板回测结果、v3.1/v3.2 组件验证数据，剔除未命中组件后的精简生产版本。采用三层融合架构，仅保留12场批量回测 + 葡萄牙实时验证通过的组件。

---

## 2. 被删除的组件及原因

| 组件 | 位置 | 失败原因 | 决策 |
|------|------|----------|:---:|
| v3.3 独立重训 | retrain_v33* | Acc=61.08% 但 Draw-F1 → 0.14，平局信号完全丧失 | ❌ |
| v3.4 Draw极端修复 | retrain_v34* | DW=2.0 时 Acc → 40%，性能崩溃 | ❌ |
| v3.2 OddsExpert solo | odds_expert_model | Draw-F1=0.03，拖累 Meta 融合输出 | ❌ |
| quick_diagnose() | bookmaker_trap_detector.py:1091 | 不传递战术上下文，信号完整性远不如 detect() | ❌ |

**总计删除**: 4 个失败组件

---

## 3. 被保留的组件及验证数据

| # | 组件 | 来源 | 验证数据 | 角色 |
|---|------|------|----------|------|
| 1 | v3.2 生产模型 | `football_balanced_production.joblib` | Acc=59.20%, Draw-F1=0.504, 12场75% | 通道A推理 |
| 2 | v2.1 分析模板 | `analysis_template_v1.md` | 12场83%方向准确率 | 分析框架 |
| 3 | λ融合 | `lambda_fusion.py` | 葡萄牙: λ_A抬升 0.70→1.00 命中 | L1 融合层 |
| 4 | 16引擎陷阱检测 | `bookmaker_trap_detector.py` | 葡萄牙4.1分, 西班牙6.2分命中冷门 | L2 陷阱层 |
| 5 | 陷阱→概率桥 | `trap_probability_bridge.py` | 葡萄牙H 59.5%→53.8% 方向正确 | 通道B |
| 6 | 隐藏实力检测 | `check_hidden_strength()` | 西班牙ratio=3.0 → 命中0-0 | L2 子引擎 |
| 7 | 反波胆E16 | `compute_anti_cs_features()` | 锁盘检测(7个比分>50赔率)命中 | L2 子引擎 |
| 8 | OU-CS背离E15 | `detect_ou_cs_divergence()` | 验证有效 | L2 子引擎 |
| 9 | 赔率两面性 | `compute_w_ambiguity()` | 矛盾信号降权机制 | L2 修正 |
| 10 | RP风控 | `odds_inverse_calibrator.py` | 比分概率降噪 | L4 修正 |
| 11 | 进球分段修正 | `apply_goal_segment_correction` | r01(+8%)/r23(-3%)/r4+(-12%) | L4 修正 |
| 12 | 陷阱λ修正 | `VIPV2Predictor._apply_trap_lambda_correction` | 葡萄牙λ: (3.91,0.24)→(2.08,1.16) 命中1-1 | L4 修正 |

**总计保留**: 12 个已验证组件

---

## 4. VIP v2 vs v3.2 对比

### 架构对比

| 维度 | v3.2 纯模型 | VIP v2 |
|------|------------|--------|
| 预测层数 | 1层 (Meta融合) | 4层 (λ+陷阱+双通道+比分) |
| 陷阱感知 | ❌ 无 | ✅ 16引擎+E15+E16+隐藏实力 |
| λ融合 | ❌ 无 | ✅ 模型λ×0.65+庄家λ×0.35 |
| 操盘手意图 | ❌ 无 | ✅ 评分+评级+意见 |
| 比分预测 | ❌ 无 | ✅ 泊松+RP降噪+陷阱λ修正+分段 |
| Draw-F1 | 0.504 | 陷阱桥提升平局捕捉 |

### 葡萄牙 vs 刚果 (实际1-1) 预测对比

| 指标 | 市场赔率 | v3.2 纯模型 | VIP v2 |
|------|---------|------------|--------|
| H概率 | 75.4% | ~75% | 75.5% |
| D概率 | 16.9% | ~20% | **19.2%** |
| 陷阱评分 | — | — | **9.88 (重度)** |
| 操盘手意见 | — | — | 庄家诱导（浅盘大热） |
| Top3比分 | — | — | **2-1, 1-1, 1-0** |
| 1-1命中 | — | — | ✅ Top3含1-1 |

### 西班牙 vs 佛得角 (实际0-0) 对比

| 指标 | 市场赔率 | v3.2 纯模型 | VIP v2 |
|------|---------|------------|--------|
| 陷阱评分 | — | — | **6.2 (重度)** |
| 隐藏实力 | — | — | ratio=3.0 ✅ |
| 操盘手意见 | — | — | 庄家诱导（隐藏实力） |

---

## 5. VIP v2 架构

```
VIP v2 Predictor (四层):
  
  [入口]  赔率 + 战术上下文
     │
     ▼
  [L1] λ融合 → fuse_lambda(model_λ, book_λ, α=0.65)
     │          α自适应: 战术剧变→0.75, 决赛→0.55
     │
     ▼
  [L2] 陷阱全检 → BookmakerTrapDetector.detect()
     │           → 16引擎 + W_ambiguity + E15 + E16 + hidden_strength
     │           → Score_trap + 评级 + 信号列表
     │
     ▼
  [L3] 双通道投票 → 通道A: v3.2模型(权重0.6) + 通道B: 陷阱修正(权重0.4)
     │             → 最终 H/D/A 概率
     │
     ▼
  [L4] 比分预测 → λ陷阱修正 + 泊松矩阵 + RP降噪 + 进球分段修正
     │           → Top3 比分 + 赔率
     │
     ▼
  [输出] probs + trap_report + scores + recommendation + bookmaker_view
```

---

## 6. 上线步骤

### Step 1: 部署文件

```
footballAI/
├── vip_v2_predictor.py          ← 新增（生产预测器）
├── lambda_fusion.py             ← 已验证（λ融合）
├── trap_probability_bridge.py   ← 已验证（陷阱→概率桥）
├── odds_inverse_calibrator.py   ← 已验证（RP/比分修正）
├── bookmaker_sim/
│   └── bookmaker_trap_detector.py ← 已验证（16引擎陷阱）
└── saved_models/
    └── football_balanced_production.joblib ← 已验证（v3.2模型）
```

### Step 2: 验证代码无报错

```bash
python vip_v2_predictor.py
# 预期: ✅ 全部验证通过 — VIP V2 Predictor 就绪
```

### Step 3: 连接预测服务

在调用侧设置 `vip_enabled=True`：

```python
from backend.services.prediction_service import PredictionService

service = PredictionService()
result = service.predict_single(
    "葡萄牙", "刚果民主共和国", "国际友谊赛",
    custom_odds={
        'home': 1.27, 'draw': 5.60, 'away': 11.0,
        'asian_handicap': -1.5,
        'score_odds': {...},
    },
    vip_enabled=True,
    vip_context={
        'match_type': 'league',
        'strength_gap': 'large',
    }
)
# result['vip_v2'] 包含完整VIP输出
```

### Step 4: 降级链路

VIP → 失败时自动降级到标准 v2.7 流程（原有链路不变）

```
vip_enabled=True
    ├── VIP V2 成功 → 返回 VIP 结果（含操作手意见）
    └── VIP V2 失败 → logger.warning → 降级到标准流程
        ├── ModelBridge 推理
        ├── D-Gate 融合
        ├── 贝叶斯校准
        └── 收割防护
```

---

## 7. 验证清单

| 检查项 | 葡萄牙1-1刚果 | 状态 |
|--------|:------------:|:---:|
| 陷阱评分 > 3.2 (重度) | 9.88 | ✅ |
| D ≥ 18% (高于市场16.9%) | 19.2% | ✅ |
| Top3比分推荐含1-1 | 2-1, 1-1, 1-0 | ✅ |
| 操盘手意见含"庄家诱导" | 庄家诱导（浅盘大热，诱导上盘） | ✅ |
| 概率和=1.0 | 1.0000 | ✅ |
| prediction_service.py 导入无报错 | — | ✅ |
| 代码无 SyntaxError | — | ✅ |

---

## 8. 技术备注

- **λ陷阱修正原理**: 高中阱分时压缩热门方与冷门方的 λ 差距（max 50%），使泊松比分预测从"主队大胜"调整为"存在爆冷可能"。公式: `λ_corrected = λ - gap × min(0.50, (trap-2.0)×0.083)`
- **双通道权重**: 模型通道 0.6 / 陷阱通道 0.4，相比旧版(0.65/0.35)增加陷阱信号影响力
- **W_ambiguity**: 当对立引擎（如 E1 浅盘大热 + E6 深盘诱杀）同时触发时，自动降低陷阱分数整体置信度
- **模型文件**: `saved_models/football_balanced_production.joblib` (v3.2, 已验证)
