"""
live_score_conditional — 滚球神器·状态感知剩余破蛋模型 (v1.0)
=================================================================
设计目标: 用户要"根据滚球实时比分来预测"的模型。现有 probe_core 的 full/half
分支只靠盘口去水隐含概率(黑箱), 不显式消费 current_score, 也没有"剩余时间"维度。
本模块把破蛋概率升级为**状态感知剩余破蛋概率**:

  已知 比分 X-Y(已实现 G=X+Y)、当前分钟 t、剩余时间比例 r=(90-t)/90、
  实时盘口去水隐含总球 λ_live(已含已实现比分) →
    剩余期望进球  λ_rem = λ_live - G            (减法口径, λ_live 已含已实现)
    还需 N = ceil(line - G) 球破蛋
    剩余破蛋概率  P = 1 - PoissonCDF(N-1; λ_rem)

理论依据(已用 events.db 2483 场半场+终比分严格验证, scripts/validate_poisson_conditional.py):
  - 进球过程近似时间齐次泊松(下半/上半均值比 1.062 ≈ 1.0)
  - 赛前 λ_pre 时间缩放 λ_pre*0.5 预测下半场, 实际/预测比率 1.043(极准)
    → 间接证明运行时减法口径 λ_rem=λ_live-G 正确
  - 唯一偏差: 实际进球略高于纯独立泊松(动量/正相关, ht=2+桶下半场均值1.60>ht=0桶1.25)
    → 用经验乘子 CALIB_GAMMA=1.04 吸收(保守, 不夸大 edge)

符合抗诱导铁律(2026-08-18):
  - 全部输入为去水不变量(λ_live 来自去水隐含总球, 非原始赔率值)
  - 标签=实际进球数(非庄家盘口方向), 学"实际-市场"残差
  - 闭式泊松(非 ML 黑箱), 可审计; 验证用分组对比而非单次切分
  - 经验乘子来自历史分箱验证, 非临时捏造

运行时无需训练(joblib); 校准参数 CALIB_GAMMA 由 validate 脚本得出, 定期复核。
"""
import math
from typing import Optional, Dict, Any

# 经验校准乘子: GQ 2483 场验证实际/泊松预测比率 1.043, 取 1.04 保守吸收动量偏差
CALIB_GAMMA = 1.04

# 剩余期望上限保护(避免 λ_live 异常大导致荒谬高概率)
LAMBDA_REM_CAP = 6.0

# 时间物理上限(2026-08-21 新增护栏): 无论 λ 来自赛前盘、滚球盘还是异常插值,
# "剩余时间内的期望进球" 都不能超过 该时段最高进球率 × 剩余时间比例。
# 取 90 分钟 5.0 球 / 45 分钟 2.5 球 作宽松上限(高于任何真实联赛均值 ~2.6/90,
# 只截断荒谬值, 不干扰正常区间)。
# 实测必要性: 塔什干棉农 35.6′ 0-0 半场仅剩 9min, 插值异常给出 λ_rem=2.0 →
# P(半场破蛋)=0.875, 而庄家盘口 P(over@0.5)=0.389。护栏把 λ_rem 压到 ≤0.55。
MAX_GOALS_PER_FULL = 5.0
MAX_GOALS_PER_HALF = 2.5

# 开盘锚定权重(2026-08-20 接 OU 开盘模型): 滚球 OU 判断用"已验证可靠的开盘隐含总球"
# 给实时(噪声大、易因进球过激降线)去水总球做贝叶斯式锚定。W=1.0 = 开盘/临场等权;
# 实证开盘隐含P(大) AUC 0.603 是可靠单一基准, 故开盘作为稳定先验拉住临场过激。
OPENING_ANCHOR_WEIGHT = 1.0

# 杯赛/决赛联赛关键词(赛事类型感知用)
CUP_KEYWORDS = ['杯', '决赛', 'Cup', 'Final', '天皇', '联赛杯']
# 杯赛场景剩余期望进球收缩因子。
# 依据 GQ 2473 场(半场+终比分)验证: 半场胶着(ht<=1)的杯赛全场均值 1.69,
# 显著低于非杯赛 1.97 —— 杯赛(尤其强弱悬殊/强强保守对话)半场胶着后全场更少进球,
# 而盘口 λ_live 仍锚定"全场总球预期"未充分反映"可能0-0收场", 会高估剩余破蛋。
# 收缩 0.88 吸收该系统性高估(保守, 不夸大 edge)。
CUP_LAMBDA_FACTOR = 0.88


