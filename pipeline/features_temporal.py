"""零泄漏时间序特征引擎 (v1.0).

核心约束: 对每一场比赛, 其派生的 Elo / 滚动 form 特征, 只使用
该场比赛 *之前* 已发生的赛果. 状态机按 match_date 全局排序逐场更新,
计算完特征后才用本场结果更新状态 -> 测试场不会泄漏任何未来信息.

适用: WC(wc_xlsx_matches) 与 五大联赛(william_ht) 通用, 只需传列名.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

# 超参 (标准足球 Elo)
HOME_ADV = 60.0      # 主场优势折算 Elo 分
K = 32.0             # Elo 更新系数
INIT = 1500.0        # 初始评分
FORM_N = 5           # 滚动窗口


def _result(h: float, a: float) -> float:
    if h > a:
        return 1.0
    if h == a:
        return 0.5
    return 0.0


def _exp(eh: float, ea: float) -> float:
    """主队期望胜率 (含主场优势)."""
    return 1.0 / (1.0 + 10.0 ** ((ea - eh + HOME_ADV) / 400.0))


def build_features(
    df: pd.DataFrame,
    date_col: str = "match_date",
    home_col: str = "home_norm",
    away_col: str = "away_norm",
    hg_col: str = "hg",
    ag_col: str = "ag",
    league_col: str | None = None,
    league_val: str | None = None,
) -> pd.DataFrame:
    """返回原始 df + 时序特征列.

    特征列:
      home_elo, away_elo, elo_diff
      home_form_pts5, away_form_pts5      (近5场拿分率 0~1)
      home_form_gf5,  away_form_gf5       (近5场场均进球)
      home_form_ga5,  away_form_ga5       (近5场场均失球)
      home_form_gd5,  away_form_gd5       (近5场场均净胜球)
    """
    d = df.copy()
    if league_col and league_val is not None:
        d = d[d[league_col].astype(str).str.strip().str.contains(league_val, na=False)]
    need = [date_col, home_col, away_col, hg_col, ag_col]
    d = d.dropna(subset=need)
    d[date_col] = pd.to_datetime(d[date_col], errors="coerce")
    d = d.dropna(subset=[date_col]).sort_values(date_col).reset_index(drop=True)

    elo: dict[str, float] = {}
    form: dict[str, list] = {}  # team -> [(gf, ga), ...] 最近 FORM_N 场 (该队视角)

    home_elo_l, away_elo_l, elo_diff_l = [], [], []
    hfp, afp, hgf, agf, hga, aga, hgd, agd = ([] for _ in range(8))

    for _, r in d.iterrows():
        h, a = r[home_col], r[away_col]
        eh = elo.get(h, INIT)
        ea = elo.get(a, INIT)
        fh = form.get(h, [])
        fa = form.get(a, [])

        # ---- 特征 (更新前状态) ----
        home_elo_l.append(eh)
        away_elo_l.append(ea)
        elo_diff_l.append(eh - ea)

        def agg(fl):
            if not fl:
                return 0.5, 0.0, 0.0, 0.0  # 无历史 -> 中性
            gf = np.mean([x[0] for x in fl])
            ga = np.mean([x[1] for x in fl])
            pts = np.mean([1.0 if x[0] > x[1] else (0.5 if x[0] == x[1] else 0.0) for x in fl])
            return pts, gf, ga, gf - ga

        hp, hgf_, hga_, hgd_ = agg(fh)
        ap, agf_, aga_, agd_ = agg(fa)
        hfp.append(hp); afp.append(ap)
        hgf.append(hgf_); agf.append(agf_)
        hga.append(hga_); aga.append(aga_)
        hgd.append(hgd_); agd.append(agd_)

        # ---- 用本场结果更新状态机 ----
        hg = float(r[hg_col]); ag = float(r[ag_col])
        res_h = _result(hg, ag)
        exp_h = _exp(eh, ea)
        elo[h] = eh + K * (res_h - exp_h)
        elo[a] = ea + K * ((1.0 - res_h) - _exp(ea, eh))
        form.setdefault(h, []).append((hg, ag))
        form.setdefault(a, []).append((ag, hg))
        if len(form[h]) > FORM_N:
            form[h].pop(0)
        if len(form[a]) > FORM_N:
            form[a].pop(0)

    out = d.copy()
    out["home_elo"] = home_elo_l
    out["away_elo"] = away_elo_l
    out["elo_diff"] = elo_diff_l
    out["home_form_pts5"] = hfp
    out["away_form_pts5"] = afp
    out["home_form_gf5"] = hgf
    out["away_form_gf5"] = agf
    out["home_form_ga5"] = hga
    out["away_form_ga5"] = aga
    out["home_form_gd5"] = hgd
    out["away_form_gd5"] = agd
    return out
