# -*- coding: utf-8 -*-
"""行情接入层 — 把真实赔率数据源转换成统一 MatchMarket 结构.

两个数据源:
  1. live_odds_raw  — 实时多庄赔率 (The Odds API 采集, 用于「自动扫描在跑比赛」)
  2. odds_features  — 历史双庄可结算 (WH×IW, 用于「历史回放」演示系统价值)

手动单场: parse_single_match() 对应图片场景 — 用户贴入 1X2/波胆/总进球赔率.

绝不重造: 跨庄最优价聚合沿用项目既有逻辑 (各庄去抽水隐含 → 最优价).
"""
from __future__ import annotations
import sqlite3, json, os
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "football_data.db")


# ── 统一数据结构 ──────────────────────────────────────────────

@dataclass
class BookOdds:
    """单庄赔率."""
    source: str               # 庄家名
    h: float                  # 主胜赔率
    d: float                  # 平局赔率
    a: float                  # 客胜赔率

@dataclass
class MatchMarket:
    """一场比赛的完整行情 (统一结构, 供 scanner 消费)."""
    mid: str                  # 比赛唯一键
    home: str
    away: str
    league: str = ""
    match_time: str = ""      # 开赛时间 / 日期
    books: List[BookOdds] = field(default_factory=list)        # 多庄 1X2
    best_h2h: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])  # 跨庄最优 [H,D,A]
    # 单庄全市场 (对应图片场景: 波胆/总进球挂盘)
    score_odds: Optional[Dict[str, float]] = None    # {"1-0": 9.0, "2-1": 8.5, ...}
    total_goals_odds: Optional[Dict[str, float]] = None  # {"0": 11.5, "1": 5.0, ...}
    handicap_odds: Optional[Dict[str, Any]] = None   # {"line": -1, "home": 5.05, "draw": 3.85, "away": 1.49}
    ou_odds: Optional[Dict[str, Any]] = None         # {"line": 2.5, "over": 1.9, "under": 1.9}
    # 赛果 (历史回放/结算用)
    actual_result: Optional[str] = None              # "H"/"D"/"A"
    actual_score: Optional[str] = None               # "2-1"
    is_live: bool = False                            # 是否在跑比赛

    @property
    def book_count(self) -> int:
        return len(self.books)

    @property
    def is_multi_book(self) -> bool:
        """≥2庄才能证伪 → 触发 BET; 单庄只能 EVAL."""
        return self.book_count >= 2


# ── 实时行情: live_odds_raw 表 ──────────────────────────────────

def _parse_best_h2h(raw: Optional[str]) -> List[float]:
    """解析 best_h2h JSON → [H, D, A]."""
    if not raw:
        return [0.0, 0.0, 0.0]
    try:
        d = json.loads(raw)
        return [float(d.get("home", 0)), float(d.get("draw", 0)), float(d.get("away", 0))]
    except Exception:
        return [0.0, 0.0, 0.0]


def _parse_bookmakers_detail(raw: Optional[str]) -> List[BookOdds]:
    """解析 bookmakers_detail JSON → 逐庄 1X2."""
    if not raw:
        return []
    try:
        detail = json.loads(raw)
    except Exception:
        return []
    books = []
    if isinstance(detail, list):
        for bm in detail:
            try:
                h2h = bm.get("h2h") or bm.get("best_h2h") or {}
                h, d, a = h2h.get("home"), h2h.get("draw"), h2h.get("away")
                if h and d and a:
                    books.append(BookOdds(source=bm.get("source", bm.get("key", "?")),
                                          h=float(h), d=float(d), a=float(a)))
            except Exception:
                continue
    return books


def load_live_matches(limit: int = 50) -> List[MatchMarket]:
    """从 live_odds_raw 拉取真实在跑/近期比赛.

    优先返回未结算的在跑比赛; 不够时补充已结算的近期比赛(供回放).
    """
    if not os.path.exists(_DB_PATH):
        return []
    con = sqlite3.connect(_DB_PATH)
    cur = con.cursor()
    matches = []
    try:
        # 先拉未结算的 (在跑)
        cur.execute("""
            SELECT id, home_team, away_team, sport_key, commence_time,
                   best_h2h, bookmakers_detail, bookmakers_count, actual_result, actual_score
            FROM live_odds_raw
            WHERE bookmakers_count >= 2 AND (actual_result IS NULL OR actual_result = '')
            ORDER BY captured_at DESC LIMIT ?""", (limit,))
        for row in cur.fetchall():
            best = _parse_best_h2h(row[5])
            if not all(x > 0 for x in best):
                continue
            matches.append(MatchMarket(
                mid=f"live_{row[0]}", home=row[1], away=row[2], league=row[3] or "",
                match_time=row[4] or "", best_h2h=best,
                books=_parse_bookmakers_detail(row[6]),
                actual_result=row[8], actual_score=row[9], is_live=True,
            ))
        # 不够则补已结算的
        if len(matches) < limit:
            cur.execute("""
                SELECT id, home_team, away_team, sport_key, commence_time,
                       best_h2h, bookmakers_detail, bookmakers_count, actual_result, actual_score
                FROM live_odds_raw
                WHERE actual_result IS NOT NULL AND actual_result != ''
                ORDER BY id DESC LIMIT ?""", (limit - len(matches),))
            for row in cur.fetchall():
                best = _parse_best_h2h(row[5])
                if not all(x > 0 for x in best):
                    continue
                matches.append(MatchMarket(
                    mid=f"live_{row[0]}", home=row[1], away=row[2], league=row[3] or "",
                    match_time=row[4] or "", best_h2h=best,
                    books=_parse_bookmakers_detail(row[6]),
                    actual_result=row[8], actual_score=row[9], is_live=False,
                ))
    finally:
        con.close()
    return matches


