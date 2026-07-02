"""Full Linkage Predictor — 拆分子模块"""
import os, sys, json, math
import logging
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

from ._compat import np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pipeline.predictors.data_classes import *  # noqa: F401, F403

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
    def get_ou_honesty(cls, line: float, match=None) -> dict:
        """获取OU诚实度分级 (含WC校准)"""
        # 精确匹配
        if line in cls.OU_HONESTY:
            grade, exp_g, mult, note = cls.OU_HONESTY[line]
        else:
            closest = min(cls.OU_HONESTY.keys(), key=lambda x: abs(x - line))
            grade, exp_g, mult, note = cls.OU_HONESTY[closest]
            note = f'≈{closest}: {note}'

        # WC校准: 31K交叉数据库偏移修正
        calib = {'offset': 0, 'direction': 'neutral'}
        if match and hasattr(match, 'odds_h') and match.odds_h > 0:
            calib = cls.get_wc_ou_calibration(match)
            if calib['direction'] != 'neutral' and calib['strength'] in ('medium', 'high'):
                offset = calib['offset']
                exp_g += offset
                note += f' | WC{offset:+.1f}球'
                if calib['strength'] == 'high' and calib['direction'] == 'down':
                    if 'honest' in grade:
                        note += ' (WC模式→该小)'
        
        return {'line': line, 'grade': grade, 'exp_goals': exp_g, 'honesty_mult': mult,
                'note': note, 'wc_calibration': calib}

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

    # ═══ WC校准: 31K交叉数据库偏移 ═══
    _crossref_cache = None

    @classmethod
    def _load_crossref(cls):
        if cls._crossref_cache is not None:
            return cls._crossref_cache
        try:
            import json
            p = os.path.join(ROOT, 'data', 'ou_crossref_database.json')
            if os.path.exists(p):
                cls._crossref_cache = json.load(open(p, encoding='utf-8'))
        except Exception:
            cls._crossref_cache = {}
        return cls._crossref_cache or {}

    @classmethod
    def get_wc_ou_calibration(cls, match: MatchInput) -> Dict[str, Any]:
        """WC校准: 31K历史vs世界杯OU偏移修正 (加权KNN)
        
        用d_prob+spread查找最相似的3个历史bin, 距离加权计算偏移
        """
        cr = cls._load_crossref()
        if not cr:
            return {'offset': 0, 'direction': 'neutral', 'strength': 0}

        oh, od, oa = match.odds_h, match.odds_d, match.odds_a
        ti = 1/oh + 1/od + 1/oa
        oid = round((1/od)/ti, 2)
        spread = round(abs((1/oh)/ti - (1/oa)/ti), 2)
        
        # 加权KNN: 找最近的3个bin
        neighbors = []
        for key, entry in cr.items():
            d_p = entry.get('d_prob', 0.2)
            s_p = entry.get('spread', 0.4)
            dist = abs(d_p - oid) + abs(s_p - spread)
            neighbors.append((dist, entry))
        neighbors.sort(key=lambda x: x[0])
        
        # 加权平均: 距离越近权重越大
        total_w = 0; weighted_offset = 0; weighted_samples = 0
        for dist, entry in neighbors[:3]:
            if dist >= 0.20:  # 太远忽略
                continue
            w = 1.0 / (dist + 0.02)  # 防止除零
            wc_avg = entry.get('wc_avg', 2.5)
            hist_avg = entry.get('hist_avg', 2.5)
            offset = wc_avg - hist_avg
            weighted_offset += w * offset
            weighted_samples += w * entry.get('hist_count', 0)
            total_w += w
        
        if total_w == 0:
            return {'offset': 0, 'direction': 'neutral', 'strength': 0}
        
        offset = weighted_offset / total_w
        strength = 'high' if abs(offset) >= 0.8 else ('medium' if abs(offset) >= 0.4 else 'low')
        direction = 'down' if offset < -0.1 else ('up' if offset > 0.1 else 'neutral')
        
        return {
            'offset': round(offset, 2),
            'direction': direction,
            'strength': strength,
            'hist_samples': int(weighted_samples / total_w) if total_w > 0 else 0
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
        honesty = cls.get_ou_honesty(match.ou_line, match=match)
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
