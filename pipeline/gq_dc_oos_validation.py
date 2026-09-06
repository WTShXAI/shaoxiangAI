"""
gq_dc_oos_validation.py — GQ-DC 样本外(OOS)诚实验证

背景:
  gq_dc_model.py 报出 corr(H)=+0.231 是 **in-sample** 指标:
    n_train=583 场 / 486 队 => 974 个自由参数 vs 583 个观测
    参数量 > 样本量 => 必然过拟合, in-sample corr 无意义
  接入生产前必须做时间切分 OOS 验证。

方法:
  1. 时间切分: 前 70% 训练, 后 30% 测试 (严格无泄漏)
  2. 只评估测试集中「双方球队都在训练集出现过 >= min_matches 场」的比赛
  3. 三个基线并排 (铁律: 命中率对外必须并排 naive 基线):
       - naive_uniform : 均匀 1/3
       - naive_homeadv : 全局主胜/平/客胜频率 (无球队信息)
       - market_devig  : GQ 开盘赔率去水 (庄家基准, 最强 baseline)
       - gq_dc         : 待检模型
  4. 指标: LogLoss / RPS / corr(p, actual) / 分箱单调性
     (铁律: 禁用单次 split+accuracy, 用概率类指标)
  5. 多种 min_matches 网格, 找参数量/样本量的可行区间

输出: data/pricing_template/gq_dc_oos_report.json
"""
import os
import sys
import json
import sqlite3
import logging
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GQ_DB = os.path.join(ROOT, "data", "events.db")
OUT_DIR = os.path.join(ROOT, "data", "pricing_template")
os.makedirs(OUT_DIR, exist_ok=True)

from gq_dc_model import GQDCFairPricing  # noqa: E402
from pipeline.clean_outcomes import load_clean_outcomes  # noqa: E402


# ---------------------------------------------------------------- 数据加载
def load_gq_with_odds() -> pd.DataFrame:
    """加载 GQ 比分 + 开盘 1X2 (用于 market baseline).

    ⚠ 2026-08-05: 必须走 clean_outcomes。直接读 match_outcomes 会吃进
    228 场电子盘(8分钟制虚拟赛) + 1307 场采集截断(半场缺失导致终场比分
    被冻结, 均进球 1.95 / 零封 29%)。DC 模型对进球分布极敏感, 用污染数据
    拟合出的 λ 会系统性偏低, 让 in-sample 相关性虚高。
    """
    df, rep = load_clean_outcomes(return_report=True)
    log.info(f"[数据] 赛果清洗: {rep['before']['n']} -> {rep['after']['n']} 场 "
             f"(剔虚拟 {rep['dropped_virtual']} / 剔截断 {rep['dropped_truncated']}), "
             f"均进球 {rep['before']['avg_goals']} -> {rep['after']['avg_goals']}")
    df = df[df["home"].notna() & df["away"].notna()].copy()
    df = df.rename(columns={"score_home": "home_score", "score_away": "away_score"})
    for c in ("op_1x2_h", "op_1x2_d", "op_1x2_a", "league", "kickoff"):
        if c not in df.columns:
            df[c] = np.nan
    df["date"] = pd.to_datetime(df["kickoff"], errors="coerce")
    df["home_team"] = df["home"]
    df["away_team"] = df["away"]
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    df["actual"] = np.where(
        df["home_score"] > df["away_score"], "home",
        np.where(df["home_score"] == df["away_score"], "draw", "away"),
    )
    return df


# ---------------------------------------------------------------- 指标
def logloss(probs: np.ndarray, y_idx: np.ndarray) -> float:
    """probs: (n,3) [H,D,A]; y_idx: (n,) 0/1/2"""
    p = np.clip(probs[np.arange(len(y_idx)), y_idx], 1e-12, 1.0)
    return float(-np.mean(np.log(p)))


def rps(probs: np.ndarray, y_idx: np.ndarray) -> float:
    """Ranked Probability Score (序数: H < D < A), 越小越好."""
    n = len(y_idx)
    onehot = np.zeros((n, 3))
    onehot[np.arange(n), y_idx] = 1.0
    cum_p = np.cumsum(probs, axis=1)
    cum_o = np.cumsum(onehot, axis=1)
    return float(np.mean(np.sum((cum_p - cum_o) ** 2, axis=1) / 2.0))


def bin_monotonic(p: np.ndarray, y: np.ndarray, n_bins: int = 5):
    """返回分箱 (预测均值, 实际频率, n), 以及是否单调递增."""
    try:
        edges = np.quantile(p, np.linspace(0, 1, n_bins + 1))
        edges = np.unique(edges)
        if len(edges) < 3:
            return [], False
        idx = np.clip(np.digitize(p, edges[1:-1]), 0, len(edges) - 2)
        rows, actuals = [], []
        for b in range(len(edges) - 1):
            m = idx == b
            if m.sum() < 5:
                continue
            rows.append({
                "bin": b,
                "n": int(m.sum()),
                "pred": round(float(p[m].mean()), 4),
                "actual": round(float(y[m].mean()), 4),
            })
            actuals.append(float(y[m].mean()))
        mono = all(actuals[i] <= actuals[i + 1] for i in range(len(actuals) - 1)) if len(actuals) >= 3 else False
        return rows, mono
    except Exception:
        return [], False


