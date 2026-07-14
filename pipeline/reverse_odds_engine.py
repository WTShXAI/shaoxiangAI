"""
赔率逆向引擎 (ReverseOddsEngine) — 核心模块
=============================================
项目核心: 用赔率开盘→收盘的漂移, 逆向分析机构真实意图, 检测市场误定价。

⚠️ 诚实声明 (基于30万行严格时序回测):
  - 1x2整体预测天花板 ≈ 52.4%, 模型打不过收盘价argmax (市场有效)
  - drift/意图规则做1x2预测也逊于市场
  - 本模块的价值不在"预测比分", 而在三个经回测验证的方向:
    1. 误定价检测: 识别"市场高估自身定价准确度"的场次 (实测Top2000子集ROI+0.72%)
    2. 机构意图识别: 解释机构在防什么 (诚实防X vs 诱盘假X)
    3. 凯利注码建议: 基于误定价偏差给出是否值得下注

架构:
  ReverseOddsEngine
    ├── predict_mispricing()  误定价检测器 (核心价值, LGBM二分类: argmax是否命中)
    ├── classify_intent()     机构意图识别 (drift三方模式)
    ├── kelly_stake()         凯利注码建议
    └── analyze()             综合分析 (一站式输出)

依赖: 赔率(open/close) + 已训练的误定价模型(saved_models/mispricing_detector.joblib)
"""
from __future__ import annotations
import os, sys, json, sqlite3, logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Dict, List, Tuple, Any
import numpy as np
import pandas as pd
import joblib
from sklearn.metrics import roc_auc_score, accuracy_score