def is_cup_league(league: Optional[str]) -> bool:
    """赛事类型感知: 是否为杯赛/决赛场景。"""
    if not league:
        return False
    return any(k.lower() in str(league).lower() for k in CUP_KEYWORDS)


def _parse_score(score: str):
    """'h-a' 或 'h:a' -> (h, a) int; 非法回 (0,0)。
    支持连字符 '-' 与冒号 ':' 两种分隔符(probe_core 传入为 'h-a' 格式)。"""
    import re
    try:
        if not score:
            return 0, 0
        s = str(score).replace('：', ':').strip()
        parts = re.split(r'[:\-]', s)
        if len(parts) < 2:
            return 0, 0
        return int(float(parts[0])), int(float(parts[1]))
    except Exception:
        return 0, 0


def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    if k < 0:
        return 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def poisson_sf(k: int, lam: float) -> float:
    """P(X >= k)。k<=0 返回 1.0。"""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0 if k > 0 else 1.0
    s = 0.0
    for i in range(k):
        s += poisson_pmf(i, lam)
    return max(0.0, min(1.0, 1.0 - s))


def remaining_break_prob(
    implied_total_live: Optional[float],
    current_score: str = '0-0',
    current_minute: int = 0,
    line: float = 2.5,
    is_halftime: bool = False,
    segment: str = 'full',
    league: Optional[str] = None,
    opening_implied_total: Optional[float] = None,
    lambda_source: str = 'live',
) -> Dict[str, Any]:
    """
    状态感知剩余破蛋概率(泊松条件化)。

    参数:
      implied_total_live: 实时盘口去水隐含总球 λ_live(全场或半场)。None 时返回 None(上游回退)。
      current_score:      当前比分 'h-a' 或 'h:a'
      current_minute:     当前分钟(全场视角; 半场分支传 0-45)
      line:               OU 目标线(半场线或全场线)
      is_halftime:        是否中场休息
      segment:            'full'(全场剩余) | 'half'(半场剩余)
      league:             联赛名(用于赛事类型感知: 杯赛/决赛收缩剩余进球预期)
      opening_implied_total: 开盘去水隐含总球(已验证可靠, 见 analysis/ou_calibration_by_line.md)。
                             提供时, 对 λ 做贝叶斯式锚定(拉住临场因进球过激降线)。
                             ⚠ 混合前必须先把开盘锚转成"剩余口径"(×rem_ratio), 否则晚期
                             会把 90 分钟视角的开盘 λ 当剩余期望, 系统性高估破蛋。
      lambda_source:         'live'   = implied_total_live 取自**滚球**快照(庄家已随时间降线,
                                        λ 自带时间衰减) → 减法口径 λ_rem = λ - G
                             'prematch' = 取自**赛前**快照(无任何时间衰减) → 必须按剩余时间
                                        比例缩放 λ_rem = λ × rem_ratio (且不再减 G, 泊松独立)
                             ⚠ 2026-08-21 重大修复: 原实现算出 rem_ratio 却从未使用(死变量),
                             导致 89' 0-0 与 5' 0-0 破蛋概率完全相同(实测均 0.5074)。

    返回:
      {prob, lambda_rem, need_balls, gamma, method, detail, cup, cup_warning,
       lambda_full_eff, opening_total, anchor_used}
    """
    if implied_total_live is None or implied_total_live <= 0:
        return {'prob': None, 'lambda_rem': None, 'need_balls': None,
                'gamma': CALIB_GAMMA, 'method': 'no_lambda', 'detail': '无实时去水隐含总球',
                'cup': False, 'cup_warning': None,
                'lambda_full_eff': None, 'opening_total': opening_implied_total,
                'anchor_used': False}

    cup = is_cup_league(league)

    h, a = _parse_score(current_score)
    G = h + a  # 已实现总球

    # 半场已结束 -> 破蛋状态由 half_signal 决定, 本模型不重复(返回 settled)
    if segment == 'half' and (is_halftime or current_minute >= 45):
        return {'prob': None, 'lambda_rem': 0.0, 'need_balls': 0,
                'gamma': CALIB_GAMMA, 'method': 'halftime_settled',
                'detail': '半场已结束, 破蛋状态由 half_signal 决定',
                'cup': cup, 'cup_warning': None,
                'lambda_full_eff': implied_total_live, 'opening_total': opening_implied_total,
                'anchor_used': False}

    # 剩余时间比例
    # 修(2026-08-20 特罗姆瑟U19审计): 原模型用名义90分钟, 忽略补时。实证该场
    # 打到104min+(U19超长补时), 75'后名义剩15min实际剩~29min → 破蛋概率被低估。
    # 补时先验(分钟): U19/青年/后备联赛 +10(体能崩盘+裁判宽松), 杯赛+5, 顶级联赛+4。
    def _extra_time_prior(lg):
        if lg and any(k in str(lg) for k in ('U19', 'U21', 'U23', '青年', '后备', '预备')):
            return 10.0
        if is_cup_league(lg):
            return 5.0
        return 4.0
    if segment == 'half':
        rem_ratio = max(0.0, (45 - current_minute) / 45.0)
    else:
        _eff_total = 90.0 + (_extra_time_prior(league) if current_minute >= 60 else 0.0)
        rem_ratio = max(0.0, (_eff_total - current_minute) / _eff_total)

    # ── λ_rem 口径分流(2026-08-21 重大修复) ──────────────────────────────────
    # 原实现无论 λ 来源一律走"减法口径 λ-G", 且 rem_ratio 是死变量 → 时间窗口
    # 完全不进模型。实测 89' 0-0 与 5' 0-0 破蛋概率同为 0.5074, 属致命高估。
    # 正确口径取决于 λ 是否已含时间衰减:
    #   滚球快照: 庄家已随剩余时间降线(85' 0-0 会把全场线压到 0.5), λ 自带衰减 → λ-G
    #   赛前快照: λ 是开赛时的全场期望, 不含任何衰减 → 必须 ×rem_ratio
    # 本机 events.db 实证: 3,022,205 条快照中仅 15.03% 带 minute_at>0, 且其中 97.7%
    # 是 45/90 占位污染 → 绝大多数 probe 读到的其实是赛前盘, 必须走 prematch 分支。
    anchor_used = False
    lambda_full = implied_total_live
    _is_prematch = (str(lambda_source).lower() == 'prematch')

    if _is_prematch:
        # 赛前 λ: 先做开盘锚(同为全场口径, 单位一致), 再按剩余时间比例缩放。
        # 不再减 G —— λ×rem_ratio 已是"剩余时间段的期望进球", 与已进球数独立(泊松)。
        if opening_implied_total is not None and opening_implied_total > 0:
            lambda_full = (implied_total_live + OPENING_ANCHOR_WEIGHT * opening_implied_total) \
                          / (1.0 + OPENING_ANCHOR_WEIGHT)
            anchor_used = True
        lambda_rem = max(0.0, lambda_full * rem_ratio)
        _rem_method = f'prematch λ×rem_ratio({rem_ratio:.3f})'
    else:
        # 滚球 λ: 减法口径取剩余期望。开盘锚须先转"剩余口径"再混合, 否则把 90 分钟
        # 视角的 open_T 当剩余期望, 晚期严重高估(旧 bug 的第二重来源)。
        live_rem = max(0.0, implied_total_live - G)
        if opening_implied_total is not None and opening_implied_total > 0:
            open_rem = opening_implied_total * rem_ratio
            lambda_rem = (live_rem + OPENING_ANCHOR_WEIGHT * open_rem) \
                         / (1.0 + OPENING_ANCHOR_WEIGHT)
            anchor_used = True
            _rem_method = (f'live(λ-G={live_rem:.2f}) ⊕ open_rem'
                           f'({opening_implied_total:.2f}×{rem_ratio:.3f}={open_rem:.2f})')
        else:
            lambda_rem = live_rem
            _rem_method = f'live λ-G={live_rem:.2f}'
    # 杯赛场景收缩(半场胶着后全场更少进球, 盘口 λ_live 偏高会高估剩余破蛋)
    if cup:
        lambda_rem *= CUP_LAMBDA_FACTOR
    lambda_rem = min(lambda_rem, LAMBDA_REM_CAP)

    # ── 时间物理上限护栏(2026-08-21) ──────────────────────────────────────────
    # 兜住所有上游异常(盘口插值失真 / 滚球线未及时更新 / λ 口径误判):
    # 剩余期望进球 ≤ 该时段最高进球率 × 剩余时间比例。
    _time_cap = (MAX_GOALS_PER_HALF if segment == 'half' else MAX_GOALS_PER_FULL) * rem_ratio
    _capped_by_time = lambda_rem > _time_cap
    if _capped_by_time:
        lambda_rem = _time_cap

    # 还需多少球破蛋: 总球需 > line, 即 >= floor(line)+1 (line 非整数时 ceil(line+eps))
    need_balls = math.ceil(line - G - 1e-9)
    if need_balls <= 0:
        return {'prob': 1.0, 'lambda_rem': lambda_rem, 'need_balls': 0,
                'gamma': CALIB_GAMMA, 'method': 'already_broken',
                'detail': f'已实现 {G} 球 >= 线 {line}, 已破蛋',
                'cup': cup, 'cup_warning': None,
                'lambda_full_eff': round(float(lambda_full), 4),
                'opening_total': round(float(opening_implied_total), 4) if opening_implied_total else None,
                'anchor_used': anchor_used,
                'rem_ratio': round(float(rem_ratio), 4),
                'lambda_source': lambda_source}

    # 剩余破蛋概率 = P(剩余进球 >= need_balls)
    # 经验校准: λ_rem 乘 gamma 吸收动量偏差(实际略高于纯独立泊松)
    lam_cal = lambda_rem * CALIB_GAMMA
    p = poisson_sf(need_balls, lam_cal)

    return {
        'prob': round(float(p), 4),
        'lambda_rem': round(float(lambda_rem), 4),
        'need_balls': need_balls,
        'gamma': CALIB_GAMMA,
        'method': 'poisson_conditional',
        'cup': cup,
        'cup_warning': '杯赛场景·剩余进球已收缩(模型置信打折)' if cup else None,
        'lambda_full_eff': round(float(lambda_full), 4),
        'opening_total': round(float(opening_implied_total), 4) if opening_implied_total else None,
        'anchor_used': anchor_used,
        'rem_ratio': round(float(rem_ratio), 4),
        'lambda_source': lambda_source,
        'time_capped': _capped_by_time,
        'detail': (f'λ={implied_total_live:.2f}({"赛前盘" if _is_prematch else "滚球盘"})'
                   f' {current_minute}′ 剩余占比={rem_ratio:.3f} G={G} '
                   f'{_rem_method} {"×杯缩0.88 " if cup else ""}'
                   f'{f"[时间护栏截断→{_time_cap:.2f}] " if _capped_by_time else ""}'
                   f'λ_rem={lambda_rem:.2f} ×γ={CALIB_GAMMA}→{lam_cal:.2f} '
                   f'需{need_balls}球 P(剩余破蛋)={p:.3f}'),
    }


