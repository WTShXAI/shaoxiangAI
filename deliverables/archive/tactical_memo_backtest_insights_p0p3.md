# 战术备忘录：多模型回测启发与 P0-P3 优化执行方案

**文档级别**: 内部战略 · 全团对齐  
**生成时间**: 2026-06-20 21:05  
**主导**: 郝优算（算法总工）  
**参与**: 荣合众（集成）、曾均衡（不平衡）、毕建模（数学）、舒治理（数据）、杜博弈（博弈论）  
**基线数据**: 世界杯 2026 小组赛 26 场回测 · 5 个 Stacking 模型 · 系统级 16 场

---

## 〇、执行摘要

多模型回测暴露了四个被"模型精度"掩盖的结构性问题。核心结论：**继续调模型参数是死路，改判型策略和场景适配才是活路**。本备忘录将四大发现转化为 P0-P3 四级执行方案，按投入产出比排序，P0 可立即落地。

| 优先级 | 方向 | 预期收益 | 开发量 | 风险 |
|--------|------|---------|--------|------|
| **P0** | 改判型策略（动态阈值 + cost-sensitive） | 平局 F1: 0 → 0.15-0.25 | 1天 | 低 |
| **P1** | 建高平局率场景验证集 | 防止 OOF 指标误导版本决策 | 2天 | 低 |
| **P2** | 特征对齐逻辑解耦 | 子模型可独立调用，降级链路可用 | 3-4天 | 中 |
| **P3** | 杯赛专用规则策略层 | 杯赛场景准确率 +5-8pp | 2天 | 低 |

---

## 一、四大回测发现

### 发现 1：argmax 结构性缺陷 = 平局 F1=0 的真正元凶

**数据证据**:
- 4 个 Stacking 模型（v3.2/v4.0/v4.1）在 26 场世界杯上准确率完全一致：57.69%
- 平局 F1 全部为 0.0 — 10 场真实平局全部漏判
- 混淆矩阵：真实平局 10 场 → 8 场判主胜、2 场判客胜、0 场判平局

**根因定位**:
```
UnifiedPredictor 判型逻辑 (unified_predictor.py L383-396):
    if p_d > 0.46:        ← 阈值太高！pD 几乎永远 < 0.46
        prediction = 'D'
    elif p_h > p_a:       ← 退化为 H/A 二分类
        prediction = 'H'
    else:
        prediction = 'A'
```

**为什么 pD 永远达不到 0.46？**
1. 赔率结构天然 pD < max(pH, pA)（除非极端均衡赛）
2. Stacking meta_learner 在联赛数据上学习到"平局=少数类"(25%)，倾向压制 pD
3. DrawExpert boost 最多 +0.08（L377: `min(draw_signal * 0.15, 0.08)`），杯水车薪
4. λ融合的泊松模型进一步压制 pD（泊松分布的平局概率天然偏低）

**启发**: 真正能救平局的不是继续堆模型精度，而是**改判型策略**。

---

### 发现 2：分布偏移让"OOF 提升"变成错觉

**数据证据**:
| 场景 | v3.2 | v4.1 | 提升 |
|------|------|------|------|
| 联赛 OOF Acc | 59.20% | 62.43% | +3.23pp ✅ |
| 联赛 OOF Draw-F1 | 0.504 | 0.520 | +0.016 ✅ |
| 世界杯 Acc | 57.69% | 57.69% | 0pp ❌ |
| 世界杯 Draw-F1 | 0.000 | 0.000 | 0 ❌ |

**根因**:
- 世界杯平局率 38.5%，联赛约 25% — 模型在"平局是少数派"的世界训练
- 到了"平局快成主流"的世界杯，整个概率分布失真
- v4.1 的 DrawExpert 衰减×0.25 + 阈值 0.46 在世界杯上完全无效（pD 从未达到 0.46）

**启发**: OOF 指标在分布偏移场景下会误导决策。未来评估模型，不能只看联赛 OOF，必须加一个"高平局率场景"验证集。

---

### 发现 3：规则层在小样本场景反而碾压模型

**数据证据**:
| 配置 | 准确率 | 平局 F1 | 说明 |
|------|--------|---------|------|
| 纯 Stacking argmax | 57.69% | 0.000 | 模型完全失效 |
| 纯 D-Gate 规则判型 | 42.3% | 0.306 | 准确率低但能抓平局 |
| v4.1 + D-Gate + 风控 | ~50% | 0.200 | 模式 C 贡献平局命中 |

**反直觉但合理**:
- 模型靠统计规律，规则靠因果逻辑
- 26 场样本太小，模型统计置信度不够
- 规则只要条件满足就触发，不受样本量限制
- 模式 C（超热门翻车）在葡萄牙 vs 民主刚果成功触发

**启发**: 世界杯/杯赛这种小样本场景，应该是"规则主导 + 模型辅助"，而不是反过来。

---

### 发现 4：单模型失效暴露了管线耦合陷阱

