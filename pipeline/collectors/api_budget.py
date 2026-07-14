"""
api_budget.py — 中央 API 预算护栏 + 缓存层
===========================================
所有 The Odds API 调用必须经过本模块，禁止客户端裸 requests.get。

职责:
  1. 日配额硬闸 — daily_cap (默认 300/天)。跨进程磁盘计数，所有调用方共用。
     烧到上限即返回 429 (BUDGET_EXCEEDED)，调用方自然 no-op，彻底止血。
  2. 磁盘缓存 — 按 (url+params) 哈希落盘 data/cache/api_cache/，TTL 按 group 不同:
       sports : 24h  (联赛列表几乎不变)
       odds   : 1h   (比赛盘口)
       quota  : 5min (remaining 探测)
     缓存命中不花配额、不发请求。
  3. 真实剩余追踪 — 解析每次响应的 x-requests-remaining 头，落 quota_state 表。
     peek_remaining() 免调用读取，避免为"查剩余"再烧一次。
  4. 硬地板 — 真实剩余 < hard_floor_remaining (默认 500) 时拒绝新调用，留缓冲。

设计铁律:
  - 状态持久化到磁盘 (data/api_budget.db)，因为 bridge(常驻) 与 daily_collector(cron)
    是不同进程，内存计数会互相看不到。
  - guarded_get 返回 GuardResponse(status_code, text, headers, json)，向后兼容
    SPOddsAPI / TheOddsCollector 现有 resp.status_code / resp.text / resp.json() 用法。

配置 (config/api_budget.yaml 优先, 环境变量次之, 否则默认值):
  daily_cap: 300
  hard_floor_remaining: 500
  cache_ttl: {sports: 86400, odds: 3600, quota: 300}
"""
from __future__ import annotations
import os
import sys
import json
import time
import sqlite3
import logging
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = PROJECT_ROOT / "data" / "api_budget.db"
CACHE_ROOT = PROJECT_ROOT / "data" / "cache" / "api_cache"

# ── 默认配置 ──
_DEFAULTS = {
    "daily_cap": 300,
    "hard_floor_remaining": 500,
    "cache_ttl": {"sports": 86400, "odds": 3600, "quota": 300},
}

try:
    import requests  # noqa
except ImportError:
    requests = None  # 极端情况下仍可走缓存


def _load_config() -> Dict[str, Any]:
    cfg = dict(_DEFAULTS)
    # 1) yaml
    yml = PROJECT_ROOT / "config" / "api_budget.yaml"
    try:
        import yaml
        if yml.exists():
            with open(yml, "r", encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            cfg.update({k: v for k, v in user.items() if k in cfg})
            if isinstance(user.get("cache_ttl"), dict):
                cfg["cache_ttl"].update(user["cache_ttl"])
    except Exception:
        pass
    # 2) 环境变量覆盖
    api_daily_cap = os.getenv("API_DAILY_CAP")
    if api_daily_cap is not None:
        try:
            cfg["daily_cap"] = int(api_daily_cap.strip())
        except ValueError:
            pass

    api_hard_floor = os.getenv("API_HARD_FLOOR")
    if api_hard_floor is not None:
        try:
            cfg["hard_floor_remaining"] = int(api_hard_floor.strip())
        except ValueError:
            pass
    return cfg


class GuardResponse:
    """模拟 requests.Response，向后兼容现有调用方。"""

    def __init__(self, status_code: int, text: str, headers: Dict[str, str],
                 payload: Any = None, from_cache: bool = False):
        self.status_code = status_code
        self.text = text
        self.headers = headers
        self._payload = payload
        self.from_cache = from_cache

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text) if self.text else {}


