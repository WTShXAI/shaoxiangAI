#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
live_structure_classifier.py — 滚球(在场)赔率结构分类器
================================================================
扫描 events.db odds_snapshots (3163万行滚球快照) + 关联 match_outcomes(最终赛果),
把"相同滚球赔率结构"定义为一类, 为每一类建立历史画像:
  · 1X2 结构  → 最终 H/D/A 频率 · 庄家隐含 · 各方向ROI · 校准偏差 · 热门命中
  · OU 结构    → 该盘口线/方向最终打穿频率 · 隐含 · ROI · 校准

方法学(与赛前 odds_structure_classifier.py 同一套口径, 已逐项验证):
  - 结构维度: 滚球赔率本身已编码比分/分钟状态, 故与赛前一致按"赔率线"分桶
    (0.05 网格) 为主信号; 可选 --score-conditioned 用 score_at/minute_at(仅21%有值)加比分状态。
  - 结算锚: match_outcomes 最终比分/赛果(覆盖率85.5%)。
  - ROI: 每场以"该快照自身赔率"下注1单位, 命中=(赔率-1), 未中=-1, 求均值。
  - 校准偏差: 实际频率 − 隐含(1/赔率去水)。

⚠ 诚实边界(IR-30):
  - score_at/minute_at 仅 ~21% 填充, 故默认按赔率结构分桶(全量覆盖); 比分条件模式仅21%子集。
  - OU 结算按 最终总进球 vs line (全场) / 半场总进球 (OU_1H) / 下半场总进球 (OU_2H);
    .25/.75 分界线的"走盘/半赢半输"按 >line 简化判定(赢/不赢二值), 与庄家实际 split 结算略有差异, 已注明。
  - ROI 为描述性历史统计, 非未来收益保证; 单庄是否含 edge 须逐场判定。

🔴 数据质量红线(已实测, 必须遵守):
  - match_outcomes.ht_score_* 字段 **63.5% 被污染**(半场总进球 == 全场总进球, 即半场比分=全场比分);
    其中 OU_2H 关联赛事污染率 98.4%。根因: 该字段对"无下半场进球"的赛事被写成全场合。
  - 因此 OU_1H / OU_2H 结算不可信:
      · 未加校验时 OU_2H 出现 under@1.5=98.9%/over@1.5=0% 的退化(物理不可能)。
      · 加 (半场总<全场总) 完整性过滤后, 幸存子集仍**非随机**(系统性剔除低比分赛事), 存在存活偏差。
  - 结论: OU_1H / OU_2H 分类仅作"存在性展示 + 数据质量告警", **不得用于任何 edge/建仓判定**。
    可靠交付仅限 1X2(仅用最终赛果) 与 OU全场(仅用最终总进球)。

数据源 SSoT: D:/Architecture/data/events.db
  · odds_snapshots(3163万): market/selection/odds/line/score_at/minute_at
  · match_outcomes(9.5千有效场): home/away/result(H/D/A归一)/score_*/ht_score_*

用法:
  # 1X2 滚球单目标归类
  python live_structure_classifier.py --market 1x2 --h 2.10 --d 3.30 --a 3.40 --html live_1x2_report.html
  # OU 全场滚球单目标归类
  python live_structure_classifier.py --market ou --line 2.5 --odds 1.90 --sel over --html live_ou_report.html
  # 扫描全量建分类
  python live_structure_classifier.py --market 1x2 --build-taxonomy --min-sample 20 --json live_1x2_taxonomy.json --html live_1x2_taxonomy.html
  python live_structure_classifier.py --market ou  --build-taxonomy --min-sample 20 --json live_ou_taxonomy.json  --html live_ou_taxonomy.html
