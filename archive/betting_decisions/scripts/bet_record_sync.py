"""
scripts/bet_record_sync.py — 乐鱼 bet_record 闭环同步与 ROI 看板  (哨响AI P2)
=============================================================================
把用户真实账户投注记录沉淀为可复用闭环模块:
  - 归一化已结算(leyu_parsed.json) + 未结算(thenight_real_bets.json) 为统一 schema
  - 用 detail 文本"赢/输"判定输赢 (is_win 标志失真, 已证)
  - 产出闭环统计: 整体/按玩法/CS vs 非CS/按时间窗口/未结算待结算
  - 输出 analysis/bet_record_dashboard.json + 控制台摘要

抓取(可选, 需 leyu agent-browser 会话):
  fetch_raw() 用 agent-browser 导航 bet_record, 切 已结算/未结算 + 7天内,
  点数字页码抓全, 保存快照 JSON, 再由 parse_*_snapshot() 解析。
  本模块主路径直接消费已解析 JSON, 使统计可离线复跑、可审计。

用法:
  python scripts/bet_record_sync.py
"""
from __future__ import annotations
import os
import json
import sqlite3
import datetime as dt
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANALYSIS = os.path.join(ROOT, "analysis")
SETTLED_PATH = os.path.join(ANALYSIS, "leyu_parsed.json")
LIVE_PATH = os.path.join(ANALYSIS, "tonight_real_bets.json")
DASHBOARD_PATH = os.path.join(ANALYSIS, "bet_record_dashboard.json")

# 用户自述的时间窗口: 2026-08-11 起为"真实盘口交叉分析"阶段
REAL_ERA_START = dt.date(2026, 8, 11)


# ── 归一化 ──
def _is_win_from_detail(detail: str) -> Optional[bool]:
    """用 detail 文本判输赢 (is_win 标志不可靠)。
    注意: 玩法名含"独赢"(如"滚球全场独赢"), 不能简单查"赢"——会误判输单为赢。
    优先判"输"(结算结果词), 再判"赢"; 两者皆无(退款/未结算)返回 None。"""
    if not detail:
        return None
    if "输" in detail:
        return False
    if "赢" in detail:
        return True
    return None


def normalize_settled(rec: dict) -> dict:
    w = _is_win_from_detail(rec.get("detail", ""))
    stake = float(rec.get("stake") or 0.0)
    win = rec.get("win")
    win = float(win) if win is not None else 0.0
    return {
        "dt": rec.get("dt", ""),
        "betid": str(rec.get("bid", "")),
        "play": rec.get("play", ""),
        "is_cs": bool(rec.get("is_cs")),
        "is_win": w,
        "stake": stake,
        "pnl": win,                 # 净盈亏 (负=输全本金)
        "settled": True,
        "virt": bool(rec.get("virt")),
    }


def normalize_live(rec: dict) -> dict:
    return {
        "dt": rec.get("dt", "")[:10],
        "betid": str(rec.get("betid", "")),
        "play": rec.get("play", ""),
        "is_cs": "波胆" in (rec.get("play", "")),
        "is_win": None,             # 未结算
        "stake": float(rec.get("stake") or 0.0),
        "pnl": None,                # 待结算
        "settled": False,
        "virt": False,
        "max_win": float(rec.get("max_win") or 0.0),
        "cashout": float(rec.get("cashout") or 0.0),
        "league": rec.get("league", ""),
        "pick": rec.get("pick", ""),
        "odds": float(rec.get("odds") or 0.0),
        "live_score_at_bet": rec.get("live_score_at_bet", ""),
    }


# ── 统计 ──
def _agg(records: List[dict]) -> dict:
    staked = sum(r["stake"] for r in records)
    if not records:
        return {"n": 0, "stake": 0.0, "pnl": 0.0, "roi": 0.0,
                "wins": 0, "losses": 0, "win_rate": 0.0}
    # 仅已结算计入输赢
    dec = [r for r in records if r["is_win"] is not None]
    wins = sum(1 for r in dec if r["is_win"])
    losses = len(dec) - wins
    pnl = sum(r["pnl"] for r in records if r["pnl"] is not None)
    roi = (pnl / staked) if staked else 0.0
    return {
        "n": len(records), "stake": round(staked, 2), "pnl": round(pnl, 2),
        "roi": round(roi, 4),
        "wins": wins, "losses": losses,
        "win_rate": round(wins / len(dec), 4) if dec else 0.0,
    }


