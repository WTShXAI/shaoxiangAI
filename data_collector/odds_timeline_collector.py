"""
哨响AI - 赔率时序采集器 v1.0
=====================================
定时采集实时赔率并构建时序数据，供 OTSM 实时信号推断使用。

架构:
  odds 表 (最新赔率) → 轮询检测变化 → odds_timeline 表 (时序快照)

工作模式:
  1.Daemon 模式: 每隔 N 分钟轮询 odds 表，检测变化并写入 timeline
  2.Backfill 模式: 从 football-data.org 历史数据回填 timeline
  3.API 模式: 调用 The Odds API 获取实时赔率并写入 timeline

使用:
  python odds_timeline_collector.py --daemon --interval 30   # 每30分钟轮询
  python odds_timeline_collector.py --backfill                  # 回填历史数据
  python odds_timeline_collector.py --collect-now              # 立即采集一次
"""
import os
import sys
import time
import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('TimelineCollector')

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "football_data.db")
THE_ODDS_API_KEY = os.environ.get("THE_ODDS_API_KEY", "")

class OddsTimelineCollector:
    """
    赔率时序采集器
    
    核心逻辑:
    1. 从 odds 表读取最新赔率
    2. 与 odds_timeline 中该比赛的最后一条快照比较
    3. 如果赔率变化超过阈值 (默认 0.01)，写入新快照
    4. 如果 odds 表有新比赛，初始化第一条快照
    """
    
    def __init__(self, db_path: str = DB_PATH, change_threshold: float = 0.01):
        self.db_path = db_path
        self.change_threshold = change_threshold
        self.stats = {
            "snapshots_added": 0,
            "matches_tracked": 0,
            "changes_detected": 0,
            "no_change": 0,
        }
    
    def ensure_table(self):
        """确保 odds_timeline 表存在"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS odds_timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_id INTEGER NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    home_odds REAL,
                    draw_odds REAL,
                    away_odds REAL,
                    bookmaker TEXT DEFAULT 'merged',
                    source TEXT DEFAULT 'odds_table_poll',
                    prev_home_odds REAL,
                    prev_draw_odds REAL,
                    prev_away_odds REAL,
                    change_magnitude REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(match_id, snapshot_time, bookmaker)
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_timeline_match 
                ON odds_timeline(match_id, snapshot_time)
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_timeline_time 
                ON odds_timeline(snapshot_time)
            ''')
        logger.info("odds_timeline 表已就绪")
    
    def get_pending_matches(self, hours_ahead: int = 48) -> List[Dict]:
        """
        获取待跟踪的比赛 (未来 N 小时内开赛且已有赔率)
        
        Returns:
            [{match_id, home_team_name, away_team_name, match_date, 
              home_odds, draw_odds, away_odds, odds_updated_at}, ...]
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT m.match_id, m.home_team_name, m.away_team_name, 
                       m.match_date, m.match_time,
                       o.home_odds, o.draw_odds, o.away_odds,
                       o.updated_at as odds_updated_at
                FROM matches m
                JOIN odds o ON m.match_id = o.match_id
                WHERE m.home_score IS NULL
                  AND m.match_date >= date('now')
                  AND m.match_date <= date('now', ?)
                ORDER BY m.match_date, m.match_time
            ''', (f"+{hours_ahead//24} days",)).fetchall()
            
            return [dict(r) for r in rows]
    
    def get_last_snapshot(self, match_id: int) -> Optional[Dict]:
        """获取该比赛的最后一条时序快照"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute('''
                SELECT * FROM odds_timeline
                WHERE match_id = ?
                ORDER BY snapshot_time DESC
                LIMIT 1
            ''', (match_id,)).fetchone()
            return dict(row) if row else None
    
    def detect_change(self, old: Dict, new: Dict) -> Tuple[bool, float]:
        """
        检测赔率是否发生显著变化
        
        Returns:
            (has_changed, change_magnitude)
        """
        if not old:
            return True, 1.0  # 第一条快照
        
        old_odds = (old.get("home_odds") or 0, 
                    old.get("draw_odds") or 0, 
                    old.get("away_odds") or 0)
        new_odds = (new.get("home_odds") or 0,
                    new.get("draw_odds") or 0,
                    new.get("away_odds") or 0)
        
        # 计算最大相对变化
        max_change = 0.0
        for o, n in zip(old_odds, new_odds):
            if o > 0:
                rel_change = abs(n - o) / o
                max_change = max(max_change, rel_change)
        
        return max_change >= self.change_threshold, max_change
    
    def collect_snapshot(self, match: Dict, snapshot_time: str = None) -> bool:
        """
        为单场比赛采集一次时序快照
        
        Args:
            match: 比赛字典 (from get_pending_matches)
            snapshot_time: 快照时间，默认当前时间
            
        Returns:
            是否成功写入新快照
        """
        snapshot_time = snapshot_time or datetime.now(timezone.utc).isoformat()
        match_id = match["match_id"]
        
        # 获取最后快照
        last = self.get_last_snapshot(match_id)
        
        new_odds = {
            "home_odds": match.get("home_odds"),
            "draw_odds": match.get("draw_odds"),
            "away_odds": match.get("away_odds"),
        }
        
        # 检测变化
        has_change, change_mag = self.detect_change(last, new_odds)
        
        if not has_change:
            self.stats["no_change"] += 1
            return False
        
        # 写入快照
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR IGNORE INTO odds_timeline
                    (match_id, snapshot_time, home_odds, draw_odds, away_odds,
                     bookmaker, source, 
                     prev_home_odds, prev_draw_odds, prev_away_odds,
                     change_magnitude, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                match_id,
                snapshot_time,
                new_odds["home_odds"],
                new_odds["draw_odds"],
                new_odds["away_odds"],
                "merged",
                "odds_table_poll",
                last.get("home_odds") if last else None,
                last.get("draw_odds") if last else None,
                last.get("away_odds") if last else None,
                change_mag,
                snapshot_time,
            ))
        
        self.stats["snapshots_added"] += 1
        self.stats["changes_detected"] += 1
        return True
    
    def collect_all(self, hours_ahead: int = 48) -> Dict:
        """
        采集所有待跟踪比赛的时序快照
        
        Returns:
            统计信息字典
        """
        self.ensure_table()
        snapshot_time = datetime.now(timezone.utc).isoformat()
        
        matches = self.get_pending_matches(hours_ahead)
        logger.info(f"待跟踪比赛: {len(matches)} 场")
        
        self.stats = {
            "snapshots_added": 0,
            "matches_tracked": len(matches),
            "changes_detected": 0,
            "no_change": 0,
            "errors": 0,
        }
        
        for match in matches:
            try:
                self.collect_snapshot(match, snapshot_time)
            except (Exception) as e:
                logger.debug(f"快照采集失败 match_id={match['match_id']}: {e}")
                self.stats["errors"] += 1
        
        logger.info(f"采集完成: 新增 {self.stats['snapshots_added']} 条快照, "
                    f"变化 {self.stats['changes_detected']}, "
                    f"无变化 {self.stats['no_change']}")
        return self.stats
    
    def run_daemon(self, interval_minutes: int = 30, max_iterations: int = None):
        """
        以 Daemon 模式运行，定时采集
        
        Args:
            interval_minutes: 采集间隔（分钟）
            max_iterations: 最大迭代次数（None=无限）
        """
        self.ensure_table()
        logger.info(f"Daemon 启动: 间隔 {interval_minutes} 分钟")
        
        iteration = 0
        while True:
            iteration += 1
            logger.info(f"\n{'='*60}")
            logger.info(f"迭代 #{iteration} @ {datetime.now(timezone.utc).strftime('%H:%M:%S')}")
            
            try:
                stats = self.collect_all()
                logger.info(f"统计: {json.dumps(stats, ensure_ascii=False)}")
            except (Exception, json.JSONDecodeError) as e:
                logger.error(f"采集异常: {e}")
            
            if max_iterations and iteration >= max_iterations:
                logger.info(f"达到最大迭代次数 {max_iterations}，退出")
                break
            
            logger.info(f"等待 {interval_minutes} 分钟...")
            time.sleep(interval_minutes * 60)
    
    def get_timeline(self, match_id: int) -> List[Dict]:
        """
        获取指定比赛的赔率时序数据
        
        Returns:
            [{snapshot_time, home_odds, draw_odds, away_odds, change_magnitude}, ...]
            按时间正序排列
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT snapshot_time, home_odds, draw_odds, away_odds,
                       prev_home_odds, prev_draw_odds, prev_away_odds,
                       change_magnitude, source
                FROM odds_timeline
                WHERE match_id = ?
                ORDER BY snapshot_time ASC
            ''', (match_id,)).fetchall()
            
            return [dict(r) for r in rows]
    
    def get_coverage_stats(self) -> Dict:
        """获取时序数据覆盖统计"""
        with sqlite3.connect(self.db_path) as conn:
            # 总比赛数
            total = conn.execute('''
                SELECT COUNT(*) as c FROM matches WHERE home_score IS NULL
            ''').fetchone()[0]
            
            # 有 timeline 的比赛数
            with_tl = conn.execute('''
                SELECT COUNT(DISTINCT match_id) as c FROM odds_timeline
            ''').fetchone()[0]
            
            # 总快照数
            total_snapshots = conn.execute('''
                SELECT COUNT(*) as c FROM odds_timeline
            ''').fetchone()[0]
            
            # 平均快照数/比赛
            avg_snapshots = total_snapshots / with_tl if with_tl else 0
            
            return {
                "upcoming_matches": total,
                "matches_with_timeline": with_tl,
                "coverage_pct": round(with_tl / total * 100, 1) if total else 0,
                "total_snapshots": total_snapshots,
                "avg_snapshots_per_match": round(avg_snapshots, 1),
            }

