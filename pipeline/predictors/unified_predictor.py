"""
unified_predictor.py — v7.4 盘口锚定预测器 (操盘手优先)
设计铁律(用户指令):
  1. 所有结论依据操盘手(盘口) — 默认 100% 跟盘
  2. 仅当检测到操盘手重大错判(跨盘口分歧)时, 才在该场降低操盘手权重
  3. 不依赖球队实力特征覆盖盘口 — 盘口是最强单信号

降权逻辑(数据标定, 无外推隐患):
  基于 Interwetten 14万场评估: 盘口大幅移动(分歧>0.06)的场次,
  跟"修正后线"比跟"原线"准 +3.8~4.3pp -> 分歧越大越该降权。
  用线性斜坡: 分歧 LO=0.06 起降, HI=0.16 时降到最低(主源权重0.5)。
  -> 平稳盘 100% 跟盘(最优); 仅重大分歧降权, 最多降一半。

接口(向后兼容):
  predict(home, away, odds_h, odds_d, odds_a, match_date=None, league=None,
          odds2_h=None, odds2_d=None, odds2_a=None,
          open_h=None, open_d=None, open_a=None)
  - odds_*   : 主源盘口(锚, 默认权重1.0) = 收盘/即时盘
  - odds2_*  : 可选跨盘口第二源( sharper 共识, 如雷速) — 重大错判检测/降权目标
  - open_*   : 可选初盘(开盘赔率) — 庄家意图诊断(阻/诱), 基于 IW 14万场验证

庄家意图诊断(数据标定, IW 140729场, 真实重算 2026-07-25):
  - 阻盘(开盘看好方临场升赔): 该方实际打出率仅 48.8% (<盲跟收盘51.9%) ->
        传统"阻盘=庄家真看好"在单庄1X2上不成立(庄家升热门赔率多为减负债, 该方反而少打出)
  - 诱盘(开盘看好方临场降赔造热): 该方打出率53.5%(略高于基准+1.6pp), 但边缘微弱 ->
        庄家靠抽水盈利不靠赛果反向, 无可靠额外edge
  - 平稳盘: 开盘=收盘, 跟收盘即可(最优单信号)
  - 结论: 单庄1X2的初盘->收盘漂移不是可破解的"庄家意图"信号(群体统计被资金流主导);
        真实edge来自跨庄分歧(雷速多庄 sharp vs 散户)与亚盘陷阱识别(盘口口诀维度)。
  诊断仅作解释性输出, 不覆盖主预测(主预测锚定收盘/跨庄, 已是有效市场最优)。
"""
import os
import warnings
import numpy as np

warnings.filterwarnings("ignore")

# 多庄 sharp 共识 (开源借脑落地): 用作跨庄第二源 market prior。
# 仅当调用方开启 leisu_consensus 且 leisu_odds 有同场 true sharp 庄时才生效,
# 否则保持纯单源锚定 (零副作用)。导入失败则优雅降级, 不影响单源预测。
try:
    from pipeline.multibook_consensus import sharp_consensus_for_match
except Exception:  # 包路径异常(如独立运行)时关闭该功能
    sharp_consensus_for_match = None

# 滚球神器 v2 平局模块 (初盘编码 + 类型识别, 全量实证 AUC 0.57/0.579)
# 仅在调用方传入初盘(open_*)时生效, 用于提升平局概率口径; 否则保持原收盘锚定(零副作用)。
# 设计铁律: 已证伪路径(跨市场残差/多庄edge)一律不使用, 初盘1X2去水p_d + 类型识别为唯一合法平局信号。
try:
    from analysis.draw_module import predict_draw as _draw_module_predict
except Exception:  # 模块缺失/路径异常时关闭, 不影响单源预测
    _draw_module_predict = None

# 平局模块口径元数据(供前端/bridge 透明展示模型准确性, 用户铁律: 直接说明模型准确性)
DRAW_MODULE_META = {
    "source": "opening_1x2 + match_type (滚球神器v2)",
    "validated_auc": {"football_data_open_312k": 0.570, "GQ": 0.579},
    "deprecated_paths": ["跨市场残差(1X2泊松vs报出平局率)", "OU×1X2隐含总球差", "多庄edge"],
    "note": "初盘1X2去水概率 + 比赛类型识别; 已证伪路径不再使用。仅初盘传递时激活。",
}

# 数据标定常数
# 同庄开盘->收盘代理: 分歧>0.06 即具 +4pp 纠错价值 (eval_operator_anchored.py, IW 140k)
# 但跨庄两本书差 5~8pp 是常态(双方都没错), 仅 outliers 才是真错判 ->
# 部署按"跨庄重大错判"口径, 起降门槛抬到 0.10, 上限 0.22 (对应主源最低权重0.5)
FADE_LO = 0.10   # 跨庄分歧起点: 此处开始降权(重大错判)
FADE_HI = 0.22   # 跨庄分歧上限: 主源权重降到 0.5
FADE_MIN_W = 0.5 # 最大降权幅度(主源最低权重)

