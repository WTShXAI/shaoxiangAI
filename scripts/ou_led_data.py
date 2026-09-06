"""
scripts.ou_led_data — OU 主导回测数据加载 (SSoT 数据源: events.db match_outcomes)

match_outcomes 是完美单源: 自带操盘手(乐鱼) OU 盘口(op_ou_line/over/under) +
1X2(op_1x2_h/d/a) + CS(op_cs JSON) + 真实比分(score_home/away). 609 场完整样本.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List

GQ_PATH = "data/events.db"


def load_matches(db_path: str = GQ_PATH) -> List[Dict[str, Any]]:
    """返回可用于 OU 主导四级回测的比赛列表."""
    db = sqlite3.connect(db_path)
    rows = db.execute("""
        SELECT home, away,
               op_1x2_h, op_1x2_d, op_1x2_a,
               op_ou_line, op_ou_over, op_ou_under,
               op_cs, score_home, score_away
        FROM match_outcomes
        WHERE op_ou_line IS NOT NULL AND op_ou_line > 0
          AND op_ou_over IS NOT NULL AND op_ou_under IS NOT NULL
          AND op_cs IS NOT NULL AND score_home IS NOT NULL AND score_away IS NOT NULL
    """).fetchall()
    out = []
    for r in rows:
        sh, sa = int(r[9]), int(r[10])
        out.append({
            "home": r[0], "away": r[1],
            "h": float(r[2]) if r[2] else None,
            "d": float(r[3]) if r[3] else None,
            "a": float(r[4]) if r[4] else None,
            "ou_line": float(r[5]),
            "ou_over": float(r[6]), "ou_under": float(r[7]),
            "op_cs": r[8],
            "score_home": sh, "score_away": sa,
            "total": sh + sa,
            "score": f"{sh}-{sa}",
        })
    db.close()
    return out


if __name__ == "__main__":
    m = load_matches()
    print(f"loaded {len(m)} matches")
    print("sample:", m[0])
