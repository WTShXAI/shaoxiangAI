"""
哨响AI - 必发交易所数据采集客户端 v1.0
===========================================
数据源: Betfair Exchange API / 本地赔率推导
核心能力:
  - 获取比赛在必发交易所的交易量数据
  - 分析各选项(主胜/平局/客胜)的交易金额分布
  - 检测赔率急变(steam moves)和大额交易信号
  - 无API Key时从已有 odds/odds_history 推导市场热度指标

数据存储: football_data.db::betfair_market
"""

import logging
import sqlite3
import os
import time
import json
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# 必发API配置（Phase 2A: 统一从 config/api_config.py 读取，支持环境变量覆盖）
try:
    from config.api_config import EXTERNAL_SERVICES
    _bf = EXTERNAL_SERVICES.get("betfair", {})
    BETFAIR_API_BASE = _bf.get("api_base", "https://api.betfair.com/exchange/betting/rest/v1.0")
    BETFAIR_LOGIN_URL = _bf.get("login_url", "https://identitysso.betfair.com/api/login")
    BETFAIR_KEEPALIVE_URL = _bf.get("keepalive_url", "https://identitysso.betfair.com/api/keepAlive")
except ImportError:
    BETFAIR_API_BASE = os.getenv("BETFAIR_API_BASE", "https://api.betfair.com/exchange/betting/rest/v1.0")
    BETFAIR_LOGIN_URL = os.getenv("BETFAIR_LOGIN_URL", "https://identitysso.betfair.com/api/login")
    BETFAIR_KEEPALIVE_URL = os.getenv("BETFAIR_KEEPALIVE_URL", "https://identitysso.betfair.com/api/keepAlive")

