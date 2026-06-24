"""
哨响AI v4.0 — 意图分类器 v2 (Intent Classifier v2)
====================================================
v4.0架构核心模块。基于贝叶斯分类器 + 规则引擎的混合意图识别系统。

v2 升级:
    - 意图从8类扩展为5大类+15子类
    - 集成专业术语词典增强关键词匹配
    - 输出标准化为 RouteResult (含专家调度建议)
    - 三级置信度门控 (HIGH直接执行 / MEDIUM反问确认 / LOW拒绝)

与 v1 (bayesian_commander.py) 的关系:
    - v2 兼容 v1 的8类意图定义
    - v2 新增 ANALYSIS/BACKTEST/OPTIMIZE 场景
    - v2 输出路由建议包含 WorkBuddy 专家调度信息

意图分类体系 (v4.0):
    大类          子类                        → 路由目标
    ═══════════════════════════════════════════════════════════
    PREDICT        match_result/score/goals   → prediction_engine + 全栈预测(A模式)
    ANALYZE        odds/team/tactical/market  → 专项分析(B/C模式)
    BACKTEST       single/batch/compare       → backtest_engine
    OPTIMIZE       feature/model/weight/all   → 自主优化(D模式)
    EXPLAIN        prediction/feature/error   → 归因分析

作者: Architecture v4.0
日期: 2026-06-18
"""
from __future__ import annotations
import re
import json
import logging
import math
from enum import Enum
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1. 意图定义体系
# ═══════════════════════════════════════════════════════════════

class IntentCategory(Enum):
    """v4.0 五大意图大类"""
    PREDICT = "predict"
    ANALYZE = "analyze"
    BACKTEST = "backtest"
    OPTIMIZE = "optimize"
    EXPLAIN = "explain"


class IntentSubType(Enum):
    """意图子类"""
    # PREDICT 子类
    MATCH_RESULT = "match_result"     # 胜平负预测
    SCORE_PREDICT = "score_predict"   # 比分预测
    GOALS_PREDICT = "goals_predict"   # 进球数预测
    MULTI_MARKET = "multi_market"     # 多市场综合预测

    # ANALYZE 子类
    ODDS_ANALYSIS = "odds_analysis"           # 赔率深度分析
    TEAM_ANALYSIS = "team_analysis"           # 球队实力分析
    TACTICAL_ANALYSIS = "tactical_analysis"   # 战术/阵型分析
    MARKET_ANALYSIS = "market_analysis"       # 市场行为分析(庄家意图)

    # BACKTEST 子类
    SINGLE_BACKTEST = "single_backtest"       # 单场回测
    BATCH_BACKTEST = "batch_backtest"         # 批量回测
    VERSION_COMPARE = "version_compare"       # 版本对比

    # OPTIMIZE 子类
    FEATURE_OPTIMIZE = "feature_optimize"     # 特征优化
    MODEL_OPTIMIZE = "model_optimize"         # 模型调优
    WEIGHT_OPTIMIZE = "weight_optimize"       # 权重调整
    FULL_OPTIMIZE = "full_optimize"           # 全链路优化

    # EXPLAIN 子类
    PREDICTION_EXPLAIN = "prediction_explain"   # 预测解释
    FEATURE_EXPLAIN = "feature_explain"         # 特征重要性
    ERROR_EXPLAIN = "error_explain"             # 错误归因


@dataclass
class IntentDef:
    """意图定义"""
    category: IntentCategory
    subtype: IntentSubType
    keywords: List[str]               # 高权重关键词
    negative_keywords: List[str] = field(default_factory=list)  # 负向关键词(排除)
    sample_questions: List[str] = field(default_factory=list)
    # 调度信息
    collaboration_mode: str = ""      # A/B/C/D 四类协同模式
    primary_expert: str = ""          # 主导专家 (WorkBuddy agent name)
    support_experts: List[str] = field(default_factory=list)  # 辅助专家
    # 评分参数
    priority_bias: float = 0.0       # 优先级偏移 (解决关键词平局, 专项意图应高于通用意图)


# ═══════════════════════════════════════════════════════════════
# 2. 意图注册表 (15子类)
# ═══════════════════════════════════════════════════════════════

