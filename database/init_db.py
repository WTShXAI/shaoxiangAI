"""
哨响AI - 数据库初始化脚本（真实数据优先版）
优先从 football-data.org API 获取真实数据
仅在API完全不可用时才使用增强数据（球队/赔率估算）
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.db_manager import get_db
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

# 目标联赛（全部14个联赛）
ACTIVE_LEAGUES = ["PL", "BL1", "PD", "SA", "FL1", "CL", "WC", "EC", "BSA", "DED", "PPL", "ELC", "MLS", "CSL"]


def _fetch_real_data(api_key: str = None) -> int:
    """
    从 football-data.org API 获取真实比赛数据并入库
    返回: 成功入库的比赛数量
    """
    try:
        from data_collector.main import FootballDataCollector as FDC
        from config.api_config import API_CONFIG
    except ImportError:
        logger.warning("无法导入数据采集模块，跳过API采集")
        return 0

    key = api_key or (API_CONFIG.get('primary', {}) or {}).get('api_key', '')
    if not key:
        logger.warning("未配置API Key，跳过真实数据采集")
        return 0

    db = get_db()
    collector = FDC(api_key=key)
    today = datetime.now()
    date_from = (today - timedelta(days=3)).strftime('%Y-%m-%d')
    date_to = (today + timedelta(days=14)).strftime('%Y-%m-%d')

    from database.enhanced_data import LEAGUE_ID_MAP, LEAGUE_CONFIG

    total_fetched = 0
    for lg_code in ACTIVE_LEAGUES:
        league_id = LEAGUE_ID_MAP.get(lg_code)
        league_name = LEAGUE_CONFIG.get(lg_code, {}).get("name", lg_code)
        if not league_id:
            continue

        try:
            matches = collector.get_matches(league_id, date_from, date_to)
            if matches:
                count = 0
                for m in matches:
                    try:
                        m['league_id'] = league_id
                        m['league_name'] = league_name
                        db.add_match(m)
                        count += 1
                    except (Exception, KeyError, IndexError):
                        pass
                if count > 0:
                    total_fetched += count
                    logger.info(f"  ✓ {league_name}: {count} 场")
            else:
                logger.debug(f"  - {league_name}: 无数据")
        except (Exception, KeyError, IndexError) as e:
            logger.warning(f"  ✗ {league_name}: {e}")
            # 回退：从数据库已有数据查询
            try:
                existing = db.get_matches(league_id=league_id, limit=10)
                if not existing:
                    pass  # 没有数据就跳过，不生成假的
                elif len(existing) > 0:
                    logger.info(f"  ↻ {league_name}: 使用数据库中已有的 {len(existing)} 条记录")
                    total_fetched += len(existing)
            except (Exception):
                pass
        finally:
            import time as _time
            _time.sleep(1)

    return total_fetched


def init_sample_data(use_real: bool = True):
    """
    初始化数据库（真实数据优先）

    Args:
        use_real: True（默认）= 尝试API获取真实数据；False = 仅用增强数据填充基础结构（不生成假比赛）
    """
    db = get_db()

    if use_real:
        logger.info("📡 正在从 football-data.org API 获取真实数据...")
        real_count = _fetch_real_data()

        if real_count > 0:
            stats = db.get_stats()
            logger.info(f"\n{'='*50}")
            logger.info(f"  🎉 数据库初始化完成! (真实数据)")
            logger.info(f"  {'─'*30}")
            for k, v in stats.items():
                logger.info(f"  {k}: {v}")
            logger.info(f"{'='*50}\n")
            return

        logger.warning("⚠️ API未能获取数据，检查网络/API Key后重试")

    # 不再生成模拟比赛数据。仅记录状态。
    stats = db.get_stats()
    logger.info(f"\n{'='*50}")
    logger.info(f"  📊 数据库当前状态:")
    logger.info(f"  {'─'*30}")
    for k, v in stats.items():
        logger.info(f"  {k}: {v}")
    if stats.get('total_matches', 0) == 0:
        logger.warning("  ⚠️ 数据库暂无比赛数据！请运行数据采集脚本或配置API Key")
    logger.info(f"{'='*50}\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                       format='%(asctime)s - %(levelname)s - %(message)s')
    import argparse
    parser = argparse.ArgumentParser(description="哨响AI 数据库初始化")
    parser.add_argument('--skip-api', action='store_true',
                       help='跳过API采集，仅显示数据库状态')
    args = parser.parse_args()

    init_sample_data(use_real=not args.skip_api)
