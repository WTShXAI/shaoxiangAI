"""
每日数据飞轮采集器 (DailyCollector)
====================================
自动运转的数据飞轮，持续增长训练数据：

  ① collect_daily_odds()    智能拉取活跃联赛多庄赔率 → live_odds_raw
  ② backfill_results()      拉已结束比赛赛果 → 回填 actual_result
  ③ sync_to_odds_features() 有赛果数据 → odds_features（增长训练集）

智能筛选（用户要求）：首次全量探测34联赛，后续只拉有比赛的活跃联赛。

用法:
    from pipeline.collectors.daily_collector import DailyCollector
    dc = DailyCollector()
    dc.collect_daily_odds()       # 智能拉取
    dc.backfill_results()         # 赛果回填
    dc.sync_to_odds_features()    # 训练数据同步
"""
from __future__ import annotations
import os, sys, json, sqlite3, logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "football_data.db")

# 34 联赛目录 (与 bridge_service.py LEAGUE_CATALOG 同步)
LEAGUE_CATALOG: Dict[str, Dict[str, str]] = {
    "soccer_epl": {"name": "英超", "category": "五大联赛"},
    "soccer_spain_la_liga": {"name": "西甲", "category": "五大联赛"},
    "soccer_italy_serie_a": {"name": "意甲", "category": "五大联赛"},
    "soccer_germany_bundesliga": {"name": "德甲", "category": "五大联赛"},
    "soccer_france_ligue_one": {"name": "法甲", "category": "五大联赛"},
    "soccer_efl_champ": {"name": "英冠", "category": "英格兰联赛"},
    "soccer_england_league1": {"name": "英甲", "category": "英格兰联赛"},
    "soccer_england_league2": {"name": "英乙", "category": "英格兰联赛"},
    "soccer_england_efl_cup": {"name": "联赛杯", "category": "英格兰联赛"},
    "soccer_germany_bundesliga2": {"name": "德乙", "category": "德国联赛"},
    "soccer_germany_liga3": {"name": "德丙", "category": "德国联赛"},
    "soccer_germany_dfb_pokal": {"name": "德国杯", "category": "德国联赛"},
    "soccer_sweden_allsvenskan": {"name": "瑞典超", "category": "北欧"},
    "soccer_sweden_superettan": {"name": "瑞典甲", "category": "北欧"},
    "soccer_norway_eliteserien": {"name": "挪威超", "category": "北欧"},
    "soccer_denmark_superliga": {"name": "丹麦超", "category": "北欧"},
    "soccer_finland_veikkausliiga": {"name": "芬兰超", "category": "北欧"},
    "soccer_brazil_serie_a": {"name": "巴甲", "category": "美洲"},
    "soccer_brazil_serie_b": {"name": "巴乙", "category": "美洲"},
    "soccer_argentina_primera_division": {"name": "阿根廷", "category": "美洲"},
    "soccer_mexico_ligamx": {"name": "墨西哥", "category": "美洲"},
    "soccer_usa_mls": {"name": "MLS", "category": "美洲"},
    "soccer_conmebol_copa_libertadores": {"name": "解放者杯", "category": "美洲"},
    "soccer_conmebol_copa_sudamericana": {"name": "南美杯", "category": "美洲"},
    "soccer_china_superleague": {"name": "中超", "category": "亚洲/其他"},
    "soccer_korea_kleague1": {"name": "韩K联", "category": "亚洲/其他"},
    "soccer_ireland_premier": {"name": "爱尔兰超", "category": "亚洲/其他"},
    "soccer_fifa_world_cup": {"name": "世界杯", "category": "杯赛/国际"},
    "soccer_uefa_europa_league": {"name": "欧联杯", "category": "杯赛/国际"},
    "soccer_uefa_champs_league": {"name": "欧冠", "category": "杯赛/国际"},
    "soccer_scotland_premiership": {"name": "苏格兰超", "category": "杯赛/国际"},
    "soccer_switzerland_superleague": {"name": "瑞士超", "category": "杯赛/国际"},
    "soccer_austria_bundesliga": {"name": "奥地利超", "category": "杯赛/国际"},
}

