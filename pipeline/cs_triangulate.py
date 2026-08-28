# -*- coding: utf-8 -*-
"""
cs_triangulate.py — 市场结构波胆三角定位引擎 (Correct-Score Triangulation)

设计思想 (涛哥 2026-07-18 亲授 + 联网口诀):
  波胆不是纯 Poisson 能搞定的——它是被市场结构"过度约束"的:
    ① OU 盘   → 总进球下界/上界   (大2.5 ⇒ 总球≥3)
    ② AH 盘   → 净胜球下界        (主让1.5 ⇒ 主需赢≥2才穿盘; 若"输0.5"⇒ 实际只赢1球)
    ③ 1X2     → 胜负方            (最低赔方=市场认定的赢家)
    ④ CS 波胆赔率 → 对候选比分按市场隐含概率排序 (events.db 真有此数据)
  四者取交集 = "市场自洽"的波胆候选集, 再用 CS 赔率/ Poisson 排序。

口诀固化:
  - 「亚盘让球盘与大小球盘口有 1 球差值 ⇒ 上盘+小球对冲」(如 AH2.75+OU3.75 ⇒ 3-0)
  - 「半大全小, 半小全大」(in-play: 上半大球⇒全场难超初盘; 上半小球⇒下半常穿)
  - 「大小球盘型比对法」: OU 线 vs 联赛标准盘(2.5), 开大=大球倾向, 开浅=小球倾向
  - 联赛大小球属性: 大球联赛(德甲/荷甲/日职...) 偏 over, 小球联赛(法甲/意甲/罗甲...) 偏 under

兼容:
  - 赛前 (pre-match): AH/ OU 只给"下界约束", 生成候选集
  - in-play: 传入 live_score(当前比分) ⇒ 候选 h≥H,a≥A, 再用 AH/ OU 约束终盘
  - 可与 OIP Poisson 矩阵融合 (poisson_matrix 可选), 也可纯靠 CS 赔率排序

第一性原理: 本引擎不声称"命中率翻倍"——波胆天花板确凿(最常用1-1仅~12%)。
它的价值是: 把 Poisson 的 ~12% top1 进一步"聚焦"到市场自洽的 2-4 个比分上,
让 top-N 排序更准、且可被盘口逻辑解释 (可审计, 非黑箱)。
"""
from __future__ import annotations
import math
from typing import Optional, Dict, List, Tuple, Any

MAX_GOAL = 9  # 候选搜索空间上界

# 联赛大小球属性 (口诀·联赛维度) — True=大球联赛, False=小球联赛, None=中性
BIG_LEAGUES = {
    'bundesliga', 'epl', 'premier_league', 'eredivisie', 'j_league', 'j1',
    'k_league', 'allsvenskan', 'swiss_super', 'eliteserien', 'superliga',
    'brazilian_serie_a', 'liga_mx', 'mls',
}
SMALL_LEAGUES = {
    'ligue_1', 'serie_a', 'liga_1', 'rpl', 'segunda', 'a_league', 'liga_portugal',
}


def _implied_from_odds(h: Optional[float], d: Optional[float], a: Optional[float]
                        ) -> Tuple[float, float, float]:
    """1X2 赔率 → 去水隐含概率 (复刻 score_model.deoverround 的简化版)。"""
    vals = [v for v in (h, d, a) if v and v > 0]
    if not vals:
        return (1 / 3, 1 / 3, 1 / 3)
    inv = [1.0 / v for v in (h or 1e9, d or 1e9, a or 1e9)]
    tot = sum(inv)
    if tot <= 0:
        return (1 / 3, 1 / 3, 1 / 3)
    return tuple(x / tot for x in inv)


def _ah_cover_threshold(ah_line: float) -> int:
    """AH 让球方要'穿盘'所需的最小净胜球数。
    ah_line>0 ⇒ 主队受让(客为让球方); ah_line<0 ⇒ 主队让球(主为让球方)。
    -1.5→2, -0.5→1, -1.0→2(1球走盘), -2.0→3, +0.75→1(客穿需赢≥1)
    公式: ceil(|ah_line| + 0.25)  (亚盘 quarter 盘精确: 2.75→3, 1.5→2, 0.5→1, 1.0→2走盘)
    """
    return int(math.ceil(abs(ah_line) + 0.25))


def _ah_favorite(ah_line: float) -> str:
    """返回 AH 让球方 ('home' / 'away')。"""
    return 'away' if ah_line > 0 else 'home'


