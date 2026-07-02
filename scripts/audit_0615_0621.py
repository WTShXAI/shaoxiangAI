#!/usr/bin/env python
"""6/15-6/21 WC2026 Elo预测 vs 实际赛果交叉审查"""
import sys, math

from rules.drawgate_v53 import apply_drawgate, imp_from_odds

# Web-fetched actual results (bracketmundial2026.com)
actual_results = [
    ("06-15", "法国", "塞内加尔", "H", "3-1"),
    ("06-15", "伊拉克", "挪威", "A", "1-4"),
    ("06-15", "阿根廷", "阿尔及利亚", "H", "3-0"),
    ("06-15", "奥地利", "约旦", "H", "3-1"),
    ("06-17", "奥地利", "约旦", "H", "3-1"),   # dup removed by key
    ("06-17", "葡萄牙", "民主刚果", "D", "1-1"),
    ("06-17", "英格兰", "克罗地亚", "H", "4-2"),
    ("06-17", "加纳", "巴拿马", "H", "1-0"),
    ("06-17", "乌兹别克斯坦", "哥伦比亚", "A", "1-3"),
    ("06-18", "捷克", "南非", "D", "1-1"),
    ("06-18", "瑞士", "波黑", "H", "4-1"),
    ("06-18", "加拿大", "卡塔尔", "H", "6-0"),
    ("06-18", "墨西哥", "韩国", "H", "1-0"),
    ("06-19", "美国", "澳大利亚", "H", "2-0"),
    ("06-19", "苏格兰", "摩洛哥", "A", "0-1"),
    ("06-19", "巴西", "海地", "H", "3-0"),
    ("06-19", "土耳其", "巴拉圭", "A", "0-1"),
    ("06-20", "荷兰", "瑞典", "H", "5-1"),
    ("06-20", "德国", "科特迪瓦", "H", "2-1"),
    ("06-20", "厄瓜多尔", "库拉索", "D", "0-0"),
    ("06-21", "突尼斯", "日本", "A", "0-4"),
    ("06-21", "西班牙", "沙特阿拉伯", "H", "4-0"),
    ("06-21", "比利时", "伊朗", "D", "0-0"),
    ("06-21", "乌拉圭", "佛得角", "D", "2-2"),
    ("06-21", "新西兰", "埃及", "A", "1-3"),
]

# Deduplicate
seen = set()
unique = []
for r in actual_results:
    key = (r[0], r[1], r[2])
    if key not in seen:
        seen.add(key)
        unique.append(r)

fifa_ranks = {
    "法国": 2, "塞内加尔": 20, "伊拉克": 72, "挪威": 45, "阿根廷": 1, "阿尔及利亚": 35,
    "奥地利": 25, "约旦": 65, "葡萄牙": 8, "民主刚果": 55, "英格兰": 5, "克罗地亚": 12,
    "加纳": 60, "巴拿马": 50, "乌兹别克斯坦": 58, "哥伦比亚": 15,
    "捷克": 30, "南非": 70, "瑞士": 18, "波黑": 40, "加拿大": 38, "卡塔尔": 48,
    "墨西哥": 22, "韩国": 28, "美国": 14, "澳大利亚": 32, "苏格兰": 35, "摩洛哥": 16,
    "巴西": 3, "海地": 80, "土耳其": 36, "巴拉圭": 42,
    "荷兰": 7, "瑞典": 24, "德国": 6, "科特迪瓦": 52, "厄瓜多尔": 33, "库拉索": 75,
    "突尼斯": 44, "日本": 17, "西班牙": 4, "沙特阿拉伯": 56, "比利时": 9, "伊朗": 30,
    "佛得角": 68, "埃及": 34, "新西兰": 95,
}

def fe(t):
    return 2000 - (fifa_ranks.get(t, 100) - 1) * 6

elo = {t: fe(t) for t in fifa_ranks}

def elo_predict(home, away):
    eh, ea = elo.get(home, 1500), elo.get(away, 1500)
    d = eh - ea
    ph = 1 / (1 + 10**(-d/400))
    pa = 1 / (1 + 10**(d/400))
    pd = 0.28 * 2.71828**(-(abs(d)/400)**2)
    t = ph + pa + pd
    ph, pd, pa = ph/t, pd/t, pa/t
    oh, od, oa = 1/ph, 1/pd, 1/pa

    ou_est = max(2.0, min(4.0, 2.5 + abs(d)/300))
    ratio = 1 / (1 + 10**(-d/400))
    lh = ou_est * ratio
    la = ou_est - lh
    lh = max(0.8, min(4.5, lh))
    la = max(0.5, min(3.5, la))

    scores = []
    for h in range(8):
        for a in range(8):
            try:
                p_h = (lh**h * math.exp(-lh)) / max(math.factorial(h), 1)
                p_a = (la**a * math.exp(-la)) / max(math.factorial(a), 1)
                scores.append((h, a, p_h * p_a))
            except (OverflowError, ValueError):
                pass
    scores.sort(key=lambda x: x[2], reverse=True)

    imp_h, imp_d, imp_a = imp_from_odds(oh, od, oa)
    dgate = apply_drawgate(imp_h, imp_d, imp_a,
        odds={'home': oh, 'draw': od, 'away': oa},
        handicap=round(d/100, 2), ou_line=ou_est, match_type='tournament')

    if ph > 0.55:
        verdict = 'H'
    elif pa > 0.55:
        verdict = 'A'
    elif pd > 0.28 and dgate.get('dgate_mode') != 'none':
        verdict = 'D'
    else:
        verdict = max([('H', ph), ('D', pd), ('A', pa)], key=lambda x: x[1])[0]

    top = []
    for h, a, pr in scores:
        ok = (verdict == 'H' and h > a) or (verdict == 'A' and h < a) or (verdict == 'D' and h == a)
        if ok:
            top.append((h, a, pr))
        if len(top) >= 2:
            break
    sc_str = ' / '.join([f'{h}-{a}' for h, a, _ in top[:2]]) if top else '?'
    return verdict, sc_str

