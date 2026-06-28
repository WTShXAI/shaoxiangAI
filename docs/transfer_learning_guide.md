# 哨响AI - 迁移学习微调指南 (T11)

## 概述

迁移学习框架允许在大联赛（如英超、西甲）上预训练模型，然后在小联赛或目标场景上微调，以利用大联赛的丰富数据提升小联赛的预测性能。

## 联赛数据分布

| 联赛 | 完赛场次 | Tier |
|------|---------|------|
| Premier League | 3,841 | 1 |
| La Liga | 3,849 | 1 |
| Serie A | 3,837 | 2 |
| Ligue 1 | 3,576 | 2 |
| Bundesliga | 3,085 | 3 |

## 快速开始

### 1. 领域差距评估

在决定是否使用迁移学习之前，先评估源域和目标域的差距：

```python
from optimize.transfer_learning import evaluate_transfer

# 评估 PL+LaLiga → Bundesliga 的迁移可行性
result = evaluate_transfer(
    source_leagues=['Premier League', 'La Liga'],
    target_league='Bundesliga',
)

print(f"领域差距: {result['domain_gap_level']}")    # low/medium/high
print(f"差距评分: {result['domain_gap_score']}")     # 0~1, 越小越接近
print(f"建议: {result['recommendation']}")
```

**差距等级解读**：
- **low (<0.3)**: 5大联赛之间，标签分布和特征模式非常接近，迁移效果好
- **medium (0.3-0.6)**: 存在一定特征偏移，需要合适的微调策略
- **high (>0.6)**: 领域差距大，可能负面迁移，建议从零训练

### 2. 一站式迁移学习

```python
from optimize.transfer_learning import TransferLearningManager, FineTuningConfig

config = FineTuningConfig(
    strategy='gradual',       # 微调策略
    pretrain_epochs=60,        # 预训练轮数
    finetune_epochs=30,        # 微调轮数
    pretrain_lr=0.001,         # 预训练学习率
    finetune_lr=0.0001,        # 微调学习率 (更小)
)

mgr = TransferLearningManager(db_path='data/football_data.db', config=config)
result = mgr.pretrain_and_finetune(
    source_leagues=['Premier League', 'La Liga'],
    target_league='Bundesliga',
    model_class_name='gru',
    strategies=['head_only', 'gradual', 'discriminative', 'full'],
    include_baseline=True,
    verbose=True,
)
```

### 3. 分步执行

```python
# Step 1: 预训练
pretrained_model, pretrain_result = mgr.pretrain(
    source_leagues=['Premier League', 'La Liga', 'Serie A'],
    model_class_name='gru',
)

# Step 2: 微调
finetuned_model, finetune_result = mgr.finetune(
    pretrained_model,
    target_leagues=['Bundesliga'],
    strategy='gradual',
)

# Step 3: 使用微调后的模型
from optimize.dl_models import save_dl_model
save_dl_model(finetuned_model, 'saved_models/bundesliga_finetuned.pt')
```

## 微调策略详解

### 1. head_only — 仅训练分类头

**适用场景**: 源域和目标域非常接近，编码器学到的特征可以直接复用

**配置建议**:
```python
FineTuningConfig(
    strategy='head_only',
    finetune_lr=0.001,
    finetune_epochs=20,
    finetune_patience=8,
)
```

**特点**:
- 冻结编码器，仅训练分类头（~28% 参数可训练）
- 训练速度快，不易过拟合
- 适合数据量极少的目标域

### 2. gradual — 渐进解冻 ★推荐

**适用场景**: 通用场景，平衡稳定性和适应性

**配置建议**:
```python
FineTuningConfig(
    strategy='gradual',
    gradual_unfreeze_steps=3,     # 分3步解冻
    gradual_epochs_per_step=5,    # 每步5轮
    finetune_lr=0.0001,
    finetune_epochs=30,
)
```

**工作流程**:
1. Step 1: 冻结编码器，仅训练分类头 (5 epochs)
2. Step 2: 解冻深层编码器 (5 epochs, lr × 0.5)
3. Step 3: 解冻中层编码器 (5 epochs, lr × 0.25)
4. Step 4: 解冻浅层编码器 (5 epochs, lr × 0.125)
5. Final: 全模型精调 (剩余 epochs, lr × 0.1)

**特点**:
- 从分类头到编码器逐层解冻，避免灾难性遗忘
- 每步降低学习率，保持已学到的特征
- 最适合5大联赛之间的迁移

### 3. discriminative — 判别式学习率

**适用场景**: 需要同时适应所有层，但浅层变化更小

