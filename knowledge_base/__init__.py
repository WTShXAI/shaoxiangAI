"""
哨响AI v5.0 — 知识底座 (Knowledge Base)
=========================================
v5.0架构 L5层。五库一索引的足球AI知识管理系统。

五库:
    1. football_domain     — 足球领域知识 (联赛DNA、球队分层、主客场因子)
    2. historical_patterns — 历史统计规律 (312K 赔率-胜率映射、spread-热门规律)
    3. model_registry      — 模型版本注册 (版本号、参数、效果、特征列表)
    4. lessons_learned     — 踩坑经验库 (禁止方案、数据陷阱、架构教训)
    5. feature_cookbook    — 特征工程手册 (有效特征公式、最佳维度、计算成本)

查询API:
    KnowledgeBase.lookup(domain, key) → 结构化知识
    KnowledgeBase.get_lessons(category) → 相关教训
    KnowledgeBase.get_pattern(query) → 历史规律

设计原则:
    1. 声明式存储 (YAML) — 人类可读、AI可解析
    2. 版本化管理 — 每次更新记录版本和时间戳
    3. 自动索引 — 启动时加载到内存，支持关键词搜索
    4. 专家绑定 — 每条知识标注负责专家和适用场景

作者: Architecture
日期: 2026-06-18
"""
import os
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 知识条目数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class KnowledgeEntry:
    """单条知识条目"""
    key: str                            # 唯一标识
    title: str                          # 标题
    content: str                        # 核心内容
    category: str                       # 分类 (domain/pattern/model/lesson/feature)
    domain: str                         # 领域 (quantization/game_theory/...)
    responsible_expert: str = ""        # 负责专家
    tags: List[str] = field(default_factory=list)
    severity: str = "info"              # info | warning | critical
    version_added: str = "v5.0"
    last_updated: str = ""

    def matches_query(self, query: str) -> bool:
        """检查是否匹配查询关键词"""
        q = query.lower()
        return (q in self.key.lower() or
                q in self.title.lower() or
                q in self.content.lower() or
                any(q in t.lower() for t in self.tags))

    def summary(self) -> str:
        return f"[{self.category}/{self.domain}] {self.title}: {self.content[:100]}..."

# ═══════════════════════════════════════════════════════════════
# 知识底座
# ═══════════════════════════════════════════════════════════════

