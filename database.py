"""SQLite 投注数据库 — 资金曲线 + 风控 + 报表"""
from __future__ import annotations
import sqlite3, os, time, csv
from typing import List

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bets.db")

class Database:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, match TEXT, outcome TEXT,
                odds REAL, stake REAL, result TEXT,
                pnl REAL, kelly REAL, ev REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ts ON bets(ts DESC);
        """)
        self.conn.commit()

    def add_bet(self, match="", outcome="", odds=0.0, stake=0.0, result="", pnl=0.0, kelly=0.0, ev=0.0) -> int:
        cur = self.conn.execute(
            "INSERT INTO bets (ts,match,outcome,odds,stake,result,pnl,kelly,ev) VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), match, outcome, odds, stake, result, pnl, kelly, ev)
        )
        self.conn.commit()
        return cur.lastrowid

    def get_bets(self, limit=500, offset=0) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM bets ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        cols = ["id","ts","match","outcome","odds","stake","result","pnl","kelly","ev"]
        return [dict(zip(cols, r)) for r in rows]

    def get_equity_curve(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT ts, SUM(pnl) OVER (ORDER BY ts) as equity FROM bets ORDER BY ts"
        ).fetchall()
        return [{"ts": r[0], "equity": round(r[1], 2)} for r in rows]

    def get_stats(self) -> dict:
        row = self.conn.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),
                   SUM(pnl), AVG(ev), MAX(ABS(pnl))
            FROM bets
        """).fetchone()
        total = row[0] or 0
        return {
            "total_bets": total,
            "wins": row[1] or 0,
            "losses": row[2] or 0,
            "win_rate": round((row[1] or 0) / total * 100, 1) if total else 0,
            "total_pnl": round(row[3] or 0, 2),
            "avg_ev": round(row[4] or 0, 2),
            "max_single_pnl": round(row[5] or 0, 2),
        }

    def export_csv(self, path: str):
        bets = self.get_bets(limit=10000)
        if not bets:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(bets[0].keys()))
            w.writeheader()
            w.writerows(bets)

    # ── 风控 ──
    def max_drawdown(self) -> float:
        cur = self.conn.execute(
            "SELECT pnl FROM bets ORDER BY ts"
        ).fetchall()
        peak = cum = dd = 0.0
        for (pnl,) in cur:
            cum += pnl
            if cum > peak: peak = cum
            if peak > 0:
                d = (peak - cum) / peak
                if d > dd: dd = d
        return dd

    def lost_streak(self) -> int:
        rows = self.conn.execute(
            "SELECT result FROM bets ORDER BY ts DESC"
        ).fetchall()
        streak = 0
        for (r,) in rows:
            if r == 'loss':
                streak += 1
            else:
                break
        return streak

    def equity(self) -> float:
        row = self.conn.execute("SELECT SUM(pnl) FROM bets").fetchone()
        return round(row[0] or 0, 2)


db = Database()
