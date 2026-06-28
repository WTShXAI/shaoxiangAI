"""
哨响AI v4.0 — 配置加载器 (Pydantic 代理层)
=============================================
2026-06-28: 统一配置 → 后端配置由 backend/core/config.py (Pydantic) 管理.
本文件保留作为向后兼容层, 顶层模块的 `from config.settings import get_setting` 仍然可用.

用法:
    from config.settings import get_setting, load_config, settings
    threshold = get_setting('prediction.draw_threshold')   # 点号路径兼容
    threshold = settings.DRAW_THRESHOLD                     # Pydantic 直接访问

迁移指南:
    新代码请直接 `from backend.core.config import settings`
"""
import warnings
from backend.core.config import settings as _pydantic_settings

warnings.warn(
    "config.settings 已迁移至 backend.core.config (Pydantic). "
    "请更新 import: from backend.core.config import settings",
    DeprecationWarning, stacklevel=2,
)

def load_config():
    """向后兼容: 返回 pydantic settings 对象 (原返回 dict)"""
    return _pydantic_settings

def get_setting(path: str, default=None):
    """
    向后兼容: 点号路径获取配置值.
    优先匹配 Pydantic 大写字段, 再走 fallback.

    示例:
        get_setting('prediction.draw_threshold')  → 0.32
        get_setting('global_switches.pure_v32_mode') → False
    """
    # 尝试 Pydantic 直接字段名映射
    field_map = {
        'prediction.draw_threshold': _pydantic_settings.DRAW_THRESHOLD,
        'prediction.ha_gap': _pydantic_settings.HA_GAP,
        'prediction.d_gate.junk_threshold': _pydantic_settings.DGATE_JUNK_THRESHOLD,
        'prediction.d_gate.fuzzy_threshold': _pydantic_settings.DGATE_FUZZY_THRESHOLD,
        'prediction.d_gate.usable_threshold': _pydantic_settings.DGATE_USABLE_THRESHOLD,
        'prediction.v32_baseline.draw_threshold': _pydantic_settings.V32_DRAW_THRESHOLD,
        'prediction.v32_baseline.ha_gap': _pydantic_settings.V32_HA_GAP,
        'global_switches.pure_v32_mode': _pydantic_settings.PURE_V32_MODE,
        'server.host': _pydantic_settings.HOST,
        'server.port': _pydantic_settings.PORT,
        'paths.project_root': _pydantic_settings.PROJECT_ROOT,
        'paths.model_dir': _pydantic_settings.MODEL_DIR,
        'paths.db_path': _pydantic_settings.DB_PATH,
        'paths.v41_model': _pydantic_settings.V41_MODEL,
        'paths.v32_model': _pydantic_settings.V32_MODEL,
        'paths.draw_expert': _pydantic_settings.DRAW_EXPERT,
        'paths.nn_model': _pydantic_settings.NN_MODEL,
        'paths.sp_db_path': _pydantic_settings.SP_DB_PATH,
        'paths.output_dir': _pydantic_settings.OUTPUT_DIR,
        'paths.data_dir': _pydantic_settings.DATA_DIR,
        'logging.level': _pydantic_settings.LOG_LEVEL,
    }
    if path in field_map:
        return field_map[path]

    # 功能开关映射
    if path.startswith('global_switches.enable_'):
        feature = path.replace('global_switches.enable_', '')
        return _pydantic_settings.is_enabled(feature)

    # 场景映射
    if path.startswith('scenarios.'):
        scenario_key = path[len('scenarios.'):]
        dot_idx = scenario_key.find('.')
        if dot_idx > 0:
            scenario_name = scenario_key[:dot_idx]
            field = scenario_key[dot_idx + 1:]
            sc = _pydantic_settings.get_scenario(scenario_name)
            return sc.get(field, default) if isinstance(sc, dict) else default

    # 专家权重映射
    if path.startswith('experts.'):
        parts = path.split('.')
        if len(parts) >= 3 and parts[2] == 'default_weight':
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

def reload_config():
    """向后兼容: 重新加载 (Pydantic 已单例, 此函数保留无实际效果)"""
    warnings.warn("reload_config() 在 Pydantic 模式下无实际效果 (settings 是单例)", DeprecationWarning)
    return _pydantic_settings

settings = _pydantic_settings
