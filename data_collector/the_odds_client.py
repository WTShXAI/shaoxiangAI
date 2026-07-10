"""
哨响AI - The Odds API 客户端 v2.1
=================================
专业赔率聚合API: 跨博彩公司赔率对比、赔率走势历史、套利检测
免费额度: 500 req/month (odds-history 端点需付费层)
API文档: https://the-odds-api.com/liveapi/guides/v4/

核心能力:
- /sports/{sport}/odds — 当前赔率 (含多家博彩公司, 滚球期间持续更新)
- /sports/{sport}/events/{id}/odds-history — 历史赔率走势 (时序数据, 需付费层)
- /sports — 支持的运动/联赛列表

方法清单:
  get_live_odds()              获取当前实时赔率 (多玩法 h2h/spreads/totals)
  extract_best_odds()          提取最佳(最高)1X2赔率 [修复: 原未定义致AttributeError]
  extract_all_markets()        提取全部玩法赔率明细
  get_odds_history()           获取赔率历史时序 (需付费层) [修复: 原docstring虚报]
  extract_timeline_from_match() 从当前盘口提取单时间点快照 [修复: 原docstring虚报]
  batch_collect_timeline()     批量采集快照写入 odds_timeline 表 [修复: 原docstring虚报]
  batch_fetch_odds()           批量获取比赛赔率

环境变量: THE_ODDS_API_KEY (https://the-odds-api.com/#get-access 注册)
未配置 key 时自动降级: 所有 API 调用返回空, 不崩溃。
"""
import os
import time
import json
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)

# 运动映射: 哨响AI联赛名 → The Odds API sport key
SPORT_KEY_MAP = {
    # 五大联赛 + 欧冠
    "Premier League": "soccer_epl",
    "La Liga": "soccer_spain_la_liga",
    "Serie A": "soccer_italy_serie_a",
    "Bundesliga": "soccer_germany_bundesliga",
    "Ligue 1": "soccer_france_ligue_one",
    "UEFA Champions League": "soccer_uefa_champions_league",
    "UEFA Europa League": "soccer_uefa_europa_league",
    # 其他常见联赛
    "Championship": "soccer_efl_champ",
    "MLS": "soccer_usa_mls",
    "Eredivisie": "soccer_netherlands_eredivisie",
    "Primeira Liga": "soccer_portugal_primeira_liga",
    "Brasileirão": "soccer_brazil_campeonato",
}

# 博彩公司优先级 (按综合声誉排序)
BOOKMAKER_PRIORITY = [
    "pinnacle",      # 最具参考价值
    "bet365",
    "williamhill",
    "unibet",
    "marathonbet",
    "bwin",
    "betfair",
    "onexbet",
    "betvictor",
    "sport888",
]

# 地区: uk=英国博彩公司, eu=欧洲, us=美国, au=澳大利亚
REGIONS = "uk,eu"

