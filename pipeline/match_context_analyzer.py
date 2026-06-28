"""
MatchContextAnalyzer v1.0 — 战意/情境分析层
===========================================

学习自微信视频号世界杯分析视频的分析维度:
  - 生存压力分析 (survival pressure)
  - 轮换预期 (rotation expectation)
  - 互惠博弈 (mutual benefit game theory)
  - 出线路径优化 (qualification path optimization)
  - 核心缺阵影响 (key absence impact)
  - 进攻意愿 (offensive willingness)

在全链路管道中作为 Layer 0, 调整后续各层的置信度和方向。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

class MotivationGrade(Enum):
    """战意等级"""
    SURVIVAL = "survival"         # 生死战, 必须赢
    MUST_NOT_LOSE = "must_not_lose"  # 不能输
    QUALIFIED_FREE = "qualified_free"  # 已出线, 可轮换
    HONOR = "honor"              # 为荣誉而战
    STRATEGIC = "strategic"      # 战略选择(可能故意输)

class RotationRisk(Enum):
    """轮换风险"""
    FULL_STRENGTH = "full"       # 全主力
    LIGHT_ROTATION = "light"     # 轻度轮换
    HEAVY_ROTATION = "heavy"     # 大幅轮换
    UNKNOWN = "unknown"

@dataclass
class TeamContext:
    """球队情境"""
    team: str
    points: int
    games_played: int
    position: int
    group_size: int = 4
    
    # 出线条件
    need_win: bool = False
    need_draw_or_win: bool = False
    already_qualified: bool = False
    already_eliminated: bool = False
    
    # 战意评估
    motivation: MotivationGrade = MotivationGrade.HONOR
    rotation_risk: RotationRisk = RotationRisk.UNKNOWN
    
    # 特殊因素
    key_absence: List[str] = field(default_factory=list)
    offensive_willingness: float = 0.5  # 0=保守 1=狂攻
    
    # 对手强弱 (用于判断屠杀概率)
    opponent_strength: float = 0.5  # 0=弱 1=强

@dataclass
class MatchContext:
    """比赛情境分析结果"""
    home_context: TeamContext
    away_context: TeamContext
    mutual_benefit_draw: bool = False       # 双方平局即满足
    survival_clash: bool = False            # 双方都是生死战
    dead_rubber: bool = False               # 双方都无欲无求
    
    # 调整系数
    motivation_mult: float = 1.0            # 战意倍率 (影响置信度)
    rotation_penalty: float = 0.0           # 轮换惩罚 (削弱强队)
    offensive_bias: float = 0.0             # 进攻倾向修正
    
    # 分析备注
    notes: List[str] = field(default_factory=list)

class MatchContextAnalyzer:
    """
    比赛情境分析引擎
    
    在预测前分析双方战意、出线形势、轮换可能, 
    为全链路管道提供信心调整和方向修正。
    """
    
    # ═══ 战意-置信度映射 ═══
    MOTIVATION_MULTIPLIER = {
        MotivationGrade.SURVIVAL:       1.20,  # 生死战 → 提升预测置信
        MotivationGrade.MUST_NOT_LOSE:  1.10,
        MotivationGrade.QUALIFIED_FREE: 0.75,  # 已出线轮换 → 降低置信
        MotivationGrade.HONOR:          0.90,
        MotivationGrade.STRATEGIC:      0.50,  # 战略放弃 → 极大降低置信
    }
    
    # ═══ 轮换惩罚 ═══
    ROTATION_PENALTY = {
        RotationRisk.FULL_STRENGTH:  0.00,
        RotationRisk.LIGHT_ROTATION: 0.10,  # 轻度轮换 → 实力-10%
        RotationRisk.HEAVY_ROTATION: 0.25,  # 大幅轮换 → 实力-25%
        RotationRisk.UNKNOWN:        0.05,
    }
    
    # ═══ WC2026 小组赛出线规则 ═══
    # 每组4队, 前2名出线
    # R3(第3轮)时: 每队已打2场
    # v1.1: 改为动态数据库, 不再硬编码
    
    # 小组映射 (用于推断R3时的相互依存关系)
    # 动态方案: 可以从API获取, 但小组名称对分析影响不大, 保留静态映射
    GROUP_MAP = {
        '挪威': 'E', '法国': 'E', '塞内加尔': 'E', '伊拉克': 'E',
        '埃及': 'G', '伊朗': 'G', '比利时': 'G', '新西兰': 'G',
        '西班牙': 'H', '乌拉圭': 'H', '佛得角': 'H', '佛得角共和国': 'H',
        '沙特': 'H', '沙特阿拉伯': 'H',
        '克罗地亚': 'F', '加纳': 'F', '巴拿马': 'F', '英格兰': 'F',
        '哥伦比亚': 'C', '葡萄牙': 'C', '民主刚果': 'C', '乌兹别克斯坦': 'C',
    }
    
    @classmethod
    def get_team_pts(cls, team: str) -> int:
        """获取球队当前积分 (来自DynamicTeamDB)"""
        from data.dynamic_team_db_module import DynamicTeamDB
        t = DynamicTeamDB.get_team(team)
        return t.get('pts', 0)
    
    @classmethod
    def get_team_gp(cls, team: str) -> int:
        """获取球队已赛场次"""
        from data.dynamic_team_db_module import DynamicTeamDB
        t = DynamicTeamDB.get_team(team)
        return t.get('gp', 0)
    
    @classmethod
    def get_group(cls, team: str) -> str:
        """获取球队所在小组"""
        return cls.GROUP_MAP.get(team, '?')
    
    @classmethod
    def analyze_motivation(cls, team: str, opponent: str) -> TeamContext:
        """
        分析单队战意
        
        判断逻辑:
          6pts → 已出线, 可能轮换
          4pts → 打平即出线(很大概率), 赢球争第一
          3pts → 生死战(必须赢), 除非净胜球优势大
          1-2pts → 生死战(必须赢), 还需看另一场结果
          0pts → 基本淘汰, 为荣誉而战
        """
        pts = cls.get_team_pts(team)
        grp = cls.get_group(team)
        
        ctx = TeamContext(team=team, points=pts, games_played=2, position=1)
        
        if pts >= 6:
            # 已确定出线
            ctx.already_qualified = True
            ctx.need_win = False
            ctx.motivation = MotivationGrade.QUALIFIED_FREE
            ctx.rotation_risk = RotationRisk.HEAVY_ROTATION
            ctx.offensive_willingness = 0.3  # 保守, 避免受伤
            ctx.notes = [f'{team}已6分确定出线, 大概率大幅轮换']
            
        elif pts == 4:
            # 基本出线但未锁定(大概率)
            ctx.already_qualified = False
            ctx.need_draw_or_win = True  # 打平即可确保出线
            ctx.motivation = MotivationGrade.MUST_NOT_LOSE
            ctx.rotation_risk = RotationRisk.LIGHT_ROTATION
            ctx.offensive_willingness = 0.4  # 偏保守, 先保平再求胜
            ctx.notes = [f'{team}有4分, 打平即可确保出线, 策略保守']
            
        elif pts == 3:
            # 有竞争力但未锁定
            ctx.already_qualified = False
            ctx.need_win = True  # 通常需要赢球
            ctx.motivation = MotivationGrade.SURVIVAL
            ctx.rotation_risk = RotationRisk.FULL_STRENGTH
            ctx.offensive_willingness = 0.7  # 偏进攻
            ctx.notes = [f'{team}有3分, 大概率需赢球确保出线, 战意极强']
            
        elif pts <= 2 and pts > 0:
            # 理论上有机会 — R3小组赛, 2分也可能出线
            ctx.already_qualified = False
            ctx.already_eliminated = False
            ctx.need_win = True
            ctx.motivation = MotivationGrade.SURVIVAL
            ctx.rotation_risk = RotationRisk.FULL_STRENGTH
            ctx.offensive_willingness = 0.85  # 狂攻
            ctx.notes = [f'{team}仅{pts}分, 必须赢球争取出线']
            
        else:  # pts == 0
            # 0分 — 基本淘汰但理论上还可能(需大胜+其他条件)
            ctx.already_eliminated = True  # 大概率淘汰
            ctx.need_win = True
            ctx.motivation = MotivationGrade.SURVIVAL  # 仍会全力争胜
            ctx.rotation_risk = RotationRisk.FULL_STRENGTH
            ctx.offensive_willingness = 0.8  # 无压力, 会放开打
            ctx.notes = [f'{team}0分基本淘汰, 但仍会为荣誉全力争胜']
        
        # 如果对手是强队, 进攻意愿降低(更倾向死守)
        if cls.get_team_pts(opponent) >= 4:
            ctx.offensive_willingness = max(0.2, ctx.offensive_willingness - 0.2)
            ctx.notes.append(f'对手{opponent}实力强, 倾向保守')
        
        return ctx
    
    @classmethod
    def analyze(cls, home: str, away: str, matchday: int = 3) -> MatchContext:
        """
        比赛情境分析主入口
        
        Returns:
            MatchContext with motivation adjustments and analysis notes
        """
        home_ctx = cls.analyze_motivation(home, away)
        away_ctx = cls.analyze_motivation(away, home)
        
        ctx = MatchContext(home_context=home_ctx, away_context=away_ctx)
        
        # ═══ 互惠博弈检测 ═══
        # 双方打平即可满足 → 默契平局概率极高
        both_need_draw = home_ctx.need_draw_or_win and away_ctx.need_draw_or_win
        if both_need_draw:
            ctx.mutual_benefit_draw = True
            ctx.motivation_mult = 0.65  # 降低模型对实力差距的判断
            ctx.notes.append('⚠️ 双方打平即可出线! 默契平局概率极高')
        
        # 双方都是生死战 → 比赛会非常开放
        if home_ctx.motivation == MotivationGrade.SURVIVAL and away_ctx.motivation == MotivationGrade.SURVIVAL:
            ctx.survival_clash = True
            ctx.motivation_mult = 0.85
            ctx.offensive_bias = 0.3  # 双方都必须进攻
            ctx.notes.append('🔥 双方生死战! 比赛将非常开放')
        
        # 双方都无欲无求
        if home_ctx.motivation == MotivationGrade.QUALIFIED_FREE and away_ctx.motivation == MotivationGrade.QUALIFIED_FREE:
            ctx.dead_rubber = True
            ctx.motivation_mult = 0.4
            ctx.notes.append('💤 双方都已出线, 比赛参考价值低')
        
        # 一方生死战 + 一方已出线 → 生死战方有巨大优势
        if home_ctx.motivation == MotivationGrade.SURVIVAL and away_ctx.motivation == MotivationGrade.QUALIFIED_FREE:
            ctx.motivation_mult = 0.70
            ctx.rotation_penalty = cls.ROTATION_PENALTY[away_ctx.rotation_risk]
            ctx.notes.append(f'⚡ {home}生死战 vs {away}已出线(可能轮换) → {home}优势大')
        
        if away_ctx.motivation == MotivationGrade.SURVIVAL and home_ctx.motivation == MotivationGrade.QUALIFIED_FREE:
            ctx.motivation_mult = 0.70
            ctx.rotation_penalty = cls.ROTATION_PENALTY[home_ctx.rotation_risk]
            ctx.notes.append(f'⚡ {away}生死战 vs {home}已出线(可能轮换) → {away}优势大')
        
        # 汇总所有备注
        ctx.notes.extend(home_ctx.notes)
        ctx.notes.extend(away_ctx.notes)
        
        return ctx
    
    @classmethod
    def get_adjustment(cls, home: str, away: str, matchday: int = 3) -> Dict:
        """
        返回可直接用于全链路管道的调整系数
        
        Returns:
            {
                'motivation_mult': float,     # 全局信心倍率
                'rotation_penalty': float,    # 轮换方实力惩罚
                'offensive_bias': float,      # 进攻倾向(+开大球, -小球)
                'mutual_benefit_draw': bool,  # 是否默契平局场景
                'survival_clash': bool,       # 是否双求生战
                'notes': [str],                # 分析备注
            }
        """
        ctx = cls.analyze(home, away, matchday)
        return {
            'motivation_mult': ctx.motivation_mult,
            'rotation_penalty': ctx.rotation_penalty,
            'offensive_bias': ctx.offensive_bias,
            'mutual_benefit_draw': ctx.mutual_benefit_draw,
            'survival_clash': ctx.survival_clash,
            'dead_rubber': ctx.dead_rubber,
            'home_motivation': ctx.home_context.motivation.value,
            'away_motivation': ctx.away_context.motivation.value,
            'home_rotation': ctx.home_context.rotation_risk.value,
            'away_rotation': ctx.away_context.rotation_risk.value,
            'notes': ctx.notes,
        }

# ════════════════════════════════════════════════════
# 快速测试
# ════════════════════════════════════════════════════

if __name__ == '__main__':
    analyzer = MatchContextAnalyzer()
    
    test_matches = [
        ('挪威', '法国'),      # 3pts vs 6pts → 挪威生死战, 法国已出线
        ('塞内加尔', '伊拉克'),  # 0pts vs 0pts → 双双淘汰
        ('佛得角共和国', '沙特阿拉伯'),  # 2pts vs 1pts
        ('乌拉圭', '西班牙'),   # 2pts vs 4pts
        ('埃及', '伊朗'),       # 4pts vs 2pts → 埃及平局即出线
        ('新西兰', '比利时'),   # 1pts vs 1pts
    ]
    
    for home, away in test_matches:
        adj = analyzer.get_adjustment(home, away)
        print(f'\n{"="*50}')
        print(f'  {home} vs {away}')
        print(f'  战意: {adj["home_motivation"]} vs {adj["away_motivation"]}')
        print(f'  轮换: {adj["home_rotation"]} vs {adj["away_rotation"]}')
        print(f'  倍率: {adj["motivation_mult"]}  | 轮换罚: {adj["rotation_penalty"]}  | 进攻偏移: {adj["offensive_bias"]}')
        for n in adj['notes']:
            print(f'  → {n}')
