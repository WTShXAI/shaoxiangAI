"""
哨响AI - 实时赔率获取服务
============================
从 football-data.org API 获取 LIVE 比赛的赔率数据。

API 限制 (免费版): 每分钟最多 10 次调用, 轮询间隔不低于 30 秒。
"""
import httpx
import os
import logging

logger = logging.getLogger(__name__)

FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")


async def fetch_live_odds():
    """获取进行中比赛的实时赔率。

    Returns:
        list[dict]: 每场比赛包含 id, home, away, home_odds, draw_odds, away_odds
    """
    if not FOOTBALL_DATA_API_KEY:
        logger.warning("FOOTBALL_DATA_API_KEY 未配置，跳过实时赔率获取")
        return []

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://api.football-data.org/v4/matches?status=LIVE",
                headers={"X-Auth-Token": FOOTBALL_DATA_API_KEY},
                timeout=10,
            )
            r.raise_for_status()
            matches = r.json().get("matches", [])
            result = []
            for m in matches:
                odds = m.get("odds", {})
                result.append({
                    "id": m["id"],
                    "home": m["homeTeam"]["name"],
                    "away": m["awayTeam"]["name"],
                    "home_odds": odds.get("homeWin"),
                    "draw_odds": odds.get("draw"),
                    "away_odds": odds.get("awayWin"),
                })
            if result:
                logger.debug(f"实时赔率获取: {len(result)} 场 LIVE 比赛")
            return result
    except httpx.HTTPStatusError as e:
        logger.warning(f"实时赔率 API 状态错误: {e.response.status_code}")
        return []
    except httpx.RequestError as e:
        logger.warning(f"实时赔率 API 请求失败: {e}")
        return []
    except (KeyError, TypeError, ValueError) as e:
        logger.error(f"实时赔率数据解析异常: {e}")
        return []
