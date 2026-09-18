"""结算单实盘 ROI 标定 — 验证 obscure 联赛 edge 假说 (SSoT 分析脚本)

读取 long_images.db 中 page_type='settlement' 的截图 parsed_json,
仅用「已结算」单(赢/输)计算真实 ROI, 排除「未结算/预约中」。

约定
----
parsed_json.stake  = 投注本金
parsed_json.payout = 派彩总额(本金+盈利), 赢单有值, 输单为 None
净盈利 = payout - stake  (赢);  = -stake  (输)
ROI    = Σ净盈利 / Σ本金

这是唯一来自用户真实资金的 obscured 联赛 ROI 样本, 用于对标
pipeline/evaluation 的模型 ROI —— 真 edge 须在此类实盘样本下 ROI>0。

用法: python scripts/settlement_roi.py
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path

DB_PATH = Path("data/long_images.db")


@dataclass
class Bet:
    stake: float
    payout: float          # 总额(本金+盈利)
    net: float
    win: bool
    label: str             # 联赛/赛事(尽量还原)


def compute(db: Path = DB_PATH) -> dict:
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT parsed_json FROM images WHERE page_type='settlement'"
    ).fetchall()
    conn.close()

    resolved: list[Bet] = []
    pending: list[tuple[float, str]] = []
    for (pj,) in rows:
        p = json.loads(pj) if pj else {}
        stake = p.get("stake")
        if stake is None:
            continue
        try:
            stake = float(stake)
        except (TypeError, ValueError):
            continue
        wl = p.get("win_loss") or ""
        home = p.get("home_team") or ""
        away = p.get("away_team") or ""
        # 待结算识别: v1 解析器可能把"未结算/预约中"误标为"输"
        if "未结算" in (home, away, wl) or "预约中" in (home, away, wl):
            pending.append((stake, "未结算/预约中"))
            continue
        payout = p.get("payout")
        if wl == "赢":
            try:
                po = float(payout) if payout not in (None, "") else 0.0
            except (TypeError, ValueError):
                po = 0.0
            resolved.append(Bet(stake, po, po - stake, True,
                                 f"{home} vs {away}"))
        elif wl == "输":
            resolved.append(Bet(stake, 0.0, -stake, False,
                                 f"{home} vs {away}"))
        else:
            pending.append((stake, wl or "未知"))

    tot_stake = sum(b.stake for b in resolved)
    tot_payout = sum(b.payout for b in resolved)
    tot_net = sum(b.net for b in resolved)
    n_win = sum(1 for b in resolved if b.win)
    n_loss = len(resolved) - n_win
    roi = (tot_net / tot_stake) if tot_stake else 0.0
    hit = (n_win / len(resolved)) if resolved else 0.0

    return {
        "n_resolved": len(resolved),
        "n_win": n_win,
        "n_loss": n_loss,
        "n_pending_excluded": len(pending),
        "total_stake": round(tot_stake, 2),
        "total_payout": round(tot_payout, 2),
        "total_net": round(tot_net, 2),
        "roi_pct": round(roi * 100, 2),
        "hit_rate_pct": round(hit * 100, 2),
        "bets": [asdict(b) for b in resolved],
        "pending_sample": pending[:12],
    }


def _console(r: dict) -> str:
    L = []
    L.append("结算单实盘 ROI 标定 (obscure 联赛, 仅已结算单)")
    L.append("=" * 64)
    L.append(f"已结算: {r['n_resolved']} 单 (赢 {r['n_win']} / 输 {r['n_loss']})  "
             f"| 排除未结算: {r['n_pending_excluded']} 单")
    L.append(f"总本金: {r['total_stake']}  总派彩: {r['total_payout']}  净盈利: {r['total_net']}")
    L.append(f"**实盘 ROI = {r['roi_pct']:+}%**   命中率 = {r['hit_rate_pct']}%")
    L.append("-" * 64)
    for b in r["bets"]:
        tag = "赢" if b["win"] else "输"
        L.append(f"  [{tag}] stake={b['stake']:>7.2f} payout={b['payout']:>7.2f} "
                 f"net={b['net']:>+7.2f}  {b['label']}")
    return "\n".join(L)


if __name__ == "__main__":
    r = compute()
    print(_console(r))
    out = Path("data/settlement_roi_report.json")
    out.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {out}")
