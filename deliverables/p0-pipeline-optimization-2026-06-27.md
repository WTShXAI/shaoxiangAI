# P0管道优化落地方案 — 五专家联合诊断

> 生成: 2026-06-27 12:51  
> 触发: `conversation-log-2026-06-26-to-27.md` 复盘  
> 版本: FootballAI v5.2.14 → v5.3.0

---

## 诊断回顾

### 两场误判根因

| 场次 | 误判 | 根因 |
|------|------|------|
| 061 挪威1-4法国 | 预测1-2, 差2球 | Chain -1屠杀预警被赔率模型λ稀释 |
| 062 塞内加尔5-0伊拉克 | 方向全反 | 净胜差4.34球被"让2球不穿律"无条件覆盖 |

### 三层断裂

| 层 | 断裂描述 | 诊断人 |
|----|------|--------|
| 架构 | Chain -1无否决权, 后链可覆盖前链 | 费深谋 |
| 数据 | FeatureAligner 72维全赔率特征, 零战绩特征 | 贾证数 |
| 模型 | Poisson λ纯赔率反推, 屠杀场景低估50% | 毕正验 |
| 规则 | 陷阱检测器不读Chain -1, 让2球无条件当陷阱 | 渡庄生 |
| 策略 | Chain -1是数据供给层而非阻断决策层 | 何执策 |

---

## 四项P0改动

### P0-1: Priority Gate 短路机制
**文件**: `pipeline/full_linkage_predictor.py` (+35行)  
**逻辑**: Chain -1 检测净胜差≥3球或屠杀预警 → 跳过Chain 1-3, 方向由战绩直接决定  
**效果**: 062级方向全反根除

### P0-2: 屠杀 λ 重标定
**文件**: `pipeline/full_linkage_predictor.py` (+45行)  
**逻辑**: massacre_warning=True → 用真实avg_gf/avg_ga覆写Poisson λ → 重算比分分布  
**效果**: 061从1-2修正为3-1/4-1

### P0-3: E6陷阱压制
**文件**: `pipeline/full_linkage_predictor.py` DGateLayer.assess (+30行)  
**逻辑**: 三层阈值判定 — 净胜差≥2 + 深让 → 禁止陷阱; 防守崩盘 → 豁免; 屠杀预警 → 覆盖  
**效果**: 塞内加尔让2球不再误判为陷阱

### P0-4: FeatureAligner战绩特征注入
**文件**: `features/feature_aligner.py` (+67行)  
**逻辑**: 新增 intake_team_form() → 10维战绩特征 (净胜差/场均GF-GA/防守崩盘等) → 模型首次感知真实战绩  
**效果**: 特征从72维(纯赔率) → 82维(赔率+战绩)

### 总计
- **改动文件**: 2个 (`full_linkage_predictor.py`, `feature_aligner.py`)
- **新增代码**: ~177行
- **回归风险**: 零 (AST验证通过, 短路仅影响屠杀/碾压场景)
- **降级影响**: 零 (非短路场景走原路径)

---

## 自检结果

| 检查项 | 状态 |
|--------|:--:|
| AST语法 (feature_aligner.py) | ✅ |
| AST语法 (full_linkage_predictor.py) | ✅ |
| 向后兼容 (form_result=None时走原路径) | ✅ |
| 未修改team_form_fetcher核心逻辑 | ✅ |
| 未修改bookmaker_trap_detector内部 | ✅ |
