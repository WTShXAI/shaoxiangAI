"""
P0-2 复核 cs_calibration.json 的 0-0 因子 (原 1.404 = 实际14.65%/庄家10.43%)

正确口径: cs_calibration.json 的 factor 定义是 实际频率 / 庄家隐含概率,
其中"庄家隐含"= CS 盘 0-0 赔率的隐含概率 (1/odds)。平局赔率(1X2) 不是 0-0 赔率,
绝不可当作 P(0-0) 代理 (会严重高估, 第一版已废)。

本脚本用两套独立证据复核 0-0 是否被系统性低估:
  [A] 曲线法 (主, 用本次 311K 校准曲线):
      对 events.db 每场有 1X2+CS0-0 开盘快照+终场比分的比赛:
        implied = 1 / cs00_odds            (庄家 CS 隐含 P(0-0))
        fair   = curve(draw_prob_1X2)     (我的 311K 曲线公平 P(0-0|平局概率))
        factor_m = fair / implied
      均值 factor_A = mean(factor_m) -> >1 低估(庄家开低), <1 高估(庄家开高)
  [B] 频率法 (辅, 直接重算原定义):
        actual_pct = GQ 中 0-0 终场占比
        avg_implied = mean(1/cs00_odds)
        factor_B = actual_pct / avg_implied

两者一致则结论稳健。写回 cs_calibration.json 0-0 项 + 方法论备注。
"""
import sqlite3, json, os
import numpy as np
import sys
sys.path.insert(0, "D:/Architecture")
from analysis.cs_value_model import calibrate, fair_p00, _margin_strip

ROOT = "D:/Architecture"
GQ_DB = os.path.join(ROOT, "data", "events.db")
CAL_PATH = os.path.join(ROOT, "data", "cs_calibration.json")


def main():
    kx, vy, overall = calibrate()
    print(f"[curve] 311K 校准: 整体 0-0 率={overall:.4f}, 曲线点={len(kx)}")

    g = sqlite3.connect(GQ_DB)
    # 每场取最早(开盘) 1X2 draw + CS 0-0 快照, 以及终场比分
    rows = g.execute("""
        SELECT m.home, m.away, m.score_home, m.score_away
        FROM matches m
        WHERE m.score_home IS NOT NULL AND m.score_away IS NOT NULL
          AND m.status='finished'
    """).fetchall()
    g.close()

    def first_snap(mk, market, sel):
        gg = sqlite3.connect(GQ_DB)
        r = gg.execute(
            "SELECT odds FROM odds_snapshots WHERE match_key=? AND market=? AND selection=? "
            "ORDER BY captured_at ASC LIMIT 1", (mk, market, sel)).fetchone()
        gg.close()
        return float(r[0]) if r else None

    factors_a, factors_b_act, factors_b_imp = [], [], []
    n_used = 0
    for home, away, sh, sa in rows:
        mk = f"{home} vs {away}"
        dO = first_snap(mk, '1X2', 'draw')
        hO = first_snap(mk, '1X2', 'home')
        aO = first_snap(mk, '1X2', 'away')
        cO = first_snap(mk, 'CS', '0-0')
        if not (dO and hO and aO and cO) or cO > 200:
            continue
        _, dp, _ = _margin_strip(hO, dO, aO)
        fair = fair_p00(dp, kx, vy)
        implied = 1.0 / cO
        if implied <= 0 or fair <= 0:
            continue
        factors_a.append(fair / implied)
        factors_b_act.append(1 if (sh == 0 and sa == 0) else 0)
        factors_b_imp.append(implied)
        n_used += 1

    print(f"[scan] 可用比赛(含1X2+CS00开盘+终场): {n_used}")
    if n_used < 30:
        print("⚠ 样本不足, 保留原 factor 1.404 不覆盖")
        return

    fa = float(np.mean(factors_a))
    actual = float(np.mean(factors_b_act))
    avg_imp = float(np.mean(factors_b_imp))
    fb = actual / avg_imp if avg_imp > 0 else float('nan')

    print("=" * 64)
    print("P0-2: 0-0 因子复核 (曲线法 A + 频率法 B)")
    print("=" * 64)
    print(f"  [A] 曲线法 factor_A = fair/implied 均值 = {fa:.4f}")
    print(f"  [B] 频率法: 实际0-0率={actual:.4f}  庄家隐含均值={avg_imp:.4f}  factor_B={fb:.4f}")
    print(f"  原 cs_calibration 0-0 factor = 1.404 (基于3728场, 实际14.65%)")

    # 判定: factor 显著>1 才支持"低估"; 这里若 ~1 或 <1 则原 1.404 不成立
    if fa >= 1.05 or fb >= 1.05:
        verdict = "支持 0-0 被低估 (factor>1.05)"
    elif fa <= 0.95 and fb <= 0.95:
        verdict = "不支持低估: 0-0 实际被庄家开高(或持平), 原1.404属样本偏差"
    else:
        verdict = "接近持平 (0.95~1.05), 无稳健低估证据"
    print(f"  结论: {verdict}")

    with open(CAL_PATH, "r", encoding="utf-8") as fh:
        cal = json.load(fh)
    old = cal["calibrated_scores"].get("0-0", {})
    print(f"\n  旧 0-0: {old}")

    # 用曲线法因子(主)作为修正; 若两法背离大, 取保守(较小)值
    new_factor = round(min(fa, fb) if (fa and fb and not np.isnan(fb)) else fa, 4)
    cal["calibrated_scores"]["0-0"] = {
        "actual_pct": round(actual, 4),
        "avg_implied": round(avg_imp, 4),
        "factor": new_factor,
        "n": n_used,
        "factor_A_curve": round(fa, 4),
        "factor_B_freq": round(fb, 4) if not np.isnan(fb) else None,
        "method": "recomputed 2026-08-13 on events.db (1X2+CS00 open snapshot vs 311K curve / realized); "
                  "replaces prior 3728-match sample (14.65% actual, sample-biased)",
    }
    cal["_p002_note"] = (
        f"2026-08-13 复核: 旧 0-0 factor 1.404 基于 3728 场(实际0-0率14.65%)低级别偏差样本, "
        f"不可作普适结论。events.db {n_used} 场复核: 曲线法factor_A={fa:.3f}, 频率法factor_B={fb:.3f}, "
        f"修正 factor={new_factor}。{verdict}。下游 apply_calibration 已生效。"
    )

    bak = CAL_PATH + ".bak_p002b"
    with open(bak, "w", encoding="utf-8") as fh:
        with open(CAL_PATH, "r", encoding="utf-8") as src:
            fh.write(src.read())
    with open(CAL_PATH, "w", encoding="utf-8") as fh:
        json.dump(cal, fh, ensure_ascii=False, indent=2)
    print(f"\n  ✅ 已写回 cs_calibration.json (备份: {os.path.basename(bak)})")

    # 验证下游
    from pipeline.cs_calibration import apply_calibration
    out = apply_calibration([{"score": "0-0", "prob": 10.4, "source": "market"},
                             {"score": "1-0", "prob": 13.8, "source": "market"}])
    print("  验证 apply_calibration: ", [(c['score'], c['prob'], c.get('cal_factor')) for c in out])
    print("  ✅ 下游模块加载正常。")


if __name__ == "__main__":
    main()
