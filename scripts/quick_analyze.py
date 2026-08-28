#!/usr/bin/env python3
"""
哨响AI · 快速分析器 v1.0
========================
单场比赛一站式分析: 方向 + 波胆 + 冷门 + 漂移 + tick + OU

用法:
  python scripts/quick_analyze.py [比赛名] oh_out od_out oa_out [ou_line] [ou_over] [ou_under] [oh_in od_in oa_in]

示例(终盘):
  python scripts/quick_analyze.py "曼城 vs 曼联" 1.85 3.60 4.20

示例(带OU):
  python scripts/quick_analyze.py "曼城 vs 曼联" 1.85 3.60 4.20 2.5 1.95 1.91

示例(带初盘漂移):
  python scripts/quick_analyze.py "曼城 vs 曼联" 1.85 3.60 4.20 2.5 1.95 1.91 2.10 3.40 3.80
"""

import sys

def tick(o):
    try:
        o=float(o); return int(round(o*100))%10 if 1.0<=o<1.5 else None
    except: return None

def poisson_scores(oh, od, oa, goals_per_side=1.4):
    """简易Poisson比分预测"""
    import math
    # 从1X2反推主/客λ
    h_prob = 1.0/oh; d_prob = 1.0/od; a_prob = 1.0/oa
    s = h_prob + d_prob + a_prob
    h_prob /= s; d_prob /= s; a_prob /= s
    # 主客进球期望
    h_lambda = goals_per_side * h_prob
    a_lambda = goals_per_side * a_prob
    scores = []
    for hg in range(0,6):
        for ag in range(0,6):
            p = (h_lambda**hg * math.exp(-h_lambda) / max(1, math.factorial(hg))) * \
                (a_lambda**ag * math.exp(-a_lambda) / max(1, math.factorial(ag)))
            scores.append((f'{hg}-{ag}', p))
    scores.sort(key=lambda x: -x[1])
    return scores[:5]

def analyze(name, oh, od, oa, ou_line=None, ou_over=None, ou_under=None, oh_in=None, od_in=None, oa_in=None):
    oh=float(oh); od=float(od); oa=float(oa)
    score = 3  # 基础分
    rec = []; warnings = []; props = []

    # ── 方向 ──
    o = {'home':oh,'draw':od,'away':oa}
    fav = min(o, key=lambda k: o[k])
    dir_cn = {'home':'主胜','draw':'平局','away':'客胜'}
    fav_odds = o[fav]
    props.append(f'方向={dir_cn[fav]}({fav_odds:.2f})')

    # ── 波胆 ──
    try:
        ps = poisson_scores(oh, od, oa)
        top_scores = [s + '({:.1f}%)'.format(p*100) for s, p in ps[:3]]
        props.append('波胆: ' + ' / '.join(top_scores))
    except:
        pass

    # ── 冷门风险 ──
    gap = abs(oh - oa)
    if fav_odds < 1.5:
        digit = int(round(fav_odds*100))%10
        if digit == 4:
            score -= 3; warnings.append(f'高冷门风险: 尾数.04大热门翻车率15.3%(vs正常11.6%)')
        elif digit in (1,2,9):
            score += 1  # 稳定资金
    if gap < 0.5 and od < 3.5:
        score -= 1; warnings.append(f'胶着盘: gap<0.5的翻车率62.9%')
    if gap > 10:
        score += 1; rec.append('deep fav(极低冷门)')

    # ── tick ──
    ht = tick(oh); at = tick(oa)
    if ht == 4: score -= 2; warnings.append('主陷.04(-7pp)')
    elif ht in (1,2,9): score += 3; rec.append(f'主强.{ht}(+8pp)')
    if at == 4: score -= 2; warnings.append('客陷.04(-8pp)')
    elif at in (1,2,9): score += 3; rec.append(f'客强.{at}(+8pp)')

    # ── 漂移 ──
    if oh_in and od_in and oa_in:
        oh_in=float(oh_in); od_in=float(od_in); oa_in=float(oa_in)
        dh = oh - oh_in; dd = od - od_in; da = oa - oa_in
        drec = []
        if dh < -0.1 and da > 0.3: drec.append('庄加主')
        if dd < -0.5: drec.append(f'锁平({od_in:.2f}→{od:.2f})')
        if drec: rec.append('漂: ' + '/'.join(drec))

    # ── OU ──
    if ou_over and ou_under:
        ov=float(ou_over); un=float(ou_under)
        if ov <= 1.75: score += 2; rec.append('大球优(+8.3pp)')
        if un <= 1.75: score += 2; rec.append('小球优(+10.3pp)')

    # ── 组装输出 ──
    level = '★★★★★' if score>=7 else ('★★★★' if score>=5 else ('★★★' if score>=3 else ('★★' if score>=1 else '★')))
    print(f'\n┏{"━"*62}┓')
    print(f'┃  {name[:50]}')
    print(f'┃  {level} 强度: {score:+d}  |  {" | ".join(props)}')
    if rec:
        print('┃  → ' + ' / '.join(rec))
    if warnings:
        for w in warnings:
            print(f'┃  ⚠ {w}')
    else:
        print(f'┃  ✓ 无风险信号')
    print(f'┗{"━"*62}┛')

    return score

if __name__ == '__main__':
    args = sys.argv[1:]
    if len(args) < 4:
        print('用法: quick_analyze.py 比赛名 oh od oa [ou_line ou_over ou_under] [oh_in od_in oa_in]')
        sys.exit(1)
    name = args[0]
    oh, od, oa = args[1], args[2], args[3]
    ou_line = args[4] if len(args)>4 else None
    ou_over = args[5] if len(args)>5 else None
    ou_under = args[6] if len(args)>6 else None
    oh_in = args[7] if len(args)>7 else None
    od_in = args[8] if len(args)>8 else None
    oa_in = args[9] if len(args)>9 else None
    analyze(name, oh, od, oa, ou_line, ou_over, ou_under, oh_in, od_in, oa_in)
