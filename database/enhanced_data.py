"""
哨响AI - 增强数据模块
从回收站抢救恢复: D:\.Trash-1000\files\database\enhanced_data.py
包含 160+ 球队评分、14 联赛配置、14 联赛场均进球数据
"""
from datetime import datetime, timezone, timedelta
import random
import math

# ========== 联赛编码 → 哨响AI League ID 映射 ==========
LEAGUE_ID_MAP = {
    "PL": 2021,   # 英超
    "BL1": 2002,  # 德甲
    "SA": 2019,   # 意甲
    "PD": 2014,   # 西甲
    "FL1": 2015,  # 法甲
    "CL": 2001,   # 欧冠
    "WC": 2000,   # 世界杯
    "EC": 2018,   # 欧洲杯
    "BSA": 2013,  # 巴甲
    "DED": 2003,  # 荷甲
    "PPL": 2017,  # 葡超
    "ELC": 2016,  # 英冠
    "MLS": 2006,  # 美职联
    "CSL": 2007,  # 中超
}

# ========== 联赛配置 ==========
LEAGUE_CONFIG = {
    "PL":  {"name": "英超", "name_en": "Premier League", "country": "England"},
    "BL1": {"name": "德甲", "name_en": "Bundesliga", "country": "Germany"},
    "SA":  {"name": "意甲", "name_en": "Serie A", "country": "Italy"},
    "PD":  {"name": "西甲", "name_en": "La Liga", "country": "Spain"},
    "FL1": {"name": "法甲", "name_en": "Ligue 1", "country": "France"},
    "CL":  {"name": "欧冠", "name_en": "Champions League", "country": "Europe"},
    "WC":  {"name": "世界杯", "name_en": "World Cup", "country": "World"},
    "EC":  {"name": "欧洲杯", "name_en": "European Championship", "country": "Europe"},
    "BSA": {"name": "巴甲", "name_en": "Brasileirao", "country": "Brazil"},
    "DED": {"name": "荷甲", "name_en": "Eredivisie", "country": "Netherlands"},
    "PPL": {"name": "葡超", "name_en": "Primeira Liga", "country": "Portugal"},
    "ELC": {"name": "英冠", "name_en": "Championship", "country": "England"},
    "MLS": {"name": "美职联", "name_en": "MLS", "country": "USA"},
    "CSL": {"name": "中超", "name_en": "CSL", "country": "China"},
}

# ========== 球队基础评分（160+ 支球队）==========
TEAM_RATINGS = {
    # === 英超 ===
    "曼城": 90, "阿森纳": 86, "利物浦": 87, "切尔西": 80,
    "曼联": 78, "热刺": 76, "纽卡斯尔": 75, "阿斯顿维拉": 74,
    "布莱顿": 72, "西汉姆": 70, "水晶宫": 68, "富勒姆": 69,
    "埃弗顿": 67, "诺丁汉森林": 70, "伯恩茅斯": 69, "布伦特福德": 68,
    "狼队": 67, "莱斯特城": 66, "伊普斯维奇": 60, "南安普顿": 62,
    # === 德甲 ===
    "拜仁": 91, "勒沃库森": 83, "多特蒙德": 82, "莱比锡": 79,
    "斯图加特": 74, "法兰克福": 73, "弗赖堡": 71, "霍芬海姆": 70,
    "不莱梅": 68, "门兴": 67, "沃尔夫斯堡": 69, "奥格斯堡": 65,
    "美因茨": 66, "柏林联合": 67, "海登海姆": 62, "波鸿": 60,
    "汉堡": 68, "沙尔克04": 67, "柏林赫塔": 64,
    # === 西甲 ===
    "皇马": 92, "巴萨": 89, "马竞": 82, "毕尔巴鄂竞技": 76,
    "皇家社会": 76, "皇家贝蒂斯": 72, "比利亚雷亚尔": 74,
    "塞维利亚": 75, "瓦伦西亚": 70, "塞尔塔": 68,
    "赫塔菲": 66, "奥萨苏纳": 68, "马洛卡": 67, "赫罗纳": 73,
    "巴列卡诺": 67, "阿拉维斯": 64, "拉斯帕尔马斯": 63,
    # === 意甲 ===
    "国际米兰": 85, "AC米兰": 81, "尤文图斯": 82, "那不勒斯": 83,
    "罗马": 77, "拉齐奥": 76, "亚特兰大": 80, "佛罗伦萨": 74,
    "博洛尼亚": 73, "都灵": 70, "乌迪内斯": 68,
    "恩波利": 65, "卡利亚里": 66, "热那亚": 67, "维罗纳": 66,
    "莱切": 64, "蒙扎": 63,
    # === 法甲 ===
    "巴黎圣日耳曼": 88, "马赛": 75, "摩纳哥": 76,
    "里昂": 73, "里尔": 74, "尼斯": 72, "雷恩": 71,
    "朗斯": 73, "斯特拉斯堡": 66, "南特": 67, "蒙彼利埃": 68,
    "布雷斯特": 69, "图卢兹": 67, "兰斯": 68,
    # === 欧冠其他 ===
    "本菲卡": 78, "波尔图": 77, "葡萄牙体育": 76, "阿贾克斯": 74,
    "埃因霍温": 73, "费耶诺德": 72, "凯尔特人": 70, "流浪者": 68,
    "萨尔茨堡": 70, "哥本哈根": 67, "布拉加": 72,
    # === 巴甲 ===
    "弗拉门戈": 80, "帕尔梅拉斯": 79, "圣保罗": 74, "弗鲁米嫩塞": 75,
    "格雷米奥": 73, "科林蒂安": 72, "桑托斯": 71, "克鲁赛罗": 70,
    "米内罗竞技": 72, "国际体育": 69,
    # === 荷甲 ===
    "阿尔克马尔": 68, "特温特": 67,
    # === 英冠 ===
    "利兹联": 69, "桑德兰": 63, "谢菲联": 64, "伯恩利": 65,
    "卢顿": 62, "考文垂": 61, "米德尔斯堡": 63,
    # === 国家队 ===
    "法国": 89, "德国": 84, "西班牙": 88, "意大利": 83,
    "英格兰": 86, "巴西": 90, "阿根廷": 91, "葡萄牙": 85,
    "荷兰": 83, "比利时": 82, "克罗地亚": 80, "摩洛哥": 76,
    "日本": 74, "韩国": 72, "乌拉圭": 79,
    "瑞士": 76, "丹麦": 75, "墨西哥": 73, "美国": 74,
    "奥地利": 72, "土耳其": 71,
    # === 美职联 ===
    "迈阿密国际": 73, "哥伦布机员": 72, "辛辛那提": 71, "洛杉矶FC": 71,
    "西雅图海湾人": 70, "费城联合": 69, "洛杉矶银河": 68,
    "亚特兰大联": 68, "纽约城": 67, "奥兰多城": 66,
    # === 中超 ===
    "上海海港": 66, "上海申花": 65, "成都蓉城": 64, "北京国安": 63,
    "山东泰山": 64, "天津津门虎": 60, "浙江": 62,
    "武汉三镇": 61, "河南": 59, "长春亚泰": 60,
    # === 阿根廷 ===
    "河床": 76, "博卡青年": 75, "竞技俱乐部": 68,
    "拉普拉塔大学生": 71, "圣洛伦索": 67,
    # === 葡超 ===
    "吉马良斯": 69,
}

