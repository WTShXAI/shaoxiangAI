"""leisu_store.py — 微瑞/乐鱼平台 6 大市场赔率快照存储 + 水位检测

数据模型:
  - snapshots: 每 60s 一份完整赔率快照
  - movements: 前后两次快照差值 (水位信号)

水位信号 = (|ΔH|/H, |ΔA|/A, |ΔOver|/Over) > 阈值 触发
  - 跌水 (down) = 主队赔率下跌 → 资金涌入主队
  - 升水 (up)   = 主队赔率上升 → 资金撤出主队
"""
import os, time, sqlite3, math
from typing import Optional, List, Dict, Any

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "leisu_odds.db")

# 水位检测阈值 (绝对变化)
WATER_THRESH = {
    "odds_h": 0.10,      # 主胜 1X2 跌 0.10 → 强信号
    "odds_d": 0.20,
    "odds_a": 0.10,
    "ah_home": 0.08,     # 主队让球
    "ah_away": 0.08,
    "ou_over": 0.08,
    "ou_under": 0.08,
    "h1_odds_h": 0.15,
    "h1_odds_d": 0.20,
    "h1_odds_a": 0.15,
    "h_ah_home": 0.10,
    "h_ah_away": 0.10,
    "h_ou_over": 0.10,
    "h_ou_under": 0.10,
}


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    """建表 (幂等)。"""
    c = _conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mid TEXT NOT NULL,
            league TEXT,
            home TEXT,
            away TEXT,
            commence_ms INTEGER,
            match_state TEXT,
            snapshot_at INTEGER NOT NULL,
            -- 1X2 全场
            odds_h REAL, odds_d REAL, odds_a REAL,
            -- 全场让球
            ah_line TEXT, ah_home REAL, ah_away REAL,
            -- 全场大小
            ou_line TEXT, ou_over REAL, ou_under REAL,
            -- 半场 1X2
            h1_odds_h REAL, h1_odds_d REAL, h1_odds_a REAL,
            -- 半场让球
            h_ah_line TEXT, h_ah_home REAL, h_ah_away REAL,
            -- 半场大小
            h_ou_line TEXT, h_ou_over REAL, h_ou_under REAL,
            UNIQUE(mid, snapshot_at)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_snap_mid ON odds_snapshots(mid, snapshot_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_snap_time ON odds_snapshots(snapshot_at DESC)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS water_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mid TEXT NOT NULL,
            market TEXT NOT NULL,
            direction TEXT NOT NULL,
            delta REAL,
            delta_pct REAL,
            old_val REAL,
            new_val REAL,
            detected_at INTEGER NOT NULL,
            UNIQUE(mid, market, detected_at)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_water_time ON water_signals(detected_at DESC)")
    c.commit()
    c.close()


def _to_ms(iso_or_ms):
    if isinstance(iso_or_ms, (int, float)):
        return int(iso_or_ms)
    if not iso_or_ms:
        return None
    try:
        from datetime import datetime
        if iso_or_ms.endswith('Z'):
            d = datetime.strptime(iso_or_ms.replace('Z', '+00:00'), "%Y-%m-%dT%H:%M:%S+00:00")
        else:
            d = datetime.fromisoformat(iso_or_ms)
        return int(d.timestamp() * 1000)
    except Exception:
        return None


def save_snapshot(m: dict) -> Optional[int]:
    """存一场比赛的赔率快照。返回 snapshot_id, 若 mid 缺失则 None。"""
    mid = m.get("id")
    if not mid:
        return None
    now = int(time.time())
    c = _conn()
    try:
        cur = c.execute("""
            INSERT OR IGNORE INTO odds_snapshots
            (mid, league, home, away, commence_ms, match_state, snapshot_at,
             odds_h, odds_d, odds_a, ah_line, ah_home, ah_away,
             ou_line, ou_over, ou_under,
             h1_odds_h, h1_odds_d, h1_odds_a, h_ah_line, h_ah_home, h_ah_away,
             h_ou_line, h_ou_over, h_ou_under)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mid, m.get("league", ""), m.get("home", ""), m.get("away", ""),
            _to_ms(m.get("commence_time")), str(m.get("match_state", "")),
            now,
            m.get("odds_h"), m.get("odds_d"), m.get("odds_a"),
            m.get("ah_line"), m.get("ah_home"), m.get("ah_away"),
            m.get("ou_line"), m.get("ou_over"), m.get("ou_under"),
            m.get("h1_odds_h"), m.get("h1_odds_d"), m.get("h1_odds_a"),
            m.get("h_ah_line"), m.get("h_ah_home"), m.get("h_ah_away"),
            m.get("h_ou_line"), m.get("h_ou_over"), m.get("h_ou_under"),
        ))
        c.commit()
        return cur.lastrowid or None
    finally:
        c.close()


def get_last_snapshot(mid: str) -> Optional[Dict[str, Any]]:
    """取该 mid 最近一次快照 (用于水位比较)。"""
    c = _conn()
    try:
        cur = c.execute("""
            SELECT * FROM odds_snapshots WHERE mid=? ORDER BY snapshot_at DESC LIMIT 1
        """, (mid,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    finally:
        c.close()


def detect_water_movement(current: dict) -> List[Dict[str, Any]]:
    """比对 current vs 上次快照, 生成水位信号列表。"""
    mid = current.get("id")
    if not mid:
        return []
    last = get_last_snapshot(mid)
    if not last:
        return []  # 第一次没东西可比
    signals = []
    now = int(time.time())
    for key, thresh in WATER_THRESH.items():
        new = current.get(key)
        old = last.get(key)
        if new is None or old is None:
            continue
        try:
            new = float(new)
            old = float(old)
        except (TypeError, ValueError):
            continue
        if old <= 0 or abs(new - old) < thresh:
            continue
        delta = new - old
        delta_pct = (delta / old) * 100
        direction = "down" if delta < 0 else "up"
        signals.append({
            "mid": mid,
            "market": key,
            "direction": direction,
            "delta": round(delta, 4),
            "delta_pct": round(delta_pct, 2),
            "old_val": old,
            "new_val": new,
            "detected_at": now,
            "home": current.get("home", ""),
            "away": current.get("away", ""),
            "league": current.get("league", ""),
        })
    return signals


def save_signals(signals: List[Dict[str, Any]]) -> int:
    """批量写入水位信号表 (去重 INSERT OR IGNORE)。"""
    if not signals:
        return 0
    c = _conn()
    try:
        rows = [(
            s["mid"], s["market"], s["direction"],
            s["delta"], s["delta_pct"], s["old_val"], s["new_val"],
            s["detected_at"],
        ) for s in signals]
        c.executemany("""
            INSERT OR IGNORE INTO water_signals
            (mid, market, direction, delta, delta_pct, old_val, new_val, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        c.commit()
        return len(rows)
    finally:
        c.close()


def get_recent_signals(limit: int = 50, min_delta_pct: float = 1.0) -> List[Dict[str, Any]]:
    """取最近水位信号, 按 |delta_pct| 倒序。"""
    c = _conn()
    try:
        rows = c.execute("""
            SELECT mid, market, direction, delta, delta_pct, old_val, new_val, detected_at
            FROM water_signals
            WHERE detected_at > ?
            ORDER BY ABS(delta_pct) DESC, detected_at DESC
            LIMIT ?
        """, (int(time.time()) - 24 * 3600, limit)).fetchall()
        out = []
        for r in rows:
            out.append({
                "mid": r[0], "market": r[1], "direction": r[2],
                "delta": r[3], "delta_pct": r[4], "old_val": r[5],
                "new_val": r[6], "detected_at": r[7],
            })
        return out
    finally:
        c.close()


def store_and_detect(matches: List[dict]) -> Dict[str, int]:
    """批量存快照 + 检测水位信号。返回统计。"""
    init_db()
    saved = 0
    signals_count = 0
    all_signals = []
    for m in matches:
        if save_snapshot(m):
            saved += 1
        sigs = detect_water_movement(m)
        all_signals.extend(sigs)
    if all_signals:
        signals_count = save_signals(all_signals)
    return {"snapshots_saved": saved, "signals_new": signals_count, "signals_total": len(all_signals)}


if __name__ == "__main__":
    init_db()
    print(f"DB: {DB_PATH}")
    print(f"snapshots: {sqlite3.connect(DB_PATH).execute('SELECT COUNT(*) FROM odds_snapshots').fetchone()[0]}")
    print(f"signals:   {sqlite3.connect(DB_PATH).execute('SELECT COUNT(*) FROM water_signals').fetchone()[0]}")