INTENT_V2_REGISTRY: List[IntentDef] = [
    # ── PREDICT 大类 ──
    IntentDef(
        category=IntentCategory.PREDICT, subtype=IntentSubType.MATCH_RESULT,
        keywords=["预测", "谁赢", "怎么看", "结果", "胜平负", "推荐", "买什么", "下注", "这场"],
        negative_keywords=["为什么", "复盘", "对比", "优化"],
        collaboration_mode="A", primary_expert="郝优算",
        support_experts=["季泊松", "荣合众", "杜博弈", "曾均衡", "施时序", "毕建模"],
        sample_questions=["这场怎么看", "巴西对摩洛哥谁赢", "给我个预测", "这场胜平负怎么走"]
    ),
    IntentDef(
        category=IntentCategory.PREDICT, subtype=IntentSubType.SCORE_PREDICT,
        keywords=["波胆", "比分", "几比几", "具体比分", "进球数预测"],
        collaboration_mode="A", primary_expert="季泊松",
        support_experts=["施时序", "毕建模"],
        priority_bias=0.5,  # 专项意图优先级高于通用 match_result
        sample_questions=["波胆预测多少", "这场比分会是多少", "具体几比几"]
    ),
    IntentDef(
        category=IntentCategory.PREDICT, subtype=IntentSubType.GOALS_PREDICT,
        keywords=["进球数", "大小球", "总进球", "over", "under", "大球", "小球"],
        collaboration_mode="A", primary_expert="季泊松",
        support_experts=["杜博弈", "毕建模"],
        sample_questions=["大小球怎么看", "这场能进几个", "over2.5能出吗"]
    ),
    IntentDef(
        category=IntentCategory.PREDICT, subtype=IntentSubType.MULTI_MARKET,
        keywords=["多市场", "综合预测", "全面分析", "让球", "亚盘", "AH"],
        collaboration_mode="A", primary_expert="郝优算",
        support_experts=["季泊松", "杜博弈", "荣合众", "曾均衡"],
        sample_questions=["全面分析下这场", "让球盘怎么看", "所有市场都分析下"]
    ),

    # ── ANALYZE 大类 ──
    IntentDef(
        category=IntentCategory.ANALYZE, subtype=IntentSubType.ODDS_ANALYSIS,
        keywords=["赔率", "水位", "抽水", "凯利", "盘口", "spread", "隐含概率", "赔率分析", "margin", "overround"],
        collaboration_mode="B", primary_expert="杜博弈",
        support_experts=["季泊松", "毕建模"],
        priority_bias=0.5,
        sample_questions=["这个赔率什么意思", "水位怎么看的", "凯利指数分析下", "抽水率多少"]
    ),
    IntentDef(
        category=IntentCategory.ANALYZE, subtype=IntentSubType.MARKET_ANALYSIS,
        keywords=["庄家", "诱盘", "收割", "操盘手", "风控", "陷阱", "意图"],
        collaboration_mode="B", primary_expert="杜博弈",
        support_experts=["季泊松", "毕建模"],
        sample_questions=["庄家这个盘口想干嘛", "这是诱盘吗", "庄家在收割了吗"]
    ),
    IntentDef(
        category=IntentCategory.ANALYZE, subtype=IntentSubType.TEAM_ANALYSIS,
        keywords=["球队", "阵容", "伤病", "实力", "状态", "战绩", "交锋", "历史"],
        collaboration_mode="C", primary_expert="曾均衡",
        support_experts=["季泊松", "施时序"],
        priority_bias=0.5,
        sample_questions=["两队实力差距多大", "最近状态怎么样", "历史交锋记录"]
    ),
    IntentDef(
        category=IntentCategory.ANALYZE, subtype=IntentSubType.TACTICAL_ANALYSIS,
        keywords=["战术", "阵型", "打法", "策略", "教练", "换人", "首发"],
        collaboration_mode="C", primary_expert="施时序",
        support_experts=["季泊松", "曾均衡"],
        priority_bias=0.5,
        sample_questions=["他们打什么战术", "这场阵型怎么排", "教练会怎么打"]
    ),

    # ── BACKTEST 大类 ──
    IntentDef(
        category=IntentCategory.BACKTEST, subtype=IntentSubType.SINGLE_BACKTEST,
        keywords=["回测", "验证", "对了没", "预测准不准", "结果对了"],
        collaboration_mode="D", primary_expert="郝优算",
        support_experts=["毕建模", "荣合众"],
        sample_questions=["上次预测准不准", "回测一下这场", "验证下结果"]
    ),
    IntentDef(
        category=IntentCategory.BACKTEST, subtype=IntentSubType.BATCH_BACKTEST,
        keywords=["批量回测", "全量回测", "历史准确率", "OOF", "整体表现"],
        collaboration_mode="D", primary_expert="毕建模",
        support_experts=["荣合众", "郝优算"],
        sample_questions=["最近准确率多少", "全量回测下", "整体表现怎么样"]
    ),
    IntentDef(
        category=IntentCategory.BACKTEST, subtype=IntentSubType.VERSION_COMPARE,
        keywords=["版本对比", "v3", "v4", "哪个好", "A/B", "对比测试"],
        collaboration_mode="D", primary_expert="荣合众",
        support_experts=["毕建模", "郝优算"],
        sample_questions=["v3和v4哪个好", "对比下不同版本", "新版有没有提升"]
    ),

    # ── OPTIMIZE 大类 ──
    IntentDef(
        category=IntentCategory.OPTIMIZE, subtype=IntentSubType.FEATURE_OPTIMIZE,
        keywords=["特征", "因子", "SHAP", "重要性", "特征工程", "VIF", "维度"],
        collaboration_mode="D", primary_expert="齐优化",
        support_experts=["季泊松", "毕建模"],
        sample_questions=["特征重要性看下", "哪些特征有用", "特征太多怎么精简"]
    ),
    IntentDef(
        category=IntentCategory.OPTIMIZE, subtype=IntentSubType.MODEL_OPTIMIZE,
        keywords=["模型调优", "过拟合", "正则", "学习率", "树深度", "hyperparameter"],
        collaboration_mode="D", primary_expert="齐优化",
        support_experts=["荣合众", "毕建模"],
        sample_questions=["模型过拟合怎么办", "调下参数", "怎么提高准确率"]
    ),
    IntentDef(
        category=IntentCategory.OPTIMIZE, subtype=IntentSubType.WEIGHT_OPTIMIZE,
        keywords=["权重", "融合", "Stacking", "基模型", "投票", "加权"],
        collaboration_mode="D", primary_expert="荣合众",
        support_experts=["齐优化", "毕建模"],
        sample_questions=["调整下权重", "基模型怎么配比", "融合策略优化下"]
    ),
    IntentDef(
        category=IntentCategory.OPTIMIZE, subtype=IntentSubType.FULL_OPTIMIZE,
        keywords=["全面优化", "全链路", "整体提升", "系统迭代", "自主优化"],
        collaboration_mode="D", primary_expert="郝优算",
        support_experts=["季泊松", "杜博弈", "荣合众", "曾均衡", "施时序", "毕建模", "齐优化"],
        sample_questions=["全面优化下系统", "整体提升方案", "下一阶段怎么迭代"]
    ),

    # ── EXPLAIN 大类 ──
    IntentDef(
        category=IntentCategory.EXPLAIN, subtype=IntentSubType.PREDICTION_EXPLAIN,
        keywords=["为什么预测", "解释", "原因", "理由", "凭什么", "依据"],
        negative_keywords=["错", "不准", "误判"],
        collaboration_mode="C", primary_expert="毕建模",
        support_experts=["荣合众", "杜博弈"],
        sample_questions=["为什么预测平局", "解释下这个预测", "预测依据是什么"]
    ),
    IntentDef(
        category=IntentCategory.EXPLAIN, subtype=IntentSubType.FEATURE_EXPLAIN,
        keywords=["特征贡献", "哪个特征影响", "SHAP值", "决策因子", "关键因素", "特征影响"],
        collaboration_mode="C", primary_expert="季泊松",
        support_experts=["毕建模", "齐优化"],
        priority_bias=0.5,
        sample_questions=["哪些特征影响最大", "SHAP值看下", "关键决策因素是啥"]
    ),
    IntentDef(
        category=IntentCategory.EXPLAIN, subtype=IntentSubType.ERROR_EXPLAIN,
        keywords=["为什么错", "误判", "不准", "翻车", "打脸", "漏了"],
        collaboration_mode="C", primary_expert="郝优算",
        support_experts=["毕建模", "曾均衡", "荣合众"],
        priority_bias=0.5,
        sample_questions=["为什么这次预测错了", "怎么又翻车了", "漏了什么因素"]
    ),
]


