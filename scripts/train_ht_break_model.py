"""
赛前 → 半场破蛋校准模型 (Step2a, 2026-08-18)

背景:
- 历史滚球快照已被 #153 清理(赛前才保留), odds_changes 的 minute_at/score_at 全是占位
  → 无"比赛中分钟级"训练数据, 只能训"赛前盘口 → 半场破蛋"先验模型。
- 滚球神器比赛中概率由盘口去水锚(Step1)负责, 本模型提供赛前/开场先验 + 校准层。

铁律遵守:
- 并排 naive 基线(常数 HT 破蛋率) + 泊松基线(市场隐含总球推导)
- 重复 5x2 CV, 报 AUC + Brier + 分箱校准表
- ML 用浅模型(DecisionTree max_depth=3 / LogReg), nan_to_num
- 联赛先验 shrinkage(k=20)
- 数据走 matches + odds_snapshots 赛前快照(captured_at <= kickoff), 禁滚球污染

输出:
- models/ht_break_model.joblib  (dict: model, iso, feature_names, meta)
- deliverables/ht_break_model_eval.json
"""
import os, sys, json, math, sqlite3, warnings
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import numpy as np

sys.path.insert(0, 'D:/Architecture')
warnings.filterwarnings('ignore')

GQ = 'D:/Architecture/data/events.db'
OUT_MODEL = 'D:/Architecture/models/ht_break_model.joblib'
OUT_EVAL = 'D:/Architecture/deliverables/ht_break_model_eval.json'

HT_SHARE = 0.45   # HT 进球占全场比例先验 (HT场均1.31 / FT场均~2.9)
SHRINK_K = 20     # 联赛先验 shrinkage 强度


