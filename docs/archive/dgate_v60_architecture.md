"""
D-Gate v6.0 架构设计
===================
基于 v5.1 成果 + Plan B 数据采集

核心升级: "其它比分"赔率信号 + 时间序列drift
目标: D-F1 > 0.65, 准确率 > 62%, 误判 < 8场 (34场回测)
"""

V60_ARCHITECTURE = """

## 🎯 v6.0 核心升级

### 升级1: "其它比分"赔率信号 (Other Score Indicator)

逻辑:
  赛前"其它比分"赔率是庄家对所有非常规比分的综合定价
  - 高赔(>7.0): 市场认为结果高度不确定 → 平局土壤 ↑
  - 中赔(5.0-7.0): 市场有一定预期 → 常规比赛
  - 低赔(<5.0): 市场锁定方向 → 屠杀信号

实现:
```python
def other_score_signal(cs_other_odds):
    if cs_other_odds > 7.0:
        return 0.15  # draw boost
    elif cs_other_odds > 5.0:
        return 0.05
    else:
        return -0.08  # draw penalty
```

预期效果:
  荷兰vs日本(D): cs_other ~7-8 (高, draw signal)
  荷兰vs瑞典(H): cs_other ~4-5 (低, blowout signal)
  → 区分度: 目前v5.1完全无法区分的案例!

### 升级2: 时间序列赔率漂移 (Odds Drift)

采集: 赛前8h/4h/1h/开赛 四个时间点的1X2赔率

信号:
  S_drift: draw odds变化率 = (T-1h - T-8h) / T-8h
  - 如果draw赔率在赛前显著下降(S_drift < -0.05): 庄家收draw → draw signal
  - 如果draw赔率在赛前上升(S_drift > +0.05): 庄家推draw → anti-draw

  direction: 赔率变动方向
  - 如果H赔率下降 + D赔率上升 → 市场对H更有信心 → 屠杀signal
  - 如果H赔率持平 + D赔率下降 → 市场不确定 → draw signal

实现:
```python
def drift_signal(h_drift, d_drift):
    score = 0
    if d_drift < -0.03: score += 0.10
    if d_drift > 0.03:  score -= 0.05
    if h_drift > 0.02:  score += 0.05  # favorite odds rising = uncertainty
    return score
```

### 升级3: v6.0判型流程

```
输入: 赔率(1X2+AH+OU+HT+波胆) + 时间序列
  │
  ├→ [新] OtherScore信号: cs_other赔率
  │    └→ 高(>7.0) +0.15 / 中(5-7) +0.05 / 低(<5) -0.08
  │
  ├→ [新] Drift信号: draw赔率漂移
  │    └→ 下降(<-0.03) +0.10 / 上升(>0.03) -0.05
  │
  ├→ [v5.1] Mode C: max_imp≥70% → spread反转 ×2.2
  ├→ [v5.1] Mode C-away: pa>65% → ×2.0
  ├→ [v5.1] Mode A: 48-70% → S7+S1过滤
  ├→ [v5.1] Mode B: spread<0.15 → 高门槛
  └→ [v5.1] Default
  │
  输出: verdict + confidence
```

## 📊 预期效果 (基于信号理论)

| 指标 | v5.1 | v6.0 (预期) | 改善 |
|------|------|-------------|------|
| 准确率 | 58.8% | 62%+ | +3pp |
| D-F1 | 0.606 | 0.65+ | +0.05 |
| D召回 | 91% (10/11) | 91%+ | 持平 |
| D精确 | 45.5% | 50%+ | +5pp |
| 误判 | 12 | <8 | -4+ |
| 荷兰区分 | ❌ | ✅ | 核心突破 |

## 🗂️ 文件结构

```
rules/
├── d_gate_engine.py        # ← 已升级到 v5.1
└── d_gate_v60.py           # ← v6.0 新增 (含OtherScore+Drift)

pipeline/
├── wc_collector.py         # ← 数据采集器 (Plan B)
├── dgate_v50_backtest.py   # ← v5.0回测
└── wc2026_v60_backtest.py  # ← v6.0回测 (待数据采集完成后)

data/
└── wc2026_timeline.db      # ← 时间序列数据库 (已初始化)
```

## 🔄 实施路线

Phase 1: 数据采集 (6.22-6.28)
  - 每场比赛前: 截图T-8h/T-4h/T-1h
  - 手动OCR填入insert_snapshot()
  - 重点: cs_other字段

Phase 2: 信号验证 (6.29-7.01)
  - 30+场数据回测v6.0
  - 调优OtherScore和Drift权重
  - AB对比 v5.1 vs v6.0

Phase 3: 生产部署 (7月初)
  - d_gate_v60.py 写入生产
  - 更新 tournament_rules.json
"""
