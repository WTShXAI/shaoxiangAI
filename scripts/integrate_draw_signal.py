# -*- coding: utf-8 -*-
"""
新特征接入 draw_signal 规则引擎试跑 —
把特征表的独赢三赔喂 pipeline.draw_signal.market_draw_prob (操盘手一手平局信号),
叠加本次发现的新特征 draw_deviation (平局溢价) 和 handicap_ou_divergence (让球-大小背离),
生成增强平局信号评分。

信号设计 (借鉴 draw_signal 的 lift 思路):
  base_draw_prob   = market_draw_prob(oh,od,oa)   ← 操盘手一手 P(平), 主信号
  draw_deviation   = imp_d - 0.333                 ← 平局隐含概率偏离, 正=操盘手抬平
  handicap_ou_div  = 让球与大小方向矛盾(0/1)        ← 操盘手定价矛盾, 平局概率升高
  综合平局分:
    enhanced_draw_score = base_draw_prob
                       + 0.5 * max(draw_deviation, 0)   # 平局溢价加权
                       + 0.03 * handicap_ou_div          # 矛盾信号小幅加成
  预警: enhanced_draw_score >= 0.28 → DRAW_ALERT

输出:
  data/long_features/draw_signal_enhanced.csv  — 有独赢三赔的比赛 + 增强平局信号
"""
import csv, os, sys

sys.path.insert(0, r"D:\Architecture")
# draw_signal 依赖 sqlite3/pandas/numpy, ocr_venv 可能缺 pandas → 直接内联 demargin 避免依赖
def demargin(oh, od, oa):
    inv = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/inv, (1.0/od)/inv, (1.0/oa)/inv

def market_draw_prob(oh, od, oa):
    try:
        _, pd_, _ = demargin(float(oh), float(od), float(oa))
        return float(pd_)
    except Exception:
        return 0.0

IN_CSV = r"D:\Architecture\data\long_features\prematch_subset.csv"
OUT_CSV = r"D:\Architecture\data\long_features\draw_signal_enhanced.csv"
DRAW_ALERT_ENHANCED = 0.28  # 增强阈值 (draw_signal 原阈值 0.26, 加新特征后略调)

def main():
    rows = list(csv.DictReader(open(IN_CSV, encoding="utf-8-sig")))
    out = []
    n_alert = 0
    for r in rows:
        oh, od, oa = r.get("odds_h"), r.get("odds_d"), r.get("odds_a")
        if not (oh and od and oa):
            continue
        try:
            oh, od, oa = float(oh), float(od), float(oa)
        except:
            continue
        if not (1 < oh < 30 and 1 < od < 30 and 1 < oa < 30):
            continue
        ph, pd_base, pa = demargin(oh, od, oa)
        # 新特征
        draw_dev = pd_base - 0.3333
        hcap_ou_div = 1 if str(r.get("handicap_ou_divergence", "0")) == "1" else 0
        # 综合平局分
        enhanced = pd_base + 0.5 * max(draw_dev, 0.0) + 0.03 * hcap_ou_div
        enhanced = round(enhanced, 4)
        alert = enhanced >= DRAW_ALERT_ENHANCED
        if alert: n_alert += 1
        r["base_draw_prob"] = round(pd_base, 4)
        r["draw_deviation"] = round(draw_dev, 4)
        r["handicap_ou_div"] = hcap_ou_div
        r["enhanced_draw_score"] = enhanced
        r["draw_alert"] = "1" if alert else "0"
        out.append(r)

    if out:
        fields = ["home", "away", "league", "date", "odds_h", "odds_d", "odds_a",
                  "base_draw_prob", "draw_deviation", "handicap_ou_div",
                  "enhanced_draw_score", "draw_alert", "handicap_home", "ou_line",
                  "is_truly_prematch"] + [f for f in out[0] if f not in (
                  "home","away","league","date","odds_h","odds_d","odds_a",
                  "base_draw_prob","draw_deviation","handicap_ou_div",
                  "enhanced_draw_score","draw_alert","handicap_home","ou_line","is_truly_prematch")]
        with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in out: w.writerow(r)

    print(f"=== draw_signal 增强试跑 ===")
    print(f"输入: {len(rows)} 场赛前子集")
    print(f"有独赢三赔可评估: {len(out)} 场")
    print(f"平局预警 (enhanced>=0.28): {n_alert} 场")
    print(f"→ {OUT_CSV}")
    # 展示 Top 平局信号
    out_sorted = sorted(out, key=lambda x: -float(x["enhanced_draw_score"]))
    print("\n--- Top 8 平局信号 ---")
    print(f"  {'主队':12}{'客队':12}{'base':>7}{'dev':>7}{'div':>4}{'增强':>7}{'预警':>4}")
    for r in out_sorted[:8]:
        print(f"  {r['home'][:11]:12}{r['away'][:11]:12}{r['base_draw_prob']:>7}{r['draw_deviation']:>+7}{r['handicap_ou_div']:>4}{r['enhanced_draw_score']:>7}{r['draw_alert']:>4}")

if __name__ == "__main__":
    main()
