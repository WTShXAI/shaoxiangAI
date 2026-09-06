#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
odds_structure_classifier.py — 同赔率结构历史场次分析 · 全量分类器
================================================================
扫描 football_data.historical_matches 全量真实场次(31.2万场, 收盘1X2 + 赛果),
把"相同赔率结构"定义为一类(bucket, 0.05 网格), 为每一类建立历史画像:
  赛果频率(H/D/A) · 庄家隐含概率 · 各方向ROI · 校准偏差(实际-隐含) · 热门(argmax)命中率 · 分胜负%

两类入口:
  1) classify(h,d,a)      — 对任意"目标收盘赔率线"归类, 返回其历史结构画像
                            (精确分桶 + 最近邻TOP-K + 明细TOP-20 + 结构分类标签)
  2) build_taxonomy()     — 扫描全量, 把所有赔率结构建成分类(每类一眼历史画像)
                            → 导出 odds_structure_taxonomy.json / .html

方法学(与 qingdao_historical_comp.html 演示一致, 已逐项复现验证):
  - 精确分桶: 目标线四舍五入到 0.05 网格 → (2.22,3.20,2.94)→(2.20,3.20,2.95),
    取该网格内全部历史场次。
  - 最近邻: 欧氏距离在(主,平,客)赔率空间取 TOP-K。
  - ROI: 每场以"该场自身收盘赔率"下注1单位, 命中=(赔率-1), 未中=-1, 求均值。
  - 校准偏差: 实际赛果频率 − 庄家去水隐含概率。
  - 主信号: 精确分桶(若 n>=min_sample), 否则回退最近邻。

⚠ 诚实边界(IR-30): ROI 为描述性历史统计, 非未来收益保证; 单庄是否含 edge 须逐场判定,
  不预设。低样本(n<min_sample)结构不计入分类主信号。

数据源 SSoT: D:/Architecture/data/football_data.db · historical_matches
(注: 根目录 D:/Architecture/football_data.db 为空壳, 勿用)

用法:
  python odds_structure_classifier.py --h 2.22 --d 3.20 --a 2.94
  python odds_structure_classifier.py --h 2.22 --d 3.20 --a 2.94 --html demo_report.html --json demo.json
  python odds_structure_classifier.py --build-taxonomy --min-sample 20 \
        --json odds_structure_taxonomy.json --html odds_structure_taxonomy.html
"""
import sqlite3, argparse, json, html, time
import numpy as np
from collections import Counter, defaultdict

DB = 'D:/Architecture/data/football_data.db'
TOL = 0.05

# ---------- 全局缓存 ----------
_CACHE = None


def load_all():
    """一次性加载全量历史场次(收盘1X2 + 赛果 + 明细)。numpy 数组 + meta 列表。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    t0 = time.time()
    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute(
        """SELECT close_home_odds, close_draw_odds, close_away_odds, final_result,
                  home_score, away_score, league_name, home_team, away_team, match_date
           FROM historical_matches
           WHERE close_home_odds>0 AND close_draw_odds>0 AND close_away_odds>0
             AND final_result IS NOT NULL"""
    ).fetchall()
    con.close()
    CH = np.array([r[0] for r in rows], dtype=float)
    CD = np.array([r[1] for r in rows], dtype=float)
    CA = np.array([r[2] for r in rows], dtype=float)
    meta = [dict(outcome=r[3], hs=r[4], aw=r[5], league=r[6], home=r[7], away=r[8], date=r[9],
               ch=r[0], cd=r[1], ca=r[2]) for r in rows]
    # 预建 bucket 键(字符串, 鲁棒) → 行索引列表, 供精确分桶 O(1) 查
    bk = np.round(np.round(CH / TOL) * TOL, 2)
    bkd = np.round(np.round(CD / TOL) * TOL, 2)
    bka = np.round(np.round(CA / TOL) * TOL, 2)
    keystr = np.char.add(np.char.add(
        np.char.mod('%.2f', bk), np.array([','], dtype='U')),
        np.char.add(np.char.mod('%.2f', bkd), np.array([','], dtype='U')))
    keystr = np.char.add(keystr, np.char.mod('%.2f', bka))
    bucket_map = defaultdict(list)
    for i, k in enumerate(keystr):
        bucket_map[k].append(i)
    _CACHE = dict(CH=CH, CD=CD, CA=CA, meta=meta, n=len(rows), bucket_map=bucket_map,
                  load_sec=round(time.time() - t0, 2))
    return _CACHE


