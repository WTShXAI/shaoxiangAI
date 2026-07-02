# Phase 0 止血策略设计 v1.0

> 赵统筹 · 2026-07-02 · 基于孙策/何执策/费深谋/钱代驾四位专家评审

---

## 策略总纲

**核心原则**：先止血（让系统诚实运转），再验证（让数字可信），后增强（让模型进化）。

Phase 0 做三件事：
1. **修** — P0致命bug（预测链路断点）
2. **建** — 数据采集管线（一切验证的基础）
3. **验** — 让回测吐真实数字（当前全是随机标签）

---

## 任务清单（5项，按执行顺序）

### T1：数据采集入库（新增P0）

**理由**：三位专家（孙策/何执策/费深谋）一致判定为生命线。当前 FootballDataCollector 只读不写——API拉取后存入内存，过期即丢。没有入库就没有回测，没有回测就没有准确率数字，Phase 1/2 的所有"优化提升"都无法验证。

**位置**：
- `data_collector/main.py` — FootballDataCollector
- `database/init_db.py` — 已有采集→入库调用链

**当前状态**（孙策/钱代驾确认）：
- FootballDataCollector 只做 API 拉取，返回 Python dict，**不写入数据库**
- sportsapi_wc2026.py 存 JSON 文件，不入库
- odds_fetcher.py 用硬编码字典，只写文件缓存

**设计**：
```python
# FootballDataCollector 新增方法（不改现有接口）
def sync_to_database(self, league_code='WC', season=2026):
    """采集→入库一键同步，保留现有API客户端逻辑不变"""
    matches = self.get_matches(league_code, season)
    for m in matches:
        db.add_match(m)  # 已有init_db.py的入库逻辑
    return len(matches)

# odds_fetcher 新增DB writer
def save_odds_to_db(self, match_id, odds_data):
    """赔率快照写入 odds 表"""
    ...

# CLI入口
# python main.py collect --league WC --season 2026
```

**验收标准**：
- `python main.py collect --league WC` 执行成功，DB中 matches 表新增记录
- odds_snapshots 表有赔率时间序列
- 增量更新：二次执行不重复入库（按 match_id 去重）

**预估**: 2-3个文件，~100行，1天

---

### T2：三信号融合修复（P0，Phase 0第一天）

**理由**：何执策量化——修好后淘汰赛 D-F1 从 0.28 拉到 0.38-0.40（**+0.10-0.12**），是淘汰赛预测最大的单点提升。当前 DrawExpert 恒返回 None，三信号路径永远走不到，D-Gate 退化为双信号或纯 Heuristic 单信号。

**位置**：
- `prediction_service.py:523-530` — 三信号融合分支
- `model_bridge.py` — `get_de_output()` 从 `_last_submodel_probas['draw_expert']` 读取
- `drawgate_v53.py` — **已有可工作的 DrawGate 模块**，只是没有被三信号路径消费

**设计（方案A — 何执策推荐）**：
```python
# 不依赖完整 DrawExpert 模型（不在仓库）
# 直接使用 drawgate_v53 的 draw_boost 输出作为 DrawExpert 信号源

# prediction_service.py:529
from rules.drawgate_v53 import DrawGateV53

dg = DrawGateV53(match_data)
draw_expert_prob = dg.get_draw_probability()  # 返回 P(D)

# 三信号融合，DrawExpert 权重 0.20（临时），待重训后调整为 0.33
d_spec = 0.50 * d_heur + 0.30 * d_oe + 0.20 * draw_expert_prob
```

**为什么权重 0.20 不是 0.33**：
- drawgate_v53 不是完整 DrawExpert 模型，准确率约 55-58%
- 待 Phase 2 DrawExpert 重训后（用真实数据），调为 0.33

**验收标准**：
- `draw_expert_prob` 不再恒为 None
- 三信号融合分支被实际执行（加日志验证）
- 窄 spread 比赛（proba_spread<0.15）Draw-F1 > 0.35