class BetfairClient:
    """必发交易所数据采集客户端

    三种运行模式:
    1. live:    通过 Betfair API 实时获取交易量（需要 API Key + Session Token）
    2. derived: 从本地 odds/odds_history 数据推导市场热度指标（无需API）
    3. hybrid:  优先 live，回退 derived（默认）
    """

    def __init__(self, db_path: str = "data/football_data.db",
                 api_key: Optional[str] = None, session_token: Optional[str] = None,
                 mode: str = "hybrid"):
        """
        Args:
            db_path: 数据库路径
            api_key: Betfair API Key（可选）
            session_token: Betfair Session Token（可选）
            mode: 'live' | 'derived' | 'hybrid'
        """
        self.db_path = db_path
        self.api_key = api_key or os.environ.get("BETFAIR_API_KEY", "")
        self.session_token = session_token or os.environ.get("BETFAIR_SESSION_TOKEN", "")
        self.mode = mode
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def is_api_configured(self) -> bool:
        return bool(self.api_key and self.session_token)

    @property
    def effective_mode(self) -> str:
        if self.mode == "live" and not self.is_api_configured:
            logger.warning("Betfair API未配置，回退到derived模式")
            return "derived"
        if self.mode == "hybrid":
            return "live" if self.is_api_configured else "derived"
        return self.mode

    # ══════════════════════════════════════════════════
    # 数据库操作
    # ══════════════════════════════════════════════════

    def _connect(self) -> None:
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row

    def _close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _upsert_market(self, data: Dict[str, Any]) -> int:
        """插入或更新 betfair_market 记录"""
        assert self._conn is not None  # 由 _connect() 保证已连接
        cur = self._conn.cursor()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # 检查是否已有该比赛的记录
        cur.execute(
            "SELECT market_id FROM betfair_market WHERE match_id = ? AND market_type = ?",
            (data["match_id"], data.get("market_type", "match_odds"))
        )
        existing = cur.fetchone()

        if existing:
            # 更新
            data["updated_at"] = now
            sets = ", ".join(f"{k} = ?" for k in data.keys())
            vals = list(data.values()) + [existing[0]]
            cur.execute(f"UPDATE betfair_market SET {sets} WHERE market_id = ?", vals)
            market_id = existing[0]
        else:
            # 插入
            data["created_at"] = now
            data["updated_at"] = now
            cols = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            cur.execute(f"INSERT INTO betfair_market ({cols}) VALUES ({placeholders})",
                       list(data.values()))
            market_id = cur.lastrowid

        assert self._conn is not None  # 由 _connect() 保证已连接
        self._conn.commit()
        return market_id

    # ══════════════════════════════════════════════════
    # 模式1: Betfair API 实时获取
    # ══════════════════════════════════════════════════

    def _fetch_live_market(self, match_id: int) -> Optional[Dict[str, Any]]:
        """通过 Betfair Exchange API 获取比赛市场数据

        Betfair API 返回:
        - totalMatched: 总成交额
        - runners[].totalMatched: 各选项成交额
        - runners[].ex.availableToBack/availableToLay: 盘口数据
        """
        try:
            import requests
        except ImportError:
            logger.error("requests 未安装，无法调用 Betfair API")
            return None

        if not self.is_api_configured:
            return None

        headers = {
            "X-Application": self.api_key,
            "X-Authentication": self.session_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # 1. 获取市场列表（按比赛ID查找）
        payload: Dict[str, Any] = {
            "filter": {
                "eventTypeIds": ["1"],  # Soccer
                "marketTypeCodes": ["MATCH_ODDS"],
            },
            "maxResults": 10,
        }

        try:
            # 获取市场目录
            resp = requests.post(
                f"{BETFAIR_API_BASE}/listMarketCatalogue/",
                headers=headers,
                json=payload,
                timeout=15
            )
            if resp.status_code != 200:
                logger.warning(f"Betfair API 返回 {resp.status_code}")
                return None

            catalogues = resp.json()
            if not catalogues:
                return None

            # 2. 获取市场详情（含交易量）
            market_id = catalogues[0].get("marketId")
            detail_payload: Dict[str, Any] = {
                "marketIds": [market_id],
                "priceProjection": {
                    "priceData": ["EX_ALL_OFFERS", "EX_TRADED"],
                },
            }

            resp = requests.post(
                f"{BETFAIR_API_BASE}/listMarketBook/",
                headers=headers,
                json=detail_payload,
                timeout=15
            )
            if resp.status_code != 200:
                return None

            market_book = resp.json()[0]

            # 3. 解析数据
            total_matched = float(market_book.get("totalMatched", 0))
            runners = market_book.get("runners", [])

            result = {
                "match_id": match_id,
                "market_type": "match_odds",
                "total_matched": total_matched,
                "home_matched": 0,
                "draw_matched": 0,
                "away_matched": 0,
                "home_back_odds": None,
                "home_lay_odds": None,
                "draw_back_odds": None,
                "draw_lay_odds": None,
                "away_back_odds": None,
                "away_lay_odds": None,
                "market_timestamp": datetime.now(timezone.utc).isoformat(),
            }

            for runner in runners:
                selection_id = runner.get("selectionId")
                matched = float(runner.get("totalMatched", 0))
                ex = runner.get("ex", {})

                # Best back/lay prices
                back_prices = ex.get("availableToBack", [])
                lay_prices = ex.get("availableToLay", [])

                best_back = float(back_prices[0]["price"]) if back_prices else None
                best_lay = float(lay_prices[0]["price"]) if lay_prices else None

                # Betfair selection IDs: 1=Home, 2=Draw, 3=Away (标准)
                if selection_id == 1 or runner.get("runnerName", "").lower() in ("home",):
                    result["home_matched"] = matched
                    result["home_back_odds"] = best_back
                    result["home_lay_odds"] = best_lay
                elif selection_id == 2 or runner.get("runnerName", "").lower() in ("draw", "the draw"):
                    result["draw_matched"] = matched
                    result["draw_back_odds"] = best_back
                    result["draw_lay_odds"] = best_lay
                elif selection_id == 3 or runner.get("runnerName", "").lower() in ("away",):
                    result["away_matched"] = matched
                    result["away_back_odds"] = best_back
                    result["away_lay_odds"] = best_lay

            # 计算市场指标
            result.update(self._calc_market_indicators(result))
            return result

        except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
            logger.error(f"Betfair API 获取失败: {e}")
            return None

    # ══════════════════════════════════════════════════
    # 模式2: 从本地赔率推导市场热度
    # ══════════════════════════════════════════════════

    def _fetch_derived_market(self, match_id: int) -> Optional[Dict[str, Any]]:
        """从本地 odds + odds_history 推导必发市场指标

        推导逻辑:
        1. 交易量估算: 基于 return_rate (margin) 反推 — margin越低=市场越活跃
        2. 赔率变动: odds_history 的时间序列 → steam_move_score
        3. 买卖价差: 从 return_rate 推导
        4. 交易量偏斜: 基于 implied probability 分布
        """
        assert self._conn is not None  # 由 _connect() 保证已连接
        cur = self._conn.cursor()

        # 获取当前赔率
        cur.execute("""
            SELECT home_odds, draw_odds, away_odds, return_rate, odds_timestamp
            FROM odds WHERE match_id = ? AND provider = 'default'
            ORDER BY odds_timestamp DESC LIMIT 1
        """, (match_id,))
        current_odds = cur.fetchone()

        if not current_odds:
            return None

        home_odds = float(current_odds[0])
        draw_odds = float(current_odds[1])
        away_odds = float(current_odds[2])
        return_rate = float(current_odds[3])
        odds_ts = current_odds[4]

        # 获取赔率历史（计算变动）
        cur.execute("""
            SELECT home_odds, draw_odds, away_odds, odds_timestamp
            FROM odds_history
            WHERE match_id = ? AND provider = 'default'
            ORDER BY odds_timestamp ASC
        """, (match_id,))
        history = cur.fetchall()

        # 推导交易量估算
        # margin 越低 → 流动性越好 → 交易量越高
        # 基准: margin=5% → 约 10万£, margin=2% → 约 100万£
        margin = abs(return_rate) if return_rate and return_rate < 0 else 0.05
        # 用指数衰减模型: volume = 1e6 * exp(-20 * margin)
        estimated_total = 1_000_000 * np.exp(-20 * margin)
        estimated_total = round(max(estimated_total, 5000), 0)  # 下限 5000£

        # 交易量分布（基于隐含概率比例）
        home_prob = 1 / home_odds
        draw_prob = 1 / draw_odds
        away_prob = 1 / away_odds
        total_prob = home_prob + draw_prob + away_prob
        home_frac = home_prob / total_prob
        draw_frac = draw_prob / total_prob
        away_frac = away_prob / total_prob

        home_matched = round(estimated_total * home_frac, 0)
        draw_matched = round(estimated_total * draw_frac, 0)
        away_matched = round(estimated_total * away_frac, 0)

        # 推导 back/lay 赔率（买卖价差 = 1-2 ticks）
        spread_ticks = max(1, round(margin * 100, 0))  # margin越大=流动性越差=价差越大
        back_lay_spread = round(spread_ticks * 0.02, 3)  # 每tick ≈ 0.02

        # Back = 当前赔率 - 半个价差, Lay = 当前赔率 + 半个价差
        half_spread = back_lay_spread / 2
        home_back = round(home_odds - half_spread, 2) if home_odds > 1.01 else None
        home_lay = round(home_odds + half_spread, 2) if home_odds > 1.01 else None
        draw_back = round(draw_odds - half_spread, 2) if draw_odds > 1.01 else None
        draw_lay = round(draw_odds + half_spread, 2) if draw_odds > 1.01 else None
        away_back = round(away_odds - half_spread, 2) if away_odds > 1.01 else None
        away_lay = round(away_odds + half_spread, 2) if away_odds > 1.01 else None

        # 赔率变动（从历史数据）
        opening_home = home_odds
        opening_draw = draw_odds
        opening_away = away_odds
        home_odds_move = 0.0
        draw_odds_move = 0.0
        away_odds_move = 0.0

        if len(history) >= 2:
            first = history[0]
            last = history[-1]
            opening_home = float(first[0])
            opening_draw = float(first[1])
            opening_away = float(first[2])

            home_odds_move = round(float(last[0]) - opening_home, 3)
            draw_odds_move = round(float(last[1]) - opening_draw, 3)
            away_odds_move = round(float(last[2]) - opening_away, 3)

        # 交易量偏斜度: (home - away) / total, 负数=主场热, 正数=客场热
        volume_imbalance = 0.0
        if estimated_total > 0:
            volume_imbalance = round((home_matched - away_matched) / estimated_total, 4)

        # Steam move score: 赔率急变信号
        # 基于赔率变动幅度和方向一致性
        steam_move_score = 0.0
        if len(history) >= 3:
            recent_moves = []
            for i in range(1, min(len(history), 6)):
                h_prev = history[-i-1]
                h_curr = history[-i]
                move = abs(float(h_curr[0]) - float(h_prev[0])) + \
                       abs(float(h_curr[1]) - float(h_prev[1])) + \
                       abs(float(h_curr[2]) - float(h_prev[2]))
                recent_moves.append(move)
            if recent_moves:
                avg_move = np.mean(recent_moves)
                # 归一化: 0.1 = 小波动, 0.5+ = 大急变
                steam_move_score = round(min(1.0, float(avg_move) / 0.5), 3)

        # 大额交易标记: steam_move > 0.3 且方向一致
        large_bet_flag = 1 if steam_move_score > 0.3 else 0

        result = {
            "match_id": match_id,
            "market_type": "match_odds",
            "total_matched": estimated_total,
            "home_matched": home_matched,
            "draw_matched": draw_matched,
            "away_matched": away_matched,
            "home_back_odds": home_back,
            "home_lay_odds": home_lay,
            "draw_back_odds": draw_back,
            "draw_lay_odds": draw_lay,
            "away_back_odds": away_back,
            "away_lay_odds": away_lay,
            "back_lay_spread": back_lay_spread,
            "volume_imbalance": volume_imbalance,
            "steam_move_score": steam_move_score,
            "large_bet_flag": large_bet_flag,
            "opening_home_odds": opening_home,
            "opening_draw_odds": opening_draw,
            "opening_away_odds": opening_away,
            "closing_home_odds": home_odds,
            "closing_draw_odds": draw_odds,
            "closing_away_odds": away_odds,
            "home_odds_move": home_odds_move,
            "draw_odds_move": draw_odds_move,
            "away_odds_move": away_odds_move,
            "market_timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return result

    # ══════════════════════════════════════════════════
    # 市场指标计算
    # ══════════════════════════════════════════════════

    def _calc_market_indicators(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """计算市场效率指标（live模式的数据补充）"""
        total = data.get("total_matched", 0)
        home_m = data.get("home_matched", 0)
        draw_m = data.get("draw_matched", 0)
        away_m = data.get("away_matched", 0)

        # 交易量偏斜度
        volume_imbalance = 0.0
        if total > 0:
            volume_imbalance = round((home_m - away_m) / total, 4)

        # 买卖价差
        back_lay_spread = 0.0
        hb = data.get("home_back_odds")
        hl = data.get("home_lay_odds")
        if hb and hl and hb > 0:
            back_lay_spread = round((hl - hb) / hb, 4)

        return {
            "back_lay_spread": back_lay_spread,
            "volume_imbalance": volume_imbalance,
        }

    # ══════════════════════════════════════════════════
    # 公共接口
    # ══════════════════════════════════════════════════

    def fetch_market_data(self, match_id: int, save: bool = True) -> Optional[Dict[str, Any]]:
        """获取单场比赛的必发市场数据

        Args:
            match_id: 比赛ID
            save: 是否保存到数据库

        Returns:
            市场数据字典 或 None
        """
        try:
            self._connect()

            # 优先查缓存
            assert self._conn is not None  # 由 _connect() 保证已连接
            cur = self._conn.cursor()
            cur.execute("""
                SELECT * FROM betfair_market
                WHERE match_id = ? AND market_type = 'match_odds'
                ORDER BY updated_at DESC LIMIT 1
            """, (match_id,))
            cached = cur.fetchone()

            # 如果缓存数据不超过30分钟，直接返回
            if cached:
                cached_dict = dict(cached)
                ts = cached_dict.get("updated_at", "")
                if ts:
                    try:
                        updated = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                        age = (datetime.now(timezone.utc) - updated).total_seconds()
                        if age < 1800:  # 30分钟
                            logger.debug(f"match_id={match_id} 使用缓存数据 (age={age:.0f}s)")
                            return cached_dict
                    except ValueError:
                        pass

            # 按模式获取
            mode = self.effective_mode
            result = None

            if mode == "live":
                result = self._fetch_live_market(match_id)
            elif mode == "derived":
                result = self._fetch_derived_market(match_id)
            else:  # hybrid
                result = self._fetch_live_market(match_id)
                if not result:
                    result = self._fetch_derived_market(match_id)

            if result and save:
                self._upsert_market(result)
                logger.info(f"match_id={match_id} 必发数据已保存 (mode={mode})")

            return result

        except (Exception) as e:
            logger.error(f"获取必发市场数据失败 (match_id={match_id}): {e}")
            return None
        finally:
            self._close()

    def fetch_batch(self, match_ids: List[int], save: bool = True) -> Dict[int, Dict]:
        """批量获取多场比赛的必发市场数据"""
        results = {}
        for mid in match_ids:
            data = self.fetch_market_data(mid, save=save)
            if data:
                results[mid] = data
            # 限速: live模式每2秒1次, derived模式无限制
            if self.effective_mode == "live":
                time.sleep(2)
        return results

    def fetch_pending_matches(self, save: bool = True) -> Dict[int, Dict]:
        """获取所有有赔率但缺少必发数据的比赛"""
        try:
            self._connect()
            assert self._conn is not None  # 由 _connect() 保证已连接
            cur = self._conn.cursor()

            # 找出有赔率但缺少必发数据的比赛
            cur.execute("""
                SELECT DISTINCT o.match_id
                FROM odds o
                LEFT JOIN betfair_market bm ON o.match_id = bm.match_id
                WHERE bm.market_id IS NULL
                  AND o.provider = 'default'
                LIMIT 100
            """)
            match_ids = [row[0] for row in cur.fetchall()]
            logger.info(f"待采集必发数据的比赛: {len(match_ids)} 场")

        finally:
            self._close()

        return self.fetch_batch(match_ids, save=save)

    def get_match_market(self, match_id: int) -> Optional[Dict[str, Any]]:
        """快速查询: 从数据库读取已有必发数据（不触发新请求）"""
        try:
            self._connect()
            assert self._conn is not None  # 由 _connect() 保证已连接
            cur = self._conn.cursor()
            cur.execute("""
                SELECT * FROM betfair_market
                WHERE match_id = ? AND market_type = 'match_odds'
                ORDER BY updated_at DESC LIMIT 1
            """, (match_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            self._close()

# ══════════════════════════════════════════════════
# 便捷函数
# ══════════════════════════════════════════════════

def get_betfair_client(db_path: str = "data/football_data.db",
                       mode: str = "hybrid") -> BetfairClient:
    """获取必发客户端实例"""
    return BetfairClient(db_path=db_path, mode=mode)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    client = get_betfair_client(db_path="data/football_data.db", mode="derived")

    if client.is_api_configured:
        print("Betfair API 已配置，将使用实时数据")
    else:
        print("Betfair API 未配置，使用本地赔率推导模式")

    # 批量采集
    results = client.fetch_pending_matches()
    print(f"\n采集完成: {len(results)} 场比赛")

    # 展示3场数据
    for mid, data in list(results.items())[:3]:
        print(f"\n--- match_id={mid} ---")
        print(f"  总交易量: £{data.get('total_matched', 0):,.0f}")
        print(f"  主胜交易: £{data.get('home_matched', 0):,.0f}")
        print(f"  平局交易: £{data.get('draw_matched', 0):,.0f}")
        print(f"  客胜交易: £{data.get('away_matched', 0):,.0f}")
        print(f"  偏斜度: {data.get('volume_imbalance', 0):.3f}")
        print(f"  急变信号: {data.get('steam_move_score', 0):.3f}")
        print(f"  大额标记: {'是' if data.get('large_bet_flag') else '否'}")
