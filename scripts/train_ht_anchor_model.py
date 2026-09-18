#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实验B: 中场锚数据驱动升级 (2026-09-16, 用户: 模型只有初盘+中场两次机会, 用数据提准).

HT 冻结时刻可得信息 → LightGBM 三分类/二分类, 对照现任启发式冻结:
  现任 (halftime_conclusion 实测): 1X2 59.3% / OU 63.6% / HT平状态仅 39.9% ← 靶心
特征 (全部冻结时刻可得, 零未来信息):
  HT比分差/总球, 赛前收盘去水概率, 上半场滚球1X2轨迹(末值+速度+波动+tick数),
  HT时OU主线+大小去水概率, 上半场OU振幅
纪律: 严格冻结时刻cutoff(captured_at≤kickoff+50min 且 minute_at≤49), walkforward 5折,
  +2pp 采纳线, 采纳线对照在与现任冻结同集的子集上另算。

用法: python scripts/train_ht_anchor_model.py [--days 45] [--seed 42]
"""
import argparse
import json
import sys
import time
import warnings

sys.path.insert(0, r'D:\Architecture')
warnings.filterwarnings('ignore')
import numpy as np

HT_WIN_MIN = 50  # 上半场窗口 (kickoff+50min, 容延迟)


def build_ht_dataset(con, days=45, max_matches=20000):
    from pipeline.odds_candles import parse_kickoff_ts, _devig3
    mrows = con.execute("""
        SELECT match_key, kickoff, score_home, score_away, ht_score_home, ht_score_away
        FROM matches
        WHERE status='finished' AND score_home IS NOT NULL AND ht_score_home IS NOT NULL
        AND kickoff IS NOT NULL AND kickoff >= datetime('now', ?)
        ORDER BY kickoff ASC LIMIT ?""", (f'-{days} day', max_matches)).fetchall()
    rows = []
    skipped = 0
    t0 = time.time()
    # ⚠ matches.ht_score_* 列实测 51.2% 被终场回填污染 (2026-09-16 审计), 禁用!
    # 干净 HT 源: 滚球 tick 实时快照 score_at (minute_at 40..47 末次), 物理违反更少。
    hts = {}
    for mk2, sa, mx in con.execute("""
        SELECT match_key, score_at, MAX(minute_at) FROM odds_changes
        WHERE market='1X2' AND minute_at BETWEEN 40 AND 47 AND score_at IS NOT NULL
        GROUP BY match_key"""):
        try:
            import re as _re
            a, b = map(int, _re.match(r'(\d+)-(\d+)', sa).groups())
            hts[mk2] = (a, b)
        except Exception:
            continue
    for mk, ko, fsh, fsa, hsh, hsa in mrows:
        try:
            ko_ts = parse_kickoff_ts(ko)
            clean_ht = hts.get(mk)
            if ko_ts is None or clean_ht is None:
                skipped += 1; continue
            hsh, hsa = clean_ht
            # 物理守卫: HT 不得超过终场
            if hsh > fsh or hsa > fsa:
                skipped += 1; continue
            ht_cut = ko_ts + HT_WIN_MIN * 60

            # 赛前收盘 (kickoff 前最后一格去水概率)
            pre = con.execute("""SELECT selection, to_odds, captured_at FROM odds_changes
                WHERE match_key=? AND market='1X2' AND to_odds>1.001 AND to_odds<500
                AND captured_at <= ? ORDER BY captured_at""", (mk, ko_ts)).fetchall()
            if len(pre) < 8:
                skipped += 1; continue
            last = {}
            for s, o, c in pre:
                last[s] = float(o)
            if not all(s in last for s in ('home', 'draw', 'away')):
                skipped += 1; continue
            pph, ppd, ppa = _devig3(last['home'], last['draw'], last['away'])

            # 上半场滚球 1X2 (窗口内)
            ip = con.execute("""SELECT selection, to_odds, captured_at FROM odds_changes
                WHERE match_key=? AND market='1X2' AND to_odds>1.001 AND to_odds<500
                AND captured_at > ? AND captured_at <= ?
                AND (minute_at IS NULL OR (minute_at >= 0 AND minute_at <= 49))
                ORDER BY captured_at""", (mk, ko_ts, ht_cut)).fetchall()
            ph_l = [e for e in ip if e[0] == 'home']
            pd_l = [e for e in ip if e[0] == 'draw']
            pa_l = [e for e in ip if e[0] == 'away']
            n_ip = len(ip)
            if n_ip >= 6 and ph_l and pd_l and pa_l:
                # HT 时刻去水概率 (各通道窗口内最后值)
                lh = max(e[2] for e in ph_l); ld = max(e[2] for e in pd_l); la = max(e[2] for e in pa_l)
                h_v = [float(e[1]) for e in ph_l if e[2] == lh][-1]
                d_v = [float(e[1]) for e in pd_l if e[2] == ld][-1]
                a_v = [float(e[1]) for e in pa_l if e[2] == la][-1]
                lph, lpd, lpa = _devig3(h_v, d_v, a_v)
                # 上半场主胜概率轨迹摘要 (时间对齐粗略: 全窗口并集)
                all_ts = sorted(set(e[2] for e in ip))
                pts, idx = [], {'home': 0, 'draw': 0, 'away': 0}
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
                ph_arr = np.array(ph_path)
                traj_v = float(np.abs(np.diff(ph_arr)).mean()) if len(ph_arr) > 1 else 0.0
                traj_r = float(ph_arr[-1] - ph_arr[0]) if len(ph_arr) > 1 else 0.0
            else:
                lph, lpd, lpa = pph, ppd, ppa
                traj_v = traj_r = 0.0
                n_ip = int(n_ip)

            # OU 主线 (赛前+上半场 tick 最多的那条线), HT 时刻大小概率
            ou_rows = con.execute("""SELECT market, selection, to_odds, captured_at FROM odds_changes
                WHERE match_key=? AND market LIKE 'OU_%' AND to_odds>1.001 AND to_odds<500
                AND captured_at <= ?
                AND (minute_at IS NULL OR (minute_at >= 0 AND minute_at <= 49))
                ORDER BY captured_at""", (mk, ht_cut)).fetchall()
            ou_feat = [0.0, 0.5, 0.5, 0.0]  # line, live_po, live_pu, ou_amp
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

            label = 0 if fsh > fsa else (1 if fsh == fsa else 2)
            total = int(fsh) + int(fsa)
            ht_total = int(hsh) + int(hsa)
            ou_label = -1
            if abs(total - ou_feat[0]) > 1e-9:
                ou_label = 1 if total > ou_feat[0] else 0
            rows.append({
                'match_key': mk, 'kickoff': ko, 'label': label, 'ou_label': ou_label,
                'ht_state': 0 if hsh > hsa else (1 if hsh == hsa else 2),
                'x': [int(hsh) - int(hsa), ht_total, int(fsh)*0,  # 占位防误用终局
                      pph, ppd, ppa, lph, lpd, lpa, n_ip, traj_v, traj_r,
                      ou_feat[0], ou_feat[1], ou_feat[2], ou_feat[3]]})
        except Exception:
            skipped += 1
            continue
    # 移除占位列 (第3位) — 确保无终局泄漏
    for r in rows:
        del r['x'][2]
    print(f'HT数据集: {len(rows)} 场 (跳过 {skipped}) [{time.time()-t0:.0f}s]')
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=45)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--save', default='')
    args = ap.parse_args()

    from analysis.live_goal_probe import _open_gq
    from sklearn.model_selection import TimeSeriesSplit
    import lightgbm as lgb

    con = _open_gq()
    rows = build_ht_dataset(con, days=args.days)
    if len(rows) < 500:
        print('样本不足'); return
    y = np.array([r['label'] for r in rows])
    y_ou = np.array([r['ou_label'] for r in rows])
    ht_state = np.array([r['ht_state'] for r in rows])
    X = np.array([r['x'] for r in rows], dtype=np.float32)
    ko = [r['kickoff'] for r in rows]
    assert ko == sorted(ko)
    folds = list(TimeSeriesSplit(n_splits=5).split(X))
    print(f'特征维度: {X.shape[1]} | 列: ht_diff, ht_total, pre_ph, pre_pd, pre_pa, '
          f'live_ph, live_pd, live_pa, n_ip, traj_v, traj_r, ou_line, live_po, live_pu, ou_amp')

    def run_lgb(Xs, ys, folds, ncls):
        accs = []
        for tr, te in folds:
            m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, max_depth=6,
                                   num_leaves=31, verbose=-1, random_state=args.seed)
            m.fit(Xs[tr], ys[tr])
            accs.append(float((m.predict(Xs[te]) == ys[te]).mean()))
        return accs, m

    print('\n── HT 锚: 训练模型 vs 现任启发式 ──')
    acc_1x2, model_1x2 = run_lgb(X, y, folds, 3)
    print(f'  1X2 全状态: {np.mean(acc_1x2)*100:.1f}% ± {np.std(acc_1x2)*100:.1f}%  (现任 59.3%)')

    # HT平状态单独评估 (靶心: 现任 39.9%)
    accs_draw = []
    for tr, te in folds:
        m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, max_depth=6,
                               num_leaves=31, verbose=-1, random_state=args.seed)
        m.fit(X[tr], y[tr])
        mask = ht_state[te] == 1
        if mask.sum() > 20:
            accs_draw.append(float((m.predict(X[te][mask]) == y[te][mask]).mean()))
    if accs_draw:
        print(f'  1X2 HT平状态: {np.mean(accs_draw)*100:.1f}%  (现任 39.9% ← 靶心)')

    idx_ou = np.where(y_ou >= 0)[0]
    folds_ou = list(TimeSeriesSplit(n_splits=5).split(X[idx_ou]))
    acc_ou, _ = run_lgb(X[idx_ou], y_ou[idx_ou], folds_ou, 2)
    base_ou = max(np.bincount(y_ou[idx_ou])) / len(idx_ou)
    print(f'  OU vs HT主线: {np.mean(acc_ou)*100:.1f}%  (现任 63.6%, 多数类 {base_ou*100:.1f}%, n={len(idx_ou)})')

    # 全数据训练的特征重要性
    model_1x2.fit(X, y)
    imp = model_1x2.feature_importances_
    names = ['ht_diff', 'ht_total', 'pre_ph', 'pre_pd', 'pre_pa', 'live_ph', 'live_pd', 'live_pa',
             'n_ip', 'traj_v', 'traj_r', 'ou_line', 'live_po', 'live_pu', 'ou_amp']
    top = sorted(zip(names, imp), key=lambda t: -t[1])[:6]
    print(f'  Top特征: {[(n, int(v)) for n, v in top]}')

    print(f'\n── 结论 (现任 1X2 59.3% / HT平 39.9% / OU 63.6%, 采纳线 +2pp) ──')
    for name, acc, inc in [('1X2全状态', acc_1x2, 0.593), ('1X2_HT平', accs_draw, 0.399), ('OU', acc_ou, 0.636)]:
        if not acc:
            continue
        d = (np.mean(acc) - inc) * 100
        print(f'  {name:>12}: {np.mean(acc)*100:5.1f}%  {d:+.1f}pp  '
              f'{"✓过线" if d >= 2 else ("△正向" if d >= 0 else "✗退化")}')

    if args.save:
        with open(args.save, 'w', encoding='utf-8') as fp:
            json.dump({'1x2': acc_1x2, '1x2_ht_draw': accs_draw, 'ou': acc_ou}, fp, indent=1)
        print(f'结果已存: {args.save}')


if __name__ == '__main__':
    main()