MODEL_DIR = os.path.dirname(__file__)
VERSION = "v7.4-operator-anchored"

_idx = {"H": 0, "D": 1, "A": 2}
_lbl = ["H", "D", "A"]


def devig(h, d, a):
    inv = 1.0 / float(h) + 1.0 / float(d) + 1.0 / float(a)
    return np.array([(1.0 / float(h)) / inv, (1.0 / float(d)) / inv, (1.0 / float(a)) / inv])


# 温度缩放校准常量 (开源借脑落地: worldcup-predictor calibration.py 的温度缩放思路)
# 拟合方式: 在 IW 140k 上, 市场隐含概率(deoverround of close) vs 真实赛果,
# 按时间切分(2021-22 拟合 / 2023+ 验证)网格搜索 T 最小化 logloss.
# 结果(T=0.90): TEST ECE 0.0115->0.0030, logloss 0.9761->0.9750, acc 52.7%->52.7%(方向不变).
# 温度缩放是逐分量单调变换 -> argmax(胜负平方向)在合理 T 下不变, 仅纠校准(降过自信), 安全.
CALIB_T = 0.90


def _temp_scale(p: np.ndarray, T: float = CALIB_T) -> np.ndarray:
    """p ∝ p^(1/T) 后归一化. T<1 尖锐化, T>1 软化. 用于校准输出概率."""
    pp = np.clip(p, 1e-9, None) ** (1.0 / T)
    return pp / pp.sum()


def _fade_weight(op_prob, other_prob):
    """返回 0~FADE_MIN_W 的降权幅度(仅重大错判>0)。数据标定斜坡, 无外推。"""
    div = np.abs(np.array(op_prob) - np.array(other_prob))
    d = float(np.max(div))
    if d <= FADE_LO:
        return 0.0
    ramp = min((d - FADE_LO) / (FADE_HI - FADE_LO), 1.0)
    return float(ramp * FADE_MIN_W)