**预估**: 1-2个文件，~50行，半天

---

### T3：SKY通道赔率接入（P0）

**理由**：钱代驾确认——当前 `sky_predictor.py:_build_features()` 在无赔率时用默认值 2.5/3.2/2.8 填充所有特征向量。这意味着对"巴西vs日本"和"日本vs巴西"可能产生相同的特征。修好后 SKY 通道 Acc 从 ~48% 跳到 ~55%（何执策估算）。

**位置**：
- `pipeline/predictors/sky/sky_predictor.py:_build_features()` — 赔率fallback
- `data_collector/odds_fetcher.py` — 当前用硬编码 KNOWN_ODDS 字典

**设计**：
```python
# sky_predictor.py: _build_features()
# 修改：将 odds_fetcher 的硬编码字典 → 从 DB/API 实时读取

def _build_features(self, match_data):
    odds = match_data.get('odds') or self._fetch_live_odds(match_data)
    if odds:
        features['odds_h'] = odds['home']
        features['odds_d'] = odds['draw']
        features['odds_a'] = odds['away']
    else:
        # 真·无赔率→降级为均匀概率，不再伪造
        features['odds_h'] = features['odds_d'] = features['odds_a'] = 2.5
        logger.warning(f"无赔率数据: {match_data['home']} vs {match_data['away']}，使用均匀降级")
```

**关键**：T1（数据采集入库）必须先行——SKY 通道从 DB 读赔率，不再依赖硬编码字典。

**验收标准**：
- 两场不同赔率的比赛 → SKY 输出不同
- 赔率变化时 SKY 输出变化
- 无赔率时降级日志可追踪

**预估**: 1个文件，~30行，半天

---

### T4：walkforward_backtest 去随机化（P0）

**理由**：何执策的💣级发现——`walkforward_backtest.py:1302` 用 `np.random.choice` 生成随机标签。当前所有"回测准确率"数字都不可信——造了虚假信心。比任何 P0 bug 都危险。Phase 0 必须修复，否则 Phase 1/2 的所有优化都测不出来。

**位置**：
- `optimize/walkforward_backtest.py:1302` — 随机标签
- `optimize/league_evaluator.py:1428` — 随机标签
- `optimize/class_weight_optimizer.py:578` — 随机标签

**设计**：
```python
# 修改前 (walkforward_backtest.py:1302)
y_true = np.random.choice([0, 1, 2], size=len(y_pred))  # ❌ 随机标签

# 修改后
y_true = self._load_true_labels(match_ids)  # 从 matches 表读取 H/D/A
# matches表已有 label 字段（H/D/A → 0/1/2）
```

**验收标准**：
- 两次运行同一回测 → 输出一致（可复现）
- 回测准确率基于真实比赛结果，不是随机数
- 运行 `python scripts/backtest_all_models.py` 输出真实准确率数字

**预估**: 3个文件，~50行，半天

---

### T5：_FakeArray 清理（P2→提至Phase 0）

**理由**：费深谋确认——9个文件各有完全相同的 _FakeArray 类定义，numpy 已是 requirements.txt 硬依赖，所有生产路径都走 `_HAS_NUMPY=True`，_FakeArray 从未被实际使用。钱代驾估算：9文件删108行，零功能影响。趁 Phase 0 清理，不给后面留债。

**位置**（9个文件，全在 `pipeline/predictors/`）：
```
cli.py, data_classes.py, dgate_layer.py, helpers.py,
live_movement.py, model_layer.py, ou_linkage.py, pipeline.py, taoge_strategy.py
```

**设计**：
```python
# 修改前（每个文件各有一份）
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None
    _HAS_NUMPY = False
    class _FakeArray: ...

# 修改后（统一单文件）
# pipeline/predictors/_compat.py:
import numpy as np

# 其他9个文件：
from ._compat import np
```

