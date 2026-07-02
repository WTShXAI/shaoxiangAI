"""
哨响AI - 数据采集接口配置文件
支持多个数据源，自动切换
"""
import os
import secrets
import warnings
from dotenv import load_dotenv

load_dotenv()

# 读取 API Key（仅从环境变量，无硬编码回退）
_api_key = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
if not _api_key:
    warnings.warn(
        "FOOTBALL_DATA_API_KEY 未设置！请访问 https://football-data.org/ 注册获取免费 API Key，"
        "并写入 .env 文件中。缺少 Key 将导致数据采集功能不可用。"
    )

API_CONFIG = {
    "primary": {
        "name": "Football-Data.org",
        "base_url": "https://api.football-data.org/v4",
        "api_key": _api_key,
        "rate_limit": 20,
        "endpoints": {
            "matches": "/matches",
            "teams": "/teams",
            "competitions": "/competitions",
            "standings": "/competitions/{code}/standings",
            "schedule": "/competitions/{code}/matches",
            "live": "/matches",
            "odds": "/matches/{match_id}"
        }
    },
    "secondary": {
        "name": "OpenFootballData",
        "base_url": "https://api.openfootball.com",
        "api_key": "",
        "endpoints": {
            "matches": "/matches",
            "stats": "/matches/{match_id}/stats"
        }
    },
    "mock": {
        "name": "MockData",
        "base_url": "http://localhost:9000/mock",
        "endpoints": {
            "matches": "/matches",
            "odds": "/odds"
        }
    }
}

LEAGUES = {
    "premier_league": {
        "id": 2021, "name": "Premier League", "name_cn": "英超",
        "country": "England", "season": "2025/2026", "collect_odds": True
    },
    "la_liga": {
        "id": 2014, "name": "La Liga", "name_cn": "西甲",
        "country": "Spain", "season": "2025/2026", "collect_odds": True
    },
    "serie_a": {
        "id": 2019, "name": "Serie A", "name_cn": "意甲",
        "country": "Italy", "season": "2025/2026", "collect_odds": True
    },
    "bundesliga": {
        "id": 2002, "name": "Bundesliga", "name_cn": "德甲",
        "country": "Germany", "season": "2025/2026", "collect_odds": True
    },
    "ligue_1": {
        "id": 2015, "name": "Ligue 1", "name_cn": "法甲",
        "country": "France", "season": "2025/2026", "collect_odds": True
    },
    "champions_league": {
        "id": 2001, "name": "Champions League", "name_cn": "欧冠",
        "country": "Europe", "season": "2025/2026", "collect_odds": True
    },
    "world_cup": {
        "id": 2000, "name": "World Cup", "name_cn": "世界杯",
        "country": "World", "season": "2026", "collect_odds": False
    },
    "european_championship": {
        "id": 2018, "name": "European Championship", "name_cn": "欧洲杯",
        "country": "Europe", "season": "2024", "collect_odds": False
    },
    "brasileirao": {
        "id": 2013, "name": "Brasileirão", "name_cn": "巴甲",
        "country": "Brazil", "season": "2025", "collect_odds": False
    },
    "eredivisie": {
        "id": 2003, "name": "Eredivisie", "name_cn": "荷甲",
        "country": "Netherlands", "season": "2025/2026", "collect_odds": False
    },
    "primeira_liga": {
        "id": 2017, "name": "Primeira Liga", "name_cn": "葡超",
        "country": "Portugal", "season": "2025/2026", "collect_odds": False
    },
    "championship": {
        "id": 2016, "name": "Championship", "name_cn": "英冠",
        "country": "England", "season": "2025/2026", "collect_odds": False
    }, 
}

# 系统核心参数（第零章参数表）
SYSTEM_PARAMS = {
    'w_high_ball': 0.6,      # 高空球权重 [0.5, 0.8] 赛季级更新
    'beta': 0.3,             # 情绪偏差权重 固定
    'fitness_coeff': 0.3,    # 体能系数 固定
    'sigma_trap_threshold': 0.15,  # 异常波动阈值 [0.1, 0.2] 月度优化
    'alpha_threshold': 0.03, # Alpha价值缺口阈值 3%
    'ev_threshold': 0.02,    # 预期价值阈值 2元/100元
    'max_single_invest': 0.05,  # 单次最大投资占总资金比例 5%
    'half_kelly': True,      # 凯利减半原则
}

# Flask配置
_flask_secret = os.getenv("FLASK_SECRET_KEY", "").strip()
if not _flask_secret:
    _flask_secret = secrets.token_hex(32)
    warnings.warn(
        "FLASK_SECRET_KEY 未设置，已自动生成临时密钥。"
        f"生产环境请在 .env 文件中设置 FLASK_SECRET_KEY={_flask_secret}"
    )

FLASK_CONFIG = {
    'host': '0.0.0.0',
    'port': int(os.getenv("FLASK_PORT", 9000)),
    'debug': os.getenv("FLASK_DEBUG", "false").lower() == "true",
    'secret_key': _flask_secret
}

# ========== 外部服务端点配置（Phase 2A 配置化） ==========
# 所有外部 API/服务端点统一管理，支持环境变量覆盖

def _env_or(key: str, default: str) -> str:
    return os.getenv(key, default).strip()

EXTERNAL_SERVICES = {
    "football_data": {
        "base_url": _env_or("FOOTBALL_DATA_BASE_URL", "https://api.football-data.org/v4"),
        "api_key": _api_key,
    },
    "the_odds": {
        "base_url": _env_or("THE_ODDS_BASE_URL", "https://api.the-odds-api.com/v4"),
        "api_key": _env_or("THE_ODDS_API_KEY", ""),
    },
    "api_football_rapidapi": {
        "base_url": _env_or("RAPIDAPI_FOOTBALL_BASE_URL", "https://api-football-v1.p.rapidapi.com/v3"),
        "api_key": _env_or("RAPIDAPI_KEY", ""),
    },
    "betfair": {
        "api_base": _env_or("BETFAIR_API_BASE", "https://api.betfair.com/exchange/betting/rest/v1.0"),
        "login_url": _env_or("BETFAIR_LOGIN_URL", "https://identitysso.betfair.com/api/login"),
        "keepalive_url": _env_or("BETFAIR_KEEPALIVE_URL", "https://identitysso.betfair.com/api/keepAlive"),
    },
    "open_meteo": {
        "archive_url": _env_or("OPEN_METEO_ARCHIVE_URL", "https://archive-api.open-meteo.com/v1/archive"),
        "forecast_url": _env_or("OPEN_METEO_FORECAST_URL", "https://api.open-meteo.com/v1/forecast"),
    },
    "mlflow": {
        "tracking_uri": _env_or("MLFLOW_TRACKING_URI", "http://localhost:5001"),
    },
    "otel": {
        "endpoint": _env_or("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
    },
    "cors_origins": [
        o.strip() for o in _env_or(
            "CORS_ORIGINS",
            "http://localhost:3000,http://localhost:9000"
        ).split(",") if o.strip()
    ],
}
