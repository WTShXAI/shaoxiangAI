"""
CS (波胆) 错价 / +EV 检测模型  —— 哨响AI
=================================================
核心思想（用户实战直觉 + 系统铁律#3 真edge）:
  用 1X2 平局赔率推断"公平 0-0 概率"，与 CS 盘口给的 0-0 赔率隐含概率比较。
  当 公平概率 - 盘口隐含概率 >= 阈值 时，判定该比分被低估（盘口锚定偏差），
  即为 +EV 投注机会。

校准来源: football_data.db.historical_matches (31万场, 含 open 1X2 + 真实赛果)
回测来源: events.db 已完赛场次 (pre-match 1X2 + CS 0-0 开盘快照 + 真实赛果)

⚠️ 诚实声明:
  - 回测 ROI 偏高(+130%)主要来自 (1)采用最膨胀的开盘快照 (2)样本偏向低级别/低进球联赛。
  - 这不是可直接宣称的"实盘+130% edge"，必须经 paper-trading 验证，且平台限号会封顶可兑现 edge。
  - 本模型是"纪律化+EV过滤器"，作用是剔除用户当前 309 笔随机CS亏损单中的绝大多数。
"""
import sqlite3, json
import numpy as np


def _margin_strip(h, d, a):
    s = 1.0 / h + 1.0 / d + 1.0 / a
    return 1.0 / h / s, 1.0 / d / s, 1.0 / a / s


def calibrate(football_db="D:/Architecture/data/football_data.db"):
    """返回 (kx, vy) 经验校准曲线: P(0-0 | 平局隐含概率)。"""
    f = sqlite3.connect(football_db)
    rows = f.execute("""
        SELECT open_home_odds, open_draw_odds, open_away_odds, home_score, away_score
        FROM historical_matches
        WHERE open_home_odds>1.01 AND open_draw_odds>1.01 AND open_away_odds>1.01
          AND home_score IS NOT NULL AND away_score IS NOT NULL
    """).fetchall()
    f.close()
    xs, ys = [], []
    for h, d, a, hs, as_ in rows:
        _, dp, _ = _margin_strip(h, d, a)
        xs.append(dp)
        ys.append(1 if hs == 0 and as_ == 0 else 0)
    xs = np.array(xs); ys = np.array(ys)
    edges = np.linspace(xs.min(), xs.max(), 12)
    bins = np.digitize(xs, edges)
    calib = {}
    for b in range(1, len(edges) + 1):
        m = bins == b
        if m.sum() > 30:
            calib[float(edges[b - 1])] = float(ys[m].mean())
    kx = np.array(sorted(calib)); vy = np.array([calib[k] for k in sorted(calib)])
    return kx, vy, float(ys.mean())


def fair_p00(dp, kx, vy):
    if dp <= kx[0]: return vy[0]
    if dp >= kx[-1]: return vy[-1]
    return float(np.interp(dp, kx, vy))


def evaluate_cs0_0(draw_odds, home_odds, away_odds, cs00_odds, kx, vy, thresh=0.03):
    """给定一场比赛的 pre-match 赔率，评估 0-0 是否 +EV。"""
    _, dp, _ = _margin_strip(home_odds, draw_odds, away_odds)
    fair = fair_p00(dp, kx, vy)
    impl = 1.0 / cs00_odds
    div = fair - impl
    return {"draw_prob": round(dp, 4), "fair_p00": round(fair, 4),
            "implied_p00": round(impl, 4), "divergence": round(div, 4),
            "is_ev": bool(div >= thresh)}


def backtest(gq_db="D:/Architecture/data/events.db", thresh=0.03, cap=60.0):
    g = sqlite3.connect(gq_db)
    mrows = g.execute(
        "SELECT home,away,score_home,score_away FROM matches "
        "WHERE score_home IS NOT NULL AND score_away IS NOT NULL AND status='finished'"
    ).fetchall()
    def snap(mk, market, sel):
        r = g.execute(
            "SELECT odds FROM odds_snapshots WHERE match_key=? AND market=? AND selection=? "
            "ORDER BY captured_at ASC LIMIT 1", (mk, market, sel)).fetchone()
        return float(r[0]) if r else None
    kx, vy, _ = calibrate()
    naive_f = naive_w = 0; naive_pnl = 0.0
    ev_f = ev_w = 0; ev_pnl = 0.0
    for home, away, sh, sa in mrows:
        mk = f"{home} vs {away}"
        dO = snap(mk, '1X2', 'draw'); hO = snap(mk, '1X2', 'home'); aO = snap(mk, '1X2', 'away'); cO = snap(mk, 'CS', '0-0')
        if not (dO and hO and aO and cO) or cO > cap:
            continue
        is00 = 1 if sh == 0 and sa == 0 else 0
        naive_f += 1
        if is00: naive_w += 1; naive_pnl += cO - 1
        else: naive_pnl -= 1
        _, dp, _ = _margin_strip(hO, dO, aO)
        fair = fair_p00(dp, kx, vy); impl = 1.0 / cO
        if fair - impl >= thresh:
            ev_f += 1
            if is00: ev_w += 1; ev_pnl += cO - 1
            else: ev_pnl -= 1
    g.close()
    return {
        "tested": naive_f,
        "naive": {"flags": naive_f, "wins": naive_w, "hit": naive_w / naive_f, "roi": naive_pnl / naive_f},
        "ev": {"flags": ev_f, "wins": ev_w, "hit": ev_w / ev_f, "roi": ev_pnl / ev_f} if ev_f else None,
    }


if __name__ == "__main__":
    kx, vy, ov = calibrate()
    print("calib points:", len(kx), "overall 0-0:", round(ov, 4))
    r = backtest()
    print(json.dumps(r, indent=2, ensure_ascii=False))
