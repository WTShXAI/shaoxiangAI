"""
metrics.py — 评估指标纯函数实现 (无第三方依赖)

所有函数接受 numpy-free 的纯 Python 结构 (list / tuple), 便于在 managed 环境直接跑。
约定:
  - probs: 长度为 3 的 [p_home, p_draw, p_away], 已归一化到 sum=1
  - outcome: 单字符 'H' / 'D' / 'A'
"""
from bisect import bisect_right


def devig(oh: float, od: float, oa: float):
    """对 1X2 赔率去水, 返回归一化隐含概率 [p_h, p_d, p_a]。

    overround = 1/oh + 1/od + 1/oa ; 去水 = (1/odds) / overround。
    若任一赔率无效 (<1.01) 返回 None。
    """
    if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
        return None
    rh, rd, ra = 1.0 / oh, 1.0 / od, 1.0 / oa
    s = rh + rd + ra
    if s <= 0:
        return None
    return [rh / s, rd / s, ra / s]


def _outcome_idx(o: str):
    return {"H": 0, "D": 1, "A": 2}.get(o)


def log_loss(probs_list, outcomes):
    """多分类对数损失。probs_list: List[[ph,pd,pa]]; outcomes: List['H'/'D'/'A']。"""
    if not probs_list:
        return None
    tot = 0.0
    for p, o in zip(probs_list, outcomes):
        i = _outcome_idx(o)
        if i is None:
            continue
        pp = max(p[i], 1e-12)
        tot += -__import__("math").log(pp)
    return tot / len(probs_list)


def brier_score(probs_list, outcomes):
    """Brier 分数 = mean( sum((p - y_onehot)^2) ), 越小越好, 范围 [0,2]。"""
    if not probs_list:
        return None
    tot = 0.0
    n = 0
    for p, o in zip(probs_list, outcomes):
        i = _outcome_idx(o)
        if i is None:
            continue
        y = [0.0, 0.0, 0.0]
        y[i] = 1.0
        tot += sum((p[k] - y[k]) ** 2 for k in range(3))
        n += 1
    return tot / n


def accuracy(probs_list, outcomes):
    """argmax 预测与赛果一致的比例。"""
    if not probs_list:
        return None
    ok = 0
    n = 0
    for p, o in zip(probs_list, outcomes):
        i = _outcome_idx(o)
        if i is None:
            continue
        if p.index(max(p)) == i:
            ok += 1
        n += 1
    return ok / n


def _auc_binary(y_true, y_score):
    """单组 0/1 标签的 AUC (Mann-Whitney U / 秩)。"""
    pos = []
    neg = []
    for t, s in zip(y_true, y_score):
        (pos if t == 1 else neg).append(s)
    n_pos, n_neg = len(pos), len(neg)
    if n_pos == 0 or n_neg == 0:
        return None
    pos_s = sorted(pos)
    concord = 0
    for ns in neg:
        # 正样本得分严格大于负样本得分的对数
        concord += n_pos - bisect_right(pos_s, ns)
    return concord / (n_pos * n_neg)


def auc_ovr(probs_list, outcomes):
    """One-vs-Rest AUC: 每个类别对"其余"算 AUC, 返回 {H,D,A, macro}。"""
    if not probs_list:
        return None
    classes = ["H", "D", "A"]
    per = {}
    for ci, c in enumerate(classes):
        y = [1 if o == c else 0 for o in outcomes]
        s = [p[ci] for p in probs_list]
        per[c] = _auc_binary(y, s)
    valid = [v for v in per.values() if v is not None]
    per["macro"] = sum(valid) / len(valid) if valid else None
    return per


