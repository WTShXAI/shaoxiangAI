"""Full Linkage Predictor — 拆分子模块"""
import os, sys, json, math
import logging
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None
    _HAS_NUMPY = False
    class _FakeArray:
        def __init__(self, data):
            self.data = list(data)
        def copy(self): return _FakeArray(self.data)
        def __iter__(self): return iter(self.data)
        def __getitem__(self, i): return self.data[i]
        def __len__(self): return len(self.data)
        def sum(self): return sum(self.data)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pipeline.predictors.data_classes import *  # noqa: F401, F403
from pipeline.predictors.ou_linkage import *  # noqa: F401, F403
from pipeline.predictors.dgate_layer import *  # noqa: F401, F403
from pipeline.predictors.model_layer import *  # noqa: F401, F403
from pipeline.predictors.helpers import (  # noqa: F401, F403
    _constrain_ou_to_line, _vote_three_paths, _half_time_adjust,
)
from pipeline.predictors.ou_linkage import OULinkageEngine
from pipeline.predictors.dgate_layer import DGateLayer
from pipeline.predictors.model_layer import ModelLayer
from pipeline.predictors.taoge_strategy import TaoGeStrategy
from pipeline.predictors.data_classes import MatchInput, ChainResult
from pipeline.predictors.live_movement import LiveMovementSignal

