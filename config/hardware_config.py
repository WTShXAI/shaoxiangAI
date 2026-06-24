"""
哨响AI - 硬件检测与配置模块 v1.0
================================
自动检测可用硬件（GPU/CPU/内存）并提供优化配置参数。

支持的硬件:
  - GPU: NVIDIA CUDA (via PyTorch), Apple MPS (via PyTorch)
  - CPU: 多核并行 (via sklearn n_jobs / numpy threading)
  - RAM: 可用内存检测，动态调整批处理大小

用法:
    from config.hardware_config import get_hardware_config
    config = get_hardware_config()
    print(config.summary())
"""

import os
import logging
import multiprocessing
from typing import Dict, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 尝试导入内存检测库
try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False


@dataclass
class HardwareConfig:
    """硬件配置信息"""
    # GPU 信息
    gpu_available: bool = False
    gpu_name: str = ""
    gpu_count: int = 0
    gpu_memory_mb: float = 0.0
    cuda_available: bool = False
    mps_available: bool = False
    pytorch_available: bool = False
    cupy_available: bool = False
    xgboost_gpu_available: bool = False  # XGBoost 自带 CUDA 支持，不依赖 PyTorch

    # CPU 信息
    cpu_count: int = 0
    cpu_physical: int = 0

    # 内存信息（MB）
    ram_total_mb: float = 0.0
    ram_available_mb: float = 0.0

    # 计算设备
    device: str = "cpu"  # 'cuda', 'mps', 'cpu'

    # 优化参数（根据硬件自动计算）
    batch_size: int = 1024
    n_jobs: int = 1
    use_gpu: bool = False
    gpu_batch_multiplier: float = 1.0

    # 特征
    training_backend: str = "sklearn"  # 'sklearn', 'pytorch', 'cupy'

    def summary(self) -> Dict[str, Any]:
        """返回硬件信息摘要"""
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

    def log_summary(self):
        """打印硬件信息到日志"""
        s = self.summary()
        lines = [
            "=" * 60,
            " 哨响AI - 硬件配置检测",
            "=" * 60,
            f" 设备:          {s['device'].upper()}",
            f" GPU:           {'✅ ' + s['gpu']['name'] if s['gpu']['available'] else '❌ 不可用'}",
        ]
        if s['gpu']['available']:
            lines.append(f"   GPU 数量:     {s['gpu']['count']}")
            lines.append(f"   GPU 显存:     {s['gpu']['memory_mb']:.0f} MB")
        lines.extend([
            f" CPU:           {s['cpu']['logical_cores']} 逻辑核心 ({s['cpu']['physical_cores']} 物理核心)",
            f" 内存:          {s['ram']['total_mb']:.0f} MB 总量 / {s['ram']['available_mb']:.0f} MB 可用",
            f" 训练后端:      {s['optimization']['training_backend']}",
            f" 批处理大小:    {s['optimization']['batch_size']}",
            f" 并行度:        {s['optimization']['n_jobs']}",
            "=" * 60,
        ])
        for line in lines:
            logger.info(line)
        return "\n".join(lines)

    @property
    def can_use_gpu(self) -> bool:
        """是否可以真正使用GPU（PyTorch 或 XGBoost 任一可用即可）"""
        return (self.gpu_available and self.pytorch_available) or self.xgboost_gpu_available


def _detect_gpu_pytorch() -> Dict:
    """通过 PyTorch 检测 GPU"""
    result = {
        "gpu_available": False,
        "gpu_name": "",
        "gpu_count": 0,
        "gpu_memory_mb": 0.0,
        "cuda_available": False,
        "mps_available": False,
        "pytorch_available": False,
        "device": "cpu",
    }

    # Runtime修复: CUDA_VISIBLE_DEVICES="" 时跳过PyTorch GPU检测, 避免 segfault
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

            # 获取显存
            try:
                result["gpu_memory_mb"] = (
                    torch.cuda.get_device_properties(0).total_memory / (1024 * 1024)
                )
            except (Exception, KeyError, IndexError):
                result["gpu_memory_mb"] = 0.0

            logger.info(f"检测到 CUDA GPU: {result['gpu_name']} "
                        f"({result['gpu_count']}个, "
                        f"{result['gpu_memory_mb']:.0f}MB)")

        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            result["gpu_available"] = True
            result["mps_available"] = True
            result["gpu_name"] = "Apple MPS"
            result["gpu_count"] = 1
            result["device"] = "mps"
            logger.info("检测到 Apple MPS GPU")

        else:
            logger.info("PyTorch 可用但未检测到 GPU，使用 CPU 模式")

    except ImportError:
        logger.info("PyTorch 未安装，将使用纯 CPU 模式（scikit-learn）")
    except OSError as e:
        logger.warning(f"PyTorch 导入失败 (缺系统库): {e}")
        logger.info("将使用 CPU 模式（scikit-learn）")
    except (Exception) as e:
        logger.warning(f"PyTorch 初始化异常: {e}")
        logger.info("将使用 CPU 模式（scikit-learn）")

    return result


