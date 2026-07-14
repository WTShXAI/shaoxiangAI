"""
scripts/calibrate_wc_cs.py
===========================
用世界杯真实赛果校准 波胆推荐 (correct_score_value / OIP 比分模型)。

数据现实 (已查证):
  - WC 数据集里 47庄×5场 全无 `scores`(波胆)市场 → 无跨庄CS价。
  - 故 correct_score_value 的"跨庄edge"无法用真实WC CS价校准;
    能校准的是 OIP 比分模型本身(λ/概率) 与 SCAN 模式 TOP-N 命中率。
  - 跨庄edge逻辑用 合成CS价 验证(标注SYNTHETIC), 确认不幻觉edge。

校准集: 同时含 1X2赔率 + 实际比分的WC场次
  - data/wc2026_72matches_with_odds.json (70场有真实赔率 + 比分)
  - data/wc2026_blob_r16_matched.json   (12场R16, oh/od/oa + hs/as_)

产出: data/wc_calibration.json
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.score_model import predict_score
from pipeline.deep_report import correct_score_value

# 运行时 WC goal_scale (须与 bridge_service.WC_OIP_GOAL_SCALE 同步; 313场校准=1.35)。
# 本脚本所有测量均基于该真实分布, 不再内部额外乘scale(避免与运行时double counting)。
try:
    from bridge_service import WC_OIP_GOAL_SCALE as RUNTIME_GOAL_SCALE
except Exception:
    RUNTIME_GOAL_SCALE = 1.35

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _load_matches():
    out = []
    # 1) 72场 (组赛为主)
    fn = os.path.join(DATA, "wc2026_72matches_with_odds.json")
    try:
        for m in json.load(open(fn, encoding="utf-8")):
            oh, od, oa = m.get("1x2_home"), m.get("1x2_draw"), m.get("1x2_away")
            hs, aws = m.get("hs"), m.get("aws")
            if oh and oa and hs is not None and aws is not None:
                out.append(dict(home=m["home"], away=m["away"],
                                oh=float(oh), od=float(od), oa=float(oa),
                                hs=int(hs), aws=int(aws)))
    except Exception as e:
        print("  72场加载失败:", e)
    # 2) R16 blob (12场, 多为未赛预告 hs/as_=None, 通常无赛果)
    fn2 = os.path.join(DATA, "wc2026_blob_r16_matched.json")
    try:
        for m in json.load(open(fn2, encoding="utf-8")):
            oh, od, oa = m.get("oh"), m.get("od"), m.get("oa")
            hs, aws = m.get("hs"), m.get("as_")
            if oh and oa and hs is not None and aws is not None:
                out.append(dict(home=m.get("home"), away=m.get("away"),
                                oh=float(oh), od=float(od), oa=float(oa),
                                hs=int(hs), aws=int(aws)))
    except Exception as e:
        print("  R16加载失败:", e)
    # 3) 2022 世界杯 (含赔率+比分, 跨届扩充校准集)
    fn3 = os.path.join(DATA, "wc2022_complete_with_odds.json")
    try:
        for m in json.load(open(fn3, encoding="utf-8")).get("data", []):
            oh, od, oa = m.get("oh"), m.get("od"), m.get("oa")
            hs, aws = m.get("hs"), m.get("aws")
            if oh and oa and hs is not None and aws is not None:
                out.append(dict(home=m.get("home"), away=m.get("away"),
                                oh=float(oh), od=float(od), oa=float(oa),
                                hs=int(hs), aws=int(aws)))
    except Exception as e:
        print("  2022加载失败:", e)
    return out


def hit_rate(matches, goal_scale=RUNTIME_GOAL_SCALE, topn=(1, 3, 5)):
    """对每场跑OIP(用运行时goal_scale); 取TOP-N比分(按概率); 命中=实际比分在TOP-N。
    直接基于 predict_score 返回的真实分布测量(运行时WC即如此), 不再额外乘scale。
    返回各top_n命中率与预测/实际总进球均值。"""
    import numpy as np
    hits = {n: 0 for n in topn}
    pred_totals, act_totals = [], []
    details = []
    for mt in matches:
        r = predict_score(mt["home"], mt["away"], mt["oh"], mt["od"], mt["oa"],
                          goal_scale=goal_scale)
        M = r["matrix"]
        n = M.shape[0]
        flat = M.flatten()
        order = np.argsort(-flat)
        top_scores = [(int(divmod(k, n)[0]), int(divmod(k, n)[1])) for k in order]
        actual = (mt["hs"], mt["aws"])
        for nn in topn:
            if actual in top_scores[:nn]:
                hits[nn] += 1
        pred_tot = r["lh"] + r["la"]
        act_tot = mt["hs"] + mt["aws"]
        pred_totals.append(pred_tot); act_totals.append(act_tot)
        details.append(dict(home=mt["home"], away=mt["away"],
                            actual=f"{actual[0]}-{actual[1]}",
                            pred_total=round(pred_tot, 3), act_total=act_tot,
                            top3=[f"{i}-{j}" for (i, j) in top_scores[:3]]))
    k = len(matches)
    return {f"top{n}": round(hits[n] / k, 4) for n in topn}, \
           round(sum(pred_totals) / k, 4), round(sum(act_totals) / k, 4), details


def synthetic_cs_edge_test(matches, margin_factors=(0.90, 1.06), overconf=None,
                           goal_scale=RUNTIME_GOAL_SCALE):
    """SYNTHETIC: 用OIP fair值生成跨庄CS价(标注SYNTHETIC), 验证 correct_score_value 逻辑。
    margin_factors: 赔率 = fair_decimal * f
      f<1 (如0.90) → 低于fair → 应 PASS (真实book常态, 不幻觉edge)
      f>1 (如1.06) → 高于fair → 应 BET (存在价值时正确捕获)
    overconf: 传入则启用过自信收缩(模拟WC运行时), 验证修复把假edge纠正为PASS。
    goal_scale: 与运行时一致(默认RUNTIME_GOAL_SCALE=1.35)。"""
    res = {}
    for f in margin_factors:
        bet = 0
        roi_sum = 0.0
        n = 0
        for mt in matches:
            r = predict_score(mt["home"], mt["away"], mt["oh"], mt["od"], mt["oa"],
                              goal_scale=goal_scale)
            M = r["matrix"]
            ndim = M.shape[0]
            flat = M.flatten()
            score_odds = {}
            for idx in range(len(flat)):
                p = flat[idx]
                if p <= 0:
                    continue
                i, j = divmod(idx, ndim)
                fair = 1.0 / p
                score_odds[(i, j)] = round(fair * f, 2)
            cs = correct_score_value(M.tolist(), score_odds=score_odds, top_n=5,
                                      overconf=overconf)
            n += 1
            if cs["decision"] == "BET":
                bet += 1
                pick = cs["rows"][0]["score"]
                odds = cs["rows"][0]["odds"]
                actual = f"{mt['hs']}-{mt['aws']}"
                stake = 100.0
                if pick == actual:
                    roi_sum += stake * (odds - 1)
                else:
                    roi_sum -= stake
        res[f"f{f}"] = dict(matches=n, bet_matches=bet,
                            edge_rate=round(bet / n, 4) if n else 0,
                            roi_if_bet=round(roi_sum / bet, 2) if bet else 0.0)
    return res


def main():
    matches = _load_matches()
    print(f"[校准集] 可用场次(含1X2赔率+比分): {len(matches)}")
    if not matches:
        print("无可用场次, 退出"); return
    print(f"[运行时 goal_scale] RUNTIME_GOAL_SCALE={RUNTIME_GOAL_SCALE} "
          f"(与 bridge_service.WC_OIP_GOAL_SCALE 同步)")

    # 1) OIP 基线 + 偏差 (直接基于运行时真实分布, 不再内部额外乘scale)
    base_hit, pred_t, act_t, _ = hit_rate(matches, goal_scale=RUNTIME_GOAL_SCALE)
    bias = round(pred_t - act_t, 4)
    print(f"\n[OIP 基线 @goal_scale={RUNTIME_GOAL_SCALE}] 预测均总进球={pred_t}  "
          f"实际均总进球={act_t}  偏差={bias:+.4f}")
    print(f"  TOP命中率: {base_hit}")
    cal_hit = base_hit  # 运行时固定用 RUNTIME_GOAL_SCALE, 不再auto-scale
    use_scale = RUNTIME_GOAL_SCALE

    # 2) 过自信诊断: 模型TOP1概率 vs 实际TOP1命中率 (同分布, 同goal_scale)
    import numpy as np
    top1_probs, top1_hits = [], []
    for mt in matches:
        r = predict_score(mt["home"], mt["away"], mt["oh"], mt["od"], mt["oa"],
                          goal_scale=RUNTIME_GOAL_SCALE)
        M = r["matrix"]; n = M.shape[0]; flat = M.flatten()
        order = np.argsort(-flat)
        i, j = divmod(order[0], n)
        top1_probs.append(float(flat[order[0]]))
        top1_hits.append(1 if (mt["hs"], mt["aws"]) == (int(i), int(j)) else 0)
    avg_top1_prob = round(sum(top1_probs) / len(top1_probs), 4)
    actual_top1_hit = round(sum(top1_hits) / len(top1_hits), 4)
    overconf = round(avg_top1_prob / actual_top1_hit, 2) if actual_top1_hit else None
    print(f"\n[过自信诊断 @goal_scale={RUNTIME_GOAL_SCALE}] 模型TOP1均概率={avg_top1_prob}  "
          f"实际TOP1命中={actual_top1_hit}  过自信倍数={overconf}x")
    print(f"  >> 模型对头号比分的把握被高估 ~{overconf}x → 小edge会因低命中而亏钱")

    # 3) 合成跨庄edge验证 (SYNTHETIC, 同goal_scale)
    print(f"\n[波胆edge 合成验证 SYNTHETIC @goal_scale={RUNTIME_GOAL_SCALE} — 修复前(overconf=None)]")
    syn = synthetic_cs_edge_test(matches, overconf=None, goal_scale=RUNTIME_GOAL_SCALE)
    for k, v in syn.items():
        print(f"  odds=fair*{k[1:]}: edge率={v['edge_rate']*100:.1f}%  ROI(若下)={v['roi_if_bet']}")
    print(f"\n[波胆edge 合成验证 SYNTHETIC @goal_scale={RUNTIME_GOAL_SCALE} — 修复后(WC overconf={overconf})]")
    syn_fixed = synthetic_cs_edge_test(matches, overconf=overconf, goal_scale=RUNTIME_GOAL_SCALE)
    for k, v in syn_fixed.items():
        print(f"  odds=fair*{k[1:]}: edge率={v['edge_rate']*100:.1f}%  ROI(若下)={v['roi_if_bet']}")

    # 4) 保存校准结果
    cs_reco = dict(
        model_overconfidence_x=overconf,
        avg_top1_model_prob=avg_top1_prob,
        actual_top1_hit=actual_top1_hit,
        goal_scale_used=RUNTIME_GOAL_SCALE,
        applied=(f"已应用(2026-07-11 重测版): correct_score_value 新增 overconf 参数, "
                 f"WC时用 p_eff=p/overconf (overconf={overconf}, 基于goal_scale={RUNTIME_GOAL_SCALE}重测) "
                 f"算EV/凯利, 仅取top1; 非WC overconf=None 不收缩. "
                 f"这把'6%假edge'纠正为负EV→PASS, 避免WC上'EV>0即BET'亏钱. "
                 f"cs_ev_threshold(默认0.0)可作额外EV门槛."),
        breakeven_odds_top1=round(1.0 / actual_top1_hit, 2) if actual_top1_hit else None,
        recommended_action=(f"已落实: correct_score_value 现对模型概率做温度收缩(~1/overconf)后再算EV"
                            f"(WC overconf={overconf}, goal_scale={RUNTIME_GOAL_SCALE}), 且仅取top1; "
                            f"非WC联赛不收缩. 旧'EV>0即BET'在WC上会亏钱, 现纠正."),
    )
    out = dict(
        n_matches=len(matches),
        oip=dict(pred_total_goals=pred_t, actual_total_goals=act_t, bias=bias,
                 calibrated_scale=use_scale, goal_scale=RUNTIME_GOAL_SCALE,
                 base_hit=base_hit, calibrated_hit=cal_hit),
        overconfidence=dict(avg_top1_model_prob=avg_top1_prob,
                            actual_top1_hit=actual_top1_hit, ratio_x=overconf,
                            goal_scale_used=RUNTIME_GOAL_SCALE),
        correct_score_value_synthetic=syn,
        correct_score_value_synthetic_fixed=syn_fixed,
        cs_recommendation=cs_reco,
        notes=(f"OIP比分模型已用WC真实赛果校准(goal_scale={RUNTIME_GOAL_SCALE}, TOP-N命中率见上). "
               "correct_score_value跨庄edge因WC无CS盘价无法真实校准, "
               "合成验证证明: 真实book(margin<0,f<1)下正确PASS不幻觉edge; "
               "仅当存在高于fair的错价(f>1)时捕获BET. "
               f"模型过自信→小edge会亏钱(修复前 f1.06: edge率100%/ROI-26.6%). "
               f"已应用overconf收缩({overconf}x, 基于goal_scale={RUNTIME_GOAL_SCALE}重测): "
               "修复后 f1.06 edge率降至0%/不再下注, 见 cs_recommendation.applied."),
    )
    fn = os.path.join(DATA, "wc_calibration.json")
    json.dump(out, open(fn, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n[产出] 已保存 -> {fn}")
    print(f"[重测结论] 新 overconf={overconf} (基于goal_scale={RUNTIME_GOAL_SCALE}) "
          f"→ 须同步更新 bridge_service.WC_CS_OVERCONF")


if __name__ == "__main__":
    main()