class FullLinkagePipeline:
    """
    全链路联动预测管道
    串联: D-Gate → UnifiedPredictor → OU联动 → TaoGe策略
    """

    def __init__(self):
        self.dgate_layer = DGateLayer()
        self.model_layer = ModelLayer()
        self.linkage_engine = OULinkageEngine()
        self.strategy_layer = TaoGeStrategy()
        self.context_analyzer = None  # 惰性加载
        self.form_fetcher = None      # Chain -1: 惰性加载

    def predict(self, match: MatchInput) -> Dict[str, Any]:
        """主预测入口: 全链路联动 (7层: Chain -1,0,0.5,1,2,3,4)"""

        print(f"\n{'='*60}")
        print(f"  [FULL] {match.home} vs {match.away}")
        print(f"  HCP={match.hcp:+} | OU={match.ou_line} | 1X2={match.odds_h}/{match.odds_d}/{match.odds_a}")
        if match.home_formation or match.away_formation:
            fmt_info = []
            if match.home_formation:
                fmt_info.append(f"{match.home}={match.home_formation}")
            if match.away_formation:
                fmt_info.append(f"{match.away}={match.away_formation}")
            if match.home_missing_stars:
                fmt_info.append(f"{match.home}缺:{match.home_missing_stars}")
            if match.away_missing_stars:
                fmt_info.append(f"{match.away}缺:{match.away_missing_stars}")
            print(f"  阵容: {' | '.join(fmt_info)}")
        print(f"{'='*60}")

        # ── 链-1: 球队近10场真实战绩分析 (优先级最高!) ──
        form_result = None
        try:
            if self.form_fetcher is None:
                from pipeline.team_form_fetcher import TeamFormFetcher
                self.form_fetcher = TeamFormFetcher()
            form_result = self.form_fetcher.analyze_match_input(match)
            print(f"\n  [链-1] 球队近10场真实战绩分析...")
            print(f"    → {match.home}: 近{form_result.home.matches}场 "
                  f"进{form_result.home.avg_gf}/失{form_result.home.avg_ga} "
                  f"净{form_result.home.goal_diff:+.2f} "
                  f"动量{form_result.home.momentum:.2f}")
            print(f"    → {match.away}: 近{form_result.away.matches}场 "
                  f"进{form_result.away.avg_gf}/失{form_result.away.avg_ga} "
                  f"净{form_result.away.goal_diff:+.2f} "
                  f"动量{form_result.away.momentum:.2f}")
            print(f"    → 净胜差: {form_result.goal_diff_advantage:+.2f}/场 "
                  f"| 实力差距: {form_result.strength_gap}")
            if form_result.massacre_warning:
                print(f"    🚨 屠杀预警! 比分修正{form_result.score_adjustment:+.1f}球")
                print(f"    → {form_result.verdict}")
            if form_result.verdict and not form_result.massacre_warning:
                print(f"    → {form_result.verdict}")
        except Exception as e:
            print(f"\n  [链-1] 战绩数据暂不可用: {e}")
            form_result = None

        # ═══ P0-1: Priority Gate 短路机制 (费深谋+何执策) ═══
        # v5.11修复: 小样本(≤5场WC数据)抑制 — 3场小组赛数据不足以判定屠杀
        short_circuit = False
        short_circuit_level = 4
        short_circuit_reason = ''
        if form_result and form_result.is_valid:
            abs_gap = abs(form_result.goal_diff_advantage)
            min_samples = min(form_result.home.matches, form_result.away.matches)
            small_sample = min_samples <= 5  # WC小组赛仅3场数据

            if small_sample:
                # 小样本下仅最极端的差距才短路(≥3球)
                if abs_gap >= 3.0:
                    short_circuit = True
                    short_circuit_level = -1
                    short_circuit_reason = f'净胜差≥3(小样本)'
                    print(f"\n  ⚡ [Priority Gate] 净胜差≥3球({abs_gap:.1f}, 仅{min_samples}场样本) → 阻断")
                    print(f"      方向: {'跟主' if form_result.goal_diff_advantage > 0 else '跟客'}")
                elif form_result.massacre_warning and abs_gap >= 2.5:
                    short_circuit = True
                    short_circuit_level = -1
                    short_circuit_reason = f'屠杀预警(小样本高阈值)'
                    print(f"\n  ⚡ [Priority Gate] 屠杀预警({abs_gap:.1f}, 仅{min_samples}场) → 阻断")
                    print(f"      强队: {form_result.home.team if form_result.goal_diff_advantage > 0 else form_result.away.team}")
                else:
                    print(f"\n  [Priority Gate] 小样本({min_samples}场)抑制 — 阈值不足, 不短路")
                    # ═══ v5.16: MD1小样本下屠杀警告降级 ═══
                    # MD1数据全部来自友谊赛/预选赛, 场均GA不可靠
                    # MD2+有WC真实数据, 3场足够判断屠杀趋势
                    matchday_val = getattr(match, 'matchday', 3)
                    if matchday_val <= 1 and form_result and form_result.massacre_warning:
                        form_result.massacre_warning = False
                        print(f"      🟡 v5.16: MD1屠杀警告降级 (gap={abs_gap:.1f}), 友谊赛数据不强制方向")
            else:
                # 正常样本(≥6场): 使用标准阈值
                if abs_gap >= 3.0:
                    short_circuit = True
                    short_circuit_level = -1
                    short_circuit_reason = f'净胜差≥3'
                    print(f"\n  ⚡ [Priority Gate] 净胜差≥3球 ({abs_gap:.1f}) → 阻断Chain 1-3")
                    print(f"      方向锁定: {'强队方向(跟主)' if form_result.goal_diff_advantage > 0 else '强队方向(跟客)'}")
                elif form_result.massacre_warning:
                    short_circuit = True
                    short_circuit_level = -1
                    short_circuit_reason = '屠杀预警'
                    print(f"\n  ⚡ [Priority Gate] 屠杀预警 → 阻断Chain 1-3")
                    print(f"      强队: {form_result.home.team if form_result.goal_diff_advantage > 0 else form_result.away.team}")

            # ═══ v5.24: 淘汰赛屠杀降级 ═══
            # 案例: 德国vs巴拉圭(1/16决赛) — 德国小组赛7-1库拉索→屠杀预警
            #       实际1-1点球负。巴拉圭小组赛2场零封(0-0澳洲/1-0土耳其)→防守强
            # 淘汰赛下: 小组赛屠杀数据不代表淘汰赛表现, 弱队摆大巴+淘汰赛保守
            # 规则: 淘汰赛+弱队防守好(GA<1.2) → 屠杀降级, 允许D-Gate平局信号
            stage_val = getattr(match, 'stage', 'group')
            if stage_val == 'knockout' and short_circuit and short_circuit_level == -1:
                # 确定弱队
                if form_result.goal_diff_advantage > 0:
                    weak_ga = form_result.away.avg_ga
                    weak_team = form_result.away.team
                else:
                    weak_ga = form_result.home.avg_ga
                    weak_team = form_result.home.team

                if weak_ga < 1.2:
                    # 弱队防守好 → 屠杀降级
                    short_circuit = False
                    short_circuit_level = 4
                    short_circuit_reason = ''
                    if form_result.massacre_warning:
                        form_result.massacre_warning = False
                    print(f"\n  🟡 v5.24: 淘汰赛屠杀降级 — {weak_team}防守强(GA={weak_ga:.1f}<1.2)")
                    print(f"      小组赛屠杀数据不代表淘汰赛表现, 允许D-Gate平局信号")
                elif weak_ga < 1.5 and abs_gap < 3.0:
                    # 弱队防守中等 + 差距不极端 → 降级但不完全清除
                    short_circuit = False
                    short_circuit_level = 4
                    short_circuit_reason = ''
                    if form_result.massacre_warning:
                        form_result.massacre_warning = False
                    print(f"\n  🟡 v5.24: 淘汰赛屠杀部分降级 — {weak_team}GA={weak_ga:.1f}+gap={abs_gap:.1f}")
                    print(f"      差距不极端, 降级为常规分析")

        # ── 链0: 战意/情境分析 (动机层) ──
        context_adj = {}
        try:
            if self.context_analyzer is None:
                from pipeline.match_context_analyzer import MatchContextAnalyzer
                self.context_analyzer = MatchContextAnalyzer
            context_adj = self.context_analyzer.get_adjustment(
                match.home, match.away, 
                matchday=getattr(match, 'matchday', 3),
                stage=getattr(match, 'stage', 'group')
            )
            print(f"\n  [链0] 战意/情境分析...")
            for note in context_adj.get('notes', [])[:4]:
                print(f"    → {note}")
            if context_adj.get('mutual_benefit_draw'):
                print(f"    ⚠️ 默契平局场景! 动机倍率={context_adj['motivation_mult']:.2f}")
            if context_adj.get('survival_clash'):
                print(f"    🔥 双求生战! 比赛开放, 进攻偏移={context_adj['offensive_bias']:+.2f}")
        except Exception as e:
            logger.warning("情境分析失败(不阻塞): %s", e)

        # ── 链0.5: 临场升盘信号 (Live Movement) ──
        print("\n  [链0.5] 临场升盘分析...")
        live_movement = LiveMovementSignal.analyze(match)
        if live_movement['signal'] != 'no_data':
            arrow = "→" * min(int(abs(live_movement['depth_diff']) * 2) + 1, 3)
            print(f"    → 外围: {live_movement['offshore_display']} | 竞彩: {live_movement['sporttery_display']} {arrow}")
            print(f"    → 深度差: △{live_movement['depth_diff']:+.2f}球 | 等级: {live_movement['grade']} | 诱盘风险: {live_movement['trap_risk']:.0%}")
            print(f"    → 解读: {live_movement['interpretation']}")
            if live_movement.get('is_level_to_deep'):
                print(f"    🚨 特殊: 平手→深让! 全场最危险信号!")
            if live_movement.get('is_confirming'):
                print(f"    ✅ 特殊: 同向确认! 屠杀/碾压方向可信")
        else:
            print("    → 无竞彩对比数据")

        # ── 链1: OU联动推理 (锚定) ──
        if short_circuit:
            # P0-1: 短路 → OU联动跳过，直接用战绩数据构建基础比分锚
            print("\n  [链1] OU联动推理 [短路]...")
            ou_link = self.linkage_engine.infer(match)  # 仍计算供参考
            print(f"    → ⚡ 短路: 战绩数据优先，OU联动仅供参考")
        else:
            print("\n  [链1] OU联动推理...")
            ou_link = self.linkage_engine.infer(match)
            honesty_info = ou_link.get('ou_honesty', {})
            honesty_tag = f" [{honesty_info.get('grade','?')}]" if honesty_info else ''
            print(f"    → 联动律: {ou_link['law']}{honesty_tag} | 判决: {ou_link['verdict']}")
            print(f"    → OU诚实度: {honesty_info.get('note','?')}" if honesty_info else "")
            print(f"    → 比分锚: {ou_link['scores']}")

        # ── 链2: D-Gate风控 ──
        if short_circuit:
            # P0-1: 短路 → D-Gate跳过，构造pass-through结果
            print("\n  [链2] D-Gate v5.3 风控 [短路]...")
            print(f"    → ⚡ 短路: 战绩阻断，D-Gate不参与方向决策")
            dgate_result = ChainResult(
                chain_name='D-Gate v5.3 [短路段]',
                verdict='?', draw_prob=0.0, confidence=0.0,
                signals=['short_circuit_by_form'], metadata={'dgate_active': False}
            )
        else:
            print("\n  [链2] D-Gate v5.3 风控...")
            dgate_result = self.dgate_layer.assess(match, form_result=form_result)  # P0-3: 传入战绩数据
            print(f"    → 风险: {dgate_result.verdict} | D-Prob: {dgate_result.draw_prob:.3f}")
            if dgate_result.metadata.get('trap_suppressed'):
                print(f"    → ✅ 陷阱被Chain -1战绩压制 (净胜差证实深盘合理性)")
            print(f"    → 信号: {dgate_result.signals}")

        # ── 链3: 模型推理 ──
        if short_circuit:
            # P0-1: 短路 → 模型跳过
            print("\n  [链3] UnifiedPredictor v4.1 [短路]...")
            print(f"    → ⚡ 短路: 战绩阻断，模型不参与方向决策")
            model_result = ChainResult(
                chain_name='UnifiedPredictor v4.1 [短路段]',
                verdict='?', draw_prob=0.0, confidence=0.0,
                signals=['short_circuit_by_form'],
            )
        else:
            print("\n  [链3] UnifiedPredictor v4.1 模型推理...")
            model_result = self.model_layer.assess(match)
            print(f"    → 预测: {model_result.verdict} | D-Prob: {model_result.draw_prob:.3f}")
            print(f"    → 信号: {model_result.signals}")

        # ═══ P0-2: 屠杀λ重标定 ═══
        # 不论是否短路, 屠杀场景都用真实GF/GA覆写λ (方向由短路决定, λ影响比分精度)
        if form_result and form_result.massacre_warning:
            print(f"\n  [链3.5] 屠杀λ重标定...")
            # 获取真实场均数据
            if form_result.goal_diff_advantage > 0:
                real_gf_strong = form_result.home.avg_gf
                real_ga_weak = form_result.away.avg_ga
            else:
                real_gf_strong = form_result.away.avg_gf
                real_ga_weak = form_result.home.avg_ga
            # ═══ v5.19: 动态λ放大系数 + OU约束比分Top-5 ═══
            # 放大系数: GA差距越大 → 屠杀越狠; OU线越高 → 比分越大
            # ═══ v5.22: Dixon-Coles λ重标定 ═══
            # 修正: λ应基于攻防交叉(DC模型), 而非孤立GF/GA
            # λ_strong = (强队GF + 弱队GA)/2 × mult  — 强队进攻 vs 弱队防守
            # λ_weak  = (弱队GF + 强队GA)/2          — 弱队进攻 vs 强队防守
            # 旧版: lam_weak=max(GA_weak,0.8)错误地用弱队失球当λ
            # 案例: 挪威GF=2.67→旧λ=2.33(失球), 新λ=(2.67+0.67)/2=1.67(进攻)
            ga_gap = real_ga_weak
            gap_val = abs(form_result.goal_diff_advantage)
            
            # 获取强弱双方的完整数据
            if form_result.goal_diff_advantage > 0:
                # 主队是强队
                gf_strong = form_result.home.avg_gf
                ga_strong = form_result.home.avg_ga
                gf_weak = form_result.away.avg_gf
                ga_weak = form_result.away.avg_ga
            else:
                gf_strong = form_result.away.avg_gf
                ga_strong = form_result.away.avg_ga
                gf_weak = form_result.home.avg_gf
                ga_weak = form_result.home.avg_ga
            
            # DC交叉λ
            lam_strong_raw = (gf_strong + ga_weak) / 2
            lam_weak_raw = (gf_weak + ga_strong) / 2
            
            # 动态放大: gap越大屠杀越狠
            if gap_val >= 3.5:   mult = 1.5
            elif gap_val >= 2.5: mult = 1.4
            elif gap_val >= 2.0: mult = 1.3
            else:                mult = 1.2
            
            if match.ou_line > 3.5: mult += 0.1
            elif match.ou_line < 2.0: mult -= 0.1
            
            lam_strong = max(lam_strong_raw, 1.5) * max(mult, 1.0)
            lam_weak = max(lam_weak_raw, 0.5)   # 弱队至少0.5, 但不再取GA_weak
            # 确定强队在哪一侧 (用于正确分配λ)
            strong_is_home = form_result.goal_diff_advantage > 0
            print(f"    → Dixon-Coles: 强队(GF={gf_strong:.1f})×弱队(GA={ga_weak:.1f}) 弱队(GF={gf_weak:.1f})×强队(GA={ga_strong:.1f})")
            print(f"    → 重标定λ(×{mult:.1f}): strong={lam_strong:.2f} weak={lam_weak:.2f}")
            # Poisson比分Top-5
            massacre_scores = []
            max_score = 7
            score_probs = []
            for h in range(max_score + 1):
                for a in range(max_score + 1):
                    try:
                        # 关键: λ必须匹配正确方向 — 强队λ给强队侧
                        if strong_is_home:
                            p_h = (lam_strong ** h) * math.exp(-lam_strong) / max(math.factorial(h), 1)
                            p_a = (lam_weak ** a) * math.exp(-lam_weak) / max(math.factorial(a), 1)
                        else:
                            p_h = (lam_weak ** h) * math.exp(-lam_weak) / max(math.factorial(h), 1)
                            p_a = (lam_strong ** a) * math.exp(-lam_strong) / max(math.factorial(a), 1)
                        score_probs.append((p_h * p_a, f"{h}-{a}"))
                    except (OverflowError, ValueError):
                        continue
            score_probs.sort(reverse=True, key=lambda x: x[0])
            
            # ═══ v5.19: OU约束Top-5 ═══
            # 屠杀场景: 仅用OU做宽松锚(容忍度=min(OU*1.5, 4)), 避免过度过滤
            ou_line = match.ou_line
            ou_tolerance = min(max(3.0, ou_line * 1.5), 5.0)
            filtered = []
            for prob, s in score_probs:
                try:
                    sh, sa = map(int, s.split('-'))
                    total = sh + sa
                    if abs(total - ou_line) <= ou_tolerance:
                        filtered.append((prob, s))
                except:
                    filtered.append((prob, s))
            
            if len(filtered) >= 3:
                massacre_scores = [s for _, s in filtered[:5]]
            else:
                massacre_scores = [s for _, s in score_probs[:5]]
            
            print(f"    → 重标定比分(OU={ou_line}约束): {massacre_scores}")
            # 覆写OU联动比分锚
            ou_link['scores'] = massacre_scores
            ou_link['massacre_rescaled'] = True
            model_result.metadata['lambda_rescaled'] = True
            model_result.metadata['lambda_strong'] = round(lam_strong, 2)
            model_result.metadata['lambda_weak'] = round(lam_weak, 2)

        # ═══ P0-5: OU盘口约束总进球 (何执策 · 解决5场OU误判) ═══
        # 原理: 外围OU盘口反映市场对总进球的共识, 偏差>1.5球时应修正
        ou_constrained = _constrain_ou_to_line(ou_link, match, form_result, hasattr(self, '_ou_constrained') and self._ou_constrained)
        if ou_constrained.get('adjusted'):
            ou_link['scores'] = ou_constrained['scores']
            ou_link['ou_constrained'] = True
            model_result.metadata['ou_constrained'] = True

        # ═══ P0: 三路径对比投票 (v5.7 Agent思维设计) ═══
        if not short_circuit:
            vote_result = _vote_three_paths(
                model_result.verdict, dgate_result.verdict, form_result,
                match.home, match.away, match.hcp, match.ou_line
            )
            print(f"\n  [🗳] 三路径投票...")
            print(f"    → {vote_result['paths']}")
            print(f"    → 裁决: {vote_result['verdict']} | 共识: {vote_result['consensus']} | {vote_result['reason']}")
            if vote_result['similar_match']:
                print(f"    → 历史相似: {vote_result['similar_match']}")
        else:
            vote_result = None

        # ── 链4: TaoGe策略 ──
        print("\n  [链4] TaoGe策略决策...")
        strategy = self.strategy_layer.decide(
            match, ou_link, dgate_result, model_result,
            form_result=form_result,  # Chain -1 战绩数据
            context_adj=context_adj    # 链0战意/积分形势
        )
        print(f"    → 策略: {strategy['strategy']}")
        print(f"    → 选择: {strategy['primary']}+{strategy['secondary']}")
        print(f"    → 最佳比分: {strategy['best_score']}")
        print(f"    → 备选比分: {strategy['alt_scores']}")

        # ── 组装最终报告 ──
        return {
            'match': f'{match.home} vs {match.away}',
            'input': {
                'odds': f'{match.odds_h}/{match.odds_d}/{match.odds_a}',
                'hcp': match.hcp,
                'ou': match.ou_line,
                'r3_rotation': match.r3_rotation,
            },
            'chains': {
                'Form_Analysis': {
                    'home_avg_gf': form_result.home.avg_gf if form_result else 0,
                    'home_avg_ga': form_result.home.avg_ga if form_result else 0,
                    'away_avg_gf': form_result.away.avg_gf if form_result else 0,
                    'away_avg_ga': form_result.away.avg_ga if form_result else 0,
                    'goal_diff_advantage': form_result.goal_diff_advantage if form_result else 0,
                    'strength_gap': form_result.strength_gap if form_result else 'unknown',
                    'massacre_warning': form_result.massacre_warning if form_result else False,
                    'verdict': form_result.verdict if form_result else '',
                },
                'OU_linkage': {
                    'law': ou_link['law'],
                    'verdict': ou_link['verdict'],
                    'scores': ou_link['scores'],
                    'confidence': ou_link['confidence'],
                    'ou_honesty': ou_link.get('ou_honesty', {}),
                    'hcp_class': ou_link.get('hcp_class', 'level'),
                    'hcp_override': ou_link.get('hcp_override', None),
                },
                'Live_Movement': {
                    'signal': live_movement.get('signal', 'no_data'),
                    'depth_diff': live_movement.get('depth_diff', 0),
                    'offshore': live_movement.get('offshore_display', '-'),
                    'sporttery': live_movement.get('sporttery_display', '-'),
                    'grade': live_movement.get('grade', '?'),
                    'trap_risk': live_movement.get('trap_risk', 0),
                    'interpretation': live_movement.get('interpretation', ''),
                },
                'D_Gate': {
                    'verdict': dgate_result.verdict,
                    'draw_prob': round(dgate_result.draw_prob, 3),
                    'signals': dgate_result.signals,
                },
                'UnifiedPredictor': {
                    'verdict': model_result.verdict,
                    'draw_prob': round(model_result.draw_prob, 3),
                    'signals': model_result.signals,
                },
            },
            'strategy': strategy,
            'final_verdict': {
                'primary': strategy['primary'],
                'secondary': strategy['secondary'],
                'best_score': strategy['best_score'],
                'alt_scores': strategy['alt_scores'],
                'confidence': strategy['confidence'],
                'rec_type': strategy.get('rec_type', 'balanced'),
                'rec_markets': strategy.get('rec_markets', ['1X2']),
                'massacre_adjusted': strategy.get('massacre_adjusted', False),
                'massacre_warning': form_result.massacre_warning if form_result else False,
                # P0-1/P0-2 新增元数据
                'short_circuit': short_circuit,
                'short_circuit_reason': f'净胜差≥3' if short_circuit and form_result and abs(form_result.goal_diff_advantage) >= 3.0 else ('屠杀预警' if short_circuit else ''),
                'lambda_rescaled': ou_link.get('massacre_rescaled', False),
                'trap_suppressed': dgate_result.metadata.get('trap_suppressed', False),
                # v5.7: 三路径投票
                'vote': vote_result,
                # v5.7: 链0战意覆盖
                'context_override': strategy.get('context_override'),
                'context_reason': strategy.get('context_reason', ''),
                # v5.7: 半场修正 (null=未提供半场比分, 传入半场后自动激活)
                'half_time': None,
            },
            '_half_time_adjust': lambda ht_h, ht_a: _half_time_adjust(
                ht_h, ht_a, strategy, form_result, match.hcp
            ),
        }

