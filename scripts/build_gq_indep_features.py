"""build_gq_indep_features.py — 为 GQ 干净 2026 赛果建独立特征(供 independent_model 干净真相重测)

语义对齐 football_data.indep_features 的 13 个 indep_feats, 使 independent_model(训练于该表)收到同语义输入:
  elo_home/away/diff, form_home/away/diff, rest_home/away/diff,
  h2h_home_win/draw/away_win, league_strength

做法: 按 kickoff 日期升序单遍滚算, 每场特征 = 用该场"之前"的全部 GQ 历史计算, 再更新状态。
  - Elo: 标准 logistic, K=24, 初始化 1500 (与训练 Elo 量级 ~1500 一致)
  - form: 该队最近 <=5 场结果指数衰减均值 (W=1,D=.5,L=0)
  - rest: 该队距上一场的天数 (无前场默认 7)
  - h2h: 该有序对之前交锋胜平负率 (Laplace+1 平滑, 无前场=均匀 1/3)
  - league_strength: 该联赛截至当前的球队 Elo 滚动均值

输出: data/indep_features_gq.db :: indep_features(match_key, home, away, league, match_date, 13 feats)
用法: .venv/Scripts/python.exe scripts/build_gq_indep_features.py
"""
import sqlite3, os, json
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GQ_DB = os.path.join(ROOT, "data", "GQ.db")
OUT_DB = os.path.join(ROOT, "data", "indep_features_gq.db")
K = 24.0


def parse_dt(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s[:16], fmt)
        except Exception:
            continue
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except Exception:
        return None


def main():
    cg = sqlite3.connect(f"file:{GQ_DB}?mode=ro", uri=True, timeout=30)
    cg.row_factory = sqlite3.Row
    rows = cg.execute(
        "SELECT match_key, home, away, league, kickoff, score_home, score_away "
        "FROM matches WHERE score_home IS NOT NULL AND score_away IS NOT NULL "
        "AND kickoff IS NOT NULL AND kickoff!='' ORDER BY kickoff ASC"
    ).fetchall()
    cg.close()

    elo = {}          # team -> rating
    last5 = {}        # team -> list of recent results (1/0.5/0)
    last_date = {}    # team -> datetime of last match
    h2h = {}          # (home,away) -> [w,d,a]
    lg_elo_sum = {}   # league -> [sum_elo, count]
    DEFAULT_ELO = 1500.0

    out = []
    for r in rows:
        dt = parse_dt(r["kickoff"])
        if dt is None:
            continue
        h, a = r["home"], r["away"]
        lg = r["league"] or "UNK"
        sh, sa = r["score_home"], r["score_away"]
        # ---- 计算特征(用当前状态, 即该场之前) ----
        eh = elo.get(h, DEFAULT_ELO)
        ea = elo.get(a, DEFAULT_ELO)
        fh = sum(last5.get(h, [])[-5:]) / max(1, len(last5.get(h, [])[-5:]))
        fa = sum(last5.get(a, [])[-5:]) / max(1, len(last5.get(a, [])[-5:]))
        rh = (dt - last_date[h]).days if h in last_date else 7
        ra = (dt - last_date[a]).days if a in last_date else 7
        pair = (h, a)
        hw, hd, ha = h2h.get(pair, [0, 0, 0])
        tot = hw + hd + ha
        if tot == 0:
            h2h_h, h2h_d, h2h_a = 1/3, 1/3, 1/3
        else:
            h2h_h = (hw + 1) / (tot + 3)
            h2h_d = (hd + 1) / (tot + 3)
            h2h_a = (ha + 1) / (tot + 3)
        s = lg_elo_sum.get(lg, [0.0, 0])
        ls = (s[0] / s[1]) if s[1] else DEFAULT_ELO
        out.append({
            "match_key": r["match_key"], "home": h, "away": a, "league": lg,
            "match_date": dt.strftime("%Y-%m-%d"),
            "elo_home": round(eh, 2), "elo_away": round(ea, 2), "elo_diff": round(eh - ea, 2),
            "form_home": round(fh, 4), "form_away": round(fa, 4), "form_diff": round(fh - fa, 4),
            "rest_home": rh, "rest_away": ra, "rest_diff": rh - ra,
            "h2h_home_win": round(h2h_h, 4), "h2h_draw": round(h2h_d, 4), "h2h_away_win": round(h2h_a, 4),
            "league_strength": round(ls, 2),
        })
        # ---- 更新状态(该场结果) ----
        # Elo
        eh_c, ea_c = elo.get(h, DEFAULT_ELO), elo.get(a, DEFAULT_ELO)
        exp_h = 1.0 / (1.0 + 10 ** (-(eh_c - ea_c) / 400.0))
        if sh > sa:
            act_h, act_a = 1.0, 0.0
        elif sh < sa:
            act_h, act_a = 0.0, 1.0
        else:
            act_h, act_a = 0.5, 0.5
        elo[h] = eh_c + K * (act_h - exp_h)
        elo[a] = ea_c + K * (act_a - (1 - exp_h))
        # form
        res_h = act_h if sh != sa else 0.5
        last5.setdefault(h, []).append(1.0 if sh > sa else (0.5 if sh == sa else 0.0))
        last5.setdefault(a, []).append(1.0 if sa > sh else (0.5 if sh == sa else 0.0))
        # rest
        last_date[h] = dt
        last_date[a] = dt
        # h2h
        if sh > sa:
            hw += 1
        elif sh < sa:
            ha += 1
        else:
            hd += 1
        h2h[pair] = [hw, hd, ha]
        # league strength
        cur = lg_elo_sum.get(lg, [0.0, 0])
        lg_elo_sum[lg] = [cur[0] + elo[h] + elo[a], cur[1] + 2]

    # 写库
    if os.path.exists(OUT_DB):
        os.remove(OUT_DB)
    o = sqlite3.connect(OUT_DB)
    o.execute("""CREATE TABLE indep_features(
        match_key TEXT, home TEXT, away TEXT, league TEXT, match_date TEXT,
        elo_home REAL, elo_away REAL, elo_diff REAL,
        form_home REAL, form_away REAL, form_diff REAL,
        rest_home INTEGER, rest_away INTEGER, rest_diff INTEGER,
        h2h_home_win REAL, h2h_draw REAL, h2h_away_win REAL, league_strength REAL)""")
    o.executemany(
        "INSERT INTO indep_features VALUES ("
        ":match_key,:home,:away,:league,:match_date,:elo_home,:elo_away,:elo_diff,"
        ":form_home,:form_away,:form_diff,:rest_home,:rest_away,:rest_diff,"
        ":h2h_home_win,:h2h_draw,:h2h_away_win,:league_strength)", out)
    o.commit()
    # 覆盖统计: 2540 宇宙(以 GQ∩events 1X2 近似) 命中
    ev = sqlite3.connect(f"file:{ROOT}/data/events.db?mode=ro", uri=True, timeout=30)
    ev_keys = set(x[0] for x in ev.execute("SELECT DISTINCT match_key FROM odds_snapshots WHERE market='1X2'").fetchall())
    ev.close()
    built_keys = set(x["match_key"] for x in out)
    inter = built_keys & ev_keys
    print(f"[build_gq_indep] GQ 有比分场: {len(out)}")
    print(f"  Elo 收敛球队数: {len(elo)} (范围 {min(elo.values()):.0f}~{max(elo.values()):.0f})")
    print(f"  与 events 1X2 odds 交集(干净宇宙近似): {len(inter)} 场有独立特征")
    print(f"-> {OUT_DB}")
    o.close()


if __name__ == "__main__":
    main()