"""
import sqlite3, argparse, json, html, time
import numpy as np
from collections import defaultdict, Counter
from odds_structure_classifier import devig, pct, favorite_side, archetype

DB = 'D:/Architecture/data/events.db'
TOL = 0.05


def _norm_res(res):
    return {'home': 'H', 'draw': 'D', 'away': 'A'}.get(res, res)


def load_match_outcomes():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()
    mo = {}
    for home, away, res, sh, sa, hsh, has in cur.execute(
        "SELECT home,away,result,score_home,score_away,ht_score_home,ht_score_away "
        "FROM match_outcomes WHERE is_valid=1 AND result IS NOT NULL"):
        mo[f"{home} vs {away}"] = dict(res=_norm_res(res), sh=sh, sa=sa, hsh=hsh, has=has)
    con.close()
    return mo


# ---------- 载入 1X2 滚球快照 ----------
def load_1x2(score_cond=False):
    mo = load_match_outcomes()
    extra = ", MAX(score_at) sa, MAX(minute_at) mi" if score_cond else ""
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()
    q = f"""SELECT match_key,
              MAX(CASE selection WHEN 'home' THEN odds END) h,
              MAX(CASE selection WHEN 'draw' THEN odds END) d,
              MAX(CASE selection WHEN 'away' THEN odds END) a
              {extra}
           FROM odds_snapshots WHERE market LIKE '1X2%' AND odds>1.01 AND odds<1000
           GROUP BY match_key, round(captured_at,1)"""
    rows = cur.execute(q).fetchall()
    con.close()
    recs = []
    for r in rows:
        mk, h, d, a = r[0], r[1], r[2], r[3]
        if h is None or d is None or a is None:
            continue
        sc = (r[4] if score_cond else None, r[5] if score_cond else None)
        m = mo.get(mk)
        recs.append(dict(mk=mk, h=float(h), d=float(d), a=float(a),
                         res=m['res'] if m else None,
                         score_at=sc[0], minute_at=sc[1]))
    return recs


# ---------- 载入 OU 滚球快照 ----------
def load_ou(market_like, score_cond=False):
    mo = load_match_outcomes()
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()
    # OU 全场须排除半场/下半场盘口(OU_1H_*/OU_2H_*), 否则会按全场总进球误结算半场盘口 → 污染低盘口桶
    if market_like == 'OU%':
        where = "market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%'"
    else:
        where = f"market LIKE '{market_like}'"
    rows = cur.execute(
        f"SELECT match_key,line,selection,odds,score_at,minute_at "
        f"FROM odds_snapshots WHERE {where} AND odds>1.01 AND odds<1000").fetchall()
    con.close()
    recs = []
    for mk, line, sel, odds, sa, mi in rows:
        m = mo.get(mk)
        if not m:
            continue
        if market_like == 'OU_1H%':
            # 半场总进球: 必须 hsh/has 有值 且 半场总 < 全场总(否则 ht_score 被污染=全场合)
            if m['hsh'] is None or (m['hsh'] + m['has']) >= (m['sh'] + m['sa']):
                continue
            tot = m['hsh'] + m['has']
        elif market_like == 'OU_2H%':
            # 下半场总进球 = 全场 - 半场; 同上须 ht_score 有效(<全场)
            if m['hsh'] is None or (m['hsh'] + m['has']) >= (m['sh'] + m['sa']):
                continue
            tot = (m['sh'] + m['sa']) - (m['hsh'] + m['has'])
        else:
            tot = m['sh'] + m['sa']  # 全场
        if tot is None:
            continue
        win = (tot > line) if sel == 'over' else (tot < line)
        recs.append(dict(mk=mk, line=float(line), sel=sel, odds=float(odds), win=bool(win),
                         score_at=sa, minute_at=mi))
    return recs


# ---------- 聚合 ----------
def profile_1x2(subset):
    n = len(subset)
    if n == 0:
        return None
    cnt = Counter(r['res'] for r in subset if r['res'])
    H = cnt.get('H', 0); D = cnt.get('D', 0); A = cnt.get('A', 0)
    eff = H + D + A
    def roi(side_idx, ch):
        tot = 0.0; k = 0
        for r in subset:
            if not r['res']:
                continue
            o = (r['h'], r['d'], r['a'])[side_idx]
            tot += (o - 1.0) if r['res'] == ch else -1.0
            k += 1
        return tot / k if k else 0.0
    roi_h, roi_d, roi_a = roi(0, 'H'), roi(1, 'D'), roi(2, 'A')
    ph, pd, pa = devig(subset[0]['h'], subset[0]['d'], subset[0]['a'])
    fav = favorite_side(subset[0]['h'], subset[0]['d'], subset[0]['a'])
    fav_hit = sum(1 for r in subset if r['res'] == fav) / eff if eff else 0
    return dict(n=n, eff=eff, H=H, D=D, A=A,
                pH=H / eff, pD=D / eff, pA=A / eff,
                imp_h=ph, imp_d=pd, imp_a=pa,
                roi_h=roi_h, roi_d=roi_d, roi_a=roi_a,
                cal_h=H / eff - ph, cal_d=D / eff - pd, cal_a=A / eff - pa,
                fav=fav, fav_hit=fav_hit, over=(H + A) / eff,
                arch=archetype(subset[0]['h'], subset[0]['d'], subset[0]['a']))


def profile_ou(subset):
    n = len(subset)
    if n == 0:
        return None
    win = sum(1 for r in subset if r['win'])
    freq = win / n
    imp = 1.0 / subset[0]['odds']
    roi = sum((r['odds'] - 1.0) if r['win'] else -1.0 for r in subset) / n
    return dict(n=n, win=win, freq=freq, line=subset[0]['line'], sel=subset[0]['sel'],
                odds=subset[0]['odds'], imp=imp, roi=roi, cal=freq - imp,
                arch=f"OU{subsets_line(subset)} {subset[0]['sel']}｜隐含{imp*100:.1f}%")


def subsets_line(subset):
    return subset[0]['line']


# ---------- 1X2 归类 ----------
def classify_1x2(h, d, a, k=200, min_sample=20, score_cond=False):
    recs = [r for r in load_1x2(score_cond) if r['res']]
    rnd = lambda x: round(round(x / TOL) * TOL, 2)
    key = f"{rnd(h):.2f},{rnd(d):.2f},{rnd(a):.2f}"
    exact = [r for r in recs if f"{rnd(r['h']):.2f},{rnd(r['d']):.2f},{rnd(r['a']):.2f}" == key]
    CH = np.array([r['h'] for r in recs]); CD = np.array([r['d'] for r in recs]); CA = np.array([r['a'] for r in recs])
    d2 = (CH - h) ** 2 + (CD - d) ** 2 + (CA - a) ** 2
    near = [recs[i] for i in np.argpartition(d2, k)[:k]]
    agg_ex = profile_1x2(exact) if len(exact) else None
    agg_ne = profile_1x2(near)
    if agg_ex and agg_ex['eff'] >= min_sample:
        primary, mode, pidx = agg_ex, 'exact', exact
    else:
        primary, mode, pidx = agg_ne, 'nearest_fallback', near
    detail = sorted(pidx, key=lambda r: abs(r['h'] - h) + abs(r['d'] - d) + abs(r['a'] - a))[:20]
    return dict(target=(h, d, a), tol=TOL, key=key, kind='1x2',
                exact_n=len(exact), near_n=len(near),
                agg_ex=agg_ex, agg_ne=agg_ne, primary=primary, mode=mode, detail=detail)


# ---------- OU 归类 ----------
def classify_ou(line, odds, sel, k=200, min_sample=20, market_like='OU%'):
    recs = load_ou(market_like)
    rnd = lambda x: round(round(x / TOL) * TOL, 2)
    key = f"{rnd(line):.2f}|{sel}|{rnd(odds):.2f}"
    exact = [r for r in recs if f"{rnd(r['line']):.2f}|{r['sel']}|{rnd(r['odds']):.2f}" == key]
    L = np.array([r['line'] for r in recs]); O = np.array([r['odds'] for r in recs])
    d2 = (L - line) ** 2 * 4 + (O - odds) ** 2
    near = [recs[i] for i in np.argpartition(d2, k)[:k]]
    agg_ex = profile_ou(exact) if len(exact) else None
    agg_ne = profile_ou(near)
    if agg_ex and agg_ex['n'] >= min_sample:
        primary, mode, pidx = agg_ex, 'exact', exact
    else:
        primary, mode, pidx = agg_ne, 'nearest_fallback', near
    detail = sorted(pidx, key=lambda r: abs(r['line'] - line) + abs(r['odds'] - odds))[:20]
    return dict(target=(line, odds, sel), tol=TOL, key=key, kind='ou',
                exact_n=len(exact), near_n=len(near),
                agg_ex=agg_ex, agg_ne=agg_ne, primary=primary, mode=mode, detail=detail)


# ---------- 全量分类 ----------
def build_taxonomy_1x2(min_sample=20, score_cond=False):
    recs = [r for r in load_1x2(score_cond) if r['res']]
    rnd = lambda x: round(round(x / TOL) * TOL, 2)
    buckets = defaultdict(list)
    for r in recs:
        buckets[f"{rnd(r['h']):.2f},{rnd(r['d']):.2f},{rnd(r['a']):.2f}"].append(r)
    out = []
    for key, sub in buckets.items():
        if len(sub) < min_sample:
            continue
        p = profile_1x2(sub)
        if not p:
            continue
        hs, ds, as_ = (float(x) for x in key.split(','))
        out.append(dict(key=key, line=(hs, ds, as_), **p))
    out.sort(key=lambda x: -x['eff'])
    return dict(kind='1x2', total_eligible=len(recs), classified_buckets=len(out),
                min_sample=min_sample, score_cond=score_cond, buckets=out)


def build_taxonomy_ou(min_sample=20, market_like='OU%'):
    recs = load_ou(market_like)
    dq_warn = market_like in ('OU_1H%', 'OU_2H%')
    rnd = lambda x: round(round(x / TOL) * TOL, 2)
    buckets = defaultdict(list)
    for r in recs:
        buckets[f"{rnd(r['line']):.2f}|{r['sel']}|{rnd(r['odds']):.2f}"].append(r)
    out = []
    for key, sub in buckets.items():
        if len(sub) < min_sample:
            continue
        p = profile_ou(sub)
        if not p:
            continue
        out.append(dict(key=key, **p))
    out.sort(key=lambda x: -x['n'])
    return dict(kind='ou', market_like=market_like, total_eligible=len(recs),
                classified_buckets=len(out), min_sample=min_sample,
                data_quality_warning=dq_warn, buckets=out)


# ---------- 报告 ----------
def report_html_1x2(r):
    h, d, a = r['target']
    det = r['detail']
    rows = "".join(
        f"<tr><td>{html.escape(str(m['mk']))}</td>"
        f"<td>{m['h']:.2f},{m['d']:.2f},{m['a']:.2f}</td>"
        f"<td>{m['res']}</td></tr>"
        for m in det)
    arch = r['primary']['arch'] if r['primary'] else '—'
    def blk(title, agg, tag):
        if not agg:
            return f"<h3>{title}</h3><p style='color:#f55'>n=0 无同结构</p>"
        e = agg['eff']
        return f"""<h3>{title}（{tag}）</h3>
