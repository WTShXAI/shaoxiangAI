#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FootballAI v4.9 全链路回测 — 6/26 + 6/27 共11场
================================================
验证: OU联动 + 让球冷门律 + 大小球诚实度 + D-Gate风控 + TaoGe策略 + 模型概率
基准: 实际赛果

执行:
    python pipeline/backtest_626_627_full_linkage.py
"""
import os, sys, json
from pathlib import Path

ARCH_ROOT = Path(__file__).resolve().parent.parent

from pipeline.full_linkage_predictor import FullLinkagePipeline, MatchInput

# ═══════════════════════════════════════════════════════════
# 6/26 + 6/27 已知赛果的11场比赛 (来自截图 + 方案D中奖票 + WC26 API)
# hcp: 负=主队让, 正=主队受让 (外围初盘口径)
# sporttery_hcp: 竞彩实盘让球
# ═══════════════════════════════════════════════════════════

MATCHES_BACKTEST = [
    # ── 6/26 五场 (方案D验证过) ──
    {
        'date': '6/26', 'id': '26A',
        'home': '厄瓜多尔', 'away': '德国',
        'odds_h': 4.20, 'odds_d': 3.50, 'odds_a': 1.85,
        'hcp': +1.0, 'ou': 2.5, 'sporttery_hcp': +1.0,
        'actual': '2-1', 'actual_1x2': 'H',
        'actual_hcp_result': '让胜',  # 厄瓜多尔受让1球+实际赢=让胜
        'actual_ou_result': '大球',   # 3球>2.5
    },
    {
        'date': '6/26', 'id': '26B',
        'home': '库拉索', 'away': '科特迪瓦',
        'odds_h': 3.60, 'odds_d': 3.30, 'odds_a': 2.00,
        'hcp': +2.0, 'ou': 2.5, 'sporttery_hcp': +2.0,
        'actual': '0-2', 'actual_1x2': 'A',
        'actual_hcp_result': '让平',  # 库拉索受让2球, 0-2净负2=走水
        'actual_ou_result': '小球',   # 2球<2.5
    },
    {
        'date': '6/26', 'id': '26C',
        'home': '突尼斯', 'away': '荷兰',
        'odds_h': 5.50, 'odds_d': 3.80, 'odds_a': 1.55,
        'hcp': +2.0, 'ou': 2.5, 'sporttery_hcp': +2.0,
        'actual': '1-3', 'actual_1x2': 'A',
        'actual_hcp_result': '让平',  # 突尼斯受让2球, 1-3净负2=走水
        'actual_ou_result': '大球',   # 4球>2.5
    },
    {
        'date': '6/26', 'id': '26D',
        'home': '巴拉圭', 'away': '澳大利亚',
        'odds_h': 2.40, 'odds_d': 3.10, 'odds_a': 3.00,
        'hcp': 0.0, 'ou': 2.0, 'sporttery_hcp': 0.0,
        'actual': '0-0', 'actual_1x2': 'D',
        'actual_hcp_result': '平',
        'actual_ou_result': '小球',   # 0球<2.0
    },
    {
        'date': '6/26', 'id': '26E',
        'home': '土耳其', 'away': '美国',
        'odds_h': 2.70, 'odds_d': 3.20, 'odds_a': 2.55,
        'hcp': 0.0, 'ou': 2.25, 'sporttery_hcp': -1.0,  # 竞彩让1球
        'actual': '2-2', 'actual_1x2': 'D',
        'actual_hcp_result': '让负',  # 竞彩让1球, 2-2平=让负
        'actual_ou_result': '大球',   # 4球>2.25
    },
    # ── 6/27 六场 (2场已完赛 + 4场待定) ──
    {
        'date': '6/27', 'id': '27A',
        'home': '挪威', 'away': '法国',
        'odds_h': 4.05, 'odds_d': 3.55, 'odds_a': 1.80,
        'hcp': +0.5, 'ou': 2.5, 'sporttery_hcp': +1.0,
        'r3_rotation': True,
        'home_formation': '4-1-2-3', 'away_formation': '4-2-3-1',
        'home_full_strength': False, 'away_full_strength': True,
        'home_missing_stars': '哈兰德,厄德高',
        'actual': '1-4', 'actual_1x2': 'A',
        'actual_hcp_result': '让负',  # 挪威受让0.5球, 1-4净负3=让负 (外围口径)
        'actual_ou_result': '大球',   # 5球>2.5
    },
    {
        'date': '6/27', 'id': '27B',
        'home': '塞内加尔', 'away': '伊拉克',
        'odds_h': 1.40, 'odds_d': 4.40, 'odds_a': 7.00,
        'hcp': -1.25, 'ou': 2.5, 'sporttery_hcp': -2.0,
        'r3_rotation': True,
        'actual': '5-0', 'actual_1x2': 'H',
        'actual_hcp_result': '让胜',  # 塞内加尔让1.25球, 5-0净胜5=让胜 (外围); 竞彩让2球也是让胜
        'actual_ou_result': '大球',   # 5球>2.5
    },
    {
        'date': '6/27', 'id': '27C',
        'home': '佛得角共和国', 'away': '沙特阿拉伯',
        'odds_h': 2.47, 'odds_d': 3.35, 'odds_a': 2.62,
        'hcp': 0.0, 'ou': 2.25, 'sporttery_hcp': -1.0,
        'actual': None,  # 待定
    },
    {
        'date': '6/27', 'id': '27D',
        'home': '乌拉圭', 'away': '西班牙',
        'odds_h': 4.70, 'odds_d': 3.90, 'odds_a': 1.63,
        'hcp': +0.75, 'ou': 2.5, 'sporttery_hcp': +1.0,
        'r3_rotation': True,
        'actual': None,
    },
    {
        'date': '6/27', 'id': '27E',
        'home': '埃及', 'away': '伊朗',
        'odds_h': 2.16, 'odds_d': 3.00, 'odds_a': 3.40,
        'hcp': -0.25, 'ou': 2.0, 'sporttery_hcp': -1.0,
        'actual': None,
    },
    {
        'date': '6/27', 'id': '27F',
        'home': '新西兰', 'away': '比利时',
        'odds_h': 9.00, 'odds_d': 5.20, 'odds_a': 1.28,
        'hcp': +1.5, 'ou': 2.5, 'sporttery_hcp': +2.0,
        'r3_rotation': True,
        'actual': None,
    },
]

def _parse_score(s: str):
    """'2-1' → (2, 1)"""
    if not s or '-' not in s:
        return None
    try:
        h, a = s.split('-')
        return int(h), int(a)
    except Exception:
        return None

def _eval_prediction(result: dict, m: dict) -> dict:
    """评估单场预测 vs 实际"""
    if m.get('actual') is None:
        return {'status': 'pending', 'note': '比赛未完赛'}

    actual_score = _parse_score(m['actual'])
    if not actual_score:
        return {'status': 'error', 'note': f'比分解析失败: {m["actual"]}'}

    ah, aa = actual_score
    actual_1x2 = m['actual_1x2']
    actual_total = ah + aa

    fv = result.get('final_verdict', {})
    pred_primary = fv.get('primary', '?')
    pred_secondary = fv.get('secondary', '?')
    pred_score = fv.get('best_score', '?')

    # 1X2 方向
    pred_1x2_map = {'胜': 'H', '平': 'D', '负': 'A',
                    '让胜': None, '让平': None, '让负': None,  # 让球结果不直接映射1X2
                    '让胜+让平': None, '让平+让负': None}
    pred_1x2 = pred_1x2_map.get(pred_primary)
    dir_match = (pred_1x2 == actual_1x2) if pred_1x2 else None

    # 比分命中
    pred_score_tuple = _parse_score(pred_score) if isinstance(pred_score, str) else None
    score_exact = (pred_score_tuple == (ah, aa)) if pred_score_tuple else False
    score_in_top3 = False
    if pred_score_tuple:
        alt = fv.get('alt_scores', [])
        for s in alt:
            st = _parse_score(s) if isinstance(s, str) else None
            if st == (ah, aa):
                score_in_top3 = True
                break

    # OU 方向 (从 actual_total vs ou_line 推断 + 链1 verdict)
    ou_link = result.get('chains', {}).get('OU_linkage', {})
    ou_verdict = ou_link.get('verdict', '')
    ou_law = ou_link.get('law', '')
    # 解析 "让1球冷门区(OU2.0)" 等格式 — 提取 OU 提示
    ou_pred_over = False
    ou_pred_under = False
    if '小球' in ou_verdict or '小' in ou_law or 'honest_low' in ou_law:
        ou_pred_under = True
    elif '大球' in ou_verdict or '大' in ou_law:
        ou_pred_over = True
    elif '小球' in ou_law or 'small' in ou_law:
        ou_pred_under = True
    elif '大球' in ou_law or 'large' in ou_law:
        ou_pred_over = True
    actual_ou_over = actual_total > m['ou']
    ou_match = None
    if ou_pred_over:
        ou_match = actual_ou_over
    elif ou_pred_under:
        ou_match = not actual_ou_over

    # 让球结果 (用竞彩口径)
    hcp_result_match = None
    if m.get('actual_hcp_result'):
        # 简化: 看预测主选是否含让胜/让平/让负
        for k in ['让胜', '让平', '让负']:
            if k in pred_primary and k in m['actual_hcp_result']:
                hcp_result_match = True
                break
            if k in pred_primary and k not in m['actual_hcp_result']:
                hcp_result_match = False
                break

    return {
        'status': 'done',
        'actual_score': m['actual'],
        'actual_1x2': actual_1x2,
        'actual_total': actual_total,
        'pred_primary': pred_primary,
        'pred_secondary': pred_secondary,
        'pred_score': pred_score,
        'direction_match': dir_match,
        'score_exact': score_exact,
        'score_in_top3': score_in_top3,
        'ou_match': ou_match,
        'hcp_result_match': hcp_result_match,
    }

def main():
    print("=" * 80)
    print("  FootballAI v4.9 全链路回测 — 6/26 + 6/27 (11场)")
    print("  验证: OU联动 + 让球冷门律 + 大小球 + D-Gate + TaoGe + 模型概率")
    print("=" * 80)

    pipeline = FullLinkagePipeline()
    results = []
    evals = []

    for m in MATCHES_BACKTEST:
        print(f"\n{'─' * 80}")
        print(f"  [{m['id']}] {m['date']} · {m['home']} vs {m['away']}  "
              f"| 1X2={m['odds_h']}/{m['odds_d']}/{m['odds_a']} "
              f"| hcp={m['hcp']} ou={m['ou']} "
              f"| sporttery_hcp={m.get('sporttery_hcp', '?')}")
        if m.get('actual'):
            print(f"  实际: {m['actual']} ({m['actual_1x2']}) | "
                  f"让球={m['actual_hcp_result']} | OU={'大' if sum(int(x) for x in m['actual'].split('-')) > m['ou'] else '小'}")

        # 构建 MatchInput
        kwargs = {
            'home': m['home'], 'away': m['away'],
            'odds_h': m['odds_h'], 'odds_d': m['odds_d'], 'odds_a': m['odds_a'],
            'hcp': m['hcp'], 'ou_line': m['ou'],
            'r3_rotation': m.get('r3_rotation', False),
            'sporttery_hcp': m.get('sporttery_hcp'),
        }
        if m.get('home_formation'):
            kwargs['home_formation'] = m['home_formation']
            kwargs['away_formation'] = m['away_formation']
            kwargs['home_full_strength'] = m.get('home_full_strength', True)
            kwargs['away_full_strength'] = m.get('away_full_strength', True)
            if m.get('home_missing_stars'):
                kwargs['home_missing_stars'] = m['home_missing_stars']

        try:
            match_input = MatchInput(**kwargs)
            result = pipeline.predict(match_input)
            results.append(result)

            fv = result['final_verdict']
            print(f"  预测: 主选={fv['primary']} 次选={fv['secondary']} "
                  f"| 比分={fv['best_score']} (备选{fv.get('alt_scores', [])}) "
                  f"| conf={fv['confidence']:.2f}")
            if fv.get('short_circuit'):
                print(f"  ⚡ Priority Gate 短路: {fv.get('short_circuit_reason', '')}")
            if fv.get('massacre_warning'):
                print(f"  🔥 屠杀预警触发")

            # 评估
            ev = _eval_prediction(result, m)
            evals.append({'id': m['id'], 'match': f"{m['home']} vs {m['away']}", **ev})
            if ev['status'] == 'done':
                marks = []
                if ev.get('direction_match') is not None:
                    marks.append(f"方向{'✅' if ev['direction_match'] else '❌'}")
                if ev.get('score_exact'):
                    marks.append("比分精确✅")
                elif ev.get('score_in_top3'):
                    marks.append("比分Top3✅")
                else:
                    marks.append("比分❌")
                if ev.get('ou_match') is not None:
                    marks.append(f"OU{'✅' if ev['ou_match'] else '❌'}")
                if ev.get('hcp_result_match') is not None:
                    marks.append(f"让球{'✅' if ev['hcp_result_match'] else '❌'}")
                print(f"  评估: {' | '.join(marks)}")
        except Exception as e:
            import traceback
            print(f"  ❌ ERROR: {e}")
            traceback.print_exc()
            evals.append({'id': m['id'], 'match': f"{m['home']} vs {m['away']}",
                          'status': 'error', 'note': str(e)})

    # ═══ 汇总 ═══
    print(f"\n\n{'=' * 80}")
    print(f"  📊 全链路回测汇总")
    print(f"{'=' * 80}")

    done = [e for e in evals if e.get('status') == 'done']
    pending = [e for e in evals if e.get('status') == 'pending']
    errors = [e for e in evals if e.get('status') == 'error']

    print(f"\n  样本: {len(done)} 已完赛 / {len(pending)} 待定 / {len(errors)} 错误")

    if done:
        # 方向准确率
        dir_results = [e for e in done if e.get('direction_match') is not None]
        dir_correct = sum(1 for e in dir_results if e['direction_match'])
        print(f"\n  1X2方向: {dir_correct}/{len(dir_results)} = "
              f"{dir_correct/len(dir_results)*100:.1f}%" if dir_results else "")

        # 比分精确命中率
        score_exact_count = sum(1 for e in done if e.get('score_exact'))
        print(f"  比分精确命中: {score_exact_count}/{len(done)} = "
              f"{score_exact_count/len(done)*100:.1f}%")

        # 比分Top3命中率
        score_top3_count = sum(1 for e in done if e.get('score_exact') or e.get('score_in_top3'))
        print(f"  比分Top3命中: {score_top3_count}/{len(done)} = "
              f"{score_top3_count/len(done)*100:.1f}%")

        # OU准确率
        ou_results = [e for e in done if e.get('ou_match') is not None]
        if ou_results:
            ou_correct = sum(1 for e in ou_results if e['ou_match'])
            print(f"  OU方向: {ou_correct}/{len(ou_results)} = "
                  f"{ou_correct/len(ou_results)*100:.1f}%")

        # 让球结果准确率
        hcp_results = [e for e in done if e.get('hcp_result_match') is not None]
        if hcp_results:
            hcp_correct = sum(1 for e in hcp_results if e['hcp_result_match'])
            print(f"  让球结果: {hcp_correct}/{len(hcp_results)} = "
                  f"{hcp_correct/len(hcp_results)*100:.1f}%")

        # 逐场明细
        print(f"\n  ── 逐场明细 ──")
        print(f"  {'ID':<5}{'比赛':<25}{'实际':<8}{'预测主选':<12}{'比分':<8}{'方向':<6}{'比分':<8}{'OU':<6}{'让球':<6}")
        for e in done:
            dir_m = '✅' if e.get('direction_match') else ('❌' if e.get('direction_match') is not None else '-')
            sc_m = '✅' if e.get('score_exact') else ('~' if e.get('score_in_top3') else '❌')
            ou_m = '✅' if e.get('ou_match') else ('❌' if e.get('ou_match') is not None else '-')
            hcp_m = '✅' if e.get('hcp_result_match') else ('❌' if e.get('hcp_result_match') is not None else '-')
            print(f"  {e['id']:<5}{e['match'][:24]:<25}{e.get('actual_score', '-'):<8}"
                  f"{e.get('pred_primary', '-')[:11]:<12}{e.get('pred_score', '-'):<8}"
                  f"{dir_m:<6}{sc_m:<8}{ou_m:<6}{hcp_m:<6}")

    # 保存JSON
    out = ARCH_ROOT / "reports" / f"full_linkage_backtest_626_627_{__import__('datetime').datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({
            'matches': MATCHES_BACKTEST,
            'evaluations': evals,
            'summary': {
                'total': len(evals),
                'done': len(done),
                'pending': len(pending),
                'errors': len(errors),
                'direction_accuracy': f"{sum(1 for e in done if e.get('direction_match'))}/{len([e for e in done if e.get('direction_match') is not None])}",
                'score_exact': sum(1 for e in done if e.get('score_exact')),
                'score_top3': sum(1 for e in done if e.get('score_exact') or e.get('score_in_top3')),
            }
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  结果已保存: {out}")

if __name__ == '__main__':
    main()
