#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
live_structure_cruiser.py — 滚球赔率结构·自主巡航扫描
=====================================================
基于已验证的滚球赔率结构分类(1X2 + OU全场, 见 live_1x2_taxonomy.json / live_ou_taxonomy.json),
扫描 events.db 中当前在滚球的比赛, 把每场的"实时赔率结构"归类到历史画像, 输出巡航卡片。

诚实边界(IR-30):
  - 仅用 1X2(最终赛果) 与 OU全场(最终总进球) 两个已验证可靠分类; OU_1H/2H 因 ht_score 污染排除。
  - 历史 ROI/校准是"同赔率结构过往表现"的描述性统计, 非未来收益保证。
  - 价值旗标须满足: 样本充足(n>=MIN_N) + ROI 方向与校准方向一致(实际>隐含=被低定价的价值)。
  - 热门偏差: 主胜/客胜高 ROI 桶常是"热门被低定价"的市场异象, 非稳定 edge, 旗标时标注。
  - 不做任何建仓/推荐动作, 仅呈现结构画像 + 风险标注。

用法:
  python live_structure_cruiser.py --window 60 --limit 300 --min-n 50 --html live_cruise_report.html --json live_cruise_report.json
  python live_structure_cruiser.py --match-key "主 vs 客"   # 单场归类(诊断用)
