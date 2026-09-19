#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""预测产品层 — 把"投注决策"换成"概率输出" (2026-09-18 改造主入口)
================================================================================
改造原则 (见 docs/prediction_refactor_checklist.md):
  · 只产品化既有已验证模型的输出, 不做新的混合建模 (AGENTS.md 铁律: 无回测的模型改动禁止)
      1X2  ← prematch_candles_verdict (K线集成, 4轮walkforward采纳) / 无判定时标注"市场基准"
      λ    ← score_model.predict_score (OIP 赔率隐含 Poisson, 生产SSoT, OU诚实锚)
      派生  ← 比分矩阵边缘化: O/U 2.5、BTTS、总进球分布、top比分 (同一矩阵, 概率自洽)
  · 市场赔率只做对照 (de-vig 隐含概率), 不驱动任何"买不买"
  · 输出只解释, 不喊单 — 无 stake / 无 kelly / 无 edge 投注语义

用法:
  python -m pipeline.predict_export --date 2026-09-20            # 生成当日预测表
  python -m pipeline.predict_export --date 2026-09-20 --refresh  # 覆盖未冻结场次
  python -m pipeline.predict_export --match "利物浦 vs 切尔西"     # 单场输出 JSON
存储: events.db daily_predictions (开赛即冻结, 赛后只读 — 校准台账口径)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, r'D:\Architecture')

