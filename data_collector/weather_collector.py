"""
哨响AI - 天气数据采集器 (Open-Meteo)
免费 API，无需 Key，无速率限制
基于经纬度查询历史/未来天气，注入特征管道
"""
import requests
import sqlite3
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'data', 'football_data.db')

# 天气代码映射 (WMO 标准)
WEATHER_CODE_MAP = {
    0: '晴天', 1: '大部晴', 2: '局部多云', 3: '多云',
    45: '雾', 48: '冰雾',
    51: '小雨', 53: '中雨', 55: '大雨',
    61: '小雨', 63: '中雨', 65: '大雨',
    71: '小雪', 73: '中雪', 75: '大雪',
    80: '阵雨', 81: '中阵雨', 82: '大阵雨',
    95: '雷暴', 96: '雷暴+冰雹', 99: '强雷暴+冰雹',
}

class WeatherCollector:
    """Open-Meteo 天气数据采集器"""

    # Phase 2A: 统一从 config/api_config.py 读取，支持环境变量覆盖
    try:
        from config.api_config import EXTERNAL_SERVICES
        _om = EXTERNAL_SERVICES.get("open_meteo", {})
        BASE_URL = _om.get("archive_url", "https://archive-api.open-meteo.com/v1/archive")
        FORECAST_URL = _om.get("forecast_url", "https://api.open-meteo.com/v1/forecast")
    except ImportError:
        BASE_URL = os.getenv("OPEN_METEO_ARCHIVE_URL", "https://archive-api.open-meteo.com/v1/archive")
        FORECAST_URL = os.getenv("OPEN_METEO_FORECAST_URL", "https://api.open-meteo.com/v1/forecast")

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'footballAI-weather/1.0 (contact@example.com)'
        })

    # ──────────── 数据获取 ────────────

    def fetch_weather(self, lat: float, lon: float, date: str) -> Optional[Dict]:
        """
        获取指定坐标和日期的天气数据
        返回: {temperature, humidity, precipitation, wind_speed, weather_code, is_rainy, ...}
        """
        target_date = datetime.strptime(date, '%Y-%m-%d')
        end_date = target_date + timedelta(days=1)

        params = {
            'latitude': lat,
            'longitude': lon,
            'start_date': date,
            'end_date': end_date.strftime('%Y-%m-%d'),
            'daily': [
                'temperature_2m_mean', 'temperature_2m_max', 'temperature_2m_min',
                'precipitation_sum', 'rain_sum',
                'wind_speed_10m_max', 'wind_gusts_10m_max',
                'weather_code',
                'relative_humidity_2m_mean',
            ],
            'timezone': 'auto',
        }

        try:
            # 历史数据用 archive API，未来数据用 forecast API
            now = datetime.now(timezone.utc)
            url = self.BASE_URL if target_date < now else self.FORECAST_URL
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            daily = data.get('daily', {})
            if not daily or not daily.get('time'):
                logger.warning(f"Open-Meteo 无数据: lat={lat} lon={lon} date={date}")
                return None

            idx = 0  # 取第一个日期的数据
            weather_code = daily.get('weather_code', [None])[idx] or 0

            return {
                'temperature_mean': daily.get('temperature_2m_mean', [None])[idx],
                'temperature_max': daily.get('temperature_2m_max', [None])[idx],
                'temperature_min': daily.get('temperature_2m_min', [None])[idx],
                'precipitation': daily.get('precipitation_sum', [0])[idx] or 0,
                'rain': daily.get('rain_sum', [0])[idx] or 0,
                'wind_speed_max': daily.get('wind_speed_10m_max', [0])[idx] or 0,
                'wind_gusts_max': daily.get('wind_gusts_10m_max', [0])[idx] or 0,
                'humidity': daily.get('relative_humidity_2m_mean', [None])[idx],
                'weather_code': weather_code,
                'weather_desc': WEATHER_CODE_MAP.get(weather_code, '未知'),
                'is_rainy': weather_code in (51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99),
                'is_stormy': weather_code in (95, 96, 99),
                'is_windy': (daily.get('wind_speed_10m_max', [0])[idx] or 0) > 30,
                'is_cold': (daily.get('temperature_2m_mean', [15])[idx] or 15) < 8,
                'is_hot': (daily.get('temperature_2m_max', [25])[idx] or 25) > 32,
                'source': 'open-meteo',
                'fetched_at': datetime.now(timezone.utc).isoformat(),
            }
        except requests.RequestException as e:
            logger.error(f"Open-Meteo 请求失败 ({lat},{lon},{date}): {e}")
            return None

    def fetch_batch(self, coords_dates: List[Tuple[float, float, str]]) -> List[Dict]:
        """批量获取天气 (顺序请求，避免速率问题)"""
        results: List[Dict] = []
        for lat, lon, date in coords_dates:
            w = self.fetch_weather(lat, lon, date)
            if w:
                w['_lat'] = lat
                w['_lon'] = lon
                w['_date'] = date
                results.append(w)
        return results

    # ──────────── 特征注入 ────────────

    @staticmethod
    def weather_to_features(weather: Optional[Dict]) -> Dict:
        """将天气数据转换为特征输入字典"""
        if not weather:
            return {
                'aerial_advantage_mod': 1.0,    # 防空修正 (下雨降低防空)
                'press_mod': 1.0,                # 逼抢修正
                'fatigue_mod': 1.0,              # 体能修正 (高温/低温加剧消耗)
                'weather_risk': 0.0,             # 天气风险
            }

        mods = {'aerial_advantage_mod': 1.0, 'press_mod': 1.0,
                'fatigue_mod': 1.0, 'weather_risk': 0.0}

        # 雨天: 防空效率降低 (球滑/场地湿)
        if weather.get('is_rainy'):
            precip = weather.get('precipitation', 0)
            mods['aerial_advantage_mod'] = max(0.6, 1.0 - precip * 0.03)
            mods['weather_risk'] += 0.15

        # 大风: 防空+传球受影响
        if weather.get('is_windy'):
            wind = weather.get('wind_speed_max', 30)
            mods['aerial_advantage_mod'] -= (wind - 30) * 0.01
            mods['press_mod'] *= 0.9
            mods['weather_risk'] += 0.1

        # 高温: 体能消耗加快
        if weather.get('is_hot'):
            mods['fatigue_mod'] = 0.85
            mods['weather_risk'] += 0.08

        # 低温: 肌肉僵硬，传球精准度降
        if weather.get('is_cold'):
            mods['fatigue_mod'] = 0.9
            mods['press_mod'] *= 0.85
            mods['weather_risk'] += 0.05

        # 雷暴: 比赛可能中断/推迟
        if weather.get('is_stormy'):
            mods['weather_risk'] += 0.25

        mods['weather_risk'] = min(mods['weather_risk'], 1.0)
        return mods

    # ──────────── 数据库操作 ────────────

    def save_weather(self, match_id: int, weather: Dict) -> bool:
        """保存天气数据到 weather_data 表"""
        if not weather or not match_id:
            return False

        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute('''
                INSERT OR REPLACE INTO weather_data
                (match_id, temperature_mean, temperature_max, temperature_min,
                 precipitation, humidity, wind_speed_max, wind_gusts_max,
                 weather_code, weather_desc, is_rainy, is_stormy, is_windy,
                 is_cold, is_hot, source, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                match_id,
                weather.get('temperature_mean'),
                weather.get('temperature_max'),
                weather.get('temperature_min'),
                weather.get('precipitation'),
                weather.get('humidity'),
                weather.get('wind_speed_max'),
                weather.get('wind_gusts_max'),
                weather.get('weather_code'),
                weather.get('weather_desc', ''),
                int(weather.get('is_rainy', False)),
                int(weather.get('is_stormy', False)),
                int(weather.get('is_windy', False)),
                int(weather.get('is_cold', False)),
                int(weather.get('is_hot', False)),
                weather.get('source', 'open-meteo'),
                weather.get('fetched_at', datetime.now(timezone.utc).isoformat()),
            ))
            conn.commit()
            return True
        except (Exception, ValueError, requests.exceptions.RequestException) as e:
            logger.error(f"保存天气数据失败 match_id={match_id}: {e}")
            return False
        finally:
            conn.close()

# ──────────── 模块级便捷函数 ────────────

def get_weather_for_match(match_id: int, lat: float, lon: float,
                           match_date: str) -> Optional[Dict]:
    """为一场比赛获取天气"""
    collector = WeatherCollector()
    weather = collector.fetch_weather(lat, lon, match_date)
    if weather:
        collector.save_weather(match_id, weather)
    return weather

def get_stadium_coords(db_path: Optional[str] = None) -> Dict[int, Tuple[float, float]]:
    """从数据库读取球队→球场坐标映射"""
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    try:
        cur.execute('SELECT team_id, latitude, longitude FROM stadiums WHERE latitude IS NOT NULL')
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()

if __name__ == '__main__':
    # 测试: 伦敦 (温布利) 2024-05-15
    collector = WeatherCollector()
    test_weather = collector.fetch_weather(51.556, -0.279, '2024-05-15')
    if test_weather:
        print(json.dumps(test_weather, indent=2, ensure_ascii=False))
        print(f"\n→ 特征修正: {WeatherCollector.weather_to_features(test_weather)}")
    else:
        print("天气数据获取失败")
