"""
CoachDualPathAnalyzer v1.1 — 教练双路径推理层 + 动态生成引擎
=============================================================

站在球队教练的视角, 对每场比赛推演两种走向:
  - Plan A (理想路径): 如果比赛按预期走势发展
  - Plan B (应急路径): 如果预期被打破该怎么办

v1.1: 增加动态生成引擎, 不再依赖预定义数据库, 支持任意对阵分析

学习自微信视频号世界杯前瞻分析视频:
  - https://weixin.qq.com/sph/AhCIyhTOP9 (6/27全6场前瞻数据分析)
  - https://weixin.qq.com/sph/ARvtDqy7fO (西班牙出线路径博弈分析)
  - https://weixin.qq.com/sph/Ayo3dnPJyk (MD3赛事回顾分析)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

class GameFlow(Enum):
    PLAN_A_ACTIVE = "plan_a"
    PLAN_B_TRIGGERED = "plan_b"
    DEADLOCK = "deadlock"
    CHAOS = "chaos"

@dataclass
class TeamCoachPlan:
    team: str
    formation: str
    strategic_objective: str
    key_player: str
    key_matchup: str
    plan_a: str
    plan_a_score: str
    plan_a_trigger: str
    plan_b: str
    plan_b_score: str
    plan_b_trigger: str
    tactical_strength: str
    tactical_weakness: str
    pressure_level: float = 0.5
    strategic_dilemma: Optional[str] = None

@dataclass
class CoachDualPathResult:
    match_name: str
    home_plan: TeamCoachPlan
    away_plan: TeamCoachPlan
    most_likely_path: str = ''
    pivot_moment: str = ''
    tactical_key: str = ''

class CoachDualPathAnalyzer:
    """
    教练双路径推理引擎 v1.1
    
    支持两种模式:
    1. 预定义数据库 (COACH_PLANS — 精调过的对阵)
    2. 动态生成引擎 (generate_plan — 任意对阵, 基于球队分档+战意)
    """
    
    # ═══ 球队分档 = 动态数据库驱动 ═══
    # 移除了硬编码的TIER字典, 所有分档来自DynamicTeamDB
    
    @classmethod
    def get_tier(cls, team: str) -> int:
        """从动态数据库获取球队档位"""
        from data.dynamic_team_db_module import DynamicTeamDB
        return DynamicTeamDB.get_tier(team)
    
    # ═══ 球队历史赛果 = 动态数据库驱动 ═══
    # 移除了硬编码的HISTORICAL_SCOUTING, 所有侦察数据来自DynamicTeamDB
    
    @classmethod
    def get_scout_report(cls, team: str) -> Optional[dict]:
        """从动态数据库获取球队历史赛果侦察报告"""
        from data.dynamic_team_db_module import DynamicTeamDB
        t = DynamicTeamDB.get_team(team)
        if not t or t.get('gp', 0) == 0:
            return None
        results = t.get('results', [])
        return {
            'results': [(r['gf'], r['ga'], r['opp']) for r in results],
            'pattern': t.get('scout_pattern', ''),
            'weakness': t.get('scout_weakness', ''),
        }
    
    @classmethod
    def get_tier_legacy(cls, team: str) -> int:
        """遗留方法 — 已被DynamicTeamDB替代"""
        from data.dynamic_team_db_module import DynamicTeamDB
        return DynamicTeamDB.get_tier(team)
    
    @classmethod
    def generate_plan(cls, team: str, opponent: str, 
                      team_pts: int = 0, games_played: int = 2,
                      is_home: bool = True) -> TeamCoachPlan:
        """
        动态生成球队教练计划 v1.2
        
        基于: 球队分档 + 积分情况 + 对手强弱 + **历史赛果侦察**
        """
        tier = cls.get_tier(team)
        opp_tier = cls.get_tier(opponent)
        strength_gap = opp_tier - tier  # positive = 我方更强 (T1打T4 = 3)
        
        # ── 历史赛果侦察 ──
        scout = cls.get_scout_report(team)
        opp_scout = cls.get_scout_report(opponent)
        hist_note = ''
        if scout:
            total_gf = sum(r[0] for r in scout['results'])
            total_ga = sum(r[1] for r in scout['results'])
            n = len(scout['results'])
            _pat = scout['pattern'][:30]
            hist_note = f' [历史{n}场: {total_gf/n:.1f}球/场, {_pat}]'
        
        # ── 战略目标推断 ──
        # MD1(0pts, 0 games) vs MD2(0-3pts, 1-2 games) vs MD3(0-6pts, 2 games)
        if games_played == 0:
            # MD1: 所有人都0分, 全力争胜
            objective = '首战, 全力争取开门红'
            pressure = 0.6
            rotation = False
        elif team_pts >= 6:
            objective = '已出线, 可能轮换保存主力'
            pressure = 0.1
            rotation = True
        elif team_pts == 4:
            objective = '打平即出线, 策略保守'
            pressure = 0.4
            rotation = False
        elif team_pts == 3:
            objective = '需赢球确保出线, 全力争胜'
            pressure = 0.75
            rotation = False
        elif team_pts >= 1:
            objective = '必须赢球争取出线, 战意极强'
            pressure = 0.85
            rotation = False
        else:
            # 0分但已打2场(MD3) → 基本淘汰
            if games_played >= 2:
                objective = '已淘汰, 为荣誉而战'
                pressure = 0.3
            else:
                objective = '首战失利但仍有出线希望, 全力争胜'
                pressure = 0.7
            rotation = False
        
        # ── 战术选择 (含历史数据修正) ──
        if strength_gap <= -2:
            # 我方弱 vs 对手强 → 死守反击
            formation = '5-4-1 铁桶阵' if not rotation else '4-5-1 轮换阵'
            key_player = '门将 + 防线核心'
            key_matchup = f'{team}防线 vs {opponent}进攻线'
            plan_a = f'全员退守, 压缩空间, 争取0-0到70分钟。利用{opponent}压上的身后空间反击'
            plan_a_score = '0-0 或 0-1'
            plan_b = '如果先丢球 → 不得不压出进攻, 但可能被反击打爆'
            plan_b_score = f'0-2 或 0-3'
            strength = '防守纪律, 定位球偷鸡, 无压力心态'
            weakness = f'技术差距大, 面对{opponent}持续施压容易崩'
        elif strength_gap == -1:
            # 我方稍弱 → 防守反击
            formation = '4-5-1 防守反击'
            key_player = '中场核心 + 快速前锋'
            key_matchup = f'{team}反击速度 vs {opponent}防线身后'
            plan_a = f'稳守反击, 利用{opponent}压上的空档。中场绞杀限制对方组织'
            plan_a_score = '0-0 或 1-0'
            plan_b = '如果先丢球 → 增加进攻投入, 阵型前提'
            plan_b_score = '0-1 或 1-1'
            strength = '反击效率, 战术纪律, 弱队心态(敢拼)'
            weakness = f'控球劣势, {opponent}的个人能力可随时打破平衡'
        elif strength_gap == 0:
            # 实力接近 → 均衡博弈
            formation = '4-4-2 或 4-2-3-1 均衡阵'
            key_player = '中场核心'
            key_matchup = f'{team}中场 vs {opponent}中场'
            plan_a = '中场争夺是关键, 控制节奏, 耐心寻找机会'
            plan_a_score = '1-1 或 1-0'
            plan_b = '如果落后 → 加强进攻, 可能互爆'
            plan_b_score = '1-2 或 2-2'
            strength = '整体均衡, 无明显短板'
            weakness = '缺乏一击致命的能力'
        elif strength_gap == 1:
            # 我方稍强 → 控球进攻
            formation = '4-3-3 攻击阵'
            key_player = '前锋 + 组织核心'
            key_matchup = f'{team}进攻线 vs {opponent}防线'
            plan_a = f'控球主导, 利用技术优势渗透{opponent}防线。边中结合, 定位球是得分手段'
            plan_a_score = '2-0 或 2-1'
            plan_b = f'如果久攻不下 → 增加远射和边路传中, 换上高中锋'
            plan_b_score = '1-0 或 1-1'
            strength = '技术优势, 控球能力, 个人能力碾压'
            weakness = f'可能轻敌, {opponent}的反击是威胁'
        else:
            # 我方碾压 → 围攻屠杀
            formation = '4-3-3 全力攻击阵'
            key_player = '全队进攻球员'
            key_matchup = f'{team}全方位碾压 vs {opponent}全线防守'
            plan_a = f'从第一分钟开始施压, 争取上半场就锁定胜局。{opponent}防线将在持续压力下崩溃'
            plan_a_score = '3-0 或 4-1'
            plan_b = '不存在Plan B, 屠杀会自然发生'
            plan_b_score = '5-0 或 4-0'
            strength = f'实力碾压, 多个进攻点, {opponent}无法招架'
            weakness = '唯一的风险是轻敌或运气不好'
        
        # ── 战意修正 ──
        if rotation:
            formation += ' (轮换)'
            plan_a_score = '0-0 或 1-1'
            plan_b = '如果落后但已锁定出线→接受失利'
            plan_b_score = '0-1 或 0-2'
            strength += ' (但本场轮换)'
            key_player = '轮换球员的发挥'
        
        # ── 战略困境检测 ──
        dilemma = None
        if team_pts == 4 and opp_tier <= 2 and tier <= 2:
                dilemma = f'⚠️ {team}平局即可, 但赢球可能面对更强的淘汰赛对手, 可能无动力求胜'
        
        return TeamCoachPlan(
            team=team,
            formation=formation,
            strategic_objective=objective,
            key_player=key_player,
            key_matchup=key_matchup,
            plan_a=plan_a,
            plan_a_score=plan_a_score,
            plan_a_trigger=f'{team}顺利执行Plan A',
            plan_b=plan_b,
            plan_b_score=plan_b_score,
            plan_b_trigger=f'{team}场面上被动或比分落后',
            tactical_strength=f'{strength}{hist_note}',
            tactical_weakness=weakness,
            pressure_level=pressure,
            strategic_dilemma=dilemma,
        )
    
    @classmethod
    def get_match_plan(cls, home: str, away: str, 
                       home_pts: int = 0, away_pts: int = 0) -> CoachDualPathResult:
        """获取或生成比赛教练计划"""
        hp = cls.generate_plan(home, away, team_pts=home_pts, is_home=True)
        ap = cls.generate_plan(away, home, team_pts=away_pts, is_home=False)
        
        return CoachDualPathResult(
            match_name=f'{home} vs {away}',
            home_plan=hp,
            away_plan=ap,
            tactical_key=f'{hp.key_matchup} | {ap.key_matchup}',
        )
    
    @classmethod
    def analyze(cls, home: str, away: str, home_score: int = 0, away_score: int = 0, 
                minute: int = 0, home_pts: int = 0, away_pts: int = 0,
                home_gp: int = 0, away_gp: int = 0) -> Dict:
        """
        教练视角综合分析
        
        Args:
            home, away: 队名
            home_score, away_score: 当前比分 (赛中可用)
            minute: 当前分钟 (赛中可用)
            home_pts, away_pts: 球队当前积分
            home_gp, away_gp: 已赛场次 (0=MD1, 1=MD2, 2=MD3)
        """
        # Generate plans with full context
        hp = cls.generate_plan(home, away, team_pts=home_pts, games_played=home_gp, is_home=True)
        ap = cls.generate_plan(away, home, team_pts=away_pts, games_played=away_gp, is_home=False)
        
        tactical_key = f'{hp.key_matchup} | {ap.key_matchup}'
        
        # 判断当前比赛所处的战术阶段
        if minute < 15: phase = '开场试探/抢攻期'
        elif minute < 45: phase = '上半场战术执行期'
        elif minute < 60: phase = '下半场调整期'
        elif minute < 75: phase = 'Plan B触发窗口'
        else: phase = '决胜期'
        
        # 判断双方Plan的状态
        if home_score > away_score:
            home_status = 'Plan A 执行中 ✓'
            away_status = 'Plan B 可能已触发 ⚠'
        elif away_score > home_score:
            home_status = 'Plan B 可能已触发 ⚠'
            away_status = 'Plan A 执行中 ✓'
        else:
            home_status = '僵持阶段'
            away_status = '僵持阶段'
        
        coach_verdict_parts = []
        if hp.strategic_dilemma:
            coach_verdict_parts.append(f'🏠 {home}: {hp.strategic_dilemma}')
        if ap.strategic_dilemma:
            coach_verdict_parts.append(f'🚩 {away}: {ap.strategic_dilemma}')
        
        return {
            'phase': phase,
            'home': {
                'team': hp.team,
                'tier': cls.get_tier(home),
                'formation': hp.formation,
                'objective': hp.strategic_objective,
                'plan_a': hp.plan_a,
                'plan_a_score': hp.plan_a_score,
                'plan_b': hp.plan_b,
                'plan_b_score': hp.plan_b_score,
                'key_player': hp.key_player,
                'key_matchup': hp.key_matchup,
                'strength': hp.tactical_strength,
                'weakness': hp.tactical_weakness,
                'pressure': hp.pressure_level,
                'status': home_status,
                'strategic_dilemma': hp.strategic_dilemma,
            },
            'away': {
                'team': ap.team,
                'tier': cls.get_tier(away),
                'formation': ap.formation,
                'objective': ap.strategic_objective,
                'plan_a': ap.plan_a,
                'plan_a_score': ap.plan_a_score,
                'plan_b': ap.plan_b,
                'plan_b_score': ap.plan_b_score,
                'key_player': ap.key_player,
                'key_matchup': ap.key_matchup,
                'strength': ap.tactical_strength,
                'weakness': ap.tactical_weakness,
                'pressure': ap.pressure_level,
                'status': away_status,
                'strategic_dilemma': ap.strategic_dilemma,
            },
            'tactical_key': tactical_key,
            'coach_verdict': ' | '.join(coach_verdict_parts) if coach_verdict_parts else '无战略困境',
        }

# ════════════════════════════════════════════════════
# 回测
# ════════════════════════════════════════════════════

if __name__ == '__main__':
    analyzer = CoachDualPathAnalyzer()
    
    # 回测已知结果的比赛
    BACKTEST = [
        # (home, away, actual_hs, actual_as, home_pts, away_pts, home_gp, away_gp)
        # MD1: 所有人都0分0场
        ('巴西', '摩洛哥', 1, 1, 0, 0, 0, 0),
        ('德国', '库拉索', 7, 1, 0, 0, 0, 0),
        ('英格兰', '克罗地亚', 4, 2, 0, 0, 0, 0),
        ('荷兰', '日本', 2, 2, 0, 0, 0, 0),
        # MD2: 基于首轮结果
        ('阿根廷', '阿尔及利亚', 3, 0, 3, 0, 1, 1),
        ('西班牙', '佛得角', 0, 0, 3, 0, 1, 1),
        ('比利时', '埃及', 1, 1, 0, 3, 1, 1),
        ('法国', '塞内加尔', 3, 1, 3, 0, 1, 1),
        ('葡萄牙', '民主刚果', 1, 1, 3, 0, 1, 1),
        # MD3: 两轮后
        ('Ecuador', 'Germany', 2, 1, 0, 6, 2, 2),
        ('Japan', 'Sweden', 1, 1, 3, 3, 2, 2),
        ('Paraguay', 'Australia', 0, 0, 3, 3, 2, 2),
        ('Turkey', 'USA', 2, 2, 0, 6, 2, 2),
    ]
    
    def parse_scores(score_str):
        scores = []
        for s in score_str.replace(' 或 ', '|').split('|'):
            s = s.strip()
            parts = s.split('-')
            if len(parts) == 2:
                try: scores.append((int(parts[0]), int(parts[1])))
                except: pass
        return scores
    
    print(f'{"="*65}')
    print(f'  教练双路径 v1.1 回测 ({len(BACKTEST)}场)')
    print(f'{"="*65}')
    
    exact, outcome, total_h, total_count = 0, 0, 0, 0
    missed = []
    
    for home, away, ahs, aas, hpts, apts, hgp, agp in BACKTEST:
        total_count += 1
        r = analyzer.analyze(home, away, home_pts=hpts, away_pts=apts, home_gp=hgp, away_gp=agp)
        hp, ap = r['home'], r['away']
        
        home_scores = parse_scores(hp['plan_a_score']) + parse_scores(hp['plan_b_score'])
        away_scores = parse_scores(ap['plan_a_score']) + parse_scores(ap['plan_b_score'])
        
        exact_match = (ahs, aas) in home_scores or (ahs, aas) in away_scores
        total_match = (ahs + aas) in {h+a for h,a in home_scores+away_scores}
        
        if ahs > aas: ao = 'H'
        elif aas > ahs: ao = 'A'
        else: ao = 'D'
        
        ps = home_scores[0] if home_scores else (0,0)
        if ps[0] > ps[1]: po = 'H'
        elif ps[1] > ps[0]: po = 'A'
        else: po = 'D'
        
        oc_match = ao == po
        
        if exact_match: exact += 1; tag = '🎯'
        elif oc_match: outcome += 1; tag = '✅'
        elif total_match: total_h += 1; tag = '➖'
        else: tag = '❌'; missed.append(f'{home}vs{away}')
        
        ps_str = f'{ps[0]}-{ps[1]}' if home_scores else '?'
        _w = '✓' if oc_match else '✗'
        _t = '✓' if total_match else '✗'
        print(f'  {tag} {home:12s} vs {away:12s} T{hpts}/T{apts} | PlanA:{ps_str:5s} | 实际:{ahs}-{aas} | 胜负{_w} 总球{_t}')
    
    print(f'{"="*65}')
    print(f'  精确比分: {exact}/{total_count} ({exact/total_count*100:.0f}%)')
    print(f'  胜负平:   {exact+outcome}/{total_count} ({(exact+outcome)/total_count*100:.0f}%)')
    print(f'  总进球:   {exact+outcome+total_h}/{total_count} ({(exact+outcome+total_h)/total_count*100:.0f}%)')
    if missed:
        _missed_str = ', '.join(missed)
        print(f'\n  未命中: {_missed_str}')