# ════════════════════════════════════════════════════
# 批量管道: 6/27 六场全链路分析
# ════════════════════════════════════════════════════

MATCHES_6_27 = [
    # 格式: MatchInput(主队, 客队, 独赢H/D/A, 让球(外围), OU, r3,
    #                   home_formation=主队阵型, away_formation=客队阵型,
    #                   home_full_strength=主力, away_full_strength=主力,
    #                   home_missing_stars=缺阵, away_missing_stars=缺阵,
    #                   sporttery_hcp=竞彩让球)
    # 让球: 负值=主队让球, 正值=主队受让 (来自外围截图原始赔率)
    # 竞彩让球数据来源: sporttery.cn 实时赔率 (6/27截图)
    MatchInput('挪威', '法国',       4.05, 3.55, 1.80, +0.5,   2.5,   r3_rotation=True,
               home_formation='4-1-2-3', away_formation='4-2-3-1',
               home_full_strength=False, away_full_strength=True,
               home_missing_stars='哈兰德,厄德高',
               sporttery_hcp=+1.0),    # 竞彩[+1]: 挪威受让1球(法国让1球)
    MatchInput('塞内加尔', '伊拉克',  1.40, 4.40, 7.00, -1.25, 2.5,   r3_rotation=True,
               sporttery_hcp=-2.0),    # 竞彩[-2]: 塞内加尔让2球
    MatchInput('佛得角共和国', '沙特阿拉伯', 2.47, 3.35, 2.62, 0.0,   2.25,
               sporttery_hcp=-1.0),    # 竞彩[-1]: 佛得角让1球
    MatchInput('乌拉圭', '西班牙',   4.70, 3.90, 1.63, +0.75, 2.5,   r3_rotation=True,
               sporttery_hcp=+1.0),    # 竞彩[+1]: 乌拉圭受让1球(西班牙让1球)
    MatchInput('埃及', '伊朗',       2.16, 3.00, 3.40, -0.25, 2.0,
               sporttery_hcp=-1.0),    # 竞彩[-1]: 埃及让1球
    MatchInput('新西兰', '比利时',   9.00, 5.20, 1.28, +1.5,  2.5,   r3_rotation=True,
               sporttery_hcp=+2.0),    # 竞彩[+2]: 新西兰受让2球(比利时让2球)
]
