"""Full Linkage Predictor — 拆分子模块"""
import os, sys, json, math
import logging
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

from ._compat import np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pipeline.predictors.data_classes import *  # noqa: F401, F403
from pipeline.predictors.ou_linkage import *  # noqa: F401, F403
# dgate_layer removed 2026-07-06 — D-Gate退化为中性层
class DGateLayerStub:
    def assess(self, match, form_result=None):
        return ChainResult(
            chain_name='D-Gate [已移除]',
            verdict='?',
            draw_prob=0.30,
            confidence=0.0,
            signals=['D-Gate已移除: 中性评估'],
            metadata={'dgate_active': False}
        )
DGateLayer = DGateLayerStub
from pipeline.predictors.model_layer import ModelLayer
from pipeline.predictors.taoge_strategy import TaoGeStrategy
from pipeline.predictors.data_classes import MatchInput, ChainResult
from pipeline.predictors.live_movement import LiveMovementSignal
from pipeline.predictors.consistent_validator import run_consistency_checks
from pipeline.predictors.helpers import _constrain_ou_to_line, _vote_three_paths, _half_time_adjust

# ═══ P0-6: 数据不可用优雅降级 (v6.0 数据清理) ═══
# 当 TeamFormFetcher API 失败时, 不再用 FIFA 排名+赔率反推伪造战绩数据
# form_result 返回 None, 下游用 data_quality='unavailable' 标记
# 系统优雅降级: 跳过依赖战绩的链 (Priority Gate / 屠杀λ), 仅用赔率+模型推理


