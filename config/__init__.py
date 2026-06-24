"""
哨响AI 配置模块

从项目根目录的 config.yaml 加载配置，暴露以下顶级变量：
  - config: 完整配置字典
  - feature_columns: 72维特征列表 (data.feature_columns)
  - default_values: 特征默认值 (data.default_values)
  - db_path: 数据库路径
  - 其他常用配置项

用法:
    from config import feature_columns, default_values
    from config import config  # 完整配置字典
"""

import os
import yaml

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_PROJECT_ROOT, 'config.yaml')


def _load_config():
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    # 回退: 尝试 config/settings.yaml (v4.1 单人维护版)
    _settings_path = os.path.join(os.path.dirname(__file__), 'settings.yaml')
    if os.path.exists(_settings_path):
        with open(_settings_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        # 转换为旧格式兼容
        return {
            'data': {'feature_columns': [], 'default_values': {}},
            'database': {'path': cfg.get('paths', {}).get('db_path', 'data/football_data.db')},
            'paths': cfg.get('paths', {}),
            'models': {'main': cfg.get('paths', {}).get('v41_model', '')},
            'label': {},
        }
    return {'data': {'feature_columns': [], 'default_values': {}},
            'database': {'path': 'data/football_data.db'},
            'paths': {}, 'models': {}, 'label': {}}


config = _load_config()

# 常用顶级导出
feature_columns = config['data']['feature_columns']
default_values = config['data']['default_values']
db_path = os.path.join(_PROJECT_ROOT, config['database']['path'])
paths = config['paths']
data_config = config['data']
model_config = config['models']
label_config = config['label']

__all__ = [
    'config',
    'feature_columns',
    'default_values',
    'db_path',
    'paths',
    'data_config',
    'model_config',
    'label_config',
    # v4.1版本管理
    'APP_VERSION',
    'get_active_model_version',
    'get_active_model_info',
    'PRODUCTION_BASELINE',
]

# ━━━ APP v4.1 + v3.2 版本管理 ━━━
import json

APP_VERSION = "4.1"

_registry_path = os.path.join(_PROJECT_ROOT, "saved_models", "model_registry.json")

def get_active_model_version():
    """返回活跃模型版本号"""
    if os.path.exists(_registry_path):
        with open(_registry_path, 'r', encoding='utf-8') as f:
            reg = json.load(f)
        return reg.get("active", "3.2")
    return "3.2"

def get_active_model_info():
    """返回活跃模型的完整信息"""
    if os.path.exists(_registry_path):
        with open(_registry_path, 'r', encoding='utf-8') as f:
            reg = json.load(f)
        active = reg.get("active", "3.2")
        for v in reg.get("versions", []):
            if v.get("version") == active:
                return v
        return reg.get("current", {})
    return {}

ACTIVE_MODEL = get_active_model_version()
PRODUCTION_BASELINE = f"APP_{APP_VERSION}+V{ACTIVE_MODEL}"
