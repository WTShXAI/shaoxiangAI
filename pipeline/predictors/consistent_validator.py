"""P0-2: 7条一致性校验自动化

将原先依赖人工检查的7条一致性规则自动化,
在最终输出前运行, 发现矛盾时报告并触发修正或告警.

7条校验:
1. 让球-比分一致性: 让球推荐(best_score)必须匹配让球结果
2. 1X2-比分一致性: 1X2推荐(best_score)必须匹配主胜/平局/客胜
3. D-Gate平局一致性: D-Gate draw_alert + 非ignore_draw时, best_score应含平局
4. 诱盘一致性: ignore_draw 信号触发时, 推荐方向不应诱向平局
5. OU小球一致性: OU≤2.0或OU小时, best_score总进球不应超过2
6. 让1球冷门律: 0.75≤|hcp|<1.75 + OU≤2.25 + 弱队GF>0.5 → 应双选
7. 屠杀预警一致性: massacre_warning时, best_score应体现强队大胜方向
"""
from typing import Dict, Any, List, Tuple


class ConsistencyValidator:
    CHECK_NAMES = [
        'hcp_score',
        '1x2_score',
        'dgate_draw',
        'trap_direction',
        'ou_small',
        'hcp1_upset',
        'massacre_score',
    ]

    @classmethod
    def validate(cls, match, strategy: Dict, ou_link: Dict, dgate: Any,
                 model: Any, form_result: Any) -> Dict[str, Any]:
        """运行全部7条校验, 返回一致性报告"""
        report = {
            'overall_pass': True,
            'checks': [],
            'failures': [],
        }
        for name in cls.CHECK_NAMES:
            method = getattr(cls, f'_check_{name}')
            ok, note = method(match, strategy, ou_link, dgate, model, form_result)
            report['checks'].append({'name': name, 'pass': ok, 'note': note})
            if not ok:
                report['overall_pass'] = False
                report['failures'].append(name)
        return report

    @staticmethod
    def _parse_score(score_str: str) -> Tuple[int, int]:
        try:
            h, a = map(int, score_str.split('-'))
            return h, a
        except (ValueError, TypeError, AttributeError):
            return -1, -1

    @staticmethod
    def _score_matches_hcp(score_str: str, hcp: float) -> bool:
        """检查比分是否满足让球方赢盘/走水"""
        h, a = ConsistencyValidator._parse_score(score_str)
        if h < 0 or a < 0:
            return False
        hcp_abs = abs(hcp)
        if hcp < 0:  # 主让
            adjusted = (h - a) - hcp_abs
        elif hcp > 0:  # 主受
            adjusted = (h - a) + hcp_abs
        else:
            adjusted = h - a
        return adjusted >= -0.5  # 允许走水/赢盘/小负(不穿但不离谱)

    @staticmethod
    def _check_hcp_score(match, strategy, ou_link, dgate, model, form_result) -> Tuple[bool, str]:
        primary = strategy.get('primary', '')
        best_score = strategy.get('best_score', '0-0')
        if not any(x in primary for x in ['让胜', '让平', '让负']):
            return True, '非让球推荐, 跳过'

        h, a = ConsistencyValidator._parse_score(best_score)
        if h < 0:
            return False, f'比分解析失败: {best_score}'
        hcp = match.hcp

        if '让胜' in primary:
            if hcp < 0:
                ok = (h - a) > abs(hcp)
            elif hcp > 0:
                ok = (a - h) < abs(hcp)  # 主受球后, 主队总分>客队
            else:
                ok = h > a
        elif '让平' in primary:
            if hcp < 0:
                ok = (h - a) == abs(hcp)
            elif hcp > 0:
                ok = (a - h) == abs(hcp)
            else:
                ok = h == a
        elif '让负' in primary:
            if hcp < 0:
                ok = (h - a) < abs(hcp)
            elif hcp > 0:
                ok = (a - h) > abs(hcp)
            else:
                ok = h < a
        else:
            ok = True

        return ok, f'让球{primary} vs 比分{best_score} {"✅" if ok else "❌"}'

    @staticmethod
    def _check_1x2_score(match, strategy, ou_link, dgate, model, form_result) -> Tuple[bool, str]:
        primary = strategy.get('primary', '')
        best_score = strategy.get('best_score', '0-0')
        if any(x in primary for x in ['让胜', '让平', '让负']):
            return True, '让球推荐, 1X2校验跳过'

        h, a = ConsistencyValidator._parse_score(best_score)
        if h < 0:
            return False, f'比分解析失败: {best_score}'

        if '主胜' in primary or primary == '胜':
            ok = h > a
        elif '客胜' in primary or primary == '负':
            ok = a > h
        elif '平' in primary or primary == '平局':
            ok = h == a
        else:
            ok = True

        return ok, f'1X2{primary} vs 比分{best_score} {"✅" if ok else "❌"}'

    @staticmethod
    def _check_dgate_draw(match, strategy, ou_link, dgate, model, form_result) -> Tuple[bool, str]:
        if dgate is None:
            return True, 'D-Gate结果缺失, 跳过'
        risk_tag = dgate.metadata.get('risk_tag', '') if dgate.metadata else ''
        signals = dgate.signals or []
        draw_alert = 'draw_alert' in str(risk_tag) or any('draw_alert' in s for s in signals)
        ignore_draw = 'ignore_draw' in str(risk_tag)
        best_score = strategy.get('best_score', '0-0')
        h, a = ConsistencyValidator._parse_score(best_score)

        if draw_alert and not ignore_draw:
            ok = h == a
            return ok, f'D-Gate draw_alert + 非ignore_draw, best_score应为平局 {"✅" if ok else "❌"}'
        return True, '未触发D-Gate draw_alert 或 ignore_draw激活, 跳过'

    @staticmethod
    def _check_trap_direction(match, strategy, ou_link, dgate, model, form_result) -> Tuple[bool, str]:
        if dgate is None:
            return True, 'D-Gate结果缺失, 跳过'
        risk_tag = dgate.metadata.get('risk_tag', '') if dgate.metadata else ''
        ignore_draw = 'ignore_draw' in str(risk_tag)
        primary = strategy.get('primary', '')
        if ignore_draw and '平' in primary:
            return False, 'ignore_draw诱盘信号触发, 但primary仍指向平局'
        return True, '无诱盘冲突'

    @staticmethod
    def _check_ou_small(match, strategy, ou_link, dgate, model, form_result) -> Tuple[bool, str]:
        ou_line = match.ou_line
        verdict = ou_link.get('verdict', '')
        best_score = strategy.get('best_score', '0-0')
        h, a = ConsistencyValidator._parse_score(best_score)
        if h < 0:
            return False, f'比分解析失败: {best_score}'
        total = h + a

        if ou_line <= 2.0 or 'OU小' in verdict or '小球' in verdict:
            ok = total <= 2
            return ok, f'OU小场景(ou_line={ou_line}, verdict={verdict}), best_score总进球{total} {"✅" if ok else "❌"}'
        return True, f'OU={ou_line}, 非小球场景, 跳过'

    @staticmethod
    def _check_hcp1_upset(match, strategy, ou_link, dgate, model, form_result) -> Tuple[bool, str]:
        hcp_abs = abs(match.hcp)
        ou_line = match.ou_line
        if not (0.75 <= hcp_abs < 1.75):
            return True, f'|hcp|={hcp_abs}, 非让1球区间, 跳过'
        if ou_line > 2.25:
            return True, f'OU={ou_line}>2.25, 不满足冷门触发条件'
        if not form_result or not form_result.is_valid:
            return True, '无战绩数据, 跳过弱队GF判断'

        weak_team_gf = form_result.away.avg_gf if match.hcp > 0 else form_result.home.avg_gf
        if weak_team_gf <= 0.5:
            return True, f'弱队GF={weak_team_gf:.2f}≤0.5, 不满足冷门触发条件'

        rec_type = strategy.get('rec_type', '')
        rec_markets = strategy.get('rec_markets', [])
        ok = '双推' in rec_type or 'AH' in rec_markets
        return ok, f'让1球冷门律(|hcp|={hcp_abs}, OU={ou_line}, 弱队GF={weak_team_gf:.2f}), 应双选 {"✅" if ok else "❌"}'

    @staticmethod
    def _check_massacre_score(match, strategy, ou_link, dgate, model, form_result) -> Tuple[bool, str]:
        if not form_result or not form_result.massacre_warning:
            return True, '无屠杀预警, 跳过'
        best_score = strategy.get('best_score', '0-0')
        h, a = ConsistencyValidator._parse_score(best_score)
        if h < 0:
            return False, f'比分解析失败: {best_score}'

        gap = form_result.goal_diff_advantage
        if gap > 0:
            strong_lead = h - a
        else:
            strong_lead = a - h

        ok = strong_lead >= 1
        return ok, f'屠杀预警(gap={gap:+.2f}), best_score应体现强队大胜 {"✅" if ok else "❌"}'


def run_consistency_checks(match, strategy: Dict, ou_link: Dict, dgate: Any,
                           model: Any, form_result: Any) -> Dict[str, Any]:
    """便捷入口, 与 pipeline 集成"""
    return ConsistencyValidator.validate(match, strategy, ou_link, dgate, model, form_result)
