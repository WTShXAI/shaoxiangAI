#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""预测校准评估 — 把系统主指标从 ROI 换成 LogLoss / Brier / 可靠性 (2026-09-18)
================================================================================
「博彩量化 → 预测系统」改造的评估半场。对系统里每一路**已定格的赛前/半场概率输出**,
对照真实赛果计算预测质量指标, 并与**市场去水隐含基准**同口径对照:

  覆盖判定源:
    A. candles_1x2        prematch_candles_verdict  (K线集成, 1X2 三向概率)
    B. knn_prematch       prematch_conclusion       (KNN, 只有方向 → 准确率口径)
    C. halftime           halftime_conclusion       (半场冻结 1X2+OU 概率)
    D. ht_model           ht_model_verdict          (HT锚模型, 样本尚小)
    E. daily_predictions  daily_predictions         (预测产品层, 前向积累)
    F. market_baseline    match_outcomes op_1x2_*   (市场去水隐含, 大样本效率基准)

  指标: 多分类 LogLoss (随机基线 ln3≈1.0986) / 多分类 Brier / TOP1 准确率 /
        分结果可靠性图数据 (ECE + 校准斜率, 复用 pipeline.calibration)
  口径: 只算已完赛且有完整概率的场; 市场基准与模型取**同一场集**对照 (A/E),
        另给 F 大样本参考。

输出: reports/prediction_calibration_report.json + .md + 控制台摘要
用法: python scripts/eval_prediction_calibration.py [--days 3650]
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, r'D:\Architecture')

from pipeline.calibration import reliability, ece, calibration_slope
from pipeline.score_model import deoverround
from pipeline.settle import result_1x2, settle_ou

EPS = 1e-15
REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'reports')


# ── 多分类指标 ────────────────────────────────────────────────────────────

def multi_metrics(rows):
    """rows = [(p_home, p_draw, p_away, actual)] → 指标 dict。actual ∈ home/draw/away。"""
    if not rows:
        return None
    n = len(rows)
    logloss = 0.0
    brier = 0.0
    hits = 0
    per = {'home': [], 'draw': [], 'away': []}
    for ph, pd, pa, act in rows:
        ps = {'home': ph, 'draw': pd, 'away': pa}
        s = ps['home'] + ps['draw'] + ps['away']
        if s <= 0 or s > 1.5:
            continue
        ps = {k: v / s for k, v in ps.items()}  # 容忍 ~1% 的和偏差, 重归一
        pred = max(ps, key=ps.get)
        hits += (pred == act)
        logloss += -math.log(max(ps[act], EPS))
        brier += sum((ps[k] - (1.0 if k == act else 0.0)) ** 2 for k in ps)
        for k in ps:
            per[k].append((ps[k], 1 if act == k else 0))
    n = len(rows)
    out = {'n': n, 'accuracy': round(hits / n, 4),
           'log_loss': round(logloss / n, 5),
           'log_loss_uniform_ref': round(math.log(3), 5),
           'brier_multiclass': round(brier / n, 5),
           'per_outcome': {}}
    for k, pts in per.items():
        b = reliability(pts, nbins=10)
        out['per_outcome'][k] = {
            'n': len(pts),
            'brier': round(sum((p - w) ** 2 for p, w in pts) / len(pts), 5) if pts else None,
            'ece': ece(b), 'slope': calibration_slope(b), 'buckets': b}
    return out


def diff_vs(base, comp):
    """comp 相对 base 的指标差 (负 LogLoss/Brier = 更好)。"""
    if not base or not comp:
        return None
    return {'log_loss_delta': round(comp['log_loss'] - base['log_loss'], 5),
            'brier_delta': round(comp['brier_multiclass'] - base['brier_multiclass'], 5),
            'accuracy_delta': round(comp['accuracy'] - base['accuracy'], 4)}


# ── 各判定源取数 ──────────────────────────────────────────────────────────

