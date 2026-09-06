"""leisu_live_scores.py — 实时比分监控 (从 feed 中过滤 + 单独抓 getMatchDetailPB)

数据源:
  - 主路径: 从 leisu_live.build_feed() 已抓的 odds 列表里过滤 mststi!=0 的比赛
  - 增强路径: 对正在进行的比赛, 主动调 getMatchDetailPB?mid=... 拿详细比分/事件

状态映射:
  mststi: 0=未开赛, 1=第一节/上半场, 2=中场, 3=下半场, 4=加时, 5=点球, -1=已结束
"""
import os, time, json, base64, gzip, zlib, sqlite3, threading
from typing import List, Dict, Any, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "leisu_odds.db")


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_scores_db():
    c = _conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS live_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mid TEXT NOT NULL,
            home TEXT,
            away TEXT,
            league TEXT,
            mststi INTEGER,
            score_home INTEGER,
            score_away INTEGER,
            match_minute TEXT,
            mlet TEXT,
            events_json TEXT,
            snapshot_at INTEGER NOT NULL,
            UNIQUE(mid, snapshot_at)
        )
    """)
    # 实时赔率列 (1X2 / 大小 / 让球) — 旧表用 ALTER 补全, 不重建以免丢数据
    _odds_cols = [
        ("odds_h", "REAL"), ("odds_d", "REAL"), ("odds_a", "REAL"),
        ("ou_line", "REAL"), ("ou_over", "REAL"), ("ou_under", "REAL"),
        ("ah_line", "REAL"), ("ah_home", "REAL"), ("ah_away", "REAL"),
    ]
    existing = {r[1] for r in c.execute("PRAGMA table_info(live_scores)")}
    for col, typ in _odds_cols:
        if col not in existing:
            try:
                c.execute(f"ALTER TABLE live_scores ADD COLUMN {col} {typ}")
            except Exception:
                pass
    c.execute("CREATE INDEX IF NOT EXISTS idx_lscore_time ON live_scores(snapshot_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_lscore_mid ON live_scores(mid, snapshot_at DESC)")
    c.commit()
    c.close()


def _fnum(v):
    """转 float 或 None (赔率字段可能缺失/非数字)。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def save_live_score(m: dict):
    """存一场比赛的实时比分 (mststi != 0 时存)。"""
    mid = str(m.get("id") or m.get("mid") or "")
    if not mid:
        return False
    mststi = m.get("match_state") or m.get("mststi")
    try:
        mststi_int = int(mststi) if mststi is not None else 0
    except (TypeError, ValueError):
        mststi_int = 0
    if mststi_int == 0:
        return False  # 未开赛, 不存

    now = int(time.time())
    events = m.get("msc") or m.get("events") or []
    c = _conn()
    try:
        c.execute("""
            INSERT OR IGNORE INTO live_scores
            (mid, home, away, league, mststi, score_home, score_away, match_minute, mlet, events_json, snapshot_at,
             odds_h, odds_d, odds_a, ou_line, ou_over, ou_under, ah_line, ah_home, ah_away)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mid,
            m.get("home") or m.get("mhn", ""),
            m.get("away") or m.get("man", ""),
            m.get("league", ""),
            mststi_int,
            m.get("score_home") if m.get("score_home") is not None else m.get("mhs"),
            m.get("score_away") if m.get("score_away") is not None else m.get("mas"),
            str(m.get("match_minute") or m.get("mprmc") or ""),
            str(m.get("mlet") or ""),
            json.dumps(events, ensure_ascii=False) if events else None,
            now,
            _fnum(m.get("odds_h")), _fnum(m.get("odds_d")), _fnum(m.get("odds_a")),
            _fnum(m.get("ou_line")), _fnum(m.get("ou_over")), _fnum(m.get("ou_under")),
            _fnum(m.get("ah_line")), _fnum(m.get("ah_home")), _fnum(m.get("ah_away")),
        ))
        c.commit()
        return True
    finally:
        c.close()


def is_live(m: dict) -> bool:
    """判断一场比赛是否正在进行。"""
    mststi = m.get("match_state") or m.get("mststi")
    try:
        return int(mststi) > 0 if mststi is not None else False
    except (TypeError, ValueError):
        return False


def filter_live_matches(matches: List[dict]) -> List[dict]:
    """从 feed 的比赛列表里过滤正在进行的。"""
    return [m for m in matches if is_live(m)]


def get_live_matches(limit: int = 50) -> List[dict]:
    """从 DB 拉最近更新的正在进行比赛 (每 mid 取最新快照, 含实时赔率)。"""
    c = _conn()
    try:
        rows = c.execute("""
            SELECT mid, home, away, league, mststi, score_home, score_away, match_minute, mlet,
                   events_json, snapshot_at,
                   odds_h, odds_d, odds_a, ou_line, ou_over, ou_under, ah_line, ah_home, ah_away
            FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY mid ORDER BY snapshot_at DESC) as rn
                FROM live_scores
                WHERE snapshot_at > ?
            ) WHERE rn = 1
            ORDER BY snapshot_at DESC
            LIMIT ?
        """, (int(time.time()) - 180, limit)).fetchall()
        out = []
        for r in rows:
            ev = []
            if r[9]:
                try: ev = json.loads(r[9])
                except: pass
            out.append({
                "mid": r[0], "home": r[1], "away": r[2], "league": r[3],
                "mststi": r[4], "match_state": r[4], "score_home": r[5], "score_away": r[6],
                "match_minute": r[7], "mlet": r[8],
                "events": ev[:5],  # 只取最近 5 个事件
                "snapshot_at": r[10],
                "is_live": (r[4] or 0) > 0,
                # 实时赔率 (可能为 None)
                "odds_h": r[11], "odds_d": r[12], "odds_a": r[13],
                "ou_line": r[14], "ou_over": r[15], "ou_under": r[16],
                "ah_line": r[17], "ah_home": r[18], "ah_away": r[19],
            })
        return out
    finally:
        c.close()


def get_match_score_history(mid: str, limit: int = 60) -> List[dict]:
    """某 mid 的比分时序 (用于动态折线图)。"""
    c = _conn()
    try:
        rows = c.execute("""
            SELECT snapshot_at, score_home, score_away, match_minute, mststi
            FROM live_scores WHERE mid=?
            ORDER BY snapshot_at DESC LIMIT ?
        """, (mid, limit)).fetchall()
        return [{
            "ts": r[0], "score_home": r[1], "score_away": r[2],
            "match_minute": r[3], "mststi": r[4],
        } for r in rows]
    finally:
        c.close()


# ═══ 后台轮询线程 (每 30s 拉一次 getMatchDetailPB) ═══

def _poll_live_details(mids: List[str]):
    """对正在进行的 mid 主动拉 getMatchDetailPB, 写入 DB。"""
    from playwright.sync_api import sync_playwright
    if not mids:
        return

    captured = {}

    def on_response(resp):
        if "getMatchDetailPB" in resp.url:
            try:
                t = resp.text()
                obj = json.loads(t)
                if obj.get("code") != "0000000":
                    return
                raw = base64.b64decode(obj.get("data", ""))
                dec = None
                for fn in (lambda: gzip.decompress(raw),
                           lambda: zlib.decompress(raw, -zlib.MAX_WBITS),
                           lambda: zlib.decompress(raw)):
                    try:
                        dec = json.loads(fn().decode("utf-8"))
                        break
                    except Exception:
                        continue
                if dec and isinstance(dec, dict):
                    mid = str(dec.get("mid", ""))
                    if mid:
                        captured[mid] = dec
            except Exception:
                pass

    # 复用 leisu_live 的 CHROME_PATH 与动态 deep_link(session/token 轮换时同步)
    from pipeline.collectors.leisu_live import _load_session, CHROME_PATH

    deep_link = _load_session().get("deep_link")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME_PATH, headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-gpu", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            ignore_https_errors=True)
        page = ctx.new_page()
        page.on("response", on_response)
        page.goto(deep_link, wait_until="networkidle", timeout=60000)
        # 等所有 detail 调用
        for _ in range(10):
            if all(m in captured for m in mids):
                break
            time.sleep(1.0)
        browser.close()

    for mid in mids:
        if mid in captured:
            from pipeline.collectors.leisu_live import normalize_match
            norm = normalize_match(captured[mid], captured[mid])
            save_live_score(norm)


def background_live_poller():
    """后台线程, 每 30s 轮询正在进行的比赛。"""
    init_scores_db()
    while True:
        try:
            live = get_live_matches(limit=20)
            if live:
                mids = [m["mid"] for m in live if m.get("mid")]
                if mids:
                    _poll_live_details(mids[:5])  # 一次最多 5 场防超时
        except Exception as e:
            print(f"[live_poller] err: {e}", flush=True)
        time.sleep(120)  # 修2026-08-06: 30s→120s降频, 减少与analyze争抢资源


def start_background_poller():
    t = threading.Thread(target=background_live_poller, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    init_scores_db()
    print(f"DB: {DB_PATH}")
    print(f"live_scores: {sqlite3.connect(DB_PATH).execute('SELECT COUNT(*) FROM live_scores').fetchone()[0]}")
