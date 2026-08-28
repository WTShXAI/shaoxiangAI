# -*- coding: utf-8 -*-
"""
web_results_500.py — 500网「完场」页批量赛果抓取器 (纯 HTTP, 无反爬)

背景
====
GQ(乐鱼) 采集器系统性缺陷: 比赛结束后从盘口列表下架 → 采集器拿不到最新比分;
_sweep_finished() 在开赛 2.5h 后强行标 finished 且把 minute 伪造成 90,
比分被永久冻结在最后一次采到的中间值。
19 场人工联网抽样 → 15 场终场比分错误(79%), minute=90 无法作错误指纹。
乐鱼 getMatchBaseInfoByOddsPB 对老比赛返回空 data → 源头不可回补。

外部源选型实测:
  - 雷速 live.leisu.com/wanchang-YYYYMMDD : SSR 出队名但比分由 JS 填,
    detail/data 页有瑞数反爬(arg1), 列表走 socket 推送 → 放弃
  - TheSportsDB 免费版: 单日仅 3 场且无比分 → 覆盖太差, 放弃
  - 球探/7m: 路径 404 / 不可达 → 放弃
  - **500网 live.500.com/wanchang.php?e=YYYY-MM-DD : 纯 SSR, GBK, 1.4MB/天,
    877 场/天, 无反爬, 中文队名与 GQ 天然对齐, 覆盖 obscure 联赛 → 采用**

页面行结构 (tr id="a<mid500>" gy="联赛,主队,客队" lid=...)
  td[0] 联赛  td[1] 轮次  td[2] MM-DD HH:MM  td[3] 状态(完/取消/...)
  td[4] 主队(含排名[xx]/红牌)  td[5] 终场 "3 - 0"  td[6] 客队  td[7] 半场 "0 - 0"

用法
====
  python scripts/web_results_500.py --start 2026-07-15 --end 2026-08-03
  python scripts/web_results_500.py --date 2026-07-18 --out data/x.jsonl

输出 data/results_500.jsonl 每行:
  {date, mid500, league, round, time, status, home, away,
   ft_home, ft_away, ht_home, ht_away}
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "results_500.jsonl"

URL_TPL = "https://live.500.com/wanchang.php?e={date}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_ROW_RE = re.compile(r'<tr id="a(\d+)"([^>]*)>(.*?)</tr>', re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_GY_RE = re.compile(r'gy="([^"]*)"')
_SCORE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")


def _text(html: str) -> str:
    t = _TAG_RE.sub(" ", html)
    t = t.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", t).strip()


def _score(txt: str):
    m = _SCORE_RE.search(txt or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def fetch_day(date: str, retries: int = 3, timeout: int = 30) -> str:
    """抓单日完场页 HTML (GBK 解码)。"""
    url = URL_TPL.format(date=date)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://live.500.com/",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("gbk", errors="ignore")
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"fetch {date} failed: {last}")


def parse_day(html: str, date: str):
    """解析完场页 → list[dict]。"""
    out = []
    for mid500, attrs, body in _ROW_RE.findall(html):
        tds = [_text(x) for x in _TD_RE.findall(body)]
        if len(tds) < 8:
            continue
        gy = _GY_RE.search(attrs)
        if gy:
            parts = [p.strip() for p in gy.group(1).split(",")]
            league = parts[0] if parts else tds[0]
            home = parts[1] if len(parts) > 1 else ""
            away = parts[2] if len(parts) > 2 else ""
        else:
            league, home, away = tds[0], "", ""
        # gy 缺失时从 td 兜底剥离排名/红牌
        if not home:
            home = re.sub(r"\[\d+\]|\s\d+\s*$", " ", tds[4]).strip()
        if not away:
            away = re.sub(r"\[\d+\]|^\s*\d+\s", " ", tds[6]).strip()
        fh, fa = _score(tds[5])
        hh, ha = _score(tds[7])
        out.append({
            "date": date,
            "mid500": mid500,
            "league": league,
            "round": tds[1],
            "time": tds[2],
            "status": tds[3],
            "home": home,
            "away": away,
            "ft_home": fh, "ft_away": fa,
            "ht_home": hh, "ht_away": ha,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--sleep", type=float, default=1.2)
    args = ap.parse_args()

    if args.date:
        days = [args.date]
    elif args.start and args.end:
        d0 = datetime.date.fromisoformat(args.start)
        d1 = datetime.date.fromisoformat(args.end)
        days, d = [], d0
        while d <= d1:
            days.append(d.isoformat())
            d += datetime.timedelta(days=1)
    else:
        ap.error("需要 --date 或 --start/--end")

    total = withscore = 0
    with open(args.out, "a", encoding="utf-8") as f:
        for day in days:
            try:
                recs = parse_day(fetch_day(day), day)
            except Exception as e:
                print(f"[ERR] {day}: {e}", flush=True)
                continue
            ws = sum(1 for r in recs if r["ft_home"] is not None)
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            total += len(recs)
            withscore += ws
            print(f"[OK] {day}: rows={len(recs)} withScore={ws}", flush=True)
            time.sleep(args.sleep)
    print(f"TOTAL rows={total} withScore={withscore} -> {args.out}")


if __name__ == "__main__":
    main()
