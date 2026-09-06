"""
scripts/train_from_snapshots_20260830.py — 从分析快照表回训
============================================================
用户指令 (2026-08-30): "记录前端所有分析 → 结合赛果优化训练"

本脚本消费 analysis_snapshot 表里**已解析**的分析-赛果对, 回训:
  1. 三级判定阈值 CONF_LOW (观望阈值): 扫 0.40~0.60, 看哪个阈值下「给方向」的子集命中率最高
     (precision-recall 权衡: 阈值越高命中越高但观望越多)
  2. 领先方先验校准: 按当前比分领先方统计最终方向命中率, 与历史先验(74.6%/93.5%)对齐
  3. 诱盘有效性: 诱盘标记(RED/假防)的场次, 跟随市场方向是否真的更低命中

输出: 打印最优阈值 + 建议值, 供人工拍板后改 score_analyzer.CONF_LOW。
只读分析, 不改任何模型参数(回训结果由用户拍板后手动落地)。
"""
import sqlite3, sys, json
sys.path.insert(0, 'D:/Architecture')
from pipeline.analysis_snapshot import training_set, TABLE

DB = 'D:/Architecture/data/events.db'


def main(min_n=30):
    con = sqlite3.connect(DB, timeout=60)
    rows = training_set(con)
    con.close()
    n = len(rows)
    print(f"已解析分析-赛果对: {n} 条\n")
    if n < min_n:
        print(f"⚠ 样本不足 {min_n} 条, 阈值回训结果仅参考, 勿直接落地。")
        print(f"  当前前端已接落库, 随使用累积, 攒够 {min_n}+ 条再回训定档。")
        return

    # ── 1. 方向命中 (按是否有比分 = 赛前/滚球 分层) ──
    print("=" * 60)
    print("1. 方向命中率 (模型方向 vs 实际方向)")
    print("=" * 60)
    for phase in ("pre", "live"):
        sub = [r for r in rows if r["phase"] == phase and r["dir_hit"] is not None]
        if not sub:
            continue
        hit = sum(r["dir_hit"] for r in sub) / len(sub)
        print(f"  {phase:5s}: n={len(sub):4d}  方向命中 {hit*100:5.2f}%")

    # ── 2. 比分 top1/top3 命中 ──
    print("\n" + "=" * 60)
    print("2. 比分命中 (top1 / top3)")
    print("=" * 60)
    for phase in ("pre", "live"):
        sub = [r for r in rows if r["phase"] == phase]
        t1 = [r for r in sub if r["score_top1_hit"] is not None]
        t3 = [r for r in sub if r["score_top3_hit"] is not None]
        if not t1:
            continue
        print(f"  {phase:5s}: top1 {sum(r['score_top1_hit'] for r in t1)/len(t1)*100:5.2f}%  "
              f"top3 {sum(r['score_top3_hit'] for r in t3)/len(t3)*100:5.2f}%  (n={len(t1)})")

    # ── 3. 三级判定阈值扫描 ──
    print("\n" + "=" * 60)
    print("3. 三级判定 CONF_LOW 阈值扫描 (赛前+滚球合并)")
    print("=" * 60)
    # 有 sa_confidence 且有方向的样本
    live = [r for r in rows if r["phase"] == "live" and r["sa_confidence"] is not None]
    # 用"领先方先验"近似: 三级判定的 conf = 1 - |领先方胜率 - 市场概率|
    # 这里直接用 sa_confidence 作为排序键, 模拟不同 CONF_LOW 下给方向的命中率
    print(f"  (滚球样本 n={len(live)})")
    if len(live) >= 20:
        live_sorted = sorted(live, key=lambda r: -r["sa_confidence"])
        print(f"  {'CONF_LOW':>9s} {'给方向':>6s} {'方向命中':>8s} {'观望率':>7s}")
        for thr in (0.45, 0.50, 0.55, 0.60):
            gave = [r for r in live if r["sa_confidence"] >= thr and r["dir_hit"] is not None]
            if not gave:
                continue
            hit = sum(r["dir_hit"] for r in gave) / len(gave)
            watch = 1 - len(gave) / len([r for r in live if r["dir_hit"] is not None])
            print(f"  {thr:9.2f} {len(gave):6d} {hit*100:7.2f}% {watch*100:6.1f}%")

    # ── 4. 诱盘有效性 ──
    print("\n" + "=" * 60)
    print("4. 诱盘标记有效性 (诱导场 vs 诚实场 的方向命中)")
    print("=" * 60)
    ind = [r for r in rows if r["induce_label"] in ("fake_def", "RED") and r["dir_hit"] is not None]
    hon = [r for r in rows if r["induce_label"] in ("honest_def", "neutral", "NONE") and r["dir_hit"] is not None]
    if ind:
        print(f"  诱盘场  : n={len(ind):3d}  方向命中 {sum(r['dir_hit'] for r in ind)/len(ind)*100:5.2f}%")
    if hon:
        print(f"  诚实场  : n={len(hon):3d}  方向命中 {sum(r['dir_hit'] for r in hon)/len(hon)*100:5.2f}%")
    if ind and hon:
        print(f"  结论: 诱盘场命中 {'更低(诱盘识别有效)' if sum(r['dir_hit'] for r in ind)/len(ind) < sum(r['dir_hit'] for r in hon)/len(hon) else '更高(诱盘识别需重审)'}")

    # ── 5. 领先方先验核对 ──
    print("\n" + "=" * 60)
    print("5. 领先方先验核对 (滚球样本, 按当前比分领先方)")
    print("=" * 60)
    lead_map = {}
    for r in rows:
        if r["phase"] != "live" or not r["current_score"] or r["dir_hit"] is None:
            continue
        try:
            ch, ca = map(int, str(r["current_score"]).replace(':', '-').split('-')[:2])
        except Exception:
            continue
        if ch == ca:
            lead = "平"
        else:
            lead = "主" if ch > ca else "客"
            lead_map.setdefault(lead, []).append(r)
    for lead, sub in lead_map.items():
        if len(sub) < 5:
            continue
        # 领先方最终胜 = 实际方向 == 领先方
        final_win = sum(1 for r in sub if r["actual_direction"] == (lead == "主" and "home" or (lead == "客" and "away" or None))) / len(sub)
        print(f"  {lead}队领先: n={len(sub):3d}  最终胜率 {final_win*100:5.1f}%  (历史先验: 主1球74.6%/客1球≈74.6%)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("min_n", nargs="?", type=int, default=30)
    args = ap.parse_args()
    main(args.min_n)
