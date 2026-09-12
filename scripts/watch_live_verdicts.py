#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""盯盘器 (2026-09-10, 用户: 盯着前端的比赛和分析内容, 最后根据赛果迭代).

对滚球中比赛的当前判定(OU方向/1X2方向/CS首选+三选/门控)做时点快照,
比赛完赛后按快照结算命中率 — 并按 OU线段/领先状态 分层, 产出迭代依据。

用法:
  python scripts/watch_live_verdicts.py snapshot   # 抓当前滚球判定快照
  python scripts/watch_live_verdicts.py settle     # 对已完赛快照结算赛果
  python scripts/watch_live_verdicts.py loop       # snapshot + settle
快照存 data/watch_verdicts.json (判定首见即冻结, 不随后续刷新改写)。
"""
import json
import os
import sys
import urllib.request
import urllib.parse

sys.path.insert(0, r'D:\Architecture')
BASE = 'http://127.0.0.1:9000'
WATCH = r'D:\Architecture\data\watch_verdicts.json'


def get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load():
    if os.path.exists(WATCH):
        with open(WATCH, encoding='utf-8') as f:
            return json.load(f)
    return {}


def save(d):
    with open(WATCH, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def outcome(score):
    try:
        h, a = (int(x) for x in str(score).split('-')[:2])
    except Exception:
        return None
    return 'home' if h > a else ('draw' if h == a else 'away')


def snapshot():
    store = load()
    j = get(f'{BASE}/api/live-goal-probe/matches?limit=200')
    ms = ((j.get('data') or j).get('matches') or [])
    n_new = 0
    for m in ms:
        sc = m.get('score')
        if not sc or '-' not in sc:
            continue
        mk = m['match_key']
        try:
            d = (get(f"{BASE}/api/rollball/analyze?match_key={urllib.parse.quote(mk)}"
                     f"&score={urllib.parse.quote(sc)}&minute={int(m.get('minute') or 0)}").get('data') or {})
        except Exception:
            continue
        cs = d.get('cs') or {}
        ou = d.get('ou') or {}
        gate = d.get('consensus_gate') or {}
        if not (cs.get('found') and (cs.get('top5') or [])):
            continue
        import time as _t
        now_v = {
            'ou': {'line': ou.get('line'), 'direction': ou.get('direction'), 'prob': ou.get('prob'),
                   'signal': ou.get('signal'), 'data_source': ou.get('data_source')},
            'x2': (d.get('direction') or {}).get('winner'),
            'cs_top1': (cs.get('top5') or [{}])[0].get('score'),
            'cs_top3': [t['score'] for t in (cs.get('top5') or [])[:3]],
            'gate': gate.get('level'),
            'score': sc,
            'minute': m.get('minute'),
        }
        htf = d.get('ht_freeze')
        if mk not in store:
            v = dict(now_v)
            v['first_score'] = sc
            v['first_minute'] = m.get('minute')
            v['first_seen'] = _t.time()
            v['first_verdict'] = {'x2': now_v['x2'], 'cs_top1': now_v['cs_top1'],
                                  'cs_top3': now_v['cs_top3'],
                                  'ou_direction': (ou.get('direction') or {}).get('x') if False else (ou or {}).get('direction')}
            store[mk] = v
            n_new += 1
        else:
            v = store[mk]
            v.update(now_v)          # 最新判定持续更新 = 用户页面实际所见
            if htf and 'ht_freeze' not in v:
                v['ht_freeze'] = htf  # 中场冻结判定(只捕一次, 冻结语义)
    save(store)
    print(f'快照: 总 {len(store)} 场, 本轮新增 {n_new}')


def settle():
    store = load()
    import collections
    res = collections.defaultdict(lambda: {'win': 0, 'lose': 0})
    detail = collections.defaultdict(list)
    n_done = 0
    bias_latest = []
    bias_first = []

    def ou_win(v, f):
        o = v.get('ou') or {}
        fh, fa = (int(x) for x in str(f).split('-')[:2])
        ft = fh + fa
        if o.get('direction') in ('OVER', 'UNDER') and o.get('line'):
            ln = float(o['line'])
            if ft == ln:
                return None
            return (ft > ln) == (o['direction'] == 'OVER')
        return None

    def out_cls(score):
        fh, fa = (int(x) for x in str(score).split('-')[:2])
        return 'home' if fh > fa else ('draw' if fh == fa else 'away')

    for mk, v in list(store.items()):
        if not v.get('final'):
            try:
                import sqlite3
                con = sqlite3.connect(r'D:\Architecture\data\events.db', timeout=15)
                r = con.execute("SELECT score_home, score_away, status FROM matches WHERE match_key=?", (mk,)).fetchone()
                con.close()
            except Exception:
                continue
            if not r or r[2] != 'finished' or r[0] is None:
                continue
            v['final'] = f'{r[0]}-{r[1]}'
        n_done += 1
        f = v['final']
        for tag in ('first', 'latest'):
            vd = v.get(f'{tag}_verdict') if tag == 'first' else v
            if not vd:
                continue
            key = f'{tag}_1X2'
            if vd.get('x2'):
                act = out_cls(f)
                res[key]['win' if vd['x2'] == act else 'lose'] += 1
                if vd['x2'] != act:
                    detail[key].append(f"{mk[:20]} 判{vd['x2']} 实际{act}({f})")
            key = f'{tag}_CS_TOP1'
            if vd.get('cs_top1'):
                res[key]['win' if vd['cs_top1'] == f else 'lose'] += 1
            key = f'{tag}_CS_TOP3'
            if vd.get('cs_top3'):
                res[key]['win' if f in vd['cs_top3'] else 'lose'] += 1
            key = f'{tag}_OU'
            ow = ou_win(vd, f)
            if ow is not None:
                res[key]['win' if ow else 'lose'] += 1
        if v.get('cs_top1'):
            try:
                bias_latest.append(sum(int(x) for x in str(v['cs_top1']).split('-')[:2]) - sum(int(x) for x in str(f).split('-')[:2]))
            except Exception:
                pass
        if v.get('first_verdict', {}).get('cs_top1'):
            try:
                bias_first.append(sum(int(x) for x in str(v['first_verdict']['cs_top1']).split('-')[:2]) - sum(int(x) for x in str(f).split('-')[:2]))
            except Exception:
                pass
    save(store)
    print(f'完赛结算: 累计 {n_done} 场')
    import statistics
    for k in sorted(res):
        d = res[k]
        tot = d['win'] + d['lose']
        if tot:
            print(f'  {k}: {d["win"]}/{tot} = {d["win"]/tot*100:.1f}%')
    for k in list(detail):
        for e in detail[k][:6]:
            print(f'     ✗ {k}: {e}')
    if bias_latest:
        print(f'── 总球偏差(最新判定): 均值 {statistics.mean(bias_latest):+.2f} | 负 {sum(1 for b in bias_latest if b<0)}/{len(bias_latest)}')
    if bias_first:
        print(f'── 总球偏差(首见判定): 均值 {statistics.mean(bias_first):+.2f} | 负 {sum(1 for b in bias_first if b<0)}/{len(bias_first)}')
    # HT 冻结判定
    hts = [(mk, v) for mk, v in store.items() if v.get('ht_freeze') and v.get('final')]
    if hts:
        ht_ou = collections.defaultdict(lambda: [0, 0])
        for mk, v in hts:
            hf = v['ht_freeze']
            fh, fa = (int(x) for x in str(v['final']).split('-')[:2])
            ft = fh + fa
            od = (hf.get('ou') or {}).get('direction')
            ol = (hf.get('ou') or {}).get('line')
            if od in ('OVER', 'UNDER') and ol:
                if ft != ol:
                    ht_ou['OU'][0] += 1
                    ht_ou['OU'][1] += ((ft > ol) == (od == 'OVER'))
            xd = (hf.get('x2') or {}).get('direction')
            if xd:
                act = out_cls(v['final'])
                ht_ou['1X2'][0] += 1
                ht_ou['1X2'][1] += xd == act
        for k, (n, w) in ht_ou.items():
            print(f'  [HT冻结·{k}] {w}/{n} = {w/max(1,n)*100:.1f}%')


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if mode == 'snapshot':
        snapshot()
    elif mode == 'settle':
        settle()
    else:
        snapshot()
        settle()