class TheOddsCollector:
    """The Odds API 赔率数据采集器"""

    def __init__(self, api_key: str = None, base_url: str = "https://api.the-odds-api.com/v4"):
        self.api_key = api_key or os.environ.get("THE_ODDS_API_KEY", "")
        self.base_url = base_url
        self.request_count = 0
        self.max_requests = 500  # 免费层月配额
        self.last_response_header: Dict = {}  # x-requests-used, x-requests-remaining

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _api_call(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """
        统一 API 调用封装

        Args:
            endpoint: API 端点路径，如 "/sports/soccer_epl/odds"
            params: 查询参数

        Returns:
            JSON 响应或 None
        """
        if not self.is_configured:
            logger.warning("The Odds API Key 未配置，跳过请求")
            return None

        url = f"{self.base_url}{endpoint}"
        query = params or {}
        query["apiKey"] = self.api_key

        try:
            resp = requests.get(url, params=query, timeout=15)
            self.request_count += 1

            # 追踪配额使用
            self.last_response_header = {
                "x-requests-used": resp.headers.get("x-requests-used", "?"),
                "x-requests-remaining": resp.headers.get("x-requests-remaining", "?"),
            }

            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 401:
                logger.error(f"The Odds API 401: API Key 无效")
                return None
            elif resp.status_code == 429:
                logger.warning(f"The Odds API 429: 超出配额，本月剩余请求: ?")
                return None
            elif resp.status_code == 422:
                logger.warning(f"The Odds API 422: 参数错误 {params}")
                return None
            else:
                logger.warning(f"The Odds API {resp.status_code}: {endpoint}")
                return None

        except requests.exceptions.Timeout:
            logger.error(f"The Odds API 请求超时: {endpoint}")
            return None
        except (Exception, requests.exceptions.RequestException) as e:
            logger.error(f"The Odds API 请求异常: {e}")
            return None

    def get_quota_info(self) -> Dict:
        """获取API配额信息"""
        return {
            "requests_used": self.request_count,
            "api_configured": self.is_configured,
            "last_response_header": self.last_response_header,
            "monthly_limit": self.max_requests,
            "remaining_estimate": max(0, self.max_requests - self.request_count),
        }

    def get_supported_sports(self) -> List[Dict]:
        """
        获取所有支持的运动/联赛列表

        Returns:
            [{key, title, group, active, has_outrights}, ...]
        """
        data = self._api_call("/sports")
        if not data:
            return []

        sports = []
        for s in data:
            if "soccer" in s.get("key", "").lower():
                sports.append({
                    "key": s.get("key"),
                    "title": s.get("title"),
                    "group": s.get("group"),
                    "active": s.get("active"),
                    "description": s.get("description"),
                })
        logger.info(f"获取到 {len(sports)} 个足球联赛")
        return sports

    # The Odds API v4 支持的盘口类型
    AVAILABLE_MARKETS = {
        "h2h": "1X2 胜平负",
        "spreads": "亚洲让球 (点差)",
        "totals": "大小球",
        "outrights": "锦标赛冠军",
        "btts": "双方进球 (部分联赛支持)",
    }

    def get_live_odds(self, sport_key: str, regions: str = REGIONS,
                      markets: str = "h2h,spreads,totals") -> List[Dict]:
        """
        获取当前实时赔率 (多玩法)

        Args:
            sport_key: 联赛key，如 "soccer_epl"
            regions: 地区代码，如 "uk,eu"
            markets: 盘口类型，默认 "h2h,spreads,totals"
                     支持: h2h, spreads, totals, outrights

        Returns:
            [{id, sport_key, commence_time, home_team, away_team,
              bookmakers: [{key, title, markets: [
                  {key: "h2h", outcomes: [{name, price}]},
                  {key: "spreads", outcomes: [{name, price, point}]},
                  {key: "totals", outcomes: [{name, price, point}]},
              ]}]}]
        """
        params = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        data = self._api_call(f"/sports/{sport_key}/odds", params)
        if not data:
            return []

        logger.info(f"获取 {sport_key} 赔率 ({markets}): {len(data)} 场比赛")
        return data

    def get_odds_by_match_date(
        self, sport_key: str, match_date: str, home_team: str, away_team: str,
        regions: str = REGIONS, markets: str = "h2h"
    ) -> Optional[Dict]:
        """
        根据比赛日期和球队名查找赔率

        The Odds API 返回的是当天及近期的比赛赔率，通过匹配球队名定位

        Args:
            sport_key: 联赛key
            match_date: 比赛日期 (YYYY-MM-DD)
            home_team: 主队名
            away_team: 客队名

        Returns:
            匹配的比赛赔率 dict 或 None
        """
        params = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        }
        data = self._api_call(f"/sports/{sport_key}/odds", params)
        if not data:
            return None

        # 模糊匹配球队名
        home_lower = home_team.lower().replace(" fc", "").replace(" afc", "")
        away_lower = away_team.lower().replace(" fc", "").replace(" afc", "")

        for match in data:
            api_home = match.get("home_team", "").lower()
            api_away = match.get("away_team", "").lower()
            api_date = match.get("commence_time", "")[:10]

            # 日期匹配 + 球队名模糊匹配
            if api_date == match_date:
                if (home_lower in api_home or api_home in home_lower) and \
                   (away_lower in api_away or api_away in away_lower):
                    return match
                # 反向尝试 (球队可能对调)
                if (home_lower in api_away or api_away in home_lower) and \
                   (away_lower in api_home or api_home in away_lower):
                    return match

        return None

    def extract_all_markets(self, match_odds: Dict, bookmakers: List[str] = None) -> Dict:
        """
        从比赛赔率数据中提取所有玩法的最佳赔率
        
        Args:
            match_odds: API返回的单场比赛赔率
            bookmakers: 优先博彩公司列表

        Returns:
            {
                match_id_external, home_team, away_team, commence_time,
                markets: {
                    "h2h": {home_odds, draw_odds, away_odds, ...},
                    "spreads": [{line, home_odds, away_odds, point}, ...],
                    "totals": [{over_under_line, over_odds, under_odds}, ...],
                },
                bookmaker_count, bookmakers_details
            }
        """
        bookmakers = bookmakers or BOOKMAKER_PRIORITY[:4]
        
        result = {
            "match_id_external": match_odds.get("id"),
            "home_team": match_odds.get("home_team"),
            "away_team": match_odds.get("away_team"),
            "commence_time": match_odds.get("commence_time"),
            "markets": {},
            "bookmaker_count": 0,
            "bookmakers_details": [],
        }
        
        all_bm_names = []
        
        for bm in match_odds.get("bookmakers", []):
            bm_key = bm.get("key", "").lower()
            if bm_key not in bookmakers:
                continue
            all_bm_names.append(bm.get("title", bm_key))
            
            for market in bm.get("markets", []):
                mk_key = market.get("key")
                
                if mk_key == "h2h":
                    self._extract_h2h_market(result, market, match_odds)
                elif mk_key == "spreads":
                    self._extract_spreads_market(result, market)
                elif mk_key == "totals":
                    self._extract_totals_market(result, market)
        
        result["bookmaker_count"] = len(set(all_bm_names))
        if all_bm_names:
            result["bookmakers_details"] = list(set(all_bm_names))
        
        return result
    
    def _extract_h2h_market(self, result: Dict, market: Dict, match_odds: Dict):
        """提取1X2赔率"""
        if "h2h" not in result["markets"]:
            result["markets"]["h2h"] = {"home_odds": [], "draw_odds": [], "away_odds": []}
        
        outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}
        home_team = match_odds.get("home_team", "")
        away_team = match_odds.get("away_team", "")
        
        h = outcomes.get(home_team)
        d = outcomes.get("Draw")
        a = outcomes.get(away_team)
        
        if h and d and a:
            result["markets"]["h2h"]["home_odds"].append(h)
            result["markets"]["h2h"]["draw_odds"].append(d)
            result["markets"]["h2h"]["away_odds"].append(a)
    
    def _extract_spreads_market(self, result: Dict, market: Dict):
        """提取亚洲让球 (spreads) 赔率"""
        if "spreads" not in result["markets"]:
            result["markets"]["spreads"] = []
        
        outcomes = market.get("outcomes", [])
        if len(outcomes) == 2:
            point = outcomes[0].get("point", 0)
            # spreads: outcome1=home team at point, outcome2=away team at -point
            home_outcome = None
            away_outcome = None
            for o in outcomes:
                if o.get("name") == result.get("home_team"):
                    home_outcome = o
                else:
                    away_outcome = o
            
            if home_outcome and away_outcome:
                result["markets"]["spreads"].append({
                    "line": point,
                    "home_odds": home_outcome["price"],
                    "away_odds": away_outcome["price"],
                })
    
    def _extract_totals_market(self, result: Dict, market: Dict):
        """提取大小球赔率"""
        if "totals" not in result["markets"]:
            result["markets"]["totals"] = []
        
        outcomes = market.get("outcomes", [])
        if len(outcomes) == 2:
            point = outcomes[0].get("point", 2.5)
            over_outcome = None
            under_outcome = None
            for o in outcomes:
                if o.get("name") == "Over":
                    over_outcome = o
                elif o.get("name") == "Under":
                    under_outcome = o
            
            if over_outcome and under_outcome:
                result["markets"]["totals"].append({
                    "over_under_line": point,
                    "over_odds": over_outcome["price"],
                    "under_odds": under_outcome["price"],
                })
    
    def _average_market_odds(self, result: Dict):
        """对多博彩公司的赔率取平均值, 生成最终结果"""
        markets = result.get("markets", {})
        
        # 1X2 平均
        if "h2h" in markets:
            h2h = markets["h2h"]
            for key in ["home_odds", "draw_odds", "away_odds"]:
                vals = h2h[key]
                if vals:
                    h2h[key + "_avg"] = round(sum(vals) / len(vals), 2)
                    h2h[key + "_max"] = round(max(vals), 2)
                    h2h[key + "_min"] = round(min(vals), 2)
        
        # Spreads 平均 (按 line 分组)
        if "spreads" in markets:
            spread_groups = {}
            for s in markets["spreads"]:
                line = s["line"]
                if line not in spread_groups:
                    spread_groups[line] = {"home_odds": [], "away_odds": []}
                spread_groups[line]["home_odds"].append(s["home_odds"])
                spread_groups[line]["away_odds"].append(s["away_odds"])
            
            markets["spreads_avg"] = []
            for line, vals in sorted(spread_groups.items()):
                markets["spreads_avg"].append({
                    "line": line,
                    "home_odds": round(sum(vals["home_odds"]) / len(vals["home_odds"]), 2),
                    "away_odds": round(sum(vals["away_odds"]) / len(vals["away_odds"]), 2),
                })
        
        # Totals 平均 (按线分组)
        if "totals" in markets:
            totals_groups = {}
            for t in markets["totals"]:
                line = t["over_under_line"]
                if line not in totals_groups:
                    totals_groups[line] = {"over_odds": [], "under_odds": []}
                totals_groups[line]["over_odds"].append(t["over_odds"])
                totals_groups[line]["under_odds"].append(t["under_odds"])
            
            markets["totals_avg"] = []
            for line, vals in sorted(totals_groups.items()):
                markets["totals_avg"].append({
                    "over_under_line": line,
                    "over_odds": round(sum(vals["over_odds"]) / len(vals["over_odds"]), 2),
                    "under_odds": round(sum(vals["under_odds"]) / len(vals["under_odds"]), 2),
                })
        
        return result

    def extract_best_odds(self, match_odds: Dict, bookmakers: List[str] = None) -> Dict:
        """
        从比赛赔率中提取最佳(最高)1X2赔率 — extract_all_markets 的薄封装。

        被 batch_fetch_odds / __main__ 调用, 返回扁平的 1X2 dict:
        {home_team, away_team, commence_time, home_odds, draw_odds, away_odds,
         bookmaker_count, bookmakers, source}

        "最佳" = 跨庄家取每个结果的最大赔率 (买方最优价), 用于 soft-line 套利分析。
        bookmaker_count = 实际提供了完整 h2h 三项赔率的庄家数 (不含仅spreads/totals的庄家)。
        若某结果无赔率则填 None。
        """
        full = self.extract_all_markets(match_odds, bookmakers)
        h2h = full.get("markets", {}).get("h2h", {})
        h_list = h2h.get("home_odds", [])
        d_list = h2h.get("draw_odds", [])
        a_list = h2h.get("away_odds", [])
        # 三个列表应等长 (每个庄家贡献一组 h/d/a); 取最短长度防不一致
        n_bm = min(len(h_list), len(d_list), len(a_list)) if (h_list and d_list and a_list) else 0
        return {
            "match_id_external": full.get("match_id_external"),
            "home_team": full.get("home_team"),
            "away_team": full.get("away_team"),
            "commence_time": full.get("commence_time"),
            "home_odds": max(h_list) if h_list else None,
            "draw_odds": max(d_list) if d_list else None,
            "away_odds": max(a_list) if a_list else None,
            "bookmaker_count": n_bm,
            "bookmakers": full.get("bookmakers_details", []),
            "source": "the_odds_api",
        }

    # ──────────────────────────────────────────
    # 时序采集 (odds-history → odds_timeline 表)
    # ──────────────────────────────────────────
    def get_odds_history(self, sport_key: str, event_id: str,
                         bookmakers: str = None) -> Optional[List[Dict]]:
        """
        获取单场比赛的赔率历史时序 (The Odds API odds-history 端点)。

        需要付费层 (历史端点不在免费500req内)。返回该场比赛从开盘到当前的
        多时间点赔率快照列表, 是构建 odds_timeline / 滚球 drift 分析的核心数据。

        Args:
            sport_key: 联赛key, 如 "soccer_epl"
            event_id: 比赛ID (从 get_live_odds 返回的 "id" 字段获取)
            bookmakers: 逗号分隔的庄家过滤, 如 "pinnacle,bet365"; None=全部

        Returns:
            时序快照列表 [{timestamp, h2h: {home/draw/away 各庄家}, ...}], 或 None
            每个快照含 "timestamp" + 各庄家该时刻的赔率
        """
        params = {"oddsFormat": "decimal", "dateFormat": "iso"}
        if bookmakers:
            params["bookmakers"] = bookmakers
        data = self._api_call(f"/sports/{sport_key}/events/{event_id}/odds-history", params)
        if not data:
            return None
        # API 返回 {sport_key, events: [{timestamp, bookmakers: [...]}]}
        # 取 events 数组 (去掉外层包装)
        return data if isinstance(data, list) else data.get("events", [])

    def extract_timeline_from_match(self, match_odds: Dict,
                                     bookmakers: List[str] = None) -> List[Dict]:
        """
        从单场比赛的当前赔率数据中提取一个"时序快照" (单时间点)。

        用于: 当无法获取完整 odds-history 时, 至少把当前盘口作为一条快照写入
        odds_timeline, 供后续多时间点拼接 drift 分析。

        Args:
            match_odds: get_live_odds 返回的单场比赛 dict
            bookmakers: 优先庄家列表

        Returns:
            [{bookmaker, home_odds, draw_odds, away_odds}] — 每家庄家一条快照
            (时间戳统一用当前时间, 由调用方填充)
        """
        bm_filter = set(bm.lower() for bm in (bookmakers or BOOKMAKER_PRIORITY[:4]))
        snapshots = []
        for bm in match_odds.get("bookmakers", []):
            bm_key = bm.get("key", "").lower()
            if bm_filter and bm_key not in bm_filter:
                continue
            bm_title = bm.get("title", bm_key)
            h_odds = d_odds = a_odds = None
            for market in bm.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                for o in market.get("outcomes", []):
                    name = o.get("name", "")
                    price = o.get("price")
                    if name == match_odds.get("home_team"):
                        h_odds = price
                    elif name == "Draw":
                        d_odds = price
                    elif name == match_odds.get("away_team"):
                        a_odds = price
            if h_odds and d_odds and a_odds:
                snapshots.append({
                    "bookmaker": bm_title,
                    "home_odds": float(h_odds), "draw_odds": float(d_odds),
                    "away_odds": float(a_odds),
                })
        return snapshots

    def batch_collect_timeline(self, sport_key: str, match_id_map: Dict[str, int],
                               db_path: str = None, bookmakers: List[str] = None) -> Dict:
        """
        批量采集当前赔率快照并写入 odds_timeline 表。

        滚球/临场定期调用此方法 (建议 Celery 每 30-60s), 持续填充 odds_timeline,
        为 reverse_odds_engine 的 drift 分析提供时序数据。

        Args:
            sport_key: 联赛key, 如 "soccer_epl"
            match_id_map: {API event_id 或 "home_away": 本地 match_id} 映射,
                          用于把 API 比赛关联到 DB match_id
            db_path: 数据库路径 (默认 data/football_data.db)
            bookmakers: 优先庄家

        Returns:
            {collected, written, skipped, api_calls}
        """
        import sqlite3
        if db_path is None:
            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "data", "football_data.db")

        stats = {"collected": 0, "written": 0, "skipped": 0, "api_calls": 0}
        live_odds = self.get_live_odds(sport_key, markets="h2h")
        stats["api_calls"] = self.request_count

        now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(db_path)
        try:
            for lo in live_odds:
                ev_id = lo.get("id", "")
                ha_key = f"{lo.get('home_team','')}|{lo.get('away_team','')}"
                match_id = match_id_map.get(ev_id) or match_id_map.get(ha_key)
                if not match_id:
                    stats["skipped"] += 1
                    continue
                snapshots = self.extract_timeline_from_match(lo, bookmakers)
                stats["collected"] += len(snapshots)
                for snap in snapshots:
                    try:
                        conn.execute(
                            """INSERT OR REPLACE INTO odds_timeline
                               (match_id, snapshot_time, home_odds, draw_odds, away_odds,
                                bookmaker, source, raw_json)
                               VALUES (?, ?, ?, ?, ?, ?, 'the_odds_api', ?)""",
                            (match_id, now_iso, snap["home_odds"], snap["draw_odds"],
                             snap["away_odds"], snap["bookmaker"],
                             json.dumps({"event_id": ev_id}, ensure_ascii=False))
                        )
                        stats["written"] += 1
                    except sqlite3.IntegrityError:
                        pass  # UNIQUE 冲突 (同一时刻同一庄家), 跳过
            conn.commit()
        finally:
            conn.close()
        logger.info(f"odds_timeline 批量采集: 采集{stats['collected']} 写入{stats['written']} "
                    f"跳过{stats['skipped']} API调用{stats['api_calls']}")
        return stats

    def batch_fetch_odds(
        self, sport_key: str, matches: List[Dict], delay: float = 1.0
    ) -> Tuple[List[Dict], Dict]:
        """
        批量获取比赛赔率

        Args:
            sport_key: 联赛key
            matches: 比赛列表 [{match_date, home_team_name, away_team_name}, ...]
            delay: 请求间隔(秒)

        Returns:
            (成功的赔率列表, 统计信息)
        """
        all_odds = []
        stats = {"total": len(matches), "success": 0, "skipped": 0, "failed": 0}

        # 先获取该联赛的所有当期赔率（1次API调用）
        live_odds = self.get_live_odds(sport_key)

        # 构建 {home_away: match} 索引，O(n) 构建 → O(1) 查询
        _live_idx: Dict[str, dict] = {}
        # 额外模糊索引: 归一化球队名
        _live_fuzzy_idx: Dict[str, dict] = {}
        for lo in live_odds:
            lo_home = lo.get("home_team", "")
            lo_away = lo.get("away_team", "")
            if lo_home and lo_away:
                _live_idx[f"{lo_home}|{lo_away}"] = lo
                # 归一化键（用于模糊匹配）
                _nh = _normalize_team_name(lo_home)
                _na = _normalize_team_name(lo_away)
                if _nh and _na:
                    _live_fuzzy_idx[f"{_nh}|{_na}"] = lo

        for i, match in enumerate(matches):
            # 从索引中查找匹配（O(1) 查询，避免 O(n*m) 双层循环）
            home = match.get("home_team_name", "")
            away = match.get("away_team_name", "")
            lo = _live_idx.get(f"{home}|{away}")
            if lo is None:
                # fallback: 归一化键模糊匹配（O(1)，避免嵌套循环）
                _nh = _normalize_team_name(home)
                _na = _normalize_team_name(away)
                lo = _live_fuzzy_idx.get(f"{_nh}|{_na}") if _nh and _na else None
            if lo:
                extracted = self.extract_best_odds(lo)
                if extracted:
                    extracted["match_date"] = match.get("match_date", "")
                    all_odds.append(extracted)
                    stats["success"] += 1
                else:
                    stats["skipped"] += 1
            else:
                stats["skipped"] += 1

            # 每10个请求输出进度
            if (i + 1) % 20 == 0:
                logger.info(f"  赔率进度: {i+1}/{len(matches)} (成功{stats['success']})")

        logger.info(f"赔率批量获取完成: 成功{stats['success']}/{stats['total']}, "
                     f"跳过{stats['skipped']}, 共{self.request_count}次API调用")
        return all_odds, stats

