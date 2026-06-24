"""
贝叶斯意图识别总指挥 (Bayesian Intent Commander)
================================================
用途: 操盘手日常对话比赛分析模块的"总指挥"
角色: 监听操盘手输入 → 识别意图 → 路由到对应处理子模块

意图分类体系 (8类, 操盘手视角):
  PREDICT          - 预测请求 ("这场怎么看""谁赢")
  ODDS_ANALYSIS    - 赔率分析 ("这个水位""抽水多少")
  BOOKMAKER_INTENT - 庄家意图 ("庄家想干嘛""诱盘""收割")
  RISK_ASSESS      - 风险评估 ("能买吗""值不值""仓位")
  COMPARE          - 对比分析 ("和上场比""两队差异")
  REVIEW           - 深度复盘 ("为什么错""复盘""漏了什么")
  DATA_QUERY       - 数据查询 ("最近战绩""历史交锋""阵容")
  STRATEGY         - 策略讨论 ("怎么打""战术""让球策略")

架构:
  用户输入
    ↓
  [预处理: 分词 + 关键词提取]
    ↓
  [朴素贝叶斯意图分类器] ← 训练样本库
    ↓
  [置信度门控] → 置信度<0.5 时反问澄清
    ↓
  [路由表] → 分发到对应子模块
    ↓
  子模块执行 → 返回结果
"""
import os, re, json, math, logging
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger('IntentCommander')


# ════════════════════════════════════════════════════════════
# 1. 意图分类体系定义
# ════════════════════════════════════════════════════════════

@dataclass
class IntentDef:
    """意图定义"""
    code: str
    name: str
    description: str
    keywords: list  # 高权重关键词
    route_to: str   # 路由目标模块
    sample_questions: list  # 训练样本

