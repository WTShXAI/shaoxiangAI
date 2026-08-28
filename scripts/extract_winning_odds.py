#!/usr/bin/env python3
"""
哨响AI · 正确选项赔率提取器
=============================
从 events.db 提取每场有赛果比赛的五类盘口中
"正确选项对应的赔率值"，用于 0.01 tick 级赔率结构分析。

核心理念（涛哥指定）：
  不是预测概率，而是提取历史正确答案的赔率数值本身，
  研究"正确选项赔率 vs 错误选项赔率"的统计特征。

输出 CSV 格式：
  match_key, league, home, away, score_home, score_away,
  market, line, correct_selection, correct_odds, other_odds_json
"""

import sqlite3, json, csv, os
from collections import defaultdict

GQ_DB = r'D:\Architecture\data\events.db'
OUT_DIR = r'D:\Architecture\data\extracted_odds'

def get_match_outcomes():
    """取所有有终场比分的比赛"""
    db = sqlite3.connect(GQ_DB)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT mid, home, away, league, kickoff, result,
               CAST(score_home AS INTEGER) as score_home,
               CAST(score_away AS INTEGER) as score_away
        FROM match_outcomes
        WHERE result IN ('home','draw','away')
          AND score_home IS NOT NULL AND score_away IS NOT NULL
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_closing_odds():
    """取每场比赛每个 market+selection 的收盘赔率（最后一帧）"""
    db = sqlite3.connect(GQ_DB)
    db.row_factory = sqlite3.Row

    db.execute("""
        CREATE TEMP TABLE _co AS
        SELECT match_key, market, selection, odds, captured_at
        FROM odds_snapshots
        WHERE (
            market IN ('1X2','CS')
            OR market LIKE 'AH_%'
            OR (market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%')
        )
          AND odds IS NOT NULL AND odds != ''
    """)

    db.execute("""
        CREATE TEMP TABLE _co_final AS
        SELECT match_key, market, selection, MAX(odds) as odds
        FROM (
            SELECT match_key, market, selection, odds,
                   ROW_NUMBER() OVER (
                       PARTITION BY match_key, market, selection
                       ORDER BY captured_at DESC
                   ) AS rn
            FROM _co
        ) WHERE rn=1
        GROUP BY match_key, market, selection
    """)

    rows = db.execute("SELECT match_key, market, selection, odds FROM _co_final").fetchall()
    db.close()

    # 聚合为 match_key -> {market: {selection: odds}}
    agg = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        agg[r['match_key']][r['market']][r['selection']] = float(r['odds'])
    return agg


def parse_ah_line(market):
    """AH_+0.25 -> +0.25, AH_-0.75 -> -0.75, AH_0.00 -> 0.0"""
    s = market[3:]  # strip 'AH_'
    try:
        return float(s.replace('+', ''))
    except ValueError:
        return None


def parse_ou_line(market):
    """OU_2.75 -> 2.75"""
    try:
        return float(market[3:])
    except ValueError:
        return None


def determine_correct(mo, market, selections):
    """
    给定比赛赛果(mo)和一个盘口的赔率字典{selection: odds}，
    返回 (correct_selection, correct_odds, sorted_other_odds)
    """
    h = mo['score_home']
    a = mo['score_away']
    total = h + a
    result = mo['result']  # 'home' / 'draw' / 'away'
    home_diff = h - a

    # ── 1X2 ──
    if market == '1X2':
        correct_sel = result
        if correct_sel not in selections:
            return None
        others = {k: v for k, v in selections.items() if k != correct_sel}
        return (correct_sel, selections[correct_sel], others, None)

    # ── AH ──
    if market.startswith('AH_'):
        line = parse_ah_line(market)
        if line is None:
            return None
        # home_diff = h - a
        # 若 line > 0: 主场受让，实际差距 = home_diff + line
        # 若 line < 0: 主场让球，实际差距 = home_diff + line (line为负)
        adjusted = home_diff + line
        if adjusted > 0:
            correct_sel = 'home'
        elif adjusted < 0:
            correct_sel = 'away'
        else:
            correct_sel = 'draw'  # 走水

        if correct_sel not in selections:
            return None
        correct_odds = selections[correct_sel]
        others = {k: v for k, v in selections.items() if k != correct_sel}
        return (correct_sel, correct_odds, others, line)

    # ── OU ──
    if market.startswith('OU_'):
        line = parse_ou_line(market)
        if line is None:
            return None
        if total > line:
            correct_sel = 'over'
        elif total < line:
            correct_sel = 'under'
        else:
            return None  # push, no winner

        if correct_sel not in selections:
            return None
        correct_odds = selections[correct_sel]
        others = {k: v for k, v in selections.items() if k != correct_sel}
        return (correct_sel, correct_odds, others, line)

    # ── CS ──
    if market == 'CS':
        score_str = f"{h}-{a}"
        # odds_snapshots CS selection 有多种格式: "1-0", "home/1-0", "away/0-1"
        candidates = [score_str, f"home/{score_str}"]
        # 翻转: 如果 away/0-1 存在，则对应 home 1-0 其实是主队视角
        rev = f"{a}-{h}"
        candidates.append(f"away/{rev}")

        correct_odds = None
        correct_sel = None
        for c in candidates:
            if c in selections:
                correct_sel = c
                correct_odds = selections[c]
                break

        if correct_sel is None:
            # 尝试模糊匹配: "1-0" 类比分
            for sel in selections:
                s = sel.strip()
                if s.startswith('home/'):
                    s = s[5:]
                elif s.startswith('away/'):
                    parts = s[5:].split('-')
                    if len(parts) == 2:
                        s = f"{parts[1]}-{parts[0]}"
                if s == score_str:
                    correct_sel = sel
                    correct_odds = selections[sel]
                    break

        if correct_sel is None:
            return None  # 比分不在波胆范围（如5-2等）

        others = {k: v for k, v in selections.items() if k != correct_sel}
        return (correct_sel, correct_odds, others, None)

    return None


