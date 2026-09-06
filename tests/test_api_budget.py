"""
tests/test_api_budget.py — API 预算护栏单元测试
================================================
覆盖: 缓存命中不重复计费 / 日额度封顶 / 硬地板拦截 / 真实剩余解析 /
      预算状态结构 / 密钥(apiKey)不进入缓存键

不触发真实网络: monkeypatch api_budget.requests 为本地假对象。
不污染磁盘: DB / 缓存根均指向 pytest tmp_path。
"""
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# 项目根加入 path, 确保 pipeline / scripts 包可导入
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pipeline.collectors.api_budget as api_budget


class _FakeResp:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class _FakeRequests:
    def __init__(self, remaining=10046):
        self.remaining = remaining
        self.calls = 0

    def get(self, url, params=None, timeout=15):
        self.calls += 1
        return _FakeResp(
            status_code=200,
            payload={"ok": True, "url": url},
            headers={"x-requests-remaining": str(self.remaining)},
            text='{"ok":true}',
        )


@pytest.fixture
def guard(tmp_path):
    db_path = tmp_path / "api_budget.db"
    cache_root = tmp_path / "cache"
    fake = _FakeRequests(remaining=9999)
    with mock.patch.object(api_budget, "DB_PATH", str(db_path)), \
         mock.patch.object(api_budget, "CACHE_ROOT", Path(str(cache_root))), \
         mock.patch.object(api_budget, "requests", fake):
        g = api_budget.ApiBudgetGuard(config={
            "daily_cap": 3,
            "hard_floor_remaining": 500,
            "cache_ttl": {"sports": 86400, "odds": 3600, "quota": 300},
        })
        yield g, fake
    try:
        g.close()
    except Exception:
        pass


def test_cache_hit_does_not_spend(guard):
    g, fake = guard
    url = "https://api.example.com/odds"
    params = {"sport": "soccer", "region": "eu"}
    r1 = g.guarded_get(url, params, cache_group="odds")
    assert r1.status_code == 200 and r1.from_cache is False
    assert fake.calls == 1
    assert g.daily_used() == 1
    # 再次相同请求 → 缓存命中, 不重复计费 / 不重复网络
    r2 = g.guarded_get(url, params, cache_group="odds")
    assert r2.from_cache is True
    assert fake.calls == 1
    assert g.daily_used() == 1


def test_daily_cap_enforcement(guard):
    g, fake = guard
    for i in range(3):
        g.guarded_get(f"https://api.example.com/o{i}", {"x": i}, cache_group="odds")
    assert g.daily_used() == 3
    assert g.can_spend(1) is False
    # 第 4 次 → 429 BUDGET_EXCEEDED
    r = g.guarded_get("https://api.example.com/over", {"x": 99}, cache_group="odds")
    assert r.status_code == 429
    assert r.text == "BUDGET_EXCEEDED"


def test_hard_floor_blocks(guard):
    g, fake = guard
    g._store_remaining(100)  # 真实剩余 < hard_floor(500)
    assert g.peek_remaining() == 100
    assert g.can_spend(1) is False
    r = g.guarded_get("https://api.example.com/floor", {}, cache_group="odds")
    assert r.status_code == 429


def test_real_remaining_parsed(guard):
    g, fake = guard
    g.guarded_get("https://api.example.com/rem", {"a": 1}, cache_group="quota")
    assert g.peek_remaining() == 9999


def test_budget_status_shape(guard):
    g, fake = guard
    s = g.budget_status()
    for k in ("daily_used", "daily_cap", "daily_remaining", "hard_floor",
              "can_spend", "cache_ttl", "today"):
        assert k in s
    assert s["daily_cap"] == 3
    assert isinstance(s["can_spend"], bool)


def test_cache_key_excludes_api_key(guard):
    g, fake = guard
    with_key = g._make_key("https://api.example.com/x", {"apiKey": "SECRET123", "p": 1})
    without = g._make_key("https://api.example.com/x", {"p": 1})
    # apiKey 不进入缓存键 → 密钥不会泄漏到缓存文件名 / 磁盘路径
    assert with_key == without


def test_cache_put_and_expiry(guard):
    g, fake = guard
    g._cache_put("odds", "k1", {"x": 1})
    assert g._cache_get("odds", "k1", ttl=3600) == {"x": 1}   # 未过期
    assert g._cache_get("odds", "k1", ttl=-1) is None          # 过期 (负 ttl 立即过期)


def test_store_remaining_none_noop(guard):
    g, fake = guard
    # None 不写入, peek 仍返回 None
    g._store_remaining(None)
    assert g.peek_remaining() is None


def test_get_guard_singleton(tmp_path):
    db_path = tmp_path / "api_budget.db"
    with mock.patch.object(api_budget, "DB_PATH", str(db_path)), \
         mock.patch.object(api_budget, "CACHE_ROOT", Path(str(tmp_path / "cache"))):
        a = api_budget.get_guard()
        b = api_budget.get_guard()
        assert a is b
        api_budget._guard_instance = None  # 清理模块单例, 避免跨测试泄漏

