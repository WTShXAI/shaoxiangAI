"""
哨响AI v4.0 — 核心工具集 (core/base)
=======================================
单人维护版: 高频复用的计算、校验、数据读取，统一沉淀到这里。
所有模块直接 import 即可，不用重复写。

用法:
    from core.base import math_utils, validators, data_utils
    result = math_utils.poisson_prob(lambda_h, lambda_a, max_goals=6)
"""
import logging
import math
import sqlite3
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger('CoreBase')

# ═══════════════════════════════════════════════════════════════
# 1. 数学计算工具
# ═══════════════════════════════════════════════════════════════

class MathUtils:
    """高频计算函数"""

    @staticmethod
    def poisson_pmf(k: int, lam: float) -> float:
        """泊松PMF: P(X=k)"""
        if lam <= 0:
            return 1.0 if k == 0 else 0.0
        return math.exp(-lam) * (lam ** k) / math.factorial(k)

    @staticmethod
    def odds_to_probs(oh: float, od: float, oa: float) -> Tuple[float, float, float]:
        """赔率 → 隐含概率 (比例法)"""
        inv_sum = 1/oh + 1/od + 1/oa
        return (1/oh)/inv_sum, (1/od)/inv_sum, (1/oa)/inv_sum

    @staticmethod
    def probs_to_odds(ph: float, pd: float, pa: float, margin: float = 0.08) -> Tuple[float, float, float]:
        """概率 → 含抽水赔率"""
        total = ph + pd + pa
        ph, pd, pa = ph/total, pd/total, pa/total
        return 1/(ph*(1+margin)), 1/(pd*(1+margin)), 1/(pa*(1+margin))

    @staticmethod
    def overround(oh: float, od: float, oa: float) -> float:
        """抽水率计算"""
        return 1/oh + 1/od + 1/oa - 1

    @staticmethod
    def kelly_index(prob: float, odds: float) -> float:
        """凯利指数"""
        if odds <= 1:
            return 0.0
        return (prob * odds - 1) / (odds - 1)

    @staticmethod
    def normalize(h: float, d: float, a: float) -> Tuple[float, float, float]:
        """三分类概率归一化"""
        total = h + d + a
        if total <= 0:
            return 1/3, 1/3, 1/3
        return h/total, d/total, a/total

    @staticmethod
    def classify_threshold(h: float, d: float, a: float,
                           draw_thresh: float = 0.46, ha_gap: float = 0.0) -> str:
        """阈值分类: H/D/A"""
        if d > draw_thresh:
            return 'D'
        elif h > a + ha_gap:
            return 'H'
        else:
            return 'A'

    @staticmethod
    def d_gate(h: float, d: float, a: float) -> Tuple[str, float]:
        """D-Gate 精度过滤"""
        margin = d - max(h, a)
        if margin < 0.02:
            return "垃圾区", margin
        elif margin < 0.05:
            return "模糊区", margin
        elif margin < 0.08:
            return "可用区", margin
        elif margin < 0.20:
            return "高置信区", margin
        else:
            return "强D信号", margin

# ═══════════════════════════════════════════════════════════════
# 2. 校验工具
# ═══════════════════════════════════════════════════════════════

class Validators:
    """数据校验 — 统一标准，避免各模块各写各的"""

    @staticmethod
    def is_valid_odds(oh: float, od: float, oa: float) -> bool:
        """赔率有效性检查"""
        return all(1.01 < x < 50.0 for x in [oh, od, oa])

    @staticmethod
    def is_valid_probs(h: float, d: float, a: float, tolerance: float = 0.15) -> bool:
        """概率有效性检查 (允许15%归一化偏差)"""
        if not all(isinstance(p, (int, float)) for p in [h, d, a]):
            return False
        if any(p < 0 or p > 1 for p in [h, d, a]):
            return False
        return abs(h + d + a - 1.0) <= tolerance

    @staticmethod
    def is_string_safe(s: str, max_len: int = 200) -> bool:
        """输入字符串安全性检查"""
        if not isinstance(s, str):
            return False
        if len(s) > max_len:
            return False
        # 防注入: 不含危险字符
        dangerous = [';', '--', '/*', '*/', 'DROP', 'DELETE']
        return not any(d in s.upper() for d in dangerous)

    @staticmethod
    def sanitize_team_name(name: str) -> str:
        """清洗队名"""
        import re
        name = name.strip()
        name = re.sub(r'\s+', ' ', name)
        name = re.sub(r'[（(].*?[）)]', '', name)  # 去括号内容
        return name[:30]

# ═══════════════════════════════════════════════════════════════
# 3. 数据读取工具
# ═══════════════════════════════════════════════════════════════

class DataUtils:
    """统一数据读取 — 不用每次都写SQL"""

    @staticmethod
    def get_db_connection(db_path: str = None) -> sqlite3.Connection:
        """获取数据库连接"""
        if db_path is None:
            from config.settings import get_setting
            db_path = get_setting('paths.db_path', 'data/football_data.db')
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def load_odds_for_match(home: str, away: str, db_path: str = None) -> Optional[Dict]:
        """查询单场比赛赔率"""
        conn = DataUtils.get_db_connection(db_path)
        try:
            row = conn.execute(
                '''SELECT m.home_team_name, m.away_team_name, m.league_name,
                          o.home_odds, o.draw_odds, o.away_odds
                   FROM matches m
                   JOIN odds o ON m.match_id = o.match_id
                   WHERE m.home_team_name LIKE ? AND m.away_team_name LIKE ?
                   ORDER BY m.match_date DESC LIMIT 1''',
                [f'%{home}%', f'%{away}%']
            ).fetchone()
            if row:
                return dict(row)
        finally:
            conn.close()
        return None

    @staticmethod
    def load_recent_results(team: str, limit: int = 5, db_path: str = None) -> List[Dict]:
        """查询某队近期赛果"""
        conn = DataUtils.get_db_connection(db_path)
        try:
            rows = conn.execute(
                '''SELECT match_date, home_team_name, away_team_name,
                          home_score, away_score, final_result
                   FROM matches
                   WHERE (home_team_name LIKE ? OR away_team_name LIKE ?)
                     AND home_score IS NOT NULL
                   ORDER BY match_date DESC LIMIT ?''',
                [f'%{team}%', f'%{team}%', limit]
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
        return []
