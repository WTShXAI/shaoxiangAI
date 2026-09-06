"""
GQ 实时盘口 → ReverseOddsEngine 适配器
======================================
把 GQ  odds_snapshots 的 tick 序列转换成 ReverseOddsEngine 能消费的
开盘/收盘赔率, 并叠加亚盘/大小球主线, 实现图片里 "亚欧对比 + P路跟踪" 的前 5 步。

注意：
- 本模块只输出"庄家在降P付/抬P付"、"亚欧是否一致"、"谁是隐含热门"等信号,
  不下"大热必死"的硬结论(与实证冲突)。
- 跨庄分歧 edge 需要多庄数据; GQ 是单庄, 所以这里主要跑单庄 drift + AH/OU 结构。
- 多庄分析请继续用 pipeline.reverse_odds_engine.analyze_multi() + odds_features/live_odds_raw。
"""
from __future__ import annotations
import sqlite3, json, argparse, os, sys
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List, Tuple, Any
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 抑制 sklearn 版本/特征名警告, 保持输出干净
import warnings
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', category=FutureWarning)
from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput, Intent

GQ_DB = os.environ.get('GQ_DB', 'D:/Architecture/data/events.db')


@dataclass
class LineState:
    market: str
    line: Optional[float]
    open_odds: Dict[str, float]
    close_odds: Dict[str, float]
    min_odds: Dict[str, float]
    max_odds: Dict[str, float]
    n_ticks: int
    first_seen: float
    last_seen: float

    def open_fav(self) -> Optional[str]:
        return _argmin(self.open_odds)

    def close_fav(self) -> Optional[str]:
        return _argmin(self.close_odds)

    def avg_odds_change(self) -> Dict[str, float]:
        out = {}
        for k in self.open_odds:
            if self.open_odds[k] > 0:
                out[k] = (self.close_odds.get(k, self.open_odds[k]) - self.open_odds[k]) / self.open_odds[k]
        return out


def _argmin(d: Dict[str, float]) -> Optional[str]:
    if not d:
        return None
    return min(d, key=lambda k: d[k])


def _fetch_ticks(match_key: str, db: str = GQ_DB) -> List[Tuple]:
    con = sqlite3.connect(db)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT captured_at, market, selection, odds, line FROM odds_snapshots "
        "WHERE match_key=? ORDER BY captured_at, market, selection",
        (match_key,)
    ).fetchall()
    con.close()
    return rows


def _extract_1x2(ticks: List[Tuple]) -> Optional[OddsInput]:
    """从 1X2 tick 序列提取开盘/收盘。取每个选项的最早/最晚报价。"""
    sel_times = {}  # selection -> [(captured_at, odds)]
    for ts, market, sel, odds, line in ticks:
        if market != '1X2':
            continue
        if sel not in ('home', 'draw', 'away'):
            continue
        sel_times.setdefault(sel, []).append((ts, float(odds)))
    if not all(k in sel_times and sel_times[k] for k in ('home', 'draw', 'away')):
        return None

    def _first_last(lst):
        lst = sorted(lst, key=lambda x: x[0])
        return lst[0][1], lst[-1][1], min(o for _, o in lst), max(o for _, o in lst)

    open_h, close_h, min_h, max_h = _first_last(sel_times['home'])
    open_d, close_d, min_d, max_d = _first_last(sel_times['draw'])
    open_a, close_a, min_a, max_a = _first_last(sel_times['away'])
    return OddsInput(open_h=open_h, open_d=open_d, open_a=open_a,
                     close_h=close_h, close_d=close_d, close_a=close_a)


def _group_markets(ticks: List[Tuple]) -> Dict[str, List[Tuple]]:
    """按 market 分组, 仅保留全场 AH/OU (剔除 1H/2H)。"""
    groups = {}
    for ts, market, sel, odds, line in ticks:
        if market == '1X2':
            continue
        if not (market.startswith('AH_') or market.startswith('OU_')):
            continue
        # 剔除半场/次节
        if '_1H_' in market or '_2H_' in market:
            continue
        groups.setdefault(market, []).append((ts, sel, float(odds), line))
    return groups