logger = logging.getLogger(__name__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 队名跨语言归一 (G6: live 英文/中英混排队名 → odds_features 中文音译) ──
def _latin_key(s: str) -> str:
    """提取串中的连续拉丁字母(去空格小写), 用于跨语言队名比对。
    例: '阿什杜德(Ashdod)' -> 'ashdod'; 'Arsenal' -> 'arsenal'; '曼城' -> ''。"""
    if not s:
        return ''
    import re
    toks = re.findall(r'[A-Za-z]+', str(s))
    return ''.join(toks).lower()


def _build_alias_map(db_path: str) -> List[Tuple[str, str]]:
    """从 team_canonical 构建 (latin_key, canonical) 列表, 用于 EN/ZH 队名桥接。"""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT canonical, aliases_json FROM team_canonical").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    amap = []
    for canon, aj in rows:
        try:
            al = json.loads(aj) if aj else []
        except Exception:
            al = []
        if isinstance(al, list):
            for a in al:
                amap.append((_latin_key(str(a)), canon))
        amap.append((_latin_key(str(canon)), canon))  # canonical 自身也入表
    return amap


def _resolve_canonical(name: str, alias_map: List[Tuple[str, str]]) -> Optional[str]:
    """将任意队名(英文/中英混排/中文)解析为 team_canonical.canonical(中文)。

    返回 canonical 或 None。不命中返回 None(调用方走 open=close 兜底并标 drift 不可用)。
    """
    if not name:
        return None
    nl = _latin_key(name)
    if nl:
        for lk, canon in alias_map:
            if lk and lk == nl:
                return canon
    # 纯中文或拉丁未匹配: 直接比原串/canonical
    s = str(name).strip()
    for lk, canon in alias_map:
        if s == canon:
            return canon
    return None


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════
class Intent(str, Enum):
    """机构操盘意图 (基于drift三方模式)"""
    HONEST_DEF_H = "honest_defH"      # 诚实防主胜: H↓D↑A↑ → 真防H
    HONEST_DEF_A = "honest_defA"      # 诚实防客胜: H↑D↑A↓ → 真防A
    FAKE_DEF_H = "fake_defH"          # 诱盘假防H: H↓D↓A↑ → 表面防H实防A
    FAKE_DEF_A = "fake_defA"          # 诱盘假防A: H↑D↓A↓ → 表面防A实防H
    ALL_DOWN = "all_down"             # 三方全降: 资金均压
    ALL_UP = "all_up"                 # 三方全升: 资金流出
    BALANCE_ACTION = "balance_action" # 单机构独调/跨机构分歧 → 平衡动作(非陷阱信号)
    NEUTRAL = "neutral"               # 无显著漂移


@dataclass
class OddsInput:
    """单场比赛赔率输入 (开盘→收盘)"""
    open_h: float; open_d: float; open_a: float
    close_h: float; close_d: float; close_a: float
    drift_h: Optional[float] = None    # 可自动计算
    drift_d: Optional[float] = None
    drift_a: Optional[float] = None

    def __post_init__(self) -> None:
        if self.drift_h is None:
            self.drift_h = (self.close_h - self.open_h) / self.open_h
        if self.drift_d is None:
            self.drift_d = (self.close_d - self.open_d) / self.open_d
        if self.drift_a is None:
            self.drift_a = (self.close_a - self.open_a) / self.open_a

    @property
    def implied_probs(self) -> Tuple[float, float, float]:
        """收盘隐含概率 (去overround归一化)"""
        ih, idd, ia = 1/self.close_h, 1/self.close_d, 1/self.close_a
        s = ih + idd + ia
        return ih/s, idd/s, ia/s

    @property
    def open_implied_probs(self) -> Tuple[float, float, float]:
        ih, idd, ia = 1/self.open_h, 1/self.open_d, 1/self.open_a
        s = ih + idd + ia
        return ih/s, idd/s, ia/s

    @property
    def overround(self) -> float:
        return (1/self.close_h + 1/self.close_d + 1/self.close_a - 1)


@dataclass
class AnalysisResult:
    """综合分析结果"""
    # 概率
    implied_probs: Tuple[float, float, float]      # 市场隐含 (H,D,A)
    true_probs: Optional[Tuple[float, ...]] = None  # 校准后真实概率估计
    # 意图
    intent: Intent = Intent.NEUTRAL
    intent_confidence: float = 0.0
    drift_pattern: str = ""
    # 误定价
    mispricing_score: float = 0.0       # 0-1, 越高越可能定错价
    argmax_hit_prob: float = 0.0         # argmax命中概率 (模型估计)
    expected_edge: float = 0.0           # 期望边际 = 真实命中 - 隐含概率
    # 注码建议
    kelly_fraction: float = 0.0          # 凯利比例 (负=不建议下注)
    recommended_bet: Optional[str] = None  # 'H'/'D'/'A'/None
    verdict: str = ""                    # 人类可读结论
    raw: Dict = field(default_factory=dict)
    # ── P3 操盘手框架扩展 (多机构 / CLV / RLM代理) ──
    single_book_only: bool = True        # True=仅单机构, drift可能是平衡动作
    confirmed: bool = False              # 跨机构同向异动确认(真信号)
    n_books: int = 1                     # 参与分析的机构数
    cross_book_sync: Optional[float] = None   # 跨机构一致性 0-1 (None=单源)
    clv_beat: Optional[float] = None     # 跨盘/跨庄 beat-close 边际(正=有edge)
    rlm_proxy: Optional[float] = None    # 跨机构分歧度(代理RLM, 非真bet-split)
    rlm_real: Optional[dict] = None     # 真 bet-split 源(dict: home/draw/away_pct+sharp_side), G4 接入; None=用代理
    # ── 跨庄分歧 soft-line 概率调整 (OOS验证2026-07-10: 分歧→淡共识热门) ──
    softline_adjusted_probs: Optional[Tuple[float, float, float]] = None
    disagreement_detected: Optional[bool] = None   # 两庄对"谁热门"看法不同
    softline_fade_applied: Optional[bool] = None   # 是否已对共识热门做淡化处理
    # ── honest_def 低权重次级修正 (全量30w行验证: 条件胜率~0.559, +4.3pp vs基线51.6%; OOS 56.6%) ──
    honest_def_target: Optional[str] = None      # 'H'/'A'/None (drift仅DB路径有, live无)
    honest_def_applied: Optional[bool] = None    # 是否做了低权重修正
    honest_def_weight: Optional[float] = None    # 修正权重(单庄0.12 / 多庄>=2同意0.25)


# ═══════════════════════════════════════════════════════════════
# 核心引擎
# ═══════════════════════════════════════════════════════════════
class ReverseOddsEngine:
    """赔率逆向引擎"""

    # 意图模式映射 (基于探索报告验证)
    # drift符号: -1=下调(防), 0=平稳, 1=上调(引), 阈值0.02
    DRIFT_THRESHOLD = 0.02
    PATTERN_MAP = {
        (-1, 1, 1): Intent.HONEST_DEF_H,
        (1, 1, -1): Intent.HONEST_DEF_A,
        (-1, -1, 1): Intent.FAKE_DEF_H,
        (1, -1, -1): Intent.FAKE_DEF_A,
        (-1, -1, -1): Intent.ALL_DOWN,
        (1, 1, 1): Intent.ALL_UP,
    }
    # 意图 → 该意图指向的"真实结果" (训练集统计验证)
    INTENT_TARGET = {
        Intent.HONEST_DEF_H: 'H',
        Intent.HONEST_DEF_A: 'A',
        Intent.FAKE_DEF_H: 'A',    # 诱盘假H, 实际防A
        Intent.FAKE_DEF_A: 'H',    # 诱盘假A, 实际防H
        Intent.ALL_DOWN: None,
        Intent.ALL_UP: None,
        Intent.NEUTRAL: None,
    }

    def __init__(self, model_path: Optional[str] = None):
        """
        Args:
            model_path: 误定价检测器模型路径 (joblib)。None则运行时加载失败时降级为纯规则。
        """
        self.mispricing_model = None
        self.model_features = None
        if model_path is None:
            model_path = os.path.join(PROJECT_ROOT, 'saved_models', 'mispricing_detector.joblib')
        self.model_path = model_path
        if os.path.exists(model_path):
            try:
                bundle = joblib.load(model_path)
                self.mispricing_model = bundle.get('model') or bundle
                self.model_features = bundle.get('feature_names')
                logger.info(f"误定价模型已加载: {model_path}")
            except Exception as e:
                logger.warning(f"误定价模型加载失败, 降级为纯规则模式: {e}")

    # ──────────────────────────────────────────
    # 意图识别 (纯规则, 无需模型)
    # ──────────────────────────────────────────
    def classify_intent(self, odds: OddsInput) -> Tuple[Intent, float, str]:
        """
        识别机构操盘意图 (drift三方模式)。
        返回 (intent, confidence, pattern_str)。
        confidence = drift幅度归一化 (越大越显著)。
        """
        hd = self._drift_sign(odds.drift_h)
        dd = self._drift_sign(odds.drift_d)
        ad = self._drift_sign(odds.drift_a)
        pattern = f"{'↓' if hd==-1 else '↑' if hd==1 else '-'}{'↓' if dd==-1 else '↑' if dd==1 else '-'}{'↓' if ad==-1 else '↑' if ad==1 else '-'}"

        intent = self.PATTERN_MAP.get((hd, dd, ad), Intent.NEUTRAL)
        # 置信度: 三方drift的显著程度
        drift_mag = max(abs(odds.drift_h or 0.0), abs(odds.drift_d or 0.0), abs(odds.drift_a or 0.0))
        confidence = min(1.0, drift_mag / 0.30)  # 0.30为强drift
        return intent, confidence, pattern

    def _drift_sign(self, drift: Optional[float]) -> int:
        if drift is None:
            return 0
        if abs(drift) < self.DRIFT_THRESHOLD:
            return 0
        return -1 if drift < 0 else 1

    # ──────────────────────────────────────────
    # 误定价检测 (核心, 需模型)
    # ──────────────────────────────────────────
    def predict_mispricing(self, odds: OddsInput) -> Tuple[float, float, float]:
        """
        误定价检测: 预测"市场argmax是否命中"的概率。
        返回 (argmax_hit_prob, expected_edge, mispricing_score)。
        - argmax_hit_prob: 模型估计的市场argmax命中率
        - expected_edge: argmax_hit_prob - 隐含概率 (正=市场低估了命中)
        - mispricing_score: 0-1, 定错价风险
        """
        imp_h, imp_d, imp_a = odds.implied_probs
        argmax_imp = max(imp_h, imp_d, imp_a)
        drift_mag = max(abs(odds.drift_h or 0.0), abs(odds.drift_d or 0.0), abs(odds.drift_a or 0.0))

        if self.mispricing_model is not None:
            # 用模型预测
            feats = self._build_features(odds)
            hit_prob = float(self.mispricing_model.predict_proba([feats])[0, 1])
        else:
            # 降级: 用统计规律 (drift越大, argmax命中越高于隐含, 实测+2.9%@极大drift)
            # 拟合: 小drift +1.9%, 大drift +2.1%, 极大drift +2.9%
            edge_est = 0.015 + 0.04 * min(drift_mag, 0.5)  # 线性近似
            hit_prob = argmax_imp + edge_est
            hit_prob = min(0.95, hit_prob)

        expected_edge = hit_prob - argmax_imp
        # mispricing_score: edge相对overround的占比 (越接近overround越值得)
        mispricing_score = max(0.0, expected_edge) / max(odds.overround, 0.01)
        mispricing_score = min(1.0, mispricing_score)
        return hit_prob, expected_edge, mispricing_score

    def _build_features(self, odds: OddsInput) -> List[float]:
        """构建误定价模型输入特征 (与训练脚本对齐)"""
        imp_h, imp_d, imp_a = odds.implied_probs
        oimp_h, oimp_d, oimp_a = odds.open_implied_probs
        feats = [
            odds.drift_h or 0.0, odds.drift_d or 0.0, odds.drift_a or 0.0,
            max(abs(odds.drift_h or 0.0), abs(odds.drift_d or 0.0), abs(odds.drift_a or 0.0)),  # drift_mag
            odds.overround,
            1.0 if odds.close_h <= odds.close_a else 0.0,  # home_edge简化
            max(imp_h, imp_d, imp_a),  # argmax_imp
            imp_d,  # cimp_d
            oimp_d,  # imp_d (开盘平局隐含)
        ]
        # 若模型有feature_names, 按其顺序对齐 (默认按上述顺序)
        return feats

    # ──────────────────────────────────────────
    # 凯利注码
    # ──────────────────────────────────────────
    def kelly_stake(self, odds: OddsInput, true_probs: Tuple[float, ...],
                    bankroll: float = 1.0) -> Tuple[float, Optional[str]]:
        """
        凯利准则注码建议 (委托 bet_core, 收敛 SSoT)。
        f* = (b*p - q) / b, b=赔率-1, p=真实胜率, q=1-p
        半凯利 (FRAC_KELLY=0.5), 返回 (kelly_fraction, recommended_side)。负=不下注。
        """
        from scripts.bet_core import kelly_fraction, FRAC_KELLY
        close_odds = [odds.close_h, odds.close_d, odds.close_a]
        best_frac = 0.0
        best_side = None
        for i, (p, o, side) in enumerate(zip(true_probs, close_odds, ['H', 'D', 'A'])):
            if p <= 0 or o <= 1:
                continue
            f = kelly_fraction(p, o)
            if f > best_frac:
                best_frac = f
                best_side = side
        return best_frac * FRAC_KELLY, best_side

    # ──────────────────────────────────────────
    # 多机构分析 (操盘手框架: 跨机构同步=真信号, 单机构独调=平衡动作)
    # ──────────────────────────────────────────
    def _consensus_implied(self, books: List[OddsInput]) -> Tuple[float, float, float]:
        """跨机构共识隐含概率 = 各机构收盘隐含概率均值 (v6 模型概率代理, 诚实估计)。"""
        if not books:
            return (0.0, 0.0, 0.0)
        s = [b.implied_probs for b in books]
        n = len(s)
        return (sum(p[0] for p in s) / n,
                sum(p[1] for p in s) / n,
                sum(p[2] for p in s) / n)

    def _best_odds(self, books: List[OddsInput]) -> List[float]:
        """跨机构最优价 (soft line 下注侧), 用于凯利 EV 计算。"""
        if not books:
            return [0.0, 0.0, 0.0]
        return [max(b.close_h for b in books), max(b.close_d for b in books),
                max(b.close_a for b in books)]

    def classify_intent_multi(self, books: List[OddsInput]) -> Dict:
        """跨机构意图识别: 只有 >=2 家独立机构同向 drift 才算"真信号"。
        单机构独调或跨机构分歧 → BALANCE_ACTION (平衡动作, 非陷阱)。

        直接编码操盘手铁律:
          "跨机构同向异动=真信号; 单机构独调=平衡动作"
          "真陷阱判定必须有跨机构同步/drift证据"。"""
        if not books:
            return {"intent": Intent.NEUTRAL, "confidence": 0.0, "pattern": "",
                    "confirmed": False, "n_books": 0, "cross_book_sync": None}
        from collections import Counter
        n = len(books)
        per_book = [self.classify_intent(b) for b in books]
        defensive = [(it, self.INTENT_TARGET.get(it))
                     for it, _, _ in per_book
                     if it in (Intent.HONEST_DEF_H, Intent.HONEST_DEF_A,
                               Intent.FAKE_DEF_H, Intent.FAKE_DEF_A)]
        cnt = Counter(it for it, _ in defensive)
        best_intent, best_count = (Intent.NEUTRAL, 0)
        if cnt:
            best_intent, best_count = cnt.most_common(1)[0]
        confirmed = best_count >= 2  # >=2独立机构同向 → 真信号
        sync = (best_count / n) if n else 0.0
        if confirmed:
            intent = best_intent
            conf = min(1.0, (max(b[1] for b in per_book) + sync) / 2)
        else:
            intent = Intent.BALANCE_ACTION
            conf = max((b[1] for b in per_book), default=0.0)
        pattern = " | ".join(b[2] for b in per_book)
        return {"intent": intent, "confidence": round(conf, 3), "pattern": pattern,
                "confirmed": confirmed, "n_books": n,
                "cross_book_sync": round(sync, 3) if n > 1 else None}

    def clv_beat(self, books: List[OddsInput]) -> Optional[float]:
        """跨盘/跨庄 beat-close 边际: 共识隐含 vs 单家收盘隐含的最大正向差。
        正 = 可在某家更优价下注 (soft line edge, v6 唯一可量化 edge 源)。"""
        if len(books) < 2:
            return None
        cons = self._consensus_implied(books)
        ai = int(np.argmax(cons))
        vals = [b.implied_probs[ai] for b in books]
        return round(float(cons[ai] - min(vals)), 4)  # 与最低隐含(=最高赔率)之差

    # 跨庄分歧 soft-line 概率调整 (OOS验证2026-07-10: 分歧→淡共识热门)
    # 实证: WH≠IW 选不同热门时, 共识热门仅命中 ~40.5%(OOS) / 33.3%(train),
    #       显著低于整体 ~53% → 唯一稳健 edge = "分歧时淡共识热门".
    # 数据约束: 2023+ odds_features 全为单庄(仅interwetten), 训练集100%缺失该特征,
    #           故只能作推理期调整规则(多庄数据可用时激活), 不可作训练特征喂v6.0.
    SOFTLINE_FAV_WIN_GIVEN_DISAGREE = 0.41  # OOS实测 0.405, 取保守 0.41
    # honest_def 低权重次级修正: 条件胜率 (全量30w行对齐55.9%, OOS测试56.6%, vs基线51.6%)
    HONEST_DEF_COND_WIN = 0.559
    HONEST_DEF_W_SINGLE = 0.12   # 仅1家庄显式 honest_def
    HONEST_DEF_W_MULTI = 0.25    # >=2家庄同向 honest_def

    def softline_adjust(self, books: List[OddsInput]) -> Dict:
        """跨庄分歧 → 共识热门概率淡化 (soft-line edge 唯一稳健形式).
        仅在 >=2 家庄且对"谁热门"看法不一致时激活; 否则返回共识隐含原值(不改).
        返回 {adjusted_probs, disagreement, fade_applied}."""
        cons = self._consensus_implied(books)
        if len(books) < 2:
            return {'adjusted_probs': None, 'disagreement': None, 'fade_applied': None}
        favs = [int(np.argmax(b.implied_probs)) for b in books]
        disagree = len(set(favs)) > 1
        if not disagree:
            return {'adjusted_probs': cons, 'disagreement': False, 'fade_applied': False}
        fav = int(np.argmax(cons))
        p_fav = cons[fav]
        target = self.SOFTLINE_FAV_WIN_GIVEN_DISAGREE
        if p_fav <= target:
            return {'adjusted_probs': cons, 'disagreement': True, 'fade_applied': False}
        new_fav = target
        removed = p_fav - new_fav
        others = list(cons); others[fav] = 0.0
        osum = sum(others)
        out = list(cons); out[fav] = new_fav
        if osum > 0:
            for i in range(3):
                if i != fav:
                    out[i] = cons[i] + removed * (others[i] / osum)
        return {'adjusted_probs': tuple(out), 'disagreement': True, 'fade_applied': True}

    def honest_def_nudge(self, books: List[OddsInput],
                         base_probs: Tuple[float, ...]) -> Dict:
        """honest_def 低权重次级修正 (软line主调整之后).

        仅当至少1家庄存在显著 drift (drift_h/d/a, 阈值DRIFT_THRESHOLD) 时激活 —
        live盘 open=close→drift自动为0→不触发 (数据现实: 逐庄历史drift仅DB路径有)。

        逻辑: 逐庄 classify_intent 检测 HONEST_DEF_H/A (诚实防H/A),
        其指向的"真实结果"侧(H/A)在全量验证中条件胜率~55.9%(+4.3pp vs基线)。
        以低权重 W 把该侧概率向 0.559 回归 (贝叶斯式温和修正, 非硬覆盖):
          W=0.12 仅1家庄; W=0.25 >=2家庄同向 (多数投票).
        返回 {probs, detected, target, weight}."""
        # 门控: 任一庄有显著 drift 才考虑
        has_drift = any(b.drift_h is not None
                        and abs(b.drift_h) >= self.DRIFT_THRESHOLD for b in books)
        if not has_drift:
            return {'probs': base_probs, 'detected': False,
                    'target': None, 'weight': 0.0}
        targets = []
        for b in books:
            it, _, _ = self.classify_intent(b)
            if it in (Intent.HONEST_DEF_H, Intent.HONEST_DEF_A):
                targets.append(self.INTENT_TARGET[it])
        if not targets:
            return {'probs': base_probs, 'detected': False,
                    'target': None, 'weight': 0.0}
        from collections import Counter
        tgt, count = Counter(targets).most_common(1)[0]
        ti = 0 if tgt == 'H' else (2 if tgt == 'A' else 1)
        W = self.HONEST_DEF_W_MULTI if count >= 2 else self.HONEST_DEF_W_SINGLE
        cond = self.HONEST_DEF_COND_WIN
        out = list(base_probs)
        out[ti] = base_probs[ti] + W * (cond - base_probs[ti])
        s = sum(out)
        out = [x / s for x in out]
        return {'probs': tuple(out), 'detected': True, 'target': tgt, 'weight': W}

    def kelly_stake_from_probs(self, true_probs: Tuple[float, ...],
                               close_odds: List[float]) -> Tuple[float, Optional[str]]:
        """凯利: 真实概率 vs 给定赔率列表 (委托 bet_core, 收敛 SSoT)。"""
        from scripts.bet_core import kelly_fraction, FRAC_KELLY
        best_frac, best_side = 0.0, None
        for i, (p, o, side) in enumerate(zip(true_probs, close_odds, ['H', 'D', 'A'])):
            if p <= 0 or o <= 1:
                continue
            f = kelly_fraction(p, o)
            if f > best_frac:
                best_frac, best_side = f, side
        return best_frac * FRAC_KELLY, best_side

    @staticmethod
    def _to_rlm_dict(rlm_real: Any) -> Optional[Dict[str, Any]]:
        if rlm_real is None:
            return None
        if hasattr(rlm_real, 'home_pct'):
            return {'home_pct': rlm_real.home_pct, 'draw_pct': rlm_real.draw_pct,
                    'away_pct': rlm_real.away_pct, 'sharp_side': rlm_real.sharp_side()}
        if isinstance(rlm_real, dict):
            d = dict(rlm_real)
            if 'sharp_side' not in d and 'home_pct' in d:
                mx = max(d['home_pct'], d['draw_pct'], d['away_pct'])
                d['sharp_side'] = 'H' if mx == d['home_pct'] else ('D' if mx == d['draw_pct'] else 'A')
            return d
        return None

    def analyze_multi(self, books: List[OddsInput], rlm_real: Optional[object] = None) -> AnalysisResult:
        """多机构一站式分析 (操盘手三段框架核心)。

        流程: 跨机构共识隐含概率 → 跨机构意图(同步判定) → CLV(beat-close) →
        凯利(仅用跨机构最优价, edge只来自不平衡)。"""
        if not books:
            return self.analyze(OddsInput(open_h=2.0, open_d=3.0, open_a=3.5,
                                         close_h=2.0, close_d=3.0, close_a=3.5))
        n = len(books)
        cons_imp = self._consensus_implied(books)
        cons_drift_mag = float(np.mean([max(abs(b.drift_h or 0.0), abs(b.drift_d or 0.0), abs(b.drift_a or 0.0))
                                        for b in books]))
        argmax_imp = max(cons_imp)
        # 误定价: 共识 drift 驱动 (稳健 fallback; 模型若存在则单机构已用)
        edge_est = 0.015 + 0.04 * min(cons_drift_mag, 0.5)
        hit_prob = min(0.95, argmax_imp + edge_est)
        expected_edge = hit_prob - argmax_imp
        mispricing_score = max(0.0, expected_edge) / max(books[0].overround, 0.01)
        mispricing_score = min(1.0, mispricing_score)

        argmax_idx = int(np.argmax(cons_imp))
        remainder = 1.0 - hit_prob
        other = list(cons_imp); other[argmax_idx] = 0
        osum = sum(other)
        true_probs = [0.0, 0.0, 0.0]; true_probs[argmax_idx] = hit_prob
        if osum > 0:
            for i in range(3):
                if i != argmax_idx:
                    true_probs[i] = remainder * other[i] / osum

        ci = self.classify_intent_multi(books)
        clv = self.clv_beat(books)
        imps = np.array([b.implied_probs for b in books])
        rlm_proxy = float(np.mean(imps.std(axis=0))) if n > 1 else None
        # 跨庄分歧 soft-line 调整 (OOS验证: 分歧→淡共识热门)
        sl = self.softline_adjust(books)
        sl_probs = sl['adjusted_probs']
        # 当触发淡化, v6.0 价值层消费的概率 = soft-line 调整后 (edge来自不平衡)
        if sl['fade_applied'] and sl_probs is not None:
            true_probs = list(sl_probs)
            fav_sl = int(np.argmax(true_probs))
            hit_prob = true_probs[fav_sl]
            expected_edge = hit_prob - cons_imp[fav_sl]

        # 次级: honest_def 低权重修正 (仅DB路径有drift时激活, 不影响live)
        hd = self.honest_def_nudge(books, tuple(true_probs))
        if hd['detected']:
            true_probs = list(hd['probs'])
            fav_hd = int(np.argmax(true_probs))
            hit_prob = true_probs[fav_hd]
            expected_edge = hit_prob - cons_imp[fav_hd]

        best = self._best_odds(books)
        kelly, bet_side = self.kelly_stake_from_probs(tuple(true_probs), best)

        if ci["confirmed"]:
            it = ci["intent"]
            if it in (Intent.FAKE_DEF_H, Intent.FAKE_DEF_A):
                tgt = self.INTENT_TARGET[it]
                verdict = (f"⚠ 跨机构同步确认诱盘信号({it.value}): "
                           f"表面防{'H' if 'defH' in it.value else 'A'}实防{tgt}, 真陷阱概率高")
            elif it in (Intent.HONEST_DEF_H, Intent.HONEST_DEF_A):
                tgt = self.INTENT_TARGET[it]
                verdict = (f"跨机构同步确认诚实防{tgt} (同步{ci['cross_book_sync']:.0%}), "
                           f"但市场已定价, 无套利")
            else:
                verdict = "跨机构同步信号(非标准防守型), 需结合CLV判断"
        else:
            verdict = ("单机构独调/跨机构分歧 → 平衡动作(非陷阱信号), 信号弱, "
                      "需≥2家同向异动确认")
        if clv is not None and clv > 0:
            verdict += f" | 跨盘beat-close边际 +{clv:.1%} (soft line edge)"
        if rlm_real is not None:
            rd = self._to_rlm_dict(rlm_real)
            if rd:
                ss = rd.get('sharp_side')
                verdict += (f" | RLM真源: 投注集中{ss} "
                            f"(H{rd['home_pct']:.0%}/D{rd['draw_pct']:.0%}/A{rd['away_pct']:.0%})")
        elif rlm_proxy is not None:
            verdict += f" | RLM代理(跨机构离散){rlm_proxy:.3f}(高=分歧, 非真bet-split)"
        if sl['disagreement']:
            fade = "已淡共识热门" if sl['fade_applied'] else "分歧但共识热门未超阈值(不淡)"
            verdict += f" | 跨庄分歧→{fade}(soft-line)"
        if hd['detected']:
            verdict += (f" | honest_def→微修{hd['target']}(W={hd['weight']:.2f}, "
                        f"条件胜率{self.HONEST_DEF_COND_WIN:.1%})")

        return AnalysisResult(
            implied_probs=cons_imp, true_probs=tuple(true_probs),
            intent=ci["intent"], intent_confidence=ci["confidence"],
            drift_pattern=ci["pattern"],
            mispricing_score=mispricing_score, argmax_hit_prob=hit_prob,
            expected_edge=expected_edge, kelly_fraction=kelly,
            recommended_bet=bet_side, verdict=verdict,
            raw={'overround': books[0].overround, 'cons_drift_mag': round(cons_drift_mag, 4)},
            single_book_only=(n < 2), confirmed=ci["confirmed"], n_books=n,
            cross_book_sync=ci["cross_book_sync"], clv_beat=clv, rlm_proxy=rlm_proxy,
            rlm_real=self._to_rlm_dict(rlm_real),
            softline_adjusted_probs=sl_probs,
            disagreement_detected=sl['disagreement'],
            softline_fade_applied=sl['fade_applied'],
            honest_def_target=hd['target'],
            honest_def_applied=hd['detected'],
            honest_def_weight=hd['weight'],
        )

    def query_odds_multi(self, home_team: str, away_team: str,
                         db_path: Optional[str] = None, match_date: Optional[str] = None) -> List[OddsInput]:
        """从 odds_features 按队名查询所有 source 的 open/close 赔率,
        返回多机构 OddsInput 列表 (>=2 家即可做跨机构同步判定)。

        注: odds_features 无 match_id 列, 直接按 (home_team, away_team) 分组各 source。
        修正(2026-07-10): 同一对阵跨赛季会出现同一 source 多行 — 必须按 source 去重
        (保留 match_date 最新一行), 否则会把"同一庄家跨赛季"误算成多家独立机构,
        虚高 n_books 并伪造跨机构同步(cross_book_sync)确认。无匹配返回 []。
        match_date 可选: 给定则只取该日期的盘口 (用于精确锁定单场)。
        """
        if db_path is None:
            db_path = os.path.join(PROJECT_ROOT, 'data', 'football_data.db')
        try:
            conn = sqlite3.connect(db_path)
            if match_date:
                rows = conn.execute(
                    "SELECT source, match_date, open_h, open_d, open_a, close_h, close_d, close_a, "
                    "drift_h, drift_d, drift_a "
                    "FROM odds_features WHERE home_team=? AND away_team=? AND match_date=? "
                    "AND open_h>0 AND close_h>0",
                    (home_team, away_team, match_date)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT source, match_date, open_h, open_d, open_a, close_h, close_d, close_a, "
                    "drift_h, drift_d, drift_a "
                    "FROM odds_features WHERE home_team=? AND away_team=? "
                    "AND open_h>0 AND close_h>0",
                    (home_team, away_team)
                ).fetchall()
            conn.close()
            # 按 source 去重: 同一庄家跨赛季多行 -> 仅留 match_date 最新一行
            best: Dict[str, tuple] = {}
            for src, mdate, oh, od, oa, ch, cd, ca, dh, dd, da in rows:
                if None in (oh, od, oa, ch, cd, ca):
                    continue
                # drift 为 NULL 时允许 (OddsInput 会按 open→close 自动重算)
                dh_v = None if dh is None else float(dh)
                dd_v = None if dd is None else float(dd)
                da_v = None if da is None else float(da)
                if src not in best or (mdate or '') > (best[src][0] or ''):
                    best[src] = (mdate, oh, od, oa, ch, cd, ca, dh_v, dd_v, da_v)
            books = []
            for src, (mdate, oh, od, oa, ch, cd, ca, dh, dd, da) in best.items():
                books.append(OddsInput(open_h=oh, open_d=od, open_a=oa,
                                       close_h=ch, close_d=cd, close_a=ca,
                                       drift_h=dh, drift_d=dd, drift_a=da))
            return books
        except sqlite3.Error as e:
            logger.debug(f"[ReverseOdds] query_odds_multi 失败 ({home_team} vs {away_team}): {e}")
            return []

    def query_odds_from_live(self, home_team: str, away_team: str,
                             db_path: Optional[str] = None, sport_key: Optional[str] = None) -> List[OddsInput]:
        """从 live_odds_raw.bookmakers_detail 解析真实多庄 HDA 明细 (供 soft-line 实战).

        bookmakers_detail 由 sp_odds_api 采集器落库 (逐庄 {name,h,d,a}, 已过滤合法1X2盘).
        live 盘口仅含当前价 → open=close=当前价 (drift类信号不激活, 但跨庄分歧检测有效).
        匹配 home_team/away_team 中文名 (采集器已转中文). 无匹配返回 [].
        """
        if db_path is None:
            db_path = os.path.join(PROJECT_ROOT, 'data', 'football_data.db')
        try:
            conn = sqlite3.connect(db_path)
            if sport_key:
                rows = conn.execute(
                    "SELECT bookmakers_detail FROM live_odds_raw "
                    "WHERE home_team=? AND away_team=? AND sport_key=? "
                    "ORDER BY id DESC LIMIT 1",
                    (home_team, away_team, sport_key)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT bookmakers_detail FROM live_odds_raw "
                    "WHERE home_team=? AND away_team=? ORDER BY id DESC LIMIT 1",
                    (home_team, away_team)).fetchall()
            conn.close()
            books = []
            for (bj,) in rows:
                if not bj:
                    continue
                try:
                    detail = json.loads(bj)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(detail, list):
                    continue
                for bk in detail:
                    h, d, a = bk.get('h'), bk.get('d'), bk.get('a')
                    if not (h and d and a):
                        continue
                    try:
                        h, d, a = float(h), float(d), float(a)
                    except (ValueError, TypeError):
                        continue
                    books.append(OddsInput(open_h=h, open_d=d, open_a=a,
                                           close_h=h, close_d=d, close_a=a))
            return books
        except sqlite3.Error as e:
            logger.debug(f"[ReverseOdds] query_odds_from_live 失败 ({home_team} vs {away_team}): {e}")
            return []

    # ──────────────────────────────────────────
    # 综合分析 (一站式)
    # ──────────────────────────────────────────
    def analyze(self, odds: OddsInput) -> AnalysisResult:
        """一站式综合分析: 意图 + 误定价 + 注码建议"""
        imp_h, imp_d, imp_a = odds.implied_probs
        intent, conf, pattern = self.classify_intent(odds)
        hit_prob, edge, mp_score = self.predict_mispricing(odds)

        # 真实概率估计: 在argmax方向用hit_prob反推
        argmax_idx = int(np.argmax([imp_h, imp_d, imp_a]))
        # 简化: argmax方向 = hit_prob, 其余按隐含比例缩放
        remainder = 1.0 - hit_prob
        other_imps = [imp_h, imp_d, imp_a]
        other_imps[argmax_idx] = 0
        other_sum = sum(other_imps)
        true_probs = [0.0, 0.0, 0.0]
        true_probs[argmax_idx] = hit_prob
        if other_sum > 0:
            for i in range(3):
                if i != argmax_idx:
                    true_probs[i] = remainder * other_imps[i] / other_sum

        # 次级: honest_def 低权重修正 (单庄drift可用时; live盘drift=0不触发)
        hd = self.honest_def_nudge([odds], tuple(true_probs))
        if hd['detected']:
            true_probs = list(hd['probs'])
        kelly, bet_side = self.kelly_stake(odds, tuple(true_probs))

        # 结论判定: 诱盘/诚实防意图优先于edge阈值
        if intent in (Intent.FAKE_DEF_H, Intent.FAKE_DEF_A):
            target = self.INTENT_TARGET[intent]
            verdict = f"⚠ 检测到{intent.value}: 机构表面防{'H' if 'defH' in intent.value else 'A'}实际防{target}, 警惕诱盘"
        elif mp_score > 0.5 and kelly > 0.02:
            verdict = f"⭐ 定错价信号强 (edge={edge:+.1%}, 凯利={kelly:.1%}), 建议下注 {bet_side}"
        elif mp_score > 0.3:
            verdict = f"△ 存在边际优势 (edge={edge:+.1%}), 但不足以克服抽水, 观望"
        elif intent in (Intent.HONEST_DEF_H, Intent.HONEST_DEF_A):
            target = self.INTENT_TARGET[intent]
            verdict = f"机构诚实防{target} (置信{conf:.0%}), 但市场已定价, 无套利空间"
        else:
            verdict = "市场定价有效, 无显著套利机会"

        # 次级 honest_def 修正提示
        if hd['detected']:
            verdict += f" | honest_def→微修{hd['target']}(W={hd['weight']:.2f})"

        # 单机构局限: 操盘手铁律——单机构独调多为平衡动作, 诱盘/诚实防须跨机构验证
        if intent in (Intent.FAKE_DEF_H, Intent.FAKE_DEF_A, Intent.HONEST_DEF_H, Intent.HONEST_DEF_A):
            verdict = "[单机构·未跨机构验证] " + verdict

        return AnalysisResult(
            implied_probs=(imp_h, imp_d, imp_a),
            true_probs=tuple(true_probs),
            intent=intent, intent_confidence=conf, drift_pattern=pattern,
            mispricing_score=mp_score, argmax_hit_prob=hit_prob,
            expected_edge=edge, kelly_fraction=kelly, recommended_bet=bet_side,
            verdict=verdict,
            raw={'overround': odds.overround, 'drift_mag': max(abs(odds.drift_h or 0.0), abs(odds.drift_d or 0.0), abs(odds.drift_a or 0.0))},
            single_book_only=True, confirmed=False, n_books=1,
            cross_book_sync=None, clv_beat=None, rlm_proxy=None, rlm_real=None,
            softline_adjusted_probs=None, disagreement_detected=None,
            softline_fade_applied=None,
            honest_def_target=hd['target'], honest_def_applied=hd['detected'],
            honest_def_weight=hd['weight'],
        )


    @staticmethod
    def query_odds_by_teams(home_team: str, away_team: str,
                            db_path: Optional[str] = None) -> Optional[OddsInput]:
        """从 odds_features 表按队名查询 open/close 赔率，构造 OddsInput。

        G6 增强: 先精确匹配; 失败则经 team_canonical 把英文/中英混排队名解析为中文
        canonical 再查 odds_features(解决 live 英文队名 ↔ 中文音译库的对齐缺口);
        再失败则主客互换兜底。无匹配记录时返回 None（调用方优雅跳过）。
        """
        if db_path is None:
            db_path = os.path.join(PROJECT_ROOT, 'data', 'football_data.db')
        try:
            conn = sqlite3.connect(db_path)
            # 跨语言归一解析 (供正向/反向复用): 英文/中英混排 → team_canonical → 中文
            alias_map = _build_alias_map(db_path)
            ch = _resolve_canonical(home_team, alias_map)
            ca = _resolve_canonical(away_team, alias_map)
            # 快速路径: 精确 (home,away)
            feat = conn.execute(
                "SELECT open_h, open_d, open_a, close_h, close_d, close_a "
                "FROM odds_features WHERE home_team=? AND away_team=? "
                "AND open_h>0 AND close_h>0 ORDER BY match_date DESC LIMIT 1",
                (home_team, away_team)
            ).fetchone()
            # G6 归一: 精确失败 → 用 canonical 查
            if not feat or None in feat[:6]:
                if ch or ca:
                    feat = conn.execute(
                        "SELECT open_h, open_d, open_a, close_h, close_d, close_a "
                        "FROM odds_features WHERE home_team=? AND away_team=? "
                        "AND open_h>0 AND close_h>0 ORDER BY match_date DESC LIMIT 1",
                        (ch or home_team, ca or away_team)
                    ).fetchone()
            # 反向 (主客互换, 部分库存储顺序不同)
            if not feat or None in feat[:6]:
                feat = conn.execute(
                    "SELECT open_h, open_d, open_a, close_h, close_d, close_a "
                    "FROM odds_features WHERE home_team=? AND away_team=? "
                    "AND open_h>0 AND close_h>0 ORDER BY match_date DESC LIMIT 1",
                    (ca or away_team, ch or home_team)
                ).fetchone()
            conn.close()
            if not feat or None in feat[:6]:
                return None
            return OddsInput(
                open_h=feat[0], open_d=feat[1], open_a=feat[2],
                close_h=feat[3], close_d=feat[4], close_a=feat[5],
            )
        except sqlite3.Error as e:
            logger.debug(f"[ReverseOdds] query_odds_by_teams 失败 ({home_team} vs {away_team}): {e}")
            return None


# ═══════════════════════════════════════════════════════════════
# 训练工具 (生成 mispricing_detector.joblib)
# ═══════════════════════════════════════════════════════════════
def train_mispricing_detector(db_path: Optional[str] = None, split: str = '2023-01-01',
                               output: Optional[str] = None, use_betfair_features: bool = False) -> Dict:
    """
    训练误定价检测器 (二分类: 市场argmax是否命中)。
    返回训练指标。保存模型到 output。

    Phase B 增强: use_betfair_features=True 时, 通过 matches.match_id
    JOIN betfair_market 表, 将 steam_move_score / volume_imbalance /
    large_bet_flag / back_lay_spread 作为新特征加入训练。
    """
    if db_path is None:
        db_path = os.path.join(PROJECT_ROOT, 'data', 'football_data.db')
    if output is None:
        output = os.path.join(PROJECT_ROOT, 'saved_models', 'mispricing_detector.joblib')

    from lightgbm import LGBMClassifier
    logger.info("训练误定价检测器...")
    c = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        'SELECT * FROM odds_features WHERE open_h>0 AND close_h>0 AND outcome IS NOT NULL AND match_date IS NOT NULL', c)
    c.close()
    df.match_date = pd.to_datetime(df.match_date)

    cimp = df[['cimp_h', 'cimp_d', 'cimp_a']].values
    y3 = df.outcome.map({'H': 0, 'D': 1, 'A': 2}).values
    df['close_argmax'] = np.argmax(cimp, axis=1)
    df['argmax_hit'] = (df.close_argmax.values == y3).astype(int)
    df['drift_mag'] = np.maximum.reduce([df.drift_h.abs(), df.drift_d.abs(), df.drift_a.abs()])
    df['argmax_imp'] = cimp[np.arange(len(df)), df.close_argmax.values]
    df['oimp_d'] = df.imp_d  # 开盘平局隐含

    feat_names = ['drift_h', 'drift_d', 'drift_a', 'drift_mag', 'overround',
                  'home_edge', 'argmax_imp', 'cimp_d', 'oimp_d']

    # ── Phase B: betfair 特征 JOIN (可选) ──
    if use_betfair_features:
        try:
            bf = pd.read_sql_query("""
                SELECT match_id, 
                       COALESCE(steam_move_score, 0) AS steam_move_score,
                       COALESCE(volume_imbalance, 0) AS volume_imbalance,
                       COALESCE(large_bet_flag, 0) AS large_bet_flag,
                       COALESCE(back_lay_spread, 0) AS back_lay_spread
                FROM betfair_market
                WHERE steam_move_score IS NOT NULL
                   OR volume_imbalance IS NOT NULL
            """, c)
            
            if len(bf) > 500:  # 至少有500条betfair数据才值得加入特征
                # 通过 matches 表桥接 odds_features ↔ betfair_market
                df = df.merge(
                    bf, left_on='match_id', right_on='match_id', how='left'
                )
                # 填充缺失 (无betfair数据的比赛用默认值)
                for col in ['steam_move_score', 'volume_imbalance', 
                            'large_bet_flag', 'back_lay_spread']:
                    if col in df.columns:
                        df[col] = df[col].fillna(0)
                
                # 扩展特征列表
                extra_names = []
                if 'steam_move_score' in df.columns:
                    extra_names.append('steam_move_score')
                if 'volume_imbalance' in df.columns:
                    extra_names.append('volume_imbalance')
                if 'large_bet_flag' in df.columns:
                    extra_names.append('large_bet_flag')
                if 'back_lay_spread' in df.columns:
                    extra_names.append('back_lay_spread')
                
                feat_names.extend(extra_names)
                logger.info(f"[PhaseB] betfair JOIN 完成: {len(bf)}条记录, "
                           f"新增{len(extra_names)}个特征 → 总{len(feat_names)}维")
            else:
                logger.info(f"[PhaseB] betfair数据不足({len(bf)}条), 跳过增强")
        except Exception as e:
            logger.warning(f"[PhaseB] betfair JOIN 失败: {e}, 使用原始9特征")

    X = df[feat_names].fillna(0).values
    y = df.argmax_hit.values
    tr = (df.match_date < split).values
    te = ~tr

    model = LGBMClassifier(
        n_estimators=300, max_depth=6, num_leaves=47,
        learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
        min_child_samples=50, reg_alpha=0.1, reg_lambda=1.0,
        random_state=42, n_jobs=-1, verbose=-1,
    )
    model.fit(X[tr], y[tr])
    proba = model.predict_proba(X[te])[:, 1]
    auc = roc_auc_score(y[te], proba)

    # Top-K ROI验证 (注意 proba/y_te/close_odds 都是测试集大小)
    y_te = y3[te]
    close_odds = df[['close_h', 'close_d', 'close_a']].values[te]
    sa = df.close_argmax.values[te]
    results = {'auc': round(auc, 4), 'feature_names': feat_names}
    order = np.argsort(-proba)  # 测试集内降序
    for top in [1000, 2000, 5000, 10000]:
        if top > len(y_te):
            continue
        idx = order[:top]
        ao = close_odds[idx][np.arange(top), sa[idx]]
        win = (sa[idx] == y_te[idx]).astype(float)
        roi = float((win * ao - 1).mean())
        hit = float(win.mean())
        results[f'top{top}'] = {'hit': round(hit, 4), 'roi': round(roi, 4),
                                 'avg_odds': round(float(ao.mean()), 3)}

    os.makedirs(os.path.dirname(output), exist_ok=True)
    joblib.dump({'model': model, 'feature_names': feat_names, 'metrics': results,
                 'trained_at': pd.Timestamp.now().isoformat(), 'split': split}, output)
    logger.info(f"模型保存: {output} | AUC={auc:.4f}")
    logger.info(f"Top2000 ROI={results.get('top2000', {}).get('roi')}")
    return results


