"""博弈逆向引擎: 赔率解码 + 积分路径 + 赛会赛制 三层融合
============================================================
不在"预测比分",而在"还原动机":
  赔率异常 ∩ 积分动机 ∩ 赛制路径 → "球队在挑谁/在避谁"
============================================================
"""
import json, math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from pathlib import Path

ROOT = Path(__file__).parent.parent

# ═══════════════════════════════════════════════
# 一、赔率解码器 (Vig Strip + FairProb + Anomaly)
# ═══════════════════════════════════════════════

def strip_vig(odds: List[float]) -> List[float]:
    """剥离博彩公司抽水, 得到fair概率
    公式: implied = 1/odds, 归一化 sum(implied) -> fair_prob
    """
    implied = [1.0 / o for o in odds]
    total = sum(implied)
    fair = [p / total for p in implied]
    return [round(f, 4) for f in fair]

def detect_odds_anomaly(fair_probs: List[float], model_probs: List[float], 
                         threshold: float = 0.03) -> Dict:
    """检测赔率异常: 模型概率 vs 市场fair概率
    Returns: {direction: 'overvalued'/'undervalued'/'normal', gap: float, team_index: int}
    """
    gaps = [abs(f - m) for f, m in zip(fair_probs, model_probs)]
    max_gap = max(gaps)
    idx = gaps.index(max_gap)
    
    if max_gap < threshold:
        return {'status': 'normal', 'max_gap': round(max_gap, 4), 'team_index': idx}
    
    # 哪个方向被高估/低估
    if fair_probs[idx] > model_probs[idx]:
        direction = 'overvalued_by_market'
        note = f"市场高估方向{idx}(赔率偏热), 实际概率可能更低"
    else:
        direction = 'undervalued_by_market'
        note = f"市场低估方向{idx}(赔率偏冷), 可能存在价值"
    
    return {
        'status': direction,
        'max_gap': round(max_gap, 4),
        'team_index': idx,
        'fair_vs_model': {i: {'fair': round(f, 4), 'model': round(m, 4), 'gap': round(abs(f-m), 4)} 
                          for i, (f, m) in enumerate(zip(fair_probs, model_probs))},
        'note': note
    }

class OddsDecoder:
    """赔率解码师: vig剥离 + 偏差检测"""
    
    @staticmethod
    def analyze(home: str, away: str, oh: float, od: float, oa: float) -> Dict:
        fair = strip_vig([oh, od, oa])
        ti = 1/oh + 1/od + 1/oa
        raw_imp = [round((1/oh)/ti, 4), round((1/od)/ti, 4), round((1/oa)/ti, 4)]
        
        # 用简单模型概率: 赔率内含的返奖率偏差
        # 校准数据来源: WC2026小组赛70场实际胜率校准, Jun 2026
        # WC2026 70场统计: H=40.0%, D=28.6%, A=31.4%
        # 市场隐含概率平均: H=44.7%, D=27.1%, A=28.2%
        # 校准乘数: H=0.895, D=1.055, A=1.113
        model_est = [
            max(0.20, min(0.75, raw_imp[0] * 0.895)),
            max(0.15, min(0.45, raw_imp[1] * 1.055)),
            max(0.10, min(0.60, raw_imp[2] * 1.113))
        ]
        model_est = [round(p / sum(model_est), 4) for p in model_est]
        
        anomaly = detect_odds_anomaly(fair, model_est)
        
        return {
            'odds_raw': [oh, od, oa],
            'implied': raw_imp,
            'fair_prob': fair,
            'model_est': model_est,
            'anomaly': anomaly,
            'vig': round(sum(1/o for o in [oh, od, oa]) - 1, 4)
        }


# ═══════════════════════════════════════════════
# 二、积分路径逆向师 (Monte Carlo + Path)
# ═══════════════════════════════════════════════

