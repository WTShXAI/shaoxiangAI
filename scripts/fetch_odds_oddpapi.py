#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[DEPRECATED] 此脚本用 oddpapi.io 域名, 与 OGDS=The Odds API 不符 (key 打此源会 INVALID_API_KEY)。
            改用 scripts/fetch_odds_theoddsapi.py; 免费档 historical 被墙, 这6场需付费档或手动贴 (见 scripts/g9_fill_wc6.py)。

fetch_odds_oddpapi.py  —  在你自己的【白名单服务器】上运行（NOT 沙盒）。

oddpapi.io 对沙盒出口 IP 做了 default-deny 白名单限制，所以由你这边
已加白名单的机器拉取，输出归一化 JSON，再贴给沙盒的 ingest_odds_oddpapi.py 入库。

用法：
    export ODDPAPi_API_KEY=xxxx          # 不要硬编码，走环境变量
    python fetch_odds_oddpapi.py --status 0 --out oddpapi_raw.json
    # 未来赛用 status=0；已完赛若 status=0 拉不到，改 status=1 再跑一次（--append 合并）
    python fetch_odds_oddpapi.py --status 1 --out oddpapi_raw.json --append

输出 oddpapi_raw.json 结构（沙盒解析器直接吃这个）：
{
  "source": "oddpapi",
  "count": N,
  "records": [
    {"fixtureId":123,"date":"2026-07-04","home":"Paraguay","away":"France",
     "bookmaker":"pinnacle","home_odds":5.0,"draw_odds":3.8,"away_odds":1.6},
    ...
  ]
}
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://api.oddspapi.io/v4"
KEY = os.environ.get("ODDPAPi_API_KEY")
if not KEY:
    sys.exit("ERROR: 请先 export ODDPAPi_API_KEY=你的key")


def get(path, params):
    params = dict(params)
    params["apiKey"] = KEY
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}/{path}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:400]}
    except Exception as e:  # noqa
        return {"_error": "conn", "_body": str(e)[:400]}


def pick_1x2(o):
    """从 oddpapi 的一个 odds 条目里尽量抽出 1X2 (胜平负) 三项赔率。"""
    mk = str(o.get("market", "")).lower()
    if mk and mk not in ("1x2", "1", "match_winner", "winner", "full_time_result", "ftr"):
        return None
    h = o.get("home")
    d = o.get("draw")
    aw = o.get("away")
    if h is None or d is None or aw is None:
        outs = o.get("outcomes") or o.get("odds")
        if isinstance(outs, list):
            for x in outs:
                nm = str(x.get("name", "")).lower()
                if nm in ("home", "1", "h"):
                    h = x.get("odds")
                elif nm in ("draw", "x", "0"):
                    d = x.get("odds")
                elif nm in ("away", "2", "a"):
                    aw = x.get("odds")
    if h is None or d is None or aw is None:
        return None
    try:
        return float(h), float(d), float(aw)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default="0")
    ap.add_argument("--sport", default="10")  # 10 = FIFA World Cup
    ap.add_argument("--bookmakers", default="pinnacle")
    ap.add_argument("--out", default="oddpapi_raw.json")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--from", dest="date_from", default="")
    ap.add_argument("--to", dest="date_to", default="")
    a = ap.parse_args()

    params = {"sportId": a.sport, "status": a.status, "limit": "300"}
    if a.date_from:
        params["dateFrom"] = a.date_from
    if a.date_to:
        params["dateTo"] = a.date_to
    fx = get("fixtures", params)
    if "_error" in fx:
        sys.exit(f"fixtures 拉取失败: {fx}")
    fixtures = fx.get("result") or fx.get("data") or []
    print(f"fixtures 取到 {len(fixtures)} 场", file=sys.stderr)

    records = []
    seen = set()
    for f in fixtures:
        fid = f.get("fixtureId") or f.get("id")
        home = (f.get("home") or {}).get("name") or f.get("homeTeam") or f.get("home_name")
        away = (f.get("away") or {}).get("name") or f.get("awayTeam") or f.get("away_name")
        start = f.get("startDate") or f.get("date") or f.get("kickoff") or ""
        date = str(start)[:10]
        od = get("odds", {"fixtureId": fid, "bookmakers": a.bookmakers})
        odds_list = od.get("result") or od.get("data") or []
        for o in odds_list:
            t = pick_1x2(o)
            if not t:
                continue
            h, d, aw = t
            bm = o.get("bookmaker") or o.get("bookmakerName") or "unknown"
            key = (fid, bm)
            if key in seen:
                continue
            seen.add(key)
            records.append({
                "fixtureId": fid, "date": date, "home": home, "away": away,
                "bookmaker": bm, "home_odds": h, "draw_odds": d, "away_odds": aw,
            })
        time.sleep(0.15)

    if a.append and os.path.exists(a.out):
        try:
            prev = json.load(open(a.out, encoding="utf-8"))
            existing = {(r["fixtureId"], r["bookmaker"]) for r in prev.get("records", [])}
            for r in records:
                if (r["fixtureId"], r["bookmaker"]) not in existing:
                    prev["records"].append(r)
            prev["count"] = len(prev["records"])
            records = prev["records"]
        except Exception:  # noqa
            pass

    out = {"source": "oddpapi", "count": len(records), "records": records}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n=> 已存 {len(records)} 条到 {a.out}（贴这个文件内容给沙盒即可）", file=sys.stderr)


if __name__ == "__main__":
    main()