class KnowledgeBase:
    """
    FootballAI 知识底座 — 中央知识管理

    启动时加载五库，提供统一的查询接口。
    """

    def __init__(self, base_dir: str = None):
        if base_dir is None:
            base_dir = os.path.join(os.path.dirname(__file__), 'knowledge_base')
        self.base_dir = base_dir
        self.entries: Dict[str, KnowledgeEntry] = {}
        self._loaded = False

    def load(self, force_reload: bool = False) -> int:
        """加载所有知识库
        
        Args:
            force_reload: 是否强制重新加载(清空已有条目)
        """
        if force_reload:
            self.entries.clear()
        elif self._loaded:
            return len(self.entries)  # 已加载, 直接返回
        
        count = 0
        count += self._load_football_domain()
        count += self._load_historical_patterns()
        count += self._load_lessons_learned()
        count += self._load_feature_cookbook()
        self._loaded = True
        logger.info(f"KnowledgeBase loaded: {count} entries")
        return count

    def is_loaded(self) -> bool:
        return self._loaded

    # ── 查询API ──

    def search(self, query: str, category: str = None, domain: str = None,
               limit: int = 10) -> List[KnowledgeEntry]:
        """关键词搜索
        
        Args:
            query: 搜索关键词 (空字符串返回所有条目)
            category: 按分类过滤
            domain: 按领域过滤
            limit: 最大返回数
        """
        if not query or not query.strip():
            results = list(self.entries.values())
        else:
            results = []
            for entry in self.entries.values():
                if not entry.matches_query(query):
                    continue
                if category and entry.category != category:
                    continue
                if domain and entry.domain != domain:
                    continue
                results.append(entry)
                if len(results) >= limit:
                    break
        
        # 对无查询的"全量"也应用过滤
        if not query or not query.strip():
            if category:
                results = [e for e in results if e.category == category]
            if domain:
                results = [e for e in results if e.domain == domain]
            if limit > 0:
                results = results[:limit]
        
        return results

    def get_lessons(self, severity: str = None, domain: str = None) -> List[KnowledgeEntry]:
        """获取经验教训"""
        results = [e for e in self.entries.values() if e.category == "lesson"]
        if severity:
            results = [e for e in results if e.severity == severity]
        if domain:
            results = [e for e in results if e.domain == domain]
        return results

    def get_pattern(self, query: str) -> List[KnowledgeEntry]:
        """查询历史规律"""
        return self.search(query, category="pattern")

    def get_by_domain(self, domain: str) -> List[KnowledgeEntry]:
        """按领域获取"""
        return [e for e in self.entries.values() if e.domain == domain]

    def get_by_expert(self, expert_name: str) -> List[KnowledgeEntry]:
        """获取某专家负责的知识"""
        return [e for e in self.entries.values() if expert_name in e.responsible_expert]

    def get_stats(self) -> Dict:
        """统计概览"""
        cats = {}
        for e in self.entries.values():
            cats[e.category] = cats.get(e.category, 0) + 1
        return {
            "total_entries": len(self.entries),
            "by_category": cats,
            "critical_lessons": len([e for e in self.entries.values()
                                     if e.severity == "critical"]),
        }

    # ── 内部加载方法 ──

    def _load_football_domain(self) -> int:
        """加载足球领域知识"""
        entries = [
            KnowledgeEntry(
                key="league_dna", title="联赛风格DNA",
                category="domain", domain="quantization",
                content="不同联赛有显著不同的进球率、平局率和主客场优势。英超: 进球率2.8/场, D率24%; 意甲: 进球率2.6/场, D率27%; 德甲: 进球率3.0/场, D率22%; J联赛: D率28%最高; 巴甲: 主场优势+15%",
                responsible_expert="季泊松",
                tags=["联赛", "进球率", "平局率", "主客场"],
            ),
            KnowledgeEntry(
                key="home_advantage", title="主客场优势系数",
                category="domain", domain="quantization",
                content="全球均值主场胜率+18pp vs 中立场。英超+15pp, 土超+25pp(最强), 巴甲+20pp, 亚洲联赛+12pp(较弱)。无观众时衰减至+8pp。",
                responsible_expert="季泊松",
                tags=["主场", "优势", "联赛差异"],
            ),
            KnowledgeEntry(
                key="derby_factor", title="德比/杯赛因子",
                category="domain", domain="quantization",
                content="德比战D率比普通比赛高8-12pp。杯赛淘汰赛阶段D率比联赛高5pp。决赛D率最高(压力大→保守)。",
                responsible_expert="季泊松",
                tags=["德比", "杯赛", "平局"],
            ),
            KnowledgeEntry(
                key="team_tier", title="球队实力分层",
                category="domain", domain="quantization",
                content="按ELO/赔率spread将球队分为5层: T1(顶级, spread>10), T2(强队, 5-10), T3(中游, 2-5), T4(弱队, 0-2), T5(鱼腩, <0)。同层对决D率最高(30-35%)。",
                responsible_expert="季泊松",
                tags=["球队", "实力", "分层", "spread"],
            ),
            KnowledgeEntry(
                key="bookmaker_signal_philosophy", title="赔率=加密协议",
                category="domain", domain="game_theory",
                content="FootballAI核心理念: 庄家赔率不是定价信号，是经过非线性变换的加密产物。预测本质=逆向工程还原庄家风控'锁定期'信号。庄家不靠猜对结果赚钱，靠抽水+资金不平衡+诱盘收割。",
                responsible_expert="杜博弈",
                tags=["核心理念", "方法论", "逆向工程"],
                severity="critical",
            ),
            KnowledgeEntry(
                key="trap_odds_patterns", title="常见诱盘模式",
                category="domain", domain="game_theory",
                content="12种诱盘模式: 浅盘大热(让球偏浅→诱导热门)、降赔升水(赔率降水位升→背离)、深盘诱杀(深盘掩护冷门)、非均匀抽水(D方向抽水极高→庄家不看好D)、波胆防线(特定比分赔率异常)。",
                responsible_expert="杜博弈",
                tags=["诱盘", "模式", "庄家", "检测"],
            ),
        ]
        for e in entries:
            self.entries[e.key] = e
        return len(entries)

    def _load_historical_patterns(self) -> int:
        """加载历史统计规律"""
        entries = [
            KnowledgeEntry(
                key="spread_favorite_mapping", title="Spread-热门胜率映射",
                category="pattern", domain="quantization",
                content="312K历史数据统计: spread 0-2→H≈45%, 2-5→H≈55%, 5-8→H≈70%, 8-12→H≈78%, 12-20→H≈85%, ≥20→H≈93%。spread每翻倍≈热门胜率+10pp(客观规律，非模型问题)。",
                responsible_expert="季泊松",
                tags=["spread", "热门", "统计", "312K"],
            ),
            KnowledgeEntry(
                key="draw_by_spread", title="各spread段平局率",
                category="pattern", domain="imbalance",
                content="D率与spread呈U型: spread 0-1→D≈30%(实力最接近), 1-3→D≈26%, 3-5→D≈22%, 5-8→D≈18%, >8→D≈12%(强者碾压)。spread<3的D预测最有价值。",
                responsible_expert="曾均衡",
                tags=["平局", "spread", "U型"],
            ),
            KnowledgeEntry(
                key="drift_signal_power", title="赔率漂移信号强度",
                category="pattern", domain="quantization",
                content="赔率漂移1%→胜率变化约0.8pp。降赔(赔率下降)信号强于升赔: 降赔1%→+1.2pp, 升赔1%→-0.6pp(不对称)。赛前1小时内漂移信号最强。",
                responsible_expert="季泊松",
                tags=["漂移", "赔率", "不对称"],
            ),
            KnowledgeEntry(
                key="optimal_feature_count", title="最优特征维度区间",
                category="pattern", domain="math",
                content="FootballAI特征工程经验: 72维基线(Acc 59.2%), 173维过拟合(维度灾难, Acc下降), 最优80-100维。新增特征必须替换低质旧特征，保持总量不膨胀。",
                responsible_expert="毕建模",
                tags=["特征", "维度", "最优", "灾难"],
                severity="warning",
            ),
            KnowledgeEntry(
                key="honest_oof_data_size", title="OOF数据量对效果的影响",
                category="pattern", domain="ensemble",
                content="诚实OOF样本量是Stacking效果的关键: v3.1用3.6K→D_F1=0.30, v3.2用8.6K→D_F1=0.50。OOF样本越多→元学习器越稳定→D预测越准。",
                responsible_expert="荣合众",
                tags=["OOF", "样本量", "Stacking", "D_F1"],
            ),
            KnowledgeEntry(
                key="optimal_non_odds_ratio", title="非赔率特征重要性占比",
                category="pattern", domain="quantization",
                content="模型虽消费赔率特征，但非赔率特征重要性占比71.1%→模型非'赔率传声筒'。赔率衍生特征(漂移/凯利/抽水)的核心价值=将静态赔率转化动态信号。",
                responsible_expert="季泊松",
                tags=["赔率", "非赔率", "特征重要性"],
            ),
        ]
        for e in entries:
            self.entries[e.key] = e
        return len(entries)

    def _load_lessons_learned(self) -> int:
        """加载踩坑经验库"""
        entries = [
            KnowledgeEntry(
                key="beta_calibration_destroy_draw", title="⚠️ Beta校准摧毁Draw召回",
                category="lesson", domain="ensemble",
                content="在多分类不平衡场景下，Beta校准(概率温度缩放)严重摧毁少数类(Draw)的召回率。v2.6验证: Acc从55.6%→49.6%, D召回从24.7%→几乎为0。永久禁用。",
                responsible_expert="荣合众",
                tags=["Beta校准", "Draw", "禁用", "灾难"],
                severity="critical",
            ),
            KnowledgeEntry(
                key="random_kfold_leakage", title="⚠️ 随机K折导致时间泄漏",
                category="lesson", domain="ensemble",
                content="随机K折CV会让未来数据混入训练集→OOF虚高(79.46%→真实52.82%)。必须严格时序切分: pre-2023训练, 2023+ OOF。这是项目最重要的验证纪律。",
                responsible_expert="荣合众",
                tags=["K折", "泄漏", "时序", "验证"],
                severity="critical",
            ),
            KnowledgeEntry(
                key="dimension_disaster_173", title="⚠️ 173维维度灾难",
                category="lesson", domain="math",
                content="P4扩容版173维特征导致严重维度灾难: 特征冗余、过拟合、推理变慢。最优区间80-100维。每新增一个特征必须先SHAP验证其独立贡献，并替换一个低质旧特征。",
                responsible_expert="毕建模",
                tags=["维度", "灾难", "173", "特征"],
                severity="critical",
            ),
            KnowledgeEntry(
                key="numpy_int64_blob", title="⚠️ numpy.int64→SQLite BLOB",
                category="lesson", domain="data",
                content="Python 3.13下 numpy.int64 被 SQLite 序列化为 BLOB 而非 INTEGER。所有入库值必须先 int() 转换。",
                responsible_expert="舒治理",
                tags=["BLOB", "numpy", "SQLite", "数据类型"],
                severity="warning",
            ),
            KnowledgeEntry(
                key="odds_expert_draw_blind", title="⚠️ OddsExpert Draw盲区",
                category="lesson", domain="ensemble",
                content="OddsExpert(纯赔率GBDT)的Draw-F1≈0.03(几乎完全看不见D)。高权重拖累融合。D-Gate方案: OE+Heuristic专科替代(仅这两者接管D通道)。",
                responsible_expert="荣合众",
                tags=["OddsExpert", "D盲", "D-Gate"],
                severity="warning",
            ),
            KnowledgeEntry(
                key="freeze_model_kfold_leakage", title="⚠️ 冻结模型+5-fold OOF泄漏",
                category="lesson", domain="ensemble",
                content="v3.1错误: 冻结预训练模型后做5-fold OOF→fold 1-4被基模型见过→虚高。正确做法: 用时间切分的诚实OOF完全重新生成元模型训练数据。",
                responsible_expert="荣合众",
                tags=["冻结", "K折", "泄漏", "OOF"],
                severity="critical",
            ),
            KnowledgeEntry(
                key="nn_pickle_trap", title="⚠️ NN pickle陷阱",
                category="lesson", domain="temporal",
                content="动态导入的PyTorch类不能pickle序列化。NN模型必须用state_dict存储(.pth格式)，不能用joblib。",
                responsible_expert="施时序",
                tags=["PyTorch", "pickle", "state_dict", "序列化"],
                severity="warning",
            ),
            KnowledgeEntry(
                key="new_team_feature_collapse", title="⚠️ 新球队冷启动特征坍缩",
                category="lesson", domain="data",
                content="新球队缺少历史特征→特征坍缩到默认值→预测偏差。补救: 用赔率反推智能默认值 + 动态赔率权重提升。",
                responsible_expert="舒治理",
                tags=["冷启动", "新球队", "坍缩"],
                severity="warning",
            ),
            KnowledgeEntry(
                key="xgb_lgb_draw_blind", title="⚠️ LGB/XGB Draw盲区",
                category="lesson", domain="imbalance",
                content="LightGBM和XGBoost在D类别上F1≈0.000-0.002(完全看不见D)。meta D通道被污染。D-Gate Fusion: 仅OE+Heuristic接管D。",
                responsible_expert="曾均衡",
                tags=["LightGBM", "XGBoost", "D盲", "D-Gate"],
                severity="warning",
            ),
            KnowledgeEntry(
                key="meta_class_weight_draw_tradeoff", title="⚠️ meta class_weight D加权trade-off",
                category="lesson", domain="imbalance",
                content="给meta learner的D类增加class_weight可提升D_F1到0.42，但Acc跌16pp(结构性trade-off)。最优: 直接加权融合 LGB=0.20/XGB=0.20/OE=0.30/Heuristic=0.30。",
                responsible_expert="曾均衡",
                tags=["class_weight", "trade-off", "D_F1", "Acc"],
                severity="warning",
            ),
            KnowledgeEntry(
                key="oof_need_scaler", title="⚠️ OOF回测必须缩放",
                category="lesson", domain="data",
                content="prepare_features返回的X未缩放，直接进_predict_with_stacking会跌至31%(过度预测D)。必须用trainer.scaler.transform(X)后再预测→v3.2=56.0%。",
                responsible_expert="舒治理",
                tags=["scaler", "OOF", "缩放", "Predict"],
                severity="critical",
            ),
        ]
        for e in entries:
            self.entries[e.key] = e
        return len(entries)

    def _load_feature_cookbook(self) -> int:
        """加载特征工程手册"""
        entries = [
            KnowledgeEntry(
                key="odds_derived_feature_formulas", title="赔率衍生特征公式",
                category="feature", domain="quantization",
                content="核心赔率衍生: drift_h = (close_h - open_h)/open_h, drift_magnitude = |drift_h|+|drift_d|+|drift_a|, implied_p = 1/odds, kelly = p_model - p_implied, margin = sum(1/odds)-1, odds_balance = |1/H-1/A|. 这些特征贡献了模型70%+的预测力。",
                responsible_expert="季泊松",
                tags=["赔率", "衍生", "drift", "公式"],
            ),
            KnowledgeEntry(
                key="draw_specialized_features", title="Draw专精特征",
                category="feature", domain="imbalance",
                content="有效Draw特征: odds_symmetry=|1/H-1/A|(越小D概率越高), balance_score=1-|H_prob-A_prob|, handicap_draw_signal=让球盘D赔率异常低, under_water_signal=大小球水位压小, low_score_env=大小球盘≤2.0。18维D专精特征已验证有效。",
                responsible_expert="曾均衡",
                tags=["Draw", "专精", "平局", "特征"],
            ),
            KnowledgeEntry(
                key="interaction_features", title="赔率交互项特征",
                category="feature", domain="quantization",
                content="高价值交互项: ix_h_drift = spread * drift_h (spread大+降赔=强信号), ix_draw_odds = (1/D) * drift_magnitude (D赔率低+漂移大=D信号), ix_home_strength = home_advantage * (1/H). 交互项帮助模型捕捉非线性关系。",
                responsible_expert="季泊松",
                tags=["交互项", "非线性", "spread", "drift"],
            ),
            KnowledgeEntry(
                key="feature_selection_criteria", title="特征筛选标准",
                category="feature", domain="math",
                content="新特征准入三关: ① SHAP值>0.005(有独立贡献), ② VIF<5(无多重共线), ③ 离线A/B Acc↑≥0.3pp(有效果提升)。不达标→不加入。",
                responsible_expert="毕建模",
                tags=["SHAP", "VIF", "筛选", "准入"],
            ),
            KnowledgeEntry(
                key="feature_computation_cost", title="特征计算成本参考",
                category="feature", domain="data",
                content="特征计算耗时: 赔率衍生(10维)≈5ms, 赔率漂移(10维)≈8ms, 交互项(30维)≈15ms, 序列特征(15维)≈30ms。总计72维≈60ms。新增特征需控制总耗时在100ms以内。",
                responsible_expert="舒治理",
                tags=["成本", "耗时", "计算", "性能"],
            ),
            KnowledgeEntry(
                key="temporal_split_discipline", title="时间切分纪律",
                category="feature", domain="ensemble",
                content="任何特征计算、模型训练、OOF回测都必须遵循严格时间切分: pre-2023训练, 2023+验证。禁止任何跨时间点的信息泄漏(包括特征工程中的数据标准化/编码)。",
                responsible_expert="荣合众",
                tags=["时间切分", "纪律", "泄漏", "训练"],
                severity="critical",
            ),
        ]
        for e in entries:
            self.entries[e.key] = e
        return len(entries)

# ═══════════════════════════════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════════════════════════════

_kb_instance: Optional[KnowledgeBase] = None

def get_knowledge_base(base_dir: str = None) -> KnowledgeBase:
    """获取知识底座单例"""
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = KnowledgeBase(base_dir)
        _kb_instance.load()
    return _kb_instance

def reset_knowledge_base():
    """重置单例(测试用)"""
    global _kb_instance
    _kb_instance = None
