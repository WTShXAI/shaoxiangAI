# -*- coding: utf-8 -*-
"""
track_live_ou_rolling.py  (2026-08-26)
=====================================
对某场滚球按固定间隔读取最新 OU 盘口状态, 喂 live_ou_model 输出 P(大),
追加写入 rolling 日志。用于"后半场每5分钟滚一次 P(大)"的实时跟踪。

用法:
  python scripts/track_live_ou_rolling.py            # 每5分钟滚一次, 直到终场或达上限
  python scripts/track_live_ou_rolling.py --once     # 只取当前一次读数(验证用)
"""
import os, json, time, sqlite3, argparse, sys
import numpy as np
import joblib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis.live_rollball_features import build_ou_features

ROOT = r"D:\Architecture"
GQ = os.path.join(ROOT, "data", "events.db")
MODEL = os.path.join(ROOT, "data", "live_ou_model.joblib")
MATCH = "索尔海岸 vs 黑牛队"
LINE = 2.0
INTERVAL = 300          # 5 分钟
MAX_ITERS = 18          # ~90 分钟上限(防僵死)

model = joblib.load(MODEL)


def parse_score(s):
    if not s:
        return (0, 0)
    s = str(s).strip()
    for sep in ["-", ":"]:
        if sep in s:
            try:
                a, b = s.split(sep)
                return (int(a), int(b))
            except Exception:
                continue
    return (0, 0)


def latest_ou_state(match_key, line):
    c = sqlite3.connect(GQ, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    q = """
    WITH last AS (
      SELECT market, selection, MAX(id) AS maxid
      FROM odds_snapshots WHERE minute_at>0 AND match_key=? AND line=?
      GROUP BY market, selection
    )
    SELECT o.selection, o.odds, o.minute_at, o.score_at
    FROM odds_snapshots o JOIN last l ON o.id=l.maxid
    """
    rows = c.execute(q, (match_key, line)).fetchall()
    c.close()
    st = {}
    for sel, odds, min_, score in rows:
        st[sel] = (odds, min_, score)
    return st


def finished(match_key):
    h, a = match_key.split(" vs ", 1)
    c = sqlite3.connect(GQ, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    row = c.execute(
        "SELECT result, score_home, score_away FROM match_outcomes "
        "WHERE home=? AND away=? AND result IS NOT NULL", (h, a)).fetchone()
    c.close()
    return row


def read_once():
    st = latest_ou_state(MATCH, LINE)
    over = st.get("over")
    under = st.get("under")
    if not (over and under):
        return {"ok": False, "note": "暂无 OU_%.2f 双边快照" % LINE}
    o_odds, min_, score = over
    u_odds, _, _ = under
    sh, sa = parse_score(score)
    X = np.array([build_ou_features(min_, sh, sa, LINE, o_odds, u_odds)], dtype=float)
    p = float(model.predict_proba(X)[0, 1])
    # 市场去水隐含
    inv_o, inv_u = 1.0 / o_odds, 1.0 / u_odds
    mkt = inv_o / (inv_o + inv_u)
    return {
        "ok": True, "minute": min_, "score": score,
        "over_odds": o_odds, "under_odds": u_odds,
        "model_p_over": round(p, 4),
        "market_p_over": round(mkt, 4),
        "model_vs_market_pp": round((p - mkt) * 100, 1),
        "direction": "over" if p >= 0.5 else "under",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只取一次读数后退出")
    args = ap.parse_args()

    log_path = os.path.join(ROOT, "data",
                            "live_ou_rolling_%s.jsonl" % MATCH.replace(" vs ", "_"))
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    iters = 1 if args.once else MAX_ITERS
    for i in range(iters):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        rec = read_once()
        rec["ts"] = ts
        rec["match_key"] = MATCH
        rec["ou_line"] = LINE
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if rec.get("ok"):
            print("[%d] %s min=%s %s | P(大)=%.3f 市场P(大)=%.3f 差%+.1fpp 方向=%s" % (
                i, ts, rec["minute"], rec["score"],
                rec["model_p_over"], rec["market_p_over"],
                rec["model_vs_market_pp"], rec["direction"]))
        else:
            print("[%d] %s %s" % (i, ts, rec.get("note")))

        fin = finished(MATCH)
        if fin:
            print(">>> 终场: result=%s 比分=%s-%s , 停止跟踪" % (fin[0], fin[1], fin[2]))
            # 写一条终场标记
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": ts, "match_key": MATCH,
                                    "event": "finished",
                                    "final_result": fin[0],
                                    "final_score": "%s-%s" % (fin[1], fin[2])},
                                   ensure_ascii=False) + "\n")
            break
        if i < iters - 1:
            time.sleep(INTERVAL)

    print("log ->", log_path)


if __name__ == "__main__":
    main()