**数据证据**:
| 子模型 | 独立使用结果 | 失败原因 |
|--------|------------|---------|
| DrawExpert v1 | 输出恒定 0.331 | 特征对齐失败（需 72 维，仅传 5 维） |
| Neural Network | 11.54% 准确率 | 权重加载错误（key 前缀不匹配） |

**根因**:
- DrawExpert 训练时依赖完整 72 维特征（含联赛风格嵌入、球队近况等）
- 回测时只有赔率，无法完整重建特征向量
- NN 的 state_dict 在 v4.1 模型 dict 中存储，独立加载 .pth 时 key 路径不对
- 这些模型不是独立可用组件，是深度耦合在 Stacking 管线里的

**启发**: 当前架构的"模块化"是假的。真要做实时预测或灵活降级，必须先把特征对齐逻辑从 Stacking 管线里解耦出来。

---

## 二、P0：改判型策略（动态阈值 + cost-sensitive argmax）

**主导**: 荣合众（集成专家）  
**辅助**: 曾均衡（不平衡专家）  
**目标**: 平局 F1 从 0 提升到 0.15-0.25，不牺牲主胜/客胜准确率  
**开发量**: 1 天  
**风险**: 低（仅改判型逻辑，不动模型权重）

### 2.1 问题根因

当前判型逻辑（`predictors/unified_predictor.py` L383-396）：

```python
# 当前: 固定高阈值，pD 几乎永远过不了
if p_d > 0.46:          # ← 太高
    prediction = 'D'
elif p_h > p_a + 0.0:
    prediction = 'H'
else:
    prediction = 'A'
```

三个问题：
1. **阈值 0.46 太高** — 世界杯 pD 均值约 0.25-0.30，永远达不到
2. **DrawExpert boost 太弱** — 最多 +0.08，不够跨越 0.46 门槛
3. **无赛事类型感知** — 世界杯平局率 38.5% vs 联赛 25%，同一阈值不适用

### 2.2 方案设计：三层判型改造

#### 改造 1：动态阈值（根据赛事类型 + 赔率结构自适应）

```python
# 新: 动态阈值
def _compute_draw_threshold(self, odds_h, odds_d, odds_a, match_type):
    """根据赛事类型和赔率结构计算动态平局阈值"""
    imp_sum = 1/odds_h + 1/odds_d + 1/odds_a
    imp_d = (1/odds_d) / imp_sum
    spread = abs(odds_a - odds_h)
    
    # 基础阈值
    base_threshold = 0.35  # 从 0.46 降到 0.35
    
    # 赛事类型调整
    if match_type in ('world_cup', 'continental_cup', 'tournament'):
        base_threshold -= 0.05  # 杯赛平局率高，降低门槛
    
    # 赔率结构调整: 均衡赛降低门槛
    if spread < 0.5:  # 均衡赛
        base_threshold -= 0.03
    if imp_d > 0.28:  # 平赔隐含概率已较高
        base_threshold -= 0.02
    
    # 下限保护: 不低于 0.25 (防止过度判平局)
    return max(base_threshold, 0.25)
```

**修改位置**: `predictors/unified_predictor.py`  
**修改行**: L106 附近新增方法 + L383-396 替换判型逻辑

#### 改造 2：cost-sensitive argmax（给 pD 加权）

```python
# 新: cost-sensitive 判型
def _classify(self, probs, draw_threshold, match_type):
    """cost-sensitive 分类: 平局类别加权"""
    p_h, p_d, p_a = probs[0], probs[1], probs[2]
    
    # 杯赛场景: 给 pD 额外加权
    draw_weight = 1.15 if match_type in ('world_cup', 'tournament') else 1.0
    
    # 阈值判型: pD 超过动态阈值 → 判平局
    if p_d * draw_weight > draw_threshold:
        return 'D'
    
    # cost-sensitive argmax: H vs A 时考虑 pD 的分流
    # 如果 pD 较高(接近阈值), 倾向判平局而非勉强分胜负
    if p_d > draw_threshold * 0.85:  # 接近阈值
        return 'D'
    
    # 标准 H vs A
    return 'H' if p_h > p_a else 'A'
```

**修改位置**: `predictors/unified_predictor.py` L383-396

#### 改造 3：增大 DrawExpert boost 幅度

```python
# 当前 (L377): boost = min(draw_signal * 0.15, 0.08)  ← 最多 +0.08
# 新: 分级 boost
if draw_signal > self.draw_gate_threshold:
    if draw_signal > 0.50:        # 强信号
        boost = min(draw_signal * 0.25, 0.15)   # 最多 +0.15
    elif draw_signal > 0.35:      # 中信号
        boost = min(draw_signal * 0.20, 0.12)   # 最多 +0.12
    else:                         # 弱信号
        boost = min(draw_signal * 0.15, 0.08)   # 保持原逻辑
    final_probs[1] += boost
    final_probs = final_probs / final_probs.sum()
```

