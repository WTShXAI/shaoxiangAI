#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
操盘手 盘口稳定性置信度 (bookmaker line-stability confidence) — 哨响AI 优化方向

输入: match_key (+ 可选 kickoff), events.db odds_snapshots(market='1X2')
输出: 操盘手 盘口置信度 conf ∈ [0,1] + 诊断

设计依据 (data/bookmaker_drift_report.json, n=738, 含预赛开盘 557):
  - 锚定最新盘成立: 收盘热门 39.8% ≈ 开盘 39.4%  -> 沿用 v7.4 100% 跟盘
  - 跟随变线方向胜率仅 35.4% (< 39.8%)            -> 拒绝"追变线"信号
  - 预赛开盘变线档位(纯操盘手意图):
        low/mid 摆幅 : 热门命中 40.5%
        high 摆幅(≥8pp): 热门命中 18.2% (n=11)   -> 大变线=陷阱线, 置信度骤降

因此: conf 随"开盘→收盘变线摆幅"单调递减; 大摆幅(尤其预赛) => 低置信 => 降注/避险。
绝不覆盖盘口锚定结论(只缩放注码, 不改动 pick)。

用法:
  from bookmaker_confidence import bookmaker_confidence
  r = bookmaker_confidence(match_key, kickoff="2026-07-20 03:00")
  # r = {"conf": 0.82, "spread": 0.031, "tier": "low", "used_pre": True, ...}
"""
from __future__ import annotations
import sqlite3, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GQ = ROOT / "data" / "events.db"
SIDES = ["H", "D", "A"]
DRIFT_TH = 0.02


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


def _open_close(c, mk, ko):
    """返回 (open_ts, close_ts, used_pre)。优先 kickoff 前窗口(纯操盘手变线)。"""
    r = c.execute(
        "SELECT MIN(captured_at), MAX(captured_at) FROM odds_snapshots "
        "WHERE match_key=? AND market='1X2'", (mk,)
    ).fetchone()
    if not r or r[0] is None:
        return None, None, False
    open_ts, close_ts = r[0], r[1]
    used_pre = False
    if ko is not None:
        pre = c.execute(
            "SELECT COUNT(*) FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "AND captured_at < ?", (mk, ko)
        ).fetchone()[0]
        if pre >= 3:
            used_pre = True
            ots = c.execute(
                "SELECT MAX(captured_at) FROM odds_snapshots WHERE match_key=? "
                "AND market='1X2' AND captured_at < ?", (mk, ko)
            ).fetchone()[0]
            its = c.execute(
                "SELECT MIN(captured_at) FROM odds_snapshots WHERE match_key=? "
                "AND market='1X2' AND captured_at < ?", (mk, ko)
            ).fetchone()[0]
            open_ts, close_ts = its, ots
    return open_ts, close_ts, used_pre


def bookmaker_confidence(match_key, kickoff=None, db=str(GQ)):
    """操盘手盘口稳定性置信度。

    返回 dict: conf(∈[0,1]), spread, tier(low/mid/high), used_pre,
               open_prob, close_prob, favorite, n_snaps。
    任何异常/缺数据 -> conf=1.0 (不阻止下注, 仅当真有信号才降注)。
    """
    out = {"conf": 1.0, "spread": None, "tier": None, "used_pre": False,
           "open_prob": None, "close_prob": None, "favorite": None, "n_snaps": 0,
           "ok": False}
    try:
        c = sqlite3.connect(f"file:{Path(db).resolve().as_posix()}?mode=ro", uri=True)
        ko = parse_kickoff(kickoff)
        open_ts, close_ts, used_pre = _open_close(c, match_key, ko)
        if open_ts is None:
            return out
        open_rows = c.execute(
            "SELECT selection,odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "AND captured_at>=? AND captured_at<=?", (match_key, open_ts - 1, open_ts + 1)
        ).fetchall()
        close_rows = c.execute(
            "SELECT selection,odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "AND captured_at>=? AND captured_at<=?", (match_key, close_ts - 1, close_ts + 1)
        ).fetchall()
        om = {s: o for s, o in open_rows}
        cm = {s: o for s, o in close_rows}
        if not ({"home", "draw", "away"} <= set(om) and {"home", "draw", "away"} <= set(cm)):
            return out
        op = devig([om["home"], om["draw"], om["away"]])
        cp = devig([cm["home"], cm["draw"], cm["away"]])
        spread = max(abs(cp[i] - op[i]) for i in range(3))
        out.update({
            "ok": True, "spread": round(spread, 4), "used_pre": used_pre,
            "open_prob": [round(x, 4) for x in op], "close_prob": [round(x, 4) for x in cp],
            "favorite": SIDES[cp.index(max(cp))],
            "n_snaps": c.execute(
                "SELECT COUNT(*) FROM odds_snapshots WHERE match_key=? AND market='1X2'",
                (match_key,)).fetchone()[0],
        })
        # 置信度: 随摆幅单调递减, 大摆幅封底 0.4 (仅降注, 不归零)
        # spread 0 -> 1.0 ; spread >=0.08 -> 0.4
        conf = 1.0 - min(spread / 0.08, 1.0) * 0.6
        # 预赛大摆幅额外惩罚(陷阱线): 再乘 0.7
        if used_pre and spread >= 0.08:
            conf *= 0.7
        out["conf"] = round(max(0.4, min(1.0, conf)), 4)
        if spread < 0.04:
            out["tier"] = "low"
        elif spread < 0.08:
            out["tier"] = "mid"
        else:
            out["tier"] = "high"
        return out
    except Exception:
        return out


def stake_scale(conf, floor=0.5):
    """注码缩放因子 ∈ [floor, 1.0]: 低置信降注, 高置信满注。绝不放大(上限 1.0)。"""
    return round(max(floor, min(1.0, 0.4 + 0.6 * conf)), 4)


if __name__ == "__main__":
    import sys
    mk = sys.argv[1] if len(sys.argv) > 1 else "西班牙 vs 阿根廷"
    ko = sys.argv[2] if len(sys.argv) > 2 else None
    r = bookmaker_confidence(mk, ko)
    print(json.dumps if False else r)
    print(f"match={mk} kickoff={ko}")
    print(f"  ok={r['ok']} used_pre={r['used_pre']} spread={r['spread']} tier={r['tier']}")
    print(f"  favorite={r['favorite']} conf={r['conf']} stake_scale={stake_scale(r['conf'])}")
