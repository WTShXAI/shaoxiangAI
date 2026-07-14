"""
哨响AI - 数据采集模块
仅使用 Football-Data.org API（真实数据）
⛔ 死命令：模拟数据功能已永久禁用
包含: 比赛、球队、积分榜、赛程、实时比分、竞赛列表、API测试
v2.1: 移除模拟数据，全部走真实API
"""
import requests
import logging
import time as time_module
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, timedelta

# ⛔ 死命令 — 禁止模拟数据
from database.data_integrity import block_mock_data, DataIntegrityError

logger = logging.getLogger(__name__)

class APICache:
    """简单的内存缓存，带TTL过期机制"""

    def __init__(self, default_ttl: int = 300):
        self._cache: Dict[str, tuple] = {}  # key → (data, expire_timestamp)
        self.default_ttl = default_ttl

    def get(self, key: str):
        """获取缓存，过期返回None"""
        entry = self._cache.get(key)
        if entry is None:
            return None
        data, expires = entry
        if time_module.time() > expires:
            del self._cache[key]
            return None
        return data

    def set(self, key: str, data, ttl: Optional[int] = None):
        """设置缓存"""
        ttl = ttl if ttl is not None else self.default_ttl
        self._cache[key] = (data, time_module.time() + ttl)

    def clear(self):
        self._cache.clear()

    def stats(self) -> dict:
        """缓存统计"""
        now = time_module.time()
        active = sum(1 for _, (_, exp) in self._cache.items() if exp > now)
        return {"total_entries": len(self._cache), "active_entries": active}

