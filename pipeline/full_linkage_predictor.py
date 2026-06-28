"""
FootballAI v4.9 • 全链路联动预测管道
======================================
整合项目全部预测组件为统一管道, 确保分析比赛时全链路联动而非单一维度:

  输入: 欧赔(1X2) + 让球 + 大小球 + 首发阵容
    │
    ├─ [链-1] TeamFormFetcher — 球队近10场真实战绩
    │         └→ 场均净胜差 + 屠杀预警 + 防守崩盘检测
    ├─ [链0] MatchContextAnalyzer — 战意/出线形势
    │         └→ 默契平局 / 双求生战 / 动机倍率
    ├─ [链0.5] LiveMovementSignal — 临场升盘
    │         └→ 外围 vs 竞彩让球对比
    ├─ [链1] OU联动推理 (OULinkageEngine)
    │         └→ HCP+OU→比分锚 + OU诚实度
    ├─ [链2] D-Gate v5.3 风控
    │         └→ 平局风险 + 庄家意图
    ├─ [链3] UnifiedPredictor v4.1 模型
    │         └→ H/D/A概率 + λ融合
    ├─ [链4] TaoGe策略决策
    │         └→ 让胜/让平/胜/平 + 比分锚点
    ↓
  输出: 统一预测报告 (逐链证据 + 加权决策 + 比分推荐)

用法:
    python pipeline/full_linkage_predictor.py --match 挪威vs法国 --hcp +0.5 --ou 2.25 --odds 4.05,3.55,1.80

依赖:
    - FootballAI v4.1 模型 (football_v4.1_production.joblib)
    - D-Gate v5.3 引擎 (rules/d_gate_engine.py)
    - 四维交叉模型 (集成内联)
    - 球队战绩分析 (pipeline/team_form_fetcher.py)
"""

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
    # Pure Python fallback for basic array ops
    class _FakeArray:
        def __init__(self, data):
            self.data = list(data)
        def copy(self): return _FakeArray(self.data)
        def __iter__(self): return iter(self.data)
        def __getitem__(self, i): return self.data[i]
        def __len__(self): return len(self.data)
        def sum(self): return sum(self.data)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ════════════════════════════════════════════════════
# Layer 0: 数据结构
# ════════════════════════════════════════════════════

@dataclass
class MatchInput:
    """比赛原始输入"""
    home: str
    away: str
    odds_h: float
    odds_d: float
    odds_a: float
    hcp: float           # 让球 (-1=主让1球, +0.5=主受让0.5) — 外围初盘
    ou_line: float       # 大小球盘口 (2.0, 2.25, 2.5, 2.75, 3.0)
    over_water: float = 1.90
    under_water: float = 1.92
    matchday: int = 3
    r3_rotation: bool = False  # R3轮换信号
    # Chain -1 阵容信息 (从首发分析获得)
    home_formation: str = ''       # 主队阵型 (如 '4-1-2-3')
    away_formation: str = ''       # 客队阵型
    home_full_strength: bool = True  # 主队是否全主力
    away_full_strength: bool = True  # 客队是否全主力
    home_missing_stars: str = ''     # 主队缺阵球星 (如 '哈兰德,厄德高')
    away_missing_stars: str = ''     # 客队缺阵球星
    sporttery_hcp: float = 0.0       # 竞彩让球 (0=无竞彩数据, 非零=竞彩实盘)

    @property
    def hcp_depth(self) -> float:
        """让球深度 (优先竞彩, 回退外围)"""
        if self.sporttery_hcp:
            return abs(self.sporttery_hcp)
        return abs(self.hcp)

    @property
    def hcp_direction(self) -> str:
        """让球方向"""
        if self.hcp < 0:
            return '主让'
        elif self.hcp > 0:
            return '客让'
        return '平手'

    @classmethod
    def from_odds_snapshot(cls, home: str, away: str,
                           odds_1x2: str, hcp_str: str, ou_str: str,
                           ou_odds: str = "1.90/1.92",
                           r3: bool = False) -> 'MatchInput':
        """从截图格式快速构造"""
        oh, od, oa = map(float, odds_1x2.split(','))
        hcp = float(hcp_str)
        ou_line = float(ou_str)
        over_w, under_w = map(float, ou_odds.split('/'))
        return cls(
            home=home, away=away,
            odds_h=oh, odds_d=od, odds_a=oa,
            hcp=hcp, ou_line=ou_line,
            over_water=over_w, under_water=under_w,
            r3_rotation=r3
        )

@dataclass
class ChainResult:
    """单链输出"""
    chain_name: str
    verdict: str           # H/A/D
    draw_prob: float
    confidence: float
    signals: List[str]
    metadata: Dict = field(default_factory=dict)

# ════════════════════════════════════════════════════
# Layer 1: OU联动推理引擎 (核心)
# ════════════════════════════════════════════════════

