#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
historical_comp_system.py — 历史库 + 模型分析 系统 (GQ-free)
================================================================
取代 gq_comp_system.py 对 GQ 实时采集的依赖。数据源改为
football_data.db 的 odds_features 表(312K 场, 含 open/close 1X2 +
模型原生9特征 + 真实赛果 outcome)。完全不依赖 GQ 实时采集。

核心四层 (GQ-free):
  1. 同赔率结构历史场次匹配 (SSoT = odds_features.close_*)
     - 精确分桶 + 最近邻 TOP-K
     - 赛果分布 / 各方向ROI / 校准偏差(实际-隐含)
  2. 模型分析层A (mispricing_detector, LGBM, 9个盘口派生特征)
     - 在匹配到的历史场次"真实特征行"上跑模型(不搞特征猜测)
     - 输出: 模型对"市场热门(argmax)是否命中"的概率
  3. 模型分析层B (historical_1x2_model, 在历史 odds_features 312K 上现训的1X2模型)
     - 取代在历史上不可运行的 wc_main_v1(WC专用/115样本/77维实时特征缺失)
     - 在匹配到的历史场次"真实特征行"上跑模型, 输出 P(胜/平/负)
  4. 四方三角校正: 庄家隐含概率 vs 历史实证频率 vs mispricing读 vs 现训1X2读
     ⚠ 模型仅作四方校正, 不假装击败庄家; 单庄是否含edge须逐场判定, 不预设.

用法:
  python historical_comp_system.py --h 2.22 --d 3.20 --a 2.94
  python historical_comp_system.py --h 2.22 --d 3.20 --a 2.94 --html out.html --json out.json
  python historical_comp_system.py --sweep --league 英超 --limit 8000
  python historical_comp_system.py --sweep --topk            # 输出模型 Top-K ROI 校准
"""
import sqlite3, argparse, html, json, sys, math
import numpy as np

DB = 'D:/Architecture/data/football_data.db'
MODEL = 'D:/Architecture/saved_models/mispricing_detector.joblib'
MODEL_1X2 = 'D:/Architecture/data/historical_1x2_model.joblib'  # 现训第三层

# 模型期望的9特征(顺序必须与训练一致)
MODEL_FEATS = ['drift_h', 'drift_d', 'drift_a', 'drift_mag',
               'overround', 'home_edge', 'argmax_imp', 'cimp_d', 'oimp_d']

# 现训1X2模型的9特征(顺序必须与训练一致)
MODEL_1X2_FEATS = ['imp_h', 'imp_d', 'imp_a',
                   'drift_h', 'drift_d', 'drift_a',
                   'overround', 'home_edge', 'sigma_trap']

# ---------- 全局缓存 ----------
_CACHE = None
_MODEL = None
_MODEL1X2 = None


def load_features():
    """加载 odds_features(模型原生特征+赛果)。一次性, 全表缓存。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("""SELECT close_h,close_d,close_a, drift_h,drift_d,drift_a,
                          overround,home_edge, cimp_h,cimp_d,cimp_a, imp_d,
                          outcome, league, home_team, away_team, match_date,
                          home_score, away_score,
                          imp_h, imp_d, imp_a, sigma_trap
                   FROM odds_features
                   WHERE outcome IS NOT NULL AND close_h>0 AND close_d>0 AND close_a>0""")
    rows = cur.fetchall(); con.close()
    CH = np.array([r[0] for r in rows], dtype=float)
    CD = np.array([r[1] for r in rows], dtype=float)
    CA = np.array([r[2] for r in rows], dtype=float)
    dh = np.array([r[3] for r in rows], dtype=float)
    dd = np.array([r[4] for r in rows], dtype=float)
    da = np.array([r[5] for r in rows], dtype=float)
    over = np.array([r[6] for r in rows], dtype=float)
    home_edge = np.array([r[7] for r in rows], dtype=float)
    cimp_h = np.array([r[8] for r in rows], dtype=float)
    cimp_d = np.array([r[9] for r in rows], dtype=float)
    cimp_a = np.array([r[10] for r in rows], dtype=float)
    imp_d = np.array([r[11] for r in rows], dtype=float)
    IMP_H = np.array([r[19] for r in rows], dtype=float)
    IMP_D = np.array([r[20] for r in rows], dtype=float)
    IMP_A = np.array([r[21] for r in rows], dtype=float)
    SIGMA = np.array([r[22] for r in rows], dtype=float)
    drift_mag = np.maximum(np.maximum(np.abs(dh), np.abs(dd)), np.abs(da))
    argmax_imp = np.maximum(np.maximum(cimp_h, cimp_d), cimp_a)  # 热门(最小赔率)的clean隐含
    X = np.column_stack([dh, dd, da, drift_mag, over, home_edge, argmax_imp, cimp_d, imp_d])
    X1 = np.column_stack([IMP_H, IMP_D, IMP_A, dh, dd, da, over, home_edge, SIGMA])
    meta = [dict(outcome=r[12], league=r[13], home=r[14], away=r[15], date=r[16],
                hs=r[17], aw=r[18], ch=r[0], cd=r[1], ca=r[2]) for r in rows]
    fav = np.argmin(np.column_stack([CH, CD, CA]), axis=1)  # 0=H,1=D,2=A
    _CACHE = dict(CH=CH, CD=CD, CA=CA, X=X, X1=X1, meta=meta, fav=fav, n=len(rows))
    return _CACHE