<table border=1 cellpadding=6 cellspacing=0 style='border-collapse:collapse;font-size:14px'>
<tr><th>指标</th><th>主胜 H</th><th>平 D</th><th>客胜 A</th></tr>
<tr><td>样本(有赛果) / 频率</td><td colspan=3>n={e} ｜ 分胜负 {pct(agg['over'])}</td></tr>
<tr><td>实际赛果频率</td><td>{pct(agg['pH'])}</td><td>{pct(agg['pD'])}</td><td>{pct(agg['pA'])}</td></tr>
<tr><td>庄家隐含概率</td><td>{pct(agg['imp_h'])}</td><td>{pct(agg['imp_d'])}</td><td>{pct(agg['imp_a'])}</td></tr>
<tr><td>ROI(每场自身赔率下注1单位)</td><td>{agg['roi_h']:+.3f}</td><td>{agg['roi_d']:+.3f}</td><td>{agg['roi_a']:+.3f}</td></tr>
<tr><td>校准偏差(实际-隐含)</td><td>{agg['cal_h']:+.3f}</td><td>{agg['cal_d']:+.3f}</td><td>{agg['cal_a']:+.3f}</td></tr>
<tr><td>热门(argmax)命中</td><td colspan=3>{agg['fav']} = {pct(agg['fav_hit'])}</td></tr></table>"""
    return f"""<!doctype html><html><head><meta charset=utf-8><title>滚球1X2结构分析</title></head>
