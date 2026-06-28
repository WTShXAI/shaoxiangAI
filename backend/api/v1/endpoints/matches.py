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

# 英→中队名映射（用于 API 数据翻译 + 去重匹配）
EN_ZH_TEAM = {
    # 世界杯球队
    'Qatar': '卡塔尔', 'Switzerland': '瑞士', 'United States': '美国',
    'Paraguay': '巴拉圭', 'Brazil': '巴西', 'Morocco': '摩洛哥',
    'Haiti': '海地', 'Scotland': '苏格兰', 'Australia': '澳大利亚',
    'Turkey': '土耳其', 'Germany': '德国', 'Netherlands': '荷兰',
    'Japan': '日本', 'Ivory Coast': '科特迪瓦', 'Ecuador': '厄瓜多尔',
    'Sweden': '瑞典', 'Tunisia': '突尼斯', 'Curaçao': '库拉索',
    'Spain': '西班牙', 'Belgium': '比利时', 'Saudi Arabia': '沙特阿拉伯',
    'Iran': '伊朗', 'New Zealand': '新西兰', 'Egypt': '埃及',
    'France': '法国', 'Argentina': '阿根廷', 'Portugal': '葡萄牙',
    'England': '英格兰', 'Colombia': '哥伦比亚', 'Uruguay': '乌拉圭',
    'Italy': '意大利', 'Croatia': '克罗地亚', 'Denmark': '丹麦',
    'Mexico': '墨西哥', 'Poland': '波兰', 'Serbia': '塞尔维亚',
    'South Korea': '韩国', 'Canada': '加拿大', 'Costa Rica': '哥斯达黎加',
    'Cameroon': '喀麦隆', 'Ghana': '加纳', 'Nigeria': '尼日利亚',
    'Senegal': '塞内加尔', 'Chile': '智利', 'Peru': '秘鲁',
    'Wales': '威尔士', 'Ukraine': '乌克兰', 'Austria': '奥地利',
    'Czech Republic': '捷克', 'Romania': '罗马尼亚', 'Hungary': '匈牙利',
    'Russia': '俄罗斯', 'Norway': '挪威', 'Republic of Ireland': '爱尔兰',
    'Greece': '希腊', 'Switzerland': '瑞士', 'Venezuela': '委内瑞拉',
    'Bolivia': '玻利维亚', 'Paraguay': '巴拉圭', 'Honduras': '洪都拉斯',
    'Jamaica': '牙买加', 'Panama': '巴拿马', 'El Salvador': '萨尔瓦多',
    'Guinea': '几内亚', 'Mali': '马里', 'Algeria': '阿尔及利亚',
    'DR Congo': '民主刚果', 'South Africa': '南非', 'Zimbabwe': '津巴布韦',
    # 五大联赛俱乐部
    'Manchester City': '曼城', 'Manchester United': '曼联',
    'Liverpool FC': '利物浦', 'Liverpool': '利物浦',
    'Chelsea FC': '切尔西', 'Chelsea': '切尔西',
    'Arsenal FC': '阿森纳', 'Arsenal': '阿森纳',
    'Tottenham Hotspur': '热刺', 'Tottenham': '热刺',
    'Newcastle United': '纽卡斯尔', 'Newcastle': '纽卡斯尔',
    'Aston Villa': '阿斯顿维拉', 'West Ham United': '西汉姆',
    'West Ham': '西汉姆', 'Brighton & Hove Albion': '布莱顿',
    'Brighton': '布莱顿', 'Crystal Palace': '水晶宫',
    'Fulham FC': '富勒姆', 'Fulham': '富勒姆',
    'Wolverhampton Wanderers': '狼队', 'Wolverhampton': '狼队',
    'AFC Bournemouth': '伯恩茅斯', 'Bournemouth': '伯恩茅斯',
    'Nottingham Forest': '诺丁汉森林', 'Brentford FC': '布伦特福德',
    'Brentford': '布伦特福德', 'Everton FC': '埃弗顿', 'Everton': '埃弗顿',
    'Leicester City': '莱斯特城', 'Leicester': '莱斯特城',
    'Leeds United': '利兹联', 'Leeds': '利兹联',
    'Burnley FC': '伯恩利', 'Burnley': '伯恩利',
    'Southampton FC': '南安普顿', 'Southampton': '南安普顿',
    'Watford FC': '沃特福德', 'Watford': '沃特福德',
    'Norwich City': '诺维奇', 'Sunderland': '桑德兰',
    'Sheffield United': '谢菲尔德联', 'Luton Town': '卢顿',
    'Real Madrid': '皇家马德里', 'FC Barcelona': '巴塞罗那',
    'Barcelona': '巴塞罗那', 'Atlético Madrid': '马德里竞技',
    'Atletico Madrid': '马德里竞技', 'Sevilla FC': '塞维利亚',
    'Sevilla': '塞维利亚', 'Real Sociedad': '皇家社会',
    'Real Betis': '皇家贝蒂斯', 'Villarreal CF': '比利亚雷亚尔',
    'Villarreal': '比利亚雷亚尔', 'Athletic Club': '毕尔巴鄂竞技',
    'Athletic Bilbao': '毕尔巴鄂竞技', 'Valencia CF': '瓦伦西亚',
    'Valencia': '瓦伦西亚', 'RC Celta': '塞尔塔',
    'Celta de Vigo': '塞尔塔', 'CA Osasuna': '奥萨苏纳',
    'Getafe CF': '赫塔菲', 'Getafe': '赫塔菲',
    'Rayo Vallecano': '巴列卡诺', 'RCD Mallorca': '马洛卡',
    'Mallorca': '马洛卡', 'Girona FC': '赫罗纳',
    'Girona': '赫罗纳', 'UD Almería': '阿尔梅里亚',
    'UD Las Palmas': '拉斯帕尔马斯', 'Cádiz CF': '加的斯',
    'Elche CF': '埃尔切', 'RCD Espanyol': '西班牙人',
    'Espanyol': '西班牙人', 'Deportivo de La Coruña': '拉科鲁尼亚',
    'Deportivo': '拉科鲁尼亚', 'Levante UD': '莱万特',
    'Juventus': '尤文图斯', 'FC Internazionale Milano': '国际米兰',
    'Inter Milan': '国际米兰', 'Inter': '国际米兰',
    'AC Milan': 'AC米兰', 'Milan': 'AC米兰',
    'SSC Napoli': '那不勒斯', 'Napoli': '那不勒斯',
    'AS Roma': '罗马', 'Roma': '罗马',
    'SS Lazio': '拉齐奥', 'Lazio': '拉齐奥',
    'Atalanta BC': '亚特兰大', 'Atalanta': '亚特兰大',
    'ACF Fiorentina': '佛罗伦萨', 'Fiorentina': '佛罗伦萨',
    'Bologna FC': '博洛尼亚', 'Bologna': '博洛尼亚',
    'Torino FC': '都灵', 'Torino': '都灵',
    'US Sassuolo': '萨索洛', 'Sassuolo': '萨索洛',
    'Empoli FC': '恩波利', 'Empoli': '恩波利',
    'Cagliari Calcio': '卡利亚里', 'Cagliari': '卡利亚里',
    'Udinese Calcio': '乌迪内斯', 'Udinese': '乌迪内斯',
    'Hellas Verona': '维罗纳', 'Genoa CFC': '热那亚',
    'Genoa': '热那亚', 'US Cremonese': '克雷莫纳',
    'US Lecce': '莱切', 'Lecce': '莱切',
    'Salernitana': '萨勒尼塔纳', 'Spezia Calcio': '斯佩齐亚',
    'Sampdoria': '桑普多利亚', 'Venezia FC': '威尼斯',
    'Palermo': '巴勒莫', 'FC Bayern München': '拜仁慕尼黑',
    'FC Bayern Munich': '拜仁慕尼黑', 'Bayern': '拜仁慕尼黑',
    'Borussia Dortmund': '多特蒙德', 'Dortmund': '多特蒙德',
    'RB Leipzig': '莱比锡', 'Bayer 04 Leverkusen': '勒沃库森',
    'Leverkusen': '勒沃库森', 'VfB Stuttgart': '斯图加特',
    'Stuttgart': '斯图加特', 'Eintracht Frankfurt': '法兰克福',
    'Frankfurt': '法兰克福', 'SC Freiburg': '弗赖堡',
    'Freiburg': '弗赖堡', 'TSG Hoffenheim': '霍芬海姆',
    'Hoffenheim': '霍芬海姆', 'VfL Wolfsburg': '沃尔夫斯堡',
    'Wolfsburg': '沃尔夫斯堡', 'Borussia Mönchengladbach': '门兴',
    'Mönchengladbach': '门兴', 'FC Augsburg': '奥格斯堡',
    'Augsburg': '奥格斯堡', '1. FSV Mainz 05': '美因茨',
    'Mainz': '美因茨', '1. FC Union Berlin': '柏林联合',
    'Union Berlin': '柏林联合', 'SV Werder Bremen': '不莱梅',
    'Werder Bremen': '不莱梅', 'FC Schalke 04': '沙尔克04',
    'Schalke': '沙尔克04', 'Hertha BSC': '柏林赫塔',
    'Hertha': '柏林赫塔', '1. FC Köln': '科隆',
    'FC Köln': '科隆', 'Hamburger SV': '汉堡',
    'Hamburg': '汉堡', 'VfL Bochum': '波鸿',
    'Bochum': '波鸿', '1. FC Heidenheim': '海登海姆',
    'Heidenheim': '海登海姆', 'SV Darmstadt 98': '达姆施塔特',
    'Darmstadt': '达姆施塔特', 'SC Paderborn': '帕德博恩',
    'Paris Saint-Germain': '巴黎圣日耳曼', 'PSG': '巴黎圣日耳曼',
    'Olympique de Marseille': '马赛', 'Marseille': '马赛',
    'AS Monaco': '摩纳哥', 'Monaco': '摩纳哥',
    'Olympique Lyonnais': '里昂', 'Lyon': '里昂',
    'Olympique Lyon': '里昂', 'LOSC Lille': '里尔',
    'Lille': '里尔', 'Stade Rennais FC': '雷恩',
    'Rennes': '雷恩', 'OGC Nice': '尼斯',
    'Nice': '尼斯', 'RC Lens': '朗斯',
    'Lens': '朗斯', 'FC Nantes': '南特',
    'Nantes': '南特', 'Montpellier HSC': '蒙彼利埃',
    'Montpellier': '蒙彼利埃', 'Stade Brestois 29': '布雷斯特',
    'Brest': '布雷斯特', 'Toulouse FC': '图卢兹',
    'Toulouse': '图卢兹', 'Stade de Reims': '兰斯',
    'Reims': '兰斯', 'FC Lorient': '洛里昂',
    'Lorient': '洛里昂', 'Clermont Foot 63': '克莱蒙',
    'Clermont': '克莱蒙', 'Le Havre AC': '勒阿弗尔',
    'Le Havre': '勒阿弗尔', 'FC Metz': '梅斯',
    'Metz': '梅斯', 'Angers SCO': '昂热',
    'Angers': '昂热', 'ESTAC Troyes': '特鲁瓦',
    'Troyes': '特鲁瓦', 'AJ Auxerre': '欧塞尔',
    'Auxerre': '欧塞尔', 'SM Caen': '卡昂',
    'SC Bastia': '巴斯蒂亚', 'AS Saint-Étienne': '圣埃蒂安',
    'Saint-Étienne': '圣埃蒂安',
    # 反向映射：中→英（用于 DB→API 匹配）
    '卡塔尔': 'Qatar', '瑞士': 'Switzerland', '美国': 'United States',
    '巴拉圭': 'Paraguay', '巴西': 'Brazil', '摩洛哥': 'Morocco',
    '海地': 'Haiti', '苏格兰': 'Scotland', '澳大利亚': 'Australia',
    '土耳其': 'Turkey', '德国': 'Germany', '荷兰': 'Netherlands',
    '日本': 'Japan', '科特迪瓦': 'Ivory Coast', '厄瓜多尔': 'Ecuador',
    '瑞典': 'Sweden', '突尼斯': 'Tunisia', '库拉索': 'Curaçao',
    '西班牙': 'Spain', '比利时': 'Belgium', '沙特阿拉伯': 'Saudi Arabia',
    '伊朗': 'Iran', '新西兰': 'New Zealand', '埃及': 'Egypt',
    '法国': 'France', '阿根廷': 'Argentina', '葡萄牙': 'Portugal',
    '英格兰': 'England', '哥伦比亚': 'Colombia', '乌拉圭': 'Uruguay',
    '意大利': 'Italy', '克罗地亚': 'Croatia', '丹麦': 'Denmark',
    '墨西哥': 'Mexico', '波兰': 'Poland', '塞尔维亚': 'Serbia',
    '韩国': 'South Korea', '加拿大': 'Canada', '智利': 'Chile',
    '秘鲁': 'Peru', '威尔士': 'Wales', '乌克兰': 'Ukraine',
    '奥地利': 'Austria', '挪威': 'Norway', '希腊': 'Greece',
}

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
    home_zh = EN_ZH_TEAM.get(home_en, home_en)
    away_zh = EN_ZH_TEAM.get(away_en, away_en)

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
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """获取比赛列表（含赔率和预测数据）"""
    try:
        db = get_db()
        league_id = None
        if league:
            # 尝试将联赛缩写转为ID
            league_map = {
                'PL': 1, '英超': 1, 'BL': 2, '德甲': 2,
                'SA': 3, '意甲': 3, 'LL': 4, '西甲': 4,
                'L1': 5, '法甲': 5, 'WC': 6, '世界杯': 6,
            }
            league_id = league_map.get(league)

        # 如果 league_id 映射失败，尝试用 league_name 后备查询
        league_name_fallback = None
        if league and league_id is None:
            league_name_fallback = league

        matches = db.get_matches(
            league_id=league_id,
            league_name=league_name_fallback,
            date_from=date_from,
            date_to=date_to,
            status=status,
            limit=limit,
            offset=offset,
        )

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

            item = {
                **m,
                'id': m.get('match_id'),
                'date': m.get('match_date'),
                'time': m.get('match_time'),
                'home_team': m.get('home_team_name'),
                'away_team': m.get('away_team_name'),
                'home_team_zh': m.get('home_team_zh') or m.get('home_team_name'),
                'away_team_zh': m.get('away_team_zh') or m.get('away_team_name'),
                'league': m.get('league_name'),
                'confidence': m.get('confidence') or odds_confidence,
                'prediction': m.get('prediction') or odds_prediction,  # H/D/A (from JOIN or odds)
                'status': m.get('status') or m.get('match_status'),
                # 赔率字段透传（前端显示用）
                'home_odds': m.get('home_odds'),
                'draw_odds': m.get('draw_odds'),
                'away_odds': m.get('away_odds'),
                # 比分字段（终场+半场）
                'home_score': m.get('home_score'),
                'away_score': m.get('away_score'),
                'halftime_home': m.get('halftime_home'),
                'halftime_away': m.get('halftime_away'),
            }
            # 补充推断 prediction（如果 JOIN 未带上且赔率也未推断出）
            if not item.get('prediction') and m.get('home_prob') is not None:
                probs = {'H': m['home_prob'], 'D': m['draw_prob'], 'A': m['away_prob']}
                item['prediction'] = max(probs, key=probs.get)
            normalized.append(item)

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
            key_en = (EN_ZH_TEAM.get(home, home), EN_ZH_TEAM.get(away, away))
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
                "home_team": EN_ZH_TEAM.get(home, home),  # 英文名（如果有映射）
                "away_team": EN_ZH_TEAM.get(away, away),
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
            key_en = (EN_ZH_TEAM.get(home, home), EN_ZH_TEAM.get(away, away))
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
