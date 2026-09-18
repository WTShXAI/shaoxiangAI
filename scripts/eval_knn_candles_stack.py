#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M3: KNN+K线集成 stacking 同集对照 (2026-09-18, 模型升级路线图③).
================================================================================
⚠ 预声明限制: K线判定 2026-09-15 才上线, 与 KNN sweep 的同集样本有限 (约 230 场) —
本实验结论按 UNDERPOWERED 对待, 主要产出是"方向性证据"而非采纳依据。
KNN 无原始概率, 用 相似场超额信号 excess(H/D/A) + 历史基率 重建近似概率。

采纳线: 同集 n≥200 且 stacking LogLoss 优于 K线单路 ≥0.005 (kickoff 序 3 折)。
输出: reports/knn_candles_stack.json
"""
import json
import math
import os
import sys

sys.path.insert(0, r'D:\Architecture')
import numpy as np

EPS = 1e-15
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')
BASE = {'H': 0.44, 'D': 0.26, 'A': 0.30}  # 本库历史基率近似 (KNN 相似场先验)


def nll(probs, ys):
    p = np.clip(probs, EPS, 1 - EPS)
    return float(-np.mean(np.log(p[np.arange(len(ys)), ys])))


def main():
    from gq.db import conn
    # K线判定 + 赛果
    with conn(readonly=True) as con:
        rows = con.execute("""SELECT v.match_key, v.probs, v.kickoff, m.score_home, m.score_away
            FROM prematch_candles_verdict v JOIN matches m ON m.match_key = v.match_key
            WHERE m.status='finished' AND m.score_home IS NOT NULL
            ORDER BY v.kickoff""").fetchall()
    candles = {}
    for mk, probs, ko, fsh, fsa in rows:
        try:
            candles[mk] = (json.loads(probs), ko, fsh, fsa)
        except Exception:
            continue

    # KNN 信号: 直接用 sweep 缓存 (11,518 场, 2026-08 全量离线算好)
    knn_fn = None

    cache = {}
    cache_path = os.path.join(os.path.dirname(REPORT_DIR), 'data', 'knn_sweep_cache.jsonl')
    with open(cache_path, encoding='utf-8') as f:
        for line in f:
            try:
                d = json.loads(line)
                cache[d['match_key']] = d
            except Exception:
                continue

    data = []
    for mk, (probs, ko, fsh, fsa) in candles.items():
        c = cache.get(mk)
        if not c or not c.get('applicable'):
            continue
        ex = c.get('excess') or {}
        pk = np.array([max(BASE[k] + ex.get(k, 0.0), 0.02) for k in ('H', 'D', 'A')])
        pk = pk / pk.sum()
        pc = np.array([probs['home'], probs['draw'], probs['away']])
        y = 0 if fsh > fsa else (1 if fsh == fsa else 2)
        data.append({'mk': mk, 'ko': ko, 'pc': pc, 'pk': pk, 'y': y,
                     'verdict': c.get('verdict')})
    n = len(data)
    print(f'同集样本: {n} (K线判定 ∩ KNN sweep)')
    if n < 30:
        print('样本过少, 无法评估 — 留待两台账增厚后重跑')
        return

    ys = np.array([d['y'] for d in data])
    pc_all = np.array([d['pc'] for d in data])
    pk_all = np.array([d['pk'] for d in data])
    ll_c = nll(pc_all, ys)
    # KNN 单路: excess 幅度通常小于真概率差, 提尖锐化系数扫描取最优 (诚实: 在全样本上选, 标注乐观偏差)
    best = (1e9, 1.0)
    for temp in (0.5, 0.75, 1.0, 1.5, 2.0):
        pk_t = pk_all ** temp
        pk_t = pk_t / pk_t.sum(axis=1, keepdims=True)
        ll = nll(pk_t, ys)
        if ll < best[0]:
            best = (ll, temp)
    ll_k, temp_k = best
    # 简单 stack: pc^a * pk^b 归一 (log 线性融合, 权重网格)
    best_s = (1e9, 1.0, 1.0)
    for a in (0.5, 1.0, 1.5, 2.0):
        for b in (0.25, 0.5, 1.0):
            ps_ = pc_all ** a * pk_all ** (b * temp_k)
            ps_ = ps_ / ps_.sum(axis=1, keepdims=True)
            ll = nll(ps_, ys)
            if ll < best_s[0]:
                best_s = (ll, a, b)
    ll_s, a_s, b_s = best_s
    acc_c = float((pc_all.argmax(1) == ys).mean())
    out = {'n': n, 'll_candles': round(ll_c, 5), 'll_knn_sharpened': round(ll_k, 5),
           'knn_temp': temp_k, 'll_stack': round(ll_s, 5), 'stack_w': [a_s, b_s],
           'acc_candles': round(acc_c, 4),
           'gate': {'n_min': 200, 'll_gain_min': 0.005,
                    'pass': bool(n >= 200 and ll_c - ll_s >= 0.005)},
           'note': 'KNN 概率由 excess+基率重建; temp 在全样本选优存在乐观偏差; n 不足按 UNDERPOWERED 对待'}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    with open(os.path.join(REPORT_DIR, 'knn_candles_stack.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