def _detect_gpu_xgboost() -> bool:
    """通过 XGBoost 检测 GPU（XGBoost 自带 CUDA 支持，不依赖 PyTorch）。
    适用于 PyTorch 不支持的新架构 GPU（如 RTX 5070 Ti sm_120）。
    """
    try:
        import xgboost as xgb
        import numpy as np
        # 尝试用 GPU 创建并训练一个微型模型
        dtrain = xgb.DMatrix(np.zeros((10, 5), dtype=np.float32),
                             label=np.zeros(10, dtype=np.float32))
        params = {'device': 'cuda', 'tree_method': 'hist', 'max_depth': 1}
        xgb.train(params, dtrain, num_boost_round=1)
        logger.info("XGBoost GPU (CUDA) 加速可用")
        return True
    except ImportError:
        logger.info("XGBoost 未安装，无法检测 GPU")
    except (Exception) as e:
        logger.info(f"XGBoost GPU 不可用: {e}")
    return False


def _detect_gpu_cupy() -> bool:
    """检测 CuPy GPU 加速是否可用"""
    try:
        import cupy as cp
        if cp.cuda.is_available():
            logger.info(f"CuPy GPU 加速可用: {cp.cuda.runtime.getDeviceCount()} 个设备")
            return True
    except ImportError:
        pass
    except (Exception):
        pass
    return False


