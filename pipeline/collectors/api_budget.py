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


# ── 自适应预算调度 ──

# 核心联赛 sport_key (12个, 与 config/api_budget.yaml adaptive.core_league_keys 同步)
CORE_LEAGUE_KEYS = frozenset([
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_efl_champ",
    "soccer_england_league1",
    "soccer_england_league2",
    "soccer_germany_bundesliga2",
    "soccer_france_ligue_two",
    "soccer_portugal_primeira_liga",
    "soccer_netherlands_eredivisie",
    "soccer_scotland_premiership",
])


class AdaptiveBudgetScheduler:
    """自适应 API 预算调度器

    核心逻辑:
      1. 核心联赛（5大+英冠英甲乙+德乙+法乙+葡超+荷甲+苏超=12个）缓存4小时
      2. 全量联赛每天凌晨拉一次 (1次/天)
      3. 开赛前24h加密: 任何比赛开赛前24h内，缓存降低到1小时
      4. 月末保护: 每月25号后检查剩余额度，若<100则全量降级为每12小时
      5. 熔断: 日消耗达250次时自动暂停，次日恢复

    接口:
      schedule_next_fetch(league_key, kickoff_time) → 返回建议的缓存TTL秒数
    """

    def __init__(self, guard: Optional[ApiBudgetGuard] = None):
        self._guard = guard or get_guard()
        cfg = _load_config()
        adp = cfg.get("adaptive", {})
        self.core_ttl = int(adp.get("core_league_ttl", 14400))
        self.prematch_ttl = int(adp.get("prematch_ttl", 3600))
        self.full_scan_hour = int(adp.get("full_scan_utc_hour", 3))
        self.eom_day = int(adp.get("eom_protection_day", 25))
        self.eom_remaining = int(adp.get("eom_remaining_threshold", 100))
        self.eom_ttl = int(adp.get("eom_degraded_ttl", 43200))
        self.cb_limit = int(adp.get("circuit_breaker_limit", 250))
        self.cb_ttl = int(adp.get("circuit_breaker_ttl", 86400))
        # 可用配置覆盖核心联赛列表
        yml_keys = adp.get("core_league_keys")
        if yml_keys and isinstance(yml_keys, list):
            self._core_keys = frozenset(yml_keys)
        else:
            self._core_keys = CORE_LEAGUE_KEYS

    @property
    def core_league_keys(self) -> frozenset:
        return self._core_keys

    def is_core_league(self, league_key: str) -> bool:
        return league_key in self._core_keys

    def schedule_next_fetch(self, league_key: str,
                            kickoff_time=None) -> int:
        """返回建议的缓存 TTL 秒数, 调用方以此决定下次何时刷新。

        Args:
            league_key: sport_key (e.g. "soccer_epl")
            kickoff_time: datetime 或 ISO 字符串, 可选

        Returns:
            int: 建议缓存 TTL 秒数
        """
        # 1) 熔断: 日消耗达上限 → 暂停
        daily_used = self._guard.daily_used()
        if daily_used >= self.cb_limit:
            logger.warning(
                f"[AdaptiveScheduler] 熔断触发: 日消耗 {daily_used}/{self.cb_limit}, "
                f"所有拉取暂停至次日")
            return self.cb_ttl

        # 2) 月末保护
        now_utc = datetime.now(timezone.utc)
        remaining = self._guard.peek_remaining()
        if (now_utc.day >= self.eom_day
                and remaining is not None
                and remaining < self.eom_remaining):
            logger.info(
                f"[AdaptiveScheduler] 月末保护: D{now_utc.day}, "
                f"剩余 {remaining} < {self.eom_remaining}, "
                f"全量降级为 {self.eom_ttl // 3600}h")
            return self.eom_ttl

        # 3) 开赛前24h内 → 1h (优先级高于核心联赛)
        if kickoff_time is not None:
            ko_dt = self._parse_kickoff(kickoff_time)
            if ko_dt is not None:
                hours_to_ko = (ko_dt - now_utc).total_seconds() / 3600
                if 0 < hours_to_ko <= 24:
                    return self.prematch_ttl

        # 4) 核心联赛 → 4h
        if league_key in self._core_keys:
            return self.core_ttl

        # 5) 非核心联赛: 每日定时全量扫 (默认 UTC 3:00)
        scan_hour = self.full_scan_hour
        next_scan = now_utc.replace(hour=scan_hour, minute=0, second=0, microsecond=0)
        if now_utc.hour >= scan_hour:
            from datetime import timedelta
            next_scan += timedelta(days=1)
        return max(3600, int((next_scan - now_utc).total_seconds()))

    def _parse_kickoff(self, ko):
        """解析开赛时间为 timezone-aware UTC datetime。"""
        if isinstance(ko, datetime):
            if ko.tzinfo is None:
                return ko.replace(tzinfo=timezone.utc)
            return ko.astimezone(timezone.utc)
        try:
            s = str(ko).replace("Z", "+00:00")
            if "T" in s:
                return datetime.fromisoformat(s).astimezone(timezone.utc)
            return datetime.strptime(
                s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def status(self) -> Dict[str, Any]:
        """返回调度器状态快照。"""
        used = self._guard.daily_used()
        rem = self._guard.peek_remaining()
        now = datetime.now(timezone.utc)
        eom_active = (
            now.day >= self.eom_day
            and rem is not None
            and rem < self.eom_remaining
        )
        return {
            "daily_used": used,
            "daily_cap": self._guard.daily_cap,
            "circuit_breaker_hit": used >= self.cb_limit,
            "circuit_breaker_limit": self.cb_limit,
            "remaining_quota": rem,
            "eom_protection_active": eom_active,
            "eom_day_threshold": self.eom_day,
            "eom_remaining_threshold": self.eom_remaining,
            "core_ttl_hours": self.core_ttl / 3600,
            "prematch_ttl_minutes": self.prematch_ttl / 60,
            "full_scan_hour_utc": self.full_scan_hour,
            "core_league_count": len(self._core_keys),
        }


# 便捷单例 (同进程内复用, 但状态仍在磁盘)
_guard_instance: Optional[ApiBudgetGuard] = None
_scheduler_instance: Optional[AdaptiveBudgetScheduler] = None


def get_guard() -> ApiBudgetGuard:
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = ApiBudgetGuard()
    return _guard_instance


def get_scheduler() -> AdaptiveBudgetScheduler:
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = AdaptiveBudgetScheduler()
    return _scheduler_instance
