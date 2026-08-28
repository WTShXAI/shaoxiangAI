# -*- coding: utf-8 -*-
"""
OU/总进球模型对接 — 用大小球盘口(97%覆盖)给全部126场比赛算总进球概率分布。
不需要独赢三赔(绕过 score_model 的硬需求), 直接从 OU 盘口建模。

模型:
  1. ou_line (亚洲盘X/Y取均值) → 隐含总进球中线 λ_total
  2. ou_odds_over/under 去抽水 → P(大), P(小)
  3. 总进球服从 Poisson(λ_total), 算每个进球数 0~8 的概率
  4. 累加得 P(大球)、P(小球)、最可能总进球、Top3总进球

特征输出 (每场):
  ou_implied_total     隐含总进球 (盘口线)
  ou_prob_over/under   去抽水大小球概率
  ou_margin            大小球抽水率
  poisson_lambda       Poisson参数
  most_likely_total    最可能总进球
  top3_totals          前3可能总进球 [(n,p),...]
  ou_signal            信号标签 (低进球/正常/高进球/对攻)

输出: data/long_features/ou_model_predictions.csv (126场)
"""
import csv, json, math
from collections import Counter

IN = r"D:\Architecture\data\long_features\match_features_canon.csv"
OUT = r"D:\Architecture\data\long_features\ou_model_predictions.csv"

def poisson_pmf(k, lam):
    return math.exp(-lam) * lam**k / math.factorial(k)

def ou_line_to_lambda(line_str):
    """亚洲盘 X/Y → 均值。如 '2.5/3' → 2.75, '3' → 3.0"""
    try:
        parts = [float(x) for x in line_str.split("/")]
        return sum(parts)/len(parts)
    except:
        return None

def main():
    rows = list(csv.DictReader(open(IN, encoding="utf-8-sig")))
    out = []
    n_over = n_under = n_balanced = 0

    for r in rows:
        ou_line = r.get("ou_line", "")
        if not ou_line:
            continue
        lam = ou_line_to_lambda(ou_line)
        if lam is None:
            continue

        rec = {
            "home": r.get("home", ""), "away": r.get("away", ""),
            "league": r.get("league", ""), "date": r.get("date", ""),
            "ou_line": ou_line,
            "ou_implied_total": round(lam, 2),
        }

        # 大小球赔率去抽水
        oho = r.get("ou_odds_over"); oha = r.get("ou_odds_under")
        if oho and oha:
            try:
                oho, oha = float(oho), float(oha)
                inv = 1/oho + 1/oha
                p_over = (1/oho)/inv
                p_under = (1/oha)/inv
                rec["ou_prob_over"] = round(p_over, 4)
                rec["ou_prob_under"] = round(p_under, 4)
                rec["ou_margin"] = round((inv-1)*100, 2)
                # 信号: 哪边赔率低=庄家更倾向
                if p_over > 0.55:
                    rec["ou_lean"] = "大球"; n_over += 1
                elif p_under > 0.55:
                    rec["ou_lean"] = "小球"; n_under += 1
                else:
                    rec["ou_lean"] = "均衡"; n_balanced += 1
            except: pass

        # Poisson 总进球分布
        rec["poisson_lambda"] = round(lam, 3)
        totals = [(k, round(poisson_pmf(k, lam), 4)) for k in range(0, 9)]
        totals.sort(key=lambda x: -x[1])
        rec["most_likely_total"] = totals[0][0]
        rec["top3_totals"] = json.dumps([(n, p) for n, p in totals[:3]], ensure_ascii=False)
        # P(大球) = P(total > line) 用 Poisson 累积
        line_val = lam  # 近似用 lambda 当线
        p_over_poisson = sum(p for k, p in totals if k > line_val)
        rec["poisson_p_over"] = round(p_over_poisson, 4)

        # 信号标签
        if lam < 2.0: rec["ou_signal"] = "低进球(防守型)"
        elif lam <= 2.75: rec["ou_signal"] = "正常"
        elif lam <= 3.5: rec["ou_signal"] = "高进球"
        else: rec["ou_signal"] = "对攻"

        out.append(rec)

    if out:
        fields = list(out[0].keys())
        with open(OUT, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in out: w.writerow(r)

    # 信号分布
    sig_dist = Counter(r.get("ou_signal") for r in out)
    lean_dist = Counter(r.get("ou_lean","无赔率") for r in out)
    print(f"=== OU/总进球模型 ===")
    print(f"覆盖: {len(out)} 场 (全量盘口)")
    print(f"信号分布: {dict(sig_dist)}")
    print(f"大小球倾向: {dict(lean_dist)}")
    print(f"→ {OUT}")
    print("\n--- 样本 ---")
    for r in out[:8]:
        print(f"  {r['home'][:9]:9} vs {r['away'][:9]:9} | 线{r['ou_line']:>5} λ={r['poisson_lambda']:>5} | 最可能{r['most_likely_total']}球 | {r.get('ou_lean',''):4} | {r['ou_signal']}")

if __name__ == "__main__":
    main()