**修改位置**: `predictors/unified_predictor.py` L375-379

### 2.3 预期效果

| 指标 | 当前 | P0 后 | 变化 |
|------|------|-------|------|
| 准确率 | 57.69% | 53-56% | -2~5pp（用准确率换平局召回） |
| 平局 F1 | 0.000 | 0.15-0.25 | **质变** |
| 平局召回 | 0% | 30-40% | 10 场平局命中 3-4 场 |
| 主胜 F1 | 0.7273 | 0.65-0.70 | 轻微下降 |
| 客胜 F1 | 0.6667 | 0.60-0.65 | 轻微下降 |

**关键认知**: 准确率会下降 2-5pp，但这是**有意义的牺牲** — 在平局率 38.5% 的世界杯场景下，一个能识别 30-40% 平局的系统比一个 57.69% 准确率但完全漏判平局的系统更有实战价值。

### 2.4 验收标准

```python
# 验收脚本: scripts/verify_p0_threshold.py
# 1. 26 场世界杯回测: 平局 F1 >= 0.15
# 2. 主胜准确率下降 <= 5pp (从 92% → >= 87%)
# 3. 客胜准确率不下降 (100% → >= 90%)
# 4. 联赛 OOF 回测: 准确率下降 <= 2pp (从 62.43% → >= 60.5%)
# 5. 联赛 OOF Draw-F1 不下降 (>= 0.504)
```

### 2.5 风险与兜底

| 风险 | 概率 | 影响 | 兜底 |
|------|------|------|------|
| 动态阈值过度判平局 | 中 | 主胜准确率下降超 5pp | 设置下限 0.25 + 回测验证 |
| 联赛场景受影响 | 低 | OOF 指标下降 | 赛事类型区分，联赛保持原阈值 |
| DrawExpert 信号不稳定 | 中 | boost 噪声 | 分级 boost + 信号阈值过滤 |

**回滚方案**: `use_threshold=False` 回退到纯 argmax 模式，1 分钟内恢复原逻辑。

---

## 三、P1：建高平局率场景验证集

**主导**: 毕建模（数学专家）  
**辅助**: 舒治理（数据专家）  
**目标**: 防止 OOF 指标继续误导版本决策  
**开发量**: 2 天  
**风险**: 低（仅新增验证逻辑，不动现有代码）

### 3.1 问题根因

当前训练管线（`training/training_pipeline.py`）的问题：

1. **单一时序切分**（L258）: `cutoff = '2023-01-01'`，只有联赛 train/test，没有杯赛验证集
2. **评估用 argmax**（L93）: `y_pred = proba.argmax(axis=1)` — 与生产判型逻辑不一致
3. **自动晋升只看 accuracy**（L196-199）: 不看 draw_f1，可能晋升准确率高但平局 F1 下降的模型

### 3.2 方案设计

#### 改造 1：新增高平局率场景验证集

```python
# training/training_pipeline.py 新增方法
def _build_cup_validation_set(self, df_all):
    """构建杯赛/高平局率场景验证集"""
    # 筛选条件: 世界杯、洲际杯、锦标赛
    cup_keywords = ['world_cup', 'continental', 'tournament', 'cup', 'playoff']
    mask = df_all['match_type'].str.contains('|'.join(cup_keywords), case=False, na=False)
    df_cup = df_all[mask].copy()
    
    # 如果杯赛数据不足, 用高平局率联赛数据补充
    if len(df_cup) < 50:
        # 筛选平局率 > 30% 的联赛轮次
        for league in df_all['league'].unique():
            df_league = df_all[df_all['league'] == league]
            draw_rate = (df_league['result'] == 1).mean()  # 1=平局
            if draw_rate > 0.30:
                df_cup = pd.concat([df_cup, df_league])
    
    return df_cup

def _evaluate_multi_scenario(self, trainer, df_test_league, df_test_cup):
    """多场景评估: 联赛 OOF + 高平局率场景"""
    metrics_league = self._evaluate_model(trainer, df_test_league)
    metrics_cup = self._evaluate_model(trainer, df_test_cup) if len(df_test_cup) > 0 else None
    
    # 综合评分: 联赛 60% + 杯赛 40%
    if metrics_cup:
        composite_score = (
            metrics_league['accuracy'] * 0.6 + 
            metrics_cup['accuracy'] * 0.4
        )
        draw_f1_composite = (
            metrics_league['draw_f1'] * 0.5 + 
            metrics_cup['draw_f1'] * 0.5
        )
    else:
        composite_score = metrics_league['accuracy']
        draw_f1_composite = metrics_league['draw_f1']
    
    return {
        'league': metrics_league,
        'cup': metrics_cup,
        'composite_accuracy': composite_score,
        'composite_draw_f1': draw_f1_composite,
    }
```

**修改位置**: `training/training_pipeline.py` 新增方法 + 修改 `run()` 调用

#### 改造 2：自动晋升增加 draw_f1 约束