def _cs_implied_probs(cs_odds: Dict[str, float]) -> Dict[str, float]:
    """CS 赔率 dict {'3-2':31.0,...} → 去'other'后的隐含概率。"""
    spec = {k: v for k, v in cs_odds.items() if k not in ('other', '', 'OT', 'Others') and v and v > 0}
    if not spec:
        return {}
    inv = {k: 1.0 / v for k, v in spec.items()}
    z = sum(inv.values())
    return {k: iv / z for k, iv in inv.items()}


def _poisson_over_prob(matrix, ou_line: float) -> Optional[float]:
    """从 poisson/条件矩阵算 P(总球 > ou_line)。矩阵需为 2D (hg,ag)；不可用返回 None。

    用于 OU 盘型比对与模型概率对账: 禁止"线>2.75 就吐大球倾向"的硬套,
    方向以模型隐含的 P(over) 为准。
    """
    if matrix is None or ou_line is None:
        return None
    try:
        import numpy as np
        M = np.asarray(matrix, dtype=float)
        if M.ndim != 2:
            return None
        total = float(M.sum())
        if total <= 0:
            return None
        M = M / total
        h, w = M.shape
        over = 0.0
        for i in range(h):
            for j in range(w):
                if (i + j) > ou_line:   # over 严格大于线 (3.75→总球≥4)
                    over += M[i, j]
        return float(over)
    except Exception:
        return None