INTENT_REGISTRY = [
    IntentDef(
        code='PREDICT',
        name='预测请求',
        description='操盘手要求给出比赛结果预测',
        keywords=['预测', '谁赢', '怎么看', '结果', '胜平负', '推荐', '买什么', '下注', '波胆', '比分预测'],
        route_to='prediction_engine',
        sample_questions=[
            '这场怎么看',
            '巴西对摩洛哥谁赢',
            '给我个预测',
            '这场胜平负怎么走',
            '推荐一下这场',
            '波胆预测多少',
            '这场比赛结果会怎样',
            '你觉得谁会赢',
            '帮我分析下这场的结果',
            '这场该怎么买',
        ]
    ),
    IntentDef(
        code='ODDS_ANALYSIS',
        name='赔率分析',
        description='深入分析赔率结构、水位、抽水',
        keywords=['赔率', '水位', '抽水', '凯利', '让球盘', '大小球', '盘口', '返赔率', '隐含概率', 'spread', 'overround'],
        route_to='odds_analyzer',
        sample_questions=[
            '这个赔率什么意思',
            '水位怎么看的',
            '抽水多少',
            '凯利指数多少',
            '让球盘怎么看',
            '大小球水位分析下',
            '盘口变了说明什么',
            '返赔率多少',
            '隐含概率是多少',
            '这个spread正常吗',
            '赔率漂移了多少',
            '盘口交互特征怎么样',
        ]
    ),
    IntentDef(
        code='BOOKMAKER_INTENT',
        name='庄家意图',
        description='逆向分析庄家操盘意图、诱盘、收割',
        keywords=['庄家', '诱盘', '收割', '陷阱', '操盘', '意图', '风控', '锁定期', '信号', '逆向', '加密协议'],
        route_to='bookmaker_sim',
        sample_questions=[
            '庄家想干嘛',
            '这是诱盘吗',
            '庄家在收割吗',
            '有陷阱吗',
            '庄家操盘意图是什么',
            '风控信号有哪些',
            '锁定期信号出来了吗',
            '逆向分析下庄家',
            '赔率是加密协议怎么解',
            '庄家自信吗',
            '这个赔率是诱饵吗',
        ]
    ),
    IntentDef(
        code='RISK_ASSESS',
        name='风险评估',
        description='评估投注风险、价值、仓位',
        keywords=['能买吗', '值不值', '风险', '仓位', '价值', '值得', '稳吗', '敢不敢', '把握', '置信度'],
        route_to='risk_guard',
        sample_questions=[
            '这场能买吗',
            '值不值得下注',
            '风险大不大',
            '仓位该多少',
            '有价值吗',
            '这场稳吗',
            '敢不敢重仓',
            '把握多大',
            '置信度多少',
            '这场风险高吗',
            '能下重注吗',
        ]
    ),
    IntentDef(
        code='COMPARE',
        name='对比分析',
        description='对比两队、两场、两个赔率',
        keywords=['对比', '比较', '差异', '和...比', '哪个强', '区别', '相比', 'VS', '相对'],
        route_to='comparator',
        sample_questions=[
            '和上场对比下',
            '两队差异在哪',
            '和昨天那场比',
            '哪个队强',
            '区别是什么',
            '相比上轮怎样',
            '巴西vs阿根廷对比',
            '两场有什么不同',
            '赔率对比下',
            '主客队相比如何',
        ]
    ),
    IntentDef(
        code='REVIEW',
        name='深度复盘',
        description='复盘历史预测错误、分析原因',
        keywords=['复盘', '为什么错', '漏了什么', '错在哪', '原因', '总结', '教训', '上次', '回测'],
        route_to='review_engine',
        sample_questions=[
            '为什么预测错了',
            '复盘下这场',
            '漏了什么信号',
            '错在哪里',
            '失败原因是什么',
            '总结下教训',
            '上次类似情况',
            '回测结果怎样',
            '这个误报怎么来的',
            'D误报怎么减少',
        ]
    ),
    IntentDef(
        code='DATA_QUERY',
        name='数据查询',
        description='查询球队历史、交锋、阵容、战绩',
        keywords=['历史', '交锋', '战绩', '阵容', '最近', '数据', '统计', '排名', '伤病', '教练'],
        route_to='data_collector',
        sample_questions=[
            '最近战绩怎样',
            '历史交锋记录',
            '阵容是什么',
            '两队历史数据',
            '统计下',
            '排名多少',
            '有伤病吗',
            '教练是谁',
            '主客场战绩',
            '进球数统计',
            '最近五场怎样',
        ]
    ),
    IntentDef(
        code='STRATEGY',
        name='策略讨论',
        description='讨论战术、让球策略、投注策略',
        keywords=['战术', '怎么打', '策略', '让球', '走地', '滚球', '半场', '角球', '黄牌', '阵型'],
        route_to='strategy_advisor',
        sample_questions=[
            '战术怎么安排',
            '这场怎么打',
            '让球策略',
            '走地怎么玩',
            '滚球策略',
            '半场该怎么看',
            '角球数会多吗',
            '黄牌预测',
            '阵型是什么',
            '战术会变吗',
            '让球该怎么买',
        ]
    ),
]


# ════════════════════════════════════════════════════════════
# 2. 朴素贝叶斯意图分类器
# ════════════════════════════════════════════════════════════

