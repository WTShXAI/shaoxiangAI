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