def triangulate(
    ou_line: Optional[float] = None,
    ou_outcome: Optional[str] = None,          # 'over' | 'under' | None(只按线定软界)
    ah_line: Optional[float] = None,           # >0 主受让(客让); <0 主让
    h: Optional[float] = None, d: Optional[float] = None, a: Optional[float] = None,
    cs_odds: Optional[Dict[str, float]] = None,
    league: Optional[str] = None,
    live_score: Optional[Tuple[int, int]] = None,
    elapsed: Optional[int] = None,
    poisson_matrix: Optional[Any] = None,      # numpy 矩阵或类数组, 可选
) -> Dict[str, Any]:
    """市场结构波胆三角定位。

    返回:
      candidates: [{score,hg,ag,reason,cs_prob,poisson,blend}]
      ranked:     按 blend 降序的比分字符串列表 (top-N)
      winner:     'home'|'draw'|'away' (1X2 最低赔方)
      ah_favorite:'home'|'away'|None
      notes:      口诀注释列表
      method:     'market-structure'
    """
    notes: List[str] = []
    candidates: List[Dict[str, Any]] = []

    # ── 1. 胜负方 (1X2) ──
    ph, pd, pa = _implied_from_odds(h, d, a)
    # 注: 隐含概率最高者 = 赔率最低者 = 市场认定的赢家
    if ph >= pd and ph >= pa:
        winner = 'home'
    elif pa >= pd and pa >= ph:
        winner = 'away'
    else:
        winner = 'draw'
    # 平局弱约束: 若平赔与最低方差<8%, 视为"平局敏感"
    draw_lean = (pd <= min(ph, pa) * 1.08)

    # ── 2. AH 约束 ──
    ah_fav = _ah_favorite(ah_line) if ah_line is not None else None
    ah_thr = _ah_cover_threshold(ah_line) if ah_line is not None else 0
    # 自洽性: 正常市场 AH 让球方必 == 1X2 胜方; 若矛盾(跨庄/盘口错位), AH 降级为软提示
    ah_consistent = (ah_fav == winner) if ah_fav else True
    if ah_fav and ah_line is not None:
        notes.append(f"AH 盘 {ah_line:+.2f} ⇒ 让球方={ah_fav}, 穿盘需净胜≥{ah_thr}球")
    if ah_fav and not ah_consistent:
        notes.append(f"AH 与 1X2 胜方矛盾({ah_fav}≠{winner}) ⇒ AH 约束降级(盘口错位/跨庄)")

    # ── 3. OU 约束 ──
    ou_branch = 'both'
    total_lo, total_hi = 0, MAX_GOAL
    model_over = None  # 模型 P(over), 供盘型比对/半小全大对账
    if ou_line is not None:
        if ou_outcome == 'over':
            total_lo = int(math.floor(ou_line)) + 1  # 修C2(2026-07-30): quarter line(2.75/3.75)用 floor+1 与 under 分支对称, 避免误剔合法 3/4 球比分, 且与 _poisson_over_prob(>ou) 口径一致
            ou_branch = 'over'
            notes.append(f"OU {ou_line} 大球 ⇒ 总球≥{total_lo}")
        elif ou_outcome == 'under':
            total_hi = int(math.floor(ou_line))
            ou_branch = 'under'
            notes.append(f"OU {ou_line} 小球 ⇒ 总球≤{total_hi}")
        else:
            # 软界: 线 ±1 作为合理带
            total_lo = max(0, int(math.floor(ou_line)) - 1)
            total_hi = int(math.ceil(ou_line + 0.5)) + 1
            notes.append(f"OU {ou_line} 未定方向 ⇒ 总球带 [{total_lo},{total_hi}]")
        # 盘型比对法 — 与模型概率对账 (铁律: 禁止"线>2.75就吐大球倾向"硬套; 方向以模型概率为准)
        if ou_line > 2.75:
            line_label = "开大(>2.75)"
        elif ou_line < 2.25:
            line_label = "开浅(<2.25)"
        else:
            line_label = f"中性({ou_line})"
        model_over = _poisson_over_prob(poisson_matrix, ou_line)
        if model_over is None:
            notes.append(f"大小球盘型比对: {line_label} ⇒ 线型参考(无模型概率, 方向以模型为准)")
        elif model_over >= 0.5:
            notes.append(f"大小球盘型比对: {line_label} ⇒ 模型P(over)={model_over:.0%}, 倾向大球(与模型一致)")
        else:
            notes.append(f"大小球盘型比对: {line_label} ⇒ 模型P(over)={model_over:.0%}, 倾向Under(以模型为准)")
        # 联赛属性 (附加参考, 不覆盖模型结论)
        lg = (league or '').lower()
        if lg in BIG_LEAGUES:
            notes.append("联赛属性: 大球联赛 ⇒ 参考偏 over")
        elif lg in SMALL_LEAGUES:
            notes.append("联赛属性: 小球联赛 ⇒ 参考偏 under")

    # ── 4. in-play 硬约束 ──
    H0 = A0 = 0
    if live_score is not None:
        H0, A0 = int(live_score[0]), int(live_score[1])
        notes.append(f"in-play: 当前 {H0}-{A0}" + (f" @ {elapsed}'" if elapsed else ""))
        if ou_line is not None and (H0 + A0) > ou_line:
            notes.append("半大全小: 当前总球已超 OU 线 ⇒ 全场难再大(剩余时间偏小球)")
        if elapsed and elapsed >= 45 and (H0 + A0) < ou_line:
            note = "半小全大: 上半未超盘 ⇒ 下半常击穿初盘(偏大)"
            if model_over is not None and model_over < 0.5:
                note += f"; 但模型P(over)仅{model_over:.0%}→以Under为准"
            notes.append(note)

    # ── 5. CS 隐含概率 ──
    cs_probs = _cs_implied_probs(cs_odds) if cs_odds else {}

    # ── 6. 候选生成 (约束满足) ──
    for hg in range(H0, MAX_GOAL + 1):
        for ag in range(A0, MAX_GOAL + 1):
            total = hg + ag
            if total < total_lo or total > total_hi:
                continue
            margin = hg - ag
            # 胜负方约束
            if winner == 'home' and margin <= 0:
                continue
            if winner == 'away' and margin >= 0:
                continue
            if winner == 'draw' and margin != 0:
                continue
            # 平局敏感: 允许平局候选即使 winner 非 draw (避免漏 1-1)
            if winner != 'draw' and margin == 0:
                if not draw_lean:
                    continue
            # AH 约束: 仅当 AH 让球方 == 1X2 胜方(市场自洽)时硬性穿盘;
            # 矛盾时(盘口错位/跨庄)降级为软提示, 不裁剪候选, 避免误杀合法比分
            if ah_fav == winner:
                if ah_fav == 'home' and margin < ah_thr:
                    continue
                if ah_fav == 'away' and (-margin) < ah_thr:
                    continue
            # 记录
            reason_parts = []
            if winner != 'draw':
                reason_parts.append(f"胜方={winner}")
            if ah_fav:
                reason_parts.append(f"净胜≥{ah_thr}" if (ah_fav == 'home') == (margin >= 0) else "穿盘")
            if ou_branch == 'over':
                reason_parts.append(f"总球≥{total_lo}")
            elif ou_branch == 'under':
                reason_parts.append(f"总球≤{total_hi}")
            sc = f"{hg}-{ag}"
            cp = cs_probs.get(sc)
            pp = None
            if poisson_matrix is not None:
                try:
                    pp = float(poisson_matrix[hg, ag])
                except Exception:
                    pp = None
            candidates.append({
                'score': sc, 'hg': hg, 'ag': ag,
                'reason': ';'.join(reason_parts) or '市场自洽',
                'cs_prob': cp, 'poisson': pp,
            })

    # ── 7. 口诀·AH-OU 差1球 → 上盘+小球对冲 ──
    if ah_line is not None and ou_line is not None and ah_fav and ah_consistent:
        diff = ou_line - (abs(ah_line) if ah_fav == 'home' else abs(ah_line))
        # 当 OU - |AH| ≈ 1.0 (强队让深盘+大小球仅高1球) → 上盘赢盘+小球对冲
        if abs(diff - 1.0) < 0.35 and ah_fav == 'home':
            hedge_h = ah_thr          # 主穿盘净胜
            hedge_a = 0
            hedge_total = min(total_hi, hedge_h)  # 小球 ⇒ 总球≈净胜
            if hedge_total >= hedge_h:
                sc = f"{hedge_total}-{hedge_a}"
                if not any(c['score'] == sc for c in candidates):
                    candidates.append({
                        'score': sc, 'hg': hedge_total, 'ag': hedge_a,
                        'reason': '口诀·上盘+小球对冲(AH与OU差1球)',
                        'cs_prob': cs_probs.get(sc), 'poisson': None,
                    })
                notes.append(f"口诀: AH与OU差≈1球 ⇒ 上盘+小球对冲, 重点 {sc}")

    # ── 8. 排序 (CS 优先, Poisson 辅助) ──
    for c in candidates:
            cp = c['cs_prob'] or 0.0
            pp = c['poisson'] or 0.0
            if cp > 0 and pp > 0:
                c['blend'] = 0.7 * cp + 0.3 * pp
            elif cp > 0:
                c['blend'] = cp
            elif pp > 0:
                c['blend'] = pp
            else:
                # 无概率信息: 启发式 — 贴近 OU 线总球 & AH 穿盘净胜
                exp_total = ou_line if ou_line is not None else 2.5
                exp_margin = ah_thr if ah_fav else 1
                c['blend'] = 1.0 - 0.08 * abs(total - exp_total) - 0.05 * abs(abs(margin) - exp_margin)
    candidates.sort(key=lambda c: c['blend'], reverse=True)
    ranked = [c['score'] for c in candidates]

    return {
        'candidates': candidates,
        'ranked': ranked,
        'winner': winner,
        'ah_favorite': ah_fav,
        'ou_branch': ou_branch,
        'notes': notes,
        'method': 'market-structure',
        'cs_coverage': f"{len(cs_probs)} 个具体比分有CS赔率" if cs_probs else "无CS赔率(纯约束/Poisson)",
    }