class PointsPathReconstructor:
    """积分路径逆向师: 小组出线蒙特卡洛 + 挑对手推演"""
    
    @staticmethod
    def group_standings_snapshot(group_id: str) -> Dict:
        """获取小组当前积分"""
        p = ROOT / 'data' / 'final_group_standings_v2.json'
        if p.exists():
            data = json.load(open(p, 'r', encoding='utf-8'))
            standings = data.get('standings', {})
            if group_id in standings:
                return {t[0]: {'pts': t[1], 'gd': t[2], 'gf': t[3], 'ga': t[4], 'pos': t[5]} for t in standings[group_id]}
        return {}

    @staticmethod
    def infer_motivation(team: str, group_id: str, position: int) -> Dict:
        """反推球队动机: 冲头名/保第二/苟第三"""
        # 2026淘汰赛对阵树: 
        # C1→下半区(碰F2→I1/J1枝), C2→上半区(碰D1→A1/B1枝)
        # D1→上半区(碰C2→E1/F1枝), D2→下半区
        # ...完整树见赛会赛制师模块
        
        if position == 1:
            return {
                'motivation': 'maintain',
                'target': '头名晋级',
                'risk': '撞下半区强队(I1/J1枝)',
                'note': '有微弱动机控分→掉到第二走软半区'
            }
        elif position == 2:
            return {
                'motivation': 'push',
                'target': '争第二或保最佳第三',
                'risk': '被第三反超',
                'note': '胜=保第二, 平=可能掉第三被挤'
            }
        elif position == 3:
            return {
                'motivation': 'survive',
                'target': '挤进TOP8第三名',
                'risk': '被挤出前8',
                'note': '必须赢+刷净胜球, 同时看其他组脸色'
            }
        return {'motivation': 'eliminated', 'target': '荣誉之战'}


# ═══════════════════════════════════════════════
# 三、赛会赛制师 (2026 美加墨 48→32 对阵树 · 规则锚)
# ═══════════════════════════════════════════════
#
# 底层规则 (FIFA 官方):
#   1. 12组 A-L, 每组前二(24队) + 8最佳第三(跨组PK: 积分→GD→GF→公平竞赛→FIFA排名) → 32强
#   2. R16 不抽签, 对阵树锁死. 12组拆成四个方块: 左上(ACD)、右上(DEF)、左下(GHI)、右下(JKL)
#   3. A/B/D/E/G/I/K/L 组第一 → 打小组第三; C/F/H/J 组第一 → 打别组第二
#   4. 同组半决赛前不碰 (唯一保护)
#   5. 上半区 (Top Half) = A/B/C/D/E/F; 下半区 (Bottom Half) = G/H/I/J/K/L
#
# 关键反直觉点:
#   - C1(巴西)打2D不打第三 → C1枝"硬"
#   - J1(阿根廷)打2K/2L不打第三 → J1枝碰不到欧洲豪门, 相对"软"
#   - E1(德国)第三候选池含C3 → C3(巴西/摩洛哥)末轮掉链子可能撞E1!
#   - I1(法国)第三候选池含C和H → C3巴西或H3西班牙皆可能
# ═══════════════════════════════════════════════

# ── 8个"组第一 vs 第三"坑位 (FIFA 495组合查表键) ──
THIRD_PLACE_SLOTS = {
    'M74': {'winner': '1E', 'team': '德国', 'pool': ('A','B','C','D','F'), 'pool_teams': 'A/B/C/D/F组第三'},
    'M77': {'winner': '1I', 'team': '法国', 'pool': ('C','D','F','G','H'), 'pool_teams': 'C/D/F/G/H组第三'},
    'M79': {'winner': '1A', 'team': '墨西哥', 'pool': ('C','E','F','H','I'), 'pool_teams': 'C/E/F/H/I组第三'},
    'M80': {'winner': '1L', 'team': '英格兰', 'pool': ('E','H','I','J','K'), 'pool_teams': 'E/H/I/J/K组第三'},
    'M81': {'winner': '1D', 'team': '美国', 'pool': ('B','E','F','I','J'), 'pool_teams': 'B/E/F/I/J组第三'},
    'M82': {'winner': '1G', 'team': '比利时', 'pool': ('A','E','H','I','J'), 'pool_teams': 'A/E/H/I/J组第三'},
    'M85': {'winner': '1B', 'team': '加拿大', 'pool': ('E','F','G','I','J'), 'pool_teams': 'E/F/G/I/J组第三'},
    'M87': {'winner': '1K', 'team': '葡萄牙', 'pool': ('D','E','I','J','L'), 'pool_teams': 'D/E/I/J/L组第三'},
}