def load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    import joblib, warnings
    warnings.filterwarnings('ignore')
    m = joblib.load(MODEL)
    _MODEL = m
    return m


def load_1x2_model():
    global _MODEL1X2
    if _MODEL1X2 is not None:
        return _MODEL1X2
    import joblib, warnings
    warnings.filterwarnings('ignore')
    m = joblib.load(MODEL_1X2)
    _MODEL1X2 = m
    return m


# ---------- 工具 ----------
def devig(h, d, a):
    inv = 1.0 / h + 1.0 / d + 1.0 / a
    return (1.0 / h) / inv, (1.0 / d) / inv, (1.0 / a) / inv


def bucket_key(h, d, a, tol):
    r = lambda x: round(round(x / tol) * tol, 2)
    return (r(h), r(d), r(a))


def pct(x):
    return f"{x*100:.1f}%"


def favorite_side(h, d, a):
    m = min(h, d, a)
    return 'H' if m == h else ('A' if m == a else 'D')


# ---------- 实证聚合 ----------
def aggregate(subset_meta, th, td, ta):
    """subset_meta: list of dict(outcome,...). 返回赛果分布/ROI/校准偏差。"""
    n = len(subset_meta)
    if n == 0:
        return None
    from collections import Counter
    cnt = Counter(m['outcome'] for m in subset_meta)
    H = cnt.get('H', 0); D = cnt.get('D', 0); A = cnt.get('A', 0)
    # ROI: 每场以"该场自身收盘赔率"下注1单位(忠实于 odds_comp_finder 原方法)
    def roi(side_idx, res_char):
        tot = 0.0
        for m in subset_meta:
            odds = (m['ch'], m['cd'], m['ca'])[side_idx]
            tot += (odds - 1) if m['outcome'] == res_char else -1.0
        return tot / n
    roi_h, roi_d, roi_a = roi(0, 'H'), roi(1, 'D'), roi(2, 'A')
    ph, pd, pa = devig(th, td, ta)
    fav = favorite_side(th, td, ta)
    fav_hit = sum(1 for m in subset_meta if m['outcome'] == fav) / n
    return dict(n=n, H=H, D=D, A=A, pH=H / n, pD=D / n, pA=A / n,
                roi_h=roi_h, roi_d=roi_d, roi_a=roi_a,
                imp_h=ph, imp_d=pd, imp_a=pa, fav=fav, fav_hit=fav_hit,
                over=(H + A) / n)