class UnifiedPredictor:
    version = VERSION

    def predict(self, home, away, odds_h, odds_d, odds_a,
                match_date=None, league=None,
                odds2_h=None, odds2_d=None, odds2_a=None,
                open_h=None, open_d=None, open_a=None,
                ah_line=None, ou_line=None,
                leisu_consensus: bool = False,
                temp_scale: float = None):
        op = devig(odds_h, odds_d, odds_a)
        anchor = op.copy()
        operator_weight = 1.0
        operator_overridden = False
        cross_check = None
        major_error_score = 0.0
        final = op.copy()
        intent = None
        multibook_used = False

        # 多庄 sharp 共识作为跨庄第二源 (开源借脑: 破单庄天花板唯一方向)
        # 仅当调用方未显式传 odds2 且开启 leisu_consensus 时, 自动查 leisu_odds 同场。
        # 有 true sharp 庄则把 sharp 共识概率转公平赔率喂给 odds2, 走既有跨庄降权逻辑;
        # 无数据/无 sharp 庄则 multibook_used=False, 完全回退单源锚定 (零副作用)。
        if (None in (odds2_h, odds2_d, odds2_a)) and leisu_consensus and sharp_consensus_for_match is not None:
            try:
                sc = sharp_consensus_for_match(home, away)
                if sc is not None:
                    # sharp 共识概率 -> 公平赔率 (devig(1/p)=p), 喂给 odds2 走跨庄降权
                    odds2_h, odds2_d, odds2_a = 1.0 / sc["h"], 1.0 / sc["d"], 1.0 / sc["a"]
                    multibook_used = True
            except Exception:
                pass

        # 跨盘口第二源 -> 重大错判检测 + 降权(向 sharper 共识靠拢)
        if None not in (odds2_h, odds2_d, odds2_a):
            try:
                other = devig(odds2_h, odds2_d, odds2_a)
                cross_check = other
                w = _fade_weight(op, other)
                major_error_score = float(np.max(np.abs(op - other)))
                if w > 0.0:
                    operator_overridden = True
                    operator_weight = round(1.0 - w, 3)
                    final = (1.0 - w) * op + w * other
            except Exception:
                pass

        # 庄家意图诊断(初盘->收盘漂移, 基于 IW 14万场标定)
        # 关键: 用原始赔率比较(不比 devig 概率, 避免升/降语义倒置)
        #   开盘看好方 = 开盘隐含概率最高方(=开盘赔率最低方)
        #   临场升其赔率(原始赔率↑) = 阻盘(庄家真看好, 该方打出率↑)
        #   临场降其赔率(原始赔率↓) = 诱盘(造热, 但庄家靠抽水盈利不靠赛果反向)
        if None not in (open_h, open_d, open_a):
            try:
                oopen = devig(open_h, open_d, open_a)
                ro = int(np.argmax(oopen))          # 开盘看好方(隐含概率最高)
                rc = int(np.argmax(op))              # 收盘看好方
                open_raw = np.array([open_h, open_d, open_a], dtype=float)
                close_raw = np.array([odds_h, odds_d, odds_a], dtype=float)
                if close_raw[ro] > open_raw[ro]:     # 临场升开盘看好方原始赔率 = 阻盘
                    intent = {
                        "open_fav": _lbl[ro], "close_fav": _lbl[rc],
                        "type": "阻盘",
                        "note": f"庄家升{_lbl[ro]}赔率(阻{_lbl[ro]}), 但单庄上该方实际打出率仅48.8%(<基准51.9%) -> 传统'阻盘=真看好'在单庄1X2不成立, 维持锚定收盘",
                    }
                elif close_raw[ro] < open_raw[ro]:    # 临场降开盘看好方原始赔率 = 诱盘
                    intent = {
                        "open_fav": _lbl[ro], "close_fav": _lbl[rc],
                        "type": "诱盘",
                        "note": f"庄家降{_lbl[ro]}赔率造热(诱{_lbl[ro]}), 该方打出率53.5%(略高于基准+1.6pp但边缘微弱) -> 庄家靠抽水盈利不靠赛果反向, 维持锚定收盘",
                    }
                else:
                    intent = {
                        "open_fav": _lbl[ro], "close_fav": _lbl[rc],
                        "type": "平稳",
                        "note": "开盘=收盘看法一致, 100%跟盘(最优单信号)",
                    }
            except Exception:
                pass

        # 温度缩放校准(开源借脑: worldcup-predictor calibration.py). 逐分量单调变换,
        # 在 IW 上验证 T=0.90 不改 argmax(方向 52.7%->52.7%), 仅纠校准(ECE 0.0115->0.0030).
        # temp_scale=None 时用默认 CALIB_T; 传入则覆盖(供 walk-forward 标定/OOS 回撤).
        final = _temp_scale(final, T=temp_scale if temp_scale is not None else CALIB_T)

        # —— 平局模块 (滚球神器v2, 初盘编码 + 类型识别) ——
        # 仅传入初盘(open_*)且模块可用时生效。初盘对平局的编码力(AUC 0.57/0.579)显著优于
        # 收盘(被资金流洗到~0.43), 故对 D 分量偏重 draw_module; 收盘锚定仍主导 H/A 方向。
        # 不传初盘时 draw_module=None(零副作用, 维持原收盘锚定口径)。
        draw_module_out = None
        if (_draw_module_predict is not None) and (None not in (open_h, open_d, open_a)):
            try:
                dm = _draw_module_predict(float(open_h), float(open_d), float(open_a),
                                          ahL=(float(ah_line) if ah_line is not None else None),
                                          ouL=(float(ou_line) if ou_line is not None else None))
                dm_pd = float(dm["p_draw"])
                close_d = float(final[1])
                # D 分量融合: 初盘口径与收盘口径各 0.5(初盘对平局信号更强, 取均权集成)
                blended_d = 0.5 * close_d + 0.5 * dm_pd
                new_final = np.array([float(final[0]), blended_d, float(final[2])], dtype=float)
                new_final = new_final / new_final.sum()
                final = new_final
                draw_module_out = {
                    "p_draw": round(dm_pd, 4),
                    "match_type": dm["match_type"],
                    "verdict": dm["verdict"],
                    "blended_d": round(float(blended_d), 4),
                    "meta": DRAW_MODULE_META,
                }
            except Exception:
                draw_module_out = None

        pred_i = int(np.argmax(final))
        pred = _lbl[pred_i]
        conf = float(final[pred_i])
        return {
            "prediction": pred,
            "probabilities": {
                "H": round(float(final[0]), 4),
                "D": round(float(final[1]), 4),
                "A": round(float(final[2]), 4),
            },
            "confidence": round(conf, 4),
            "operator_weight": operator_weight,
            "operator_overridden": operator_overridden,
            "major_error_score": round(major_error_score, 4),
            "multibook_used": multibook_used,
            "anchor_probabilities": {
                "H": round(float(anchor[0]), 4),
                "D": round(float(anchor[1]), 4),
                "A": round(float(anchor[2]), 4),
            },
            "cross_check_probabilities": (
                None if cross_check is None else {
                    "H": round(float(cross_check[0]), 4),
                    "D": round(float(cross_check[1]), 4),
                    "A": round(float(cross_check[2]), 4),
                }
            ),
            "intent_diagnosis": intent,
            "draw_module": draw_module_out,
            "version": self.version,
        }


if __name__ == "__main__":
    p = UnifiedPredictor()
    print("[单源] 跟盘:", p.predict("Man City", "Arsenal", 1.9, 3.6, 4.2)["prediction"],
          "op_weight=1.0")
    print("[小分歧] 仍跟盘:", p.predict("Man City", "Arsenal", 1.9, 3.6, 4.2,
           odds2_h=1.95, odds2_d=3.55, odds2_a=4.1)["operator_weight"])
    r = p.predict("Man City", "Arsenal", 1.9, 3.6, 4.2, odds2_h=2.6, odds2_d=3.2, odds2_a=2.9)
    print("[大分歧] 降权:", r["prediction"], r["probabilities"],
          "op_weight=", r["operator_weight"], "overridden=", r["operator_overridden"],
          "major_err=", r["major_error_score"])
