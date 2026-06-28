"""
哨响AI v4.0 — 世界杯射手榜追踪器 (World Cup Scorer Tracker)
=============================================================
从网络获取2026世界杯射手数据, 用于杯赛校准的射手因子。

数据源: Sporting News Golden Boot tracker
更新时间: 2026-06-16

用法:
    from modules.scorer_tracker import ScorerTracker
    st = ScorerTracker()
    boost = st.get_team_attack_boost('德国')  # → 0.08 (Havertz×2 + Musiala + 多人进球)

作者: Architecture v4.0
日期: 2026-06-19
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 2026世界杯射手数据 (as of June 16, Sporting News)
# 格式: {国家队: [(球员, 进球数, 助攻数), ...]}
# ═══════════════════════════════════════════════════════════════

WC2026_SCORERS: Dict[str, List[tuple]] = {
    "阿根廷": [("Lionel Messi", 3, 0)],
    "美国": [("Folarin Balogun", 2, 0), ("Gio Reyna", 1, 0)],
    "德国": [("Kai Havertz", 2, 1), ("Nico Schlotterbeck", 1, 0),
             ("Jamal Musiala", 1, 0), ("Nathaniel Brown", 1, 0),
             ("Denis Undav", 1, 0), ("Felix Nmecha", 1, 0)],
    "瑞典": [("Yasin Ayari", 2, 0), ("Viktor Gyokeres", 1, 1),
             ("Alexander Isak", 1, 2), ("Mattias Svanberg", 1, 0)],
    "新西兰": [("Elijah Just", 2, 0)],
    "挪威": [("Erling Haaland", 2, 0)],
    "法国": [("Kylian Mbappe", 2, 0), ("Bradley Barcola", 1, 0),
             ("Ibrahim Mbaye", 1, 0)],
    "英格兰": [("Harry Kane", 2, 1), ("Marcus Rashford", 1, 0),
               ("Jude Bellingham", 1, 0)],
    "韩国": [("Hwang In-beom", 1, 1), ("Oh Hyeon-gyu", 1, 0)],
    "塞内加尔": [("Ibrahim Mbaye", 1, 0)],
    "捷克": [("Ladislav Krejci", 1, 0)],
    "墨西哥": [("Julian Quinones", 1, 0), ("Raul Jimenez", 1, 0)],
    "巴拉圭": [("Mauricio", 1, 0)],
    "瑞士": [("Breel Embolo", 1, 1)],
    "卡塔尔": [("Boualem Khoukhi", 1, 0)],
    "摩洛哥": [("Ismael Saibari", 1, 0)],
    "巴西": [("Vinicius Jr.", 1, 0)],
    "苏格兰": [("John McGinn", 1, 0)],
    "澳大利亚": [("Connor Metcalfe", 1, 0), ("Nestory Irankunda", 1, 0)],
    "波黑": [("Jovo Lukic", 1, 0)],
    "加拿大": [("Cyle Larin", 1, 0)],
    "库拉索": [("Livano Comenencia", 1, 0)],
    "荷兰": [("Crysencio Summerville", 1, 0), ("Virgil van Dijk", 1, 0)],
    "日本": [("Keito Nakamura", 1, 0), ("Daichi Kamada", 1, 0)],
    "伊拉克": [("Aymen Hussein", 1, 0)],
    "科特迪瓦": [("Amad Diallo", 1, 0)],
    "突尼斯": [("Omar Rekik", 1, 0)],
    "埃及": [("Emam Ashour", 1, 0)],
    "沙特阿拉伯": [("Abdulelah Alamri", 1, 0)],
    "乌拉圭": [("Maximiliano Araujo", 1, 0)],
    "伊朗": [("Ramin Rezaeian", 1, 1), ("Mohammad Mohebbi", 1, 0)],
    "奥地利": [("Romano Schmid", 1, 0), ("Yazan Al-Arab", 1, 0),
               ("Marko Arnautovic", 1, 1)],
    "约旦": [("Ali Olwan", 1, 0)],
    "葡萄牙": [("Joao Neves", 1, 0)],
    "民主刚果": [("Yoane Wissa", 1, 0)],
    "克罗地亚": [("Petar Musa", 1, 0), ("Martin Baturina", 1, 0)],
    "加纳": [("Caleb Yirenkyi", 1, 0)],
    "乌兹别克斯坦": [("Abbosbek Fayzullaev", 1, 0)],
    "哥伦比亚": [("Luis Diaz", 1, 1), ("Jaminton Campaz", 1, 0),
                 ("Daniel Munoz", 1, 0)],
    "厄瓜多尔": [],
    "海地": [],
    "土耳其": [],
    "佛得角": [],
    "比利时": [],
    "西班牙": [],
    "阿尔及利亚": [],
    "巴拿马": [],
}

# 针对团队名变体做模糊匹配
TEAM_ALIASES = {
    "民主刚果": "民主刚果", "刚果民主共和国": "民主刚果", "刚果(金)": "民主刚果",
    "DR Congo": "民主刚果", "刚果": "民主刚果",
    "库拉索": "库拉索", "库拉索岛": "库拉索", "Curacao": "库拉索",
    "沙特阿拉伯": "沙特阿拉伯", "沙特": "沙特阿拉伯", "Saudi Arabia": "沙特阿拉伯",
}

class ScorerTracker:
    """
    射手榜追踪器

    核心思路:
      如果某队已有球员进多球 → 攻击线状态好 → λ_H/λ_A 上调
      如果某队无人进球 → 攻击乏力 → λ 下调
    """

    def __init__(self):
        self.data = WC2026_SCORERS

    def _resolve_team(self, team: str) -> Optional[str]:
        """模糊匹配队名"""
        if team in self.data:
            return team
        # 别名查找
        if team in TEAM_ALIASES:
            resolved = TEAM_ALIASES[team]
            if resolved in self.data:
                return resolved
        # 部分匹配
        for known in self.data:
            if team in known or known in team:
                return known
        return None

    def get_team_scorers(self, team: str) -> List[tuple]:
        """获取某队射手列表"""
        resolved = self._resolve_team(team)
        return self.data.get(resolved, []) if resolved else []

    def get_team_total_goals(self, team: str) -> int:
        """某队总进球数 (基于射手榜)"""
        scorers = self.get_team_scorers(team)
        return sum(g for _, g, _ in scorers)

    def get_top_scorer_goals(self, team: str) -> int:
        """某队头号射手进球数"""
        scorers = self.get_team_scorers(team)
        return max((g for _, g, _ in scorers), default=0)

    def get_scorer_count(self, team: str) -> int:
        """某队进球人数 (火力分散度)"""
        return len(self.get_team_scorers(team))

    def get_attack_boost(self, team: str) -> float:
        """
        攻击力加成因子

        计算逻辑:
          - 头号射手≥2球 → +0.05
          - 多人进球(≥3人) → +0.03 (火力点分散, 对手难防)
          - 总进球≥5 → +0.05 (攻击线全面开花)
          - 无人进球 → -0.03 (攻击乏力)
          - 上限: ±0.10
        """
        scorers = self.get_team_scorers(team)
        if not scorers:
            # 未查到 = 可能无进球或数据缺失
            return -0.03

        total_goals = sum(g for _, g, _ in scorers)
        top_goals = max(g for _, g, _ in scorers)
        scorer_count = len(scorers)

        boost = 0.0
        if top_goals >= 2:
            boost += 0.05
        if scorer_count >= 3:
            boost += 0.03
        if total_goals >= 5:
            boost += 0.05

        return max(-0.10, min(0.10, boost))

    def get_attack_summary(self, team: str) -> str:
        """生成攻击力摘要"""
        scorers = self.get_team_scorers(team)
        if not scorers:
            return f"{team}: 暂无进球记录"
        total = sum(g for _, g, _ in scorers)
        top = max(g for _, g, _ in scorers)
        names = [f"{n}({g}球)" for n, g, _ in scorers[:3]]
        return f"{team}: 总{total}球 | 头号{top}球 | {', '.join(names)}"

    def compare_attack(self, home: str, away: str) -> Dict:
        """对比两队攻击力"""
        h_goals = self.get_team_total_goals(home)
        a_goals = self.get_team_total_goals(away)
        h_boost = self.get_attack_boost(home)
        a_boost = self.get_attack_boost(away)
        h_scorers = self.get_scorer_count(home)
        a_scorers = self.get_scorer_count(away)

        advantage = ""
        if h_boost > a_boost + 0.05:
            advantage = f"{home}攻击线更热"
        elif a_boost > h_boost + 0.05:
            advantage = f"{away}攻击线更热"
        else:
            advantage = "攻击力相当"

        return {
            "home_total_goals": h_goals,
            "away_total_goals": a_goals,
            "home_boost": h_boost,
            "away_boost": a_boost,
            "home_scorer_count": h_scorers,
            "away_scorer_count": a_scorers,
            "advantage": advantage,
            "home_summary": self.get_attack_summary(home),
            "away_summary": self.get_attack_summary(away),
        }

# 单例
_scorer_tracker: Optional[ScorerTracker] = None

def get_scorer_tracker() -> ScorerTracker:
    global _scorer_tracker
    if _scorer_tracker is None:
        _scorer_tracker = ScorerTracker()
    return _scorer_tracker
