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
            # 双求生战 → 双方都需要赢, 方向不明确 → 主队方向+高总球
            primary, secondary = '主胜', '客胜'
            strategy = '双求生对攻(链0战意覆盖)'
            rationale = context_override_reason
            evidence.append('🔥 链0战意覆盖: 双求生, 大球方向')
        elif context_override and '搏命' in context_override:
            # ═══ v5.10: 已出线队不搏命 ═══
            # 6分已出线 → 轮换衰减, 不强制方向。哥伦比亚069案例: 6分轮换→实际0-0
            team = ctx.get('must_win_team', '?')
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
            # 默认策略
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
                primary, secondary = '主胜', '让胜'

        # ── 最终比分 ──
        top_scores = ou_link['scores'][:3]
        best_score = top_scores[0]
        alt_scores = top_scores[1:3]

        # Chain -1 屠杀预警 → 比分向上修正
        # P0-fix (6/27回测): 让2球不穿律场景不修正比分 (走水为主)
        massacre_adjusted = False
        if form_result and form_result.massacre_warning:
            try:
                adj = max(1, round(form_result.score_adjustment))
                parts = best_score.split('-')
                h = int(parts[0]); a = int(parts[1])
                if form_result.goal_diff_advantage < 0:
                    a += adj; h = max(0, h - max(0, adj - 2))
                else:
                    h += adj; a = max(0, a - max(0, adj - 2))
                best_score = f"{h}-{a}"
                # 备选也修正
                new_alts = []
                for s in alt_scores:
                    ph, pa = s.split('-')
                    nh = int(ph); na = int(pa)
                    if form_result.goal_diff_advantage < 0:
                        na = min(na + adj, 7); nh = max(0, nh - max(0, adj - 2))
                    else:
                        nh = min(nh + adj, 7); na = max(0, na - max(0, adj - 2))
                    new_alts.append(f"{nh}-{na}")
                alt_scores = new_alts
                top_scores[0] = best_score
                massacre_adjusted = True
            except (ValueError, IndexError):
                pass

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

        # ═══ 多约束评分优化 (WC2026真实数据 + 方向容错) ═══
        # best_score: 预测方向硬约束Top-1
        # alt_scores[0]: 预测方向Top-2 (方向对比分差一点)
        # alt_scores[1]: 反向/平局容错Top-1 (方向错时兜底)
        try:
            from pipeline.predictors.helpers import fault_tolerant_scores, _score_dir
            target_dir = _score_dir(best_score)
            honesty = ou_link.get('ou_honesty', {})
            ou_grade = honesty.get('grade', 'honest_mid')
            is_under = 'trap_low' in ou_grade or 'honest_low' in ou_grade \
                       or ou_link.get('verdict', '').startswith('OU小')
            target_ou = 'U' if is_under else 'O'
            
            ft_scores = fault_tolerant_scores(target_dir, target_ou, match.ou_line)
            if ft_scores:
                best_score = ft_scores[0]
                alt_scores = ft_scores[1:3]
                top_scores = ft_scores
                # 标注容错方向
                if len(ft_scores) >= 3:
                    alt_dir = _score_dir(ft_scores[2])
                    if alt_dir != target_dir:
                        evidence.append(f'🛡️ 容错: {target_dir}→{best_score}, 备选含{alt_dir}方向{ft_scores[2]}')
                    else:
                        evidence.append(f'📊 优化: {target_dir}方向+{target_ou}球 → {best_score}')
                else:
                    evidence.append(f'📊 优化: {target_dir}方向+{target_ou}球 → {best_score}')
        except Exception as e:
            pass  # 降级: 用原始比分锚

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
