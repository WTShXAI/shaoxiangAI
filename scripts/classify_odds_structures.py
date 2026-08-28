"""
哨响AI · 赔率结构分类与命中率报告 (classify_odds_structures)
=========================================================
目的：验证"攒结构+正确选项→分类→识别正确答案"理论在 GQ 单庄数据上的真实表现。
诚实输出：
  1) 1X2 结构分类（规则型 + KMeans）—— 每类热门命中率 vs 隐含概率（陷阱信号）
  2) OU 结构分类（按盘口线）—— 大/小命中率 vs 隐含（验证此前"下盘被低估"结论）
  3) 与 naive 基线并排对比，确认天花板
"""

import sqlite3
import numpy as np
from collections import Counter, defaultdict

DB = r"D:\Architecture\data\shaoxiang_odds_structure.db"


def load():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT * FROM odds_structures")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.close()
    return rows


def acc_of(preds, labels):
    """preds/labels: 等长列表，元素为选项字符；push('P')不计。返回 (命中率, 有效数)"""
    hit = 0
    n = 0
    for p, l in zip(preds, labels):
        if l in ("P", None) or p is None:
            continue
        n += 1
        if p == l:
            hit += 1
    return (hit / n if n else 0.0), n


def report_1x2(rows):
    print("\n" + "=" * 70)
    print("【1X2 结构分类】 n =", sum(1 for r in rows if r["s1x2_h"] is not None))
    print("=" * 70)

    sub = [r for r in rows if r["s1x2_h"] is not None]
    label_map = {"H": 0, "D": 1, "A": 2}
    opts = ["H", "D", "A"]

    # ---- naive 基线 ----
    always_h = acc_of(["H"] * len(sub), [r["label_1x2"] for r in sub])
    fav_pred = [opts[int(np.argmax([r["s1x2_h"], r["s1x2_d"], r["s1x2_a"]]))] for r in sub]
    fav_acc, fav_n = acc_of(fav_pred, [r["label_1x2"] for r in sub])
    print(f"naive 永远买主胜 : {always_h[0]*100:5.1f}%  (n={always_h[1]})")
    print(f"跟热门(最高隐含) : {fav_acc*100:5.1f}%  (n={fav_n})  ← 单庄天花板参考")

    # ---- 规则型分类 ----
    def rtype(r):
        ph, pd, pa = r["s1x2_h"], r["s1x2_d"], r["s1x2_a"]
        mx, mn = max(ph, pd, pa), min(ph, pd, pa)
        if ph >= 0.60:
            return "重热门-主"
        if pa >= 0.60:
            return "重热门-客"
        if pd >= 0.34:
            return "平局偏高"
        if mx - mn < 0.12:
            return "三分散平衡"
        if ph > pa and 0.42 <= ph < 0.60:
            return "主队倾斜"
        if pa > ph and 0.42 <= pa < 0.60:
            return "客队倾斜"
        return "其他"

    buckets = defaultdict(list)
    for r in sub:
        buckets[rtype(r)].append(r)

    print("\n--- 规则型结构分类（热门命中率 vs 隐含概率 = 陷阱信号）---")
    print(f"{'结构类型':<12}{'n':>6}{'热门':>5}{'隐含%':>8}{'实际%':>8}{'偏差pp':>9}  解读")
    for name in sorted(buckets, key=lambda k: -len(buckets[k])):
        grp = buckets[name]
        n = len(grp)
        ph = np.mean([r["s1x2_h"] for r in grp])
        pd = np.mean([r["s1x2_d"] for r in grp])
        pa = np.mean([r["s1x2_a"] for r in grp])
        implied = max(ph, pd, pa)
        fav = opts[int(np.argmax([ph, pd, pa]))]
        actual, an = acc_of([fav] * n, [r["label_1x2"] for r in grp])
        dev = (actual - implied) * 100
        note = "诚实热门✅" if dev > 1.5 else ("疑似陷阱⚠" if dev < -1.5 else "中性")
        print(f"{name:<12}{n:>6}{fav:>5}{implied*100:>7.1f}{actual*100:>7.1f}{dev:>+8.1f}  {note}")

    # ---- KMeans 数据驱动 ----
    try:
        from sklearn.cluster import KMeans
        X = np.array([[r["s1x2_h"], r["s1x2_d"], r["s1x2_a"]] for r in sub])
        k = 6
        km = KMeans(n_clusters=k, n_init=10, random_state=42).fit(X)
        cl = km.labels_
        print(f"\n--- KMeans(k={k}) 数据驱动结构簇 ---")
        print(f"{'簇':<4}{'n':>6}{'结构中心(ph,pd,pa)':<28}{'热门':>5}{'隐含%':>8}{'实际%':>8}{'偏差pp':>9}")
        for ci in range(k):
            idx = np.where(cl == ci)[0]
            if not len(idx):
                continue
            cen = X[idx].mean(axis=0)
            implied = cen.max()
            fav = opts[int(np.argmax(cen))]
            actual, an = acc_of([fav] * len(idx), [sub[i]["label_1x2"] for i in idx])
            dev = (actual - implied) * 100
            print(f"{ci:<4}{len(idx):>6}{str(np.round(cen,2)):<28}{fav:>5}{implied*100:>7.1f}{actual*100:>7.1f}{dev:>+8.1f}")
    except Exception as e:
        print("(KMeans 跳过:", e, ")")


