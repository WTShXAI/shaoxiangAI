"""H1 favorite-被低估 检测器 (哨响AI / 接 reverse_odds_engine).

核心命题 (涛哥洞察): 庄家常在 1X2 开盘隐藏强队真实战力 -> 开盘 favorite 隐含概率
  低于"公平模型"应有的概率 -> 赛前可识别并下注 favorite @开盘 = +EV.

方法:
- 公平概率 = Dixon-Coles 队力模型 (analysis/dixon_coles.py) 按联赛拟合.
- 偏差 dev = fair_fav - open_implied_fav (开盘去水隐含概率). dev>buffer => favorite 被低估.
- 回测用 historical_matches 做 out-of-time 验证 (训练<2023, 测试 2023+), 不看收盘,
  直接验证 "用公平模型替代收盘也能复现 +EV".

铁律: 仅赛前信息; 不开盘价不预测; 未知队回退联赛均值公平概率.

==== 实测结论 (2026-08-13, out-of-time 39371 场测试) ====
H1 检测器 (DC公平 vs 开盘去水) 本身可运行, 但信号 **不具 +EV**:
  无脑买favorite@开盘 ROI=-4.62%; DC-undervalued 子集 ROI=-7.39%, AUC=0.38 (反预测).
对照探针 "favorite开盘->收盘变短":
  @开盘下注(用收盘选, 不可部署) ROI=+6.31% (看似成立, 实为 look-ahead 假象);
  @收盘下注(可部署真实版) ROI=-3.08% -> 边缘消失.
=> 本模块 out-of-time 回测实证: 单庄 1X2 开盘/收盘已被有效校准, DC-undervalue 信号反预测(ROI -7.39%), 赛前无稳定 +EV.
   真+EV 来自跨庄/跨市场软线价差 (见 pipeline/leyu_value_signal.py); 单庄是否含edge须逐场用开盘去水P/临场漂移/联赛方差判定, 不预设。
   本模块保留为研究/护栏工具 (DC-undervalue 实测反预测 -> 可作"勿下"过滤); 其 flag 不得作为真钱下注依据.
"""
from __future__ import annotations
import os, re, sys, json, sqlite3, pickle
import numpy as np
# 自包含: 无论从 analysis/ 还是项目根(引擎挂载)导入, 都能找到同目录的 dixon_coles
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from dixon_coles import DixonColes, implied_from_odds


# ---------------- 联赛名归一化 ----------------
# historical_matches.league_name 跨年份命名不一致:
#   训练(<2023) 干净名 如 '英超'/'意甲'; 测试(>=2023) 带赛季+轮次 如 '22/23英超第18轮'.
# 归一化: 去头部赛季前缀(22/23,2023/2024,22,2023) + 去尾部轮次/阶段(第N轮,分组赛...),
# 使训练/测试落到同一 canonical key, bank 才能命中.
_SEASON = re.compile(r"^[\d/]+")
_PHASE = re.compile(r"(第.*$|分组赛|小组赛|淘汰赛|半决赛|决赛|排位赛|外围赛|资格赛|附加赛|季后赛|常规赛)")
def normalize_league(lg: str) -> str:
    if not lg:
        return ""
    s = lg.strip()
    s = _SEASON.sub("", s)        # 去 '22/23' / '2023' 等赛季前缀
    s = _PHASE.sub("", s)         # 去 '第18轮' / '分组赛' 等尾部阶段
    return s.strip()

DB_HIST = "D:/Architecture/data/football_data.db"
BANK_CACHE = "D:/Architecture/analysis/_h1_bank.pkl"
TRAIN_CUTOFF = "2023-01-01"


