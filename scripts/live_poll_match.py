#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_poll_match.py — 单场乐鱼(GQ)高频盘口监控器
================================================
1 秒级轮询 fetch_match_odds(mid)，把**全部** market 的赔率快照忠实落盘。

  · 定价模板 (pricing template) : 本场出现过的全部市场键
  · 定价赔率值 (pricing values) : 每一轮的全部赔率快照 (逐秒)
  · 定价规则 (pricing rule)     : 结束时由轨迹派生的首/末/极值/振幅摘要

落盘 (只写下面 3 个文件, 绝不碰 events.db):
  data/live_poll_<mid>.jsonl          逐行快照
  data/live_poll_<mid>.db             SQLite snapshots 表
  data/live_poll_<mid>_summary.json   结束时的模板/规则/极值摘要

用法:
  python scripts/live_poll_match.py <mid> [--interval 1] [--max-minutes 180]
                                          [--once] [--only-changes] [--max-print 12]

市场覆盖策略:
  标准盘沿用 gq/auto_collector.py::record_match_odds 的规范命名
  (1X2 / AH_-0.50 / OU_2.50 / CS / *_1H / *_2H)；
  其余 100+ 个玩法(角球/波胆变种/组合盘/15分钟盘…)用通用解析原样保留,
  market 名 = GQ 的 hpn 原文。**只解析, 绝不调用 record_match_odds(会写 events.db)。**
