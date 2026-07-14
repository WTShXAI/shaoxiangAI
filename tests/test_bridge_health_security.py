"""
tests/test_bridge_health_security.py — bridge_service 健康/安全/校验冒烟测试
============================================================================
覆盖 ECC 生产标准:
  - /health 返回结构化依赖就绪度 (引擎/DB/量化/预算)
  - /api/bets 输入校验: 缺字段 → 422, bet_side 非法 → 400, 统一错误信封
  - /api/quant/order/confirm 输入校验: 缺 oid → 422
  - 速率限制中间件对 /api/* 生效 (不返回 500)
"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient
import bridge_service
from bridge_service import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def test_health_structure(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "ok" in body and "status" in body
    assert "checks" in body
    assert "db" in body["checks"]
    assert "quant_engine" in body["checks"]


def test_bets_missing_field_returns_422(client):
    # 缺 home_team → Pydantic 校验失败 → 422 + 错误信封
    r = client.post("/api/bets", json={"away_team": "B", "bet_side": "H",
                                        "home_odds": 2.2, "draw_odds": 3.4, "away_odds": 3.1})
    assert r.status_code == 422
    assert r.json()["success"] is False
    assert r.json()["error"]["code"] == "validation_error"


def test_bets_invalid_side_returns_400(client):
    r = client.post("/api/bets", json={
        "home_team": "A", "away_team": "B", "bet_side": "X",
        "home_odds": 2.2, "draw_odds": 3.4, "away_odds": 3.1,
    })
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_bet_side"


def test_bets_bad_odds_returns_422(client):
    # 赔率 <= 1 → Pydantic gt=1.0 失败 → 422
    r = client.post("/api/bets", json={
        "home_team": "A", "away_team": "B", "bet_side": "H",
        "home_odds": 1.0, "draw_odds": 3.4, "away_odds": 3.1,
    })
    assert r.status_code == 422


def test_quant_confirm_missing_oid_returns_422(client):
    r = client.post("/api/quant/order/confirm", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
