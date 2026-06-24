"""
哨响AI - 多层缓存管理器
======================
- L1: 进程内 LRU + TTL 缓存（静态数据：球队信息、积分榜）
- L2: Redis 缓存（可选，共享多进程/多实例）
- 自动降级：Redis 不可用时回退到 L1

用法:
    cache = CacheManager(redis_url=None)  # 仅 L1
    cache = CacheManager(redis_url="redis://localhost:6379")  # L1 + L2
"""

import time
import json
import hashlib
import threading
import logging
from collections import OrderedDict
from functools import wraps
from typing import Any, Callable, Optional, Dict, Tuple

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════
# L1: 进程内 LRU + TTL 缓存
# ══════════════════════════════════════════════════

class MemoryCache:
    """线程安全的 LRU + TTL 内存缓存"""

    def __init__(self, max_size: int = 500, default_ttl: int = 600):
        """
        Args:
            max_size: 最大条目数
            default_ttl: 默认过期时间（秒），0=永不过期
        """
        self._cache: OrderedDict[str, Tuple[Any, float]] = OrderedDict()
        self._lock = threading.RLock()
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def _make_key(self, key_parts) -> str:
        """生成缓存键"""
        if isinstance(key_parts, str):
            return key_parts
        raw = json.dumps(key_parts, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, key_parts) -> Optional[Any]:
        """获取缓存值（带 LRU 晋升和 TTL 检查）"""
        key = self._make_key(key_parts)
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            value, expires_at = self._cache[key]
            # TTL 检查
            if self.default_ttl > 0 and time.time() > expires_at:
                del self._cache[key]
                self._misses += 1
                return None
            # LRU: 移到末尾
            self._cache.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key_parts, value: Any, ttl: int = None) -> None:
        """设置缓存值"""
        key = self._make_key(key_parts)
        ttl = ttl if ttl is not None else self.default_ttl
        expires_at = time.time() + ttl if ttl > 0 else float('inf')
        with self._lock:
            # 驱逐最旧条目
            while len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            # 如果 key 已存在，先删除再插入（更新位置）
            if key in self._cache:
                del self._cache[key]
            self._cache[key] = (value, expires_at)

    def invalidate(self, key_parts=None) -> int:
        """失效缓存（不指定 key 则清空全部）"""
        with self._lock:
            if key_parts is None:
                count = len(self._cache)
                self._cache.clear()
                return count
            key = self._make_key(key_parts)
            if key in self._cache:
                del self._cache[key]
                return 1
            return 0

    @property
    def stats(self) -> Dict:
        """缓存统计"""
        with self._lock:
            active = sum(
                1 for _, (_, exp) in self._cache.items()
                if exp > time.time()
            )
            hit_rate = self._hits / max(self._hits + self._misses, 1)
            return {
                "entries": len(self._cache),
                "active_entries": active,
                "max_size": self.max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 4),
            }


# ══════════════════════════════════════════════════
# L2: Redis 缓存（可选）
# ══════════════════════════════════════════════════