<body style='font-family:Segoe UI,Microsoft YaHei;background:#0f1115;color:#e6e6e6;padding:24px'>
<h2>滚球 1X2 赔率结构分析</h2>
<p>目标滚球1X2: <b>主 {h} / 平 {d} / 客 {a}</b> ｜ tol={r['tol']} ｜ 模式: {r['mode']}</p>
<p style='color:#9ab'>结构标签: <b>{html.escape(arch)}</b></p>
{blk('① 精确分桶（同赔率结构, n='+str(r['exact_n'])+'）', r['agg_ex'], 'exact')}
{blk('② 最近邻 TOP-'+str(r['near_n'])+'（'+r['mode']+'）', r['agg_ne'], r['mode'])}
<h3>③ 明细 (TOP {len(det)})</h3>
<table border=1 cellpadding=5 cellspacing=0 style='border-collapse:collapse;font-size:13px'>
<tr><th>对阵</th><th>滚球1X2</th><th>最终赛果</th></tr>{rows}</table>
<p style='color:#888;font-size:12px'>数据源: events.db odds_snapshots(滚球) + match_outcomes(最终赛果, 覆盖率85.5%)。ROI为描述性统计。</p>
</body></html>"""


def report_html_ou(r):
    line, odds, sel = r['target']
    det = r['detail']
    rows = "".join(
        f"<tr><td>{html.escape(str(m['mk']))}</td><td>{m['line']:.2f}</td>"
        f"<td>{m['sel']}</td><td>{m['odds']:.2f}</td><td>{'打穿' if m['win'] else '未打穿'}</td></tr>"
        for m in det)
    def blk(title, agg, tag):
        if not agg:
            return f"<h3>{title}</h3><p style='color:#f55'>n=0 无同结构</p>"
        return f"""<h3>{title}（{tag}）</h3>