def _outcome_and_market(con, day_limit):
    """预载: match_key → (actual, (mkt_h, mkt_d, mkt_a) 去水)。市场价来自 match_outcomes 开盘1X2。"""
    rows = con.execute("""
        SELECT m.match_key,
               CASE WHEN m.score_home > m.score_away THEN 'home'
                    WHEN m.score_home = m.score_away THEN 'draw' ELSE 'away' END,
               o.op_1x2_h, o.op_1x2_d, o.op_1x2_a
        FROM matches m
        JOIN match_outcomes o ON o.mid = m.mid
        WHERE m.status='finished' AND m.score_home IS NOT NULL
          AND o.op_1x2_h > 1 AND o.op_1x2_d > 1 AND o.op_1x2_a > 1
          AND o.is_valid = 1
          AND m.kickoff >= datetime('now', 'localtime', ?)""",
        (f'-{day_limit} day',)).fetchall()
    out = {}
    for mk, act, oh, od, oa in rows:
        mkt = deoverround(float(oh), float(od), float(oa))
        out[mk] = (act, mkt)
    return out


def section_candles(con, om):
    rows = con.execute("""SELECT v.match_key, v.probs FROM prematch_candles_verdict v
        JOIN matches m ON m.match_key = v.match_key
        WHERE m.status='finished' AND m.score_home IS NOT NULL""").fetchall()
    model_rows, market_rows = [], []
    for mk, probs in rows:
        if mk not in om:
            continue
        act, mkt = om[mk]
        try:
            p = json.loads(probs)
        except Exception:
            continue
        model_rows.append((float(p['home']), float(p['draw']), float(p['away']), act))
        market_rows.append((*mkt, act))
    return {'candles_1x2': multi_metrics(model_rows),
            'market_same_set': multi_metrics(market_rows)}


def section_knn(con):
    rows = con.execute("""SELECT p.verdict_code, p.draw_signal,
        CASE WHEN m.score_home > m.score_away THEN 'home'
             WHEN m.score_home = m.score_away THEN 'draw' ELSE 'away' END
        FROM prematch_conclusion p JOIN matches m ON m.match_key = p.match_key
        WHERE m.status='finished' AND m.score_home IS NOT NULL
          AND p.verdict_code IN ('H','D','A')""").fetchall()
    if not rows:
        return None
    n = len(rows)
    hits = sum(1 for code, _, act in rows if {'H': 'home', 'D': 'draw', 'A': 'away'}[code] == act)
    alerts = [(act) for code, ds, act in rows if ds]
    alert_draw = sum(1 for act in alerts if act == 'draw')
    base = sum(1 for _, _, act in rows if act == 'draw') / n
    return {'n': n, 'accuracy': round(hits / n, 4),
            'note': 'KNN只输出方向无概率 → 仅准确率口径 (历史主口径, 注意其标签由相似场ROI派生)',
            'draw_alert_n': len(alerts),
            'draw_alert_precision': round(alert_draw / len(alerts), 4) if alerts else None,
            'draw_base_rate': round(base, 4)}


def section_halftime(con):
    rows = con.execute("""SELECT h.x2_home, h.x2_draw, h.x2_away, h.ou_prob, h.ou_line,
        m.score_home, m.score_away, m.ht_score_home, m.ht_score_away
        FROM halftime_conclusion h JOIN matches m ON m.match_key = h.match_key
        WHERE m.status='finished' AND m.score_home IS NOT NULL
          AND h.x2_home IS NOT NULL AND h.x2_draw IS NOT NULL AND h.x2_away IS NOT NULL""").fetchall()
    x2_rows, ou_rows = [], []
    for xh, xd, xa, oup, oul, fsh, fsa, hth, hta in rows:
        act = result_1x2(fsh, fsa)
        if act:
            x2_rows.append((float(xh), float(xd), float(xa), act))
        if oup is not None and oul is not None and fsh is not None and hth is not None:
            ft_total, ht_total = fsh + fsa, hth + hta
            res = settle_ou(ft_total - ht_total, float(oul))  # 下半场大小
            if res in ('over', 'under'):
                ou_rows.append((float(oup), 1 if res == 'over' else 0))
    if not x2_rows and not ou_rows:
        return None
    out = {'note': '半场冻结时刻的条件概率 (下半场/全场读数)'}
    if x2_rows:
        out['x2_1x2'] = multi_metrics(x2_rows)
    if ou_rows:
        b = reliability(ou_rows, nbins=10)
        out['ou_half2'] = {'n': len(ou_rows),
                           'brier': round(sum((p - w) ** 2 for p, w in ou_rows) / len(ou_rows), 5),
                           'log_loss': round(-sum(w * math.log(max(p, EPS)) + (1 - w) * math.log(max(1 - p, EPS))
                                                  for p, w in ou_rows) / len(ou_rows), 5),
                           'ece': ece(b), 'slope': calibration_slope(b)}
    return out


