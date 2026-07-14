"""Full Linkage Predictor — 拆分子模块"""
import os, sys, json, math
import logging
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass, field

# Windows GBK 终端输出 emoji/Unicode 会崩溃，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logger = logging.getLogger(__name__)

from ._compat import np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pipeline.predictors.pipeline import *  # noqa: F401, F403

def run_batch_full_linkage(matches=None):
    """批量运行全链路联动分析
    
    Args:
        matches: MatchInput 列表, 或 None 从 API/DB 自动加载
    """
    if matches is None:
        print("⚠️ 未提供赛程数据。请通过 --match 指定单场, 或从 API/DB 加载赛程。")
        print("   示例: python cli.py --match 挪威vs法国 --odds 4.05,3.55,1.80 --hcp 0.5 --ou 2.5")
        return []
    
    pipeline = FullLinkagePipeline()
    results = []

    for m in matches:
        result = pipeline.predict(m)
        results.append(result)
        print()

    # ── 汇总 ──
    print(f"\n{'='*60}")
    print(f"  🎫 全链路联动 • 预测结果汇总")
    print(f"{'='*60}")

    picks = []
    for r in results:
        primary = r['final_verdict']['primary']
        secondary = r['final_verdict']['secondary']
        conf = r['final_verdict']['confidence']

        # 寻找对应赔率
        m = r['match']
        print(f"  {m:30s} → {primary}+{secondary:4s} "
              f"| 比分: {r['final_verdict']['best_score']:<5s} "
              f"| conf={conf:.2f}")

    return results