from pipeline.score_model import deoverround, predict_score

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS daily_predictions (
    match_key   TEXT PRIMARY KEY,
    kickoff     TEXT,
    match_date  TEXT,
    home        TEXT,
    away        TEXT,
    league      TEXT,
    status      TEXT,
    model_source TEXT,
    payload     TEXT NOT NULL,
    generated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_dp_date ON daily_predictions(match_date);
"""


# ── 数据取数 ──────────────────────────────────────────────────────────────

def _kickoff_ts(kickoff_str: str) -> Optional[float]:
    from pipeline.odds_candles import parse_kickoff_ts
    return parse_kickoff_ts(kickoff_str)


def latest_prematch_1x2(con, match_key: str, ko_ts: float) -> Optional[Tuple[float, float, float]]:
    """赛前最后 tick 的 1X2 三向赔率 (与K线模型同源: odds_changes, captured_at<=开赛)。"""
    rows = con.execute("""
        SELECT selection, to_odds, captured_at FROM odds_changes
        WHERE match_key=? AND market='1X2' AND to_odds>1.001 AND to_odds<500
        ORDER BY captured_at""", (match_key,)).fetchall()
    last: Dict[str, float] = {}
    for sel, odds, cap in rows:
        if float(cap) <= ko_ts:
            last[sel] = float(odds)
    if not all(last.get(s) for s in ('home', 'draw', 'away')):
        return None
    return last['home'], last['draw'], last['away']


def latest_prematch_ou(con, match_key: str, ko_ts: float) -> Optional[Dict[str, float]]:
    """赛前主 OU 线 (全场盘, 非1H/2H): 最接近 2.5 的、双向有价的线。
    返回 {line, p_over, implied_total} — implied_total 即 _live_predict 同款的"诚实锚"。"""
    rows = con.execute("""
        SELECT market, selection, to_odds, captured_at FROM odds_changes
        WHERE match_key=? AND market LIKE 'OU\\_%' ESCAPE '\\' AND to_odds>1.001 AND to_odds<500
        ORDER BY captured_at""", (match_key,)).fetchall()
    by_line: Dict[float, Dict[str, float]] = {}
    for market, sel, odds, cap in rows:
        parts = str(market).split('_')
        if len(parts) < 2 or parts[1] in ('1H', '2H'):
            continue
        try:
            line = float(parts[1])
        except ValueError:
            continue
        if float(cap) <= ko_ts:
            by_line.setdefault(line, {})[sel] = float(odds)
    cands = [(abs(l - 2.5), l, v) for l, v in by_line.items()
             if v.get('over') and v.get('under')]
    if not cands:
        return None
    cands.sort()
    _, line, sides = cands[0]
    po_raw, pu_raw = 1.0 / sides['over'], 1.0 / sides['under']
    p_over = po_raw / (po_raw + pu_raw)
    implied_total = line + 2.0 * (p_over - 0.5)
    if not (1.0 < implied_total < 6.0):
        return None
    return {'line': line, 'p_over': round(p_over, 4), 'implied_total': round(implied_total, 3)}


def latest_prematch_btts(con, match_key: str, ko_ts: float) -> Optional[Dict[str, float]]:
    """赛前最后 tick 的 BTTS (双方进球) 盘 — 市场对照口径 (2026-09-19 接入, 单庄同源, IR-32 合规)。
    返回 {p_yes, odds_yes, odds_no}; 无盘/非法返回 None。"""
    from pipeline.odds_math import devig2
    rows = con.execute("""
        SELECT selection, to_odds, captured_at FROM odds_changes
        WHERE match_key=? AND market='BTTS' AND to_odds>1.001 AND to_odds<500
        ORDER BY captured_at""", (match_key,)).fetchall()
    last: Dict[str, float] = {}
    for sel, odds, cap in rows:
        if float(cap) <= ko_ts:
            last[str(sel).lower()] = float(odds)
    if not last.get('yes') or not last.get('no'):
        return None
    r = devig2(last['yes'], last['no'])
    if r is None:
        return None
    return {'p_yes': round(r[0], 4), 'odds_yes': last['yes'], 'odds_no': last['no']}


def candles_verdict(con, match_key: str) -> Optional[Dict[str, Any]]:
    """读采集器开赛定格的 K线集成判定 (采集器在临场≤2h 写入, 无则返回 None)。"""
    row = con.execute(
        "SELECT direction, probs, confidence, margin FROM prematch_candles_verdict WHERE match_key=?",
        (match_key,)).fetchone()
    if not row:
        return None
    try:
        probs = json.loads(row[1])
    except Exception:
        return None
    return {'direction': row[0], 'probs': probs,
            'confidence': float(row[2] or 0.0), 'margin': float(row[3] or 0.0)}


# ── 单场预测 ──────────────────────────────────────────────────────────────

def predict_match_full(con, match_key: str, allow_candles_compute: bool = False) -> Optional[Dict[str, Any]]:
    """单场完整概率输出。返回 None = 无赛前1X2赔率, 无法构成概率输出。"""
    m = con.execute("""SELECT home, away, league, kickoff, status, score_home, score_away
                       FROM matches WHERE match_key=?""", (match_key,)).fetchone()
    if not m:
        return None
    home, away, league, kickoff, status, fsh, fsa = m
    if not kickoff:
        return None
    ko_ts = _kickoff_ts(kickoff)
    if ko_ts is None:
        return None
    odds = latest_prematch_1x2(con, match_key, ko_ts)
    if odds is None:
        return None
    oh, od, oa = odds
    mkt_ph, mkt_pd, mkt_pa = deoverround(oh, od, oa)
    market_implied = {'home': round(mkt_ph, 4), 'draw': round(mkt_pd, 4), 'away': round(mkt_pa, 4)}

    # 1X2 模型概率: K线集成判定优先, 否则如实标注市场基准 (不造新模型)
    cv = candles_verdict(con, match_key)
    if cv is None and allow_candles_compute and status != 'finished':
        try:
            from pipeline.odds_candles_predict import predict_match
            r = predict_match(con, match_key, ko_ts)
            if r.get('ok'):
                cv = {'direction': r['direction'], 'probs': r['probs'],
                      'confidence': float(r.get('confidence') or 0.0),
                      'margin': float(r.get('margin') or 0.0)}
        except Exception:
            cv = None  # 模型不可用 → 市场基准, 不硬编
    if cv:
        p = cv['probs']
        p_home, p_draw, p_away = float(p['home']), float(p['draw']), float(p['away'])
        model_source = 'candles_ensemble'
        conf_val = cv['confidence']
        conf_band = 'high' if conf_val >= 0.60 else ('medium' if conf_val >= 0.48 else 'low')
    else:
        p_home, p_draw, p_away = mkt_ph, mkt_pd, mkt_pa
        model_source = 'market_baseline'
        conf_val, conf_band = None, 'baseline'

    # OIP 比分矩阵 (生产 SSoT): λ 锚定 OU 总进球 (诚实锚), 派生市场全部出自同一矩阵。
    # ⚠ goal_scale=1.0 (2026-09-18 A/B 实证, n=8194 回填场, scratch_ab_goalscale.py):
    #   score_model 默认 1.2 是波胆 top3 命中率的 λ 放大 (方差补丁), 用于期望/大小球输出会
    #   系统性高估 ~1 球 (O2.5 LogLoss 0.757→0.677, ECE 0.209→0.085, BTTS ECE 0.159→0.084,
    #   期望偏差 -1.13→-0.59)。产品层派生市场用诚实锚 1.0; score_model 本体默认值不动。
    ou = latest_prematch_ou(con, match_key, ko_ts)
    btts_mkt = latest_prematch_btts(con, match_key, ko_ts)
    r = predict_score(home, away, oh, od, oa,
                      goal_scale=1.0,
                      implied_total=(ou or {}).get('implied_total'))
    M = r['matrix']
    mg = M.shape[0] - 1
    total_dist: Dict[str, float] = {}
    for t in range(0, 7):
        total_dist[str(t)] = float(sum(M[i, t - i] for i in range(0, t + 1) if t - i <= mg))
    total_dist['7+'] = max(0.0, 1.0 - sum(total_dist.values()))
    over_2_5 = 1.0 - (total_dist['0'] + total_dist['1'] + total_dist['2'])
    btts = float(sum(M[i, j] for i in range(1, mg + 1) for j in range(1, mg + 1)))
    top = [[f"{i}-{j}", round(float(M[i, j]), 4)] for i, j, _ in r['top_scores']]

    # 偏差说明 (只解释, 不喊单)
    diff = {'home': (p_home - mkt_ph) * 100, 'draw': (p_draw - mkt_pd) * 100,
            'away': (p_away - mkt_pa) * 100}
    if model_source == 'market_baseline':
        note = '本场无独立模型判定, 概率即市场去水隐含基准'
    else:
        name = {'home': '主胜', 'draw': '平局', 'away': '客胜'}
        k = max(diff, key=lambda x: abs(diff[x]))
        if abs(diff[k]) < 2.0:
            note = '模型与市场基本一致 (偏差 < 2pp)'
        else:
            orient = '更看好' if diff[k] > 0 else '更谨慎于'
            note = f'模型较市场{orient}{name[k]} ({diff[k]:+.1f}pp)'
        if abs(diff['draw']) >= 3.0 and k != 'draw':
            note += f'; 平局偏差 {diff["draw"]:+.1f}pp 值得留意'

    out = {
        'match_key': match_key,
        'home': home, 'away': away, 'league': league,
        'kickoff': kickoff, 'status': status,
        'model_source': model_source,
        'model_confidence': conf_band,
        'model_confidence_value': round(conf_val, 4) if conf_val is not None else None,
        'p_home': round(p_home, 4), 'p_draw': round(p_draw, 4), 'p_away': round(p_away, 4),
        'expected_home_goals': r['lh'], 'expected_away_goals': r['la'],
        'over_2_5': round(over_2_5, 4),
        'btts': round(btts, 4),
        'total_goals_distribution': {k: round(v, 4) for k, v in total_dist.items()},
        'top_scorelines': top,
        'market_implied': {**market_implied,
                           'ou_line': (ou or {}).get('line'),
                           'p_over': (ou or {}).get('p_over'),
                           'btts_p': (btts_mkt or {}).get('p_yes'),
                           'odds_1x2': [round(oh, 2), round(od, 2), round(oa, 2)],
                           'odds_btts': ([round(btts_mkt['odds_yes'], 2), round(btts_mkt['odds_no'], 2)]
                                         if btts_mkt else None)},
        'deviation_note': note,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    return out


# ── 按日构建 + 落库 ────────────────────────────────────────────────────────

def build_for_date(con, date_str: str, refresh: bool = False,
                   allow_candles_compute: bool = True,
                   include_finished: bool = False) -> List[Dict[str, Any]]:
    """对指定日期 (本地, kickoff 前缀匹配) 的比赛生成预测并写 daily_predictions。
    开赛即冻结: 已开赛/已完赛的场不覆盖既有行 (赛后校准台账口径)。
    include_finished=True 为历史回填模式 (回测口径): 含已完赛场, 只补空行、绝不覆盖。
    回填依然只用赛前 tick (captured_at<=kickoff), 与前向预测同一数据纪律。"""
    if include_finished:
        rows = con.execute("""SELECT match_key FROM matches
            WHERE kickoff LIKE ? ORDER BY kickoff""", (f'{date_str}%',)).fetchall()
    else:
        rows = con.execute("""SELECT match_key FROM matches
            WHERE kickoff LIKE ? AND status NOT IN ('finished','abandoned','postponed','canceled')
            ORDER BY kickoff""", (f'{date_str}%',)).fetchall()
    now = time.time()
    out: List[Dict[str, Any]] = []
    for (mk,) in rows:
        ko_row = con.execute("SELECT kickoff FROM matches WHERE match_key=?", (mk,)).fetchone()
        ko_ts = _kickoff_ts((ko_row[0] if ko_row else '') or '')
        existing = con.execute(
            "SELECT generated_at, kickoff FROM daily_predictions WHERE match_key=?", (mk,)).fetchone()
        if include_finished:
            if existing:
                continue  # 回填只补空行, 绝不覆盖 (前向定格行是校准台账, 神圣不可动)
        else:
            if existing and not refresh:
                continue
            if existing and ko_ts and now > ko_ts:
                continue  # 开赛定格
        pred = predict_match_full(con, mk, allow_candles_compute=allow_candles_compute)
        if pred is None:
            continue
        con.execute("""INSERT INTO daily_predictions
            (match_key, kickoff, match_date, home, away, league, status, model_source, payload, generated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_key) DO UPDATE SET
              kickoff=excluded.kickoff, match_date=excluded.match_date, status=excluded.status,
              model_source=excluded.model_source, payload=excluded.payload,
              generated_at=excluded.generated_at""",
            (mk, pred['kickoff'], date_str, pred['home'], pred['away'], pred['league'],
             pred['status'], pred['model_source'], json.dumps(pred, ensure_ascii=False), now))
        out.append(pred)
    return out


def read_for_date(con, date_str: str) -> List[Dict[str, Any]]:
    rows = con.execute("""SELECT payload FROM daily_predictions WHERE match_date=?
        ORDER BY kickoff""", (date_str,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def _connect(readonly: bool = False):
    from gq.db import conn
    return conn(readonly=readonly)


def main():
    ap = argparse.ArgumentParser(description='预测产品层: 生成每日概率预测表')
    ap.add_argument('--date', default=datetime.now().strftime('%Y-%m-%d'))
    ap.add_argument('--match', help='单场 match_key, 直接打印 JSON')
    ap.add_argument('--refresh', action='store_true', help='重算未冻结场次')
    ap.add_argument('--no-candles-compute', action='store_true', help='只用已定格判定, 不现算K线')
    ap.add_argument('--backfill-days', type=int, default=0,
                    help='回填近N天(含今天)全部场次的预测 (已完赛场做赛前重建, 只补空行)')
    ap.add_argument('--out', help='同时导出 JSON 文件路径')
    args = ap.parse_args()

    if args.match:
        with _connect(readonly=True) as con:
            pred = predict_match_full(con, args.match, allow_candles_compute=not args.no_candles_compute)
        print(json.dumps(pred, ensure_ascii=False, indent=2) if pred else
              f'[{args.match}] 无赛前1X2赔率, 无法构成概率输出')
        return

    if args.backfill_days > 0:
        from datetime import timedelta
        with _connect() as con:
            con.executescript(TABLE_DDL)
            total = 0
            for i in range(args.backfill_days - 1, -1, -1):
                d = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
                n = len(build_for_date(con, d, allow_candles_compute=not args.no_candles_compute,
                                       include_finished=True))
                total += n
                print(f'  [{d}] 回填 {n} 场')
        print(f'回填完成: 共 {total} 场')
        return

    with _connect() as con:
        con.executescript(TABLE_DDL)
        built = build_for_date(con, args.date, refresh=args.refresh,
                               allow_candles_compute=not args.no_candles_compute)
    with _connect(readonly=True) as ro:
        all_rows = read_for_date(ro, args.date)
    print(f'[{args.date}] 新生成 {len(built)} 场, 表内共 {len(all_rows)} 场 '
          f'(K线集成 {sum(1 for p in all_rows if p["model_source"] == "candles_ensemble")} / '
          f'市场基准 {sum(1 for p in all_rows if p["model_source"] == "market_baseline")})')
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(all_rows, f, ensure_ascii=False, indent=2)
        print(f'→ {args.out}')


if __name__ == '__main__':
    main()