<table border=1 cellpadding=6 cellspacing=0 style='border-collapse:collapse;font-size:14px'>
<tr><th>指标</th><th>值</th></tr>
<tr><td>样本 n</td><td>{agg['n']}</td></tr>
<tr><td>{agg['sel']} 打穿频率</td><td>{pct(agg['freq'])}</td></tr>
<tr><td>庄家隐含(1/赔率)</td><td>{pct(agg['imp'])}</td></tr>
<tr><td>ROI(自身赔率下注1单位)</td><td>{agg['roi']:+.3f}</td></tr>
<tr><td>校准偏差(实际-隐含)</td><td>{agg['cal']:+.3f}</td></tr></table>"""
    return f"""<!doctype html><html><head><meta charset=utf-8><title>滚球OU结构分析</title></head>
<body style='font-family:Segoe UI,Microsoft YaHei;background:#0f1115;color:#e6e6e6;padding:24px'>
<h2>滚球 大小球(OU) 赔率结构分析</h2>
<p>目标: <b>OU {line} {sel} @ {odds}</b> ｜ tol={r['tol']} ｜ 模式: {r['mode']}</p>
{blk('① 精确分桶（同结构, n='+str(r['exact_n'])+'）', r['agg_ex'], 'exact')}
{blk('② 最近邻 TOP-'+str(r['near_n'])+'（'+r['mode']+'）', r['agg_ne'], r['mode'])}
<h3>③ 明细 (TOP {len(det)})</h3>
<table border=1 cellpadding=5 cellspacing=0 style='border-collapse:collapse;font-size:13px'>
<tr><th>对阵</th><th>盘口</th><th>方向</th><th>赔率</th><th>结算</th></tr>{rows}</table>
<p style='color:#888;font-size:12px'>数据源: events.db odds_snapshots(滚球) + match_outcomes(最终总进球)。全场OU按最终总进球 vs line 结算; .25/.75 线按&gt;line 二值简化。ROI为描述性统计。</p>
</body></html>"""


def taxonomy_html_1x2(taxo):
    b = taxo['buckets']
    top = "".join(
        f"<tr><td>{x['key']}</td><td>{x['eff']}</td><td>{pct(x['pH'])}</td><td>{pct(x['pD'])}</td>"
        f"<td>{pct(x['pA'])}</td><td>{x['roi_h']:+.3f}</td><td>{x['roi_d']:+.3f}</td><td>{x['roi_a']:+.3f}</td></tr>"
        for x in b[:200])
    return f"""<!doctype html><html><head><meta charset=utf-8><title>滚球1X2结构分类</title></head>
