"""
tests/test_bridge_health_security.py — bridge_service 健康/安全/校验冒烟测试
============================================================================
覆盖:
  - /health 返回结构化依赖就绪度 (引擎/DB/预算)
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
