"""
tests/test_audit_bridge_auth.py — audit_bridge_auth 解析逻辑单测（零生产 I/O）
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import scripts.audit_bridge_auth as m  # noqa: E402

SAMPLE = '''
import bridge_service
from fastapi import FastAPI
app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/predict")
def predict(req):
    try:
        return {"success": True, "data": 1}
    except Exception:
        return {"success": True, "data": None}   # 吞没

@app.post("/api/focus")
def focus(req):
    try:
        return {"success": True, "count": 1}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.websocket("/ws/odds_ingest")
def ingest(ws):
    try:
        return {"success": True, "x": 1}
    except Exception:
        return {"success": True, "x": None}      # 吞没

@app.get("/api/predictions")
def predictions():
    return {}
'''


def test_parse_endpoints_basic():
    eps = m.parse_endpoints(SAMPLE)
    paths = {p for _, p, _, _ in eps}
    assert "/health" in paths
    assert "/predict" in paths
    assert "/api/focus" in paths
    assert "/ws/odds_ingest" in paths
    assert "/api/predictions" in paths


def test_classify_auth_prefix_not_api():
    # 非 /api 前缀写端点 → UNGUARDED，即使设 API_KEY 也不覆盖
    assert m.classify_auth("POST", "/predict").startswith("UNGUARDED")
    assert m.classify_auth("POST", "/predict/simple").startswith("UNGUARDED")


def test_classify_auth_api_write_conditional():
    assert m.classify_auth("POST", "/api/focus") == "CONDITIONAL_api_key_middleware"
    assert m.classify_auth("PUT", "/api/x").startswith("CONDITIONAL")


def test_classify_auth_websocket():
    assert m.classify_auth("WEBSOCKET", "/ws/odds_ingest") == "UNGUARDED_no_auth_mechanism"
    assert m.classify_auth("WEBSOCKET", "/ws/realtime") == "n/a_read_websocket"


def test_classify_auth_read():
    assert m.classify_auth("GET", "/api/predictions") == "n/a_read"
    assert m.classify_auth("GET", "/health") == "n/a_read"


def test_detect_swallow_dict_success_true():
    tree = m.ast.parse(SAMPLE)

    def find(name):
        for fn in m.ast.walk(tree):
            if isinstance(fn, m.ast.FunctionDef) and fn.name == name:
                return fn
        return None

    # /predict 的 except 返回 {"success": True, ...} → 吞没
    sw = m.detect_swallow(find("predict"), SAMPLE.splitlines())
    assert any(x["pattern"] == "except_returns_success_or_200" for x in sw)

    # /api/focus 的 except 返回 success:False → 不吞没
    sw2 = m.detect_swallow(find("focus"), SAMPLE.splitlines())
    assert sw2 == []

    # /ws/odds_ingest 吞没
    sw3 = m.detect_swallow(find("ingest"), SAMPLE.splitlines())
    assert any(x["pattern"] == "except_returns_success_or_200" for x in sw3)


def test_run_audit_smoke(tmp_path):
    p = tmp_path / "bridge_sample.py"
    p.write_text(SAMPLE, encoding="utf-8")
    # 创建假守卫文件引用不影响（PROJECT_ROOT 真实守卫存在与否均可）
    res = m.run_audit(str(p))
    assert res["summary"]["total_endpoints"] == 5
    assert res["summary"]["write_endpoints"] == 3  # predict, focus, ws_ingest
    un = [e["path"] for e in res["unauthenticated_candidates"]]
    assert "/predict" in un
    assert "/ws/odds_ingest" in un
    assert "/api/focus" not in un  # 被 CONDITIONAL 覆盖
    assert len(res["swallow_candidates"]) >= 2  # predict + ingest
