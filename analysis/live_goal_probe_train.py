"""
Live Goal Probe 模型训练 v4 (时间序列对齐)
目标: 基于当前滚球盘口状态, 预测半场/全场破蛋概率。

odds_snapshots 中不同 market 的 captured_at 不同步, 需对齐到统一时间轴后前向填充。
标签来自 matches 终场/半场比分。
"""
import os, sys, sqlite3, re, json
from collections import defaultdict
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, classification_report

GQ = "D:/Architecture/data/events.db"
MIN_ODDS, MAX_ODDS = 1.01, 1000.0
TIME_STEP = 60  # 统一时间轴步长(秒)


def get_match_time_aligned(con, match_key, ht_home, ht_away, ft_home, ft_away):
    """为一场比赛构建时间对齐的盘口序列。"""
    if ht_home is None or ht_away is None or ft_home is None or ft_away is None:
        return []
    cur = con.cursor()
    rows = cur.execute("""
        SELECT market, selection, odds, captured_at, minute_at
        FROM odds_snapshots
        WHERE match_key=? AND odds>? AND odds<?
        ORDER BY captured_at
    """, (match_key, MIN_ODDS, MAX_ODDS)).fetchall()

    # 按 market+selection 分组
    series = defaultdict(list)  # key -> [(captured_at, odds, minute_at)]
    all_ts = set()
    for mkt, sel, odds, cap, minute in rows:
        key = f"{mkt}__{sel}"
        series[key].append((cap, odds, minute))
        all_ts.add(cap)

    if not all_ts:
        return []
    t_min, t_max = min(all_ts), max(all_ts)

    # 统一时间轴: 每 TIME_STEP 秒一个点
    grid = np.arange(t_min, t_max + TIME_STEP, TIME_STEP)

    # 对每个序列前向填充到 grid
    aligned = defaultdict(dict)  # t -> {key: odds}
    for key, pts in series.items():
        pts.sort()
        vals = [p[1] for p in pts]
        times = [p[0] for p in pts]
        j = 0
        for t in grid:
            while j < len(times) - 1 and times[j+1] <= t:
                j += 1
            if times[j] <= t:
                aligned[t][key] = vals[j]

    # 构建 minute_at 序列
    minute_map = {}
    for mkt, sel, odds, cap, minute in rows:
        if minute is not None:
            minute_map[cap] = minute
    # 对 grid 填充 minute_at (用最近的)
    minute_sorted = sorted(minute_map.items())
    grid_minute = []
    j = 0
    for t in grid:
        while j < len(minute_sorted) - 1 and minute_sorted[j+1][0] <= t:
            j += 1
        grid_minute.append(minute_sorted[j][1] if minute_sorted else 0)

    # 标签
    ht_total = (ht_home if ht_home is not None else 0) + (ht_away if ht_away is not None else 0)
    ft_total = (ft_home if ft_home is not None else 0) + (ft_away if ft_away is not None else 0)
    label_ht_break = 1 if ht_total >= 1 else 0
    label_ft_break_base = 1 if ft_total >= 0.5 else 0

    samples = []
    for t, minute in zip(grid, grid_minute):
        if minute > 45:
            continue
        a = aligned.get(t, {})

        def get_odds(mkt, sel):
            return a.get(f"{mkt}__{sel}", np.nan)

        # 1X2
        x2h = get_odds('1X2', 'home')
        x2d = get_odds('1X2', 'draw')
        x2a = get_odds('1X2', 'away')
        x2_fav_odds = min([v for v in (x2h, x2d, x2a) if not np.isnan(v)], default=np.nan)

        # OU 1H
        ou1h_over = ou1h_under = ou1h_delta = np.nan
        ou1h_low_is_over = 0
        for line_key in ['OU_1H_0.50', 'OU_1H_0.75', 'OU_1H_1.00']:
            ov = get_odds(line_key, 'over')
            un = get_odds(line_key, 'under')
            if not (np.isnan(ov) or np.isnan(un)):
                ou1h_over, ou1h_under = ov, un
                ou1h_delta = abs(ov - un)
                ou1h_low_is_over = 1 if ov < un else 0
                break

        # OU FT
        ouf_over = ouf_under = ouf_delta = np.nan
        ouf_low_is_over = 0
        ouf_line = np.nan
        for line_key in ['OU_0.50', 'OU_0.75', 'OU_1.00', 'OU_1.25', 'OU_1.50']:
            ov = get_odds(line_key, 'over')
            un = get_odds(line_key, 'under')
            if not (np.isnan(ov) or np.isnan(un)):
                ouf_over, ouf_under = ov, un
                ouf_delta = abs(ov - un)
                ouf_low_is_over = 1 if ov < un else 0
                try: ouf_line = float(line_key.split('_')[1])
                except: ouf_line = np.nan
                break

        # AH
        ah_home = ah_away = ah_delta = np.nan
        for line_key in ['AH_0.00', 'AH_0.25', 'AH_-0.25', 'AH_0.50', 'AH_-0.50', 'AH_0.75', 'AH_-0.75', 'AH_1.00', 'AH_-1.00']:
            hm = get_odds(line_key, 'home')
            aw = get_odds(line_key, 'away')
            if not (np.isnan(hm) or np.isnan(aw)):
                ah_home, ah_away = hm, aw
                ah_delta = abs(hm - aw)
                break

        # 赔率变化率 (相对上一 grid)
        prev = samples[-1] if samples else None
        if prev:
            ou1h_over_change = (ou1h_over - prev['ou1h_over']) / prev['ou1h_over'] if prev['ou1h_over'] > 0 else 0.0
            ou1h_under_change = (ou1h_under - prev['ou1h_under']) / prev['ou1h_under'] if prev['ou1h_under'] > 0 else 0.0
        else:
            ou1h_over_change = 0.0
            ou1h_under_change = 0.0

        label_ft_break = 1 if ft_total >= (ouf_line if not np.isnan(ouf_line) else 0.5) else 0

        samples.append({
            'match_key': match_key,
            'captured_at': t,
            'minute_at': minute,
            'x2_fav_odds': x2_fav_odds,
            'ou1h_over': ou1h_over,
            'ou1h_under': ou1h_under,
            'ou1h_delta': ou1h_delta,
            'ou1h_low_is_over': ou1h_low_is_over,
            'ouf_over': ouf_over,
            'ouf_under': ouf_under,
            'ouf_delta': ouf_delta,
            'ouf_low_is_over': ouf_low_is_over,
            'ouf_line': ouf_line,
            'ah_home': ah_home,
            'ah_away': ah_away,
            'ah_delta': ah_delta,
            'ou1h_over_change': ou1h_over_change,
            'ou1h_under_change': ou1h_under_change,
            'label_ht_break': label_ht_break,
            'label_ft_break': label_ft_break,
        })
    return samples


