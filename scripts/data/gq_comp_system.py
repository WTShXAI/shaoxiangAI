#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gq_comp_system.py — GQ实时盘口 → 历史同赔率结构 批量检索系统
====================================================================
⚠ 2026-08-07 起已被 historical_comp_system.py 取代:
   GQ 采集量不足, 新系统改为【只对接历史库 + 模型分析】,
   数据源=football_data.odds_features(312K场, 模型原生特征+赛果), 完全不依赖 GQ。
   本文件保留为 GQ 实时拉取的历史实现(按需仍可 --source gq 使用)。
====================================================================
把"单场查询"升级为"整盘扫描武器":
  1. 拉 GQ 所有带1X2盘口的实时比赛(最新赔率)
  2. 对每场跑 odds_comp_finder.analyze (精确分桶 + 最近邻, SSoT复用)
  3. 计算校准偏差(实际赛果频率 - 庄家去水隐含), 找历史同结构下被庄家错估的方向
  4. 出 consolidated 报告: 文本摘要(按信号强度排序) + HTML + JSON

历史库 football_data.historical_matches (31.2万场, 真实正赛) 为唯一对照源。
野鸡盘(后备/预备/梦幻/友谊/U20/U23/女/B队)默认排除, 因其历史库无对应层级。

用法:
  python gq_comp_system.py                         # 默认: 非野鸡盘, 精确优先, 最近邻兜底
  python gq_comp_system.py --include-obscure       # 含野鸡盘
  python gq_comp_system.py --league 巴西杯 --html report.html --json report.json
  python gq_comp_system.py --min-sample 8 --top 60