<body style='font-family:Segoe UI,Microsoft YaHei;background:#0f1115;color:#e6e6e6;padding:24px'>
<h2>滚球 1X2 赔率结构分类 · 全量</h2>
<p style='color:#9ab'>数据源: events.db ｜ 有效滚球快照(关联赛果) {taxo['total_eligible']:,} ｜ 纳入分类(n>={taxo['min_sample']}) {taxo['classified_buckets']:,}{' ｜ 比分条件模式' if taxo['score_cond'] else ''}</p>
<h3>TOP 结构桶 (按样本量, 前200)</h3>
<table border=1 cellpadding=5 cellspacing=0 style='border-collapse:collapse;font-size:13px'>
<tr><th>结构(主,平,客)</th><th>n</th><th>主%</th><th>平%</th><th>客%</th><th>ROI主</th><th>ROI平</th><th>ROI客</th></tr>
{top}</table>
<p style='color:#888;font-size:12px'>ROI为描述性历史统计。同赔率结构 = 滚球赔率线(0.05网格); 滚球赔率已编码比分/分钟状态。</p></body></html>"""


def taxonomy_html_ou(taxo):
    b = taxo['buckets']
    top = "".join(
        f"<tr><td>{x['key']}</td><td>{x['n']}</td><td>{pct(x['freq'])}</td>"
        f"<td>{x['roi']:+.3f}</td><td>{x['cal']:+.3f}</td></tr>"
        for x in b[:200])
    warn = ""
    if taxo.get('data_quality_warning'):
        warn = f"""<div style='background:#3a0d0d;border:1px solid #f55;color:#fbb;padding:12px;border-radius:8px;margin:10px 0'>
⚠ <b>数据质量红线 · 此分类不可用于任何 edge/建仓判定</b><br>
match_outcomes.ht_score_* 字段 63.5% 被污染(半场总进球=全场总进球), 且污染<b>非随机</b>
(系统性剔除低比分赛事, 幸存子集存在存活偏差)。即便加了 (半场总&lt;全场总) 完整性过滤,
本分类仍仅作"存在性展示"。可靠交付仅限 OU全场(最终总进球) 与 1X2(最终赛果)。</div>"""
    return f"""<!doctype html><html><head><meta charset=utf-8><title>滚球OU结构分类</title></head>