# ---------------- 数据 ----------------
def load_historical(open_cutoff: str = "2013-01-01", train_cutoff: str = TRAIN_CUTOFF):
    """返回 (train_rows, test_rows); 每行 (league,home,away,hg,ag,oh,od,oa)."""
    f = sqlite3.connect(DB_HIST)
    rows = f.execute(f"""
        SELECT league_name,home_team,away_team,home_score,away_score,
               open_home_odds,open_draw_odds,open_away_odds,
               close_home_odds,close_draw_odds,close_away_odds,match_date
        FROM historical_matches
        WHERE open_home_odds>1.01 AND open_draw_odds>1.01 AND open_away_odds>1.01
          AND home_score IS NOT NULL AND away_score IS NOT NULL
          AND match_date >= ?
        ORDER BY match_date
    """, (open_cutoff,)).fetchall()
    f.close()
    def mk(r):
        return dict(league=normalize_league(r[0]), home=r[1], away=r[2], hg=r[3], ag=r[4],
                   oh=r[5], od=r[6], oa=r[7], ch=r[8], cd=r[9], ca=r[10], date=r[11])
    train = [mk(r) for r in rows if r[11] < train_cutoff]
    test = [mk(r) for r in rows if r[11] >= train_cutoff]
    return train, test


# ---------------- bank ----------------
def build_bank(train_rows, verbose: bool = False, min_matches: int = 200):
    by_league: dict[str, list] = {}
    for r in train_rows:
        lg = r["league"]
        # 归一化后联赛名不再含轮次/赛季; 仅跳过空名或极短碎片
        if not lg or len(lg) < 2:
            continue
        by_league.setdefault(lg, []).append((r["home"], r["away"], r["hg"], r["ag"]))
    bank = {}
    for lg, ms in by_league.items():
        if len(ms) < min_matches:
            continue
        dc = DixonColes()
        if dc.fit(ms):
            bank[lg] = dc
            if verbose:
                print(f"  [DC] {lg}: n={dc.n_train} teams={len(dc.teams)} "
                      f"int={dc.intercept:.3f} hadv={dc.home_adv:.3f} rho={dc.rho:+.3f}")
    return bank


def generic_fair(dc: DixonColes):
    """联赛均值公平概率 (两平均队, a=0,d=0): lam=exp(int+hadv), mu=exp(int)."""
    if dc is None or not dc.fitted_:
        return None
    lam = np.exp(dc.intercept + dc.home_adv)
    mu = np.exp(dc.intercept)
    G = dc.max_goals + 1
    xs = np.arange(G)
    pl = np.exp(-lam + xs * np.log(lam) - (xs * np.log(xs + 1e-12) - xs + np.where(xs == 0, 0, 0)))  # 近似
    # 用精确 poisson
    from scipy.special import gammaln
    pl = np.exp(-lam + xs * np.log(lam) - gammaln(xs + 1))
    pu = np.exp(-mu + xs * np.log(mu) - gammaln(xs + 1))
    M = np.outer(pl, pu)
    s = M.sum(); M = M / s
    ph = M[np.triu_indices(G, 1)].sum()
    pd = np.diag(M).sum()
    pa = M[np.tril_indices(G, -1)].sum()
    return float(ph), float(pd), float(pa)


def get_fair(bank, league, home, away):
    dc = bank.get(league)
    if dc is not None:
        pf = dc.predict(home, away)
        if pf is not None:
            return pf, "team"
    # 回退: 联赛均值 (generic)
    if dc is not None:
        gf = generic_fair(dc)
        if gf is not None:
            return gf, "league_avg"
    return None, "none"


# ---------------- 检测 ----------------
def detect(bank, league, home, away, oh, od, oa, buffer: float = 0.015):
    io = implied_from_odds(oh, od, oa)
    if io is None:
        return None
    fair, src = get_fair(bank, league, home, away)
    if fair is None:
        return None
    # favorite = 开盘最短赔率方
    odds = [oh, od, oa]; imp = list(io)
    fi = int(np.argmin(odds))
    fav_open = odds[fi]; fav_impl = imp[fi]; fair_fav = fair[fi]
    edge = fair_fav - fav_impl
    undervalued = edge > buffer
    # 凯利 (参考注码, 铁律建议实盘 FLAT)
    p = fair_fav
    ev = p * fav_open - 1.0
    if ev > 0:
        kelly = (p * fav_open - 1.0) / fav_open
    else:
        kelly = 0.0
    kelly = float(min(max(kelly, 0.0), 0.15))
    label = {0: "home", 1: "draw", 2: "away"}[fi]
    return dict(league=league, home=home, away=away,
                open_odds=(oh, od, oa), open_implied=imp,
                fair=fair, fair_source=src,
                favorite=label, favorite_open=fav_open,
                fair_fav=float(fair_fav), open_implied_fav=float(fav_impl),
                edge=float(edge), undervalued=undervalued,
                ev=float(ev), kelly=kelly,
                recommendation=("BET_FAVORITE" if undervalued else "PASS"))