class FullLinkagePipeline:
    """
    全链路联动预测管道
    串联: D-Gate → UnifiedPredictor → OU联动 → TaoGe策略
    """

    @staticmethod
    def _apply_hcp2_law(match: MatchInput, ou_link: Dict, form_result=None) -> Dict:
        """P0-1: 让2球不穿律前置到管道

        铁律: 竞彩让2球(>=1.75) → 0%穿盘(WC2026 4场验证)
               仅屠杀队(巴西/德国/荷兰/美国/加拿大) + 非R3 可穿
        行为: 将 ou_link['scores'] 中强队净胜>=3球的屠杀比分移除,
              补充走水/小胜/小负比分, 使比分锚回归不穿区间
        """
        hcp_depth = abs(match.hcp)
        if hcp_depth < 1.75:
            return ou_link

        # 屠杀队穿盘例外
        is_massacre_team = (match.home in OULinkageEngine.MASSACRE_TEAMS or
                            match.away in OULinkageEngine.MASSACRE_TEAMS)
        r3_rotation = getattr(match, 'r3_rotation', False)
        if is_massacre_team and not r3_rotation:
            return ou_link

        # 确定强队侧
        if form_result and form_result.is_valid:
            strong_is_home = form_result.goal_diff_advantage > 0
        else:
            strong_is_home = match.odds_h < match.odds_a  # 低赔率方为强

        scores = ou_link.get('scores', [])
        if not scores:
            return ou_link

        # 过滤: 移除强队净胜>=3球的大比分
        filtered = []
        for s in scores:
            try:
                h, a = map(int, s.split('-'))
                if strong_is_home:
                    diff = h - a
                else:
                    diff = a - h
                if diff >= 3:
                    continue
                filtered.append(s)
            except Exception:
                continue

        # 补充走水/小胜/小负比分(让2球不穿常见结果)
        if match.hcp < 0:  # 主让2球
            water_scores = ['1-0', '2-0', '0-0', '2-1', '1-1', '0-1']
        else:  # 客让2球
            water_scores = ['0-1', '0-2', '0-0', '1-2', '1-1', '1-0']

        final = list(dict.fromkeys(filtered))
        for s in water_scores:
            if s not in final:
                final.append(s)
            if len(final) >= 5:
                break

        if len(final) >= 3:
            ou_link['scores'] = final[:5]
            ou_link['hcp2_law_applied'] = True
            print(f"\n  🟡 P0-1: 让2球不穿律 — hcp={match.hcp:+.2f}, 非屠杀队/或R3")
            print(f"      屠杀比分降级 → {ou_link['scores']}")
        return ou_link

    def __init__(self):
        self.dgate_layer = DGateLayer()
        self.model_layer = ModelLayer()
        self.linkage_engine = OULinkageEngine()
        self.strategy_layer = TaoGeStrategy()
        self.context_analyzer = None  # 惰性加载
        self.form_fetcher = None      # Chain -1: 惰性加载
        self._ou_constrained = False  # v6.0 初始值: OU约束尚未执行

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
            print(f"\n  [链-1] 战绩API失败: {e}")
            print(f"    → ⚠️ 数据不可用，跳过战绩依赖链 (不生成假数据)")
            form_result = None

        # ═══ P0-1: Priority Gate 短路机制 (费深谋+何执策) ═══
        # v5.11修复: 小样本(≤5场WC数据)抑制 — 3场小组赛数据不足以判定屠杀
        short_circuit = False
        short_circuit_level = 4
        short_circuit_reason = ''
        if form_result and form_result.is_valid and form_result.data_quality == 'full':
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

                # ═══ P0-3: 让2球淘汰赛屠杀降级 ═══
                # 规则: 淘汰赛+让2球(>=1.75) 穿盘率极低, 除非屠杀队+非R3
                # 案例: 巴拉圭vs法国(1/8决赛) — 法国让2球, 实际0-1点球(不穿)
                # 与v5.24弱队GA降级是并列条件, 只要满足其一就降级
                hcp_depth = abs(match.hcp)
                is_massacre_team = (match.home in OULinkageEngine.MASSACRE_TEAMS or
                                    match.away in OULinkageEngine.MASSACRE_TEAMS)
                r3_rotation = getattr(match, 'r3_rotation', False)
                massacre_can_wear = is_massacre_team and not r3_rotation
                if hcp_depth >= 1.75 and not massacre_can_wear:
                    short_circuit = False
                    short_circuit_level = 4
                    short_circuit_reason = ''
                    if form_result.massacre_warning:
                        form_result.massacre_warning = False
                    print(f"\n  🟡 P0-3: 让2球淘汰赛屠杀降级 — hcp={match.hcp:+.2f}, 非屠杀队/或R3, 不强制方向")
                    print(f"      让2球不穿律: 淘汰赛穿盘率极低, 允许D-Gate/模型重新参与")

                # P0-3补充: 淘汰赛屠杀降级兜底
                # 即使弱队GA≥1.5/非让2球, 只要淘汰赛+屠杀预警+非屠杀队/或R3, 仍降级
                # 案例: 巴拉圭vs法国(客让1.5, GA=1.2) 实际0-1点球, 屠杀预警失效
                if (stage_val == 'knockout' and short_circuit and short_circuit_level == -1
                        and not massacre_can_wear):
                    short_circuit = False
                    short_circuit_level = 4
                    short_circuit_reason = ''
                    if form_result.massacre_warning:
                        form_result.massacre_warning = False
                    print(f"\n  🟡 P0-3: 淘汰赛屠杀兜底降级 — {weak_team}(GA={weak_ga:.1f}), 非屠杀队/或R3")
                    print(f"      淘汰赛弱队摆大巴, 小组赛屠杀数据不强制方向")

        # ── 链0: 战意/情境分析 (动机层) ──
        context_adj = {}
        try:
            if self.context_analyzer is None:
                from pipeline.match_context_analyzer import MatchContextAnalyzer
                self.context_analyzer = MatchContextAnalyzer
            context_adj = self.context_analyzer.get_adjustment(
                match.home, match.away, 
                matchday=getattr(match, 'matchday', 3),
                stage=getattr(match, 'stage', 'group'),
                odds_h=match.odds_h, odds_a=match.odds_a
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
        # P0-3: 先检查淘汰赛屠杀降级
        if form_result and form_result.massacre_warning:
            stage_val = getattr(match, 'stage', 'group')
            is_massacre_team = (match.home in OULinkageEngine.MASSACRE_TEAMS or
                                match.away in OULinkageEngine.MASSACRE_TEAMS)
            r3_rotation = getattr(match, 'r3_rotation', False)
            massacre_can_wear = is_massacre_team and not r3_rotation
            if stage_val == 'knockout' and not massacre_can_wear:
                form_result.massacre_warning = False
                print(f"\n  🟡 P0-3: 淘汰赛屠杀λ降级 — 非屠杀队/或R3, 关闭屠杀λ重标定")

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
                except Exception:
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

        else:
            # ═══ P0-2.5: 标准 Dixon-Coles λ (v6.0 无屠杀场景, 确保C3.5不缺失) ═══
            # 用赔率反推λ: 从1X2隐含概率 + OU线估算双方期望进球
            # 确保每场比赛都有 C3.5 输出, 不再因数据不足而跳过
            if form_result and form_result.is_valid:
                print(f"\n  [链3.5] Dixon-Coles λ (标准模式)...")
                try:
                    # 1. 隐含概率归一化
                    imp_h = 1.0 / match.odds_h
                    imp_d = 1.0 / match.odds_d
                    imp_a = 1.0 / match.odds_a
                    total_imp = imp_h + imp_d + imp_a
                    prob_h = imp_h / total_imp
                    prob_d = imp_d / total_imp
                    prob_a = imp_a / total_imp

                    # 2. 从 OU 线估算总 λ
                    ou_line = match.ou_line
                    # 总进球期望 ≈ OU线 (市场共识)
                    total_lambda = max(0.5, ou_line)

                    # 3. λ分配: 按隐含胜率比例 + 主场bonus
                    if prob_h + prob_a > 0:
                        share_h = prob_h / (prob_h + prob_a)
                    else:
                        share_h = 0.55
                    # 主场进攻加成5%
                    lam_home = total_lambda * share_h * 1.05
                    lam_away = total_lambda * (1 - share_h)

                    # 约束范围
                    lam_home = max(0.4, min(5.0, lam_home))
                    lam_away = max(0.3, min(5.0, lam_away))

                    print(f"    → λ(赔率反推): home={lam_home:.2f} away={lam_away:.2f} "
                          f"| 隐含 H={prob_h:.1%} D={prob_d:.1%} A={prob_a:.1%}")

                    # 4. Poisson Top-5 比分
                    standard_scores = []
                    max_s = 6
                    sp_list = []
                    for h in range(max_s + 1):
                        for a in range(max_s + 1):
                            try:
                                ph = (lam_home ** h) * math.exp(-lam_home) / max(math.factorial(h), 1)
                                pa = (lam_away ** a) * math.exp(-lam_away) / max(math.factorial(a), 1)
                                sp_list.append((ph * pa, f"{h}-{a}"))
                            except (OverflowError, ValueError):
                                continue
                    sp_list.sort(reverse=True, key=lambda x: x[0])
                    standard_scores = [s for _, s in sp_list[:5]]

                    print(f"    → DC λ 标准比分: {standard_scores}")

                    # 存储到 ou_link (但不覆写已有比分, 仅作参考)
                    ou_link['dc_standard_scores'] = standard_scores
                    ou_link['dc_lambda_computed'] = True
                    model_result.metadata['lambda_home'] = round(lam_home, 2)
                    model_result.metadata['lambda_away'] = round(lam_away, 2)
                    model_result.metadata['dc_standard'] = True
                except Exception as e:
                    print(f"    → ⚠️ DC λ 计算异常: {e}")

        # ═══ P0-5: OU盘口约束总进球 (何执策 · 解决5场OU误判) ═══
        # 原理: 外围OU盘口反映市场对总进球的共识, 偏差>1.5球时应修正
        ou_constrained = _constrain_ou_to_line(ou_link, match, form_result, hasattr(self, '_ou_constrained') and self._ou_constrained)
        if ou_constrained.get('adjusted'):
            ou_link['scores'] = ou_constrained['scores']
            ou_link['ou_constrained'] = True
            model_result.metadata['ou_constrained'] = True
            self._ou_constrained = True  # v6.0 fix: 标记已约束, 避免重复打印

        # ═══ P0-1: 让2球不穿律前置 ═══
        # 在OU约束后, 三路径投票前, 强制修正让2球深让的比分锚
        ou_link = self._apply_hcp2_law(match, ou_link, form_result)
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
        # ── v6.0 Fix B: 数据不可用标记 (无降级数据折让) ──
        if form_result is None:
            strategy['downgrade_note'] = '[数据不可用] 战绩API不可用, 未生成假数据。预测仅基于赔率+模型推理。'
            strategy['data_quality'] = 'unavailable'
        elif getattr(form_result, 'data_quality', None) == 'partial':
            downgrade_factor = 0.7
            orig_conf = strategy['confidence']
            strategy['confidence'] = round(orig_conf * downgrade_factor, 3)
            strategy['downgrade_note'] = f'[降级折让] 战绩API不可用→FIFA排名+赔率反推, 置信度×{downgrade_factor}: {orig_conf:.3f}→{strategy["confidence"]:.3f}'
            strategy['data_quality'] = 'partial'
            print(f"    → ⚠️ 降级折让: 置信度 {orig_conf:.3f}→{strategy['confidence']:.3f} (×{downgrade_factor})")
        else:
            strategy['downgrade_note'] = ''
            strategy['data_quality'] = 'full'
        print(f"    → 策略: {strategy['strategy']}")
        print(f"    → 选择: {strategy['primary']}+{strategy['secondary']}")
        print(f"    → 最佳比分: {strategy['best_score']}")
        print(f"    → 备选比分: {strategy['alt_scores']}")

        # ═══ P0-2: 7条一致性校验自动化 ═══
        consistency_report = run_consistency_checks(
            match, strategy, ou_link, dgate_result, model_result, form_result
        )
        if not consistency_report['overall_pass']:
            print(f"\n  ⚠️ [一致性校验] 未通过: {consistency_report['failures']}")
            for check in consistency_report['checks']:
                if not check['pass']:
                    print(f"      ❌ {check['name']}: {check['note']}")
        else:
            print(f"\n  ✅ [一致性校验] 全部通过")

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
                    'data_quality': form_result.data_quality if form_result else 'missing',
                    'confidence': form_result.confidence if form_result else 0.0,
                },
                'Context': {  # v6.0: 链0战意/情境分析, CLI 直接读取
                    'home_motivation': context_adj.get('home_motivation', ''),
                    'away_motivation': context_adj.get('away_motivation', ''),
                    'home_rotation': context_adj.get('home_rotation', ''),
                    'away_rotation': context_adj.get('away_rotation', ''),
                    'survival_clash': context_adj.get('survival_clash', False),
                    'mutual_benefit_draw': context_adj.get('mutual_benefit_draw', False),
                    'dead_rubber': context_adj.get('dead_rubber', False),
                    'motivation_mult': context_adj.get('motivation_mult', 1.0),
                    'offensive_bias': context_adj.get('offensive_bias', 0.0),
                    'notes': context_adj.get('notes', [])[:4],
                    'context_override': strategy.get('context_override'),
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
                # P0-2: 一致性校验报告
                'consistency': consistency_report,
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
# v6.0 数据清理: MATCHES_6_27 硬编码测试数据已移除
# 实际赛程数据应从 FootballDataLive API / 数据库 / 配置文件读取
# 参见: pipeline/auto_pipeline.py 中的 _load_fixtures_from_api()
# ════════════════════════════════════════════════════