class OULinkageEngine:
    """
    OU锚定联动推理: 输入(让球深度, OU盘口) → 输出(比分区间, 1X2倾向)
    v2.0: 全量7场回测验证, 非屠杀队命中率100%(6/6)
    v3.0: OU诚实度分级 (整数盘=诚实, 分裂盘=陷阱)
    """

    # 历史比分分布 (46场回测)
    SCORE_BY_GOALS = {
        0: {'0-0': 1.00},
        1: {'1-0': 0.88, '0-1': 0.12},
        2: {'1-1': 0.73, '2-0': 0.27},
        3: {'2-1': 0.43, '3-0': 0.29, '0-3': 0.28},
        4: {'3-1': 0.67, '2-2': 0.33},
        5: {'4-1': 0.50, '3-2': 0.30, '5-0': 0.20},
        6: {'5-1': 0.50, '4-2': 0.33, '6-0': 0.17},
    }

    # 屠杀惯性队 (穿盘例外 + OU诚实度豁免)
    # 标准: 本届WC场均进球≥2.0的强队, 对阵弱队时OU分裂线不适用
    # 数据来源: WC2026已完赛38场统计
    MASSACRE_TEAMS = {'巴西', '德国', '荷兰', '美国', '加拿大'}
    # 准屠杀 (场均≥2.0但≤2.5): 分裂线陷阱半豁免
    NEAR_MASSACRE = {'瑞士', '瑞典', '英格兰', '法国', '阿根廷', '日本', '伊朗'}

    # ═══ OU诚实度分级 v5.1 (方向性修正+适度倍率) ═══
    # 原理: 庄家靠滚球赚钱 → OU线必须能制造最大滚球参与度
    # 整数盘(2.0/2.5/3.0) = 诚实 → 实际进球≈OU线
    #
    # ⚠️ 分裂线方向性 (v5.1修正):
    #   低于标准线 → 庄家诱导买大 → 实际小球 (trap_low, mult<1.0)
    #   高于标准线 → 庄家诱导买小 → 实际偏大球 (trap_high_side, mult≈1.0)
    #   关键: mult不宜过大, 否则Poisson摊平概率导致方向混乱
    OU_HONESTY = {
        2.0:  ('honest_low',      1.5, 0.90, '庄家诚实小球'),
        2.25: ('trap_low',        1.0, 0.70, '⚠️ 分裂线陷阱! 诱导买大→实际≤1球'),
        2.5:  ('honest_mid',      2.5, 1.00, '庄家诚实中球, 实际2-3球'),
        2.75: ('trap_high_side',  2.7, 0.95, '⚠️ 高侧分裂线! 诱导买小→实际≥2.5球'),
        3.0:  ('honest_high',     3.0, 0.95, '庄家诚实大球'),
        3.25: ('trap_high',       3.2, 0.95, '⚠️ 高线分裂! 诱导买小→实际≥3球'),
        3.5:  ('honest_high',     3.5, 0.90, '高OU诚实盘(注意屠杀豁免)'),
    }

    @classmethod
    def get_ou_honesty(cls, line: float) -> dict:
        """获取OU诚实度分级"""
        # 精确匹配
        if line in cls.OU_HONESTY:
            grade, exp_g, mult, note = cls.OU_HONESTY[line]
            return {'line': line, 'grade': grade, 'exp_goals': exp_g, 'honesty_mult': mult, 'note': note}
        # 近似匹配 (取最接近的)
        closest = min(cls.OU_HONESTY.keys(), key=lambda x: abs(x - line))
        grade, exp_g, mult, note = cls.OU_HONESTY[closest]
        return {'line': line, 'grade': grade, 'exp_goals': exp_g, 'honesty_mult': mult,
                'note': f'≈{closest}: {note}', 'matched_line': closest}

    @staticmethod
    def classify_ou(line: float) -> str:
        if line <= 2.0:
            return 'small'
        elif line <= 2.5:
            return 'medium'
        return 'large'

    @staticmethod
    def classify_hcp(hcp: float) -> str:
        """让球深度分级 (外围/竞彩让球)
        
        分级标准:
          very_deep ≥ 1.75 (让2球+)
          deep      ≥ 0.75 (让1球)
          medium    ≥ 0.25 (让半球)
          level     ≈ 0    (平手)
        """
        depth = abs(hcp)
        if depth >= 1.75:
            return 'very_deep'
        elif depth >= 0.75:
            return 'deep'
        elif depth >= 0.25:
            return 'medium'
        return 'level'

    # ═══ HCP+OU 交叉联动矩阵 ═══
    # 赛果四维联动铁律: 让球深度+OU盘口联合约束比分可行集
    # 法则1: 深让小球走水 (hcp≥0.75+OU≤2)
    # 法则2: 深让大球偏冷 (hcp≥0.75+OU≥2.75)
    # 法则3: 平手小球平局 (hcp≈0+OU≤2)
    # 法则4: 两球走水双路径 (hcp≥1.75)
    LINKAGE_MATRIX = {
        # (hcp_class, ou_band) → (verdict, scores, confidence, note)
        # 法则1: 深让小球走水
        ('deep', 'small'):      ('深让小球走水', ['0-2', '1-0', '0-1'], 0.80, 'hcp≥0.75+OU≤2: 强队赢但净胜≈盘口'),
        # 法则2: 深让大球偏冷
        ('deep', 'large'):      ('深让大球偏冷', ['2-1', '1-1', '2-2'], 0.65, 'hcp≥0.75+OU≥2.75: 弱队有进球能力'),
        # 法则3: 平手小球平局
        ('level', 'small'):     ('平手小球平局', ['0-0', '1-0', '0-1'], 0.75, 'hcp≈0+OU≤2: 双方保守低分'),
        # 法则4: 两球走水双路径
        ('very_deep', 'small'): ('两球走水(小球)', ['0-2', '1-0', '0-0'], 0.75, 'hcp≥1.75+OU≤2: 走水0-2路径'),
        ('very_deep', 'medium'):('两球走水(中球)', ['0-2', '1-1', '1-0'], 0.70, 'hcp≥1.75+OU中: 走水或1球小胜'),
        ('very_deep', 'large'): ('两球走水(大球)', ['1-3', '2-2', '1-2'], 0.65, 'hcp≥1.75+OU≥2.75: 走水1-3路径'),
        # 中让 (0.25-0.75)
        ('medium', 'small'):    ('中让小球', ['1-0', '0-0', '0-1'], 0.70, 'hcp≈0.25-0.75+OU小: 低分'),
        ('medium', 'medium'):   ('中让中球', ['1-0', '1-1', '2-1'], 0.65, 'hcp≈0.25-0.75+OU中: 中性'),
        ('medium', 'large'):    ('中让大球', ['2-1', '1-1', '1-2'], 0.60, 'hcp≈0.25-0.75+OU大: 偏大'),
        # 其他组合
        ('deep', 'medium'):     ('深让中球', ['1-0', '2-1', '1-1'], 0.70, 'hcp≥0.75+OU中: 稳健1球胜'),
        ('level', 'medium'):    ('平手中球', ['1-1', '1-0', '0-1'], 0.65, 'hcp≈0+OU中: 中性'),
        ('level', 'large'):     ('平手大球', ['2-1', '1-1', '2-2'], 0.60, 'hcp≈0+OU大: 开放对攻'),
    }

    @classmethod
    def infer(cls, match: MatchInput) -> Dict[str, Any]:
        """OU联动推理主入口"""
        # 屠杀队豁免
        if match.home in cls.MASSACRE_TEAMS or match.away in cls.MASSACRE_TEAMS:
            massacre_home = match.home in cls.MASSACRE_TEAMS
            if massacre_home:
                scores = ['3-1', '4-1', '2-0']   # 屠杀队主场大胜
            else:
                scores = ['1-3', '1-4', '0-3']   # 屠杀队客场大胜
            return {
                'law': '🔴 屠杀豁免',
                'verdict': '屠杀穿盘',
                'scores': scores,
                'confidence': 0.55,
                'note': f'{match.home if massacre_home else match.away} 场均3+球, 联动法则不适用'
            }

        ou_band = cls.classify_ou(match.ou_line)

        # ═══ OU诚实度调整 v3.0 ═══
        honesty = cls.get_ou_honesty(match.ou_line)
        honesty_mult = honesty['honesty_mult']
        exp_goals = honesty['exp_goals']
        honesty_grade = honesty['grade']

        # 屠杀队OU豁免: 屠杀队打弱队时分裂线可能是诚实的
        has_massacre_home = match.home in cls.MASSACRE_TEAMS
        has_massacre_away = match.away in cls.MASSACRE_TEAMS
        has_near_home = match.home in cls.NEAR_MASSACRE
        has_near_away = match.away in cls.NEAR_MASSACRE

        if has_massacre_home or has_massacre_away:
            if match.ou_line >= 3.0:
                honesty_grade = 'honest_massacre'
                honesty_mult = 1.0
                exp_goals = match.ou_line + 0.5
                honesty['note'] = '🔴 屠杀豁免: OU分裂线对屠杀队不适用, 按诚实大球处理'
            elif has_massacre_home and not has_massacre_away:
                if 'trap' in honesty_grade and match.ou_line >= 2.5:
                    honesty_grade = 'massacre_trap_override'
                    honesty_mult = 0.95
                    exp_goals = match.ou_line
                    honesty['note'] = '🟡 屠杀队+分裂线: 陷阱效应减半(屠杀队能进球)'
            elif has_massacre_away and not has_massacre_home:
                if 'trap' in honesty_grade:
                    honesty_mult = min(honesty_mult + 0.1, 0.95)
        elif has_near_home or has_near_away:
            if 'trap' in honesty_grade:
                honesty_mult += 0.05
                honesty['note'] += ' (准屠杀轻微豁免)'

        # OU波段默认比分锚
        OU_SCORE_MAP = {
            'small':  ('OU小比分', ['0-0', '1-0', '0-1'], 0.75),
            'medium': ('OU中比分', ['1-1', '1-0', '2-1'], 0.65),
            'large':  ('OU大比分', ['2-1', '2-2', '1-1'], 0.60),
        }
        verdict, scores, conf = OU_SCORE_MAP.get(ou_band, ('手动判断', ['1-1', '1-0', '0-1'], 0.50))

        # ═══ HCP深度联动 (让球+OU交叉, 赛果四维铁律) ═══
        # 让球深度+OU盘口联合约束比分可行集
        # 当HCP深度≥0.25时, LINKAGE_MATRIX覆盖OU默认判决
        hcp_class = cls.classify_hcp(match.hcp_depth)
        link_key = (hcp_class, ou_band)
        if link_key in cls.LINKAGE_MATRIX and hcp_class != 'level':
            law_override, hcp_scores, hcp_conf, hcp_note = cls.LINKAGE_MATRIX[link_key]
            verdict = law_override
            scores = hcp_scores[:]
            conf = hcp_conf
            # 记录HCP联动覆盖 (供TaoGe策略参考)
            hcp_override_note = hcp_note
        else:
            hcp_override_note = None

        # ═══ v5.10: 分裂线陷阱降权 (替代硬排除) ═══
        if 'trap' in honesty_grade and honesty_mult < 0.95:
            conf = min(conf, 0.70)
            exp_goals = honesty['exp_goals']
            weighted = []
            for s in scores:
                total = int(s.split('-')[0]) + int(s.split('-')[1])
                if total <= round(exp_goals) + 1:
                    weighted.append((1.5, s))
                else:
                    weighted.append((0.4, s))
            weighted.sort(key=lambda x: -x[0])
            scores = [s for _, s in weighted]
            if not scores:
                scores = ['1-0', '0-0', '1-1'][:3]

        return {
            'ou_band': ou_band,
            'law': f'ou_{ou_band}',
            'verdict': verdict,
            'scores': scores,
            'confidence': conf * honesty_mult,
            'exp_goals': exp_goals,
            'ou_honesty': {
                'grade': honesty_grade,
                'note': honesty['note'],
                'multiplier': honesty_mult,
            },
            'hcp_class': hcp_class,
            'hcp_override': hcp_override_note,
        }

