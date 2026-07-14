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
    - 外围: MatchInput.hcp (从实时 API/DB 获取)
    - 竞彩: MatchInput.sporttery_hcp (从 sporttery.cn 实时获取)
    
    v6.0 数据清理: SPORTTERY_HCP_627 硬编码竞彩数据已移除
    - 竞彩让球数据现在从 MatchInput.sporttery_hcp 动态获取
    - get_movement_summary_table() 改为接受 matches 参数
    """

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
        # v6.0: 竞彩让球数据从 MatchInput.sporttery_hcp 动态获取
        # 不再使用硬编码 SPORTTERY_HCP_627 字典
        sporttery_hcp = None
        if match.sporttery_hcp is not None and abs(match.sporttery_hcp) > 0.01:
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
    def get_movement_summary_table(cls, matches: List[MatchInput]) -> List[Dict]:
        """生成临场升盘汇总表
        
        Args:
            matches: 比赛列表 (MatchInput), 需包含 sporttery_hcp 字段
            
        v6.0 数据清理: 不再使用硬编码 MATCHES_6_27 / SPORTTERY_HCP_627
        竞彩数据须通过 MatchInput.sporttery_hcp 传入
        """
        rows = []
        for match_data in matches:
            analysis = cls.analyze(match_data)
            rows.append({
                'match': f'{match_data.home} vs {match_data.away}',
                'offshore': f'{analysis.get("offshore_display", "-")}',
                'sporttery': f'{analysis.get("sporttery_display", "-")}',
                'diff': analysis['depth_diff'],
                'grade': analysis['grade'],
                'trap_risk': analysis['trap_risk'],
                'interpretation': analysis['interpretation'][:30] if analysis.get('interpretation') else '',
            })
        return rows

# ════════════════════════════════════════════════════
# Layer 4: TaoGe 策略决策层
# ════════════════════════════════════════════════════
