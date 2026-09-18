"""跨庄软线实时告警服务 — 哨响AI 真 edge 信号落地层 (SSoT)

建立在 pipeline.cross_book_edge 之上:
  - 检测引擎产出每场软线 (单庄隐含概率 vs 跨庄共识偏离 > 阈值)
  - 本模块把软线转为「告警」: 分级 + 持久化 + 实时监测 + 可选 GQ 实盘交叉参考

实时能力
--------
  --once   单次检测并落库 (适合 cron / 手动触发)
  --watch  轮询监测: 监听 long_images.db.cross_book_odds 的新增 (由 long_images_v2.py
            持续摄入用户截图驱动), 仅对「新出现」的软线发告警 → 准实时信号通道

GQ 实盘交叉参考 (诚实集成)
--------------------------
  GQ 为单源 feed (乐鱼), 本身不足以构成跨庄; 但本模块对「同时出现在 cross_book_odds
  与 GQ 实时赔率」的赛事, 把 GQ 去水概率与跨庄共识对比, 增补「sharp-vs-consensus」
  告警。当前赛事日期不重合 → 自然返回 0, 不虚构信号。

告警分级
--------
  HIGH   : 偏离 >= 15pp
  MEDIUM : 偏离 >= 10pp
  LOW    : 偏离 >= 5pp  (默认阈值, 同 cross_book_edge)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from pipeline.cross_book_edge import (
    analyze_all, to_report, devig, DEFAULT_SOFT_LINE_PP,
)

DB_PATH = Path("data/long_images.db")
GQ_DB_PATH = Path("data/events.db")
ALERTS_JSON = Path("data/cross_book_alerts.json")
ALERTS_MD = Path("data/cross_book_alerts.md")

# 分级阈值 (pp = 单庄概率与共识差, 百分点)
SEV_HIGH = 15.0
SEV_MEDIUM = 10.0
# LOW 阈值 = soft_pp (默认 5.0)


def severity_of(pp: float, soft_pp: float = DEFAULT_SOFT_LINE_PP) -> str | None:
    if pp >= SEV_HIGH:
        return "HIGH"
    if pp >= SEV_MEDIUM:
        return "MEDIUM"
    if pp >= soft_pp:
        return "LOW"
    return None


def build_alerts(report: dict, soft_pp: float = DEFAULT_SOFT_LINE_PP,
                 source: str = "cross_book") -> list[dict]:
    """从 to_report 的软线列表构建告警记录 (按分级过滤)"""
    alerts: list[dict] = []
    for m in report["matches"]:
        if not m["soft_lines"]:
            continue
        mk = f"{m['league']}|{m['home']}|{m['away']}"
        for s in m["soft_lines"]:
            sev = severity_of(s["pp"], soft_pp)
            if sev is None:
                continue
            sel = s["sel"]  # H / D / A
            best = m["best"][sel.lower()]
            alerts.append({
                "match_key": mk,
                "league": m["league"], "home": m["home"], "away": m["away"],
                "selection": sel,
                "book": s["book"],
                "deviation_pp": s["pp"],
                "book_prob": s["prob"], "consensus_prob": s["consensus"],
                "best_odds": best["odds"], "best_bookmaker": best["bookmaker"],
                "severity": sev,
                "source": source,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            })
    return alerts


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cross_book_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_key TEXT, league TEXT, home TEXT, away TEXT,
            selection TEXT, book TEXT, deviation_pp REAL,
            book_prob REAL, consensus_prob REAL,
            best_odds REAL, best_bookmaker TEXT,
            severity TEXT, source TEXT, created_at TEXT, seen INTEGER DEFAULT 0,
            UNIQUE(match_key, selection, book, source)
        )"""
    )
    conn.commit()