# ---------- 单场分析 ----------
def analyze_target(h, d, a, tol=0.05, k=40, min_sample=5):
    c = load_features()
    CH, CD, CA = c['CH'], c['CD'], c['CA']
    bt = bucket_key(h, d, a, tol)
    emask = (np.round(np.round(CH / tol) * tol, 2) == bt[0]) & \
            (np.round(np.round(CD / tol) * tol, 2) == bt[1]) & \
            (np.round(np.round(CA / tol) * tol, 2) == bt[2])
    exact_idx = np.nonzero(emask)[0]
    dist2 = (CH - h) ** 2 + (CD - d) ** 2 + (CA - a) ** 2
    near_idx = np.argpartition(dist2, k)[:k]
    exact_meta = [c['meta'][i] for i in exact_idx]
    near_meta = [c['meta'][i] for i in near_idx]
    agg_ex = aggregate(exact_meta, h, d, a) if len(exact_meta) else None
    agg_ne = aggregate(near_meta, h, d, a)
    if agg_ex and agg_ex['n'] >= min_sample:
        primary, mode = agg_ex, 'exact'
    else:
        primary, mode = agg_ne, 'nearest_fallback'

    # 模型读: 在 primary 集合的真实特征行上跑模型
    pidx = exact_idx if (agg_ex and agg_ex['n'] >= min_sample) else near_idx
    model_read = model_read_on_rows(pidx, h, d, a)
    model_1x2 = model_1x2_read_on_rows(pidx)           # 第三层
    point_1x2 = point_1x2_predict(h, d, a)             # 单点补充(中性假设)

    # 四方校正: 庄家隐含 / 历史实证 / mispricing_detector / 现训1X2模型
    ph, pd, pa = devig(h, d, a)
    fav = favorite_side(h, d, a)
    book_fav_prob = max(ph, pd, pa)
    triangulation = dict(
        book_implied_fav=fav,
        book_implied_fav_prob=book_fav_prob,
        empirical_fav_hit=primary['fav_hit'] if primary else None,
        model_p_argmax_hit=(model_read.get('p_argmax_hit_mean')
                             if model_read and 'error' not in model_read else None),
        model_1x2=(model_1x2 if (model_1x2 and 'error' not in model_1x2) else None),
    )
    return dict(target=(h, d, a), tol=tol, agg_ex=agg_ex, agg_ne=agg_ne,
                primary=primary, mode=mode, exact_n=len(exact_meta),
                near_n=len(near_meta), model_read=model_read,
                model_1x2=model_1x2, point_1x2=point_1x2,
                triangulation=triangulation)


def model_read_on_rows(idxs, h, d, a):
    """在给定历史行的真实特征行上跑 mispricing_detector, 返回模型读。"""
    if len(idxs) == 0:
        return None
    c = load_features()
    X = c['X'][idxs]
    # 过滤 NaN 行
    ok = ~np.any(np.isnan(X), axis=1)
    if ok.sum() == 0:
        return None
    X = X[ok]
    try:
        m = load_model()
        p = m['model'].predict_proba(X)[:, 1]  # P(argmax_hit)
    except Exception as e:
        return {'error': str(e)}
    fav = favorite_side(h, d, a)
    return dict(p_argmax_hit_mean=float(p.mean()),
                p_argmax_hit_std=float(p.std()),
                n_rows=int(ok.sum()),
                fav=fav,
                note="模型=mispricing_detector(LGBM), 输出=P(市场热门argmax命中赛果)")


def model_1x2_read_on_rows(idxs):
    """第三层: 在给定历史行的真实特征行上跑现训 historical_1x2_model, 返回 P(H/D/A) 均值。"""
    if len(idxs) == 0:
        return None
    c = load_features()
    X1 = c['X1'][idxs]
    ok = ~np.any(np.isnan(X1), axis=1)
    if ok.sum() == 0:
        return None
    X1 = X1[ok]
    try:
        m = load_1x2_model()
        p = m['model'].predict_proba(X1)         # (n,3) H/D/A
    except Exception as e:
        return {'error': str(e)}
    return dict(pH=float(p[:, 0].mean()), pD=float(p[:, 1].mean()),
                pA=float(p[:, 2].mean()),
                n_rows=int(ok.sum()),
                oos_auc=float(m['meta'].get('oos_auc', float('nan'))),
                note="模型=historical_1x2_model(LGBM, 在历史 odds_features 312K 上现训); 输出=该结构下 P(胜/平/负)")