```python
# 当前 (L196-199):
accuracy_gain = metrics['accuracy'] - prod['metrics']['accuracy']
if accuracy_gain >= self.auto_promote_threshold:
    self.registry.deploy(model_id)

# 新: 增加 draw_f1 不下降约束
accuracy_gain = metrics['accuracy'] - prod['metrics']['accuracy']
draw_f1_change = metrics['draw_f1'] - prod['metrics'].get('draw_f1', 0)

# 晋升条件: 准确率提升 AND 平局 F1 不下降
if accuracy_gain >= self.auto_promote_threshold and draw_f1_change >= -0.02:
    self.registry.deploy(model_id)
    logger.info(f"自动晋升: +{accuracy_gain:.1f}pp Acc, ΔF1={draw_f1_change:+.3f}")
elif accuracy_gain >= self.auto_promote_threshold and draw_f1_change < -0.02:
    logger.warning(f"拒绝晋升: Acc +{accuracy_gain:.1f}pp 但 Draw-F1 {draw_f1_change:+.3f}")
```

**修改位置**: `training/training_pipeline.py` L196-199

#### 改造 3：评估判型与生产判型对齐

```python
# 当前 (L93): y_pred = proba.argmax(axis=1)
# 新: 使用与生产一致的阈值判型
def _predict_with_threshold(self, proba, draw_threshold=0.35):
    """与生产一致的阈值判型"""
    y_pred = np.zeros(len(proba), dtype=int)
    for i in range(len(proba)):
        if proba[i, 1] > draw_threshold:
            y_pred[i] = 1  # Draw
        elif proba[i, 0] > proba[i, 2]:
            y_pred[i] = 0  # Home
        else:
            y_pred[i] = 2  # Away
    return y_pred
```

**修改位置**: `training/training_pipeline.py` L93

### 3.3 验收标准

```python
# 验收脚本: scripts/verify_p1_validation.py
# 1. 训练管线输出包含 league + cup 两套指标
# 2. 自动晋升逻辑包含 draw_f1 约束
# 3. 评估判型使用阈值模式而非 argmax
# 4. 已有模型的 OOF 指标不受影响（回归测试）
```

### 3.4 风险与兜底

| 风险 | 概率 | 影响 | 兜底 |
|------|------|------|------|
| 杯赛数据不足 | 高 | 验证集样本太小 | 用高平局率联赛数据补充 |
| 双场景评估增加训练时间 | 低 | +10-15秒 | 可接受 |

---

## 四、P2：特征对齐逻辑解耦

**主导**: 舒治理（数据专家）  
**辅助**: 荣合众（集成专家）  
**目标**: 子模型可独立调用，降级链路真正可用  
**开发量**: 3-4 天  
**风险**: 中（涉及特征管线重构）

### 4.1 问题根因

当前特征对齐的两个核心问题：

**问题 1: DrawExpert 特征对齐失败**

```python
# unified_predictor.py L596-619
def _get_draw_expert_signal(self, home, away, oh, od, oa):
    # 只构建了 5 个特征!
    feats = {
        'draw_imp': ..., 'odds_spread': ..., 'draw_odds': ...,
        'draw_odds_dev': ..., 'match_evenness': ...
    }
    return float(de.predict_proba(feats)[1])
    # 但 DrawExpert 训练时用 72 维特征 → 输入不匹配 → 输出恒定 0.331
```

**问题 2: NN 权重加载路径错误**

```python
# 独立加载 .pth 文件时 key 前缀不匹配
# 正确路径: v41_model['nn_state_dict'] + v41_model['scaler']
# 错误路径: 独立加载 .pth 文件
```

**问题 3: 生产特征计算与训练特征计算口径不一致**

- 训练时: `EnsembleTrainer.prepare_features()` 完整构建 72 维
- 生产时: `UnifiedPredictor._sky_predict()` 从赔率推导 40+ 维，剩余用默认值填充
- 导致: 训练/生产特征分布不同 → 效果滑坡

### 4.2 方案设计

#### 改造 1：提取统一特征构建器

