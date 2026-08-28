# -*- coding: utf-8 -*-
"""
哨响AI · 优化对决 独立 harness 后端 (标准库 http.server, 端口 8000)
========================================================
零外部依赖(不需要 fastapi), 保证在 managed 环境可跑。
加载真实融合组件 (WI / live_1x2) + GitHub heuristic + 去水隐含基线:
  POST /api/predict  {odds, open_odds?, score?, minute?} -> 单场三方对比
  GET  /api/metrics  -> 对决准确率看板 (unified_corrected_duel_result.json)
  GET  /api/health
独立端口 8000, 不与 bridge_service(9000) 冲突。
"""
import os, sys, json, math
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import numpy as np
import joblib

ROOT = r"D:\Architecture"
GH   = r"C:\Users\ShXAI\Documents\GitHub\shaoxiangAI"
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(GH, "agents"))

from analysis.live_goal_probe import _dewater_1x2
from analysis.live_rollball_features import build_1x2_features
from pipeline.william_inter_model import derive_features as wi_derive
from heuristic_predictor import HeuristicPredictor

WI   = joblib.load(os.path.join(ROOT, "data", "wi_1x2_model.joblib"))
LIVE = joblib.load(os.path.join(ROOT, "data", "live_1x2_model.joblib"))
GH_P = HeuristicPredictor()

METRICS_PATH = os.path.join(ROOT, "deliverables", "unified_corrected_duel_result.json")
try:
    with open(METRICS_PATH, encoding="utf-8") as f:
        _metrics = json.load(f)
except Exception:
    _metrics = None

LABELS = ["H", "D", "A"]
ARG = {"H": "主胜", "D": "平局", "A": "客胜"}


def _sys_static(home, draw, away, oh, od, oa):
    feats = wi_derive(oh or home, od or draw, oa or away, home, draw, away)
    if feats is None or any(math.isnan(x) for x in feats):
        return None
    p = WI.predict_proba(np.array([feats], dtype=float))[0]
    return [float(x) for x in p]


def _sys_inplay(h, d, a, sh, sa, minute):
    x2 = _dewater_1x2(h, d, a)
    if x2 is None:
        return None
    feats = build_1x2_features(minute, sh, sa, x2[0], x2[1], x2[2])
    p = LIVE.predict_proba(np.array([feats], dtype=float))[0]
    return [float(x) for x in p]


def _github(h, d, a):
    p = GH_P.predict_proba(np.zeros((1, 1)), feature_names=[],
                           odds_data={"home": h, "draw": d, "away": a}, league_name="")
    return [float(p[0][0]), float(p[0][1]), float(p[0][2])]


def _baseline(h, d, a):
    x2 = _dewater_1x2(h, d, a)
    return [x2[0], x2[1], x2[2]] if x2 else [1/3, 1/3, 1/3]

# 优化混合权重 (来源: lock_w_holdout.py 时序 holdout 锁定)
#   滚球段: w=0.6 live_1x2 + 0.4 去水 -> AUC 0.8217 (最优, 严格优于两端, n=14465 时序窗口)
#   静态段: 混合无增量, 优化=去水基线 (WI教师 AUC 仅 +0.0013, 不值得部署)
W_INPLAY_LIVE = 0.6


def _pack(probs):
    if probs is None:
        return None
    i = int(np.argmax(probs))
    return {"p_home": round(probs[0], 4), "p_draw": round(probs[1], 4), "p_away": round(probs[2], 4),
            "argmax": LABELS[i], "argmax_cn": ARG[LABELS[i]]}


def do_predict(req: dict):
    home, draw, away = req.get("home"), req.get("draw"), req.get("away")
    if not (home and draw and away):
        return {"error": "odds required"}, 400
    sh = sa = 0
    score = req.get("score")
    if score and "-" in str(score):
        try:
            sh, sa = (int(x) for x in str(score).split("-"))
        except Exception:
            sh = sa = 0
    minute = req.get("minute")
    inplay = (minute is not None and int(minute) > 0)
    fH, fD, fA = float(home), float(draw), float(away)
    if inplay:
        sys_p = _sys_inplay(fH, fD, fA, sh, sa, int(minute))
        sys_arr = np.array(sys_p, dtype=float) if sys_p else None
        base_arr = np.array(_baseline(fH, fD, fA), dtype=float)
        # 优化混合: w*live + (1-w)*去水, 扫描最优 AUC=0.823 @ w=0.55
        opt = None
        if sys_arr is not None:
            opt = W_INPLAY_LIVE * sys_arr + (1 - W_INPLAY_LIVE) * base_arr
            opt = (opt / opt.sum()).tolist()
        mode = "inplay (live_1x2_model)"
    else:
        sys_p = _sys_static(fH, fD, fA,
                            req.get("open_home"), req.get("open_draw"), req.get("open_away"))
        # 静态段: 优化=去水基线 (WI教师无增量)
        opt = _baseline(fH, fD, fA)
        mode = "static (wi_1x2_model)"
    return {
        "mode": mode,
        "system": _pack(sys_p),
        "github": _pack(_github(fH, fD, fA)),
        "baseline": _pack(_baseline(fH, fD, fA)),
        "optimized": _pack(opt),
    }, 200


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send({}, 204)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._send({"status": "ok"})
        if path == "/api/metrics":
            if _metrics is None:
                return self._send({"ready": False, "note": "对决指标尚未生成, 请先运行 unified_corrected_duel.py"})
            return self._send({"ready": True, **_metrics})
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/predict":
            return self._send({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            return self._send({"error": str(e)}, 400)
        obj, code = do_predict(req)
        return self._send(obj, code)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("harness backend on http://127.0.0.1:8000  (Ctrl+C to stop)")
    HTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