def eval_probs(name: str, probs: np.ndarray, y_idx: np.ndarray) -> dict:
    out = {
        "model": name,
        "n": int(len(y_idx)),
        "logloss": round(logloss(probs, y_idx), 4),
        "rps": round(rps(probs, y_idx), 4),
    }
    for i, lbl in enumerate(["H", "D", "A"]):
        y = (y_idx == i).astype(float)
        p = probs[:, i]
        # 常数预测无相关性可言
        corr = float(np.corrcoef(p, y)[0, 1]) if p.std() > 1e-9 else 0.0
        rows, mono = bin_monotonic(p, y)
        out[f"corr_{lbl}"] = round(corr, 4)
        out[f"mono_{lbl}"] = mono
        out[f"bins_{lbl}"] = rows
    return out


# ---------------------------------------------------------------- OOS 主流程
def run_oos(min_matches: int, ridge: float, train_frac: float = 0.70) -> dict | None:
    df = load_gq_with_odds()
    cut = int(len(df) * train_frac)
    train_df = df.iloc[:cut].copy()
    test_df = df.iloc[cut:].copy()

    log.info(f"\n{'=' * 62}")
    log.info(f"OOS 验证: min_matches={min_matches}, ridge={ridge}")
    log.info(f"{'=' * 62}")
    log.info(f"训练集: {len(train_df)} 场 ({train_df['date'].min().date()} ~ {train_df['date'].max().date()})")
    log.info(f"测试集: {len(test_df)} 场 ({test_df['date'].min().date()} ~ {test_df['date'].max().date()})")

    # train_years 设大避免二次窗口截断 (我们已用时间切分)
    dc = GQDCFairPricing(min_matches=min_matches, train_years=99.0, ridge=ridge)
    if not dc.fit(train_df):
        log.warning("  拟合失败, 跳过")
        return None

    n_teams = dc.model["n_teams"]
    n_train = dc.model["n_train"]
    n_params = 2 * n_teams + 2
    ratio = n_train / max(1, n_params)
    log.info(f"  拟合: {n_teams} 队, {n_train} 场, 参数 {n_params}, 样本/参数 = {ratio:.2f}")

    known = set(getattr(dc, "teams", None) or dc.model.get("attack", {}).keys())
    if not known:
        log.warning("  无法确定已知球队集合")
        return None

    # 测试集: 只保留双方都在训练集见过的比赛 (否则 DC 无参数可用)
    mask = test_df["home_team"].isin(known) & test_df["away_team"].isin(known)
    cov = float(mask.mean())
    te = test_df[mask].copy()
    log.info(f"  测试集覆盖: {len(te)}/{len(test_df)} = {cov:.1%} (双方球队均在训练集)")
    if len(te) < 50:
        log.warning(f"  测试样本不足 ({len(te)} < 50), 结论不可靠")
        if len(te) < 20:
            return None

    y_map = {"home": 0, "draw": 1, "away": 2}
    y_idx = te["actual"].map(y_map).values

    # --- GQ-DC 预测
    dc_probs = []
    for _, r in te.iterrows():
        fp = dc.fair_probabilities(r["home_team"], r["away_team"])
        dc_probs.append([fp["p_h"], fp["p_d"], fp["p_a"]])
    dc_probs = np.array(dc_probs)

    # --- baseline 1: uniform
    uni = np.full((len(te), 3), 1.0 / 3.0)

    # --- baseline 2: 训练集全局频率 (无球队信息)
    tr_freq = train_df["actual"].value_counts(normalize=True)
    base = np.array([tr_freq.get("home", 1 / 3), tr_freq.get("draw", 1 / 3), tr_freq.get("away", 1 / 3)])
    base = base / base.sum()
    freq = np.tile(base, (len(te), 1))

    results = [
        eval_probs("gq_dc", dc_probs, y_idx),
        eval_probs("naive_uniform", uni, y_idx),
        eval_probs("naive_globalfreq", freq, y_idx),
    ]

    # --- baseline 3: 市场去水 (最强 baseline, 只在有开盘赔率的子集上)
    om = te[["op_1x2_h", "op_1x2_d", "op_1x2_a"]].notna().all(axis=1)
    n_mkt = int(om.sum())
    market_block = None
    if n_mkt >= 30:
        sub = te[om]
        inv = np.array([
            1.0 / sub["op_1x2_h"].values,
            1.0 / sub["op_1x2_d"].values,
            1.0 / sub["op_1x2_a"].values,
        ]).T
        mkt = inv / inv.sum(axis=1, keepdims=True)
        y_sub = sub["actual"].map(y_map).values
        dc_sub = dc_probs[om.values]
        market_block = {
            "n_with_market": n_mkt,
            "market_devig": eval_probs("market_devig", mkt, y_sub),
            "gq_dc_same_subset": eval_probs("gq_dc(同子集)", dc_sub, y_sub),
        }

    return {
        "config": {
            "min_matches": min_matches, "ridge": ridge, "train_frac": train_frac,
            "n_teams": n_teams, "n_train_matches": n_train,
            "n_params": n_params, "sample_per_param": round(ratio, 3),
            "test_coverage": round(cov, 4),
        },
        "oos": results,
        "vs_market": market_block,
    }