# ── 历史回放: odds_features 双庄可结算 (WH × IW) ────────────────

def load_history_matches(limit: int = 200, multi_book_only: bool = True) -> List[MatchMarket]:
    """从 odds_features 拉取历史可结算比赛 (用于资金曲线回放).

    数据: william_hill × interwetten 双庄同场, 每场含 close 赔率 + 真实赛果.
    这是项目验证过 '+83.76% 路径' 的数据源 (live_pilot_guardian 回测同源).
    """
    if not os.path.exists(_DB_PATH):
        return []
    con = sqlite3.connect(_DB_PATH)
    cur = con.cursor()
    matches = []
    try:
        if multi_book_only:
            # 双庄同场: 按 (home,away,date) 聚合, 取有≥2 source 的
            cur.execute("""
                SELECT home_team, away_team, match_date, league,
                       MAX(CASE WHEN source='william_hill' THEN close_h END) wh_h,
                       MAX(CASE WHEN source='william_hill' THEN close_d END) wh_d,
                       MAX(CASE WHEN source='william_hill' THEN close_a END) wh_a,
                       MAX(CASE WHEN source='interwetten' THEN close_h END) iw_h,
                       MAX(CASE WHEN source='interwetten' THEN close_d END) iw_d,
                       MAX(CASE WHEN source='interwetten' THEN close_a END) iw_a,
                       MAX(home_score), MAX(away_score), MAX(outcome)
                FROM odds_features
                WHERE home_score IS NOT NULL AND outcome IS NOT NULL
                  AND source IN ('william_hill','interwetten')
                GROUP BY home_team, away_team, match_date
                HAVING wh_h IS NOT NULL AND iw_h IS NOT NULL
                ORDER BY match_date DESC LIMIT ?""", (limit,))
        else:
            # 单庄也行 (用 william_hill 收盘)
            cur.execute("""
                SELECT home_team, away_team, match_date, league,
                       close_h, close_d, close_a,
                       NULL, NULL, NULL,
                       home_score, away_score, outcome
                FROM odds_features
                WHERE home_score IS NOT NULL AND outcome IS NOT NULL
                  AND source = 'william_hill'
                ORDER BY match_date DESC LIMIT ?""", (limit,))
        for row in cur.fetchall():
            home, away, mdate, league = row[0], row[1], row[2], row[3] or ""
            wh_h, wh_d, wh_a = row[4], row[5], row[6]
            iw_h, iw_d, iw_a = row[7], row[8], row[9]
            hs, a_s, outcome = row[10], row[11], row[12]
            books = [BookOdds("william_hill", float(wh_h), float(wh_d), float(wh_a))]
            if iw_h is not None:
                books.append(BookOdds("interwetten", float(iw_h), float(iw_d), float(iw_a)))
            # 跨庄最优价
            best_h = max(b.h for b in books)
            best_d = max(b.d for b in books)
            best_a = max(b.a for b in books)
            matches.append(MatchMarket(
                mid=f"hist_{home}_{away}_{mdate}",
                home=home, away=away, league=league, match_time=str(mdate),
                books=books, best_h2h=[best_h, best_d, best_a],
                actual_result=outcome, actual_score=f"{hs}-{a_s}", is_live=False,
            ))
    finally:
        con.close()
    return matches


# ── 手动单场解析 (对应图片场景) ─────────────────────────────────

def parse_single_match(
    home: str, away: str,
    h: float, d: float, a: float,
    league: str = "",
    score_odds: Optional[Dict[str, float]] = None,
    total_goals_odds: Optional[Dict[str, float]] = None,
    handicap_odds: Optional[Dict[str, Any]] = None,
    ou_odds: Optional[Dict[str, Any]] = None,
) -> MatchMarket:
    """手动构造单场比赛行情 (用户贴入图片里的赔率板).

    对应法国vs西班牙图片: 标准1X2 + 让球 + 波胆(28选项) + 总进球.
    单庄 → is_multi_book=False → scanner 输出 EVAL 不下 BET 结论 (诚实铁律).
    """
    book = BookOdds(source="manual", h=float(h), d=float(d), a=float(a))
    return MatchMarket(
        mid=f"manual_{home}_{away}",
        home=home, away=away, league=league,
        books=[book], best_h2h=[float(h), float(d), float(a)],
        score_odds=score_odds, total_goals_odds=total_goals_odds,
        handicap_odds=handicap_odds, ou_odds=ou_odds,
        is_live=False,
    )
