#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
操盘手 线移动分析 (bookmaker / 庄家意图) — 哨响AI 优化方向

数据底座: events.db odds_snapshots (market='1X2', ms 级 captured_at) + matches(赛果).
现实约束: GQ 主要采集【盘中(in-play)】赔率(开盘快照多在 kickoff 后),
          故本分析聚焦「盘中变线预测力」(closing-line-value 的盘中版),
          验证「跟随操盘手最新盘口」是否优于「早期盘口」,
          并量化「操盘手变线方向」是否预示赛果(信号 vs 陷阱).

输出: data/bookmaker_drift_report.json + 控制台摘要.

用法: python scripts/bookmaker_drift.py
"""
from __future__ import annotations
import sqlite3, datetime, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GQ = ROOT / "data" / "events.db"
OUT = ROOT / "data" / "bookmaker_drift_report.json"
SIDES = ["H", "D", "A"]
DRIFT_TH = 0.02  # 变线阈值(概率差)


def devig(odds):
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [i / s for i in inv]


def parse_kickoff(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M").timestamp()
    except Exception:
        return None


def outcome_of(sh, sa):
    if sh is None or sa is None:
        return None
    if sh > sa:
        return "H"
    if sa > sh:
        return "A"
    return "D"


def main():
    c = sqlite3.connect(str(GQ))
    rows = c.execute(
        "SELECT match_key,home,away,league,kickoff,score_home,score_away "
        "FROM matches WHERE status='finished' AND score_home IS NOT NULL "
        "AND score_away IS NOT NULL"
    ).fetchall()

    st = {
        "n": 0,
        "with_premark": 0,           # 存在 kickoff 前快照(真·开盘→收盘)
        "open_fav_win": 0,
        "close_fav_win": 0,
        "winner_drift_up": 0,        # 操盘手赛果方概率↑(看对)
        "winner_drift_down": 0,      # 操盘手赛果方概率↓(看错/诱盘)
        "per_side": {s: [0, 0] for s in SIDES},   # [win, total] 当该方概率↑
        "drift_toward_win_rate": [0, 0],          # 操盘手↑方最终获胜
        "high_drift_matches": 0,
        # 变线档位(最大概率摆幅) vs 收盘热门命中率 / 赛果命中率
        "tier": {
            "low":  [0, 0, 0],   # [收盘热门命中, 赛果命中(收盘热门==胜方), total]
            "mid":  [0, 0, 0],
            "high": [0, 0, 0],
        },
        # 仅预赛开盘(open/close 皆在 kickoff 前)的档位 — 纯操盘手变线信号, 剔除盘中进球扰动
        "tier_pre": {
            "low":  [0, 0, 0],
            "mid":  [0, 0, 0],
            "high": [0, 0, 0],
        },
    }

    for mk, home, away, league, kickoff, sh, sa in rows:
        ko = parse_kickoff(kickoff)
        r = c.execute(
            "SELECT MIN(captured_at), MAX(captured_at) FROM odds_snapshots "
            "WHERE match_key=? AND market='1X2'", (mk,)
        ).fetchone()
        if not r or r[0] is None:
            continue
        open_ts, close_ts = r[0], r[1]
        # 是否存在 kickoff 前快照(真预赛开盘)
        if ko is not None:
            pre = c.execute(
                "SELECT COUNT(*) FROM odds_snapshots WHERE match_key=? AND market='1X2' "
                "AND captured_at < ?", (mk, ko)
            ).fetchone()[0]
            if pre >= 3:
                st["with_premark"] += 1
                # 用 kickoff 前最后一条作为收盘(预赛), 前第一条作为开盘
                ots = c.execute(
                    "SELECT MAX(captured_at) FROM odds_snapshots WHERE match_key=? "
                    "AND market='1X2' AND captured_at < ?", (mk, ko)
                ).fetchone()[0]
                its = c.execute(
                    "SELECT MIN(captured_at) FROM odds_snapshots WHERE match_key=? "
                    "AND market='1X2' AND captured_at < ?", (mk, ko)
                ).fetchone()[0]
                open_ts, close_ts = its, ots
        open_rows = c.execute(
            "SELECT selection,odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "AND captured_at>=? AND captured_at<=?", (mk, open_ts - 1, open_ts + 1)
        ).fetchall()
        close_rows = c.execute(
            "SELECT selection,odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "AND captured_at>=? AND captured_at<=?", (mk, close_ts - 1, close_ts + 1)
        ).fetchall()
        om = {s: o for s, o in open_rows}
        cm = {s: o for s, o in close_rows}
        if not ({"home", "draw", "away"} <= set(om) and {"home", "draw", "away"} <= set(cm)):
            continue
        op = devig([om["home"], om["draw"], om["away"]])
        cp = devig([cm["home"], cm["draw"], cm["away"]])
        win = outcome_of(sh, sa)
        if win is None:
            continue
        open_fav = SIDES[op.index(max(op))]
        close_fav = SIDES[cp.index(max(cp))]
        if open_fav == win:
            st["open_fav_win"] += 1
        if close_fav == win:
            st["close_fav_win"] += 1
        # 操盘手对赛果方的变线方向
        wi = SIDES.index(win)
        wdrift = cp[wi] - op[wi]
        if wdrift > DRIFT_TH:
            st["winner_drift_up"] += 1
        elif wdrift < -DRIFT_TH:
            st["winner_drift_down"] += 1
        # 跟随操盘手变线方向: 每方概率↑则该方最终胜率
        for i, s in enumerate(SIDES):
            d = cp[i] - op[i]
            if abs(d) >= DRIFT_TH:
                st["per_side"][s][1] += 1
                if s == win:
                    st["per_side"][s][0] += 1
                # 操盘手↑方=该方
                st["drift_toward_win_rate"][1] += 1
                if s == win:
                    st["drift_toward_win_rate"][0] += 1
        spread = max(abs(cp[i] - op[i]) for i in range(3))
        if spread >= 0.08:
            st["high_drift_matches"] += 1
        if spread < 0.04:
            t = "low"
        elif spread < 0.08:
            t = "mid"
        else:
            t = "high"
        st["tier"][t][1] += 1
        if close_fav == win:
            st["tier"][t][0] += 1
        if ko is not None and pre >= 3:
            st["tier_pre"][t][1] += 1
            if close_fav == win:
                st["tier_pre"][t][0] += 1
        st["n"] += 1

    # 汇总
    def pct(a, b):
        return round(100.0 * a / b, 1) if b else None

    summary = {
        "n_matches": st["n"],
        "with_premark": st["with_premark"],
        "open_fav_win_rate": pct(st["open_fav_win"], st["n"]),
        "close_fav_win_rate": pct(st["close_fav_win"], st["n"]),
        "winner_drift_up": st["winner_drift_up"],
        "winner_drift_down": st["winner_drift_down"],
        "操盘手看对赛果方(↑)占比": pct(st["winner_drift_up"], st["winner_drift_up"] + st["winner_drift_down"]),
        "跟随操盘手变线方向胜率": pct(st["drift_toward_win_rate"][0], st["drift_toward_win_rate"][1]),
        "high_drift_matches": st["high_drift_matches"],
        "tier_fav_hit": {t: pct(st["tier"][t][0], st["tier"][t][1]) for t in ("low", "mid", "high")},
        "tier_n": {t: st["tier"][t][1] for t in ("low", "mid", "high")},
        "tier_pre_fav_hit": {t: pct(st["tier_pre"][t][0], st["tier_pre"][t][1]) for t in ("low", "mid", "high")},
        "tier_pre_n": {t: st["tier_pre"][t][1] for t in ("low", "mid", "high")},
        "per_side_when_prob_up": {
            s: {"win": st["per_side"][s][0], "total": st["per_side"][s][1],
                "hit_rate": pct(st["per_side"][s][0], st["per_side"][s][1])}
            for s in SIDES
        },
    }
    OUT.write_text(json.dumps({"summary": summary, "raw": st}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[bookmaker_drift] 分析完毕 n={st['n']} (含预赛开盘 {st['with_premark']})")
    print(f"  开盘热门命中率 : {summary['open_fav_win_rate']}%")
    print(f"  收盘(最新)热门命中率 : {summary['close_fav_win_rate']}%  "
          f"-> {'最新盘更准, 锚定最新盘成立' if (summary['close_fav_win_rate'] or 0) >= (summary['open_fav_win_rate'] or 0) else '开盘更准'}")
    print(f"  操盘手对赛果方变线↑(看对) : {st['winner_drift_up']}  ↓(看错/诱) : {st['winner_drift_down']}  "
          f"看对占比 {summary['操盘手看对赛果方(↑)占比']}%")
    print(f"  跟随操盘手变线方向胜率 : {summary['跟随操盘手变线方向胜率']}%")
    print(f"  大变线(≥8pp)场数 : {st['high_drift_matches']}")
    for s in SIDES:
        ps = summary["per_side_when_prob_up"][s]
        print(f"    操盘手↑{s} -> 该方胜率 {ps['hit_rate']}% (n={ps['total']})")
    print(f"  --- 变线档位(稳定性) vs 收盘热门命中率 ---")
    for t in ("low", "mid", "high"):
        print(f"    {t:4s} 摆幅档: 命中 {summary['tier_fav_hit'][t]}% (n={summary['tier_n'][t]})")
    print(f"  --- 仅预赛开盘(纯操盘手变线信号) 档位命中率 ---")
    for t in ("low", "mid", "high"):
        print(f"    {t:4s} 摆幅档: 命中 {summary['tier_pre_fav_hit'][t]}% (n={summary['tier_pre_n'][t]})")
    print(f"[bookmaker_drift] 报告已写 {OUT}")


if __name__ == "__main__":
    main()
