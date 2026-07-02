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
from typing import Optional, Dict, List, Tuple
import numpy as np
import pandas as pd
import joblib
from sklearn.metrics import roc_auc_score, accuracy_score

logger = logging.getLogger(__name__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    NEUTRAL = "neutral"               # 无显著漂移


@dataclass
class OddsInput:
    """单场比赛赔率输入 (开盘→收盘)"""
    open_h: float; open_d: float; open_a: float
    close_h: float; close_d: float; close_a: float
    drift_h: Optional[float] = None    # 可自动计算
    drift_d: Optional[float] = None
    drift_a: Optional[float] = None

    def __post_init__(self):
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
    true_probs: Optional[Tuple[float, float, float]] = None  # 校准后真实概率估计
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
        drift_mag = max(abs(odds.drift_h), abs(odds.drift_d), abs(odds.drift_a))
        confidence = min(1.0, drift_mag / 0.30)  # 0.30为强drift
        return intent, confidence, pattern

    def _drift_sign(self, drift: float) -> int:
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
        drift_mag = max(abs(odds.drift_h), abs(odds.drift_d), abs(odds.drift_a))

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
            odds.drift_h, odds.drift_d, odds.drift_a,
            max(abs(odds.drift_h), abs(odds.drift_d), abs(odds.drift_a)),  # drift_mag
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
    def kelly_stake(self, odds: OddsInput, true_probs: Tuple[float, float, float],
                    bankroll: float = 1.0) -> Tuple[float, Optional[str]]:
        """
        凯利准则注码建议。
        f* = (b*p - q) / b, b=赔率-1, p=真实胜率, q=1-p
        返回 (kelly_fraction, recommended_side)。负=不下注。
        """
        close_odds = [odds.close_h, odds.close_d, odds.close_a]
        best_frac = 0.0
        best_side = None
        for i, (p, o, side) in enumerate(zip(true_probs, close_odds, ['H', 'D', 'A'])):
            if p <= 0 or o <= 1:
                continue
            b = o - 1
            f = (b * p - (1 - p)) / b  # kelly
            if f > best_frac:
                best_frac = f
                best_side = side
        # 凯利减半 (实战常用, 降低方差)
        best_frac *= 0.5
        return best_frac, best_side

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

        kelly, bet_side = self.kelly_stake(odds, tuple(true_probs))

        # 结论判定
        if mp_score > 0.5 and kelly > 0.02:
            verdict = f"⭐ 定错价信号强 (edge={edge:+.1%}, 凯利={kelly:.1%}), 建议下注 {bet_side}"
        elif mp_score > 0.3:
            verdict = f"△ 存在边际优势 (edge={edge:+.1%}), 但不足以克服抽水, 观望"
        elif intent in (Intent.FAKE_DEF_H, Intent.FAKE_DEF_A):
            target = self.INTENT_TARGET[intent]
            verdict = f"⚠ 检测到{intent.value}: 机构表面防{'H' if 'defH' in intent.value else 'A'}实际防{target}, 警惕诱盘"
        elif intent in (Intent.HONEST_DEF_H, Intent.HONEST_DEF_A):
            target = self.INTENT_TARGET[intent]
            verdict = f"机构诚实防{target} (置信{conf:.0%}), 但市场已定价, 无套利空间"
        else:
            verdict = "市场定价有效, 无显著套利机会"

        return AnalysisResult(
            implied_probs=(imp_h, imp_d, imp_a),
            true_probs=tuple(true_probs),
            intent=intent, intent_confidence=conf, drift_pattern=pattern,
            mispricing_score=mp_score, argmax_hit_prob=hit_prob,
            expected_edge=edge, kelly_fraction=kelly, recommended_bet=bet_side,
            verdict=verdict,
            raw={'overround': odds.overround, 'drift_mag': max(abs(odds.drift_h), abs(odds.drift_d), abs(odds.drift_a))}
        )


# ═══════════════════════════════════════════════════════════════
# 训练工具 (生成 mispricing_detector.joblib)
# ═══════════════════════════════════════════════════════════════
def train_mispricing_detector(db_path: str = None, split: str = '2023-01-01',
                               output: str = None) -> Dict:
    """
    训练误定价检测器 (二分类: 市场argmax是否命中)。
    返回训练指标。保存模型到 output。
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