# ---------- 工具 ----------
def devig(h, d, a):
    inv = 1.0 / h + 1.0 / d + 1.0 / a
    return (1.0 / h) / inv, (1.0 / d) / inv, (1.0 / a) / inv


def bucket_key_str(h, d, a):
    r = lambda x: round(round(x / TOL) * TOL, 2)
    return f"{r(h):.2f},{r(d):.2f},{r(a):.2f}"


def pct(x):
    return f"{x * 100:.1f}%"


def favorite_side(h, d, a):
    m = min(h, d, a)
    return 'H' if m == h else ('A' if m == a else 'D')


def archetype(h, d, a):
    """赔率结构分类标签: 热门方向 + 抽水档位 + 形态简述。"""
    fav = favorite_side(h, d, a)
    fav_cn = {'H': '主胜', 'D': '平局', 'A': '客胜'}[fav]
    inv = 1.0 / h + 1.0 / d + 1.0 / a
    over = inv - 1.0
    if over < 0.06:
        tier = '低水(<6%)'
    elif over < 0.10:
        tier = '中水(6-10%)'
    else:
        tier = '高水(>10%)'
    # 形态: 三方离散度
    spread = max(h, d, a) - min(h, d, a)
    if spread < 1.2:
        shape = '三方胶着'
    elif (max(h, d, a) - min(h, d, a)) / min(h, d, a) > 2.5:
        shape = '极度一边倒'
    else:
        shape = '常规结构'
    return f"{fav_cn}热门·{tier}·{shape}｜抽水{over*100:.1f}%"


# ---------- 实证聚合 ----------
def bucket_profile(meta_subset, th, td, ta):
    """对一组历史场次聚合: 赛果频率/隐含/ROI/校准/热门命中/分胜负。"""
    n = len(meta_subset)
    if n == 0:
        return None
    cnt = Counter(m['outcome'] for m in meta_subset)
    H, D, A = cnt.get('H', 0), cnt.get('D', 0), cnt.get('A', 0)

    def roi(side_idx, res_char):
        tot = 0.0
        for m in meta_subset:
            odds = (m['ch'], m['cd'], m['ca'])[side_idx]
            tot += (odds - 1.0) if m['outcome'] == res_char else -1.0
        return tot / n

    roi_h, roi_d, roi_a = roi(0, 'H'), roi(1, 'D'), roi(2, 'A')
    ph, pd, pa = devig(th, td, ta)
    fav = favorite_side(th, td, ta)
    fav_hit = sum(1 for m in meta_subset if m['outcome'] == fav) / n
    return dict(
        n=n, H=H, D=D, A=A,
        pH=H / n, pD=D / n, pA=A / n,
        imp_h=ph, imp_d=pd, imp_a=pa,
        roi_h=roi_h, roi_d=roi_d, roi_a=roi_a,
        cal_h=H / n - ph, cal_d=D / n - pd, cal_a=A / n - pa,
        fav=fav, fav_hit=fav_hit,
        over=(H + A) / n,
        arch=archetype(th, td, ta),
    )


def nearest_indices(h, d, a, k):
    c = load_all()
    d2 = (c['CH'] - h) ** 2 + (c['CD'] - d) ** 2 + (c['CA'] - a) ** 2
    return np.argpartition(d2, k)[:k]


# ---------- 单目标归类 ----------
def classify(h, d, a, k=40, min_sample=20):
    c = load_all()
    key = bucket_key_str(h, d, a)
    exact_idx = c['bucket_map'].get(key, [])
    near_idx = nearest_indices(h, d, a, k)
    exact_meta = [c['meta'][i] for i in exact_idx]
    near_meta = [c['meta'][i] for i in near_idx]
    agg_ex = bucket_profile(exact_meta, h, d, a) if len(exact_meta) else None
    agg_ne = bucket_profile(near_meta, h, d, a)
    if agg_ex and agg_ex['n'] >= min_sample:
        primary, mode = agg_ex, 'exact'
        primary_idx = exact_idx
    else:
        primary, mode = agg_ne, 'nearest_fallback'
        primary_idx = list(near_idx)
    # 明细: 精确桶全列(若足够)否则最近邻 TOP-20
    if agg_ex and agg_ex['n'] >= min_sample:
        detail = sorted(exact_meta, key=lambda m: abs(m['ch'] - h) + abs(m['cd'] - d) + abs(m['ca'] - a))[:20]
    else:
        # 最近邻按距离排序取 TOP-20
        d2 = (np.array([m['ch'] for m in near_meta]) - h) ** 2 + \
             (np.array([m['cd'] for m in near_meta]) - d) ** 2 + \
             (np.array([m['ca'] for m in near_meta]) - a) ** 2
        order = np.argsort(d2)[:20]
        detail = [near_meta[i] for i in order]
    return dict(
        target=(h, d, a), tol=TOL, key=key,
        exact_n=len(exact_meta), near_n=len(near_meta),
        agg_ex=agg_ex, agg_ne=agg_ne, primary=primary, mode=mode,
        detail=detail,
    )