# ════════════════════════════════════════════════════
# Layer 2: D-Gate v5.3 风险层
# ════════════════════════════════════════════════════

class DGateLayer:
    """D-Gate v5.3 风险分析适配器

    P0-3: 支持 Chain -1 form_result 注入，自动压制净胜差≥2球时的深盘陷阱误判
    """

    @staticmethod
    def assess(match: MatchInput, form_result: Any = None) -> ChainResult:
        """运行 D-Gate 风控分析

        Args:
            match: 比赛输入
            form_result: Chain -1 TeamFormResult, P0-3 用于陷阱压制
        """
        signals = []
        trap_suppressed = False  # P0-3
        try:
            from rules.d_gate_engine import apply_dgate_v51

            # 计算隐含概率
            imp_sum = 1/match.odds_h + 1/match.odds_d + 1/match.odds_a
            imp_h = 1/(match.odds_h * imp_sum)
            imp_d = 1/(match.odds_d * imp_sum)
            imp_a = 1/(match.odds_a * imp_sum)

            dg_result = apply_dgate_v51(
                imp_h=imp_h,
                imp_d=imp_d,
                imp_a=imp_a,
                odds={'H': match.odds_h, 'D': match.odds_d, 'A': match.odds_a},
                handicap=match.hcp,
                ou_line=match.ou_line,
                group_round=match.matchday,
            )

            # P0-3: Chain -1 战绩数据交叉验证 — 压制深盘陷阱误判
            if form_result and form_result.is_valid:
                abs_gap = abs(form_result.goal_diff_advantage)
                hcp_depth = abs(match.hcp)
                # 三层阈值判定 (渡庄生方案)
                if abs_gap >= 2.0 and hcp_depth >= 1.5:
                    # 净胜差≥2 + 深让 → 让球=实力反映，禁止陷阱
                    trap_suppressed = True
                    signals.append(f'陷阱压制: 净胜差{abs_gap:.1f}证实深盘合理性')
                elif abs_gap >= 1.0 and getattr(form_result.home, 'defensive_collapse', False) and hcp_depth >= 1.5:
                    # 防守崩盘豁免
                    trap_suppressed = True
                    signals.append(f'陷阱压制: 防守崩盘+净胜差{abs_gap:.1f}豁免')
                elif abs_gap >= 1.0 and form_result.massacre_warning:
                    # 屠杀预警覆盖
                    trap_suppressed = True
                    signals.append(f'陷阱压制: 屠杀预警覆盖')

            dgate_active = dg_result.get('dgate_active', False)
            risk_tag = dg_result.get('risk_tag', 'neutral')
            draw_boost = dg_result.get('draw_boost', 0.0)

            # ═══ P0: 超热门情境平局过滤器 v5.7 ═══
            # imp>85% 超热门 → 检查情境三问 → 强制激活D-Gate模式C
            strong_imp = max(imp_h, imp_a)
            context_draw_boost = 0.0
            context_tag = ''
            if strong_imp > 0.85:
                # 问1: R3轮换?
                if getattr(match, 'r3_rotation', False):
                    context_draw_boost = max(context_draw_boost, 2.0)
                    context_tag = 'R3轮换'
                    signals.append(f'情境: R3轮换, super_imp={strong_imp:.1%}, 平局boost+{context_draw_boost:.1f}')
                # 问2: 默契平局?
                if form_result and form_result.is_valid:
                    gap = abs(form_result.goal_diff_advantage)
                    if gap < 0.5 and max(imp_h, imp_a) < 0.50 and imp_d > 0.25:
                        context_draw_boost = max(context_draw_boost, 2.5)
                        context_tag = '默契情境'
                        signals.append(f'情境: 实力接近+低盘口, 平局boost+{context_draw_boost:.1f}')
                # 问3: OU≤2.5 + 超热门 → 小球平局场景
                if match.ou_line <= 2.5:
                    context_draw_boost = max(context_draw_boost, 1.5)
                    if not context_tag:
                        context_tag = '小球预警'
                    signals.append(f'情境: OU≤2.5+super_imp={strong_imp:.1%}, 平局boost+{context_draw_boost:.1f}')
            
            # 情境boost融合到D-Gate
            if context_draw_boost > 0:
                draw_boost = draw_boost + context_draw_boost * 0.3
                dgate_active = True  # 强制激活
                signals.append(f'超热门情境过滤器: {context_tag} draw_boost→{draw_boost:.3f}')

            # P0-3: 如果陷阱被压制，清除 ignore_draw 标签
            if trap_suppressed and 'ignore_draw' in risk_tag:
                risk_tag = risk_tag.replace('ignore_draw', '').strip('_')
                if not risk_tag:
                    risk_tag = 'neutral'

            if dgate_active:
                signals.append(f'D-Gate激活:{dg_result.get("dgate_mode","?")}')
            if risk_tag != 'neutral':
                signals.append(f'风险标记:{risk_tag}')
            if draw_boost > 0.1:
                signals.append(f'平局boost:{draw_boost:.3f}')

            # 隐含概率
            imp_sum = 1/match.odds_h + 1/match.odds_d + 1/match.odds_a
            draw_imp = 1/(match.odds_d * imp_sum)

            return ChainResult(
                chain_name='D-Gate v5.3',
                verdict='D' if dgate_active or draw_boost > 0.1 else '?',
                draw_prob=draw_imp + draw_boost * 0.3,
                confidence=0.7 if dgate_active else 0.5,
                signals=signals,
                metadata={
                    'dgate_active': dgate_active,
                    'risk_tag': risk_tag,
                    'draw_boost': draw_boost,
                    'trap_suppressed': trap_suppressed,  # P0-3
                }
            )
        except Exception as e:
            imp_sum = 1/match.odds_h + 1/match.odds_d + 1/match.odds_a
            draw_imp = 1/(match.odds_d * imp_sum)
            return ChainResult(
                chain_name='D-Gate v5.3',
                verdict='?',
                draw_prob=draw_imp,
                confidence=0.3,
                signals=[f'DGATE_ERR:{e}'],
            )

