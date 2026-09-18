"""
leyu_value_signal.py — 乐鱼实时价值投注信号 (哨响AI 单庄突破口落地)

把 deliverables/single_book_model_features.md 提交的「共识概率方法论」落成 LIVE 实现:
  F1 consensus_prob 共识概率 (可插拔)
  F2 book_implied   本庄(乐鱼)隐含概率
  F3 divergence = F1 - F2            # 触发信号 & 利润主变量
  F4 overround     本庄抽水
  F5 odds_level     赔率水平
  F6 league_tier    联赛层级

决策阈值 (账本实证, 非拍脑袋):
  - 触发: divergence >= max(0.10, book_margin)   # 动态跟本庄抽水, 套利不等式 +EV⇔div>margin
  - 只押 H/A, D 平局永不触发
  - 赔率封顶 <= 6.5
  - 联赛白名单: 意甲/西甲/法甲/英超/德甲 (欧冠/荷甲/葡超剔除)
  - 注码: FLAT 等额 (禁 Kelly 复利, 回测回撤 75%)

consensus_kind 诚实标注:
  - 'self_single_book': 乐鱼单庄跨市场自共识 — 非尖庄, 分歧≈0, 触发极少 (真实反映单庄无 edge)
  - 'sharp': 真尖庄共识 (预留钩子 SHARP_CONSENSUS_PROVIDER) — 接上即点亮历史 +EV

铁律: 不糊弄. 乐鱼单庄下 F1 默认=本庄去水隐含(即 F2), 分歧≈0 → 系统如实 PASS.
真要复现 +EV, 必须给 SHARP_CONSENSUS_PROVIDER 接一个尖庄共识源(Pinnacle/多庄聚合).
"""
from __future__ import annotations
import math
from typing import Optional, Callable, Dict, Tuple, List

# ── 联赛白名单 (账本实证正ROI) ──
LEAGUE_WHITELIST = {
    "serie_a", "la_liga", "ligue_1", "premier_league", "bundesliga",
    "意甲", "西甲", "法甲", "英超", "德甲", "italy", "spain", "france", "england", "germany",
}
LEAGUE_BLACKLIST = {"uefa_champions_league", "eredivisie", "primeira_liga", "欧冠", "荷甲", "葡超"}

ODDS_CAP = 6.5          # (已废弃, 保留兼容/展示) 原 F5 赔率封顶原始值
MIN_IMPLIED_PROB = 1.0 / 6.5  # ≈0.154: F5 冷门过滤概率口径 — 共识概率低于此值不押(跨日稳定)
MIN_TRIGGER_DIV = 0.10  # 基础触发分歧 (会被本庄 margin 动态覆盖)

# 尖庄共识钩子: 设为 callable(home, away, league) -> (ph, pd, pa) 即点亮 sharp 模式
SHARP_CONSENSUS_PROVIDER: Optional[Callable[[str, str, str], Tuple[float, float, float]]] = None

# ── [M6改动1] 模块加载时尝试注册真实尖庄源 (live_odds_raw 多庄快照, Pinnacle 优先) ──
# 失败一律静默: 保持 None → evaluate() 回落 self_single_book → 如实 PASS。
# 绝不因数据源不可用而让 bridge_service 导入崩溃。
try:
    from pipeline.sharp_provider import build_sharp_provider
    _prov = build_sharp_provider()
    if _prov is not None:
        SHARP_CONSENSUS_PROVIDER = _prov
except Exception:
    pass  # 保持 None, 回落 self_single_book


def de_margin(oh: float, od: float, oa: float) -> Tuple[float, float, float]:
    """本庄去水隐含概率 (F2)."""
    ov = 1.0 / oh + 1.0 / od + 1.0 / oa
    return (1.0 / oh / ov, 1.0 / od / ov, 1.0 / oa / ov), ov


def leyu_self_consensus(ph: float, pd: float, pa: float) -> Tuple[Tuple[float, float, float], str]:
    """乐鱼单庄自共识: 就是本庄去水隐含本身 (无外部尖庄). 如实标注 self_single_book."""
    return (ph, pd, pa), "self_single_book"


def _normalize(p):
    s = sum(p)
    return tuple(x / s for x in p)


