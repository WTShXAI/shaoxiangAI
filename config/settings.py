"""
哨响AI - 配置代理层（向后兼容）
===============================
所有配置由 backend.core.config (Pydantic) 管理。
本模块提供向后兼容的 get_setting() / load_config() 接口。

用法:
    from config.settings import get_setting, settings
    threshold = get_setting("prediction.draw_threshold")
"""
from backend.core.config import settings as _pydantic_settings


def load_config():
    """向后兼容：返回 Pydantic settings 对象"""
    return _pydantic_settings


def get_setting(path: str, default=None):
    """向后兼容：点号路径获取配置值"""
    field_map = {
        "prediction.draw_threshold": _pydantic_settings.DRAW_THRESHOLD,
        "prediction.ha_gap": _pydantic_settings.HA_GAP,
        "prediction.d_gate.junk_threshold": _pydantic_settings.DGATE_JUNK_THRESHOLD,
        "prediction.d_gate.fuzzy_threshold": _pydantic_settings.DGATE_FUZZY_THRESHOLD,
        "prediction.d_gate.usable_threshold": _pydantic_settings.DGATE_USABLE_THRESHOLD,
        "prediction.v32_baseline.draw_threshold": _pydantic_settings.V32_DRAW_THRESHOLD,
        "prediction.v32_baseline.ha_gap": _pydantic_settings.V32_HA_GAP,
        "global_switches.pure_v32_mode": _pydantic_settings.PURE_V32_MODE,
        "server.host": _pydantic_settings.HOST,
        "server.port": _pydantic_settings.PORT,
        "paths.project_root": _pydantic_settings.PROJECT_ROOT,
        "paths.model_dir": _pydantic_settings.MODEL_DIR,
        "paths.db_path": _pydantic_settings.DB_PATH,
        "paths.v41_model": _pydantic_settings.V41_MODEL,
        "paths.v32_model": _pydantic_settings.V32_MODEL,
        "paths.draw_expert": _pydantic_settings.DRAW_EXPERT,
        "paths.nn_model": _pydantic_settings.NN_MODEL,
        "paths.output_dir": _pydantic_settings.OUTPUT_DIR,
        "paths.data_dir": _pydantic_settings.DATA_DIR,
        "logging.level": _pydantic_settings.LOG_LEVEL,
    }
    if path in field_map:
        return field_map[path]

    if path.startswith("global_switches.enable_"):
        feature = path.replace("global_switches.enable_", "")
        return _pydantic_settings.is_enabled(feature)

    if path.startswith("scenarios."):
        scenario_key = path[len("scenarios."):]
        dot_idx = scenario_key.find(".")
        if dot_idx > 0:
            sc = _pydantic_settings.get_scenario(scenario_key[:dot_idx])
            return sc.get(scenario_key[dot_idx + 1:], default) if isinstance(sc, dict) else default

    if path.startswith("experts."):
        parts = path.split(".")
        if len(parts) >= 3 and parts[2] == "default_weight":
            return _pydantic_settings.get_expert_weight(parts[1])

    return default


def is_pure_v32() -> bool:
    return _pydantic_settings.PURE_V32_MODE


def is_enabled(feature: str) -> bool:
    return _pydantic_settings.is_enabled(feature)


def get_scenario_config(scenario: str) -> dict:
    return _pydantic_settings.get_scenario(scenario)


def get_expert_weight(expert: str) -> float:
    return _pydantic_settings.get_expert_weight(expert)


settings = _pydantic_settings
