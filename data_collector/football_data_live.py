"""
football-data.org 实时数据接入层
===============================
API Key: 7天有效期 (到期前需续期或切换数据源)
功能: 实时比分 | 赛程 | 积分榜 | 射手榜 | 历史数据(10赛季)

用法:
    from data_collector.football_data_live import FootballDataLive
    fdl = FootballDataLive()
    matches = fdl.get_live_scores()
    fixtures = fdl.get_wc2026_fixtures()
    standings = fdl.get_wc2026_standings()
"""
import os
import json
import urllib.request
import logging
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

API_KEY = os.environ.get(
    'FOOTBALL_DATA_API_KEY',
    '993ff50d0e2c4b0cb367a7f79eafc6a0'  # 7天有效期
)
BASE_URL = "https://api.football-data.org/v4"
CACHE_DIR = Path(__file__).parent.parent / "data" / "api_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── P3: 球队名英→中翻译表 (football-data.org → 中文) ────────
_TEAM_NAME_ZH: Dict[str, str] = {
    # 2026世界杯参赛队 (基于FIFA排名 + football-data.org实际返回名称)
    "Argentina": "阿根廷", "Brazil": "巴西", "Spain": "西班牙", "France": "法国",
    "England": "英格兰", "Portugal": "葡萄牙", "Morocco": "摩洛哥",
    "Netherlands": "荷兰", "Belgium": "比利时", "Germany": "德国",
    "Croatia": "克罗地亚", "Italy": "意大利", "Colombia": "哥伦比亚",
    "Mexico": "墨西哥", "Senegal": "塞内加尔", "Uruguay": "乌拉圭",
    "United States": "美国", "USA": "美国", "Japan": "日本",
    "Switzerland": "瑞士", "Iran": "伊朗", "Denmark": "丹麦",
    "Turkey": "土耳其", "Ecuador": "厄瓜多尔", "Austria": "奥地利",
    "South Korea": "韩国", "Korea Republic": "韩国", "Nigeria": "尼日利亚",
    "Australia": "澳大利亚", "Algeria": "阿尔及利亚", "Egypt": "埃及",
    "Canada": "加拿大", "Norway": "挪威", "Ukraine": "乌克兰",
    "Ivory Coast": "科特迪瓦", "Côte d'Ivoire": "科特迪瓦",
    "Panama": "巴拿马", "Russia": "俄罗斯", "Poland": "波兰",
    "Wales": "威尔士", "Sweden": "瑞典", "Hungary": "匈牙利",
    "Czech Republic": "捷克", "Czechia": "捷克", "Paraguay": "巴拉圭",
    "Scotland": "苏格兰", "Serbia": "塞尔维亚", "Cameroon": "喀麦隆",
    "Tunisia": "突尼斯", "DR Congo": "民主刚果",
    "Congo DR": "民主刚果", "Slovakia": "斯洛伐克", "Greece": "希腊",
    "Venezuela": "委内瑞拉", "Uzbekistan": "乌兹别克斯坦",
    "Qatar": "卡塔尔", "Iraq": "伊拉克", "South Africa": "南非",
    "Saudi Arabia": "沙特", "Jordan": "约旦",
    "Bosnia and Herzegovina": "波黑", "Bosnia": "波黑", "Bosnia-Herzegovina": "波黑",
    "Cape Verde": "佛得角", "Cape Verde Islands": "佛得角",
    "Ghana": "加纳", "Curaçao": "库拉索", "Haiti": "海地",
    "New Zealand": "新西兰",
    # 常见附加名 (别称/缩写)
    "Costa Rica": "哥斯达黎加", "Chile": "智利", "Peru": "秘鲁",
    "Finland": "芬兰", "Romania": "罗马尼亚", "Ireland": "爱尔兰",
    "Northern Ireland": "北爱尔兰", "Bulgaria": "保加利亚",
    "Iceland": "冰岛", "Slovenia": "斯洛文尼亚",
    "North Macedonia": "北马其顿", "Macedonia": "北马其顿",
    "Albania": "阿尔巴尼亚", "Montenegro": "黑山",
    "Georgia": "格鲁吉亚", "Armenia": "亚美尼亚",
    "Israel": "以色列", "Cyprus": "塞浦路斯", "Luxembourg": "卢森堡",
    "Kosovo": "科索沃", "Malta": "马耳他", "Lithuania": "立陶宛",
    "Latvia": "拉脱维亚", "Estonia": "爱沙尼亚",
    "Belarus": "白俄罗斯", "Moldova": "摩尔多瓦", "Azerbaijan": "阿塞拜疆",
    "Kazakhstan": "哈萨克斯坦", "Faroe Islands": "法罗群岛",
    "Andorra": "安道尔", "San Marino": "圣马力诺",
    "Gibraltar": "直布罗陀", "Liechtenstein": "列支敦士登",
    "Equatorial Guinea": "赤道几内亚", "Gabon": "加蓬",
    "Angola": "安哥拉", "Mali": "马里", "Burkina Faso": "布基纳法索",
    "Guinea": "几内亚", "Benin": "贝宁", "Zambia": "赞比亚",
    "Uganda": "乌干达", "Togo": "多哥", "Libya": "利比亚",
    "Sudan": "苏丹", "Kenya": "肯尼亚", "Tanzania": "坦桑尼亚",
    "Ethiopia": "埃塞俄比亚", "Rwanda": "卢旺达", "Mozambique": "莫桑比克",
    "Zimbabwe": "津巴布韦", "Namibia": "纳米比亚", "Madagascar": "马达加斯加",
    "Botswana": "博茨瓦纳", "Mauritania": "毛里塔尼亚",
    "Niger": "尼日尔", "Burundi": "布隆迪", "Sierra Leone": "塞拉利昂",
    "Liberia": "利比里亚", "Malawi": "马拉维", "Gambia": "冈比亚",
    "Guinea-Bissau": "几内亚比绍", "Eswatini": "斯威士兰",
    "Djibouti": "吉布提", "Somalia": "索马里", "Eritrea": "厄立特里亚",
    "Chad": "乍得", "Mauritius": "毛里求斯",
    "Central African Republic": "中非",
    "Seychelles": "塞舌尔", "Comoros": "科摩罗",
    "South Sudan": "南苏丹", "São Tomé and Príncipe": "圣多美和普林西比",
    "Lesotho": "莱索托",
    # 亚洲
    "China": "中国", "China PR": "中国",
    "United Arab Emirates": "阿联酋", "UAE": "阿联酋",
    "Syria": "叙利亚", "Oman": "阿曼", "Bahrain": "巴林",
    "Kuwait": "科威特", "Lebanon": "黎巴嫩", "Palestine": "巴勒斯坦",
    "India": "印度", "Vietnam": "越南", "Thailand": "泰国",
    "Indonesia": "印度尼西亚", "Malaysia": "马来西亚",
    "Philippines": "菲律宾", "Singapore": "新加坡",
    "Myanmar": "缅甸", "Cambodia": "柬埔寨", "Laos": "老挝",
    "Bangladesh": "孟加拉国", "Kyrgyzstan": "吉尔吉斯斯坦",
    "Tajikistan": "塔吉克斯坦", "Turkmenistan": "土库曼斯坦",
    "Mongolia": "蒙古", "Nepal": "尼泊尔", "Sri Lanka": "斯里兰卡",
    "Maldives": "马尔代夫", "Bhutan": "不丹",
    "Brunei Darussalam": "文莱", "Brunei": "文莱",
    "East Timor": "东帝汶", "Timor-Leste": "东帝汶",
    "Afghanistan": "阿富汗", "Pakistan": "巴基斯坦",
    "Yemen": "也门", "Hong Kong": "中国香港",
    "Chinese Taipei": "中国台湾",
    # 其他
    "Argentina U20": "阿根廷U20", "Brazil U20": "巴西U20",
}