def section_ht_model(con):
    rows = con.execute("""SELECT v.x2_probs, v.ou_probs, v.ou_line,
        m.score_home, m.score_away FROM ht_model_verdict v
        JOIN matches m ON m.match_key = v.match_key
        WHERE m.status='finished' AND m.score_home IS NOT NULL""").fetchall()
    x2_rows, ou_rows = [], []
    for xp, op, oul, fsh, fsa in rows:
        act = result_1x2(fsh, fsa)
        if act and xp:
            try:
                p = json.loads(xp)
                if isinstance(p, dict):
                    x2_rows.append((float(p['home']), float(p['draw']), float(p['away']), act))
                else:  # 数组格式 [home, draw, away]
                    x2_rows.append((float(p[0]), float(p[1]), float(p[2]), act))
            except Exception:
                pass
        if act and op and oul:
            try:
                p = json.loads(op)
                res = settle_ou(fsh + fsa, float(oul))
                if res in ('over', 'under'):
                    if isinstance(p, dict):
                        ov = float(p.get('OVER', p.get('over', 0)))
                    else:  # 数组格式 [OVER, UNDER]
                        ov = float(p[0])
                    ou_rows.append((ov, 1 if res == 'over' else 0))
            except Exception:
                pass
    if not x2_rows and not ou_rows:
        return None
    out = {'note': 'HT锚模型 (2026-09-16采纳), 样本尚小, 校准结论置信=low'}
    if x2_rows:
        out['x2_1x2'] = multi_metrics(x2_rows)
    if ou_rows:
        b = reliability(ou_rows, nbins=10)
        out['ou_full'] = {'n': len(ou_rows),
                          'brier': round(sum((p - w) ** 2 for p, w in ou_rows) / len(ou_rows), 5),
                          'ece': ece(b)}
    return out


def _binary_metrics(points):
    """二元概率点 [(p, win)] → LogLoss/Brier/ECE/斜率。"""
    if not points:
        return None
    b = reliability(points, nbins=10)
    return {'n': len(points),
            'log_loss': round(-sum(w * math.log(max(p, EPS)) + (1 - w) * math.log(max(1 - p, EPS))
                                   for p, w in points) / len(points), 5),
            'brier': round(sum((p - w) ** 2 for p, w in points) / len(points), 5),
            'ece': ece(b), 'slope': calibration_slope(b)}


def section_daily_predictions(con):
    try:
        rows = con.execute("""SELECT d.payload, m.score_home, m.score_away
            FROM daily_predictions d JOIN matches m ON m.match_key = d.match_key
            WHERE m.status='finished' AND m.score_home IS NOT NULL""").fetchall()
    except Exception:
        return None
    model_rows, market_rows = [], []
    ou25_rows, btts_rows, mou25_rows = [], [], []
    for payload, fsh, fsa in rows:
        try:
            p = json.loads(payload)
        except Exception:
            continue
        act = result_1x2(fsh, fsa)
        if act is None:
            continue
        model_rows.append((p['p_home'], p['p_draw'], p['p_away'], act))
        mi = p.get('market_implied') or {}
        if mi.get('home'):
            market_rows.append((mi['home'], mi['draw'], mi['away'], act))
        total = fsh + fsa
        if p.get('over_2_5') is not None:
            ou25_rows.append((float(p['over_2_5']), 1 if total > 2.5 else 0))
        if p.get('btts') is not None:
            btts_rows.append((float(p['btts']), 1 if (fsh > 0 and fsa > 0) else 0))
        if mi.get('ou_line') == 2.5 and mi.get('p_over') is not None:
            mou25_rows.append((float(mi['p_over']), 1 if total > 2.5 else 0))
    if not model_rows:
        return {'n': 0, 'note': '预测产品层样本不足 (需已完赛且有预测行的场次)'}
    return {'daily_predictions': multi_metrics(model_rows),
            'market_same_set': multi_metrics(market_rows),
            'over_2_5': _binary_metrics(ou25_rows),
            'market_over_2_5_line2.5': _binary_metrics(mou25_rows),
            'btts': _binary_metrics(btts_rows),
            'expected_total_bins': _etot_simple(rows),
            'note': 'O2.5/BTTS/期望进球出自 OIP 比分矩阵 (赛前tick重建), 市场O2.5对照仅限盘口=2.5的场'}