# ========== 联赛球队列表 ==========
LEAGUE_TEAMS = {
    "PL": ["曼城","阿森纳","利物浦","切尔西","曼联","热刺","纽卡斯尔","阿斯顿维拉","布莱顿","西汉姆","水晶宫","富勒姆","埃弗顿","诺丁汉森林","伯恩茅斯","布伦特福德","狼队","莱斯特城","伊普斯维奇","南安普顿"],
    "BL1": ["拜仁","勒沃库森","多特蒙德","莱比锡","斯图加特","法兰克福","弗赖堡","霍芬海姆","不莱梅","门兴","沃尔夫斯堡","奥格斯堡","美因茨","柏林联合","汉堡","沙尔克04","柏林赫塔","波鸿"],
    "PD": ["皇马","巴萨","马竞","毕尔巴鄂竞技","皇家社会","皇家贝蒂斯","比利亚雷亚尔","塞维利亚","瓦伦西亚","塞尔塔","赫塔菲","奥萨苏纳","马洛卡","赫罗纳","巴列卡诺","阿拉维斯","拉斯帕尔马斯"],
    "SA": ["国际米兰","AC米兰","尤文图斯","那不勒斯","罗马","拉齐奥","亚特兰大","佛罗伦萨","博洛尼亚","都灵","乌迪内斯","恩波利","卡利亚里","热那亚","维罗纳","莱切","蒙扎"],
    "FL1": ["巴黎圣日耳曼","马赛","摩纳哥","里昂","里尔","尼斯","雷恩","朗斯","斯特拉斯堡","南特","蒙彼利埃","布雷斯特","图卢兹","兰斯"],
    "CL": ["曼城","皇马","巴萨","拜仁","国际米兰","巴黎圣日耳曼","阿森纳","多特蒙德","马竞","那不勒斯","利物浦","切尔西","尤文图斯","AC米兰","勒沃库森","本菲卡","波尔图","阿贾克斯"],
    "BSA": ["弗拉门戈","帕尔梅拉斯","圣保罗","弗鲁米嫩塞","格雷米奥","科林蒂安","桑托斯","克鲁赛罗","米内罗竞技","国际体育"],
    "DED": ["阿贾克斯","埃因霍温","费耶诺德","阿尔克马尔","特温特"],
    "PPL": ["本菲卡","波尔图","葡萄牙体育","布拉加","吉马良斯"],
    "ELC": ["利兹联","莱斯特城","伊普斯维奇","南安普顿","桑德兰","谢菲联","伯恩利","卢顿","考文垂","米德尔斯堡"],
    "MLS": ["迈阿密国际","哥伦布机员","辛辛那提","洛杉矶FC","西雅图海湾人","费城联合","洛杉矶银河","亚特兰大联","纽约城","奥兰多城"],
    "CSL": ["上海海港","上海申花","成都蓉城","北京国安","山东泰山","天津津门虎","浙江","武汉三镇","河南","长春亚泰"],
    "WC": ["法国","德国","西班牙","意大利","英格兰","巴西","阿根廷","葡萄牙","荷兰","比利时","克罗地亚","摩洛哥","日本","韩国","乌拉圭","墨西哥"],
    "EC": ["法国","德国","西班牙","意大利","英格兰","葡萄牙","荷兰","比利时","克罗地亚","丹麦","瑞士","奥地利","土耳其"],
}