QUOTA_WARN_THRESHOLD = 50


class DailyCollector:
    """每日数据飞轮采集器"""
    # 类级缓存: 活跃联赛跨实例共享 (避免 _live_odds_mini_loop 每次全量拉取)
    _active_leagues_cache: Optional[set] = None
    _cache_timestamp: Optional[float] = None
    CACHE_TTL_SECONDS = 3600  # 1小时过期, 联赛变化慢

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        self._last_quota_remaining: Optional[int] = None

    def _get_api(self):
        """延迟导入 SPOddsAPI（避免循环依赖, 确保 PROJECT_ROOT 在 path）"""
        if PROJECT_ROOT not in sys.path:
            sys.path.insert(0, PROJECT_ROOT)
        from pipeline.collectors.sp_odds_api import SPOddsAPI
        return SPOddsAPI()

    # ════════════════════════════════════════
    # ① 智能拉取
    # ════════════════════════════════════════
    def collect_daily_odds(self, force_full: bool = False) -> Dict:
        """
        智能拉取当天赛事赔率。
        首次或 force_full=True 时全量拉34联赛探测；
        后续只拉缓存的活跃联赛（省配额）。

        Returns: {collected, skipped, active_leagues, remaining_quota, error?}
        """
        stats = {"collected": 0, "skipped": 0, "active_leagues": [],
                 "remaining_quota": None, "error": None}
        try:
            api = self._get_api()
            remaining = api.get_remaining_requests()
            self._last_quota_remaining = remaining
            stats["remaining_quota"] = remaining
            if remaining <= 0:
                stats["error"] = "API配额耗尽"
                logger.warning("DailyCollector: API配额耗尽，跳过拉取")
                return stats

            # 确定要拉的联赛 (使用类级缓存, 跨实例共享)
            now_ts = datetime.now().timestamp()
            cache_valid = (
                DailyCollector._active_leagues_cache is not None
                and DailyCollector._cache_timestamp is not None
                and (now_ts - DailyCollector._cache_timestamp) < DailyCollector.CACHE_TTL_SECONDS
            )
            if force_full or not cache_valid:
                leagues_to_pull = list(LEAGUE_CATALOG.keys())
                logger.info(f"DailyCollector: 全量模式，探测 {len(leagues_to_pull)} 个联赛")
            else:
                leagues_to_pull = list(DailyCollector._active_leagues_cache)
                logger.info(f"DailyCollector: 增量模式，只拉 {len(leagues_to_pull)} 个活跃联赛")

            new_active = set()
            for sk in leagues_to_pull:
                if api.get_remaining_requests() <= 0:
                    logger.warning("DailyCollector: 配额耗尽，停止拉取")
                    break
                try:
                    matches = api.get_odds(sk)
                    if matches:
                        for m in matches:
                            try:
                                api.save_to_db(m)
                            except Exception:
                                pass
                        stats["collected"] += len(matches)
                        new_active.add(sk)
                    else:
                        stats["skipped"] += 1
                        if force_full:
                            logger.debug(f"  {sk}: 今日无赛事")
                except Exception as e:
                    stats["skipped"] += 1
                    logger.debug(f"  {sk}: 拉取失败 {e}")

            # 更新活跃联赛缓存 (类级) + 配额
            if new_active:
                DailyCollector._active_leagues_cache = new_active
                DailyCollector._cache_timestamp = datetime.now().timestamp()
            stats["active_leagues"] = list(new_active) if new_active else list(DailyCollector._active_leagues_cache or [])
            stats["remaining_quota"] = api.get_remaining_requests()
            self._last_quota_remaining = stats["remaining_quota"]

            if stats["remaining_quota"] < QUOTA_WARN_THRESHOLD:
                logger.warning(f"DailyCollector: 配额低! 剩余 {stats['remaining_quota']}")

            logger.info(f"DailyCollector 完成: 采集 {stats['collected']} 场, "
                        f"活跃 {len(stats['active_leagues'])} 联赛, "
                        f"剩余配额 {stats['remaining_quota']}")
        except Exception as e:
            stats["error"] = str(e)
            logger.error(f"DailyCollector 拉取失败: {e}")
        return stats

    # ════════════════════════════════════════
    # ② 赛果回填
    # ════════════════════════════════════════
    def backfill_results(self) -> Dict:
        """
        回填已结束比赛的赛果。
        扫描 live_odds_raw 中 commence_time 已过但无 actual_result 的比赛，
        判定胜负（需外部赛果源，当前用 commence_time 过期 + 手动回填兜底）。

        Returns: {scanned, backfilled, pending, error?}
        """
        stats = {"scanned": 0, "backfilled": 0, "pending": 0, "error": None}
        try:
            # 先确保 actual_result 列存在
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            try:
                cur.execute("ALTER TABLE live_odds_raw ADD COLUMN actual_result TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                cur.execute("ALTER TABLE live_odds_raw ADD COLUMN actual_score TEXT")
            except sqlite3.OperationalError:
                pass

            now_iso = datetime.now(timezone.utc).isoformat()
            # 查找已过开赛时间但未回填赛果的比赛
            rows = cur.execute(
                """SELECT id, sport_key, home_team, away_team, commence_time
                   FROM live_odds_raw
                   WHERE (actual_result IS NULL OR actual_result = '')
                     AND commence_time IS NOT NULL
                     AND commence_time < ?
                   GROUP BY home_team, away_team
                   ORDER BY commence_time DESC LIMIT 200""",
                (now_iso,)
            ).fetchall()
            stats["scanned"] = len(rows)
            conn.close()

            # 尝试用 football-data.org 拉世界杯赛果（其他联赛暂标记为待手动回填）
            wc_results = self._fetch_wc_results()

            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            for row in rows:
                rid, sk, home, away, ct = row
                # 匹配世界杯赛果
                matched = self._match_result(home, away, wc_results)
                if matched:
                    result, score = matched
                    cur.execute(
                        "UPDATE live_odds_raw SET actual_result=?, actual_score=? WHERE id=?",
                        (result, score, rid))
                    stats["backfilled"] += 1
                else:
                    stats["pending"] += 1
            conn.commit()
            conn.close()
            logger.info(f"赛果回填: 扫描 {stats['scanned']}, 回填 {stats['backfilled']}, "
                        f"待手动 {stats['pending']}")
        except Exception as e:
            stats["error"] = str(e)
            logger.error(f"赛果回填失败: {e}")
        return stats

    def _fetch_wc_results(self) -> List[Dict]:
        """拉世界杯已完赛结果"""
        try:
            from data_collector.football_data_live import FootballDataLive
            fdl = FootballDataLive()
            finished = fdl.get_wc2026_finished()
            results = []
            for m in finished:
                ft = m.get('score', {}).get('fullTime', {})
                hs, aws = ft.get('home'), ft.get('away')
                if hs is None or aws is None:
                    continue
                result = 'H' if hs > aws else ('A' if hs < aws else 'D')
                results.append({
                    'home': m.get('homeTeam', {}).get('name', ''),
                    'away': m.get('awayTeam', {}).get('name', ''),
                    'result': result,
                    'score': f"{hs}-{aws}",
                })
            return results
        except Exception as e:
            logger.debug(f"拉世界杯赛果失败: {e}")
            return []

    def _match_result(self, home: str, away: str, results: List[Dict]):
        """模糊匹配赛果"""
        home_l = (home or '').lower().strip()
        away_l = (away or '').lower().strip()
        for r in results:
            rh = (r.get('home') or '').lower().strip()
            ra = (r.get('away') or '').lower().strip()
            if (home_l in rh or rh in home_l) and (away_l in ra or ra in away_l):
                return r['result'], r['score']
        return None

    # ════════════════════════════════════════
    # ③ 训练数据同步 → odds_features
    # ════════════════════════════════════════
    def sync_to_odds_features(self) -> Dict:
        """
        将已回填赛果的 live_odds_raw 数据同步到 odds_features 表（增量增长训练集）。
        从 bookmakers_detail 提取开盘/收盘/drift/outcome。

        Returns: {synced, skipped, total_odds_features, error?}
        """
        stats = {"synced": 0, "skipped": 0, "total_odds_features": 0, "error": None}
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # 确保 odds_features 表存在
            of_exists = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='odds_features'"
            ).fetchone()
            if not of_exists:
                stats["error"] = "odds_features 表不存在"
                conn.close()
                return stats

            # 查有赛果但未同步到 odds_features 的 live_odds_raw
            rows = cur.execute(
                """SELECT lr.id, lr.home_team, lr.away_team, lr.commence_time,
                          lr.sport_key, lr.best_h2h, lr.bookmakers_detail,
                          lr.actual_result, lr.actual_score, lr.captured_at
                   FROM live_odds_raw lr
                   WHERE lr.actual_result IS NOT NULL AND lr.actual_result != ''
                     AND lr.actual_result IN ('H','D','A')"""
            ).fetchall()

            for row in rows:
                try:
                    best_h2h = json.loads(row['best_h2h']) if row['best_h2h'] else {}
                    oh = best_h2h.get('home', 0)
                    od = best_h2h.get('draw', 0)
                    oa = best_h2h.get('away', 0)
                    if not (oh > 1 and od > 1 and oa > 1):
                        stats["skipped"] += 1
                        continue

                    # 从 bookmakers_detail 取多庄数据做开盘/收盘近似
                    bm_detail = json.loads(row['bookmakers_detail']) if row['bookmakers_detail'] else []
                    open_h, open_d, open_a = oh, od, oa  # 默认 open=close (单快照无 drift)
                    close_h, close_d, close_a = oh, od, oa
                    if len(bm_detail) >= 2:
                        # 多庄: 取中位数作 close, 第一家作 open 近似
                        hs = sorted([b.get('h', oh) for b in bm_detail if b.get('h')])
                        ds = sorted([b.get('d', od) for b in bm_detail if b.get('d')])
                        as_ = sorted([b.get('a', oa) for b in bm_detail if b.get('a')])
                        if hs:
                            close_h = hs[len(hs)//2]
                            open_h = bm_detail[0].get('h', oh)
                        if ds:
                            close_d = ds[len(ds)//2]
                            open_d = bm_detail[0].get('d', od)
                        if as_:
                            close_a = as_[len(as_)//2]
                            open_a = bm_detail[0].get('a', oa)

                    # drift
                    drift_h = (close_h - open_h) / open_h if open_h > 0 else 0
                    drift_d = (close_d - open_d) / open_d if open_d > 0 else 0
                    drift_a = (close_a - open_a) / open_a if open_a > 0 else 0

                    # 隐含概率
                    inv = 1/oh + 1/od + 1/oa
                    imp_h = (1/oh)/inv
                    imp_d = (1/od)/inv
                    imp_a = (1/oa)/inv
                    cimp_inv = 1/close_h + 1/close_d + 1/close_a
                    cimp_h = (1/close_h)/cimp_inv
                    cimp_d = (1/close_d)/cimp_inv
                    cimp_a = (1/close_a)/cimp_inv

                    outcome = row['actual_result']
                    league_name = LEAGUE_CATALOG.get(row['sport_key'], {}).get('name', row['sport_key'])
                    match_date = (row['commence_time'] or '')[:10]

                    # INSERT OR REPLACE (用 home+away+date 做唯一键)
                    cur.execute(
                        """INSERT OR REPLACE INTO odds_features
                           (source, match_date, league, home_team, away_team,
                            home_score, away_score, outcome,
                            open_h, open_d, open_a, close_h, close_d, close_a,
                            drift_h, drift_d, drift_a,
                            imp_h, imp_d, imp_a, cimp_h, cimp_d, cimp_a, overround)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ('live_sync', match_date, league_name, row['home_team'], row['away_team'],
                         None, None, outcome,
                         open_h, open_d, open_a, close_h, close_d, close_a,
                         drift_h, drift_d, drift_a,
                         imp_h, imp_d, imp_a, cimp_h, cimp_d, cimp_a, inv - 1)
                    )
                    stats["synced"] += 1
                except Exception as e:
                    stats["skipped"] += 1
                    logger.debug(f"同步跳过 (id={row['id']}): {e}")

            conn.commit()
            stats["total_odds_features"] = cur.execute("SELECT COUNT(*) FROM odds_features").fetchone()[0]
            conn.close()
            logger.info(f"odds_features 同步: {stats['synced']} 条, 跳过 {stats['skipped']}, "
                        f"总行数 {stats['total_odds_features']}")
        except Exception as e:
            stats["error"] = str(e)
            logger.error(f"odds_features 同步失败: {e}")
        return stats

    # ════════════════════════════════════════
    # 数据增长统计
    # ════════════════════════════════════════
    def get_growth_stats(self) -> Dict:
        """数据增长统计"""
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            live_count = cur.execute("SELECT COUNT(*) FROM live_odds_raw").fetchone()[0]
            live_with_result = cur.execute(
                "SELECT COUNT(*) FROM live_odds_raw WHERE actual_result IS NOT NULL AND actual_result != ''"
            ).fetchone()[0]
            of_count = cur.execute("SELECT COUNT(*) FROM odds_features").fetchone()[0]
            of_live_sync = cur.execute(
                "SELECT COUNT(*) FROM odds_features WHERE source='live_sync'"
            ).fetchone()[0]
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            today_count = cur.execute(
                "SELECT COUNT(*) FROM live_odds_raw WHERE captured_at LIKE ?", (f"{today}%",)
            ).fetchone()[0]
            conn.close()
            return {
                "live_odds_raw_total": live_count,
                "live_odds_raw_with_result": live_with_result,
                "odds_features_total": of_count,
                "odds_features_live_sync": of_live_sync,
                "today_collected": today_count,
                "active_leagues": len(DailyCollector._active_leagues_cache) if DailyCollector._active_leagues_cache else 0,
                "quota_remaining": self._last_quota_remaining,
            }
        except Exception as e:
            return {"error": str(e)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    dc = DailyCollector()
    print("=" * 60)
    print("  DailyCollector 自测")
    print("=" * 60)

    # 1. 智能拉取 (全量探测)
    print("\n[1] 全量拉取探测活跃联赛...")
    r1 = dc.collect_daily_odds(force_full=True)
    print(f"  采集 {r1['collected']} 场, 活跃 {len(r1['active_leagues'])} 联赛, "
          f"剩余配额 {r1['remaining_quota']}")

    # 2. 赛果回填
    print("\n[2] 赛果回填...")
    r2 = dc.backfill_results()
    print(f"  扫描 {r2['scanned']}, 回填 {r2['backfilled']}, 待手动 {r2['pending']}")

    # 3. odds_features 同步
    print("\n[3] odds_features 同步...")
    r3 = dc.sync_to_odds_features()
    print(f"  同步 {r3['synced']} 条, 总行数 {r3['total_odds_features']}")

    # 4. 增长统计
    print("\n[4] 数据增长统计...")
    r4 = dc.get_growth_stats()
    for k, v in r4.items():
        print(f"  {k}: {v}")
