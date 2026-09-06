#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
v5.7 赛后学习闭环 — 每场实际结果入库后自动分析教训
用法:
  python pipeline/post_match_learner.py --json reports/full_linkage_backtest_*.json
  python pipeline/post_match_learner.py --match "塞内加尔" "伊拉克" "5-0" "让平" "让平(-2)+2:3"
"""

import json, argparse, hashlib
from datetime import datetime, timezone
from pathlib import Path

ARCH_ROOT = Path(__file__).resolve().parent.parent
LESSONS_FILE = ARCH_ROOT / 'config' / 'lessons.json'

def load_lessons():
    if LESSONS_FILE.exists():
        with open(LESSONS_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {'version': 'v5.7', 'lessons': [], 'stats': {'total': 0, 'fixed': 0}}

def save_lessons(data):
    with open(LESSONS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def classify_error(actual, pred_primary, pred_score, actual_score, 
                   sporttery_hcp, attack_crush):
    """自动分类错误类型"""
    errors = []
    
    try:
        ah, aa = map(int, actual_score.split('-'))
        ph, pa = map(int, pred_score.split('-'))
    except (ValueError, TypeError):
        return ['parse_error']
    
    actual_total = ah + aa
    pred_total = ph + pa
    actual_diff = ah - aa
    pred_diff = ph - pa
    
    # 类型A: 让2球不穿律误触发
    if abs(sporttery_hcp or 0) >= 1.75 and not attack_crush:
        if abs(actual_diff) >= 3:
            errors.append('A:让2球不穿律误触发(需进攻碾压豁免)')
            errors.append(f'A_detail: 实际净胜{actual_diff} vs 预测{pred_diff}')
    
    # 类型B: 攻击力误判
    if ah >= 4 or aa >= 4:
        if max(ph, pa) <= 3:
            errors.append('B:强队攻击力低估(预测≤3球 vs 实际≥4球)')
    
    # 类型C: 对冲比分保守
    if abs(actual_diff - pred_diff) >= 2 and abs(actual_diff) >= 2:
        errors.append('C:比分偏差大(对冲保守或激进)')
    
    # 类型D: 方向误判
    if (actual_diff > 0 and pred_diff < 0) or (actual_diff < 0 and pred_diff > 0):
        errors.append('D:方向完全误判(爆冷)')
    
    # 差值分级
    score_gap = abs(actual_total - pred_total)
    if score_gap <= 1:
        errors.append('OK:总球数差值≤1')
    else:
        errors.append(f'GAP:总球数差值={score_gap}')
    
    return errors

def learn_from_backtest(json_path: str):
    """从回测JSON学习"""
    data = load_lessons()
    
    with open(json_path, encoding='utf-8') as f:
        report = json.load(f)
    
    evaluations = report.get('evaluations', [])
    matches = {m['id']: m for m in report.get('matches', [])}
    
    new_lessons = []
    
    for ev in evaluations:
        if ev.get('status') != 'done':
            continue
        
        mid = ev['id']
        match = matches.get(mid, {})
        actual = ev.get('actual_score', '?')
        
        # 只分析错误场次
        is_correct = ev.get('score_exact') or ev.get('hcp_result_match')
        score_gap = None
        try:
            ah, aa = map(int, actual.split('-'))
            ph, pa = map(int, ev.get('pred_score', '0-0').split('-'))
            score_gap = abs((ah + aa) - (ph + pa))
        except (ValueError, TypeError):
            pass
        
        # 差值≤1球视为OK
        if score_gap is not None and score_gap <= 1 and is_correct:
            continue
        
        errors = classify_error(
            actual, ev.get('pred_primary', '?'), ev.get('pred_score', '?'), actual,
            match.get('sporttery_hcp', 0), False
        )
        
        lesson = {
            'id': hashlib.md5(f"{mid}|{actual}|{ev.get('pred_score')}".encode()).hexdigest()[:8],
            'match': ev['match'],
            'actual': actual,
            'pred_primary': ev['pred_primary'],
            'pred_score': ev['pred_score'],
            'score_gap': score_gap,
            'errors': errors,
            'needs_review': any(e.startswith(('A:', 'B:', 'D:')) for e in errors),
            'learned_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'),
        }
        new_lessons.append(lesson)
    
    # 去重合并
    existing_ids = {l['id'] for l in data['lessons']}
    added = 0
    for lesson in new_lessons:
        if lesson['id'] not in existing_ids:
            data['lessons'].append(lesson)
            existing_ids.add(lesson['id'])
            added += 1
    
    data['stats']['total'] = len(data['lessons'])
    data['stats']['needs_review'] = sum(1 for l in data['lessons'] if l.get('needs_review'))
    data['stats']['last_learned'] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
    
    # 按错误类型统计
    error_counts = {}
    for l in data['lessons']:
        for e in l.get('errors', []):
            etype = e[0] if e else '?'
            error_counts[etype] = error_counts.get(etype, 0) + 1
    data['stats']['error_types'] = error_counts
    
    save_lessons(data)
    
    print(f"📚 赛后学习: {len(new_lessons)}条新教训, {added}条新增入库")
    print(f"   累计: {data['stats']['total']}条 | 待审查: {data['stats']['needs_review']}")
    print(f"   错误分布: {error_counts}")
    
    return data

def get_active_rules():
    """读取当前激活的教训规则(供规则引擎使用)"""
    data = load_lessons()
    active = {}
    for l in data['lessons']:
        if l.get('needs_review'):
            for err in l.get('errors', []):
                if err.startswith(('A:', 'B:', 'C:', 'D:')):
                    etype = err[0]
                    if etype not in active:
                        active[etype] = []
                    active[etype].append({
                        'match': l['match'],
                        'detail': err,
                        'count': len(active[etype]) + 1,
                    })
    return active

def main():
    ap = argparse.ArgumentParser(description='赛后学习闭环 v5.7')
    ap.add_argument('--json', help='回测JSON路径')
    ap.add_argument('--match', nargs=5, help='手动录入: 主队 客队 实际比分 预测方向 预测详细')
    ap.add_argument('--rules', action='store_true', help='查看当前激活规则')
    args = ap.parse_args()
    
    if args.json:
        learn_from_backtest(args.json)
    elif args.match:
        home, away, actual, pred_dir, pred_detail = args.match
        try:
            ah, aa = map(int, actual.split('-'))
        except (ValueError, TypeError):
            print(f"错误: 比分格式应为 X-Y, 实际: {actual}")
            return
        errors = classify_error(actual, pred_dir, '0-0', actual, 0, False)
        print(f"   错误分类: {errors}")
        print(f"   待审查: {any(e.startswith(('A:', 'B:', 'D:')) for e in errors)}")
    elif args.rules:
        rules = get_active_rules()
        print(json.dumps(rules, ensure_ascii=False, indent=2))
    else:
        ap.print_help()

if __name__ == '__main__':
    main()