class FootballDataCollector:
    """Football-Data.org 数据采集器 v2.0"""

    # 联赛代码映射（API端点用）
    LEAGUE_CODES = {
        2021: "PL",   # 英超
        2014: "PD",   # 西甲
        2019: "SA",   # 意甲
        2002: "BL1",  # 德甲
        2015: "FL1",  # 法甲
        2001: "CL",   # 欧冠
        2000: "WC",   # 世界杯
        2018: "EC",   # 欧洲杯
        2013: "BSA",  # 巴甲
        2003: "DED",  # 荷甲
        2017: "PPL",  # 葡超
        2016: "ELC",  # 英冠
        2006: "MLS",  # 美职联
        2007: "CSL",  # 中超
    }

    def __init__(self, api_key: str, base_url: str = "https://api.football-data.org/v4"):
        self.api_key = api_key
        self.base_url = base_url
        self.headers = {"X-Auth-Token": api_key} if api_key else {}
        # 请求统计
        self.request_count = 0
        self.last_request_time: Optional[datetime] = None
        self.last_response_status: Optional[int] = None
        # 缓存
        self.cache = APICache(default_ttl=300)  # 默认缓存5分钟
        # 速率限制（免费版: 10次/分钟）
        self.rate_limit_per_minute = 10
        self._minute_request_times: List[float] = []

    def _check_rate_limit(self):
        """检查并等待速率限制"""
        now = time_module.time()
        # 清理超过1分钟的记录
        self._minute_request_times = [
            t for t in self._minute_request_times if now - t < 60
        ]
        if len(self._minute_request_times) >= self.rate_limit_per_minute:
            wait_time = 60 - (now - self._minute_request_times[0]) + 1
            if wait_time > 0:
                logger.info(f"速率限制等待 {wait_time:.1f}s...")
                time_module.sleep(wait_time)

    def _record_request(self):
        """记录一次API请求"""
        self._minute_request_times.append(time_module.time())
        self.request_count += 1
        self.last_request_time = datetime.now(timezone.utc)

    def _api_call(self, endpoint: str, params: Optional[Dict] = None, use_cache: bool = False,
                  cache_ttl: Optional[int] = None, max_retries: int = 3) -> Optional[dict]:
        """统一 API 调用封装（带缓存支持和 429 指数退避重试）"""
        if not self.api_key:
            logger.warning("未配置API Key，无法调用API")
            return None

        # 检查缓存
        cache_key = None
        if use_cache:
            cache_key = f"{endpoint}:{str(params or {})}"
            cached = self.cache.get(cache_key)
            if cached is not None:
                logger.debug(f"缓存命中: {endpoint}")
                return cached

        for attempt in range(max_retries + 1):
            # 速率限制（首次请求前检查，重试时跳过以尊重 backoff）
            if attempt == 0:
                self._check_rate_limit()

            try:
                url = f"{self.base_url}{endpoint}"
                resp = requests.get(url, headers=self.headers, params=params or {}, timeout=15)
                self._record_request()
                self.last_response_status = resp.status_code

                if resp.status_code == 200:
                    data = resp.json()
                    # 写入缓存
                    if use_cache and cache_key:
                        self.cache.set(cache_key, data, ttl=cache_ttl)
                    return data
                elif resp.status_code == 429:
                    if attempt < max_retries:
                        wait = 2 ** attempt
                        logger.warning(
                            f"API 429 限流: {endpoint}，第 {attempt+1} 次重试，等待 {wait}s..."
                        )
                        time_module.sleep(wait)
                        continue
                    else:
                        logger.error(
                            f"API 429 限流: {endpoint}，已达最大重试次数 ({max_retries})"
                        )
                        return None
                elif resp.status_code == 403:
                    logger.error(f"API 403 禁止访问: {endpoint}，请检查API Key是否有效")
                    return None
                else:
                    logger.warning(f"API {resp.status_code}: {endpoint}")
                    return None
            except requests.exceptions.Timeout:
                logger.error(f"API请求超时: {endpoint}")
                return None
            except (Exception, requests.exceptions.RequestException) as e:
                logger.error(f"API请求异常 {endpoint}: {e}")
                return None

        return None

    def get_api_status(self) -> dict:
        """获取API连接状态和统计"""
        return {
            "api_key_configured": bool(self.api_key),
            "base_url": self.base_url,
            "total_requests": self.request_count,
            "last_request_time": self.last_request_time.isoformat() if self.last_request_time else None,
            "last_response_status": self.last_response_status,
            "minute_requests": len(self._minute_request_times),
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "rate_limit_remaining": max(0, self.rate_limit_per_minute - len(self._minute_request_times)),
            "cache_stats": self.cache.stats(),
        }

    def test_connection(self) -> dict:
        """测试API连接是否正常（轻量级请求）"""
        if not self.api_key:
            return {"success": False, "error": "API Key 未配置", "hint": "请访问 https://football-data.org 注册获取免费Key"}
        try:
            data = self._api_call("/competitions/PL", use_cache=True, cache_ttl=600)
            if data:
                comp_name = data.get("name", "unknown")
                return {
                    "success": True,
                    "message": f"API连接正常 ✓ 测试联赛: {comp_name}",
                    "competition_name": comp_name,
                    "rate_limit_remaining": max(0, self.rate_limit_per_minute - len(self._minute_request_times)),
                }
            return {"success": False, "error": "API返回空数据", "hint": "请检查API Key是否有效"}
        except (Exception, requests.exceptions.RequestException) as e:
            return {"success": False, "error": str(e), "hint": "请检查网络连接和API Key"}

    def get_competitions(self, use_cache: bool = True) -> List[Dict]:
        """获取所有可用联赛/竞赛列表"""
        data = self._api_call("/competitions", use_cache=use_cache, cache_ttl=3600)
        if not data:
            return []

        competitions = []
        for c in data.get("competitions", []):
            area = c.get("area", {})
            current_season = c.get("currentSeason", {})
            competitions.append({
                "id": c.get("id"),
                "name": c.get("name", ""),
                "code": c.get("code", ""),
                "type": c.get("type", ""),
                "emblem": c.get("emblemUrl") or c.get("emblem", ""),
                "area_name": area.get("name", ""),
                "area_flag": area.get("ensignUrl", ""),
                "current_season_start": current_season.get("startDate", ""),
                "current_season_end": current_season.get("endDate", ""),
                "current_matchday": current_season.get("currentMatchday"),
                "last_updated": c.get("lastUpdated", ""),
            })
        logger.info(f"获取到 {len(competitions)} 个联赛/竞赛")
        return competitions

    def get_matches(self, league_id: int, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, use_cache: bool = False) -> List[Dict]:
        """获取比赛数据"""
        if not self.api_key:
            logger.warning("未配置API Key，无法获取比赛数据")
            return []

        try:
            params: Dict[str, Any] = {"competitions": league_id}
            if date_from:
                params["dateFrom"] = date_from
            if date_to:
                params["dateTo"] = date_to

            # 比赛数据不使用长期缓存（实时性要求高），短期缓存60秒
            data = self._api_call("/matches", params=params, use_cache=use_cache, cache_ttl=60)
            if not data:
                return []

            matches = []
            for m in data.get('matches', []):
                utc_date = m.get('utcDate', '')
                competition = m.get('competition', {})
                matches.append({
                    'match_id': m.get('id'),
                    'match_date': utc_date[:10] if utc_date else '',
                    'match_time': utc_date,
                    'league_id': league_id,
                    'league_name': competition.get('name', ''),
                    'league_code': competition.get('code', ''),
                    'home_team_id': m.get('homeTeam', {}).get('id'),
                    'home_team_name': m.get('homeTeam', {}).get('shortName',
                                                                 m.get('homeTeam', {}).get('name', '')),
                    'away_team_id': m.get('awayTeam', {}).get('id'),
                    'away_team_name': m.get('awayTeam', {}).get('shortName',
                                                                 m.get('awayTeam', {}).get('name', '')),
                    'status': 'finished' if m.get('status') == 'FINISHED' else 'scheduled',
                    'home_score': m.get('score', {}).get('fullTime', {}).get('home'),
                    'away_score': m.get('score', {}).get('fullTime', {}).get('away'),
                    'matchday': m.get('matchday'),
                    'stage': m.get('stage', 'REGULAR_SEASON'),
                    'venue': m.get('venue', ''),
                    'referee_name': (
                        (m.get('referees') or [{}])[0].get('name', '')
                        if m.get('referees') else ''
                    ),
                })
            logger.info(f"获取到{len(matches)}场比赛")
            return matches

        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            logger.error(f"获取比赛失败: {e}")
            return []

    def get_teams(self, league_id: int, use_cache: bool = True) -> List[Dict]:
        """获取联赛球队"""
        if not self.api_key:
            logger.warning("未配置API Key，无法获取球队数据")
            return []

        try:
            data = self._api_call(
                f"/competitions/{league_id}/teams",
                use_cache=use_cache, cache_ttl=3600
            )
            if not data:
                return []

            teams = []
            for t in data.get('teams', []):
                area = t.get('area', {})
                coach = t.get('coach', {})
                teams.append({
                    'team_id': t.get('id'),
                    'team_name': t.get('shortName', t.get('name', '')),
                    'team_full_name': t.get('name', ''),
                    'team_code': t.get('tla', ''),
                    'country': area.get('name', ''),
                    'league_id': league_id,
                    'founded': t.get('founded'),
                    'venue': t.get('venue', ''),
                    'club_colors': t.get('clubColors', ''),
                    'crest_url': t.get('crest', ''),
                    'coach_name': coach.get('name', ''),
                    'squad_size': len(t.get('squad', [])),
                })
            logger.info(f"获取球队: league_id={league_id} → {len(teams)} 支")
            return teams

        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            logger.error(f"获取球队失败: {e}")
            return []

    # ===================== 新增: 积分榜 =====================

    def get_standings(self, league_id: int, season: Optional[int] = None) -> List[Dict]:
        """
        获取联赛积分榜数据

        API: GET /competitions/{code}/standings
        支持 season 参数（当前赛季仅提供最新）
        """
        league_code = self.LEAGUE_CODES.get(league_id, str(league_id))
        endpoint = f"/competitions/{league_code}/standings"
        params = {}
        if season:
            params["season"] = season

        data = self._api_call(endpoint, params)
        if not data:
            logger.warning(f"无法获取积分榜数据: {league_code}，API不可用")
            return []

        standings = []
        try:
            # football-data.org v4 返回格式: {"standings": [{"table": [...]}]}
            tables = data.get("standings", [])
            for table_group in tables:
                for row in table_group.get("table", []):
                    team_info = row.get("team", {})
                    standings.append({
                        "league_id": league_id,
                        "league_name": data.get("competition", {}).get("name", ""),
                        "season": season or data.get("season", {}).get("id", 2024),
                        "team_name": team_info.get("shortName") or team_info.get("name", ""),
                        "position": row.get("position"),
                        "played_games": row.get("playedGames", 0),
                        "wins": row.get("won", 0),
                        "draws": row.get("draw", 0),
                        "losses": row.get("lost", 0),
                        "goals_for": row.get("goalsFor", 0),
                        "goals_against": row.get("goalsAgainst", 0),
                        "goal_diff": row.get("goalDifference", 0),
                        "points": row.get("points", 0),
                        "form": row.get("form", ""),
                    })
        except (Exception, requests.exceptions.RequestException) as e:
            logger.error(f"解析积分榜失败: {e}")
            return []

        logger.info(f"获取积分榜: {league_code} → {len(standings)} 支球队 (赛季{season})")
        return standings

    # ===================== 新增: 赛程/时间表 =====================

    def get_schedules(self, league_id: int, date_from: str, date_to: str) -> List[Dict]:
        """
        获取联赛赛程（含确切开赛时间）

        API: GET /matches?competitions={id}&dateFrom=X&dateTo=Y&status=SCHEDULED
        """
        params = {
            "competitions": league_id,
            "dateFrom": date_from,
            "dateTo": date_to,
            "status": "SCHEDULED",
        }
        data = self._api_call("/matches", params)
        if not data:
            return []

        schedules = []
        for m in data.get("matches", []):
            home = m.get("homeTeam", {})
            away = m.get("awayTeam", {})
            competition = m.get("competition", {})
            schedules.append({
                "match_id": m.get("id"),
                "match_date": m.get("utcDate", "")[:10],
                "kickoff_time": m.get("utcDate", ""),
                "league_id": league_id,
                "league_name": competition.get("name", ""),
                "home_team_name": home.get("shortName") or home.get("name", ""),
                "away_team_name": away.get("shortName") or away.get("name", ""),
                "status": m.get("status", "SCHEDULED"),
                "matchday": m.get("matchday"),
                "venue": m.get("venue", ""),
                "stage": m.get("stage", "REGULAR_SEASON"),
            })
        logger.info(f"获取赛程: {len(schedules)} 场")
        return schedules

    # ===================== 新增: 实时比分 =====================

    def get_live_scores(self, league_id: Optional[int] = None) -> List[Dict]:
        """
        获取实时比分 / 进行中的比赛

        API: GET /matches?status=LIVE (可选 filtered by competitions)
        """
        params: Dict[str, Any] = {"status": "LIVE"}
        if league_id:
            params["competitions"] = league_id
        data = self._api_call("/matches", params)
        if not data:
            return []

        live_matches = []
        for m in data.get("matches", []):
            home = m.get("homeTeam", {})
            away = m.get("awayTeam", {})
            score = m.get("score", {})
            competition = m.get("competition", {})

            ft = score.get("fullTime", {})
            ht = score.get("halfTime", {})
            live_matches.append({
                "match_id": m.get("id"),
                "match_date": m.get("utcDate", "")[:10],
                "kickoff_time": m.get("utcDate", ""),
                "league_id": competition.get("id") if league_id is None else league_id,
                "league_name": competition.get("name", ""),
                "home_team_name": home.get("shortName") or home.get("name", ""),
                "away_team_name": away.get("shortName") or away.get("name", ""),
                "home_score": ft.get("home"),
                "away_score": ft.get("away"),
                "ht_home_score": ht.get("home"),
                "ht_away_score": ht.get("away"),
                "status": m.get("status", "LIVE"),
                "minute": m.get("minute", "N/A"),
                "matchday": m.get("matchday"),
            })
        logger.info(f"实时比分: {len(live_matches)} 场比赛进行中")
        return live_matches

    # ===================== 增量数据获取 =====================

    def fetch_matches_since(self, league_id: int, since_date: str,
                            include_scheduled: bool = True) -> List[Dict]:
        """
        增量拉取：获取指定日期之后的所有比赛。

        免费版 API 限制 dateFrom/dateTo 范围 ≤ 10 天，自动分片请求。

        Args:
            league_id: 联赛ID
            since_date: 起始日期 (YYYY-MM-DD)
            include_scheduled: 是否包含未开始的比赛

        Returns:
            比赛列表，包含 finished 和 scheduled 状态的比赛
        """
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        since_dt = datetime.strptime(since_date, '%Y-%m-%d')
        today_dt = datetime.strptime(today, '%Y-%m-%d')

        all_matches: List[Dict] = []
        chunk_start = since_dt

        # 每次最多拉取 10 天（免费版上限）
        while chunk_start <= today_dt:
            chunk_end = min(chunk_start + timedelta(days=9), today_dt)
            cs = chunk_start.strftime('%Y-%m-%d')
            ce = chunk_end.strftime('%Y-%m-%d')

            params = {"competitions": league_id, "dateFrom": cs, "dateTo": ce}
            data = self._api_call("/matches", params=params)
            if data:
                for m in data.get('matches', []):
                    utc_date = m.get('utcDate', '')
                    status = m.get('status', '')
                    competition = m.get('competition', {})

                    if status == 'FINISHED':
                        match_status = 'finished'
                    elif status in ('LIVE', 'IN_PLAY', 'PAUSED'):
                        match_status = 'live'
                    elif status in ('SCHEDULED', 'TIMED'):
                        if not include_scheduled:
                            continue
                        match_status = 'scheduled'
                    else:
                        match_status = 'scheduled'

                    score = m.get('score', {}).get('fullTime', {})
                    ht_score = m.get('score', {}).get('halfTime', {})
                    match_entry = {
                        'match_id': m.get('id'),
                        'match_date': utc_date[:10] if utc_date else '',
                        'match_time': utc_date,
                        'league_id': league_id,
                        'league_name': competition.get('name', ''),
                        'league_code': competition.get('code', ''),
                        'home_team_id': m.get('homeTeam', {}).get('id'),
                        'home_team_name': m.get('homeTeam', {}).get('shortName',
                                                                      m.get('homeTeam', {}).get('name', '')),
                        'away_team_id': m.get('awayTeam', {}).get('id'),
                        'away_team_name': m.get('awayTeam', {}).get('shortName',
                                                                      m.get('awayTeam', {}).get('name', '')),
                        'status': match_status,
                        'home_score': score.get('home'),
                        'away_score': score.get('away'),
                        'halftime_home': ht_score.get('home'),
                        'halftime_away': ht_score.get('away'),
                        'minute': m.get('minute'),
                        'matchday': m.get('matchday'),
                        'stage': m.get('stage', 'REGULAR_SEASON'),
                        'venue': m.get('venue', ''),
                    }
                    # 去重（同一 match_id 只保留一次）
                    if not any(mm['match_id'] == match_entry['match_id'] for mm in all_matches):
                        all_matches.append(match_entry)

            # v4: dateTo 不包含当天，下一片从 chunk_end+1 开始（dateFrom 是包含的）
            # 当 chunk 仅 1 天范围时，+1 确保时间窗口始终向前推进
            chunk_start = chunk_end + timedelta(days=1)

        logger.info(f"增量拉取 [{league_id}]: {since_date}~{today} → {len(all_matches)} 场比赛 "
                     f"({(today_dt - since_dt).days}天窗口)")
        return all_matches

    def fetch_current_season_matches(self, league_id: int, league_code: str,
                                     season: Optional[int] = None) -> List[Dict]:
        """
        拉取当前赛季的所有比赛（用于初次增量初始化）。

        Args:
            league_id: 联赛ID
            league_code: 联赛代码 (PL, PD, SA, BL1, FL1)
            season: 赛季起始年份，默认为当前年份

        Returns:
            比赛列表
        """
        if season is None:
            season = datetime.now(timezone.utc).year

        endpoint = f"/competitions/{league_code}/matches"
        params = {"season": season}
        data = self._api_call(endpoint, params=params)
        if not data:
            logger.warning(f"获取 {league_code} 赛季 {season} 数据失败")
            return []

        matches = []
        for m in data.get('matches', []):
            utc_date = m.get('utcDate', '')
            status = m.get('status', '')
            competition = m.get('competition', {})
            score = m.get('score', {}).get('fullTime', {})
            ht_score = m.get('score', {}).get('halfTime', {})

            if status == 'FINISHED':
                match_status = 'finished'
            elif status in ('LIVE', 'IN_PLAY', 'PAUSED'):
                match_status = 'live'
            else:
                match_status = 'scheduled'

            matches.append({
                'match_id': m.get('id'),
                'match_date': utc_date[:10] if utc_date else '',
                'match_time': utc_date,
                'league_id': league_id,
                'league_name': competition.get('name', ''),
                'league_code': competition.get('code', ''),
                'home_team_id': m.get('homeTeam', {}).get('id'),
                'home_team_name': m.get('homeTeam', {}).get('shortName',
                                                              m.get('homeTeam', {}).get('name', '')),
                'away_team_id': m.get('awayTeam', {}).get('id'),
                'away_team_name': m.get('awayTeam', {}).get('shortName',
                                                              m.get('awayTeam', {}).get('name', '')),
                'status': match_status,
                'home_score': score.get('home'),
                'away_score': score.get('away'),
                'halftime_home': ht_score.get('home'),
                'halftime_away': ht_score.get('away'),
                'minute': m.get('minute'),
                'matchday': m.get('matchday'),
                'stage': m.get('stage', 'REGULAR_SEASON'),
                'season': season,
            })

        finished = sum(1 for m in matches if m['status'] == 'finished')
        scheduled = sum(1 for m in matches if m['status'] == 'scheduled')
        logger.info(f"当前赛季 [{league_code} {season}/{season+1}]: "
                     f"{len(matches)} 场 (已完成{finished}, 待比赛{scheduled})")
        return matches

    # ===================== 新增: 比赛赔率 =====================

    def get_odds(self, match_id: int) -> Optional[Dict]:
        """
        获取单场比赛的赔率数据

        v4: GET /matches/{match_id} 返回的 match 对象含 odds 子字段
            {"odds": {"homeWin": 1.95, "draw": 3.50, "awayWin": 4.20}}

        也兼容旧版 /matches/{match_id}/odds 端点格式

        Returns:
            {'home_odds': 1.95, 'draw_odds': 3.50, 'away_odds': 4.20} 或 None
        """
        # v4 主方式：从 match 详情中取 odds 子字段
        endpoint = f"/matches/{match_id}"
        data = self._api_call(endpoint)
        if not data:
            return None

        odds_raw = data.get('odds', {})
        if not odds_raw:
            # 回退：尝试老版 /matches/{id}/odds 端点
            fallback = self._api_call(f"/matches/{match_id}/odds")
            if fallback:
                odds_raw = fallback

        try:
            if isinstance(odds_raw, dict):
                hw = odds_raw.get('homeWin')
                dw = odds_raw.get('draw')
                aw = odds_raw.get('awayWin')
                if hw is not None and aw is not None:
                    return {
                        'home_odds': float(hw),
                        'draw_odds': float(dw or 2.0),
                        'away_odds': float(aw),
                    }
            return None
        except (Exception, ValueError, requests.exceptions.RequestException) as e:
            logger.error(f"解析赔率失败 match_id={match_id}: {e}")
            return None

    # ===================== 采集→数据库同步 =====================

    def sync_to_database(self, league_code: str = 'WC', season: int = 2026,
                         date_from: Optional[str] = None, date_to: Optional[str] = None) -> int:
        """
        采集→入库一键同步。

        保留现有 API 客户端逻辑不变，调用 get_matches() 后逐条写入数据库。
        按 match_id 去重，二次执行不重复入库。

        Args:
            league_code: 联赛代码 (WC, PL, PD, SA, BL1, FL1, CL, EC 等)
            season: 赛季年份
            date_from: 起始日期 (YYYY-MM-DD)，不传则拉取得分榜/赛季接口
            date_to: 结束日期 (YYYY-MM-DD)

        Returns:
            成功入库的比赛数量
        """
        league_id = None
        for lid, code in self.LEAGUE_CODES.items():
            if code == league_code:
                league_id = lid
                break
        if league_id is None:
            logger.error(f"未知联赛代码: {league_code}")
            return 0

        # 拉取比赛数据
        if date_from or date_to:
            matches = self.get_matches(league_id, date_from=date_from, date_to=date_to)
        else:
            # 无日期范围 → 拉取当前赛季
            try:
                matches = self.fetch_current_season_matches(league_id, league_code, season=season)
            except (Exception, AttributeError):
                matches = self.get_matches(league_id)

        if not matches:
            logger.warning(f"{league_code} 无比赛数据可同步")
            return 0

        # 写入数据库
        try:
            from database.db_manager import get_db
            db = get_db()
            synced = 0
            skipped = 0
            for m in matches:
                try:
                    m['league_id'] = league_id
                    m['league_name'] = f'{league_code} {season}' if not m.get('league_name') else m['league_name']
                    # 确保 NOT NULL 列有值（API 可能不返回 team_id）
                    m.setdefault('home_team_id', 0)
                    m.setdefault('away_team_id', 0)
                    if m['home_team_id'] is None:
                        m['home_team_id'] = 0
                    if m['away_team_id'] is None:
                        m['away_team_id'] = 0
                    db.add_match(m)
                    synced += 1
                except (Exception, KeyError, IndexError):
                    skipped += 1
            logger.info(f"[sync_to_database] {league_code}: 入库 {synced}/{len(matches)} 场 (match_id去重, 跳过{skipped})")
            return synced
        except ImportError:
            logger.error("无法导入 database.db_manager，跳过入库")
            return 0
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"入库失败: {e}")
            return 0

    def sync_odds_to_database(self, league_code: str = 'WC', season: int = 2026,
                               date_from: Optional[str] = None, date_to: Optional[str] = None) -> int:
        """
        采集赔率→入库同步。

        先拉取比赛列表，再逐场获取赔率写入 odds_snapshots 表。
        依赖 sync_to_database() 先入库比赛记录（赔率表有外键关系）。

        Returns:
            成功入库的赔率快照数量
        """
        league_id = None
        for lid, code in self.LEAGUE_CODES.items():
            if code == league_code:
                league_id = lid
                break
        if league_id is None:
            logger.error(f"未知联赛代码: {league_code}")
            return 0

        if date_from or date_to:
            matches = self.get_matches(league_id, date_from=date_from, date_to=date_to)
        else:
            try:
                matches = self.fetch_current_season_matches(league_id, league_code, season=season)
            except (Exception, AttributeError):
                matches = self.get_matches(league_id)

        if not matches:
            logger.warning(f"{league_code} 无比赛数据可获取赔率")
            return 0

        try:
            from database.db_manager import get_db
            db = get_db()
            synced = 0
            from datetime import datetime, timezone
            snapshot_time = datetime.now(timezone.utc).isoformat()

            for m in matches:
                mid = m.get('match_id')
                if not mid:
                    continue
                odds_data = self.get_odds(mid)
                if not odds_data:
                    continue
                try:
                    db.add_odds_snapshot(mid, odds_data, snapshot_time)
                    synced += 1
                except (Exception, AttributeError):
                    # 表可能未创建，记录告警
                    logger.debug(f"赔率快照写入跳过 match_id={mid} (表可能未就绪)")
                    pass
            logger.info(f"[sync_odds_to_database] {league_code}: 赔率入库 {synced}/{len(matches)} 场")
            return synced
        except ImportError:
            logger.error("无法导入 database.db_manager，跳过赔率入库")
            return 0
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"赔率入库失败: {e}")
            return 0

    # ===================== ⛔ 死命令：模拟数据已永久禁用 =====================
    # 以下方法已永久禁用并删除，所有数据必须来自 football-data.org API
    # _mock_standings: 积分榜必须从API获取真实数据
    # _mock_matches: 比赛数据必须从API获取真实数据
    # _mock_teams: 球队数据必须从API获取真实数据