# ── 4个"组第一 vs 组第二"固定场 (不打第三) ──
WINNER_VS_RUNNERUP = {
    'M75': {'match': '1C vs 2D', 'note': 'C1(巴西)打D组第二 — 枝硬, 巴西无动机轮换'},
    'M78': {'match': '1F vs 2E', 'note': 'F1(荷兰)打E组第二 — E组含德国/科特迪瓦/厄瓜多尔'},
    'M83': {'match': '1H vs 2G', 'note': 'H1(西班牙)打G组第二 — 西班牙枝不软不硬'},
    'M84': {'match': '1J vs 2K(或2L)', 'note': 'J1(阿根廷)打K或L组第二 — 阿根廷枝相对软'},
}

# ── 组第一分流规则 ──
GROUPS_VS_THIRD = {'A','B','D','E','G','I','K','L'}   # 8个组第一打第三
GROUPS_VS_SECOND = {'C','F','H','J'}                     # 4个组第一打别组第二

# ── 半区归属 ──
TOP_HALF_GROUPS = {'A','B','C','D','E','F'}
BOTTOM_HALF_GROUPS = {'G','H','I','J','K','L'}

# ── 上半区树 (A/B/C/D/E/F 六组出线队) ──
UPPER_BRANCH = {
    'M74': {'type': 'R16', 'winner_slot': '1E', 'opponent_slot': '3rd_pool', 'pool_key': 'M74',
            'QF': 'M90', 'QF_opponent': 'M75_winner',
            'SF': 'M98', 'SF_opponent': 'M94_winner'},
    'M75': {'type': 'R16', 'match': '1C vs 2D',
            'QF': 'M90', 'QF_opponent': 'M74_winner',
            'SF': 'M98', 'SF_opponent': 'M94_winner'},
    'M76': {'type': 'R16', 'winner_slot': '1B', 'opponent_slot': '3rd_pool', 'pool_key': 'M85',
            'QF': 'M94', 'QF_opponent': 'M77_winner',
            'SF': 'M98', 'SF_opponent': 'M90_winner'},
    'M77': {'type': 'R16', 'winner_slot': '1D', 'opponent_slot': '3rd_pool', 'pool_key': 'M81',
            'QF': 'M94', 'QF_opponent': 'M76_winner',
            'SF': 'M98', 'SF_opponent': 'M90_winner'},
    'M78': {'type': 'R16', 'match': '1F vs 2E',
            'QF': 'M91', 'QF_opponent': 'M79_winner',
            'SF': 'M95', 'SF_opponent': ''},
    'M79': {'type': 'R16', 'winner_slot': '1A', 'opponent_slot': '3rd_pool', 'pool_key': 'M79',
            'QF': 'M91', 'QF_opponent': 'M78_winner',
            'SF': 'M95', 'SF_opponent': ''},
}

# ── 下半区树 (G/H/I/J/K/L 六组出线队) ──
LOWER_BRANCH = {
    'M82': {'type': 'R16', 'winner_slot': '1I', 'opponent_slot': '3rd_pool', 'pool_key': 'M77',
            'QF': 'M92', 'QF_opponent': 'M83_winner',
            'SF': 'M99', 'SF_opponent': 'M96_winner'},
    'M83': {'type': 'R16', 'match': '1K vs 3rd_pool', 'pool_key': 'M87',
            'QF': 'M92', 'QF_opponent': 'M82_winner',
            'SF': 'M99', 'SF_opponent': 'M96_winner'},
    'M84': {'type': 'R16', 'match': '1J vs 2K/2L',
            'QF': 'M96', 'QF_opponent': 'M85_winner',
            'SF': 'M99', 'SF_opponent': 'M92_winner'},
    'M85': {'type': 'R16', 'winner_slot': '1L', 'opponent_slot': '3rd_pool', 'pool_key': 'M80',
            'QF': 'M96', 'QF_opponent': 'M84_winner',
            'SF': 'M99', 'SF_opponent': 'M92_winner'},
    'M86': {'type': 'R16', 'match': '1H vs 2G',
            'QF': 'M93', 'QF_opponent': 'M87_winner',
            'SF': 'M97', 'SF_opponent': ''},
    'M87': {'type': 'R16', 'winner_slot': '1G', 'opponent_slot': '3rd_pool', 'pool_key': 'M82',
            'QF': 'M93', 'QF_opponent': 'M86_winner',
            'SF': 'M97', 'SF_opponent': ''},
}