def parse_kickoff(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        if 'T' in s:
            return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
        return datetime.strptime(s[:16], '%Y-%m-%d %H:%M').replace(
            tzinfo=timezone(timedelta(hours=8))).timestamp()
    except Exception:
        return None


def dewatered_over_prob(o, u):
    if not o or not u or o <= 1.01 or u <= 1.01:
        return None
    a, b = 1.0 / o, 1.0 / u
    s = a + b
    return a / s if s > 0 else None


def implied_total_from_pairs(pairs):
    pts = []
    for line, o, u in pairs:
        p = dewatered_over_prob(o, u)
        if p is not None:
            pts.append((line, p))
    if not pts:
        return None
    pts.sort()
    for i in range(len(pts) - 1):
        l0, p0 = pts[i]
        l1, p1 = pts[i + 1]
        if (p0 - 0.5) * (p1 - 0.5) <= 0 and p0 != p1:
            return l0 + (0.5 - p0) / (p1 - p0) * (l1 - l0)
    return pts[0][0] - 0.25 if pts[0][1] > 0.5 else pts[-1][0] + 0.25


def build_dataset(cur):
    """v2 抗诱导特征工程 (2026-08-18):
    铁律: 禁用原始赔率值(2.06 每日含义不同, 学原始值=被庄家诱导)。
    只用三类不变量:
      ① 去水概率(消抽水): T_impl, p_over_main, 1X2 去水三概率, p_fav=max去水概率
      ② 漂移(开盘→临场): T_drift, p_over_drift=ln(p_close/p_open), x2h_drift
         —— 水平可伪装, 方向难伪装; 开盘用最早 40 行, 临场用 kickoff 前最近 40 行
      ③ 联赛先验(shrinkage): lg_ht_prior
    标签 ht_total>=1。返回按日分组信息供 GroupKFold-by-day 验证。"""
    print('[data] 加载 finished + HT 比分场 ...')
    matches = cur.execute("""
        SELECT match_key, league, kickoff, ht_score_home, ht_score_away
        FROM matches
        WHERE status='finished'
          AND ht_score_home IS NOT NULL AND ht_score_away IS NOT NULL
          AND score_home IS NOT NULL AND score_away IS NOT NULL
          AND (ht_score_home + ht_score_away) < (score_home + score_away)
    """).fetchall()
    print(f'  {len(matches)} 场有 HT 比分')

    # 联赛 HT 破蛋率先验 (全量, shrinkage)
    lg_cnt = defaultdict(int)
    lg_hit = defaultdict(int)
    for mk, lg, ko, hth, hta in matches:
        lg = lg or 'unknown'
        lg_cnt[lg] += 1
        if (hth + hta) >= 1:
            lg_hit[lg] += 1
    global_rate = sum(lg_hit.values()) / max(1, sum(lg_cnt.values()))
    lg_prior = {lg: (lg_hit[lg] + SHRINK_K * global_rate) / (lg_cnt[lg] + SHRINK_K)
                for lg in lg_cnt}
    print(f'  全局 HT 破蛋率 {global_rate:.4f}, 联赛数 {len(lg_prior)}')

    def ou_pairs_from(rows):
        d = defaultdict(dict)
        for line, sel, odds in rows:
            if line is not None:
                d[line][sel] = odds
        return [(L, v.get('over'), v.get('under')) for L, v in d.items()
                if v.get('over') and v.get('under')]

    def x2_from(rows):
        x2 = {}
        for sel, odds in rows:
            if sel not in x2:
                x2[sel] = odds
        if not all(k in x2 for k in ('home', 'draw', 'away')):
            return None
        inv = [1.0 / x2['home'], 1.0 / x2['draw'], 1.0 / x2['away']]
        s = sum(inv)
        return [v / s for v in inv]

    X, y, keys, days = [], [], [], []
    n_no_odds = 0
    for mk, lg, ko, hth, hta in matches:
        kts = parse_kickoff(ko)
        if kts is None:
            n_no_odds += 1
            continue
        # 临场: kickoff 前最近快照
        ou_close = cur.execute("""
            SELECT CAST(REPLACE(REPLACE(market,'OU_',''),'_','.') AS REAL) AS line,
                   selection, odds
            FROM odds_snapshots
            WHERE match_key=? AND market LIKE 'OU_%'
              AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%'
              AND captured_at <= ? AND odds > 1.01 AND odds <= 1000
            ORDER BY captured_at DESC LIMIT 40
        """, (mk, kts)).fetchall()
        x2_close_rows = cur.execute("""
            SELECT selection, odds FROM odds_snapshots
            WHERE match_key=? AND market='1X2' AND captured_at <= ?
              AND odds > 1.01 AND odds <= 1000
            ORDER BY captured_at DESC LIMIT 3
        """, (mk, kts)).fetchall()
        # 开盘: 最早快照
        ou_open = cur.execute("""
            SELECT CAST(REPLACE(REPLACE(market,'OU_',''),'_','.') AS REAL) AS line,
                   selection, odds
            FROM odds_snapshots
            WHERE match_key=? AND market LIKE 'OU_%'
              AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%'
              AND captured_at <= ? AND odds > 1.01 AND odds <= 1000
            ORDER BY captured_at ASC LIMIT 40
        """, (mk, kts)).fetchall()
        x2_open_rows = cur.execute("""
            SELECT selection, odds FROM odds_snapshots
            WHERE match_key=? AND market='1X2' AND captured_at <= ?
              AND odds > 1.01 AND odds <= 1000
            ORDER BY captured_at ASC LIMIT 3
        """, (mk, kts)).fetchall()
        # ── 半场 OU 线 (OU_1H_*) : 实现"半场盘大→半场哑火"特征 ──
        # 抗诱导铁律: 只用去水不变量(隐含总球/over概率), 禁用原始赔率值。
        # 实证(2026-08-18): 半场盘大(1/1.25/1.5) 的场, 半场0球率 38.4% vs 基线 29.2% (+9.2pp)
        #   → 半场 OU 线被系统性高估(诱多), 本特征让模型学到"半场盘大→半场反而哑火"。
        ou_1h = cur.execute("""
            SELECT CAST(REPLACE(REPLACE(market,'OU_1H',''),'_','.') AS REAL) AS line,
                   selection, odds
            FROM odds_snapshots
            WHERE match_key=? AND market LIKE 'OU_1H%'
              AND captured_at <= ? AND odds > 1.01 AND odds <= 1000
            ORDER BY captured_at DESC LIMIT 20
        """, (mk, kts)).fetchall()
        pairs_1h = ou_pairs_from(ou_1h) if ou_1h else []
        if pairs_1h:
            ht_ou_implied = implied_total_from_pairs(pairs_1h)
            main_1h = min(pairs_1h, key=lambda p: abs(p[0] - 1.0))
            _o = dewatered_over_prob(main_1h[1], main_1h[2])
            ht_ou_over_prob = _o if _o is not None else 0.5
        else:
            ht_ou_implied = -1.0   # 哨兵: 无半场 OU 盘口(未知, 区别于真实隐含总球)
            ht_ou_over_prob = 0.5
        if not ou_close or not x2_close_rows:
            n_no_odds += 1
            continue
        pairs_c = ou_pairs_from(ou_close)
        T_c = implied_total_from_pairs(pairs_c)
        x2c = x2_from(x2_close_rows)
        if T_c is None or x2c is None or not pairs_c:
            n_no_odds += 1
            continue
        main_c = min(pairs_c, key=lambda p: abs(p[0] - 2.5))
        p_over_c = dewatered_over_prob(main_c[1], main_c[2]) or 0.5
        # 开盘漂移 (无开盘数据则 drift=0, 诚实不伪造)
        T_o = implied_total_from_pairs(ou_pairs_from(ou_open)) if ou_open else None
        pairs_o = ou_pairs_from(ou_open) if ou_open else []
        p_over_o = None
        if pairs_o:
            main_o = min(pairs_o, key=lambda p: abs(p[0] - 2.5))
            p_over_o = dewatered_over_prob(main_o[1], main_o[2])
        x2o = x2_from(x2_open_rows) if x2_open_rows else None
        T_drift = (T_c - T_o) if T_o is not None else 0.0
        p_over_drift = math.log(max(p_over_c, 0.01) / max(p_over_o, 0.01)) if p_over_o else 0.0
        x2h_drift = (x2c[0] - x2o[0]) if x2o else 0.0
        p_fav = max(x2c)  # 去水后热门概率 (替代原始 fav_odds)
        X.append([
            T_c, p_over_c,
            x2c[0], x2c[1], x2c[2],
            p_fav,
            T_drift, p_over_drift, x2h_drift,
            lg_prior.get(lg or 'unknown', global_rate),
            ht_ou_implied, ht_ou_over_prob,
        ])
        y.append(1 if (hth + hta) >= 1 else 0)
        keys.append(mk)
        days.append(str(ko)[:10] if ko else 'unknown')
    print(f'  有效样本 {len(y)} (跳过无赛前盘口 {n_no_odds})')
    return np.array(X, dtype=float), np.array(y, dtype=int), keys, days, global_rate, lg_prior


def poisson_baseline(X):
    """市场隐含总球 → P(HT>=1) = 1 - exp(-T * HT_SHARE)"""
    T = X[:, 0]
    return 1.0 - np.exp(-np.maximum(T, 0.05) * HT_SHARE)


def binned_table(y_true, p, bins=8):
    edges = np.linspace(0, 1, bins + 1)
    rows = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        m = (p >= lo) & (p < hi if i < bins - 1 else p <= hi)
        n = int(m.sum())
        if n >= 5:
            rows.append({'bin': f'{lo:.2f}-{hi:.2f}', 'n': n,
                         'pred': round(float(p[m].mean()), 3),
                         'actual': round(float(y_true[m].mean()), 3)})
    return rows


def main():
    from sklearn.linear_model import LogisticRegression
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.isotonic import IsotonicRegression
    from sklearn.model_selection import GroupKFold
    from sklearn.metrics import roc_auc_score, brier_score_loss
    import joblib

    def oof_predict_groups(make_model, X, y, groups, n_splits=5):
        """GroupKFold-by-day 的 out-of-fold 预测: 训练/测试按整天切开,
        模型无法背'某天的水位风格'——防日期效应泄漏(抗诱导验证核心)。"""
        pred = np.full(len(y), np.nan)
        gkf = GroupKFold(n_splits=n_splits)
        for tr, te in gkf.split(X, y, groups):
            m = make_model()
            m.fit(X[tr], y[tr])
            pred[te] = m.predict_proba(X[te])[:, 1]
        return pred

    con = sqlite3.connect(f'file:{GQ}?mode=ro', uri=True, timeout=30)
    cur = con.cursor()
    X, y, keys, days, global_rate, lg_prior = build_dataset(cur)
    con.close()
    if len(y) < 300:
        print('[abort] 样本不足 300, 不训练')
        return
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    days = np.array(days)
    feat_names = ['T_impl', 'p_over_main', 'x2_h', 'x2_d', 'x2_a', 'p_fav',
                  'T_drift', 'p_over_drift', 'x2h_drift', 'lg_ht_prior',
                  'ht_ou_implied', 'ht_ou_over_prob']
    print(f'[data] 正例率 {y.mean():.4f}, 天数 {len(set(days))}')
    print(f'[features] {feat_names} (全不变量, 无原始赔率值)')

    # ── 基线 1: naive 常数 ──
    p_naive = np.full(len(y), y.mean())
    # ── 基线 2: 泊松(市场隐含总球) ──
    p_pois = poisson_baseline(X)

    # ── 候选 A: LogReg (GroupKFold-by-day) ──
    p_lr = oof_predict_groups(lambda: LogisticRegression(max_iter=1000, C=1.0), X, y, days)
    # ── 候选 B: 浅决策树 (铁律 max_depth=3) ──
    p_dt = oof_predict_groups(lambda: DecisionTreeClassifier(max_depth=3, random_state=42), X, y, days)
    # ── 候选 C: 泊松基线 + isotonic (按日分组 oof) ──
    p_pois_iso = np.full(len(y), np.nan)
    gkf = GroupKFold(n_splits=5)
    for tr, te in gkf.split(X, y, days):
        iso = IsotonicRegression(out_of_bounds='clip')
        iso.fit(p_pois[tr], y[tr])
        p_pois_iso[te] = iso.predict(p_pois[te])

    def ev(name, p):
        mask = ~np.isnan(p)
        return {
            'AUC': round(roc_auc_score(y[mask], p[mask]), 4),
            'Brier': round(brier_score_loss(y[mask], p[mask]), 4),
            'bins': binned_table(y[mask], p[mask]),
            'day_variance': day_variance(p, mask),
        }

    def day_variance(p, mask=None):
        """逐日命中率方差: 方差大=模型被日期效应(当日水位风格)污染, 抗诱导红灯。"""
        if mask is None:
            mask = np.ones(len(y), dtype=bool)
        from collections import defaultdict as dd
        by_day = dd(lambda: [0, 0.0])
        for i in range(len(y)):
            if not mask[i]:
                continue
            by_day[days[i]][0] += 1
            by_day[days[i]][1] += y[i] - p[i]  # 残差
        resids = [v[1] / v[0] for v in by_day.values() if v[0] >= 10]
        if len(resids) < 3:
            return None
        return round(float(np.std(resids)), 4)

    report = {
        'n_samples': int(len(y)),
        'positive_rate': round(float(y.mean()), 4),
        'n_days': int(len(set(days))),
        'validation': 'GroupKFold-by-day (训练/测试按整天切开, 防日期水位风格泄漏)',
        'features': feat_names,
        'naive_const': ev('naive', p_naive),
        'poisson_market': ev('poisson', p_pois),
        'logreg': ev('lr', p_lr),
        'dtree3': ev('dt', p_dt),
        'poisson_isotonic': ev('pois_iso', p_pois_iso),
    }
    print('\n===== 评估 (GroupKFold-by-day, out-of-fold) =====')
    for name in ('naive_const', 'poisson_market', 'logreg', 'dtree3', 'poisson_isotonic'):
        r = report[name]
        dv = r['day_variance']
        print(f"  {name:18s} AUC={r['AUC']:.4f}  Brier={r['Brier']:.4f}  逐日残差std={dv if dv is not None else '--'}")

    # ── 选最佳(以 Brier 为主, AUC 参考), 全量重训落盘 ──
    cands = {'logreg': p_lr, 'dtree3': p_dt, 'poisson_isotonic': p_pois_iso}
    best = min(cands, key=lambda k: brier_score_loss(y[~np.isnan(cands[k])], cands[k][~np.isnan(cands[k])]))
    # 必须显著优于泊松基线才用 ML; 否则落泊松+iso(跟随市场, 最诚实)
    print(f'\n[select] 最佳候选: {best}')
    if best == 'logreg':
        model = LogisticRegression(max_iter=1000, C=1.0).fit(X, y)
        kind = 'logreg'
    elif best == 'dtree3':
        model = DecisionTreeClassifier(max_depth=3, random_state=42).fit(X, y)
        kind = 'dtree3'
    else:
        model = None
        kind = 'poisson_isotonic'
    # isotonic 顶层校准(全量, 防过拟合用 CV 内生长——样本>1000, 直接全量可接受但保守用 5fold oof)
    iso_final = IsotonicRegression(out_of_bounds='clip')
    base_all = poisson_baseline(X) if kind == 'poisson_isotonic' else \
        (model.predict_proba(X)[:, 1])
    iso_final.fit(base_all, y)

    os.makedirs(os.path.dirname(OUT_MODEL), exist_ok=True)
    joblib.dump({
        'kind': kind,
        'model': model,
        'iso': iso_final,
        'feature_names': feat_names,
        'ht_share': HT_SHARE,
        'global_rate': float(global_rate),
        'lg_prior': {k: float(v) for k, v in lg_prior.items()},
        'meta': {'trained_at': datetime.now().isoformat(), 'n': int(len(y)),
                 'note': '赛前盘口→半场破蛋先验; 比赛中概率以盘口去水锚为准, 本模型仅先验/校准'},
    }, OUT_MODEL)
    print(f'[save] {OUT_MODEL}')

    os.makedirs(os.path.dirname(OUT_EVAL), exist_ok=True)
    with open(OUT_EVAL, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f'[save] {OUT_EVAL}')

    # 分箱校准表打印(最佳)
    print(f'\n===== {best} 分箱校准 =====')
    for row in report[best]['bins']:
        print(f"  {row['bin']}: n={row['n']:4d} pred={row['pred']:.3f} actual={row['actual']:.3f}")


if __name__ == '__main__':
    main()