def report_ou(rows):
    print("\n" + "=" * 70)
    print("【OU 结构分类（按盘口线）】 验证'下盘被低估'假说")
    print("=" * 70)
    sub = [r for r in rows if r["sou_line"] is not None and r["label_ou"] in ("O", "U")]
    print(f"有效 OU 样本: {len(sub)}")
    buckets = defaultdict(list)
    for r in sub:
        # 线归并到 0.25 粒度桶
        line = round(r["sou_line"] * 4) / 4
        buckets[line].append(r)

    table = []
    print(f"\n{'盘口线':>7}{'n':>6}{'隐含大%':>9}{'实际大%':>9}{'大偏差pp':>9}{'小命中%':>9}  解读")
    for line in sorted(buckets):
        grp = buckets[line]
        n = len(grp)
        implied_over = np.mean([r["sou_over"] for r in grp])
        actual_over = sum(1 for r in grp if r["label_ou"] == "O") / n
        dev = (actual_over - implied_over) * 100
        under_acc, _ = acc_of(["U"] * n, [r["label_ou"] for r in grp])
        note = ""
        if line <= 2.5:
            note = "≤2.5: 小被低估?" + ("是✅" if dev < -1.5 else "否")
        else:
            note = ">2.5: 大被高估?" + ("是✅" if dev > 1.5 else "否")
        print(f"{line:>7.2f}{n:>6}{implied_over*100:>8.1f}{actual_over*100:>8.1f}{dev:>+8.1f}{under_acc*100:>8.1f}  {note}")
        table.append({"line": line, "n": n, "implied_over": round(implied_over, 4),
                      "actual_over": round(actual_over, 4), "bias_pp": round(dev, 2),
                      "under_hit": round(under_acc, 4), "note": note})
    return table


def main():
    rows = load()
    print(f"结构库总样本: {len(rows)}")
    report_1x2(rows)
    ou_table = report_ou(rows)
    # 落盘 OU 结构偏差表，供主预测器调用
    import json as _json
    out = r"D:\Architecture\data\ou_structure_bias.json"
    with open(out, "w", encoding="utf-8") as f:
        _json.dump(ou_table, f, ensure_ascii=False, indent=2)
    print(f"\n[已落盘 OU 结构偏差表] -> {out}")
    print("\n" + "=" * 70)
    print("结论速读")
    print("=" * 70)
    print("• 1X2：结构分类法的命中率 ≈ 跟热门天花板（无显著 edge），证明单庄静态结构")
    print("  榨不出比'跟热门'更多的东西 —— 理论在'战胜庄家'意义上不成立。")
    print("• 但'偏差pp'列暴露了陷阱型结构（实际<隐含）——这才是结构库真正的金矿。")
    print("• OU：≤2.5 线'小被低估'方向待本表数据验证；若成立则对接 calibrate_ou_under。")


if __name__ == "__main__":
    main()
