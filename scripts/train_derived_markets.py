#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""派生市场模型训练 (O2.5 / BTTS) — 2026-09-18 迭代三
================================================================================
任务: 为预测产品层的派生市场训练专用 LightGBM, 对照现行基线 (OIP 诚实锚比分矩阵):
  基线 B1: score_model.predict_score(goal_scale=1.0, implied_total=OU锚) 矩阵边缘化
  基线 B2: 市场同盘口 p_over (仅 line=2.5 子集, 参考口径)

特征 (全部赛前可得, 零泄漏): 去水 1X2 三向 + OU 锚 implied_total / line / p_over +
  overround + 强弱差 + 联赛场均总进球 (**expanding train-only 口径**, 每折用训练窗口
  截止前的已完赛场计算, 测试行只查不算)。

验证: kickoff 升序 + TimeSeriesSplit(5) expanding walkforward。
采纳线 (预先声明): O2.5 与 BTTS 各自 walkforward 平均 LogLoss 相对 B1 改善 ≥ 0.010,
  且 5 折中 ≥4 折不劣于 B1。过线才落模型 (data/models/derived_markets/), 否则如实保留 OIP。

用法: python scripts/train_derived_markets.py [--days 120] [--max 20000] [--save]
"""
import argparse
import json
import math
import os
import sys
import time
import warnings

sys.path.insert(0, r'D:\Architecture')
warnings.filterwarnings('ignore')
import numpy as np

from pipeline.calibration import reliability, ece
from pipeline.score_model import deoverround, predict_score
from gq.db import DB_PATH

EPS = 1e-15
MODEL_DIR = r'D:\Architecture\data\models\derived_markets'


def parse_kickoff_safe(ko):
    from pipeline.odds_candles import parse_kickoff_ts
    return parse_kickoff_ts(ko)


def _open_snapshot(con, mk, ko_ts, line):
    """开盘快照 (首个赛前tick): 1X2 主胜去水概率 + 主线 p_over。取不到返回 None。"""
    from pipeline.predict_export import latest_prematch_ou
    rows1 = con.execute("""SELECT selection, to_odds, captured_at FROM odds_changes
        WHERE match_key=? AND market='1X2' AND to_odds>1.001 AND to_odds<500
        ORDER BY captured_at""", (mk,)).fetchall()
    first = {}
    for sel, odds, cap in rows1:
        if float(cap) <= ko_ts and sel not in first:
            first[sel] = float(odds)
    if not all(first.get(s) for s in ('home', 'draw', 'away')):
        return None, None
    ph_o = deoverround(first['home'], first['draw'], first['away'])[0]
    ou_o = latest_prematch_ou(con, mk, ko_ts)
    po_o = None
    rows2 = con.execute("""SELECT market, selection, to_odds, captured_at FROM odds_changes
        WHERE match_key=? AND market LIKE 'OU\\_%' ESCAPE '\\' AND to_odds>1.001 AND to_odds<500
        ORDER BY captured_at""", (mk,)).fetchall()
    sides = {}
    for market, sel, odds, cap in rows2:
        parts = str(market).split('_')
        try:
            ln = float(parts[1])
        except (IndexError, ValueError):
            continue
        if parts[1] in ('1H', '2H') or ln != line or float(cap) > ko_ts:
            continue
        sides.setdefault(sel, odds)
    if sides.get('over') and sides.get('under'):
        po, pu = 1.0 / sides['over'], 1.0 / sides['under']
        po_o = po / (po + pu)
    return ph_o, po_o


def binary_pt(pts):
    if not pts:
        return None
    b = reliability(pts, nbins=10)
    return {'n': len(pts),
            'log_loss': round(-sum(w * math.log(max(p, EPS)) + (1 - w) * math.log(max(1 - p, EPS))
                                   for p, w in pts) / len(pts), 5),
            'brier': round(sum((p - w) ** 2 for p, w in pts) / len(pts), 5),
            'ece': round(ece(b), 5)}


# ── 数据集构建 ────────────────────────────────────────────────────────────

def build_dataset(days, max_matches):
    """逐场取赛前 1X2 + OU 锚 + 联赛/赛果, 输出行:
    {match_key, league, ko_str, ko_ts, oh,od,oa, ou_line, p_over, implied_total,
     total(actual), btts(actual)}"""
    import sqlite3
    con = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    from pipeline.predict_export import latest_prematch_1x2, latest_prematch_ou
    rows = con.execute("""
        SELECT match_key, league, kickoff, score_home, score_away FROM matches
        WHERE status='finished' AND score_home IS NOT NULL AND kickoff IS NOT NULL
          AND kickoff >= datetime('now', ?)
        ORDER BY kickoff ASC LIMIT ?""", (f'-{days} day', max_matches)).fetchall()
    out, skipped = [], 0
    t0 = time.time()
    for mk, league, ko, fsh, fsa in rows:
        try:
            ko_ts = parse_kickoff_safe(ko)
            if ko_ts is None:
                skipped += 1; continue
            x12 = latest_prematch_1x2(con, mk, ko_ts)
            if x12 is None:
                skipped += 1; continue
            ou = latest_prematch_ou(con, mk, ko_ts)
            if ou is None:
                skipped += 1; continue
            # v2 移动特征: 开盘(首个赛前tick)快照
            ph_open, po_open = _open_snapshot(con, mk, ko_ts, ou['line'])
            out.append({
                'match_key': mk, 'league': league or '未知', 'ko_str': ko, 'ko_ts': ko_ts,
                'oh': x12[0], 'od': x12[1], 'oa': x12[2],
                'ou_line': ou['line'], 'p_over': ou['p_over'], 'implied_total': ou['implied_total'],
                'ph_open': ph_open, 'p_over_open': po_open,
                'total': fsh + fsa, 'btts': 1 if (fsh > 0 and fsa > 0) else 0,
            })
        except Exception:
            skipped += 1
    con.close()
    print(f'数据集: {len(out)} 场可用 / 跳过 {skipped} / 耗时 {time.time()-t0:.0f}s')
    return out


def league_mean_totals(rows, cutoff_ts, min_n=15):
    """训练窗口内 (ko_ts < cutoff) 各联赛平均实际总进球 + 全局均值 (泄漏安全)。"""
    agg = {}
    gsum = gn = 0
    for r in rows:
        if r['ko_ts'] >= cutoff_ts:
            continue
        agg.setdefault(r['league'], [0.0, 0])
        agg[r['league']][0] += r['total']
        agg[r['league']][1] += 1
        gsum += r['total']; gn += 1
    gmean = gsum / gn if gn else 2.6
    lm = {lg: (s / n if n >= min_n else gmean) for lg, (s, n) in agg.items()}
    return lm, gmean


def featurize(r, lm, gmean):
    ph, pd_, pa = deoverround(r['oh'], r['od'], r['oa'])
    lm_tot = lm.get(r['league'], gmean)
    return [ph, pd_, pa, r['implied_total'], r['ou_line'], r['p_over'],
            (1.0 / r['oh'] + 1.0 / r['od'] + 1.0 / r['oa']) - 1.0,
            abs(ph - pa), lm_tot,
            r['implied_total'] * lm_tot / 2.6]


def featurize_v2(r, lm, gmean, p_b1):
    """v2: v1 特征 + B1 基线概率 (stacking) + 开盘→临场移动量 (基线没吃的信息)。"""
    base = featurize(r, lm, gmean)
    ph_o = r.get('ph_open')
    po_o = r.get('p_over_open')
    ph, pd_, pa = deoverround(r['oh'], r['od'], r['oa'])
    dph = (ph - ph_o) if ph_o is not None else 0.0
    dpo = (r['p_over'] - po_o) if po_o is not None else 0.0
    return base + [ph_o if ph_o is not None else ph, dph,
                   po_o if po_o is not None else r['p_over'], dpo, p_b1]


def oip_baseline(r, key):
    """B1: OIP 诚实锚矩阵边缘化 (goal_scale=1.0, 与生产一致)。key: 'o25'|'btts'。"""
    m = predict_score(r['match_key'], '', r['oh'], r['od'], r['oa'],
                      goal_scale=1.0, implied_total=r['implied_total'])
    M = m['matrix']
    mg = M.shape[0] - 1
    if key == 'o25':
        return float(1.0 - sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j <= 2))
    return float(sum(M[i, j] for i in range(1, mg + 1) for j in range(1, mg + 1)))


# ── Walkforward ───────────────────────────────────────────────────────────

def run_walkforward(rows, n_folds=5):
    """v2 口径: 特征 = v1 + B1基线概率(stacking) + 开盘→临场移动量; 训练/测试同构。"""
    from sklearn.model_selection import TimeSeriesSplit
    from lightgbm import LGBMClassifier
    rows = sorted(rows, key=lambda r: r['ko_ts'])
    tss = TimeSeriesSplit(n_splits=n_folds)
    res = {k: {'model': [], 'b1': [], 'b2': [], 'n': []} for k in ('o25', 'btts')}
    fold_no = 0
    for tr_idx, te_idx in tss.split(rows):
        fold_no += 1
        tr = [rows[i] for i in tr_idx]
        te = [rows[i] for i in te_idx]
        cutoff = te[0]['ko_ts']
        lm, gmean = league_mean_totals(rows, cutoff)
        for key in ('o25', 'btts'):
            for r in tr + te:
                r.setdefault('_p_b1', {})[key] = oip_baseline(r, key)
        X = {}
        for key in ('o25', 'btts'):
            X[key] = (np.array([featurize_v2(r, lm, gmean, r['_p_b1'][key]) for r in tr]),
                      np.array([featurize_v2(r, lm, gmean, r['_p_b1'][key]) for r in te]))
        for key in ('o25', 'btts'):
            Xtr, Xte = X[key]
            ytr = np.array([1 if (r['total'] > 2.5 if key == 'o25' else r['btts']) else 0 for r in tr])
            yte = np.array([1 if (r['total'] > 2.5 if key == 'o25' else r['btts']) else 0 for r in te])
            clf = LGBMClassifier(n_estimators=150, learning_rate=0.05, num_leaves=31,
                                 min_child_samples=60, subsample=0.9, colsample_bytree=0.9,
                                 random_state=42, verbose=-1)
            clf.fit(Xtr, ytr)
            p_model = clf.predict_proba(Xte)[:, 1]
            p_b1 = np.array([r['_p_b1'][key] for r in te])
            pts_m = list(zip(p_model, yte))
            pts_b1 = list(zip(p_b1, yte))
            # B2: 市场同盘口 (仅 line=2.5)
            sub = [(r['p_over'], 1 if r['total'] > 2.5 else 0) for r in te if r['ou_line'] == 2.5]
            res[key]['model'].append(binary_pt(pts_m))
            res[key]['b1'].append(binary_pt(pts_b1))
            res[key]['b2'].append(binary_pt(sub) if sub else None)
            res[key]['n'].append(len(te))
            print(f"  fold{fold_no} {key}: n={len(te)} 模型 LL={res[key]['model'][-1]['log_loss']} "
                  f"Brier={res[key]['model'][-1]['brier']} | B1(LL)={res[key]['b1'][-1]['log_loss']} "
                  f"| B2市场(line2.5, n={len(sub)}) LL={res[key]['b2'][-1]['log_loss'] if sub else '-'}")
    return res


def summarize(res):
    def avg(seq, k):
        vals = [x[k] for x in seq if x]
        return round(sum(vals) / len(vals), 5) if vals else None
    out = {}
    for key in ('o25', 'btts'):
        m_ll, b_ll = avg(res[key]['model'], 'log_loss'), avg(res[key]['b1'], 'log_loss')
        m_br, b_br = avg(res[key]['model'], 'brier'), avg(res[key]['b1'], 'brier')
        m_ec = avg(res[key]['model'], 'ece')
        wins = sum(1 for m, b in zip(res[key]['model'], res[key]['b1'])
                   if m and b and m['log_loss'] <= b['log_loss'])
        out[key] = {'model_ll': m_ll, 'b1_ll': b_ll, 'delta': round(m_ll - b_ll, 5),
                    'model_brier': m_br, 'b1_brier': b_br, 'model_ece': m_ec,
                    'folds_not_worse': f'{wins}/{len(res[key]["model"])}'}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=120)
    ap.add_argument('--max', type=int, default=20000)
    ap.add_argument('--save', action='store_true', help='过线时落模型文件')
    args = ap.parse_args()

    rows = build_dataset(args.days, args.max)
    if len(rows) < 2000:
        print('样本不足 (<2000), 放弃训练')
        return
    print(f'── Walkforward ({5} 折 expanding) ──')
    res = run_walkforward(rows)
    summ = summarize(res)
    print('── 汇总 (walkforward 平均) ──')
    print(json.dumps(summ, ensure_ascii=False, indent=2))

    adopted = {}
    for key in ('o25', 'btts'):
        s = summ[key]
        ok = (s['delta'] is not None and s['delta'] <= -0.010
              and int(s['folds_not_worse'].split('/')[0]) >= 4)
        adopted[key] = ok
        print(f"[{'采纳' if ok else '不采纳'}] {key}: ΔLL={s['delta']} (线: ≤-0.010), "
              f"不劣折数={s['folds_not_worse']} (线: ≥4/5)")

    if args.save and any(adopted.values()):
        import joblib
        os.makedirs(MODEL_DIR, exist_ok=True)
        from sklearn.model_selection import TimeSeriesSplit
        from lightgbm import LGBMClassifier
        rows_s = sorted(rows, key=lambda r: r['ko_ts'])
        # 全量重训 (特征用最后一折 cutoff 的联赛均值口径, 服务时同样按 kickoff 截止计算)
        lm, gmean = league_mean_totals(rows_s, rows_s[-1]['ko_ts'])
        meta = {'trained_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'n': len(rows_s),
                'walkforward': summ, 'adopted': adopted, 'variant': 'v2',
                'features': ['ph', 'pd', 'pa', 'implied_total', 'ou_line', 'p_over',
                             'overround', 'fav_gap', 'league_mean_total', 'imp_x_lm',
                             'ph_open', 'dph', 'p_over_open', 'dp_over', 'p_b1_stacking'],
                'league_means': lm, 'gmean': gmean,
                'note': '服务时 league_mean_total 必须用 kickoff 之前窗口重算 (predict_export.league_mean_total_asof); p_b1 用 OIP 诚实锚矩阵现算'}
        for key in ('o25', 'btts'):
            if not adopted[key]:
                continue
            y = np.array([1 if (r['total'] > 2.5 if key == 'o25' else r['btts']) else 0 for r in rows_s])
            # walkforward 已为全量行缓存 _p_b1; 兜底现算
            X = np.array([featurize_v2(r, lm, gmean,
                                       r.get('_p_b1', {}).get(key) or oip_baseline(r, key))
                          for r in rows_s])
            clf = LGBMClassifier(n_estimators=150, learning_rate=0.05, num_leaves=31,
                                 min_child_samples=60, subsample=0.9, colsample_bytree=0.9,
                                 random_state=42, verbose=-1)
            clf.fit(X, y)
            joblib.dump(clf, os.path.join(MODEL_DIR, f'lgb_{key}.joblib'))
            print(f'→ {MODEL_DIR}\\lgb_{key}.joblib')
        with open(os.path.join(MODEL_DIR, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f'→ {MODEL_DIR}\\meta.json')


if __name__ == '__main__':
    main()
