# FootballAI D-Gate 系统完整清单

> 更新: 2026-06-22 | 版本: v5.2.2

---

## 一、核心推理链路

```
用户输入(赔率/队名/盘口)
    │
    ▼
UnifiedPredictor (v4.1模型 + λ融合)
    │  ph, pd, pa (模型调整后概率)
    ▼
D-Gate v5.2.2 (平局检测引擎)
    │  verdict: H/D/A
    ▼
cs_other 校验 (可选, 需比分赔率)
    │  否决/确认平局信号
    ▼
最终预测 → 比分推荐 → 风控标签
```

---

## 二、核心文件

### 🧠 推理引擎

| 文件 | 用途 | 调用方式 |
|------|------|----------|
| `predictors/unified_predictor.py` | 主预测器, 加载v4.1 Stacking模型 | `up = UnifiedPredictor(model_path=...)` |
| `rules/d_gate_v52.py` | D-Gate v5.2.2 平局检测引擎 | `from rules.d_gate_v52 import dgate_v52` |
| `rules/d_gate_engine.py` | D-Gate v4.9 生产引擎(兼容旧版) | `from rules.d_gate_engine import apply_dgate` |

### 🧩 ML组件

| 文件 | 用途 | 调用方式 |
|------|------|----------|
| `predictors/components/ensemble_trainer.py` | Stacking集成(LGB+XGB+NN+DE+Heuristic) | 内部由UnifiedPredictor调用 |
| `predictors/components/scripts/train_neural_net.py` | ✅ FootballNN (72→256→128→64→3) | 由ensemble_trainer动态导入 |
| `features/feature_aligner.py` | 统一特征构建器 (72维) | `from features.feature_aligner import FeatureAligner` |

### 📊 模型文件

| 文件 | 用途 |
|------|------|
| `D:/AI/footballAI/saved_models/football_v4.1_production.joblib` | 主模型 (4.57MB, 25 keys) |
| `D:/AI/footballAI/saved_models/football_nn_20260616_125617.pth` | NN子模型 (60,931参数) |

### 🗄️ 数据

| 文件 | 用途 |
|------|------|
| `data/football_data.db` | 主数据库 (507MB) |
| `data/ht_enhanced_training_v6.parquet` | 训练数据 (54MB, 312K场) |
| `data/knowledge_base.db` | OU知识库 (229KB) |
| `data/wc2026_timeline.db` | 世界杯时序数据库 |
| `2026WC/` | 70场世界杯赔率截图 (PNG) |

---

## 三、D-Gate v5.2.2 API

### 主函数

```python
from rules.d_gate_v52 import dgate_v52, COVER_DB, get_s7_threshold

verdict, mode, d_boost, signals = dgate_v52(
    ph,    # float: 模型输出P(H)
    pd,    # float: 模型输出P(D)  
    pa,    # float: 模型输出P(A)
    oh,    # float: 主胜赔率
    od,    # float: 平局赔率
    oa,    # float: 客胜赔率
    hcp,   # float: 让球盘口(主队让球, 负值=主队让)
    ou,    # float: 大小球线
    home='', # str: 主队名(可选, 启用球队风格)
    away=''  # str: 客队名(可选, 启用球队风格)
)

# 返回:
#   verdict: 'H'/'D'/'A'  — 判型结果
#   mode: 'C'/'C-away'/'A'/'B'/'default'/'normal'
#   d_boost: float        — 调整后的d值
#   signals: list[str]    — 触发的信号标签
```

### 五层判型架构

| 层 | 触发条件 | 阈值 | 目标 |
|----|----------|------|------|
| Mode C | max_imp ≥ 70% | 0.14 | 超热门翻车 |
| Mode C-away | pa > 65%, max_imp < 70% | 0.14 | 客场强队翻车 |
| Mode A | 48% ≤ max_imp ≤ 70% | 0.28 | 中等热门 |
| Mode B | spread < 0.15, ou ≤ 2.75 | 0.44 | 均衡赛 |
| Default | 其他 | 0.32 | 标准抑制 |

### S7动态阈值 (v5.2.2)

```python
get_s7_threshold(hcp):
    abs(hcp) ≥ 1.75  → 6.0
    abs(hcp) ≥ 1.0   → 4.5
    abs(hcp) ≥ 0.5   → 3.5  # 主客场统一
    else             → 2.5
```

### 球队风格数据库

```python
from rules.d_gate_v52 import COVER_DB

# COVER_DB[team_name] = {
#     'style': '互捅型'/'稳赢型'/'沉闷型'/'均衡型',
#     'total': n,        # 场次(需≥2场才有风格标签)
#     'gf90': float,     # 每90分钟进球
#     'ga90': float,     # 每90分钟失球
#     'blowout_ratio': float,  # 3+球差比例
#     'draw_ratio': float,     # 平局比例
#     'cover_rate': float,     # 穿盘率
# }
```

### 调整信号清单

| 信号 | 触发条件 | 乘数 | 说明 |
|------|----------|------|------|
| 互捅型 | style=='互捅型' | ×0.85 | 高分互爆, 平局少 |
| 沉闷型 | style=='沉闷型'(≥2场) | ×1.03 | 低分, 平局多 |
| 屠杀率高 | blowout_ratio≥0.5 | ×0.90 | 3+球差频率高 |
| 平局率高 | draw_ratio≥0.5 | ×1.06 | 历史平局多 |
| S7+S1惩罚 | S7≥thresh & S1<1.35 | ×0.70 | OU/HCP异常 |
| 同类赔率-屠杀偏 | blowout_bias | ×0.85 | 历史相似赔率偏屠杀 |
| 同类赔率-平局偏 | draw_bias | ×1.06 | 历史相似赔率偏平局 |
| cs_other < 5 | 屠杀确认 (需赛程压力) | 否决平局 | 庄家极度确定非平 |

