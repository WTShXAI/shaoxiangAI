# API 参考文档

> 基于实际路由表生成 | FastAPI (v5.0) | 端口 8000 | 前缀 `/api/v1`

---

## 基础信息

| 属性 | 值 |
|------|-----|
| 基础 URL | `http://localhost:8000` |
| API 前缀 | `/api/v1` |
| Swagger UI | `http://localhost:8000/api/v1/docs` |
| OpenAPI JSON | `http://localhost:8000/api/v1/openapi.json` |
| 认证方式 | JWT Bearer Token |
| 产品版本 | v4.1.0 |

---

## 路由总览

| 分组 | 路由文件 | 前缀 |
|------|---------|------|
| 预测 | `backend/api/v1/endpoints/predictions.py` | `/api/v1/predict` |
| 模型管理 | `backend/api/v1/endpoints/models.py` | `/api/v1/models` |
| 训练 | `backend/api/v1/endpoints/training.py` | `/api/v1/training` |
| 比赛数据 | `backend/api/v1/endpoints/matches.py` | `/api/v1/matches` |
| 特征 | `backend/api/v1/endpoints/features.py` | `/api/v1/features` |
| A/B 测试 | `backend/api/v1/endpoints/ab_test.py` | `/api/v1/ab-test` |
| 告警 | `backend/api/v1/endpoints/alerts.py` | `/api/v1/alerts` |
| 历史数据 | `backend/api/v1/endpoints/historical_data.py` | `/api/v1/historical` |
| 评估 | `backend/api/v1/endpoints/evaluation.py` | `/api/v1/evaluation` |
| 管理 | `backend/api/v1/endpoints/admin.py` | `/api/v1/admin` |
| 认证 | `backend/api/v1/endpoints/auth.py` | `/api/v1/auth` |
| 数据质量 | `backend/api/v1/endpoints/data_quality.py` | `/api/v1/data-quality` |
| 监控 | `backend/api/v1/endpoints/monitor.py` | `/api/v1/monitor` |
| 聊天 | `backend/routers/chat.py` | `/api/v1` |
| 赛程 | `backend/routers/fixtures.py` | `/api/v1` |
| 图片预测 | `backend/routers/predict_image.py` | `/api/v1` |
| JEPA | `backend/routers/jepa.py` | `/api/v1` |
| 杂项 | `backend/routers/misc.py` | `/` (根路径) |

---

## 1. 预测 (Predictions)

**前缀**: `/api/v1/predict`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/predict/next-match` | 获取下一场比赛预测 |
| POST | `/api/v1/predict/single` | 单场比赛预测 |
| POST | `/api/v1/predict/batch` | 批量预测 |
| GET | `/api/v1/predict/history` | 预测历史查询 |
| GET | `/api/v1/predict/stats` | 预测统计 |
| POST | `/api/v1/predict/report` | 生成预测报告 |
| POST | `/api/v1/predict/multi` | 多市场预测 |
| POST | `/api/v1/predict/v4` | V4 引擎预测 |
| GET | `/api/v1/predict/v4/health` | V4 引擎健康检查 |
| POST | `/api/v1/predict/v4/backtest` | V4 引擎回测 |

---

## 2. 模型管理 (Models)

**前缀**: `/api/v1/models`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/models/versions` | 列出所有模型版本 |
| GET | `/api/v1/models/versions/{model_id}` | 获取模型详情 |
| POST | `/api/v1/models/deploy` | 部署模型 |
| POST | `/api/v1/models/rollback` | 回滚模型 |
| GET | `/api/v1/models/compare` | 模型版本对比 |
| POST | `/api/v1/models/register` | 注册新模型 |
| GET | `/api/v1/models/best` | 获取最佳模型 |
| GET | `/api/v1/models/info` | 模型信息 |
| POST | `/api/v1/models/auto-promote` | 自动提升模型 |

---

## 3. 训练 (Training)

**前缀**: `/api/v1/training`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/training/start` | 启动训练 |
| GET | `/api/v1/training/status` | 训练状态查询 |
| GET | `/api/v1/training/history` | 训练历史 |
| POST | `/api/v1/training/celery` | Celery 分布式训练 |

---

## 4. 比赛数据 (Matches)

**前缀**: `/api/v1/matches`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/matches/list` | 比赛列表 |
| GET | `/api/v1/matches/scores` | 比赛比分 |

---

## 5. 特征 (Features)

