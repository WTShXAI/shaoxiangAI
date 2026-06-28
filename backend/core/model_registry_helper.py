"""
模型注册表读取工具 — 统一从 saved_models/model_registry.json 读取活跃版本号
避免硬编码版本字符串
"""
import json
import os

_MODEL_REGISTRY_PATH = None

def _get_registry_path() -> str:
    global _MODEL_REGISTRY_PATH
    if _MODEL_REGISTRY_PATH is None:
        # 尝试多个可能的根路径
        candidates = [
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ]
        for root in candidates:
            path = os.path.join(root, "saved_models", "model_registry.json")
            if os.path.isfile(path):
                _MODEL_REGISTRY_PATH = path
                break
        if _MODEL_REGISTRY_PATH is None:
            # 最后兜底
            _MODEL_REGISTRY_PATH = os.path.join("saved_models", "model_registry.json")
    return _MODEL_REGISTRY_PATH

def get_active_version() -> str:
    """从 model_registry.json 读取当前活跃模型版本号"""
    try:
        path = _get_registry_path()
        with open(path, "r", encoding="utf-8") as f:
            registry = json.load(f)
        return registry.get("active", "4.1")
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        import logging
        logging.getLogger(__name__).warning(f"读取 model_registry.json 失败: {e}")
        return "4.1"

def get_active_model_version() -> str:
    """返回完整的 model_version 字符串（带 v 前缀），如 'v3.2'"""
    active = get_active_version()
    return f"v{active}" if not active.startswith("v") else active