class RedisCache:
    """Redis 缓存包装器，自动降级"""

    def __init__(self, redis_url: str, prefix: str = "footballai:"):
        self._redis = None
        self._prefix = prefix
        self._available = False
        try:
            import redis
            self._redis = redis.from_url(
                redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            self._redis.ping()
            self._available = True
            logger.info(f"[Cache] Redis 已连接: {redis_url}")
        except ImportError:
            logger.warning("[Cache] redis-py 未安装，Redis 缓存不可用")
        except (Exception, KeyError, IndexError) as e:
            logger.warning(f"[Cache] Redis 连接失败: {e}，回退到内存缓存")

    def _key(self, raw: str) -> str:
        return f"{self._prefix}{raw}"

    def get(self, key: str) -> Optional[Any]:
        if not self._available:
            return None
        try:
            data = self._redis.get(self._key(key))
            return json.loads(data) if data else None
        except (Exception, KeyError, IndexError, requests.exceptions.RequestException, json.JSONDecodeError) as e:
            logger.debug(f"[Cache] Redis GET 失败: {e}")
            return None

    def set(self, key: str, value: Any, ttl: int = 600) -> bool:
        if not self._available:
            return False
        try:
            self._redis.setex(
                self._key(key),
                ttl,
                json.dumps(value, ensure_ascii=False, default=str)
            )
            return True
        except (Exception, json.JSONDecodeError) as e:
            logger.debug(f"[Cache] Redis SET 失败: {e}")
            return False

    def delete(self, key: str) -> bool:
        if not self._available:
            return False
        try:
            self._redis.delete(self._key(key))
            return True
        except (Exception, KeyError, IndexError):
            return False

    @property
    def available(self) -> bool:
        return self._available


# ══════════════════════════════════════════════════
# 统一缓存管理器
# ══════════════════════════════════════════════════

class CacheManager:
    """
    双层缓存管理器：L1 内存 + L2 Redis

    缓存策略：
    - 球队信息(teams): TTL=3600s (1小时)
    - 积分榜(standings): TTL=1800s (30分钟)
    - 联赛列表(leagues): TTL=86400s (24小时)
    - 预测特征(features): TTL=600s (10分钟)
    - 外部API响应: TTL=300s (5分钟，速率限制防护)
    """

    # 各类别默认 TTL (秒)
    DEFAULT_TTLS = {
        "teams": 3600,
        "standings": 1800,
        "leagues": 86400,
        "features": 600,
        "api_response": 300,
        "odds": 300,
        "form_trends": 900,
        "default": 600,
    }

    def __init__(self, redis_url: Optional[str] = None, memory_size: int = 500):
        self._l1 = MemoryCache(max_size=memory_size)
        self._l2 = RedisCache(redis_url) if redis_url else None

    def get(self, namespace: str, key_parts, ttl: int = None) -> Optional[Any]:
        """从缓存获取值：L1 -> L2 -> miss"""
        # L1
        cache_key = (namespace, key_parts)
        value = self._l1.get(cache_key)
        if value is not None:
            return value
        # L2
        if self._l2:
            l2_key = f"{namespace}:{self._l1._make_key(key_parts)}"
            value = self._l2.get(l2_key)
            if value is not None:
                # 回填 L1
                ttl = ttl or self.DEFAULT_TTLS.get(namespace, 600)
                self._l1.set(cache_key, value, ttl)
                return value
        return None

    def set(self, namespace: str, key_parts, value: Any, ttl: int = None) -> None:
        """写入双层缓存"""
        ttl = ttl or self.DEFAULT_TTLS.get(namespace, 600)
        cache_key = (namespace, key_parts)
        # L1
        self._l1.set(cache_key, value, ttl)
        # L2
        if self._l2:
            l2_key = f"{namespace}:{self._l1._make_key(key_parts)}"
            self._l2.set(l2_key, value, ttl)

    def invalidate(self, namespace: str, key_parts=None) -> int:
        """失效缓存"""
        cache_key = (namespace, key_parts) if key_parts is not None else None
        count = self._l1.invalidate(cache_key)
        if self._l2 and key_parts:
            l2_key = f"{namespace}:{self._l1._make_key(cache_key)}"
            self._l2.delete(l2_key)
        return count

    @property
    def stats(self) -> Dict:
        """完整缓存统计"""
        s = {"l1": self._l1.stats}
        if self._l2:
            s["l2_available"] = self._l2.available
        return s


# ══════════════════════════════════════════════════
# 缓存装饰器
# ══════════════════════════════════════════════════

def cached(namespace: str, ttl: int = None, key_func: Callable = None):
    """
    方法级缓存装饰器

    用法:
        @cached("teams", ttl=3600)
        def get_team_info(self, team_name: str):
            ...

    缓存键自动从函数名+参数生成
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 尝试从 self 获取 cache_manager
            obj = args[0] if args else None
            cache_mgr = getattr(obj, 'cache_manager', None)
            if cache_mgr is None:
                return func(*args, **kwargs)

            # 生成缓存键
            if key_func:
                cache_key = key_func(*args, **kwargs)
            else:
                # 默认：函数名 + 除 self 外的所有参数
                f_args = args[1:] if args else ()
                cache_key = (func.__name__,) + f_args + tuple(sorted(kwargs.items()))

            # 检查缓存
            cached_val = cache_mgr.get(namespace, cache_key, ttl)
            if cached_val is not None:
                return cached_val

            # 执行并缓存
            result = func(*args, **kwargs)
            cache_mgr.set(namespace, cache_key, result, ttl)
            return result
        return wrapper
    return decorator


# ══════════════════════════════════════════════════
# 全局单例
# ══════════════════════════════════════════════════

_global_cache: Optional[CacheManager] = None


def get_cache(redis_url: str = None, memory_size: int = 500) -> CacheManager:
    """获取全局缓存管理器单例"""
    global _global_cache
    if _global_cache is None:
        redis_env = redis_url or None
        if not redis_env:
            import os
            redis_env = os.getenv("REDIS_URL", "").strip() or None
        _global_cache = CacheManager(redis_url=redis_env, memory_size=memory_size)
        logger.info(f"[Cache] 缓存管理器初始化 (Redis: {redis_env or 'disabled'}, "
                    f"Memory: {memory_size} entries)")
    return _global_cache