# ════════════════════════════════════════════════════
# Layer 3: UnifiedPredictor 模型推理层
# ════════════════════════════════════════════════════

class ModelLayer:
    """UnifiedPredictor v4.1 模型推理适配器"""

    @staticmethod
    def assess(match: MatchInput) -> ChainResult:
        """运行 v4.1 Stacking 模型推理"""
        signals = []
        try:
            from predictors.unified_predictor import UnifiedPredictor
            up = UnifiedPredictor()
            result = up.predict(
                home=match.home, away=match.away,
                odds_h=match.odds_h, odds_d=match.odds_d, odds_a=match.odds_a,
                asian_handicap=match.hcp,
                ou_line=match.ou_line,
                over_water=match.over_water,
                under_water=match.under_water,
            )

            probs = result.get('probabilities', {})
            draw_prob = probs.get('D', probs.get('draw', 0.0))
            trap = result.get('trap_level', 'none')
            raw_verdict = result.get('prediction', '?')

            if trap != 'none':
                signals.append(f'陷阱:{trap}({result.get("trap_type","?")})')

            return ChainResult(
                chain_name='UnifiedPredictor v4.1',
                verdict=raw_verdict,
                draw_prob=float(draw_prob),
                confidence=float(result.get('confidence', 0.5)),
                signals=signals,
                metadata={
                    'probs': {k: float(v) for k, v in probs.items()} if isinstance(probs, dict) else {},
                    'lambda_info': result.get('lambda_info', {}),
                    'trap_level': trap,
                }
            )
        except Exception as e:
            imp_sum = 1/match.odds_h + 1/match.odds_d + 1/match.odds_a
            draw_imp = 1/(match.odds_d * imp_sum)
            return ChainResult(
                chain_name='UnifiedPredictor v4.1',
                verdict='?',
                draw_prob=draw_imp,
                confidence=0.3,
                signals=[f'MODEL_ERR:{e}'],
            )

