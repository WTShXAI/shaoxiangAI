"""
SP Odds API 集成模块（v6.0 移植版）
====================================
移植自 D:/AI/SP/sp_odds_api.py（footballAI v3.2/v4.0 智囊团成品客户端），
经哨响AI总工适配，作为 v6.0 的统一实时赔率采集器。

变更点（相对原版）：
- PROJECT_ROOT / CONFIG_PATH / DB_PATH 重指到 D:/Architecture（当前单一真源根）
- 实时落库目标由 SP 的 sp_data.db(第二套schema) 改为统一库自包含表 `live_odds_raw`
  （不写入 WC 专属的 odds 表，避免引入 FK 依赖/双库漂移）
- 其余抓取 / Pinnacle优先选价 / 隐含概率 / overround / Kelly / API-vs-截图比对 /
  阵容抓取逻辑 100% 沿用原版

铁律：仅欧盘(1X2)，禁止亚盘。
"""

import json
import os
import sys
import sqlite3
import configparser
import logging

logger = logging.getLogger(__name__)

# ── 加载 .env (使 THEODDS_API_KEY 等环境变量可用, 优先于 config.ini) ──
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    from urllib import request as urllib_request
    import urllib.error

    class _RequestsShim:
        """当requests不可用时，用urllib替代"""
        @staticmethod
        def get(url, timeout=10):
            req = urllib_request.Request(url)
            try:
                resp = urllib_request.urlopen(req, timeout=timeout)
                class _R:
                    status_code = 200
                    text = resp.read().decode()
                    headers = dict(resp.headers)
                    def json(self):
                        return json.loads(self.text)
                return _R()
            except urllib.error.HTTPError as e:
                class _R:
                    status_code = e.code
                    text = e.read().decode()
                    headers = {}
                    def json(self):
                        return json.loads(self.text)
                return _R()

    requests = _RequestsShim()


# 项目根目录 (D:/Architecture)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# ── 接入中央 API 预算护栏 (带防御性降级) ──
try:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from pipeline.collectors.api_budget import ApiBudgetGuard, get_guard
    _HAS_GUARD = True
except Exception as _guard_err:  # pragma: no cover
    logger.warning(f"API 预算护栏不可用, 降级为裸请求: {_guard_err}")
    _HAS_GUARD = False
# 本采集器专属配置 (pipeline/collectors/config.ini)
CONFIG_PATH = Path(__file__).resolve().parent / "config.ini"
# 统一库（单一真源）
DB_PATH = PROJECT_ROOT / "data" / "football_data.db"

# 美加墨世界杯对应的API sport_key
WORLD_CUP_KEY = "soccer_fifa_world_cup"

# 支持的足球赛事映射（API sport_key → 中文联赛名）
SPORT_KEY_MAP = {
    "soccer_fifa_world_cup": "美加墨世界杯",
    "soccer_epl": "英超",
    "soccer_germany_bundesliga": "德甲",
    "soccer_spain_la_liga": "西甲",
    "soccer_italy_serie_a": "意甲",
    "soccer_france_ligue_one": "法甲",
    "soccer_china_superleague": "中超",
    "soccer_conmebol_copa_libertadores": "解放者杯",
    "soccer_conmebol_copa_sudamericana": "南美杯",
}

