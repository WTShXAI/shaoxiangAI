# Step 2: 数据缺失检测与补全 — 实施指南

> **状态**: ✅ 实施完成  
> **日期**: 2026-05-31

---

## 一、数据缺失诊断回顾

| 缺失维度 | 当前状态 | 严重等级 | 补全方案 |
|----------|---------|----------|----------|
| **积分榜 (standings)** | 空表 (0行) | 🔴 严重 | Football-Data.org API |
| **球队信息 (teams)** | 空表 (0行) | 🔴 严重 | Football-Data.org API |
| **表单趋势 (form_trends)** | 空表 (0行) | 🔴 严重 | 从现有 match 数据计算 |
| **赔率 (odds)** | 14条 (基本空) | 🔴 严重 | The Odds API + Football-Data.org |
| **球员伤病** | 无 | 🟡 中等 | API-Football (RapidAPI) |
| **阵容数据** | 无 | 🟡 中等 | API-Football (RapidAPI) |
| **天气数据** | 无 | 🟢 轻度 | OpenWeatherMap (后续) |
| **比赛统计** | 无 | 🟡 中等 | API-Football |

---

## 二、3个推荐API详情

### 2.1 API-Football (RapidAPI)

| 项目 | 详情 |
|------|------|
| **端点** | `https://api-football-v1.p.rapidapi.com/v3/` |
| **免费额度** | 100 req/day |
| **注册** | https://rapidapi.com/api-sports/api/api-football/ |
| **核心能力** | 球员伤病(`/players/sidelined`), 首发阵容(`/fixtures/lineups`), 球队统计(`/teams/statistics`), 赔率(`/odds`), 历史交锋(`/fixtures/headtohead`) |
| **客户端** | `data_collector/api_football_client.py` |

### 2.2 Football-Data.org (已集成)

| 项目 | 详情 |
|------|------|
| **端点** | `https://api.football-data.org/v4/` |
| **免费额度** | 10 req/min |
| **注册** | https://football-data.org/ |
| **已有客户端** | `data_collector/main.py`, `scripts/pull_historical_data.py` |

### 2.3 The Odds API

| 项目 | 详情 |
|------|------|
| **端点** | `https://api.the-odds-api.com/v4/` |
| **免费额度** | 500 req/month |
| **注册** | https://the-odds-api.com/#get-access |
| **核心能力** | 跨博彩公司赔率对比、赔率走势 |
| **客户端** | `data_collector/the_odds_client.py` |

---

## 三、新增文件清单

| 文件 | 用途 |
|------|------|
| `data_collector/the_odds_client.py` | The Odds API 客户端 — 真实市场赔率 |
| `data_collector/api_football_client.py` | API-Football 客户端 — 伤病/阵容/统计 |
| `scripts/backfill_all_data.py` | 主补全脚本 — 编排全部数据补全流程 |

---

## 四、环境配置

在 `.env` 文件中添加新的 API Key:

```bash
# 已有
FOOTBALL_DATA_API_KEY=your_football_data_key

# 新增 (可选，增强赔率数据)
THE_ODDS_API_KEY=your_the_odds_api_key

# 新增 (可选，增强伤病/阵容数据)
RAPIDAPI_KEY=your_rapidapi_key
```

---

## 五、执行补全

### 5.1 完整补全 (推荐)

```bash
cd footballAI
python scripts/backfill_all_data.py
```

这将依次执行:
1. 📊 积分榜补全 (5联赛 × 6赛季 = ~120条)
2. 👥 球队信息补全 (5联赛 = ~100支球队)
3. 📈 表单趋势计算 (~1600条)
4. 💰 赔率补全 (The Odds API)
5. 🔄 特征更新 (recalculate rank/form/h2h)

### 5.2 分步执行

```bash
# 仅积分榜
python scripts/backfill_all_data.py --standings-only

# 仅赔率
python scripts/backfill_all_data.py --odds-only

# 补全后自动训练
python scripts/backfill_all_data.py --retrain

# 预览模式 (不写入)
python scripts/backfill_all_data.py --dry-run

# 单联赛
python scripts/backfill_all_data.py --league PL --league PD
```

### 5.3 单独使用 API 客户端

```bash
# 测试 The Odds API
python data_collector/the_odds_client.py

# 测试 API-Football
python data_collector/api_football_client.py
```

---

## 六、数据合并与一致性校验

### 6.1 自动校验规则

`backfill_all_data.py` 内置以下校验:

| 校验项 | 规则 | 处理方式 |
|--------|------|----------|
| **球队名匹配** | 模糊匹配 (去FC/AC/AFC后缀) | 不一致时跳过 |
| **日期范围** | 积分榜赛季 = 比赛日期年份 | 按 match_date 提取 season |
| **主键冲突** | standings: (league_id, season, team_name) UNIQUE | INSERT OR REPLACE |
| **重复赔率** | 同一 match_id+provider 保留最新 | 按 odds_timestamp |

### 6.2 验证数据完整性

```sql
-- 检查补全后的数据状态
SELECT 'standings' as tbl, COUNT(*) FROM standings
UNION ALL
SELECT 'teams', COUNT(*) FROM teams
UNION ALL
SELECT 'form_trends', COUNT(*) FROM form_trends
UNION ALL
SELECT 'odds', COUNT(*) FROM odds;

-- 检查特征更新效果
SELECT 
    SUM(CASE WHEN rank_diff_factor != 0 THEN 1 ELSE 0 END) as rank_filled,
    SUM(CASE WHEN form_momentum != 0 THEN 1 ELSE 0 END) as form_filled,
    SUM(CASE WHEN h2h_factor != 0 THEN 1 ELSE 0 END) as h2h_filled,
    COUNT(*) as total
FROM match_features;
```

---

## 七、预期效果

| 指标 | 补全前 | 补全后 |
|------|--------|--------|
| `standings` 记录 | 0 | ~120 (5联赛×6赛季×平均20队) |
| `teams` 记录 | 0 | ~100 (5联赛×20队) |
| `form_trends` 记录 | 0 | ~3200 (800场×2队×2方向) |
| `odds` 记录 | 14 | 100+ (近期比赛实时赔率) |
| `rank_diff_factor` 填充率 | 0% | ~80%+ |
| `form_momentum` 填充率 | 0% | ~80%+ |
| `h2h_factor` 填充率 | ~0% | ~60%+ |

---

## 八、后续 Step 3 衔接

数据补全完成后，Step 3 (模型架构分析与重构) 将:
1. 分析新数据对模型性能的影响
2. 基于 19 个现有特征 + 3 个新特征提出优化建议
3. 生成 Mermaid 架构图
4. 输出《架构优化方案》

---

> **⚠️ 注意**: API-Football 和 The Odds API 需要分别注册免费 API Key。若无 Key，积分榜/球队/表单趋势仍可补全 (基于 Football-Data.org)，仅赔率和伤病数据受限。
