"""
哨响AI - 增量更新引擎
=====================
- 只处理新比赛/变更数据，避免全量重算
- 支持：比赛数据、积分榜、表单趋势、特征计算
- 版本追踪：记录每个联赛最后同步的比赛日期与赛季

用法:
    updater = IncrementalUpdater(db, collector)
    report = updater.sync_all(league_codes=["PL","PD"])
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


class IncrementalUpdater:
    """
    增量数据同步引擎

    追踪每个 (league, season) 的同步状态，仅拉取比上次同步更新的数据。
    """

    # 同步检查间隔建议
    RECOMMENDED_INTERVALS = {
        "matches": 3600,       # 比赛数据：每小时
        "standings": 7200,     # 积分榜：每2小时
        "live_scores": 300,    # 实时比分：每5分钟
        "form_trends": 86400,  # 表单趋势：每天
        "odds": 1800,          # 赔率：每30分钟
    }

    def __init__(self, db, collector=None, cache_manager=None):
        """
        Args:
            db: DatabaseManager 实例
            collector: FootballDataCollector 实例（可选，用于远程采集）
            cache_manager: CacheManager 实例（可选，用于缓存）
        """
        self.db = db
        self.collector = collector
        self.cache = cache_manager

    # ══════════════════════════════════════════════════
    # 同步状态管理
    # ══════════════════════════════════════════════════

    def _ensure_sync_tracking_table(self):
        """创建同步追踪表"""
        with self.db.get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_tracker (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data_type TEXT NOT NULL,
                    league_name TEXT NOT NULL,
                    season TEXT,
                    last_synced_at TEXT NOT NULL,
                    last_match_date TEXT,
                    match_count INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'success',
                    error_message TEXT,
                    UNIQUE(data_type, league_name, season)
                )
            """)

    def get_last_sync(self, data_type: str, league_name: str,
                      season: str = None) -> Optional[Dict]:
        """获取上次同步状态"""
        self._ensure_sync_tracking_table()
        with self.db.get_connection() as conn:
            conn.row_factory = None
            if season:
                row = conn.execute(
                    "SELECT * FROM sync_tracker WHERE data_type=? AND league_name=? "
                    "AND season=? ORDER BY last_synced_at DESC LIMIT 1",
                    (data_type, league_name, season)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM sync_tracker WHERE data_type=? AND league_name=? "
                    "ORDER BY last_synced_at DESC LIMIT 1",
                    (data_type, league_name)
                ).fetchone()
            if row:
                cols = ["id", "data_type", "league_name", "season",
                        "last_synced_at", "last_match_date", "match_count",
                        "status", "error_message"]
                return dict(zip(cols, row))
        return None

    def update_sync_status(self, data_type: str, league_name: str,
                           new_match_count: int, last_match_date: str = None,
                           season: str = None, status: str = "success",
                           error_msg: str = None):
        """更新同步状态"""
        self._ensure_sync_tracking_table()
        now = datetime.now().isoformat()
        with self.db.get_connection() as conn:
            conn.execute("""
                INSERT INTO sync_tracker (data_type, league_name, season,
                                           last_synced_at, last_match_date,
                                           match_count, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(data_type, league_name, season) DO UPDATE SET
                    last_synced_at=excluded.last_synced_at,
                    last_match_date=excluded.last_match_date,
                    match_count=excluded.match_count,
                    status=excluded.status,
                    error_message=excluded.error_message
            """, (data_type, league_name, season, now, last_match_date,
                  new_match_count, status, error_msg))

    # ══════════════════════════════════════════════════
    # 增量同步核心逻辑
    # ══════════════════════════════════════════════════

    def get_new_matches_candidates(self, league_name: str,
                                   season: str = None) -> Dict:
        """
        对比本地数据库与远端（若有 collector 则查询远端），
        返回仅需拉取的比赛日期范围

        Returns:
            {"date_from": "2025-01-01", "date_to": "2025-06-01", "new_count_estimate": 50}
        """
        with self.db.get_connection() as conn:
            conn.row_factory = None
            latest = conn.execute(
                "SELECT MAX(match_date) FROM matches WHERE league_name=?",
                (league_name,)
            ).fetchone()[0]

        if latest:
            date_from = latest
        else:
            date_from = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        date_to = datetime.now().strftime("%Y-%m-%d")

        days = (datetime.strptime(date_to, "%Y-%m-%d") -
                datetime.strptime(date_from, "%Y-%m-%d")).days
        estimate = max(0, days * 8)

        return {
            "date_from": date_from,
            "date_to": date_to,
            "new_count_estimate": min(estimate, 200),
        }

    def sync_matches_incremental(self, league_name: str,
                                 league_api_code: str = None,
                                 season: str = None) -> Dict:
        """
        增量同步比赛数据：仅拉取比本地最新日期新的数据

        Returns:
            {"synced": 15, "skipped": 120, "new": 15, "errors": 0}
        """
        if self.collector is None:
            logger.warning("[IncrSync] 未配置 collector，无法远程同步")
            return {"synced": 0, "skipped": 0, "new": 0,
                    "errors": 0, "error": "no_collector"}

        try:
            info = self.get_new_matches_candidates(league_name, season)
            date_from = info["date_from"]
            date_to = info["date_to"]

            logger.info(f"[IncrSync] {league_name}: 拉取 {date_from} ~ {date_to}")

            results = self.collector.fetch_matches_since(
                league_code=league_api_code or league_name,
                date_from=date_from,
                date_to=date_to,
                store_to_db=True,
                db_manager=self.db,
            )

            synced = results.get("stored", 0) if isinstance(results, dict) else 0
            self.update_sync_status(
                "matches", league_name, synced, date_to, season
            )

            logger.info(f"[IncrSync] {league_name}: 同步 {synced} 场新比赛")
            return {
                "synced": synced,
                "skipped": info["new_count_estimate"] - synced,
                "new": synced,
                "errors": 0,
                "date_range": f"{date_from} ~ {date_to}",
            }

        except (Exception, KeyError, IndexError) as e:
            logger.error(f"[IncrSync] {league_name} 失败: {e}")
            self.update_sync_status(
                "matches", league_name, 0, None, season,
                status="failed", error_msg=str(e)
            )
            return {"synced": 0, "skipped": 0, "new": 0, "errors": 1, "error": str(e)}

    def should_sync(self, data_type: str, league_name: str,
                    season: str = None) -> Tuple[bool, str]:
        """
        判断是否需要同步

        Returns:
            (should_sync, reason)
        """
        last = self.get_last_sync(data_type, league_name, season)
        if last is None:
            return True, "首次同步"

        interval = self.RECOMMENDED_INTERVALS.get(data_type, 3600)
        last_time = datetime.fromisoformat(last["last_synced_at"])
        elapsed = (datetime.now() - last_time).total_seconds()

        if elapsed < interval:
            remaining = interval - elapsed
            return False, f"距上次同步仅 {int(elapsed)}s (最小间隔 {interval}s)"

        if last.get("status") == "failed":
            return True, "上次同步失败，重试"

        return True, f"距上次同步 {int(elapsed)}s >= 间隔 {interval}s"

    def sync_features_incremental(self, league_name: str = None) -> Dict:
        """增量计算特征：仅对最近状态变化的比赛"""
        with self.db.get_connection() as conn:
            conn.row_factory = None
            query = """
                SELECT m.match_id, m.league_name, m.home_team_name, m.away_team_name
                FROM matches m
                JOIN match_features mf ON m.match_id = mf.match_id
                WHERE m.match_date >= date('now', '-7 days')
                  AND m.status IN ('finished', 'live')
            """
            params = []
            if league_name:
                query += " AND m.league_name = ?"
                params.append(league_name)
            query += " ORDER BY m.match_date DESC LIMIT 200"
            rows = conn.execute(query, params).fetchall()

        if len(rows) == 0:
            return {"updated": 0, "skipped": 0, "message": "无需要更新的特征"}

        from features.feature_calculator import FeatureCalculator
        calc = FeatureCalculator(self.db)

        updated = 0
        errors = 0
        for row in rows:
            try:
                match_id, lg, home, away = row
                calc.update_single_match_features(match_id, home, away, lg)
                updated += 1
            except (Exception) as e:
                logger.error(f"[IncrSync] 特征更新失败 match_id={row[0]}: {e}")
                errors += 1

        logger.info(f"[IncrSync] 特征增量更新: {updated} 成功, {errors} 失败")
        return {"updated": updated, "errors": errors, "skipped": 0}

    def perform_smart_sync(self, league_codes: List[str] = None,
                           data_types: List[str] = None
                           ) -> Dict[str, Dict]:
        """
        智能同步：自动判断每个联赛是否需要同步

        Args:
            league_codes: 联赛代码列表，None=所有
            data_types: 数据类型列表，None=["matches", "standings", "features"]

        Returns:
            {league_code: {data_type: result_dict}}
        """
        if league_codes is None:
            with self.db.get_connection() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT league_name FROM matches ORDER BY league_name"
                ).fetchall()
            league_codes = [r[0] for r in rows]

        if data_types is None:
            data_types = ["matches", "standings", "features"]

        results = {}
        for league in league_codes:
            league_results = {}
            for dtype in data_types:
                should, reason = self.should_sync(dtype, league)
                if not should:
                    league_results[dtype] = {"status": "skipped", "reason": reason}
                    continue

                if dtype == "matches":
                    league_results[dtype] = self.sync_matches_incremental(league)
                elif dtype == "features":
                    league_results[dtype] = self.sync_features_incremental(league)
                else:
                    league_results[dtype] = {"status": "pending",
                                             "reason": f"未实现 {dtype} 增量同步"}

            results[league] = league_results

        return results

    def get_sync_summary(self) -> Dict:
        """获取全局同步状态摘要"""
        self._ensure_sync_tracking_table()
        with self.db.get_connection() as conn:
            conn.row_factory = None
            rows = conn.execute(
                "SELECT league_name, data_type, last_synced_at, match_count, status "
                "FROM sync_tracker ORDER BY league_name, data_type"
            ).fetchall()

        summary = defaultdict(dict)
        for league, dtype, synced_at, count, status in rows:
            hours_ago = None
            if synced_at:
                elapsed = (datetime.now() - datetime.fromisoformat(synced_at))
                hours_ago = round(elapsed.total_seconds() / 3600, 1)
            summary[league][dtype] = {
                "last_synced": synced_at,
                "hours_ago": hours_ago,
                "count": count,
                "status": status,
            }

        return dict(summary)
