"""
集中式路径解析 — 所有相对路径从此模块解析，确保从项目根运行。
用法:
    from utils.paths import get_db_path, get_model_dir, PROJECT_ROOT
"""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FILENAME = "football_data.db"
MODEL_DIR = os.path.join(PROJECT_ROOT, "saved_models")
DEFAULT_DB = os.path.join(PROJECT_ROOT, "data", DB_FILENAME)


def get_db_path(db_path: str = None) -> str:
    """解析数据库路径。相对路径自动从 PROJECT_ROOT 解析。"""
    if db_path is None:
        return DEFAULT_DB
    if os.path.isabs(db_path):
        return db_path
    return os.path.join(PROJECT_ROOT, db_path)


def get_model_dir() -> str:
    """模型目录绝对路径。"""
    return MODEL_DIR


def get_config_path() -> str:
    """config.yaml 绝对路径。"""
    return os.path.join(PROJECT_ROOT, "config.yaml")