```python
# 新建: features/feature_aligner.py
class FeatureAligner:
    """统一特征构建器 — 训练和生产共用"""
    
    FEATURE_NAMES = None  # 从模型加载
    
    @classmethod
    def from_trainer(cls, trainer):
        """从 EnsembleTrainer 加载特征名和默认值"""
        inst = cls()
        inst.FEATURE_NAMES = trainer.feature_names
        inst.defaults = trainer.config.get('data', {}).get('default_values', {})
        return inst
    
    def build_features(self, home, away, oh, od, oa, 
                       asian_handicap=0, ou_line=2.5,
                       over_water=1.90, under_water=1.92,
                       open_h=0, open_d=0, open_a=0,
                       extra_features=None):
        """
        统一特征构建 — 训练和生产调用同一入口
        extra_features: 可选的额外特征(联赛风格嵌入等), 生产环境可为None
        """
        vec = np.zeros(len(self.FEATURE_NAMES), dtype=np.float32)
        
        # 1. 填充默认值
        for i, name in enumerate(self.FEATURE_NAMES):
            if name in self.defaults:
                vec[i] = float(self.defaults[name])
        
        # 2. 从赔率推导的核心特征 (训练和生产完全一致)
        odds_features = self._compute_odds_features(oh, od, oa, asian_handicap, ou_line, 
                                                     over_water, under_water, open_h, open_d, open_a)
        for name, val in odds_features.items():
            if name in self.FEATURE_NAMES:
                vec[self.FEATURE_NAMES.index(name)] = float(val)
        
        # 3. 额外特征 (训练时有, 生产时可能缺失)
        if extra_features:
            for name, val in extra_features.items():
                if name in self.FEATURE_NAMES:
                    vec[self.FEATURE_NAMES.index(name)] = float(val)
        
        return vec
    
    def _compute_odds_features(self, oh, od, oa, ah, ou, ow, uw, oph, opd, opa):
        """从赔率推导的特征 — 与 _sky_predict 中的逻辑完全一致"""
        # ... (从 unified_predictor.py L449-541 提取)
        pass
```

**修改位置**: 新建 `features/feature_aligner.py` + 修改 `unified_predictor.py` 调用

#### 改造 2：修复 DrawExpert 特征对齐

```python
# unified_predictor.py 修改 _get_draw_expert_signal
def _get_draw_expert_signal(self, home, away, oh, od, oa):
    """DrawExpert P(Draw) 信号 — 使用统一特征构建器"""
    from features.feature_aligner import FeatureAligner
    
    # 用统一构建器生成完整特征向量
    aligner = FeatureAligner.from_trainer(self.trainer)
    vec = aligner.build_features(home, away, oh, od, oa)
    
    # 标准化
    if self.trainer.scaler:
        vec = self.trainer.scaler.transform(vec.reshape(1, -1))[0]
    
    # 用完整特征向量调用 DrawExpert
    de_p = self.trainer.draw_expert_model.predict_proba(vec.reshape(1, -1))
    if de_p.shape[1] == 2:
        return float(de_p[0, 1])
    return 0.0
```

**修改位置**: `predictors/unified_predictor.py` L596-619

#### 改造 3：修复 NN 权重加载

```python
# unified_predictor.py 修改 NN 加载逻辑
def _load_nn_from_model_dict(self):
    """从 v4.1 模型 dict 提取 NN 权重 (而非独立加载 .pth)"""
    if hasattr(self.trainer, 'nn_state_dict') and self.trainer.nn_state_dict:
        # 正确路径: 从模型 dict 提取
        nn_model = self.trainer._build_nn_model()
        nn_model.load_state_dict(self.trainer.nn_state_dict)
        nn_model.eval()
        return nn_model
    return None
```

**修改位置**: `predictors/unified_predictor.py` + `ensemble_trainer.py`

#### 改造 4：降级链路验证

```python
# 新建: tests/test_degradation_chain.py
def test_draw_expert_standalone():
    """DrawExpert 独立调用 — 不再输出恒定值"""
    up = UnifiedPredictor()
    signal1 = up._get_draw_expert_signal("英格兰", "克罗地亚", 1.30, 5.00, 8.30)
    signal2 = up._get_draw_expert_signal("卡塔尔", "瑞士", 5.60, 3.75, 1.61)
    assert signal1 != signal2  # 不同比赛应有不同输出
    assert 0.0 < signal1 < 1.0
    assert 0.0 < signal2 < 1.0

def test_nn_standalone():
    """NN 独立调用 — 不再全部预测客胜"""
    nn = up._load_nn_from_model_dict()
    assert nn is not None
    # 不同输入应有不同输出
    # ...
```

### 4.3 验收标准

```python
# 验收脚本: scripts/verify_p2_feature_align.py
# 1. DrawExpert 独立调用: 不同比赛输出不同值 (不再恒定 0.331)
# 2. NN 独立调用: 准确率 > 30% (从 11.54% 提升)
# 3. 训练/生产特征一致性: 同一赔率输入, 特征向量完全相同
# 4. 降级链路: 主模型故障时, 子模型可独立提供预测
# 5. 471 回归测试全通过
```

### 4.4 风险与兜底

| 风险 | 概率 | 影响 | 兜底 |
|------|------|------|------|
| 特征重构引入 bug | 中 | 线上效果波动 | 471 回归测试 + 灰度上线 |
| DrawExpert 特征仍不对齐 | 中 | 信号无改善 | 保留原 fallback 逻辑 |
| NN 加载仍失败 | 低 | NN 通道不可用 | 保持 CPU 降级兜底 |

---

## 五、P3：杯赛专用规则策略层

**主导**: 杜博弈（博弈论专家）  
**辅助**: 曾均衡（不平衡专家）  
**目标**: 杯赛场景准确率 +5-8pp，平局识别优于纯模型  
**开发量**: 2 天  
**风险**: 低（独立规则配置，不影响联赛逻辑）