"""
import sqlite3, argparse, html, json, sys
import numpy as np
from odds_comp_finder import aggregate, devig

GQ = 'D:/Architecture/data/events.db'
DB = 'D:/Architecture/data/football_data.db'
OBSCURE_KW = ['后备', '预备', '梦幻', '友谊', 'U20', 'U23', '女', 'B队']

# lazy import drift adapter to avoid heavy model load when not used
_gq_drift_adapter = None


def _get_drift_adapter():
    global _gq_drift_adapter
    if _gq_drift_adapter is None:
        import gq_drift_adapter
        _gq_drift_adapter = gq_drift_adapter
    return _gq_drift_adapter

# 历史库预加载(numpy向量化, 批量扫描323场→秒级)
_HIST = None


def load_hist(tol):
    global _HIST
    if _HIST is None:
        con = sqlite3.connect(DB); cur = con.cursor()
        cur.execute("""SELECT close_home_odds,close_draw_odds,close_away_odds,match_date,
                              home_team,away_team,home_score,away_score,final_result,league_name,total_goals
                       FROM historical_matches WHERE close_home_odds>0""")
        rows = cur.fetchall(); con.close()
        H = np.array([r[0] for r in rows], dtype=float)
        D = np.array([r[1] for r in rows], dtype=float)
        A = np.array([r[2] for r in rows], dtype=float)
        _HIST = dict(rows=rows, H=H, D=D, A=A)
    H, D, A = _HIST['H'], _HIST['D'], _HIST['A']
    BH = np.round(np.round(H / tol) * tol, 2)
    BD = np.round(np.round(D / tol) * tol, 2)
    BA = np.round(np.round(A / tol) * tol, 2)
    return _HIST['rows'], H, D, A, BH, BD, BA


def gq_live_1x2(only_non_obscure=True, league_sub=None, mode='live', max_matches=400):
    """拉 GQ 带 1X2 盘口的比赛。

    mode:
      'live'      -> 只扫 status='live'
      'scheduled' -> 只扫 status 含 scheduled/upcoming/pre/not_started
      'all'       -> 全部(含 finished, 慢)
    """
    con = sqlite3.connect(GQ); cur = con.cursor()
    cur.execute("SELECT match_key,selection,odds FROM odds_snapshots WHERE market='1X2' ORDER BY match_key,captured_at DESC")
    latest = {}
    for mk, sel, o in cur.fetchall():
        d = latest.setdefault(mk, {})
        if sel not in d:
            d[sel] = o
    cur.execute("SELECT match_key,home,away,league,status,score_home,score_away,minute FROM matches")
    meta = {r[0]: r[1:] for r in cur.fetchall()}
    con.close()
    out = []
    for mk, d in latest.items():
        if not (d.get('home') and d.get('draw') and d.get('away')):
            continue
        home, away, lg, st, sh, sa, minute = meta.get(mk, (None,) * 7)
        if home is None:
            continue
        st_l = (st or '').lower()
        if mode == 'live':
            if st != 'live':
                continue
        elif mode == 'scheduled':
            if not any(x in st_l for x in ('sched', 'upcom', 'pre', 'not_started')):
                continue
        # mode=='all' 不限制
        if only_non_obscure and any(k in (lg or '') for k in OBSCURE_KW):
            continue
        if league_sub and league_sub not in (lg or ''):
            continue
        out.append(dict(match_key=mk, home=home, away=away, league=lg, status=st,
                        score=f"{sh}-{sa}" if sh is not None else "-", minute=minute,
                        h=d['home'], d=d['draw'], a=d['away']))
    out.sort(key=lambda m: (m['league'] or '', m['home'] or ''))
    return out[:max_matches]


def classify_signal(agg):
    if agg is None:
        return None
    devs = {'H': agg['pH'] - agg['imp_h'],
            'D': agg['pD'] - agg['imp_d'],
            'A': agg['pA'] - agg['imp_a']}
    return max(devs.items(), key=lambda kv: abs(kv[1]))


def run(matches, tol, k, min_sample, with_drift=False):
    rows, H, D, A, BH, BD, BA = load_hist(tol)
    results = []
    drift_adapter = _get_drift_adapter() if with_drift else None
    engine = drift_adapter and drift_adapter.ReverseOddsEngine()
    for m in matches:
        th, td, ta = m['h'], m['d'], m['a']
        bth = round(round(th / tol) * tol, 2)
        btd = round(round(td / tol) * tol, 2)
        bta = round(round(ta / tol) * tol, 2)
        emask = (BH == bth) & (BD == btd) & (BA == bta)
        exact_idx = np.nonzero(emask)[0]
        exact = [rows[i] for i in exact_idx]
        dist2 = (H - th) ** 2 + (D - td) ** 2 + (A - ta) ** 2
        near_idx = np.argpartition(dist2, k)[:k]
        near = [rows[i] for i in near_idx]
        agg_ex = aggregate(exact, th, td, ta) if exact else None
        agg_ne = aggregate(near, th, td, ta)
        if agg_ex and agg_ex['n'] >= min_sample:
            primary, mode = agg_ex, 'exact'
        else:
            primary, mode = agg_ne, 'nearest_fallback'
        rec = dict(m=m, agg_ex=agg_ex, agg_ne=agg_ne,
                   primary=primary, mode=mode, signal=classify_signal(primary))
        if with_drift:
            try:
                d = drift_adapter.analyze_gq_match(m['match_key'], engine=engine)
                rec['drift'] = d
            except Exception as e:
                rec['drift'] = {'error': str(e)}
        results.append(rec)
    results.sort(key=lambda r: abs(r['signal'][1]) if r['signal'] else 0, reverse=True)
    return results


def pct(x):
    return f"{x*100:.1f}%"


_INTENT_ZH = {
    'honest_defH': '诚防H', 'honest_defA': '诚防A',
    'fake_defH': '诱H', 'fake_defA': '诱A',
    'all_down': '全降', 'all_up': '全升',
    'balance_action': '平衡', 'neutral': '无',
}


def _drift_summary(r):
    d = r.get('drift')
    if not d or 'error' in d:
        return ''
    it = d.get('intent', {})
    pat = it.get('pattern', '')
    lbl = it.get('label', '')
    zh = _INTENT_ZH.get(lbl, lbl[:6])
    ah = d.get('ah') or {}
    ou = d.get('ou') or {}
    ah_str = f"AH{ah.get('line')} {ah.get('close_odds', {})}" if ah.get('line') is not None else ''
    ou_str = f"OU{ou.get('line')} {ou.get('close_odds', {})}" if ou.get('line') is not None else ''
    return f"| drift {pat}({zh}) {ah_str} {ou_str}"


def text_report(results):
    L = []
    has_drift = any('drift' in r for r in results)
    L.append(f"=== GQ实时盘口 × 历史同赔率结构 信号扫描 ({len(results)} 场) ===")
    L.append(f"{'信号强度':>8} | {'比赛':<34} | 1X2 | 精确n | 主/平/客实际 | 最大校准偏差方向" + (" | drift摘要" if has_drift else ""))
    L.append("-" * (150 if has_drift else 110))
    for r in results:
        m = r['m']; p = r['primary']; sig = r['signal']
        if p is None:
            L.append(f"{'--':>8} | {m['home']} vs {m['away']} [{m['league']}] | "
                     f"{m['h']:.2f}/{m['d']:.2f}/{m['a']:.2f} | n=0 | 无历史同盘")
            continue
        siden = {'H': '主胜', 'D': '平局', 'A': '客胜'}[sig[0]]
        base = (f"{abs(sig[1]):+.3f} | {m['home'][:14]} vs {m['away'][:14]} [{m['league'][:8]}] | "
                f"{m['h']:.2f}/{m['d']:.2f}/{m['a']:.2f} | {p['n']:>3}({r['mode'][:5]}) | "
                f"{pct(p['pH'])}/{pct(p['pD'])}/{pct(p['pA'])} | {siden}{sig[1]:+.3f}")
        if has_drift:
            base += " " + _drift_summary(r)
        L.append(base)
    L.append("-" * (150 if has_drift else 110))
    L.append("说明: 校准偏差=实际赛果频率-庄家去水隐含概率。正偏差=该方向在历史同结构中'跑赢庄家预期'。")
    if has_drift:
        L.append("drift摘要: ↑/↓=赔率相对开盘涨跌; AH/OULine=当前主线。单庄drift非陷阱, 仅作盘口动向参考。")
    L.append("⚠ 注意: 单庄drift仅作盘口动向参考; 跨庄分歧是更强edge信号, 但单庄是否含edge须逐场用开盘去水P/临场漂移/联赛方差判定, 不预设单庄无edge。本系统为单庄对照。")
    return "\n".join(L)


def _drift_html(r):
    d = r.get('drift')
    if not d or 'error' in d:
        return "<td>-</td>"
    it = d.get('intent', {})
    ah = d.get('ah') or {}
    ou = d.get('ou') or {}
    ah_s = f"AH{ah.get('line')} {ah.get('close_odds', {})}" if ah.get('line') is not None else '-'
    ou_s = f"OU{ou.get('line')} {ou.get('close_odds', {})}" if ou.get('line') is not None else '-'
    return f"<td>{it.get('pattern','')} {it.get('label','')}<br>{ah_s}<br>{ou_s}</td>"


def html_report(results):
    has_drift = any('drift' in r for r in results)
    def cell(agg, mode):
        if agg is None:
            return "<td colspan=6 style='color:#f55'>无历史同盘 (n=0)</td>"
        return (f"<td>n={agg['n']}<br>({mode})</td>"
                f"<td>主 {pct(agg['pH'])}<br>平 {pct(agg['pD'])}<br>客 {pct(agg['pA'])}</td>"
                f"<td>主 {pct(agg['imp_h'])}<br>平 {pct(agg['imp_d'])}<br>客 {pct(agg['imp_a'])}</td>"
                f"<td>主 {agg['roi_h']:+.3f}<br>平 {agg['roi_d']:+.3f}<br>客 {agg['roi_a']:+.3f}</td>"
                f"<td>H {agg['pH']-agg['imp_h']:+.3f}<br>D {agg['pD']-agg['imp_d']:+.3f}<br>A {agg['pA']-agg['imp_a']:+.3f}</td>")
    rows_html = ""
    for r in results:
        m = r['m']; p = r['primary']; sig = r['signal']
        siden = {'H': '主胜', 'D': '平局', 'A': '客胜'}[sig[0]] if sig else '-'
        color = '#1c5e2a' if (sig and sig[1] > 0) else ('#5e1c1c' if (sig and sig[1] < 0) else '#333')
        drift_td = _drift_html(r) if has_drift else ''
        rows_html += f"""<tr style='background:{color}'>
          <td><b>{html.escape(m['home'])}</b> vs <b>{html.escape(m['away'])}</b><br>
              <span style='color:#9ab'>{html.escape(str(m['league']))} | {m['status']} {m['minute']}' | 比分{m['score']}</span></td>
          <td>{m['h']:.2f}<br>{m['d']:.2f}<br>{m['a']:.2f}</td>
          {cell(p, r['mode'])}
          <td><b>{siden} {sig[1]:+.3f}</b></td>{drift_td}</tr>"""
    drift_th = "<th>盘口漂移<br>(P路/亚欧)</th>" if has_drift else ''
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>GQ盘口×历史同赔率结构扫描</title></head>
<body style='font-family:Segoe UI,Microsoft YaHei;background:#0f1115;color:#e6e6e6;padding:24px'>
<h2>GQ实时盘口 × 历史同赔率结构 信号扫描</h2>
<p style='color:#9ab'>数据源: events.db(实时1X2+AH/OU快照) × football_data.historical_matches(31.2万场真实正赛)。按校准偏差强度排序。</p>
<table border=1 cellpadding=6 cellspacing=0 style='border-collapse:collapse;font-size:13px'>
<tr><th>比赛</th><th>实时1X2<br>(主/平/客)</th><th>样本n<br>(模式)</th><th>实际赛果频率</th><th>庄家隐含</th><th>各方向ROI</th><th>校准偏差<br>(实际-隐含)</th><th>最强信号</th>{drift_th}</tr>
{rows_html}</table>
<p style='color:#888;font-size:12px'>⚠ 单庄drift仅作盘口动向参考; 跨庄分歧是更强edge信号, 单庄是否含edge须逐场判定, 不预设。本系统为单庄对照。ROI为描述性历史统计。</p>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tol', type=float, default=0.05)
    ap.add_argument('--k', type=int, default=40)
    ap.add_argument('--min-sample', type=int, default=5)
    ap.add_argument('--include-obscure', action='store_true')
    ap.add_argument('--all', action='store_true', help='扫GQ全部有1X2的比赛(含finished, 慢)')
    ap.add_argument('--scheduled', action='store_true', help='扫 scheduled/upcoming 比赛(更适合看P路初→临)')
    ap.add_argument('--max', type=int, default=400, help='最大扫描场数')
    ap.add_argument('--league', default=None)
    ap.add_argument('--html', default=None)
    ap.add_argument('--json', default=None)
    ap.add_argument('--drift', action='store_true', help='叠加 GQ 盘口漂移/亚欧对比分析(会加载 ReverseOddsEngine)')
    args = ap.parse_args()

    mode = 'all' if args.all else ('scheduled' if args.scheduled else 'live')
    matches = gq_live_1x2(only_non_obscure=not args.include_obscure,
                          league_sub=args.league, mode=mode,
                          max_matches=args.max)
    if not matches:
        print("无符合条件的GQ实时盘口。"); return
    results = run(matches, args.tol, args.k, args.min_sample, with_drift=args.drift)
    print(text_report(results))
    if args.html:
        with open(args.html, 'w', encoding='utf-8') as f:
            f.write(html_report(results))
        print(f"\n[HTML] {args.html}")
    if args.json:
        out = []
        for r in results:
            item = {'mode': r['mode'], 'match': r['m'],
                    'primary': r['primary'], 'signal': r['signal']}
            if 'drift' in r:
                item['drift'] = r['drift']
            out.append(item)
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"[JSON] {args.json}")


if __name__ == '__main__':
    main()
