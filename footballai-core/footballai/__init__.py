"""
FootballAI Core — 赔率逆向工程引擎
=====================================
D-Gate v5.3 庄家意图检测 + Poisson λ + TeamDB 球队数据

用法:
  from footballai import apply_drawgate, imp_from_odds, TeamDB
  db = TeamDB.from_wc2026()
  db.get('阿根廷')  # {'pts': 6, 'tier': 1, ...}
"""

from footballai.rules.drawgate_v53 import apply_drawgate, imp_from_odds, detect_match_type
from footballai.rules.d_gate_utils import ALL_RESULTS, COVER_DB, STAR_PLAYERS
from footballai.rules.d_gate_engine import apply_dgate_v51
from footballai.data.team_db import TeamDB

__version__ = "5.7.0"
__all__ = [
    'apply_drawgate', 'imp_from_odds', 'detect_match_type',
    'ALL_RESULTS', 'COVER_DB', 'STAR_PLAYERS',
    'apply_dgate_v51', 'TeamDB',
]
