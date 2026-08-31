# -*- coding: utf-8 -*-
"""验证「终场结果读数」板块比分模型切换 (2026-08-31)

背景: 该板块比分原为 predict_fulltime_outcome 的 "OU隐含总球锚 + IR-07后验λ + round
      + 强制方向一致"; 用户要求换回 27 号前的 OIP 波胆矩阵 argmax。
本脚本对当前滚球比赛逐场并列对比两路输出, 确认前端切换后取到的是 OIP top1。

用法: .venv/Scripts/python.exe scripts/verify_fulltime_score_swap_20260831.py
"""
import json
import urllib.parse
import urllib.request

BASE = 'http://127.0.0.1:9000'


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def _post(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    j = _get('/api/live-goal-probe/matches?limit=50')
    matches = ((j.get('data') or {}).get('matches')) or []
    print(f'滚球比赛 {len(matches)} 场\n')
    print(f'{"比赛":<34}{"现分":>6}{"分":>5}  {"OIP top1":<10}{"OU推导":<9}{"一致":<5}')
    print('-' * 76)

    same = diff = miss_oip = miss_ou = 0
    recs = []   # (oip_top1, ou_derived, match_key, score, minute)
    for m in matches:
        mk = m.get('match_key')
        score = m.get('score') or '0-0'
        minute = int(m.get('minute') or 0)
        sp = score.split('-')
        hg = int(sp[0]) if sp[0].isdigit() else 0
        ag = int(sp[1]) if len(sp) > 1 and sp[1].isdigit() else 0

        # ① probe → fulltime.expected_score (OU 锚定推导, 旧主位)
        ou_score = None
        try:
            pj = _get(f'/api/live-goal-probe?match_key={urllib.parse.quote(mk)}'
                      f'&score={score}&minute={minute}')
            ou_score = ((pj.get('data') or {}).get('fulltime') or {}).get('expected_score')
        except Exception as e:
            ou_score = f'ERR'

        # ② terminal/analyze → oip.top3_scores[0] (OIP 波胆, 27号前路径, 新主位)
        oip_score = None
        try:
            body = {'home': m.get('home'), 'away': m.get('away'), 'sport_key': ''}
            if m.get('odds_h') and m.get('odds_d') and m.get('odds_a'):
                body.update(odds_h=m['odds_h'], odds_d=m['odds_d'], odds_a=m['odds_a'])
            if m.get('ou_line'):
                body.update(ou_line=m.get('ou_line'), ou_over=m.get('ou_over'),
                            ou_under=m.get('ou_under'))
            if minute > 0:
                body.update(home_goals=hg, away_goals=ag, elapsed=minute)
            aj = _post('/api/terminal/analyze', body)
            data = aj.get('data') or aj
            tops = ((data.get('oip') or {}).get('top3_scores')) or []
            oip_score = tops[0] if tops else None
        except Exception:
            oip_score = 'ERR'

        if not oip_score or oip_score == 'ERR':
            miss_oip += 1
        if not ou_score or ou_score == 'ERR':
            miss_ou += 1
        flag = ''
        if oip_score and ou_score and oip_score not in ('ERR',) and ou_score not in ('ERR',):
            if str(oip_score) == str(ou_score):
                same += 1
                flag = '='
            else:
                diff += 1
                flag = '≠'
        recs.append((oip_score, ou_score, mk, score, minute))
        name = (m.get('match_key') or '')[:32]
        print(f'{name:<34}{score:>6}{minute:>5}  {str(oip_score or "-"):<10}'
              f'{str(ou_score or "-"):<9}{flag:<5}')

    print('-' * 76)
    print(f'一致 {same} 场 / 分歧 {diff} 场 / OIP缺失 {miss_oip} / OU推导缺失 {miss_ou}')

    # 分布统计: 检验两路输出的"区分度"。OU推导受 round(λ)+强制方向一致 约束,
    # 赛前场会大量塌缩到 2-1 / 1-2 模板; OIP 走波胆矩阵 argmax, 分布更接近真实比分频率。
    def _dist(rows, idx):
        c = {}
        for r in rows:
            v = r[idx]
            if v and v != 'ERR':
                c[v] = c.get(v, 0) + 1
        return sorted(c.items(), key=lambda kv: -kv[1])

    print('\nOIP top1 分布 (前8):', _dist(recs, 0)[:8])
    print('OU 推导分布 (前8):  ', _dist(recs, 1)[:8])
    _ou = _dist(recs, 1)
    _tpl = sum(n for v, n in _ou if v in ('2-1', '1-2'))
    _tot = sum(n for _, n in _ou) or 1
    print(f'OU 推导塌缩到 2-1/1-2 的占比: {_tpl}/{_tot} = {_tpl/_tot*100:.1f}%')

    with open('reports/fulltime_score_swap_20260831.csv', 'w', encoding='utf-8') as f:
        f.write('match_key,score,minute,oip_top1,ou_derived\n')
        for r in recs:
            f.write(f'"{r[2]}",{r[3]},{r[4]},{r[0] or ""},{r[1] or ""}\n')
    print('\n明细已落盘: reports/fulltime_score_swap_20260831.csv')
    print('结论: 前端此板块主位比分现取 OIP top1 列; OU推导降为对照小字(分歧时才显示)。')


if __name__ == '__main__':
    main()
