"""多庄 sharp 共识 → 波胆 HDA 锚定 (outcome-constrained tilt) 演示
================================================================
验证思路 (开源借脑共识: wc26 _calibrate_matrix_to_outcomes / JetQiao tilt_matrix):
  当前全库无多庄 CS 赔率(leisu 只有1X2, GQ.cs 为单庄), 故用多庄 sharp 1X2 共识
  去锚定波胆矩阵的 HDA 边缘 -> 把"多庄 sharp 共识"杠杆落到波胆推荐。

对每场有 true sharp 庄的 leisu 比赛:
  1) 用全庄平均 1X2(含 retail, 单源等价) 建 OIP 波胆矩阵
  2) tilt 到多庄 sharp 1X2 共识
  3) 对比 tilt 前后 HDA 边缘 + top3 比分, 看 sharp 杠杆把波胆往哪推

诚实边界: 仅 6 场 / 每场1个 sharp 庄(无Pinnacle), 证明"机制能跑",
          非"波胆胜率已提升"。历史 walk-forward 需雷速积累多庄数据后做。
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import numpy as np
from scipy.optimize import root
from scipy.stats import poisson

from pipeline.multibook_consensus import (
    load_leisu_groups, analyze_match, tilt_score_matrix_to_sharp,
)


def _indep_1x2(lam: float, mu: float, maxg: int = 8):
    h = poisson.pmf(np.arange(maxg + 1), lam)
    a = poisson.pmf(np.arange(maxg + 1), mu)
    M = np.outer(h, a)
    return np.tril(M, -1).sum(), np.trace(M), np.triu(M, 1).sum()


def oip_matrix_from_1x2(ph: float, pd_: float, pa: float, maxg: int = 8):
    """从目标 1X2 概率反解独立 Poisson λ,μ, 建 OIP 波胆矩阵。"""
    def obj(p):
        lam, mu = p
        h, d, a = _indep_1x2(lam, mu, maxg)
        return [h - ph, d - pd_]
    sol = root(obj, [1.3, 1.1], method="hybr")
    if not sol.success:
        return None
    lam, mu = max(0.05, float(sol.x[0])), max(0.05, float(sol.x[1]))
    h = poisson.pmf(np.arange(maxg + 1), lam)
    a = poisson.pmf(np.arange(maxg + 1), mu)
    M = np.outer(h, a)
    return M / M.sum()


def hda(M: np.ndarray):
    return (np.tril(M, -1).sum(), np.trace(M), np.triu(M, 1).sum())


def top3(M: np.ndarray):
    flat = M.flatten()
    order = np.argsort(-flat)[:3]
    return [(int(np.unravel_index(k, M.shape)[0]),
             int(np.unravel_index(k, M.shape)[1]),
             round(float(flat[k]) * 100, 2)) for k in order]


def main():
    rows = []
    for m in load_leisu_groups(market="1X2"):
        res = analyze_match(m)
        if not res.has_true_sharp:
            continue
        base = res.all_consensus          # 全庄平均(含 retail) = 单源等价
        sharp = res.sharp_consensus       # 多庄 sharp 共识
        oip = oip_matrix_from_1x2(base["h"], base["d"], base["a"])
        if oip is None:
            continue
        tilted, used = tilt_score_matrix_to_sharp(m["home"], m["away"], oip)
        b_h, b_d, b_a = hda(oip)
        t_h, t_d, t_a = hda(tilted)
        dh = (t_h - b_h) * 100
        dd = (t_d - b_d) * 100
        da = (t_a - b_a) * 100
        rows.append({
            "home": m["home"], "away": m["away"],
            "sharp_books": res.sharp_books,
            "base_1x2": {k: round(v * 100, 1) for k, v in base.items()},
            "sharp_1x2": {k: round(v * 100, 1) for k, v in sharp.items()},
            "base_hda": [round(b_h * 100, 1), round(b_d * 100, 1), round(b_a * 100, 1)],
            "tilted_hda": [round(t_h * 100, 1), round(t_d * 100, 1), round(t_a * 100, 1)],
            "delta_hda_pp": [round(dh, 2), round(dd, 2), round(da, 2)],
            "base_top3": top3(oip),
            "tilted_top3": top3(tilted),
            "tilt_used": used,
        })

    rows.sort(key=lambda r: max(abs(x) for x in r["delta_hda_pp"]), reverse=True)
    report = {
        "module": "multibook_score_tilt",
        "method": "OIP matrix tilted to multi-book sharp 1X2 consensus (outcome-constrained tilt)",
        "n_matches_with_true_sharp": len(rows),
        "note": "全库无多庄CS赔率, 用多庄1X2共识锚定波胆HDA边缘; 仅演示机制, 非胜率提升证据",
        "matches": rows,
    }
    out = Path("data/multibook_score_tilt_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"多庄 sharp 共识 → 波胆 HDA 锚定  | {len(rows)} 场含 true sharp")
    print("=" * 84)
    for r in rows:
        print(f"{r['home']} vs {r['away']}  sharp={r['sharp_books']}")
        print(f"   单源1X2 H/D/A = {r['base_1x2']['h']}/{r['base_1x2']['d']}/{r['base_1x2']['a']}%  "
              f"sharp共识 = {r['sharp_1x2']['h']}/{r['sharp_1x2']['d']}/{r['sharp_1x2']['a']}%")
        print(f"   波胆HDA 单源={r['base_hda']}  → sharp锚={r['tilted_hda']}  "
              f"Δ={r['delta_hda_pp']}pp")
        print(f"   top3单源  = {r['base_top3']}")
        print(f"   top3sharp = {r['tilted_top3']}")
    print(f"\n已写出 {out}")


if __name__ == "__main__":
    main()