def _normalize_team_name(name: str) -> str:
    """归一化球队名（去掉 FC, AFC 等后缀后比较）"""
    s = name.lower().strip()
    for suffix in [" fc", " afc", " cf", " ac", " united", " city", " town"]:
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    return s

def similar_team_name(name1: str, name2: str) -> bool:
    """球队名模糊匹配"""
    n1 = _normalize_team_name(name1)
    n2 = _normalize_team_name(name2)
    return n1 in n2 or n2 in n1

# ─── 便捷函数 ───

def get_odds_collector() -> TheOddsCollector:
    """获取 The Odds API 采集器实例"""
    return TheOddsCollector()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    collector = TheOddsCollector()

    if not collector.is_configured:
        print("请设置环境变量 THE_ODDS_API_KEY")
        print("注册地址: https://the-odds-api.com/#get-access")
    else:
        print("=== 配额信息 ===")
        print(json.dumps(collector.get_quota_info(), indent=2))
        print("\n=== 英超实时赔率 (前3场) ===")
        odds = collector.get_live_odds("soccer_epl")
        for match in odds[:3]:
            print(f"\n{match.get('home_team')} vs {match.get('away_team')}")
            print(f"  开赛: {match.get('commence_time')}")
            extracted = collector.extract_best_odds(match)
            print(f"  最佳赔率: H{extracted.get('home_odds')} D{extracted.get('draw_odds')} A{extracted.get('away_odds')}")
