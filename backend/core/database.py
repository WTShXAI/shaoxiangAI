"""
数据库连接管理 — SQLAlchemy 2.0 异步引擎
"""
import sys
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from core.config import settings

# 确保项目根目录可导入现有模块
if settings.PROJECT_ROOT not in sys.path:
    sys.path.insert(0, settings.PROJECT_ROOT)

# ── SQLAlchemy 引擎 ────────────────────────
# SQLite 需要 check_same_thread=False 用于多线程
_connect_args = {}
if settings.DATABASE_URL.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    echo=settings.DEBUG,
    pool_pre_ping=True,
)

# ── SQLite WAL 模式 ───────────────────────
# 开启 WAL (Write-Ahead Logging) 提高并发读写性能
with engine.connect() as conn:
    conn.execute(text("PRAGMA journal_mode=WAL"))
    conn.commit()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── FastAPI 依赖 ──────────────────────────
def get_db():
    """FastAPI 数据库会话依赖注入"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_path() -> str:
    """获取完整数据库路径（供现有模块使用）"""
    db_path = settings.DB_PATH
    if not os.path.isabs(db_path):
        db_path = os.path.join(settings.PROJECT_ROOT, db_path)
    return db_path
