# 哨响AI 模型卡 (Model Card)

## Model Details

| 项目 | 内容 |
|------|------|
| **模型名称** | football_balanced_production |
| **版本** | 3.0 |
| **格式** | EnsembleTrainer 标准格式 (.joblib) |
| **文件** | `saved_models/football_balanced_production.joblib` (12MB) |
| **创建日期** | 2026-06-10 |
| **框架** | XGBoost 3.2+ / Ridge Regression / scikit-learn 1.7+ |
| **架构** | 双模型加权集成 (XGBoost + Ridge, Soft Voting) |

## Intended Use

### 适用场景
- 男子职业足球比赛的赛前胜负预测（主胜/平局/客胜）
- 覆盖全球 30+ 联赛，包括英超、西甲、德甲、意甲、法甲等主流联赛
- 作为决策参考工具，辅助分析比赛走向

### 不适用场景
- ❌ 不适用于女子足球（训练数据未覆盖）
- ❌ 不适用于友谊赛/全明星赛等非正式比赛
- ❌ 不应用于非法赌博或任何违法活动
- ❌ 预测结果不构成投注建议

## Training Data

| 项目 | 内容 |
|------|------|
| **数据来源** | api-football.com + 赔率数据 |
| **训练样本** | ~33,000 场比赛 |
| **时间范围** | 2015-2025 赛季 |
| **特征数** | 90+ 个特征 |
| **目标变量** | 比赛结果 (H/D/A) |
| **类别平衡** | 使用 balanced 类权重和 SMOTE 增强平局类 |

## Evaluation Data

| 指标 | 值 |
|------|------|
| **准确率 (Accuracy)** | ~52% (三分类) |
| **主胜精确率** | ~60% |
| **平局召回率** | ~40% (最难预测的类别) |
| **客胜精确率** | ~58% |
| **Brier Score** | ~0.55 |
| **对数损失** | ~1.02 |

## Model Architecture

### 集成策略
```
输入 → 特征工程 → 标准化 → [XGBoost, Ridge] → 加权平均 → 归一化 → 输出
```

- **XGBoost**: 梯度提升树 (max_depth=6, n_estimators=200)
- **Ridge**: L2 正则化线性模型 (alpha=1.0)
- **权重**: XGBoost 0.7 + Ridge 0.3
- **标准化**: StandardScaler (零均值, 单位方差)

### 特征类别

| 类别 | 特征数 | 示例 |
|------|--------|------|
| 🏠 主队实力 | ~15 | home_attack_strength, home_defense_strength, home_form_last5 |
| ✈️ 客队实力 | ~15 | away_attack_strength, away_defense_strength, away_form_last5 |
| 💰 赔率信号 | ~10 | home_win_odds, draw_odds, away_win_odds, odds_implied_* |
| ⚔️ 历史交锋 | ~8 | h2h_home_wins, h2h_draws, h2h_away_wins |
| 🏟️ 联赛特征 | ~6 | league_home_advantage, league_draw_rate |
| 📊 趋势特征 | ~10 | home_recent_trend, away_recent_trend |
| 🧮 量化特征 | ~15 | 各类统计衍生特征 |
| 🌐 综合特征 | ~11 | 加权组合特征 |

## Output Interpretation

### 概率输出
```json
{
  "home": 0.4512,    // 主队胜概率 (0-1)
  "draw": 0.2817,    // 平局概率 (0-1)
  "away": 0.2671,    // 客队胜概率 (0-1)
  "_model": "football_balanced_production.joblib",  // 模型来源
  "_version": "3.0",                                  // 模型版本
  "_timestamp": "2026-06-11T10:30:00+00:00",         // 预测时间
  "_feature_count": 93                                 // 输入特征数
}
```

### 置信度解释

| 最高概率 | 置信度 | 解读 |
|---------|--------|------|
| > 0.55 | 🟢 高 | 模型对预测结果较有信心 |
| 0.40-0.55 | 🟡 中 | 有一定倾向，但不确定性较高 |
| < 0.40 | 🔴 低 | 三种结果概率接近，预测不可靠 |

### ⚠️ 硬编码概率检测
如果预测结果接近 H=0.40/D=0.28/A=0.32（±0.02），系统会自动抛出 `HardcodedProbabilityError` 异常并终止预测。这表示模型可能未正确加载，回退到了硬编码值。

## Limitations

1. **平局预测偏弱**: 足球比赛中平局发生概率约 25-28%，是三分类中最难预测的
2. **冷门频发**: 足球比赛本身具有高度不确定性，弱队胜率约 15-20%
3. **数据滞后**: 模型基于历史数据训练，无法预测突发事件（伤病、天气骤变等）
4. **赔率依赖**: 模型对赔率特征有较高依赖，若赔率数据异常会影响预测
5. **联赛差异**: 不同联赛的比赛风格差异较大，模型对非主流联赛的预测精度较低

## Ethical Considerations

- 本模型仅供学术研究和娱乐用途
- 预测结果不应被视为投注建议
- 模型可能存在对特定联赛/球队的系统性偏差
- 用户应理性看待预测概率，理解不确定性的存在

---

> 本模型卡由工程保障团队 AI 协作生成，最后更新: 2026-06-11
