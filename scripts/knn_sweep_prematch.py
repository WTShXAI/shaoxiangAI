#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KNN 赛前判定全量扫库 (2026-09-18 自主迭代: 初盘锚 stacking 的数据准备).

对 walkforward 数据集逐场离线计算 KNN verdict (query_match 2.5s/场, ~3400场≈145min),
落 JSONL 缓存 (断点续跑: 跳过已存在的 match_key)。
消费方: 下轮 stacking 评估 (KNN + K线集成 同折对照)。
"""
import json
import os
import sys
import time

sys.path.insert(0, r'D:\Architecture')

CACHE = r'D:\Architecture\data\knn_sweep_cache.jsonl'


def main():
    from analysis.live_goal_probe import _open_gq
    from pipeline.prematch_similarity import query_match
    con = _open_gq()
    mrows = con.execute("""
        SELECT match_key, kickoff FROM matches
        WHERE status='finished' AND score_home IS NOT NULL AND kickoff IS NOT NULL
        AND kickoff >= datetime('now', '-26 day')
        ORDER BY kickoff ASC LIMIT 12000""").fetchall()
    done = set()
    if os.path.exists(CACHE):
        with open(CACHE, encoding='utf-8') as f:
            for line in f:
                try:
                    done.add(json.loads(line)['match_key'])
                except Exception:
                    pass
    todo = [(mk, ko) for mk, ko in mrows if mk not in done]
    print(f'待扫 {len(todo)}/{len(mrows)} 场 (缓存已有 {len(done)})', flush=True)
    t0 = time.time()
    n_ok = n_na = 0
    with open(CACHE, 'a', encoding='utf-8') as f:
        for i, (mk, ko) in enumerate(todo):
            try:
                r = query_match(mk, k=8, draw_upgrade=True)
                rec = {'match_key': mk, 'kickoff': ko,
                       'applicable': bool(r.get('applicable')),
                       'verdict': r.get('verdict'), 'excess': r.get('excess')}
                if r.get('applicable'):
                    n_ok += 1
                else:
                    n_na += 1
            except Exception as e:
                rec = {'match_key': mk, 'kickoff': ko, 'applicable': False,
                       'verdict': None, 'err': f'{type(e).__name__}: {e}'}
                n_na += 1
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            if (i + 1) % 50 == 0:
                el = time.time() - t0
                print(f'[{i+1}/{len(todo)}] {el/60:.0f}min ok={n_ok} na={n_na} '
                      f'剩余≈{(len(todo)-i-1)*el/(i+1)/60:.0f}min', flush=True)
    print(f'完成: ok={n_ok} na={n_na} 总耗时 {(time.time()-t0)/60:.0f}min', flush=True)


if __name__ == '__main__':
    main()