# ---------- 全量分类 ----------
def build_taxonomy(min_sample=20):
    """扫描全量, 把每类赔率结构建成一个分类(bucket), 计算历史画像。"""
    c = load_all()
    out = []
    for key, idxs in c['bucket_map'].items():
        if len(idxs) < min_sample:
            continue
        meta_sub = [c['meta'][i] for i in idxs]
        # bucket 代表线 = 该桶网格中心(四舍五入值)
        h_s, d_s, a_s = (float(x) for x in key.split(','))
        prof = bucket_profile(meta_sub, h_s, d_s, a_s)
        if prof is None:
            continue
        out.append(dict(key=key, line=(h_s, d_s, a_s), **prof))
    out.sort(key=lambda x: -x['n'])
    return dict(
        total_matches=c['n'],
        distinct_buckets=len(c['bucket_map']),
        classified_buckets=len(out),
        min_sample=min_sample,
        load_sec=c['load_sec'],
        buckets=out,
    )


# ---------- 报告 ----------
def _block_table(title, agg, mode_tag):
    if agg is None:
        return f"<h3>{title}</h3><p style='color:#f55'>n=0 无历史同结构</p>"
    db_h = agg['pH'] - agg['imp_h']
    return f"""<h3>{title}（{mode_tag}）</h3>
<table border=1 cellpadding=6 cellspacing=0 style='border-collapse:collapse;font-size:14px'>
<tr><th>指标</th><th>主胜 H</th><th>平 D</th><th>客胜 A</th></tr>
<tr><td>样本数 / 频率</td><td colspan=3>n={agg['n']} ｜ 分胜负 {pct(agg['over'])}</td></tr>
<tr><td>实际赛果频率</td><td>{pct(agg['pH'])}</td><td>{pct(agg['pD'])}</td><td>{pct(agg['pA'])}</td></tr>
<tr><td>庄家隐含概率</td><td>{pct(agg['imp_h'])}</td><td>{pct(agg['imp_d'])}</td><td>{pct(agg['imp_a'])}</td></tr>
<tr><td>ROI(每场自身收盘赔率下注1单位)</td><td>{agg['roi_h']:+.3f}</td><td>{agg['roi_d']:+.3f}</td><td>{agg['roi_a']:+.3f}</td></tr>
<tr><td>校准偏差(实际-隐含)</td><td>{db_h:+.3f}</td><td>{agg['pD']-agg['imp_d']:+.3f}</td><td>{agg['pA']-agg['imp_a']:+.3f}</td></tr>
<tr><td>热门(argmax)命中</td><td colspan=3>{agg['fav']} = {pct(agg['fav_hit'])}</td></tr>
</table>"""