BRACKET_2026 = """
               2026 美加墨 R16 对阵树（上半区 / 下半区 · 规则锚）
================================================================================

   【上半区】A/B/C/D/E/F 六组 → 12+4队                            【下半区】G/H/I/J/K/L 六组 → 12+4队

  M74: 1E(德国) vs 3rd(A/B/C/D/F)                                 M82: 1I(法国) vs 3rd(C/D/F/G/H)
  M75: 1C(巴西) vs 2D                   ┐                         M83: 1K(葡萄牙) vs 3rd(D/E/I/J/L)    ┐
  ├─ M90 ──┐                          │                         M84: 1J(阿根廷) vs 2K或2L             │
  M76: 1B(加拿大) vs 3rd(E/F/G/I/J)    │                         M85: 1L(英格兰) vs 3rd(E/H/I/J/K)    ├─
  M77: 1D(美国) vs 3rd(B/E/F/I/J)     ┘                         M86: 1H(西班牙) vs 2G                │
    └── M94 ──┘                                                    M87: 1G(比利时) vs 3rd(A/E/H/I/J)    ┘
         └── M98 (SF1) ──┐                                            └── M99 (SF2) ──┐
  M78: 1F(荷兰) vs 2E      │                                        (上半区决胜)      │
  M79: 1A(墨西哥) vs 3rd(C/E/F/H/I) ┐                                M88: ...         │
    └── M91 ──┘                      │                                  └── M95 ──┘    │
         └── M95 ──┐                 │                                                  │
                    ├── M100 (Final) │                                                  │
                    │                │                                                  │

  ⚠️ 关键反直觉:
  · C1(巴西)打2D→枝硬, 巴西无动机轮换
  · J1(阿根廷)打2K/2L→枝软, 阿根廷锁头名后末轮动机=保二放队友?
  · E1(德国)第三候选池含C3→巴西末轮掉链子可能撞德国!
  · I1(法国)第三候选池含C和H→C3(巴西/摩洛哥)或H3(西班牙/乌拉圭)皆可能
  · 同组半决赛前不碰 (唯一保护) | 一张黄牌可能决定最佳第三PK
================================================================================
"""