**前缀**: `/api/v1/features`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/features/teams/{team_name}` | 球队特征数据 |
| GET | `/api/v1/features/compute` | 计算特征 |

---

## 6. A/B 测试 (A/B Test)

**前缀**: `/api/v1/ab-test`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/ab-test/tests` | 创建 A/B 测试 |
| GET | `/api/v1/ab-test/tests` | 列出 A/B 测试 |
| GET | `/api/v1/ab-test/tests/{test_name}` | 测试详情 |
| POST | `/api/v1/ab-test/tests/{test_name}/stop` | 停止测试 |
| POST | `/api/v1/ab-test/record` | 记录测试数据 |
| GET | `/api/v1/ab-test/variant` | 获取实验分组 |

---

## 7. 告警 (Alerts)

**前缀**: `/api/v1/alerts`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/alerts/alerts` | 告警列表 |
| GET | `/api/v1/alerts/rules` | 告警规则列表 |
| POST | `/api/v1/alerts/check` | 检查告警条件 |
| POST | `/api/v1/alerts/rules` | 创建告警规则 |
| DELETE | `/api/v1/alerts/alerts` | 清除告警 |

---

## 8. 历史数据 (Historical Data)

**前缀**: `/api/v1/historical`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/historical/leagues` | 联赛列表 |
| GET | `/api/v1/historical/{league_code}/matches` | 联赛比赛数据 |
| GET | `/api/v1/historical/{league_code}/standings` | 联赛积分榜 |
| GET | `/api/v1/historical/{league_code}/teams` | 联赛球队信息 |
| GET | `/api/v1/historical/summary` | 数据摘要 |

---

## 9. 评估 (Evaluation)

**前缀**: `/api/v1/evaluation`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/evaluation/latest` | 最新评估结果 |
| GET | `/api/v1/evaluation/history` | 评估历史 |
| POST | `/api/v1/evaluation/run` | 运行评估 |
| GET | `/api/v1/evaluation/status` | 评估状态 |

---

## 10. 管理 (Admin)

**前缀**: `/api/v1/admin`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/admin/restart` | 重启服务 |
| POST | `/api/v1/admin/clear-cache` | 清除缓存 |

---

## 11. 认证 (Auth)

**前缀**: `/api/v1/auth`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/auth/login` | 用户登录 |
| GET | `/api/v1/auth/me` | 当前用户信息 |
| GET | `/api/v1/auth/users` | 用户列表 |

---

## 12. 数据质量 (Data Quality)

**前缀**: `/api/v1/data-quality`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/data-quality/reports` | 数据质量报告 |
| GET | `/api/v1/data-quality/check` | 数据质量检查 |
| GET | `/api/v1/data-quality/drift-detection` | 数据漂移检测 |
| GET | `/api/v1/data-quality/freshness` | 数据新鲜度 |

---

## 13. 监控 (Monitor)

**前缀**: `/api/v1/monitor`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/monitor/health` | 健康检查 |
| GET | `/api/v1/monitor/health/ready` | 就绪检查 |
| GET | `/api/v1/monitor/health/live` | 存活检查 |
| GET | `/api/v1/monitor/model-health` | 模型健康状态 |
| GET | `/api/v1/monitor/system` | 系统信息 |
| GET | `/api/v1/monitor/metrics/summary` | 指标摘要 |

---

## 14. 聊天 (Chat)

**前缀**: `/api/v1`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/chat` | 智能体对话 |
| GET | `/api/v1/chat/health` | 聊天服务健康检查 |

---

## 15. 赛程 (Fixtures)

**前缀**: `/api/v1`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/fixtures/upcoming` | 即将到来的比赛 |

---

## 16. 图片预测 (Predict Image)

**前缀**: `/api/v1`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/predict/image` | 图片预测（如阵容图 OCR） |

---

## 17. JEPA

**前缀**: `/api/v1`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/v5/predict` | JEPA v5 预测 |
| GET | `/api/v1/v5/health` | JEPA 健康检查 |

---

## 18. 杂项 (Misc)

**前缀**: `/` (根路径)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 根路径 (HTML 欢迎页) |
| GET | `/generate.html` | 生成页面 |
| GET | `/api/monitor/health` | 旧版健康检查路径 |

---

## 19. 其他路径

| 方法 | 路径 | 说明 |
|------|------|------|
| WebSocket | `/ws/health` | WebSocket 健康检查 |
| GET | `/metrics` | Prometheus 指标 |
| GET | `/static/*` | 静态文件服务 |

---

> 本文档基于代码 `backend/api/v1/endpoints/` + `backend/routers/` 实际路由生成。
> 最后更新: 2026-06-28
