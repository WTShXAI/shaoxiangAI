#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
electronic_poll_daemon.py — 电子盘(VS-)定价模板 自动采集器 (隔离版)
================================================================
自动发现乐鱼(GQ) 的 VS- 模拟联赛比赛, 对每场做 1 秒级高频监控
(从初盘到终场), 把**全部**盘口赔率快照落盘到隔离区, 并跨场沉淀
"定价模板库" (每联赛的市场结构 + 庄家 margin/overround 模板)。

铁律防火墙 (与训练标签物理隔开):
  · 只调用 fetch_match_odds (只读), 绝不调用 record_match_odds
  · 只写 data/electronic_poll_* 文件, 绝不碰 events.db / match_outcomes / 特征库
  · VS- 比赛本会进训练标签中毒, 故本守护进程产出的数据仅供"定价模板研究"

落盘:
  data/electronic_poll_<mid>.jsonl              逐场逐秒快照
  data/electronic_poll_<mid>.db                SQLite snapshots 表
  data/electronic_poll_<mid>_summary.json      单场模板/规则/极值摘要
  data/electronic_poll_library.json            跨场定价模板库 (本守护进程核心产出)

用法:
  python scripts/electronic_poll_daemon.py [--discover-interval 30]
                                           [--poll-interval 1]
                                           [--max-match-minutes 120]
                                           [--max-runtime 0]   (0=无限)
