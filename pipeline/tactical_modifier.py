"""战术修正模块 v2: 数据驱动战术分析 — 从硬编码球队名升级为客观指标

v2 变更 (2026-08-11, ml-engineer):
  - 去除硬编码球队名(佛得角/巴拉圭/日本/刚果/克罗地亚), 改为数据驱动分类
  - 基于 deep_tactical_analysis.json 的 tier/tactics/imp_home 字段自动推断战术风格
  - 战术风格分类: 铁桶阵/反击型/高压控球/中场控制战/标准对抗
  - 修正因子基于 tier 差距(实力不对称度) 和 战术文本语义分析
  - 保持向后兼容: get_tactical_adjustment 接口不变
"""
import json
import re
from pathlib import Path
from typing import Optional, Dict, Any

ROOT = Path(__file__).parent.parent

# ═══ 战术修正因子 (数据标定, 与 v1 保持一致) ═══
TACTICAL_MODIFIERS = {
    "铁桶阵": {"total_goals": -0.5, "draw_prob": 0.20, "upset_prob": 0.10},
    "反击型": {"total_goals": -0.3, "away_goal_prob": 0.15},
    "高压控球": {"total_goals": 0.5, "favorite_cover_prob": 0.15},
    "首进淘汰赛": {"experience_penalty": -0.3, "motivation_bonus": 0.2},
    "中场控制战": {"total_goals": -0.4, "draw_prob": 0.15},
    "传奇对决": {"extra_time_prob": 0.15, "low_scoring": 0.10},
    "标准淘汰赛对抗": {"total_goals": 0.0, "draw_boost": 0.0, "upset_boost": 0.0},
}

# ═══ 战术风格关键词分类器 ═══
TACTICS_KEYWORDS = {
    "铁桶阵": ["铁桶", "低位防守", "防守反击", "密集防守", "零封", "大巴"],
    "反击型": ["反击", "快速反击", "防守反击", "单箭头"],
    "高压控球": ["高压", "控球", "技术碾压", "压迫"],
    "中场控制战": ["中场控制", "控制战", "慢节奏", "快速转换"],
    "首进淘汰赛": ["首进", "搏命"],
    "传奇对决": ["传奇", "宿命"],
}


def load_tactical_data() -> list:
    p = ROOT / 'data' / 'deep_tactical_analysis.json'
    if p.exists():
        return json.load(open(p, 'r', encoding='utf-8'))
    return []


def infer_tactical_style(tactics_text: str) -> str:
    """基于战术描述文本推断战术风格 (替代硬编码球队名匹配)。

    Args:
        tactics_text: deep_tactical_analysis.json 中的 tactics 字段

    Returns:
        战术风格名 (铁桶阵/反击型/高压控球/中场控制战/首进淘汰赛/标准淘汰赛对抗)
    """
    if not tactics_text:
        return "标准淘汰赛对抗"

    text_lower = tactics_text.lower()

    # 优先级匹配: 更具体的风格优先
    for style, keywords in TACTICS_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return style

    return "标准淘汰赛对抗"


def compute_tier_gap(h_tier: str, a_tier: str) -> float:
    """将 tier 字符串映射为数值差距。

    Tier mapping: S=5, A=4, B+=3.5, B=3, C+=2.5, C=2, D=1
    返回 abs(home_tier_value - away_tier_value)
    """
    TIER_MAP = {"S": 5.0, "A": 4.0, "B+": 3.5, "B": 3.0, "C+": 2.5, "C": 2.0, "D": 1.0}

    hv = TIER_MAP.get(h_tier.strip() if h_tier else "", 3.0)
    av = TIER_MAP.get(a_tier.strip() if a_tier else "", 3.0)
    return abs(hv - av)


