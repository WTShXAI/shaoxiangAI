"""
哨响AI — SportsAPI WC2026 数据采集器
=======================================
API: https://v1.football.sportsapipro.com/api/v1/world-cup/*
限额: 100次/天
用法:
  python data_collector/sportsapi_wc2026.py          # 采集全部数据
  python data_collector/sportsapi_wc2026.py --results # 仅结果
  python data_collector/sportsapi_wc2026.py --today   # 今日赛程+预测
"""

import os, sys, json, time, argparse
from datetime import datetime, timezone
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "wc2026_api"
BASE = "https://v1.football.sportsapipro.com/api/v1/world-cup"
KEY = "f1351a28-318e-451c-9b88-602f9eb9b600"

HEADERS = {"x-api-key": KEY, "Accept": "application/json"}

def _fetch(endpoint: str) -> dict:
    url = f"{BASE}/{endpoint.lstrip('/')}"
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url, headers=HEADERS)
        resp.raise_for_status()
        return resp.json()

def fetch_results() -> list:
    """获取所有已完成比赛"""
    data = _fetch("results")
    return data.get("data", {}).get("games", [])

def fetch_matches() -> list:
    """获取全部比赛 (含未开始)"""
    data = _fetch("matches")
    return data.get("data", {}).get("games", [])

def fetch_standings() -> list:
    """获取小组积分榜"""
    data = _fetch("standings")
    return data.get("data", {}).get("standings", [])

def fetch_odds() -> list:
    """获取粉丝预测投票"""
    data = _fetch("odds")
    return data.get("data", {}).get("games", [])

def fetch_stats() -> dict:
    """获取射手榜/助攻榜"""
    return _fetch("stats").get("data", {})

def fetch_game_detail(game_id: int) -> dict:
    """获取单场比赛详情 (场馆/阵容/事件)"""
    return _fetch(f"game/{game_id}").get("data", {})

def save_json(data, filename: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  💾 {filename} ({len(json.dumps(data)):,} bytes)")
    return path

def run_full():
    """全量采集"""
    t0 = time.time()
    print(f"🌍 SportsAPI WC2026 全量采集")
    print(f"  时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")

    results = fetch_results()
    print(f"  ✅ 结果: {len(results)} 场已完成")
    save_json(results, "results.json")

    matches = fetch_matches()
    print(f"  ✅ 赛程: {len(matches)} 场 (含未开始)")
    save_json(matches, "matches.json")

    standings = fetch_standings()
    print(f"  ✅ 积分榜: {len(standings)} 组")
    save_json(standings, "standings.json")

    try:
        odds = fetch_odds()
        print(f"  ✅ 预测: {len(odds)} 场有投票")
        save_json(odds, "odds.json")
    except Exception as e:
        print(f"  ⚠️ 预测获取失败: {e}")

    try:
        stats = fetch_stats()
        print(f"  ✅ 统计: {len(stats)} 个类别")
        save_json(stats, "stats.json")
    except Exception as e:
        print(f"  ⚠️ 统计获取失败: {e}")

    elapsed = time.time() - t0
    print(f"\n  完成 | {elapsed:.1f}s | 数据: {DATA_DIR}")

def run_today():
    """今日赛程 + 预测"""
    matches = fetch_matches()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"📅 今日赛程 ({today})")
    upcoming = [m for m in matches if m.get("startTime", "").startswith(today)]
    if not upcoming:
        print("  今日无比赛")
        return

    for m in upcoming:
        h = m.get("homeCompetitor", {})
        a = m.get("awayCompetitor", {})
        print(f"  {m['startTime'][11:16]} {h.get('name','?'):>20s} vs {a.get('name','?')}")

    # 尝试获取预测
    try:
        odds = fetch_odds()
        for m in upcoming:
            for o in odds:
                if o.get("homeTeam") == m["homeCompetitor"]["name"]:
                    preds = o.get("predictions", {}).get("predictions", [])
                    for p in preds:
                        if "Who Will Win" in p.get("title", ""):
                            opts = {x["name"]: x["vote"]["percentage"] for x in p["options"]}
                            print(f"    🗳️ {opts}")
    except Exception as e:
        print(f"[WARN] 获取赔率预测失败: {e}")

def run_results():
    """仅获取赛果 + 打印"""
    results = fetch_results()
    print(f"🏆 WC2026 赛果 ({len(results)}场)")
    for g in sorted(results, key=lambda x: x.get("startTime", "")):
        h = g.get("homeCompetitor", {})
        a = g.get("awayCompetitor", {})
        t = g["startTime"][:10]
        print(f"  {t} {h.get('name','?'):>20s} {h.get('score','?')}-{a.get('score','?')} {a.get('name','?')}")
    save_json(results, "results.json")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SportsAPI WC2026 采集器")
    parser.add_argument("--results", action="store_true", help="仅赛果")
    parser.add_argument("--today", action="store_true", help="今日赛程+预测")
    parser.add_argument("--full", action="store_true", help="全量采集")
    args = parser.parse_args()

    if args.results:
        run_results()
    elif args.today:
        run_today()
    elif args.full:
        run_full()
    else:
        # 默认: 全量
        run_full()
