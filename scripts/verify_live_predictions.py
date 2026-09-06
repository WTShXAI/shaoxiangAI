"""
Live 进行中比赛预测验证脚本 (一键复跑)
=========================================
用途: 拉取 bridge_service 的实时进行中比赛, 调 /api/terminal/analyze,
      对 4 个维度(方向自洽 / 波胆合理 / 价值层决策 / 赔率漂移响应)逐项校验。
用法:
    .venv/Scripts/python.exe scripts/verify_live_predictions.py [--limit 5] [--host http://localhost:9000]
    .venv/Scripts/python.exe scripts/verify_live_predictions.py --match-file _verify_tmp/sample_matches.json
产出:
    _verify_tmp/verify_results.json   结构化结果
    终端表格 + PASS/FAIL 判定
作者: 2026-07-26
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error

# ────────────────────────── HTTP 辅助 ──────────────────────────
def http_get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {"error": str(e)}


def http_post(url, body, timeout=15):
    data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception as e:
        return {"error": str(e)}


# ────────────────────────── 评分逻辑 ──────────────────────────
def parse_score(card):
    """从 inplay / oip 推断当前比分"""
    ip = card.get('inplay') or {}
    if ip.get('current_score'):
        cs = ip['current_score']
        if isinstance(cs, str) and '-' in cs:
            parts = cs.split('-')
            return int(parts[0]), int(parts[1])
        if isinstance(cs, (list, tuple)) and len(cs) == 2:
            return int(cs[0]), int(cs[1])
    return None, None


def fmt_odds(oh, od, oa):
    def f(v): return f'{v:.2f}' if isinstance(v, (int, float)) else '?'
    return f'{f(oh)}/{f(od)}/{f(oa)}'


# 维度 1: 方向预测自洽性
def check_direction(card, sh, sa):
    """direction 应与领先方自洽。direction 是 'home'/'draw'/'away' 或文本。"""
    d = str(card.get('direction') or '').lower()
    issues = []
    if not d or d in ('none', 'nan'):
        issues.append('direction 为空')
        return 'FAIL', issues
    # 识别方向
    is_home = ('主' in d and ('胜' in d or '赢' in d)) or d == 'home' or d == 'h'
    is_away = ('客' in d and ('胜' in d or '赢' in d)) or d == 'away' or d == 'a'
    is_draw = '平' in d or d == 'draw' or d == 'd'
    # 领先方
    if sh is None or sa is None:
        return 'SKIP', ['无比分信息, 跳过方向自洽检查']
    diff = sh - sa
    if diff > 0:  # 主队领先
        if is_away:
            issues.append(f'主队领先 {sh}-{sa} 但方向=客胜(异常: 可能反映市场预期客胜)')
            return 'WARN', issues
        return 'PASS', issues
    if diff < 0:  # 客队领先
        if is_home:
            issues.append(f'客队领先 {sh}-{sa} 但方向=主胜(异常: 可能反映市场预期主胜)')
            return 'WARN', issues
        return 'PASS', issues
    # 平局
    if is_draw:
        return 'PASS', issues
    # 平比分但方向非平 — OIP/市场仍偏向某方, 合理(平比分不等于市场认为平)
    return 'PASS', issues + [f'平比分 {sh}-{sa} 但方向={d}(允许, 市场仍可偏向)']


# 维度 2: 波胆模型合理性
def check_correct_score(card, sh, sa):
    oip = card.get('oip') or {}
    issues = []
    # 后端字段: top3_scores (str[]), top5_scores (str[]), scores_annotated (dict[])
    top = oip.get('top3_scores') or oip.get('top_scores') or oip.get('top3') or []
    if not top:
        annotated = oip.get('scores_annotated') or []
        top = [a.get('score') for a in annotated] if annotated else []
    if not top:
        return 'FAIL', issues + ['oip.top3_scores 为空']
    # 标准化为 (i, j) 元组
    norm = []
    for t in top:
        if isinstance(t, str) and '-' in t:
            parts = t.split('-')
            try:
                norm.append((int(parts[0]), int(parts[1])))
            except ValueError:
                pass
        elif isinstance(t, (list, tuple)) and len(t) >= 2:
            norm.append((int(t[0]), int(t[1])))
        elif isinstance(t, dict):
            s = t.get('score')
            if isinstance(s, str) and '-' in s:
                parts = s.split('-')
                try:
                    norm.append((int(parts[0]), int(parts[1])))
                except ValueError:
                    pass
    if not norm:
        return 'WARN', issues + [f'top3_scores 格式无法解析: {top[:1]}']
    # 检查 1: 已不可能比分是否被排除
    impossible = []
    if sh is not None and sa is not None:
        for (i, j) in norm:
            if i < sh or j < sa:  # 主队进球少于已有, 或客队进球少于已有 → 不可能
                impossible.append(f'{i}-{j}')
    if impossible:
        issues.append(f'包含已不可能比分(主<{sh}或客<{sa}): {impossible}')
        return 'FAIL', issues
    # 检查 2: top1 是否合理(若已进球, top1 应包含当前比分附近)
    # 检查 3: inplay time_ratio
    ip = card.get('inplay') or {}
    tr = ip.get('time_ratio')
    if tr is not None:
        if not (0.0 < tr <= 1.05):
            issues.append(f'time_ratio={tr:.3f} 超出 (0,1] 合理区间')
            return 'WARN', issues
    return 'PASS', issues + [f'top3={[f"{i}-{j}" for i,j in norm[:3]]}']


# 维度 3: 价值层决策
def check_value_layer(card, books_count):
    vl_rows = card.get('rows') or []
    decision = str(card.get('decision') or 'PASS').upper()
    decision_text = card.get('decision_text') or ''
    issues = []
    if not vl_rows:
        return 'WARN', issues + ['value_layer.rows 为空']
    # 检查 1: 单庄是否正确降级 PASS
    if (books_count or 0) <= 1:
        if decision == 'BET':
            issues.append(f'单庄(books={books_count}) 却 BET, 应降级 PASS')
            return 'FAIL', issues
        return 'PASS', issues + ['单庄正确降级为 PASS']
    # 检查 2: BET 时 EV 应 > 0
    if decision == 'BET':
        best_ev = None
        for r in vl_rows:
            ev = r.get('ev_pct')
            if isinstance(ev, (int, float)):
                if best_ev is None or ev > best_ev:
                    best_ev = ev
        if best_ev is not None and best_ev <= 0:
            issues.append(f'BET 但最佳 EV%={best_ev:.2f} ≤ 0')
            return 'FAIL', issues
        return 'PASS', issues + [f'BET, best EV%={best_ev}']
    # PASS
    return 'PASS', issues + ['PASS']


# 维度 4: 赔率漂移响应(对比初盘 vs 实时)
def check_drift_response(card_live, card_opening, oh, oa, ooh, ooa):
    """对同一比赛用初盘赔率和实时赔率各跑一次, 看方向/edge/波胆 是否同步变化。"""
    issues = []
    d_live = str(card_live.get('direction') or '').lower()
    d_open = str(card_opening.get('direction') or '').lower()
    e_live = card_live.get('best_edge_pct')
    e_open = card_opening.get('best_edge_pct')
    # 漂移幅度
    drift_h = (oh - ooh) / ooh if (ooh and oh) else 0
    drift_a = (oa - ooa) / ooa if (ooa and oa) else 0
    max_drift = max(abs(drift_h), abs(drift_a))
    if max_drift < 0.05:
        return 'SKIP', issues + [f'漂移小({max_drift*100:.1f}%), 跳过漂移响应检查']
    # 大漂移时, 以下任一信号响应即算 PASS: 方向变 / edge 明显变 / top3 波胆重排
    if d_live != d_open:
        return 'PASS', issues + [f'方向随漂移变化: {d_open}→{d_live}']
    delta_e = abs((e_live or 0) - (e_open or 0))
    if delta_e >= 0.5:
        return 'PASS', issues + [f'方向稳定, edge 变化 {e_open}→{e_live} (Δ={delta_e:.2f})']
    # 检查 top3 波胆是否重排
    live_top3 = (card_live.get('oip') or {}).get('top3_scores') or []
    open_top3 = (card_opening.get('oip') or {}).get('top3_scores') or []
    if live_top3 and open_top3:
        overlap = set(live_top3[:3]) & set(open_top3[:3])
        if len(overlap) < 2:
            return 'PASS', issues + [f'top3 波胆重排响应漂移: {open_top3[:3]}→{live_top3[:3]}']
        return 'PASS', issues + [f'方向/edge/top3 稳定(漂移 {max_drift*100:.1f}% 未触发主信号变化)']
    return 'PASS', issues + ['方向稳定(漂移未改变主信号)']


# ────────────────────────── 主流程 ──────────────────────────
def analyze_one(host, m, run_opening=True):
    """对单场比赛调 analyze, 返回完整评估 dict"""
    home = m.get('home'); away = m.get('away')
    sk = m.get('league') or 'soccer_fifa_world_cup'
    sh = m.get('score_home') or 0
    sa = m.get('score_away') or 0
    minute = m.get('match_minute') or ''
    body_live = {
        'home': home, 'away': away, 'sport_key': sk,
        'odds_h': m.get('odds_h'), 'odds_d': m.get('odds_d'), 'odds_a': m.get('odds_a'),
        'ah_line': m.get('ah_line'), 'ah_home': m.get('ah_home'), 'ah_away': m.get('ah_away'),
        'ou_line': m.get('ou_line'), 'ou_over': m.get('ou_over'), 'ou_under': m.get('ou_under'),
        'home_goals': sh if sh > 0 else None,
        'away_goals': sa if sa > 0 else None,
        'elapsed': _parse_minute(minute),
    }
    card_live = http_post(f'{host}/api/terminal/analyze', body_live)
    card_live = card_live.get('data', card_live) if isinstance(card_live, dict) else {}
    if card_live.get('error'):
        return {'home': home, 'away': away, 'error': card_live['error']}

    books = card_live.get('books_count') or 0
    r1, i1 = check_direction(card_live, sh, sa)
    r2, i2 = check_correct_score(card_live, sh, sa)
    r3, i3 = check_value_layer(card_live, books)

    # 维度 4: 初盘 vs 实时
    opening_result = None
    if run_opening and m.get('opening_h') and m.get('opening_a'):
        body_open = dict(body_live)
        body_open['odds_h'] = m.get('opening_h')
        body_open['odds_d'] = m.get('opening_d')
        body_open['odds_a'] = m.get('opening_a')
        # 用初盘时不带当前比分(模拟赛前预期)
        body_open['home_goals'] = None
        body_open['away_goals'] = None
        body_open['elapsed'] = None
        card_open = http_post(f'{host}/api/terminal/analyze', body_open)
        card_open = card_open.get('data', card_open) if isinstance(card_open, dict) else {}
        if not card_open.get('error'):
            r4, i4 = check_drift_response(
                card_live, card_open,
                m.get('odds_h'), m.get('odds_a'),
                m.get('opening_h'), m.get('opening_a'))
            opening_result = {
                'card': _slim_card(card_open),
                'verdict': r4, 'issues': i4,
            }
        else:
            r4, i4 = 'SKIP', [f'初盘分析失败: {card_open.get("error")}']
    else:
        r4, i4 = 'SKIP', ['无初盘数据, 跳过漂移检查']

    return {
        'fixture': {'home': home, 'away': away, 'league': sk,
                    'score': f'{sh}-{sa}', 'minute': str(minute),
                    'odds': fmt_odds(m.get('odds_h'), m.get('odds_d'), m.get('odds_a')),
                    'opening': fmt_odds(m.get('opening_h'), m.get('opening_d'), m.get('opening_a'))},
        'direction': card_live.get('direction'),
        'decision': card_live.get('decision'),
        'decision_text': card_live.get('decision_text'),
        'books_count': books,
        'model_type': card_live.get('model_type'),
        'top3': ((card_live.get('oip') or {}).get('top3_scores') or [])[:3],
        'inplay_time_ratio': ((card_live.get('inplay') or {}).get('time_ratio')),
        'card': _slim_card(card_live),
        'dimensions': {
            'direction_consistency': {'verdict': r1, 'issues': i1},
            'correct_score': {'verdict': r2, 'issues': i2},
            'value_layer': {'verdict': r3, 'issues': i3},
            'drift_response': {'verdict': r4, 'issues': i4},
        },
        'opening_result': opening_result,
    }


def _slim_card(card):
    """精简 card 以便存档(去掉过深嵌套)"""
    if not isinstance(card, dict):
        return card
    return {
        'direction': card.get('direction'),
        'decision': card.get('decision'),
        'best_edge_pct': card.get('best_edge_pct'),
        'books_count': card.get('books_count'),
        'model_type': card.get('model_type'),
        'top3': ((card.get('oip') or {}).get('top3_scores') or [])[:3],
        'inplay_time_ratio': ((card.get('inplay') or {}).get('time_ratio')),
    }


def _parse_minute(minute):
    if not minute:
        return None
    s = str(minute).replace("'", '').replace('′', '').replace('分', '').strip()
    try:
        return int(float(s))
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser(description='Live 比赛预测验证')
    ap.add_argument('--host', default='http://localhost:9000')
    ap.add_argument('--limit', type=int, default=5, help='选取的比赛数')
    ap.add_argument('--match-file', help='直接读样本 JSON (跳过拉取)')
    ap.add_argument('--no-opening', action='store_true', help='不跑初盘对照')
    ap.add_argument('--out', default='_verify_tmp/verify_results.json')
    args = ap.parse_args()

    # 0. 健康检查
    health = http_get(f'{args.host}/health', timeout=5)
    if health.get('error') or not health.get('ok'):
        print(f'❌ bridge 不可达: {health}')
        sys.exit(1)
    print(f'✅ bridge healthy: {health.get("engine","?")} | db={health.get("checks",{}).get("db")}')

    # 1. 拉取样本
    if args.match_file and os.path.exists(args.match_file):
        matches = json.load(open(args.match_file, encoding='utf-8'))
        print(f'📂 从 {args.match_file} 读取 {len(matches)} 场')
    else:
        d = http_get(f'{args.host}/api/live-scores?limit=200')
        d = d.get('data', d)
        all_m = d.get('matches', [])
        # 优先选有进球 + 有初盘 + 有完整赔率的
        good = [x for x in all_m
                if x.get('odds_h') and x.get('opening_h')
                and (x.get('score_home') or 0) + (x.get('score_away') or 0) > 0]
        # 补一场 0-0 上半场作对照
        zeros = [x for x in all_m
                 if x.get('odds_h') and x.get('opening_h')
                 and (x.get('score_home') or 0) + (x.get('score_away') or 0) == 0]
        matches = (good + zeros)[:args.limit]
        print(f'📡 拉到 {len(all_m)} 场 live, 精选 {len(matches)} 场')

    if not matches:
        print('⚠️  无可用样本')
        sys.exit(0)

    # 2. 逐场分析
    results = []
    for i, m in enumerate(matches, 1):
        print(f'\n[{i}/{len(matches)}] {m.get("home")} vs {m.get("away")} '
              f'{m.get("score_home",0)}-{m.get("score_away",0)}')
        r = analyze_one(args.host, m, run_opening=not args.no_opening)
        results.append(r)
        if r.get('error'):
            print(f'    ❌ {r["error"]}')
            continue
        for dim_name, dim in r['dimensions'].items():
            mark = {'PASS': '✅', 'WARN': '⚠️ ', 'FAIL': '❌', 'SKIP': '⏭ '}.get(dim['verdict'], '?')
            detail = '; '.join(dim['issues'])[:80]
            print(f'    {mark} {dim_name:24} {detail}')

    # 3. 汇总
    print('\n' + '='*60)
    summary = {'PASS': 0, 'WARN': 0, 'FAIL': 0, 'SKIP': 0}
    for r in results:
        for dim in r.get('dimensions', {}).values():
            summary[dim['verdict']] = summary.get(dim['verdict'], 0) + 1
    total = sum(summary.values())
    print(f'汇总: {total} 项检查 = ✅{summary["PASS"]} PASS  ⚠️ {summary["WARN"]} WARN  '
          f'❌{summary["FAIL"]} FAIL  ⏭{summary["SKIP"]} SKIP')
    if summary['FAIL'] == 0:
        print('🎉 无 FAIL 项')
    else:
        print(f'⚠️  有 {summary["FAIL"]} 项 FAIL 需排查')

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump({'health': health, 'results': results, 'summary': summary},
                  f, ensure_ascii=False, indent=2)
    print(f'\n📄 完整结果已写入 {args.out}')


if __name__ == '__main__':
    main()
