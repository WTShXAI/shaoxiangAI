"""
ShXAI — 历史数据 API 端点
提供 10 赛季历史比赛与积分榜 JSON 数据
"""
import json
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

import os

# 数据根目录 — 通过环境变量或项目根目录定位
_PROJECT_ROOT = os.environ.get('PROJECT_ROOT') or Path(__file__).resolve().parent.parent.parent.parent.parent
DATA_ROOT = Path(_PROJECT_ROOT) / "10赛季历史数据"

LEAGUE_CODES = {
    "PL": "英超",
    "PD": "西甲",
    "SA": "意甲",
    "BL1": "德甲",
    "FL1": "法甲",
    "CL": "欧冠",
    "EC": "欧洲杯",
    "WC": "世界杯",
    "BSA": "巴甲",
    "ELC": "英冠",
    "DED": "荷甲",
    "PPL": "葡超",
}

# 展示排序：杯赛 → 五大联赛 → 其他联赛
LEAGUE_ORDER = [
    "CL",   # 欧冠
    "EC",   # 欧洲杯
    "WC",   # 世界杯
    "PL",   # 英超
    "PD",   # 西甲
    "BL1",  # 德甲
    "SA",   # 意甲
    "FL1",  # 法甲
    "BSA",  # 巴甲
    "ELC",  # 英冠
    "DED",  # 荷甲
    "PPL",  # 葡超
]

SEASONS = [str(y) for y in range(2015, 2026)]  # 2015-2025


@router.get("/leagues")
async def list_leagues():
    """返回所有可用联赛及其数据摘要"""
    result = []
    for code, name_cn in LEAGUE_CODES.items():
        league_dir = DATA_ROOT / code
        available_seasons = []
        if league_dir.exists():
            for season in SEASONS:
                mf = league_dir / f"{code}_{season}_matches.json"
                sf = league_dir / f"{code}_{season}_standings.json"
                if mf.exists():
                    available_seasons.append({
                        "season": season,
                        "has_matches": mf.exists(),
                        "has_standings": sf.exists(),
                        "matches_size_kb": round(mf.stat().st_size / 1024, 1) if mf.exists() else 0,
                    })
        result.append({
            "code": code,
            "name": name_cn,
            "available_seasons": available_seasons,
            "season_count": len(available_seasons),
        })
    # 按指定顺序排序：杯赛 → 五大联赛 → 其他联赛
    result.sort(key=lambda l: LEAGUE_ORDER.index(l["code"]) if l["code"] in LEAGUE_ORDER else 99)
    return {"leagues": result, "total_seasons_available": sum(l["season_count"] for l in result)}


@router.get("/{league_code}/matches")
async def get_matches(
    league_code: str,
    season: Optional[str] = Query(None, description="赛季年份，如 2024"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """
    获取比赛数据
    - league_code: PL/PD/SA/BL1/FL1
    - season: 可选，不传则返回所有赛季
    """
    if league_code not in LEAGUE_CODES:
        raise HTTPException(404, f"未知联赛: {league_code}，可用: {list(LEAGUE_CODES.keys())}")

    league_dir = DATA_ROOT / league_code
    if not league_dir.exists():
        raise HTTPException(404, f"联赛数据目录不存在: {league_code}")

    if season:
        filepath = league_dir / f"{league_code}_{season}_matches.json"
        if not filepath.exists():
            raise HTTPException(404, f"赛季数据不存在: {league_code} {season}")
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 按日期倒序排列（最新在前，最早在最后一页）
        data.sort(key=lambda m: m.get("utc_date", ""), reverse=True)
        return {
            "league_code": league_code,
            "league_name": LEAGUE_CODES[league_code],
            "season": season,
            "total": len(data),
            "matches": data[(page - 1) * page_size: page * page_size],
            "page": page,
            "page_size": page_size,
        }

    # 返回所有赛季聚合
    all_matches = []
    seasons_found = []
    for s in SEASONS:
        fp = league_dir / f"{league_code}_{s}_matches.json"
        if fp.exists():
            with open(fp, "r", encoding="utf-8") as f:
                all_matches.extend(json.load(f))
            seasons_found.append(s)

    # 按日期倒序排列（最新在前，最早在最后一页）
    all_matches.sort(key=lambda m: m.get("utc_date", ""), reverse=True)
    total = len(all_matches)
    return {
        "league_code": league_code,
        "league_name": LEAGUE_CODES[league_code],
        "seasons": seasons_found,
        "total": total,
        "matches": all_matches[(page - 1) * page_size: page * page_size],
        "page": page,
        "page_size": page_size,
    }


@router.get("/{league_code}/standings")
async def get_standings(
    league_code: str,
    season: Optional[str] = Query(None, description="赛季年份"),
):
    """
    获取积分榜数据
    """
    if league_code not in LEAGUE_CODES:
        raise HTTPException(404, f"未知联赛: {league_code}")

    league_dir = DATA_ROOT / league_code
    if not league_dir.exists():
        raise HTTPException(404, f"联赛数据目录不存在: {league_code}")

    if season:
        filepath = league_dir / f"{league_code}_{season}_standings.json"
        if not filepath.exists():
            raise HTTPException(404, f"赛季积分榜不存在: {league_code} {season}")
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "league_code": league_code,
            "league_name": LEAGUE_CODES[league_code],
            "season": season,
            "total": len(data),
            "standings": data,
        }

    # 返回所有赛季
    all_standings = {}
    for s in SEASONS:
        fp = league_dir / f"{league_code}_{s}_standings.json"
        if fp.exists():
            with open(fp, "r", encoding="utf-8") as f:
                all_standings[s] = json.load(fp)

    return {
        "league_code": league_code,
        "league_name": LEAGUE_CODES[league_code],
        "seasons": list(all_standings.keys()),
        "standings": all_standings,
    }


@router.get("/{league_code}/teams")
async def get_teams(league_code: str):
    """获取联赛球队信息"""
    if league_code not in LEAGUE_CODES:
        raise HTTPException(404, f"未知联赛: {league_code}")

    filepath = DATA_ROOT / league_code / f"{league_code}_teams.json"
    if not filepath.exists():
        raise HTTPException(404, f"球队数据不存在: {league_code}")

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    return {
        "league_code": league_code,
        "league_name": LEAGUE_CODES[league_code],
        "total": len(data.get("teams", data) if isinstance(data, dict) else data),
        "teams": data,
    }


@router.get("/summary")
async def get_summary():
    """获取所有数据的统计摘要"""
    summary = {
        "data_root": str(DATA_ROOT),
        "leagues": {},
        "total_matches": 0,
        "total_standings": 0,
    }
    for code, name_cn in LEAGUE_CODES.items():
        league_dir = DATA_ROOT / code
        if league_dir.exists():
            league_info = {"name": name_cn, "seasons": {}}
            for s in SEASONS:
                mf = league_dir / f"{code}_{s}_matches.json"
                sf = league_dir / f"{code}_{s}_standings.json"
                if mf.exists():
                    with open(mf, "r", encoding="utf-8") as f:
                        match_count = len(json.load(f))
                    league_info["seasons"][s] = {
                        "matches": match_count,
                        "has_standings": sf.exists(),
                    }
                    summary["total_matches"] += match_count
                    if sf.exists():
                        summary["total_standings"] += 1
            summary["leagues"][code] = league_info

    return summary