def engineer(df):
    df = df.copy()
    df['half_time_pressure'] = df['minute_at'] / 45.0
    df['is_0_0'] = 1
    df['ou1h_strong_low_over'] = ((df['ou1h_delta'] >= 0.10) & (df['ou1h_low_is_over'] == 1)).astype(int)
    df['ou1h_strong_low_under'] = ((df['ou1h_delta'] >= 0.10) & (df['ou1h_low_is_over'] == 0)).astype(int)
    df['ouf_strong_low_over'] = ((df['ouf_delta'] >= 0.10) & (df['ouf_low_is_over'] == 1)).astype(int)
    df['ouf_strong_low_under'] = ((df['ouf_delta'] >= 0.10) & (df['ouf_low_is_over'] == 0)).astype(int)
    df['ou1h_over_dropping'] = (df['ou1h_over_change'] < -0.01).astype(int)
    df['ou1h_under_dropping'] = (df['ou1h_under_change'] < -0.01).astype(int)
    df['big_momentum'] = df['ou1h_over_dropping'] | df['ouf_strong_low_over']
    return df


def train_model(df, label_col, name):
    feature_cols = [
        'minute_at', 'half_time_pressure', 'is_0_0',
        'x2_fav_odds',
        'ou1h_delta', 'ou1h_low_is_over',
        'ou1h_over_change', 'ou1h_under_change',
        'ou1h_strong_low_over', 'ou1h_strong_low_under',
        'ouf_delta', 'ouf_low_is_over',
        'ouf_strong_low_over', 'ouf_strong_low_under',
        'ouf_line',
        'ah_delta',
        'big_momentum'
    ]
    df2 = df.dropna(subset=feature_cols + [label_col]).copy()
    if len(df2) < 100:
        print(f"[{name}] too few samples: {len(df2)}")
        return None
    X = df2[feature_cols].fillna(0).values
    y = df2[label_col].values
    # 按时间顺序划分
    df2 = df2.sort_values('captured_at')
    split = int(len(df2) * 0.7)
    Xtr = df2[feature_cols].fillna(0).values[:split]
    Xte = df2[feature_cols].fillna(0).values[split:]
    ytr = df2[label_col].values[:split]
    yte = df2[label_col].values[split:]
    clf = GradientBoostingClassifier(n_estimators=120, max_depth=3, learning_rate=0.08, random_state=42)
    clf.fit(Xtr, ytr)
    prob = clf.predict_proba(Xte)[:, 1]
    auc = roc_auc_score(yte, prob)
    print(f"\n=== {name} ===")
    print(f"samples={len(df2)} pos={y.mean()*100:.1f}% AUC={auc:.4f}")
    print(classification_report(yte, (prob >= 0.5).astype(int), target_names=['no_break', 'break']))
    print("feature importances:")
    for c, imp in sorted(zip(feature_cols, clf.feature_importances_), key=lambda x: -x[1]):
        print(f"  {c}: {imp:.3f}")
    return {'clf': clf, 'feature_cols': feature_cols, 'name': name, 'auc': auc}


