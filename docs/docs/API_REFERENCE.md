# 哨响AI API 参考文档 v3.0

> 最后更新: 2026-06-02

---

## 概述

哨响AI 提供 RESTful API 接口，支持模型预测、健康检查、数据查询等功能。
默认服务地址: `http://localhost:8080`

---

## 端点索引

### 预测服务

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/predict/next-match` | 预测下一场比赛 |
| GET | `/api/predict/batch` | 批量预测（本周所有比赛） |
| POST | `/api/predict/match` | 预测指定比赛 |
| GET | `/api/predict/league/{league_id}` | 预测指定联赛所有比赛 |

### 模型管理

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/model/info` | 当前模型信息 |
| GET | `/api/model/versions` | 已注册模型版本列表 |
| POST | `/api/model/deploy` | 部署指定版本 (admin) |
| POST | `/api/model/rollback` | 回滚到上一版本 (admin) |

### 监控与健康

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/monitor/health` | 系统健康检查 |
| GET | `/api/monitor/model-health` | 模型健康检查 |
| GET | `/api/monitor/stats` | 预测统计 (24h/7d/30d) |
| GET | `/api/monitor/data-quality` | 数据质量报告 |
| GET | `/metrics` | Prometheus 指标 |

### 数据查询

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/data/leagues` | 联赛列表 |
| GET | `/api/data/teams` | 球队列表 |
| GET | `/api/data/fixtures` | 比赛时间表 |
| GET | `/api/data/standings/{league}` | 积分榜 |

---

## 详细说明

### `GET /api/predict/next-match`

预测最近一场未开始的比赛。

**响应示例:**
```json
{
  "status": "ok",
  "prediction": {
    "match_id": 450123,
    "home_team": "Manchester United",
    "away_team": "Liverpool",
    "kickoff": "2026-06-03T15:00:00Z",
    "probabilities": {
      "home": 0.42,
      "draw": 0.28,
      "away": 0.30
    },
    "prediction": "H",
    "confidence": 0.42,
    "decision": "WATCH",
    "analysis": {
      "value_gap": 0.03,
      "model_id": "v0012"
    }
  }
}
```

**状态码:**
- `200`: 成功
- `404`: 无待预测比赛
- `500`: 模型错误

---

### `GET /api/monitor/health`

系统健康检查端点（用于 Docker 健康检查、负载均衡器、CI/CD）。

**响应示例:**
```json
{
  "status": "ok",
  "timestamp": "2026-06-02T18:00:00Z",
  "uptime_seconds": 86400,
  "version": "3.1.0",
  "checks": {
    "database": "ok",
    "model_loaded": true,
    "model_id": "v0012",
    "model_accuracy": 45.3,
    "api_connections": 2,
    "disk_free_gb": 42.5,
    "memory_used_pct": 35.2
  }
}
```

---

### `GET /api/monitor/data-quality`

数据质量综合报告。

**响应示例:**
```json
{
  "timestamp": "2026-06-02T18:00:00Z",
  "db_integrity": {
    "passed": true,
    "issues": [],
    "table_stats": {
      "matches": {"row_count": 18218},
      "predictions": {"row_count": 4521}
    }
  },
  "feature_distribution": {
    "passed": true,
    "average_missing_rate": 0.034
  },
  "data_freshness": {
    "matches_last_update_hours": 2.1,
    "predictions_last_update_hours": 0.5
  },
  "data_drift": {
    "drift_detected": false,
    "ks_drifted": 0,
    "psi_drifted": 0
  },
  "health_score": 95
}
```

---

### `POST /api/predict/match`

预测指定比赛（需提供两队名称）。

**请求体:**
```json
{
  "home_team": "Arsenal",
  "away_team": "Chelsea",
  "match_date": "2026-06-03",
  "odds": {
    "home": 2.10,
    "draw": 3.50,
    "away": 3.20
  }
}
```

**响应:** 同 `next-match` 格式

---

### `GET /metrics` (Prometheus)

Prometheus 格式指标导出端点。

可用指标:
- `football_predictions_total` — 预测计数（按联赛/结果）
- `football_prediction_accuracy` — 7日滑动准确率
- `football_prediction_latency_seconds` — 预测延迟
- `football_data_freshness_hours` — 数据新鲜度
- `football_data_quality_score` — 数据质量分数 (0-100)
- `football_model_registry_models` — 注册模型数
- `football_drift_detected` — 数据漂移标志
- `football_calibration_ece` — 校准误差

---

## Python SDK 快速使用

```python
import requests

BASE = "http://localhost:8080"

# 健康检查
r = requests.get(f"{BASE}/api/monitor/health")
print(r.json())

# 获取预测
r = requests.get(f"{BASE}/api/predict/next-match")
pred = r.json()
print(f"预测: {pred['prediction']['prediction']} "
      f"(置信度 {pred['prediction']['confidence']:.1%})")

# 模型信息
r = requests.get(f"{BASE}/api/model/info")
info = r.json()
print(f"模型: {info['model_id']} 准确率: {info['accuracy']}%")
```

---

## 错误码

| 状态码 | 含义 |
|--------|------|
| 200 | 成功 |
| 400 | 请求参数错误 |
| 404 | 资源不存在 |
| 429 | 请求频率限制 |
| 500 | 内部服务器错误 |
| 503 | 服务暂不可用（模型未加载） |
