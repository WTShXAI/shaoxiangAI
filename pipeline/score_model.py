"""
哨响AI — 真比分概率模型 (生产级: OIP 赔率隐含 Poisson)
========================================================
为什么是 OIP 而不是 Dixon-Coles:
  - 跨届 OOS 验证 (wc_all_matches 106场含赔率+比分):
      OIP  OOS logloss=2.83  Top1=16.7%  Top3=50.0%  H-D-A=66.7%
      DC   OOS logloss=3.85  Top1= 0.0%  Top3=22.2%  H-D-A=50.0%  (过拟合, rho撞0.5上界)
  - 106场/50队 对 DC 的 100+ 参数样本太小, 即便 reg=5.0 仍崩.
  - OIP 逐场解 λ_h/λ_a, 无训练 → 天然 OOS 安全, 等价于庄家隐含分布的比分管线.

接口:
  predict_score(home, away, oh, od, oa, max_goal=8) -> dict
    {lh, la, p_h, p_d, p_a, matrix(np), top_scores:[(i,j,p),...]}

作者: 赵统筹(总工) | 2026-07-07
"""
import math
import numpy as np
from scipy.optimize import root

MAX_GOAL_DEFAULT = 8

def deoverround(oh, od, oa):
    """1X2 去抽水 → 隐含 P(H),P(D),P(A)"""
    o = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/o, (1.0/od)/o, (1.0/oa)/o

def _poisson_marginal(lh, la, maxg):
    ph = pd_ = pa = 0.0
    for i in range(maxg+1):
        pi = math.exp(-lh)*lh**i/math.factorial(i)
        for j in range(maxg+1):
            pj = math.exp(-la)*la**j/math.factorial(j)
            p = pi*pj
            if i > j: ph += p
            elif i == j: pd_ += p
            else: pa += p
    return ph, pd_, pa

def score_matrix(lh, la, maxg=MAX_GOAL_DEFAULT):
    col = [math.exp(-lh)*lh**i/math.factorial(i) for i in range(maxg+1)]
    row = [math.exp(-la)*la**j/math.factorial(j) for j in range(maxg+1)]
    return np.outer(col, row)

def _dc_correct(M, rho, maxg):
    """
    Dixon-Coles 低比分依赖修正 (仅调整比分矩阵 M, 不影响 1X2:
    1X2 由 deoverround 直接得出, 与 M 无关)。
    对 i,j ∈ {0,1} 的格子乘 (1 - rho*i*j) 后整体重归一化。
    rho<0 → 抑制低比分式联合概率(真实足球负相关); rho>0 → 抬升。
    默认不调用 (rho=0 → 独立Poisson, 与旧行为一致)。
    """
    M = M.copy().astype(float)
    for i in range(min(2, maxg + 1)):
        for j in range(min(2, maxg + 1)):
            M[i][j] *= (1.0 - rho * i * j)
    s = M.sum()
    return M / s if s > 0 else M

def solve_oip(ph, pd, pa, maxg=MAX_GOAL_DEFAULT):
    """数值解 λ_h,λ_a 使独立Poisson边缘匹配P(H/D/A)"""
    def eq(x):
        lh, la = x
        if lh <= 0 or la <= 0:
            return [1e6, 1e6]
        eh, ed, ea = _poisson_marginal(lh, la, maxg)
        return [eh - ph, ed - pd]
    sol = root(eq, [1.3, 1.1], method='hybr')
    if sol.success and sol.x[0] > 0 and sol.x[1] > 0:
        return float(sol.x[0]), float(sol.x[1])
    best, bestr = (1.3, 1.1), 1e9
    for lh in np.arange(0.3, 4.5, 0.1):
        for la in np.arange(0.3, 4.5, 0.1):
            eh, ed, ea = _poisson_marginal(lh, la, maxg)
            r = (eh - ph)**2 + (ed - pd)**2
            if r < bestr:
                bestr, best = r, (lh, la)
    return best

def predict_score(home, away, oh, od, oa, max_goal=MAX_GOAL_DEFAULT, rho=0.0, goal_scale: float = 1.0):
    """
    赔率隐含 Poisson 比分预测.
    返回 dict: lh, la(期望进球), p_h/p_d/p_a(胜平负), matrix(比分概率矩阵),
               top_scores(前5可能比分 [(i,j,p),...]), home/away(回声)

    rho: Dixon-Coles 低比分依赖项 (默认0=独立Poisson)。仅修正比分矩阵 M,
         不影响 1X2 (p_h/p_d/p_a 来自 deoverround)。
    goal_scale: λ 全局缩放(校准用)。WC-313校准得 1.35 修正OIP低估总进球(~0.48球),
               使波胆top3命中率 29.7%→34.4%(+4.7pp)。仅WC生效; 经验收缩α/Dixon-Colesρ会拉低top3, 不采用。
    """
    ph, pd, pa = deoverround(oh, od, oa)
    lh, la = solve_oip(ph, pd, pa, max_goal)
    lh, la = lh * goal_scale, la * goal_scale   # WC校准: 修正OIP低估总进球
    M = score_matrix(lh, la, max_goal)
    M = M / M.sum()
    if rho:
        M = _dc_correct(M, rho, max_goal)
    flat = M.flatten()
    order = np.argsort(-flat)[:5]
    top = [(int(divmod(k, max_goal+1)[0]), int(divmod(k, max_goal+1)[1]), round(float(flat[k]), 4))
           for k in order]
    return dict(home=home, away=away, lh=round(lh, 3), la=round(la, 3),
                p_h=round(ph, 4), p_d=round(pd, 4), p_a=round(pa, 4),
                matrix=M, top_scores=top)
