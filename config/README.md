# 配置中心说明

> v5.10 (2026-07-01) — 统一配置层级化

## 配置加载层级

```
优先级: 环境变量 > .env > Pydantic Settings (backend/core/config.py) > YAML fallback
```

## 文件分类

### 核心配置（生产配置源）

| 文件 | 作用 | 状态 |
|------|------|------|
| `backend/core/config.py` | Pydantic Settings 单例 - 所有运行时配置的主入口 | ✅ 活跃 |
| `config/api_config.py` | 外部API端点 + 联赛定义 + 系统参数 | ✅ 活跃 |

### 向后兼容层

| 文件 | 作用 | 状态 |
|------|------|------|
| `config/settings.py` | 代理层 → backend.core.config（点号路径兼容） | ⚠️ DEPRECATED - v5.20移除 |
| `config/config.yaml` | 旧版YAML配置（已被Pydantic取代） | ⚠️ DEPRECATED - 仅保留参考 |
| `config/settings.yaml` | 旧版YAML设置 | ⚠️ DEPRECATED |

### 领域规则数据（JSON/YAML，非配置）

| 文件 | 内容 | 更新频率 |
|------|------|---------|
| `config/drawgate_v53_rules.json` | DrawGate v5.3 阈值和规则参数 | 模型迭代时 |
| `config/tournament_rules.json` | 杯赛/联赛参数（D-Gate赛事分离） | 赛事开始前 |
| `config/jepa_v5.yaml` | JEPA v5 训练超参数 | 模型训练时 |
| `config/benchmarks.yaml` | 模型性能基准 | 评估后 |
| `config/terminology.yaml` | 术语映射表（中英对齐） | 新增术语时 |
| `config/fifa_rankings_2026.json` | FIFA排名数据 | 排名更新后 |
| `config/pre_tournament_form.json` | 赛前状态数据 | 赛季开始时 |
| `config/standings_template.json` | 积分榜模板 | 静态 |
| `config/lessons.json` | 经验教训数据库 | 赛后分析后 |

### 独立模块配置

| 文件 | 作用 | 独立性 |
|------|------|--------|
| `config/hardware_config.py` | 硬件自动检测（GPU/CPU/RAM/磁盘） | 独立 - 无外部依赖 |
| `config/external_data_rules.py` | 数据管道参数（API限流、重试策略） | 独立 - data_collector专用 |

## 使用指南

```python
# ✅ 推荐: 直接从 Pydantic 导入
from backend.core.config import settings
print(settings.HOST, settings.PORT)
print(settings.DRAW_THRESHOLD)

# ⚠️ 向后兼容 (v5.20将移除):
from config.settings import get_setting
threshold = get_setting('prediction.draw_threshold')
```