**或者在9个文件中直接**：
```python
import numpy as np  # requirements.txt 已声明依赖
```

**验收标准**：
- 9个文件中无 _FakeArray 类定义
- `python pipeline/predictors/pipeline.py` 正常运行
- grep `_FakeArray` 全仓库 = 0

**预估**: 9个文件，删108行，1小时

---

## 执行顺序与依赖

```
                 ┌─────────────────┐
                 │ T1: 数据采集入库 │ ← 必须先做，T3 依赖它
                 └───────┬─────────┘
                         │
         ┌───────────────┼───────────────┐
         │               │               │
         ▼               ▼               ▼
┌────────────────┐ ┌──────────┐ ┌──────────────────┐
│ T2: 三信号融合  │ │T3: SKY   │ │ T4: walkforward  │
│ (独立,可并行)   │ │(依赖T1)  │ │ (独立,可并行)     │
└────────────────┘ └──────────┘ └──────────────────┘
         │               │               │
         └───────────────┼───────────────┘
                         │
                         ▼
                 ┌─────────────────┐
                 │ T5: _FakeArray  │ ← 最后做，改动面最广
                 │ 清理 (9文件)     │
                 └─────────────────┘
```

**并行策略**：T2 和 T4 与 T1 并行启动。T3 等 T1 完成后做。T5 在全部完成后统一清理。

---

## 不包含在 Phase 0 的内容（及理由）

| 任务 | 理由 |
|------|------|
| 键名统一 (1.2) | 费深谋: SKY/VIP已有probabilities兼容输出，零改动 |
| 模型API (1.4) | 孙策: 9个endpoint已完整实现 |
| 删除废弃接口 (1.5) | 钱代驾: 0引用，ABC抽象类非死代码 |
| SECRET_KEY (1.6) | 孙策: .env已随机化，仅start.bat遗留2行 |
| 演示模式隔离 | 低ROI，不影响功能 |
| S级信号 | 费深谋: 缺多数据源，Phase 3再做 |

---

## 交付物

| 文件 | 改动 | 预估工时 |
|------|------|---------|
| `data_collector/main.py` | +sync_to_database() | 3h |
| `data_collector/odds_fetcher.py` | +save_odds_to_db() | 2h |
| `pipeline/service/prediction_service.py` | 三信号融合drawgate接入 | 2h |
| `pipeline/predictors/sky/sky_predictor.py` | 赔率fallback替换 | 1h |
| `optimize/walkforward_backtest.py` | 随机标签→真实标签 | 1h |
| `optimize/league_evaluator.py` | 随机标签→真实标签 | 0.5h |
| `optimize/class_weight_optimizer.py` | 随机标签→真实标签 | 0.5h |
| `pipeline/predictors/_compat.py` | 新建，统一numpy导入 | 0.25h |
| `pipeline/predictors/*.py` (9文件) | 删_FakeArray | 1h |
| **合计** | | **~11h ≈ 1.5天** |

---

## 回归验证计划（赵统筹 post-CodeBuddy）

CodeBuddy 落地代码后，哨响AI做以下验证：

1. **端到端预测验证**：`python main.py predict --home 巴西 --away 阿根廷` → 输出完整概率
2. **数据管线验证**：`python main.py collect --league WC` → DB中 matches 表新增记录
3. **三信号融合验证**：窄spread比赛日志中 draw_expert_prob ≠ None
4. **SKY通道验证**：同一场比赛不同赔率输入 → SKY输出不同
5. **回测真实性验证**：两次运行回测 → 输出完全一致
6. **_FakeArray清零验证**：grep 全仓库 _FakeArray = 0
7. **回归测试**：`python scripts/full_self_check.py` 全部通过

---

> **给 CodeBuddy 的执行指令**：按 T1→T2+T3+T4→T5 的顺序执行。T2/T4 可与 T1 并行。每个任务完成后 commit 一条。完成后通知赵统筹做回归验证。
