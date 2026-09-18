#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HT锚模型推理 (2026-09-16, 实验B产物: 与训练 build_ht_dataset 逐特征同构).

predict_ht(con, match_key, kickoff_ts, ht_sh, ht_sa) →
  {'ok', 'x2_dir', 'x2_probs', 'ou_dir', 'ou_probs', 'ou_line'}
特征仅用冻结时刻可得信息; HT 比分由调用方传入 (收集器实时比分, 非污染列)。
"""
import sys

sys.path.insert(0, r'D:\Architecture')
MODEL_DIR = r'D:\Architecture\data\models\ht_anchor'
HT_WIN_MIN = 50
FEATURES = ['ht_diff', 'ht_total', 'pre_ph', 'pre_pd', 'pre_pa', 'live_ph', 'live_pd',
            'live_pa', 'n_ip', 'traj_v', 'traj_r', 'ou_line', 'live_po', 'live_pu', 'ou_amp']


def extract_ht_features(con, match_key, kickoff_ts, ht_sh, ht_sa):
    """与 scripts/train_ht_anchor_model.build_ht_dataset 内层逐位同构 (训练=推理)."""
    from pipeline.odds_candles import _devig3
    ht_cut = kickoff_ts + HT_WIN_MIN * 60
    pre = con.execute("""SELECT selection, to_odds, captured_at FROM odds_changes
        WHERE match_key=? AND market='1X2' AND to_odds>1.001 AND to_odds<500
        AND captured_at <= ? ORDER BY captured_at""", (match_key, kickoff_ts)).fetchall()
    if len(pre) < 8:
        return None
    last = {}
    for s, o, c in pre:
        last[s] = float(o)
    if not all(s in last for s in ('home', 'draw', 'away')):
        return None
    pph, ppd, ppa = _devig3(last['home'], last['draw'], last['away'])

    ip = con.execute("""SELECT selection, to_odds, captured_at FROM odds_changes
        WHERE match_key=? AND market='1X2' AND to_odds>1.001 AND to_odds<500
        AND captured_at > ? AND captured_at <= ?
        AND (minute_at IS NULL OR (minute_at >= 0 AND minute_at <= 49))
        ORDER BY captured_at""", (match_key, kickoff_ts, ht_cut)).fetchall()
    ph_l = [e for e in ip if e[0] == 'home']
    pd_l = [e for e in ip if e[0] == 'draw']
    pa_l = [e for e in ip if e[0] == 'away']
    n_ip = len(ip)
    if n_ip >= 6 and ph_l and pd_l and pa_l:
        lh = max(e[2] for e in ph_l); ld = max(e[2] for e in pd_l); la = max(e[2] for e in pa_l)
        h_v = [float(e[1]) for e in ph_l if e[2] == lh][-1]
        d_v = [float(e[1]) for e in pd_l if e[2] == ld][-1]
        a_v = [float(e[1]) for e in pa_l if e[2] == la][-1]
        lph, lpd, lpa = _devig3(h_v, d_v, a_v)
        all_ts = sorted(set(e[2] for e in ip))
        idx = {'home': 0, 'draw': 0, 'away': 0}
        cur = {}
        series = {'home': sorted([(e[2], float(e[1])) for e in ph_l]),
                  'draw': sorted([(e[2], float(e[1])) for e in pd_l]),
                  'away': sorted([(e[2], float(e[1])) for e in pa_l])}
        ph_path = []
        for ts in all_ts:
            for k in series:
                lst = series[k]
                while idx[k] < len(lst) and lst[idx[k]][0] <= ts:
                    cur[k] = lst[idx[k]][1]; idx[k] += 1
            if len(cur) == 3:
                ph_path.append(_devig3(cur['home'], cur['draw'], cur['away'])[0])
        ph_arr = __import__('numpy').array(ph_path)
        traj_v = float(abs(__import__('numpy').diff(ph_arr)).mean()) if len(ph_arr) > 1 else 0.0
        traj_r = float(ph_arr[-1] - ph_arr[0]) if len(ph_arr) > 1 else 0.0
    else:
        lph, lpd, lpa = pph, ppd, ppa
        traj_v = traj_r = 0.0

    ou_feat = [0.0, 0.5, 0.5, 0.0]
    ou_rows = con.execute("""SELECT market, selection, to_odds, captured_at FROM odds_changes
        WHERE match_key=? AND market LIKE 'OU_%' AND to_odds>1.001 AND to_odds<500
        AND captured_at <= ?
        AND (minute_at IS NULL OR (minute_at >= 0 AND minute_at <= 49))
        ORDER BY captured_at""", (match_key, ht_cut)).fetchall()
    if ou_rows:
        from collections import Counter
        cnt = Counter(r[0] for r in ou_rows)
        main_line = cnt.most_common(1)[0][0]
        try:
            line = float(main_line.split('_')[1])
        except Exception:
            line = None
        if line is not None:
            ov = [float(r[2]) for r in ou_rows if r[0] == main_line and r[1] == 'over']
            un = [float(r[2]) for r in ou_rows if r[0] == main_line and r[1] == 'under']
            if ov and un:
                s = 1/ov[-1] + 1/un[-1]
                ou_feat = [line, (1/ov[-1])/s, (1/un[-1])/s,
                           float(abs(ov[-1] - ov[0])/ov[0]) if len(ov) > 1 else 0.0]
    return [int(ht_sh) - int(ht_sa), int(ht_sh) + int(ht_sa),
            pph, ppd, ppa, lph, lpd, lpa, n_ip, traj_v, traj_r,
            ou_feat[0], ou_feat[1], ou_feat[2], ou_feat[3]]


_MODELS = None


def _load():
    global _MODELS
    if _MODELS is None:
        import joblib
        _MODELS = {'m1': joblib.load(rf'{MODEL_DIR}\lgb_1x2.joblib'),
                   'm2': joblib.load(rf'{MODEL_DIR}\lgb_ou.joblib')}
    return _MODELS


def predict_ht(con, match_key, kickoff_ts, ht_sh, ht_sa):
    x = extract_ht_features(con, match_key, kickoff_ts, ht_sh, ht_sa)
    if x is None:
        return {'ok': False, 'reason': 'HT特征不足(赛前tick<8)'}
    m = _load()
    import numpy as np
    x = __import__('numpy').array(x, dtype=np.float32).reshape(1, -1)
    p1 = m['m1'].predict_proba(x)[0]
    out = {'ok': True,
           'x2_dir': ('home', 'draw', 'away')[int(p1.argmax())],
           'x2_probs': [round(float(v), 4) for v in p1]}
    line = x[0, 11]
    if line and line > 0:
        p2 = m['m2'].predict_proba(x)[0]
        out['ou_dir'] = ('UNDER', 'OVER')[int(p2.argmax())]
        out['ou_probs'] = [round(float(v), 4) for v in p2]
        out['ou_line'] = float(line)
    return out


if __name__ == '__main__':
    # 特征一致性自检: 推理提取器 vs 训练build, 同场必须逐位一致
    from analysis.live_goal_probe import _open_gq
    from scripts.train_ht_anchor_model import build_ht_dataset
    from pipeline.odds_candles import parse_kickoff_ts
    con = _open_gq()
    rows = build_ht_dataset(con, days=20, max_matches=3000)
    ok = bad = 0
    for r in rows[:40]:
        ko_ts = parse_kickoff_ts(r['kickoff'])
        # 反推HT: build里 hsh-hsa 在 x[0], ht_total 在 x[1]
        d, tot = r['x'][0], r['x'][1]
        hsh = (d + tot) // 2 if (d + tot) % 2 == 0 else None
        hsa = tot - hsh
        x2 = extract_ht_features(con, r['match_key'], ko_ts, hsh, hsa)
        if x2 is None:
            continue
        if max(abs(a - b) for a, b in zip(x2, r['x'])) < 1e-6:
            ok += 1
        else:
            bad += 1
            if bad == 1:
                print('不一致样例:', r['match_key'])
                print('  train:', r['x'])
                print('  infer:', x2)
    print(f'特征一致性: {ok} 一致 / {bad} 不一致')