def point_1x2_predict(h, d, a):
    """对单点目标用现训1X2模型做点预测。
    ⚠ 中性假设: 目标仅给收盘赔率, 无开盘→故 drift=0, sigma_trap=0, home_edge=0(缺失信息不伪造)。
    仅作补充, 主信号应看 cohort 真实特征行读。"""
    try:
        m = load_1x2_model()
    except Exception as e:
        return {'error': str(e)}
    inv = 1.0 / h + 1.0 / d + 1.0 / a
    imp_h, imp_d, imp_a = (1.0 / h) / inv, (1.0 / d) / inv, (1.0 / a) / inv
    overround = inv - 1.0
    X1 = np.array([[imp_h, imp_d, imp_a, 0.0, 0.0, 0.0, overround, 0.0, 0.0]])
    try:
        p = m['model'].predict_proba(X1)[0]
    except Exception as e:
        return {'error': str(e)}
    return dict(pH=float(p[0]), pD=float(p[1]), pA=float(p[2]),
                caveat="无开盘漂移/陷阱信息(drift=sigma=home_edge=0=中性假设), 仅供参照; 以cohort真实特征行读为准")


# ---------- 批量扫描: 模型在历史库上的校准 ----------
def sweep(league=None, date_from=None, date_to=None, limit=None, topk=False):
    c = load_features()
    X = c['X']; meta = c['meta']; fav = c['fav']; outcome = np.array([m['outcome'] for m in meta])
    mask = ~np.any(np.isnan(X), axis=1)
    if league:
        mask &= np.array([(league in (m['league'] or '')) for m in meta])
    if date_from:
        mask &= np.array([(m['date'] or '') >= date_from for m in meta])
    if date_to:
        mask &= np.array([(m['date'] or '') <= date_to for m in meta])
    idx = np.nonzero(mask)[0]
    if limit:
        idx = idx[:limit]
    Xs = X[idx]
    actual_fav = fav[idx]                       # 0=H,1=D,2=A 整数
    out_chars = np.array([m['outcome'] for m in meta])[idx]
    fav_chars = np.array(['H', 'D', 'A'])[actual_fav]
    hit = (out_chars == fav_chars).astype(int)  # 市场热门(argmax)是否命中
    m = load_model()
    p = m['model'].predict_proba(Xs)[:, 1]
    from sklearn.metrics import roc_auc_score, accuracy_score
    auc = float(roc_auc_score(hit, p))
    order = np.argsort(-p)
    # 第三层模型B: 现训1X2 在历史库上的准度
    X1 = c['X1'][idx]
    y_int = np.array([{'H': 0, 'D': 1, 'A': 2}[o] for o in out_chars])
    try:
        m1 = load_1x2_model()
        p1 = m1['model'].predict_proba(X1)
        auc1 = float(roc_auc_score(y_int, p1, multi_class='ovo'))
        acc1 = float(accuracy_score(y_int, p1.argmax(1)))
    except Exception:
        auc1, acc1 = float('nan'), float('nan')
    res = dict(n=int(len(idx)), auc=auc,
               empirical_fav_hit=float(hit.mean()),
               model_mean_p=float(p.mean()),
               modelB_auc=auc1, modelB_acc=acc1,
               topk={})
    if topk:
        close = np.column_stack([c['CH'][idx], c['CD'][idx], c['CA'][idx]])
        for K in (500, 1000, 2000, 5000, 10000):
            K = min(K, len(order))
            o = order[:K]
            odds_fav = np.take_along_axis(close[o], actual_fav[o][:, None], axis=1).ravel()
            win = hit[o].astype(float)
            roi = float((win * odds_fav - 1).mean())
            res['topk'][K] = dict(hit=float(win.mean()), roi=roi,
                                  avg_odds=float(odds_fav.mean()))
    # 联赛级模型准度 (仅扫描已筛选 idx 内的联赛)
    leagues = {}
    present = {}
    for j in idx:
        lg = meta[j]['league'] or '(未知)'
        present.setdefault(lg, []).append(j)
    for lg, li in present.items():
        if len(li) >= 50:
            li = np.array(li)
            hh = (out_chars[li] == fav_chars[li]).astype(int)
            pp = p[li]
            try:
                la = float(roc_auc_score(hh, pp))
            except Exception:
                la = float('nan')
            leagues[lg] = dict(n=len(li), auc=la, fav_hit=float(hh.mean()))
    res['leagues'] = leagues
    return res


