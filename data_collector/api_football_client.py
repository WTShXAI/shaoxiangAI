"""
哨响AI - API-Football (RapidAPI) 客户端 v1.0
=============================================
数据覆盖: 170+联赛, 球员伤病/阵容/统计数据/赔率
免费额度: 100 req/day (RapidAPI免费层)
API文档: https://www.api-football.com/documentation-v3

核心能力:
- /fixtures/lineups — 首发阵容
- /players/sidelined — 伤病/禁赛球员 (按球队/联赛)
- /teams/statistics — 球队赛季统计数据
- /fixtures/events — 比赛事件(进球/黄牌/换人)
- /odds — 赔率数据
- /fixtures/headtohead — 历史交锋
"""
import os
import time
import json
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)

# RapidAPI 配置（Phase 2A: 统一从 config/api_config.py 读取，支持环境变量覆盖）
RAPIDAPI_HOST = "api-football-v1.p.rapidapi.com"
try:
    from config.api_config import EXTERNAL_SERVICES
    RAPIDAPI_BASE_URL = EXTERNAL_SERVICES.get("api_football_rapidapi", {}).get(
        "base_url", "https://api-football-v1.p.rapidapi.com/v3")
except ImportError:
    RAPIDAPI_BASE_URL = os.getenv("RAPIDAPI_FOOTBALL_BASE_URL", "https://api-football-v1.p.rapidapi.com/v3")

# 联赛ID映射: 哨响AI内部ID → API-Football league ID
LEAGUE_ID_MAP = {
    2021: 39,    # 英超 → 39
    2014: 140,   # 西甲 → 140
    2019: 135,   # 意甲 → 135
    2002: 78,    # 德甲 → 78
    2015: 61,    # 法甲 → 61
    2001: 2,     # 欧冠 → 2
    2000: 1,     # 世界杯 → 1
}

# 联赛ID反向映射
LEAGUE_NAME_TO_ID = {v: k for k, v in LEAGUE_ID_MAP.items()}


