# -*- coding: utf-8 -*-
"""
leisu_web_results.py — 雷速「完场」网页赛果批量抓取器 (Playwright + 本机 Edge)

背景
====
GQ(乐鱼) 采集器存在系统性缺陷: 比赛结束后从盘口列表下架, 采集器再也拿不到
最新比分; _sweep_finished() 在开赛 2.5h 后强行把 status 标 finished 且
minute 伪造成 90, 比分被永久冻结在最后一次采到的中间值。
19 场人工联网抽样 → 15 场终场比分错误 (79%), 且 minute=90 无法作为错误指纹。
乐鱼侧 getMatchBaseInfoByOddsPB 对老比赛返回空 data → 源头不可回补。

因此改用外部权威中文源: 雷速 live.leisu.com/wanchang-YYYYMMDD (完场页)。
该页队名为简体中文, 与 GQ 天然对齐, 且覆盖 obscure 联赛(球会友谊/瓦尔哈拉杯等)。
页面 SSR 只出队名不出比分(比分由 JS 填充), detail/data 页有瑞数反爬,
故必须用真实浏览器渲染 → Playwright 驱动本机 Edge。

用法
====
  # 抓单日 (调试)
  python scripts/leisu_web_results.py --date 2026-07-18

  # 抓日期区间, 落库到 data/leisu_web_results.jsonl
  python scripts/leisu_web_results.py --start 2026-07-15 --end 2026-08-03

输出
====
  data/leisu_web_results.jsonl  每行一场:
    {date, league, status, home, away, ft_home, ft_away, ht_home, ht_away, raw_score, raw_half}
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "leisu_web_results.jsonl"

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

URL_TPL = "https://live.leisu.com/wanchang-{ymd}"

# 页面上每行的字段 class
ROW_SEL = "div.box_h.dd-item"
FIELD_SEL = {
    "league": ".lier-event-name",
    "status": ".lier-status",
    "home": ".lier-team-home",
    "score": ".lier-score",
    "away": ".lier-team-away",
    "half": ".lier-half",
    "corner": ".lier-corner",
}

_SCORE_RE = re.compile(r"(\d+)\s*[-:]\s*(\d+)")


def _parse_score(txt: str):
    """'2-1' / '2 - 1' / '(1-0)' → (2,1); 解析失败返回 (None,None)。"""
    if not txt:
        return None, None
    m = _SCORE_RE.search(txt)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _find_edge():
    for p in EDGE_PATHS:
        if Path(p).exists():
            return p
    return None


def scrape_day(page, ymd: str, wait_ms: int = 3500):
    """抓单日完场页, 返回 list[dict]。"""
    url = URL_TPL.format(ymd=ymd)
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    # 等 JS 把比分填进来: 轮询直到出现非 '-' 的比分, 或超时
    try:
        page.wait_for_function(
            """() => {
                const els = document.querySelectorAll('.lier-score');
                if (!els.length) return false;
                let filled = 0;
                els.forEach(e => { if (/\\d+\\s*[-:]\\s*\\d+/.test(e.innerText)) filled++; });
                return filled > els.length * 0.5;
            }""",
            timeout=20000,
        )
    except Exception:
        page.wait_for_timeout(wait_ms)  # 兜底: 固定等待

    rows = page.query_selector_all(ROW_SEL)
    out = []
    for r in rows:
        rec = {"date": ymd}
        for k, sel in FIELD_SEL.items():
            el = r.query_selector(sel)
            rec[k] = (el.inner_text().strip() if el else "").replace("\n", " ").strip()
        fh, fa = _parse_score(rec.get("score", ""))
        hh, ha = _parse_score(rec.get("half", ""))
        rec["ft_home"], rec["ft_away"] = fh, fa
        rec["ht_home"], rec["ht_away"] = hh, ha
        rec["raw_score"] = rec.pop("score", "")
        rec["raw_half"] = rec.pop("half", "")
        # 队名清洗: 去掉排名括号/红黄牌数字残留
        for side in ("home", "away"):
            rec[side] = re.sub(r"\s*\[\d+\]\s*", "", rec[side]).strip()
        if rec["home"] and rec["away"]:
            out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="单日 YYYY-MM-DD")
    ap.add_argument("--start", help="起始日 YYYY-MM-DD")
    ap.add_argument("--end", help="结束日 YYYY-MM-DD")
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--show", dest="headless", action="store_false")
    args = ap.parse_args()

    if args.date:
        days = [args.date]
    elif args.start and args.end:
        d0 = datetime.date.fromisoformat(args.start)
        d1 = datetime.date.fromisoformat(args.end)
        days = []
        d = d0
        while d <= d1:
            days.append(d.isoformat())
            d += datetime.timedelta(days=1)
    else:
        ap.error("需要 --date 或 --start/--end")

    from playwright.sync_api import sync_playwright

    edge = _find_edge()
    total = 0
    out_f = open(args.out, "a", encoding="utf-8")
    with sync_playwright() as pw:
        launch_kw = {"headless": args.headless}
        if edge:
            launch_kw["executable_path"] = edge
        browser = pw.chromium.launch(**launch_kw)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
            viewport={"width": 1600, "height": 1200},
            locale="zh-CN",
        )
        page = ctx.new_page()
        for day in days:
            ymd = day.replace("-", "")
            try:
                recs = scrape_day(page, ymd)
            except Exception as e:
                print(f"[ERR] {day}: {e}", flush=True)
                continue
            withscore = sum(1 for r in recs if r["ft_home"] is not None)
            for r in recs:
                out_f.write(json.dumps(r, ensure_ascii=False) + "\n")
            out_f.flush()
            total += len(recs)
            print(f"[OK] {day}: rows={len(recs)} withScore={withscore}", flush=True)
        browser.close()
    out_f.close()
    print(f"TOTAL rows={total} → {args.out}")


if __name__ == "__main__":
    main()
