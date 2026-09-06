"""
odds_theory_exam.py — 理论考卷 (医生上岗前必须先考 100 分)

机制: 用一批【人工标定答案钥】的合成病例 (覆盖每个分类维度 + 边界),
对拍 classify() 的输出。分数 = 正确维度数 / 总维度数 × 100。
用户指令: "100分之后再实操" → 必须满分(100)才放行 real-case 诊断。

答案钥由领域理论独立标定 (不依赖 classify 实现), 故 classify 若答错 = 模型"不会"
(理论缺陷), 必须修规则到 100 分, 才许上手真实病例。

v2: 8 维 (tier/line_state/stage/consistency/draw_risk/water_level/eu_ah_fit/operation)。
   新增病例覆盖: 水位 7 级、欧亚换算 deeper/shallower、造冷造热漂移(E3升盘降水/
   E2降赔升水/E11反向/E10深盘退热/降盘阻上)、E4 死水僵持平局、live 低水双态(IR-23)。

用法:
  python pipeline/odds_theory_exam.py            # 跑考卷, 打印分数 + 错题
  python pipeline/odds_theory_exam.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline.odds_taxonomy import OddsCase, classify, DIMENSIONS, THEORY_NOTES


# ── 考卷: 人工标定答案钥 (领域理论独立判定, 非程序产出) ──────────────
# 每个 case: 构造 OddsCase + expected(各维度正确类别)
CASES = [
    # 1. 主流强队主场深让低水 → 陷阱候选
    dict(case=OddsCase("c1","曼城","伯恩利","英超",1.70,3.40,5.00,
                        ah_line=-1.5,ah_home_odds=1.80,ah_away_odds=2.00,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="trap_candidate",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="超高水",eu_ah_fit="consistent",
                       operation="deep_handicap_trap_candidate")),
    # 2. obscure 平局低水 → 低水聪明边
    dict(case=OddsCase("c2","A队","B队","印度西隆超",2.10,1.80,3.60,
                        has_db_analysis=False,stage="opening"),
         expected=dict(tier="obscure",line_state="low_water_smart",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="n/a",eu_ah_fit="unknown",operation="balanced")),
    # 3. 虚拟赛事 → 剔除
    dict(case=OddsCase("c3","X","Y","瓦尔哈拉杯(8分钟)",1.50,4.00,6.00,
                        is_virtual=True,has_db_analysis=False),
         expected=dict(tier="virtual",line_state="balanced",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="n/a",eu_ah_fit="unknown",operation="balanced")),
    # 4. 热门方高水 → 诱导
    dict(case=OddsCase("c4","切尔西","埃弗顿","英超",2.30,3.20,2.90,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="high_water_lure",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="n/a",eu_ah_fit="unknown",operation="shallow_line_hot")),
    # 5. 1X2 主强(0.60) 但 AH(-0.5) 主胜仅 0.48 → 内部分歧 + 欧亚开小盘
    dict(case=OddsCase("c5","皇马","赫塔菲","西甲",1.60,3.80,6.50,
                        ah_line=-0.5,ah_home_odds=2.00,ah_away_odds=1.85,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="balanced",stage="opening",
                       consistency="internal_divergence",draw_risk="neutral",
                       water_level="超高水",eu_ah_fit="shallower",operation="balanced")),
    # 6. 平局高警
    dict(case=OddsCase("c6","国米","都灵","意甲",1.95,3.20,4.20,
                        has_db_analysis=True,stage="opening",draw_alert=0.33),
         expected=dict(tier="main",line_state="balanced",stage="opening",
                       consistency="consistent",draw_risk="high",
                       water_level="n/a",eu_ah_fit="unknown",operation="balanced")),
    # 7. 平局低警
    dict(case=OddsCase("c7","利物浦","诺丁汉","英超",1.80,3.60,4.50,
                        has_db_analysis=True,stage="opening",draw_alert=0.18),
         expected=dict(tier="main",line_state="balanced",stage="opening",
                       consistency="consistent",draw_risk="low",
                       water_level="n/a",eu_ah_fit="unknown",operation="balanced")),
    # 8. 临场阶段
    dict(case=OddsCase("c8","巴黎","兰斯","法甲",1.90,3.40,4.10,
                        has_db_analysis=True,stage="closing"),
         expected=dict(tier="main",line_state="balanced",stage="closing",
                       consistency="consistent",draw_risk="neutral",
                       water_level="n/a",eu_ah_fit="unknown",operation="balanced")),
    # 9. live + obscure 弱势方(平局)低水 → 低水聪明边 (IR-23 双态: live 仍 smart 不升级)
    dict(case=OddsCase("c9","C队","D队","印度超",2.40,1.80,3.80,
                        has_db_analysis=False,stage="live"),
         expected=dict(tier="obscure",line_state="low_water_smart",stage="live",
                       consistency="consistent",draw_risk="neutral",
                       water_level="n/a",eu_ah_fit="unknown",operation="balanced")),
    # 10. 深让但热门方高水(>=1.85) → 非陷阱, balanced (测阈值)
    dict(case=OddsCase("c10","拜仁","波鸿","德甲",1.95,3.50,5.20,
                        ah_line=-1.5,ah_home_odds=2.00,ah_away_odds=1.80,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="balanced",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="超高水",eu_ah_fit="consistent",operation="balanced")),
    # 11. 深让 + 热门方低水边界(<1.85) → 陷阱候选 (测边界)
    dict(case=OddsCase("c11","阿森纳","副班长","英超",1.84,3.40,5.20,
                        ah_line=-1.5,ah_home_odds=1.82,ah_away_odds=2.00,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="trap_candidate",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="超高水",eu_ah_fit="consistent",
                       operation="deep_handicap_trap_candidate")),
    # 12. 1X2 主强(0.56) 但 AH(-0.5) 主胜仅 0.45 → 反向分歧 + 欧亚开小盘
    dict(case=OddsCase("c12","巴萨","阿拉维斯","西甲",1.70,3.60,5.50,
                        ah_line=-0.5,ah_home_odds=2.00,ah_away_odds=1.64,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="balanced",stage="opening",
                       consistency="internal_divergence",draw_risk="neutral",
                       water_level="超高水",eu_ah_fit="shallower",operation="balanced")),

    # ── 新增: 水位 7 级 / 欧亚换算 / 造冷造热 漂移 / E4 死水 ──
    # 13. 主流强队超低水(<0.75) → 水位 超低水 + 陷阱候选
    dict(case=OddsCase("c13","曼城","谢菲联","英超",1.40,4.00,8.00,
                        ah_line=-1.5,ah_home_odds=0.72,ah_away_odds=1.10,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="trap_candidate",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="超低水",eu_ah_fit="consistent",
                       operation="deep_handicap_trap_candidate")),
    # 14. 欧亚换算 deeper (实际盘比欧赔理论盘深) → deeper
    dict(case=OddsCase("c14","那不勒斯","萨勒尼塔纳","意甲",2.00,3.20,3.50,
                        ah_line=-1.5,ah_home_odds=1.85,ah_away_odds=2.00,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="balanced",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="超高水",eu_ah_fit="deeper",operation="balanced")),
    # 15. 欧亚换算 shallower + 中水 (0.90-0.95) + 半球内部分歧
    dict(case=OddsCase("c15","马竞","赫罗纳","西甲",1.65,3.60,6.00,
                        ah_line=-0.5,ah_home_odds=0.92,ah_away_odds=1.10,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="balanced",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="中水",eu_ah_fit="shallower",operation="balanced")),
    # 16. 造冷造热漂移: 升盘+降水 (E3 heat_up_line_drop_water)
    dict(case=OddsCase("c16","阿贾克斯","福图纳","荷甲",1.80,3.40,4.50,
                        ah_line=-1.5,ah_home_odds=0.85,ah_away_odds=2.00,
                        has_db_analysis=True,stage="opening",
                        ah_line_open=-1.0,ah_line_close=-1.5,
                        fav_water_open=0.95,fav_water_close=0.85),
         expected=dict(tier="main",line_state="trap_candidate",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="中低水",eu_ah_fit="deeper",
                       operation="heat_up_line_drop_water")),
    # 17. 造冷造热漂移: 降盘阻上/造冷 (drop_line_cool)
    dict(case=OddsCase("c17","塞维利亚","加的斯","西甲",2.00,3.20,3.80,
                        ah_line=-1.0,ah_home_odds=0.88,ah_away_odds=2.00,
                        has_db_analysis=True,stage="opening",
                        ah_line_open=-1.5,ah_line_close=-1.0,
                        fav_water_open=0.90,fav_water_close=0.88),
         expected=dict(tier="main",line_state="balanced",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="中低水",eu_ah_fit="consistent",
                       operation="drop_line_cool")),
    # 18. E4 死水盘: 水位>=0.98 僵持 → 平局信号 (deadlock_draw)
    dict(case=OddsCase("c18","法兰克福","波鸿","德甲",1.95,3.30,4.00,
                        ah_line=-0.5,ah_home_odds=1.00,ah_away_odds=1.00,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="balanced",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="高水",eu_ah_fit="consistent",
                       operation="deadlock_draw")),
    # 19. live 低水聪明边 (IR-23 双态验证: live 阶段仍标 low_water_smart, 不升级为诱)
    dict(case=OddsCase("c19","E队","F队","印度超",2.30,1.80,3.80,
                        has_db_analysis=False,stage="live"),
         expected=dict(tier="obscure",line_state="low_water_smart",stage="live",
                       consistency="consistent",draw_risk="neutral",
                       water_level="n/a",eu_ah_fit="unknown",operation="balanced")),
    # 20. 欧亚 deeper + 中水 (0.90-0.95) 边界 (测 water 中水分档 + deeper)
    dict(case=OddsCase("c20","罗马","恩波利","意甲",2.10,3.10,3.40,
                        ah_line=-1.5,ah_home_odds=0.92,ah_away_odds=2.00,
                        has_db_analysis=True,stage="opening"),
         expected=dict(tier="main",line_state="balanced",stage="opening",
                       consistency="consistent",draw_risk="neutral",
                       water_level="中水",eu_ah_fit="deeper",operation="balanced")),
]


def run() -> dict:
    total = len(CASES) * len(DIMENSIONS)
    correct = 0
    mistakes = []
    per_case = []
    for item in CASES:
        got = classify(item["case"])
        exp = item["expected"]
        case_hits = 0
        case_miss = []
        for dim in DIMENSIONS:
            if got.get(dim) == exp.get(dim):
                correct += 1
                case_hits += 1
            else:
                case_miss.append({
                    "dimension": dim, "expected": exp.get(dim), "got": got.get(dim)
                })
        if case_miss:
            mistakes.append({"case": item["case"].match_key, "misses": case_miss})
        per_case.append({"case": item["case"].match_key, "hits": case_hits,
                         "total": len(DIMENSIONS)})
    score = round(100.0 * correct / total, 1)
    passed = (score == 100.0)
    return {
        "score": score, "correct": correct, "total": total,
        "passed": passed, "mistakes": mistakes, "per_case": per_case,
        "gate": "必须 100 分才放行 real-case 实操 (用户指令)",
        "n_cases": len(CASES), "n_dims": len(DIMENSIONS),
    }


def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None, help="写出 JSON 报告路径")
    args = ap.parse_args()
    r = run()
    print("=" * 70)
    print(f"赔率分类 理论考卷 (v2, {r['n_cases']} 病例 × {r['n_dims']} 维)")
    print("=" * 70)
    for pc in r["per_case"]:
        print(f"  {pc['case']:<6s} {pc['hits']}/{pc['total']}")
    print("-" * 70)
    print(f"总分: {r['score']} / 100  (正确维度 {r['correct']}/{r['total']})")
    if r["passed"]:
        print("✅ 通过 — 模型已掌握理论, 可放行 real-case 实操")
    else:
        print("❌ 未满分 — 模型'不会'(理论缺陷), 禁止实操, 须修 classify 规则到 100 分")
        for m in r["mistakes"]:
            print(f"   ✗ {m['case']}: " + ", ".join(
                f"{d['dimension']} 期望 {d['expected']} 实得 {d['got']}" for d in m["misses"]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print(f"\n报告: {args.json}")


if __name__ == "__main__":
    _cli()