# ════════════════════════════════════════════════════
# Layer 3.5: 临场升盘信号层 (Live Movement Signal)
# ════════════════════════════════════════════════════

class LiveMovementSignal:
    """
    临场升盘分析引擎 v1.0
    对比外围初盘(offshore) vs 竞彩实盘(sporttery.cn)的让球深度差,
    识别庄家升/降盘信号。

    核心原理:
    - 外围初盘更接近"庄家真实判断"(需要滚球流量)
    - 竞彩深让 = 后期资金驱动或制造热门
    - △≥0.75 或 从平手直接到受让 = 高概率诱盘

    数据来源:
    - 外围: 2026WC/6.27/ 截图 (原始赔率)
    - 竞彩: sporttery.cn 实时赔率
    """

    # 竞彩让球数据 (从sporttery.cn获取, 相对于主队视角)
    # 符号约定: 负=主队让球, 正值=主队受让 (与MatchInput.hcp一致)
    # 竞彩[+N]=主队受让N球 →正值,  竞彩[-N]=主队让N球→负值
    SPORTTERY_HCP_627 = {
        ('挪威', '法国'):       +1.0,   # 竞彩[+1]: 挪威受让1球(法国让1球)
        ('塞内加尔', '伊拉克'): -2.0,   # 竞彩[-2]: 塞内加尔让2球
        ('佛得角共和国', '沙特阿拉伯'): -1.0,  # 竞彩[-1]: 佛得角让1球(沙特受让1球)
        ('乌拉圭', '西班牙'):   +1.0,   # 竞彩[+1]: 乌拉圭受让1球(西班牙让1球)
        ('埃及', '伊朗'):       -1.0,   # 竞彩[-1]: 埃及让1球
        ('新西兰', '比利时'):   +2.0,   # 竞彩[+2]: 新西兰受让2球(比利时让2球)
    }

    # 信号等级阈值
    THRESHOLDS = {
        'normal':     0.25,   # 正常波动, 方向可信
        'caution':    0.50,   # 需交叉验证
        'warning':    0.75,   # 高概率诱盘
        'danger':     1.00,   # 极度异常, 超级诱盘
    }

    @classmethod
    def analyze(cls, match: MatchInput) -> Dict[str, Any]:
        """分析临场升盘信号"""
        key = (match.home, match.away)

        # 竞彩让球数据来源优先级: 硬编码字典 → match.sporttery_hcp → 无数据
        sporttery_hcp = None
        if key in cls.SPORTTERY_HCP_627:
            sporttery_hcp = cls.SPORTTERY_HCP_627[key]
        elif match.sporttery_hcp and abs(match.sporttery_hcp) > 0.01:
            sporttery_hcp = match.sporttery_hcp

        if sporttery_hcp is None:
            return {
                'signal': 'no_data',
                'depth_diff': 0,
                'offshore_hcp': match.hcp,
                'sporttery_hcp': None,
                'grade': 'unknown',
                'interpretation': '无竞彩对比数据',
                'trap_risk': 0.0,
                'adjustment': {},
            }
        offshore_hcp = match.hcp

        # 计算深度差 (取绝对值的差异)
        # 注意: 让球方向可能不同, 需要统一比较"强队受让深度"
        offshore_depth = abs(offshore_hcp)
        sporttery_depth = abs(sporttery_hcp)

        depth_diff = sporttery_depth - offshore_depth

        # 判定信号等级
        if depth_diff <= cls.THRESHOLDS['normal']:
            grade = 'normal'
            signal_type = 'market_adjust'
            trap_risk = 0.1
        elif depth_diff <= cls.THRESHOLDS['caution']:
            grade = 'caution'
            signal_type = 'deep_move'
            trap_risk = 0.35
        elif depth_diff <= cls.THRESHOLDS['warning']:
            grade = 'warning'
            signal_type = 'trap_signal'
            trap_risk = 0.65
        else:
            grade = 'danger'
            signal_type = 'super_trap'
            trap_risk = 0.85

        # 特殊检测: 平手→深让 (最危险信号)
        is_level_to_deep = (abs(offshore_hcp) < 0.26) and (abs(sporttery_hcp) >= 0.75)
        if is_level_to_deep:
            grade = 'danger'
            signal_type = 'level_to_deep_trap'
            trap_risk = 0.90

        # 特殊检测: 同向确认 (外围已深+竞彩更深=真实看好)
        is_confirming = (offshore_depth >= 1.25) and (depth_diff > 0)
        if is_confirming:
            grade = 'confirmed'
            signal_type = 'same_direction_confirm'
            trap_risk = 0.15

        # 生成解读
        interpretations = {
            'normal': '市场正常调整, 方向基本可信',
            'caution': '中度升盘, 需结合战意判断',
            'warning': '⚠️ 异常升盘! 可能是诱盘陷阱',
            'danger': '🚨 极度异常! 经典诱盘结构',
            'confirmed': '✅ 同向确认: 外围+竞彩一致深让',
            'level_to_deep_trap': '🚨🚨 最危险: 外围平手→竞彩突然深让!',
            'same_direction_confirm': '✅ 同向确认: 屠杀/实力碾压信号',
            'deep_move': '中度升盘: 市场向热门方向调整',
            'market_adjust': '轻微调整: 正常市场波动',
        }

        interpretation = interpretations.get(signal_type, f'未知信号({signal_type})')

        # 调整建议
        adjustment = {}
        if trap_risk > 0.6:
            adjustment['confidence_penalty'] = -0.15 * trap_risk
            adjustment['suggest'] = '反向操作: 追冷门/让球方走水'
            adjustment['score_shift'] = 'towards_underdog'
        elif trap_risk < 0.2:
            adjustment['confidence_bonus'] = 0.05
            adjustment['suggest'] = '方向可跟: 热门方向有支撑'
            adjustment['score_shift'] = 'towards_favorite'
        else:
            adjustment['suggest'] = '谨慎观望: 降低仓位'

        return {
            'signal': signal_type,
            'depth_diff': round(depth_diff, 2),
            'offshore_hcp': offshore_hcp,
            'sporttery_hcp': sporttery_hcp,
            'offshore_display': f'{"主让" if offshore_hcp < 0 else "客让" if offshore_hcp > 0 else "平手"}{abs(offshore_hcp)}',
            'sporttery_display': f'{"主让" if sporttery_hcp < 0 else "客让" if sporttery_hcp > 0 else "平手"}{abs(sporttery_hcp)}',
            'grade': grade,
            'trap_risk': round(trap_risk, 2),
            'interpretation': interpretation,
            'is_level_to_deep': is_level_to_deep,
            'is_confirming': is_confirming,
            'adjustment': adjustment,
        }

    @classmethod
    def get_movement_summary_table(cls) -> List[Dict]:
        """生成六场汇总表"""
        rows = []
        for (home, away), st_hcp in cls.SPORTTERY_HCP_627.items():
            # 找对应的外围让球
            match_data = None
            for m in MATCHES_6_27:
                if m.home == home and m.away == away:
                    match_data = m
                    break
            if match_data is None:
                continue

            analysis = cls.analyze(match_data)
            rows.append({
                'match': f'{home} vs {away}',
                'offshore': f'{analysis["offshore_display"]}',
                'sporttery': f'{analysis["sporttery_display"]}',
                'diff': analysis['depth_diff'],
                'grade': analysis['grade'],
                'trap_risk': analysis['trap_risk'],
                'interpretation': analysis['interpretation'][:30],
            })
        return rows

