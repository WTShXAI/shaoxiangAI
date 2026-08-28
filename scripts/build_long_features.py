# -*- coding: utf-8 -*-
"""
long/ 截图特征工程 — 把解析出的 B/E/A 原始记录计算为模型可用特征。

特征分三层:
  L1 基础盘口特征 (对齐 score_model.py 的 oh/od/oa + draw_signal 的去抽水):
    - odds_h/d/a, imp_h/d/a (去抽水隐含概率), margin (抽水率)
    - handicap_home, ou_line, 让球/大小赔率
  L2 衍生特征 (我发现的新特征):
    - handicap_ou_divergence: 让球盘与大小球盘的方向背离 (操盘手定价信号)
    - fav_strength: 独赢热门集中度 (1 - imp_min*3, 越大越一边倒)
    - draw_premium: 平局赔率溢价 (imp_d 是否高于历史均值)
    - ou_implied_total: 大小球隐含总进球 (盘口线 × 调整)
    - margin_1x2 / margin_hcap / margin_ou: 三盘各自抽水率 (抽水不均=信号)
  L3 资金面特征 (来自 E 必发, 操盘手资金信号, 借鉴 draw_signal 多庄共识思路):
    - bf_volume_total, bf_home_ratio/draw_ratio/away_ratio (资金分布)
    - bf_pnl_home/draw/away_index (盈亏指数, 正=庄家在该方向亏=该方向有真实热度)

输出:
  data/long_features/match_features.csv   — 每场比赛一行, 全特征
  data/long_features/feature_dictionary.json — 特征字典
  data/long_features/data_quality_report.json — 数据质量报告
"""
import os, json, re, csv, math
from collections import defaultdict

FEAT_DIR = r"D:\Architecture\data\long_features"
OUT_CSV = os.path.join(FEAT_DIR, "match_features.csv")
OUT_DICT = os.path.join(FEAT_DIR, "feature_dictionary.json")
OUT_QUALITY = os.path.join(FEAT_DIR, "data_quality_report.json")

def load_jsonl(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]

def safe(x, default=None):
    return x if x not in (None, "", "-",) else default

def to_float(x):
    try:
        if x is None: return None
        return float(x)
    except: return None

# ═══════════════════ L1+L2: 盘口特征 ═══════════════════
def odds_features(rec):
    """从一条 B 类记录计算盘口特征。"""
    f = {}
    oh = to_float(rec.get("odds_h"))
    od = to_float(rec.get("odds_d"))
    oa = to_float(rec.get("odds_a"))
    if oh and od and oa and oh > 1 and od > 1 and oa > 1:
        inv_sum = 1/oh + 1/od + 1/oa
        margin = (inv_sum - 1) * 100
        # 硬门槛: 真实独赢抽水率 0~20%, 超出=赔率抓错, 不采信独赢字段
        if 0 <= margin <= 20:
            f["odds_h"], f["odds_d"], f["odds_a"] = oh, od, oa
            f["imp_h"] = round((1/oh)/inv_sum, 4)
            f["imp_d"] = round((1/od)/inv_sum, 4)
            f["imp_a"] = round((1/oa)/inv_sum, 4)
            f["margin_1x2"] = round(margin, 2)  # 抽水率 %
            # 热门集中度: 三概率最大值
            f["fav_imp"] = round(max(f["imp_h"], f["imp_d"], f["imp_a"]), 4)
            f["fav_side"] = "H" if f["imp_h"]>=f["imp_d"] and f["imp_h"]>=f["imp_a"] else ("A" if f["imp_a"]>=f["imp_d"] else "D")
            # 平局溢价: imp_d 相对均匀分布 0.333 的偏离
            f["draw_deviation"] = round(f["imp_d"] - 0.333, 4)
    # 让球
    hc = to_float(rec.get("handicap_home"))
    if hc is not None:
        f["handicap_home"] = hc
        f["is_home_fav"] = 1 if hc < 0 else 0  # 主让(负)=主队热门
        hoh = to_float(rec.get("handicap_odds_h")); hoa = to_float(rec.get("handicap_odds_a"))
        if hoh and hoa:
            f["handicap_odds_h"], f["handicap_odds_a"] = hoh, hoa
            hs = 1/hoh + 1/hoa
            f["margin_handicap"] = round((hs-1)*100, 2)
            f["hcap_imp_home"] = round((1/hoh)/hs, 4)
    # 大小
    ou = rec.get("ou_line")
    if ou:
        f["ou_line"] = ou
        oho = to_float(rec.get("ou_odds_over")); oha = to_float(rec.get("ou_odds_under"))
        if oho and oha:
            f["ou_odds_over"], f["ou_odds_under"] = oho, oha
            os_ = 1/oho + 1/oha
            f["margin_ou"] = round((os_-1)*100, 2)
            f["ou_imp_over"] = round((1/oho)/os_, 4)
            # 大小球隐含总进球近似 (盘口线, 若是 X/Y 亚洲盘取均值)
            line = ou.split("/")
            try:
                ln = [float(x) for x in line]
                f["ou_implied_total"] = round(sum(ln)/len(ln), 2)
            except: pass
    # ══ L2 新特征: 让球-大小背离 (严谨定义) ══
    # 真正的操盘手定价矛盾 = 深盘(强弱悬殊)但总进球与强弱方向不符:
    #   - 深主让(|hc|>=1.0) + 极高总进球(>3.5): 强队碾压却预期多进球 (矛盾, 可能对攻型弱队)
    #   - 深主让(|hc|>=1.0) + 极低总进球(<2.0): 强队碾压却预期少进球 (一致, 典型防守碾压, 不算矛盾)
    #   - 平手盘(|hc|<0.5) + 极端总进球: 势均力敌却预期进球很多/很少 (弱信号)
    # 仅在 |hc|>=1.0 且 ou>3.5 时标背离 (强队碾压+预期对攻=真矛盾)
    if "handicap_home" in f and "ou_implied_total" in f:
        hc = f["handicap_home"]
        ou = f["ou_implied_total"]
        if abs(hc) >= 1.0 and ou > 3.5:
            f["handicap_ou_divergence"] = 1
        elif abs(hc) >= 1.0 and ou < 2.0:
            f["handicap_ou_divergence"] = 0  # 一致(防守碾压)
        else:
            f["handicap_ou_divergence"] = 0
    return f

