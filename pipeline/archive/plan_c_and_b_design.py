#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Plan C: 半场-全场赔率错配信号分析
Plan B: 数据采集方案设计
==================================

Plan C目标: 利用现有的半场赔率(HT handicap/HT OU/HT 1X2)挖掘区分信号
Plan B目标: 为后续比赛设计时间序列赔率采集方案
"""
import sys, os, json, math
from pathlib import Path
from collections import defaultdict

ARCH_ROOT = Path(r"D:/Architecture")
FAI_ROOT = Path(r"D:/Architecture")

# ═══════════════════════════════════════════════════════════════
# Plan C: 半场-全场错配信号 (基于可用数据)
# ═══════════════════════════════════════════════════════════════

HALFTIME_SIGNALS = """

## 半场赔率结构特征 (从WC图片可提取)

### 信号1: HT让球深度 vs FT让球深度
- HT handicap / FT handicap 比值
- 高值 = 庄家认为半场就决定胜负 = 屠杀信号
- 低值 = 庄家认为半场难分胜负 = 平局土壤

示例:
- 荷兰vs瑞典: FT hcp=-0.5, HT hcp≈0 或 -0.25
  → 比值低 = 半场难分 = 可能平局? (实际荷兰5-1, 屠杀)
- 荷兰vs日本: FT hcp=-0.5, HT hcp≈-0.25
  → 比值低 = 半场难分 = 实际平局

### 信号2: HT OU vs FT OU
- HT OU线通常约等于 FT OU线的一半 (如FT 2.5, HT 1.0-1.25)
- HT OU / FT OU 比值异常高 = 预期半场进球多 = 开放比赛 ≠平局
- HT OU / FT OU 比值低 = 预期半场保守 = 可能闷平

### 信号3: HT 1X2赔率结构
- 与FT 1X2对比, 如果HT draw odds < FT draw odds * 0.6
  → 庄家更看好半场平局 = 全场平局概率↑

### 信号4: HT让球水位 vs FT让球水位
- HT favorite水位 < FT favorite水位
  → 庄家对半场优势更有信心 = 屠杀信号

"""

# ═══════════════════════════════════════════════════════════════
# Plan B: 数据采集方案 (针对6.22-6.28剩余36场比赛)
# ═══════════════════════════════════════════════════════════════

DATA_COLLECTION_PLAN = """

## 采集目标
为2026世界杯剩余36场比赛(6.22-6.28)采集完整时间序列数据

## 采集时间点 (每个比赛)
1. T-8h: 赛前8小时 (开盘初赔参考)
2. T-4h: 赛前4小时 (资金进入初期)
3. T-1h: 赛前1小时 (临场最终)
4. T-0: 开赛时 (确认最终变动)

## 采集字段

### 核心1X2市场
```json
{
  "timestamp": "2026-06-22T14:00:00Z",
  "match": "乌拉圭 vs 佛得角",
  "market": "1X2",
  "home": 1.44,
  "draw": 4.25,
  "away": 6.30
}
```

### 亚盘市场
```json
{
  "market": "AH",
  "handicap": -1.25,
  "home_odds": 1.95,
  "away_odds": 1.95
}
```

### 大小球市场
```json
{
  "market": "OU",
  "line": 2.5,
  "over": 1.90,
  "under": 1.90
}
```

### 半场市场 (关键!)
```json
{
  "market": "HT_1X2",
  "home": 1.60,
  "draw": 2.35,
  "away": 5.50
}
```

### 正确比分市场 (核心创新!)
```json
{
  "market": "correct_score",
  "scores": {
    "1-0": 8.50,
    "2-0": 11.0,
    "0-0": 12.0,
    "1-1": 6.50,  // 关注! 平局比分
    "2-1": 9.00,
    "other": 4.50  // 核心: "其它"比分
  }
}
```

### 衍生信号计算 (采集后计算)
1. odds_movement: (T-1h - T-8h) / T-8h
2. draw_odds_drift: draw赔率变化率
3. other_score_odds: "其它"比分赔率水平
4. ht_ft_misalignment: 半场/全场让球比值

## 采集方式