# ---------- 报告 ----------
def text_report(r):
    h, d, a = r['target']
    L = []
    L.append(f"=== 历史库+模型分析: 1X2 主{h}/平{d}/客{a} (tol={r['tol']}) ===")
    L.append(f"数据源: football_data.odds_features (312K场, 真实赛果, 模型原生特征). GQ: 未依赖.")
    L.append("=" * 70)
    def block(title, agg):
        if agg is None:
            L.append(f"\n【{title}】 n=0 无历史同结构"); return
        L.append(f"\n【{title}】 样本 n={agg['n']}")
        L.append(f"  赛果分布  胜(H){agg['H']}({pct(agg['pH'])}) | 平(D){agg['D']}({pct(agg['pD'])}) | 负(A){agg['A']}({pct(agg['pA'])})")
        L.append(f"  庄家隐含   H {pct(agg['imp_h'])} | D {pct(agg['imp_d'])} | A {pct(agg['imp_a'])}")
        L.append(f"  ROI(每场自身收盘赔率下注1单位): 押H{agg['roi_h']:+.3f} 押D{agg['roi_d']:+.3f} 押A{agg['roi_a']:+.3f}")
        db_h = agg['pH'] - agg['imp_h']
        L.append(f"  校准偏差(实际-隐含): H{db_h:+.3f} D{agg['pD']-agg['imp_d']:+.3f} A{agg['pA']-agg['imp_a']:+.3f}")
        L.append(f"  实证热门(argmax)命中率: {pct(agg['fav_hit'])} (热门={agg['fav']})")
    block(f"精确分桶(同赔率结构, n={r['exact_n']})", r['agg_ex'])
    block(f"最近邻TOP-{r['near_n']} ({r['mode']})", r['agg_ne'])
    # 模型读
    mr = r['model_read']
    L.append("\n【模型读 mispricing_detector】")
    if mr is None or 'error' in mr:
        L.append(f"  (不可用: {mr.get('error') if mr else 'no rows'})")
    else:
        L.append(f"  P(市场热门argmax命中)均值 = {mr['p_argmax_hit_mean']:.3f} ± {mr['p_argmax_hit_std']:.3f}  (样本{mr['n_rows']}行真实特征)")
    # 三角校正
    t = r['triangulation']
    L.append("\n【四方三角校正】")
    L.append(f"  庄家隐含热门概率 : {t['book_implied_fav']} = {pct(t['book_implied_fav_prob'])}")
    L.append(f"  历史实证热门命中 : {pct(t['empirical_fav_hit']) if t['empirical_fav_hit'] is not None else '—'}")
    L.append(f"  模型读A P(热门命中): {t['model_p_argmax_hit']:.3f}" if t['model_p_argmax_hit'] is not None else "  模型读A: —")
    m1 = t.get('model_1x2')
    L.append(f"  模型读B 现训1X2 P(H/D/A): " + (f"{pct(m1['pH'])}/{pct(m1['pD'])}/{pct(m1['pA'])} (OOS AUC={m1['oos_auc']:.3f})" if m1 else "—"))
    p1 = r.get('point_1x2')
    if p1 and 'error' not in p1:
        L.append(f"  [单点补充] 现训1X2点预测 P(H/D/A)={pct(p1['pH'])}/{pct(p1['pD'])}/{pct(p1['pA'])}  ⚠无开盘漂移/陷阱信息(中性假设)")
    L.append("\n⚠ 模型仅作四方校正(庄家/实证/模型A/模型B对照), 不假装击败庄家; 单庄是否含edge须逐场判定, 不预设。")
    return "\n".join(L)