### 5.1 问题根因

当前 D-Gate 三模式（`backend/main.py` L618-668）对联赛和杯赛使用同一套参数：
- 模式 A 的 `0.50 < max_imp <= 0.70` 对杯赛太窄（杯赛强队多）
- 模式 B 的 `spread < 0.16` 对杯赛太严（杯赛均衡赛多）
- 模式 C 的 `max_imp > 0.72` 对杯赛合适但触发条件单一
- 没有"杯赛模式"开关，无法按赛事类型调整规则权重

### 5.2 方案设计

#### 改造 1：赛事类型感知的规则配置

```json
// config/tournament_rules.json
{
  "world_cup": {
    "rule_weight": 0.65,        // 规则层权重 (vs 模型 0.35)
    "mode_a": {
      "imp_range": [0.45, 0.72],  // 扩大范围 (杯赛强队多)
      "min_signals": 2,
      "ou_threshold": 2.75        // 放宽 OU (杯赛进球偏少)
    },
    "mode_b": {
      "spread_threshold": 0.20,   // 放宽 (杯赛均衡赛多)
      "draw_odds_range": [2.8, 4.8],
      "ou_threshold": 2.75
    },
    "mode_c": {
      "imp_threshold": 0.68,      // 降低 (杯赛超热门更多)
      "draw_odds_max": 6.5,       // 放宽
      "ou_threshold": 3.25
    },
    "extra_rules": {
      "fifa_rank_diff_threshold": 30,  // 排名接近 → 平局
      "group_round_1_draw_boost": 0.05  // 小组赛首轮保守
    }
  },
  "league": {
    "rule_weight": 0.35,        // 模型主导
    "mode_a": {
      "imp_range": [0.50, 0.70],
      "min_signals": 2,
      "ou_threshold": 2.5
    },
    "mode_b": {
      "spread_threshold": 0.16,
      "draw_odds_range": [3.0, 4.5],
      "ou_threshold": 2.5
    },
    "mode_c": {
      "imp_threshold": 0.72,
      "draw_odds_max": 6.0,
      "ou_threshold": 3.0
    }
  },
  "continental_cup": {
    // 洲际杯配置 (介于世界杯和联赛之间)
    "rule_weight": 0.55,
    "mode_a": { "imp_range": [0.48, 0.71], "min_signals": 2, "ou_threshold": 2.6 },
    "mode_b": { "spread_threshold": 0.18, "draw_odds_range": [2.9, 4.6], "ou_threshold": 2.6 },
    "mode_c": { "imp_threshold": 0.70, "draw_odds_max": 6.2, "ou_threshold": 3.1 }
  }
}
```

#### 改造 2：D-Gate 加载赛事配置

```python
# backend/main.py 修改 D-Gate 逻辑
import json

# 模块顶部加载规则配置
_TOURNAMENT_RULES = {}
_rules_path = os.path.join(ROOT, 'config', 'tournament_rules.json')
if os.path.exists(_rules_path):
    with open(_rules_path, 'r', encoding='utf-8') as f:
        _TOURNAMENT_RULES = json.load(f)

def _get_tournament_config(match_type):
    """获取赛事类型对应的规则配置"""
    if not match_type:
        return _TOURNAMENT_RULES.get('league', {})
    
    mt = match_type.lower()
    if 'world' in mt or 'wc' in mt:
        return _TOURNAMENT_RULES.get('world_cup', {})
    elif 'continental' in mt or 'euro' in mt or 'copa' in mt or 'asian' in mt or 'africa' in mt:
        return _TOURNAMENT_RULES.get('continental_cup', {})
    else:
        return _TOURNAMENT_RULES.get('league', {})

# D-Gate 三模式使用赛事配置
def _apply_dgate_with_config(imp_h, imp_a, od, spread, ou_line, 
                               handicap, water_level, d_boosted,
                               match_type, fifa_rank_diff, group_round):
    """赛事感知的 D-Gate 判定"""
    config = _get_tournament_config(match_type)
    
    mode_a = config.get('mode_a', {})
    mode_b = config.get('mode_b', {})
    mode_c = config.get('mode_c', {})
    extra = config.get('extra_rules', {})
    
    max_imp = max(imp_h, imp_a)
    d_gate_active = False
    verdict = None
    
    # 庄家信号 (共用)
    _shallow_hcap = handicap is not None and abs(handicap) <= 0.5
    _high_water = water_level is not None and water_level >= 2.0
    _highly_balanced = spread < mode_b.get('spread_threshold', 0.16)
    _low_ou = ou_line is not None and ou_line <= mode_a.get('ou_threshold', 2.5)
    
    # FIFA 排名信号
    _extra_signal = 0
    if fifa_rank_diff is not None and abs(fifa_rank_diff) < extra.get('fifa_rank_diff_threshold', 30):
        _extra_signal += 1
    if group_round == 1:
        _extra_signal += 1
    
    _signal_count = sum([_shallow_hcap, _high_water, _highly_balanced, d_boosted > 0.30, _extra_signal > 0])
    
    # 模式 A
    imp_range = mode_a.get('imp_range', [0.50, 0.70])
    if imp_range[0] < max_imp <= imp_range[1] and _low_ou and _signal_count >= mode_a.get('min_signals', 2):
        d_gate_active = True
        verdict = 'D'
    
    # 模式 B
    elif (spread < mode_b.get('spread_threshold', 0.16) and 
          mode_b.get('draw_odds_range', [3.0, 4.5])[0] <= od <= mode_b.get('draw_odds_range', [3.0, 4.5])[1] and 
          _low_ou and _signal_count > 0):
        d_gate_active = True
        d_boosted = min(d_boosted + 0.10, 0.42)
        verdict = 'D'
    
    # 模式 C
    elif (max_imp > mode_c.get('imp_threshold', 0.72) and 
          od < mode_c.get('draw_odds_max', 6.0) and 
          ou_line is not None and ou_line <= mode_c.get('ou_threshold', 3.0)):
        d_gate_active = True
        d_boosted = min(d_boosted + 0.15, 0.45)
        verdict = 'D'
    
    # 小组赛首轮额外 boost
    if d_gate_active and group_round == 1:
        d_boosted = min(d_boosted + extra.get('group_round_1_draw_boost', 0.05), 0.50)
    
    return d_gate_active, verdict, d_boosted
```