class NaiveBayesIntentClassifier:
    """
    朴素贝叶斯意图分类器
    特征: 字符级 n-gram (1-2 gram) + 高权重关键词二值特征
    平滑: Laplace (add-k) 平滑
    """

    def __init__(self, alpha=0.1, keyword_boost=3.0):
        self.alpha = alpha
        self.keyword_boost = keyword_boost
        self.intents = []  # [IntentDef]
        self.intent_codes = []
        # 模型参数
        self.class_prior = {}          # {intent_code: log P(class)}
        self.feature_likelihood = {}   # {intent_code: {feature: log P(feature|class)}}
        self.feature_set = set()
        self.vocab_size = 0
        self.keyword_features = set()  # 高权重关键词集合

    def _tokenize(self, text):
        """中文分词: 滑动窗口 n-gram (1-2字)"""
        text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', text.lower())
        tokens = []
        # 单字
        for ch in text:
            tokens.append(ch)
        # 双字 bigram
        for i in range(len(text) - 1):
            tokens.append(text[i:i+2])
        # 英文单词
        words = re.findall(r'[a-zA-Z]+', text)
        tokens.extend([w for w in words if len(w) >= 2])
        return tokens

    def _extract_features(self, text, intent_def=None):
        """提取特征: n-gram + 关键词二值"""
        features = Counter()
        tokens = self._tokenize(text)
        for t in tokens:
            features[t] += 1

        # 关键词二值特征 (boost)
        if intent_def:
            for kw in intent_def.keywords:
                if kw in text:
                    features[f'KW:{kw}'] = self.keyword_boost

        # 全局关键词扫描 (不绑定 intent_def, 用于预测时)
        for intent in self.intents:
            for kw in intent.keywords:
                if kw in text:
                    features[f'KW:{kw}'] = self.keyword_boost

        return features

    def fit(self, intent_defs):
        """训练"""
        self.intents = intent_defs
        self.intent_codes = [d.code for d in intent_defs]

        # 收集所有关键词特征
        for d in intent_defs:
            for kw in d.keywords:
                self.keyword_features.add(f'KW:{kw}')

        # 统计
        class_doc_count = defaultdict(int)     # {code: 样本数}
        class_feature_count = defaultdict(lambda: defaultdict(int))  # {code: {feature: count}}
        total_docs = 0

        for d in intent_defs:
            for q in d.sample_questions:
                feats = self._extract_features(q, d)
                class_doc_count[d.code] += 1
                total_docs += 1
                for f, c in feats.items():
                    class_feature_count[d.code][f] += c
                    self.feature_set.add(f)

        self.vocab_size = len(self.feature_set)
        if self.vocab_size == 0:
            self.vocab_size = 1

        # 计算对数先验和似然
        for code in self.intent_codes:
            # 先验 (均匀 + 经验混合)
            empirical = class_doc_count[code] / total_docs
            prior = 0.5 * empirical + 0.5 * (1.0 / len(self.intent_codes))
            self.class_prior[code] = math.log(prior)

            # 似然 (Laplace 平滑)
            total_feat_count = sum(class_feature_count[code].values())
            denom = total_feat_count + self.alpha * self.vocab_size
            self.feature_likelihood[code] = {}
            for f in self.feature_set:
                count = class_feature_count[code].get(f, 0)
                self.feature_likelihood[code][f] = math.log((count + self.alpha) / denom)

        logger.info(f"意图分类器训练完成 | {len(self.intent_codes)}类 | 词表{self.vocab_size} | 样本{total_docs}")
        return self

    def predict_proba(self, text):
        """
        返回 {intent_code: probability}
        """
        feats = self._extract_features(text)
        log_probs = {}
        for code in self.intent_codes:
            lp = self.class_prior[code]
            for f, c in feats.items():
                if f in self.feature_likelihood[code]:
                    lp += c * self.feature_likelihood[code][f]
                else:
                    # 未见特征用平滑值
                    lp += c * math.log(self.alpha / (self.alpha * self.vocab_size))
            log_probs[code] = lp

        # softmax 归一化
        max_lp = max(log_probs.values())
        exp_vals = {k: math.exp(v - max_lp) for k, v in log_probs.items()}
        total = sum(exp_vals.values())
        return {k: v / total for k, v in exp_vals.items()}

    def predict(self, text, confidence_threshold=0.5):
        """
        返回 (intent_code, confidence, top3)
        置信度 < threshold 时返回 None (需澄清)
        """
        probs = self.predict_proba(text)
        sorted_probs = sorted(probs.items(), key=lambda x: -x[1])
        top_code, top_prob = sorted_probs[0]
        top3 = sorted_probs[:3]

        if top_prob < confidence_threshold:
            return None, top_prob, top3

        return top_code, top_prob, top3


# ════════════════════════════════════════════════════════════
# 3. 路由表 & 总指挥
# ════════════════════════════════════════════════════════════

@dataclass
class RouteResult:
    """路由结果"""
    user_input: str
    intent_code: Optional[str]
    intent_name: str
    confidence: float
    top3: list  # [(code, prob), ...]
    route_to: str
    action: str  # 'execute' | 'clarify'
    clarify_options: list = field(default_factory=list)