results = []
for dt, home, away, act_res, act_score in unique:
    verdict, sc = elo_predict(home, away)
    try:
        ah, aa = map(int, act_score.split('-'))
        ps = sc.split(' / ')[0]
        ph_s, pa_s = map(int, ps.split('-'))
    except (ValueError, TypeError):
        ah = aa = ph_s = pa_s = 0

    act_total = ah + aa
    pred_total = ph_s + pa_s
    dir_ok = act_res == verdict[0]
    score_exact = (ph_s == ah and pa_s == aa)
    score_gap = abs(act_total - pred_total)

    errors = []
    if not dir_ok: errors.append('方向错')
    if not score_exact:
        if score_gap == 1: errors.append('差1球')
        elif score_gap >= 2: errors.append(f'差{score_gap}球')
    if act_res == 'D' and verdict != 'D': errors.append('漏平局')
    if max(ah, aa) >= 4 and max(ph_s, pa_s) <= 3: errors.append('攻低')
    if verdict == 'D' and act_res != 'D': errors.append('误平')

    results.append({
        'dt': dt[-2:], 'match': f'{home} vs {away}',
        'actual': act_score, 'verdict': verdict, 'score': sc,
        'dir_ok': dir_ok, 'exact': score_exact, 'gap': score_gap,
        'error': '|'.join(errors) if errors else '✅'
    })

total = len(results)
exact = sum(1 for r in results if r['exact'])
dir_c = sum(1 for r in results if r['dir_ok'])
ok1 = sum(1 for r in results if r['gap'] <= 1)
avg = sum(r['gap'] for r in results) / max(total, 1)
bad = [r for r in results if r['error'] != '✅']

print(f'=== 6/15-21 Elo+D-Gate ===')
print(f'总:{total} 方向:{dir_c}/{total}={dir_c/total*100:.0f}% 精确:{exact}/{total}={exact/total*100:.0f}% ≤1球差:{ok1}/{total}={ok1/total*100:.0f}% 均差:{avg:.1f}球')
for r in sorted(results, key=lambda x: x['dt']):
    mk = 'X' if r['error'] != '✅' else 'O'
    print(f"{mk} {r['dt']} {r['match']:28s} 实={r['actual']:5s} 预={r['verdict']} {r['score']:10s} | {r['error']}")
print(f'\n问题({len(bad)}):')
for r in bad:
    print(f"  {r['match']}: 实={r['actual']} 预={r['verdict']} {r['score']} → {r['error']}")

# Write report
with open('D:/Architecture/deliverables/wc-audit-0615-0621.md', 'w', encoding='utf-8') as f:
    f.write(f'# WC2026 预测审查 6/15-6/21 (Elo+D-Gate)\n\n')
    f.write('## 汇总\n| 指标 | 结果 |\n|------|------|\n')
    f.write(f'| 总场次 | {total} |\n')
    f.write(f'| 方向准确率 | {dir_c}/{total}={dir_c/total*100:.0f}% |\n')
    f.write(f'| 比分精确 | {exact}/{total}={exact/total*100:.0f}% |\n')
    f.write(f'| ≤1球差 | {ok1}/{total}={ok1/total*100:.0f}% |\n')
    f.write(f'| 均差 | {avg:.1f}球 |\n\n')
    f.write('## 逐场\n| 日期 | 比赛 | 实际 | 预测 | 差 | 问题 |\n|------|------|------|------|------|------|\n')
    for r in sorted(results, key=lambda x: x['dt']):
        f.write(f"| {r['dt']} | {r['match']} | {r['actual']} | {r['verdict']} {r['score']} | {r['gap']}球 | {r['error']} |\n")
    f.write('\n## 问题场次\n')
    for r in bad:
        f.write(f"- {r['match']}: 实={r['actual']} 预={r['verdict']} {r['score']} → {r['error']}\n")
print(f'Report: deliverables/wc-audit-0615-0621.md')