def main():
    print("=" * 60)
    print("哨响AI 正确选项赔率提取")
    print("=" * 60)

    outcomes = get_match_outcomes()
    print(f"[1/4] 有终场比分的比赛: {len(outcomes)} 场")

    odds = get_closing_odds()
    print(f"[2/4] 有收盘赔率的比赛: {len(odds)} 场")

    # 用 match_outcomes 的 home+away 匹配 odds 的 match_key
    # odds 的 match_key 格式: "队名 vs 队名"
    mk_map = {}
    for mo in outcomes:
        mk = f"{mo['home']} vs {mo['away']}"
        mk_map[mk] = mo

    print(f"[3/4] 可匹配的比赛: {len(set(mk_map) & set(odds))} 场")

    # 提取
    rows = []
    matched = 0
    total_extractions = 0
    market_counts = defaultdict(int)

    for mk, mo in mk_map.items():
        if mk not in odds:
            continue
        matched += 1
        markets = odds[mk]

        for mkt, sels in markets.items():
            dc = determine_correct(mo, mkt, sels)
            if dc is None:
                continue
            correct_sel, correct_odds, others, line = dc
            total_extractions += 1
            market_counts[mkt.split('_')[0] if '_' in mkt else mkt] += 1

            rows.append({
                'match_key': mk,
                'mid': mo['mid'],
                'league': mo['league'],
                'home': mo['home'],
                'away': mo['away'],
                'kickoff': mo['kickoff'],
                'score_home': mo['score_home'],
                'score_away': mo['score_away'],
                'result': mo['result'],
                'market': mkt,
                'line': line,
                'correct_selection': correct_sel,
                'correct_odds': correct_odds,
                'other_selections': json.dumps(others, ensure_ascii=False),
            })

    print(f"   匹配成功: {matched} 场, 提取记录: {total_extractions} 条")
    print(f"   按盘口: {dict(market_counts)}")

    # 写入 CSV
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, 'winning_odds.csv')
    fieldnames = ['match_key','mid','league','home','away','kickoff','score_home','score_away',
                  'result','market','line','correct_selection','correct_odds','other_selections']

    with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"\n[4/4] 输出: {out_path} ({len(rows)} 行)")
    print(f"\n{'='*60}")
    print("关键统计:")

    # 1X2 正确选项赔率分布
    x1 = [r['correct_odds'] for r in rows if r['market'] == '1X2']
    if x1:
        import statistics
        x1_ok = [x for x in x1 if 1.0 <= x <= 100.0]
        print(f"  1X2 正确选项赔率(全量): n={len(x1)} 脏值={len(x1)-len(x1_ok)}")
        print(f"  1X2 正确选项赔率(clean): mean={statistics.mean(x1_ok):.2f} median={statistics.median(x1_ok):.2f} min={min(x1_ok):.2f} max={max(x1_ok):.2f}")

    # AH 正确选项赔率分布
    ah = [r for r in rows if r['market'].startswith('AH_')]
    if ah:
        ah_odds = [r['correct_odds'] for r in ah]
        print(f"  AH  正确选项赔率: mean={statistics.mean(ah_odds):.2f} median={statistics.median(ah_odds):.2f} 记录数={len(ah)}")

    # OU 正确选项赔率分布
    ou = [r for r in rows if r['market'].startswith('OU_')]
    if ou:
        ou_odds = [r['correct_odds'] for r in ou]
        print(f"  OU  正确选项赔率: mean={statistics.mean(ou_odds):.2f} median={statistics.median(ou_odds):.2f} 记录数={len(ou)}")

    # CS 正确选项赔率分布
    cs = [r for r in rows if r['market'] == 'CS']
    if cs:
        cs_odds = [r['correct_odds'] for r in cs]
        print(f"  CS  正确选项赔率: mean={statistics.mean(cs_odds):.2f} median={statistics.median(cs_odds):.2f} 记录数={len(cs)}")

    # 1X2: 正确选项赔率的小数精度分析（排除脏值）
    x1_clean = [x for x in x1 if 1.0 <= x <= 100.0]
    print(f"\n  1X2 正确选项赔率(clean,排除脏值): mean={statistics.mean(x1_clean):.2f} median={statistics.median(x1_clean):.2f} n={len(x1_clean)}")
    print(f"  1X2 脏值(不在1-100): {[x for x in x1 if not (1.0<=x<=100.0)]}")
    from collections import Counter
    cents = Counter()
    for o in x1_clean:
        frac = int(round(o * 100)) % 100
        cents[frac] += 1
    print(f"  1X2 正确选项赔率小数位(分) Top10:")
    for k, v in cents.most_common(10):
        print(f"    .{k:02d}: {v} 场")

    # CS: 过滤异常值 (应该>=1.0)
    cs_clean = [x for x in cs_odds if 1.0 <= x <= 200.0]
    print(f"\n  CS  正确选项赔率(clean): mean={statistics.mean(cs_clean):.2f} median={statistics.median(cs_clean):.2f} n={len(cs_clean)}")
    if len(cs_clean) < len(cs_odds):
        print(f"  CS  脏值: {[x for x in cs_odds if not (1.0<=x<=200.0)]}")

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
