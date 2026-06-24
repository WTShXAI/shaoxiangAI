"""
market_goal_predictor.py v2.0 — 让球+大小球→预期进球（进攻系数修正版）
============================================================
基于4场回测修正：英格兰4-2(偏差+3.2)、乌兹别克1-3(偏差+2.0)

v2.0 改进:
  1. over水位非线性敏感度: <1.85→×1.25, <1.90→×1.12, >2.05→×0.85
  2. 球队进攻系数: 从WebSearch缓存自动读取场均进球
  3. 让球水位穿透力: 低水让球=庄家真信穿盘
  4. 同阵型效应: 4-2-3-1 vs 3-4-3 → 历史均球偏高

用法:
  from market_goal_predictor import MarketGoalPredictor
  mgp = MarketGoalPredictor()
  home, away = mgp.predict(handicap=-1.25, ou_line=2.5, over_water=2.05, 
                            away_attack=2.5, home_attack=1.2)
"""

import math, json, os
from pathlib import Path
from typing import Tuple, List, Dict, Optional

CACHE_DIR = Path(__file__).parent / ".temp" / "team_data"


class MarketGoalPredictor:
    """盘口→预期进球 预测器 v2.0"""
    
    def __init__(self):
        self.formation_boost = {
            # (强队阵型, 弱队阵型): 进球增幅
            ('4-2-3-1', '3-4-3'): 0.25,
            ('4-2-3-1', '3-4-2-1'): 0.25,   # 等同于3-4-3
            ('4-3-3', '5-3-2'): 0.10,
            ('4-2-3-1', '5-4-1'): -0.10,
            ('4-3-3', '4-4-2'): 0.05,
            ('4-3-3', '3-4-3'): 0.30,        # 对攻阵→大球
        }
        self.tournament_first_round = 0.15     # 世界杯首轮进球偏多
    
    def get_team_attack(self, team_name: str) -> float:
        """从WebSearch缓存读取球队场均进球"""
        cache_file = CACHE_DIR / f"{team_name}.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding='utf-8'))
            rf = data.get('recent_form', {})
            goals = rf.get('goals_for', 0)
            matches = rf.get('wins', 0) + rf.get('draws', 0) + rf.get('losses', 0)
            if matches > 0:
                return round(goals / matches, 1)
        return 1.5  # 默认值
    
    def predict(self,
                handicap_line: float,
                handicap_water: float = 1.95,
                ou_line: float = 2.5,
                over_water: float = 1.95,
                under_water: float = 2.05,
                away_attack: Optional[float] = None,
                home_attack: Optional[float] = None,
                away_formation: str = '',
                home_formation: str = '',
                ) -> Tuple[float, float, Dict]:
        """
        返回: (主队预期进球, 客队预期进球, 详细分解)
        """
        detail = {}
        
        # ━━ 1. 大小球非线性修正 ━━
        if over_water < 1.80:
            ou_mult = 1.35       # 极强小球信号
        elif over_water < 1.85:
            ou_mult = 1.25       # 强小球信号（英格兰1.84→6球, 加纳1.84→1球）
        elif over_water < 1.90:
            ou_mult = 1.12
        elif over_water < 1.95:
            ou_mult = 1.05
        elif over_water < 2.05:
            ou_mult = 0.95       # 中性偏小
        elif over_water < 2.10:
            ou_mult = 0.85
        else:
            ou_mult = 0.75       # 强烈小球信号
        
        detail['ou_mult'] = ou_mult
        
        # ━━ 2. 球队进攻系数 ━━
        # 默认值：强队场均2.0球，弱队场均1.0球
        if away_attack is None:
            away_attack = 2.0
        if home_attack is None:
            home_attack = 1.0
        
        # 进攻力越强 → 越能推高总球
        # 公式: 用双方进攻力几何平均 vs 基准1.5来调整
        attack_geo = math.sqrt(away_attack * home_attack)
        attack_factor = attack_geo / 1.5  # 基准1.5球/队/场
        attack_factor = max(0.65, min(1.5, attack_factor))  # 限制0.65~1.5
        
        detail['away_attack'] = away_attack
        detail['home_attack'] = home_attack
        detail['attack_factor'] = attack_factor
        
        # ━━ 3. 阵型效应 ━━
        form_key = (away_formation, home_formation) if away_formation and home_formation else None
        formation_boost = self.formation_boost.get(form_key, 0) if form_key else 0
        detail['formation_boost'] = formation_boost
        
        # ━━ 4. 净胜球 ━━
        abs_hc = abs(handicap_line)
        # 让球水位：低水(<1.90) → 庄家信穿盘 → 净胜球+0.25
        hc_adjust = 0.25 if handicap_water < 1.90 else (0 if handicap_water < 2.0 else -0.25)
        expected_diff = max(0, abs_hc + hc_adjust)
        detail['expected_diff'] = expected_diff
        
        # ━━ 5. 总进球 (三层修正) ━━
        # 基础 = 大小球线 × over水位乘数
        base_total = ou_line * ou_mult
        
        # 一层修正: 球队进攻力
        attack_total = base_total * attack_factor
        
        # 二层修正: 阵型效应 + 大赛首轮加成
        final_total = attack_total + formation_boost + self.tournament_first_round
        
        detail['base_total'] = round(base_total, 2)
        detail['attack_total'] = round(attack_total, 2)
        detail['final_total'] = round(final_total, 2)
        
        # ━━ 6. 分解到两队 ━━
        if handicap_line < 0:
            # 主队受让 → 客队强
            home_goals = max(0, (final_total - expected_diff) / 2)
            away_goals = max(0, (final_total + expected_diff) / 2)
        else:
            home_goals = max(0, (final_total + expected_diff) / 2)
            away_goals = max(0, (final_total - expected_diff) / 2)
        
        return round(home_goals, 1), round(away_goals, 1), detail