**配置建议**:
```python
FineTuningConfig(
    strategy='discriminative',
    finetune_lr=0.0001,              # 浅层基础学习率
    discriminative_lr_factor=2.5,    # 每深一层 × 2.5
)
# 实际学习率: 浅层 0.0001, 中层 0.00025, 深层 0.000625, 分类头 0.001563
```

**特点**:
- 所有层同时训练，但浅层用小学习率，分类头用大学习率
- 平衡特征保持和任务适应
- 比 gradual 更灵活，但需要更仔细的正则化

### 4. full — 全模型微调

**适用场景**: 领域差距中等偏大，或目标域数据量充足

**配置建议**:
```python
FineTuningConfig(
    strategy='full',
    finetune_lr=0.00005,           # 非常小的学习率
    finetune_weight_decay=5e-4,    # 较强正则化
    finetune_epochs=40,
)
```

**特点**:
- 所有参数都参与训练
- 容易过拟合，需要小学习率 + 强正则化
- 数据量 < 500 条时不推荐

## 策略选择决策树

```
目标域数据量?
├── > 2000 条
│   └── 领域差距?
│       ├── low → head_only 或 gradual
│       └── medium/high → discriminative 或 full
├── 500-2000 条
│   └── gradual (推荐)
└── < 500 条
    └── head_only (仅训练分类头)
```

## 领域适应评估指标

### DomainGapMeasurer 提供的指标

| 指标 | 含义 | 理想值 |
|------|------|--------|
| label_kl_divergence | 标签分布KL散度 | < 0.01 |
| static_feature_shift.mean_shift | 静态特征均值偏移 | < 0.1 |
| sequence_feature_shift.cosine_similarity | 序列特征方向相似度 | > 0.99 |
| domain_gap_score | 综合差距评分 (0~1) | < 0.3 |
| encoder_mmd | 编码器特征空间MMD | < 0.1 |

### 5大联赛之间的差距参考

实测结果 (源=PL+LaLiga, 目标=Bundesliga):
- KL散度: 0.0003 (极低)
- 序列余弦相似度: 0.9981 (极高)
- 静态特征余弦相似度: -0.6126 (标准化后存在方向差异)
- 综合差距: 0.351 (medium)

**结论**: 5大联赛之间迁移学习效果预期较好，推荐 `gradual` 策略。

## API 参考

### 核心类

- `TransferLearningManager` — 迁移学习管理器
  - `pretrain(source_leagues, model_class_name)` — 预训练
  - `finetune(pretrained_model, target_leagues, strategy)` — 微调
  - `pretrain_and_finetune(source_leagues, target_league)` — 端到端

- `LeagueAwareSplitter` — 联赛感知数据分割
  - `filter_by_leagues(league_names)` — 按联赛过滤
  - `split_source_target(source, target)` — 源/目标分割
  - `leave_one_out_split(target_league)` — 留一法

- `LayerFreezer` — 层冻结工具
  - `freeze_encoder(model)` — 冻结编码器
  - `unfreeze_all(model)` — 解冻全部
  - `get_encoder_layer_groups(model)` — 获取层组

- `DiscriminativeLR` — 判别式学习率
  - `build_param_groups(model, base_lr, factor)` — 构建参数组

- `DomainGapMeasurer` — 领域差距度量
  - `measure_domain_gap(source, target, model)` — 全面度量
  - `compute_mmd(source, target)` — MMD距离
  - `compute_label_kl(source, target)` — 标签KL散度

### 便捷函数

```python
from optimize.transfer_learning import transfer_learn, evaluate_transfer

# 一站式迁移
result = transfer_learn(
    source_leagues=['Premier League', 'La Liga'],
    target_league='Bundesliga',
    strategy='gradual',
    model='gru',
)

# 快速评估
gap = evaluate_transfer(
    source_leagues=['Premier League'],
    target_league='Ligue 1',
)
```

## 最佳实践

1. **先评估再迁移**: 使用 `evaluate_transfer()` 评估领域差距，差距 > 0.6 时迁移可能无效
2. **渐进解冻优先**: 5大联赛之间推荐 `gradual` 策略，稳定且效果好
3. **小学习率微调**: 微调学习率通常为预训练的 1/10 到 1/100
4. **关闭增强**: 微调时关闭数据增强 (`use_augmentation_in_finetune=False`)，避免噪声
5. **强正则化**: 微调时使用更高的 weight_decay 和 dropout
6. **对比基线**: 始终包含从零训练的基线，确认迁移是否真的有效
7. **多策略对比**: 使用 `strategies=['head_only', 'gradual', 'discriminative', 'full']` 对比所有策略
8. **留一法评估**: 使用 `leave_one_out_split()` 评估每个联赛的迁移效果