def _detect_cpu() -> Dict:
    """检测 CPU 信息"""
    cpu_count = multiprocessing.cpu_count()
    try:
        cpu_physical = multiprocessing.cpu_count()
        # Windows 上 multiprocessing.cpu_count() 返回逻辑核心数
        if os.name == 'nt':
            cpu_physical = max(1, cpu_count // 2)  # 超线程约2x
        else:
            cpu_physical = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else cpu_count // 2
    except (Exception):
        cpu_physical = max(1, cpu_count // 2)

    return {
        "cpu_count": cpu_count,
        "cpu_physical": max(1, cpu_physical),
    }


def _detect_ram() -> Dict:
    """检测内存信息"""
    result = {"ram_total_mb": 0.0, "ram_available_mb": 0.0}

    if _PSUTIL_AVAILABLE:
        try:
            mem = psutil.virtual_memory()
            result["ram_total_mb"] = mem.total / (1024 * 1024)
            result["ram_available_mb"] = mem.available / (1024 * 1024)
            return result
        except (Exception, KeyError, IndexError):
            pass

    # 备选：Windows WMI 方式
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                ]

            mem_status = MEMORYSTATUSEX()
            mem_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if kernel32.GlobalMemoryStatusEx(ctypes.byref(mem_status)):
                result["ram_total_mb"] = mem_status.ullTotalPhys / (1024 * 1024)
                result["ram_available_mb"] = mem_status.ullAvailPhys / (1024 * 1024)
        except (Exception, KeyError, IndexError):
            pass

    # 最终回退
    if result["ram_total_mb"] == 0.0:
        result["ram_total_mb"] = 8192.0  # 默认8GB
        result["ram_available_mb"] = 4096.0

    return result


def _compute_optimal_params(hw: HardwareConfig) -> None:
    """
    根据检测到的硬件计算最优参数。

    策略:
    - GPU 可用 → PyTorch 后端 + 大batch（显存/10 per batch ≈ 1GB）
    - 无GPU但多核 → sklearn + n_jobs 并行
    - 单核或无要求 → 保持默认
    """
    if hw.can_use_gpu:
        hw.use_gpu = True
        hw.training_backend = "pytorch"

        # 根据显存计算批次大小
        gpu_mem = hw.gpu_memory_mb if hw.gpu_memory_mb > 0 else 4096
        # 每个样本约 1KB（float32 × 9特征 ≈ 36 bytes，加上开销）
        # 用显存的 1/10 做 batch ≈ gpu_mem/10 * 1024*1024 / 36 ≈ gpu_mem * 2900
        # 保守估计：显存(MB) * 100
        hw.batch_size = min(int(gpu_mem * 100), 65536)
        hw.batch_size = max(hw.batch_size, 1024)
        hw.gpu_batch_multiplier = 10.0  # GPU 上 batch 可以放大

        logger.info(f"GPU 模式: batch_size={hw.batch_size}, "
                    f"backend={hw.training_backend}")

    elif hw.training_backend == "sklearn" and hw.cpu_count > 1:
        # CPU 多核模式
        hw.n_jobs = min(hw.cpu_count - 1, 8)  # 留一个核心给系统
        hw.batch_size = min(4096, max(512, int(hw.ram_available_mb // 4)))
        logger.info(f"CPU 多核模式: n_jobs={hw.n_jobs}, batch_size={hw.batch_size}")

    else:
        hw.batch_size = 1024
        hw.n_jobs = 1
        logger.info("CPU 单核模式")


# ═══════════════════════════════════════════════════════════════════
#  公开接口
# ═══════════════════════════════════════════════════════════════════

# 模块级缓存：只检测一次
_cached_config: Optional[HardwareConfig] = None


def get_hardware_config(force_refresh: bool = False) -> HardwareConfig:
    """
    获取硬件配置（单例缓存）。

    Args:
        force_refresh: 是否强制重新检测

    Returns:
        HardwareConfig 实例
    """
    global _cached_config
    if _cached_config is not None and not force_refresh:
        return _cached_config

    logger.info("正在检测硬件配置...")

    # 1. GPU 检测（PyTorch）
    gpu_info = _detect_gpu_pytorch()

    # 2. XGBoost GPU 检测（独立于 PyTorch，支持新架构）
    xgb_gpu = _detect_gpu_xgboost()
    if xgb_gpu and not gpu_info["gpu_available"]:
        # PyTorch 不支持此 GPU，但 XGBoost 支持 → 更新 GPU 信息
        try:
            import subprocess
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split(',')
                gpu_info["gpu_name"] = parts[0].strip() if len(parts) >= 1 else "NVIDIA GPU (XGBoost)"
                gpu_info["gpu_memory_mb"] = float(parts[1].strip()) if len(parts) >= 2 else 0
            else:
                gpu_info["gpu_name"] = "NVIDIA GPU (XGBoost)"
        except (Exception, ValueError, KeyError, IndexError):
            gpu_info["gpu_name"] = "NVIDIA GPU (XGBoost)"
        gpu_info["gpu_available"] = True
        gpu_info["cuda_available"] = True
        gpu_info["device"] = "cuda"
        gpu_info["gpu_count"] = 1
        logger.info(f"GPU 检测: PyTorch 不支持此 GPU，但 XGBoost CUDA 可用 → {gpu_info['gpu_name']}")

    # 3. CPU 检测
    cpu_info = _detect_cpu()

    # 3. 内存检测
    ram_info = _detect_ram()

    # 4. CuPy 检测
    cupy_ok = _detect_gpu_cupy()

    # 5. 构建配置
    config = HardwareConfig(
        gpu_available=gpu_info["gpu_available"],
        gpu_name=gpu_info["gpu_name"],
        gpu_count=gpu_info["gpu_count"],
        gpu_memory_mb=gpu_info["gpu_memory_mb"],
        cuda_available=gpu_info["cuda_available"],
        mps_available=gpu_info["mps_available"],
        pytorch_available=gpu_info["pytorch_available"],
        cupy_available=cupy_ok,
        xgboost_gpu_available=xgb_gpu,
        cpu_count=cpu_info["cpu_count"],
        cpu_physical=cpu_info["cpu_physical"],
        ram_total_mb=ram_info["ram_total_mb"],
        ram_available_mb=ram_info["ram_available_mb"],
        device=gpu_info["device"],
    )

    # 6. 计算最优参数
    _compute_optimal_params(config)

    _cached_config = config
    return config


def reset_hardware_config():
    """重置缓存，下次调用 re-detect"""
    global _cached_config
    _cached_config = None


# ═══════════════════════════════════════════════════════════════════
#  GPU 内存管理工具
# ═══════════════════════════════════════════════════════════════════

class GPUMemoryMonitor:
    """GPU 内存使用监控器"""

    def __init__(self, hw: Optional[HardwareConfig] = None):
        self.hw = hw or get_hardware_config()
        self._peak_memory = 0.0

    @property
    def is_gpu_available(self) -> bool:
        return self.hw.gpu_available and self.hw.pytorch_available

    def get_memory_usage(self) -> Optional[Dict[str, float]]:
        """获取当前 GPU 内存使用量（MB）"""
        if not self.is_gpu_available:
            return None
        try:
            import torch
            if torch.cuda.is_available():
                allocated = torch.cuda.memory_allocated(0) / (1024 * 1024)
                reserved = torch.cuda.memory_reserved(0) / (1024 * 1024)
                self._peak_memory = max(self._peak_memory, allocated)
                return {
                    "allocated_mb": round(allocated, 1),
                    "reserved_mb": round(reserved, 1),
                    "peak_mb": round(self._peak_memory, 1),
                    "total_mb": round(self.hw.gpu_memory_mb, 0),
                }
        except (Exception):
            pass
        return None

    def clear_cache(self):
        """清理 GPU 缓存"""
        if not self.is_gpu_available:
            return
        try:
            import torch
            torch.cuda.empty_cache()
        except (Exception):
            pass

    def log_memory(self, tag: str = ""):
        """记录 GPU 内存使用情况到日志"""
        usage = self.get_memory_usage()
        if usage:
            label = f"[{tag}] " if tag else ""
            logger.info(
                f"{label}GPU内存: {usage['allocated_mb']:.0f}MB / "
                f"{usage['total_mb']:.0f}MB "
                f"(峰值: {usage['peak_mb']:.0f}MB)"
            )


def estimate_batch_size(hw: HardwareConfig, feature_count: int,
                         bytes_per_element: int = 4) -> int:
    """
    根据可用显存/内存估算安全批量大小。

    Args:
        hw: 硬件配置
        feature_count: 特征数量
        bytes_per_element: 每个元素的字节数（float32=4, float64=8）

    Returns:
        推荐批量大小
    """
    if hw.can_use_gpu and hw.gpu_memory_mb > 0:
        # 使用 GPU 显存的 1/10 作为工作空间
        work_memory_mb = hw.gpu_memory_mb * 0.1
    else:
        # 使用可用 RAM 的 1/8
        work_memory_mb = hw.ram_available_mb * 0.125

    # 单条样本大小（特征数 × 字节数 + 标签 + 开销）
    sample_bytes = feature_count * bytes_per_element + 8 + 64
    batch = int((work_memory_mb * 1024 * 1024) / sample_bytes)

    return max(256, min(batch, 65536))


# ═══════════════════════════════════════════════════════════════════
#  内存优化数据加载器
# ═══════════════════════════════════════════════════════════════════

class MemoryAwareDataLoader:
    """
    内存感知数据加载器，支持分块处理大型数据集。

    用途:
    - 当数据量超过可用内存时，分块加载处理
    - 在 GPU 上逐 batch 处理，避免 OOM
    """

    def __init__(self, hw: Optional[HardwareConfig] = None):
        self.hw = hw or get_hardware_config()
        self.monitor = GPUMemoryMonitor(self.hw)

    def should_chunk(self, n_samples: int, n_features: int) -> bool:
        """
        判断是否需要分块处理。
        估算: 原始数据 + 中间结果 + 模型参数 ≈ n_samples × n_features × 12 bytes
        """
        est_memory_mb = (n_samples * n_features * 12) / (1024 * 1024)

        if self.hw.can_use_gpu:
            # GPU: 单次加载不超过显存的 50%
            return est_memory_mb > (self.hw.gpu_memory_mb * 0.5)
        else:
            # CPU: 单次加载不超过可用内存的 30%
            return est_memory_mb > (self.hw.ram_available_mb * 0.3)

    def compute_chunk_size(self, n_features: int) -> int:
        """计算安全的分块大小"""
        return estimate_batch_size(self.hw, n_features)

    def numpy_to_device(self, array, device: Optional[str] = None):
        """
        将 numpy 数组转移到计算设备（GPU/CPU）。

        Args:
            array: numpy array
            device: 'cuda', 'mps', 'cpu'（None→自动选择）
        """
        if device is None:
            device = self.hw.device

        if device == "cpu" or not self.hw.pytorch_available:
            return array

        try:
            import torch
            tensor = torch.from_numpy(array).float()
            if device == "cuda" and self.hw.cuda_available:
                return tensor.cuda()
            elif device == "mps" and self.hw.mps_available:
                return tensor.to("mps")
            return tensor
        except (Exception, ValueError):
            return array

    def device_to_numpy(self, tensor) -> 'np.ndarray':
        """将设备上的 tensor 转回 numpy"""
        try:
            import torch
            if isinstance(tensor, torch.Tensor):
                return tensor.detach().cpu().numpy()
        except (Exception):
            pass
        return tensor
