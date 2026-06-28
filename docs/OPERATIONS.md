# 哨响AI — 运维手册 v4.0

## 日常运维

### 服务启停

```bash
# Docker Compose
cd footballAI
docker compose -f deployment/docker-compose.yml --profile full up -d    # 启动
docker compose -f deployment/docker-compose.yml down                      # 停止
docker compose -f deployment/docker-compose.yml restart api celery        # 重启核心

# 查看日志
docker compose -f deployment/docker-compose.yml logs -f api
docker compose -f deployment/docker-compose.yml logs -f celery
```

### 健康检查

```bash
# API 健康
curl http://localhost:8000/api/v1/monitor/health

# 就绪检查
curl http://localhost:8000/api/v1/monitor/health/ready

# 存活检查
curl http://localhost:8000/api/v1/monitor/health/live

# 模型健康
curl http://localhost:8000/api/v1/monitor/model-health

# 系统资源
curl http://localhost:8000/api/v1/monitor/system
```

### 模型管理

```bash
# 查看模型列表
curl http://localhost:8000/api/v1/models/versions

# 查看最优模型
curl http://localhost:8000/api/v1/models/best?metric=accuracy

# 版本对比
curl "http://localhost:8000/api/v1/models/compare?model_id_a=v0001&model_id_b=v0002"

# 部署模型
curl -X POST http://localhost:8000/api/v1/models/deploy \
  -H "Content-Type: application/json" \
  -d '{"model_id": "v0003"}'

# 回滚
curl -X POST "http://localhost:8000/api/v1/models/rollback?target_version=v0001"

# 自动晋升
curl -X POST "http://localhost:8000/api/v1/models/auto-promote?min_gain=0.5"
```

### 训练管理

```bash
# 启动训练
curl -X POST http://localhost:8000/api/v1/training/start \
  -H "Content-Type: application/json" \
  -d '{"data_source": "latest", "n_estimators": 1000}'

# 查看训练状态
curl http://localhost:8000/api/v1/training/status

# 查看训练历史
curl http://localhost:8000/api/v1/training/history?limit=10
```

## 监控与告警

### Prometheus 指标

```
# 自定义指标
model_predictions_total{model_version, prediction_type}
model_prediction_accuracy{model_version}
model_prediction_latency_seconds{model_version}
model_prediction_confidence{model_version}
data_quality_score
model_registry_models_count
data_drift_detected
model_calibration_ece
```

### Grafana 仪表盘

1. 访问 http://localhost:3001 (admin/admin)
2. 导入预置仪表盘或手动创建
3. 推荐面板：
   - 模型性能趋势（折线图）
   - 预测延迟分布（热力图）
   - 数据质量评分（仪表盘）
   - 系统资源（柱状图）

### 告警规则配置

```bash
# 查看告警规则
curl http://localhost:8000/api/v1/alerts/rules

# 添加自定义规则
curl -X POST "http://localhost:8000/api/v1/alerts/rules?name=自定义&metric=accuracy&condition=lt&threshold=30&level=error&description=准确率过低"

# 查看最近告警
curl http://localhost:8000/api/v1/alerts/alerts?limit=20&level=error
```

## 数据维护

### 数据采集

```bash
# 手动触发数据采集
python scripts/pull_historical_data.py

# 检查数据新鲜度
curl http://localhost:8000/api/v1/data-quality/freshness

# 数据质量检查
curl http://localhost:8000/api/v1/data-quality/check

# 漂移检测
curl "http://localhost:8000/api/v1/data-quality/drift-detection?window_days=30"
```

### 数据库维护

```bash
# SQLite 维护
sqlite3 data/football_data.db "VACUUM;"
sqlite3 data/football_data.db "ANALYZE;"

# 备份
cp data/football_data.db backups/football_data_$(date +%Y%m%d_%H%M).db
```

## 故障排除

### 常见问题

| 问题 | 诊断 | 解决 |
|------|------|------|
| API 无响应 | `docker ps` 检查容器状态 | `docker restart football-api` |
| 预测返回404 | `curl /models/info` 检查模型 | 训练或注册模型 |
| 训练失败 | `docker logs football-celery` | 检查 Redis 和数据量 |
| 数据过期 | `/data-quality/freshness` | 运行数据采集 |
| 内存不足 | `/monitor/system` | 减少 worker 并发数 |

### 日志分析

```bash
# 查找错误
docker logs football-api 2>&1 | grep -i error | tail -20

# 查找慢请求
docker logs football-api 2>&1 | grep "Process-Time"

# Celery 日志
docker logs football-celery 2>&1 | grep -i failed
```

## A/B测试运维

```bash
# 创建测试
curl -X POST http://localhost:8000/api/v1/ab-test/tests \
  -H "Content-Type: application/json" \
  -d '{"name": "model-v3-vs-v4", "variants": {"control": "v0003", "treatment": "v0004"}, "traffic_split": {"control": 0.5, "treatment": 0.5}}'

# 查看结果
curl http://localhost:8000/api/v1/ab-test/tests/model-v3-vs-v4

# 停止测试
curl -X POST http://localhost:8000/api/v1/ab-test/tests/model-v3-vs-v4/stop
```

## 性能优化

- **Redis 缓存**: 高频查询结果缓存 5 分钟
- **模型预加载**: 启动时加载模型，避免冷启动
- **Celery 并发**: 根据 CPU 核心数调整 `--concurrency`
- **数据库索引**: 在 match_date, team_name 列建立索引
- **Gzip 压缩**: Nginx 对静态资源启用 gzip

## 定期维护清单

### 每日
- [ ] 检查 API 健康状态
- [ ] 检查数据新鲜度
- [ ] 查看告警列表

### 每周
- [ ] 查看模型性能趋势
- [ ] 检查数据质量报告
- [ ] 自动重训练 (CI 定时触发)
- [ ] 清理过期日志

### 每月
- [ ] 数据库 VACUUM + ANALYZE
- [ ] 备份模型注册表
- [ ] 更新依赖包
- [ ] 审查 A/B 测试结果
- [ ] 安全审计
