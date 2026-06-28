"""战术修正模块: 将足球战术分析转化为预测调整因子"""
import json
from pathlib import Path

ROOT = Path(__file__).parent.parent

def load_tactical_data():
    p = ROOT / 'data' / 'deep_tactical_analysis.json'
    if p.exists():
        return json.load(open(p, 'r', encoding='utf-8'))
    return []

# ═══ 战术修正因子 ═══
# 铁桶阵(佛得角/巴拉圭): 总球↓0.5  / 平局概率↑20%
# 高位压迫(日本): 总球↑0.3 / 客队进球概率↑
# 首进淘汰赛(刚果/佛得角): 战意↑ 但经验↓ → 净胜球压缩
# 中场控制战(克罗地亚vs葡萄牙): 慢节奏 → 总球↓

TACTICAL_MODIFIERS = {
    "铁桶阵": {"total_goals": -0.5, "draw_prob": 0.20, "upset_prob": 0.10},
    "反击型": {"total_goals": -0.3, "away_goal_prob": 0.15},
    "高压控球": {"total_goals": 0.5, "favorite_cover_prob": 0.15},
    "首进淘汰赛": {"experience_penalty": -0.3, "motivation_bonus": 0.2},
    "中场控制战": {"total_goals": -0.4, "draw_prob": 0.15},
    "传奇对决": {"extra_time_prob": 0.15, "low_scoring": 0.10},
}

def get_tactical_adjustment(match_key):
    """根据比赛特征返回战术修正"""
    data = load_tactical_data()
    for m in data:
        if m['match'] == match_key:
            adj = {"total_goals": 0, "draw_boost": 0, "upset_boost": 0}
            
            if '佛得角' in match_key:
                adj['total_goals'] -= 0.5
                adj['draw_boost'] += 0.15
            if '巴拉圭' in match_key:
                adj['total_goals'] -= 0.5
                adj['upset_boost'] += 0.10
            if '日本' in match_key and '巴西' in match_key:
                adj['total_goals'] += 0.3
                adj['upset_boost'] += 0.15
            if '民主刚果' in match_key:
                adj['draw_boost'] += 0.05
            if '克罗地亚' in match_key and '葡萄牙' in match_key:
                adj['total_goals'] -= 0.3
                adj['draw_boost'] += 0.10
            
            return adj
    return {"total_goals": 0, "draw_boost": 0, "upset_boost": 0}