# ════════════════════════════════════════════════════
# Layer 4: TaoGe 策略决策层
# ════════════════════════════════════════════════════

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

def _constrain_ou_to_line(ou_link: dict, match, form_result=None, silent: bool = False) -> dict:
    """
    P0-5: 外围OU盘口约束总进球 (何执策)
    
    问题: LINKAGE_MATRIX固定比分锚不考虑实际球队能力, 导致总进球偏离OU线
    审计: 6/22-24 中5/12场的总进球预测偏差>1.5球
    方案: OU线隐含市场总进球预期, 当预测偏离>1.5球时强制修正
    
    Args:
        ou_link: OU联动结果 (含scores)
        match: MatchInput
        form_result: Chain -1战绩数据(可选, 用于精细化)
    
    Returns:
        dict: {'adjusted': bool, 'scores': [...], 'reason': str}
    """
    import math as _math
    
    scores = ou_link.get('scores', [])
    if not scores:
        return {'adjusted': False, 'scores': scores}
    
    # 1. 获取外围OU线 (优先截图OCR数据 → 竞彩OU)
    ou_line = match.ou_line if hasattr(match, 'ou_line') else 2.5
    
    # 尝试加载截图OU数据
    try:
        import json
        from pathlib import Path
        ou_file = Path(__file__).parent.parent / 'data' / 'ou_screenshot_6_28.json'
        if ou_file.exists():
            with open(ou_file, 'r', encoding='utf-8') as f:
                screenshot_ou = json.load(f)
            match_key = f'{match.home}vs{match.away}'
            if match_key in screenshot_ou:
                ou_line = screenshot_ou[match_key]
                if not silent:
                    print(f"\n  [OU截图] {match_key}: 外围OU={ou_line} (竞彩={match.ou_line})")
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logger.warning("加载截图OU数据失败: %s", e)
    
    # 2. 获取OU隐含总进球预期
    honesty = OULinkageEngine.get_ou_honesty(ou_line)
    expected_total = honesty.get('exp_goals', ou_line)
    honesty_mult = honesty.get('honesty_mult', 1.0)
    
    # 市场隐含λ = OU期望进球 (保守)
    market_lambda = expected_total * honesty_mult
    
    # 2. 计算当前预测的平均总进球
    totals = [int(s.split('-')[0]) + int(s.split('-')[1]) for s in scores]
    avg_total = sum(totals) / len(totals) if totals else 0
    
    # 3. 屠杀豁免: 屠杀λ重标定优先级 > OU约束
    if ou_link.get('massacre_rescaled'):
        if not silent:
            print(f"\n  [OU约束] ⚡ 屠杀λ重标定已生效, 跳过OU约束 (避免冲突)")
        return {'adjusted': False, 'scores': scores, 'reason': '屠杀λ重标定优先级>OU约束'}
    
    # 4. 判断是否需要修正 (偏差>1.2球)
    deviation = abs(avg_total - market_lambda)
    if deviation <= 1.2:
        return {'adjusted': False, 'scores': scores, 'reason': f'OU偏差{deviation:.1f}≤1.2, 无需修正'}
    
    # 5. 用OU隐含λ重新生成Poisson比分
    # λ分配: 根据双方10场GF比例
    lam_total = market_lambda
    if form_result and form_result.is_valid:
        h_gf = form_result.home.avg_gf or 1.0
        a_gf = form_result.away.avg_gf or 1.0
        lam_home = lam_total * h_gf / (h_gf + a_gf)
        lam_away = lam_total * a_gf / (h_gf + a_gf)
    else:
        # 无战绩数据: 默认55/45分配
        lam_home = lam_total * 0.55
        lam_away = lam_total * 0.45
    
    # 生成Poisson Top-8比分
    candidates = []
    for h in range(6):
        for a in range(6):
            try:
                ph = (_math.exp(-lam_home) * lam_home**h) / max(_math.factorial(h), 1)
                pa = (_math.exp(-lam_away) * lam_away**a) / max(_math.factorial(a), 1)
                candidates.append((ph * pa, f'{h}-{a}', h + a))
            except (OverflowError, ValueError):
                continue
    
    # 按概率排序, 取Top-5
    candidates.sort(key=lambda x: -x[0])
    constrained = [s for _, s, _ in candidates[:5]]
    
    if not silent:
        print(f"\n  [OU约束] 预测总球{avg_total:.1f} vs 市场{market_lambda:.1f} 偏差{deviation:.1f}→修正")
        print(f"    λ: home={lam_home:.2f} away={lam_away:.2f} | 修正比分: {constrained}")
    
    return {
        'adjusted': True,
        'scores': constrained,
        'reason': f'OU偏差{deviation:.1f}球→修正(λ: {lam_home:.1f}+{lam_away:.1f})',
        'lambda_home': lam_home,
        'lambda_away': lam_away,
    }

