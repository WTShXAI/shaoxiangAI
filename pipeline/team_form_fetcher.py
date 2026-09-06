"""
Team Form Fetcher (Chain -1) — 球队近10场真实战绩分析层
====================================================
优先级: 高于赔率分析。不看战绩只看赔率 = 盲人摸象。

数据源: football-data.org API → get_team_form() → 真实20场战绩 + 泊松λ
用法:
    from pipeline.team_form_fetcher import TeamFormFetcher
    tff = TeamFormFetcher()
    result = tff.analyze_match(match_input)
    # result.verdict: '屠杀预警' / '实力碾压' / '均势' / '冷门风险'

核心规则 (从6/27复盘得出):
    Rule 1: goal_diff_per_game > 1.5 + 强队全攻击阵容 → 屠杀预警 (挪威1-4法国教训)
    Rule 2: goal_diff_per_game > 1.0 + 强队让球深 → 实力碾压确认 (塞内加尔5-0伊拉克)
    Rule 3: goal_diff_per_game < 0.3 → 均势, 赔率分析权重上升
    Rule 4: 弱队10场场均失球 > 2.0 → 防守崩盘预警 → 比分向上修正
"""

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── 路径：确保能import data_collector ──
_PROJECT_ROOT = Path(__file__).parent.parent

# ── 中→英球队名逆向映射 (用于调用API) ──
# 复用 football_data_live 中的 _TEAM_NAME_ZH
_ZH_TO_EN: Dict[str, str] = {}
_EN_TO_ZH: Dict[str, str] = {}

def _init_name_maps():
    """惰性初始化中英双向映射"""
    global _ZH_TO_EN, _EN_TO_ZH
    if _ZH_TO_EN:
        return
    try:
        from data_collector.football_data_live import _TEAM_NAME_ZH
        for en, zh in _TEAM_NAME_ZH.items():
            _EN_TO_ZH[en.lower()] = zh
            # 中→英: 只用第一个(最常见)映射
            if zh not in _ZH_TO_EN:
                _ZH_TO_EN[zh] = en
        logger.info(f"TeamFormFetcher: 加载 {len(_ZH_TO_EN)} 条中→英球队名映射")
    except ImportError:
        logger.warning("无法加载 football_data_live._TEAM_NAME_ZH, 将使用基础映射")
        # 基础48队兜底
        base = {
            '挪威': 'Norway', '法国': 'France', '塞内加尔': 'Senegal',
            '伊拉克': 'Iraq', '佛得角': 'Cape Verde', '沙特': 'Saudi Arabia',
            '佛得角共和国': 'Cape Verde', '沙特阿拉伯': 'Saudi Arabia',
            '乌拉圭': 'Uruguay', '西班牙': 'Spain', '埃及': 'Egypt',
            '伊朗': 'Iran', '新西兰': 'New Zealand', '比利时': 'Belgium',
            '阿根廷': 'Argentina', '巴西': 'Brazil', '英格兰': 'England',
            '葡萄牙': 'Portugal', '摩洛哥': 'Morocco', '荷兰': 'Netherlands',
            '德国': 'Germany', '克罗地亚': 'Croatia', '意大利': 'Italy',
            '哥伦比亚': 'Colombia', '墨西哥': 'Mexico', '美国': 'United States',
            '日本': 'Japan', '瑞士': 'Switzerland', '丹麦': 'Denmark',
            '土耳其': 'Turkey', '厄瓜多尔': 'Ecuador', '奥地利': 'Austria',
            '韩国': 'South Korea', '尼日利亚': 'Nigeria', '澳大利亚': 'Australia',
            '阿尔及利亚': 'Algeria', '加拿大': 'Canada', '乌克兰': 'Ukraine',
            '科特迪瓦': 'Ivory Coast', '巴拿马': 'Panama', '波兰': 'Poland',
            '威尔士': 'Wales', '瑞典': 'Sweden', '匈牙利': 'Hungary',
            '捷克': 'Czech Republic', '巴拉圭': 'Paraguay', '苏格兰': 'Scotland',
            '塞尔维亚': 'Serbia', '喀麦隆': 'Cameroon', '突尼斯': 'Tunisia',
            '民主刚果': 'DR Congo', '斯洛伐克': 'Slovakia', '希腊': 'Greece',
            '委内瑞拉': 'Venezuela', '乌兹别克斯坦': 'Uzbekistan',
            '卡塔尔': 'Qatar', '南非': 'South Africa', '约旦': 'Jordan',
            '波黑': 'Bosnia and Herzegovina', '加纳': 'Ghana',
            '库拉索': 'Curaçao', '海地': 'Haiti',
        }
        for zh, en in base.items():
            _ZH_TO_EN[zh] = en
            _EN_TO_ZH[en.lower()] = zh

