"""
哨响AI - FastAPI 后端配置中心
所有配置外部化，支持环境变量覆盖
"""
import os
import logging
import secrets
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_DEFAULT_DB_FILE = os.path.join(_PROJECT_ROOT, "data", "football_data.db")
_DEFAULT_DATABASE_URL = "sqlite:///" + _DEFAULT_DB_FILE.replace("\\", "/")

_DEFAULT_CORS_ORIGINS = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:8000"
)


def _parse_cors_origins(raw: str) -> List[str]:
    """从逗号分隔字符串解析 CORS 来源列表。"""
    if not raw or not raw.strip():
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


class Settings(BaseSettings):
    # ── 服务 ──────────────────────────────────
    APP_NAME: str = "哨响AI - Football Prediction API"
    APP_VERSION: str = "4.1.0"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # ── 网络 ──────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    CORS_ORIGINS: List[str] = []

    # ── 数据库（绝对路径，相对 footballAI/ 根目录，与 Flask db_manager 一致）──
    DATABASE_URL: str = _DEFAULT_DATABASE_URL
    DB_PATH: str = os.path.join("data", "football_data.db")

    # ── Redis / Celery ────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # ── MLflow ────────────────────────────────
    MLFLOW_TRACKING_URI: str = "http://localhost:5001"
    MLFLOW_EXPERIMENT_NAME: str = "football_prediction"

    # ── 模型 ──────────────────────────────────
    MODEL_DIR: str = "saved_models"
    DEFAULT_MODEL_NAME: str = "football_v4.1_production.joblib"  # 修复NEW-5: v3.2→v4.1
    MODEL_REGISTRY_PATH: str = "saved_models/model_registry.json"

    # ── 安全 ──────────────────────────────────
    SECRET_KEY: str = ""  # 必须通过 .env 或环境变量设置
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    API_RATE_LIMIT: str = "100/minute"

    # ── 数据采集 ──────────────────────────────
    FOOTBALL_DATA_API_KEY: Optional[str] = None
    THE_ODDS_API_KEY: Optional[str] = None
    RAPIDAPI_KEY: Optional[str] = None

    # ── 硬件 ──────────────────────────────────
    GPU_MODE: str = "auto"

    # GPU训练总开关 (True=cuda, False=cpu)
    TRAIN_USE_GPU: bool = True
    # 显卡编号（单卡=0，多卡=[0,1]）
    GPU_DEVICE_IDS: str = "0"
    # 显存占用比例，防止OOM（0.5~0.7）
    GPU_MEM_LIMIT_RATIO: float = 0.7
    # 自动降级：GPU报错自动切CPU
    GPU_AUTO_FALLBACK: bool = True

    # 混合精度训练（加速+省显存）
    CUDA_MIXED_PRECISION: bool = True
    # 数据加载线程数 (GPU:4~8, CPU:2)
    DATALOADER_NUM_WORKERS: int = 4
    # 批量尺寸 (GPU:256, CPU:32)
    TRAIN_BATCH_SIZE: int = 256

    # 树模型GPU (XGB CUDA + LGB OpenCL)
    META_MODEL_GPU_ACCEL: bool = True
    # 交叉验证GPU并行数
    CV_GPU_WORKERS: int = 2

    CPU_N_JOBS: int = 0

    # ── 监控 ──────────────────────────────────
    PROMETHEUS_PORT: int = 9090
    LOG_LEVEL: str = "INFO"

    # ── 项目路径 ──────────────────────────────
    PROJECT_ROOT: str = _PROJECT_ROOT

    # ── 共享 .env 变量（Flask legacy 兼容）───
    API_PORT: Optional[int] = None
    LEGACY_FLASK_ENABLED: Optional[bool] = None
    MLFLOW_PORT: Optional[int] = None
    GRAFANA_PORT: Optional[int] = None
    GRAFANA_USER: Optional[str] = None
    GRAFANA_PASSWORD: Optional[str] = None
    FLASK_PORT: Optional[int] = None
    FLASK_HOST: Optional[str] = None
    FLASK_DEBUG: Optional[str] = None
    FLASK_SECRET_KEY: Optional[str] = None
    API_AUTH_TOKEN: Optional[str] = None
    REDIS_MAX_MEMORY: Optional[str] = None
    API_MEMORY_LIMIT: Optional[str] = None
    GPU_MEMORY_FRACTION: Optional[str] = None
    COLLECTOR_INTERVAL: Optional[str] = None

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        """支持从环境变量 CORS_ORIGINS 逗号分隔字符串或 list 解析。"""
        if v is None:
            return []
        if isinstance(v, str):
            return _parse_cors_origins(v)
        if isinstance(v, list):
            return v
        return []

    def model_post_init(self, _context):
        """配置加载后的验证与回退逻辑。"""
        # ── CORS 验证 ──
        if not self.CORS_ORIGINS:
            if self.DEBUG:
                self.CORS_ORIGINS = _parse_cors_origins(_DEFAULT_CORS_ORIGINS)
                logger.warning(
                    "DEBUG 模式：CORS_ORIGINS 未设置，使用默认开发值 %s。"
                    "生产环境必须设置 CORS_ORIGINS 环境变量。",
                    self.CORS_ORIGINS,
                )
            else:
                self.CORS_ORIGINS = [
                    "http://localhost:3000",
                    "http://localhost:8000",
                ]
                logger.warning(
                    "CORS_ORIGINS 未设置，回退为仅允许 localhost。"
                    "请设置 CORS_ORIGINS 环境变量。"
                )

        # ── SECRET_KEY 验证（临时禁用，方便开发）──
        # 开发模式下自动生成强密钥
        if self.DEBUG:
            if len(self.SECRET_KEY) < 32:
                self.SECRET_KEY = secrets.token_hex(32)
                logger.warning("DEBUG 模式：自动生成临时 SECRET_KEY")
        # 生产环境检查
        elif len(self.SECRET_KEY) < 32:
            raise ValueError(
                f"SECRET_KEY 长度不足: {len(self.SECRET_KEY)}，至少需要 32 字符。"
            )


settings = Settings()
