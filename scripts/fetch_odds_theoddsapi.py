#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_odds_theoddsapi.py
从 The Odds API (the-odds-api.com) 拉取 FIFA World Cup 2026 实时盘 (1X2 / h2h),
保存为归一化 JSON, 供 ingest_odds_theoddsapi.py 解析入库.

注意:
  - 实时盘(live)只在"未赛/进行中"赛事有数据; 已完赛需 historical 接口(付费墙).
  - Key 通过环境变量 THEODDS_API_KEY 传入(不写死、不落日志).

用法:
  export THEODDS_API_KEY=xxxx
  python fetch_odds_theoddsapi.py --out data/oddsapi_wc_raw.json
"""
import os, sys, json, argparse, urllib.request, urllib.parse

BASE = "https://api.the-odds-api.com/v4"
SPORT = "soccer_fifa_world_cup"

def fetch(api_key, regions="eu,uk,us", markets="h2h"):
    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": "decimal",
    }
    url = f"{BASE}/sports/{SPORT}/odds?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="D:/Architecture/data/oddsapi_wc_raw.json")
    ap.add_argument("--regions", default="eu,uk,us")
    args = ap.parse_args()

    key = os.environ.get("THEODDS_API_KEY")
    if not key:
        print("ERROR: 请先设置环境变量 THEODDS_API_KEY", file=sys.stderr)
        sys.exit(1)

    data = fetch(key, regions=args.regions)
    if isinstance(data, dict) and "message" in data:
        print("API ERROR:", data, file=sys.stderr)
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"OK: 拉取 {len(data)} 场, 已存 {args.out}")

if __name__ == "__main__":
    main()