_init_name_maps()

# 补充缺失的中文名映射 (football_data_live 中不包含的变体)
_ZH_TO_EN.setdefault('乌兹别克', 'Uzbekistan')
_ZH_TO_EN.setdefault('沙特', 'Saudi Arabia')
_ZH_TO_EN.setdefault('佛得角', 'Cape Verde')

def _zh_to_en(name: str) -> str:
    """中文队名→英文队名"""
    _init_name_maps()
    en = _ZH_TO_EN.get(name, name)
    return en

def _en_to_zh(name: str) -> str:
    """英文队名→中文队名"""
    _init_name_maps()
    return _EN_TO_ZH.get(name.lower(), name)

# ── 形态趋势评分 ──
def _score_momentum(form_trend: str) -> float:
    """
    近5场形态→动量分 (0~1)
    WWWWW=1.0, LLLLL=0.0, WLWLW=0.5
    W=1, D=0.5, L=0, 加权: 最近场权重更高
    """
    if not form_trend:
        return 0.5
    weights = [5, 4, 3, 2, 1]  # 最近场权重最高
    total_weight = 0
    total_score = 0
    for i, ch in enumerate(form_trend[-5:]):
        w = weights[min(i, 4)]
        total_weight += w
        if ch == 'W':
            total_score += w * 1.0
        elif ch == 'D':
            total_score += w * 0.5
        # L = 0
    return round(total_score / total_weight, 3) if total_weight > 0 else 0.5

# ── 防守崩盘检测 ──
def _check_defensive_collapse(recent_results: List[Dict]) -> Tuple[bool, float, int]:
    """
    检测防守崩盘: 近5场失球趋势
    返回: (是否崩盘, 场均失球, 连续失球2+场次)
    """
    if not recent_results:
        return False, 0, 0
    recent = recent_results[-5:]
    ga_values = []
    consecutive_2plus = 0
    for r in recent:
        score = r.get('score', '0-0')
        try:
            parts = score.split('-')
            if r.get('is_home', True):
                ga = int(parts[1])  # 主队失球=客队进球
            else:
                ga = int(parts[0])  # 客队失球=主队进球
        except (ValueError, IndexError):
            ga = 0
        ga_values.append(ga)
        if ga >= 2:
            consecutive_2plus += 1
        else:
            consecutive_2plus = 0

    avg_ga = sum(ga_values) / len(ga_values) if ga_values else 0
    is_collapse = avg_ga >= 2.0 and consecutive_2plus >= 2
    return is_collapse, round(avg_ga, 2), consecutive_2plus

