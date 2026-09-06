"""
哨响AI - 数据采集接口配置
==========================
"""
import os
import warnings
from dotenv import load_dotenv

load_dotenv()

_api_key = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
if not _api_key:
    warnings.warn(
        "FOOTBALL_DATA_API_KEY 未设置！请在 .env 文件中设置，否则数据采集功能不可用。"
    )

API_CONFIG = {
    "primary": {
        "name": "Football-Data.org",
        "base_url": "https://api.football-data.org/v4",
        "api_key": _api_key,
        "rate_limit": 10,
        "endpoints": {
            "matches": "/matches",
            "teams": "/teams",
            "competitions": "/competitions",
            "standings": "/competitions/{code}/standings",
            "schedule": "/competitions/{code}/matches",
        },
    },
}

EXTERNAL_SERVICES = {
    "betfair": {
        "api_base": os.getenv("BETFAIR_API_BASE", "https://api.betfair.com/exchange/betting/rest/v1.0"),
        "login_url": os.getenv("BETFAIR_LOGIN_URL", "https://identitysso.betfair.com/api/login"),
        "keepalive_url": os.getenv("BETFAIR_KEEPALIVE_URL", "https://identitysso.betfair.com/api/keepAlive"),
    },
    "the_odds": {
        "api_base": os.getenv("THE_ODDS_API_BASE", "https://api.the-odds-api.com/v4"),
    },
}

LEAGUES = {
    "premier_league":    {"id": 2021, "name": "Premier League",       "name_cn": "英超", "country": "England", "season": "2025/2026"},
    "la_liga":           {"id": 2014, "name": "La Liga",              "name_cn": "西甲", "country": "Spain", "season": "2025/2026"},
    "serie_a":           {"id": 2019, "name": "Serie A",              "name_cn": "意甲", "country": "Italy", "season": "2025/2026"},
    "bundesliga":        {"id": 2002, "name": "Bundesliga",           "name_cn": "德甲", "country": "Germany", "season": "2025/2026"},
    "ligue_1":           {"id": 2015, "name": "Ligue 1",              "name_cn": "法甲", "country": "France", "season": "2025/2026"},
    "champions_league":  {"id": 2001, "name": "Champions League",     "name_cn": "欧冠", "country": "Europe", "season": "2025/2026"},
    "world_cup":         {"id": 2000, "name": "World Cup",            "name_cn": "世界杯", "country": "World", "season": "2026"},
    "eredivisie":        {"id": 2003, "name": "Eredivisie",           "name_cn": "荷甲", "country": "Netherlands", "season": "2025/2026"},
    "primeira_liga":     {"id": 2017, "name": "Primeira Liga",        "name_cn": "葡超", "country": "Portugal", "season": "2025/2026"},
    "championship":      {"id": 2016, "name": "Championship",         "name_cn": "英冠", "country": "England", "season": "2025/2026"},
    "brasileirao":       {"id": 2013, "name": "Brasileirão",          "name_cn": "巴甲", "country": "Brazil", "season": "2025"},
}