---

## 四、分析工具链

### 回测

| 脚本 | 用途 | 命令 |
|------|------|------|
| `pipeline/dgate_v50_backtest.py` | D-Gate v5.1 34场回测 | `python pipeline/dgate_v50_backtest.py` |
| `pipeline/verify_and_deepdive.py` | 34场逐场验证+方向提案 | `python pipeline/verify_and_deepdive.py` |
| `pipeline/test_s7_rollback.py` | S7阈值对照测试 | `python pipeline/test_s7_rollback.py` |

### 预测

| 脚本 | 用途 | 命令 |
|------|------|------|
| `pipeline/analyze_622_tomorrow.py` | 明日比赛分析 | `python pipeline/analyze_622_tomorrow.py` |
| `pipeline/wc2026_full_report.py` | 36场综合预测报告 | `python pipeline/wc2026_full_report.py` |

### 分析

| 脚本 | 用途 | 命令 |
|------|------|------|
| `pipeline/odds_deep_signal_analysis.py` | 7信号深层分析 | `python pipeline/odds_deep_signal_analysis.py` |
| `pipeline/fp_goal_analysis.py` | FP进球数分层 | `python pipeline/fp_goal_analysis.py` |
| `pipeline/group_standings_motivation.py` | 小组积分+战意分析 | `python pipeline/group_standings_motivation.py` |
| `pipeline/team_strength_ranking.py` | Elo排名+模拟 | `python pipeline/team_strength_ranking.py` |

---

## 五、环境

| 组件 | 版本 | 路径 |
|------|------|------|
| Python (主力) | 3.14.5 | `C:/Python314/python.exe` |
| Python (托管) | 3.13.12 | `C:/Users/ShXAI/.workbuddy/binaries/python/versions/3.13.12/python.exe` |
| Node | v22.22.2 | 托管 |
| CUDA | 12.4 | RTX 5070 Ti (16GB) |
| numpy | 2.5.0 / 2.4.6 | 3.13 / 3.14 |
| lightgbm | 4.6.0 | 两者 |
| xgboost | 3.2.0 | 两者 |
| scikit-learn | 1.9.0 | 两者 |
| PyTorch | 2.6.0+cu124 | 3.13 |
| pandas | 3.0.3 | 两者 |
| joblib | 1.5.3 | 两者 |

### 路径变量

```python
ARCH_ROOT = Path('D:/Architecture')
FAI_ROOT  = Path('D:/AI/footballAI')
```

---

## 六、快速调用示例

### 1. 单场预测

```python
from rules.d_gate_v52 import dgate_v52

# 西班牙 vs 沙特 (模型概率)
ph, pd, pa = 0.85, 0.10, 0.05
verdict, mode, d_boost, signals = dgate_v52(
    ph, pd, pa, 1.08, 8.80, 18.0, -2.5, 3.5, '西班牙', '沙特阿拉伯'
)
print(f'判型: {verdict}, 模式: {mode}, 信号: {signals}')
# → 判型: H (Mode C被cs否决), 模式: C (d=0.338>0.14)
```

### 2. 带模型的完整预测

```python
import sys
sys.path.insert(0, 'D:/Architecture')
sys.path.insert(0, 'D:/AI/footballAI')

from predictors.unified_predictor import UnifiedPredictor
from rules.d_gate_v52 import dgate_v52

up = UnifiedPredictor(
    model_path='D:/AI/footballAI/saved_models/football_v4.1_production.joblib',
    enable_trap=False, enable_dh=False, use_threshold=False
)

r = up.predict(home='西班牙', away='沙特阿拉伯',
               odds_h=1.08, odds_d=8.80, odds_a=18.0,
               asian_handicap=-2.5, ou_line=3.5)

probs = r['probabilities']  # {'H': 0.85, 'D': 0.10, 'A': 0.05}
verdict, mode, d, sigs = dgate_v52(
    probs['H'], probs['D'], probs['A'],
    1.08, 8.80, 18.0, -2.5, 3.5, '西班牙', '沙特阿拉伯'
)
```

### 3. 查看球队风格

```python
from rules.d_gate_v52 import COVER_DB
team = COVER_DB.get('荷兰', {})
print(f"风格: {team.get('style')}, GF90: {team.get('gf90'):.1f}")
```

### 4. S7阈值查询

```python
from rules.d_gate_v52 import get_s7_threshold
print(get_s7_threshold(-0.75))  # → 3.5
print(get_s7_threshold(0.75))   # → 3.5 (v5.2.2主客场统一)
print(get_s7_threshold(-2.5))   # → 6.0
```

---

## 七、版本演进

| 版本 | 日期 | Acc(34场) | D-F1 | 核心改进 |
|------|------|-----------|------|----------|
| v5.1 | 6.20 | 20/34 | 0.606 | Mode C反转 + 五层架构 |
| v5.2 | 6.21 | 23/34 | 0.667 | +互捅型+同类赔率+S7动态 |
| **v5.2.2** | **6.22** | **27/34** | **0.769** | +S1宽松+B阈值+S7回滚 |

### v5.2.2 变更清单

| 变更 | 旧值 | 新值 | 影响 |
|------|------|------|------|
| S1宽松 (Mode A/Default) | 1.30 | 1.35 | -2FP |
| Mode B 阈值 | 0.43 | 0.44 | -1FP |
| S7客场阈值 | 3.0 | 3.5 (统一) | 回滚, 无净影响 |
| 风格标签 | 单场即可 | ≥2场 | 防假标签 |
