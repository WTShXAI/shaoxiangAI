"""
G4 · 真 bet-split 源接入 (替代 rlm_proxy 代理)
=============================================
抽象 BetSplitSource + The Odds API 适配器.
缺失 key / 调用失败 → 返回 None (上层 fallback rlm_proxy).

The Odds API 提供投注占比: GET /v4/sports/{sport}/odds?apiKey&odds=bets&eventIds={id}
返回 bookmakers[].bets[].betPercentage (各庄投注分布), 我们做市场加权均值.
契约 (BetSplit): home_pct / draw_pct / away_pct 为 [0,1] 小数.
"""
import os
import json
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional


@dataclass
class BetSplit:
    home_pct: float
    draw_pct: float
    away_pct: float

    def sharp_side(self) -> Optional[str]:
        """投注最集中的边 (聪明钱方向). 接近均匀则无明确 sharp 边."""
        mx = max(self.home_pct, self.draw_pct, self.away_pct)
        if mx < 0.34:
            return None
        if mx == self.home_pct:
            return 'H'
        if mx == self.draw_pct:
            return 'D'
        return 'A'


class BetSplitSource:
    """抽象 bet-split 源. 子类实现 fetch."""

    def fetch(self, match_key) -> Optional[BetSplit]:
        raise NotImplementedError


class TheOddsApiBetSplit(BetSplitSource):
    """The Odds API bet-percentage 适配器 (G4 真信号源)."""

    def __init__(self, api_key: Optional[str] = None,
                 sport: str = 'soccer_fifa_world_cup'):
        # 兼容两种拼写：代码原名 THEODDS_API_KEY 与 .env.example 文档名 THE_ODDS_API_KEY
        self.api_key = (api_key or os.environ.get('THEODDS_API_KEY')
                        or os.environ.get('THE_ODDS_API_KEY'))
        self.sport = sport
        self.base = 'https://api.the-odds-api.com/v4'

    def fetch(self, match_key) -> Optional[BetSplit]:
        if not self.api_key:
            return None  # 无 key → 上层 fallback rlm_proxy
        try:
            url = (f"{self.base}/sports/{self.sport}/odds"
                   f"?apiKey={self.api_key}&odds=bets&eventIds={match_key}")
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            if not data:
                return None
            agg = {'H': [], 'D': [], 'A': []}
            for ev in data:
                ht = ev.get('home_team')
                at = ev.get('away_team')
                for bm in ev.get('bookmakers', []):
                    for b in bm.get('bets', []):
                        if b.get('key') != 'h2h':
                            continue
                        for o in b.get('outcomes', []):
                            name = o.get('name')
                            pct = o.get('betPercentage') or o.get('moneyPercentage')
                            if pct is None:
                                continue
                            pct = float(pct)
                            if name in ('Home', ht):
                                agg['H'].append(pct)
                            elif name in ('Draw', 'X'):
                                agg['D'].append(pct)
                            elif name in ('Away', at):
                                agg['A'].append(pct)
            if not agg['H'] or not agg['D'] or not agg['A']:
                return None
            return BetSplit(
                sum(agg['H']) / len(agg['H']) / 100.0,
                sum(agg['D']) / len(agg['D']) / 100.0,
                sum(agg['A']) / len(agg['A']) / 100.0,
            )
        except Exception:
            return None  # 任何异常 → fallback
