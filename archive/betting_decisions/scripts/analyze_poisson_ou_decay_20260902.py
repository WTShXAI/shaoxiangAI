# -*- coding: utf-8 -*-
"""poisson OU 纸盘衰减诊断 (2026-09-02).
拆 5 段时间窗, 重点判段5 负 ROI(-12.98%) 是真实恶化还是小样本噪声.
纯分析, 不动模型/不落注(IR-21). 读 reports/paper_track_poisson_ou.csv.
"""
import csv, re
from datetime import datetime
import random

random.seed(20260902)

def parse_ko(s: str):
    s = (s or '').strip()
    if not s:
        return datetime.max  # 无时间排最后
    s2 = s.replace('Z', '')
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(s2, fmt)
        except Exception:
            pass
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s2)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return datetime.max

rows = []
with open('reports/paper_track_poisson_ou.csv', encoding='utf-8') as f:
    for r in csv.DictReader(f):
        side = (r.get('side') or '').strip()
        prof = (r.get('profit') or '').strip()
        if not side or prof == '':
            continue
        try:
            p = float(prof)
        except Exception:
            continue
        rows.append({'ko': parse_ko(r.get('ko')), 'side': side, 'profit': p,
                     'odds': float(r.get('odds') or 0), 'line': r.get('line')})

rows.sort(key=lambda x: x['ko'])
n = len(rows)
print(f"有效下注 n={n}")

# 5 段 chrono quintiles
seg = 5
size = n // seg
segs = [rows[i*size:(i+1)*size] for i in range(seg-1)]
segs.append(rows[(seg-1)*size:])  # 末段含余数

print(f"{'段':<3}{'n':>4}{'累计ROI%':>10}{'段ROI%':>9}{'末注时间':>22}")
cum = 0.0
for i, s in enumerate(segs, 1):
    sp = sum(x['profit'] for x in s)
    roi = 100.0 * sp / len(s)
    cum += sp
    cumroi = 100.0 * cum / ((i)*len(s) if i < seg else n)  # 近似
    t = s[-1]['ko'].strftime('%Y-%m-%d %H:%M') if s else '-'
    print(f"{i:<3}{len(s):>4}{cumroi:>10.2f}{roi:>9.2f}{t:>22}")

# 段5 bootstrap CI
s5 = [x['profit'] for x in segs[-1]]
def boot_ci(data, iters=20000):
    k = len(data)
    tot = sum(data)
    means = []
    for _ in range(iters):
        s = sum(random.choice(data) for _ in range(k))
        means.append(s / k)
    means.sort()
    lo = means[int(0.025*len(means))]
    hi = means[int(0.975*len(means))]
    return 100.0*lo, 100.0*hi

lo, hi = boot_ci(s5)
s5roi = 100.0 * sum(s5) / len(s5)
print(f"\n段5 n={len(s5)} ROI={s5roi:.2f}% CI[95%]=[{lo:.2f}, {hi:.2f}]")
print("段5 CI 跨零?" , "是→噪声/不显著" if lo < 0 < hi else "否→显著负(真实恶化)")

# 段1-4 对照
early = [x['profit'] for x in segs[0]] + [x['profit'] for x in segs[1]] + \
        [x['profit'] for x in segs[2]] + [x['profit'] for x in segs[3]]
elo, ehi = boot_ci(early)
eroi = 100.0 * sum(early) / len(early)
print(f"段1-4 n={len(early)} ROI={eroi:.2f}% CI[95%]=[{elo:.2f}, {ehi:.2f}]")
print(f"段5 vs 段1-4 差值={(s5roi-eroi):.2f}pp")
# 诚实判据: 两 CI 是否重叠 (重叠=差异不显著, 非独立 t 检验近似)
overlap = lo < ehi and elo < hi
print("段5 显著差于前期(独立)?" , "否→两CI重叠, 差异在噪声内" if overlap else "是→CI不重叠")