def fixed_fulltime_over_prob(
    opening_implied_total: Optional[float],
    live_implied_total: Optional[float],
    current_score: str = '0-0',
    line: float = 2.5,
    league: Optional[str] = None,
) -> dict:
    """固定全场大`line`概率 —— 不随比赛时间衰减 (2026-08-23 用户硬性要求)。

    问题背景: 原 `remaining_break_prob` 用 `rem_ratio` 把全场 λ 按剩余时间比例收缩,
    再算 P(剩余进球 >= 还需球数)。结果是越到比赛末段, "全场大 2.5" 概率越低;
    最后时刻进球、庄家升盘时, 工具无法正确反映"原始大 2.5 是否被打穿"。

    本函数改用**完整比赛(90min)隐含总球期望 λ_full**(非剩余口径, 绝不 ×rem_ratio)
    评估 "最终总球 > line" 的概率:
      - 阈值永远固定在 line(默认 2.5, 即用户要的"固定全场大 2.5")。
      - **时间无关**: 以 λ_full 作为"剩余进球能力"的速率, 不因剩余时间窗口收缩而失真。
        只在"已实现进球 G"变化时更新(need = ceil(line - G))。
      - 单调正确: G 越大(已进越多)→ need 越小 → 大 line 概率越高(2-0 > 1-0 > 0-0);
        G >= line 时直接 = 1.0(已打穿)。
      - late goal / 庄家升盘 → G 增加 → 概率正确抬升, 稳定回答"原始大 line 是否打穿"。

    ⚠ 建模口径(避免常见陷阱): 速率用 λ_full 本身, **不**用 λ_full - G。
    若用 λ_full - G 当剩余速率, 会出现"2-0 比 0-0 的大2.5概率更低"的反直觉
    非单调(因减去 G 比减去 need 更快), 且隐含时间比例假设。固定评估的语义是
    "按赛前/开盘对全场总球的确定性预期 λ_full 来判大 line", 故速率恒为 λ_full,
    仅 need 随 G 收缩。

    λ_full 来源优先级:
      1. opening_implied_total —— 开盘去水隐含总球(庄家赛前对全场总球的确定性定价,
         不随滚盘移动, 是最稳定的"原始大 2.5"参考锚)。
      2. live_implied_total —— 当前全场隐含总球(本身即"最终总球"预期, 非剩余口径)。
      两者均为全场口径, 单位一致, 直接当 λ_full 用, 不乘 rem_ratio。

    返回: {prob, signal, direction, line, need_balls, lambda_full, method, cup, cup_warning, detail}
    """
    cup = is_cup_league(league)
    h, a = _parse_score(current_score)
    G = h + a  # 已实现总球

    lambda_full = None
    src = None
    if opening_implied_total is not None and opening_implied_total > 0:
        lambda_full = float(opening_implied_total)
        src = 'opening'
    elif live_implied_total is not None and live_implied_total > 0:
        lambda_full = float(live_implied_total)
        src = 'live'
    if lambda_full is None or lambda_full <= 0:
        return {
            'prob': None, 'signal': 'NO_EDGE', 'direction': None,
            'line': line, 'need_balls': None, 'lambda_full': None,
            'method': 'no_lambda', 'cup': cup, 'cup_warning': None,
            'detail': '无全场隐含总球(开盘/实时均缺失), 固定大{}无法评估'.format(line),
        }

    # 还需多少球打穿大 line: 总球需 > line
    need_balls = math.ceil(line - G - 1e-9)
    if need_balls <= 0:
        return {
            'prob': 1.0, 'signal': 'ALREADY_OVER', 'direction': None,
            'line': line, 'need_balls': 0, 'lambda_full': round(lambda_full, 4),
            'method': 'fixed_fulltime_already', 'cup': cup, 'cup_warning': None,
            'detail': '已实现 {} 球 >= 线 {}, 固定大{}已打穿'.format(G, line, line),
        }

    # 剩余进球能力速率 = λ_full(时间无关, 不随剩余时间/已实现G收缩);
    # 仅 need 随已实现进球 G 收缩。杯赛场景收缩 + 物理上限护栏。
    rem_exp = float(lambda_full)
    if cup:
        rem_exp *= CUP_LAMBDA_FACTOR
    rem_exp = min(rem_exp, LAMBDA_REM_CAP)
    lam_cal = rem_exp * CALIB_GAMMA
    p = poisson_sf(need_balls, lam_cal)

    if p >= 0.65:
        signal = 'STRONG_OVER'
    elif p <= 0.35:
        signal = 'STRONG_UNDER'
    else:
        signal = 'NO_EDGE'
    direction = 'OVER' if p >= 0.5 else 'UNDER'

    return {
        'prob': round(float(p), 4),
        'signal': signal,
        'direction': direction,
        'line': line,
        'need_balls': need_balls,
        'lambda_full': round(lambda_full, 4),
        'method': 'fixed_fulltime',
        'cup': cup,
        'cup_warning': '杯赛场景·剩余进球已收缩(模型置信打折)' if cup else None,
        'detail': (
            f'固定全场大{line}: λ_full={lambda_full:.2f}({src}) G={G} '
            f'剩余期望={rem_exp:.2f}×γ={CALIB_GAMMA}→{lam_cal:.2f} '
            f'需{need_balls}球 P(大{line})={p:.3f}'
        ),
    }


