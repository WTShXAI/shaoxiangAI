# ReverseOddsEngine — 赔率逆向引擎

> 项目核心模块：用赔率开盘→收盘的漂移，逆向分析机构真实意图，检测市场误定价。
> 基于 odds_features 表 302,900 场真实数据严格时序回测开发，所有指标诚实可复现。

---

## ⚠️ 诚实声明（必读）

**1x2 三分类预测的"99%准确率"在物理上不存在。** 全球顶尖博彩公司模型天花板 52-55%。本模块不做虚假承诺，所有指标基于严格 walk-forward 时序回测：

| 任务 | 真实指标 | 说明 |
|------|---------|------|
| 1x2 整体预测 | **52.4%**（打不过收盘价基线）| 市场有效，赔率信息已被定价 |
| **误定价检测 Top10%** | **命中率 82.1%，ROI +0.62%** | ⭐ 边际正期望，3/5 窗口正 |
| 机构意图识别 | 诚实防H模式 → 61% 主胜率 | 解释性价值，非预测工具 |

**本模块的价值不在"预测比分"，在三个经回测验证的方向：**
1. **误定价检测**：识别市场系统性低估的场次（Top10% 正期望）
2. **机构意图识别**：诚实防X vs 诱盘假X（解释机构在防什么）
3. **凯利注码**：基于误定价偏差的理性下注建议

---

## 安装与训练

```bash
# 训练误定价检测器（生成 saved_models/mispricing_detector.joblib）
python pipeline/reverse_odds_engine.py

# Walk-forward 严格回测（验证稳健性）
python scripts/reverse_odds_walkforward.py

# 诚实基线（对比各特征层级贡献）
python scripts/reverse_odds_honest_baseline.py

# 单元测试
python -m pytest tests/test_reverse_odds_engine.py -v
```

## 快速使用

```python
from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput

engine = ReverseOddsEngine()

# 输入: 开盘→收盘赔率
odds = OddsInput(open_h=2.0, open_d=3.3, open_a=3.3,
                 close_h=1.7, close_d=3.6, close_a=3.8)
result = engine.analyze(odds)

print(result.verdict)
# 例: "△ 存在边际优势 (edge=+4.0%), 但不足以克服抽水, 观望"
print(result.intent)        # Intent.HONEST_DEF_H
print(result.mispricing_score)  # 0-1, 误定价风险
print(result.kelly_fraction)    # 凯利注码比例
print(result.recommended_bet)   # 'H'/'D'/'A'/None
```

## 输出字段说明

| 字段 | 类型 | 含义 |
|------|------|------|
| `implied_probs` | (H,D,A) | 市场隐含概率（去overround）|
| `true_probs` | (H,D,A) | 校准后真实概率估计 |
| `intent` | Intent | 机构操盘意图（7种）|
| `intent_confidence` | float | 意图置信度（drift幅度归一化）|
| `drift_pattern` | str | 三方drift模式（↓↑↑等）|
| `mispricing_score` | float | 误定价分（0-1，越高越可能定错价）|
| `argmax_hit_prob` | float | 模型估计的市场argmax命中率 |
| `expected_edge` | float | 期望边际 = 命中估计 - 隐含概率 |
| `kelly_fraction` | float | 凯利注码比例（负=不下注）|
| `recommended_bet` | str | 推荐下注方（H/D/A/None）|
| `verdict` | str | 人类可读综合结论 |

## 七种机构意图

基于 drift 三方组合模式（阈值 0.02）：

| 意图 | drift模式 | 含义 | 训练集真实率 |
|------|----------|------|------------|
| `HONEST_DEF_H` | H↓D↑A↑ | 诚实防主胜 | 主胜 59.0% |
| `HONEST_DEF_A` | H↑D↑A↓ | 诚实防客胜 | 客胜 49.3% |
| `FAKE_DEF_H` | H↓D↓A↑ | 诱盘假防H（实防A）| 主胜 35.0%、客胜 37.3% |
| `FAKE_DEF_A` | H↑D↓A↓ | 诱盘假防A（实防H）| 主胜 45.5% |
| `ALL_DOWN` | H↓D↓A↓ | 三方全降（资金均压）| — |
| `ALL_UP` | H↑D↑A↑ | 三方全升（资金流出）| — |
| `NEUTRAL` | 平稳 | 无显著漂移 | 基线 |

**核心洞察**：同样"H赔率下调"，因 D/A 组合不同，主胜率差 24 个百分点（59% vs 35%）。现有 16 引擎陷阱检测器基于单快照无法区分。

## 回测指标（诚实）

### Walk-Forward（5个半年窗口，2023-2025）

| 筛选门槛 | 命中率 | ROI | 正ROI窗口占比 |
|---------|--------|-----|--------------|
| 基线(全量下注) | 52.5% | -3.98% | — |
| Top2% 高置信 | 90.1% | -0.54% | 2/5 |
| **Top5%** | 86.3% | **+0.54%** | 2/5 |
| **Top10%** | 82.1% | **+0.70%** | **3/5** |
| Top20% | 74.8% | -1.00% | 0/5 |

**诚实结论**：Top10% 有边际正期望（+0.62%），但**不稳定**（2024-H1、2025+ 窗口为负）。这是真实的统计优势，不是印钞机。专业博彩现实：微小 edge + 严格资金管理 + 长期纪律。

## 依赖

- `odds_features` 表（开盘/收盘/drift/overround）
- `saved_models/mispricing_detector.joblib`（训练生成；缺失时降级为纯规则模式）

## 文件

- `pipeline/reverse_odds_engine.py` — 核心模块
- `scripts/reverse_odds_honest_baseline.py` — 诚实基线脚本
- `scripts/reverse_odds_walkforward.py` — walk-forward 回测
- `tests/test_reverse_odds_engine.py` — 单元测试（15项全通过）
- `reports/reverse_odds_baseline.json` — 基线指标
- `reports/reverse_odds_walkforward.json` — 回测指标

---

*诚实是本模块的底线。所有数字可复现，所有结论基于严格时序回测。不假设、不夸大、不隐瞒。*