def _extract_main_line(ticks: List[Tuple], market_type: str) -> Optional[LineState]:
    """从分组后的 tick 提取最活跃的 line (快照数最多) 作为主线。"""
    groups = _group_markets(ticks)
    prefix = 'AH_' if market_type == 'AH' else 'OU_'
    candidates = {k: v for k, v in groups.items() if k.startswith(prefix)}
    if not candidates:
        return None
    main_market = max(candidates, key=lambda k: len(candidates[k]))
    records = candidates[main_market]
    line_val = records[0][3]
    try:
        line_val = float(line_val) if line_val is not None else None
    except (TypeError, ValueError):
        line_val = None

    # 每个 selection 的 open/close/min/max
    sel = {}
    for ts, s, odds, _ in records:
        if s not in sel:
            sel[s] = []
        sel[s].append((ts, odds))
    if not sel:
        return None

    open_odds, close_odds, min_odds, max_odds = {}, {}, {}, {}
    for s, recs in sel.items():
        recs = sorted(recs, key=lambda x: x[0])
        open_odds[s] = recs[0][1]
        close_odds[s] = recs[-1][1]
        min_odds[s] = min(o for _, o in recs)
        max_odds[s] = max(o for _, o in recs)

    all_ts = [r[0] for r in records]
    return LineState(
        market=main_market,
        line=line_val,
        open_odds=open_odds,
        close_odds=close_odds,
        min_odds=min_odds,
        max_odds=max_odds,
        n_ticks=len(records),
        first_seen=min(all_ts),
        last_seen=max(all_ts),
    )


def _implied_probs(h: float, d: float, a: float) -> Tuple[float, float, float]:
    ih, idd, ia = 1.0 / h, 1.0 / d, 1.0 / a
    s = ih + idd + ia
    return ih / s, idd / s, ia / s


def _asia_europe_compare(odds: OddsInput, ah: Optional[LineState],
                         ou: Optional[LineState]) -> Dict[str, Any]:
    """亚欧对比：1X2 隐含热门 vs AH 让球方向是否一致。"""
    imp = _implied_probs(odds.close_h, odds.close_d, odds.close_a)
    fav_1x2 = 'home' if imp[0] > imp[2] else ('away' if imp[2] > imp[0] else 'draw')

    out = {
        'fav_1x2': fav_1x2,
        'imp_prob_1x2': {'home': round(imp[0], 4), 'draw': round(imp[1], 4), 'away': round(imp[2], 4)},
        'ah_aligned': None,
        'ou_aligned': None,
    }

    if ah and ah.line is not None and 'home' in ah.close_odds and 'away' in ah.close_odds:
        # AH line sign convention: negative = home favored (handicap), positive = away favored
        line = ah.line
        ah_fav = 'home' if line < 0 else ('away' if line > 0 else 'draw')
        # 但还需要看水位：如果 line=0，水位低的一方是庄家真正倾向
        if line == 0.0:
            ah_fav = _argmin(ah.close_odds) or ah_fav
        out['ah_fav'] = ah_fav
        out['ah_line'] = line
        out['ah_close_odds'] = ah.close_odds
        out['ah_aligned'] = (fav_1x2 == ah_fav)

    if ou and ou.line is not None and 'over' in ou.close_odds and 'under' in ou.close_odds:
        # OU 隐含 expectation ≈ line + (over_water - under_water) * 0.25 (经验线性)
        ov = ou.close_odds['over']
        un = ou.close_odds['under']
        ou_tilt = (1.0 / ov - 1.0 / un)  # 正 = 大球被看好, 负 = 小球
        out['ou_line'] = ou.line
        out['ou_close_odds'] = ou.close_odds
        out['ou_tilt'] = round(ou_tilt, 4)
        # 结合 1X2 隐含总进球期望: 若 1X2 平局概率低且双方接近, 总进球期望偏高
        out['ou_aligned'] = None  # 不硬判, 只给 tilt

    return out