# ━━━ 顶级便捷函数 ━━━
def predict_goals(handicap_line, handicap_water=1.95,
                  ou_line=2.5, over_water=1.95, under_water=2.05,
                  **kwargs) -> Tuple[float, float]:
    """兼容旧版API"""
    h, a, _ = MarketGoalPredictor().predict(
        handicap_line, handicap_water, ou_line, over_water, under_water, **kwargs
    )
    return h, a


def predict_scores(lam_h, lam_a, rho=0, ou_line=2.5, over_water=1.95, max_goals=7):
    """基于λ+盘口修正，输出Top比分"""
    scores = []
    mgp = MarketGoalPredictor()
    
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            prob_h = (lam_h ** i * math.exp(-lam_h)) / math.factorial(i)
            prob_a = (lam_a ** j * math.exp(-lam_a)) / math.factorial(j)
            prob_raw = prob_h * prob_a
            
            if i == j:
                prob_raw *= math.exp(rho)
            
            total = i + j
            deviation = abs(total - ou_line)
            if deviation > 1.5:
                prob_raw *= (0.85 ** (deviation - 1.5))
            
            if total > ou_line and over_water > 1.95:
                prob_raw *= 0.9
            if total < ou_line and over_water < 1.90:
                prob_raw *= 0.9
            
            scores.append({'score': f'{i}-{j}', 'prob': round(prob_raw, 6), 'total': total})
    
    total_prob = sum(s['prob'] for s in scores)
    for s in scores:
        s['prob'] = round(s['prob'] / total_prob, 4)
    scores.sort(key=lambda x: x['prob'], reverse=True)
    return scores[:10]


# ━━━ 回测验证 ━━━
if __name__ == "__main__":
    print("=" * 60)
    print("  market_goal_predictor v2.0 — 4场回测")
    print("=" * 60)
    
    mgp = MarketGoalPredictor()
    
    tests = [
        # (比赛名, 让球, 让球水, 大小线, over水, under水, 客攻, 主攻, 客阵, 主阵, 实际)
        ("英格兰vs克罗地亚", -0.5, 0.95, 2.25, 1.84, 2.06, 
         2.5, 1.2, '4-2-3-1', '3-4-3', "4-2"),
        ("葡萄牙vs刚果", -1.5, 0.935, 2.75, 1.99, 1.91,
         2.4, 0.7, '4-2-3-1', '5-3-2', "1-1"),
        ("哥伦比亚vs乌兹别克", -1.25, 0.99, 2.5, 2.05, 1.85,
         2.5, 1.2, '4-2-3-1', '3-4-2-1', "1-3"),
        ("加纳vs巴拿马", -0.5, 0.945, 2.0, 1.84, 2.03,
         1.5, 1.0, '', '', "1-0"),
    ]
    
    for name, hc, hw, ou, ow, uw, atk_a, atk_h, af, hf, actual in tests:
        h, a, d = mgp.predict(hc, hw, ou, ow, uw,
                              away_attack=atk_a, home_attack=atk_h,
                              away_formation=af, home_formation=hf)
        pred_total = h + a
        act_parts = actual.split('-')
        act_total = int(act_parts[0]) + int(act_parts[1])
        error = pred_total - act_total
        
        print(f"\n  {name}")
        print(f"    盘口: 让{abs(hc)}@水{hw} 大小{ou} over@{ow}")
        print(f"    进攻: 客{atk_a}/主{atk_h} 阵型: {af} vs {hf}")
        print(f"    ou_mult={d['ou_mult']:.2f} atk_f={d['attack_factor']:.2f} form_boost={d['formation_boost']}")
        print(f"    预期: {h:.1f}-{a:.1f} (总{pred_total:.1f}) vs 实际{actual}({act_total}) 误差{error:+.1f}")