class BayesianCommander:
    """贝叶斯总指挥 — 意图识别 + 路由"""

    def __init__(self, confidence_threshold=0.5):
        self.classifier = NaiveBayesIntentClassifier(alpha=0.1, keyword_boost=3.0)
        self.classifier.fit(INTENT_REGISTRY)
        self.confidence_threshold = confidence_threshold
        self.route_map = {d.code: d.route_to for d in INTENT_REGISTRY}
        self.intent_map = {d.code: d for d in INTENT_REGISTRY}

    def route(self, user_input):
        """识别意图并路由"""
        intent_code, confidence, top3 = self.classifier.predict(
            user_input, self.confidence_threshold)

        if intent_code is None:
            # 置信度不足, 需澄清
            clarify_options = [(code, self.intent_map[code].name, prob)
                             for code, prob in top3]
            return RouteResult(
                user_input=user_input,
                intent_code=None,
                intent_name='(不确定)',
                confidence=confidence,
                top3=top3,
                route_to='(待澄清)',
                action='clarify',
                clarify_options=clarify_options,
            )

        intent_def = self.intent_map[intent_code]
        return RouteResult(
            user_input=user_input,
            intent_code=intent_code,
            intent_name=intent_def.name,
            confidence=confidence,
            top3=top3,
            route_to=intent_def.route_to,
            action='execute',
        )

    def format_route(self, result):
        """格式化路由结果用于展示"""
        lines = []
        lines.append(f"🗣️ 操盘手: \"{result.user_input}\"")
        lines.append(f"")
        if result.action == 'clarify':
            lines.append(f"⚠️ 总指挥: 意图不太明确 (置信度{result.confidence:.1%})，请确认：")
            for code, name, prob in result.clarify_options:
                lines.append(f"   • {name} ({code}) — {prob:.1%}")
        else:
            lines.append(f"✅ 总指挥: 意图识别 = 【{result.intent_name}】 (置信度{result.confidence:.1%})")
            lines.append(f"   → 路由到: {result.route_to}")
            lines.append(f"   Top3: " + " | ".join([f"{c}={p:.1%}" for c, p in result.top3]))
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# 4. 演示 & 测试
# ════════════════════════════════════════════════════════════

DEMO_CONVERSATIONS = [
    # 预测请求
    "巴西对摩洛哥这场怎么看",
    "给我个胜平负推荐",
    "这场波胆预测多少",
    # 赔率分析
    "这个赔率的水位什么意思",
    "抽水多少正常",
    "凯利指数分析下",
    # 庄家意图
    "庄家这个盘口想干嘛",
    "这是诱盘吗",
    "赔率是加密协议怎么逆向",
    # 风险评估
    "这场能买吗",
    "值不值得下注",
    "仓位该多少",
    # 对比分析
    "和上场对比下两队差异",
    "巴西vs阿根廷哪个强",
    # 深度复盘
    "为什么这场预测错了",
    "复盘下漏了什么信号",
    # 数据查询
    "最近五场战绩怎样",
    "历史交锋记录",
    "有伤病吗",
    # 策略讨论
    "让球该怎么买",
    "走地策略怎么安排",
    # 模糊/混合
    "这场",  # 太短, 应触发澄清
    "分析下",  # 模糊
]


def run_demo():
    """运行演示"""
    print("=" * 70)
    print("贝叶斯意图识别总指挥 — 操盘手对话路由演示")
    print("=" * 70)

    commander = BayesianCommander(confidence_threshold=0.85)

    # 打印意图注册表
    print(f"\n已注册意图 ({len(INTENT_REGISTRY)} 类):")
    print(f"{'代码':<20} {'名称':<12} {'路由目标':<22} {'样本数':>6}")
    print("-" * 65)
    for d in INTENT_REGISTRY:
        print(f"{d.code:<20} {d.name:<12} {d.route_to:<22} {len(d.sample_questions):>6}")

    print(f"\n词表大小: {commander.classifier.vocab_size}")
    print(f"置信度门控阈值: {commander.confidence_threshold}")

    # 演示对话
    print("\n" + "=" * 70)
    print("模拟操盘手对话路由")
    print("=" * 70)

    correct = 0
    total = 0
    for text in DEMO_CONVERSATIONS:
        result = commander.route(text)
        print()
        print(commander.format_route(result))
        total += 1
        if result.action == 'execute':
            correct += 1

    print(f"\n{'='*70}")
    print(f"路由统计: {correct}/{total} 直接执行, {total-correct} 触发澄清")
    print(f"{'='*70}")