def classify_html(r):
    h, d, a = r['target']
    det = r['detail']
    rows = "".join(
        f"<tr><td>{html.escape(str(m['date'])[:10])}</td>"
        f"<td>{html.escape(str(m['home']))} vs {html.escape(str(m['away']))}</td>"
        f"<td>{html.escape(str(m['league']))}</td>"
        f"<td>{m['ch']:.2f},{m['cd']:.2f},{m['ca']:.2f}</td>"
        f"<td>{int(m['hs'])}-{int(m['aw'])} ({m['outcome']})</td></tr>"
        for m in det
    )
    arch = r['primary']['arch'] if r['primary'] else '—'
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>同赔率结构历史场次分析</title></head>
<body style='font-family:Segoe UI,Microsoft YaHei;background:#0f1115;color:#e6e6e6;padding:24px'>
<h2>同赔率结构历史场次分析</h2>
<p>目标1X2收盘赔率: <b>主 {h} / 平 {d} / 客 {a}</b> ｜ 分桶网格 tol={r['tol']} ｜ 模式: {r['mode']}</p>
<p style='color:#9ab'>结构分类标签: <b>{html.escape(arch)}</b></p>
{_block_table('① 精确分桶（同赔率结构, n='+str(r['exact_n'])+'）', r['agg_ex'], 'exact')}
{_block_table('② 最近邻 TOP-'+str(r['near_n'])+'（'+r['mode']+'）', r['agg_ne'], r['mode'])}
<h3>③ 最近邻明细 (TOP {len(det)})</h3>
<table border=1 cellpadding=5 cellspacing=0 style='border-collapse:collapse;font-size:13px'>
<tr><th>日期</th><th>对阵</th><th>联赛</th><th>收盘1X2</th><th>实际比分(赛果)</th></tr>
{rows}</table>
<p style='color:#888;font-size:12px'>数据源: football_data.historical_matches (31.2万场, 收盘1X2+赛果)。ROI为描述性历史统计, 非未来收益保证。单庄是否含edge须逐场判定。</p>
</body></html>"""


def taxonomy_html(taxo, embed_min=20):
    """独立可交互分类浏览器: 概要 + TOP桶表 + 内嵌可查 widget(精确桶查, n>=embed_min)。"""
    b = taxo['buckets']
    embed = [x for x in b if x['n'] >= embed_min]
    embed_js = json.dumps(
        [dict(k=x['key'], n=x['n'], pH=round(x['pH'], 4), pD=round(x['pD'], 4),
              pA=round(x['pA'], 4), roi_h=round(x['roi_h'], 3), roi_d=round(x['roi_d'], 3),
              roi_a=round(x['roi_a'], 3), arch=x['arch']) for x in embed],
        ensure_ascii=False)
    top_rows = "".join(
        f"<tr><td>{x['key']}</td><td>{x['n']}</td><td>{pct(x['pH'])}</td><td>{pct(x['pD'])}</td>"
        f"<td>{pct(x['pA'])}</td><td>{x['roi_h']:+.3f}</td><td>{x['roi_d']:+.3f}</td>"
        f"<td>{x['roi_a']:+.3f}</td><td style='font-size:11px;color:#9ab'>{html.escape(x['arch'])}</td></tr>"
        for x in b[:200])
    return f"""<!doctype html><html><head><meta charset=utf-8'>
