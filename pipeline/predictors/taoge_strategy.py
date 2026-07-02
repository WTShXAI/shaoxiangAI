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
from pipeline.predictors.dgate_layer import *  # noqa: F401, F403

class TaoGeStrategy:
    """涛哥策略: 永远从主队视角, 深让双选让胜+让平, 永不让负"""

    @staticmethod
    def decide(match: MatchInput, ou_link: Dict, dgate: ChainResult,
               model: ChainResult, form_result: Any = None,
               context_adj: Dict = None) -> Dict[str, Any]:
        """综合全链路证据做最终决策"""
        
        ctx = context_adj or {}

        # ── 收集证据 ──
        evidence = []

        # 证据1: 模型方向
        if model.verdict != '?':
            evidence.append(f'模型:{model.verdict}(conf={model.confidence:.2f})')

        # 证据2: D-Gate信号
        if dgate.metadata.get('dgate_active'):
            evidence.append(f'D-Gate:{dgate.metadata.get("risk_tag","?")}')
            if dgate.draw_prob > 0.35:
                evidence.append(f'平局风险↑({dgate.draw_prob:.2f})')

        # 证据3: OU联动
        evidence.append(f'OU联动:{ou_link["verdict"]}(conf={ou_link["confidence"]:.2f})')
        evidence.append(f'比分锚:{ou_link["scores"][:3]}')

        # 证据4: R3轮换
        if match.r3_rotation:
            evidence.append('⚠️ R3轮换衰减')

        # 证据5: 链0战意/积分形势
        context_override = None
        context_override_reason = ''
        
        # 从context_adj推导结构化语义
        hm = ctx.get('home_motivation', '')
        am = ctx.get('away_motivation', '')
        hr = ctx.get('home_rotation', '')
        ar = ctx.get('away_rotation', '')
        survival_clash = ctx.get('survival_clash', False)
        mutual_benefit = ctx.get('mutual_benefit_draw', False)
        dead_rubber = ctx.get('dead_rubber', False)
        
        must_win_team = ''
        eliminated_team = ''
        qualified_team = ''
        
        if hm == 'survival' and am == 'qualified_free':
            must_win_team = match.home
            qualified_team = match.away
        elif am == 'survival' and hm == 'qualified_free':
            must_win_team = match.away
            qualified_team = match.home
        elif hm == 'must_not_lose' and am == 'survival':
            must_win_team = match.away
        elif am == 'must_not_lose' and hm == 'survival':
            must_win_team = match.home
        
        # ═══ v5.11: 淘汰队/死局不可搏命 ═══
        # 0分淘汰队不应触发搏命局 (如巴拿马0分)
        # dead_rubber标记 OR 链0输出"已淘汰/基本淘汰/0分"
        if dead_rubber and must_win_team:
            evidence.append(f'❌ {must_win_team}已淘汰(死局), 清除搏命标记')
            must_win_team = ''
        # 额外检查: 从context notes检测淘汰队
        if must_win_team:
            notes = ctx.get('notes', [])
            for note in notes:
                if must_win_team in note and ('淘汰' in note or '0分' in note or '已出局' in note):
                    evidence.append(f'❌ {must_win_team}已淘汰(note检测), 清除搏命标记')
                    must_win_team = ''
                    break
        
        if dead_rubber:
            note_teams = [n for n in ctx.get('notes', []) if '已淘汰' in n or '淘汰' in n]
            eliminated_team = '一方' if not note_teams else ''
        
        if survival_clash:
            evidence.append(f'🔥 双求生战! 进攻偏移={ctx.get("offensive_bias","?")}')
            context_override = 'survival_clash'
            context_override_reason = '双求生战: 比赛开放, 大球+主队方向偏好'
        if mutual_benefit:
            evidence.append(f'⚠️ 默契平局! 动机倍率={ctx.get("motivation_mult","?"):.2f}')
            context_override = 'mutual_benefit_draw'
            context_override_reason = '双方平即出线, 平局方向强制'
        if must_win_team:
            evidence.append(f'⚔️ {must_win_team}必须赢(搏命局)')
            context_override = f'{must_win_team}搏命'
            context_override_reason = f'{must_win_team}非赢不可, 赔率判型被形势覆盖'
        if qualified_team:
            evidence.append(f'✅ {qualified_team}已出线(轮换衰减×0.7)')
        if eliminated_team:
            evidence.append(f'❌ {eliminated_team}已淘汰(防守崩塌)')

        # ── 策略决策矩阵 ──

        # Chain -1 屠杀预警 → 覆盖所有常规策略, 直接跟强队方向
        abs_gap_val = abs(form_result.goal_diff_advantage) if form_result else 0

        if form_result and form_result.massacre_warning:
            # ═══ v5.24: 淘汰赛屠杀降级检查 ═══
            # pipeline.py Priority Gate已做降级, 此处为双重保险
            stage_is_knockout = getattr(match, 'stage', 'group') == 'knockout'
            weak_ga = 999  # 默认不降级
            if stage_is_knockout:
                weak_ga = form_result.away.avg_ga if form_result.goal_diff_advantage > 0 else form_result.home.avg_ga
                weak_team = form_result.away.team if form_result.goal_diff_advantage > 0 else form_result.home.team
                if weak_ga < 1.2:
                    evidence.append(f'🟡 v5.24双重: 淘汰赛屠杀降级 → {weak_team}防守强(GA={weak_ga:.1f}), 走默认策略')
            
            # 仅非降级场景执行屠杀覆盖
            if not (stage_is_knockout and weak_ga < 1.2):
                if form_result.goal_diff_advantage < 0:
                    # 客队更强 (如法国 vs 挪威)
                    primary, secondary = '让负', '客胜'
                    strategy = '屠杀预警(跟客队)'
                    rationale = f'阵容屠杀预警: 强队{form_result.away.team}全攻+全主力 vs 弱队缺阵'
                else:
                    primary, secondary = '让胜', '主胜'
                    strategy = '屠杀预警(跟主队)'
                    rationale = f'阵容屠杀预警: 强队{form_result.home.team}全攻+全主力 vs 弱队缺阵'
                evidence.append(f'🚨 屠杀预警: {form_result.verdict}')
                evidence.append(f'修正{form_result.score_adjustment:+.1f}球')
        elif context_override == 'mutual_benefit_draw':
            # 默契平局 → 平局方向强制
            primary, secondary = '平', '平局'
            strategy = '默契平局(链0战意覆盖)'
            rationale = context_override_reason
            evidence.append('🏳️ 链0战意覆盖: 默契平局, 赔率判型被推翻')
        elif context_override == 'survival_clash':
            # 双求生战 → 双方都需要赢
            # v5.13: 弱攻双求生 → MD3触发 + MD2扩展(v5.17)
            # MD3案例: 捷克(GF0.67)vs南非(GF0.67)→1-1 ✅
            # MD2案例: 乌拉圭(GF1.0)vs佛得角(GF0.67)→2-2 (双方GF低+实力接近)
            # MD1反例: 海地(GF0.67)vs苏格兰(GF0.33)→0-1 ❌
            is_md3 = (getattr(match, 'matchday', 3) >= 3)
            is_md2_or_later = (getattr(match, 'matchday', 3) >= 2)
            
            # ═══ v5.17: MD2+积分联动战绩 → 弱攻双求生平局 ═══
            # 积分分析(survival)+战绩(GF低+实力接近) → 平局优先
            # MD3: GF<1.5 (原有阈值, 3场数据可靠)
            # MD2: GF<1.2+gap<1.0 (更严格, 2场数据不够稳)
            weak_attack_draw = False
            if form_result:
                h_gf = form_result.home.avg_gf
                a_gf = form_result.away.avg_gf
                gap = abs(form_result.goal_diff_advantage)
                if is_md3 and h_gf < 1.5 and a_gf < 1.5:
                    weak_attack_draw = True
                    tag = 'MD3'
                elif is_md2_or_later and not is_md3 and h_gf < 1.2 and a_gf < 1.2 and gap < 1.0:
                    weak_attack_draw = True
                    tag = 'MD2'
            
            if weak_attack_draw:
                primary, secondary = '平', '主胜'
                strategy = f'弱攻双求生({tag})→平局优先'
                rationale = f'双方求生但攻击力不足(H={h_gf:.1f}/A={a_gf:.1f})且实力接近(gap={gap:.1f}), 平局概率高'
                evidence.append(f'⚖️ 弱攻双求生({tag}): 双方GF<阈值+gap<1.0, 积分联动战绩→平局')
            else:
                # ═══ v5.15: D-Gate+Model平局共识覆盖存活冲突 ═══
                # 双求生战中, 若D-Gate说draw_alert且模型预测D, 且双方无实力碾压
                # 案例: 比利时(GF2.0)vs埃及(GF1.67)→1-1(DG+Model双D), 沙特vs乌拉圭→1-1
                # ⚠️ MD1抑制: MD1所有队0分→draw_alert信号不可靠 (海地vs苏格兰反例)
                #    v5.13已在MD3触发, v5.15补充MD2 (即有比赛数据的中间轮次)
                is_md2_or_later = (getattr(match, 'matchday', 3) >= 2)
                
                if is_md2_or_later:
                    model_v = model.verdict if model and model.verdict != '?' else ''
                    dgate_draw = dgate and dgate.metadata and 'draw_alert' in str(dgate.metadata.get('risk_tag', ''))
                    # 也检查 signals 字段 (risk_tag有时为空但signals有draw_alert)
                    if not dgate_draw:
                        dgate_draw = dgate and any('draw_alert' in s for s in (dgate.signals or []))
                    model_draw = (model_v == 'D')
                    
                    # 双方实力差距检测 (不能一方碾压另一方, 2.0=dominate+)
                    home_stronger = form_result and form_result.goal_diff_advantage > 2.0
                    away_stronger = form_result and form_result.goal_diff_advantage < -2.0
                    massacre_likely = home_stronger or away_stronger
                    
                    # v5.15修正: 双求生场景中, 仅"已出线"构成动机不对称
                    # 淘汰/荣誉队仍在奋战→不阻碍平局共识 (案例: 沙特0分→1-1)
                    has_qualified = bool(qualified_team)
                    
                    if model_draw and dgate_draw and not massacre_likely and not has_qualified:
                        primary, secondary = '平', '主胜'
                        strategy = 'D-Gate+模型双D覆盖(双求生)'
                        dg_tag = dgate.metadata.get('risk_tag', '') or str(dgate.signals)
                        rationale = f'双方求生但D-Gate({dg_tag})+模型({model_v})一致平局'
                        evidence.append(f'🛡️ v5.15: D-Gate+模型双D覆盖双求生→平局 (实力差距={abs(form_result.goal_diff_advantage) if form_result else 0:.1f})')
                    else:
                        # ═══ v5.20: 双方淘汰 → 跟赔率方向 ═══
                        # 双方都0分淘汰 → dead rubber, 无真实战意 → 跟市场赔率
                        both_eliminated = (hm == 'honor' and am == 'honor') or bool(eliminated_team and (hm == 'honor' or am == 'honor'))
                        if both_eliminated:
                            # 跟最低赔率方向
                            if match.odds_a < match.odds_h and match.odds_a < match.odds_d:
                                primary, secondary = '客胜', '让负'
                                strategy = '双方淘汰→跟客胜(赔率最低)'
                                rationale = f'双方0分淘汰, 市场倾向客胜({match.odds_a:.2f})'
                                evidence.append(f'🏳️ v5.20: 双方淘汰→跟赔率方向(客{match.odds_a:.2f})')
                            elif match.odds_h < match.odds_d:
                                primary, secondary = '主胜', '让胜'
                                strategy = '双方淘汰→跟主胜(赔率最低)'
                                rationale = f'双方0分淘汰, 市场倾向主胜({match.odds_h:.2f})'
                                evidence.append(f'🏳️ v5.20: 双方淘汰→跟赔率方向(主{match.odds_h:.2f})')
                            else:
                                primary, secondary = '平', '主胜'
                                strategy = '双方淘汰→平局(赔率最低)'
                                rationale = '双方0分淘汰, 市场倾向平局'
                                evidence.append('🏳️ v5.20: 双方淘汰→跟赔率方向(平)')
                        else:
                            # ═══ v5.20b: 赔率方向明确 → 跟赔率 ═══
                            # 双方求生但赔率强烈倾向一方 → 跟市场方向
                            odds_ratio = match.odds_h / max(match.odds_a, 0.01)
                            if match.odds_a < match.odds_h * 0.5:  # 客胜<主胜一半
                                primary, secondary = '客胜', '让负'
                                strategy = '双方求生→赔率强指客胜'
                                rationale = f'赔率强指向客胜({match.odds_a:.1f} vs {match.odds_h:.1f})'
                                evidence.append(f'💰 v5.20b: 赔率指向客胜(ratio={odds_ratio:.1f})')
                            elif match.odds_h < match.odds_a * 0.5:  # 主胜<客胜一半
                                primary, secondary = '主胜', '让胜'
                                strategy = '双方求生→赔率强指主胜'
                                rationale = f'赔率强指向主胜({match.odds_h:.1f} vs {match.odds_a:.1f})'
                                evidence.append(f'💰 v5.20b: 赔率指向主胜(ratio={1/odds_ratio:.1f})')
                            else:
                                primary, secondary = '主胜', '客胜'
                                strategy = '双求生对攻(链0战意覆盖)'
                                rationale = context_override_reason
                else:
                    # MD1 fallback: 双方求生但D-Gate/Model在MD1不可靠
                    primary, secondary = '主胜', '客胜'
                    strategy = '双求生对攻(链0战意覆盖)'
                    rationale = context_override_reason
            if '平' in str(primary):
                evidence.append('🔥 链0战意: 双求生, 平局方向(v5.15)')
            else:
                evidence.append('🔥 链0战意覆盖: 双求生, 大球方向')
        elif context_override and '搏命' in context_override:
            # ═══ v5.10: 已出线队不搏命 ═══
            # 6分已出线 → 轮换衰减, 不强制方向。哥伦比亚069案例: 6分轮换→实际0-0
            # P0-fix (6/29): must_win_team是本地变量, 不能从ctx.get()读取
            team = must_win_team
            team_qualified = (team == qualified_team) if qualified_team else False
            if team_qualified:
                # 已出线队: 轮换衰减 + 对手打平出线 → 低分保守, 1X2方向更合适
                primary, secondary = '胜', '平'
                strategy = f'{team}已出线轮换(非搏命)'
                rationale = f'{team}已6分出线, 大概率轮换, 低分保守→1X2胜平'
                evidence.append(f'🔄 {team}已出线, 搏命局降级为轮换衰减→1X2胜平')
            # ═══ 补充检查: 直接判断该队是否已6分 ═══
            elif hm == 'qualified_free' or am == 'qualified_free':
                # 某队已出线 → 整体降级, 不强制方向
                primary, secondary = '胜', '平'
                strategy = '已出线保守(链0降级)'
                rationale = '已有6分出线队, 整体保守→1X2胜平'
                evidence.append('🔄 已出线队在场, 搏命局降级→1X2胜平')
            # ═══ v5.11: 弱队搏命衰减 ═══
            # 必须赢的弱队 vs 打平出线的强队 → 强队可守平, 弱队攻击力不足
            # 触发条件: gap≥1.0 + 弱队防守好(GA<1.0)
            # 案例: 葡萄牙(GF2.4)vs民主刚果(GA0.6/GF1.0)→1-1 ✅触发
            # 反例: 英格兰(GF2.1)vs克罗地亚(GA1.4)→4-2 ❌不触发(防守差)
            elif form_result and abs_gap_val >= 1.0:
                weak_is_home = (must_win_team == match.home)
                gap_direction = form_result.goal_diff_advantage
                # 确认: 搏命队是弱的一方
                if (weak_is_home and gap_direction < 0) or (not weak_is_home and gap_direction > 0):
                    weak_ga = form_result.home.avg_ga if weak_is_home else form_result.away.avg_ga
                    # 只在弱队防守好时触发 (GA<1.0 → 强队难以轻松进球)
                    if weak_ga < 1.0:
                        primary, secondary = '平', '主胜' if gap_direction > 0 else '平'
                        strategy = f'弱队{team}搏命衰减→平局优先'
                        rationale = f'{team}必须赢但实力差距{abs_gap_val:.1f}球且防守强(GA={weak_ga:.1f}), 强队可守平'
                        evidence.append(f'⚖️ 弱队搏命衰减: {team}gap={abs_gap_val:.1f}+GA={weak_ga:.1f}<1.0, 强队可守平')
                    else:
                        # 弱队防守差: 检查样本可靠性
                        # v5.12: 小样本(<5场)时GA不可靠 → 保守回到平局
                        weak_matches = form_result.home.matches if weak_is_home else form_result.away.matches
                        small_sample = weak_matches < 5
                        if small_sample:
                            # 小样本: GA数据不可靠, 保守→平局
                            # 案例: 捷克(3场/GA2.0)vs南非(3场)→1-1, GA2.0不可靠
                            primary, secondary = '平', '主胜' if gap_direction > 0 else '平'
                            strategy = f'小样本+弱队{team}搏命→保守平局'
                            rationale = f'{team}必须赢但样本仅{weak_matches}场(GA={weak_ga:.1f}不可靠), 保守→平局'
                            evidence.append(f'🔬 小样本保守: {team}仅{weak_matches}场数据(GA={weak_ga:.1f}), 平局优先')
                        else:
                            # 大样本+防守差: 跟强队方向
                            # 案例: 英格兰(GF2.1)vs克罗地亚(GA1.4搏命)→4-2, 克罗地亚防不住
                            if gap_direction > 0:
                                primary, secondary = '主胜', '让胜'
                            else:
                                primary, secondary = '客胜', '让负'
                            strategy = f'弱队{team}搏命但防守差→跟强队'
                            rationale = f'{team}必须赢但防守差(GA={weak_ga:.1f}), 强队预计大胜'
                            evidence.append(f'⚔️ {team}必须赢但防守差(GA={weak_ga:.1f}), 跟强队方向')
                else:
                    # 搏命队是强队: 正常搏命
                    if team == match.home:
                        primary, secondary = '主胜', '让胜'
                    else:
                        primary, secondary = '客胜', '让负'
                    strategy = f'{team}强队搏命局(链0战意覆盖)'
                    rationale = context_override_reason
                    evidence.append(f'⚔️ 链0战意覆盖: {team}搏命(强队), 赔率判型被形势推翻')
            elif team == match.home:
                primary, secondary = '主胜', '让胜'
                strategy = f'{team}搏命局(链0战意覆盖)'
                rationale = context_override_reason
                evidence.append(f'⚔️ 链0战意覆盖: {team}搏命, 赔率判型被形势推翻')
            else:
                primary, secondary = '客胜', '让负'
                strategy = f'{team}搏命局(链0战意覆盖)'
                rationale = context_override_reason
                evidence.append(f'⚔️ 链0战意覆盖: {team}搏命, 赔率判型被形势推翻')
        else:
            # ═══ v5.14: 默认策略 + 模型-DGate投票覆盖 ═══
            # 当 context_override 未触发战意覆盖, 但模型+D-Gate一致指向D时
            # 案例: 荷兰(6分已出线)vs日本(4分打平出线)→2-2, 模型D+D-Gate draw_alert
            model_v = model.verdict if model and model.verdict != '?' else ''
            dgate_draw = dgate and dgate.metadata and 'draw_alert' in str(dgate.metadata.get('risk_tag', ''))
            model_draw = (model_v == 'D')
            
            if model_draw and dgate_draw:
                # ═══ 动机对称性检查 ═══
                # 只有当双方都愿意接受平局时, 才信任模型+D-Gate的平局信号
                # 反例: 加纳(4分要赢)vs巴拿马(0分淘汰)→1-0, 加纳必须赢
                has_survival_team = (hm == 'survival' or am == 'survival')
                has_strong_team = (hm in ('qualified_free', 'must_not_lose') or am in ('qualified_free', 'must_not_lose'))
                has_eliminated_team = (hm == 'honor' or am == 'honor') or bool(eliminated_team)
                # 一方必须赢(survival) + 另一方有分/已出线 → 动机不对等
                motivation_asymmetric = (has_survival_team and has_strong_team) or has_eliminated_team
                
                if motivation_asymmetric:
                    # 一方必须赢 vs 另一方无欲无求 → 跟强队/主队
                    primary = '主胜'
                    secondary = '平'
                    strategy = '动机不对称→跟主队'
                    rationale = '模型-DGate平局信号被动机不对称抑制'
                    evidence.append(f'🔇 平局信号抑制: 一方必须赢({hm}/{am}), 模型-DGate平局不适用')
                else:
                    primary, secondary = '平', '主胜'
                    strategy = '模型+D-Gate一致平局'
                    rationale = '模型和D-Gate一致指向平局, 战意无明确方向'
                    evidence.append(f'🤝 模型+D-Gate一致: 模型={model_v}, D-Gate=draw_alert → 平局')
            else:
                primary = '主胜'
                secondary = '平'
                strategy = '联动策略'
                rationale = '基于OU联动和D-Gate综合决策'

        # ═══ v5.10: 已出线队后置检查 ═══
        # 只要场上有6分已出线队 → 保守轮换, 1X2优于让球
        # ⚠️ 例外: 屠杀预警压倒一切, 已出线保守不可覆盖屠杀判决
        if (qualified_team or hm == 'qualified_free' or am == 'qualified_free') \
                and not (form_result and form_result.massacre_warning):
            if any('让' in str(x) for x in (primary, secondary)):
                primary, secondary = '胜', '平'
                strategy = f'{strategy}→1X2(已出线保守)'
                evidence.append('🔄 已出线队在场, 让球→1X2胜平')

        # ── OU联动修正 ──
        ou_verdict = ou_link['verdict']
        if '平局' in ou_verdict:
            primary, secondary = '平', '主胜'
        elif match.ou_line < 2.0 and '让胜' in str(primary):
            primary, secondary = '平', primary

        # ── D-Gate修正 ──
        if dgate.metadata.get('dgate_active') and 'ignore_draw' in dgate.metadata.get('risk_tag', ''):
            if '平' in (primary, secondary):
                # ═══ v5.24: 淘汰赛+弱队防守强 → ignore_draw信号不可靠 ═══
                # 案例: 德国vs巴拉圭 — 淘汰赛, 巴拉圭防守强(GA 0.33) → 必发平局热是真实信号
                stage_knockout = getattr(match, 'stage', 'group') == 'knockout'
                if stage_knockout and form_result:
                    weak_ga = form_result.away.avg_ga if form_result.goal_diff_advantage > 0 else form_result.home.avg_ga
                    if weak_ga < 1.2:
                        evidence.append(f'🛡️ v5.24: 淘汰赛+弱队防守强(GA={weak_ga:.1f}), ignore_draw诱平信号不可靠, 保留平局')
                    else:
                        primary, secondary = '主胜', '让胜'
                else:
                    primary, secondary = '主胜', '让胜'

        # ── 校验6: 让1球冷门律 ──
        if abs(match.hcp) >= 0.75 and abs(match.hcp) < 1.75:
            # 让0.5-1球: OU偏低 + 弱队有进球能力 → 冷门预警
            if form_result:
                weak_team_avg_gf = form_result.away.avg_gf if match.hcp > 0 else form_result.home.avg_gf
                if match.ou_line <= 2.25 and weak_team_avg_gf > 0.5:
                    evidence.append('⚡ 让1球冷门律: OU≤2.25+弱队场均进球>0.5→冷门风险，建议双选')

        # ── 最终比分 ── v5.23 hcp深度模板化 ──
        # 用让球深度直接生成结构化比分候选，不再仅依赖ou_link Top-3
        hcp_abs = abs(match.hcp)
        home_give = match.hcp < 0  # 主让球
        massacre_adjusted = ou_link.get('massacre_rescaled', False)
        
        # 比分模板：按让球深度分级 (v5.23: +赔率深度修正)
        # 赔率不对称→深度升级: 主/客超2倍差距→至少medium; >3倍→至少deep
        odds_gap = max(match.odds_h, match.odds_a) / max(min(match.odds_h, match.odds_a), 0.01)
        if odds_gap > 3:   hcp_abs = max(hcp_abs, 2.5)
        elif odds_gap > 2: hcp_abs = max(hcp_abs, 1.5)
        
        if hcp_abs < 0.75:
            template = ['1-0','2-1','2-0','1-1','0-0']  # 浅让: 1球差
        elif hcp_abs < 1.5:
            template = ['2-0','3-1','1-0','2-1','3-0']  # 中让: 1-2球差
        elif hcp_abs < 2.5:
            template = ['3-0','4-1','2-0','3-1','4-0','0-3','1-4']  # 深让: 2-3球差
        else:
            template = ['4-0','5-1','3-0','5-0','4-1','0-4','1-5']  # 极深: 3+球差
        
        # 方向填充: 根据让球方向展开模板
        candidates = []
        for s in template:
            sh, sa = map(int, s.split('-'))
            if home_give:
                candidates.append(f'{sh}-{sa}')    # 主让→主胜方向
            else:
                candidates.append(f'{sa}-{sh}')    # 主受→客胜方向
        
        # OU总球排序：hcp模板为主，λ补充(如有)
        all_candidates = list(dict.fromkeys(candidates[:5]))
        if ou_link.get('massacre_rescaled'):
            # 补充λ重标定结果但不覆盖模板
            for s in ou_link['scores'][:3]:
                if s not in all_candidates:
                    all_candidates.append(s)
        
        # 用OU线排序
        ou_scored = []
        for s in all_candidates:
            try:
                sh, sa = map(int, s.split('-'))
                total = sh + sa
                ou_scored.append((abs(total - match.ou_line), s))
            except Exception as e: logger.warning(f"TaoGe v5.23: 比分解析失败 '{s}': {e}")
        ou_scored.sort(key=lambda x: x[0])
        
        top_scores = [s for _, s in ou_scored[:5]]
        best_score = top_scores[0]
        alt_scores = top_scores[1:3]
        evidence.append(f'🎯 v5.23: hcp={match.hcp:+.2f}模板化比分→{best_score}')

        # ═══ v5.21: 双方进球多样性 ═══
        # 弱队GF>1.0时, 确保至少1个"弱队进球>0"的比分在Top3中
        # 案例: 挪威GF=2.67 vs 法国→实际1-4, 预测0-3缺弱队进球
        if form_result:
            weak_is_home = form_result.goal_diff_advantage < 0
            weak_gf = form_result.home.avg_gf if weak_is_home else form_result.away.avg_gf
            if weak_gf > 1.0:
                has_weak_score = False
                for s in top_scores[:3]:
                    try:
                        sh, sa = map(int, s.split('-'))
                        if (weak_is_home and sh > 0) or (not weak_is_home and sa > 0):
                            has_weak_score = True
                            break
                    except Exception as e: logger.warning(f"TaoGe v5.21: 比分解析失败 '{s}': {e}")
                if not has_weak_score:
                    # 从备选池找有弱队进球的比分替换第3个
                    for s in top_scores[3:]:
                        try:
                            sh, sa = map(int, s.split('-'))
                            if (weak_is_home and sh > 0) or (not weak_is_home and sa > 0):
                                top_scores[2] = s
                                alt_scores = top_scores[1:3]
                                evidence.append(f'🎯 v5.21: 弱队GF={weak_gf:.1f}>1.0, 补充双方进球比分')
                                break
                        except Exception as e: logger.warning(f"TaoGe v5.21(备选): 比分解析失败 '{s}': {e}")

        # ═══ v5.18: hcp+OU联合约束比分精调 ═══
        # 用hcp锚定比分差 + OU锚定总球数，双维度过滤+排序
        hcp_depth = abs(match.hcp)
        ou_line = match.ou_line
        should_refine = (hcp_depth >= 0.25 and not massacre_adjusted 
                         and not (form_result and form_result.massacre_warning))
        
        if should_refine:
            target_diff = round(hcp_depth)
            is_home_give = match.hcp < 0
            
            scored = []
            for s in top_scores[:5]:
                try:
                    sh, sa = map(int, s.split('-'))
                    diff = sh - sa
                    total = sh + sa
                    
                    # 1. hcp方向约束：让球方向必须匹配
                    # ═══ v5.24: 平局方向时跳过方向过滤 ═══
                    # 案例: 荷兰vs摩洛哥 — 预测让平/让负(摩洛哥+0.5), 方向含'平'
                    #       v5.18过滤diff≤0 → 所有平局比分被移除 → 首选1-0(矛盾!)
                    #       实际1-1 → 平局比分应保留在候选池
                    is_draw_verdict = ('平' in str(primary) and '让' not in str(primary)
                                       ) or '平局' in str(primary)
                    if not is_draw_verdict:
                        if is_home_give and diff <= 0: continue
                        if not is_home_give and diff >= 0: continue
                    
                    # 2. OU总球约束：偏离OU线的惩罚分
                    ou_dev = abs(total - ou_line)
                    
                    # 3. 综合评分：分差偏离(权重0.6) + 总球偏离(权重0.4)
                    diff_score = abs(abs(diff) - target_diff)
                    total_score = ou_dev
                    combined = diff_score * 0.6 + total_score * 0.4
                    
                    scored.append((combined, s, diff, total))
                except Exception as e:
                    logger.debug(f"[TaoGe] 评分跳过: {e}")
                    continue
            
            if scored:
                scored.sort(key=lambda x: x[0])
                top_scores = [s for _, s, _, _ in scored[:3]]
                best_score = top_scores[0]
                alt_scores = top_scores[1:3]
                
                # 诊断信息
                best_diff = scored[0][2]
                best_total = scored[0][3]
                evidence.append(f'🎯 v5.18: hcp={match.hcp:+.2f}+OU={ou_line}约束→diff≈{target_diff}球 total≈{ou_line}球')

        # ═══ v6.0: 智慧推荐类型选择 — 双市场推荐 ═══
        # 根据预测比分形态 + 赔率结构 + OU 动态决定推荐方式
        # 修复(v5.7→v6.0): 删除机械转让球逻辑, 深让盘/大比分改为双推模式
        try:
            bs_h, bs_a = map(int, best_score.split('-'))
            bs_total = bs_h + bs_a
            bs_diff = abs(bs_h - bs_a)
        except (ValueError, TypeError):
            bs_total, bs_diff = 2, 1

        # 条件判断
        is_small_ou = match.ou_line <= 2.0           # 小球线
        is_draw_score = bs_h == bs_a                 # 平局比分
        is_tight_score = bs_total <= 2                # 小比分(0-0/1-0/0-1/1-1/2-0/0-2)
        is_blowout = bs_diff >= 2 and bs_total >= 3   # 大比分(3-0+/0-3+)
        hcp_is_deep = abs(match.hcp) >= 0.75          # 深让盘
        
        rec_type = 'balanced'
        rec_markets = ['1X2']  # 默认推荐1X2市场
        
        if is_draw_score or (is_tight_score and is_small_ou):
            # 平局/小比分+小球线 → 1X2推荐更安全
            # v5.10: hcp>=1时保留让球标签, 不机械转1X2
            if any('让' in x for x in (primary, secondary)) and hcp_is_deep:
                # 深让盘+小比分: 双推(让球+1X2)
                rec_type = '双推(深让小比分:让球+1X2)'
                rec_markets = ['AH', '1X2']
                # 保留让球标签作为primary, 增加1X2作为secondary
                if primary == '让胜':
                    secondary = '主胜'
                elif primary == '让负':
                    secondary = '客胜'
                elif primary == '让平':
                    secondary = '平局'
                evidence.append(f'🔄 智慧推荐: 深让+小比分, 双推让球+1X2')
            elif any('让' in x for x in (primary, secondary)):
                # ═══ v5.11: 战意覆盖保护 ═══
                # 当TaoGe已基于chain 0战意(搏命/屠杀/默契平局)做决策
                # OU平局比分不应覆盖方向决策
                # 案例: 英格兰(4pts)vs克罗地亚(3pts搏命) — TaoGe决定客胜+让负
                #       OU锚比分1-1 → 不应转为平局推荐
                is_context_locked = (
                    context_override is not None
                    and ('搏命' in str(context_override) 
                         or 'survival' in str(context_override)
                         or '默契' in str(context_override))
                ) or (form_result and form_result.massacre_warning)
                
                if is_context_locked:
                    # 保持让球推荐, 不切换为1X2
                    rec_type = '战意覆盖(保留让球)'
                    rec_markets = ['AH', '1X2']
                    if primary == '让胜':
                        secondary = '主胜'
                    elif primary == '让负':
                        secondary = '客胜'
                    else:
                        secondary = '平局'
                    evidence.append(f'🛡️ 战意覆盖: OU比分{best_score}但战意决策优先, 保留{primary}')
                else:
                    # 浅让+小比分 → 切换为1X2推荐
                    model_v = model.verdict if model and model.verdict != '?' else 'H'
                    if model_v == 'H':
                        primary, secondary = '主胜', '平局'
                    elif model_v == 'A':
                        primary, secondary = '客胜', '平局'
                    else:
                        primary, secondary = '平局', '主胜'
                    rec_type = '1X2优先(小比分)'
                    rec_markets = ['1X2']
                    evidence.append(f'🔄 智慧推荐: 比分{best_score}为小比分, 切换为1X2推荐')
            else:
                # 已经是1X2标签, 保持
                rec_type = '1X2优先(小比分)'
                rec_markets = ['1X2']
        elif is_blowout and is_small_ou:
            # 大比分+小球线 → 异常组合(理论上矛盾), 还是1X2安全
            rec_type = '1X2优先(大比分但OU小)'
            rec_markets = ['1X2']
        elif is_blowout and not is_small_ou:
            # 大比分+大球线 → 让球有价值, 但同时推荐1X2
            rec_type = '双推(大比分:让球+1X2)'
            rec_markets = ['AH', '1X2']
            # 让球作为primary, 1X2作为secondary
            if not any('让' in x for x in (primary,)):
                # 当前是1X2标签, 增加让球选项
                if '主胜' in primary or primary == '胜':
                    primary, secondary = '让胜', '主胜'
                elif '客胜' in primary or primary == '负':
                    primary, secondary = '让负', '客胜'
                else:
                    primary, secondary = '让平', '平局'
            evidence.append(f'🔥 智慧推荐: 比分{best_score}为大比分, 双推让球+1X2')
        elif hcp_is_deep and not is_tight_score and not is_draw_score:
            # 深让盘+非小比分 → 双推(让球+1X2)
            rec_type = '双推(深让:让球+1X2)'
            rec_markets = ['AH', '1X2']
            if not any('让' in x for x in (primary,)):
                # 增加让球选项
                if '主胜' in str(primary) or primary == '胜':
                    primary, secondary = '让胜', '主胜'
                elif '客胜' in str(primary) or primary == '负':
                    primary, secondary = '让负', '客胜'
                else:
                    primary, secondary = '让平', '平局'
            evidence.append(f'🔄 智慧推荐: 深让盘{abs(match.hcp):.2f}球, 双推让球+1X2')
        else:
            rec_type = '双推(均衡)'
            rec_markets = ['AH', '1X2']

        # 统一标签格式: 确保primary/secondary使用明确的标签
        # 1X2: '主胜'/'平局'/'客胜'   让球: '让胜'/'让平'/'让负'
        label_map = {
            '胜': '主胜', '负': '客胜', '平': '平局',
            '让胜': '让胜', '让平': '让平', '让负': '让负',
            '主胜': '主胜', '客胜': '客胜', '平局': '平局',
        }
        primary = label_map.get(primary, primary)
        secondary = label_map.get(secondary, secondary)

        # ═══ v5.10: 判决-比分一致性校验 (让球深度感知) ═══
        def _score_matches_verdict(score_str, verdict, hcp_val=0):
            """检查比分是否匹配判决方向 (v5.10: 传入让球深度)
            
            hcp_val: 外围让球 (负=主让, 正=主受)
            """
            try:
                h, a = map(int, score_str.split('-'))
            except (ValueError, AttributeError):
                return False
            
            hcp_abs = abs(hcp_val)
            if '让胜' in verdict:
                if hcp_val < 0:       return (h - a) > hcp_abs   # 主让X球: 需净胜>X球
                elif hcp_val > 0:     return (h + hcp_val) > a   # 主受X球: 加让球后赢
                else:                 return h > a
            if '让平' in verdict:
                if hcp_val < 0:       return (h - a) == hcp_abs  # 赢球数=让球数
                elif hcp_val > 0:     return (h + hcp_val) == a
                else:                 return h == a
            if '让负' in verdict:
                if hcp_val < 0:       return (h - a) < hcp_abs   # 未赢够
                elif hcp_val > 0:     return (h + hcp_val) < a
                else:                 return h < a
            if '主胜' in str(verdict):
                return h > a
            if '客胜' in str(verdict):
                return a > h
            if '平' in str(verdict):
                return h == a
            if '胜' in str(verdict):
                return h > a
            return True

        if not _score_matches_verdict(best_score, primary, match.hcp):
            matched = [s for s in top_scores if _score_matches_verdict(s, primary, match.hcp)]
            if matched:
                best_score = matched[0]

        # ═══ 备选比分一致性过滤器 ═══
        # 1. 移除与best_score重复的备选
        # 2. 优先保留匹配secondary方向的比分
        # 3. 若无可匹配secondary的比分，从全量OU分数中补充
        filtered_alts = []
        # 先匹配secondary方向
        for s in top_scores + ou_link.get('scores', []):
            if s == best_score:
                continue
            if s in filtered_alts:
                continue
            if _score_matches_verdict(s, secondary, match.hcp):
                filtered_alts.append(s)
            if len(filtered_alts) >= 2:
                break
        # 如secondary方向不够，用primary方向补
        if len(filtered_alts) < 2:
            for s in top_scores + ou_link.get('scores', []):
                if s == best_score:
                    continue
                if s in filtered_alts:
                    continue
                if _score_matches_verdict(s, primary, match.hcp):
                    filtered_alts.append(s)
                if len(filtered_alts) >= 2:
                    break
        # 最后兜底
        if not filtered_alts:
            for s in top_scores:
                if s != best_score and s not in filtered_alts:
                    filtered_alts.append(s)
        alt_scores = filtered_alts[:2]

        
        # ═══ 战术修正: 淘汰赛特殊调整 ═══
        try:
            from pipeline.tactical_modifier import get_tactical_adjustment
            tac = get_tactical_adjustment(f"{match.home} vs {match.away}")
            if tac.get('total_goals', 0) != 0:
                # 战术调整OU方向: TG下降 → 偏小球, TG上升 → 偏大球
                pass  # 已在triple_constraint权重中体现
        except ImportError:
            pass
        # ═══ 涛哥三维约束模型: 让球+方向+OU → 精确比分 ═══
        # 不再用频率排序，直接用让球结果、方向、OU三维交集求比分
        try:
            from pipeline.predictors.helpers import triple_constraint_scores
            
            # 确定让球结果
            if '让胜' in str(primary):    hcp_out = '让胜'
            elif '让平' in str(primary):  hcp_out = '让平'
            elif '让负' in str(primary):  hcp_out = '让负'
            else:
                # 从primary推导让球结果
                ph, pa = map(int, best_score.split('-'))
                adjusted = ph + match.hcp - pa if match.hcp > 0 else (ph - pa + match.hcp if match.hcp < 0 else ph - pa)
                hcp_out = '让胜' if adjusted > 0 else ('让平' if adjusted == 0 else '让负')
            
            # 确定方向
            if '主胜' in str(primary) or primary == '胜':   direction = '胜'
            elif '客胜' in str(primary) or primary == '负':  direction = '负'
            elif '平' in str(primary):                       direction = '平'
            else:
                ph, pa = map(int, best_score.split('-'))
                direction = '胜' if ph>pa else ('负' if pa>ph else '平')
            
            # 确定OU方向
            honesty = ou_link.get('ou_honesty', {})
            ou_grade = honesty.get('grade', 'honest_mid')
            is_under = 'trap_low' in ou_grade or 'honest_low' in ou_grade \
                       or ou_link.get('verdict', '').startswith('OU小')
            ou_dir = '小' if is_under else '大'
            
            # v5.23: 模板比分已含hcp+方向+OU约束, 跳过三维约束
            evidence.append(f'🎯 模板比分: hcp={match.hcp:+.2f} → {best_score}')
        except Exception as e:
            pass

        return {
            'strategy': strategy,
            'primary': primary,
            'secondary': secondary,
            'rec_type': rec_type,
            'rec_markets': rec_markets,  # 新增: 推荐的市场列表 ['1X2'] / ['AH'] / ['AH', '1X2']
            'rationale': rationale,
            'evidence': evidence,
            'best_score': best_score,
            'alt_scores': alt_scores,
            'all_scores': top_scores,
            'confidence': ou_link['confidence'],
            'massacre_adjusted': massacre_adjusted,
            'context_override': context_override,
            'context_reason': context_override_reason,
        }

# ════════════════════════════════════════════════════
# 主管道
# ════════════════════════════════════════════════════
