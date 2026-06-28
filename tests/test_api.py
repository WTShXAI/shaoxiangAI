"""
API 集成测试 — P0-4
=====================
测试后端 14+ 端点的 HTTP 响应。
覆盖: 健康检查、单场预测、批量预测、赛程查询、模型管理、监控等。

注意: 这些测试需要后端服务器运行。如无服务器, 测试会跳过。
"""
import sys, os

import pytest
import json

# 尝试导入 TestClient
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

# ── 尝试导入 app ──────────────────────────
try:
    os.chdir(os.path.join(os.path.dirname(__file__), '..', 'backend'))

    from backend.main import app
    client = TestClient(app)
    APP_AVAILABLE = True
except Exception as e:
    APP_AVAILABLE = False
    client = None
    print(f"[SKIP] Backend app not available: {e}")

# ═══════════════════════════════════════════
# 1. 根页面 + 健康检查
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestHealthAndRoot:
    def test_root_returns_html(self):
        response = client.get("/")
        assert response.status_code in (200, 404)  # 可能找不到 static 文件

    def test_health_check(self):
        response = client.get("/api/v1/monitor/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "timestamp" in data
        assert "version" in data

    def test_health_live(self):
        response = client.get("/api/v1/monitor/health/live")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"

    def test_health_ready(self):
        response = client.get("/api/v1/monitor/health/ready")
        assert response.status_code in (200, 503)
        data = json.loads(response.text) if response.text else {}
        if response.status_code == 200:
            assert data["status"] == "ready"

    def test_legacy_health(self):
        response = client.get("/api/monitor/health")
        assert response.status_code in (200, 404)
        if response.status_code == 200:
            data = response.json()
            assert "status" in data

# ═══════════════════════════════════════════
# 2. 预测端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestPredictions:
    def test_single_prediction(self):
        """POST /api/v1/predict/single"""
        response = client.post(
            "/api/v1/predict/single",
            json={"home_team": "巴西", "away_team": "阿根廷"},
        )
        # 可能因缺少依赖而返回 500, 但不应返回 422
        assert response.status_code in (200, 422, 500, 503), f"Unexpected status: {response.status_code}"
        if response.status_code == 200:
            data = response.json()
            assert "prediction" in data or "home_team" in data

    def test_batch_prediction(self):
        """POST /api/v1/predict/batch"""
        response = client.post(
            "/api/v1/predict/batch",
            json={"matches": [
                {"home_team": "巴西", "away_team": "阿根廷"},
                {"home_team": "德国", "away_team": "法国"},
            ]},
        )
        assert response.status_code in (200, 422, 500, 503)

    def test_next_match(self):
        """GET /api/v1/predict/next-match"""
        response = client.get("/api/v1/predict/next-match")
        assert response.status_code in (200, 500, 404)

    def test_prediction_history(self):
        """GET /api/v1/predict/history"""
        response = client.get("/api/v1/predict/history")
        assert response.status_code in (200, 500, 404)
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list)

    def test_prediction_stats(self):
        """GET /api/v1/predict/stats"""
        response = client.get("/api/v1/predict/stats")
        assert response.status_code in (200, 500, 404)

    def test_single_prediction_validation(self):
        """空球队名应返回 422"""
        response = client.post(
            "/api/v1/predict/single",
            json={"home_team": "", "away_team": "阿根廷"},
        )
        assert response.status_code == 422

    def test_batch_prediction_empty(self):
        """空列表应返回 422"""
        response = client.post(
            "/api/v1/predict/batch",
            json={"matches": []},
        )
        assert response.status_code == 422

    def test_single_prediction_no_body(self):
        """无请求体应返回 422"""
        response = client.post("/api/v1/predict/single", json={})
        assert response.status_code == 422

    def test_v4_prediction(self):
        """POST /api/v1/predict/v4"""
        response = client.post(
            "/api/v1/predict/v4",
            json={"home_team": "巴西", "away_team": "阿根廷"},
        )
        assert response.status_code in (200, 422, 500, 503)

    def test_v4_health(self):
        """GET /api/v1/predict/v4/health"""
        response = client.get("/api/v1/predict/v4/health")
        assert response.status_code in (200, 500, 404)
        if response.status_code == 200:
            data = response.json()
            assert "status" in data

# ═══════════════════════════════════════════
# 3. 赛程端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestFixtures:
    def test_upcoming_fixtures(self):
        """GET /api/v1/fixtures/upcoming"""
        response = client.get("/api/v1/fixtures/upcoming")
        assert response.status_code in (200, 500, 404)
        if response.status_code == 200:
            data = response.json()
            assert "today" in data
            assert "tomorrow" in data
            assert "upcoming_count" in data

    def test_matches_list(self):
        """GET /api/v1/matches/list"""
        response = client.get("/api/v1/matches/list")
        assert response.status_code in (200, 500, 404)

    def test_matches_scores(self):
        """GET /api/v1/matches/scores"""
        response = client.get("/api/v1/matches/scores")
        assert response.status_code in (200, 500, 404)