# ── 便捷: 从 events.db odds_snapshots 聚合结构直接三角定位 ──
def triangulate_from_structure(struct: Dict[str, Any],
                                cs_odds: Optional[Dict[str, float]] = None,
                                league: Optional[str] = None,
                                live_score=None, elapsed=None,
                                poisson_matrix=None) -> Dict[str, Any]:
    """struct = analyze_all._aggregate_snapshot 的输出:
       {全场独赢:[{name,odds}...], 全场让球:[{name,odds}...], 全场大小:[{name,odds}...]}
    """
    h = d = a = None
    if '全场独赢' in struct:
        for it in struct['全场独赢']:
            if it['name'] == '主': h = it['odds']
            elif it['name'] == '平': d = it['odds']
            elif it['name'] == '客': a = it['odds']
    ou_line = ou_over = ou_under = None
    if '全场大小' in struct:
        for it in struct['全场大小']:
            try: ou_line = float(it['name'])
            except: pass
            # 大小球 odds 在此结构里 name=line, 没有 over/under 区分; 交由调用方传 ou_outcome
    ah_line = None
    if '全场让球' in struct:
        for it in struct['全场让球']:
            try: ah_line = float(it['name'])
            except: pass
    return triangulate(ou_line=ou_line, ah_line=ah_line, h=h, d=d, a=a,
                       cs_odds=cs_odds, league=league, live_score=live_score,
                       elapsed=elapsed, poisson_matrix=poisson_matrix)


if __name__ == '__main__':
    # 自测: 用户举例 — 大小2.5 + 主让1.5 + 主"输0.5" ⇒ 主只赢1球 ⇒ 2-1
    print("=== 自测: OU2.5大球 + 主让1.5 + 主赢1球(输0.5) ===")
    r = triangulate(ou_line=2.5, ou_outcome='over', ah_line=-1.5,
                    h=1.8, d=3.4, a=4.2, live_score=(1, 0), elapsed=70)
    print("winner:", r['winner'], "| ah_fav:", r['ah_favorite'])
    print("top5:", r['ranked'][:5])
    print("notes:", r['notes'])
    print()
    print("=== 自测: AH2.75 + OU3.75 差1球 ⇒ 上盘+小球对冲 ===")
    r2 = triangulate(ou_line=3.75, ah_line=-2.75, h=1.3, d=5.0, a=9.0)
    print("top5:", r2['ranked'][:5])
    print("notes:", r2['notes'])