class TournamentArchitect:
    """赛会赛制师: 2026 美加墨 48→32 对阵树 · 规则锚
    
    Built-in knowledge:
    - 12组 A-L → 32强完整R16对阵树 (16场)
    - 8个"组第一 vs 第三"坑位 + 候选池 (FIFA 495组合查表键)
    - 4个"组第一 vs 组第二"固定场
    - 上下半区分流 + 同组保护规则
    """
    
    BRACKET = BRACKET_2026
    THIRD_SLOTS = THIRD_PLACE_SLOTS
    WINNER_VS_RUNNER_UP = WINNER_VS_RUNNERUP
    TOP_HALF = TOP_HALF_GROUPS
    BOTTOM_HALF = BOTTOM_HALF_GROUPS
    VS_THIRD = GROUPS_VS_THIRD
    VS_SECOND = GROUPS_VS_SECOND
    
    @classmethod
    def lookup_third_slot(cls, qualified_thirds: List[str]) -> Dict[str, str]:
        """核心: 输入8个晋级第三来自哪8组 → 输出8个坑位各塞谁
        
        Args:
            qualified_thirds: 8个字母, 如 ['A','C','E','F','H','I','J','K']
        
        Returns:
            {坑位: 对手组别} dict, 如 {'M74': 'C3', 'M77': 'F3', ...}
            'unmapped' 键包含无法分配的组合
        
        算法: 先尝试简单贪婪匹配(按字母序依次填坑),
              若失败则回溯搜索所有8!种排列找可行解。
        """
        thirds = sorted(qualified_thirds)
        slot_order = ['M74','M77','M79','M80','M81','M82','M85','M87']
        
        def is_valid(assignment):
            used = set()
            for slot_key, t in assignment.items():
                if t in used:
                    return False
                if t not in cls.THIRD_SLOTS[slot_key]['pool']:
                    return False
                used.add(t)
            return len(used) == 8
        
        # 简单贪婪
        result = {}
        used = set()
        failed = False
        for slot_key in slot_order:
            pool = cls.THIRD_SLOTS[slot_key]['pool']
            assigned = False
            for t in thirds:
                if t in pool and t not in used:
                    result[slot_key] = f"{t}3"
                    used.add(t)
                    assigned = True
                    break
            if not assigned:
                failed = True
        
        if failed:
            # 回溯搜索: 尝试所有8!种排列 (40320种, 便宜)
            import itertools
            for perm in itertools.permutations(thirds):
                assignment = {}
                ok = True
                for i, slot_key in enumerate(slot_order):
                    t = perm[i]
                    if t not in cls.THIRD_SLOTS[slot_key]['pool']:
                        ok = False
                        break
                    assignment[slot_key] = f"{t}3"
                if ok:
                    return assignment
            # 无可行解
            result['unmapped'] = '无法分配: 此8组第三组合在FIFA规则下不可行'
        
        return result
    
    @classmethod
    def get_half(cls, group: str) -> str:
        """返回球队所在半区 (基于组别)"""
        g = group.upper() if len(group) == 1 else cls._group_from_team(group)
        return 'top' if g in cls.TOP_HALF else 'bottom'
    
    @classmethod
    def get_opponent_path(cls, group: str, position: int) -> Dict:
        """返回某组第X名的R16对手路径
        
        Args:
            group: 组别字母 (A-L)
            position: 名次 (1, 2, 3)
        
        Returns:
            dict with 'match', 'opponent_type', 'candidate_pool', 'half', 'hardness'
        """
        g = group.upper()
        half = cls.get_half(g)
        
        if position == 1:
            if g in cls.VS_THIRD:
                # 找对应坑位
                for slot_key, slot in cls.THIRD_SLOTS.items():
                    if slot['winner'] == f'1{g}':
                        return {
                            'match': slot_key,
                            'opponent_type': '3rd_place',
                            'candidate_pool': slot['pool_teams'],
                            'half': half,
                            'hardness': 'soft' if len(slot['pool']) <= 5 else 'medium',
                            'note': f"{slot['team']}({g}1)将打{slot['pool_teams']}"
                        }
            else:
                # VS_SECOND
                for slot_key, info in cls.WINNER_VS_RUNNER_UP.items():
                    if info['match'].startswith(f'1{g}'):
                        return {
                            'match': slot_key,
                            'opponent_type': 'runner_up',
                            'candidate_pool': info['match'],
                            'half': half,
                            'hardness': 'hard' if g in ('C','J') else 'medium',
                            'note': f"固定打{info['match']} — {info['note']}"
                        }
        
        elif position == 2:
            return {
                'match': 'varies',
                'opponent_type': 'group_winner',
                'candidate_pool': '取决于分组归属',
                'half': half,
                'hardness': 'varies',
                'note': f'{g}2将碰某组第一, 具体看分组归属'
            }
        
        elif position == 3:
            # 第三名: 可能被8个坑位之一选中
            possible_slots = []
            for slot_key, slot in cls.THIRD_SLOTS.items():
                if g in slot['pool']:
                    possible_slots.append(f"{slot_key}(vs {slot['team']})")
            return {
                'match': 'not_guaranteed',
                'opponent_type': 'group_winner',
                'candidate_pool': possible_slots,
                'half': half,
                'hardness': 'survival',
                'note': f'{g}3若晋级TOP8第三, 可能碰: {", ".join(possible_slots)}'
            }
        
        return {'match': 'eliminated', 'opponent_type': 'N/A', 'note': '未出线'}
    
    @classmethod
    def check_motivation_conflict(cls, team: str, group: str, current_position: int, 
                                   can_control: bool = True) -> Dict:
        """核心逆向: 球队是否有"挑对手"动机?
        
        场景:
        - 巴西末轮若锁头名 → C1打2D(枝硬) → 动机: 轮换保主力
        - 阿根廷末轮若锁头名 → J1打2K/2L(枝软) → 动机: 冲头名无阻力
        - 德国E1第三池含C3 → 若C3是巴西 → 德国动机: 可能"让"到第二避巴西?
        """
        g = group.upper()
        path = cls.get_opponent_path(g, current_position)
        
        conflict = {
            'team': team,
            'group': g,
            'position': current_position,
            'current_path': path,
            'has_conflict': False,
            'suggested_action': '正常打',
            'reasoning': []
        }
        
        # 反直觉检测1: 锁头名但枝太硬 → 有动机"让"到第二走软枝
        if current_position == 1 and can_control and path['hardness'] == 'hard':
            conflict['has_conflict'] = True
            conflict['suggested_action'] = '⚠️ 可考虑让到第二走软半区'
            conflict['reasoning'].append(f"锁头名但路径硬({path['note']}), 可能有动机控分")
        
        # 反直觉检测2: E1德国情况 — 第三池含C3
        if g == 'E' and current_position == 1:
            conflict['has_conflict'] = True
            conflict['reasoning'].append('E1第三候选池含C3: 若C3是巴西, 德国的"台阶"接不接?')
            conflict['suggested_action'] = '⚠️ 关注C组末轮赛果 → 巴西若掉第三, 德国可能"演"'
        
        # 反直觉检测3: I1法国 — 第三池含C和H
        if g == 'I' and current_position == 1:
            conflict['reasoning'].append('I1第三候选池含C(巴西枝)和H(西班牙枝)——法国枝有暗雷')
        
        # 反直觉检测4: J1阿根廷 — 枝相对软
        if g == 'J' and current_position == 1:
            conflict['reasoning'].append('J1打2K/2L(非欧洲豪门), 枝相对软 → 阿根廷锁头名后无压力')
        
        return conflict
    
    @classmethod
    def _group_from_team(cls, team: str) -> str:
        """从球队名反查组别 (需要积分数据)"""
        p = ROOT / 'data' / 'final_group_standings_v2.json'
        if p.exists():
            data = json.load(open(p, 'r', encoding='utf-8'))
            for g_id, teams in data.get('standings', {}).items():
                for ti in teams:
                    if ti[0] == team:
                        return g_id
        return '?'
    
    @classmethod
    def bracket_ascii(cls) -> str:
        return BRACKET_2026