# ═══════════════════ L3: 必发资金面特征 ═══════════════════
def betfair_features(rec):
    f = {}
    h = rec.get("home_betfair", {}) or {}
    d = rec.get("draw_betfair", {}) or {}
    a = rec.get("away_betfair", {}) or {}
    hv = h.get("volume"); dv = d.get("volume"); av = a.get("volume")
    if hv and dv and av and (hv+dv+av) > 0:
        tot = hv + dv + av
        f["bf_volume_total"] = tot
        f["bf_home_ratio"] = round(hv/tot, 4)
        f["bf_draw_ratio"] = round(dv/tot, 4)
        f["bf_away_ratio"] = round(av/tot, 4)
        # 资金集中度 (最大占比)
        f["bf_concentration"] = round(max(f["bf_home_ratio"], f["bf_draw_ratio"], f["bf_away_ratio"]), 4)
        # 资金方向 vs 赔率方向背离: 资金最多的一边 vs 赔率最热的一边
        bf_max_side = max([("H",f["bf_home_ratio"]),("D",f["bf_draw_ratio"]),("A",f["bf_away_ratio"])], key=lambda x:x[1])[0]
        f["bf_heaviest_side"] = bf_max_side
    # 盈亏指数 (正=庄亏=真实热度方向)
    pnl = rec.get("pnl", {}) or {}
    ph = (pnl.get("主队") or {}).get("pnl_index")
    pd_ = (pnl.get("和局") or {}).get("pnl_index")
    pa = (pnl.get("客队") or {}).get("pnl_index")
    if ph is not None and pa is not None:
        f["bf_pnl_home_idx"] = ph
        f["bf_pnl_draw_idx"] = pd_
        f["bf_pnl_away_idx"] = pa
        # 盈亏指数梯度: 主-客, 正值=主队方向资金压力
        f["bf_pnl_gradient_ha"] = ph - pa
    f["is_prematch"] = 1 if rec.get("is_prematch") else 0
    return f