<body style='font-family:Segoe UI,Microsoft YaHei;background:#0f1115;color:#e6e6e6;padding:24px'>
<h2>滚球 大小球(OU) 赔率结构分类 · 全量</h2>
<p style='color:#9ab'>数据源: events.db ｜ 有效滚球OU快照(关联总进球) {taxo['total_eligible']:,} ｜ 纳入分类(n>={taxo['min_sample']}) {taxo['classified_buckets']:,} ｜ market={taxo['market_like']}</p>
{warn}
<h3>TOP 结构桶 (按样本量, 前200)</h3>
<table border=1 cellpadding=5 cellspacing=0 style='border-collapse:collapse;font-size:13px'>
<tr><th>结构(line|方向|赔率)</th><th>n</th><th>打穿频率</th><th>ROI</th><th>校准偏差</th></tr>
{top}</table>
<p style='color:#888;font-size:12px'>ROI为描述性历史统计。OU_1H 按半场总进球 / OU_2H 按下半场总进球 vs line 结算; 见上方数据质量红线。</p></body></html>"""


def main():
    ap = argparse.ArgumentParser(description='滚球赔率结构分类器')
    ap.add_argument('--market', choices=['1x2', 'ou'], default='1x2')
    ap.add_argument('--h', type=float); ap.add_argument('--d', type=float); ap.add_argument('--a', type=float)
    ap.add_argument('--line', type=float); ap.add_argument('--odds', type=float); ap.add_argument('--sel', choices=['over', 'under'])
    ap.add_argument('--market-like', default='OU%', help='OU 模式: OU%(全场) / OU_1H% / OU_2H%')
    ap.add_argument('--tol', type=float, default=TOL)
    ap.add_argument('--k', type=int, default=200)
    ap.add_argument('--min-sample', type=int, default=20)
    ap.add_argument('--score-conditioned', action='store_true')
    ap.add_argument('--build-taxonomy', action='store_true')
    ap.add_argument('--html', default=None); ap.add_argument('--json', default=None)
    args = ap.parse_args()

    if args.build_taxonomy:
        if args.market == '1x2':
            taxo = build_taxonomy_1x2(min_sample=args.min_sample, score_cond=args.score_conditioned)
            print(f"[1X2 滚球分类] 有效快照={taxo['total_eligible']:,} 纳入分类={taxo['classified_buckets']:,}")
            if args.json:
                json.dump(taxo, open(args.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=2, default=str)
                print(f"[JSON] {args.json}")
            if args.html:
                open(args.html, 'w', encoding='utf-8').write(taxonomy_html_1x2(taxo))
                print(f"[HTML] {args.html}")
        else:
            taxo = build_taxonomy_ou(min_sample=args.min_sample, market_like=args.market_like)
            dq = "  ⚠ 数据质量红线: ht_score 污染, 仅展示不可用edge" if taxo.get('data_quality_warning') else ""
            print(f"[OU 滚球分类] 有效快照={taxo['total_eligible']:,} 纳入分类={taxo['classified_buckets']:,} market={args.market_like}{dq}")
            if args.json:
                json.dump(taxo, open(args.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=2, default=str)
                print(f"[JSON] {args.json}")
            if args.html:
                open(args.html, 'w', encoding='utf-8').write(taxonomy_html_ou(taxo))
                print(f"[HTML] {args.html}")
        return

    if args.market == '1x2':
        if not (args.h and args.d and args.a):
            print("错误: 1X2 需要 --h --d --a"); return
        r = classify_1x2(args.h, args.d, args.a, args.k, args.min_sample, args.score_conditioned)
        print(f"[滚球1X2] 主{args.h}/平{args.d}/客{args.a} 精确n={r['exact_n']} 最近邻n={r['near_n']} 模式={r['mode']}")
        if r['agg_ex']:
            e = r['agg_ex']; print(f"[精确] H{pct(e['pH'])}/D{pct(e['pD'])}/A{pct(e['pA'])} ROI H{e['roi_h']:+.3f}/D{e['roi_d']:+.3f}/A{e['roi_a']:+.3f}")
        if args.html:
            open(args.html, 'w', encoding='utf-8').write(report_html_1x2(r)); print(f"[HTML] {args.html}")
    else:
        if not (args.line and args.odds and args.sel):
            print("错误: OU 需要 --line --odds --sel"); return
        r = classify_ou(args.line, args.odds, args.sel, args.k, args.min_sample, args.market_like)
        print(f"[滚球OU] OU{args.line} {args.sel}@{args.odds} 精确n={r['exact_n']} 最近邻n={r['near_n']} 模式={r['mode']}")
        if r['agg_ex']:
            e = r['agg_ex']; print(f"[精确] 打穿{pct(e['freq'])} ROI{e['roi']:+.3f} 校准{e['cal']:+.3f}")
        if args.html:
            open(args.html, 'w', encoding='utf-8').write(report_html_ou(r)); print(f"[HTML] {args.html}")
    if args.json and not args.build_taxonomy:
        json.dump(r, open(args.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=2, default=str)
        print(f"[JSON] {args.json}")


if __name__ == '__main__':
    main()
