"""
football_data_collector.py — football-data.org 实时数据接入 (哨响AI P3)

数据源: https://api.football-data.org/v4
免费版限制: 10 req/min, 无实时比分 (仅完赛/赛程/积分榜)

接入计划:
  1. 联赛/赛事列表 → competitions 表
  2. 完赛结果 → match_results 表 (与现有 matches 表交叉引用)
  3. 未来赛程 → upcoming_fixtures 表
  4. 积分榜 → standings 表

用法:
  python -m pipeline.collectors.football_data_collector --competitions  # 刷新联赛列表
  python -m pipeline.collectors.football_data_collector --results       # 拉取近期赛果
  python -m pipeline.collectors.football_data_collector --fixtures      # 拉取未来赛程
  python -m pipeline.collectors.football_data_collector --standings     # 拉取积分榜
  python -m pipeline.collectors.football_data_collector --all           # 全量同步

API Key 配置:
  环境变量 FOOTBALL_DATA_API_KEY (.env 中已配置)
"""

import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

# ── 路径 ──
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "football_data.db"

# ── API 配置 ──
API_BASE = "https://api.football-data.org/v4"
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")

# 免费版限速: 10 req/min → 6s间隔
RATE_LIMIT_INTERVAL = 6.0

# 可获取的联赛 ID (2024/25 赛季活跃, 免费版准入)
# 注: 免费版仅限部分 Tier 1-2 联赛
AVAILABLE_COMPETITIONS = [
    2021,  # Premier League (英格兰)
    2019,  # Serie A (意大利)
    2002,  # Bundesliga (德国)
    2014,  # La Liga (西班牙)
    2015,  # Ligue 1 (法国)
    2003,  # Eredivisie (荷兰)
    2017,  # Primeira Liga (葡萄牙)
    2016,  # Championship (英格兰二级)
    2001,  # UEFA Champions League
    2000,  # FIFA World Cup
]