"""
import sys
import os
import time
import json
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta

# ── 复用采集器既有解析逻辑 (保证与 events.db 写入同口径) ──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gq.auto_collector import (  # noqa: E402
    fetch_match_odds,
    _score_from_msc, _status_minute,
    parse_ah_line, parse_ou_line, resolve_ah_line,
)

TZ8 = timezone(timedelta(hours=8))
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
EPS = 1e-6                      # 赔率浮点比较容差
FINISHED = ("finished", "closed")


def _now_iso():
    return datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S")


# ════════════════════ 解析层 ════════════════════
def _ov(opt):
    """GQ 赔率原始整数 -> 十进制赔率 (与 record_match_odds 一致: /100000)。"""
    try:
        return round(float(opt.get("ov", 0)) / 100000, 5)
    except Exception:
        return 0.0


def _put(markets, mk, sel, odds):
    if not mk or sel is None or sel == "":
        return
    markets.setdefault(mk, {})[str(sel)] = odds


def _parse_generic(markets, pd, hpn):
    """通用解析: 非标准玩法原样保留, market 名 = hpn。

    多档线 (len(hl)>1) 时 selection 前缀该档的 hon 标签, 防止互相覆盖。
    """
    hl = pd.get("hl") or []
    multi = len(hl) > 1
    for le in hl:
        hon = str(le.get("hon", "") or "")
        for j, opt in enumerate(le.get("ol") or []):
            # selection 名优先级: on(比分/盘口值) > ot(Over/Under/1/X/2) > 序号
            sel = opt.get("on") or opt.get("ot") or f"i{j}"
            _put(markets, hpn, f"{hon}|{sel}" if multi else sel, _ov(opt))


def parse_decoded(decoded, kickoff_ms=0):
    """复刻 record_match_odds 的解析分支 + 通用兜底，产出结构化快照。

    返回 dict: {home, away, league, status, minute, score, ht, markets}
    markets: {market_key: {selection: odds_float, ...}}
    """
    if not decoded:
        return None
    m_list = decoded.get("data") or []
    if not m_list:
        return None
    m = m_list[0]
    mhn = (m.get("mhn") or "").strip()          # 主队名
    man = (m.get("man") or "").strip()          # 客队名
    mlet = m.get("mlet") or ""                  # 已进行时间, "" = 未开赛
    if not mhn or not man:
        return None

    # kickoff 优先用详情自带的 mgt(毫秒), 免去额外拉一次全量列表
    kickoff_ms = kickoff_ms or m.get("mgt") or 0
    sh, sa, ht_sh, ht_sa = _score_from_msc(m.get("msc"))
    st, minute = _status_minute(mlet, float(kickoff_ms) if kickoff_ms else 0, time.time())

    markets = {}
    for pd in decoded.get("playData") or []:
        hpn = (pd.get("hpn") or "").strip()
        if not hpn:
            continue
        hl = pd.get("hl") or []

        # —— 全场独赢 1X2 ——
        if hpn in ("独赢", "全场独赢"):
            ol = (hl[0].get("ol") if hl else None) or []
            if len(ol) >= 3:
                _put(markets, "1X2", "home", _ov(ol[0]))
                _put(markets, "1X2", "draw", _ov(ol[1]))
                _put(markets, "1X2", "away", _ov(ol[2]))

        # —— 全场让球 AH (线可能在 hon, 也可能在 ol[].on; 2026-08-05 修) ——
        elif hpn == "全场让球":
            for le in hl:
                lv = resolve_ah_line(le)        # 挖不到返回 None (铁律1: 不填 0)
                ol = le.get("ol") or []
                if len(ol) < 2:
                    continue
                key = "AH_UNK" if lv is None else f"AH_{lv:.2f}"
                _put(markets, key, "home", _ov(ol[0]))
                _put(markets, key, "away", _ov(ol[1]))

        # —— 全场大小 OU (线在 ol[].on, 不在 hon!) ——
        elif hpn == "全场大小":
            for le in hl:
                ol = le.get("ol") or []
                if len(ol) < 2:
                    continue
                lv = parse_ou_line(ol[0].get("on", "") or le.get("hon", ""))
                if lv is None or lv <= 0 or lv > 10:
                    continue
                _put(markets, f"OU_{lv:.2f}", "over", _ov(ol[0]))
                _put(markets, f"OU_{lv:.2f}", "under", _ov(ol[1]))

        # —— 全场波胆 CS (保留"其他") ——
        elif hpn == "全场波胆":
            for le in hl:
                for opt in le.get("ol") or []:
                    _put(markets, "CS", opt.get("on", ""), _ov(opt))

        # —— 半场标准盘 ——
        elif hpn in ("下半场独赢", "下半场让球", "下半场大小",
                     "上半场独赢", "上半场让球", "上半场大小"):
            sfx = "_2H" if hpn.startswith("下半场") else "_1H"
            if "独赢" in hpn:
                ol = (hl[0].get("ol") if hl else None) or []
                if len(ol) >= 3:
                    _put(markets, f"1X2{sfx}", "home", _ov(ol[0]))
                    _put(markets, f"1X2{sfx}", "draw", _ov(ol[1]))
                    _put(markets, f"1X2{sfx}", "away", _ov(ol[2]))
            elif "让球" in hpn:
                for le in hl:
                    lv = resolve_ah_line(le)
                    ol = le.get("ol") or []
                    if len(ol) < 2:
                        continue
                    key = f"AH{sfx}_UNK" if lv is None else f"AH{sfx}_{lv:.2f}"
                    _put(markets, key, "home", _ov(ol[0]))
                    _put(markets, key, "away", _ov(ol[1]))
            else:  # 大小
                for le in hl:
                    ol = le.get("ol") or []
                    if len(ol) < 2:
                        continue
                    lv = parse_ou_line(ol[0].get("on", "") or le.get("hon", ""))
                    if lv is None or lv <= 0 or lv > 10:
                        continue
                    _put(markets, f"OU{sfx}_{lv:.2f}", "over", _ov(ol[0]))
                    _put(markets, f"OU{sfx}_{lv:.2f}", "under", _ov(ol[1]))

        # —— 其余全部玩法: 通用保留, 一个不丢 ——
        else:
            _parse_generic(markets, pd, hpn)

    return {
        "home": mhn, "away": man,
        "league": (m.get("tnjc") or m.get("tn") or "").strip(),
        "status": st, "minute": minute,
        "score": f"{sh}-{sa}" if sh is not None and sa is not None else "",
        "ht": f"{ht_sh}-{ht_sa}" if ht_sh is not None and ht_sa is not None else "",
        "markets": markets,
    }


# ════════════════════ 变化检测 ════════════════════
def _diff(prev, cur, max_items=12):
    """计算与上一轮的变化, 返回人类可读列表; 无变化返回 []。"""
    if prev is None:
        return [f"[init] 首次捕获 {len(cur.get('markets', {}))} 个市场"]
    out = []
    for k, label in (("status", "状态"), ("minute", "分钟"), ("score", "比分")):
        if prev.get(k) != cur.get(k):
            out.append(f"{label} {prev.get(k) or '-'}→{cur.get(k) or '-'}")
    pm, cm = prev.get("markets", {}), cur.get("markets", {})
    for mk, sels in cm.items():
        if mk not in pm:
            out.append(f"新市场 {mk} 出现({len(sels)}项)")
            continue
        old = pm[mk]
        for sel, ov in sels.items():
            pov = old.get(sel)
            if pov is None:
                out.append(f"{mk}.{sel} 新选项 {ov}")
            elif abs(pov - ov) > EPS:
                out.append(f"{mk}.{sel} {pov}{'↑' if ov > pov else '↓'}{ov}")
    for mk in pm:
        if mk not in cm:
            out.append(f"市场撤消 {mk}")
    if max_items and len(out) > max_items:      # 防刷屏
        out = out[:max_items] + [f"...(共 {len(out)} 处变化)"]
    return out


# ════════════════════ 存储层 ════════════════════
def _ensure_db(db_path):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_epoch REAL, ts_iso TEXT, status TEXT, minute INTEGER,
        score TEXT, ht TEXT, markets TEXT)""")
    # 迁移: 旧表缺列时 ALTER 补上, 不丢已有数据
    cols = {r[1] for r in con.execute("PRAGMA table_info(snapshots)")}
    if "ts_iso" not in cols:
        con.execute("ALTER TABLE snapshots ADD COLUMN ts_iso TEXT")
    if "ht" not in cols:
        con.execute("ALTER TABLE snapshots ADD COLUMN ht TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts_epoch)")
    con.commit()
    return con