def persist_alerts(conn: sqlite3.Connection, alerts: list[dict]) -> tuple[int, list[dict]]:
    """幂等写入; 返回 (新增条数, 新增告警列表) (UNIQUE 约束去重, 重启安全)"""
    ensure_table(conn)
    new = 0
    inserted: list[dict] = []
    for a in alerts:
        cur = conn.execute(
            """INSERT OR IGNORE INTO cross_book_alerts(
                match_key, league, home, away, selection, book, deviation_pp,
                book_prob, consensus_prob, best_odds, best_bookmaker,
                severity, source, created_at, seen)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (a["match_key"], a["league"], a["home"], a["away"], a["selection"],
             a["book"], a["deviation_pp"], a["book_prob"], a["consensus_prob"],
             a["best_odds"], a["best_bookmaker"], a["severity"], a["source"],
             a["created_at"]),
        )
        if cur.rowcount == 1:
            new += 1
            inserted.append(a)
    conn.commit()
    return new, inserted


def cross_ref_gq(report: dict, soft_pp: float = DEFAULT_SOFT_LINE_PP) -> tuple[list[dict], int]:
    """诚实的 GQ 实盘交叉参考。

    GQ 为单源(乐鱼), 不足以独立构成跨庄; 仅对「同时出现在 cross_book_odds 与 GQ
    实时赔率」的赛事, 把 GQ 去水概率与跨庄共识对比, 增补告警。当前赛事日期不重合
    → 自然返回空, 不虚构信号。任何结构漂移都被 try/except 吞掉, 不影响主流程。
    """
    if not GQ_DB_PATH.exists():
        return [], 0
    out: list[dict] = []
    try:
        gq = sqlite3.connect(str(GQ_DB_PATH))
        cons = {f"{m['league']}|{m['home']}|{m['away']}": m["consensus"]
                for m in report["matches"]}
        pairs = {(m["home"], m["away"], m["league"]) for m in report["matches"]}
        sel_map = {"home": "H", "draw": "D", "away": "A"}
        for home, away, league in pairs:
            row = gq.execute(
                "SELECT match_key FROM matches WHERE home=? AND away=?",
                (home, away)).fetchone()
            if not row:
                continue
            mk_gq = row[0]
            probs: dict[str, float] = {}
            for sel in ("home", "draw", "away"):
                r = gq.execute(
                    "SELECT odds FROM odds_snapshots WHERE match_key=? "
                    "AND market='1X2' AND selection=? ORDER BY captured_at DESC LIMIT 1",
                    (mk_gq, sel)).fetchone()
                if r:
                    probs[sel_map[sel]] = float(r[0])
            if len(probs) != 3:
                continue
            dv = devig(probs["H"], probs["D"], probs["A"])
            if dv is None:
                continue
            ph, pd, pa = dv
            gq_cons = cons.get(f"{league}|{home}|{away}")
            if not gq_cons:
                continue
            for sel, gp in (("H", ph), ("D", pd), ("A", pa)):
                diff = abs(gp - gq_cons[sel.lower()]) * 100.0
                sev = severity_of(diff, soft_pp)
                if sev is None:
                    continue
                out.append({
                    "match_key": f"{league}|{home}|{away}",
                    "league": league, "home": home, "away": away,
                    "selection": sel,
                    "book": "GQ(乐鱼)",
                    "deviation_pp": round(diff, 2),
                    "book_prob": round(gp, 4),
                    "consensus_prob": gq_cons[sel.lower()],
                    "best_odds": round(probs[sel], 3),
                    "best_bookmaker": "GQ(乐鱼)",
                    "severity": sev,
                    "source": "gq_cross_ref",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                })
        gq.close()
    except Exception:
        return [], 0
    return out, len(out)


def run_once(db: Path = DB_PATH, soft_pp: float = DEFAULT_SOFT_LINE_PP) -> dict:
    """单次检测 → 构建告警 → 持久化 → 写报告。返回统计。"""
    edges = analyze_all(db, soft_pp)
    report = to_report(edges, soft_pp)
    alerts = build_alerts(report, soft_pp, source="cross_book")
    gq_alerts, gq_n = cross_ref_gq(report, soft_pp)
    all_alerts = alerts + gq_alerts

    conn = sqlite3.connect(str(db))
    new, inserted = persist_alerts(conn, all_alerts)
    total = conn.execute("SELECT COUNT(*) FROM cross_book_alerts").fetchone()[0]
    by_sev = {s: conn.execute(
        "SELECT COUNT(*) FROM cross_book_alerts WHERE severity=?", (s,)).fetchone()[0]
        for s in ("HIGH", "MEDIUM", "LOW")}
    conn.close()

    # 报告
    out = {
        "module": "cross_book_alert",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "soft_line_threshold_pp": soft_pp,
        "matches_analyzed": report["n_matches"],
        "matches_with_soft_lines": report["n_matches_with_soft_lines"],
        "gq_cross_ref_matches": gq_n,
        "alerts_new_this_run": new,
        "alerts_total_in_db": total,
        "alerts_by_severity": by_sev,
        "alerts_new": inserted,
        "alerts": sorted(all_alerts, key=lambda a: -a["deviation_pp"]),
    }
    ALERTS_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_md(out)
    return out


def _write_md(out: dict) -> None:
    lines = []
    lines.append(f"# 跨庄软线告警 — {out['generated_at']}")
    lines.append("")
    lines.append(f"- 分析赛事: {out['matches_analyzed']} 场 (软线 {out['matches_with_soft_lines']} 场)")
    lines.append(f"- GQ 实盘交叉参考匹配: {out['gq_cross_ref_matches']} 场")
    lines.append(f"- 本次新增告警: {out['alerts_new_this_run']} | 库内累计: {out['alerts_total_in_db']}")
    lines.append(f"- 分级: HIGH={out['alerts_by_severity']['HIGH']} "
                 f"MEDIUM={out['alerts_by_severity']['MEDIUM']} "
                 f"LOW={out['alerts_by_severity']['LOW']}")
    lines.append("")
    lines.append("## 告警明细")
    if not out["alerts"]:
        lines.append("(无达到阈值的软线)")
    for a in out["alerts"]:
        lines.append(
            f"- **[{a['severity']}]** {a['league']} {a['home']} vs {a['away']} "
            f"| {a['selection']} | {a['book']} 偏离 {a['deviation_pp']}pp "
            f"({a['book_prob']*100:.1f}% vs 共识 {a['consensus_prob']*100:.1f}%) "
            f"| 最佳价 {a['best_odds']}@{a['best_bookmaker']} | 来源 {a['source']}"
        )
    ALERTS_MD.write_text("\n".join(lines), encoding="utf-8")


def watch(db: Path = DB_PATH, interval: int = 60, max_iter: int = 0,
          soft_pp: float = DEFAULT_SOFT_LINE_PP) -> None:
    """轮询监测: 每 interval 秒检测一次, 仅播报新增告警。max_iter=0 表示永久。"""
    print(f"[watch] 启动 — 间隔 {interval}s, 监听 {db} 的 cross_book_odds 新增 "
          f"(Ctrl+C 退出)", flush=True)
    it = 0
    try:
        while True:
            it += 1
            out = run_once(db, soft_pp)
            if out["alerts_new_this_run"] > 0:
                print(f"[watch] 第{it}轮 新增 {out['alerts_new_this_run']} 条告警:",
                      flush=True)
                for a in out["alerts_new"]:
                    print(f"   ⚠ [{a['severity']}] {a['league']} {a['home']} vs "
                          f"{a['away']} {a['selection']} {a['book']} "
                          f"偏离{a['deviation_pp']}pp", flush=True)
            if max_iter and it >= max_iter:
                print(f"[watch] 已达 max_iter={max_iter}, 退出", flush=True)
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("[watch] 收到中断, 退出", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="跨庄软线实时告警服务")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--soft-pp", type=float, default=DEFAULT_SOFT_LINE_PP)
    ap.add_argument("--once", action="store_true", help="单次检测并落库 (默认)")
    ap.add_argument("--watch", action="store_true", help="轮询监测模式")
    ap.add_argument("--interval", type=int, default=60, help="--watch 轮询间隔秒")
    ap.add_argument("--max-iter", type=int, default=0,
                    help="--watch 最大轮数 (0=永久, 供测试用)")
    args = ap.parse_args()

    if args.watch:
        watch(args.db, args.interval, args.max_iter, args.soft_pp)
    else:
        out = run_once(args.db, args.soft_pp)
        print(f"跨庄告警 — 分析 {out['matches_analyzed']} 场, "
              f"软线 {out['matches_with_soft_lines']} 场")
        print(f"本次新增告警 {out['alerts_new_this_run']} 条 | 库内累计 "
              f"{out['alerts_total_in_db']} 条 "
              f"(HIGH={out['alerts_by_severity']['HIGH']} "
              f"MEDIUM={out['alerts_by_severity']['MEDIUM']} "
              f"LOW={out['alerts_by_severity']['LOW']})")
        print(f"GQ 交叉参考匹配 {out['gq_cross_ref_matches']} 场")
        print(f"已写出 {ALERTS_JSON}")


if __name__ == "__main__":
    main()