def _etot_simple(rows):
    """期望总进球校准: 0.5 球宽分箱, 每箱 (n, mean_exp, mean_actual)。"""
    bins: Dict[float, List[tuple]] = {}
    for payload, fsh, fsa in rows:
        try:
            p = json.loads(payload)
        except Exception:
            continue
        if p.get('expected_home_goals') is None or fsh is None:
            continue
        exp_total = float(p['expected_home_goals']) + float(p['expected_away_goals'])
        key = round(exp_total * 2) / 2
        bins.setdefault(key, []).append((exp_total, fsh + fsa))
    out = []
    for k in sorted(bins):
        prs = bins[k]
        out.append({'bin': k, 'n': len(prs),
                    'mean_exp': round(sum(e for e, _ in prs) / len(prs), 3),
                    'mean_actual': round(sum(a for _, a in prs) / len(prs), 3)})
    return out


def section_market_big(con, day_limit):
    rows = con.execute("""
        SELECT o.op_1x2_h, o.op_1x2_d, o.op_1x2_a, o.result
        FROM match_outcomes o
        WHERE o.result IN ('home','draw','away')
          AND o.op_1x2_h > 1 AND o.op_1x2_d > 1 AND o.op_1x2_a > 1
          AND o.is_valid = 1
          AND o.kickoff >= datetime('now', 'localtime', ?)
        LIMIT 60000""", (f'-{day_limit} day',)).fetchall()
    pts = [(*deoverround(float(h), float(d), float(a)), r) for h, d, a, r in rows]
    m = multi_metrics(pts)
    if m:
        m['note'] = '市场去水隐含概率 vs 真实赛果 — 概率体系的大样本效率基准 (margin≈市场定价误差容忍度)'
    return m


# ── 报告渲染 ──────────────────────────────────────────────────────────────