# ═══════════════════════════════════════════════════════════════
# 3. 路由结果
# ═══════════════════════════════════════════════════════════════

class RouteAction(Enum):
    EXECUTE = "execute"           # 直接执行
    CLARIFY = "clarify"           # 需要澄清
    REJECT = "reject"             # 拒绝(不在能力范围)


@dataclass
class RouteResult:
    """意图分类 + 路由结果"""
    intent_category: str
    intent_subtype: str
    confidence: float
    action: str                     # execute | clarify | reject
    collaboration_mode: str         # A | B | C | D
    primary_expert: str
    support_experts: List[str]
    matched_keywords: List[str]
    clarification_question: str = ""  # 当 action=clarify 时的反问
    reject_reason: str = ""           # 当 action=reject 时的理由

    def to_dict(self) -> Dict:
        return {
            "intent": {
                "category": self.intent_category,
                "subtype": self.intent_subtype,
                "confidence": round(self.confidence, 4),
            },
            "routing": {
                "action": self.action,
                "collaboration_mode": self.collaboration_mode,
                "primary_expert": self.primary_expert,
                "support_experts": self.support_experts,
            },
            "detail": {
                "matched_keywords": self.matched_keywords,
                "clarification_question": self.clarification_question,
                "reject_reason": self.reject_reason,
            }
        }


