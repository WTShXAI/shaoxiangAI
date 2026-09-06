"""Dixon-Coles 队力公平概率模型 (哨响AI / H1 检测器核心).

稳定参数化 (标准 DC):
  lambda_h = exp(intercept + attack_i - defense_j + home_adv)
  lambda_a = exp(intercept + attack_j - defense_i)
  sum(attack)=0, sum(defense)=0  (最后队由前 n-1 决定, 硬约束)
  rho = 低分相关 (tau 修正), 限定 (-0.5, 0.5)

predict(home,away) -> (p_home, p_draw, p_away) 经 tau 修正的得分矩阵归一化.
按联赛分别拟合形成 bank; 未知队/联赛 -> None 由调用方回退.

铁律: 仅用赛前可得信息; 本模块不做未来泄露 (训练/预测切分由调用方控制).
"""
from __future__ import annotations
import numpy as np
from scipy.optimize import minimize
from scipy.special import gammaln


class DixonColes:
    def __init__(self, max_goals: int = 12):
        self.max_goals = max_goals
        self.teams: list[str] = []
        self.idx: dict[str, int] = {}
        self.intercept: float = 0.0
        self.a: np.ndarray | None = None   # attack (sum 0)
        self.d: np.ndarray | None = None   # defense (sum 0, 正=好防守)
        self.home_adv: float = 0.0
        self.rho: float = 0.0
        self.fitted_: bool = False
        self.n_train: int = 0

    # ---- 拟合 ----
    def fit(self, matches, maxiter: int = 400) -> bool:
        M = [(h, a, int(hg), int(ag)) for h, a, hg, ag in matches
             if hg is not None and ag is not None and hg >= 0 and ag >= 0]
        if len(M) < 10:
            self.fitted_ = False
            return False
        teams = sorted({t for m in M for t in (m[0], m[1])})
        self.teams = teams
        self.idx = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        H = np.array([self.idx[m[0]] for m in M])
        A = np.array([self.idx[m[1]] for m in M])
        HG = np.array([m[2] for m in M], dtype=float)
        AG = np.array([m[3] for m in M], dtype=float)
        # 参考队(teams[0]) attack=defense=0 固定; 其余自由. 尺度由参考队锚定, 不中心化.
        k = 2 * (n - 1) + 3
        p0 = np.zeros(k)
        p0[-3] = 0.25   # intercept 初值
        p0[-2] = 0.20   # home_adv 初值
        bounds = [(-2.0, 2.0)] * (2 * (n - 1)) + [(0.05, 0.6), (0.0, 0.6), (-0.3, 0.3)]
        res = minimize(self._nll, p0, method="L-BFGS-B", bounds=bounds,
                       args=(H, A, HG, AG, n), options={"maxiter": maxiter})
        self._unpack(res.x, n)
        self.fitted_ = True
        self.n_train = len(M)
        return True

    def _params(self, p, n):
        at = np.zeros(n); df = np.zeros(n)
        at[1:] = p[:n - 1]; df[1:] = p[n - 1:2 * (n - 1)]   # teams[0]=0 锚定
        intercept = p[2 * (n - 1)]; home_adv = p[2 * (n - 1) + 1]; rho = p[2 * (n - 1) + 2]
        return at, df, intercept, home_adv, rho

    def _nll(self, p, H, A, HG, AG, n):
        at, df, intercept, home_adv, rho = self._params(p, n)
        lam = np.exp(intercept + at[H] - df[A] + home_adv)
        mu = np.exp(intercept + at[A] - df[H])
        ll = (-lam + HG * np.log(lam) - gammaln(HG + 1)
              - mu + AG * np.log(mu) - gammaln(AG + 1))
        t = np.ones_like(lam)
        m00 = (HG == 0) & (AG == 0); t[m00] = 1 - rho * lam[m00] * mu[m00]
        m01 = (HG == 0) & (AG == 1); t[m01] = 1 + rho * lam[m01]
        m10 = (HG == 1) & (AG == 0); t[m10] = 1 + rho * mu[m10]
        m11 = (HG == 1) & (AG == 1); t[m11] = 1 - rho * lam[m11] * mu[m11]
        t = np.clip(t, 1e-6, None)
        ll = ll + np.log(t)
        return -np.sum(ll)

    def _unpack(self, p, n):
        at, df, intercept, home_adv, rho = self._params(p, n)
        self.a = at; self.d = df
        self.intercept = float(intercept)
        self.home_adv = float(home_adv)
        self.rho = float(rho)

    # ---- 预测 ----
    def predict(self, home, away):
        if not self.fitted_ or home not in self.idx or away not in self.idx:
            return None
        i = self.idx[home]; j = self.idx[away]
        lam = np.exp(self.intercept + self.a[i] - self.d[j] + self.home_adv)
        mu = np.exp(self.intercept + self.a[j] - self.d[i])
        G = self.max_goals + 1
        xs = np.arange(G)
        pl = np.exp(-lam + xs * np.log(lam) - gammaln(xs + 1))
        pu = np.exp(-mu + xs * np.log(mu) - gammaln(xs + 1))
        M = np.outer(pl, pu)
        for x in range(min(2, G)):
            for y in range(min(2, G)):
                if x == 0 and y == 0:
                    fac = 1 - self.rho * lam * mu
                elif x == 0 and y == 1:
                    fac = 1 + self.rho * lam
                elif x == 1 and y == 0:
                    fac = 1 + self.rho * mu
                elif x == 1 and y == 1:
                    fac = 1 - self.rho * lam * mu
                else:
                    fac = 1.0
                M[x, y] *= fac
        s = M.sum()
        if s <= 0:
            return None
        M = M / s
        ph = M[np.triu_indices(G, 1)].sum()
        pd = np.diag(M).sum()
        pa = M[np.tril_indices(G, -1)].sum()
        return float(ph), float(pd), float(pa)


def implied_from_odds(oh, od, oa):
    """去水隐含概率 (庄家 margin-stripped)."""
    inv = 1.0 / oh + 1.0 / od + 1.0 / oa
    if inv <= 0:
        return None
    return (1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv


def build_league_bank(matches_by_league: dict, verbose: bool = False) -> dict:
    bank = {}
    for lg, ms in matches_by_league.items():
        dc = DixonColes()
        ok = dc.fit(ms)
        if ok:
            bank[lg] = dc
            if verbose:
                print(f"  [DC] {lg}: n={dc.n_train} teams={len(dc.teams)} "
                      f"intercept={dc.intercept:.3f} home_adv={dc.home_adv:.3f} rho={dc.rho:.3f}")
        elif verbose:
            print(f"  [DC] {lg}: SKIP (样本<10)")
    return bank


def predict_1x2(bank: dict, league: str, home: str, away: str):
    dc = bank.get(league)
    if dc is None:
        return None
    return dc.predict(home, away)