# ════════════════════════════════════════════════════
# CLI入口
# ════════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='FootballAI 全链路联动预测管道')
    parser.add_argument('--batch', action='store_true', help='批量分析 (需通过 --matches-file 指定赛程JSON文件)')
    parser.add_argument('--match', type=str, help='单场分析 (格式: 主队vs客队)')
    parser.add_argument('--matches-file', type=str, help='批量赛程JSON文件路径')
    parser.add_argument('--hcp', type=float, default=0, help='让球')
    parser.add_argument('--ou', type=float, default=2.5, help='大小球盘口')
    parser.add_argument('--odds', type=str, default='2.0,3.5,3.5', help='1X2赔率 h,d,a')
    parser.add_argument('--r3', action='store_true', help='R3轮换标记')
    parser.add_argument('--full-report', action='store_true',
                        help='输出完整4模块报告 (Chain七链矩阵+比分推荐+深度分析)')

    args = parser.parse_args()

    if args.batch:
        matches = None
        if args.matches_file:
            try:
                with open(args.matches_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                matches = []
                for m in data.get('matches', []):
                    matches.append(MatchInput(
                        home=m['home'], away=m['away'],
                        odds_h=m['odds_h'], odds_d=m['odds_d'], odds_a=m['odds_a'],
                        hcp=m.get('hcp', 0), ou_line=m.get('ou_line', 2.5),
                        r3_rotation=m.get('r3_rotation', False),
                        sporttery_hcp=m.get('sporttery_hcp'),
                        home_formation=m.get('home_formation'),
                        away_formation=m.get('away_formation'),
                    ))
            except Exception as e:
                print(f"❌ 加载赛程文件失败: {e}")
                matches = []
        run_batch_full_linkage(matches)
    elif args.match:
        teams = args.match.split('vs')
        oh, od, oa = map(float, args.odds.split(','))
        match = MatchInput(
            home=teams[0], away=teams[1],
            odds_h=oh, odds_d=od, odds_a=oa,
            hcp=args.hcp, ou_line=args.ou,
            r3_rotation=args.r3,
        )
        pipeline = FullLinkagePipeline()
        result = pipeline.predict(match)
        print(f"\n{'='*60}")
        print(json.dumps(result['final_verdict'], ensure_ascii=False, indent=2))

        # v6.0: --full-report 输出4模块完整报告
        if args.full_report:
            from pipeline.predictors.helpers import fault_tolerant_scores, _score_dir
            chains = result['chains']
            form = chains['Form_Analysis']
            context = chains.get('Context', {})  # v6.0: 链0战意, CLI 直接读取
            ou_lk = chains['OU_linkage']
            live_mv = chains['Live_Movement']
            dgate = chains['D_Gate']
            upred = chains['UnifiedPredictor']
            strategy = result['strategy']
            final = result['final_verdict']
            match_name = result['match']

            # 赔率
            inp = result['input']
            odds = inp['odds']
            oh, od, oa = odds.split('/')
            hcp_val = inp['hcp']
            ou_val = inp['ou']

            print(f"\n{'='*70}")
            print(f"  📋 v6.0 全链路统一报告: {match_name}")
            print(f"{'='*70}")

            # ── 模块A: 赔率原始数据 ──
            print(f"\n  ┌──────────────────────────────────────────────┐")
            print(f"  │  模块A: 赔率原始数据                         │")
            print(f"  ├──────────┬──────────┬──────────┬──────────────┤")
            print(f"  │  独赢主   │  独赢平   │  独赢客   │  让球 | 大小  │")
            print(f"  ├──────────┼──────────┼──────────┼──────────────┤")
            print(f"  │  {oh:<7s}  │  {od:<7s}  │  {oa:<7s}  │  {hcp_val:+.2f} | {ou_val}  │")
            print(f"  └──────────┴──────────┴──────────┴──────────────┘")

            # ── 模块B: Chain 七链矩阵 ──
            print(f"\n  ┌─────────────────────────────────────────────────────────────────────┐")
            print(f"  │  模块B: Chain 七链联动信号矩阵                                     │")
            print(f"  ├─────────┬─────────────────────────────────┬────────────────────────┤")
            print(f"  │ Chain   │ 信号解读                         │ 强度判定               │")
            print(f"  ├─────────┼─────────────────────────────────┼────────────────────────┤")

            # C-1
            is_downgraded = form.get('data_quality') == 'partial'
            if is_downgraded:
                print(f"  │ C-1战绩 │ {form.get('verdict','[降级] 基于FIFA排名+赔率反推')[:40]:<40s} │ ☆☆☆ [降级估算] │")
            elif form.get('verdict'):
                c1_strength = '★★☆' if abs(form.get('goal_diff_advantage', 0)) >= 0.8 else '★☆☆'
                print(f"  │ C-1战绩 │ {form.get('verdict','')[:40]:<40s} │ {c1_strength} {'屠杀⚠' if form.get('massacre_warning') else '优势' if abs(form.get('goal_diff_advantage', 0))>=0.8 else '均势'} │")
            else:
                print(f"  │ C-1战绩 │ 无战绩数据                           │ ☆☆☆ 未知  │")

            # C0
            motivations = context.get('notes', [])
            if motivations:
                c0_signal = motivations[0][:40] if motivations else '无情境数据'
                c0_strength = '🔥双MAX' if context.get('survival_clash') else '🟡默契平' if context.get('mutual_benefit_draw') else '🔵标准'
            else:
                c0_signal = '战意情境数据未获取'
                c0_strength = '⭐正常'
            print(f"  │ C0 战意  │ {c0_signal:<40s} │ {c0_strength:<22s} │")

            # C0.5
            lv_signal = live_mv.get('interpretation', '无竞彩数据')[:40]
            lv_grade = live_mv.get('grade', '?')
            lv_trap = live_mv.get('trap_risk', 0)
            if lv_trap > 0.5:
                c05_strength = f'⚠️诱盘{lv_trap:.0%}'
            elif live_mv.get('is_confirming'):
                c05_strength = '✅深确认√'
            elif live_mv.get('is_level_to_deep'):
                c05_strength = '🚨平手→深让'
            else:
                c05_strength = f'{lv_grade}级别'
            print(f"  │ C0.5升盘 │ {lv_signal:<40s} │ {c05_strength:<22s} │")

            # C1
            ou_law = ou_lk.get('law', '?')
            ou_verdict = ou_lk.get('verdict', '?')
            ou_scores = ou_lk.get('scores', [])
            ou_honesty = ou_lk.get('ou_honesty', {})
            c1_signal = f'{ou_law}|{ou_verdict}'[:40]
            c1_strength = ou_honesty.get('note', '?')[:22] if ou_honesty else '?'
            print(f"  │ C1 OU联动│ {c1_signal:<40s} │ {c1_strength:<22s} │")

            # C2
            dg_verdict = dgate.get('verdict', '?')
            dg_draw = dgate.get('draw_prob', 0)
            dg_signals = ', '.join(dgate.get('signals', []))[:30]
            c2_signal = f'{dg_verdict} D-Prob={dg_draw:.2f}'[:40]
            if dg_draw > 0.3:
                c2_strength = f'△△平局风险{dg_draw:.0%}'
            elif 'upset_warning' in dg_signals or 'draw_alert' in dg_signals:
                c2_strength = '△冷门/平局预警'
            elif 'ignore_draw' in dg_signals:
                c2_strength = '✅忽略平局'
            else:
                c2_strength = '🟢正常'
            print(f"  │ C2 D-Gate │ {c2_signal:<40s} │ {c2_strength:<22s} │")

            # C3
            up_v = upred.get('verdict', '?')
            up_dp = upred.get('draw_prob', 0)
            up_sig = ', '.join(upred.get('signals', []))[:25]
            # 计算 E[goals] from scores
            e_goals = 0
            if ou_scores:
                try:
                    totals = [int(s.split('-')[0]) + int(s.split('-')[1]) for s in ou_scores[:3]]
                    e_goals = sum(totals) / len(totals) if totals else 0
                except Exception:
                    e_goals = ou_val
            c3_signal = f'{up_v} D-Prob={up_dp:.2f}'[:40]
            print(f"  │ C3 λ-Pois│ {c3_signal:<40s} │ E[goals]={e_goals:.1f}球    │")

            # C3.5
            if form.get('massacre_warning'):
                c35_signal = 'Dixon-Coles屠杀交叉重标定'[:40]
                c35_strength = f'λ_s={final.get("lambda_strong", "?"):.2f} λ_w={final.get("lambda_weak", "?"):.2f}'
            elif upred.get('verdict', '?') != '?' and e_goals > 0:
                lam_h = upred.get('draw_prob', ou_val * 0.5) if False else (ou_val * 0.5)
                # Read from metadata if available
                lam_h_meta = upred.get('metadata', {}).get('lambda_home', 0) if isinstance(upred, dict) else 0
                lam_a_meta = upred.get('metadata', {}).get('lambda_away', 0) if isinstance(upred, dict) else 0
                if lam_h_meta:
                    lam_show = f'λ_h={lam_h_meta:.2f} λ_a={lam_a_meta:.2f}'
                else:
                    # v6.0: 标注反推来源, 降级时加精度标注
                    if form.get('data_quality') == 'partial':
                        lam_show = '⚠λ反推(低精度)'
                    else:
                        lam_show = 'λ(赔率反推)'
                c35_signal = 'Dixon-Coles标准交叉'[:40]
                c35_strength = lam_show[:22]
            else:
                c35_signal = 'Dixon-Coles交叉(短路跳过)'[:40]
                c35_strength = '⚡短路段'
            print(f"  │ C3.5 DCλ  │ {c35_signal:<40s} │ {c35_strength:<22s} │")

            print(f"  └─────────┴─────────────────────────────────┴────────────────────────┘")

            # ── 模块C: 比分推荐 ──
            target_dir = {'H': 'H', 'A': 'A', 'D': 'D'}.get(strategy.get('primary', '胜')[:1] if len(strategy.get('primary','')) > 0 else 'D', 'D')
            if strategy['primary'] in ('主胜', '胜'):
                target_dir = 'H'
            elif strategy['primary'] in ('客胜', '负'):
                target_dir = 'A'
            else:
                target_dir = 'D'

            target_ou = strategy.get('ou_dir', 'O' if ou_val >= 2.5 else 'U')
            ou_flag = 'O' if target_ou in ('大', '大球', 'O') else 'U'

            try:
                ranked_scores = fault_tolerant_scores(target_dir, ou_flag, ou_val, top_n=3)
            except Exception:
                ranked_scores = ['?-?']

            print(f"\n  ┌─────────────────────────────────────────────────────────────────────┐")
            print(f"  │  模块C: 比分推荐 (四维交叉×hcp平手模板)                           │")
            print(f"  ├─────────┬──────────┬──────────┬────────────────────────────────────┤")
            print(f"  │ 优先级   │ 比分      │ 波胆赔率  │ 推荐逻辑                           │")
            print(f"  ├─────────┼──────────┼──────────┼────────────────────────────────────┤")
            for i, sc in enumerate(ranked_scores[:3]):
                tag = ['⭐首推', '☆次选', '△对冲'][i]
                reason = ['全场最低波胆', '四维交叉次优', '平局保护层'][i]
                print(f"  │ {tag:<7s} │ {sc:<8s} │ ??? @??  │ {reason:<34s} │")
            print(f"  └─────────┴──────────┴──────────┴────────────────────────────────────┘")

            # ── 模块D: 深度分析 ──
            print(f"\n  ┌─────────────────────────────────────────────────────────────────────┐")
            print(f"  │  模块D: 深度分析                                                   │")
            print(f"  ├─────────────────────────────────────────────────────────────────────┤")
            if form.get('data_quality') == 'partial':
                # v6.0: 明确标记降级模式 + 原因
                downgrade_reason = form.get('verdict', '基于FIFA排名+赔率反推')
                print(f"  │  > [降级模式] 战绩API不可用, {downgrade_reason[:50]}。置信度已自动折让。")
            elif form.get('verdict'):
                print(f"  │  > D-Gate核心发现: {dgate.get('verdict','?')}, D-Prob={dgate.get('draw_prob',0):.2%}  ({', '.join(dgate.get('signals',['?']))})")
            else:
                print(f"  │  > [数据缺失] 战绩数据不可用。")
            if form.get('goal_diff_advantage', 0) != 0:
                gap_str = f"净胜差{form.get('goal_diff_advantage', 0):+.2f}/场"
                print(f"  │  > 战绩分析: {gap_str} | 强度={form.get('strength_gap', '?')}")
            print(f"  │  > 策略: {strategy['strategy']} → {strategy['primary']}+{strategy['secondary']}")
            print(f"  │  > 最佳比分: {strategy['best_score']} | 备选: {strategy['alt_scores']}")
            print(f"  │  > 置信度: {strategy['confidence']:.0%}")
            print(f"  └─────────────────────────────────────────────────────────────────────┘")
    else:
        print("用法: python full_linkage_predictor.py --batch  或  --match 挪威vs法国 --odds 4.05,3.55,1.80 --hcp 0.5 --ou 2.25")