# ════════════════════════════════════════════════════
# 🔥 P0: 三路径对比投票 (v5.7 Agent思维设计)
# 路径A=模型v4.1 | 路径B=D-Gate规则 | 路径C=历史相似场
# 两条一致→采用; 全不一致→D-Gate>模型>历史
# ════════════════════════════════════════════════════

def _vote_three_paths(model_verdict: str, dgate_verdict: str, form_result,
                      match_home: str, match_away: str, hcp: float, ou_line: float):
    """三路径投票裁决 v5.7"""
    path_a = model_verdict  # v4.1模型
    path_b = dgate_verdict  # D-Gate规则
    path_c = '?'  # 历史相似场路径
    
    # 路径C: 基于战绩的方向
    if form_result and form_result.is_valid:
        gap = form_result.goal_diff_advantage
        if abs(gap) >= 2.0:
            path_c = 'H' if gap > 0 else 'A'
        elif abs(gap) >= 1.0:
            path_c = 'H' if gap > 0 else 'A'
        else:
            path_c = 'D'
    
    # 投票计数 (A weight=0.6, B weight=0.9, C weight=0.5)
    votes = {'H': 0, 'D': 0, 'A': 0}
    weight_a = 0.6; weight_b = 0.9; weight_c = 0.5
    
    for v, w in [(path_a, weight_a), (path_b, weight_b), (path_c, weight_c)]:
        if v in votes:
            votes[v] += w
    
    winner = max(votes, key=votes.get)
    consensus = sum(1 for v in [path_a, path_b, path_c] if v == winner)
    
    # 裁决逻辑
    if consensus >= 2:
        verdict = winner
        reason = f'三路径共识({consensus}/3): A={path_a} B={path_b} C={path_c}'
    elif path_b != winner:
        verdict = path_b  # D-Gate优先
        reason = f'D-Gate优先(分歧): A={path_a} B={path_b} C={path_c}'
    else:
        verdict = path_a
        reason = f'模型优先(分歧): A={path_a} B={path_b} C={path_c}'
    
    # 让2球场景历史相似场检索
    similar_match_ref = None
    if abs(hcp) >= 1.5:
        from rules.d_gate_utils import ALL_RESULTS
        for h, a, hg, ag, hcp_ref, _ in ALL_RESULTS:
            if abs(hcp_ref - abs(hcp)) <= 0.5 and abs((hg + ag) - ou_line) <= 1.0:
                similar_match_ref = f'{h}vs{a} {hg}-{ag}(hcp={hcp_ref})'
                break
    
    return {
        'verdict': verdict,
        'votes': votes,
        'consensus': f'{consensus}/3',
        'reason': reason,
        'paths': f'A(model)={path_a} B(D-Gate)={path_b} C(history)={path_c}',
        'similar_match': similar_match_ref,
    }

# ════════════════════════════════════════════════════
# 🔥 P0: 半场动态修正 (v5.7 Agent思维设计)
# 半场比分→下半场预测调整 (收手/拼命/巩固)
# ════════════════════════════════════════════════════