# ═══════════════════════════════════════════════
# 四、逆向推理中枢 (Fusion)
# ═══════════════════════════════════════════════

@dataclass
class MotivationReport:
    """动机还原报告"""
    match: str
    odds_anomaly: Dict
    points_motivation: Dict
    half_info: str
    conclusion: str = ""
    risk_flags: List[str] = field(default_factory=list)

def reverse_engineer_match(home: str, away: str, oh: float, od: float, oa: float,
                            group_id: str = '') -> MotivationReport:
    """三层融合逆向推理"""
    decoder = OddsDecoder.analyze(home, away, oh, od, oa)
    half = TournamentArchitect.get_half(home)
    
    # 简易动机推断
    if oh < 2.0:
        motivation = PointsPathReconstructor.infer_motivation(home, group_id, 1)
    elif oh < 4.0:
        motivation = PointsPathReconstructor.infer_motivation(home, group_id, 2)
    else:
        motivation = PointsPathReconstructor.infer_motivation(home, group_id, 3)
    
    # 融合结论
    flags = []
    if decoder['anomaly']['status'] != 'normal':
        flags.append(f"⚠️ 赔率异常: {decoder['anomaly']['note']}")
    
    vig = decoder['vig']
    if vig > 0.10:
        flags.append(f"💰 高抽水({vig:.1%}): 庄家对这场极度不确信")
    
    conclusion = f"{home} {'强' if oh < 2.5 else '中' if oh < 6 else '弱'}势 | "
    conclusion += f"半区={half} | 动机={motivation['motivation']}"
    
    return MotivationReport(
        match=f"{home} vs {away}",
        odds_anomaly=decoder,
        points_motivation=motivation,
        half_info=half,
        conclusion=conclusion,
        risk_flags=flags
    )
