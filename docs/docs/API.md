# API 端点文档

> 自动生成于 2026-06-03 16:34:07

## 路由列表 (44 个端点)

| 方法 | 路径 | 文件 |
|------|------|------|
| GET | `/` | `main.py` |
| DELETE | `/alerts` | `api\v1\endpoints\alerts.py` |
| GET | `/alerts` | `api\v1\endpoints\alerts.py` |
| GET | `/api/monitor/health` | `main.py` |
| POST | `/auto-promote` | `api\v1\endpoints\models.py` |
| POST | `/batch` | `api\v1\endpoints\predictions.py` |
| GET | `/best` | `api\v1\endpoints\models.py` |
| POST | `/celery` | `api\v1\endpoints\training.py` |
| GET | `/check` | `api\v1\endpoints\data_quality.py` |
| POST | `/check` | `api\v1\endpoints\alerts.py` |
| GET | `/compare` | `api\v1\endpoints\models.py` |
| POST | `/deploy` | `api\v1\endpoints\models.py` |
| GET | `/drift-detection` | `api\v1\endpoints\data_quality.py` |
| GET | `/freshness` | `api\v1\endpoints\data_quality.py` |
| GET | `/health` | `api\v1\endpoints\monitor.py` |
| GET | `/health/live` | `api\v1\endpoints\monitor.py` |
| GET | `/health/ready` | `api\v1\endpoints\monitor.py` |
| GET | `/history` | `api\v1\endpoints\predictions.py` |
| GET | `/history` | `api\v1\endpoints\training.py` |
| GET | `/info` | `api\v1\endpoints\models.py` |
| GET | `/leagues` | `api\v1\endpoints\historical_data.py` |
| POST | `/login` | `api\v1\endpoints\auth.py` |
| GET | `/me` | `api\v1\endpoints\auth.py` |
| GET | `/metrics` | `main.py` |
| GET | `/metrics/summary` | `api\v1\endpoints\monitor.py` |
| GET | `/model-health` | `api\v1\endpoints\monitor.py` |
| GET | `/next-match` | `api\v1\endpoints\predictions.py` |
| POST | `/register` | `api\v1\endpoints\models.py` |
| GET | `/reports` | `api\v1\endpoints\data_quality.py` |
| POST | `/rollback` | `api\v1\endpoints\models.py` |
| GET | `/rules` | `api\v1\endpoints\alerts.py` |
| POST | `/rules` | `api\v1\endpoints\alerts.py` |
| POST | `/single` | `api\v1\endpoints\predictions.py` |
| POST | `/start` | `api\v1\endpoints\training.py` |
| GET | `/stats` | `api\v1\endpoints\predictions.py` |
| GET | `/status` | `api\v1\endpoints\training.py` |
| GET | `/summary` | `api\v1\endpoints\historical_data.py` |
| GET | `/system` | `api\v1\endpoints\monitor.py` |
| GET | `/users` | `api\v1\endpoints\auth.py` |
| GET | `/versions` | `api\v1\endpoints\models.py` |
| GET | `/versions/{model_id}` | `api\v1\endpoints\models.py` |
| GET | `/{league_code}/matches` | `api\v1\endpoints\historical_data.py` |
| GET | `/{league_code}/standings` | `api\v1\endpoints\historical_data.py` |
| GET | `/{league_code}/teams` | `api\v1\endpoints\historical_data.py` |

## 分组导航

### /

- **GET** `/` (main.py)

### /alerts

- **GET** `/alerts` (api\v1\endpoints\alerts.py)
- **DELETE** `/alerts` (api\v1\endpoints\alerts.py)

### /api

- **GET** `/api/monitor/health` (main.py)

### /auto-promote

- **POST** `/auto-promote` (api\v1\endpoints\models.py)

### /batch

- **POST** `/batch` (api\v1\endpoints\predictions.py)

### /best

- **GET** `/best` (api\v1\endpoints\models.py)

### /celery

- **POST** `/celery` (api\v1\endpoints\training.py)

### /check

- **POST** `/check` (api\v1\endpoints\alerts.py)
- **GET** `/check` (api\v1\endpoints\data_quality.py)

### /compare

- **GET** `/compare` (api\v1\endpoints\models.py)

### /deploy

- **POST** `/deploy` (api\v1\endpoints\models.py)

### /drift-detection

- **GET** `/drift-detection` (api\v1\endpoints\data_quality.py)

### /freshness

- **GET** `/freshness` (api\v1\endpoints\data_quality.py)

### /health

- **GET** `/health` (api\v1\endpoints\monitor.py)
- **GET** `/health/ready` (api\v1\endpoints\monitor.py)
- **GET** `/health/live` (api\v1\endpoints\monitor.py)

### /history

- **GET** `/history` (api\v1\endpoints\predictions.py)
- **GET** `/history` (api\v1\endpoints\training.py)

### /info

- **GET** `/info` (api\v1\endpoints\models.py)

### /leagues

- **GET** `/leagues` (api\v1\endpoints\historical_data.py)

### /login

- **POST** `/login` (api\v1\endpoints\auth.py)

### /me

- **GET** `/me` (api\v1\endpoints\auth.py)

### /metrics

- **GET** `/metrics/summary` (api\v1\endpoints\monitor.py)
- **GET** `/metrics` (main.py)

### /model-health

- **GET** `/model-health` (api\v1\endpoints\monitor.py)

### /next-match

- **GET** `/next-match` (api\v1\endpoints\predictions.py)

### /register

- **POST** `/register` (api\v1\endpoints\models.py)

### /reports

- **GET** `/reports` (api\v1\endpoints\data_quality.py)

### /rollback

- **POST** `/rollback` (api\v1\endpoints\models.py)

### /rules

- **GET** `/rules` (api\v1\endpoints\alerts.py)
- **POST** `/rules` (api\v1\endpoints\alerts.py)

### /single

- **POST** `/single` (api\v1\endpoints\predictions.py)

### /start

- **POST** `/start` (api\v1\endpoints\training.py)

### /stats

- **GET** `/stats` (api\v1\endpoints\predictions.py)

### /status

- **GET** `/status` (api\v1\endpoints\training.py)

### /summary

- **GET** `/summary` (api\v1\endpoints\historical_data.py)

### /system

- **GET** `/system` (api\v1\endpoints\monitor.py)

### /users

- **GET** `/users` (api\v1\endpoints\auth.py)

### /versions

- **GET** `/versions` (api\v1\endpoints\models.py)
- **GET** `/versions/{model_id}` (api\v1\endpoints\models.py)

### /{league_code}

- **GET** `/{league_code}/matches` (api\v1\endpoints\historical_data.py)
- **GET** `/{league_code}/standings` (api\v1\endpoints\historical_data.py)
- **GET** `/{league_code}/teams` (api\v1\endpoints\historical_data.py)
