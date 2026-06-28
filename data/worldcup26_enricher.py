"""
World Cup 2026 Data Enricher — 从小红书(worldcup26.ir API)拉取全量数据
================================================================
数据源: https://worldcup26.ir (免费REST API, 无需认证)
端点:
  - /get/games   → 104场比赛(含进球者!)
  - /get/groups  → 12组积分榜
  - /get/teams   → 48队名单

核心价值: 进球者数据 + 完整赛果 → 球队攻击力/防守力精准画像
用法:
    from data.worldcup26_enricher import WC26DataEnricher
    e = WC26DataEnricher()
    e.load()
    form = e.get_team_form('France')  # → 完整WC2026战绩+进球分布
    scorers = e.get_top_scorers(10)   # → 射手榜Top 10
"""

import json
import logging
import urllib.request
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

API_BASE = "https://worldcup26.ir"
CACHE_DIR = Path(__file__).parent / "api_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── 队名翻译: worldcup26.ir English → Chinese ──
TEAM_EN_TO_ZH = {
    "Mexico": "墨西哥", "South Africa": "南非", "South Korea": "韩国",
    "Czech Republic": "捷克", "Canada": "加拿大", "Bosnia and Herzegovina": "波黑",
    "Qatar": "卡塔尔", "Switzerland": "瑞士", "Brazil": "巴西",
    "Morocco": "摩洛哥", "Haiti": "海地", "Scotland": "苏格兰",
    "United States": "美国", "Paraguay": "巴拉圭", "Australia": "澳大利亚",
    "Turkey": "土耳其", "Germany": "德国", "Curaçao": "库拉索",
    "Ivory Coast": "科特迪瓦", "Ecuador": "厄瓜多尔", "Netherlands": "荷兰",
    "Japan": "日本", "Sweden": "瑞典", "Tunisia": "突尼斯",
    "Belgium": "比利时", "Egypt": "埃及", "Iran": "伊朗",
    "New Zealand": "新西兰", "Spain": "西班牙", "Cape Verde": "佛得角",
    "Saudi Arabia": "沙特阿拉伯", "Uruguay": "乌拉圭", "France": "法国",
    "Senegal": "塞内加尔", "Iraq": "伊拉克", "Norway": "挪威",
    "Argentina": "阿根廷", "Algeria": "阿尔及利亚", "Austria": "奥地利",
    "Jordan": "约旦", "Portugal": "葡萄牙", "DR Congo": "民主刚果",
    "Uzbekistan": "乌兹别克斯坦", "Colombia": "哥伦比亚",
    "England": "英格兰", "Croatia": "克罗地亚", "Ghana": "加纳",
    "Panama": "巴拿马",
}

def _en_to_zh(name: str) -> str:
    return TEAM_EN_TO_ZH.get(name, name)

@dataclass
class TeamWCForm:
    """单队在WC2026的完整战绩"""
    team_id: int
    team_en: str
    team_zh: str
    group: str
    matches: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0
    avg_gf: float = 0.0
    avg_ga: float = 0.0
    goal_diff: float = 0.0
    clean_sheets: int = 0
    failed_to_score: int = 0
    form_trend: str = ""         # W/D/L sequence
    # 进球分布
    scorers: Dict[str, int] = field(default_factory=dict)  # player → goals
    total_scorers: int = 0       # 有进球的球员数
    # 对手强度
    opponents: List[Dict] = field(default_factory=list)
    # 详细赛果
    results: List[Dict] = field(default_factory=list)

    @property
    def attack_potency(self) -> float:
        """攻击力 0~1: 场均进球/3.0 归一化, 上限1.0"""
        return min(1.0, self.avg_gf / 3.0)

    @property
    def defense_stability(self) -> float:
        """防守力 0~1: 1 - 场均失球/3.0, 越高越好"""
        return max(0.0, 1.0 - self.avg_ga / 3.0)

    @property
    def top_scorer(self) -> Optional[Tuple[str, int]]:
        if not self.scorers:
            return None
        best = max(self.scorers.items(), key=lambda x: x[1])
        return best

    @property
    def scorer_diversity(self) -> float:
        """进球分布: 进球人数/总进球, 越高=多点开花"""
        if self.goals_for == 0:
            return 0.0
        return min(1.0, self.total_scorers / max(1, self.goals_for))

