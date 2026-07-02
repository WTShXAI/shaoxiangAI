"""
哨响AI - FastAPI 后端配置中心
==============================
所有配置通过 Pydantic BaseSettings 管理，支持 .env 环境变量覆盖。
"""
import os
import secrets
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_DEFAULT_DB_FILE = os.path.join(_PROJECT_ROOT, "data", "football_data.db")
_DEFAULT_DATABASE_URL = "sqlite:///" + _DEFAULT_DB_FILE.replace("\\", "/")


def _parse_cors_origins(raw: str) -> List[str]:
    if not raw or not raw.strip():
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


class Settings(BaseSettings):
    """哨响AI 统一配置"""

    # ── 服务 ──
    APP_NAME: str = "哨响AI - Football Prediction API"
    APP_VERSION: str = "6.0.0"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # ── 网络 ──
    HOST: str = "0.0.0.0"
    PORT: int = 9000
    CORS_ORIGINS: List[str] = []

    # ── 数据库 ──
    DATABASE_URL: str = _DEFAULT_DATABASE_URL
    DB_PATH: str = "data/football_data.db"

    # ── Redis / Celery ──
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # ── MLflow ──
    MLFLOW_TRACKING_URI: str = "http://localhost:5001"
    MLFLOW_EXPERIMENT_NAME: str = "football_prediction"

    # ── 模型路径 ──
    MODEL_DIR: str = "saved_models"
    V41_MODEL: str = "saved_models/football_v4.1_production.joblib"
    V32_MODEL: str = "saved_models/football_balanced_production.joblib"
    DRAW_EXPERT: str = "saved_models/draw_expert_v1.joblib"
    NN_MODEL: str = "saved_models/football_nn_20260616_125617.pth"
    SP_DB_PATH: str = "D:/AI/SP/data/sp_data.db"

    # ── 安全 ──
    SECRET_KEY: str = ""
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    API_RATE_LIMIT: str = "100/minute"
    API_AUTH_TOKEN: Optional[str] = None

    # ── 数据采集 ──
    FOOTBALL_DATA_API_KEY: Optional[str] = None
    THE_ODDS_API_KEY: Optional[str] = None
    RAPIDAPI_KEY: Optional[str] = None

    # ── 硬件 ──
    GPU_MODE: str = "auto"
    TRAIN_USE_GPU: bool = True
    GPU_DEVICE_IDS: str = "0"
    GPU_MEM_LIMIT_RATIO: float = 0.7
    GPU_AUTO_FALLBACK: bool = True
    CUDA_MIXED_PRECISION: bool = True
    DATALOADER_NUM_WORKERS: int = 4
    TRAIN_BATCH_SIZE: int = 256
    CPU_N_JOBS: int = 0
    CUDA_VISIBLE_DEVICES: str = ""

    # ── 时区 ──
    TIMEZONE: str = "UTC"

    # ── 监控/日志 ──
    PROMETHEUS_PORT: int = 9090
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "logs/app.log"
    LOG_MAX_BYTES: int = 10485760
    LOG_BACKUP_COUNT: int = 5

    # ── 项目路径 ──
    PROJECT_ROOT: str = _PROJECT_ROOT
    OUTPUT_DIR: str = "output"
    DATA_DIR: str = "data"
    SANDBOX_DIR: str = "sandbox"
    ARCHIVE_DIR: str = "archive"

    # ── 预测阈值 ──
    DRAW_THRESHOLD: float = 0.32
    HA_GAP: float = 0.0
    DGATE_JUNK_THRESHOLD: float = 0.02
    DGATE_FUZZY_THRESHOLD: float = 0.05
    DGATE_USABLE_THRESHOLD: float = 0.08
    V32_DRAW_THRESHOLD: float = 0.0
    V32_HA_GAP: float = 0.0

    # ── 全局功能开关 ──
    PURE_V32_MODE: bool = False
    ENABLE_6LAYER_ENGINE: bool = True
    ENABLE_L0_KNOWLEDGE: bool = True
    ENABLE_L4_BARRIER: bool = True
    ENABLE_L4_SCENARIO: bool = True
    ENABLE_L4_DEGRADATION: bool = True
    ENABLE_CROSS_OPPONENT: bool = True
    ENABLE_BALANCE_SIM: bool = True
    ENABLE_SCORER_TRACKER: bool = True
    ENABLE_IMAGE_INPUT: bool = True

    # ── 容错降级 ──
    DEGRADATION_MAX_FAILURES: int = 3
    DEGRADATION_RECOVERY_THRESHOLD: int = 2
    DEGRADATION_FALLBACK: str = "0.333,0.334,0.333"

    # ── 场景配置 (dict) ──
    SCENARIO_CONFIG: Optional[Dict[str, Any]] = None

    # ── 专家权重 (dict) ──
    EXPERT_WEIGHTS: Optional[Dict[str, Any]] = None

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return _parse_cors_origins(v)
        if isinstance(v, list):
            return v
        return []

    def model_post_init(self, _context):
        """配置加载后自动填充默认值并校验"""

        # ── 场景默认值 ──
        if self.SCENARIO_CONFIG is None:
            self.SCENARIO_CONFIG = {
                "league": {"draw_target_rate": 0.25, "home_advantage": 0.08, "confidence_mult": 1.0, "trap_threshold_mult": 1.0},
                "cup_group": {"draw_target_rate": 0.375, "home_advantage": 0.03, "cold_start_mix": 0.15, "confidence_mult": 0.85, "trap_threshold_mult": 1.2},
                "cup_knockout": {"draw_target_rate": 0.40, "home_advantage": 0.02, "cold_start_mix": 0.05, "confidence_mult": 0.80, "trap_threshold_mult": 1.3},
                "final": {"draw_target_rate": 0.42, "home_advantage": 0.0, "cold_start_mix": 0.10, "confidence_mult": 0.70, "trap_threshold_mult": 1.5, "forbid_heavy_favorite_bet": True},
                "strong_favorite": {"draw_target_rate": 0.15, "trap_threshold_mult": 0.80, "confidence_mult": 0.85, "forbid_heavy_favorite_bet": True},
                "derby": {"draw_target_rate": 0.32, "home_advantage": 0.04, "trap_threshold_mult": 1.1, "confidence_mult": 0.90},
            }

        # ── 专家权重默认值 ──
        if self.EXPERT_WEIGHTS is None:
            self.EXPERT_WEIGHTS = {
                "quant_poisson":    {"enable": True, "default_weight": 1.0},
                "game_theorist":    {"enable": True, "default_weight": 1.0},
                "imbalance_expert": {"enable": True, "default_weight": 1.0},
                "ensemble_master":  {"enable": True, "default_weight": 1.0},
                "model_builder":    {"enable": True, "default_weight": 0.8},
                "temporal_analyst": {"enable": True, "default_weight": 0.8},
                "optimizer":        {"enable": True, "default_weight": 0.5},
            }

        # ── CORS 默认值 ──
        if not self.CORS_ORIGINS:
            self.CORS_ORIGINS = ["http://localhost:3000", "http://localhost:9000"]
            if self.DEBUG:
                logger.warning("DEBUG 模式：CORS_ORIGINS 未设置，使用默认开发值。")

        # ── SECRET_KEY 校验 ──
        if self.DEBUG and len(self.SECRET_KEY) < 32:
            self.SECRET_KEY = secrets.token_hex(32)
            logger.warning("DEBUG 模式：自动生成临时 SECRET_KEY")
        elif not self.DEBUG and len(self.SECRET_KEY) < 32:
            raise ValueError(f"SECRET_KEY 长度不足 ({len(self.SECRET_KEY)})，至少需要 32 字符。")

    # ── 便捷方法 ──

    def is_enabled(self, feature: str) -> bool:
        """检查功能开关"""
        key = f"ENABLE_{feature.upper()}"
        return getattr(self, key, True)

    def get_scenario(self, name: str) -> Dict[str, Any]:
        """获取场景配置"""
        if self.SCENARIO_CONFIG is None:
            return {}
        return self.SCENARIO_CONFIG.get(name, {})

    def get_expert_weight(self, name: str) -> float:
        """获取专家权重"""
        if self.EXPERT_WEIGHTS is None:
            return 1.0
        return self.EXPERT_WEIGHTS.get(name, {}).get("default_weight", 1.0)


settings = Settings()