class ApiBudgetGuard:
    """中央 API 预算守卫 (单例语义: 状态全在磁盘, 多进程安全靠 SQLite)。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.cfg = config or _load_config()
        self.daily_cap = int(self.cfg["daily_cap"])
        self.hard_floor = int(self.cfg["hard_floor_remaining"])
        self.cache_ttl = self.cfg["cache_ttl"]
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── 磁盘状态 ──
    def _init_db(self):
        self._conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS daily_usage (date TEXT PRIMARY KEY, count INTEGER)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS quota_state (k TEXT PRIMARY KEY, v TEXT)")
        self._conn.commit()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def daily_used(self) -> int:
        row = self._conn.execute(
            "SELECT count FROM daily_usage WHERE date=?", (self._today(),)).fetchone()
        return row[0] if row else 0

    def _record(self, n: int = 1):
        today = self._today()
        self._conn.execute(
            "INSERT INTO daily_usage (date, count) VALUES (?, ?) "
            "ON CONFLICT(date) DO UPDATE SET count=count+?",
            (today, n, n))
        self._conn.commit()

    def peek_remaining(self) -> Optional[int]:
        row = self._conn.execute(
            "SELECT v FROM quota_state WHERE k='last_remaining'").fetchone()
        if not row:
            return None
        try:
            return int(row[0])
        except (ValueError, TypeError):
            return None

    def _store_remaining(self, val: Optional[int]):
        if val is None:
            return
        self._conn.execute(
            "INSERT INTO quota_state (k, v) VALUES ('last_remaining', ?) "
            "ON CONFLICT(k) DO UPDATE SET v=?", (str(val), str(val)))
        self._conn.commit()

    # ── 预算判断 ──
    def can_spend(self, n: int = 1) -> bool:
        """今日是否还能再花 n 次 (配额 + 硬地板双重判断)。"""
        if self.daily_used() + n > self.daily_cap:
            return False
        rem = self.peek_remaining()
        if rem is not None and rem < self.hard_floor:
            return False
        return True

    def budget_status(self) -> Dict[str, Any]:
        rem = self.peek_remaining()
        return {
            "daily_used": self.daily_used(),
            "daily_cap": self.daily_cap,
            "daily_remaining": max(0, self.daily_cap - self.daily_used()),
            "month_estimate_remaining": rem,
            "hard_floor": self.hard_floor,
            "can_spend": self.can_spend(),
            "cache_ttl": self.cache_ttl,
            "today": self._today(),
        }

    # ── 缓存 ──
    def _cache_path(self, group: str, cache_key: str) -> Path:
        gdir = CACHE_ROOT / group
        gdir.mkdir(parents=True, exist_ok=True)
        return gdir / f"{cache_key}.json"

    def _cache_get(self, group: str, cache_key: str, ttl: int) -> Optional[Any]:
        p = self._cache_path(group, cache_key)
        if not p.exists():
            return None
        try:
            age = time.time() - p.stat().st_mtime
            if age > ttl:
                return None
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _cache_put(self, group: str, cache_key: str, payload: Any):
        try:
            with open(self._cache_path(group, cache_key), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception:
            pass

    @staticmethod
    def _make_key(url: str, params: Dict) -> str:
        norm = url
        if params:
            flat = "&".join(f"{k}={params[k]}" for k in sorted(params)
                             if k != "apiKey")
            norm = f"{url}?{flat}"
        return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]

    # ── 主入口 ──
    def guarded_get(self, url: str, params: Optional[Dict] = None,
                    cache_group: str = "odds", timeout: int = 15) -> GuardResponse:
        """带预算 + 缓存的 GET。返回 GuardResponse。

        - 缓存命中 → 直接返回 (不花配额)
        - 预算/地板不足 → 429 BUDGET_EXCEEDED (调用方按现有逻辑 no-op)
        - 正常 → requests.get, 记配额, 存 remaining, 写缓存
        """
        params = params or {}
        ttl = int(self.cache_ttl.get(cache_group, 3600))
        cache_key = self._make_key(url, params)

        # 1) 缓存
        cached = self._cache_get(cache_group, cache_key, ttl)
        if cached is not None:
            logger.debug(f"[Budget] 缓存命中 {cache_group} {url[-40:]}")
            return GuardResponse(200, json.dumps(cached), {}, payload=cached, from_cache=True)

        # 2) 预算
        if not self.can_spend(1):
            reason = ("daily_cap" if self.daily_used() >= self.daily_cap else "hard_floor")
            logger.warning(f"[Budget] 拒绝调用 ({reason}): {url[-50:]}")
            return GuardResponse(429, "BUDGET_EXCEEDED", {}, payload={})

        # 3) 真实调用
        if requests is None:
            return GuardResponse(503, "requests_unavailable", {}, payload={})
        try:
            resp = requests.get(url, params=params, timeout=timeout)
        except Exception as e:
            return GuardResponse(0, str(e), {}, payload={})

        # 记配额 + 存剩余
        self._record(1)
        rem = resp.headers.get("x-requests-remaining")
        if rem is not None:
            try:
                self._store_remaining(int(rem))
            except ValueError:
                pass

        if resp.status_code == 200:
            try:
                payload = resp.json()
            except Exception:
                payload = None
            if payload is not None:
                self._cache_put(cache_group, cache_key, payload)
            return GuardResponse(200, resp.text, dict(resp.headers), payload=payload)
        # 非 200 也透传 (调用方按 status 处理), 但不缓存
        return GuardResponse(resp.status_code, resp.text, dict(resp.headers), payload=None)

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


# 便捷单例 (同进程内复用, 但状态仍在磁盘)
_guard_instance: Optional[ApiBudgetGuard] = None


def get_guard() -> ApiBudgetGuard:
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = ApiBudgetGuard()
    return _guard_instance