# ========== 联赛场均进球 ==========
LEAGUE_AVG_GOALS = {
    "PL": {"home": 1.52, "away": 1.18},
    "BL1": {"home": 1.62, "away": 1.24},
    "SA": {"home": 1.48, "away": 1.10},
    "PD": {"home": 1.55, "away": 1.12},
    "FL1": {"home": 1.50, "away": 1.08},
    "CL": {"home": 1.58, "away": 1.10},
    "WC": {"home": 1.42, "away": 1.06},
    "EC": {"home": 1.40, "away": 1.04},
    "BSA": {"home": 1.48, "away": 1.05},
    "DED": {"home": 1.65, "away": 1.20},
    "PPL": {"home": 1.45, "away": 1.00},
    "ELC": {"home": 1.38, "away": 1.02},
    "MLS": {"home": 1.55, "away": 1.10},
    "CSL": {"home": 1.40, "away": 1.02},
}

def get_team_rating(team_name: str, default: int = 70) -> int:
    """获取球队评分"""
    return TEAM_RATINGS.get(team_name, default)

def estimate_odds(home_rating: int, away_rating: int, league_code: str = "PL") -> dict:
    """基于球队实力差估算赔率"""
    rating_diff = home_rating - away_rating
    home_win_prob = max(0.10, min(0.80, 0.40 + rating_diff * 0.005))
    away_win_prob = max(0.10, min(0.80, 0.40 - rating_diff * 0.005))
    draw_prob = max(0.15, min(0.35, 1.0 - home_win_prob - away_win_prob))

    total = home_win_prob + draw_prob + away_win_prob
    home_win_prob /= total
    draw_prob /= total
    away_win_prob /= total

    margin = 1.05
    return {
        "home_odds": max(1.05, round(margin / home_win_prob, 2)),
        "draw_odds": max(1.05, round(margin / draw_prob, 2)),
        "away_odds": max(1.05, round(margin / away_win_prob, 2)),
    }

def get_league_avg_goals(league_code: str) -> dict:
    """获取联赛场均进球数据"""
    return LEAGUE_AVG_GOALS.get(league_code, {"home": 1.40, "away": 1.05})

def get_league_team_list(league_code: str) -> list:
    """获取联赛球队列表"""
    return LEAGUE_TEAMS.get(league_code, [])

def generate_historical_matches(num_matches: int = 30) -> list:
    """生成历史已完成比赛（用于模型训练）"""
    random.seed(123)
    matches = []
    today = datetime.now(timezone.utc).date()
    all_teams = []

    for lg_code in LEAGUE_TEAMS:
        for team in LEAGUE_TEAMS[lg_code]:
            all_teams.append((team, lg_code))

    for i in range(num_matches):
        ht, lg_code = random.choice(all_teams)
        at, _ = random.choice([t for t in all_teams if t[0] != ht])

        h_rating = get_team_rating(ht, 70)
        a_rating = get_team_rating(at, 70)
        league_id = LEAGUE_ID_MAP.get(lg_code, 9999)

        avg = get_league_avg_goals(lg_code)
        home_lambda = avg["home"] * (h_rating / 80) * (1 - a_rating / 200)
        away_lambda = avg["away"] * (a_rating / 80) * (1 - h_rating / 200)

        home_score = max(0, min(6, int(random.gauss(home_lambda, home_lambda * 0.3))))
        away_score = max(0, min(6, int(random.gauss(away_lambda, away_lambda * 0.3))))

        days_ago = random.randint(14, 120)
        match_date = today - timedelta(days=days_ago)

        matches.append({
            "match_date": match_date.isoformat(),
            "league_id": league_id,
            "league_name": LEAGUE_CONFIG.get(lg_code, {}).get("name", ""),
            "home_team_name": ht,
            "home_team_rating": h_rating,
            "away_team_name": at,
            "away_team_rating": a_rating,
            "home_score": home_score,
            "away_score": away_score,
            "status": "finished",
        })

    matches.sort(key=lambda m: m["match_date"])
    return matches