def evaluate(
    home: str, away: str,
    oh: float, od: float, oa: float,
    league: str = "",
    ah_line: Optional[float] = None,
    ah_home: Optional[float] = None,
    ah_away: Optional[float] = None,
    ou_line: Optional[float] = None,
    ou_over: Optional[float] = None,
    use_sharp: bool = False,
) -> Dict:
    """核心评估. 返回价值投注 verdict dict."""
    (bh, bd, ba), overround = de_margin(oh, od, oa)
    book_margin = overround - 1.0

    # F1 共识概率 (可插拔)
    sharp = None
    sharp_meta = {}          # [M6改动2] 尖庄源透明度元数据
    if use_sharp and SHARP_CONSENSUS_PROVIDER is not None:
        try:
            sharp = SHARP_CONSENSUS_PROVIDER(home, away, league)
            if sharp and all(0 < x < 1 for x in sharp):
                cons = _normalize(sharp)
                kind = "sharp"
                # provider 若暴露 last_meta 则取用; 普通函数钩子(如自测 fake)无此属性 → 留空
                sharp_meta = getattr(SHARP_CONSENSUS_PROVIDER, "last_meta", None) or {}
            else:
                sharp = None
        except Exception:
            sharp = None
    if sharp is None:
        cons, kind = leyu_self_consensus(bh, bd, ba)

    ph, pd, pa = cons
    # F3 分歧 (对本庄隐含)
    div_h = ph - bh
    div_d = pd - bd
    div_a = pa - ba

    # 触发阈值: 动态跟本庄 margin
    trigger = max(MIN_TRIGGER_DIV, book_margin)

    # 联赛过滤 (F6)
    lg = (league or "").lower().strip()
    league_ok = (lg in LEAGUE_WHITELIST) and (lg not in LEAGUE_BLACKLIST)
    league_note = "白名单" if league_ok else ("黑名单剔除" if lg in LEAGUE_BLACKLIST else "未配置→谨慎")

    # 候选方向: 只 H/A, 取分歧最大且 >= trigger
    cands = []
    for label, div, prob, bk, odds in [("H", div_h, ph, bh, oh), ("A", div_a, pa, ba, oa)]:
        if div < trigger:
            continue
        # F5 冷门过滤 (2026-08-18 抗诱导: 弃原始赔率值 ODDS_CAP, 改概率坐标)
        # 原"本庄 odds>6.5 不押" 在去水坐标系等价于 "本庄去水隐含 < 1/6.5≈15.4% 不押";
        # 用去水概率判定, 与当日水位/抽水结构无关, 跨日稳定。
        if bk < MIN_IMPLIED_PROB:
            continue
        if not league_ok:             # F6 联赛白名单
            continue
        edge = prob * odds - 1.0      # 套利不等式
        cands.append({"dir": label, "div": div, "cons": prob, "book": bk,
                      "odds": odds, "edge": edge, "ev": edge})
    cands.sort(key=lambda c: c["edge"], reverse=True)

    if cands:
        best = cands[0]
        decision = "BET"
        decision_text = (f"BET · 方向{best['dir']} 分歧+{best['div']*100:.1f}% "
                         f"(共识{best['cons']*100:.1f}% vs 本庄{best['book']*100:.1f}%) "
                         f"赔率{best['odds']:.2f} edge+{best['edge']*100:.1f}%")
    else:
        best = None
        decision = "PASS"
        if kind == "self_single_book":
            decision_text = ("PASS · 乐鱼单庄无独立尖庄共识, 分歧≈0 不可证伪→不接盘 "
                             f"(本庄margin {book_margin*100:.1f}%, trigger {trigger*100:.1f}%)")
        else:
            decision_text = "PASS · 无满足 分歧≥trigger 且 赔率≤封顶 且 联赛白名单 的方向"

    # [M6改动2] sharp 模式下追加共识来源透明度字段 (其余字段/逻辑一律不变)
    extra = {}
    if kind == "sharp":
        extra = {
            "consensus_method": sharp_meta.get("consensus_method"),      # pinnacle / multibook_consensus
            "sharp_book_count": sharp_meta.get("book_count", 0),         # 参与共识的庄家数
            "sharp_orientation": sharp_meta.get("orientation"),          # forward / swapped(主客反向已对调)
            "sharp_snapshot_age_hours": (round(sharp_meta["age_hours"], 2)
                                         if sharp_meta.get("age_hours") is not None else None),
        }

    return {
        "decision": decision,
        "decision_text": decision_text,
        "direction": best["dir"] if best else None,
        "best_edge_pct": round(best["edge"] * 100, 2) if best else 0.0,
        "best_div_pct": round(best["div"] * 100, 2) if best else 0.0,
        "consensus_kind": kind,
        "book_margin_pct": round(book_margin * 100, 2),
        "trigger_pct": round(trigger * 100, 2),
        "odds_cap": ODDS_CAP,
        "league": league,
        "league_filter": league_note,
        "consensus_prob": {"H": round(ph, 4), "D": round(pd, 4), "A": round(pa, 4)},
        "book_implied": {"H": round(bh, 4), "D": round(bd, 4), "A": round(ba, 4)},
        "divergence": {"H": round(div_h, 4), "D": round(div_d, 4), "A": round(div_a, 4)},
        "overround": round(overround, 4),
        "candidates": cands,
        "note": ("单庄自共识下分歧≈0, 系统如实 PASS. 接 SHARP_CONSENSUS_PROVIDER 尖庄源即点亮 +EV."
                 if kind == "self_single_book" else "尖庄共识已接入, 按分歧触发价值投注."),
        **extra,
    }


if __name__ == "__main__":
    # 自测: 乐鱼单庄 → 应 PASS
    r = evaluate("A", "B", 2.10, 3.20, 3.40, league="serie_a")
    print("self_single_book:", r["decision"], r["consensus_kind"], r["decision_text"])
    # 模拟尖庄共识: 假设尖庄更看好客胜
    def fake_sharp(h, a, lg):
        return (0.30, 0.25, 0.45)
    SHARP_CONSENSUS_PROVIDER = fake_sharp
    r2 = evaluate("A", "B", 2.10, 3.20, 3.40, league="serie_a", use_sharp=True)
    print("sharp:", r2["decision"], r2["direction"], "edge%+", r2["best_edge_pct"], "div%+", r2["best_div_pct"])