@dataclass
class TeamFormSnapshot:
    """单支球队的战绩快照"""
    team: str                      # 中文名
    team_en: str                   # 英文名
    matches: int = 0               # 有效比赛数
    wins: int = 0
    draws: int = 0
    losses: int = 0
    win_rate: float = 0.0
    goals_for: int = 0
    goals_against: int = 0
    avg_gf: float = 0.0            # 场均进球
    avg_ga: float = 0.0            # 场均失球
    goal_diff: float = 0.0         # 场均净胜球
    home_avg_gf: float = 0.0
    home_avg_ga: float = 0.0
    away_avg_gf: float = 0.0
    away_avg_ga: float = 0.0
    form_trend: str = ""           # 近5场形态 (如 'WWDLW')
    momentum: float = 0.5          # 动量分 0~1
    defensive_collapse: bool = False
    recent_ga_avg: float = 0.0
    lambda_attack: float = 0.0     # 泊松综合进攻λ
    lambda_defense: float = 0.0    # 泊松综合防守λ
    predicted_top5: List[Dict] = field(default_factory=list)
    data_quality: str = "none"     # 'full'/'partial'/'none'

    @classmethod
    def from_api(cls, api_result: Dict) -> 'TeamFormSnapshot':
        """从 football_data_live.get_team_form() 返回构造"""
        if not api_result or api_result.get('matches', 0) == 0:
            return cls(
                team=api_result.get('team', '?'),
                team_en=api_result.get('team_en', '?'),
                data_quality='none'
            )

        r = api_result
        home = r.get('home', {})
        away = r.get('away', {})

        snapshot = cls(
            team=_en_to_zh(r.get('team', '?')),
            team_en=r.get('team', '?'),
            matches=r.get('matches', 0),
            wins=r.get('wins', 0),
            draws=r.get('draws', 0),
            losses=r.get('losses', 0),
            win_rate=r.get('win_rate', 0.0),
            goals_for=r.get('goals_for', 0),
            goals_against=r.get('goals_against', 0),
            avg_gf=r.get('avg_gf', 0.0),
            avg_ga=r.get('avg_ga', 0.0),
            goal_diff=round(r.get('avg_gf', 0) - r.get('avg_ga', 0), 2),
            home_avg_gf=home.get('avg_gf', 0.0),
            home_avg_ga=home.get('avg_ga', 0.0),
            away_avg_gf=away.get('avg_gf', 0.0),
            away_avg_ga=away.get('avg_ga', 0.0),
            form_trend=r.get('form_trend', ''),
            lambda_attack=r.get('lambda_overall_attack', 0.0),
            lambda_defense=r.get('lambda_overall_defense', 0.0),
            data_quality='full' if r.get('lambda_overall_attack', 0) > 0 else 'partial',
        )
        snapshot.momentum = _score_momentum(snapshot.form_trend)

        # 防守崩盘检测
        recent = r.get('recent_results', [])
        collapse, rec_ga, cons = _check_defensive_collapse(recent)
        snapshot.defensive_collapse = collapse
        snapshot.recent_ga_avg = rec_ga

        return snapshot

    @classmethod
    def from_wc2026_only(cls, team: str, wc_data: Dict) -> 'TeamFormSnapshot':
        """仅从WC2026比赛数据构造(API不可用时的降级)"""
        avg_gf = wc_data.get('avg_gf', 0)
        avg_ga = wc_data.get('avg_ga', 0)
        gp = wc_data.get('gp', 0)
        results = wc_data.get('results', [])
        # 从结果构造 form_trend
        trend = ''
        for r in results:
            gf = r.get('gf', 0)
            ga = r.get('ga', 0)
            if gf > ga:
                trend += 'W'
            elif gf == ga:
                trend += 'D'
            else:
                trend += 'L'

        return cls(
            team=team,
            team_en=_zh_to_en(team),
            matches=gp,
            goals_for=wc_data.get('gf', 0),
            goals_against=wc_data.get('ga', 0),
            avg_gf=avg_gf,
            avg_ga=avg_ga,
            goal_diff=round(avg_gf - avg_ga, 2),
            form_trend=trend[-5:],
            data_quality='partial',
        )

@dataclass
class TeamFormResult:
    """Chain -1 完整分析结果"""
    home: TeamFormSnapshot
    away: TeamFormSnapshot
    home_lambda_exp: float = 0.0      # 主队期望进球
    away_lambda_exp: float = 0.0      # 客队期望进球
    total_lambda: float = 0.0          # 总期望进球
    goal_diff_advantage: float = 0.0   # 场均净胜球差 (正=主优)
    strength_gap: str = "unknown"      # 'massacre'/'dominate'/'edge'/'even'/'upset_risk'
    verdict: str = ""                  # 人类可读结论
    massacre_warning: bool = False     # 屠杀预警
    score_adjustment: float = 0.0      # 比分修正值 (±进球数)
    confidence: float = 0.0            # 该层信心
    predicted_scores: List[Dict] = field(default_factory=list)
    data_quality: str = "none"         # 整体数据质量
    error: str = ""                    # 错误信息

    @property
    def is_valid(self) -> bool:
        return self.data_quality != 'none' and not self.error