"""
import sqlite3, json, argparse, time, html
from datetime import datetime

DB = 'D:/Architecture/data/events.db'
TOL = 0.05
TAX_1X2 = 'live_1x2_taxonomy.json'
TAX_OU = 'live_ou_taxonomy.json'

MIN_N = 50          # 价值旗标的最小样本
VALUE_ROI = 0.05    # ROI 阈值
VALUE_CAL = 0.02    # 校准阈值(实际-隐含 正=被低定价)


def _rnd(x):
    return round(round(x / TOL) * TOL, 2)


class Cruiser:
    def __init__(self, tax1_path=TAX_1X2, taxo_path=TAX_OU):
        self.t1 = json.load(open(tax1_path, encoding='utf-8'))
        self.to = json.load(open(taxo_path, encoding='utf-8'))
        self.d1 = {b['key']: b for b in self.t1['buckets']}
        self.do = {b['key']: b for b in self.to['buckets']}

    # ---------- 归类 ----------
    def lookup_1x2(self, h, d, a):
        key = f"{_rnd(h):.2f},{_rnd(d):.2f},{_rnd(a):.2f}"
        if key in self.d1:
            return self.d1[key], 'exact'
        best, bd = None, 1e9
        for b in self.d1.values():
            bh, bd2, ba = b['line']
            dist = (bh - h) ** 2 + (bd2 - d) ** 2 + (ba - a) ** 2
            if dist < bd:
                bd, best = dist, b
        return best, 'nearest'

    def lookup_ou(self, line, odds, sel):
        key = f"{_rnd(line):.2f}|{sel}|{_rnd(odds):.2f}"
        if key in self.do:
            return self.do[key], 'exact'
        best, bd = None, 1e9
        for b in self.do.values():
            dist = (b['line'] - line) ** 2 * 4 + (b['odds'] - odds) ** 2 + (0 if b['sel'] == sel else 1)
            if dist < bd:
                bd, best = dist, b
        return best, 'nearest'

    # ---------- 扫描当前滚球 ----------
    def scan(self, window_min=60, limit=300, min_n=MIN_N):
        con = sqlite3.connect(DB)
        con.execute("PRAGMA busy_timeout=30000")
        cur = con.cursor()
        cutoff = time.time() - window_min * 60
        mks = [r[0] for r in cur.execute(
            "SELECT DISTINCT match_key FROM odds_snapshots WHERE market LIKE '1X2%' AND captured_at>=? LIMIT ?",
            (cutoff, limit)).fetchall()]
        out = []
        for mk in mks:
            # 最新 1X2 三元组: 按 round(captured_at,1) 分组(三选项常差 <0.1s), 取最新且三选项齐全的组
            rows = cur.execute(
                "SELECT selection, odds, round(captured_at,1) FROM odds_snapshots "
                "WHERE market LIKE '1X2%' AND match_key=? AND captured_at>=?",
                (mk, cutoff)).fetchall()
            groups = {}
            for sel, o, rt in rows:
                groups.setdefault(rt, {})[sel] = o
            h = d = a = None
            ts = None
            for rt in sorted(groups.keys(), reverse=True):
                g = groups[rt]
                if 'home' in g and 'draw' in g and 'away' in g:
                    h, d, a = g['home'], g['draw'], g['away']
                    ts = rt
                    break
            if h and d and a:
                b1, m1 = self.lookup_1x2(h, d, a)
                x1 = dict(h=round(h, 2), d=round(d, 2), a=round(a, 2), mode=m1, profile=b1)
            else:
                # 当前滚球 1X2 选项不全(obscure 联赛采集缺口), 跳过 1X2 归类但保留 OU
                x1 = dict(h=None, d=None, a=None, mode='incomplete', profile=None)
            rec = dict(match_key=mk, ts=ts, x1=x1)
            # 全场 OU: 取该场所有全场 OU 快照中样本最足的代表线(按 taxonomy 命中)
            ou_rows = cur.execute(
                "SELECT line, selection, odds FROM odds_snapshots "
                "WHERE market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%' "
                "AND match_key=? AND captured_at>=?", (mk, cutoff)).fetchall()
            ou_seen = {}
            for line, sel, odds in ou_rows:
                if sel not in ('over', 'under'):
                    continue
                key = f"{_rnd(line):.2f}|{sel}|{_rnd(odds):.2f}"
                ou_seen.setdefault(key, (line, sel, odds))
            ou_profiles = []
            for key, (line, sel, odds) in ou_seen.items():
                b, m = self.lookup_ou(line, odds, sel)
                if b and b['n'] >= min_n:
                    ou_profiles.append(dict(line=round(line, 2), sel=sel, odds=round(odds, 2),
                                             mode=m, profile=b))
            rec['ou'] = ou_profiles
            out.append(rec)
        con.close()
        return self._rank(out)

    def _rank(self, recs):
        for r in recs:
            flags = []
            p = r['x1']['profile']
            if p and p['eff'] >= MIN_N:
                for side, roi, cal in (('主', p['roi_h'], p['cal_h']),
                                       ('平', p['roi_d'], p['cal_d']),
                                       ('客', p['roi_a'], p['cal_a'])):
                    if roi > VALUE_ROI and cal > VALUE_CAL:
                        flags.append(f"1X2·{side}历史价值(ROI{roi:+.3f}/校准{cal:+.3f})")
                    elif roi < -0.10:
                        flags.append(f"1X2·{side}历史高估(ROI{roi:+.3f})")
            # OU: 按 (line,sel) 去重, 仅留 ROI 绝对值最强的一条(避免同线多赔率噪声)
            ou_by_key = {}
            for o in r['ou']:
                k = (o['line'], o['sel'])
                prev = ou_by_key.get(k)
                if prev is None or abs(o['profile']['roi']) > abs(prev['profile']['roi']):
                    ou_by_key[k] = o
            for o in ou_by_key.values():
                p = o['profile']
                if p['roi'] > VALUE_ROI and p['cal'] > VALUE_CAL:
                    flags.append(f"OU{o['line']}·{o['sel']}历史价值(ROI{p['roi']:+.3f}/校准{p['cal']:+.3f})")
                elif p['roi'] < -0.10:
                    flags.append(f"OU{o['line']}·{o['sel']}历史高估(ROI{p['roi']:+.3f})")
            r['flags'] = flags
        # 按旗标数 + 最大 |ROI| 排序
        def score(r):
            best = 0.0
            if r['x1']['profile']:
                best = max(best, max(abs(r['x1']['profile'][k]) for k in ('roi_h', 'roi_d', 'roi_a')))
            for o in r['ou']:
                best = max(best, abs(o['profile']['roi']))
            return (len(r['flags']), best)
        recs.sort(key=score, reverse=True)
        return recs

    # ---------- 单场诊断 ----------
    def diagnose(self, match_key):
        con = sqlite3.connect(DB)
        con.execute("PRAGMA busy_timeout=30000")
        cur = con.cursor()
        t = cur.execute("SELECT MAX(captured_at) FROM odds_snapshots WHERE market LIKE '1X2%' AND match_key=?",
                        (match_key,)).fetchone()[0]
        rows = cur.execute(
            "SELECT selection, odds, round(captured_at,1) FROM odds_snapshots WHERE market LIKE '1X2%' AND match_key=? AND captured_at>=?",
            (match_key, t - 5)).fetchall()
        groups = {}
        for sel, o, rt in rows:
            groups.setdefault(rt, {})[sel] = o
        h = d = a = None
        for rt in sorted(groups.keys(), reverse=True):
            g = groups[rt]
            if 'home' in g and 'draw' in g and 'away' in g:
                h, d, a = g['home'], g['draw'], g['away']
                break
        con.close()
        if not (h and d and a):
            return None
        b1, m1 = self.lookup_1x2(h, d, a)
        return dict(match_key=match_key, x1=dict(h=h, d=d, a=a, mode=m1, profile=b1))


# ---------- 报告 ----------
def build_html(recs, window, min_n):
    cards = ""
    for r in recs:
        p = r['x1']['profile']
        x1line = (f"<div style='font-size:12px;color:#9ab'>滚球1X2 主{p['pH']*100:.1f}/平{p['pD']*100:.1f}/客{p['pA']*100:.1f} "
                  f"｜ ROI H{p['roi_h']:+.3f}/D{p['roi_d']:+.3f}/A{p['roi_a']:+.3f} ｜ 样本{p['eff']} ｜ 热门{p['fav']}{p['fav_hit']*100:.1f}%</div>"
                  if p else
                  f"<div style='font-size:12px;color:#c96'>⚠ 本场滚球1X2选项不全(采集缺口), 跳过1X2归类, 仅OU全场</div>")
        flags = "".join(f"<span style='background:#1d3a1d;color:#8f8;border:1px solid #4a4;padding:2px 6px;border-radius:6px;font-size:11px;margin:2px'>{html.escape(f)}</span>" for f in r['flags'])
        ou = ""
        for o in r['ou']:
            pp = o['profile']
            ou += f"<tr><td>OU{o['line']} {o['sel']}@{o['odds']}</td><td>{pp['freq']*100:.1f}%</td><td>{pp['roi']:+.3f}</td><td>{pp['cal']:+.3f}</td><td>{pp['n']}</td></tr>"
        ou_table = (f"<table border=1 cellpadding=4 cellspacing=0 style='border-collapse:collapse;font-size:12px;margin-top:6px;width:100%'>"
                    f"<tr><th>OU全场</th><th>打穿频率</th><th>ROI</th><th>校准</th><th>n</th></tr>{ou}</table>"
                    if ou else "<div style='color:#667;font-size:11px;margin-top:6px'>本场无 n>={min_n} 的OU全场结构桶</div>")
        cards += f"""
