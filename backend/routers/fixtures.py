"""
赛程查询端点 — 从 backend/main.py 拆分 (2026-06-28)
================================================
原 backend/main.py L1251-1340: upcoming_fixtures GET /api/v1/fixtures/upcoming

拆分记录: 2026-06-28 God File 拆分 第3块
"""
import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["fixtures"])

@router.get("/fixtures/upcoming")
async def upcoming_fixtures():
    """获取今天+明天的世界杯赛程 (前端快速按钮)"""
    try:
        from data_collector.football_data_live import FootballDataLive
        fdl = FootballDataLive()
        fixtures = fdl.get_wc2026_fixtures()
        finished = fdl.get_wc2026_finished()

        finished_scores = {}
        for m in finished:
            mid = m.get('id')
            sc = m.get('score', {})
            ft = sc.get('fullTime', {}) if sc else {}
            finished_scores[mid] = {'home': ft.get('home'), 'away': ft.get('away'), 'status': m.get('status', 'FINISHED')}

        BJT = timezone(timedelta(hours=8))
        now_bjt = datetime.now(timezone.utc).astimezone(BJT)
        today_12_bjt = now_bjt.replace(hour=12, minute=0, second=0, microsecond=0)
        tomorrow_12_bjt = today_12_bjt + timedelta(days=2)
        today_end_utc = (today_12_bjt + timedelta(days=1)).astimezone(timezone.utc)
        tomorrow_end_utc = tomorrow_12_bjt.astimezone(timezone.utc)

        all_matches = {m['id']: m for m in fixtures}
        for m in finished:
            all_matches[m['id']] = m

        result = {"today": [], "tomorrow": [], "upcoming_count": 0}
        for mid, m in all_matches.items():
            utc_str = m.get('utcDate', '')
            try:
                match_time = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
            except (ValueError, TypeError):
                continue
            today_start_utc = today_12_bjt.astimezone(timezone.utc)
            is_finished = mid in finished_scores
            if not is_finished and match_time < today_start_utc:
                continue
            if is_finished and match_time < today_start_utc - timedelta(hours=12):
                continue
            if match_time > tomorrow_end_utc:
                continue
            match_time_bjt = match_time.astimezone(BJT)
            fs = finished_scores.get(mid, {})
            entry = {
                "id": mid, "home": m.get('homeTeam', {}).get('name', '?'),
                "away": m.get('awayTeam', {}).get('name', '?'),
                "time": utc_str, "time_local": match_time_bjt.strftime('%H:%M'),
                "group": m.get('group', '').replace('GROUP_', '') if m.get('group') else '',
                "status": fs.get('status') or m.get('status', ''),
                "score_home": fs.get('home'), "score_away": fs.get('away'),
            }
            if match_time <= today_end_utc:
                result["today"].append(entry)
            elif match_time <= tomorrow_end_utc:
                result["tomorrow"].append(entry)
        result["upcoming_count"] = len(result["today"]) + len(result["tomorrow"])
        return result
    except Exception as e:
        logger.warning(f"[Fixtures] 获取失败: {e}")
        return {"today": [], "tomorrow": [], "upcoming_count": 0, "error": str(e)}
