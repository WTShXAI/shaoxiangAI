#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GQ 赔率接口并发压测 — 找出 1 秒级轮询不触发 HTTP 506 的安全并发上限。

只读探测: 仅调用 fetch_match_odds, 不写任何库, 不碰 events.db。

用法:
    python scripts/probe_gq_ratelimit.py                      # 默认测 1/2/3/4/6 并发, 每档 15 秒
    python scripts/probe_gq_ratelimit.py --levels 2,3 --seconds 20
"""
import sys
import os
import time
import argparse
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
GQ_DIR = os.path.join(ROOT, "gq")
if GQ_DIR not in sys.path:
    sys.path.insert(0, GQ_DIR)

from gq.auto_collector import fetch_match_odds  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import electronic_poll_daemon as epd  # noqa: E402


def _worker(mid, seconds, interval, stats, lock):
    """按 interval 秒对齐轮询 mid, 记录成功/失败。"""
    t_end = time.time() + seconds
    ok = fail = 0
    nxt = time.time()
    while time.time() < t_end:
        try:
            d = fetch_match_odds(mid)
            if d and (d.get("data") or d.get("playData")):
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
        nxt += interval
        slp = nxt - time.time()
        if slp > 0:
            time.sleep(slp)
        else:
            nxt = time.time()  # 落后则重新对齐, 不累积漂移
    with lock:
        stats["ok"] += ok
        stats["fail"] += fail


def probe(mids, level, seconds, interval):
    """用 level 个并发线程压测, 返回 (成功率, ok, fail, 实际tick/秒)。"""
    use = mids[:level]
    stats = {"ok": 0, "fail": 0}
    lock = threading.Lock()
    ths = [threading.Thread(target=_worker, args=(m, seconds, interval, stats, lock),
                            daemon=True) for m in use]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    dur = time.time() - t0
    total = stats["ok"] + stats["fail"]
    ideal = level * (seconds / interval)
    rate = stats["ok"] / ideal * 100 if ideal else 0.0
    return rate, stats["ok"], stats["fail"], total / dur if dur else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,2,3,4,6", help="要测的并发档位, 逗号分隔")
    ap.add_argument("--seconds", type=float, default=15.0, help="每档持续秒数")
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒")
    ap.add_argument("--league-filter", default="EAFC25", help="只用该联赛的比赛做压测")
    ap.add_argument("--cooldown", type=float, default=8.0, help="档位之间冷却秒数")
    args = ap.parse_args()

    vs = epd.discover_vs_matches()
    if args.league_filter:
        vs = [x for x in vs if args.league_filter in (x[1] or "")]
    mids = [m for m, _, _ in vs]
    if not mids:
        print("未发现可用于压测的比赛, 退出")
        return
    print(f"压测样本: {len(mids)} 场 (联赛过滤={args.league_filter!r})")
    print(f"每档 {args.seconds}s, 间隔 {args.interval}s, 档间冷却 {args.cooldown}s\n")
    print(f"{'并发':>4} | {'理想tick':>8} | {'成功':>5} | {'失败':>5} | {'成功率':>7} | {'实测QPS':>8} | 判定")
    print("-" * 72)

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    results = []
    for lv in levels:
        if lv > len(mids):
            print(f"{lv:>4} | (样本不足, 跳过)")
            continue
        rate, ok, fail, qps = probe(mids, lv, args.seconds, args.interval)
        ideal = int(lv * args.seconds / args.interval)
        verdict = "✅ 安全" if rate >= 95 else ("⚠️ 轻微丢失" if rate >= 85 else "❌ 限流")
        print(f"{lv:>4} | {ideal:>8} | {ok:>5} | {fail:>5} | {rate:>6.1f}% | {qps:>8.2f} | {verdict}")
        results.append((lv, rate))
        time.sleep(args.cooldown)

    safe = [lv for lv, r in results if r >= 95]
    print("\n" + "=" * 72)
    if safe:
        print(f"结论: 1 秒级轮询安全并发上限 = {max(safe)} 场 (成功率 ≥95%)")
    else:
        good = [lv for lv, r in results if r >= 85]
        if good:
            print(f"结论: 无完全无损档位; 可接受(≥85%)上限 = {max(good)} 场")
        else:
            print("结论: 所有档位均被限流, 需放宽轮询间隔 (试 --interval 2)")


if __name__ == "__main__":
    main()