def _translate_team(name: str) -> str:
    """英→中球队名翻译, 未收录返回原名"""
    if not name:
        return name
    # 精确匹配优先
    if name in _TEAM_NAME_ZH:
        return _TEAM_NAME_ZH[name]
    # 去空格后匹配
    stripped = name.strip()
    if stripped in _TEAM_NAME_ZH:
        return _TEAM_NAME_ZH[stripped]
    return name


def _translate_match(match: Dict) -> Dict:
    """递归翻译比赛数据中的球队名: homeTeam/awayTeam/team/opponent/player.team"""
    if not match:
        return match

    # 主客队
    for key in ("homeTeam", "awayTeam", "team"):
        if key in match and isinstance(match[key], dict) and "name" in match[key]:
            en = match[key]["name"]
            zh = _translate_team(en)
            match[key]["name"] = zh
            if "shortName" in match[key]:
                match[key]["shortName"] = _translate_team(match[key]["shortName"])

    # opponent (历史比赛)
    if "opponent" in match and isinstance(match["opponent"], dict) and "name" in match["opponent"]:
        match["opponent"]["name"] = _translate_team(match["opponent"]["name"])

    # player.team (射手榜)
    if "player" in match and isinstance(match["player"], dict):
        if "team" in match["player"] and isinstance(match["player"]["team"], dict):
            if "name" in match["player"]["team"]:
                match["player"]["team"]["name"] = _translate_team(match["player"]["team"]["name"])

    # 递归翻译 table (积分榜)
    if "table" in match:
        for team_entry in match["table"]:
            if "team" in team_entry and isinstance(team_entry["team"], dict):
                if "name" in team_entry["team"]:
                    team_entry["team"]["name"] = _translate_team(team_entry["team"]["name"])
                if "shortName" in team_entry["team"]:
                    team_entry["team"]["shortName"] = _translate_team(team_entry["team"]["shortName"])
    if "standings" in match:
        for standing in match["standings"]:
            _translate_match(standing)

    return match