# ─── CLI ───────────────────────────────────────────────────────────

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="哨响AI - 赔率时序采集器 v1.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python odds_timeline_collector.py --collect-now          # 立即采集一次
  python odds_timeline_collector.py --daemon --interval 30 # 每30分钟采集
  python odds_timeline_collector.py --stats               # 查看覆盖统计
  python odds_timeline_collector.py --show-timeline 12345 # 查看比赛12345的时序
        """
    )
    parser.add_argument("--collect-now", action="store_true", 
                        help="立即采集一次时序快照")
    parser.add_argument("--daemon", action="store_true",
                        help="Daemon 模式: 定时采集")
    parser.add_argument("--interval", type=int, default=30,
                        help="采集间隔 (分钟, 默认30)")
    parser.add_argument("--hours-ahead", type=int, default=48,
                        help="跟踪未来 N 小时的比赛 (默认48)")
    parser.add_argument("--max-iter", type=int, default=None,
                        help="Daemon 最大迭代次数 (默认无限)")
    parser.add_argument("--stats", action="store_true",
                        help="显示时序数据覆盖统计")
    parser.add_argument("--show-timeline", type=int, metavar="MATCH_ID",
                        help="显示指定比赛的时序数据")
    parser.add_argument("--backfill", action="store_true",
                        help="从 odds 表回填历史快照 (每条比赛1条)")
    
    args = parser.parse_args()
    
    collector = OddsTimelineCollector()
    
    if args.stats:
        stats = collector.get_coverage_stats()
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return
    
    if args.show_timeline:
        timeline = collector.get_timeline(args.show_timeline)
        print(f"比赛 {args.show_timeline} 时序数据: {len(timeline)} 条")
        for snap in timeline[:10]:
            print(f"  {snap['snapshot_time'][:16]}: "
                  f"H={snap['home_odds']:.2f} D={snap['draw_odds']:.2f} "
                  f"A={snap['away_odds']:.2f} (Δ={snap['change_magnitude']:.4f})")
        return
    
    if args.backfill:
        # 从 odds 表回填: 每条有赔率的比赛创建1条 timeline 记录
        collector.ensure_table()
        with sqlite3.connect(collector.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''
                SELECT m.match_id, m.home_team_name, m.away_team_name,
                       o.home_odds, o.draw_odds, o.away_odds,
                       o.updated_at
                FROM matches m
                JOIN odds o ON m.match_id = o.match_id
                WHERE m.home_score IS NULL
                  AND m.match_id NOT IN (SELECT DISTINCT match_id FROM odds_timeline)
            ''').fetchall()
            
            count = 0
            for row in rows:
                try:
                    conn.execute('''
                        INSERT OR IGNORE INTO odds_timeline
                            (match_id, snapshot_time, home_odds, draw_odds, away_odds,
                             bookmaker, source, change_magnitude, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        row["match_id"],
                        row["updated_at"] or datetime.now(timezone.utc).isoformat(),
                        row["home_odds"],
                        row["draw_odds"],
                        row["away_odds"],
                        "merged",
                        "backfill_from_odds",
                        1.0,  # 初始快照
                        datetime.now(timezone.utc).isoformat(),
                    ))
                    count += 1
                except (Exception, KeyError, IndexError) as e:
                    logger.debug(f"回填失败: {e}")
            
            conn.commit()
            print(f"回填完成: {count} 条初始快照")
        return
    
    if args.collect_now:
        stats = collector.collect_all(args.hours_ahead)
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return
    
    if args.daemon:
        collector.run_daemon(args.interval, args.max_iter)
        return
    
    parser.print_help()

if __name__ == "__main__":
    main()