def main():
    log.info("=" * 62)
    log.info("GQ-DC 样本外(OOS)诚实验证")
    log.info("  in-sample corr=+0.231 存疑 -> 时间切分重新检验")
    log.info("=" * 62)

    all_res = []
    for mm, rg in [(3, 0.1), (5, 0.1), (8, 0.2), (12, 0.3)]:
        r = run_oos(mm, rg)
        if r is None:
            continue
        all_res.append(r)
        # 打印对比
        log.info(f"\n  {'模型':<20s} {'n':>5s} {'LogLoss':>9s} {'RPS':>8s} {'corr_H':>8s} {'corr_A':>8s}")
        log.info(f"  {'-' * 62}")
        for e in r["oos"]:
            log.info(f"  {e['model']:<20s} {e['n']:>5d} {e['logloss']:>9.4f} {e['rps']:>8.4f} "
                     f"{e['corr_H']:>+8.4f} {e['corr_A']:>+8.4f}")
        if r["vs_market"]:
            vm = r["vs_market"]
            log.info(f"\n  -- 与市场基准对比 (n={vm['n_with_market']}, 有开盘赔率) --")
            for k in ["market_devig", "gq_dc_same_subset"]:
                e = vm[k]
                log.info(f"  {e['model']:<20s} {e['n']:>5d} {e['logloss']:>9.4f} {e['rps']:>8.4f} "
                         f"{e['corr_H']:>+8.4f} {e['corr_A']:>+8.4f}")

    # 判定
    log.info("\n" + "=" * 62)
    log.info("判定")
    log.info("=" * 62)

    verdict = {"pass": False, "reason": "", "best_config": None}
    best = None
    for r in all_res:
        dc_e = next((e for e in r["oos"] if e["model"] == "gq_dc"), None)
        fq_e = next((e for e in r["oos"] if e["model"] == "naive_globalfreq"), None)
        if not dc_e or not fq_e:
            continue
        beats_naive = dc_e["logloss"] < fq_e["logloss"]
        beats_mkt = None
        if r["vs_market"]:
            beats_mkt = r["vs_market"]["gq_dc_same_subset"]["logloss"] < r["vs_market"]["market_devig"]["logloss"]
        score = fq_e["logloss"] - dc_e["logloss"]  # 越大越好
        log.info(f"  min_matches={r['config']['min_matches']:>2d} "
                 f"样本/参数={r['config']['sample_per_param']:>5.2f} "
                 f"LogLoss {dc_e['logloss']:.4f} vs naive {fq_e['logloss']:.4f} "
                 f"-> {'胜' if beats_naive else '负'} naive"
                 + (f", {'胜' if beats_mkt else '负'} 市场" if beats_mkt is not None else ""))
        if beats_naive and (best is None or score > best[0]):
            best = (score, r)

    if best:
        verdict["pass"] = True
        verdict["reason"] = f"OOS LogLoss 优于 naive 基线 {best[0]:.4f}"
        verdict["best_config"] = best[1]["config"]
        log.info(f"\n  [PASS] GQ-DC 有样本外增益 (最佳 min_matches={best[1]['config']['min_matches']})")
        log.info(f"         -> 可作为「公平概率诊断字段」接入 (不覆盖操盘手锚定)")
    else:
        verdict["reason"] = "所有配置的 OOS LogLoss 均不优于 naive 全局频率基线"
        log.info("\n  [FAIL] GQ-DC 无样本外增益")
        log.info("         根因: GQ 数据仅 2026 年单季, 球队样本过稀 (参数量 > 样本量)")
        log.info("         -> 不接入 unified_predictor。")
        log.info("         注: 曾以「模板偏差路径 ROI +12.32%」作为替代方案, 该结论已于")
        log.info("             2026-08-05 被证伪 —— 那是采集截断比分造成的假象。用干净数据")
        log.info("             重跑后 HIGH risk 小球 ROI = -14.00% (n=13), 四关全挂。")
        log.info("             目前无生产可用的 OU 策略, 前端只做信息标注。")

    out = {"verdict": verdict, "runs": all_res}
    path = os.path.join(OUT_DIR, "gq_dc_oos_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"\n报告已存: {path}")
    return out


if __name__ == "__main__":
    main()