def expected_remaining_goals(
    implied_total_live: Optional[float],
    current_score: str = '0-0',
    league: Optional[str] = None,
) -> Optional[float]:
    """剩余期望进球数(λ_rem), 供前端展示'预计还能进 N 球'。杯赛场景收缩。"""
    if implied_total_live is None or implied_total_live <= 0:
        return None
    h, a = _parse_score(current_score)
    rem = max(0.0, implied_total_live - (h + a))
    if is_cup_league(league):
        rem *= CUP_LAMBDA_FACTOR
    return rem


# ── 自检 ──
if __name__ == '__main__':
    print("=== live_score_conditional 自检 ===")
    cases = [
        # (λ_live, score, minute, line, is_ht, seg, 说明)
        (3.2, '0-2', 60, 2.5, False, 'full', '0-2后60min, OU2.5, °-2还需1球'),
        (2.5, '0-0', 20, 2.5, False, 'full', '0-0后20min, OU2.5, 还需3球'),
        (1.8, '0-0', 30, 1.5, False, 'half', '半场0-0 30min, 半场OU1.5, 还需2球'),
        (1.2, '1-0', 40, 1.5, False, 'half', '半场1-0 40min, 半场OU1.5已破'),
        (3.0, '0-0', 45, 2.5, True, 'full', '中场0-0, 全场OU2.5'),
        (4.5, '3-1', 85, 2.5, False, 'full', '3-1后85min, 已破'),
    ]
    for lam, sc, mi, ln, ht, seg, note in cases:
        r = remaining_break_prob(lam, sc, mi, ln, ht, seg)
        print(f"  [{note}] -> {r}")