**修改位置**: `backend/main.py` L618-668 替换为赛事感知版本 + 新建 `config/tournament_rules.json`

#### 改造 3：规则-模型权重融合

```python
# 杯赛场景: 规则主导 (65%) + 模型辅助 (35%)
# 联赛场景: 模型主导 (65%) + 规则辅助 (35%)

def _fuse_rule_model(self, model_probs, rule_verdict, rule_weight, match_type):
    """规则与模型概率融合"""
    config = _get_tournament_config(match_type)
    w_rule = config.get('rule_weight', 0.35)
    w_model = 1.0 - w_rule
    
    if rule_verdict == 'D':
        # 规则判平局: 提升 pD
        adjusted = model_probs.copy()
        adjusted[1] = adjusted[1] * (1 + w_rule)  # 规则权重加成
        adjusted = adjusted / adjusted.sum()
        return adjusted
    elif rule_verdict in ('H', 'A'):
        # 规则判胜负: 对应方向加成
        idx = 0 if rule_verdict == 'H' else 2
        adjusted = model_probs.copy()
        adjusted[idx] = adjusted[idx] * (1 + w_rule * 0.5)  # 较弱加成
        adjusted = adjusted / adjusted.sum()
        return adjusted
    else:
        return model_probs  # 规则未触发, 纯模型
```

### 5.3 预期效果

| 场景 | 当前准确率 | P3 后 | 变化 |
|------|----------|-------|------|
| 世界杯（规则主导） | ~50% | 55-58% | +5-8pp |
| 联赛（模型主导） | ~62% | ~62% | 不变 |
| 世界杯平局 F1 | 0.20 | 0.25-0.35 | +25-75% |

### 5.4 验收标准

```python
# 验收脚本: scripts/verify_p3_tournament_rules.py
# 1. 世界杯 26 场回测: 准确率 >= 55%
# 2. 世界杯平局 F1 >= 0.25
# 3. 联赛 OOF 回测: 准确率不下降 (>= 62.43%)
# 4. 赛事类型识别正确 (world_cup → world_cup 配置)
# 5. 471 回归测试全通过
```

### 5.5 风险与兜底

| 风险 | 概率 | 影响 | 兜底 |
|------|------|------|------|
| 杯赛规则过度触发 | 中 | 误判平局增多 | 回测验证 + 信号协同约束 |
| 赛事类型识别错误 | 低 | 加载错误配置 | 默认回退到 league 配置 |
| 联赛受影响 | 极低 | OOF 下降 | 规则权重 0.35，模型仍主导 |

**回滚方案**: 删除 `tournament_rules.json` 或将所有 `rule_weight` 设为 0.35，回退到当前逻辑。

---

## 六、执行路线图