def _half_time_adjust(ht_home: int, ht_away: int, full_pred: dict, 
                      form_result, match_hcp: float) -> dict:
    """半场动态修正 v5.7
    输入: 半场比分, 全场预测, 战绩数据
    输出: 下半场预测调整 + 置信度衰减
    """
    ht_diff = ht_home - ht_away
    ht_total = ht_home + ht_away
    best_score = full_pred.get('best_score', '0-0')
    try:
        pred_h, pred_a = map(int, best_score.split('-'))
    except (ValueError, TypeError):
        pred_h, pred_a = 1, 1
    
    need_h = pred_h - ht_home
    need_a = pred_a - ht_away
    
    situation = 'unknown'
    confidence_decay = 0.0
    adj_notes = []
    
    if abs(ht_diff) >= 2:
        if form_result and form_result.is_valid:
            gap = form_result.goal_diff_advantage
            strong_leading = (gap > 0 and ht_diff > 0) or (gap < 0 and ht_diff < 0)
            if strong_leading:
                situation = '强队大幅领先'
                confidence_decay = 0.35
                adj_notes.append('强队下半场可能收手, 走水概率上升')
                need_h = max(need_h, 0)
                need_a = max(need_a, 0)
                adj_notes.append(f'下半场预期: {need_h}:{need_a}')
            else:
                situation = '弱队意外领先'
                confidence_decay = 0.50
                adj_notes.append('弱队领先不可持续, 强队追分概率高')
                need_h = max(need_h, 0) + (1 if gap > 0 else 0)
                need_a = max(need_a, 0) + (0 if gap > 0 else 1)
                adj_notes.append(f'下半场预期(追分修正): {need_h}:{need_a}')
    elif abs(ht_diff) <= 1:
        if ht_total == 0:
            situation = '半场沉闷'
            confidence_decay = 0.20
            adj_notes.append('0-0半场, 下半场突然爆发概率上升')
        else:
            situation = '半场胶着'
            confidence_decay = 0.10
            adj_notes.append('比分接近, 下半场方向不变')
    
    if abs(match_hcp) >= 1.5:
        adj_notes.append(f'深盘{match_hcp:+.1f}球半场修正: 让球方需净胜{max(match_hcp - ht_diff, 0):.1f}球才能穿盘')
        confidence_decay += 0.05
    
    return {
        'situation': situation,
        'ht_score': f'{ht_home}-{ht_away}',
        'confidence_decay': min(confidence_decay, 0.6),
        'need_second_half': f'{need_h}:{need_a}',
        'notes': adj_notes,
    }

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
        print(f"  🔗 全链路联动预测: {match.home} vs {match.away}")
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
        # 净胜差≥3球或屠杀预警 → 跳过Chain 1-3, 方向由战绩直接决定
        # P0-fix (6/27回测): 竞彩让2球+abs_gap<3 → 让2球不穿律优先, 不短路
        # P0-fix3 (6/27回测): 进攻碾压豁免 — 弱队无进球能力+强队碾压 → 破让2球不穿律
        short_circuit = False
        short_circuit_level = 4  # 默认不短路
        short_circuit_reason = ''
        if form_result and form_result.is_valid:
            abs_gap = abs(form_result.goal_diff_advantage)

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

        # ── 链0: 战意/情境分析 (动机层) ──
        context_adj = {}
        try:
            if self.context_analyzer is None:
                from pipeline.match_context_analyzer import MatchContextAnalyzer
                self.context_analyzer = MatchContextAnalyzer
            context_adj = self.context_analyzer.get_adjustment(match.home, match.away)
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

        # ═══ P0-2: 屠杀λ重标定 (毕正验) ═══
        # 屠杀场景下，用真实场均GF/GA覆写Poisson λ再算比分分布
        if form_result and form_result.massacre_warning and not short_circuit:
            print(f"\n  [链3.5] 屠杀λ重标定...")
            # 获取真实场均数据
            if form_result.goal_diff_advantage > 0:
                real_gf_strong = form_result.home.avg_gf
                real_ga_weak = form_result.away.avg_ga
            else:
                real_gf_strong = form_result.away.avg_gf
                real_ga_weak = form_result.home.avg_ga
            # 使用真实场均构建λ (保守下界：不低于赔率λ)
            # 改动2: 屠杀预警λ放大 ×1.3 (P1回测修复: 屠杀场次强队进球数低估, 27B预测2-3实际5-0)
            lam_strong = max(real_gf_strong, 1.5) * 1.3
            lam_weak = max(real_ga_weak, 0.8)
            # 确定强队在哪一侧 (用于正确分配λ)
            strong_is_home = form_result.goal_diff_advantage > 0
            print(f"    → 真实场均: 强队GF={real_gf_strong:.2f} 弱队GA={real_ga_weak:.2f}")
            print(f"    → 重标定λ(×1.3放大): strong={lam_strong:.2f} weak={lam_weak:.2f}")
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
            massacre_scores = [s for _, s in score_probs[:5]]
            print(f"    → 重标定比分: {massacre_scores}")
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

def run_6_27_full_linkage():
    """批量运行6/27全链路联动分析"""
    pipeline = FullLinkagePipeline()
    results = []

    for m in MATCHES_6_27:
        result = pipeline.predict(m)
        results.append(result)
        print()

    # ── 汇总: 5串1方案 ──
    print(f"\n{'='*60}")
    print(f"  🎫 6/27 全链路联动 • 5串1推荐方案")
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
    parser.add_argument('--batch', action='store_true', help='批量分析6/27全部比赛')
    parser.add_argument('--match', type=str, help='单场分析 (格式: 主队vs客队)')
    parser.add_argument('--hcp', type=float, default=0, help='让球')
    parser.add_argument('--ou', type=float, default=2.5, help='大小球盘口')
    parser.add_argument('--odds', type=str, default='2.0,3.5,3.5', help='1X2赔率 h,d,a')
    parser.add_argument('--r3', action='store_true', help='R3轮换标记')

    args = parser.parse_args()

    if args.batch:
        run_6_27_full_linkage()
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
    else:
        print("用法: python full_linkage_predictor.py --batch  或  --match 挪威vs法国 --odds 4.05,3.55,1.80 --hcp 0.5 --ou 2.25")