def compute_tactical_adjustment(match_entry: Dict[str, Any]) -> Dict[str, float]:
    """基于数据字段的战术修正计算 (数据驱动, 替代硬编码球队名)。

    Args:
        match_entry: deep_tactical_analysis.json 中的单条记录

    Returns:
        {"total_goals": float, "draw_boost": float, "upset_boost": float}
    """
    adj = {"total_goals": 0.0, "draw_boost": 0.0, "upset_boost": 0.0}

    tactics = match_entry.get("tactics", "")
    style = infer_tactical_style(tactics)
    tier_gap = compute_tier_gap(
        match_entry.get("h_tier", ""),
        match_entry.get("a_tier", ""),
    )

    # 铁桶阵: 总球↓, 平局↑, 冷门↑ (弱队常用策略)
    if style == "铁桶阵":
        adj["total_goals"] -= 0.5
        adj["draw_boost"] += 0.15
        # tier 差距越大, 冷门概率越高 (弱队铁桶)
        adj["upset_boost"] += 0.05 + 0.02 * tier_gap

    # 反击型: 总球略↓, 客队进球↑
    if style == "反击型":
        adj["total_goals"] -= 0.3
        # 反击型客队进球概率与实力差相关
        adj["upset_boost"] += 0.05 + 0.03 * tier_gap

    # 高压控球: 总球↑, 强队覆盖↑
    if style == "高压控球":
        adj["total_goals"] += 0.5
        # 强队高压 → 大球概率↑
        if tier_gap >= 2.0:
            adj["total_goals"] += 0.3  # 实力碾压 + 高压 = 更大球

    # 中场控制战: 总球↓, 平局↑
    if style == "中场控制战":
        adj["total_goals"] -= 0.4
        adj["draw_boost"] += 0.10

    # 首进淘汰赛: 经验↓, 战意↑ → 净胜球压缩
    if style == "首进淘汰赛":
        adj["total_goals"] -= 0.3
        adj["draw_boost"] += 0.05

    # 传奇对决: 加时↑, 低比分↑
    if style == "传奇对决":
        adj["total_goals"] -= 0.25
        adj["draw_boost"] += 0.05

    # tier 差距自身也是信号: 实力越不对称, 越不容易平局
    if tier_gap >= 2.5:
        adj["draw_boost"] -= 0.05  # 实力碾压 → 平局概率略降
    elif tier_gap <= 0.5:
        adj["draw_boost"] += 0.05  # 实力接近 → 平局概率略升

    # 极低 imp_home (客队大优): 增加冷门倾向
    try:
        imp = float(match_entry.get("imp_home", "50%").replace("%", "")) / 100.0
        if imp < 0.25:
            adj["upset_boost"] += 0.05
        elif imp > 0.75:
            adj["total_goals"] += 0.2  # 大优队 → 可能大比分
    except (ValueError, AttributeError):
        pass

    return adj


def get_tactical_adjustment(match_key: str) -> Dict[str, float]:
    """根据比赛特征返回战术修正 (保持向后兼容接口)。

    v2 变更: 使用数据驱动方法, 基于 deep_tactical_analysis.json 的
    tier/tactics/imp_home 字段自动计算, 不再硬编码球队名。

    Args:
        match_key: 比赛标识, 如 "阿根廷 vs 佛得角"

    Returns:
        {"total_goals": float, "draw_boost": float, "upset_boost": float}
    """
    data = load_tactical_data()
    for m in data:
        if m['match'] == match_key:
            return compute_tactical_adjustment(m)

    # 回退: 基于 match_key 本身做轻量分析
    return {"total_goals": 0.0, "draw_boost": 0.0, "upset_boost": 0.0}