# ═══════════════════════════════════════════════════════════════
# 4. 混合意图分类器 (贝叶斯 + 规则)
# ═══════════════════════════════════════════════════════════════

class IntentClassifierV2:
    """
    v4.0 意图分类器 — 混合贝叶斯+规则

    分类流程:
        1. 预处理: 分词 + 关键词提取
        2. 规则匹配: 关键词命中评分
        3. 负向过滤: 排除关键词降权
        4. 置信度门控: HIGH→直接执行 / MEDIUM→反问 / LOW→拒绝
        5. 输出: RouteResult (含专家调度建议)
    """

    # 门控阈值
    CONFIDENCE_HIGH = 0.55    # 直接执行
    CONFIDENCE_MEDIUM = 0.30  # 反问澄清
    # < CONFIDENCE_MEDIUM → 拒绝

    def __init__(self, confidence_threshold: float = 0.70):
        self.threshold = confidence_threshold
        self.registry = INTENT_V2_REGISTRY
        # 构建关键词→意图索引
        self._kw_index: Dict[str, List[IntentDef]] = defaultdict(list)
        for intent in self.registry:
            for kw in intent.keywords:
                self._kw_index[kw].append(intent)

    def classify(self, user_input: str) -> RouteResult:
        """
        分类用户输入

        Args:
            user_input: 用户原始输入

        Returns:
            RouteResult: 分类结果 + 路由建议
        """
        if not user_input or not user_input.strip():
            return RouteResult(
                intent_category="unknown", intent_subtype="unknown",
                confidence=0.0, action="clarify",
                collaboration_mode="", primary_expert="",
                support_experts=[], matched_keywords=[],
                clarification_question="请输入您的需求，例如：'这场怎么看' 或 '分析下赔率结构'"
            )

        # Step 1: 预处理
        cleaned = self._preprocess(user_input)

        # Step 2: 规则匹配评分
        scores: Dict[str, Tuple[IntentDef, float, List[str]]] = {}
        for intent in self.registry:
            score, matched = self._score_intent(cleaned, intent)
            if score > 0:
                key = f"{intent.category.value}_{intent.subtype.value}"
                if key not in scores or score > scores[key][1]:
                    scores[key] = (intent, score, matched)

        # Step 3: 选最高分意图
        if not scores:
            return RouteResult(
                intent_category="unknown", intent_subtype="unknown",
                confidence=0.0, action="clarify",
                collaboration_mode="", primary_expert="",
                support_experts=[], matched_keywords=[],
                clarification_question=f"不太确定您想做什么。您可以：\n• 预测比赛 → '这场怎么看'\n• 分析赔率 → '赔率分析下'\n• 回测验证 → '回测下准确率'\n• 优化系统 → '优化下模型'"
            )

        best_key = max(scores, key=lambda k: scores[k][1])
        intent, raw_score, matched = scores[best_key]

        # Step 4: 归一化置信度
        confidence = self._normalize_confidence(raw_score, cleaned)

        # Step 5: 门控决策
        action = self._gate_action(confidence)

        # Step 6: 构建结果
        clarification = ""
        if action == "clarify":
            clarification = self._build_clarification(intent, cleaned)

        return RouteResult(
            intent_category=intent.category.value,
            intent_subtype=intent.subtype.value,
            confidence=round(confidence, 4),
            action=action.value,
            collaboration_mode=intent.collaboration_mode,
            primary_expert=intent.primary_expert,
            support_experts=list(intent.support_experts),
            matched_keywords=matched,
            clarification_question=clarification,
        )

    def _preprocess(self, text: str) -> str:
        """预处理: 去噪、标准化"""
        # 去多余空格
        text = re.sub(r'\s+', ' ', text.strip())
        # 去标点(保留中文特有标点)
        text = re.sub(r'[`~!@#$%^&*()_+\-=\[\]{};:\'",.<>?/\\|]', '', text)
        return text.lower()

    def _score_intent(self, cleaned: str, intent: IntentDef) -> Tuple[float, List[str]]:
        """关键词匹配评分"""
        matched = []
        score = 0.0
        total_matched_len = 0

        for kw in intent.keywords:
            if kw in cleaned:
                matched.append(kw)
                # 多字关键词加分更多，且用指数增长奖励长关键词
                kw_score = 1.0 + 0.3 * (len(kw) - 1)
                score += kw_score
                total_matched_len += len(kw)

        # 负向关键词降权
        for nk in intent.negative_keywords:
            if nk in cleaned:
                score *= 0.3  # 负向命中大幅降权

        # 微调: 总匹配长度作为次级排序因子 (除以100作为微量tiebreaker)
        if matched:  # 仅在有关键词命中时才加偏移量
            score += total_matched_len * 0.001
            # 优先级偏移 (专项意图应击败通用意图)
            score += intent.priority_bias

        return score, matched

    def _normalize_confidence(self, raw_score: float, cleaned: str) -> float:
        """归一化置信度到 [0, 1]"""
        # 使用更温和的归一化，适合中文短查询
        # raw_score=1 (单关键词命中) → confidence≈0.45
        # raw_score=2 (两关键词) → confidence≈0.60
        # raw_score=3+ → confidence≈0.72+
        confidence = 1.0 - math.exp(-raw_score / 2.5)
        confidence = max(0.0, min(1.0, confidence))
        # 短输入 (>5字符) 有额外奖励
        if len(cleaned) >= 8:
            confidence = min(1.0, confidence * 1.15)
        return confidence

    def _gate_action(self, confidence: float) -> RouteAction:
        """置信度门控"""
        if confidence >= self.CONFIDENCE_HIGH:
            return RouteAction.EXECUTE
        elif confidence >= self.CONFIDENCE_MEDIUM:
            return RouteAction.CLARIFY
        else:
            return RouteAction.REJECT

    def _build_clarification(self, intent: IntentDef, cleaned: str) -> str:
        """构建反问澄清语句"""
        return f"您是想了解{'/'.join(intent.keywords[:3])}方面的信息吗？请再具体描述一下。"


# ═══════════════════════════════════════════════════════════════
# 5. 便捷函数
# ═══════════════════════════════════════════════════════════════

# 全局单例
_classifier_instance: Optional[IntentClassifierV2] = None


def get_classifier(threshold: float = 0.70) -> IntentClassifierV2:
    """获取意图分类器单例"""
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = IntentClassifierV2(confidence_threshold=threshold)
    return _classifier_instance


def classify_intent(user_input: str) -> RouteResult:
    """快速分类"""
    return get_classifier().classify(user_input)


def reset_classifier():
    """重置单例(测试用)"""
    global _classifier_instance
    _classifier_instance = None