def _fmt_section(name, data, baseline=None, note=''):
    if not data:
        return f"## {name}\n\n(无已结算样本)\n"
    lines = [f"## {name}\n"]
    if note:
        lines.append(f"- {note}")
    if 'n' in data:
        lines.append(f"- 样本 n = **{data['n']:,}**")
    if 'accuracy' in data:
        lines.append(f"- TOP1 准确率 = **{data['accuracy']*100:.1f}%**")
    if 'log_loss' in data:
        lines.append(f"- LogLoss = **{data['log_loss']:.4f}** (随机基线 ln3≈1.0986)")
        lines.append(f"- 多分类 Brier = **{data['brier_multiclass']:.4f}**")
    if 'log_loss_delta' in data:
        d = data
        lines.append(f"- **vs 基准**: LogLoss {d['log_loss_delta']:+.4f} · "
                     f"Brier {d['brier_delta']:+.4f} · 准确率 {d['accuracy_delta']*100:+.1f}pp "
                     f"({'模型优于市场' if d['log_loss_delta'] < 0 else '市场基准更优'})")
    if 'draw_alert_precision' in data and data.get('draw_alert_n'):
        lines.append(f"- 平局升级信号: 触发 {data['draw_alert_n']} 场, 实际平局率 "
                     f"**{data['draw_alert_precision']*100:.1f}%** (基准 {data['draw_base_rate']*100:.1f}%)")
    for k in ('home', 'draw', 'away'):
        po = (data.get('per_outcome') or {}).get(k)
        if po and po.get('slope') is not None:
            lines.append(f"- {k} 边缘校准: ECE={po['ece']:.4f}, 斜率={po['slope']} (≈1为佳)")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=3650, help='市场大样本基准的回看天数')
    args = ap.parse_args()

    from gq.db import conn
    with conn(readonly=True) as con:
        om = _outcome_and_market(con, args.days)
        candles = section_candles(con, om)
        knn = section_knn(con)
        half = section_halftime(con)
        htm = section_ht_model(con)
        daily = section_daily_predictions(con)
        market_big = section_market_big(con, args.days)

    report = {
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'metric_principle': '预测系统主指标 = LogLoss / Brier / 校准(ECE·斜率) / TOP1准确率; ROI/CLV 已随投注决策外围归档',
        'candles_1x2': candles.get('candles_1x2'),
        'candles_vs_market_same_set': diff_vs(candles.get('market_same_set'), candles.get('candles_1x2')),
        'knn_prematch': knn,
        'halftime': half,
        'ht_model': htm,
        'daily_predictions': daily.get('daily_predictions') if daily else None,
        'daily_vs_market_same_set': diff_vs(daily.get('market_same_set'), daily.get('daily_predictions')) if daily else None,
        'daily_over_2_5': daily.get('over_2_5') if daily else None,
        'daily_market_over_2_5': daily.get('market_over_2_5_line2.5') if daily else None,
        'daily_btts': daily.get('btts') if daily else None,
        'daily_expected_total_bins': daily.get('expected_total_bins') if daily else None,
        'market_baseline_big': market_big,
    }
    if candles.get('market_same_set'):
        report['candles_market_same_set'] = candles['market_same_set']
    if daily and daily.get('market_same_set'):
        report['daily_market_same_set'] = daily['market_same_set']

    os.makedirs(REPORT_DIR, exist_ok=True)
    jp = os.path.join(REPORT_DIR, 'prediction_calibration_report.json')
    mp = os.path.join(REPORT_DIR, 'prediction_calibration_report.md')
    with open(jp, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    etot = report.get('daily_expected_total_bins') or []
    etot_md = ""
    if etot:
        rows_md = "\n".join(
            f"| {b['bin']} | {b['n']} | {b['mean_exp']:.2f} | {b['mean_actual']:.2f} "
            f"| {b['mean_actual'] - b['mean_exp']:+.2f} |" for b in etot)
        etot_md = ("\n**期望总进球校准 (0.5球宽分箱)** — 「模型说 X 球, 实际是否 ≈X 球」:\n\n"
                   f"| 分箱 | n | 平均期望 | 平均实际 | 偏差 |\n|---|---|---|---|---|\n{rows_md}\n")
    o25 = report.get('daily_over_2_5') or {}
    bts = report.get('daily_btts') or {}
    mo25 = report.get('daily_market_over_2_5')
    daily_detail = ""
    if o25 or bts:
        o25_line = (f"- **O2.5**: n={o25.get('n', 0)}, LogLoss {o25.get('log_loss')}, "
                    f"Brier {o25.get('brier')}, ECE {o25.get('ece')}"
                    + (f" | 市场同盘口对照: LogLoss {mo25.get('log_loss')} (n={mo25.get('n')})" if mo25 else ""))
        daily_detail = ("## E2. 预测产品层派生市场 (OIP 矩阵, 赛前tick重建)\n\n"
                        + o25_line + "\n"
                        + f"- **BTTS 双方进球**: n={bts.get('n', 0)}, LogLoss {bts.get('log_loss')}, "
                          f"Brier {bts.get('brier')}, ECE {bts.get('ece')}\n"
                        + f"- {daily.get('note') if daily else ''}\n" + etot_md)
    md = [f"# 预测校准报告 ({report['generated_at']})",
         f"> {report['metric_principle']}\n",
         _fmt_section("A. K线集成 1X2 (prematch_candles_verdict)", report['candles_1x2'],
                      note="与市场基准同一场集对照见下"),
         _fmt_section("A2. 市场基准 (与A同场集, 去水隐含)", report.get('candles_market_same_set')),
         _fmt_section("A3. K线集成 vs 市场 (同场集差值)", report['candles_vs_market_same_set'] or {'n': 0}),
         _fmt_section("B. KNN 赛前结论 (prematch_conclusion)", knn or {'n': 0}, note=(knn or {}).get('note')),
         _fmt_section("C. 半场冻结读数 (halftime_conclusion)", half or {'n': 0}, note=(half or {}).get('note')),
         _fmt_section("D. HT锚模型 (ht_model_verdict)", htm or {'n': 0}, note=(htm or {}).get('note')),
         _fmt_section("E. 预测产品层 1X2 (daily_predictions)", report['daily_predictions'] or {'n': 0}),
         _fmt_section("E1. 预测产品层 vs 市场 (同场集差值)", report['daily_vs_market_same_set'] or {'n': 0}),
         daily_detail,
         _fmt_section("F. 市场大样本效率基准", market_big, note=(market_big or {}).get('note'))]
    with open(mp, 'w', encoding='utf-8') as f:
        f.write("\n".join(md))

    print(f"=== 预测校准评估 ({report['generated_at']}) ===")
    print(json.dumps({k: v for k, v in report.items()
                      if k in ('candles_1x2', 'candles_vs_market_same_set', 'knn_prematch',
                               'daily_predictions', 'market_baseline_big')
                      and v}, ensure_ascii=False, indent=2)[:3000])
    print(f"\n→ {jp}\n→ {mp}")


if __name__ == '__main__':
    main()