class WC26DataEnricher:
    """World Cup 2026 数据增强器"""

    def __init__(self):
        self._games: List[Dict] = []
        self._groups: List[Dict] = []
        self._teams: List[Dict] = []
        self._team_name_to_id: Dict[str, int] = {}
        self._team_id_to_name: Dict[int, str] = {}
        self._team_id_to_group: Dict[int, str] = {}
        self._team_forms: Dict[str, TeamWCForm] = {}  # key=zh_name
        self._all_scorers: Dict[str, int] = {}          # player → total goals
        self._loaded = False

    # ── API 请求层 ──
    def _fetch(self, endpoint: str, ttl: int = 300) -> Dict:
        """带缓存的 API 请求"""
        safe_name = endpoint.replace('/', '_').replace('?', '_')
        cache_file = CACHE_DIR / f"wc26_{safe_name}.json"

        if cache_file.exists():
            age = time.time() - cache_file.stat().st_mtime
            if age < ttl:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)

        url = f"{API_BASE}{endpoint}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FootballAI/4.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            return data
        except Exception as e:
            logger.warning(f"API {endpoint} 失败: {e}")
            if cache_file.exists():
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}

    # ── 数据加载 ──
    def load(self, force: bool = False):
        """加载全部数据"""
        if self._loaded and not force:
            return

        logger.info("WC26DataEnricher: 加载数据...")

        # 1. 球队映射
        teams_data = self._fetch("/get/teams", ttl=3600)
        self._teams = teams_data if isinstance(teams_data, list) else teams_data.get('teams', [])
        for t in self._teams:
            tid = int(t.get('id', 0))
            en = t.get('name_en', '')
            self._team_name_to_id[en.lower()] = tid
            self._team_id_to_name[tid] = en
            self._team_id_to_group[tid] = t.get('groups', '?')
        logger.info(f"  球队: {len(self._teams)}")

        # 2. 分组积分榜
        groups_data = self._fetch("/get/groups", ttl=600)
        self._groups = groups_data if isinstance(groups_data, list) else groups_data.get('groups', [])

        # 3. 比赛数据 (含进球者!)
        games_data = self._fetch("/get/games", ttl=300)
        self._games = games_data if isinstance(games_data, list) else games_data.get('games', [])
        logger.info(f"  比赛: {len(self._games)}")

        # 4. 构建球队战绩
        self._build_team_forms()
        self._loaded = True
        logger.info(f"  球队战绩: {len(self._team_forms)}队")

    def _parse_scorers(self, scorers_raw) -> Dict[str, int]:
        """解析进球者字符串 → {球员: 进球数}
        
        API格式: {"Player Name 27'", "Other Player 90+4'"}
        特殊: 含单引号(分钟标记)导致JSON解析失败, 使用正则提取
        """
        result = {}
        if not scorers_raw or scorers_raw == 'null' or scorers_raw is None:
            return result
        
        try:
            raw_str = scorers_raw if isinstance(scorers_raw, str) else str(scorers_raw)
            # 正则提取: 球员名(字母/重音/点/空格/连字符) + 空格 + 分钟数字
            # 格式: "Player Name 27'" or "Player Name 90+4'" or "Player Name 17' (p)"
            import re
            names = re.findall(r'([A-Za-zÀ-ÿ\.\s\-\']+?)\s+\d+[\+\']*', raw_str)
            for name in names:
                name = name.strip().strip("'").strip('"')
                if name and len(name) > 1:  # 过滤单字符噪音
                    result[name] = result.get(name, 0) + 1
        except Exception as e:
            logger.debug(f"解析进球者失败: {str(scorers_raw)[:80]}... {e}")
        return result

    def _build_team_forms(self):
        """从比赛数据构建每队战绩"""
        # 初始化
        team_data: Dict[int, Dict] = {}
        for t in self._teams:
            tid = int(t.get('id', 0))
            team_data[tid] = {
                'goals_for': 0, 'goals_against': 0,
                'wins': 0, 'draws': 0, 'losses': 0,
                'clean_sheets': 0, 'failed_to_score': 0,
                'results': [], 'scorers': {}, 'opponents': [],
                'matches': 0,
            }

        # 统计已完成比赛
        for g in self._games:
            if g.get('finished') != 'TRUE':
                continue

            hid = int(g.get('home_team_id', 0))
            aid = int(g.get('away_team_id', 0))
            hs = int(g.get('home_score', 0) or 0)
            aw = int(g.get('away_score', 0) or 0)

            if hid not in team_data or aid not in team_data:
                continue

            # 主队统计
            hd = team_data[hid]
            hd['matches'] += 1
            hd['goals_for'] += hs
            hd['goals_against'] += aw
            if hs > aw:
                hd['wins'] += 1
            elif hs == aw:
                hd['draws'] += 1
            else:
                hd['losses'] += 1
            if aw == 0:
                hd['clean_sheets'] += 1
            if hs == 0:
                hd['failed_to_score'] += 1

            # 主队进球者
            h_scorers = self._parse_scorers(g.get('home_scorers'))
            for name, goals in h_scorers.items():
                hd['scorers'][name] = hd['scorers'].get(name, 0) + goals
                self._all_scorers[name] = self._all_scorers.get(name, 0) + goals

            # 客队统计
            ad = team_data[aid]
            ad['matches'] += 1
            ad['goals_for'] += aw
            ad['goals_against'] += hs
            if aw > hs:
                ad['wins'] += 1
            elif aw == hs:
                ad['draws'] += 1
            else:
                ad['losses'] += 1
            if hs == 0:
                ad['clean_sheets'] += 1
            if aw == 0:
                ad['failed_to_score'] += 1

            # 客队进球者
            a_scorers = self._parse_scorers(g.get('away_scorers'))
            for name, goals in a_scorers.items():
                ad['scorers'][name] = ad['scorers'].get(name, 0) + goals
                self._all_scorers[name] = self._all_scorers.get(name, 0) + goals

            # 记录对手
            h_en = self._team_id_to_name.get(hid, '?')
            a_en = self._team_id_to_name.get(aid, '?')
            group = g.get('group', '?')
            md = g.get('matchday', '?')

            hd['opponents'].append({'team': a_en, 'gf': hs, 'ga': aw, 'group': group, 'md': md})
            ad['opponents'].append({'team': h_en, 'gf': aw, 'ga': hs, 'group': group, 'md': md})

            # Form trend
            hd['results'].append({'opp': a_en, 'gf': hs, 'ga': aw, 'md': md, 'group': group})
            ad['results'].append({'opp': h_en, 'gf': aw, 'ga': hs, 'md': md, 'group': group})

        # 构建 TeamWCForm
        for tid, d in team_data.items():
            if d['matches'] == 0:
                continue
            en = self._team_id_to_name.get(tid, f'Team{tid}')
            zh = _en_to_zh(en)
            gp = d['matches']
            avg_gf = round(d['goals_for'] / gp, 2)
            avg_ga = round(d['goals_against'] / gp, 2)

            # Form trend (按 matchday 排序)
            d['results'].sort(key=lambda r: (r.get('group', 'Z'), r.get('md', 1)))
            trend = ''
            for r in d['results']:
                if r['gf'] > r['ga']:
                    trend += 'W'
                elif r['gf'] == r['ga']:
                    trend += 'D'
                else:
                    trend += 'L'

            form = TeamWCForm(
                team_id=tid,
                team_en=en,
                team_zh=zh,
                group=self._team_id_to_group.get(tid, '?'),
                matches=gp, wins=d['wins'], draws=d['draws'], losses=d['losses'],
                goals_for=d['goals_for'], goals_against=d['goals_against'],
                avg_gf=avg_gf, avg_ga=avg_ga,
                goal_diff=round(avg_gf - avg_ga, 2),
                clean_sheets=d['clean_sheets'],
                failed_to_score=d['failed_to_score'],
                form_trend=trend,
                scorers=d['scorers'],
                total_scorers=len(d['scorers']),
                opponents=d['opponents'],
                results=d['results'],
            )
            self._team_forms[zh] = form

    # ── 查询接口 ──
    def get_team_form(self, team_zh: str) -> Optional[TeamWCForm]:
        """获取单队WC2026战绩"""
        if not self._loaded:
            self.load()
        # 精确匹配 + 模糊匹配
        if team_zh in self._team_forms:
            return self._team_forms[team_zh]
        # 模糊匹配
        for zh, form in self._team_forms.items():
            if team_zh in zh or zh in team_zh:
                return form
        return None

    def get_matchup_form(self, home_zh: str, away_zh: str) -> Dict:
        """获取两队WC2026战绩对比"""
        hf = self.get_team_form(home_zh)
        af = self.get_team_form(away_zh)

        result = {
            'home': hf.__dict__ if hf else None,
            'away': af.__dict__ if af else None,
            'goal_diff_advantage': 0.0,
            'scorer_advantage': 'even',
            'attack_comparison': 'even',
            'defense_comparison': 'even',
            'confidence': 0.0,
        }

        if hf and af:
            result['goal_diff_advantage'] = round(hf.goal_diff - af.goal_diff, 2)

            # 攻击力对比
            if hf.attack_potency > af.attack_potency + 0.2:
                result['attack_comparison'] = 'home_dominant'
            elif af.attack_potency > hf.attack_potency + 0.2:
                result['attack_comparison'] = 'away_dominant'
            else:
                result['attack_comparison'] = 'balanced'

            # 防守力对比
            if hf.defense_stability > af.defense_stability + 0.2:
                result['defense_comparison'] = 'home_solid'
            elif af.defense_stability > hf.defense_stability + 0.2:
                result['defense_comparison'] = 'away_solid'
            else:
                result['defense_comparison'] = 'balanced'

            # 得分手优势
            h_best = hf.top_scorer
            a_best = af.top_scorer
            if h_best and a_best:
                if h_best[1] >= a_best[1] + 2:
                    result['scorer_advantage'] = 'home'
                elif a_best[1] >= h_best[1] + 2:
                    result['scorer_advantage'] = 'away'
                else:
                    result['scorer_advantage'] = 'even'

            result['confidence'] = min(0.9, 0.3 + abs(result['goal_diff_advantage']) * 0.15)

        return result

    def get_top_scorers(self, limit: int = 20) -> List[Tuple[str, int]]:
        """射手榜 Top N"""
        if not self._loaded:
            self.load()
        sorted_scorers = sorted(self._all_scorers.items(), key=lambda x: x[1], reverse=True)
        return sorted_scorers[:limit]

    def get_group_standings(self) -> Dict[str, List[Dict]]:
        """获取所有小组积分榜 (team_name → {pts, gf, ga, gd, gp})"""
        if not self._loaded:
            self.load()
        standings = {}
        for g in self._groups:
            group_name = g.get('name', '?')
            teams = []
            for t in g.get('teams', []):
                tid = int(t.get('team_id', 0))
                en = self._team_id_to_name.get(tid, f'Team{tid}')
                teams.append({
                    'team_en': en,
                    'team_zh': _en_to_zh(en),
                    'pts': int(t.get('pts', 0)),
                    'gf': int(t.get('gf', 0)),
                    'ga': int(t.get('ga', 0)),
                    'gd': int(t.get('gd', 0)),
                })
            standings[group_name] = sorted(teams, key=lambda x: x['pts'], reverse=True)
        return standings

    def get_all_completed_matches(self) -> List[Dict]:
        """获取所有已完赛的比赛"""
        if not self._loaded:
            self.load()
        completed = []
        for g in self._games:
            if g.get('finished') != 'TRUE':
                continue
            hid = int(g.get('home_team_id', 0))
            aid = int(g.get('away_team_id', 0))
            completed.append({
                'id': g.get('id'),
                'home': self._team_id_to_name.get(hid, f'T{hid}'),
                'away': self._team_id_to_name.get(aid, f'T{aid}'),
                'home_zh': _en_to_zh(self._team_id_to_name.get(hid, '?')),
                'away_zh': _en_to_zh(self._team_id_to_name.get(aid, '?')),
                'score': f"{g.get('home_score',0)}-{g.get('away_score',0)}",
                'group': g.get('group'),
                'matchday': g.get('matchday'),
                'home_scorers': g.get('home_scorers'),
                'away_scorers': g.get('away_scorers'),
            })
        return completed

    def format_team_report(self, team_zh: str) -> str:
        """生成球队战绩可读报告"""
        form = self.get_team_form(team_zh)
        if not form:
            return f"⚠️ 未找到 {team_zh} 的数据"

        lines = []
        lines.append(f"📊 【{form.team_zh}】WC2026 小组赛战绩 (Group {form.group})")
        lines.append(f"{'─'*50}")
        lines.append(f"  场次: {form.matches} | 胜{form.wins} 平{form.draws} 负{form.losses}")
        lines.append(f"  进球: {form.goals_for} 失球: {form.goals_against} | "
                     f"场均{form.avg_gf}进/{form.avg_ga}失 净{form.goal_diff:+.2f}")
        lines.append(f"  攻击力: {form.attack_potency:.0%} | 防守力: {form.defense_stability:.0%}")
        lines.append(f"  形态: {form.form_trend} | 零封: {form.clean_sheets} | 未进球: {form.failed_to_score}")

        if form.scorers:
            lines.append(f"\n  ⚽ 进球分布 ({form.total_scorers}人进球):")
            for name, goals in sorted(form.scorers.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"    {name}: {goals}球")

        lines.append(f"\n  📋 赛果:")
        for r in form.results:
            result = '✅' if r['gf'] > r['ga'] else ('🤝' if r['gf'] == r['ga'] else '❌')
            lines.append(f"    {result} vs {_en_to_zh(r['opp'])} {r['gf']}-{r['ga']} (MD{r['md']})")

        return '\n'.join(lines)

    def format_matchup_report(self, home_zh: str, away_zh: str) -> str:
        """生成两队战绩对比报告"""
        hf = self.get_team_form(home_zh)
        af = self.get_team_form(away_zh)

        if not hf or not af:
            return f"⚠️ 数据不足: {home_zh}={bool(hf)} {away_zh}={bool(af)}"

        comp = self.get_matchup_form(home_zh, away_zh)
        gap = comp['goal_diff_advantage']

        lines = []
        lines.append(f"⚔️ 【{home_zh} vs {away_zh}】WC2026 战绩对比")
        lines.append(f"{'─'*55}")

        # 并行显示
        lines.append(f"  {'指标':<12} {home_zh:<16} {away_zh:<16}")
        lines.append(f"  {'─'*44}")
        lines.append(f"  {'场次':<12} {hf.matches:<16} {af.matches:<16}")
        lines.append(f"  {'胜/平/负':<12} {f'{hf.wins}/{hf.draws}/{hf.losses}':<16} {f'{af.wins}/{af.draws}/{af.losses}':<16}")
        lines.append(f"  {'进球/失球':<12} {f'{hf.goals_for}/{hf.goals_against}':<16} {f'{af.goals_for}/{af.goals_against}':<16}")
        lines.append(f"  {'场均进球':<12} {hf.avg_gf:<16} {af.avg_gf:<16}")
        lines.append(f"  {'场均失球':<12} {hf.avg_ga:<16} {af.avg_ga:<16}")
        lines.append(f"  {'净胜球差':<12} {hf.goal_diff:+.2f}{'/场':<12} {af.goal_diff:+.2f}{'/场'}")
        lines.append(f"  {'形态':<12} {hf.form_trend:<16} {af.form_trend:<16}")
        lines.append(f"  {'攻击力':<12} {hf.attack_potency:.0%}{'':<13} {af.attack_potency:.0%}")
        lines.append(f"  {'防守力':<12} {hf.defense_stability:.0%}{'':<13} {af.defense_stability:.0%}")

        # 最佳射手
        h_best = hf.top_scorer
        a_best = af.top_scorer
        if h_best:
            lines.append(f"  {'🔥射手王':<12} {h_best[0]}({h_best[1]}球){'':<6} ", end='')
            if a_best:
                lines[-1] += f"{a_best[0]}({a_best[1]}球)"
            lines.append('')

        lines.append(f"\n  📊 实力差: 净胜差{gap:+.2f}/场 | 攻击: {comp['attack_comparison']} | 防守: {comp['defense_comparison']}")
        lines.append(f"  ⚽ 得分手优势: {comp['scorer_advantage']}")

        return '\n'.join(lines)

