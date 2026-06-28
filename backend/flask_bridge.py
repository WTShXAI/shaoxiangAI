"""
Flask → FastAPI 桥接层
======================
将 api/prediction_service.py 的 Flask 应用挂载到 FastAPI 下，
提取 Flask 启动逻辑到 FastAPI lifespan，实现双后端统一入口。

架构：
  FastAPI (port 8000)
  ├── /              → FastAPI root (服务信息)
  ├── /api/v1/*      → FastAPI 原生路由 (预测/模型/训练/监控/认证)
  ├── /metrics       → Prometheus 指标
  └── 其他 /api/*    → WSGI 回退 → Flask prediction_service

⚡ P1优化: 移除阻塞式后台线程，改用Celery定时任务
  - 原 _background_sync_worker() 已迁移至 tasks/sync_tasks.py
  - 使用 Celery Beat 每15分钟自动执行同步
  - 不再阻塞FastAPI事件循环
"""

import sys
import os
import time
import logging
import sqlite3
import sqlalchemy
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)

# ── Flask app 引用（延迟加载，避免循环导入）──
_flask_app = None
_startup_done = False

def get_flask_app():
    """
    获取 Flask 应用实例（单例，延迟导入）。
    使用文件级导入避免 backend/api/ 与 footballAI/api/ 包名冲突。
    """
    global _flask_app
    if _flask_app is None:
        logger.info("🔌 加载 Flask prediction_service...")
        # 文件级导入，绕过包名冲突（原 api/prediction_service 已归档至 archive/）
        import importlib.util
        ps_path = os.path.join(PROJECT_ROOT, 'archive', 'prediction_service_flask_legacy.py')
        if not os.path.exists(ps_path):
            logger.warning(f"[跳过] 旧版 Flask 服务已归档移除: {ps_path}")
            return None
        spec = importlib.util.spec_from_file_location(
            'prediction_service_legacy', ps_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _flask_app = mod.app
        api_count = sum(1 for r in _flask_app.url_map.iter_rules()
                        if r.rule.startswith('/api/'))
        logger.info(f"✅ Flask WSGI 已就绪（{api_count} API 路由）")
    return _flask_app

def run_flask_startup():
    """
    执行 Flask 启动逻辑（数据初始化、完整性检查）。
    
    ⚡ P1优化: 移除后台线程，数据同步改为Celery定时任务
    - 后台同步已迁移至 tasks/sync_tasks.py::sync_matches_task
    - 通过 Celery Beat 每15分钟自动执行
    - 如需立即同步，可手动触发: celery call tasks.sync_tasks.sync_matches_task
    """
    global _startup_done
    if _startup_done:
        logger.debug("Flask startup 已执行，跳过")
        return
    _startup_done = True

    from database.db_manager import get_db
    from config.api_config import API_CONFIG  # noqa: F401

    db = get_db()
    logger.info("🔍 数据完整性检查...")

    # 数据纯净性检查
    try:
        from database.data_integrity import startup_integrity_check
        startup_integrity_check()
    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.warning(f"完整性检查跳过: {e}")

    # 硬件信息
    try:
        from config.hardware_config import get_hardware_config
        hw = get_hardware_config()
        hw.log_summary()
    except (OSError, ValueError, KeyError) as e:
        logger.debug(f"操作失败: {e}")

    # 检查是否需要拉取数据
    stats = db.get_stats()
    need_fetch = stats.get('total_matches', 0) == 0

    if not need_fetch:
        try:
            with db.get_connection() as conn:
                scheduled_count = conn.execute(
                    "SELECT COUNT(*) FROM matches WHERE status='scheduled' "
                    "AND match_date >= date('now')"
                ).fetchone()[0]
            if scheduled_count == 0:
                logger.info("数据库有历史数据但无即将开始的比赛，补充拉取未来赛程...")
                need_fetch = True
        except (OSError, ValueError, KeyError) as e:
            logger.debug(f"操作失败: {e}")

    if need_fetch:
        logger.info("🚀 触发初始数据拉取任务（异步）")
        try:
            # ⚡ P1优化: 使用Celery任务替代同步拉取
            from tasks.sync_tasks import fetch_initial_data_task
            fetch_initial_data_task.delay()
            logger.info("✅ 初始数据拉取任务已提交至Celery")
        except (ValueError, KeyError, FileNotFoundError) as e:
            logger.warning(f"⚠️ 提交Celery任务失败，改用同步方式: {e}")
            # Fallback: 同步拉取（保持兼容性）
            _sync_fetch_data(db, API_CONFIG)
    else:
        logger.info("✅ 数据库已有足够数据，跳过初始拉取")
    
    # ⚡ P1优化: 提示用户Celery Beat已接管后台同步
    logger.info("🔄 后台同步已迁移至Celery定时任务（每15分钟执行）")
    logger.info("   启动Celery Worker: celery -A tasks.celery_app worker -l info")
    logger.info("   启动Celery Beat: celery -A tasks.celery_app beat -l info")

def _sync_fetch_data(db, API_CONFIG):
    """同步拉取数据（Fallback方法，仅在Celery不可用时使用）"""
    try:
        from config.api_config import LEAGUES
        from data_collector.main import FootballDataCollector as FDC

        api_key = API_CONFIG.get('primary', {}).get('api_key', '')
        if not api_key:
            logger.warning("⚠️ 未配置 API Key，无法获取数据（API功能受限）")
            return

        logger.info(f"同步拉取数据（Fallback）...")
        collector = FDC(api_key=api_key)
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
                        except (OSError, ValueError, KeyError) as e:
                            logger.debug(f"操作失败: {e}")
                    fetched += len(matches)
                    logger.info(f"  ✓ {lg_info.get('name', '')}: {len(matches)} 场")
            except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
                logger.warning(f"  ✗ {lg_info.get('name', '')}: {e}")
            time.sleep(1)  # 保留短暂延迟避免API限流
        
        if fetched > 0:
            logger.info(f"✅ 已获取 {fetched} 场真实比赛数据")
        else:
            logger.warning("⚠️ API 未返回数据")
    except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
        logger.warning(f"⚠️ 数据获取失败: {e}")
