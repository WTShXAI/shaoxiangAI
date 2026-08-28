"""
哨响AI — 模板偏差实时扫描 API 模块
==================================
供 bridge_service.py import 的轻量扫描器:
  - classify_tournament: 赛事类型 → 偏差评分
  - score_match_template: 单场综合评分
  - scan_live_matches: 从 events.db 取当前/今日比赛, 实时评分

不依赖重型 DC 模型, 毫秒级响应。
"""

from __future__ import annotations
import os
import json
import sqlite3
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
GQ_DB = os.path.join(DATA_DIR, "events.db")

# 赛事类型 → 模板偏差风险
TOURNAMENT_RISK = {
    "friendly": {"keywords": ["友谊", "friendly", "热身", "季前"], "risk": 0.85, "bias": "OVER_EST_GOALS"},
    "african_cup": {"keywords": ["卡加梅", "kagame", "cecafa", "非洲", "afcon", "caf"], "risk": 0.90, "bias": "OVER_EST_GOALS"},
    "low_tier_cup": {"keywords": ["杯", "cup", "足总杯", "地区杯"], "risk": 0.75, "bias": "OVER_EST_GOALS"},
    "early_cl": {"keywords": ["欧冠资", "欧联资", "欧协资", "欧冠预", "champions league q"], "risk": 0.70, "bias": "OVER_EST_GOALS"},
    "youth": {"keywords": ["u23", "u21", "u20", "u19", "u18", "青年", "预备", "新一代", "reserve"], "risk": 0.80, "bias": "OVER_EST_GOALS"},
    "low_tier_league": {"keywords": ["甲级", "乙级", "丙级", "丁级", "division", "championship", "league one", "serie b", "serie c"], "risk": 0.50, "bias": "OVER_EST_GOALS"},
    "major_league": {"keywords": ["英超", "premier league", "西甲", "la liga", "意甲", "serie a", "德甲", "bundesliga", "法甲", "ligue 1", "荷甲", "欧冠", "世界杯", "欧洲杯"], "risk": 0.15, "bias": "CALIBRATED"},
}


def classify_tournament(league: str) -> Dict:
    if not league or not isinstance(league, str):
        return {"category": "unknown", "risk": 0.60, "bias": "UNKNOWN", "reason": ""}
    name = league.lower()
    for cat, info in TOURNAMENT_RISK.items():
        for kw in info["keywords"]:
            if kw.lower() in name:
                return {"category": cat, "risk": info["risk"], "bias": info["bias"], "reason": ""}
    return {"category": "other", "risk": 0.40, "bias": "MILD_OVER_EST_GOALS", "reason": ""}


def score_match_template(league: str, op_ou_line=None, op_ou_over=None, op_ou_under=None,
                         op_cs=None) -> Dict:
    """单场综合评分 (L1赛事 + L3盘口)."""
    cat = classify_tournament(league)
    risk = cat["risk"]

    # L3: OU margin 异常
    ou_anomaly = 0.0
    ou_margin = None
    if all(v is not None for v in [op_ou_line, op_ou_over, op_ou_under]):
        try:
            ou_inv = 1.0 / float(op_ou_over) + 1.0 / float(op_ou_under)
            ou_margin = round(ou_inv - 1.0, 4)
            if ou_margin > 0.12 or ou_margin < 0.03:
                ou_anomaly = 0.3
        except (ValueError, ZeroDivisionError):
            pass

    composite = 0.5 * risk + 0.5 * ou_anomaly
    level = "HIGH" if composite > 0.6 else "MEDIUM" if composite > 0.3 else "LOW"

    return {
        "template_risk_score": round(composite, 4),
        "risk_level": level,
        "tournament_category": cat["category"],
        "tournament_risk": cat["risk"],
        "bias_direction": cat["bias"],
        "ou_margin": ou_margin,
        "ou_anomaly": ou_anomaly > 0,
    }


def scan_live_matches(limit: int = 50, risk_level: str = "ALL") -> Dict:
    """从 events.db 扫描当前比赛, 实时评分."""
    try:
        conn = sqlite3.connect(GQ_DB)
        # 优先今日, 其次最近
        rows = conn.execute(
            """SELECT m.match_key, m.home, m.away, m.league, m.kickoff, m.status,
                      mo.op_ou_line, mo.op_ou_over, mo.op_ou_under, mo.op_cs
               FROM matches m
               LEFT JOIN match_outcomes mo ON mo.mid = m.mid
               WHERE m.status NOT IN ('finished', 'completed')
               ORDER BY m.kickoff DESC LIMIT ?""",
            (limit * 3,),
        ).fetchall()
        conn.close()
    except Exception as e:
        return {"error": str(e), "matches": []}

    results = []
    for r in rows:
        (mk, home, away, league, kickoff, status, ou_line, ou_over, ou_under, op_cs) = r
        score = score_match_template(league or "", ou_line, ou_over, ou_under, op_cs)
        results.append({
            "match_key": mk,
            "home": home,
            "away": away,
            "league": league,
            "kickoff": kickoff,
            "status": status,
            **score,
        })

    # 过滤 risk_level
    if risk_level != "ALL":
        results = [r for r in results if r["risk_level"] == risk_level]

    results = results[:limit]

    return {
        "count": len(results),
        "risk_level_filter": risk_level,
        "matches": results,
        "summary": {
            "HIGH": sum(1 for r in results if r["risk_level"] == "HIGH"),
            "MEDIUM": sum(1 for r in results if r["risk_level"] == "MEDIUM"),
            "LOW": sum(1 for r in results if r["risk_level"] == "LOW"),
        },
    }


def scan_by_league(league_query: str, limit: int = 100) -> Dict:
    """按联赛名模糊查询并评分."""
    try:
        conn = sqlite3.connect(GQ_DB)
        rows = conn.execute(
            """SELECT mid, home, away, league, kickoff, score_home, score_away,
                      op_ou_line, op_ou_over, op_ou_under, op_cs, result
               FROM match_outcomes
               WHERE league LIKE ? AND score_home IS NOT NULL
               ORDER BY kickoff DESC LIMIT ?""",
            (f"%{league_query}%", limit),
        ).fetchall()
        conn.close()
    except Exception as e:
        return {"error": str(e), "matches": []}

    results = []
    for r in rows:
        (mid, home, away, league, kickoff, sh, sa, ou_line, ou_over, ou_under, op_cs, result) = r
        score = score_match_template(league or "", ou_line, ou_over, ou_under, op_cs)
        total = (sh or 0) + (sa or 0)
        results.append({
            "mid": mid, "home": home, "away": away, "league": league,
            "kickoff": kickoff, "actual_score": f"{sh}-{sa}" if sh is not None else None,
            "result": result, "total_goals": total, **score,
        })

    return {"count": len(results), "league_query": league_query, "matches": results}


if __name__ == "__main__":
    print("=== Live scan test ===")
    res = scan_live_matches(limit=10)
    print(json.dumps(res, ensure_ascii=False, indent=2)[:2000])

    print("\n=== League scan test ===")
    res2 = scan_by_league("卡加梅", limit=10)
    print(f"Kagame matches: {res2['count']}")
