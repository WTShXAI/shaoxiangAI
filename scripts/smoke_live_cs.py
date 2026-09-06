"""
scripts/smoke_live_cs.py
=======================
波胆 correct_score_value 接线的冒烟测试 (验证用, 非单元测试框架依赖)。
运行: cd D:/Architecture && .venv/Scripts/python.exe scripts/smoke_live_cs.py

验证点:
  1) 无跨庄CS盘 → decision=SCAN, correct_score 字段不缺失(不被try/except吞掉)
  2) 注入跨庄CS盘 → edge_available=True, 低于fair的价正确 PASS(不伪称edge)
"""
import sys, json
sys.path.insert(0, "D:/Architecture")
from bridge_service import _live_predict

data = json.load(open("D:/Architecture/data/wc2026_72matches_with_odds.json", encoding="utf-8"))
sample = [m for m in data if m.get("1x2_home") and m.get("hs") is not None][:3]


def run():
    print("########## A) 默认(无跨庄CS盘) → 期望 SCAN ##########")
    for m in sample:
        out = _live_predict(m["home"], m["away"], float(m["1x2_home"]), float(m["1x2_draw"]),
                            float(m["1x2_away"]), home_norm=m["home"], away_norm=m["away"],
                            date="2026-07-01", league="WC2026")
        cs = out.get("sub_markets", {}).get("correct_score")
        assert cs is not None, "BUG: correct_score 字段缺失(被try/except吞掉)"
        assert cs.get("decision") == "SCAN", f"BUG: 无CS盘却 decision={cs.get('decision')}"
        print(f"  {m['home']} vs {m['away']} 实际 {m['hs']}-{m['aws']} | "
              f"decision={cs['decision']} rows={[(r['score'], r['prob']) for r in cs['rows']]}")

    print("\n########## B) 注入低于fair的CS盘 → 期望 PASS(不伪称edge) ##########")
    m0 = sample[0]
    out0 = _live_predict(m0["home"], m0["away"], float(m0["1x2_home"]), float(m0["1x2_draw"]),
                         float(m0["1x2_away"]), home_norm=m0["home"], away_norm=m0["away"],
                         date="2026-07-01", league="WC2026")
    cs0 = out0["sub_markets"]["correct_score"]
    top1 = cs0["rows"][0]["score"]
    i, j = map(int, top1.split("-"))
    prob1 = cs0["rows"][0]["prob"]
    fair = 1.0 / prob1
    synthetic_best = round(fair * 0.93, 2)  # 低于fair → 应 PASS
    out1 = _live_predict(m0["home"], m0["away"], float(m0["1x2_home"]), float(m0["1x2_draw"]),
                         float(m0["1x2_away"]), home_norm=m0["home"], away_norm=m0["away"],
                         date="2026-07-01", league="WC2026",
                         correct_score_books=[[top1, synthetic_best]])
    cs1 = out1["sub_markets"]["correct_score"]
    print(f"  注入: {top1} 价={synthetic_best} (fair={fair:.2f})")
    print(f"  decision={cs1['decision']} edge_available={cs1['edge_available']}")
    assert cs1["edge_available"] is True
    assert cs1["decision"] == "PASS", "BUG: 低于fair的价不应产生BET"

    print("\n########## C) 过自信收缩生效: 注入6%假edge(fair*1.06) → 期望 PASS ##########")
    # 旧逻辑(EV>0即BET)会把此价判BET→合成ROI -26.6%(亏钱); 收缩后应转PASS
    synthetic_6pct = round(fair * 1.06, 2)
    out_c = _live_predict(m0["home"], m0["away"], float(m0["1x2_home"]), float(m0["1x2_draw"]),
                          float(m0["1x2_away"]), home_norm=m0["home"], away_norm=m0["away"],
                          date="2026-07-01", league="WC2026",
                          correct_score_books=[[top1, synthetic_6pct]])
    cs_c = out_c["sub_markets"]["correct_score"]
    print(f"  注入: {top1} 价={synthetic_6pct} (fair={fair:.2f}, 高于fair+6%)")
    print(f"  decision={cs_c['decision']} top1_ev_pct={cs_c['rows'][0].get('ev_pct')}")
    assert cs_c["decision"] == "PASS", "BUG: 过自信收缩未生效, 6%假edge仍被判BET(会亏钱)"
    assert cs_c["rows"][0].get("prob_eff") is not None, "BUG: 未输出收缩后有效概率"

    print("\n########## D) 方向性门: 注入极高价(fair*2.0) → 期望仍 BET ##########")
    # 证明门非'一律PASS': 足够大的正edge在收缩后仍为正→BET
    synthetic_high = round(fair * 2.0, 2)
    out_d = _live_predict(m0["home"], m0["away"], float(m0["1x2_home"]), float(m0["1x2_draw"]),
                          float(m0["1x2_away"]), home_norm=m0["home"], away_norm=m0["away"],
                          date="2026-07-01", league="WC2026",
                          correct_score_books=[[top1, synthetic_high]])
    cs_d = out_d["sub_markets"]["correct_score"]
    print(f"  注入: {top1} 价={synthetic_high} (fair={fair:.2f}, 高于fair+100%)")
    print(f"  decision={cs_d['decision']} top1_ev_pct={cs_d['rows'][0].get('ev_pct')}")
    assert cs_d["decision"] == "BET", "BUG: 方向性门失效, 真正高edge被误杀为PASS"

    print("\n[SMOKE OK] SCAN / edge / 过自信收缩 三种模式均正常, 无假edge, 门方向性正确")


if __name__ == "__main__":
    run()