def closed_loop_stats(settled: List[dict], live: List[dict]) -> dict:
    all_settled = settled
    cs = [r for r in all_settled if r["is_cs"]]
    noncs = [r for r in all_settled if not r["is_cs"]]
    # 时间窗口
    def _parse(d):
        try:
            return dt.datetime.strptime(d[:10], "%Y-%m-%d").date()
        except Exception:
            return None
    real_era = [r for r in all_settled if (lambda x: x and x >= REAL_ERA_START)(_parse(r["dt"]))]
    blind_era = [r for r in all_settled if (lambda x: x and x < REAL_ERA_START)(_parse(r["dt"]))]
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "overall_settled": _agg(all_settled),
        "cs_only": _agg(cs),
        "noncs_only": _agg(noncs),
        "blind_era_before_0811": _agg(blind_era),
        "real_era_0811plus": _agg(real_era),
        "pending_live": _agg(live),
        "pending_live_detail": live,
    }


# ── 抓取(可选, 需 agent-browser leyu 会话) ──
def fetch_raw(session: str = "leyu", out_dir: str = ANALYSIS) -> dict:
    """用 agent-browser 抓 bet_record 全部已/未结算单。返回 {settled_path, live_path}。
    依赖 agent-browser CLI; 无会话时抛错, 调用方回退到已解析 JSON。"""
    import subprocess
    base = ["agent-browser", "--session", session, "goto",
            "https://user-pc-new.realcpf.com/#/bet_record"]
    subprocess.run(base, check=True)
    # 详见 analysis/extract_all4.js: 点 体育→已结算→30天内→数字页码(1-7) 收集
    raise NotImplementedError("fetch_raw 需 agent-browser 会话; 见 analysis/extract_all4.js")


# ── 主入口 ──
def main():
    settled_raw = json.load(open(SETTLED_PATH, encoding="utf-8"))
    try:
        live_raw = json.load(open(LIVE_PATH, encoding="utf-8"))
    except FileNotFoundError:
        live_raw = []
    settled = [normalize_settled(r) for r in settled_raw]
    live = [normalize_live(r) for r in live_raw]
    stats = closed_loop_stats(settled, live)

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    o = stats["overall_settled"]
    cs = stats["cs_only"]; nc = stats["noncs_only"]
    be = stats["blind_era_before_0811"]; re_ = stats["real_era_0811plus"]
    pd_ = stats["pending_live"]
    print("=" * 60)
    print(" 乐鱼 bet_record 闭环看板")
    print("=" * 60)
    print(f" 已结算体育总单: {o['n']} 本金 {o['stake']:.0f} 净盈亏 {o['pnl']:.1f} "
          f"ROI {o['roi']*100:.1f}% 胜率 {o['win_rate']*100:.1f}%")
    print(f"   ├ CS波胆 : {cs['n']} 本金 {cs['stake']:.0f} 净 {cs['pnl']:.1f} "
          f"ROI {cs['roi']*100:.1f}% 胜 {cs['win_rate']*100:.1f}%")
    print(f"   └ 非CS   : {nc['n']} 本金 {nc['stake']:.0f} 净 {nc['pnl']:.1f} "
          f"ROI {nc['roi']*100:.1f}% 胜 {nc['win_rate']*100:.1f}%")
    print(f" 瞎买期(<0811) : {be['n']} 净 {be['pnl']:.1f} ROI {be['roi']*100:.1f}%")
    print(f" 真实盘口(≥0811): {re_['n']} 净 {re_['pnl']:.1f} ROI {re_['roi']*100:.1f}%")
    print(f" 未结算(今晚{len(live)}笔): 本金 {pd_['stake']:.1f} 待结算")
    print(f"\n ✅ 看板已写 {os.path.relpath(DASHBOARD_PATH, ROOT)}")


if __name__ == "__main__":
    main()
