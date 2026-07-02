"""
哨响AI - 硬件检测与配置模块
============================
自动检测可用硬件（GPU/CPU/内存）并提供优化配置参数。

用法:
    from config.hardware_config import get_hardware_config
    config = get_hardware_config()
    print(config.summary())
"""
import os
import logging
import multiprocessing
from typing import Dict, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False


@dataclass
class HardwareConfig:
    """硬件配置信息"""
    gpu_available: bool = False
    gpu_name: str = ""
    gpu_count: int = 0
    gpu_memory_mb: float = 0.0
    cuda_available: bool = False
    mps_available: bool = False
    pytorch_available: bool = False
    cupy_available: bool = False
    xgboost_gpu_available: bool = False

    cpu_count: int = 0
    cpu_physical: int = 0
    ram_total_mb: float = 0.0
    ram_available_mb: float = 0.0

    device: str = "cpu"
    batch_size: int = 1024
    n_jobs: int = 1
    use_gpu: bool = False
    gpu_batch_multiplier: float = 1.0
    training_backend: str = "sklearn"

    def summary(self) -> Dict[str, Any]:
        return {
            "device": self.device,
            "gpu": {
                "available": self.gpu_available,
                "name": self.gpu_name,
                "count": self.gpu_count,
                "memory_mb": round(self.gpu_memory_mb, 0),
            } if self.gpu_available else {"available": False},
            "cpu": {
                "logical_cores": self.cpu_count,
                "physical_cores": self.cpu_physical,
            },
            "ram": {
                "total_mb": round(self.ram_total_mb, 0),
                "available_mb": round(self.ram_available_mb, 0),
            },
            "optimization": {
                "training_backend": self.training_backend,
                "batch_size": self.batch_size,
                "n_jobs": self.n_jobs,
            },
        }

    @property
    def can_use_gpu(self) -> bool:
        return (self.gpu_available and self.pytorch_available) or self.xgboost_gpu_available


def _detect_gpu_pytorch() -> Dict:
    result = {
        "gpu_available": False, "gpu_name": "", "gpu_count": 0,
        "gpu_memory_mb": 0.0, "cuda_available": False,
        "mps_available": False, "pytorch_available": False, "device": "cpu",
    }
    if os.environ.get("CUDA_VISIBLE_DEVICES", "").strip() == "":
        return result
    try:
        import torch
        result["pytorch_available"] = True
        if torch.cuda.is_available():
            result["gpu_available"] = True
            result["cuda_available"] = True
            result["gpu_count"] = torch.cuda.device_count()
            result["gpu_name"] = torch.cuda.get_device_name(0)
            result["device"] = "cuda"
            try:
                result["gpu_memory_mb"] = torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)
            except Exception:
                pass
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            result["gpu_available"] = True
            result["mps_available"] = True
            result["gpu_name"] = "Apple MPS"
            result["gpu_count"] = 1
            result["device"] = "mps"
    except ImportError:
        pass
    except Exception:
        pass
    return result


def _detect_cpu() -> Dict:
    cpu_count = multiprocessing.cpu_count()
    cpu_physical = max(1, cpu_count // 2) if os.name == "nt" else cpu_count
    return {"cpu_count": cpu_count, "cpu_physical": cpu_physical}


def _detect_ram() -> Dict:
    result = {"ram_total_mb": 0.0, "ram_available_mb": 0.0}
    if _PSUTIL_AVAILABLE:
        try:
            mem = psutil.virtual_memory()
            result["ram_total_mb"] = mem.total / (1024 * 1024)
            result["ram_available_mb"] = mem.available / (1024 * 1024)
            return result
        except Exception:
            pass
    result["ram_total_mb"] = 8192.0
    result["ram_available_mb"] = 4096.0
    return result


def _compute_optimal_params(hw: HardwareConfig) -> None:
    if hw.can_use_gpu:
        hw.use_gpu = True
        hw.training_backend = "pytorch"
        gpu_mem = hw.gpu_memory_mb if hw.gpu_memory_mb > 0 else 4096
        hw.batch_size = max(int(gpu_mem * 100), 1024)
        hw.gpu_batch_multiplier = 10.0
    elif hw.cpu_count > 1:
        hw.n_jobs = min(hw.cpu_count - 1, 8)
        hw.batch_size = min(4096, max(512, int(hw.ram_available_mb // 4)))
    else:
        hw.batch_size = 1024
        hw.n_jobs = 1


_cached_config: Optional[HardwareConfig] = None


def get_hardware_config(force_refresh: bool = False) -> HardwareConfig:
    """获取硬件配置（单例缓存）"""
    global _cached_config
    if _cached_config is not None and not force_refresh:
        return _cached_config

    logger.info("正在检测硬件配置...")
    gpu_info = _detect_gpu_pytorch()
    cpu_info = _detect_cpu()
    ram_info = _detect_ram()

    config = HardwareConfig(
        gpu_available=gpu_info["gpu_available"],
        gpu_name=gpu_info["gpu_name"],
        gpu_count=gpu_info["gpu_count"],
        gpu_memory_mb=gpu_info["gpu_memory_mb"],
        cuda_available=gpu_info["cuda_available"],
        mps_available=gpu_info["mps_available"],
        pytorch_available=gpu_info["pytorch_available"],
        cpu_count=cpu_info["cpu_count"],
        cpu_physical=cpu_info["cpu_physical"],
        ram_total_mb=ram_info["ram_total_mb"],
        ram_available_mb=ram_info["ram_available_mb"],
        device=gpu_info["device"],
    )
    _compute_optimal_params(config)
    _cached_config = config
    return config


def reset_hardware_config():
    """重置缓存，下次调用重新检测"""
    global _cached_config
    _cached_config = None
