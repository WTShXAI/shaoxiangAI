"""
GQ 滚球数据采集 — 法国 VS 西班牙 实时赔率
============================================
从截图解析 + Playwright 拉取, 记录到 events.db.

支持的赔率市场:
  1X2      — 主胜/平/客胜
  AH       — 让球 (-1)
  CS       — 比分 1-0, 2-0, ..., 0-5 等
  O/U      — 总进球 0/1/2/3/4/5/6/7+
"""
from __future__ import annotations

import asyncio, sys, os, time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from gq.db import (
    init_db, upsert_match, record_snapshot, get_recent_changes, get_latest_odds, stats
)


# ── 截图赔率解析 (法国 vs 西班牙 @ 周二101 07-15 03:00) ──
SCREENSHOT_ODDS = {
    "1X2": {"home": 2.26, "draw": 3.10, "away": 2.75},
    "AH_-1": {
        "line": -1.0,
        "home": 5.05, "draw": 3.85, "away": 1.49,
    },
    "CS_HOME": {
        "1-0": 9.00, "2-0": 12.00, "2-1": 7.00, "3-0": 24.00,
        "3-1": 15.00, "3-2": 22.00, "4-0": 75.00, "4-1": 50.00,
        "4-2": 65.00, "5-0": 250.00, "5-1": 200.00, "5-2": 200.00,
        "home_other": 90.00,
    },
    "CS_DRAW": {
        "0-0": 11.50, "1-1": 6.00, "2-2": 11.00, "3-3": 40.00,
        "draw_other": 200.00,
    },
    "CS_AWAY": {
        "0-1": 10.00, "0-2": 15.00, "1-2": 7.50, "0-3": 35.00,
        "1-3": 21.00, "2-3": 25.00, "0-4": 100.00, "1-4": 75.00,
        "2-4": 90.00, "0-5": 350.00, "1-5": 300.00, "2-5": 350.00,
        "away_other": 100.00,
    },
    "OU": {
        "0": 11.50, "1": 5.00, "2": 3.50, "3": 3.50,
        "4": 5.20, "5": 9.50, "6": 17.00, "7+": 25.00,
    },
}


def record_screenshot():
    """把截图数据写入 events.db"""
    init_db()
    match_key = "法国 vs 西班牙"
    upsert_match(
        match_key=match_key,
        home="法国", away="西班牙",
        league="周二101",
        kickoff="2026-07-15 03:00",
        status="live",  # 滚球
    )
    print(f"[GQ] 写入比赛: {match_key}")

    changes = []
    # 1X2
    for sel, odds in SCREENSHOT_ODDS["1X2"].items():
        info = record_snapshot(match_key, "1X2", sel, odds, score_at="0-0")
        if info:
            changes.append(("1X2", sel, info))
    # AH
    ah = SCREENSHOT_ODDS["AH_-1"]
    for sel in ("home", "draw", "away"):
        info = record_snapshot(match_key, f"AH_{ah['line']}", sel, ah[sel], line=ah["line"], score_at="0-0")
        if info:
            changes.append((f"AH_{ah['line']}", sel, info))
    # 比分
    for cat in ("CS_HOME", "CS_DRAW", "CS_AWAY"):
        for sel, odds in SCREENSHOT_ODDS[cat].items():
            info = record_snapshot(match_key, "CS", f"{cat[3:].lower()}/{sel}", odds, score_at="0-0")
            if info:
                changes.append(("CS", f"{cat[3:].lower()}/{sel}", info))
    # 总进球
    for sel, odds in SCREENSHOT_ODDS["OU"].items():
        info = record_snapshot(match_key, "OU", sel, odds, score_at="0-0")
        if info:
            changes.append(("OU", sel, info))

    if changes:
        print(f"[GQ] 检测到 {len(changes)} 条赔率变化")
    else:
        print(f"[GQ] 首次写入, 无变化")
    print(f"[GQ] 统计: {stats()}")
    return match_key


if __name__ == "__main__":
    record_screenshot()