# ══════════════════════════════════════════════════
# 英文队名 → 中文队名映射（所有输出必须使用中文名）
# ══════════════════════════════════════════════════
TEAM_NAME_ZH = {
    # 美加墨世界杯 48队
    "Algeria": "阿尔及利亚",
    "Argentina": "阿根廷",
    "Australia": "澳大利亚",
    "Austria": "奥地利",
    "Belgium": "比利时",
    "Bosnia & Herzegovina": "波黑",
    "Brazil": "巴西",
    "Canada": "加拿大",
    "Cape Verde": "佛得角",
    "Colombia": "哥伦比亚",
    "Croatia": "克罗地亚",
    "Curaçao": "库拉索",
    "Czech Republic": "捷克",
    "DR Congo": "刚果(金)",
    "Ecuador": "厄瓜多尔",
    "Egypt": "埃及",
    "England": "英格兰",
    "France": "法国",
    "Germany": "德国",
    "Ghana": "加纳",
    "Haiti": "海地",
    "Iran": "伊朗",
    "Iraq": "伊拉克",
    "Ivory Coast": "科特迪瓦",
    "Japan": "日本",
    "Jordan": "约旦",
    "Mexico": "墨西哥",
    "Morocco": "摩洛哥",
    "Netherlands": "荷兰",
    "New Zealand": "新西兰",
    "Norway": "挪威",
    "Panama": "巴拿马",
    "Paraguay": "巴拉圭",
    "Portugal": "葡萄牙",
    "Qatar": "卡塔尔",
    "Saudi Arabia": "沙特阿拉伯",
    "Scotland": "苏格兰",
    "Senegal": "塞内加尔",
    "South Africa": "南非",
    "South Korea": "韩国",
    "Spain": "西班牙",
    "Sweden": "瑞典",
    "Switzerland": "瑞士",
    "Tunisia": "突尼斯",
    "Turkey": "土耳其",
    "USA": "美国",
    "Uruguay": "乌拉圭",
    "Uzbekistan": "乌兹别克斯坦",
    # 常见俱乐部/其他国家队
    "China": "中国",
    "Italy": "意大利",
    "Ukraine": "乌克兰",
    "Poland": "波兰",
    "Denmark": "丹麦",
    "Romania": "罗马尼亚",
    "Serbia": "塞尔维亚",
    "Chile": "智利",
    "Peru": "秘鲁",
    "Venezuela": "委内瑞拉",
    "Nigeria": "尼日利亚",
    "Cameroon": "喀麦隆",
    "Mali": "马里",
    "Costa Rica": "哥斯达黎加",
    "Honduras": "洪都拉斯",
    "Jamaica": "牙买加",
    "Trinidad and Tobago": "特立尼达和多巴哥",
    "Guatemala": "危地马拉",
    "El Salvador": "萨尔瓦多",
    "Bolivia": "玻利维亚",
    "Cuba": "古巴",
    "Suriname": "苏里南",
    "Guadeloupe": "瓜德罗普",
    "Martinique": "马提尼克",
}


def team_zh(english_name: str) -> str:
    """英文队名转中文（所有输出必须使用此函数）"""
    zh = TEAM_NAME_ZH.get(english_name)
    if zh:
        return zh
    lower = english_name.lower().replace("the ", "").strip()
    for en, cn in TEAM_NAME_ZH.items():
        if lower in en.lower() or en.lower() in lower:
            return cn
    return english_name


