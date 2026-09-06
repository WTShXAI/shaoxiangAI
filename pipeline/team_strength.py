"""
团队实力增强模块 — GQ 98队(乐鱼) + WH 249队(国家队)
用于窄差盘口时补充团队历史战力信号
"""
import json
from pathlib import Path
from typing import Optional, Dict

_ROOT = Path(__file__).resolve().parent.parent / "data"

def _load():
    gq = json.loads((_ROOT / "gq_team_strength.json").read_text(encoding="utf-8"))
    wh = json.loads((_ROOT / "team_strength.json").read_text(encoding="utf-8"))
    return gq, wh

_GQ_STR, _WH_STR = _load()

def get_strength(home: str, away: str) -> Optional[Dict]:
    """返回两队历史战力差. 无数据返回 None."""
    h = _GQ_STR.get(home) or _WH_STR.get(home)
    a = _GQ_STR.get(away) or _WH_STR.get(away)
    if not (h and a):
        return None
    pts_diff = round(h[2] - a[2], 2)
    return {
        "home_pts": h[2], "away_pts": a[2],
        "pts_diff": pts_diff,
        "gf_diff": round(h[0] - a[0], 2),
        "signal": "H" if pts_diff > 0.5 else ("A" if pts_diff < -0.5 else "D"),
        "conf": min(1.0, abs(pts_diff) / 1.5),
        "home_n": h[3], "away_n": a[3],
    }
