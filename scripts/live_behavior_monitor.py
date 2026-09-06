#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_behavior_monitor.py — 真实盘口(或电子盘) 定价行为信号 常驻监控器

用途 (B 方案·早盘增量验证机制):
  真实比赛直播期间, 1 秒级 tick 持续累积。本脚本把"已采到的 tick 序列"实时喂给
  行为校准桥, 输出当前这一刻的"庄家定价行为信号"与"行为校准后的置信度修正"。
  随着比赛推进(尤其下半场赔率开始动), 行为信号从 0 渐显 -> 直观展示
  "行为规则相对纯锚定能多榨出什么"。

零侵入: 复用 predictor_behavior_bridge.predict_file()。

用法:
  单次快照:
    python live_behavior_monitor.py data/live_poll_5487316.jsonl
  常驻轮询(每 20s 一行, 最多 60 行 / Ctrl-C 退出):
    python live_behavior_monitor.py data/live_poll_5487316.jsonl --watch 20 --max-iters 60
"""
import os
import sys
import time
import json
import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCR = os.path.join(ROOT, "scripts")
if SCR not in sys.path:
    sys.path.insert(0, SCR)

from predictor_behavior_bridge import predict_file


def _read_rows(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
    return rows


def _signal_text(out):
    """把行为特征翻译成一句话信号。"""
    if not out["behavior_used"]:
        return "无有效 tick 序列"
    b = out["behavior"]
    dl = out["behavior_delta"]
    maxk = max(("H", "D", "A"), key=lambda k: abs(dl[k]))
    mag = abs(dl[maxk]) * 100
    bits = []
    best = min(("H", "D", "A"), key=lambda k: b["drift_%s" % k.lower()])
    if b["drift_consistency"] >= 0.5 and b["drift_%s" % best.lower()] < 0:
        bits.append("drift看好%s(赔率下行)" % best)
    if b["n_events"] > 0:
        ra = b["reprice_asym"]
        hh = ra["H"]["dh"]; aa = ra["A"]["da"]
        if hh is not None:
            bits.append("主进主胜跳变%.2f" % hh)
        if aa is not None:
            bits.append("客进客胜跳变%.2f" % aa)
    if b["suspend_count"] > 0:
        bits.append("suspend×%d" % b["suspend_count"])
    if mag < 0.05:
        return "静态(中场/无漂移): 行为校准无贡献(零副作用) [" + (", ".join(bits) if bits else "无事件") + "]"
    return "信号: " + (", ".join(bits) if bits else "drift") + " -> 校准偏移 %s %+.2fpp" % (maxk, mag)


def monitor(jsonl_path, watch=0, interval=15, max_iters=40):
    print("监控目标: %s" % os.path.basename(jsonl_path))
    it = 0
    while True:
        rows = _read_rows(jsonl_path)
        n = len(rows)
        if n == 0:
            print("  [空文件]")
            break
        last = rows[-1]
        out = predict_file(jsonl_path)
        b = out["behavior"]
        minute = last.get("minute")
        score = last.get("score")
        status = last.get("status")
        ts = last.get("ts_iso") or ""
        ap = out["anchor_probabilities"]
        cp = out["behavior_calibrated_probabilities"]
        # 锚定预测方 vs 校准预测方
        apred = max(("H", "D", "A"), key=lambda k: ap[k])
        cpred = max(("H", "D", "A"), key=lambda k: cp[k])
        arrow = "" if apred == cpred else "  >>预测方变 %s!" % cpred
        margin = b.get("margin_1x2", float("nan")) if b else float("nan")
        print("[%s] %s vs %s | min=%s score=%s status=%s | ticks=%d"
              % (ts, last.get("home"), last.get("away"), minute, score, status, n))
        print("   锚定=%s(%.3f) 校准=%s(%.3f) Δ(H/D/A)=%+.3f/%+.3f/%+.3f%s"
              % (apred, ap[apred], cpred, cp[cpred],
                 out["behavior_delta"]["H"] * 100, out["behavior_delta"]["D"] * 100,
                 out["behavior_delta"]["A"] * 100, arrow))
        print("   " + _signal_text(out) + (" | margin=%.3f" % margin if b else ""))
        it += 1
        if watch <= 0 or it >= max_iters:
            break
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("  (用户中断)")
            break


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="真实盘口定价行为信号监控器")
    ap.add_argument("jsonl", nargs="?", help="赔率 tick jsonl")
    ap.add_argument("--watch", type=float, default=0, help="轮询间隔秒(>0 常驻)")
    ap.add_argument("--interval", type=float, default=15, help="轮询间隔秒(默认15)")
    ap.add_argument("--max-iters", type=int, default=40, help="最大轮询次数")
    args = ap.parse_args()

    if not args.jsonl:
        fs = sorted(glob.glob(os.path.join(ROOT, "data", "live_poll_*.jsonl")))
        if not fs:
            print("无 live_poll 文件, 请提供 jsonl")
            raise SystemExit(1)
        args.jsonl = fs[-1]
        print("[自动选最近的 live_poll: %s]" % os.path.basename(args.jsonl))
    monitor(args.jsonl, watch=args.watch, interval=args.interval, max_iters=args.max_iters)