class SPOddsAPI:
    """v6.0 统一实时赔率采集器（The Odds API 客户端）"""

    def __init__(self, config_path: Optional[str] = None):
        self._config = configparser.ConfigParser()
        self._config.read(config_path or str(CONFIG_PATH), encoding="utf-8")

        # 优先 .env (THEODDS_API_KEY), 回退 config.ini 旧值
        self.api_key = os.getenv("THEODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY") or self._config.get("api", "odds_api_key", fallback="")
        self.base_url = self._config.get("api", "odds_api_base", fallback="https://api.the-odds-api.com/v4")
        self.regions = self._config.get("analysis", "regions", fallback="eu,uk")
        self.odds_format = self._config.get("analysis", "odds_format", fallback="decimal")
        self.preferred_bookmaker = self._config.get("analysis", "preferred_bookmaker", fallback="pinnacle")

        if not self.api_key:
            raise ValueError("API密钥未配置，请在 config.ini 中设置 odds_api_key")

        # 中央预算护栏 (单例, 跨进程共享磁盘状态)
        self.guard = get_guard() if _HAS_GUARD else None

    def _req(self, url: str, params: dict, cache_group: str,
             timeout: int = 15) -> "object":
        """统一请求: 走预算护栏; 降级时裸 requests.get。
        返回类 requests.Response (status_code/text/headers/json)。
        """
        if self.guard is not None:
            return self.guard.guarded_get(url, params, cache_group=cache_group, timeout=timeout)
        # 降级: 裸请求 (无预算/缓存保护)
        try:
            resp = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            raise ConnectionError(f"API 请求失败(网络): {e}")
        return resp

    def _zh_to_en(self, chinese_name: str) -> str:
        """中文队名反查英文队名（用于API查询）"""
        for en, zh in TEAM_NAME_ZH.items():
            if zh == chinese_name:
                return en
        return chinese_name

    @property
    def _default_params(self) -> dict:
        return {
            "apiKey": self.api_key,
            "regions": self.regions,
            "oddsFormat": self.odds_format,
            "markets": "h2h,totals",
        }

    def get_remaining_requests(self) -> int:
        """查询剩余请求次数（走预算护栏，5min 缓存；无需为"查剩余"多烧一次）"""
        resp = self._req(f"{self.base_url}/sports/", {"apiKey": self.api_key},
                         cache_group="quota", timeout=15)
        if resp.status_code == 401:
            raise ConnectionError("API 密钥无效或配额耗尽 (401)")
        if resp.status_code != 200:
            # 预算耗尽或其他错误: 返回护栏记录的剩余值 (无则 -1)
            stored = self.guard.peek_remaining() if self.guard else None
            return stored if stored is not None else -1
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is None and self.guard is not None:
            remaining = self.guard.peek_remaining()
        return int(remaining) if remaining is not None else -1

    def get_sports(self) -> list:
        """获取所有可用赛事列表（24h 缓存）"""
        resp = self._req(f"{self.base_url}/sports/", {"apiKey": self.api_key},
                         cache_group="sports", timeout=15)
        if resp.status_code == 401:
            raise ConnectionError("API 密钥无效或配额耗尽 (401)")
        if resp.status_code == 200:
            return resp.json()
        raise ConnectionError(f"API请求失败: {resp.status_code} - {resp.text[:200]}")

    def get_world_cup_odds(self) -> list:
        """获取美加墨世界杯所有比赛的实时赔率"""
        return self.get_odds(WORLD_CUP_KEY)

    def get_odds(self, sport_key: str) -> list:
        """
        获取指定赛事的实时赔率（1h 缓存）

        参数:
            sport_key: 赛事标识，如 "soccer_fifa_world_cup"

        返回:
            list of match dicts
        """
        resp = self._req(
            f"{self.base_url}/sports/{sport_key}/odds/",
            self._default_params,
            cache_group="odds", timeout=15,
        )
        if resp.status_code == 401:
            raise ConnectionError("API 密钥无效或配额耗尽 (401)")
        if resp.status_code == 200:
            raw = resp.json()
            return [self._parse_match(m) for m in raw]
        raise ConnectionError(f"API请求失败: {resp.status_code} - {resp.text[:200]}")

    def _parse_match(self, raw_match: dict) -> dict:
        """解析单场比赛的赔率数据（队名自动转中文）"""
        home_en = raw_match.get("home_team", "")
        away_en = raw_match.get("away_team", "")

        result = {
            "id": raw_match.get("id"),
            "sport_key": raw_match.get("sport_key"),
            "sport_title": raw_match.get("sport_title"),
            "home_team": team_zh(home_en),
            "home_team_en": home_en,
            "away_team": team_zh(away_en),
            "away_team_en": away_en,
            "commence_time": raw_match.get("commence_time"),
            "bookmakers_raw": raw_match.get("bookmakers", []),
        }

        result["best_h2h"] = self._extract_best_h2h(
            raw_match["home_team"],
            raw_match.get("bookmakers", []),
        )
        # 逐庄 HDA 明细 (供 reverse_odds_engine 跨庄分歧/soft-line 检测; 修复: 旧版只落聚合best_h2h)
        result["bookmakers_detail"] = self._extract_bookmakers_h2h(
            raw_match["home_team"], raw_match["away_team"],
            raw_match.get("bookmakers", []),
        )
        result["totals"] = self._extract_totals(raw_match.get("bookmakers", []))

        if result["best_h2h"]:
            result["implied"] = self._calc_implied(result["best_h2h"])

        return result

    def _extract_best_h2h(self, home_team: str, bookmakers: list) -> dict:
        """提取最佳h2h赔率，Pinnacle优先，无则取中位数"""
        pinnacle_h2h = None
        all_h2h = {"home": [], "draw": [], "away": []}

        for bk in bookmakers:
            for market in bk.get("markets", []):
                if market["key"] != "h2h":
                    continue
                odds = {o["name"]: o["price"] for o in market["outcomes"]}
                h = odds.get(home_team)
                d = odds.get("Draw")
                a = odds.get(self._find_away(odds, home_team))
                if h and d and a:
                    if bk["key"] == "pinnacle" and pinnacle_h2h is None:
                        pinnacle_h2h = {"home": h, "draw": d, "away": a}
                    all_h2h["home"].append(h)
                    all_h2h["draw"].append(d)
                    all_h2h["away"].append(a)

        if pinnacle_h2h:
            return pinnacle_h2h
        if all_h2h["home"]:
            return {
                "home": sorted(all_h2h["home"])[len(all_h2h["home"]) // 2],
                "draw": sorted(all_h2h["draw"])[len(all_h2h["draw"]) // 2],
                "away": sorted(all_h2h["away"])[len(all_h2h["away"]) // 2],
                "source": "median",
            }
        return {}

    def _extract_bookmakers_h2h(self, home_team: str, away_team: str, bookmakers: list) -> list:
        """提取各庄家逐庄 HDA 明细 (供跨庄分歧检测).

        返回: [{name, h, d, a}, ...] 仅含合法 1X2 盘 (0<价 且 1.0<Σ1/价<1.30 防混入让球盘).
        """
        out = []
        seen = set()
        for bk in bookmakers:
            key = bk.get("key", "")
            for market in bk.get("markets", []):
                if market["key"] != "h2h":
                    continue
                odds = {o["name"]: o["price"] for o in market["outcomes"]}
                # 队名匹配: API outcomes 含 home_team / Draw / away_team
                h = odds.get(home_team)
                d = odds.get("Draw")
                a = None
                for nm, pr in odds.items():
                    if nm != home_team and nm != "Draw" and nm != away_team and a is None:
                        a = pr
                a = a or odds.get(away_team)
                if not (h and d and a):
                    continue
                try:
                    h, d, a = float(h), float(d), float(a)
                    inv = 1.0 / h + 1.0 / d + 1.0 / a
                    if not (1.0 < inv < 1.30):
                        continue  # 过滤让球/变盘线
                except (ValueError, TypeError, ZeroDivisionError):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                out.append({"name": key, "h": round(h, 3), "d": round(d, 3), "a": round(a, 3)})
        return out

    def _extract_totals(self, bookmakers: list) -> list:
        """提取大小球盘口，Pinnacle优先，去重"""
        raw = []
        for bk in bookmakers:
            for market in bk.get("markets", []):
                if market["key"] != "totals":
                    continue
                over_price = under_price = None
                point = None
                for outcome in market["outcomes"]:
                    if outcome.get("point") is not None:
                        point = outcome["point"]
                    if outcome["name"] == "Over":
                        over_price = outcome["price"]
                    elif outcome["name"] == "Under":
                        under_price = outcome["price"]
                if point is not None:
                    raw.append({
                        "point": point,
                        "over": over_price,
                        "under": under_price,
                        "bookmaker": bk["key"],
                    })
        best = {}
        for t in raw:
            key = str(t["point"])
            if key not in best or t["bookmaker"] == "pinnacle":
                best[key] = t
        return sorted(best.values(), key=lambda x: (0 if x["bookmaker"] == "pinnacle" else 1, x["point"]))

    @staticmethod
    def _find_away(odds: dict, home_team: str) -> str:
        """从odds字典中找到客队名"""
        for name in odds:
            if name != home_team and name != "Draw":
                return name
        return ""

    @staticmethod
    def _calc_implied(h2h: dict) -> dict:
        """计算隐含概率、overround、Kelly基准"""
        h_imp = 1.0 / h2h["home"]
        d_imp = 1.0 / h2h["draw"]
        a_imp = 1.0 / h2h["away"]
        total = h_imp + d_imp + a_imp
        overround = total - 1.0
        return {
            "home_prob": round(h_imp / total, 4),
            "draw_prob": round(d_imp / total, 4),
            "away_prob": round(a_imp / total, 4),
            "overround": round(overround, 4),
            "overround_pct": f"{overround * 100:.2f}%",
            "raw_home": round(h_imp, 4),
            "raw_draw": round(d_imp, 4),
            "raw_away": round(a_imp, 4),
        }

    # ─────────────────────────────────────────────
    # 实时落库：写入统一库的 live_odds_raw（自包含，无 FK 依赖）
    # ─────────────────────────────────────────────
    def _ensure_live_table(self, cur):
        cur.execute(
            """CREATE TABLE IF NOT EXISTS live_odds_raw (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport_key TEXT,
                home_team TEXT,
                away_team TEXT,
                home_team_en TEXT,
                away_team_en TEXT,
                commence_time TEXT,
                best_h2h TEXT,
                implied TEXT,
                totals TEXT,
                bookmakers_count INTEGER,
                bookmakers_detail TEXT,
                captured_at TEXT
            )"""
        )
        # 兼容旧表: 若已存在无该列则补 (SQLite 不支持 ADD COLUMN IF NOT EXISTS)
        try:
            cur.execute("ALTER TABLE live_odds_raw ADD COLUMN bookmakers_detail TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在

    def save_to_db(self, match: dict, db_path: Optional[str] = None) -> bool:
        """
        将API实时赔率快照存入统一库 live_odds_raw 表（自包含，不依赖 match_id）

        返回: True/False
        """
        db = db_path or str(DB_PATH)
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        self._ensure_live_table(cur)
        try:
            cur.execute(
                """INSERT INTO live_odds_raw
                   (sport_key, home_team, away_team, home_team_en, away_team_en,
                    commence_time, best_h2h, implied, totals, bookmakers_count,
                    bookmakers_detail, captured_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    match.get("sport_key"),
                    match.get("home_team"),
                    match.get("away_team"),
                    match.get("home_team_en"),
                    match.get("away_team_en"),
                    match.get("commence_time"),
                    json.dumps(match.get("best_h2h"), ensure_ascii=False),
                    json.dumps(match.get("implied"), ensure_ascii=False),
                    json.dumps(match.get("totals"), ensure_ascii=False),
                    len(match.get("bookmakers_raw", [])),
                    json.dumps(match.get("bookmakers_detail"), ensure_ascii=False),
                    datetime.now(timezone(timedelta(hours=8))).isoformat(),
                ),
            )
            conn.commit()
            return True
        except Exception as e:
            print(f"[ERROR] 保存实时赔率失败: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def get_all_football_odds(self) -> dict:
        """获取所有可用足球赛事的赔率"""
        sports = self.get_sports()
        football = [s for s in sports if s["group"] == "Soccer"]
        results = {}
        for s in football:
            key = s["key"]
            try:
                results[key] = self.get_odds(key)
            except Exception as e:
                results[key] = {"error": str(e)}
        return results

    def compare_with_image(self, api_odds: dict, image_odds: dict) -> dict:
        """
        API赔率 vs 图片赔率比对（固定分析协议要求）
        """
        comparison = {
            "api_source": api_odds.get("source", "the-odds-api"),
            "image_source": image_odds.get("source", "图片截图"),
            "differences": [],
            "signal": None,
        }
        for key, label in [("home", "主胜"), ("draw", "平局"), ("away", "客胜")]:
            api_val = api_odds.get(key)
            img_val = image_odds.get(key)
            if api_val is not None and img_val is not None:
                diff = round(img_val - api_val, 2)
                diff_pct = round(diff / api_val * 100, 2) if api_val != 0 else 0
                if abs(diff) < 0.05:
                    interp = "基本一致"
                elif diff > 0:
                    interp = "图片赔率更高，该方向回报更大（或API定价更保守）"
                else:
                    interp = "图片赔率更低，庄家在该方向定价更激进"
                comparison["differences"].append({
                    "direction": label,
                    "api_odds": api_val,
                    "image_odds": img_val,
                    "diff": diff,
                    "diff_pct": f"{diff_pct}%",
                    "interpretation": interp,
                })
        large_diffs = [d for d in comparison["differences"] if abs(d["diff"]) >= 0.1]
        if large_diffs:
            comparison["signal"] = f"发现{len(large_diffs)}处显著偏差，庄家在不同渠道定价不一致"
        return comparison

    def get_lineups(self, home_team: str, away_team: str, fixture_id: int = None) -> Optional[dict]:
        """
        获取双方球队首发阵容（固定分析协议要求）
        使用 api-football.com 免费API（需在 config.ini 配置 api_football_key）
        """
        api_key = self._config.get("api", "api_football_key", fallback="")
        if not api_key:
            return None
        try:
            headers = {"x-apisports-key": api_key}
            search_name = self._zh_to_en(home_team)
            if not fixture_id:
                resp = requests.get(
                    f"https://v3.football.api-sports.io/fixtures?search={search_name}",
                    headers=headers, timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for f in data.get("response", []):
                        h_name = f["teams"]["home"]["name"]
                        a_name = f["teams"]["away"]["name"]
                        if (home_team.lower() in h_name.lower() or
                            h_name.lower() in home_team.lower() or
                            team_zh(h_name) == home_team):
                            if (away_team.lower() in a_name.lower() or
                                a_name.lower() in away_team.lower() or
                                team_zh(a_name) == away_team):
                                fixture_id = f["fixture"]["id"]
                                break
            if not fixture_id:
                return None
            resp = requests.get(
                f"https://v3.football.api-sports.io/fixtures/lineups?fixture={fixture_id}",
                headers=headers, timeout=10,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            lineups_raw = data.get("response", [])
            if not lineups_raw:
                return None
            result = {
                "home": {"players": [], "formation": ""},
                "away": {"players": [], "formation": ""},
            }
            for lu in lineups_raw:
                team_name = lu["team"]["name"]
                formation = lu.get("formation", "")
                players = [p["player"]["name"] for p in lu.get("startXI", [])]
                if home_team.lower() in team_name.lower():
                    result["home"] = {"players": players, "formation": formation}
                elif away_team.lower() in team_name.lower():
                    result["away"] = {"players": players, "formation": formation}
            return result
        except Exception:
            return None

    def find_match_odds(self, home_team: str, away_team: str, sport_key: str = None) -> Optional[dict]:
        """
        查找指定比赛的API赔率（固定分析协议Step 1）
        支持中英文队名输入，自动双向匹配。所有返回队名为中文。
        """
        matches = self.get_odds(sport_key or WORLD_CUP_KEY) if sport_key else self.get_world_cup_odds()
        for m in matches:
            h = m.get("home_team", "")
            a = m.get("away_team", "")
            h_en = m.get("home_team_en", "")
            a_en = m.get("away_team_en", "")
            if (home_team in h and away_team in a) or (away_team in h and home_team in a):
                return m
            h_lower = h_en.lower()
            a_lower = a_en.lower()
            if (home_team.lower() in h_lower and away_team.lower() in a_lower) or \
               (away_team.lower() in h_lower and home_team.lower() in a_lower):
                return m
        return None


def main():
    """命令行入口：拉取美加墨世界杯赔率并显示 + 存入统一库 live_odds_raw"""
    api = SPOddsAPI()
    print(f"剩余请求次数: {api.get_remaining_requests()}")
    print()

    matches = api.get_world_cup_odds()
    for m in matches:
        h2h = m.get("best_h2h", {})
        implied = m.get("implied", {})
        print(f"  {m['home_team']} vs {m['away_team']}")
        print(f"    开赛: {m['commence_time']}")
        if h2h:
            print(f"    欧盘(Pinnacle优先): H={h2h.get('home', '?')}  D={h2h.get('draw', '?')}  A={h2h.get('away', '?')}")
        if implied:
            print(f"    隐含概率: H={implied['home_prob']:.1%}  D={implied['draw_prob']:.1%}  A={implied['away_prob']:.1%}")
            print(f"    Overround: {implied['overround_pct']}")
        totals = m.get("totals", [])
        if totals:
            for t in totals[:3]:
                print(f"    大小球: {t['point']} O={t.get('over', '?')} U={t.get('under', '?')} ({t['bookmaker']})")
        print()

    for m in matches:
        api.save_to_db(m)

    print(f"已存入统一库 live_odds_raw。剩余请求次数: {api.get_remaining_requests()}")


if __name__ == "__main__":
    main()