def analyze_gq_match(match_key: str, db: str = GQ_DB,
                     engine: Optional[ReverseOddsEngine] = None) -> Dict[str, Any]:
    """对单场比赛跑 GQ 盘口 drift + 亚欧对比分析。"""
    engine = engine or ReverseOddsEngine()
    ticks = _fetch_ticks(match_key, db)
    if not ticks:
        return {'error': 'no snapshots'}

    odds = _extract_1x2(ticks)
    if odds is None:
        return {'error': 'no 1X2 snapshots'}

    # ReverseOddsEngine 单庄分析
    roe = engine.analyze(odds)

    # AH/OU 主线
    ah = _extract_main_line(ticks, 'AH')
    ou = _extract_main_line(ticks, 'OU')

    # 亚欧对比
    ae = _asia_europe_compare(odds, ah, ou)

    # 构建结果
    result = {
        'match_key': match_key,
        'n_ticks_total': len(ticks),
        '1x2': {
            'open': {'home': odds.open_h, 'draw': odds.open_d, 'away': odds.open_a},
            'close': {'home': odds.close_h, 'draw': odds.close_d, 'away': odds.close_a},
            'drift': {'home': round(odds.drift_h or 0, 4),
                      'draw': round(odds.drift_d or 0, 4),
                      'away': round(odds.drift_a or 0, 4)},
            'implied_probs': {'home': round(roe.implied_probs[0], 4),
                              'draw': round(roe.implied_probs[1], 4),
                              'away': round(roe.implied_probs[2], 4)},
        },
        'intent': {
            'label': roe.intent.value,
            'confidence': round(roe.intent_confidence, 4),
            'pattern': roe.drift_pattern,
            'target': _intent_target(roe.intent),
        },
        'mispricing': {
            'score': round(roe.mispricing_score, 4),
            'argmax_hit_prob': round(roe.argmax_hit_prob, 4),
            'expected_edge': round(roe.expected_edge, 4),
            'kelly_fraction': round(roe.kelly_fraction, 4),
            'recommended_bet': roe.recommended_bet,
        },
        'verdict': roe.verdict,
        'ah': asdict(ah) if ah else None,
        'ou': asdict(ou) if ou else None,
        'asia_europe': ae,
        'note': '单庄分析; 跨庄验证请用 reverse_odds_engine.analyze_multi()',
    }
    return result


def _intent_target(intent: Intent) -> Optional[str]:
    from pipeline.reverse_odds_engine import ReverseOddsEngine
    return ReverseOddsEngine.INTENT_TARGET.get(intent)


def scan_live(db: str = GQ_DB, top_n: int = 10) -> List[Dict]:
    """扫当前有 1X2 快照且 tick 数足够的 live/scheduled 比赛。"""
    con = sqlite3.connect(db)
    cur = con.cursor()
    cur.execute("""
        SELECT m.match_key, m.home, m.away, m.league, m.status, m.minute
        FROM matches m
        WHERE (m.status LIKE '%live%' OR m.status LIKE '%sched%' OR m.status LIKE '%upcoming%'
               OR m.status LIKE '%pre%' OR m.status LIKE '%not_started%')
          AND EXISTS (SELECT 1 FROM odds_snapshots s WHERE s.match_key=m.match_key AND s.market='1X2')
        ORDER BY m.last_seen DESC
    """)
    rows = cur.fetchall()
    con.close()

    engine = ReverseOddsEngine()
    results = []
    for mk, home, away, lg, st, minute in rows[:top_n * 3]:  # 多取一些, 后面按样本过滤
        try:
            r = analyze_gq_match(mk, db, engine)
            if 'error' in r:
                continue
            r['home'] = home
            r['away'] = away
            r['league'] = lg
            r['status'] = st
            r['minute'] = minute
            results.append(r)
            if len(results) >= top_n:
                break
        except Exception as e:
            continue
    return results


def main():
    ap = argparse.ArgumentParser(description='GQ drift + 亚欧对比分析适配器')
    ap.add_argument('--match-key', default=None, help='指定比赛 match_key')
    ap.add_argument('--live', action='store_true', help='扫当前 live/scheduled 比赛')
    ap.add_argument('--top', type=int, default=10, help='live 模式最多输出几场')
    ap.add_argument('--json', default=None, help='输出 JSON 文件路径')
    args = ap.parse_args()

    if args.match_key:
        out = analyze_gq_match(args.match_key)
    elif args.live:
        out = {'matches': scan_live(top_n=args.top)}
    else:
        print('请指定 --match-key 或 --live')
        return

    text = json.dumps(out, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f'[JSON] {args.json}')


if __name__ == '__main__':
    main()