# ── 全局单例 ──
_global_enricher: Optional[WC26DataEnricher] = None

def get_enricher() -> WC26DataEnricher:
    global _global_enricher
    if _global_enricher is None:
        _global_enricher = WC26DataEnricher()
        _global_enricher.load()
    return _global_enricher

# ── CLI 测试 ──
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    e = WC26DataEnricher()
    e.load()

    print("=" * 60)
    print("WC2026 Data Enricher — 数据验证")
    print("=" * 60)

    # 射手榜
    print("\n🔥 射手榜 Top 10:")
    for i, (name, goals) in enumerate(e.get_top_scorers(10), 1):
        print(f"  {i}. {name}: {goals}球")

    # 小组积分
    standings = e.get_group_standings()
    print(f"\n📊 12组积分榜 (共{len(standings)}组):")
    for grp, teams in sorted(standings.items()):
        print(f"  Group {grp}:")
        for t in teams:
            print(f"    {t['team_zh']}: {t['pts']}pts GF{t['gf']} GA{t['ga']} GD{t['gd']:+d}")

    # 挪威 vs 法国 战绩对比
    print("\n" + e.format_matchup_report('挪威', '法国'))

    # 塞内加尔 vs 伊拉克
    print("\n" + e.format_matchup_report('塞内加尔', '伊拉克'))

    # 法国战绩
    print("\n" + e.format_team_report('法国'))