def _summarize(path_jsonl, mid):
    """流式扫描 jsonl, 产出 模板/规则/极值 摘要 (不把全部行读进内存)。"""
    if not os.path.exists(path_jsonl):
        return None
    template, seen = [], set()
    agg = {}                    # "mk.sel" -> [first, last, min, max, ticks]
    first_row = last_row = None
    n = 0
    with open(path_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            n += 1
            if first_row is None:
                first_row = {k: r.get(k) for k in ("ts_iso", "home", "away")}
            last_row = {k: r.get(k) for k in ("ts_iso", "home", "away", "status", "score")}
            for mk, sels in (r.get("markets") or {}).items():
                if mk not in seen:
                    seen.add(mk)
                    template.append(mk)
                for sel, ov in sels.items():
                    key = f"{mk}.{sel}"
                    a = agg.get(key)
                    if a is None:
                        agg[key] = [ov, ov, ov, ov, 1]
                    else:
                        a[1] = ov
                        if ov < a[2]:
                            a[2] = ov
                        if ov > a[3]:
                            a[3] = ov
                        a[4] += 1
    if not n:
        return None

    extremes, rules = {}, []
    for key, (fv, lv, mn, mx, ticks) in agg.items():
        extremes[key] = {"first": fv, "last": lv, "min": mn, "max": mx, "ticks": ticks}
        direction = "不变" if abs(fv - lv) < EPS else ("下降" if lv < fv else "上升")
        rules.append(f"{key}: 首{fv}→末{lv} ({direction}), 区间[{mn}~{mx}] 振幅{round(mx - mn, 4)}")

    return {
        "mid": mid,
        "home": (last_row or {}).get("home"), "away": (last_row or {}).get("away"),
        "ticks": n,
        "time_span": {"first": (first_row or {}).get("ts_iso"),
                      "last": (last_row or {}).get("ts_iso")},
        "pricing_template": template,
        "pricing_values_extremes": extremes,
        "pricing_rules_derived": rules,
        "final_status": (last_row or {}).get("status"),
        "final_score": (last_row or {}).get("score"),
    }


# ════════════════════ 主循环 ════════════════════
def main():
    ap = argparse.ArgumentParser(description="单场 GQ 高频盘口监控 (只写 data/live_poll_*)")
    ap.add_argument("mid", type=str, help="GQ 比赛 mid")
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒 (默认1)")
    ap.add_argument("--max-minutes", type=float, default=180.0, help="最长运行分钟 (默认180)")
    ap.add_argument("--once", action="store_true", help="只采一轮即退出 (冒烟测试)")
    ap.add_argument("--only-changes", action="store_true",
                    help="仅在有变化时落盘 (省磁盘; 默认每轮都落)")
    ap.add_argument("--max-print", type=int, default=12, help="每轮最多打印几条变化 (防刷屏)")
    args = ap.parse_args()

    mid = str(args.mid).strip()
    os.makedirs(DATA_DIR, exist_ok=True)
    jsonl_path = os.path.join(DATA_DIR, f"live_poll_{mid}.jsonl")
    db_path = os.path.join(DATA_DIR, f"live_poll_{mid}.db")

    con = _ensure_db(db_path)
    fp = open(jsonl_path, "a", encoding="utf-8")    # 常开句柄, 免去每轮 open/close

    print(f"[{_now_iso()}] 开始监控 mid={mid} interval={args.interval}s max={args.max_minutes}min")
    print(f"  落盘: {jsonl_path}")
    print(f"        {db_path}")

    prev = None
    start = time.time()
    tick = written = fin_count = 0
    header_done = False

    try:
        while True:
            tick += 1
            try:
                decoded = fetch_match_odds(mid)
            except Exception as e:                  # 网络抖动不致命, 下一轮重试
                print(f"[{_now_iso()}] tick{tick} 抓取异常 {type(e).__name__}: {e}")
                decoded = None
            snap = parse_decoded(decoded) if decoded else None

            if snap is None:
                print(f"[{_now_iso()}] tick{tick} (无赔率)")
            else:
                if not header_done:
                    print(f"[match] {snap['home']} vs {snap['away']} | {snap['league']} "
                          f"| status={snap['status']} minute={snap['minute']} "
                          f"| markets={len(snap['markets'])}")
                    header_done = True

                ts = time.time()
                rec = {
                    "ts_epoch": int(ts),
                    "ts_iso": datetime.fromtimestamp(ts, TZ8).strftime("%Y-%m-%d %H:%M:%S"),
                    "mid": mid,
                    "home": snap["home"], "away": snap["away"], "league": snap["league"],
                    "status": snap["status"], "minute": snap["minute"],
                    "score": snap["score"], "ht": snap["ht"],
                    "markets": snap["markets"],
                }
                changes = _diff(prev, snap, args.max_print)

                if (not args.only_changes) or changes:
                    fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fp.flush()
                    con.execute(
                        "INSERT INTO snapshots (ts_epoch,ts_iso,status,minute,score,ht,markets) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (rec["ts_epoch"], rec["ts_iso"], snap["status"], snap["minute"],
                         snap["score"], snap["ht"],
                         json.dumps(snap["markets"], ensure_ascii=False)))
                    con.commit()
                    written += 1

                if changes:                          # 无变化时静默
                    print(f"[{rec['ts_iso']}] [{snap['status']} {snap['minute']}' "
                          f"{snap['score'] or '-'}] " + " | ".join(changes))
                prev = snap

            if args.once:
                break

            # —— 终止条件 ——
            if snap and snap["status"] in FINISHED:
                fin_count += 1
                if fin_count >= 3:                   # 连续 3 轮确认, 防单次误判
                    print(f"[{_now_iso()}] 比赛结束 (status={snap['status']}), 退出")
                    break
            else:
                fin_count = 0
            if (time.time() - start) / 60.0 >= args.max_minutes:
                print(f"[{_now_iso()}] 达 max-minutes={args.max_minutes}, 退出")
                break

            # 按绝对时间对齐 sleep, 避免抓取耗时累积漂移
            time.sleep(max(0.0, start + tick * args.interval - time.time()))

    except KeyboardInterrupt:
        print(f"\n[{_now_iso()}] 用户中断 (Ctrl+C)")
    finally:
        try:
            fp.close()
        finally:
            con.commit()
            con.close()
        print(f"[{_now_iso()}] 轮询 {tick} 轮, 落盘 {written} 条")

    # —— 结束摘要 ——
    summary = _summarize(jsonl_path, mid)
    if summary:
        sum_path = os.path.join(DATA_DIR, f"live_poll_{mid}_summary.json")
        with open(sum_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"[{_now_iso()}] 摘要已写: {sum_path}")
        print(f"  定价模板 {len(summary['pricing_template'])} 类 | 采样 {summary['ticks']} 轮 "
              f"| {summary['time_span']}")
    print(f"[{_now_iso()}] 监控结束.")


if __name__ == "__main__":
    main()
