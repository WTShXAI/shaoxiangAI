#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
analyze_electronic_pricing.py — 电子盘「定价模板 / 定价规则 / 赔率值」提炼器

输入: data/electronic_poll_<mid>.jsonl  (由 electronic_poll_daemon.py 采集)
输出:
  - data/electronic_pricing_report.json  (机器可读)
  - data/electronic_pricing_report.md    (人读交付件, 对应原始需求)

三块产出 (对应用户需求):
  1) 定价模板 — 庄家挂哪些市场、线位怎么摆、市场生灭(suspend/reopen)
  2) 定价规则 — 赔率随「时间流逝 / 比分变化」怎么动的可量化规律
  3) 赔率值   — 初盘值、极值、变动幅度、margin(overround) 结构

只读 data/electronic_poll_*.jsonl, 不写任何业务库。

注意: 电子盘(e-fixtures)的 minute 字段在 GQ 接口里恒定冻结为 6,
status 也恒定 'live' 不翻 finished —— 故时间轴一律用墙钟 ts_iso,
比赛推进的真实信号是 score 字段(有进球才会变)。
"""

import os
import re
import json
import glob
import argparse
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")


# ══════════════════ 工具 ══════════════════
def _overround(sel_odds):
    """庄家内嵌毛利 (vig)。返回 None 表示不可算。

    注意「多重覆盖」市场: 双重机会(1X/12/X2) 三个选项各覆盖 2/3 结果空间,
    Σ(1/odds) ≈ 2 是结构性的、不是 100% 暴利。故先估覆盖倍数 k, 再算
    margin = Σ/k - 1。单覆盖市场 k=1, 退化回经典公式。
    """
    vals = [v for v in sel_odds.values() if isinstance(v, (int, float)) and v > 1.0]
    if len(vals) < 2:
        return None
    s = sum(1.0 / v for v in vals)
    # k = 结果空间被重复覆盖的倍数; 仅当 Σ 明显 >=1.8 才判为多重覆盖,
    # 避免把高 vig 的波胆(Σ≈1.37)误判成 k=2 而算出负 margin。
    k = round(s) if s >= 1.8 else 1
    k = max(1, k)
    return s / k - 1.0


def _score_tuple(s):
    """'1-2' → (1,2); 无效 → None"""
    if not s or not isinstance(s, str) or "-" not in s:
        return None
    try:
        a, b = s.split("-", 1)
        return int(a), int(b)
    except Exception:
        return None


def _load(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass
    return rows


def _line_of(market):
    """从 'OU_2.50' / 'AH_-0.50' 提取线位 float; 非线位市场返回 None。"""
    m = re.match(r"^(OU|AH)_(-?\d+(?:\.\d+)?)$", market)
    return float(m.group(2)) if m else None


def _main_ou_line(markets):
    """主线 OU = over/under 最接近平分(价差最小)的全场线。返回市场名或 None。"""
    best, bestgap = None, 9e9
    for m, sel in (markets or {}).items():
        if not m.startswith("OU_") or "_1H" in m or "_2H" in m:
            continue
        o, u = sel.get("over"), sel.get("under")
        if not (isinstance(o, (int, float)) and isinstance(u, (int, float))):
            continue
        gap = abs(o - u)
        if gap < bestgap:
            bestgap, best = gap, m
    return best


def _pct(a, b):
    """(b-a)/a*100, 无效返回 None。"""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a:
        return round((b - a) / a * 100, 1)
    return None


# ══════════════════ 单场分析 ══════════════════
def analyze_match(path):
    rows = _load(path)
    if not rows:
        return None
    mid = os.path.basename(path).replace("electronic_poll_", "").replace(".jsonl", "")
    first, last = rows[0], rows[-1]
    n = len(rows)

    # 墙钟时间轴
    t0 = first.get("ts_epoch") or 0
    t1 = last.get("ts_epoch") or 0
    span_s = max(0, int(t1 - t0))

    # ---- 1) 定价模板 ----
    market_life = defaultdict(lambda: {"first": None, "last": None, "ticks": 0})
    all_markets = set()
    for i, r in enumerate(rows):
        for mk in (r.get("markets") or {}):
            all_markets.add(mk)
            L = market_life[mk]
            if L["first"] is None:
                L["first"] = i
            L["last"] = i
            L["ticks"] += 1

    persistent = [m for m in all_markets if market_life[m]["ticks"] >= n * 0.95]
    volatile = [m for m in all_markets if market_life[m]["ticks"] < n * 0.95]

    # suspend/reopen 事件 (市场消失后又出现)
    suspend_events = []
    for mk in all_markets:
        present = [(mk in (r.get("markets") or {})) for r in rows]
        gaps = 0
        for i in range(1, n):
            if present[i - 1] and not present[i]:
                gaps += 1
        if gaps:
            suspend_events.append((mk, gaps))
    suspend_events.sort(key=lambda x: -x[1])

    # ---- 2) 定价规则 ----
    # 2a. 时间衰减: 比分未变期间, OU over/under / 1X2 的漂移方向
    drift = {"over_up": 0, "over_dn": 0, "under_up": 0, "under_dn": 0,
             "draw_up": 0, "draw_dn": 0, "home_up": 0, "home_dn": 0}
    score_change_ticks = []
    prev = None
    for i, r in enumerate(rows):
        mk = r.get("markets") or {}
        sc = r.get("score")
        if prev is not None:
            if prev.get("score") != sc and _score_tuple(sc) is not None:
                score_change_ticks.append((i, prev.get("score"), sc))
            else:
                pm = prev.get("markets") or {}
                for m in mk:
                    if not m.startswith("OU_"):
                        continue
                    for sel in ("over", "under"):
                        a = (pm.get(m) or {}).get(sel)
                        b = (mk.get(m) or {}).get(sel)
                        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                            if b > a:
                                drift[f"{sel}_up"] += 1
                            elif b < a:
                                drift[f"{sel}_dn"] += 1
                for sel in ("home", "draw"):
                    a = (pm.get("1X2") or {}).get(sel)
                    b = (mk.get("1X2") or {}).get(sel)
                    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                        if b > a:
                            drift[f"{sel}_up"] += 1
                        elif b < a:
                            drift[f"{sel}_dn"] += 1
        prev = r

    # 2b. OU 线位锚定: 主线 vs 当前总进球
    ou_anchor = []
    for r in rows:
        ml_name = _main_ou_line(r.get("markets") or {})
        st = _score_tuple(r.get("score"))
        if ml_name is not None and st is not None:
            ml = _line_of(ml_name)
            ou_anchor.append({"ts": r.get("ts_iso"), "score": r.get("score"),
                              "goals": st[0] + st[1], "main_line": ml,
                              "residual": round(ml - (st[0] + st[1]), 2)})

    # 2c. 进球瞬间重定价 (核心定价规则)
    events, reprice_by_who = _score_event_repricing(rows)

    # ---- 3) 赔率值 ----
    extremes = {}
    for r in rows:
        for m, sel in (r.get("markets") or {}).items():
            if not isinstance(sel, dict):
                continue
            for s, v in sel.items():
                # 跳过 0.0 / 非正哨兵值(已关闭/不再可能的正确比分线), 避免污染振幅
                if not isinstance(v, (int, float)) or v <= 0:
                    continue
                k = f"{m}.{s}"
                e = extremes.setdefault(k, {"min": v, "max": v, "first": v, "last": v})
                e["min"] = min(e["min"], v)
                e["max"] = max(e["max"], v)
                e["last"] = v

    def margins(markets):
        out = {}
        for m, sel in (markets or {}).items():
            if not isinstance(sel, dict):
                continue
            ov = _overround(sel)
            if ov is not None:
                out[m] = round(ov * 100, 2)
        return out

    return {
        "mid": mid,
        "league": first.get("league"),
        "home": first.get("home"),
        "away": first.get("away"),
        "ticks": n,
        "span_seconds": span_s,
        "ts_first": first.get("ts_iso"),
        "ts_last": last.get("ts_iso"),
        "score_first": first.get("score"),
        "score_last": last.get("score"),
        "template": {
            "market_count": len(all_markets),
            "persistent": sorted(persistent),
            "volatile": sorted(volatile),
            "suspend_reopen_events": suspend_events[:15],
        },
        "rules": {
            "time_decay_drift": drift,
            "score_change_count": len(score_change_ticks),
            "score_change_ticks": score_change_ticks[:20],
            "ou_anchor_sample": ou_anchor[:4] + ou_anchor[-4:],
            "reprice_events": events[:12],
            "reprice_by_who": reprice_by_who,
        },
        "values": {
            "margin_open_pct": margins(first.get("markets")),
            "margin_last_pct": margins(last.get("markets")),
            "extremes_top": dict(sorted(
                ((k, v) for k, v in extremes.items()),
                key=lambda kv: -(kv[1]["max"] - kv[1]["min"]))[:20]),
        },
    }


def _score_event_repricing(rows):
    """进球瞬间的庄家重定价规则。

    返回:
      events : 每次比分跳变的明细 (tick / 谁进 / 前后赔率跳变%)
      by_who : 按 'home'/'away'/'both' 聚合的中位跳变
    """
    events = []
    prev = None
    for i, r in enumerate(rows):
        mk = r.get("markets") or {}
        sc = r.get("score")
        if prev is not None and _score_tuple(sc) is not None:
            psc = prev.get("score")
            pt = _score_tuple(psc)
            ct = _score_tuple(sc)
            if pt is not None and ct != pt:
                who = "home" if ct[0] > pt[0] else ("away" if ct[1] > pt[1] else "both")
                pm = prev.get("markets") or {}
                mul = _main_ou_line(mk)
                mulo = _main_ou_line(pm)
                line = mul or mulo
                p1x2 = pm.get("1X2") or {}
                c1x2 = mk.get("1X2") or {}
                po = (pm.get(line) or {}) if line else {}
                co = (mk.get(line) or {}) if line else {}
                ev = {
                    "tick": i, "who": who, "prev": psc, "cur": sc,
                    "home_dp": _pct(p1x2.get("home"), c1x2.get("home")),
                    "draw_dp": _pct(p1x2.get("draw"), c1x2.get("draw")),
                    "away_dp": _pct(p1x2.get("away"), c1x2.get("away")),
                    "over_dp": _pct(po.get("over"), co.get("over")),
                    "under_dp": _pct(po.get("under"), co.get("under")),
                    "main_line": line,
                }
                events.append(ev)
        prev = r

    agg = {}
    for e in events:
        a = agg.setdefault(e["who"], {"n": 0, "home": [], "draw": [], "away": [], "over": [], "under": []})
        a["n"] += 1
        for k in ("home", "draw", "away", "over", "under"):
            v = e[f"{k}_dp"]
            if v is not None:
                a[k].append(v)
    by_who = {}
    for who, a in agg.items():
        def med(xs):
            return round(sorted(xs)[len(xs) // 2], 1) if xs else None
        by_who[who] = {"n": a["n"],
                       "home_dp_med": med(a["home"]), "draw_dp_med": med(a["draw"]),
                       "away_dp_med": med(a["away"]), "over_dp_med": med(a["over"]),
                       "under_dp_med": med(a["under"])}
    return events, by_who


# ══════════════════ 报告 ══════════════════
def _md(reports):
    L = []
    L.append("# 电子盘定价分析报告\n")
    L.append(f"> 数据源: `data/electronic_poll_*.jsonl`  |  {len(reports)} 场  |  "
             f"总 tick {sum(r['ticks'] for r in reports)}  |  "
             f"采集方式: GQ 接口 1 秒级轮询(单并发, EAFC25 专属)\n")
    L.append("> ⚠️ 电子盘(e-fixtures)在 GQ 接口的 `minute` 恒定冻结为 6、`status` 恒定 live 不翻 finished,"
             "故时间轴采用墙钟; 比赛推进以 `score` 字段为准。\"从初盘到结束\"实为\"初盘→最新采集快照\"。\n")

    # ── 1) 定价模板 ──
    L.append("\n## 一、定价模板 — 庄家挂哪些市场\n")
    tmpl_counter = Counter(r["template"]["market_count"] for r in reports)
    dist = " / ".join(f"{k}场×{v}" for k, v in sorted(tmpl_counter.items()))
    L.append(f"**市场数分布**(每场挂牌市场总数): {dist}\n")
    mk_freq = Counter()
    for r in reports:
        for m in r["template"]["persistent"]:
            mk_freq[m] += 1
    L.append("**跨场常挂市场(出现在 N/{} 场, 按复用度)**:\n".format(len(reports)))
    for m, c in mk_freq.most_common(22):
        L.append(f"- `{m}` — {c}/{len(reports)}")
    L.append("")
    ev = Counter()
    for r in reports:
        for m, g in r["template"]["suspend_reopen_events"]:
            ev[m] += g
    if ev:
        L.append("**市场生灭(suspend→reopen, 庄家重算盘口瞬间)**:\n")
        for m, g in ev.most_common(8):
            L.append(f"- `{m}` — {g} 次")
    else:
        L.append("**市场生灭**: 本批未捕获(市场在所采窗口内全程常挂)。")
    L.append("")

    # ── 2) 定价规则 ──
    L.append("\n## 二、定价规则 — 赔率怎么动\n")
    tot = defaultdict(int)
    for r in reports:
        for k, v in r["rules"]["time_decay_drift"].items():
            tot[k] += v
    L.append("### 2.1 时间衰减(比分未变、墙钟流逝时)\n")
    L.append("| 维度 | ↑上升 tick | ↓下降 tick | 主导方向 | 占比 |")
    L.append("|---|---|---|---|---|")
    for sel in ("over", "under", "home", "draw"):
        up, dn = tot[f"{sel}_up"], tot[f"{sel}_dn"]
        s = up + dn
        if s:
            bias = "↑上升" if up > dn else "↓下降"
            L.append(f"| {sel} | {up} | {dn} | {bias} | {max(up,dn)/s*100:.1f}% |")
    L.append("\n> 解读: 比分不变、时间流逝 ⇒ 剩余期望进球↓ ⇒ over 赔率↑ / under 赔率↓;"
             " 平局概率随时间累积↑ ⇒ draw 赔率↓。\n")

    L.append("### 2.2 OU 主线锚定(主线 = 当前总进球 + 剩余时间期望)\n")
    res = []
    for r in reports:
        for a in r["rules"]["ou_anchor_sample"]:
            res.append(a["residual"])
    if res:
        L.append(f"残差(主线 − 已进球) 样本 {len(res)}: "
                 f"min={min(res):.2f}  均值={sum(res)/len(res):.2f}  max={max(res):.2f}\n")
        L.append("> 庄家按「已进球 + 剩余时间期望」实时平移 OU 线位, 残差即隐含剩余期望进球。\n")

    L.append("### 2.3 进球瞬间的重定价(核心规则)\n")
    ncs = sum(r["rules"]["score_change_count"] for r in reports)
    L.append(f"本批捕获 **{ncs} 次**比分跳变(进球/失球)。按\"谁进球\"聚合的赔率中位跳变:\n")
    L.append("| 谁进球 | 次数 | 主胜赔 Δ% | 平赔 Δ% | 客胜赔 Δ% | over Δ% | under Δ% |")
    L.append("|---|---|---|---|---|---|---|")
    who_label = {"home": "主队进球", "away": "客队进球", "both": "双方同回合进球"}
    for who in ("home", "away", "both"):
        rows_w = [r["rules"]["reprice_by_who"].get(who) for r in reports if r["rules"]["reprice_by_who"].get(who)]
        if not rows_w:
            continue
        agg = {k: [x[k] for x in rows_w if x.get(k) is not None] for k in
               ("n", "home_dp_med", "draw_dp_med", "away_dp_med", "over_dp_med", "under_dp_med")}
        n = sum(agg["n"])
        def m(xs):
            return f"{sorted(xs)[len(xs)//2]:+.1f}" if xs else "—"
        L.append(f"| {who_label.get(who, who)} | {n} | {m(agg['home_dp_med'])} | {m(agg['draw_dp_med'])} | "
                 f"{m(agg['away_dp_med'])} | {m(agg['over_dp_med'])} | {m(agg['under_dp_med'])} |")
    L.append("")
    # 展示几个具体进球事件
    def _f(v):
        return f"{v:+.1f}%" if isinstance(v, (int, float)) else "—"

    L.append("**进球事件明细(节选)**:\n")
    shown = 0
    for r in reports:
        for e in r["rules"]["reprice_events"]:
            if shown >= 10:
                break
            L.append(f"- [{r['home']} vs {r['away']}] tick#{e['tick']} "
                     f"{e['prev']}→{e['cur']} ({who_label.get(e['who'], e['who'])}): "
                     f"主胜{_f(e['home_dp'])} / 平{_f(e['draw_dp'])} / 客胜{_f(e['away_dp'])} "
                     f"/ over{_f(e['over_dp'])} / under{_f(e['under_dp'])}  (线 {e['main_line']})")
            shown += 1
        if shown >= 10:
            break
    L.append("")
    L.append("> 注: over/under 跳变取「主 OU 线」前后对比; 进球后 OU 线通常整体上移约 1 球重新锚定,"
             " 故 over 常显 ~0%、真实信号体现在 under(进球后 under 赔率↑=更不可能小比分)。"
             " 1X2 三项是进球重定价最干净、可量化的规则。\n")

    # ── 3) 赔率值 / margin ──
    L.append("\n## 三、赔率值 — margin(overround) 结构\n")
    mg = defaultdict(list)
    for r in reports:
        for m, v in r["values"]["margin_open_pct"].items():
            key = re.sub(r"_-?\d+(\.\d+)?$", "_*", m)
            mg[key].append(v)
    L.append("**初盘 margin 按市场族(中位数)**:\n")
    L.append("| 市场族 | n | 中位 margin | 区间 |")
    L.append("|---|---|---|---|")
    for k in sorted(mg, key=lambda x: -len(mg[x]))[:14]:
        vs = sorted(mg[k])
        L.append(f"| `{k}` | {len(vs)} | {vs[len(vs)//2]:.2f}% | [{vs[0]:.2f}, {vs[-1]:.2f}] |")
    L.append("")
    L.append("> margin 阶梯解读: 单/双、双重机会等组合市场 vig 最低(接近公平定价), "
             "波胆(CS)与精确比分组合 vig 最高(庄家重点抽水处)。\n")

    L.append("### 3.1 单场最大赔率波动 Top\n")
    for r in sorted(reports, key=lambda x: -x["ticks"])[:4]:
        L.append(f"**{r['home']} vs {r['away']}** — {r['ticks']} tick, "
                 f"{r['ts_first']}→{r['ts_last']} ({(r['span_seconds']//60)}分{r['span_seconds']%60}秒), "
                 f"比分 {r['score_first'] or '—'}→{r['score_last'] or '—'}:")
        for k, e in list(r["values"]["extremes_top"].items())[:6]:
            L.append(f"- `{k}`  {e['first']} → {e['last']}  [区间 {e['min']}~{e['max']}]  振幅 {e['max']-e['min']:.2f}")
        L.append("")

    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="电子盘定价模板/规则/赔率值 提炼器")
    ap.add_argument("--pattern", default="electronic_poll_*.jsonl")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "electronic_pricing_report.json"))
    ap.add_argument("--md", default=os.path.join(DATA_DIR, "electronic_pricing_report.md"))
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(DATA_DIR, args.pattern)))
    paths = [p for p in paths if "_summary" not in p]
    if not paths:
        print("未找到电子盘数据文件")
        return

    reports = []
    for p in paths:
        try:
            r = analyze_match(p)
            if r:
                reports.append(r)
        except Exception as e:
            print(f"[WARN] {os.path.basename(p)} 分析失败: {type(e).__name__}: {e}")

    if not reports:
        print("无有效数据")
        return

    # 控制台摘要
    print("=" * 78)
    print(f"电子盘定价分析 — {len(reports)} 场 | 总 tick {sum(r['ticks'] for r in reports)}")
    print("=" * 78)
    for r in reports:
        print(f"  · {r['home']} vs {r['away']}: {r['ticks']}tick "
              f"({r['ts_first']}→{r['ts_last']}, {r['span_seconds']}s) "
              f"比分 {r['score_first'] or '—'}→{r['score_last'] or '—'} | "
              f"进球事件 {r['rules']['score_change_count']} | 市场 {r['template']['market_count']}")

    # 进球重定价聚合(全局)
    print("\n【进球重定价·全局聚合】")
    g_who = defaultdict(lambda: {"n": 0, "home": [], "draw": [], "away": [], "over": [], "under": []})
    for r in reports:
        for who, a in r["rules"]["reprice_by_who"].items():
            g = g_who[who]
            g["n"] += a["n"]
            for k in ("home", "draw", "away", "over", "under"):
                v = a.get(f"{k}_dp_med")
                if v is not None:
                    g[k].append(v)
    for who, g in g_who.items():
        def med(xs):
            return f"{sorted(xs)[len(xs)//2]:+.1f}%" if xs else "—"
        print(f"  {who}: n={g['n']}  主胜{med(g['home'])} 平{med(g['draw'])} "
              f"客胜{med(g['away'])} over{med(g['over'])} under{med(g['under'])}")

    # margin 阶梯
    print("\n【初盘 margin 阶梯】")
    mg = defaultdict(list)
    for r in reports:
        for m, v in r["values"]["margin_open_pct"].items():
            key = re.sub(r"_-?\d+(\.\d+)?$", "_*", m)
            mg[key].append(v)
    for k in sorted(mg, key=lambda x: -len(mg[x]))[:10]:
        vs = sorted(mg[k])
        print(f"  {k:<22} n={len(vs):<4} 中位 {vs[len(vs)//2]:>6.2f}%")

    # 写 JSON + MD
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=2)
    with open(args.md, "w", encoding="utf-8") as f:
        f.write(_md(reports))
    print(f"\nJSON  → {args.out}")
    print(f"MD    → {args.md}")


if __name__ == "__main__":
    main()
