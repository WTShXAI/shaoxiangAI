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
                pnl REAL, kelly REAL, ev REAL,
                mode TEXT DEFAULT 'real'
            );
            CREATE INDEX IF NOT EXISTS idx_ts ON bets(ts DESC);
        """)
        # 兼容已存在的旧库 (无 mode 列)
        try:
            self.conn.execute("ALTER TABLE bets ADD COLUMN mode TEXT DEFAULT 'real'")
            self.conn.commit()
        except Exception:
            pass
        self.conn.commit()

    def add_bet(self, match="", outcome="", odds=0.0, stake=0.0, result="", pnl=0.0, kelly=0.0, ev=0.0, mode="real") -> int:
        cur = self.conn.execute(
            "INSERT INTO bets (ts,match,outcome,odds,stake,result,pnl,kelly,ev,mode) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), match, outcome, odds, stake, result, pnl, kelly, ev, mode)
        )
        self.conn.commit()
        return cur.lastrowid

    def settle_bet(self, bet_id: int, result: str, pnl: float | None = None) -> bool:
        """结算一笔待确认注 (real 模式落库后调用)。

        result: win / loss / void。pnl 缺省时由存储的 odds/stake 反推:
            win  → stake*(odds-1); loss → -stake; void → 0
        返回是否成功更新。
        """
        row = self.conn.execute(
            "SELECT odds, stake FROM bets WHERE id=?", (bet_id,)
        ).fetchone()
        if not row:
            return False
        odds, stake = row
        if pnl is None:
            if result == "win":
                pnl = stake * (odds - 1)
            elif result == "loss":
                pnl = -stake
            else:
                pnl = 0.0
        self.conn.execute(
            "UPDATE bets SET result=?, pnl=? WHERE id=?",
            (result, round(pnl, 2), bet_id)
        )
        self.conn.commit()
        return True

    def get_bets(self, limit=500, offset=0) -> List[dict]:
        rows = self.conn.execute(
            "SELECT * FROM bets ORDER BY ts DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        cols = ["id","ts","match","outcome","odds","stake","result","pnl","kelly","ev","mode"]
        return [dict(zip(cols, r)) for r in rows]

    def get_equity_curve(self, mode: str | None = None) -> List[dict]:
        sql = (
            "SELECT ts, SUM(pnl) OVER (ORDER BY ts) as equity FROM bets"
            + (" WHERE mode=?" if mode else "")
            + " ORDER BY ts"
        )
        rows = self.conn.execute(sql, (mode,) if mode else ()).fetchall()
        return [{"ts": r[0], "equity": round(r[1], 2)} for r in rows]

    def get_stats(self, mode: str | None = None) -> dict:
        where = " WHERE mode=?" if mode else ""
        params = (mode,) if mode else ()
        row = self.conn.execute(f"""
            SELECT COUNT(*),
                   SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),
                   SUM(pnl), AVG(ev), MAX(ABS(pnl))
            FROM bets{where}
        """, params).fetchone()
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
