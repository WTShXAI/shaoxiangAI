"""
比赛数据 API — 比赛列表、实时比分
"""
import logging
from typing import Optional
import os
import requests
import sqlite3
import sqlalchemy
from datetime import date, timedelta
from fastapi import APIRouter, HTTPException, Query, Depends

from api.deps import get_current_user
from database.db_manager import get_db
from config.api_config import API_CONFIG

logger = logging.getLogger(__name__)
router = APIRouter()

# ─── Football-Data.org API 配置 ──────────────────────────────────
FD_API_KEY = API_CONFIG.get("primary", {}).get("api_key", "") or os.getenv("FOOTBALL_DATA_API_KEY", "")
FD_BASE_URL = "https://api.football-data.org/v4"

# ─── 请求缓存 (v5.24 Sprint3) ──────────────────────────────────
from threading import Lock
_cache: dict = {}
_cache_lock = Lock()
CACHE_TTL = 300  # 5分钟

def _cache_key(league, status, limit, offset) -> str:
    return f"{league}|{status}|{limit}|{offset}"

def _cache_get(key: str) -> tuple | None:
    import time
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry['ts'] < CACHE_TTL:
            return entry['data']
        return None

def _cache_set(key: str, data: tuple):
    import time
    with _cache_lock:
        _cache[key] = {'data': data, 'ts': time.time()}
        # 限制缓存条目数
        if len(_cache) > 50:
            oldest = min(_cache, key=lambda k: _cache[k]['ts'])
            del _cache[oldest]

# 联赛中文名映射
LEAGUE_ZH = {
    "Premier League": "英超",
    "La Liga": "西甲",
    "Serie A": "意甲",
    "Bundesliga": "德甲",
    "Ligue 1": "法甲",
    "UEFA Champions League": "欧冠",
    "UEFA Europa League": "欧联",
    "FIFA World Cup": "世界杯",
}

# 比赛状态映射 (API -> 系统)
STATUS_MAP = {
    "SCHEDULED": "scheduled",
    "TIMED": "scheduled",
    "IN_PLAY": "live",
    "PAUSED": "live",
    "FINISHED": "finished",
    "POSTPONED": "postponed",
    "CANCELLED": "cancelled",
    "SUSPENDED": "postponed",
}

