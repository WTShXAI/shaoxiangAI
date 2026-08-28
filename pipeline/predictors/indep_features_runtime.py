"""
indep_features_runtime.py — 推理期"真独立"特征计算(根除 median-fill 部署退化)

unified_predictor 旧版在推理时把 55/81 非赔率特征填训练中位数 -> 模型只见赔率 -> edge≈0。
本模块在每次推理时, 从 football_data.db(matches+fd_matches) 真正回放历史,
算出现(主/客)截至比赛日的 Elo / 形式 / 休息 / H2H / 联赛强度, 喂给独立模型。

与 build_independent_features.py 的 compute() 完全同构(同 HOME_ADV/K/公式),
保证推理特征分布 == 训练特征分布(无分布漂移隐患)。

用法:
  from indep_features_runtime import compute_live_features
  feats = compute_live_features(home, away, "2026-07-26", "Premier League", db_path)
"""
import sqlite3
import os
from collections import defaultdict, deque
from datetime import datetime
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "data", "football_data.db")

HOME_ADV = 60.0
K = 30.0

_CACHE = {}  # asof_date -> replayed state (历史库静态, 同日期只回放一次)


def build_alias_map(cur):
    import json, ast
    amap = {}
    for canon, aj in cur.execute("SELECT canonical, aliases_json FROM team_canonical"):
        amap[canon.strip().lower()] = canon
        if aj:
            try:
                lst = json.loads(aj) if aj.strip().startswith("[") else ast.literal_eval(aj)
                for a in lst:
                    amap[str(a).strip().lower()] = canon
            except Exception:
                pass
    return amap


def canon(team, amap):
    if not team:
        return None
    t = str(team).strip().lower()
    return amap.get(t, team.strip())


def pd_date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except Exception:
        return datetime(2000, 1, 1)


def _replay(cur, asof_date, amap):
    rows = []
    for hn, an, md, hs, as_, lr, lg in cur.execute(
        "SELECT home_team_name, away_team_name, match_date, home_score, away_score, final_result, league_name "
        "FROM matches WHERE final_result IN ('H','D','A') AND match_date < ?", (asof_date,)):
        rows.append((str(md)[:10], canon(hn, amap), canon(an, amap), int(hs) if hs is not None else 0,
                     int(as_) if as_ is not None else 0, lr, lg))
    for hn, an, ud, hs, as_, comp in cur.execute(
        "SELECT home_team, away_team, utc_date, home_score, away_score, competition_name "
        "FROM fd_matches WHERE home_score IS NOT NULL AND away_score IS NOT NULL AND status='FINISHED' AND utc_date < ?",
        (asof_date,)):
        try:
            hs_i, as_i = int(hs), int(as_)
        except Exception:
            continue
        res = 'H' if hs_i > as_i else ('D' if hs_i == as_i else 'A')
        rows.append((str(ud)[:10], canon(hn, amap), canon(an, amap), hs_i, as_i, res, comp))
    rows.sort(key=lambda r: r[0])

    elo = defaultdict(lambda: 1500.0)
    home_form = defaultdict(lambda: deque(maxlen=5))
    away_form = defaultdict(lambda: deque(maxlen=5))
    last_date = {}
    h2h = defaultdict(lambda: deque(maxlen=10))
    league_elo = defaultdict(list)

    for date_s, h, a, hs, as_, res, lg in rows:
        eh, ea = elo[h], elo[a]
        exp_h = 1.0 / (1.0 + 10 ** (-(eh + HOME_ADV - ea) / 400.0))
        act_h = 1.0 if res == 'H' else (0.5 if res == 'D' else 0.0)
        elo[h] = eh + K * (act_h - exp_h)
        elo[a] = ea + K * ((1.0 - act_h) - (1.0 - exp_h))
        home_form[h].append(act_h)
        away_form[a].append(1.0 - act_h)
        last_date[h] = pd_date(date_s)
        last_date[a] = pd_date(date_s)
        h2h[frozenset((h, a))].append(act_h)
        league_elo[lg].append(eh)
        league_elo[lg].append(ea)
    return dict(elo=elo, home_form=home_form, away_form=away_form,
                last_date=last_date, h2h=h2h, league_elo=league_elo, amap=amap)


def compute_live_features(home, away, match_date_str, league, db_path=DB):
    """返回 17 维独立特征 dict(与训练 FEATURES 顺序一致), 失败时返回全默认特征。"""
    asof = str(match_date_str)[:10]
    if asof not in _CACHE:
        conn = sqlite3.connect(db_path)
        try:
            amap = build_alias_map(conn)
            _CACHE[asof] = _replay(conn, asof, amap)
        finally:
            conn.close()
    st = _CACHE[asof]
    amap = st["amap"]
    h = canon(home, amap)
    a = canon(away, amap)
    elo = st["elo"]
    eh_v, ea_v = elo[h], elo[a]
    elo_diff = eh_v + HOME_ADV - ea_v
    hf = st["home_form"][h]
    af = st["away_form"][a]
    fh = float(np.mean(hf)) if hf else 0.5
    fa = float(np.mean(af)) if af else 0.5
    rh = (pd_date(asof) - st["last_date"][h]).days if h in st["last_date"] else 7
    ra = (pd_date(asof) - st["last_date"][a]).days if a in st["last_date"] else 7
    key = frozenset((h, a))
    hh = list(st["h2h"][key])
    if hh:
        hw = sum(1 for x in hh if x == 1) / len(hh)
        hd = sum(1 for x in hh if x == 0.5) / len(hh)
        ha = sum(1 for x in hh if x == 0) / len(hh)
    else:
        hw = hd = ha = 1 / 3
    ls = float(np.mean(st["league_elo"][league])) if st["league_elo"].get(league) else 1500.0
    return {
        "elo_home": round(eh_v, 1), "elo_away": round(ea_v, 1), "elo_diff": round(elo_diff, 1),
        "form_home": round(fh, 3), "form_away": round(fa, 3), "form_diff": round(fh - fa, 3),
        "rest_home": rh, "rest_away": ra, "rest_diff": rh - ra,
        "h2h_home_win": round(hw, 3), "h2h_draw": round(hd, 3), "h2h_away_win": round(ha, 3),
        "league_strength": round(ls, 1),
    }


def clear_cache():
    _CACHE.clear()
