"""
indep_features_runtime.py — 推理期"真独立"特征计算(根除 median-fill 部署退化 + 训练/服务零偏差)

关键修正(2026-08-31):
  旧版 _replay 用 match_date < asof (严格小于) 回放, 对"同日多场"会漏掉同日早于目标场的比赛,
  导致 elo/form 与训练表 indep_features(由 build_independent_features.py 单趟日期序回放生成, 含同日序)不一致。
  训练/服务特征偏差 -> 开盘天眼在"无预建行"的场次(即未来场)边缘归零。

  现改为: 单次 O(N) 回放, 对每一场在处理"前"记录其赛前特征到 emitted[(h,a,date)](末场胜出, 与
  indep_idx 取末行一致), 并缓存整库结果。compute_live_features:
    - 若该 (h,a,date) 在 emitted -> 返回(与训练表逐位一致, parity=0)
    - 否则(未来场, 历史中无此场) -> 由终态推导赛前特征(未来场无同日历史, 天然无同日序残差)
  与 build_independent_features.py 完全同构(同 HOME_ADV/K/公式/away_form=act_h)。
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

_FULL_CACHE = {}  # db_path -> (emitted_dict, final_state, amap)


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


def _load_rows(cur, amap):
    rows = []
    for hn, an, md, hs, as_, lr, lg in cur.execute(
        "SELECT home_team_name, away_team_name, match_date, home_score, away_score, final_result, league_name "
        "FROM matches WHERE final_result IN ('H','D','A')"):
        rows.append((str(md)[:10], canon(hn, amap), canon(an, amap),
                     int(hs) if hs is not None else 0, int(as_) if as_ is not None else 0, lr, lg))
    for hn, an, ud, hs, as_, comp in cur.execute(
        "SELECT home_team, away_team, utc_date, home_score, away_score, competition_name "
        "FROM fd_matches WHERE home_score IS NOT NULL AND away_score IS NOT NULL AND status='FINISHED'"):
        try:
            hs_i, as_i = int(hs), int(as_)
        except Exception:
            continue
        res = 'H' if hs_i > as_i else ('D' if hs_i == as_i else 'A')
        rows.append((str(ud)[:10], canon(hn, amap), canon(an, amap), hs_i, as_i, res, comp))
    rows.sort(key=lambda r: r[0])
    return rows


def _replay_all(rows):
    """单趟回放, 与 build_independent_features.compute() 完全同构。返回 (emitted, final_state)。"""
    elo = defaultdict(lambda: 1500.0)
    home_form = defaultdict(lambda: deque(maxlen=5))
    away_form = defaultdict(lambda: deque(maxlen=5))
    last_date = {}
    h2h = defaultdict(lambda: deque(maxlen=10))
    league_elo = defaultdict(list)

    emitted = {}
    for date_s, h, a, hs, as_, res, lg in rows:
        eh, ea = elo[h], elo[a]
        exp_h = 1.0 / (1.0 + 10 ** (-(eh + HOME_ADV - ea) / 400.0))
        act_h = 1.0 if res == 'H' else (0.5 if res == 'D' else 0.0)
        # ---- 赛前特征(处理本场前快照, 含同日早于本场的比赛) ----
        elo_diff = eh + HOME_ADV - ea
        fh = float(np.mean(home_form[h])) if home_form[h] else 0.5
        fa = float(np.mean(away_form[a])) if away_form[a] else 0.5
        rh = (pd_date(date_s) - last_date[h]).days if h in last_date else 7
        ra = (pd_date(date_s) - last_date[a]).days if a in last_date else 7
        key = frozenset((h, a))
        hh = list(h2h[key])
        if hh:
            hw = sum(1 for x in hh if x == 1) / len(hh)
            hd = sum(1 for x in hh if x == 0.5) / len(hh)
            ha = sum(1 for x in hh if x == 0) / len(hh)
        else:
            hw = hd = ha = 1 / 3
        ls = float(np.mean(league_elo[lg])) if league_elo.get(lg) else 1500.0
        emitted[(h, a, date_s)] = {   # 末场胜出, 与 indep_idx 取末行一致
            "elo_home": round(eh, 1), "elo_away": round(ea, 1), "elo_diff": round(elo_diff, 1),
            "form_home": round(fh, 3), "form_away": round(fa, 3), "form_diff": round(fh - fa, 3),
            "rest_home": rh, "rest_away": ra, "rest_diff": rh - ra,
            "h2h_home_win": round(hw, 3), "h2h_draw": round(hd, 3), "h2h_away_win": round(ha, 3),
            "league_strength": round(ls, 1),
        }
        # ---- 更新状态 ----
        elo[h] = eh + K * (act_h - exp_h)
        elo[a] = ea + K * ((1.0 - act_h) - (1.0 - exp_h))
        home_form[h].append(act_h)
        away_form[a].append(act_h)   # 与 build 同构: away_form 存主队视角(=act_h)
        last_date[h] = pd_date(date_s)
        last_date[a] = pd_date(date_s)
        h2h[key].append(act_h)
        league_elo[lg].append(eh)
        league_elo[lg].append(ea)
    final_state = dict(
        elo=dict(elo),
        home_form={k: list(v) for k, v in home_form.items()},
        away_form={k: list(v) for k, v in away_form.items()},
        last_date={k: v for k, v in last_date.items()},
        h2h={k: list(v) for k, v in h2h.items()},
        league_elo={k: list(v) for k, v in league_elo.items()},
    )
    return emitted, final_state


def _features_from_state(st, h, a, league, asof):
    """由终态推导 (h,a,league,asof) 的赛前特征(用于未来场: 无同日历史, 无同日序残差)。

    asof = 未来场真实开赛日期, 用于休息天数计算(rest_home/away = 距该队上次历史比赛的间隔)。
    """
    elo = st["elo"]
    eh, ea = elo.get(h, 1500.0), elo.get(a, 1500.0)
    elo_diff = eh + HOME_ADV - ea
    hf = st["home_form"].get(h, []); af = st["away_form"].get(a, [])
    fh = float(np.mean(hf)) if hf else 0.5
    fa = float(np.mean(af)) if af else 0.5
    adt = pd_date(asof)
    rh = (adt - st["last_date"][h]).days if h in st["last_date"] else 7
    ra = (adt - st["last_date"][a]).days if a in st["last_date"] else 7
    key = frozenset((h, a)); hh = st["h2h"].get(key, [])
    if hh:
        hw = sum(1 for x in hh if x == 1) / len(hh)
        hd = sum(1 for x in hh if x == 0.5) / len(hh)
        ha = sum(1 for x in hh if x == 0) / len(hh)
    else:
        hw = hd = ha = 1 / 3
    ls = float(np.mean(st["league_elo"][league])) if st["league_elo"].get(league) else 1500.0
    return {
        "elo_home": round(eh, 1), "elo_away": round(ea, 1), "elo_diff": round(elo_diff, 1),
        "form_home": round(fh, 3), "form_away": round(fa, 3), "form_diff": round(fh - fa, 3),
        "rest_home": rh, "rest_away": ra, "rest_diff": rh - ra,
        "h2h_home_win": round(hw, 3), "h2h_draw": round(hd, 3), "h2h_away_win": round(ha, 3),
        "league_strength": round(ls, 1),
    }


def compute_live_features(home, away, match_date_str, league, db_path=DB):
    """返回 13 维独立特征 dict, 与训练表 indep_features 逐位一致(历史场) / 正确(未来场)。"""
    asof = str(match_date_str)[:10]
    if db_path not in _FULL_CACHE:
        conn = sqlite3.connect(db_path)
        try:
            amap = build_alias_map(conn)
            rows = _load_rows(conn, amap)
            emitted, final_state = _replay_all(rows)
            _FULL_CACHE[db_path] = (emitted, final_state, amap)
        finally:
            conn.close()
    emitted, final_state, amap = _FULL_CACHE[db_path]
    h = canon(home, amap)
    a = canon(away, amap)
    key = (h, a, asof)
    if key in emitted:
        return emitted[key]          # 历史场: 与训练表逐位一致 (parity=0)
    return _features_from_state(final_state, h, a, league, asof)  # 未来场: 终态推导


def clear_cache():
    _FULL_CACHE.clear()