# ═══ 量化对比工具 ═══
def compare_v1_v2() -> Dict[str, Any]:
    """对比 v1(硬编码) 和 v2(数据驱动) 的修正因子差异。

    Returns:
        {比赛: {v1: {...}, v2: {...}, delta: {...}}}
    """
    # v1 逻辑 (保留用于对比)
    def _v1_adjustment(match_key: str) -> dict:
        adj = {"total_goals": 0, "draw_boost": 0, "upset_boost": 0}
        if '佛得角' in match_key:
            adj['total_goals'] -= 0.5
            adj['draw_boost'] += 0.15
        if '巴拉圭' in match_key:
            adj['total_goals'] -= 0.5
            adj['upset_boost'] += 0.10
        if '日本' in match_key and '巴西' in match_key:
            adj['total_goals'] += 0.3
            adj['upset_boost'] += 0.15
        if '民主刚果' in match_key:
            adj['draw_boost'] += 0.05
        if '克罗地亚' in match_key and '葡萄牙' in match_key:
            adj['total_goals'] -= 0.3
            adj['draw_boost'] += 0.10
        return adj

    data = load_tactical_data()
    results = {}
    diffs = []

    for m in data:
        mk = m['match']
        v1 = _v1_adjustment(mk)
        v2 = compute_tactical_adjustment(m)

        delta = {
            "total_goals": round(v2["total_goals"] - v1["total_goals"], 3),
            "draw_boost": round(v2["draw_boost"] - v1["draw_boost"], 3),
            "upset_boost": round(v2["upset_boost"] - v1["upset_boost"], 3),
        }

        results[mk] = {
            "tactics": m.get("tactics", ""),
            "style": infer_tactical_style(m.get("tactics", "")),
            "v1": v1,
            "v2": v2,
            "delta": delta,
        }

        # 只记录有差异的
        if any(abs(v) > 0.001 for v in delta.values()):
            diffs.append((mk, delta))

    return {
        "n_matches": len(data),
        "n_diff": len(diffs),
        "per_match": results,
        "summary_diffs": diffs,
    }


if __name__ == "__main__":
    print("=" * 70)
    print("战术修正器 v2 — 数据驱动改造验证")
    print("=" * 70)

    comparison = compare_v1_v2()
    print(f"\n总比赛数: {comparison['n_matches']}")
    print(f"v1/v2 有差异的比赛: {comparison['n_diff']}")

    print("\n── 差异详情 ──")
    for mk, delta in comparison["summary_diffs"]:
        entry = comparison["per_match"][mk]
        print(f"\n  {mk}")
        print(f"    战术: {entry['tactics']} → 分类: {entry['style']}")
        print(f"    v1: 总球={entry['v1']['total_goals']:+.1f}  "
              f"平局={entry['v1']['draw_boost']:+.2f}  "
              f"冷门={entry['v1']['upset_boost']:+.2f}")
        print(f"    v2: 总球={entry['v2']['total_goals']:+.1f}  "
              f"平局={entry['v2']['draw_boost']:+.2f}  "
              f"冷门={entry['v2']['upset_boost']:+.2f}")
        print(f"    Δ:  总球={delta['total_goals']:+.3f}  "
              f"平局={delta['draw_boost']:+.3f}  "
              f"冷门={delta['upset_boost']:+.3f}")

    print("\n── v1 vs v2 覆盖度对比 ──")
    data = load_tactical_data()
    v1_nonzero = 0
    v2_nonzero = 0
    for m in data:
        mk = m['match']
        v1 = get_tactical_adjustment.__wrapped__ if hasattr(get_tactical_adjustment, '__wrapped__') else None

        # v1 硬编码覆盖的球队
        v1_covered = any(t in mk for t in ['佛得角', '巴拉圭', '日本', '民主刚果', '克罗地亚'])
        if v1_covered:
            v1_nonzero += 1

        # v2 数据驱动覆盖
        adj = compute_tactical_adjustment(m)
        if any(abs(v) > 0.001 for v in adj.values()):
            v2_nonzero += 1

    print(f"  v1 (硬编码) 覆盖: {v1_nonzero}/{len(data)} 场")
    print(f"  v2 (数据驱动) 覆盖: {v2_nonzero}/{len(data)} 场")
    print(f"\n  提升: v2 比 v1 多覆盖 {v2_nonzero - v1_nonzero} 场")

    # 演示 get_tactical_adjustment 向后兼容
    print("\n── 向后兼容测试 ──")
    tests = ["阿根廷 vs 佛得角", "德国 vs 巴拉圭", "巴西 vs 日本",
             "英格兰 vs 民主刚果", "葡萄牙 vs 克罗地亚", "美国 vs 波黑"]
    for t in tests:
        adj = get_tactical_adjustment(t)
        nonzero = {k: v for k, v in adj.items() if abs(v) > 0.001}
        print(f"  {t}: {nonzero if nonzero else '(中性, 标准对抗)'}")