<div style='border:1px solid #2a2f3a;border-radius:10px;padding:12px;margin:10px 0;background:#15181f'>
<div style='font-size:15px;font-weight:700'>{html.escape(r['match_key'])} <span style='font-size:11px;color:#789'>(1X2结构 {r['x1']['mode']})</span></div>
{x1line}
{ou_table}
<div style='margin-top:6px'>{flags if flags else '<span style=\"color:#667;font-size:11px\">无显著历史价值/高估旗标</span>'}</div>
</div>"""
    return f"""<!doctype html><html><head><meta charset=utf-8><title>滚球结构巡航</title></head>
<body style='font-family:Segoe UI,Microsoft YaHei;background:#0f1115;color:#e6e6e6;padding:24px'>
<h2>滚球赔率结构 · 自主巡航扫描</h2>
<p style='color:#9ab'>数据源: events.db ｜ 窗口 {window}min ｜ 价值旗标阈值 n>={min_n}, ROI>{VALUE_ROI}, 校准>{VALUE_CAL} ｜ 仅 1X2+OU全场(已验证) ｜ 共 {len(recs)} 场在滚球</p>
<p style='color:#f99;font-size:12px'>⚠ ROI/校准为同结构历史描述性统计, 非未来收益保证; 旗标仅供结构研判, 不含建仓/推荐。热门高ROI常是市场异象非稳定edge。</p>
{cards}
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description='滚球赔率结构巡航扫描')
    ap.add_argument('--window', type=int, default=60)
    ap.add_argument('--limit', type=int, default=300)
    ap.add_argument('--min-n', type=int, default=MIN_N)
    ap.add_argument('--match-key', default=None)
    ap.add_argument('--html', default=None)
    ap.add_argument('--json', default=None)
    args = ap.parse_args()

    c = Cruiser()
    if args.match_key:
        d = c.diagnose(args.match_key)
        print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
        return
    recs = c.scan(window_min=args.window, limit=args.limit, min_n=args.min_n)
    print(f"[巡航] 在滚球比赛={len(recs)} 场 ｜ 带旗标={sum(1 for r in recs if r['flags'])}")
    if args.json:
        json.dump(dict(generated=datetime.now().isoformat(), window_min=args.window, min_n=args.min_n,
                       matches=recs), open(args.json, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=2, default=str)
        print(f"[JSON] {args.json}")
    if args.html:
        open(args.html, 'w', encoding='utf-8').write(build_html(recs, args.window, args.min_n))
        print(f"[HTML] {args.html}")


if __name__ == '__main__':
    main()