class ApiFootballCollector:
    """API-Football (RapidAPI) 数据采集器"""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("RAPIDAPI_KEY", "")
        self.request_count = 0
        self.max_daily = 100  # 免费层日配额
        self._last_request_time = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def _headers(self) -> Dict:
        return {
            "x-rapidapi-host": RAPIDAPI_HOST,
            "x-rapidapi-key": self.api_key,
        }

    def _rate_limit(self):
        """速率限制: 免费层 30 req/min"""
        elapsed = time.time() - self._last_request_time
        if elapsed < 2.0:  # 保守: 每2秒1次 (30/min)
            time.sleep(2.0 - elapsed)
        self._last_request_time = time.time()

    def _api_call(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """
        统一 API 调用封装

        Returns:
            {"response": [...], "results": N, "get": "..."} 或 None
        """
        if not self.is_configured:
            logger.warning("RapidAPI Key 未配置，跳过 API-Football 请求")
            return None

        url = f"{RAPIDAPI_BASE_URL}{endpoint}"
        self._rate_limit()

        try:
            resp = requests.get(
                url, headers=self._headers, params=params or {}, timeout=15
            )
            self.request_count += 1

            if resp.status_code == 200:
                data = resp.json()
                errors = data.get("errors", [])
                if errors:
                    logger.warning(f"API-Football 业务错误 [{endpoint}]: {errors}")
                    return None
                return data
            elif resp.status_code == 429:
                logger.warning("API-Football 429: 超出速率限制，等待30秒...")
                time.sleep(30)
                return self._api_call(endpoint, params)
            elif resp.status_code == 403:
                logger.error("API-Football 403: API Key 无效或未订阅")
                return None
            else:
                logger.warning(f"API-Football {resp.status_code}: {endpoint}")
                return None

        except requests.exceptions.Timeout:
            logger.error(f"API-Football 请求超时: {endpoint}")
            return None
        except (Exception, requests.exceptions.RequestException) as e:
            logger.error(f"API-Football 请求异常: {e}")
            return None

    def get_quota_info(self) -> Dict:
        """获取配额信息"""
        return {
            "requests_used": self.request_count,
            "daily_limit": self.max_daily,
            "remaining": max(0, self.max_daily - self.request_count),
            "api_configured": self.is_configured,
        }

    # ─── 伤病/禁赛信息 ───

    def get_injuries_by_team(self, team_id: int, season: int = None) -> List[Dict]:
        """
        获取球队伤病/禁赛球员列表

        API: GET /players/sidelined?team={id}&season={year}

        Returns:
            [{player: {id, name, photo}, team: {...},
              type: "Injury"|"Suspension", reason, fixture: {...}}]
        """
        season = season or datetime.now().year
        params = {"team": team_id, "season": season}
        data = self._api_call("/players/sidelined", params)
        if not data:
            return []

        injuries = []
        for item in data.get("response", []):
            player_info = item.get("player", {})
            injuries.append({
                "player_id": player_info.get("id"),
                "player_name": player_info.get("name"),
                "type": item.get("type"),         # "Injury" or "Suspension"
                "reason": item.get("reason", ""),
                "fixture": item.get("fixture", {}),
                "team_id": item.get("team", {}).get("id"),
                "team_name": item.get("team", {}).get("name"),
            })

        logger.info(f"伤病/禁赛: team_id={team_id} → {len(injuries)} 人")
        return injuries

    def get_injuries_by_league(self, league_id: int, season: int = None) -> List[Dict]:
        """
        获取整个联赛的伤病/禁赛情况

        API: GET /players/sidelined?league={id}&season={year}
        """
        api_league = LEAGUE_ID_MAP.get(league_id, league_id)
        season = season or datetime.now().year
        params = {"league": api_league, "season": season}
        data = self._api_call("/players/sidelined", params)
        if not data:
            return []

        injuries = []
        for item in data.get("response", []):
            player_info = item.get("player", {})
            injuries.append({
                "player_id": player_info.get("id"),
                "player_name": player_info.get("name"),
                "type": item.get("type"),
                "reason": item.get("reason", ""),
                "team_id": item.get("team", {}).get("id"),
                "team_name": item.get("team", {}).get("name"),
            })

        # 按球队汇总
        by_team = defaultdict(lambda: {"injured": 0, "suspended": 0, "players": []})
        for inj in injuries:
            tn = inj["team_name"]
            if inj["type"] == "Injury":
                by_team[tn]["injured"] += 1
            else:
                by_team[tn]["suspended"] += 1
            by_team[tn]["players"].append(inj["player_name"])

        logger.info(f"联赛伤病: league_id={league_id} → {len(injuries)} 条, "
                     f"{len(by_team)} 支球队受影响")
        return injuries

    # ─── 球队统计数据 ───

    def get_team_statistics(self, team_id: int, league_id: int,
                            season: int = None) -> Optional[Dict]:
        """
        获取球队赛季综合统计数据

        API: GET /teams/statistics?team={id}&league={id}&season={year}

        Returns:
            {league, team, form, fixtures: {played, wins, draws, loses},
             goals: {for: {total, average}, against: {...}},
             biggest: {streak, wins, loses, goals},
             clean_sheet: {home, away, total},
             failed_to_score: {home, away, total},
             cards: {yellow, red},
             lineups: [{formation, played}],
             penalty: {scored, missed, total}}
        """
        api_league = LEAGUE_ID_MAP.get(league_id, league_id)
        season = season or datetime.now().year
        params = {"team": team_id, "league": api_league, "season": season}
        data = self._api_call("/teams/statistics", params)
        if not data or not data.get("response"):
            return None

        resp = data["response"]
        league_info = resp.get("league", {})
        form_str = resp.get("form", "")
        fixtures = resp.get("fixtures", {})
        goals = resp.get("goals", {})
        cards = resp.get("cards", {})
        lineups = resp.get("lineups", [])

        # 计算表单分 (W=3, D=1, L=0)
        form_score = sum(
            3 if c == "W" else 1 if c == "D" else 0
            for c in form_str.replace(" ", "")[-5:]
        )

        # 最常见阵型
        top_formation = None
        if lineups:
            top_formation = max(lineups, key=lambda l: l.get("played", 0))

        return {
            "team_id": resp.get("team", {}).get("id"),
            "team_name": resp.get("team", {}).get("name"),
            "league_id": league_id,
            "season": season,
            "form": form_str,
            "form_5_score": form_score,
            "played": fixtures.get("played", {}).get("total", 0),
            "wins": fixtures.get("wins", {}).get("total", 0),
            "draws": fixtures.get("draws", {}).get("total", 0),
            "losses": fixtures.get("loses", {}).get("total", 0),
            "goals_for_avg": goals.get("for", {}).get("average", {}).get("total", 0),
            "goals_against_avg": goals.get("against", {}).get("average", {}).get("total", 0),
            "clean_sheet_pct": league_info.get("clean_sheet", {}),
            "yellow_cards": cards.get("yellow", {}).get("total", 0) or 0,
            "red_cards": cards.get("red", {}).get("total", 0) or 0,
            "top_formation": top_formation.get("formation") if top_formation else None,
            "is_high_press": form_score >= 10,  # 近5场≥10分=状态好
        }

    # ─── 首发阵容 ───

    def get_lineups(self, fixture_id: int) -> List[Dict]:
        """
        获取比赛首发阵容

        API: GET /fixtures/lineups?fixture={id}

        Returns:
            [{team, formation, startXI: [{player}], substitutes: [{player}]}]
        """
        params = {"fixture": fixture_id}
        data = self._api_call("/fixtures/lineups", params)
        if not data:
            return []

        lineups = []
        for item in data.get("response", []):
            start_xi = []
            for p in item.get("startXI", []):
                player = p.get("player", {})
                start_xi.append({
                    "id": player.get("id"),
                    "name": player.get("name"),
                    "number": player.get("number"),
                    "position": player.get("pos", "?"),
                })

            lineups.append({
                "team_id": item.get("team", {}).get("id"),
                "team_name": item.get("team", {}).get("name"),
                "formation": item.get("formation"),
                "start_xi": start_xi,
                "subs_count": len(item.get("substitutes", [])),
            })

        return lineups

    def get_team_season_players(self, team_id: int, season: int = None) -> List[Dict]:
        """
        获取球队本赛季球员列表（含出场统计）

        API: GET /players?team={id}&season={year}
        """
        season = season or datetime.now().year
        params = {"team": team_id, "season": season, "page": 1}
        all_players = []

        while True:
            data = self._api_call("/players", params)
            if not data:
                break

            for item in data.get("response", []):
                player = item.get("player", {})
                stats = item.get("statistics", [{}])[0]
                all_players.append({
                    "player_id": player.get("id"),
                    "player_name": player.get("name"),
                    "position": stats.get("games", {}).get("position", "?"),
                    "apps": stats.get("games", {}).get("appearences", 0),
                    "goals": stats.get("goals", {}).get("total", 0),
                    "assists": stats.get("goals", {}).get("assists", 0),
                    "rating": stats.get("games", {}).get("rating", "0"),
                    "is_injured": player.get("injured", False),
                })

            # 分页
            paging = data.get("paging", {})
            if paging.get("current", 1) < paging.get("total", 1):
                params["page"] += 1
            else:
                break

        logger.info(f"球员列表: team_id={team_id} → {len(all_players)} 人")
        return all_players

    # ─── 历史交锋 ───

    def get_head_to_head(self, team_a_id: int, team_b_id: int,
                         last_n: int = 10) -> List[Dict]:
        """
        获取两队历史交锋记录

        API: GET /fixtures/headtohead?h2h={teamA}-{teamB}&last={N}

        Returns:
            按时间降序的比赛列表
        """
        h2h_str = f"{team_a_id}-{team_b_id}"
        params = {"h2h": h2h_str, "last": last_n}
        data = self._api_call("/fixtures/headtohead", params)
        if not data:
            return []

        h2h = []
        for item in data.get("response", []):
            goals = item.get("goals", {})
            teams = item.get("teams", {})
            h2h.append({
                "fixture_id": item.get("fixture", {}).get("id"),
                "match_date": item.get("fixture", {}).get("date", "")[:10],
                "home_team": teams.get("home", {}).get("name"),
                "away_team": teams.get("away", {}).get("name"),
                "home_score": goals.get("home"),
                "away_score": goals.get("away"),
                "league": item.get("league", {}).get("name"),
            })

        logger.info(f"H2H: {team_a_id} vs {team_b_id} → {len(h2h)} 场")
        return h2h

    # ─── 比赛统计事件 ───

    def get_fixture_events(self, fixture_id: int) -> Dict[str, List]:
        """
        获取比赛事件（进球、黄牌、红牌、换人）

        API: GET /fixtures/events?fixture={id}

        Returns:
            {goals: [...], cards: [...], substitutions: [...]}
        """
        params = {"fixture": fixture_id}
        data = self._api_call("/fixtures/events", params)
        if not data:
            return {"goals": [], "cards": [], "substitutions": []}

        events = {"goals": [], "cards": [], "substitutions": []}
        for ev in data.get("response", []):
            ev_type = ev.get("type", "")
            detail = ev.get("detail", "")
            item = {
                "time": ev.get("time", {}).get("elapsed", 0),
                "team": ev.get("team", {}).get("name"),
                "player": ev.get("player", {}).get("name"),
                "assist": ev.get("assist", {}).get("name"),
                "detail": detail,
            }
            if ev_type == "Goal":
                if "Penalty" in (detail or ""):
                    item["type"] = "penalty"
                elif "Own" in (detail or ""):
                    item["type"] = "own_goal"
                else:
                    item["type"] = "goal"
                events["goals"].append(item)
            elif ev_type == "Card":
                if detail and "Red" in detail:
                    events["cards"].append({**item, "card": "red"})
                else:
                    events["cards"].append({**item, "card": "yellow"})
            elif ev_type == "subst":
                events["substitutions"].append(item)

        return events

    # ─── 批量预计算: 球队实力评分 ───

    def compute_team_strength_ratings(
        self, league_id: int, season: int = None
    ) -> Dict[str, Dict]:
        """
        基于 API-Football 统计数据计算球队实力评分

        为每支球队输出: attack_strength, defense_strength, form_score,
                       injuries_count, key_missing_players

        Returns:
            {team_name: {attack, defense, form, injuries, ...}}
        """
        season = season or datetime.now().year
        api_league = LEAGUE_ID_MAP.get(league_id, league_id)

        # 获取联赛所有球队
        params = {"league": api_league, "season": season}
        data = self._api_call("/teams", params)
        if not data:
            return {}

        teams = data.get("response", [])
        ratings = {}

        for i, team in enumerate(teams):
            team_id = team.get("team", {}).get("id")
            team_name = team.get("team", {}).get("name", "")

            # 获取统计
            stats = self.get_team_statistics(team_id, league_id, season)
            if not stats:
                continue

            # 获取伤病
            injuries = self.get_injuries_by_team(team_id, season)
            injury_count = sum(1 for inj in injuries if inj["type"] == "Injury")

            # 实力评分
            gf_avg = stats.get("goals_for_avg", 1.0) or 1.0
            ga_avg = stats.get("goals_against_avg", 1.0) or 1.0
            form_score = stats.get("form_5_score", 5)
            win_rate = stats.get("wins", 0) / max(stats.get("played", 1), 1)

            attack = gf_avg / 1.5     # 基准: 联赛平均1.5球/场
            defense = 2.0 - ga_avg / 1.5

            ratings[team_name] = {
                "team_id": team_id,
                "attack_strength": round(attack, 2),
                "defense_strength": round(defense, 2),
                "form_score": form_score,
                "win_rate": round(win_rate, 3),
                "yellow_cards": stats.get("yellow_cards", 0),
                "red_cards": stats.get("red_cards", 0),
                "injuries_count": injury_count,
                "top_formation": stats.get("top_formation"),
            }

            if (i + 1) % 5 == 0:
                logger.info(f"  球队评分进度: {i+1}/{len(teams)}")

        logger.info(f"实力评分: league_id={league_id} → {len(ratings)} 支球队")
        return ratings


# ─── 便捷函数 ───

def get_api_football_collector() -> ApiFootballCollector:
    """获取 API-Football 采集器实例"""
    return ApiFootballCollector()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    c = ApiFootballCollector()

    if not c.is_configured:
        print("请设置环境变量 RAPIDAPI_KEY")
        print("注册地址: https://rapidapi.com/api-sports/api/api-football/")
    else:
        print("=== 配额信息 ===")
        print(json.dumps(c.get_quota_info(), indent=2))
        print("\n=== 英超伤病 (前3队) ===")
        # 先获取球队列表
        data = c._api_call("/teams", {"league": 39, "season": 2024})
        if data:
            for team in data.get("response", [])[:3]:
                tid = team["team"]["id"]
                injuries = c.get_injuries_by_team(tid, 2024)
                print(f"\n{team['team']['name']}: {len(injuries)} 人伤缺")
                for inj in injuries[:3]:
                    print(f"  - {inj['player_name']} [{inj['type']}]: {inj.get('reason','?')}")
