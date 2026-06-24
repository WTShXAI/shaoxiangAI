"""
哨响AI v4.0 — 配置加载器
=========================
从 config/settings.yaml 读取全局配置, 提供便捷访问接口。

单人维护版: 只用 PyYAML + 简单 dict 访问, 不引入复杂配置框架。

用法:
    from config.settings import load_config, get_setting
    cfg = load_config()
    threshold = get_setting('prediction.draw_threshold')
"""
import os
import yaml
import logging
from typing import Any, Dict, Optional
from functools import lru_cache

logger = logging.getLogger('Config')

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'settings.yaml')


@lru_cache(maxsize=1)
def load_config() -> Dict[str, Any]:
    """加载全局配置 (缓存, 只读一次)"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        logger.info(f"配置加载成功: {len(cfg)} 个顶级域")
        return cfg
    except FileNotFoundError:
        logger.warning("settings.yaml 不存在, 使用默认配置")
        return _default_config()
    except Exception as e:
        logger.error(f"配置加载失败: {e}")
        return _default_config()


def get_setting(path: str, default: Any = None) -> Any:
    """
    点号路径获取配置值

    Example:
        get_setting('prediction.draw_threshold')  → 0.46
        get_setting('scenarios.cup_group.draw_target_rate')  → 0.375
    """
    cfg = load_config()
    keys = path.split('.')
    value = cfg
    for key in keys:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return default
    return value


def is_pure_v32() -> bool:
    """是否处于v3.2纯净模式"""
    return get_setting('global_switches.pure_v32_mode', False)


def is_enabled(feature: str) -> bool:
    """检查功能开关是否开启"""
    return get_setting(f'global_switches.enable_{feature}', True)


def get_scenario_config(scenario: str) -> Dict:
    """获取场景配置"""
    return get_setting(f'scenarios.{scenario}', {})


def get_expert_weight(expert: str) -> float:
    """获取专家权重"""
    return get_setting(f'experts.{expert}.default_weight', 1.0)


def reload_config() -> Dict[str, Any]:
    """强制重新加载配置"""
    load_config.cache_clear()
    return load_config()


def _default_config() -> Dict[str, Any]:
    """兜底默认配置 (保证配置加载失败时系统仍可运行)"""
    return {
        'global_switches': {'pure_v32_mode': False},
        'prediction': {'draw_threshold': 0.46, 'ha_gap': 0.0},
        'paths': {'project_root': '.', 'output_dir': 'output'},
        'server': {'host': '0.0.0.0', 'port': 8000},
    }
