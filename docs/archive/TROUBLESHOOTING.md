# 哨响AI 故障排除指南

> 最后更新: 2026-06-02

---

## 目录

1. [模型预测问题](#1-模型预测问题)
2. [数据采集问题](#2-数据采集问题)
3. [训练问题](#3-训练问题)
4. [部署问题](#4-部署问题)
5. [性能问题](#5-性能问题)
6. [数据库问题](#6-数据库问题)

---

## 1. 模型预测问题

### 预测结果全是平局 (Draw)

**症状:** 模型几乎所有预测都输出 "D"

**原因:** 
- 校准层过度校正（Isotonic 回归把正确的主/客胜概率压扁）
- 启发式模型输出均匀分布 (1/3, 1/3, 1/3)，稀释信号

**解决方案:**
```bash
# 方案A: 禁用校准 + 去除启发式 (推荐)
python scripts/calibration_fix.py --step 1  # 诊断
python scripts/calibration_fix.py --step 2  # 对比方案
# 选择 no_heu_no_cal 方案

# 方案B: 手动修改 EnsembleTrainer 配置
trainer.config['models']['calibration']['enabled'] = False
trainer.config['models']['ensemble']['heuristic_weight'] = 0.0
```

### 预测准确率突然下降

**症状:** 部署新模型后准确率下降 >3pp

**诊断步骤:**
```bash
# 1. 检查数据漂移
python -c "
from utils.drift_detector import DataDriftDetector
from ensemble_trainer import EnsembleTrainer
trainer = EnsembleTrainer()
df = trainer.load_training_data()
detector = DataDriftDetector(df.head(15000))
print(detector.check_recent_drift().get('overall_drift_detected'))
"

# 2. 对比两个版本
python optimize/model_registry.py compare --id <旧ID> --id2 <新ID>

# 3. 回滚到旧版本
python optimize/model_registry.py rollback
```

### 预测延迟过高 (>100ms)

**原因:** 模型未加载到内存 / 特征计算耗时

**解决方案:**
```python
# 确保模型在应用启动时预加载
from ensemble_trainer import EnsembleTrainer
model = EnsembleTrainer.load("saved_models/production/football_model_v0012.joblib")
# 预热
import numpy as np
model.ensemble_predict_proba(np.zeros((1, 26)))
```

---

## 2. 数据采集问题

### API 返回空数据

**症状:** 数据采集器运行但未获取到数据

**检查步骤:**
```bash
# 1. 检查 API Key
python -c "import os; from dotenv import load_dotenv; load_dotenv(); print(bool(os.getenv('FOOTBALL_DATA_API_KEY')))"
# 期望: True

# 2. 测试 API 连接
python -c "
from data_collector.main import DataCollector
from config.api_config import load_config
dc = DataCollector(load_config())
print(dc.fetch_competitions()[:2])
"

# 3. 检查速率限制
# football-data.org 免费版: 10次/分钟
# 如果超限，等待 60 秒后重试
```

### 历史数据回填中断

**解决方案:**
```bash
# 从断点续传（自动跳过已缓存赛季）
python scripts/pull_historical_data.py --incremental

# 强制全量刷新
python scripts/pull_historical_data.py --force
```

---

## 3. 训练问题

### 训练过程中内存溢出 (OOM)

**症状:** `MemoryError` 或进程被 killed

**解决方案:**
```bash
# 1. 使用子采样快速训练
python scripts/calibration_fix.py --step 1  # 使用5000样本

# 2. 降低 XGBoost 内存占用
trainer.config['models']['xgboost']['n_estimators'] = 200  # 减少到200树
trainer.config['models']['xgboost']['max_depth'] = 3       # 浅树

# 3. 分块加载数据
# 修改 ensemble_trainer.py -> load_training_data -> 添加 LIMIT
```

### 训练速度过慢

**解决方案:**
```bash
# 1. 检查 GPU 是否启用
python -c "from config.hardware_config import get_hardware_config; hw=get_hardware_config(); print(f'GPU: {hw.gpu_available}, Device: {hw.device}')"

# 2. 使用 CPU 多核
export CPU_N_JOBS=-1  # 使用所有核心

# 3. 减少特征数量
# 修改 ensemble_trainer.py -> FEATURE_SUBSET
```

### 准确率不收敛

**诊断:**
```bash
# 检查学习曲线
python scripts/core_evaluation.py --plot-learning-curve

# 检查特征重要性
python -c "
from ensemble_trainer import EnsembleTrainer
trainer = EnsembleTrainer.load('saved_models/latest.joblib')
print(trainer.xgb_model.feature_importances_)
"
```

---

## 4. 部署问题

### Docker 容器无法启动

```bash
# 检查日志
docker logs football-ai-api --tail 50

# 常见原因:
# 1. 端口冲突
netstat -ano | findstr :8080

# 2. 模型文件不存在
docker exec football-ai-api ls /app/saved_models/production/

# 3. 数据库路径错误
docker exec football-ai-api ls /app/data/
```

### 健康检查失败

```bash
# 手动测试
curl -v http://localhost:8080/api/monitor/health

# 常见 502/503:
# - Flask 未完成初始化
# - 数据库连接失败
# - 模型加载超时

# 等待30秒后重试
sleep 30 && curl http://localhost:8080/api/monitor/health
```

### 部署后预测结果异常

```bash
# 即时回滚
python optimize/model_registry.py rollback

# 或手动回滚 Docker
docker compose down
cp docker-compose.yml.backup docker-compose.yml
docker compose up -d
```

---

## 5. 性能问题

### API 响应慢

**优化建议:**
```python
# 1. 启用模型缓存
from prediction_engine import PredictionEngine
engine = PredictionEngine(cache_enabled=True)

# 2. 批量预测代替逐条预测
probas = model.ensemble_predict_proba(X_batch)  # 一次传多场

# 3. 减少特征计算开销
# 使用预计算特征表
```

### 数据库查询慢

```sql
-- 检查索引
PRAGMA index_list('matches');
PRAGMA index_list('match_features');

-- 创建缺失索引
CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(match_date);
CREATE INDEX IF NOT EXISTS idx_features_match ON match_features(match_id);
CREATE INDEX IF NOT EXISTS idx_predictions_created ON predictions(created_at);
```

---

## 6. 数据库问题

### 数据库文件损坏

```bash
# 备份当前数据库
cp data/football_data.db data/football_data.db.bak

# 修复
sqlite3 data/football_data.db "PRAGMA integrity_check;"

# 如果损坏严重:
cp data/football_data.db.bak.backup data/football_data.db
python scripts/backfill_all_data.py --since 2024-01-01
```

### 特征表为空

```bash
# 重新计算特征
python scripts/compute_extra_features.py

# 验证
python -c "
from database.db_manager import DBManager
db = DBManager()
r = db.execute_sql('SELECT COUNT(*) FROM match_features')
print(f'match_features 行数: {r[0][0] if r else 0}')
"
```

---

## 通用诊断命令

```bash
# 一键健康检查
python scripts/comprehensive_health_check.py

# 数据质量检查
python -c "
from database.db_manager import DBManager
from utils.data_quality_checker import DataQualityChecker
checker = DataQualityChecker(DBManager())
result = checker.run_full_check()
print(f'健康分数: {result[\"summary\"][\"health_score\"]}')
for k,v in result['summary'].items():
    print(f'  {k}: {v}')
"

# 模型诊断
python scripts/diagnose.py

# 数据集统计
python -c "
from ensemble_trainer import EnsembleTrainer
trainer = EnsembleTrainer()
df = trainer.load_training_data()
print(f'总计: {len(df)} 场')
print(f'日期范围: {df[\"match_date\"].min()} ~ {df[\"match_date\"].max()}')
print(f'结果分布: {df[\"result\"].value_counts().to_dict()}')
"
```
