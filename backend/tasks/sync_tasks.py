"""
后台同步任务 — Celery Periodic Task
替代 flask_bridge.py 中的阻塞式后台线程
"""
import sys
import os
import logging
import requests
import sqlite3
import sqlalchemy
from datetime import datetime, timezone, timedelta
from celery.schedules import crontab

from tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

@celery_app.task(bind=True, name="sync_matches_task")
def sync_matches_task(self):
    """
    同步比赛数据和赔率（Celery定时任务）
    
    执行频率: 每15分钟（通过Celery Beat调度）
    替代原 flask_bridge.py 中的后台线程
    """
    try:
        from config.api_config import API_CONFIG, LEAGUES
        from data_collector.main import FootballDataCollector as FDC
        from database.db_manager import get_db

        db = get_db()
        api_key = API_CONFIG.get('primary', {}).get('api_key', '')
        
        if not api_key:
            logger.warning("[后台同步] 无API Key，跳过")
            return {"status": "skipped", "reason": "no_api_key"}
        
        collector = FDC(api_key)
        
        # 优先级联赛列表
        priority_keys = [
            'premier_league', 'la_liga', 'serie_a', 'bundesliga',
            'ligue_1', 'champions_league', 'brasileirao',
            'eredivisie', 'primeira_liga', 'championship',
            'mls', 'csl', 'world_cup', 'european_championship',
        ]
        
        today = datetime.now(timezone.utc)
        date_from = (today - timedelta(days=3)).strftime('%Y-%m-%d')
        date_to = (today + timedelta(days=6)).strftime('%Y-%m-%d')
        
        updated_count = 0
        errors = []
        
        for league_key in priority_keys:
            league = LEAGUES.get(league_key)
            if not league:
                continue
            
            try:
                matches = collector.get_matches(league['id'], date_from, date_to)
                if matches:
                    with db.get_connection() as conn:
                        for m in matches:
                            if not m:
                                continue
                            m['league_id'] = league['id']
                            m['league_name'] = league.get('name_cn', league.get('name', ''))
                            db.add_match(conn, m)
                            updated_count += 1
                    
                    logger.info(f"[后台同步] {league.get('name', '')}: {len(matches)} 场")
                    
            except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as e:
                error_msg = f"{league_key}: API请求失败 - {str(e)}"
                errors.append(error_msg)
                logger.warning(f"[后台同步] {error_msg}")
            except (KeyError, ValueError) as e:
                error_msg = f"{league_key}: 数据格式错误 - {str(e)}"
                errors.append(error_msg)
                logger.warning(f"[后台同步] {error_msg}")
            except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
                error_msg = f"{league_key}: {str(e)}"
                errors.append(error_msg)
                logger.warning(f"[后台同步] {error_msg}")
        
        # 自动结算
        resolved = 0
        try:
            resolved = db.resolve_bet_results()
            if resolved > 0:
                logger.info(f"[后台同步] 自动结算 {resolved} 条记录")
        except (sqlalchemy.exc.SQLAlchemyError, sqlite3.Error) as e:
            logger.warning(f"[后台同步] 结算数据库错误: {e}")
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
            logger.warning(f"[后台同步] 结算异常: {e}")
        
        return {
            "status": "success",
            "updated": updated_count,
            "resolved": resolved,
            "errors": errors,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as e:
        logger.error(f"[后台同步] API请求失败: {e}", exc_info=True)
        self.retry(exc=e, countdown=60, max_retries=3)
    except (sqlalchemy.exc.SQLAlchemyError, sqlite3.Error) as e:
        logger.error(f"[后台同步] 数据库错误: {e}", exc_info=True)
        self.retry(exc=e, countdown=60, max_retries=3)
    except (ValueError, KeyError) as e:
        logger.error(f"[后台同步] 参数或数据格式错误: {e}", exc_info=True)
        return {"status": "error", "message": f"数据格式错误: {str(e)}"}
    except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
        logger.error(f"[后台同步] 任务执行失败: {e}", exc_info=True)
        self.retry(exc=e, countdown=60, max_retries=3)

@celery_app.task(bind=True, name="fetch_initial_data_task")
def fetch_initial_data_task(self, date_from=None, date_to=None):
    """
    初始数据拉取任务（一次性或手动触发）
    
    Args:
        date_from: 开始日期 (YYYY-MM-DD)
        date_to: 结束日期 (YYYY-MM-DD)
    """
    try:
        from config.api_config import API_CONFIG, LEAGUES
        from data_collector.main import FootballDataCollector as FDC
        from database.db_manager import get_db

        db = get_db()
        api_key = API_CONFIG.get('primary', {}).get('api_key', '')
        
        if not api_key:
            return {"status": "error", "message": "未配置API Key"}
        
        collector = FDC(api_key)
        
        if not date_from:
            today = datetime.now(timezone.utc)
            date_from = today.strftime('%Y-%m-%d')
            date_to = (today + timedelta(days=7)).strftime('%Y-%m-%d')
        
        fetched = 0
        for lg_code, lg_info in LEAGUES.items():
            lid = lg_info.get('id')
            if not lid:
                continue
            
            try:
                matches = collector.get_matches(lid, date_from, date_to)
                if matches:
                    for m in matches:
                        m['league_id'] = lid
                        m['league_name'] = lg_info.get('name_cn', lg_info.get('name', ''))
                        try:
                            db.add_match(m)
                        except (sqlalchemy.exc.SQLAlchemyError, sqlite3.Error) as e:
                            logger.debug(f"添加比赛失败（数据库错误）: {e}")
                        except (KeyError, ValueError) as e:
                            logger.debug(f"添加比赛失败（数据格式错误）: {e}")
                        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
                            logger.debug(f"添加比赛失败: {e}")
                    fetched += len(matches)
                    logger.info(f"[初始拉取] {lg_info.get('name', '')}: {len(matches)} 场")
            except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as e:
                logger.warning(f"[初始拉取] {lg_info.get('name', '')}: API请求失败 - {e}")
            except (KeyError, ValueError) as e:
                logger.warning(f"[初始拉取] {lg_info.get('name', '')}: 数据格式错误 - {e}")
            except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
                logger.warning(f"[初始拉取] {lg_info.get('name', '')}: {e}")
        
        return {
            "status": "success",
            "fetched": fetched,
            "date_from": date_from,
            "date_to": date_to,
        }
        
    except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as e:
        logger.error(f"[初始拉取] API请求失败: {e}", exc_info=True)
        return {"status": "error", "message": f"API请求失败: {str(e)}"}
    except (ValueError, KeyError) as e:
        logger.error(f"[初始拉取] 参数或数据格式错误: {e}", exc_info=True)
        return {"status": "error", "message": f"数据格式错误: {str(e)}"}
    except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
        logger.error(f"[初始拉取] 失败: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}

# ── Celery Beat 定时任务配置 ──────────────────────────────
celery_app.conf.beat_schedule = {
    # 每15分钟同步一次比赛数据
    'sync-matches-every-15min': {
        'task': 'tasks.sync_tasks.sync_matches_task',
        'schedule': crontab(minute='*/15'),  # 每15分钟
    },
}