def html_report(r):
    h, d, a = r['target']
    mr = r['model_read']
    t = r['triangulation']
    def cell(agg, label):
        if agg is None:
            return f"<td colspan=6 style='color:#f55'>{label}: n=0 无历史同结构</td>"
        db_h = agg['pH'] - agg['imp_h']
        return (f"<td>n={agg['n']}<br>({label})</td>"
                f"<td>主{pct(agg['pH'])}<br>平{pct(agg['pD'])}<br>客{pct(agg['pA'])}</td>"
                f"<td>主{pct(agg['imp_h'])}<br>平{pct(agg['imp_d'])}<br>客{pct(agg['imp_a'])}</td>"
                f"<td>主{agg['roi_h']:+.3f}<br>平{agg['roi_d']:+.3f}<br>客{agg['roi_a']:+.3f}</td>"
                f"<td>H{db_h:+.3f}<br>D{agg['pD']-agg['imp_d']:+.3f}<br>A{agg['pA']-agg['imp_a']:+.3f}</td>"
                f"<td>热门{agg['fav']} {pct(agg['fav_hit'])}</td>")
    mr_html = ("— 不可用 —" if (mr is None or 'error' in mr)
               else f"P(市场热门argmax命中) = <b>{mr['p_argmax_hit_mean']:.3f}</b> ± {mr['p_argmax_hit_std']:.3f}<br>({mr['n_rows']}行真实历史特征) <br><span style='color:#9ab'>{mr['note']}</span>")
    m1 = t.get('model_1x2')
    m1_html = ("— 不可用 —" if not m1
               else f"P(胜)=<b>{pct(m1['pH'])}</b>  P(平)=<b>{pct(m1['pD'])}</b>  P(负)=<b>{pct(m1['pA'])}</b><br>({m1['n_rows']}行真实历史特征, 模型OOS AUC={m1['oos_auc']:.3f}) <br><span style='color:#9ab'>{m1['note']}</span>")
    p1 = r.get('point_1x2')
    p1_html = ("— 不可用 —" if not p1 or 'error' in p1
               else f"P(胜/平/负)={pct(p1['pH'])}/{pct(p1['pD'])}/{pct(p1['pA'])} <br><span style='color:#e0a'>{p1['caveat']}</span>")
    tri = (f"<tr><td>庄家隐含热门概率</td><td colspan=3>{t['book_implied_fav']} = {pct(t['book_implied_fav_prob'])}</td></tr>"
           f"<tr><td>历史实证热门命中</td><td colspan=3>{pct(t['empirical_fav_hit']) if t['empirical_fav_hit'] is not None else '—'}</td></tr>"
           f"<tr><td>模型读A P(热门命中)</td><td colspan=3>{t['model_p_argmax_hit']:.3f}" + ("" if t['model_p_argmax_hit'] is not None else "—") + "</td></tr>"
           + (f"<tr><td>模型读B 现训1X2 P(H/D/A)</td><td colspan=3>{pct(m1['pH'])} / {pct(m1['pD'])} / {pct(m1['pA'])}</td></tr>" if m1 else ""))
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>历史库+模型分析</title></head>
<body style='font-family:Segoe UI,Microsoft YaHei;background:#0f1115;color:#e6e6e6;padding:24px'>
<h2>历史库 + 模型分析 (GQ-free)</h2>
<p style='color:#9ab'>目标1X2 主 <b>{h}</b> / 平 <b>{d}</b> / 客 <b>{a}</b> ｜ 数据源: football_data.odds_features (312K场) ｜ <b>GQ未依赖</b></p>
<h3>① 精确分桶（同赔率结构, n={r['exact_n']}）</h3>
<table border=1 cellpadding=6 cellspacing=0 style='border-collapse:collapse;font-size:13px'>
<tr><th>样本n</th><th>实际赛果频率</th><th>庄家隐含</th><th>各方向ROI</th><th>校准偏差(实际-隐含)</th><th>热门命中</th></tr>
{cell(r['agg_ex'],'exact')}</table>
<h3>② 最近邻 TOP-{r['near_n']}（{r['mode']}）</h3>
<table border=1 cellpadding=6 cellspacing=0 style='border-collapse:collapse;font-size:13px'>
<tr><th>样本n</th><th>实际赛果频率</th><th>庄家隐含</th><th>各方向ROI</th><th>校准偏差(实际-隐含)</th><th>热门命中</th></tr>
{cell(r['agg_ne'],'nearest')}</table>
<h3>③ 模型读A (mispricing_detector)</h3>
<p style='font-size:14px'>{mr_html}</p>
<h3>④ 模型读B (现训 historical_1x2_model, 取代不可用的 wc_main_v1)</h3>
<p style='font-size:14px'>{m1_html}</p>
<h3>④b 单点点预测（中性假设, 仅供参考）</h3>
<p style='font-size:13px'>{p1_html}</p>
<h3>⑤ 四方三角校正</h3>
<table border=1 cellpadding=6 cellspacing=0 style='border-collapse:collapse;font-size:13px'>{tri}</table>
<p style='color:#888;font-size:12px'>⚠ 模型仅作四方校正(庄家隐含/历史实证/模型读A/模型读B对照), 不假装击败庄家; 单庄是否含edge须逐场判定, 不预设。ROI为描述性历史统计。</p>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--h', type=float, help='目标主胜收盘赔率')
    ap.add_argument('--d', type=float, help='目标平局收盘赔率')
    ap.add_argument('--a', type=float, help='目标客胜收盘赔率')
    ap.add_argument('--tol', type=float, default=0.05)
    ap.add_argument('--k', type=int, default=40)
    ap.add_argument('--min-sample', type=int, default=5)
    ap.add_argument('--sweep', action='store_true', help='批量扫描: 模型在历史库上的校准(AUC/Top-K ROI/联赛级)')
    ap.add_argument('--league', default=None)
    ap.add_argument('--date-from', default=None)
    ap.add_argument('--date-to', default=None)
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--topk', action='store_true', help='sweep时输出 Top-K ROI 校准')
    ap.add_argument('--html', default=None)
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    if args.sweep:
        res = sweep(league=args.league, date_from=args.date_from,
                    date_to=args.date_to, limit=args.limit, topk=args.topk)
        print(f"=== 模型在历史库上的校准 ({res['n']} 场) ===")
        print(f"AUC(argmax_hit) = {res['auc']:.4f}")
        print(f"现训1X2模型B: AUC(ovo)={res['modelB_auc']:.4f} acc={res['modelB_acc']:.4f}")
        print(f"实证热门命中 = {pct(res['empirical_fav_hit'])} | 模型均值P = {res['model_mean_p']:.3f}")
        if res['topk']:
            print("Top-K ROI 校准:")
            for K, v in res['topk'].items():
                print(f"  K={K:>5}: hit={pct(v['hit'])} roi={v['roi']:+.3f} avg_odds={v['avg_odds']:.3f}")
        print(f"\n联赛级模型准度 (n>=50):")
        for lg, v in sorted(res['leagues'].items(), key=lambda kv: -kv[1]['auc'])[:15]:
            print(f"  {lg[:18]:<18} n={v['n']:>5} auc={v['auc']:.3f} fav_hit={pct(v['fav_hit'])}")
        if args.json:
            with open(args.json, 'w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False, indent=2, default=str)
            print(f"\n[JSON] {args.json}")
        return

    if not (args.h and args.d and args.a):
        print("错误: 单场分析需要 --h --d --a"); return
    r = analyze_target(args.h, args.d, args.a, args.tol, args.k, args.min_sample)
    print(text_report(r))
    if args.html:
        with open(args.html, 'w', encoding='utf-8') as f:
            f.write(html_report(r))
        print(f"\n[HTML] {args.html}")
    if args.json:
        out = dict(target=r['target'], mode=r['mode'], exact_n=r['exact_n'], near_n=r['near_n'],
                   primary=r['primary'], model_read=r['model_read'],
                   model_1x2=r['model_1x2'], point_1x2=r['point_1x2'],
                   triangulation=r['triangulation'])
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2, default=str)
        print(f"[JSON] {args.json}")


if __name__ == '__main__':
    main()
