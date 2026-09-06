# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ⚠ DEPRECATED — 2026-08-05 模型收敛 (M1-M7)                          ║
# ║  死链: 仅被已弃用的 model_dispatcher 调用                          ║
# ║  替代: M7 pipeline/analysis_center.py (历史相似统计)                 ║
# ║  单一真相源: pipeline/model_catalog.py                                ║
# ║  本文件保留仅为历史可追溯, 禁止在新代码中引用.                        ║
# ╚══════════════════════════════════════════════════════════════════════╝
"""
赔率→比分规则匹配器 — v1.0

从30万odds_features直接统计的70条规则，无模型无训练。
任何模块通过 classify() 查询: 给定开盘赔率, 直接返回HDA概率+比分分布。

接入模型注册中心: 所有模型共享这个查表引擎。

规则格式:
  key: "赔率差|方向" → {HDA:(h%,d%,a%), scores:{bigH:%,slimH:%,draw:%,slimA:%,bigA:%}}

方向 (direction):
  - H_fav: 主队被看好 (open_h 明显低于 open_a)
  - A_fav: 客队被看好
  - balanced: 均衡

比分分类 (scoreline):
  - bigH: 主胜3+球
  - slimH: 主胜1球
  - draw: 平局
  - slimA: 客胜1球
  - bigA: 客胜3+球
"""

import json
from pathlib import Path
from typing import Dict, Tuple, Optional

_RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "pattern_engine_rules.json"
_RULES: Dict[str, dict] = {}

def _load():
    global _RULES
    if not _RULES:
        _RULES = json.loads(_RULES_PATH.read_text(encoding="utf-8"))

def classify(open_h: float, open_d: float, open_a: float) -> Optional[dict]:
    """给定开盘赔率 → 返回规则"""
    _load()
    spread = max(open_h, open_d, open_a) - min(open_h, open_d, open_a)
    bucket = int(round(spread / 2.0) * 2)
    bucket = max(0, min(20, bucket))

    if open_h < open_a - 0.5:
        direction = "H_fav"
    elif open_a < open_h - 0.5:
        direction = "A_fav"
    else:
        direction = "balanced"

    key = f"{bucket}|{direction}"
    return _RULES.get(key)

def classify_verbose(open_h: float, open_d: float, open_a: float) -> dict:
    """返回包含解释的完整结果"""
    rule = classify(open_h, open_d, open_a)
    if not rule:
        return {"warning": "无匹配规则, 使用模型推断", "HDA": (0.33, 0.33, 0.34)}
    hda = rule["HDA"]
    top_score = rule.get("top_score", "draw")
    score_map = {"bigH": "大胜3+", "slimH": "小胜1球", "draw": "平局", "slimA": "客小胜", "bigA": "客大胜"}

    # 附加解释
    if hda[0] >= 0.75:
        verdict = "深盘主队碾压——热门方稳吃"
    elif hda[2] >= 0.75:
        verdict = "深盘客队碾压——主队难翻身"
    elif max(hda) < 0.40:
        verdict = "纯硬币——三种结果接近均等"
    elif top_score == "draw":
        verdict = f"偏平局——{hda[1]*100:.0f}%概率打平"
    else:
        verdict = f"偏{score_map.get(top_score,top_score)}——方向明确"

    return {
        "HDA": tuple(round(p, 3) for p in hda),
        "top_score": top_score,
        "score_distribution": {score_map.get(k, k): v for k, v in rule["scores"].items()},
        "verdict": verdict,
        "n": rule["n"],
        "rule_key": f"spread={rule['spread']} dir={rule['direction']}",
    }


# ── 模型注册中心桥接 ──
def register_in_registry():
    """将规则引擎注册到 model_registry"""
    from pipeline.model_registry import register as _reg
    _reg("pattern_engine_rules",
         lambda: _RULES,
         f"赔率→比分规则引擎({len(_RULES)}条, 30万场统计)",
         ["open_h,d,a"],
         ["HDA", "score_distribution"])
    return True


# 启动时自动加载
_load()
print(f"[pattern_matcher] {len(_RULES)}条规则已加载")