# ---------------- 回测 (out-of-time, 不看收盘) ----------------
def backtest(test_rows, bank, buffer: float = 0.015, verbose: bool = True):
    stake_unit = 1.0
    naive = dict(n=0, wins=0, stake=0.0, ret=0.0)
    und = dict(n=0, wins=0, stake=0.0, ret=0.0)
    edges = []; wins = []   # 用于 AUC
    skipped = 0
    for r in test_rows:
        d = detect(bank, r["league"], r["home"], r["away"], r["oh"], r["od"], r["oa"], buffer)
        if d is None:
            skipped += 1
            continue
        fi = {"home": 0, "draw": 1, "away": 2}[d["favorite"]]
        won = (r["hg"] > r["ag"]) if fi == 0 else ((r["hg"] < r["ag"]) if fi == 2 else (r["hg"] == r["ag"]))
        o = d["open_odds"][fi]
        # 无脑买 favorite @开盘
        naive["n"] += 1; naive["stake"] += stake_unit
        naive["wins"] += (1 if won else 0)
        naive["ret"] += (o if won else 0.0)
        # 仅 undervalued
        if d["undervalued"]:
            und["n"] += 1; und["stake"] += stake_unit
            und["wins"] += (1 if won else 0)
            und["ret"] += (o if won else 0.0)
        edges.append(d["edge"]); wins.append(1 if won else 0)
    def roi(d):
        return (d["ret"] / d["stake"] - 1.0) if d["stake"] > 0 else None
    def wr(d):
        return d["wins"] / d["n"] if d["n"] else None
    auc = _auc(edges, wins)
    if verbose:
        print(f"[回测] 测试场数={len(test_rows)} 跳过(无fair)={skipped}")
        nwr = wr(naive); nroi = roi(naive)
        uwr = wr(und); uroi = roi(und)
        print(f"  无脑买favorite@开盘: n={naive['n']} "
              f"胜率={(nwr or 0):.4f} ROI={(nroi or 0):+.4f}")
        print(f"  [H1] undervalued(fair>open+buffer): n={und['n']} "
              f"胜率={(uwr or 0):.4f} ROI={(uroi or 0):+.4f}")
        print(f"  edge->favorite胜 AUC={auc:.4f}")
    return dict(test_n=len(test_rows), skipped=skipped, naive=naive, under=und,
                naive_roi=roi(naive), naive_wr=wr(naive),
                und_roi=roi(und), und_wr=wr(und), auc=auc, buffer=buffer)