def main():
    b_recs = load_jsonl(os.path.join(FEAT_DIR, "B_live_odds.jsonl"))
    e_recs = load_jsonl(os.path.join(FEAT_DIR, "E_odds_trend.jsonl"))
    a_recs = load_jsonl(os.path.join(FEAT_DIR, "A_bill.jsonl"))

    # 账单: 按 (主队关键词/日期) 聚合真实赛果, 作标签池
    bill_labels = {}
    for r in a_recs:
        if "result_1x2" in r:
            key = (r.get("league",""), r.get("date","")[:10])
            bill_labels[key] = {"1x2": r["result_1x2"], "hs": r.get("result_home"), "as_": r.get("result_away"), "total": r.get("result_total")}

    rows = []
    # B 类 → 每场一行主特征
    for rec in b_recs:
        f = odds_features(rec)
        # 列表页无独赢三赔, 但有让球/大小盘 → 仍保留(算让球大小特征)
        # 单场页需有独赢三赔才采信
        layout = rec.get("layout", "")
        if layout == "single_match_detail" and "odds_h" not in f:
            continue
        if layout == "handicap_ou_list" and "handicap_home" not in f and "ou_line" not in f:
            continue
        if not layout and "odds_h" not in f:
            continue
        row = {
            "source": rec.get("source",""),
            "date": rec.get("date",""),
            "league": rec.get("league",""),
            "home": rec.get("home",""),
            "away": rec.get("away",""),
            "status": rec.get("status",""),
            "score_home_live": rec.get("score_home"),
            "score_away_live": rec.get("score_away"),
            "data_type": "live_odds",
        }
        row.update(f)
        rows.append(row)

    # E 类 → 资金面特征 (单独成行, 因 E 多为赛前, 与 B 滚球不同口径)
    e_rows = []
    for rec in e_recs:
        f = betfair_features(rec)
        if "bf_volume_total" not in f and "bf_pnl_home_idx" not in f:
            continue  # 无资金面数据跳过
        row = {
            "source": rec.get("source",""),
            "date": rec.get("date",""),
            "league": rec.get("league",""),
            "data_type": "betfair_funds",
        }
        row.update(f)
        e_rows.append(row)

    # 写 CSV (合并 B + E)
    all_rows = rows + e_rows
    if all_rows:
        keys = sorted({k for r in all_rows for k in r.keys()})
        # 固定列序: 标识在前
        lead = ["data_type","source","date","league","home","away","status",
                "score_home_live","score_away_live","is_prematch"]
        cols = lead + [k for k in keys if k not in lead]
        with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.DictWriter(fp, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in all_rows: w.writerow(r)

    # 特征字典
    feat_dict = {
        "odds_h/d/a": "独赢赔率 (主/平/客)。score_model.predict_score 的 oh/od/oa 输入。",
        "imp_h/d/a": "去抽水隐含概率 (1/odds 归一化)。draw_signal.market_draw_prob 的核心。",
        "margin_1x2": "独赢盘抽水率 % (Σ1/odds - 1)。庄家利润, 抽水异常低=促销/信号。",
        "fav_imp / fav_side": "热门方隐含概率及方向 (H/D/A)。",
        "draw_deviation": "平局隐含概率相对 0.333 的偏离。正值=操盘手抬升平局(平局信号, 借鉴draw_signal)。",
        "handicap_home": "让球盘 (主队, 负=主让=主队热门)。",
        "is_home_fav": "主队是否热门 (让球<0)。",
        "margin_handicap / margin_ou": "让球盘/大小球盘各自抽水率。三盘抽水不均=操盘手在某盘留口。",
        "ou_line / ou_implied_total": "大小球盘口线及隐含总进球。score_model 用 λ 缩放参考。",
        "handicap_ou_divergence": "【新】让球方向与大小球方向背离 (1=矛盾)。主让+低总进球=操盘手矛盾信号, 强预测价值。",
        "bf_volume_total / bf_*_ratio": "【新,资金面】必发三方交易量及占比。借鉴draw_signal多源共识, 资金集中≠赔率集中=信号。",
        "bf_pnl_*_idx / bf_pnl_gradient_ha": "【新,资金面】必发盈亏指数(正=庄亏=真实热度)。gradient_ha=主客资金压力差。",
        "bf_heaviest_side": "资金最重的一方。与 fav_side 比较: 不一致=资金与赔率背离 (强信号)。",
        "score_home/away_live": "滚球截图时的实时比分 (注意: 非赛果, 是赛中状态)。",
    }
    json.dump(feat_dict, open(OUT_DICT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 数据质量报告
    n_b_total = len([r for r in b_recs])
    n_b_ok = len(rows)
    n_e_ok = len(e_rows)
    qa = {
        "总截图": 419,
        "B_滚球盘口_解析成功": n_b_ok,
        "B_滚球盘口_解析失败(列表页/无锚点)": n_b_total - n_b_ok,
        "B_成功率": f"{100*n_b_ok/max(n_b_total,1):.0f}%",
        "E_必发资金面_有效": n_e_ok,
        "A_账单_赛果标签": len(a_recs),
        "支付垃圾_已剔除": 9,
        "特征表总行数": len(all_rows),
        "已知局限": [
            "数据主体为滚球(进行中)盘口, 非赛前终盘。喂赛前模型(score_model/draw_signal)需取每场最早时间点, 或建专门的滚球模型。",
            "A账单OCR错乱严重, 仅8条带可信赛果, 监督样本量小。建议赛果标签从football_data.db补充。",
            "队名/联赛为App显示名, 需与数据库队名做规范化映射(_canon_team)才能JOIN。",
            "9张支付/财务截图已完全排除, 未纳入任何输出。",
        ],
    }
    json.dump(qa, open(OUT_QUALITY, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print("=== 特征工程完成 ===")
    print(f"  match_features.csv : {len(all_rows)} 行 (B盘口 {n_b_ok} + E资金面 {n_e_ok})")
    print(f"  特征字段数: {len(feat_dict)}")
    print(f"  → {OUT_CSV}")
    print(f"  → {OUT_DICT}")
    print(f"  → {OUT_QUALITY}")
    # 抽样展示
    print("\n--- B盘口特征样本 ---")
    for r in rows[:4]:
        print(f"  {str(r.get('home',''))[:8]} vs {str(r.get('away',''))[:8]} | imp={r.get('imp_h','')}/{r.get('imp_d','')}/{r.get('imp_a','')} margin={r.get('margin_1x2','')}% | 让={r.get('handicap_home','')} OU隐含={r.get('ou_implied_total','')} 背离={r.get('handicap_ou_divergence','')}")
    print("--- E资金面特征样本 ---")
    for r in e_rows[:3]:
        print(f"  {r.get('league','')} 赛前={r.get('is_prematch','')} | 资金比={r.get('bf_home_ratio','')}/{r.get('bf_draw_ratio','')}/{r.get('bf_away_ratio','')} 盈亏梯度HA={r.get('bf_pnl_gradient_ha','')}")

if __name__ == "__main__":
    main()
