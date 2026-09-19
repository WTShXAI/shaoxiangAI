#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K线化模型训练+对照 (2026-09-15, E2/E3/E4 of Kronos 移植实验).

统一口径对比 (同一数据集, 同一 walkforward, 同一 LightGBM 超参):
  A. static        开盘去水概率 8 维 (现有基线)
  B. traj          现有 12 维轨迹特征
  C. static+traj   现任组合
  D. candles       111 维K线特征 (E2)
  E. candles+static+traj  全量
  F. transformer   12×14 K线序列, 4层 decoder-only (E3, torch CPU)
  G. ensemble      最优 LGB + transformer 概率平均 (E4)

验证: kickoff 升序 + TimeSeriesSplit(5) expanding walkforward, TOP1 准确率。
采纳标准: 相对现任最优 (C) TOP1 +2pp 才采纳; 多轮不同窗口稳健性检查。

用法: python scripts/train_odds_candles_model.py [--days 26] [--max 12000] [--transformer]
"""
import argparse
import math
import sys
import time
import warnings

sys.path.insert(0, r'D:\Architecture')
warnings.filterwarnings('ignore')
import numpy as np

from analysis.live_goal_probe import _open_gq
from pipeline.odds_candles import (load_match_events, parse_kickoff_ts, candleize_1x2,
                                   candle_flat_features, candle_sequence)
from pipeline.odds_trajectory import trajectory_features


def build_dataset(con, days=26, max_matches=12000):
    """构建统一数据集: 全部特征族 + 标签, 按 kickoff 升序."""
    mrows = con.execute("""
        SELECT match_key, kickoff, score_home, score_away FROM matches
        WHERE status='finished' AND score_home IS NOT NULL AND kickoff IS NOT NULL
        AND kickoff >= datetime('now', ?)
        ORDER BY kickoff ASC LIMIT ?""", (f'-{days} day', max_matches)).fetchall()
    rows = []
    skipped = 0
    import time as _t
    t0 = _t.time()
    for mk, ko, fsh, fsa in mrows:
        try:
            ko_ts = parse_kickoff_ts(ko)
            if ko_ts is None:
                skipped += 1; continue
            events = load_match_events(con, mk)
            pre = [e for e in events if e['captured_at'] <= ko_ts]
            # 假0-0守卫 (2026-09-19): 全场 0-0 且终盘 tick < kickoff+95min = 比分帧断流
            # 定格的假 0-0 → dirty 标签: 评估折一律剔除; 训练折是否剔除由 --clean-train 决定
            # (A/B 实验: 脏标签训练是否伤害模型, 实证见 pipeline/settle.py::credible_1x2)
            dirty = False
            if fsh == 0 and fsa == 0:
                last_tick = max((e['captured_at'] for e in events), default=None)
                dirty = (last_tick is None or last_tick < ko_ts + 95 * 60)
            if len(pre) < 20:
                skipped += 1; continue
            if not all(sum(1 for e in pre if e['selection'] == s) >= 2 for s in ('home', 'draw', 'away')):
                skipped += 1; continue
            # 开盘静态 (与基线同法: 每通道第一条快照/变化)
            op = {}
            for sel in ('home', 'draw', 'away'):
                vs = [e['to_odds'] for e in pre if e['selection'] == sel]
                op[sel] = min(vs) if vs else None
            if any(v is None or v <= 1.001 for v in op.values()):
                skipped += 1; continue
            h0, d0, a0 = op['home'], op['draw'], op['away']
            inv = [1/h0, 1/d0, 1/a0]
            s0 = sum(inv)
            ph, pd_, pa = inv[0]/s0, inv[1]/s0, inv[2]/s0
            xs = [ph, pd_, pa, ph*pd_, ph*pa, pd_*pa, ph+pd_, ph+pa]

            # 轨迹 (现有实现, 与基线同口径)
            tf = trajectory_features(pre, ko_ts)
            xt = None if tf is None else [tf[k] for k in (
                'traj_velocity_mean', 'traj_velocity_abs', 'traj_velocity_max',
                'traj_velocity_std', 'traj_flips', 'traj_tail_ratio',
                'traj_early_big_moves', 'traj_volatility',
                'traj_n_points', 'traj_ph_range', 'traj_ph_start', 'traj_ph_end')]

            # K线 (E1 重采样器)
            cand = candleize_1x2(events, ko_ts)
            if cand is None:
                skipped += 1; continue
            xc = candle_flat_features(cand)
            seq, mask = candle_sequence(cand)

            actual = 'home' if fsh > fsa else ('draw' if fsh == fsa else 'away')
            label = {'home': 0, 'draw': 1, 'away': 2}[actual]
            rows.append({'match_key': mk, 'kickoff': ko, 'label': label, 'actual': actual,
                         'dirty': dirty, 'static': xs, 'traj': xt, 'candles': xc, 'seq': seq, 'mask': mask})
        except Exception:
            skipped += 1
            continue
    print(f'数据集: {len(rows)} 场 (跳过 {skipped}) [{t0:_>0.0f}s]' if False else
          f'数据集: {len(rows)} 场 (跳过 {skipped}) [{_t.time()-t0:.0f}s]')
    return rows


def lgb_walkforward(X, y, folds, params=None, train_keep=None):
    import lightgbm as lgb
    accs, probs = [], []
    for tr, te in folds:
        if train_keep is not None:
            tr = tr[train_keep[tr]]
        m = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, max_depth=5,
                               verbose=-1, random_state=42, **(params or {}))
        m.fit(X[tr], y[tr])
        p = m.predict_proba(X[te])
        probs.append(p)
        accs.append(float((p.argmax(1) == y[te]).mean()))
    return accs, probs


# ── E3: 小型 decoder-only transformer ──
def train_transformer(rows, folds, epochs=25, d_model=64, n_layers=4, n_heads=4, seed=42,
                      train_keep=None):
    import torch
    import torch.nn as nn
    torch.manual_seed(seed)
    np.random.seed(seed)
    device = 'cpu'

    class Block(nn.Module):
        def __init__(self):
            super().__init__()
            self.ln1 = nn.LayerNorm(d_model)
            self.attn = nn.MultiheadAttention(d_model, n_heads, batch_first=True)
            self.ln2 = nn.LayerNorm(d_model)
            self.ff = nn.Sequential(nn.Linear(d_model, d_model*2), nn.GELU(), nn.Linear(d_model*2, d_model))
        def forward(self, x, attn_mask):
            h = self.ln1(x)
            a, _ = self.attn(h, h, h, attn_mask=attn_mask)
            x = x + a
            x = x + self.ff(self.ln2(x))
            return x

    class Model(nn.Module):
        def __init__(self, F=14, n_cls=3):
            super().__init__()
            self.proj = nn.Linear(F, d_model)
            self.pos = nn.Parameter(torch.zeros(1, 12, d_model))
            self.blocks = nn.ModuleList(Block() for _ in range(n_layers))
            self.lnf = nn.LayerNorm(d_model)
            self.head = nn.Linear(d_model, n_cls)
        def forward(self, seq):
            T = seq.shape[1]
            x = self.proj(seq) + self.pos[:, :T]
            mask = torch.triu(torch.ones(T, T, dtype=torch.bool), diagonal=1).to(seq.device)
            for b in self.blocks:
                x = b(x, mask)
            return self.head(self.lnf(x[:, -1]))  # 因果: 末位置看过全部

    seqs = torch.tensor(np.stack([r['seq'] for r in rows]))
    y = np.array([r['label'] for r in rows])
    accs, probs = [], []
    for fi, (tr, te) in enumerate(folds):
        if train_keep is not None:
            tr = tr[train_keep[tr]]
        torch.manual_seed(seed + fi)
        model = Model().to(device)
        opt = torch.optim.Adam(model.parameters(), lr=1e-3)
        # 类别加权 (平局少)
        cls_cnt = np.bincount(y[tr], minlength=3)
        w = torch.tensor(cls_cnt.sum() / (3.0 * np.maximum(cls_cnt, 1)), dtype=torch.float32)
        lossf = nn.CrossEntropyLoss(weight=w)
        Xtr, ytr = seqs[tr], torch.tensor(y[tr])
        n = len(Xtr)
        cut = int(n * 0.9)  # 尾段做早停验证 (时序内)
        best_state, best_val, patience = None, -1, 0
        for ep in range(epochs):
            model.train()
            perm = torch.randperm(cut)
            for i in range(0, cut, 128):
                bidx = perm[i:i+128]
                logits = model(Xtr[bidx])
                loss = lossf(logits, ytr[bidx])
                opt.zero_grad(); loss.backward(); opt.step()
            model.eval()
            with torch.no_grad():
                val_acc = (model(Xtr[cut:]).argmax(1) == ytr[cut:]).float().mean().item()
            if val_acc > best_val:
                best_val, patience = val_acc, 0
                best_state = {k: v.clone() for k, v in model.state_dict().items()}
            else:
                patience += 1
                if patience >= 5:
                    break
        if best_state:
            model.load_state_dict(best_state)
        model.eval()
        with torch.no_grad():
            p = torch.softmax(model(seqs[te]), dim=1).numpy()
        probs.append(p)
        accs.append(float((p.argmax(1) == y[te]).mean()))
    return accs, probs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=26)
    ap.add_argument('--max', type=int, default=12000)
    ap.add_argument('--rounds', type=int, default=1)
    ap.add_argument('--transformer', action='store_true')
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--clean-train', action='store_true',
                    help='假0-0脏标签从训练折剔除 (评估折无论开关一律剔除)')
    ap.add_argument('--save', default='')
    args = ap.parse_args()

    con = _open_gq()
    rows = build_dataset(con, days=args.days, max_matches=args.max)
    if len(rows) < 300:
        print('样本不足')
        return
    y = np.array([r['label'] for r in rows])
    dirty = np.array([bool(r.get('dirty')) for r in rows])
    kickoffs = [r['kickoff'] for r in rows]
    assert kickoffs == sorted(kickoffs), '必须 kickoff 升序'
    # --clean-train: 假0-0脏标签从训练折剔除; 无论开关, 评估折一律只用干净行 (口径统一)
    train_keep = (~dirty) if args.clean_train else None
    if dirty.any():
        print(f'假0-0脏标签: {int(dirty.sum())} 场 '
              f'({"训练折剔除 (--clean-train)" if args.clean_train else "保留在训练折 (现任策略)"}; '
              f'评估折一律剔除)')

    from sklearn.model_selection import TimeSeriesSplit
    folds = list(TimeSeriesSplit(n_splits=5).split(np.zeros(len(y))))
    # 特征族
    def mat(key):
        return np.array([r[key] if r[key] is not None else [0.0]*len(rows[0][key] or []) for r in rows], dtype=np.float32)
    X_s = np.array([r['static'] for r in rows], dtype=np.float32)
    has_traj = [r for r in rows if r['traj'] is not None]
    # 对齐: 无轨迹特征的场补均值列 (保持行集一致)
    tdim = len([r for r in rows if r['traj'] is not None][0]['traj'])
    X_t = np.array([r['traj'] if r['traj'] is not None else [np.nan]*tdim for r in rows], dtype=np.float32)
    col_mu = np.nanmean(X_t, axis=0)
    X_t = np.where(np.isnan(X_t), col_mu, X_t)
    X_c = np.array([r['candles'] for r in rows], dtype=np.float32)
    X_cst = np.hstack([X_c, X_s])
    X_all = np.hstack([X_c, X_s, X_t])

    fams = {'A static': X_s, 'B traj': X_t, 'C static+traj': np.hstack([X_s, X_t]),
            'D candles': X_c, 'E candles+static': X_cst, 'E2 all': X_all}

    # 预测系统主指标 (2026-09-18): LogLoss 与 TOP1 并列输出
    # (2026-09-19) 统一干净评估: 指标只在非 dirty 测试行上计算 — A/B 实验的同测试集保证
    def _clean_eval(probs_list):
        accs, lls = [], []
        for fi, (tr, te) in enumerate(folds):
            keep = ~dirty[te]
            te_k, y_k = te[keep], y[te][keep]
            if len(te_k) == 0:
                continue
            p = probs_list[fi][keep]
            accs.append(float((p.argmax(1) == y_k).mean()))
            pc = np.clip(p, 1e-15, 1 - 1e-15)
            lls.append(float(-np.mean(np.log(pc[np.arange(len(te_k)), y_k]))))
        return accs, (float(np.mean(lls)) if lls else float('nan'))

    def _ll(probs_list):
        return _clean_eval(probs_list)[1]

    print(f'\n── Walkforward 5 折 TOP1 / LogLoss (days={args.days}, n={len(y)}, '
          f'干净评估 n={int((~dirty).sum())}) ──')
    table = {}
    table_ll = {}
    lgb_probs = {}
    for name, X in fams.items():
        _, probs = lgb_walkforward(X, y, folds, train_keep=train_keep)
        accs, ll = _clean_eval(probs)
        table[name] = accs
        table_ll[name] = ll
        lgb_probs[name] = probs
        print(f'  {name:>18}: {np.mean(accs)*100:5.1f}% ± {np.std(accs)*100:.1f}%   LL={table_ll[name]:.4f}   '
              f'折: {" ".join(f"{a*100:.0f}" for a in accs)}')
    # 类别分布基线
    print(f'  {"多数类基线":>18}: {max(np.bincount(y))*100/len(y):.1f}%')

    if args.transformer:
        t0 = time.time()
        _, probs_t = train_transformer(rows, folds, seed=args.seed, train_keep=train_keep)
        accs_t, _ = _clean_eval(probs_t)
        table['F transformer'] = accs_t
        print(f'  {"F transformer":>18}: {np.mean(accs_t)*100:5.1f}% ± {np.std(accs_t)*100:.1f}%   [{time.time()-t0:.0f}s]')
        # E4: 集成 (最优 LGB 族 + transformer)
        best_lgb = max(fams, key=lambda n: np.mean(table[n]))
        ens_probs = [0.5*lgb_probs[best_lgb][fi] + 0.5*probs_t[fi] for fi in range(len(folds))]
        ens_accs, ens_ll = _clean_eval(ens_probs)
        table['G ensemble'] = ens_accs
        table_ll['G ensemble'] = ens_ll
        print(f'  {"G ensemble("+best_lgb[:8]+")":>18}: {np.mean(ens_accs)*100:5.1f}% ± {np.std(ens_accs)*100:.1f}%   LL={table_ll["G ensemble"]:.4f}')

    # 结论
    incumbent = np.mean(table['C static+traj'])
    print(f'\n── 结论 (现任=C static+traj {incumbent*100:.1f}%, 采纳线 +2pp) ──')
    for name, accs in table.items():
        d = (np.mean(accs) - incumbent) * 100
        flag = '✓ 采纳' if d >= 2 else ('△ 增益不足' if d >= 0 else '✗ 退化')
        if name == 'C static+traj':
            flag = '= 现任'
        print(f'  {name:>18}: {d:+.1f}pp  {flag}')

    if args.save:
        import json
        with open(args.save, 'w', encoding='utf-8') as f:
            json.dump({k: [float(a) for a in v] for k, v in table.items()}, f,
                      ensure_ascii=False, indent=1)
        print(f'结果已存: {args.save}')


if __name__ == '__main__':
    main()