def _auc(scores, labels):
    """手动 ROC-AUC (二分类)."""
    pairs = sorted(zip(scores, labels), key=lambda x: x[0])
    n_pos = sum(labels); n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    rank = 0; ties = 0
    from collections import defaultdict
    # 标准方法
    s = np.array(scores); l = np.array(labels)
    order = np.argsort(s)
    ranks = np.empty(len(s)); ranks[order] = np.arange(1, len(s) + 1)
    # 处理并列
    for v in np.unique(s):
        m = s == v
        ranks[m] = ranks[m].mean()
    return float((ranks[l == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


# ---------------- 漂移信号回测 (开盘->收盘 "favorite变短", 仅赛前数据) ----------------
def backtest_drift(test_rows, verbose: bool = True):
    """探针: 先验 +4.4% 是否来自 '开盘favorite变短(开盘->收盘漂移)' 这一市场共识信号.

    仅用赛前可得数据 (开盘+收盘均在开赛前确定): favorite=开盘最短方;
    若收盘赔 < 开盘赔 (变短/市场确认) => 买 favorite@开盘.
    这是 market-consensus 信号, 非 模型vs单庄 信号.
    """
    stake = 1.0
    naive = dict(n=0, wins=0, stake=0.0, ret=0.0)
    sh = dict(n=0, wins=0, stake=0.0, ret=0.0)   # 变短(shortens)
    lg = dict(n=0, wins=0, stake=0.0, ret=0.0)   # 变长(lengthens)
    for r in test_rows:
        oh, od, oa = r["oh"], r["od"], r["oa"]
        ch, cd, ca = r["ch"], r["cd"], r["ca"]
        if not (oh > 1.01 and od > 1.01 and oa > 1.01 and ch > 1.01 and cd > 1.01 and ca > 1.01):
            continue
        odds = [oh, od, oa]; clos = [ch, cd, ca]
        fi = int(np.argmin(odds))
        fav_open = odds[fi]; fav_close = clos[fi]
        won = (r["hg"] > r["ag"]) if fi == 0 else ((r["hg"] < r["ag"]) if fi == 2 else (r["hg"] == r["ag"]))
        naive["n"] += 1; naive["stake"] += stake
        naive["wins"] += (1 if won else 0); naive["ret"] += (fav_open if won else 0.0)
        if fav_close < fav_open:       # 变短
            sh["n"] += 1; sh["stake"] += stake
            sh["wins"] += (1 if won else 0); sh["ret"] += (fav_open if won else 0.0)
        else:                           # 变长
            lg["n"] += 1; lg["stake"] += stake
            lg["wins"] += (1 if won else 0); lg["ret"] += (fav_open if won else 0.0)
    def roi(d): return (d["ret"] / d["stake"] - 1.0) if d["stake"] > 0 else None
    def wr(d): return d["wins"] / d["n"] if d["n"] else None
    if verbose:
        print(f"[漂移探针] 无脑买favorite@开盘: n={naive['n']} 胜率={(wr(naive) or 0):.4f} ROI={(roi(naive) or 0):+.4f}")
        print(f"  [信号] favorite变短(开->收): n={sh['n']} 胜率={(wr(sh) or 0):.4f} ROI={(roi(sh) or 0):+.4f}")
        print(f"  [对照] favorite变长(开->收): n={lg['n']} 胜率={(wr(lg) or 0):.4f} ROI={(roi(lg) or 0):+.4f}")
    return dict(naive=naive, shortens=sh, lengthens=lg,
                naive_roi=roi(naive), naive_wr=wr(naive),
                shortens_roi=roi(sh), shortens_wr=wr(sh),
                lengthens_roi=roi(lg), lengthens_wr=wr(lg))


# ---------------- 缓存 ----------------
def get_or_build_bank(force: bool = False, verbose: bool = False):
    if not force and os.path.exists(BANK_CACHE):
        with open(BANK_CACHE, "rb") as fh:
            return pickle.load(fh)
    train, _ = load_historical()
    bank = build_bank(train, verbose=verbose)
    with open(BANK_CACHE, "wb") as fh:
        pickle.dump(bank, fh)
    return bank


# ---------------- 接入 ReverseOddsEngine ----------------
def attach_to_engine(engine):
    """把 detect_prematch_fav_undervalue 挂到 ReverseOddsEngine (避免循环 import)."""
    from pipeline.reverse_odds_engine import ReverseOddsEngine
    _cache = {}
    def _detect(self, league, home, away, oh, od, oa, buffer=0.015):
        if "__bank__" not in _cache:
            _cache["__bank__"] = get_or_build_bank()
        return detect(_cache["__bank__"], league, home, away, oh, od, oa, buffer)
    ReverseOddsEngine.detect_prematch_fav_undervalue = _detect
    try:
        engine.detect_prematch_fav_undervalue = _detect
    except Exception:
        pass
    print("[h1] 已挂载 detect_prematch_fav_undervalue 到 ReverseOddsEngine")


if __name__ == "__main__":
    import time
    t0 = time.time()
    bank = get_or_build_bank(force=True, verbose=True)
    print(f"\nbank 构建完成: {len(bank)} 联赛, 用时 {time.time()-t0:.1f}s")
    _, test = load_historical()
    res = backtest(test, bank, buffer=0.015)
    out = {k: v for k, v in res.items() if not isinstance(v, dict)}
    out["naive"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in res["naive"].items()}
    out["under"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in res["under"].items()}
    # 漂移探针 (开盘->收盘 favorite变短, 仅赛前数据)
    drift = backtest_drift(test)
    out["drift"] = {k: (round(v, 4) if isinstance(v, float) else
                        ({kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                         if isinstance(v, dict) else v))
                    for k, v in drift.items()}
    with open("D:/Architecture/analysis/h1_backtest_result.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("\n结果已写 analysis/h1_backtest_result.json")
