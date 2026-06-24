"""
哨响AI - 赔率历史采集器
复用 Football-Data.org API 采集赔率快照，构建赔率时间序列
支撑 sigma_trap (异常波动率) 特征计算
"""
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)


class OddsHistoryCollector:
    """赔率历史采集器 — 从现有 Football-Data.org 采集器拉取并追踪赔率变化"""

    def __init__(self, collector=None, db: DatabaseManager = None):
        """
        Args:
            collector: FootballDataCollector 实例 (data_collector.main)
            db: DatabaseManager 实例
        """
        self.collector = collector
        self.db = db or DatabaseManager()

    def fetch_and_store(self, match_id: int) -> int:
        """
        从 Football-Data.org 获取比赛赔率并存入 odds_history
        返回新增记录数
        """
        if not self.collector:
            logger.warning("OddsHistoryCollector: 未注入 FootballDataCollector，跳过")
            return 0

        try:
            odds_data = self.collector.get_odds(match_id)
            if not odds_data:
                return 0

            odds_entry = {
                'home_odds': odds_data.get('home_win') or odds_data.get('homeWin'),
                'draw_odds': odds_data.get('draw') or odds_data.get('draw'),
                'away_odds': odds_data.get('away_win') or odds_data.get('awayWin'),
                'asian_handicap': None,
                'odds_timestamp': datetime.now().isoformat(),
                'provider': 'football-data.org',
            }

            # 只保存有效赔率
            if odds_entry['home_odds']:
                return self.db.save_odds_history(match_id, [odds_entry])
            return 0
        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            logger.error(f"赔率历史采集失败 match_id={match_id}: {e}")
            return 0

    def batch_collect(self, match_ids: List[int], delay: float = 7.0) -> int:
        """批量采集 (避免触发速率限制)"""
        total = 0
        for i, mid in enumerate(match_ids):
            if i > 0:
                time.sleep(delay)
            count = self.fetch_and_store(mid)
            total += count
            if i % 10 == 0:
                logger.info(f"赔率历史进度: {i+1}/{len(match_ids)}, 新增 {total} 条")
        return total

    def build_odds_series(self, match_id: int) -> List[float]:
        """获取赔率时间序列 (用于 calc_odd_volatility)"""
        return self.db.get_odds_series(match_id, 'home')


def seed_odds_history_from_odds(db: DatabaseManager = None) -> int:
    """
    从现有 odds 表快照补偿 odds_history (向后兼容)
    为已有赔率的比赛创建至少一条历史记录
    """
    db = db or DatabaseManager()
    count = 0

    with db.get_connection() as conn:
        # 获取有赔率但无历史记录的比赛
        rows = conn.execute('''
            SELECT DISTINCT o.match_id, o.home_odds, o.draw_odds, o.away_odds,
                   o.asian_handicap, o.odds_timestamp, o.provider
            FROM odds o
            WHERE o.match_id NOT IN (SELECT match_id FROM odds_history)
              AND o.home_odds IS NOT NULL
        ''').fetchall()

        for row in rows:
            conn.execute('''
                INSERT OR IGNORE INTO odds_history
                (match_id, provider, home_odds, draw_odds, away_odds,
                 asian_handicap, odds_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                row['match_id'], row['provider'] or 'football-data.org',
                row['home_odds'], row['draw_odds'], row['away_odds'],
                row['asian_handicap'],
                row['odds_timestamp'] or datetime.now().isoformat(),
            ))
            count += 1

    logger.info(f"赔率历史补偿完成: {count} 条 (从 odds 快照迁移)")
    return count


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    n = seed_odds_history_from_odds()
    print(f"赔率历史种子: {n} 条")