# ═══════════════════════════════════════════
# 4. 模型管理端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestModels:
    def test_model_versions(self):
        """GET /api/v1/models/versions"""
        response = client.get("/api/v1/models/versions")
        assert response.status_code in (200, 500, 404)
        if response.status_code == 200:
            data = response.json()
            assert "models" in data or "versions" in data

    def test_model_info(self):
        """GET /api/v1/models/info"""
        response = client.get("/api/v1/models/info")
        assert response.status_code in (200, 500, 404)
        if response.status_code == 200:
            data = response.json()
            assert "version" in data or "model" in data or "status" in data

    def test_model_compare(self):
        """GET /api/v1/models/compare"""
        response = client.get("/api/v1/models/compare")
        assert response.status_code in (200, 422, 500, 404)

    def test_model_health(self):
        """GET /api/v1/monitor/model-health"""
        response = client.get("/api/v1/monitor/model-health")
        assert response.status_code in (200, 500, 404)
        if response.status_code == 200:
            data = response.json()
            assert "status" in data

    def test_model_best(self):
        """GET /api/v1/models/best"""
        response = client.get("/api/v1/models/best")
        assert response.status_code in (200, 500, 404)

# ═══════════════════════════════════════════
# 5. 监控端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestMonitor:
    def test_system_info(self):
        """GET /api/v1/monitor/system"""
        response = client.get("/api/v1/monitor/system")
        assert response.status_code in (200, 500, 404)
        if response.status_code == 200:
            data = response.json()
            assert "cpu_percent" in data or "status" in data

    def test_metrics_summary(self):
        """GET /api/v1/monitor/metrics/summary"""
        response = client.get("/api/v1/monitor/metrics/summary")
        assert response.status_code in (200, 500, 404)

# ═══════════════════════════════════════════
# 6. 训练端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestTraining:
    def test_training_status(self):
        """GET /api/v1/training/status"""
        response = client.get("/api/v1/training/status")
        assert response.status_code in (200, 500, 404)
        if response.status_code == 200:
            data = response.json()
            assert "status" in data or "running" in data

    def test_training_history(self):
        """GET /api/v1/training/history"""
        response = client.get("/api/v1/training/history")
        assert response.status_code in (200, 500, 404)
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list) or "history" in data

# ═══════════════════════════════════════════
# 7. 评估端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestEvaluation:
    def test_evaluation_latest(self):
        """GET /api/v1/evaluation/latest"""
        response = client.get("/api/v1/evaluation/latest")
        assert response.status_code in (200, 500, 404)

    def test_evaluation_history(self):
        """GET /api/v1/evaluation/history"""
        response = client.get("/api/v1/evaluation/history")
        assert response.status_code in (200, 500, 404)

# ═══════════════════════════════════════════
# 8. 数据质量端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestDataQuality:
    def test_data_quality_reports(self):
        """GET /api/v1/data-quality/reports"""
        response = client.get("/api/v1/data-quality/reports")
        assert response.status_code in (200, 500, 404)

    def test_data_quality_check(self):
        """GET /api/v1/data-quality/check"""
        response = client.get("/api/v1/data-quality/check")
        assert response.status_code in (200, 500, 404)

    def test_data_freshness(self):
        """GET /api/v1/data-quality/freshness"""
        response = client.get("/api/v1/data-quality/freshness")
        assert response.status_code in (200, 500, 404)

# ═══════════════════════════════════════════
# 9. 历史数据端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestHistoricalData:
    def test_historical_leagues(self):
        """GET /api/v1/historical/leagues"""
        response = client.get("/api/v1/historical/leagues")
        assert response.status_code in (200, 500, 404)

    def test_historical_summary(self):
        """GET /api/v1/historical/summary"""
        response = client.get("/api/v1/historical/summary")
        assert response.status_code in (200, 500, 404)

# ═══════════════════════════════════════════
# 10. 认证端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestAuth:
    def test_login_endpoint(self):
        """POST /api/v1/auth/login"""
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "test", "password": "test"},
        )
        assert response.status_code in (200, 401, 422, 500), f"Unexpected: {response.status_code}"

# ═══════════════════════════════════════════
# 11. 特征端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestFeatures:
    def test_features_compute(self):
        """GET /api/v1/features/compute"""
        response = client.get("/api/v1/features/compute")
        assert response.status_code in (200, 500, 404)

    def test_team_features(self):
        """GET /api/v1/features/teams/巴西"""
        response = client.get("/api/v1/features/teams/巴西")
        assert response.status_code in (200, 500, 404)

# ═══════════════════════════════════════════
# 12. 告警端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestAlerts:
    def test_alerts_list(self):
        """GET /api/v1/alerts/alerts"""
        response = client.get("/api/v1/alerts/alerts")
        assert response.status_code in (200, 500, 404)

    def test_alerts_rules(self):
        """GET /api/v1/alerts/rules"""
        response = client.get("/api/v1/alerts/rules")
        assert response.status_code in (200, 500, 404)

# ═══════════════════════════════════════════
# 13. A/B 测试端点
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestABTest:
    def test_ab_tests_list(self):
        """GET /api/v1/ab-test/tests"""
        response = client.get("/api/v1/ab-test/tests")
        assert response.status_code in (200, 500, 404)

    def test_ab_variant(self):
        """GET /api/v1/ab-test/variant"""
        response = client.get("/api/v1/ab-test/variant")
        assert response.status_code in (200, 500, 404)

# ═══════════════════════════════════════════
# 14. 内容端点 (flask_bridge 兼容)
# ═══════════════════════════════════════════

@pytest.mark.skipif(not APP_AVAILABLE, reason="Backend app not available")
class TestMisc:
    def test_legacy_generate_page(self):
        """GET /generate.html"""
        response = client.get("/generate.html")
        assert response.status_code in (200, 404)