class TeamFormFetcher:
    """Chain -1: 球队近10场真实战绩分析"""

    def __init__(self):
        self._fdl = None
        self._cache: Dict[str, TeamFormSnapshot] = {}
        self._initialized = False

    @property
    def fdl(self):
        """惰性加载 FootballDataLive"""
        if self._fdl is None:
            try:
                from data_collector.football_data_live import FootballDataLive
                self._fdl = FootballDataLive()
                logger.info("TeamFormFetcher: FootballDataLive 已加载")
            except ImportError as e:
                logger.warning(f"TeamFormFetcher: 无法加载 FootballDataLive: {e}")
                self._fdl = None
        return self._fdl

    @property
    def enricher(self):
        """惰性加载 WC26DataEnricher (worldcup26.ir)"""
        if not hasattr(self, '_enricher'):
            try:
                from data.worldcup26_enricher import WC26DataEnricher
                self._enricher = WC26DataEnricher()
                self._enricher.load()
                logger.info("TeamFormFetcher: WC26DataEnricher 已加载")
            except ImportError as e:
                logger.warning(f"TeamFormFetcher: 无法加载 WC26DataEnricher: {e}")
                self._enricher = None
        return self._enricher

    def _load_pre_tournament_data(self) -> dict:
        """加载赛前10场真实战绩数据库 (2026-06-27 建立)"""
        if hasattr(self, '_pre_tournament_db'):
            return self._pre_tournament_db
        try:
            from pathlib import Path
            db_path = Path(__file__).parent.parent / 'config' / 'pre_tournament_form.json'
            if db_path.exists():
                with open(db_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._pre_tournament_db = data.get('stats', {})
                logger.info(f"TeamFormFetcher: 加载预赛数据库 ({len(self._pre_tournament_db)}队)")
            else:
                self._pre_tournament_db = {}
        except Exception as e:
            logger.warning(f"加载预赛数据库失败: {e}")
            self._pre_tournament_db = {}
        return self._pre_tournament_db

    def get_team_form(self, team_name_zh: str) -> TeamFormSnapshot:
        """获取单支球队的近10场真实战绩（预赛10场DB > WC2026数据 > API历史）"""
        if team_name_zh in self._cache:
            return self._cache[team_name_zh]

        # ── 第0优先: 赛前10场真实战绩数据库 (最高优先级!) ──
        pre_tournament_db = self._load_pre_tournament_data()
        pre_data = pre_tournament_db.get(team_name_zh)

        # 第1优先: worldcup26.ir 全量WC2026赛果(含进球者, 3场) — 用于补充最近3场
        wc26_form = None
        if self.enricher:
            try:
                wc26_form = self.enricher.get_team_form(team_name_zh)
            except Exception as e:
                logger.debug(f"WC26Enricher {team_name_zh}: {e}")

        # 第2优先: DynamicTeamDB (WC2026赛果, 降级)
        wc_data = None
        if wc26_form is None:
            wc_data = self._try_wc2026_data(team_name_zh)

        # 第3优先: football-data.org API (完整历史, 含预赛)
        api_result = None
        if self.fdl and not pre_data:
            try:
                api_result = self._fetch_team_form_via_api(team_name_zh)
            except Exception as e:
                logger.warning(f"API获取 {team_name_zh} 战绩失败: {e}")

        # ── 构建 Snapshot: 预赛10场数据优先 (最全面) ──
        if pre_data and pre_data.get('matches', 0) >= 5:
            # ✅ 使用真实10场预赛数据
            snapshot = TeamFormSnapshot(
                team=team_name_zh,
                team_en=_zh_to_en(team_name_zh),
                matches=pre_data['matches'],
                wins=pre_data['wins'],
                draws=pre_data['draws'],
                losses=pre_data['losses'],
                win_rate=round(pre_data['wins'] / pre_data['matches'], 3),
                goals_for=pre_data['gf'],
                goals_against=pre_data['ga'],
                avg_gf=pre_data['avg_gf'],
                avg_ga=pre_data['avg_ga'],
                goal_diff=pre_data['goal_diff'],
                form_trend='',  # 从详细数据构建
                data_quality='full' if pre_data.get('quality') == 'full' else 'partial',
                lambda_attack=pre_data['avg_gf'],
                lambda_defense=pre_data['avg_ga'],
            )
            # 防守崩盘检测: 10场场均失球>2.0
            if pre_data['avg_ga'] >= 2.0:
                snapshot.defensive_collapse = True
                snapshot.recent_ga_avg = pre_data['avg_ga']
            snapshot.momentum = 0.5  # 默认中性
        elif wc26_form and wc26_form.matches >= 2:
            # 降级: WC2026真实数据 (仅2-3场)
            snapshot = TeamFormSnapshot(
                team=wc26_form.team_zh,
                team_en=wc26_form.team_en,
                matches=wc26_form.matches,
                wins=wc26_form.wins,
                draws=wc26_form.draws,
                losses=wc26_form.losses,
                win_rate=round(wc26_form.wins / wc26_form.matches, 3) if wc26_form.matches else 0,
                goals_for=wc26_form.goals_for,
                goals_against=wc26_form.goals_against,
                avg_gf=wc26_form.avg_gf,
                avg_ga=wc26_form.avg_ga,
                goal_diff=wc26_form.goal_diff,
                form_trend=wc26_form.form_trend,
                momentum=_score_momentum(wc26_form.form_trend),
                data_quality='partial',  # 仅2-3场, 降级标记
                lambda_attack=wc26_form.avg_gf,
                lambda_defense=wc26_form.avg_ga,
            )
            if wc26_form.avg_ga >= 2.5:
                snapshot.defensive_collapse = True
                snapshot.recent_ga_avg = wc26_form.avg_ga
            elif wc26_form.avg_ga >= 2.0 and wc26_form.clean_sheets == 0:
                snapshot.defensive_collapse = True
                snapshot.recent_ga_avg = wc26_form.avg_ga
        elif api_result and api_result.get('matches', 0) >= 3:
            snapshot = TeamFormSnapshot.from_api(api_result)
        elif wc_data and wc_data.get('gp', 0) >= 1:
            snapshot = TeamFormSnapshot.from_wc2026_only(team_name_zh, wc_data)
        else:
            snapshot = TeamFormSnapshot(
                team=team_name_zh,
                team_en=_zh_to_en(team_name_zh),
                data_quality='none'
            )

        self._cache[team_name_zh] = snapshot
        return snapshot

    def _try_wc2026_data(self, team_name_zh: str) -> Optional[Dict]:
        """从 DynamicTeamDB 获取WC2026赛果"""
        try:
            from data.dynamic_team_db_module import DynamicTeamDB
            db = DynamicTeamDB()
            db.load()
            team = db.get_team(team_name_zh)
            if team:
                return {
                    'gp': team.get('gp', 0),
                    'gf': team.get('gf', 0),
                    'ga': team.get('ga', 0),
                    'avg_gf': team.get('avg_gf', 0),
                    'avg_ga': team.get('avg_ga', 0),
                    'results': team.get('results', []),
                }
        except Exception as e:
            logger.debug(f"DynamicTeamDB查询 {team_name_zh} 失败: {e}")
        return None

    def _fetch_team_form_via_api(self, team_name_zh: str) -> Optional[Dict]:
        """通过 football-data.org API 获取球队战绩"""
        if not self.fdl:
            return None

        team_en = _zh_to_en(team_name_zh)

        # 尝试直接调用 get_team_form (需要 team_id)
        # 通过 get_match_form_analysis 间接获取两支球队的数据
        # 方案: 用该队自身作为对手(会被API正确识别为两个不同队)
        # 更可靠方案: 构建两个不同队名
        try:
            # 找一个对手队来触发API
            dummy_opponent = 'France' if team_en != 'France' else 'Brazil'
            analysis = self.fdl.get_match_form_analysis(team_en, dummy_opponent)

            if analysis and 'home' in analysis and 'matches' in analysis['home']:
                home_data = analysis['home']
                # 确保我们拿到的是目标球队的数据
                if home_data.get('team', '').lower() == team_en.lower():
                    home_data['team_en'] = team_en  # 补充英文字段
                    return home_data
                # 如果主队名不匹配，可能在away位置
                if 'away' in analysis and analysis['away'].get('team', '').lower() == team_en.lower():
                    away_data = analysis['away']
                    away_data['team_en'] = team_en
                    return away_data
        except Exception as e:
            logger.warning(f"get_match_form_analysis 失败 ({team_en}): {e}")

        # 降级: 尝试直接用 team_id
        try:
            team_id = self.fdl._find_team_id(team_en)
            if team_id:
                return self.fdl.get_team_form(team_id, limit=20)
        except Exception as e:
            logger.warning(f"get_team_form 失败 (team_id): {e}")

        return None

    def analyze_match(self, home_zh: str, away_zh: str, 
                      home_full_strength: bool = True,
                      away_full_strength: bool = True,
                      home_attack_formation: bool = False,
                      away_attack_formation: bool = False) -> TeamFormResult:
        """
        分析比赛双方的真实实力差

        Args:
            home_zh: 主队中文名
            away_zh: 客队中文名  
            home_full_strength: 主队是否全主力
            away_full_strength: 客队是否全主力
            home_attack_formation: 主队是否攻击阵型 (4-1-2-3, 4-3-3等)
            away_attack_formation: 客队是否攻击阵型

        Returns:
            TeamFormResult with verdict and score_adjustment
        """
        # 获取双方战绩
        home_form = self.get_team_form(home_zh)
        away_form = self.get_team_form(away_zh)

        # 计算λ期望进球
        h_la = home_form.lambda_attack or home_form.avg_gf
        h_ld = home_form.lambda_defense or home_form.avg_ga
        a_la = away_form.lambda_attack or away_form.avg_gf
        a_ld = away_form.lambda_defense or away_form.avg_ga

        home_lambda_exp = round((h_la + a_ld) / 2, 2) if (h_la + a_ld) > 0 else 0
        away_lambda_exp = round((a_la + h_ld) / 2, 2) if (a_la + h_ld) > 0 else 0
        total_lambda = round(home_lambda_exp + away_lambda_exp, 2)

        # 场均净胜球差
        goal_diff_advantage = round(home_form.goal_diff - away_form.goal_diff, 2)

        # ── 核心判断逻辑 ──
        massacre_warning = False
        score_adjustment = 0.0
        strength_gap = "even"
        verdict = ""
        confidence = 0.0

        # 双方进球/失球差
        h_gf_ga_diff = home_form.avg_gf - home_form.avg_ga
        a_gf_ga_diff = away_form.avg_gf - away_form.avg_ga

        # 确定强队和弱队
        if goal_diff_advantage > 0:
            strong_team = home_form
            weak_team = away_form
            strong_full = home_full_strength
            strong_attack = home_attack_formation
        else:
            strong_team = away_form
            weak_team = home_form
            strong_full = away_full_strength
            strong_attack = away_attack_formation

        abs_gap = abs(goal_diff_advantage)

        # ── Rule 0: 阵容不对称检测 (优先于纯数据, "挪威1-4法国"教训) ──
        # 即使数据不足/GoalDiff小, 只要: 强队全攻击阵+全主力 vs 弱队非全主力/攻击阵 → 警报
        if goal_diff_advantage > 0:
            weak_full = away_full_strength
            weak_attack = away_attack_formation
        else:
            weak_full = home_full_strength
            weak_attack = home_attack_formation

        formation_mismatch = strong_attack and strong_full and (not weak_full or weak_attack)

        # Rule 1: 屠杀预警 (数据层)
        # 场均净胜差>1.5 + 强队全主力/攻击阵 + 弱队防守崩盘/场均失球>2
        # 或: 阵容不对称 (强队全攻+主力 vs 弱队有缺失) → 降阈到0.8
        massacre_threshold = 0.8 if formation_mismatch else 1.5

        if abs_gap >= massacre_threshold:
            massacre_triggers = 0
            if strong_full:
                massacre_triggers += 1
            if strong_attack:
                massacre_triggers += 1
            if weak_team.defensive_collapse or weak_team.avg_ga >= 2.0:
                massacre_triggers += 1
            if abs_gap >= 2.0:
                massacre_triggers += 1
            if formation_mismatch:
                massacre_triggers += 1  # 阵容不对称本身就是触发条件

            if massacre_triggers >= 2:
                massacre_warning = True
                strength_gap = "massacre"
                if formation_mismatch and abs_gap < 1.5:
                    # 阵容屠杀模式: 修正更大 (挪威1-4法国: 数据差仅1.0但实际4球)
                    score_adjustment = round(max(2.0, abs(goal_diff_advantage) + 1.5), 1)
                else:
                    score_adjustment = round(max(abs_gap, 1.0) * 0.8, 1)
                if formation_mismatch and abs_gap < 1.5:
                    verdict = (f"🚨 阵容屠杀预警: {strong_team.team}全攻击阵+全主力, "
                              f"{weak_team.team}非全主力, 数据差仅{abs_gap}但阵容差距巨大 "
                              f"(挪威1-4法国模式)")
                    confidence = 0.75
                else:
                    verdict = (f"🚨 屠杀预警: {strong_team.team}场均净胜{strong_team.goal_diff}, "
                              f"{weak_team.team}场均失{weak_team.avg_ga}球, 差距{abs_gap}球/场")
                    confidence = min(0.85, 0.5 + abs_gap * 0.15)
            else:
                strength_gap = "dominate"
                score_adjustment = round(abs_gap * 0.5, 1)
                verdict = f"实力碾压: {strong_team.team}状态显著优于{weak_team.team} (净胜差{abs_gap})"
                confidence = 0.55 + abs_gap * 0.1

        elif abs_gap >= 0.8:
            strength_gap = "edge"
            score_adjustment = round(abs_gap * 0.3, 1)
            if goal_diff_advantage > 0:
                verdict = f"主队优势: {home_form.team}状态略优 (净胜差{abs_gap})"
            else:
                verdict = f"客队优势: {away_form.team}状态略优 (净胜差{abs_gap})"
            confidence = 0.4 + abs_gap * 0.1

        else:
            strength_gap = "even"
            verdict = "双方实力接近，战绩无明显差距"
            confidence = 0.3

        # 防守崩盘特判 (独立于 massacre, 任何 gap 都可能触发)
        if weak_team.defensive_collapse:
            if not massacre_warning:
                score_adjustment += 0.3
                verdict += f" | {weak_team.team}防线崩盘(近5场场均失{weak_team.recent_ga_avg})"

        # 数据质量判断
        if home_form.data_quality == 'full' and away_form.data_quality == 'full':
            data_quality = 'full'
            confidence = min(0.90, confidence + 0.05)
        elif home_form.data_quality != 'none' or away_form.data_quality != 'none':
            data_quality = 'partial'
        else:
            data_quality = 'none'
            confidence = 0.0
            verdict = "⚠️ 战绩数据缺失，请手动补充"

        # 尝试获取泊松比分预测
        predicted_scores = []
        if self.fdl and data_quality == 'full':
            try:
                h_en = _zh_to_en(home_zh)
                a_en = _zh_to_en(away_zh)
                analysis = self.fdl.get_match_form_analysis(h_en, a_en)
                if analysis and 'predicted_scores' in analysis:
                    predicted_scores = analysis['predicted_scores']
                    home_lambda_exp = analysis.get('lambda_home_expected', home_lambda_exp)
                    away_lambda_exp = analysis.get('lambda_away_expected', away_lambda_exp)
                    total_lambda = analysis.get('lambda_total', total_lambda)
            except Exception as e:
                logger.debug(f"泊松预测失败: {e}")

        return TeamFormResult(
            home=home_form,
            away=away_form,
            home_lambda_exp=home_lambda_exp,
            away_lambda_exp=away_lambda_exp,
            total_lambda=total_lambda,
            goal_diff_advantage=goal_diff_advantage,
            strength_gap=strength_gap,
            verdict=verdict,
            massacre_warning=massacre_warning,
            score_adjustment=score_adjustment,
            confidence=confidence,
            predicted_scores=predicted_scores,
            data_quality=data_quality,
        )

    def analyze_match_input(self, match) -> TeamFormResult:
        """从 MatchInput 对象分析 (兼容全链路管道)"""
        # 检测攻击阵型 (含常见进攻布局)
        ATTACK_FORMATIONS = (
            '4-1-2-3', '4-3-3', '3-4-3', '4-2-4',
            '4-2-3-1', '3-5-2', '4-4-2-diamond', '3-4-2-1',
            '4-1-3-2', '3-3-4',
        )
        DEFENSIVE_FORMATIONS = (
            '5-4-1', '5-3-2', '4-5-1', '5-2-3', '3-5-1-1',
            '4-4-1-1',
        )
        home_attack = getattr(match, 'home_formation', '') in ATTACK_FORMATIONS
        away_attack = getattr(match, 'away_formation', '') in ATTACK_FORMATIONS

        return self.analyze_match(
            home_zh=match.home,
            away_zh=match.away,
            home_full_strength=getattr(match, 'home_full_strength', True),
            away_full_strength=getattr(match, 'away_full_strength', True),
            home_attack_formation=home_attack,
            away_attack_formation=away_attack,
        )

    def format_report(self, result: TeamFormResult) -> str:
        """生成人类可读的Chain -1分析报告"""
        if result.error:
            return f"⚠️ Chain -1 失败: {result.error}"

        lines = []
        lines.append(f"📊 [Chain -1] 球队近10场真实战绩分析")
        lines.append(f"{'─'*55}")

        def _format_team(snap: TeamFormSnapshot) -> List[str]:
            t = []
            t.append(f"  【{snap.team}】")
            if snap.data_quality == 'none':
                t.append(f"    ⚠️ 无战绩数据")
                return t
            t.append(f"    近{snap.matches}场: 胜{snap.wins} 平{snap.draws} 负{snap.losses} 胜率{snap.win_rate:.0%}")
            t.append(f"    进{snap.goals_for}球 失{snap.goals_against}球 | 场均: {snap.avg_gf}进/{snap.avg_ga}失 净{snap.goal_diff:+.2f}")
            if snap.form_trend:
                t.append(f"    近5场趋势: {snap.form_trend} 动量{snap.momentum:.2f}")
            if snap.defensive_collapse:
                t.append(f"    🚨 防守崩盘! 近5场场均失{snap.recent_ga_avg}球")
            if snap.lambda_attack:
                t.append(f"    λ: 攻{snap.lambda_attack} 防{snap.lambda_defense}")
            return t

        lines.extend(_format_team(result.home))
        lines.append("")
        lines.extend(_format_team(result.away))
        lines.append("")

        # 核心结论
        lines.append(f"{'─'*55}")
        lines.append(f"🎯 净胜球差: {result.goal_diff_advantage:+.2f}/场")
        lines.append(f"⚡ 期望进球: 主{result.home_lambda_exp} vs 客{result.away_lambda_exp} (总{result.total_lambda})")
        lines.append(f"💪 实力差距: {result.strength_gap}")

        if result.massacre_warning:
            lines.append(f"🚨 屠杀预警: 是 (比分修正{result.score_adjustment:+.1f}球)")

        lines.append(f"📋 判决: {result.verdict}")
        lines.append(f"📊 数据质量: {result.data_quality} | 信心: {result.confidence:.0%}")

        if result.predicted_scores:
            lines.append(f"")
            lines.append(f"📋 泊松比分 Top 3:")
            for s in result.predicted_scores[:3]:
                lines.append(f"  {s['score']}: {s['prob']:.1%} ({s['outcome']})")

        return '\n'.join(lines)

# ── 单例 ──
_global_fetcher: Optional[TeamFormFetcher] = None

def get_team_form_fetcher() -> TeamFormFetcher:
    global _global_fetcher
    if _global_fetcher is None:
        _global_fetcher = TeamFormFetcher()
    return _global_fetcher

# ── CLI 测试 ──
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    tff = TeamFormFetcher()

    print("=" * 60)
    print("Chain -1 球队战绩分析 - 6/27 测试")
    print("=" * 60)

    # 测试1: 挪威 vs 法国
    print("\n【测试1】挪威 vs 法国")
    r1 = tff.analyze_match(
        '挪威', '法国',
        home_full_strength=False,   # 哈兰德/厄德高替补
        home_attack_formation=True,  # 4-1-2-3 自杀阵
        away_full_strength=True,     # 全主力三叉戟
        away_attack_formation=True,  # 4-2-3-1 攻击
    )
    print(tff.format_report(r1))

    # 测试2: 塞内加尔 vs 伊拉克
    print("\n\n【测试2】塞内加尔 vs 伊拉克")
    r2 = tff.analyze_match('塞内加尔', '伊拉克')
    print(tff.format_report(r2))