def main():
    con = sqlite3.connect(GQ)
    matches = pd.read_sql("""
        SELECT match_key, score_home ft_h, score_away ft_a, ht_score_home, ht_score_away
        FROM matches
        WHERE status='finished'
          AND score_home IS NOT NULL AND score_away IS NOT NULL
          AND ht_score_home IS NOT NULL AND ht_score_away IS NOT NULL
          -- HT 污染清洗(2026-08-27): 半场总进球须 < 全场总进球, 否则 ht 被回填为全场
          AND (ht_score_home + ht_score_away) < (score_home + score_away)
          AND EXISTS(SELECT 1 FROM odds_snapshots s WHERE s.match_key=matches.match_key AND s.market='OU_1H_0.50')
        ORDER BY RANDOM()
        LIMIT 120
    """, con)
    print(f"selected {len(matches)} finished matches")
    all_samples = []
    for _, row in matches.iterrows():
        try:
            samps = get_match_time_aligned(con, row['match_key'], row['ht_score_home'], row['ht_score_away'], row['ft_h'], row['ft_a'])
            all_samples.extend(samps)
        except Exception as e:
            print(f"skip {row['match_key']}: {e}")
    con.close()

    df = pd.DataFrame(all_samples)
    print(f"raw aligned samples (0-0, HT only): {len(df)}")
    if len(df) < 200:
        print("too few samples to train")
        return

    df = engineer(df)
    ht_model = train_model(df, 'label_ht_break', 'HT_Break_Probe')
    ft_model = train_model(df, 'label_ft_break', 'FT_Break_Probe')

    import joblib
    if ht_model and ft_model:
        joblib.dump({
            'ht_model': ht_model,
            'ft_model': ft_model,
            'created': str(pd.Timestamp.now())
        }, 'analysis/live_goal_probe_model.pkl')
        print("\nsaved analysis/live_goal_probe_model.pkl")


if __name__ == '__main__':
    main()