class FootballDataCollector:
    """football-data.org v4 API 客户端"""

    def __init__(self, api_key: str = "", db_path: str = str(DB_PATH)):
        self.api_key = api_key or API_KEY
        if not self.api_key:
            raise ValueError("FOOTBALL_DATA_API_KEY 未配置 (检查 .env)")
        self.db_path = db_path
        self._last_request = 0.0

    # ── 底层 HTTP ──

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < RATE_LIMIT_INTERVAL:
            time.sleep(RATE_LIMIT_INTERVAL - elapsed)
        self._last_request = time.time()

    def _get(self, endpoint: str) -> dict:
        """GET 请求, 含限速 + 错误处理"""
        self._rate_limit()
        url = f"{API_BASE}{endpoint}"
        req = urllib.request.Request(url, headers={"X-Auth-Token": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:500] if e.fp else ""
            print(f"[HTTP {e.code}] {url} → {body}", file=sys.stderr)
            return {}
        except Exception as e:
            print(f"[ERR] {url}: {e}", file=sys.stderr)
            return {}

    # ── 数据库初始化 ──

    def _ensure_tables(self, conn: sqlite3.Connection):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fd_competitions (
                id INTEGER PRIMARY KEY,
                code TEXT, name TEXT, area_name TEXT,
                current_season_start TEXT, current_season_end TEXT,
                last_updated TEXT
            );
            CREATE TABLE IF NOT EXISTS fd_match_results (
                match_id INTEGER PRIMARY KEY,
                competition_id INTEGER, competition_name TEXT,
                match_date TEXT, status TEXT,
                home_team TEXT, away_team TEXT,
                home_score INTEGER, away_score INTEGER,
                winner TEXT, duration TEXT,
                season_start TEXT, season_end TEXT,
                fetched_at TEXT
            );
            CREATE TABLE IF NOT EXISTS fd_upcoming_fixtures (
                match_id INTEGER PRIMARY KEY,
                competition_id INTEGER, competition_name TEXT,
                match_date TEXT, status TEXT,
                home_team TEXT, away_team TEXT,
                season_start TEXT, season_end TEXT,
                fetched_at TEXT
            );
            CREATE TABLE IF NOT EXISTS fd_standings (
                competition_id INTEGER, competition_name TEXT,
                stage TEXT, group_name TEXT,
                position INTEGER, team_name TEXT,
                played INTEGER, won INTEGER, draw INTEGER, lost INTEGER,
                goals_for INTEGER, goals_against INTEGER,
                goal_difference INTEGER, points INTEGER,
                fetched_at TEXT,
                PRIMARY KEY (competition_id, stage, group_name, position)
            );
        """)
        conn.commit()

    # ── 数据拉取 ──

    def fetch_competitions(self) -> List[Dict]:
        """拉取可访问的联赛列表"""
        data = self._get("/competitions")
        comps = data.get("competitions", [])
        conn = sqlite3.connect(self.db_path)
        self._ensure_tables(conn)
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for c in comps:
            season = c.get("currentSeason", {}) or {}
            conn.execute(
                """INSERT OR REPLACE INTO fd_competitions (id, code, name, area_name,
                   current_season_start, current_season_end, last_updated)
                   VALUES (?,?,?,?,?,?,?)""",
                (c["id"], c.get("code"), c["name"],
                 c.get("area", {}).get("name"),
                 season.get("startDate"), season.get("endDate"), now),
            )
            count += 1
        conn.commit()
        conn.close()
        print(f"[competitions] 拉取/更新 {count} 个联赛")
        return comps

    def fetch_results(self, competition_ids: Optional[List[int]] = None,
                      date_from: str = "", date_to: str = "",
                      limit_days: int = 30) -> int:
        """拉取完赛结果。

        date_from/date_to 格式: YYYY-MM-DD
        默认拉取最近 limit_days 天
        """
        if competition_ids is None:
            competition_ids = AVAILABLE_COMPETITIONS

        if not date_from:
            from datetime import timedelta
            date_from = (datetime.now() - timedelta(days=limit_days)).strftime("%Y-%m-%d")
        if not date_to:
            date_to = datetime.now().strftime("%Y-%m-%d")

        conn = sqlite3.connect(self.db_path)
        self._ensure_tables(conn)
        now = datetime.now(timezone.utc).isoformat()
        total = 0

        for comp_id in competition_ids:
            # 拉取该联赛在日期范围内的比赛
            endpoint = f"/competitions/{comp_id}/matches?dateFrom={date_from}&dateTo={date_to}&status=FINISHED"
            data = self._get(endpoint)
            matches = data.get("matches", [])
            comp_name = data.get("competition", {}).get("name", f"comp_{comp_id}")
            season = data.get("season", {}) or {}

            for m in matches:
                score = m.get("score", {}) or {}
                full_time = score.get("fullTime", {}) or {}
                conn.execute(
                    """INSERT OR REPLACE INTO fd_match_results
                       (match_id, competition_id, competition_name, match_date, status,
                        home_team, away_team, home_score, away_score, winner, duration,
                        season_start, season_end, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (m["id"], comp_id, comp_name,
                     m.get("utcDate"), m.get("status"),
                     m.get("homeTeam", {}).get("name"),
                     m.get("awayTeam", {}).get("name"),
                     full_time.get("home"), full_time.get("away"),
                     score.get("winner"), score.get("duration"),
                     season.get("startDate"), season.get("endDate"), now),
                )
                total += 1

            print(f"  [{comp_name}] {len(matches)} 完赛 (日期 {date_from}~{date_to})")

        conn.commit()
        conn.close()
        print(f"[results] 总计拉取 {total} 场完赛")
        return total

    def fetch_fixtures(self, competition_ids: Optional[List[int]] = None,
                       limit_days: int = 14) -> int:
        """拉取未来赛程 (SCHEDULED + TIMED)"""
        if competition_ids is None:
            competition_ids = AVAILABLE_COMPETITIONS

        date_from = datetime.now().strftime("%Y-%m-%d")
        from datetime import timedelta
        date_to = (datetime.now() + timedelta(days=limit_days)).strftime("%Y-%m-%d")

        conn = sqlite3.connect(self.db_path)
        self._ensure_tables(conn)
        now = datetime.now(timezone.utc).isoformat()
        total = 0

        for comp_id in competition_ids:
            endpoint = f"/competitions/{comp_id}/matches?dateFrom={date_from}&dateTo={date_to}&status=SCHEDULED"
            data = self._get(endpoint)
            matches = data.get("matches", [])
            comp_name = data.get("competition", {}).get("name", f"comp_{comp_id}")
            season = data.get("season", {}) or {}

            for m in matches:
                conn.execute(
                    """INSERT OR REPLACE INTO fd_upcoming_fixtures
                       (match_id, competition_id, competition_name, match_date, status,
                        home_team, away_team, season_start, season_end, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (m["id"], comp_id, comp_name,
                     m.get("utcDate"), m.get("status"),
                     m.get("homeTeam", {}).get("name"),
                     m.get("awayTeam", {}).get("name"),
                     season.get("startDate"), season.get("endDate"), now),
                )
                total += 1

            print(f"  [{comp_name}] {len(matches)} 赛程 (未来{limit_days}天)")

        conn.commit()
        conn.close()
        print(f"[fixtures] 总计拉取 {total} 场未来赛程")
        return total

    def fetch_standings(self, competition_ids: Optional[List[int]] = None) -> int:
        """拉取积分榜"""
        if competition_ids is None:
            competition_ids = AVAILABLE_COMPETITIONS

        conn = sqlite3.connect(self.db_path)
        self._ensure_tables(conn)
        now = datetime.now(timezone.utc).isoformat()
        total = 0

        for comp_id in competition_ids:
            endpoint = f"/competitions/{comp_id}/standings"
            data = self._get(endpoint)
            standings = data.get("standings", [])
            comp_name = data.get("competition", {}).get("name", f"comp_{comp_id}")

            for stage_entry in standings:
                stage = stage_entry.get("stage", "REGULAR_SEASON")
                group = stage_entry.get("group") or ""
                table = stage_entry.get("table", [])

                for row in table:
                    conn.execute(
                        """INSERT OR REPLACE INTO fd_standings
                           (competition_id, competition_name, stage, group_name,
                            position, team_name, played, won, draw, lost,
                            goals_for, goals_against, goal_difference, points, fetched_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (comp_id, comp_name, stage, group,
                         row.get("position"), row.get("team", {}).get("name"),
                         row.get("playedGames"), row.get("won"), row.get("draw"), row.get("lost"),
                         row.get("goalsFor"), row.get("goalsAgainst"),
                         row.get("goalDifference"), row.get("points"), now),
                    )
                    total += 1

            print(f"  [{comp_name}] {len(standings)} 阶段/组, {total} 行")

        conn.commit()
        conn.close()
        print(f"[standings] 总计拉取 {total} 行积分榜")
        return total

    def sync_all(self) -> Dict[str, int]:
        """全量同步: 联赛 → 赛果 → 赛程 → 积分榜"""
        print("=" * 60)
        print("football-data.org 全量同步")
        print("=" * 60)

        results = {}

        print("\n[1/4] 联赛列表...")
        results["competitions"] = len(self.fetch_competitions())

        print("\n[2/4] 近期赛果...")
        results["results"] = self.fetch_results(limit_days=30)

        print("\n[3/4] 未来赛程...")
        results["fixtures"] = self.fetch_fixtures(limit_days=14)

        print("\n[4/4] 积分榜...")
        results["standings"] = self.fetch_standings()

        print(f"\n[done] 同步完成: {results}")
        return results


# ── CLI ──

def main():
    import argparse
    ap = argparse.ArgumentParser(description="football-data.org 数据接入")
    ap.add_argument("--competitions", action="store_true", help="刷新联赛列表")
    ap.add_argument("--results", action="store_true", help="拉取近期赛果")
    ap.add_argument("--fixtures", action="store_true", help="拉取未来赛程")
    ap.add_argument("--standings", action="store_true", help="拉取积分榜")
    ap.add_argument("--all", action="store_true", help="全量同步")
    ap.add_argument("--days", type=int, default=30, help="赛果/赛程拉取天数 (默认30)")
    ap.add_argument("--competition", type=int, nargs="*",
                    help="指定联赛ID (默认: 欧洲TOP5+欧冠+世界杯)")

    args = ap.parse_args()

    if not API_KEY:
        print("ERROR: FOOTBALL_DATA_API_KEY 未设置 (检查 .env)", file=sys.stderr)
        sys.exit(1)

    collector = FootballDataCollector()
    comp_ids = args.competition if args.competition else None

    if args.all or (not any([args.competitions, args.results, args.fixtures, args.standings])):
        collector.sync_all()
        return

    if args.competitions:
        collector.fetch_competitions()
    if args.results:
        collector.fetch_results(competition_ids=comp_ids, limit_days=args.days)
    if args.fixtures:
        collector.fetch_fixtures(competition_ids=comp_ids, limit_days=args.days)
    if args.standings:
        collector.fetch_standings(competition_ids=comp_ids)


if __name__ == "__main__":
    main()