<title>赔率结构分类 · 全量浏览器</title></head>
<body style='font-family:Segoe UI,Microsoft YaHei;background:#0f1115;color:#e6e6e6;padding:24px'>
<h2>赔率结构分类 · 全量浏览器</h2>
<p style='color:#9ab'>数据源: football_data.historical_matches ｜ 总场次 {taxo['total_matches']:,} ｜
去重结构桶 {taxo['distinct_buckets']:,} ｜ 纳入分类(n>={taxo['min_sample']}) {taxo['classified_buckets']:,} ｜
加载 {taxo['load_sec']}s</p>
<h3>① 交互: 输入目标收盘赔率 → 查该结构历史画像</h3>
<div style='margin:8px 0'>
主胜 <input id=h style='width:60px' value='2.22'> 平 <input id=d style='width:60px' value='3.20'>
客胜 <input id=a style='width:60px' value='2.94'> <button onclick='lookup()'>查结构</button>
</div>
<div id=res style='font-size:14px;margin:8px 0 16px;min-height:20px'></div>
<h3>② TOP 结构桶 (按样本量, 前200)</h3>
<table border=1 cellpadding=5 cellspacing=0 style='border-collapse:collapse;font-size:13px'>
<tr><th>结构桶(主,平,客)</th><th>n</th><th>主胜%</th><th>平%</th><th>客胜%</th><th>ROI主</th><th>ROI平</th><th>ROI客</th><th>结构分类标签</th></tr>
{top_rows}</table>
<p style='color:#888;font-size:12px'>说明: 内嵌可查为 n>={embed_min} 的结构(共 {len(embed):,}); 更小样本结构未内嵌(精确桶查会提示改用 Python 工具跑 nearest)。
ROI为描述性历史统计, 非未来收益保证。</p>
<script>
const TAXO={embed_js};
function bk(x){{const r=0.05;return Math.round(Math.round(x/r)*r*100)/100;}}
function lookup(){{
  const h=+document.getElementById('h').value,d=+document.getElementById('d').value,a=+document.getElementById('a').value;
  const key=bk(h).toFixed(2)+','+bk(d).toFixed(2)+','+bk(a).toFixed(2);
  const x=TAXO.find(o=>o.k===key);
  const res=document.getElementById('res');
  if(!x){{res.innerHTML='<span style="color:#f55">无精确同结构样本(n>={embed_min})。请用 odds_structure_classifier.py --h '+h+' --d '+d+' --a '+a+' 跑最近邻模式。</span>';return;}}
  res.innerHTML='<b>结构桶 '+x.k+'</b> ｜ n='+x.n+' ｜ 主/平/客 '+ (x.pH*100).toFixed(1)+'% / '+(x.pD*100).toFixed(1)+'% / '+(x.pA*100).toFixed(1)+
    '% ｜ ROI '+x.roi_h.toFixed(3)+' / '+x.roi_d.toFixed(3)+' / '+x.roi_a.toFixed(3)+
    '<br><span style="color:#9ab;font-size:12px">'+x.arch+'</span>';
}}
</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description='同赔率结构历史场次分析 · 全量分类器')
    ap.add_argument('--h', type=float)
    ap.add_argument('--d', type=float)
    ap.add_argument('--a', type=float)
    ap.add_argument('--tol', type=float, default=TOL)
    ap.add_argument('--k', type=int, default=40)
    ap.add_argument('--min-sample', type=int, default=20)
    ap.add_argument('--build-taxonomy', action='store_true', help='扫描全量, 建立赔率结构分类')
    ap.add_argument('--embed-min', type=int, default=20, help='taxonomy.html 内嵌可查的最小样本')
    ap.add_argument('--html', default=None)
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    if args.build_taxonomy:
        print(f"[扫描全量] 加载并分桶中 (tol={args.tol}) ...")
        taxo = build_taxonomy(min_sample=args.min_sample)
        print(f"  总场次={taxo['total_matches']:,} 去重桶={taxo['distinct_buckets']:,} "
              f"纳入分类={taxo['classified_buckets']:,} 加载{taxo['load_sec']}s")
        if args.json:
            with open(args.json, 'w', encoding='utf-8') as f:
                json.dump(taxo, f, ensure_ascii=False, indent=2, default=str)
            print(f"[JSON] {args.json}")
        if args.html:
            with open(args.html, 'w', encoding='utf-8') as f:
                f.write(taxonomy_html(taxo, embed_min=args.embed_min))
            print(f"[HTML] {args.html}")
        return

    if not (args.h and args.d and args.a):
        print("错误: 单场归类需要 --h --d --a"); return
    r = classify(args.h, args.d, args.a, args.k, args.min_sample)
    print(f"=== 同赔率结构历史分析: 主{args.h}/平{args.d}/客{args.a} (tol={r['tol']}) ===")
    print(f"结构分类标签: {r['primary']['arch'] if r['primary'] else '—'}")
    print(f"精确分桶 n={r['exact_n']} ｜ 最近邻 n={r['near_n']} ｜ 模式: {r['mode']}")
    if r['agg_ex']:
        e = r['agg_ex']
        print(f"[精确] 赛果 H{pct(e['pH'])}/D{pct(e['pD'])}/A{pct(e['pA'])}  ROI H{e['roi_h']:+.3f}/D{e['roi_d']:+.3f}/A{e['roi_a']:+.3f}  校准 H{e['cal_h']:+.3f}/D{e['cal_d']:+.3f}/A{e['cal_a']:+.3f}")
    if r['agg_ne']:
        n = r['agg_ne']
        print(f"[最近邻] 赛果 H{pct(n['pH'])}/D{pct(n['pD'])}/A{pct(n['pA'])}  ROI H{n['roi_h']:+.3f}/D{n['roi_d']:+.3f}/A{n['roi_a']:+.3f}")
    if args.html:
        with open(args.html, 'w', encoding='utf-8') as f:
            f.write(classify_html(r))
        print(f"[HTML] {args.html}")
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(r, f, ensure_ascii=False, indent=2, default=str)
        print(f"[JSON] {args.json}")


if __name__ == '__main__':
    main()