if __name__ == '__main__':
    # 自测: 训练 + 示例分析
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    print("=" * 60)
    print("  ReverseOddsEngine 自测")
    print("=" * 60)

    # 1. 训练误定价检测器
    metrics = train_mispricing_detector()
    print(f"\n训练指标: {json.dumps(metrics, indent=2)}")

    # 2. 示例分析
    engine = ReverseOddsEngine()
    # 示例: 诚实防H (H赔率开盘2.0→收盘1.7, D/A上升)
    example = OddsInput(open_h=2.0, open_d=3.3, open_a=3.5, close_h=1.7, close_d=3.6, close_a=4.2)
    result = engine.analyze(example)
    print(f"\n示例分析 (诚实防H): {example.open_h}→{example.close_h}")
    print(f"  意图: {result.intent.value} ({result.drift_pattern}) 置信{result.intent_confidence:.0%}")
    print(f"  隐含概率: H={result.implied_probs[0]:.1%} D={result.implied_probs[1]:.1%} A={result.implied_probs[2]:.1%}")
    print(f"  argmax命中估计: {result.argmax_hit_prob:.1%} (隐含{max(result.implied_probs):.1%}, edge={result.expected_edge:+.1%})")
    print(f"  误定价分: {result.mispricing_score:.2f} | 凯利: {result.kelly_fraction:+.1%} → {result.recommended_bet}")
    print(f"  结论: {result.verdict}")