def calibration_curve(probs_list, outcomes, n_bins=10):
    """按模型对"主胜"的预测概率分箱, 返回每箱的 平均预测 / 实际主胜频率 / 样本数。

    用于校准曲线: 若平均预测 0.6 的箱实际频率≈0.6 则校准良好。
    这里以主胜(H)为基准类别; 三类别全量校准可分别调用。
    """
    if not probs_list:
        return []
    edges = [i / n_bins for i in range(n_bins + 1)]
    bins = [{"pred_sum": 0.0, "obs": 0, "n": 0, "lo": edges[i], "hi": edges[i + 1]}
            for i in range(n_bins)]
    for p, o in zip(probs_list, outcomes):
        ph = p[0]
        bi = min(int(ph * n_bins), n_bins - 1)
        b = bins[bi]
        b["pred_sum"] += ph
        b["obs"] += 1 if o == "H" else 0
        b["n"] += 1
    out = []
    for b in bins:
        if b["n"] == 0:
            continue
        out.append({
            "bin": f"{b['lo']:.1f}-{b['hi']:.1f}",
            "mean_pred": round(b["pred_sum"] / b["n"], 4),
            "obs_freq": round(b["obs"] / b["n"], 4),
            "n": b["n"],
            "gap": round(b["obs"] / b["n"] - b["pred_sum"] / b["n"], 4),
        })
    return out


def sharpe_ratio(returns, risk_free=0.0):
    """returns: List[float] 每注收益序列; 返回年化等价 Sharpe (无期限假设则按序列标准差)。"""
    if not returns or len(returns) < 2:
        return None
    import math
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return (mean - risk_free) / sd


def max_drawdown(equity_curve, as_pct=True):
    """最大回撤(全链路资金曲线核心指标)。

    equity_curve: List[float] 按时间升序的资金序列。
    返回最大峰谷跌幅: as_pct=True 时为单位化比例(0~1, 相对峰值);
    as_pct=False 时为绝对金额。空/单点返回 0.0。
    """
    if not equity_curve or len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for x in equity_curve:
        if x > peak:
            peak = x
        dd = peak - x
        if dd > max_dd:
            max_dd = dd
    return (max_dd / peak) if as_pct else max_dd


def simulate_strategy(probs_list, implied_list, outcomes, value_thresh=0.02,
                      kelly_frac=0.25, flat_stake=1.0):
    """模拟 Value Bet 下注策略, 返回 ROI 与 Sharpe。

    probs_list: 模型概率 (我们的预测); implied_list: 市场去水隐含概率。
    当 max(model_prob - implied_prob) > value_thresh 时, 对对应结果下注:
      - 赔率用 1/implied (恢复原始赔率) 的近似: 用 implied 反推赔率 odds=1/implied_raw
        实际我们用 probs 对应的原始赔率由调用方提供不在此算; 这里以 implied 反推赔率。
      - 平注: 每注 flat_stake
      - Kelly: stake = kelly_frac * (model_p*odds - 1)/(odds-1)
    命中收益 = stake*(odds-1); 未中 = -stake。
    返回 {roi_flat, sharpe_flat, roi_kelly, sharpe_kelly, n_bets, hit_rate}
    """
    flat_ret, kelly_ret = [], []
    n_bets = 0
    hits = 0
    for p, imp, o in zip(probs_list, implied_list, outcomes):
        i = _outcome_idx(o)
        if i is None:
            continue
        edge = p[i] - imp[i]
        if edge <= value_thresh:
            continue
        # 由隐含概率反推该结果赔率 (去水前近似)
        odds = 1.0 / imp[i] if imp[i] > 0 else 0
        if odds <= 1.0:
            continue
        win = (i == _outcome_idx(o))
        n_bets += 1
        if win:
            hits += 1
        # 平注
        if win:
            flat_ret.append(flat_stake * (odds - 1))
        else:
            flat_ret.append(-flat_stake)
        # Kelly (封顶 kelly_frac)
        kelly_full = (p[i] * odds - 1) / (odds - 1) if odds > 1 else 0
        kf = max(0.0, min(kelly_frac, kelly_full))
        stake = kf  # 单位筹码比例
        if win:
            kelly_ret.append(stake * (odds - 1))
        else:
            kelly_ret.append(-stake)
    def _roi(rets, unit):
        if not rets:
            return None
        return sum(rets) / (len(rets) * unit)
    return {
        "n_bets": n_bets,
        "hit_rate": round(hits / n_bets, 4) if n_bets else None,
        "roi_flat": _roi(flat_ret, flat_stake),
        "sharpe_flat": sharpe_ratio(flat_ret),
        "roi_kelly": _roi(kelly_ret, kelly_frac) if kelly_ret else None,
        "sharpe_kelly": sharpe_ratio(kelly_ret),
    }