class FootballDataLive:
    """football-data.org 实时数据接入"""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or API_KEY
        self.headers = {"X-Auth-Token": self.api_key}

    def _request(self, endpoint: str, cache_ttl: int = 300) -> Dict:
        """带缓存的API请求"""
        # 缓存文件名: 去掉非法字符
        safe_name = endpoint.replace('/', '_').replace('?', '_').replace('&', '_').replace('=', '_')
        cache_file = CACHE_DIR / f"{safe_name}.json"

        # 检查缓存
        if cache_file.exists():
            age = (Path(__file__).stat().st_mtime - cache_file.stat().st_mtime)
            if age < cache_ttl:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # P3: 缓存数据也需翻译 (旧缓存可能英文)
                if "matches" in data:
                    for m in data["matches"]:
                        _translate_match(m)
                if "standings" in data:
                    for s in data["standings"]:
                        _translate_match(s)
                if "scorers" in data:
                    for s in data["scorers"]:
                        _translate_match(s)
                return data

        # 实际请求
        url = f"{BASE_URL}{endpoint}"
        req = urllib.request.Request(url, headers=self.headers)
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())

            # P3: 球队名英→中翻译
            if "matches" in data:
                for m in data["matches"]:
                    _translate_match(m)
            if "standings" in data:
                for s in data["standings"]:
                    _translate_match(s)
            if "scorers" in data:
                for s in data["scorers"]:
                    _translate_match(s)

            # 写缓存
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            return data
        except Exception as e:
            logger.warning(f"API请求失败 {endpoint}: {e}")
            # 返回缓存(即使过期)
            if cache_file.exists():
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}

    def get_live_scores(self) -> List[Dict]:
        """获取正在进行的比赛实时比分"""
        data = self._request("/matches?status=LIVE", cache_ttl=60)
        return data.get('matches', [])

    def get_wc2026_fixtures(self) -> List[Dict]:
        """获取2026世界杯待赛赛程"""
        data = self._request("/competitions/WC/matches?season=2026&status=SCHEDULED", cache_ttl=3600)
        return data.get('matches', [])

    def get_wc2026_finished(self) -> List[Dict]:
        """获取2026世界杯已完赛比赛"""
        data = self._request("/competitions/WC/matches?season=2026&status=FINISHED", cache_ttl=3600)
        return data.get('matches', [])

    def get_wc2026_standings(self) -> List[Dict]:
        """获取2026世界杯积分榜"""
        data = self._request("/competitions/WC/standings?season=2026", cache_ttl=3600)
        return data.get('standings', [])

    def get_wc2026_scorers(self) -> List[Dict]:
        """获取2026世界杯射手榜"""
        data = self._request("/competitions/WC/scorers?season=2026", cache_ttl=3600)
        return data.get('scorers', [])

    def get_match_detail(self, match_id: int) -> Dict:
        """获取单场比赛详情"""
        return self._request(f"/matches/{match_id}", cache_ttl=300)

    def get_team_matches(self, team_id: int, limit: int = 10, season: int = None) -> List[Dict]:
        """获取球队近期比赛 (含跨赛事)"""
        params = [f"limit={limit}"]
        if season:
            params.append(f"season={season}")
        endpoint = f"/teams/{team_id}/matches?" + "&".join(params)
        data = self._request(endpoint, cache_ttl=3600)
        return data.get('matches', [])

    # ════════════════════════════════════════════════
    # Advanced Trend/Form — 球队真实战绩 + λ推导
    # ════════════════════════════════════════════════

    def get_team_form(self, team_id: int, limit: int = 20, season: int = None) -> Dict:
        """获取球队近期战绩 + 泊松λ参数

        Returns:
            {
                'team': str, 'matches': N, 'wins': N, 'draws': N, 'losses': N,
                'win_rate': float, 'goals_for': N, 'goals_against': N,
                'avg_gf': float, 'avg_ga': float,
                'home': {'games': N, 'avg_gf': float, 'avg_ga': float},
                'away': {'games': N, 'avg_gf': float, 'avg_ga': float},
                'lambda_home_attack': float, 'lambda_home_defense': float,
                'lambda_away_attack': float, 'lambda_away_defense': float,
                'lambda_overall_attack': float, 'lambda_overall_defense': float,
                'recent_results': [{'date','comp','opponent','score','result','is_home'}],
                'form_trend': str,  # 'WWDLW' 近5场缩写
            }
        """
        # 跨赛季拉取: 不指定season时, 尝试多个赛季直到凑够limit场
        all_matches = []
        if season:
            all_matches = self.get_team_matches(team_id, limit=limit, season=season)
        else:
            # 自动跨赛季: 当前赛季 → 上赛季 → 上上赛季
            from datetime import datetime
            current_year = datetime.now().year
            for s in [current_year, current_year - 1, current_year - 2]:
                try:
                    m = self.get_team_matches(team_id, limit=limit, season=s)
                    all_matches.extend(m)
                    if len(all_matches) >= limit:
                        break
                except Exception:
                    pass
        
        matches = all_matches[:limit]
        finished = [m for m in matches if m.get('status') == 'FINISHED'
                    and m.get('score', {}).get('fullTime', {}).get('home') is not None]

        W = D = L = 0
        gf_total = ga_total = 0
        hg = hc = hn = 0  # home goals/conceded/games
        ag = ac = an = 0  # away
        recent = []
        form_chars = []

        for m in finished:
            sc = m.get('score', {}).get('fullTime', {})
            hs = sc.get('home', 0)
            as_ = sc.get('away', 0)
            is_home = m.get('homeTeam', {}).get('id') == team_id
            winner = m.get('score', {}).get('winner', '')

            gf = hs if is_home else as_
            ga = as_ if is_home else hs
            gf_total += gf
            ga_total += ga

            if is_home:
                hg += gf; hc += ga; hn += 1
            else:
                ag += gf; ac += ga; an += 1

            if winner == 'DRAW':
                res = 'D'; D += 1
            elif (winner == 'HOME_TEAM') == is_home:
                res = 'W'; W += 1
            else:
                res = 'L'; L += 1

            form_chars.append(res)

            opp_name = (m.get('awayTeam', {}) if is_home else m.get('homeTeam', {})).get('name', '?')
            recent.append({
                'date': m.get('utcDate', '')[:10],
                'comp': m.get('competition', {}).get('code', '?'),
                'opponent': opp_name,
                'score': f"{hs}-{as_}",
                'result': res,
                'is_home': is_home,
            })

        n = W + D + L
        if n == 0:
            return {'team': str(team_id), 'matches': 0, 'error': 'no finished matches'}

        result = {
            'team': finished[0].get('homeTeam', {}).get('name', '') if finished else str(team_id),
            'team_id': team_id,
            'matches': n,
            'wins': W, 'draws': D, 'losses': L,
            'win_rate': round(W / n, 3),
            'goals_for': gf_total, 'goals_against': ga_total,
            'avg_gf': round(gf_total / n, 2), 'avg_ga': round(ga_total / n, 2),
            'home': {'games': hn, 'avg_gf': round(hg / hn, 2) if hn else 0,
                     'avg_ga': round(hc / hn, 2) if hn else 0},
            'away': {'games': an, 'avg_gf': round(ag / an, 2) if an else 0,
                     'avg_ga': round(ac / an, 2) if an else 0},
            'recent_results': recent[:10],
            'form_trend': ''.join(form_chars[:5]),
        }

        # 泊松λ推导
        if hn and an:
            result['lambda_home_attack'] = round(hg / hn, 2)
            result['lambda_home_defense'] = round(hc / hn, 2)
            result['lambda_away_attack'] = round(ag / an, 2)
            result['lambda_away_defense'] = round(ac / an, 2)
            result['lambda_overall_attack'] = round((hg / hn + ag / an) / 2, 2)
            result['lambda_overall_defense'] = round((hc / hn + ac / an) / 2, 2)

        return result

    def get_match_form_analysis(self, home_team: str, away_team: str,
                                  season: int = None) -> Dict:
        """获取对阵双方的真实战绩+λ参数 (用于比赛分析)

        Args:
            home_team: 主队名称 (英文, 如 "Arsenal FC")
            away_team: 客队名称
            season: 赛季年份

        Returns:
            {
                'home': {...form...},
                'away': {...form...},
                'lambda_home_expected': float,  # 主队期望进球
                'lambda_away_expected': float,  # 客队期望进球
                'lambda_total': float,          # 总期望进球
                'predicted_scores': [...],      # Top-5比分
            }
        """
        # 查找球队ID
        home_id = self._find_team_id(home_team)
        away_id = self._find_team_id(away_team)

        if not home_id or not away_id:
            return {'error': f'未找到球队ID: {home_team}={home_id}, {away_team}={away_id}'}

        home_form = self.get_team_form(home_id, limit=20, season=season)
        away_form = self.get_team_form(away_id, limit=20, season=season)

        if 'error' in home_form or 'error' in away_form:
            return {'error': 'form数据不足', 'home': home_form, 'away': away_form}

        # 期望进球: 主队主场进攻 × 客队客场防守 / 联赛均值
        # 简化: λ_home = (home_attack + away_defense) / 2
        h_att = home_form.get('lambda_home_attack', home_form.get('avg_gf', 1.0))
        a_def = away_form.get('lambda_away_defense', away_form.get('avg_ga', 1.0))
        a_att = away_form.get('lambda_away_attack', away_form.get('avg_gf', 1.0))
        h_def = home_form.get('lambda_home_defense', home_form.get('avg_ga', 1.0))

        lam_home = round((h_att + a_def) / 2, 2)
        lam_away = round((a_att + h_def) / 2, 2)

        # 泊松比分预测
        import math
        scores = []
        for i in range(6):
            for j in range(6):
                p = (lam_home**i * math.exp(-lam_home) / math.factorial(i)) * \
                    (lam_away**j * math.exp(-lam_away) / math.factorial(j))
                if p > 0.01:
                    scores.append({'score': f"{i}-{j}", 'prob': round(p, 4),
                                   'outcome': 'H' if i > j else ('D' if i == j else 'A')})
        scores.sort(key=lambda x: -x['prob'])

        return {
            'home': home_form,
            'away': away_form,
            'lambda_home_expected': lam_home,
            'lambda_away_expected': lam_away,
            'lambda_total': round(lam_home + lam_away, 2),
            'predicted_scores': scores[:5],
        }

    def _find_team_id(self, team_name: str) -> Optional[int]:
        """从世界杯已完赛比赛中查找球队ID"""
        # 缓存球队ID
        if not hasattr(self, '_team_id_cache'):
            self._team_id_cache = {}
            try:
                wc = self._request("/competitions/WC/matches?season=2026&status=FINISHED", cache_ttl=3600)
                for m in wc.get('matches', []):
                    for side in ['homeTeam', 'awayTeam']:
                        t = m.get(side, {})
                        if t.get('id'):
                            self._team_id_cache[t['name'].lower()] = t['id']
                # 也查待赛比赛
                wc2 = self._request("/competitions/WC/matches?season=2026&status=SCHEDULED", cache_ttl=3600)
                for m in wc2.get('matches', []):
                    for side in ['homeTeam', 'awayTeam']:
                        t = m.get(side, {})
                        if t.get('id'):
                            self._team_id_cache[t['name'].lower()] = t['id']
            except Exception:
                pass

        return self._team_id_cache.get(team_name.lower())

    def format_form_report(self, home_team: str, away_team: str, season: int = None) -> str:
        """生成可读的战绩+λ分析报告 (用于chat端点)"""
        analysis = self.get_match_form_analysis(home_team, away_team, season)

        if 'error' in analysis:
            return f"⚠️ 战绩数据不足: {analysis.get('error', '')}"

        h = analysis['home']
        a = analysis['away']
        lh = analysis['lambda_home_expected']
        la = analysis['lambda_away_expected']
        lt = analysis['lambda_total']

        lines = []
        lines.append(f"📊 真实战绩分析 (Trend/Form)")
        lines.append(f"{'─'*50}")
        lines.append(f"")
        lines.append(f"【{h['team']}】近{h['matches']}场  {h['form_trend']}")
        lines.append(f"  胜{h['wins']} 平{h['draws']} 负{h['losses']}  胜率{h['win_rate']:.0%}")
        lines.append(f"  进{h['goals_for']} 失{h['goals_against']}  场均进{h['avg_gf']} 失{h['avg_ga']}")
        if h['home']['games']:
            lines.append(f"  🏠 主场{h['home']['games']}场: 进{h['home']['avg_gf']}/场 失{h['home']['avg_ga']}/场")
        if h['away']['games']:
            lines.append(f"  ✈️ 客场{h['away']['games']}场: 进{h['away']['avg_gf']}/场 失{h['away']['avg_ga']}/场")
        if 'lambda_home_attack' in h:
            lines.append(f"  ⚡ λ: 主攻{h['lambda_home_attack']} 主防{h['lambda_home_defense']} 客攻{h['lambda_away_attack']} 客防{h['lambda_away_defense']}")

        lines.append(f"")
        lines.append(f"【{a['team']}】近{a['matches']}场  {a['form_trend']}")
        lines.append(f"  胜{a['wins']} 平{a['draws']} 负{a['losses']}  胜率{a['win_rate']:.0%}")
        lines.append(f"  进{a['goals_for']} 失{a['goals_against']}  场均进{a['avg_gf']} 失{a['avg_ga']}")
        if a['home']['games']:
            lines.append(f"  🏠 主场{a['home']['games']}场: 进{a['home']['avg_gf']}/场 失{a['home']['avg_ga']}/场")
        if a['away']['games']:
            lines.append(f"  ✈️ 客场{a['away']['games']}场: 进{a['away']['avg_gf']}/场 失{a['away']['avg_ga']}/场")
        if 'lambda_home_attack' in a:
            lines.append(f"  ⚡ λ: 主攻{a['lambda_home_attack']} 主防{a['lambda_home_defense']} 客攻{a['lambda_away_attack']} 客防{a['lambda_away_defense']}")

        lines.append(f"")
        lines.append(f"{'─'*50}")
        lines.append(f"🎯 期望进球: {h['team']}={lh}  {a['team']}={la}  总球E={lt}")
        lines.append(f"")
        lines.append(f"📋 泊松比分预测 Top 5:")
        for s in analysis['predicted_scores']:
            lines.append(f"  {s['score']}: {s['prob']:.1%} ({s['outcome']})")

        # OU判断
        over_prob = sum(s['prob'] for s in analysis['predicted_scores']
                       if sum(map(int, s['score'].split('-'))) > 2.5)
        lines.append(f"")
        lines.append(f"💰 大小球2.5: {'大球' if over_prob > 0.5 else '小球'}倾向 ({over_prob:.0%})")

        return '\n'.join(lines)

    def get_competition_matches(self, competition_code: str, season: int = None,
                                 matchday: int = None, status: str = None) -> List[Dict]:
        """获取赛事比赛 (通用)"""
        params = []
        if season:
            params.append(f"season={season}")
        if matchday:
            params.append(f"matchday={matchday}")
        if status:
            params.append(f"status={status}")
        query = "&".join(params)
        endpoint = f"/competitions/{competition_code}/matches"
        if query:
            endpoint += f"?{query}"
        data = self._request(endpoint, cache_ttl=3600)
        return data.get('matches', [])

    def get_available_competitions(self) -> List[Dict]:
        """获取可访问的赛事列表"""
        data = self._request("/competitions?plan=TIER_ONE", cache_ttl=86400)
        return data.get('competitions', [])

    # ════════════════════════════════════════════════
    # Odds Add-On (€15/月) — 40联赛 Pre-Match 1X2赔率
    # ════════════════════════════════════════════════

    def get_match_odds(self, match_id: int) -> Dict:
        """获取单场比赛赔率 (需Odds Add-On)"""
        data = self._request(f"/matches/{match_id}", cache_ttl=300)
        odds = data.get('odds', {})
        if 'msg' in odds:
            logger.warning(f"赔率不可用: {odds['msg']}")
            return {}
        return odds

    def get_competition_odds(self, competition_code: str, season: int = None,
                              matchday: int = None) -> List[Dict]:
        """获取赛事全部比赛赔率 (需Odds Add-On)
        
        Args:
            competition_code: 赛事代码 (PL=英超, PD=西甲, BL1=德甲, SA=意甲, FL1=法甲, 
                             CL=欧冠, WC=世界杯, EC=欧洲杯...)
            season: 赛季年份 (如2026)
            matchday: 指定轮次
        
        Returns:
            比赛列表, 每场含 odds 字段 (home/draw/away 赔率)
        """
        params = []
        if season:
            params.append(f"season={season}")
        if matchday:
            params.append(f"matchday={matchday}")
        query = "&".join(params)
        endpoint = f"/competitions/{competition_code}/matches"
        if query:
            endpoint += f"?{query}"
        data = self._request(endpoint, cache_ttl=300)  # 赔率5分钟刷新
        return data.get('matches', [])

    def extract_1x2_odds(self, match: Dict) -> Optional[Dict]:
        """从比赛数据中提取1X2赔率
        
        Returns:
            {'home': float, 'draw': float, 'away': float} 或 None
        """
        odds = match.get('odds', {})
        if not odds or 'msg' in odds:
            return None
        
        # football-data.org 赔率格式
        home = odds.get('homeWin')
        draw = odds.get('draw')
        away = odds.get('awayWin')
        
        if home and draw and away:
            return {
                'home': float(home),
                'draw': float(draw),
                'away': float(away),
            }
        return None

    def odds_to_implied_probs(self, odds: Dict) -> Dict:
        """1X2赔率 → 隐含概率 (去抽水)"""
        h, d, a = odds['home'], odds['draw'], odds['away']
        inv = 1/h + 1/d + 1/a
        return {
            'home': round((1/h)/inv, 4),
            'draw': round((1/d)/inv, 4),
            'away': round((1/a)/inv, 4),
            'overround': round((inv-1)*100, 2),  # 抽水率%
        }

    def odds_to_handicap_line(self, odds: Dict) -> float:
        """从1X2赔率反推亚盘让球线 (泊松近似)
        
        经验公式: 让球线 ≈ (imp_home - imp_away) * 3.5
        正值=主队让球, 负值=客队让球
        """
        probs = self.odds_to_implied_probs(odds)
        diff = probs['home'] - probs['away']
        return round(diff * 3.5, 2)

    def odds_to_ou_line(self, odds: Dict) -> float:
        """从1X2赔率反推大小球线 (泊松近似)
        
        经验公式: OU线 ≈ 2.0 + (1/平赔 - 0.28) * 4
        平赔越低 → 进球越少 → OU线越低
        """
        d_odds = odds['draw']
        return round(2.0 + (1/d_odds - 0.28) * 4, 2)

    def get_odds_for_prediction(self, competition_code: str, season: int = None,
                                  team_name: str = None) -> List[Dict]:
        """获取可用于预测的赔率数据 (批量)
        
        Returns:
            [{'match': {...}, 'odds': {...}, 'implied': {...}, 'handicap': float, 'ou_line': float}]
        """
        matches = self.get_competition_odds(competition_code, season)
        results = []
        
        for m in matches:
            odds = self.extract_1x2_odds(m)
            if not odds:
                continue
            
            home = m.get('homeTeam', {}).get('name', '')
            away = m.get('awayTeam', {}).get('awayTeam', {}).get('name', '') or m.get('awayTeam', {}).get('name', '')
            
            if team_name and team_name.lower() not in home.lower() and team_name.lower() not in away.lower():
                continue
            
            implied = self.odds_to_implied_probs(odds)
            hc = self.odds_to_handicap_line(odds)
            ou = self.odds_to_ou_line(odds)
            
            results.append({
                'match_id': m.get('id'),
                'home': home, 'away': away,
                'date': m.get('utcDate', ''),
                'status': m.get('status', ''),
                'odds': odds,
                'implied': implied,
                'handicap_est': hc,
                'ou_line_est': ou,
            })
        
        return results

    # ════════════════════════════════════════════════
    # 常用赛事代码速查
    # ════════════════════════════════════════════════
    COMPETITION_CODES = {
        'PL': '英格兰超级联赛',
        'PD': '西班牙甲级联赛',
        'BL1': '德国甲级联赛',
        'SA': '意大利甲级联赛',
        'FL1': '法国甲级联赛',
        'ELC': '英格兰冠军联赛',
        'CL': '欧洲冠军联赛',
        'EC': '欧洲杯',
        'WC': '世界杯',
        'CLI': '南美解放者杯',
        'MLS': '美国职业大联盟',
        'BSA': '巴西甲级联赛',
    }

    def sync_to_database(self, db_path: str = None) -> Dict:
        """同步世界杯数据到数据库"""
        import sqlite3
        if db_path is None:
            db_path = str(Path(__file__).parent.parent / "data" / "football_data.db")

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        stats = {'matches': 0, 'standings': 0, 'scorers': 0}

        # 同步已完赛比赛
        finished = self.get_wc2026_finished()
        for m in finished:
            try:
                cur.execute("""
                    INSERT OR REPLACE INTO matches
                    (match_id, match_date, league_name, home_team_name, away_team_name,
                     home_score, away_score, final_result, status, matchday)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    int(m['id']), m.get('utcDate', ''),
                    '世界杯',
                    m.get('homeTeam', {}).get('name', ''),
                    m.get('awayTeam', {}).get('name', ''),
                    m.get('score', {}).get('fullTime', {}).get('home'),
                    m.get('score', {}).get('fullTime', {}).get('away'),
                    m.get('score', {}).get('winner', ''),
                    m.get('status', ''),
                    m.get('matchday', 0)
                ))
                stats['matches'] += 1
            except Exception as e:
                logger.debug(f"同步比赛失败: {e}")

        conn.commit()
        conn.close()
        logger.info(f"同步完成: {stats}")
        return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fdl = FootballDataLive()

    print("\n=== 2026世界杯实时数据 ===\n")

    # 实时比分
    live = fdl.get_live_scores()
    print(f"直播中: {len(live)}场")

    # 积分榜
    standings = fdl.get_wc2026_standings()
    print(f"\n积分榜 ({len(standings)}个组):")
    for group in standings[:3]:
        print(f"  {group.get('group','?')}:")
        for t in group.get('table', [])[:3]:
            print(f"    {t['position']}. {t['team']['name']} {t['points']}pts")

    # 射手榜
    scorers = fdl.get_wc2026_scorers()
    print(f"\n射手榜 Top 5:")
    for s in scorers[:5]:
        print(f"  {s['player']['name']} ({s['team']['name']}): {s['goals']}球")

    # 待赛
    fixtures = fdl.get_wc2026_fixtures()
    print(f"\n待赛: {len(fixtures)}场")
    for f in fixtures[:5]:
        print(f"  {f.get('utcDate','')[:10]} {f.get('homeTeam',{}).get('name','?')} vs {f.get('awayTeam',{}).get('name','?')}")