```
Week 1 (P0 + P1 并行):
├── Day 1: P0 判型策略改造 (荣合众)
│   ├── 动态阈值实现
│   ├── cost-sensitive argmax
│   ├── DrawExpert boost 增强
│   └── 26 场世界杯回测验证
├── Day 1-2: P1 验证集建设 (毕建模, 与 P0 并行)
│   ├── 高平局率验证集分割
│   ├── 多场景评估逻辑
│   ├── 自动晋升 draw_f1 约束
│   └── 评估判型对齐
└── Day 2: P0+P1 联合验收
    ├── 26 场世界杯回测: 平局 F1 >= 0.15
    ├── 联赛 OOF 回测: 准确率不下降
    └── 471 回归测试通过

Week 2 (P2 + P3 并行):
├── Day 3-5: P2 特征对齐解耦 (舒治理)
│   ├── 统一特征构建器
│   ├── DrawExpert 特征修复
│   ├── NN 权重加载修复
│   └── 降级链路验证
├── Day 3-4: P3 杯赛规则层 (杜博弈, 与 P2 并行)
│   ├── 赛事规则配置文件
│   ├── D-Gate 赛事感知
│   ├── 规则-模型权重融合
│   └── 杯赛回测验证
└── Day 5: P2+P3 联合验收 + 全量回归
    ├── 26 场世界杯回测: 准确率 >= 55%, 平局 F1 >= 0.25
    ├── 联赛 OOF 回测: 准确率不下降
    ├── 降级链路测试: 子模型可独立调用
    └── 471 回归测试通过
```

### 关键里程碑

| 里程碑 | 交付物 | 验收标准 |
|--------|--------|---------|
| M1 (Day 2) | P0+P1 完成 | 平局 F1 >= 0.15, OOF 不下降 |
| M2 (Day 5) | P2+P3 完成 | 准确率 >= 55%, 降级链路可用 |
| M3 (Day 5) | 全量验收 | 471 测试通过, 线上灰度就绪 |

---

## 七、方案整合校验

### 接口对齐检查

| 对齐项 | P0 | P1 | P2 | P3 | 状态 |
|--------|----|----|----|----|------|
| 判型逻辑 | 动态阈值 | 评估对齐 | - | 规则融合 | ✅ 统一阈值模式 |
| 特征向量 | - | - | 统一构建器 | - | ✅ FeatureAligner |
| 赛事类型 | match_type 传入 | 验证集分割 | - | 规则配置 | ✅ 统一 match_type 字段 |
| DrawExpert 信号 | boost 增强 | - | 特征修复 | - | ✅ 统一信号源 |
| D-Gate 规则 | - | - | - | 赛事感知 | ✅ tournament_rules.json |

### FootballAI 专属约束 Checklist

- [x] 严格遵守时序切分规则（P1 新增杯赛验证集，不破坏现有时序切分）
- [x] 未引入 Beta 校准等已验证的负向方案
- [x] 数据类型兼容 SQLite（numpy.int64 → int() 转换）
- [x] 符合 spread 与胜率的客观规律（P3 规则配置基于 spread 客观规律）
- [x] 适配现有硬件环境（P0/P1 纯 CPU, P2/P3 无 GPU 依赖）
- [x] 兼容 v3.2/v4.0/v4.1 现有架构（P0 改判型不改模型, P2 解耦不破坏管线）
- [x] 赔率数据兼容 Interwetten 单源数据链路
- [x] 特征维度控制在 80-100 维最优区间（P2 不新增特征, 只修复对齐）

---

## 八、附录

### A. 回测数据来源

- **数据集**: 世界杯 2026 小组赛 26 场（CodeBuddy 标准数据集）
- **模型路径**: `D:/AI/footballAI/saved_models/`
- **回测脚本**: `scripts/backtest_all_models_worldcup.py`
- **完整报告**: `deliverables/multimodel_backtest_report.md`
- **原始结果**: `reports/multimodel_backtest_20260620_202444.json`

### B. 关键代码位置索引

| 模块 | 文件 | 行号 | 说明 |
|------|------|------|------|
| 判型逻辑 | `predictors/unified_predictor.py` | L383-396 | 阈值分类/argmax |
| DrawExpert boost | `predictors/unified_predictor.py` | L370-381 | 信号衰减+boost |
| 冷启动路径 | `predictors/unified_predictor.py` | L556-586 | 基模型加权平均 |
| DrawExpert 信号 | `predictors/unified_predictor.py` | L596-619 | 特征对齐失败点 |
| D-Gate 三模式 | `backend/main.py` | L618-668 | 模式 A/B/C |
| D-Gate chat 版 | `backend/main.py` | L968-1160 | risk_tag 系统 |
| 训练管线 | `training/training_pipeline.py` | L88-120 | 评估逻辑 |
| 时序切分 | `training/training_pipeline.py` | L258, L310 | cutoff 逻辑 |
| 自动晋升 | `training/training_pipeline.py` | L196-199 | accuracy 阈值 |

### C. 方案依赖关系

```
P0 (判型策略) ──────→ M1 验收
                        │
P1 (验证集) ──────────→ M1 验收
                        │
                        ↓
P2 (特征解耦) ──→ M2 验收
                    │
P3 (杯赛规则) ──→ M2 验收
                    │
                    ↓
                 M3 全量验收
```

P0 和 P1 无依赖，可并行。P2 和 P3 无依赖，可并行。M1 是 M2 的前置条件（判型策略改好后才能验证特征解耦效果）。

---

**文档结束。全团按此方案执行，有问题直接找我。**