### 方式1: 截图OCR (当前方式, 成本低)
- 手动在比赛前3个时间点截图
- OCR提取关键赔率
- 适合快速验证

### 方式2: API接入 (推荐长期)
- 寻找支持Interwetten/Sbobet的赔率API
- 自动采集时间序列
- 需要订阅成本

### 方式3: 网页爬虫 (技术方案)
- 针对公开赔率网站
- 定时爬取并存储
- 需要维护反爬

## 存储方案

### 数据库表结构
```sql
CREATE TABLE wc2026_odds_timeline (
    id INTEGER PRIMARY KEY,
    match_date TEXT,           -- 2026-06-22
    home_team TEXT,            -- 乌拉圭
    away_team TEXT,            -- 佛得角
    snapshot_label TEXT,       -- T-8h, T-4h, T-1h
    timestamp TEXT,            -- ISO8601
    ft_home REAL, ft_draw REAL, ft_away REAL,
    ft_ah_handicap REAL, ft_ah_home REAL, ft_ah_away REAL,
    ft_ou_line REAL, ft_ou_over REAL, ft_ou_under REAL,
    ht_home REAL, ht_draw REAL, ht_away REAL,
    ht_ah_handicap REAL, ht_ah_home REAL, ht_ah_away REAL,
    ht_ou_line REAL, ht_ou_over REAL, ht_ou_under REAL,
    -- 正确比分
    cs_1_0 REAL, cs_0_0 REAL, cs_1_1 REAL, cs_2_1 REAL,
    cs_other REAL,             -- "其它"比分赔率
    -- 衍生
    implied_d REAL,            -- 计算后的平局概率
    other_score_indicator REAL -- 其它比分信号
)
```

## 采集优先级 (针对剩余36场)

### P0 - 必采 (全部36场)
- 1X2赔率: 3时间点 × 3数值 = 324个数据点
- OU线: 3时间点 × 3数值 = 324个数据点

### P1 - 高价值 (重点场次)
- 亚盘: 3时间点 × 3数值
- 半场市场: 3时间点 × 6数值
- 重点: 荷兰vs日本级别的"看似平局vs实际屠杀"错配场次

### P2 - 研究用 (10-15场深度)
- 正确比分市场: 3时间点 × 10+数值
- 特别是"其它比分"赔率变化

## 预期产出

采集完成后可验证:
1. 时间序列drift信号的有效性
2. "其它比分"赔率作为平局/屠杀预测器的准确度
3. 半场-全场错配信号的真实价值
4. D-Gate v6.0的全面升级基础

"""

# ═══════════════════════════════════════════════════════════════
# 当前v5.1落地建议
# ═══════════════════════════════════════════════════════════════

V51_DEPLOYMENT = """

## D-Gate v5.1 生产部署建议

### 当前状态
- 准确率: 58.8% (vs Argmax 64.7%, -5.9pp)
- D-F1: 0.606 (从0提升)
- D召回: 91% (10/11)
- D精确: 45.5%

### 关键创新
1. Mode C反转: 超热门spread大 = 翻车信号 (×2.2)
2. S7+S1过滤: OU/HCP错配 + draw赔率便宜 → 屠杀预警 (×0.7)
3. 分层阈值: Mode C=0.14, Mode A=0.28, Default=0.32

### 部署方案

#### 方案A: 保守 (推荐生产)
- 仅启用Mode C + Mode C-away (超热门翻车检测)
- 准确率~62%, D召回~64% (7/11), 误判~8场

#### 方案B: 平衡 (当前v5.1)
- 全模式启用
- 准确率~59%, D召回~91%, 误判~12场

#### 方案C: 激进 (平局优先)
- 阈值再降10%, 追求100%平局召回
- 准确率~50%, 误判~18场

### 建议
- 当前v5.1 (方案B) 已平衡
- 等待Plan B数据采集后升级v6.0
- v6.0核心: "其它比分"赔率 + 时间序列drift

"""

print("=" * 80)
print("Plan C + Plan B 完整方案")
print("=" * 80)
print("\n" + HALFTIME_SIGNALS)
print("\n" + DATA_COLLECTION_PLAN)
print("\n" + V51_DEPLOYMENT)