def fetch_live_from_api() -> list:
    """从 Football-Data.org API 获取进行中 + 今日的比赛"""
    if not FD_API_KEY:
        logger.warning("FOOTBALL_DATA_API_KEY 未设置，无法获取实时比赛")
        return []

    try:
        headers = {"X-Auth-Token": FD_API_KEY}
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        url = f"{FD_BASE_URL}/matches"
        params = {
            "dateFrom": yesterday,
            "dateTo": tomorrow,
            "limit": 100,
        }
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"API 请求失败: {resp.status_code} {resp.text[:100]}")
            return []

        data = resp.json()
        matches = data.get("matches", [])
        logger.info(f"API 返回 {len(matches)} 场比赛")
        return matches
    except requests.exceptions.Timeout as e:
        logger.warning(f"API 请求超时: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"API 请求失败: {e}")
        return []
    except ValueError as e:
        logger.warning(f"API 响应解析失败: {e}")
        return []
    except (ConnectionError, TimeoutError, sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
        logger.error(f"获取实时比赛时发生未知错误: {e}")
        return []

# 英→中队名映射（从 JSON 配置文件加载，v5.24 工程化）
import json
_EN_ZH_CACHE: dict = {}

def _load_team_names() -> dict:
    """加载队名映射表（含缓存）"""
    global _EN_ZH_CACHE
    if _EN_ZH_CACHE:
        return _EN_ZH_CACHE
    try:
        import os
        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), 'config', 'team_names.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            _EN_ZH_CACHE = json.load(f)
    except Exception:
        # JSON 不可用时回退到内嵌最小映射
        _EN_ZH_CACHE = {
            'Brazil': '巴西', 'Germany': '德国', 'Argentina': '阿根廷',
            'France': '法国', 'Spain': '西班牙', 'England': '英格兰',
            'Italy': '意大利', 'Netherlands': '荷兰', 'Portugal': '葡萄牙',
        }
    return _EN_ZH_CACHE

# 模块加载时初始化映射表
EN_ZH = _load_team_names()

def normalize_api_match(m: dict) -> dict:
    """将 Football-Data.org API 返回的比赛格式转为系统格式"""
    home_team = m.get("homeTeam", {})
    away_team = m.get("awayTeam", {})
    score = m.get("score", {})
    comp = m.get("competition", {})

    # 状态映射
    api_status = m.get("status", "SCHEDULED")
    sys_status = STATUS_MAP.get(api_status, "scheduled")

    # 比分
    full_time = score.get("fullTime", {})
    half_time = score.get("halfTime", {})

    home_score = full_time.get("home")
    away_score = full_time.get("away")
    ht_home = half_time.get("home")
    ht_away = half_time.get("away")

    # 联赛中文名
    league_en = comp.get("name", "")
    league_zh = LEAGUE_ZH.get(league_en, league_en)

    # 队名中文（通过映射表翻译）
    home_en = home_team.get("name", "?")
    away_en = away_team.get("name", "?")
    home_zh = EN_ZH.get(home_en, home_en)
    away_zh = EN_ZH.get(away_en, away_en)

    return {
        "match_id": m.get("id"),
        "id": m.get("id"),
        "date": m.get("utcDate", "")[:10] if m.get("utcDate") else "",
        "time": m.get("utcDate", "")[11:16] if m.get("utcDate") else "",
        "home_team": home_en,
        "away_team": away_en,
        "home_team_name": home_zh,       # 中文队名
        "away_team_name": away_zh,
        "home_team_zh": home_zh,         # 中文队名
        "away_team_zh": away_zh,
        "league": league_zh,
        "league_name": league_zh,
        "home_score": home_score,
        "away_score": away_score,
        "halftime_home": ht_home,
        "halftime_away": ht_away,
        "status": sys_status,
        "prediction": None,
        "tier": None,
    }

@router.get("/list")
async def get_matches(
    league: Optional[str] = None,
    season: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """获取比赛列表（含赔率和预测数据）"""
    try:
        # ─── 缓存检查 (v5.24) ───
        ck = _cache_key(league, status, limit, offset)
        cached = _cache_get(ck)
        if cached:
            return {"matches": cached[0], "total": cached[1], "cached": True}

        db = get_db()
        league_id = None
        if league:
            league_map = {
                'PL': 1, '英超': 1, 'BL': 2, '德甲': 2,
                'SA': 3, '意甲': 3, 'LL': 4, '西甲': 4,
                'L1': 5, '法甲': 5, 'WC': 6, '世界杯': 6,
            }
            league_id = league_map.get(league)

        league_name_fallback = None
        if league and league_id is None:
            league_name_fallback = league

        # status映射: 前端'upcoming'→DB'scheduled', 'live'→'live', 'finished'→'finished'
        if status == 'upcoming':
            status = 'scheduled'

        try:
            matches = db.get_matches(
                league_id=league_id,
                league_name=league_name_fallback,
                date_from=date_from,
                date_to=date_to,
                status=status,
                limit=limit,
                offset=offset,
            )
        except Exception as je:
            # fallback: 简化查询(无JOIN)
            logger.warning(f"get_matches复杂JOIN失败, 使用简化查询: {je}")
            import sqlite3
            with db.get_connection() as conn:
                sql = "SELECT * FROM matches WHERE 1=1"
                params = []
                if status:
                    sql += " AND status=?"
                    params.append(status)
                if date_from:
                    sql += " AND match_date>=?"
                    params.append(date_from)
                sql += " ORDER BY match_date DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                rows = conn.execute(sql, params).fetchall()
                matches = [dict(r) for r in rows]

        # 规范化字段名，对齐前端期望
        normalized = []
        for m in matches:
            # 从赔率推断隐含概率（当模型预测不存在时）
            odds_prediction = None
            odds_confidence = None
            if not m.get('prediction') and not m.get('home_prob'):
                ho, do_, ao = m.get('home_odds'), m.get('draw_odds'), m.get('away_odds')
                if ho and do_ and ao:
                    # 计算隐含概率: 1/odds / sum(1/odds)
                    inv_h, inv_d, inv_a = 1.0 / ho, 1.0 / do_, 1.0 / ao
                    total_inv = inv_h + inv_d + inv_a
                    prob_h = inv_h / total_inv
                    prob_d = inv_d / total_inv
                    prob_a = inv_a / total_inv
                    best = max(('H', prob_h), ('D', prob_d), ('A', prob_a), key=lambda x: x[1])
                    odds_prediction = best[0]
                    odds_confidence = round(best[1] * 100, 1)

            # 状态映射：DB 'scheduled' -> 前端 'upcoming'
            raw_status = m.get('status') or m.get('match_status') or 'scheduled'
            status_map = {
                'scheduled': 'upcoming',
                'live': 'live',
                'finished': 'finished',
                'postponed': 'postponed',
                'cancelled': 'postponed',
            }
            mapped_status = status_map.get(raw_status, 'upcoming')

            # 构建 kickoff ISO 字符串
            match_date = m.get('match_date') or ''
            match_time = m.get('match_time') or '00:00:00'
            if match_time and len(match_time) == 5:  # "HH:MM"
                match_time += ':00'
            kickoff = f"{match_date}T{match_time}" if match_date else ''

            # 联赛代码映射
            league_id = m.get('league_id')
            league_name = m.get('league_name') or '未知联赛'
            league_code_map = {
                1: 'PL', 2: 'BL', 3: 'SA', 4: 'LL', 5: 'L1', 6: 'WC',
            }
            league_code = league_code_map.get(league_id, 'UNKNOWN')

            # 球队简称（取前3字或原名）
            home_name = m.get('home_team_name') or '主队'
            away_name = m.get('away_team_name') or '客队'
            home_short = (m.get('home_team_short') or home_name)[:3]
            away_short = (m.get('away_team_short') or away_name)[:3]

            item = {
                'id': str(m.get('match_id', '')),
                'homeTeam': {
                    'id': str(m.get('home_team_id', '')),
                    'name': home_name,
                    'shortName': home_short,
                    'logo': m.get('home_team_logo'),
                    'rank': m.get('home_team_rank'),
                    'form': m.get('home_form', []),
                },
                'awayTeam': {
                    'id': str(m.get('away_team_id', '')),
                    'name': away_name,
                    'shortName': away_short,
                    'logo': m.get('away_team_logo'),
                    'rank': m.get('away_team_rank'),
                    'form': m.get('away_form', []),
                },
                'league': {
                    'code': league_code,
                    'name': league_name,
                    'country': m.get('league_country') or '未知',
                    'logo': m.get('league_logo'),
                },
                'kickoff': kickoff,
                'status': mapped_status,
                'homeScore': m.get('home_score'),
                'awayScore': m.get('away_score'),
                'venue': m.get('venue'),
                # 赔率 (v5.24: 统一 snake_case)
                'home_odds': m.get('home_odds'),
                'draw_odds': m.get('draw_odds'),
                'away_odds': m.get('away_odds'),
                # 半场比分
                'halftime_home': m.get('halftime_home'),
                'halftime_away': m.get('halftime_away'),
                'confidence': m.get('confidence') or odds_confidence,
                'prediction': m.get('prediction') or odds_prediction,
            }
            # 补充推断 prediction（如果 JOIN 未带上且赔率也未推断出）
            if not item.get('prediction') and m.get('home_prob') is not None:
                probs = {'H': m['home_prob'], 'D': m['draw_prob'], 'A': m['away_prob']}
                item['prediction'] = max(probs, key=probs.get)
            normalized.append(item)

        _cache_set(ck, (normalized, len(normalized)))
        return {"matches": normalized, "total": len(normalized)}
    except ValueError as e:
        logger.error(f"参数错误: {e}")
        raise HTTPException(status_code=400, detail=f"参数错误: {str(e)}")
    except KeyError as e:
        logger.error(f"数据格式错误: {e}")
        raise HTTPException(status_code=500, detail="数据格式错误")
    except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
        logger.error(f"获取比赛列表失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="获取比赛列表失败")

@router.get("/scores")
async def get_live_scores():
    """获取实时比分（进行中 + 今日比赛，从 API 实时拉取，无需认证）"""
    try:
        logger.info("获取实时比分请求")
        # 1. 先从数据库获取已存储的比赛（今日 + 昨日 + 前日，包含 live + finished）
        db = get_db()
        from datetime import date, timedelta
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        day_before = (date.today() - timedelta(days=2)).isoformat()
        recent_matches = db.get_matches(date_from=day_before, date_to=today, limit=200)
        # 包含 live 和 finished 的比赛
        db_matches = [m for m in recent_matches if m.get("status") in ("live", "finished")]
        logger.info(f"数据库中有 {len(db_matches)} 场 live/finished 比赛")

        # 构建 DB 比赛查找索引：用 (主队中文, 客队中文) 做键（忽略日期差异，DB=北京时间，API=UTC）
        db_lookup = {}
        db_team_key_set = set()
        for m in db_matches:
            home = m.get("home_team_name", "")
            away = m.get("away_team_name", "")
            # 同时存中文和英文键
            key_zh = (home, away)
            key_en = (EN_ZH.get(home, home), EN_ZH.get(away, away))
            db_lookup[key_zh] = m
            db_lookup[key_en] = m
            db_team_key_set.add(key_zh)
            db_team_key_set.add(key_en)

        # 2. 从 Football-Data.org API 获取实时比赛
        api_matches = fetch_live_from_api()
        logger.info(f"API 返回 {len(api_matches)} 场比赛")

        # 3. 合并：DB 数据优先（含预测），API 补充缺失比赛
        normalized = []
        seen_team_keys = set()

        # 先加数据库的（保留完整预测信息）
        for m in db_matches:
            home = m.get("home_team_name", "")
            away = m.get("away_team_name", "")
            normalized.append({
                **m,
                "id": m.get("match_id"),
                "date": m.get("match_date"),
                "time": m.get("match_time"),
                "home_team": EN_ZH.get(home, home),  # 英文名（如果有映射）
                "away_team": EN_ZH.get(away, away),
                "home_team_zh": home,
                "away_team_zh": away,
                "league": m.get("league_name"),
                "status": m.get("status"),
                "home_score": m.get("home_score"),
                "away_score": m.get("away_score"),
                "halftime_home": m.get("halftime_home"),
                "halftime_away": m.get("halftime_away"),
            })
            key_zh = (home, away)
            key_en = (EN_ZH.get(home, home), EN_ZH.get(away, away))
            seen_team_keys.add(key_zh)
            seen_team_keys.add(key_en)
            seen_team_keys.add(m.get("match_id"))

        # 再加 API 的（去重：通过队名匹配，忽略日期差异）
        for m in api_matches:
            mid = m.get("id")
            if mid in seen_team_keys:
                continue

            norm = normalize_api_match(m)

            # 用队名做去重（中文键 + 英文键，忽略日期）
            home_zh = norm.get("home_team_zh", "")
            away_zh = norm.get("away_team_zh", "")
            home_en = norm.get("home_team", "")
            away_en = norm.get("away_team", "")
            key_zh = (home_zh, away_zh)
            key_en = (home_en, away_en)

            if key_zh in seen_team_keys or key_en in seen_team_keys:
                # DB 中已有此比赛，跳过 API 版本
                continue

            seen_team_keys.add(key_zh)
            seen_team_keys.add(key_en)
            seen_team_keys.add(mid)

            # 尝试从 DB 查找预测（按队名，忽略日期）
            item = {**norm}
            db_match = db_lookup.get(key_zh) or db_lookup.get(key_en)
            if db_match and db_match.get("prediction"):
                item["prediction"] = db_match.get("prediction")
                item["tier"] = db_match.get("tier")
            elif not item.get("prediction"):
                # 简单用比分差推断
                hs = item.get("home_score")
                aw = item.get("away_score")
                if hs is not None and aw is not None:
                    if hs > aw:
                        item["prediction"] = "H"
                    elif hs < aw:
                        item["prediction"] = "A"
                    else:
                        item["prediction"] = "D"

            normalized.append(item)

        # 按状态排序：live > finished > scheduled，再按时间倒序
        status_order = {"live": 0, "finished": 1, "scheduled": 2}
        normalized.sort(key=lambda x: (
            status_order.get(x.get("status", "scheduled"), 3),
            x.get("date", ""),
            x.get("time", ""),
        ))

        logger.info(f"返回 {len(normalized)} 场比赛")
        return {"matches": normalized, "total": len(normalized)}
    except ValueError as e:
        logger.error(f"数据格式错误: {e}")
        raise HTTPException(status_code=400, detail=f"数据格式错误: {str(e)}")
    except KeyError as e:
        logger.error(f"缺少必要字段: {e}")
        raise HTTPException(status_code=500, detail="数据格式错误")
    except Exception as e:
        logger.error(f"获取实时比分失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取实时比分失败: {str(e)}")