"""

import sys
import os
import re
import time
import json
import sqlite3
import argparse
import threading
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from gq.auto_collector import (  # noqa: E402
    fetch_match_odds, _api_post, _decode, LIST_PATH, CUID,
)
from live_poll_match import (  # noqa: E402  (只读解析, 不触发其 main)
    parse_decoded, _diff, _ensure_db, FINISHED, TZ8,
)

DATA_DIR = os.path.join(ROOT, "data")
ELEC_PREFIX = "electronic_poll_"
LIB_PATH = os.path.join(DATA_DIR, f"{ELEC_PREFIX}library.json")
DISCOVER_LOCK = threading.Lock()
LIB_LOCK = threading.Lock()


def _now_iso():
    return datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════ 发现层 ══════════════════
# 电子盘/虚拟盘识别规则
#   1) "VS- xxx PANDA独家EAFC25"  → PANDA 电竞模拟盘
#   2) "瓦尔哈拉杯 2026 (8分钟)"  → (N分钟) 短周期模拟盘
#   3) "梦幻对垒"                → 虚构对阵盘
# 注意: gq.auto_collector.fetch_match_list() 会在源头调 _is_simulated_league()
#       剔除 VS- 前缀联赛, 因此本模块必须直连原始 list API 自行解析, 否则
#       永远发现不到 PANDA 电子盘 (2026-08-04 实证: 上游剔 35 场)。
_MINUTE_PAT = re.compile(r"\(\s*\d+\s*分钟\s*\)")
_VIRTUAL_NAMES = ("梦幻对垒", "瓦尔哈拉", "瓦尔基里")


def is_electronic_league(tn: str) -> bool:
    """判定联赛名是否属于电子盘/虚拟盘。"""
    tn = (tn or "").strip()
    if not tn:
        return False
    if tn.startswith("VS-"):
        return True
    if _MINUTE_PAT.search(tn):
        return True
    return any(k in tn for k in _VIRTUAL_NAMES)


def fetch_raw_list():
    """直连 GQ 列表 API, **不做任何联赛过滤**, 返回 [(mid, tn, mgt), ...]。

    刻意不复用 auto_collector.fetch_match_list —— 那个函数为保护训练标签
    会剔除 VS- 联赛, 而本守护进程要的正是这些。只读, 不写任何库。
    """
    body = {"cuid": CUID, "sort": 1, "tid": "", "apiType": 1,
            "orpt": 0, "euid": "3020101"}
    js = _api_post(LIST_PATH, body)
    if not js or js.get("code") != "0000000":
        return []
    data = _decode(js.get("data", ""))
    if not data:
        return []
    out = []
    for grp in ("livedata", "nolivedata"):
        for it in data.get(grp, []):
            tn = it.get("tn", "") or ""
            mgt = it.get("mgt", 0)
            for mid in str(it.get("mids", "")).split(","):
                mid = mid.strip()
                if mid:
                    out.append((mid, tn, mgt))
    return out


def discover_vs_matches():
    """返回当前 GQ 列表里所有电子盘/虚拟盘比赛: [(mid, tn, mgt), ...]。"""
    out = []
    try:
        for mid, tn, mgt in fetch_raw_list():
            if is_electronic_league(tn):
                out.append((mid, tn, mgt))
    except Exception as e:
        print(f"[{_now_iso()}] 发现层异常 {type(e).__name__}: {e}")
    return out


def _kickoff_age(mgt):
    """返回 now - kickoff 的秒数。负数 = 尚未开赛; 无法解析返回 None。"""
    try:
        return time.time() - float(mgt) / 1000.0
    except Exception:
        return None


def rank_by_freshness(vs, tol_started=120.0, lead_max=600.0):
    """按"能否抓到初盘"给候选排序 + 过滤。

    单并发下必须把唯一的名额留给能从初盘抓起的场, 否则会占位在一场
    已经打到一半的比赛上, 轨迹缺头 (2026-08-04 实证: 8并发被限流后改
    单并发, 若不排序会随机抢到中途场)。

    分组优先级:
      0  即将开赛 (0 < 剩余 <= lead_max)      —— 最理想, 能完整抓初盘→终场
      1  刚开赛   (0 <= 已开赛 <= tol_started) —— 可接受, 仅缺开头几十秒
      2  远期未开 (剩余 > lead_max)            —— 等太久, 占位浪费
      3  中途场   (已开赛 > tol_started)       —— 缺初盘, 兜底才用
    组内: 0/2 按最快开赛优先; 1/3 按开赛最晚(最年轻)优先。
    """
    ranked = []
    for mid, tn, mgt in vs:
        age = _kickoff_age(mgt)
        if age is None:
            ranked.append((3, 0.0, mid, tn, mgt))
            continue
        if age < 0:                       # 尚未开赛, -age = 距开赛秒数
            lead = -age
            grp = 0 if lead <= lead_max else 2
            ranked.append((grp, lead, mid, tn, mgt))
        elif age <= tol_started:          # 刚开赛不久
            ranked.append((1, age, mid, tn, mgt))
        else:                             # 已进行较久, 抓不到初盘
            ranked.append((3, age, mid, tn, mgt))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [(r[2], r[3], r[4], r[0]) for r in ranked]


# ══════════════════ 定价模板量化 ══════════════════
def _overround(odds_list):
    """overround = Σ(1/odds) - 1 ; >0 即庄家 embedded margin。"""
    inv = [1.0 / o for o in odds_list if o and o > 0]
    return round(sum(inv) - 1.0, 4) if inv else None


def _opening_overrounds(mk):
    """从单场快照的 markets 算各市场类型的 embedded margin 模板。"""
    res = {}
    x2 = mk.get("1X2")
    if x2 and len(x2) >= 3:
        res["1X2"] = _overround([x2.get("home"), x2.get("draw"), x2.get("away")])
    for k, v in mk.items():
        if k.startswith("OU_") and "_1H" not in k and "_2H" not in k:
            if "over" in v and "under" in v:
                res.setdefault("OU", []).append(_overround([v["over"], v["under"]]))
        elif k.startswith("AH_") and "_1H" not in k and "_2H" not in k:
            if "home" in v and "away" in v:
                res.setdefault("AH", []).append(_overround([v["home"], v["away"]]))
    if "OU" in res:
        res["OU"] = round(sum(res["OU"]) / len(res["OU"]), 4)
    if "AH" in res:
        res["AH"] = round(sum(res["AH"]) / len(res["AH"]), 4)
    return res


def _market_template(mk):
    """市场结构指纹 = 排序后的市场键元组 (定价模板的"形状")。"""
    return tuple(sorted(mk.keys()))


# ══════════════════ 单场监控线程 ══════════════════
def monitor_match(mid, tn, poll_interval, max_match_minutes, registry):
    """单场 1 秒级监控直到终场; 落盘隔离文件 + 写单场摘要 + 更新模板库。"""
    jsonl_path = os.path.join(DATA_DIR, f"{ELEC_PREFIX}{mid}.jsonl")
    db_path = os.path.join(DATA_DIR, f"{ELEC_PREFIX}{mid}.db")
    sum_path = os.path.join(DATA_DIR, f"{ELEC_PREFIX}{mid}_summary.json")

    con = _ensure_db(db_path)
    fp = open(jsonl_path, "a", encoding="utf-8")
    prev = None
    start = time.time()
    tick = written = fin_count = 0
    opening_mk = None
    print(f"[{_now_iso()}] [VS] 启动监控 {mid} ({tn}) interval={poll_interval}s")
    try:
        while True:
            tick += 1
            try:
                decoded = fetch_match_odds(mid)
            except Exception as e:
                decoded = None
                if tick % 30 == 0:
                    print(f"[{_now_iso()}] [VS:{mid}] 抓取异常 {type(e).__name__}")
            snap = parse_decoded(decoded) if decoded else None
            if snap:
                if opening_mk is None:
                    opening_mk = snap["markets"]
                    print(f"[{_now_iso()}] [VS:{mid}] 初盘捕获 markets={len(opening_mk)}")
                ts = time.time()
                rec = {
                    "ts_epoch": int(ts),
                    "ts_iso": datetime.fromtimestamp(ts, TZ8).strftime("%Y-%m-%d %H:%M:%S"),
                    "mid": mid, "home": snap["home"], "away": snap["away"],
                    "league": snap["league"], "status": snap["status"],
                    "minute": snap["minute"], "score": snap["score"], "ht": snap["ht"],
                    "markets": snap["markets"],
                }
                changes = _diff(prev, snap)
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fp.flush()
                con.execute(
                    "INSERT INTO snapshots (ts_epoch,ts_iso,status,minute,score,ht,markets) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (rec["ts_epoch"], rec["ts_iso"], snap["status"], snap["minute"],
                     snap["score"], snap["ht"], json.dumps(snap["markets"], ensure_ascii=False)))
                con.commit()
                written += 1
                if changes and tick % 1 == 0:
                    print(f"[{rec['ts_iso']}] [VS:{mid}] [{snap['status']} {snap['minute']}' "
                          f"{snap['score'] or '-'}] " + " | ".join(changes[:8]))
                prev = snap

            if snap and snap["status"] in FINISHED:
                fin_count += 1
                if fin_count >= 3:
                    print(f"[{_now_iso()}] [VS:{mid}] 终场, 退出监控")
                    break
            else:
                fin_count = 0
            if (time.time() - start) / 60.0 >= max_match_minutes:
                print(f"[{_now_iso()}] [VS:{mid}] 达 max-match-minutes, 退出")
                break
            time.sleep(max(0.0, start + tick * poll_interval - time.time()))
    except Exception as e:
        print(f"[{_now_iso()}] [VS:{mid}] 线程异常 {type(e).__name__}: {e}")
    finally:
        fp.close()
        con.close()

    # —— 单场摘要 ——
    summary = _summarize_match(jsonl_path, mid, tn, opening_mk)
    if summary:
        with open(sum_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"[{_now_iso()}] [VS:{mid}] 单场摘要已写: {os.path.basename(sum_path)}")
        _update_library(summary)

    with DISCOVER_LOCK:
        registry.discard(mid)


def _summarize_match(jsonl_path, mid, tn, opening_mk):
    """流式读 jsonl, 产出单场 定价模板/规则/极值 摘要。"""
    if not os.path.exists(jsonl_path):
        return None
    first = last = None
    rows = []
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                rows.append(r)
    except Exception:
        return None
    if not rows:
        return None
    first, last = rows[0], rows[-1]

    template = []
    seen = set()
    for r in rows:
        for mk in r.get("markets", {}):
            if mk not in seen:
                seen.add(mk)
                template.append(mk)

    series = {}
    for r in rows:
        for mk, sels in r.get("markets", {}).items():
            for sel, ov in sels.items():
                series.setdefault((mk, sel), []).append(ov)

    extremes = {}
    for (mk, sel), vals in series.items():
        if not vals:
            continue
        extremes[f"{mk}.{sel}"] = {
            "first": round(vals[0], 4), "last": round(vals[-1], 4),
            "min": round(min(vals), 4), "max": round(max(vals), 4),
            "ticks": len(vals),
        }

    over = _opening_overrounds(opening_mk) if opening_mk else {}
    return {
        "mid": mid, "league": tn,
        "home": last.get("home"), "away": last.get("away"),
        "ticks": len(rows),
        "time_span": {"first": first.get("ts_iso"), "last": last.get("ts_iso")},
        "pricing_template": template,
        "template_size": len(template),
        "opening_overround": over,          # 庄家 embedded margin 模板
        "pricing_values_extremes": extremes,
        "final_status": last.get("status"),
        "final_score": last.get("score"),
    }


# ══════════════════ 跨场定价模板库 ══════════════════
def _load_library():
    if os.path.exists(LIB_PATH):
        try:
            with open(LIB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"updated": None, "total_matches": 0, "by_league": {}}


def _update_library(match_summary):
    """把单场摘要合并进跨场模板库 (按联赛聚合市场模板直方图 + margin 统计)。"""
    with LIB_LOCK:
        lib = _load_library()
        tn = match_summary.get("league", "?")
        le = lib["by_league"].setdefault(tn, {
            "matches": [], "template_hist": {}, "overround": {}, "n_overround": 0,
        })
        mid = match_summary["mid"]
        if mid in le["matches"]:
            return
        le["matches"].append(mid)
        # 市场模板直方图 (形状出现次数)
        tpl = tuple(match_summary.get("pricing_template", []))
        le["template_hist"][str(tpl)] = le["template_hist"].get(str(tpl), 0) + 1
        # margin 统计 (running sum/count, 输出时求均值)
        ov = match_summary.get("opening_overround") or {}
        for mktype, val in ov.items():
            if val is None:
                continue
            d = le["overround"].setdefault(mktype, {"sum": 0.0, "n": 0})
            d["sum"] += val
            d["n"] += 1
        le["n_overround"] += 1
        # 重算均值
        for mktype, d in le["overround"].items():
            d["avg"] = round(d["sum"] / d["n"], 4) if d["n"] else None

        lib["total_matches"] = sum(len(v["matches"]) for v in lib["by_league"].values())
        lib["updated"] = _now_iso()
        with open(LIB_PATH, "w", encoding="utf-8") as f:
            json.dump(lib, f, ensure_ascii=False, indent=2)
        print(f"[{_now_iso()}] [LIB] 已更新模板库 | 联赛={tn} | 总场次={lib['total_matches']} "
              f"| 该联赛 margin(1X2/AU/OU)={ {k: le['overround'].get(k, {}).get('avg') for k in ('1X2','AH','OU')} }")


# ══════════════════ 守护主循环 ══════════════════
def main():
    ap = argparse.ArgumentParser(description="电子盘(VS-)定价模板自动采集守护进程 (隔离版)")
    ap.add_argument("--discover-interval", type=float, default=30.0)
    ap.add_argument("--poll-interval", type=float, default=1.0)
    ap.add_argument("--max-match-minutes", type=float, default=120.0)
    ap.add_argument("--max-runtime", type=float, default=0.0,
                    help="守护进程最长运行分钟 (0=无限)")
    ap.add_argument("--max-concurrent", type=int, default=8,
                    help="同时监控的最大场次 (限流保护: 每场 1 QPS, 超限会封 token)")
    ap.add_argument("--league-filter", type=str, default="",
                    help="只监控联赛名含该子串的场次, 如 EAFC25 (留空=全部电子盘)")
    ap.add_argument("--tol-started", type=float, default=120.0,
                    help="已开赛多少秒内仍算'刚开赛'可接受 (默认120s)")
    ap.add_argument("--lead-max", type=float, default=600.0,
                    help="距开赛多少秒内算'即将开赛'值得占位 (默认600s)")
    ap.add_argument("--max-group", type=int, default=1,
                    help="接受的最低新鲜度组: 0=仅即将开赛 1=含刚开赛(默认) "
                         "2=含远期 3=含中途场(缺初盘)")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    registry = set()          # 正在监控的 mid
    start = time.time()
    print(f"[{_now_iso()}] 电子盘守护启动 | discover={args.discover_interval}s "
          f"poll={args.poll_interval}s max_match={args.max_match_minutes}min")
    print(f"  隔离落盘前缀: {ELEC_PREFIX}*  库: {LIB_PATH}")

    try:
        while True:
            # —— 发现 + 启动新比赛 ——
            vs = discover_vs_matches()
            if args.league_filter:
                vs = [x for x in vs if args.league_filter in (x[1] or "")]
            # 新鲜度排序: 名额优先给"能从初盘抓起"的场
            cands = rank_by_freshness(vs, args.tol_started, args.lead_max)
            skipped = 0
            started = 0
            with DISCOVER_LOCK:
                for mid, tn, mgt, grp in cands:
                    sum_path = os.path.join(DATA_DIR, f"{ELEC_PREFIX}{mid}_summary.json")
                    if mid in registry or os.path.exists(sum_path):
                        continue  # 已在监控 or 已完成(幂等)
                    if grp > args.max_group:
                        skipped += 1
                        continue  # 新鲜度不达标(缺初盘/等太久), 不占名额
                    if len(registry) >= args.max_concurrent:
                        skipped += 1
                        continue  # 限流: 等已有场次结束后下一轮再补位
                    registry.add(mid)
                    started += 1
                    lbl = {0: "即将开赛", 1: "刚开赛", 2: "远期", 3: "中途"}.get(grp, "?")
                    print(f"[{_now_iso()}] [选场] {mid} 新鲜度={lbl} | {tn}")
                    t = threading.Thread(
                        target=monitor_match,
                        args=(mid, tn, args.poll_interval, args.max_match_minutes, registry),
                        daemon=True)
                    t.start()
            if vs:
                extra = f", 待命 {skipped} 场" if skipped else ""
                print(f"[{_now_iso()}] 发现 {len(vs)} 场电子盘, 在监控 "
                      f"{len(registry)}/{args.max_concurrent} 场{extra}")
            else:
                print(f"[{_now_iso()}] 当前无电子盘比赛, 等待下一轮发现…")

            # —— 等待下一轮发现 ——
            for _ in range(int(args.discover_interval)):
                if args.max_runtime and (time.time() - start) / 60.0 >= args.max_runtime:
                    print(f"[{_now_iso()}] 达 max-runtime, 守护退出")
                    return
                time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[{_now_iso()}] 用户中断, 守护退出 (在跑的比赛线程会随进程结束)")


if __name__ == "__main__":
    main()