def interactive_test():
    """交互式测试"""
    print("=" * 70)
    print("贝叶斯意图识别总指挥 — 交互模式 (输入 quit 退出)")
    print("=" * 70)
    commander = BayesianCommander(confidence_threshold=0.5)
    while True:
        try:
            text = input("\n🗣️ 操盘手> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            break
        if text.lower() in ('quit', 'exit', 'q', '退出'):
            break
        if not text:
            continue
        result = commander.route(text)
        print()
        print(commander.format_route(result))


# ════════════════════════════════════════════════════════════
# 5. 经验型路由器 (Experience-Aware Commander)
#    学习4类历史经验: ①历史比赛 ②回测记录+结果 ③盘口画线轨迹 ④操盘手法/陷阱
# ════════════════════════════════════════════════════════════

import re as _re
from dataclasses import dataclass as _dataclass, field as _field
from typing import Optional as _Optional, List as _List, Dict as _Dict, Any as _Any

try:
    from experience_memory import ExperienceMemory, ExperienceFeatures, MatchRecord
    _EXP_AVAILABLE = True
except ImportError as _e:
    logger.warning(f"experience_memory 未加载, 经验型路由器不可用: {_e}")
    _EXP_AVAILABLE = False


@_dataclass
class ExperienceRouteResult(RouteResult):
    """经验型路由结果 (扩展 RouteResult)"""
    experience_active: bool = False
    experience_features: _Any = None  # ExperienceFeatures
    similar_matches: _List[_Dict] = _field(default_factory=list)  # 历史相似比赛引用
    scenario_stats: _Dict = _field(default_factory=dict)  # 场景统计
    route_adjusted: bool = False  # 是否因经验调整了路由
    route_reason: str = ""  # 调整理由


class ExperienceContextParser:
    """从用户输入中解析比赛上下文 (队名/赔率)"""

    # 常见球队名 (世界杯+主流联赛)
    KNOWN_TEAMS = {
        # 世界杯球队
        '墨西哥', '韩国', '巴西', '阿根廷', '德国', '法国', '西班牙', '英格兰', '葡萄牙',
        '荷兰', '比利时', '意大利', '乌拉圭', '克罗地亚', '瑞士', '丹麦', '瑞典', '挪威',
        '美国', '加拿大', '日本', '伊朗', '沙特', '澳大利亚', '新西兰', '土耳其', '波兰',
        '塞尔维亚', '摩洛哥', '突尼斯', '塞内加尔', '加纳', '喀麦隆', '埃及', '阿尔及利亚',
        '尼日利亚', '南非', '刚果', '巴拿马', '哥斯达黎加', '厄瓜多尔', '哥伦比亚', '智利',
        '秘鲁', '巴拉圭', '玻利维亚', '海地', '库拉索', '佛得角', '波黑', '苏格兰', '威尔士',
        # 俱乐部
        '皇马', '巴萨', '马竞', '拜仁', '多特', '巴黎', '尤文', '国米', '米兰', '那不勒斯',
        '罗马', '拉齐奥', '佛罗伦萨', '本菲卡', '波尔图', '里斯本竞技', '阿森纳', '切尔西',
        '热刺', '利物浦', '曼联', '曼城',
    }

    # 赔率模式: "H@2.03 D@3.25 A@3.95" 或 "2.03/3.25/3.95" 或 "主胜2.03平3.25客胜3.95"
    ODDS_PATTERNS = [
        r'H@([\d.]+)\s*D@([\d.]+)\s*A@([\d.]+)',
        r'([\d.]+)/([\d.]+)/([\d.]+)',
        r'主[胜赔@]*\s*([\d.]+).*平[赔@]*\s*([\d.]+).*客[胜赔@]*\s*([\d.]+)',
        r'让球?\s*([\d.-]+).*大小?\s*([\d.]+)',
    ]

    @classmethod
    def parse(cls, text: str) -> _Dict:
        """从文本解析比赛上下文

        返回:
            {
                'teams': [home, away],  # 可能部分为空
                'odds': {'home': x, 'draw': y, 'away': z},  # 可能缺失
                'has_context': bool
            }
        """
        result = {'teams': [], 'odds': {}, 'has_context': False}

        # 解析球队名
        found_teams = []
        for team in cls.KNOWN_TEAMS:
            if team in text:
                found_teams.append(team)
        if found_teams:
            # 去重 + 保留出现顺序
            seen = set()
            ordered = []
            for t in found_teams:
                if t not in seen:
                    seen.add(t)
                    ordered.append(t)
            result['teams'] = ordered[:2]  # 最多取2个
            result['has_context'] = True

        # 解析赔率
        for pattern in cls.ODDS_PATTERNS:
            m = _re.search(pattern, text)
            if m:
                groups = m.groups()
                if len(groups) >= 3:
                    try:
                        result['odds'] = {
                            'home': float(groups[0]),
                            'draw': float(groups[1]),
                            'away': float(groups[2]),
                        }
                        result['has_context'] = True
                    except (ValueError, IndexError):
                        pass
                break

        return result


class ExperienceAwareCommander(BayesianCommander):
    """经验型路由器 — 意图识别 + 历史经验融合

    在原 BayesianCommander 基础上:
    1. 解析用户输入中的比赛上下文 (队名/赔率)
    2. 从 ExperienceMemory 查询历史相似场景
    3. 根据各子模块在相似场景的历史表现调整路由权重
    4. 返回带历史经验引用的路由结果
    """

    def __init__(self, confidence_threshold=0.5, experience_weight=0.15):
        super().__init__(confidence_threshold=confidence_threshold)
        self.experience_weight = experience_weight  # 经验权重 (0-1)
        self.memory: _Optional[ExperienceMemory] = None
        if _EXP_AVAILABLE:
            try:
                self.memory = ExperienceMemory()
                logger.info("经验型路由器已启用, ExperienceMemory 加载成功")
            except Exception as e:
                logger.warning(f"ExperienceMemory 加载失败, 回退纯意图路由: {e}")
                self.memory = None

    def route_with_experience(self, user_input: str,
                                odds_override: _Dict = None) -> ExperienceRouteResult:
        """经验型路由

        Args:
            user_input: 用户输入文本
            odds_override: 显式传入赔率 {'home':x,'draw':y,'away':z} (可选, 覆盖文本解析)

        Returns:
            ExperienceRouteResult
        """
        # Step 1: 基础意图识别
        intent_code, confidence, top3 = self.classifier.predict(
            user_input, self.confidence_threshold)

        # Step 2: 解析比赛上下文
        context = ExperienceContextParser.parse(user_input)
        if odds_override:
            context['odds'] = odds_override
            context['has_context'] = True

        # Step 3: 如果没有经验库或没有上下文, 回退纯意图路由
        exp_feats = None
        similar_matches = []
        scenario_stats = {}
        route_adjusted = False
        route_reason = ""
        final_route = ""

        if intent_code is not None:
            intent_def = self.intent_map[intent_code]
            final_route = intent_def.route_to
        else:
            final_route = '(待澄清)'

        if self.memory and context['has_context'] and 'odds' in context and context['odds']:
            try:
                odds = context['odds']
                # Step 4: 生成经验特征
                exp_feats = self.memory.generate_experience_features({
                    'home_odds': odds.get('home', 2.0),
                    'draw_odds': odds.get('draw', 3.2),
                    'away_odds': odds.get('away', 3.5),
                    'return_rate': odds.get('return_rate', 0.95),
                    'predicted_class': intent_code if intent_code in ('H', 'D', 'A') else None,
                })

                # Step 5: 查询历史相似比赛
                similar = self.memory.query_similar_matches(
                    odds.get('home', 2.0), odds.get('draw', 3.2),
                    odds.get('away', 3.5), top_k=5
                )
                similar_matches = [
                    {
                        'home': m.home, 'away': m.away, 'date': m.date[:10] if m.date else '',
                        'home_odds': m.home_odds, 'draw_odds': m.draw_odds, 'away_odds': m.away_odds,
                        'result': m.result, 'league': m.league
                    }
                    for m in similar
                ]

                # Step 6: 场景统计
                scenario_stats = {
                    'similar_count': exp_feats.similar_match_count,
                    'h_rate': exp_feats.similar_match_h_rate,
                    'd_rate': exp_feats.similar_match_d_rate,
                    'a_rate': exp_feats.similar_match_a_rate,
                    'drift_pattern': exp_feats.drift_pattern,
                    'trap_danger': exp_feats.trap_danger_level,
                    'margin_bucket': exp_feats.margin_bucket,
                    'spread_bucket': exp_feats.odds_spread_bucket,
                }

                # Step 7: 经验路由调整
                if intent_code is not None and exp_feats.recommended_module:
                    recommended = exp_feats.recommended_module
                    original = final_route
                    # 只在特定条件下调整路由
                    # 条件1: 陷阱危险高 → 强制路由到 bookmaker_sim
                    # 条件2: D-Gate危险区 + 预测D → 路由到 risk_guard
                    if (exp_feats.trap_danger_level == 'high' and
                            intent_code == 'PREDICT' and recommended == 'bookmaker_sim'):
                        final_route = recommended
                        route_adjusted = True
                        route_reason = f"陷阱危险等级=high, 调整到庄家意图分析模块"
                    elif (exp_feats.margin_bucket in ('0-0.05', '<0') and
                          intent_code == 'PREDICT'):
                        final_route = 'risk_guard'
                        route_adjusted = True
                        route_reason = f"margin={exp_feats.margin_bucket}(D-Gate危险区), 调整到风险评估模块"

                # Step 8: 置信度调整
                if exp_feats.confidence_boost != 0:
                    confidence = max(0.0, min(1.0, confidence + exp_feats.confidence_boost))

            except Exception as e:
                logger.warning(f"经验查询失败, 回退纯意图路由: {e}")

        # Step 9: 构造结果
        if intent_code is None:
            clarify_options = [(code, self.intent_map[code].name, prob)
                             for code, prob in top3] if top3 else []
            return ExperienceRouteResult(
                user_input=user_input, intent_code=None, intent_name='(不确定)',
                confidence=confidence, top3=top3, route_to=final_route,
                action='clarify', clarify_options=clarify_options,
                experience_active=exp_feats is not None,
                experience_features=exp_feats, similar_matches=similar_matches,
                scenario_stats=scenario_stats, route_adjusted=route_adjusted,
                route_reason=route_reason
            )

        intent_def = self.intent_map[intent_code]
        return ExperienceRouteResult(
            user_input=user_input, intent_code=intent_code, intent_name=intent_def.name,
            confidence=confidence, top3=top3, route_to=final_route, action='execute',
            experience_active=exp_feats is not None,
            experience_features=exp_feats, similar_matches=similar_matches,
            scenario_stats=scenario_stats, route_adjusted=route_adjusted,
            route_reason=route_reason
        )

    def format_experience_route(self, result: ExperienceRouteResult) -> str:
        """格式化经验型路由结果"""
        lines = []
        lines.append(f"🗣️ 操盘手: \"{result.user_input}\"")
        lines.append("")

        if result.action == 'clarify':
            lines.append(f"⚠️ 总指挥: 意图不太明确 (置信度{result.confidence:.1%})，请确认：")
            for code, name, prob in result.clarify_options:
                lines.append(f"   • {name} ({code}) — {prob:.1%}")
        else:
            adj_mark = " [经验调整]" if result.route_adjusted else ""
            lines.append(f"✅ 总指挥: 意图 = 【{result.intent_name}】 (置信度{result.confidence:.1%}){adj_mark}")
            lines.append(f"   → 路由: {result.route_to}")
            if result.route_adjusted and result.route_reason:
                lines.append(f"   📎 调整理由: {result.route_reason}")
            lines.append(f"   Top3: " + " | ".join([f"{c}={p:.1%}" for c, p in result.top3]))

            # 经验引用
            if result.experience_active and result.scenario_stats:
                stats = result.scenario_stats
                lines.append("")
                lines.append("📊 历史经验引用:")
                lines.append(f"   相似比赛: {stats.get('similar_count', 0)} 场")
                lines.append(f"   赛果分布: H={stats.get('h_rate',0):.0%} / D={stats.get('d_rate',0):.0%} / A={stats.get('a_rate',0):.0%}")
                lines.append(f"   盘口轨迹: {stats.get('drift_pattern', 'unknown')}")
                lines.append(f"   陷阱危险: {stats.get('trap_danger', 'unknown')}")
                lines.append(f"   margin区: {stats.get('margin_bucket', 'unknown')} | spread区: {stats.get('spread_bucket', 'unknown')}")

                if result.similar_matches:
                    lines.append("")
                    lines.append("🔮 历史相似比赛 Top3:")
                    for i, m in enumerate(result.similar_matches[:3], 1):
                        teams = f"{m['home']} vs {m['away']}" if m['home'] and m['away'] else f"match#{i}"
                        lines.append(f"   {i}. {teams} ({m['date']}) H@{m['home_odds']} D@{m['draw_odds']} A@{m['away_odds']} → {m['result']}")

        return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# 6. 经验型路由演示
# ════════════════════════════════════════════════════════════

EXPERIENCE_DEMO_CONVERSATIONS = [
    # 带赔率上下文的预测请求
    ("墨西哥vs韩国这场怎么看", {'home': 2.03, 'draw': 3.25, 'away': 3.95}),
    ("H@2.03 D@3.25 A@3.95 庄家想干嘛", None),
    ("2.03/3.25/3.95 这场能买吗", None),
    ("巴西对阿根廷这场怎么看", {'home': 2.5, 'draw': 3.2, 'away': 2.8}),
    # 纯意图 (无上下文, 应回退纯路由)
    ("这个赔率的水位什么意思", None),
    ("庄家这个盘口想干嘛", None),
    ("为什么这场预测错了", None),
    # 模糊输入
    ("这场", None),
]


def run_experience_demo():
    """经验型路由演示"""
    print("=" * 70)
    print("经验型贝叶斯总指挥 — 学习4类历史经验的智能路由")
    print("=" * 70)

    if not _EXP_AVAILABLE:
        print("⚠️ experience_memory 模块不可用, 无法演示经验型路由")
        return

    commander = ExperienceAwareCommander(confidence_threshold=0.5, experience_weight=0.15)

    print(f"\n经验权重: {commander.experience_weight}")
    print(f"置信度门控: {commander.confidence_threshold}")
    print(f"ExperienceMemory: {'✅ 已加载' if commander.memory else '❌ 未加载'}")

    # 统计
    total = 0
    exp_active = 0
    route_adjusted = 0

    for text, odds in EXPERIENCE_DEMO_CONVERSATIONS:
        result = commander.route_with_experience(text, odds_override=odds)
        print()
        print(commander.format_experience_route(result))
        print("-" * 70)
        total += 1
        if result.experience_active:
            exp_active += 1
        if result.route_adjusted:
            route_adjusted += 1

    print(f"\n{'='*70}")
    print(f"路由统计: {total} 条输入 | {exp_active} 条激活经验 | {route_adjusted} 条经验调整路由")
    print(f"{'='*70}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    # 默认运行原版演示
    run_demo()
    print("\n\n")
    # 运行经验型演示
    run_experience_demo()
    # 取消注释进入交互模式
    # interactive_test()
