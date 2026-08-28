# -*- coding: utf-8 -*-
"""
集成扫描 · 双系统合并效果实测
============================
读取 unified_inplay_duel.py 导出的逐场概率向量 (inplay_per_sample.json),
量化"把两个系统合并"后效果变化:

  目标合并 = 本系统(sys) + GitHub(gh)
  辅助合并 = 本系统(sys) + 基线(nv, 去水隐含; 因为 gh≈nv, 本质同一信号)

方法:
  1) 凸组合扫描: E(w) = w*P2 + (1-w)*P1, w∈[0,1], 步长0.02
     - 对 (sys, gh) 与 (sys, nv) 两组分别扫
  2) 等权 (w=0.5)
  3) 置信度择优: 每样本选 max-prob 更高(更自信)的预测器
  4) 分歧度: argmax 不一致率 + 概率余弦相似度(解释为何(不)提升)
  5) 最优 w* 下相对"最佳单模型"的提升量

指标: 宏平均 AUC(OvR) / LogLoss / 多类 Brier / Top-1 Acc
"""
import json, os
import numpy as np
from sklearn.metrics import roc_auc_score, log_loss

PS = r"D:\Architecture\deliverables\inplay_per_sample.json"

def load():
    with open(PS, encoding="utf-8") as f:
        data = json.load(f)
    y = np.array([d["y"] for d in data])
    sys_p = np.array([d["sys"] for d in data])
    gh_p  = np.array([d["gh"]  for d in data])
    nv_p  = np.array([d["nv"]  for d in data])
    return y, sys_p, gh_p, nv_p

def metrics(y, M):
    M = np.asarray(M, dtype=float)
    M = np.where(np.isfinite(M), M, 0.0)
    s = M.sum(axis=1, keepdims=True)
    M = np.where(s > 0, M / s, np.full_like(M, 1/3))
    auc = float(roc_auc_score(y, M, multi_class="ovr", average="macro", labels=[0,1,2]))
    ll  = float(log_loss(y, M, labels=[0,1,2]))
    oh  = np.zeros_like(M); oh[np.arange(len(y)), y] = 1.0
    brier = float(((oh - M)**2).sum(axis=1).mean())
    acc = float((M.argmax(axis=1) == y).mean())
    return dict(auc=auc, logloss=ll, brier=brier, acc=acc)

def sweep(y, P1, P2, steps=51):
    """E(w)=w*P2+(1-w)*P1, 返回按 AUC 与按 LogLoss 各自最优的 w 及指标。"""
    ws = np.linspace(0, 1, steps)
    res = []
    for w in ws:
        E = w * P2 + (1 - w) * P1
        m = metrics(y, E)
        res.append((float(w), m))
    best_auc = max(res, key=lambda r: r[1]["auc"])
    best_ll  = min(res, key=lambda r: r[1]["logloss"])
    return res, best_auc, best_ll

def diversity(y, P1, P2):
    a1 = P1.argmax(axis=1); a2 = P2.argmax(axis=1)
    disagree = float((a1 != a2).mean())
    # 余弦相似度(逐样本 on 概率向量)
    dot = (P1 * P2).sum(axis=1)
    n1 = np.linalg.norm(P1, axis=1); n2 = np.linalg.norm(P2, axis=1)
    cos = np.where((n1 > 0) & (n2 > 0), dot / (n1 * n2), 1.0)
    return disagree, float(cos.mean())

def confidence_select(y, P1, P2):
    """每样本选 max-prob 更自信(更大)的预测器。"""
    c1 = P1.max(axis=1); c2 = P2.max(axis=1)
    pick2 = c2 >= c1
    E = np.where(pick2[:, None], P2, P1)
    return metrics(y, E), float(pick2.mean())

def main():
    y, sys_p, gh_p, nv_p = load()
    print(f"[load] n={len(y)}  标签分布 H/D/A = "
          f"{int((y==0).sum())}/{int((y==1).sum())}/{int((y==2).sum())}")

    # ---- 单模型基线 ----
    S, G, N = metrics(y, sys_p), metrics(y, gh_p), metrics(y, nv_p)
    print("\n===== 单模型 (held-out in-play) =====")
    for name, m in [("本系统 sys", S), ("GitHub gh", G), ("基线 nv(去水隐含)", N)]:
        print(f"  {name:<22} AUC={m['auc']:.4f}  LogLoss={m['logloss']:.4f}  Brier={m['brier']:.4f}  Acc={m['acc']:.4f}")

    # ---- 分歧度 ----
    dg_sg, cs_sg = diversity(y, sys_p, gh_p)
    dg_sn, cs_sn = diversity(y, sys_p, nv_p)
    print(f"\n===== 分歧度 (sys vs gh / sys vs nv) =====")
    print(f"  argmax 不一致率: sys↔gh={dg_sg*100:.1f}%  sys↔nv={dg_sn*100:.1f}%")
    print(f"  概率余弦相似度:  sys↔gh={cs_sg:.4f}   sys↔nv={cs_sn:.4f}  (1.0=完全一致)")

    # ---- 扫描 1: sys + gh (用户问的"两个系统合并") ----
    res_sg, ba_sg, bl_sg = sweep(y, sys_p, gh_p)
    print(f"\n===== 凸组合扫描: 本系统 + GitHub  E(w)=w·gh+(1-w)·sys =====")
    print(f"  最优 AUC  在 w={ba_sg[0]:.2f}: AUC={ba_sg[1]['auc']:.4f} LL={ba_sg[1]['logloss']:.4f} Acc={ba_sg[1]['acc']:.4f}")
    print(f"  最优 LogLoss 在 w={bl_sg[0]:.2f}: AUC={bl_sg[1]['auc']:.4f} LL={bl_sg[1]['logloss']:.4f} Acc={bl_sg[1]['acc']:.4f}")
    print(f"  等权 w=0.50:        AUC={dict([(round(w,2),m) for w,m in res_sg])[0.5]['auc']:.4f} "
          f"LL={dict([(round(w,2),m) for w,m in res_sg])[0.5]['logloss']:.4f}")

    # ---- 扫描 2: sys + nv ----
    res_sn, ba_sn, bl_sn = sweep(y, sys_p, nv_p)
    print(f"\n===== 凸组合扫描: 本系统 + 基线  E(w)=w·nv+(1-w)·sys =====")
    print(f"  最优 AUC  在 w={ba_sn[0]:.2f}: AUC={ba_sn[1]['auc']:.4f} LL={ba_sn[1]['logloss']:.4f}")
    print(f"  最优 LogLoss 在 w={bl_sn[0]:.2f}: AUC={bl_sn[1]['auc']:.4f} LL={bl_sn[1]['logloss']:.4f}")

    # ---- 置信度择优 ----
    m_sel_sg, frac_sg = confidence_select(y, sys_p, gh_p)
    print(f"\n===== 置信度择优 (每样本选更自信者) =====")
    print(f"  sys vs gh: AUC={m_sel_sg['auc']:.4f} LL={m_sel_sg['logloss']:.4f} Acc={m_sel_sg['acc']:.4f} (选gh占比={frac_sg*100:.1f}%)")

    # ---- 结论量化: 合并相对最佳单模型提升 ----
    best_single = max(G, N, key=lambda m: m["auc"])  # GitHub/基线 并列最佳
    best_single_name = "GitHub/基线"
    best_ens_auc = ba_sg[1]["auc"]
    delta_auc = best_ens_auc - best_single["auc"]
    print(f"\n===== 合并提升量 =====")
    print(f"  最佳单模型({best_single_name}) AUC={best_single['auc']:.4f}")
    print(f"  最优合并(sys+gh, w={ba_sg[0]:.2f}) AUC={best_ens_auc:.4f}")
    print(f"  ΔAUC = {delta_auc:+.4f}  ({(delta_auc/best_single['auc']*100):+.2f}%)")
    print(f"  ΔLogLoss(合并 vs 最佳单模型) = {ba_sg[1]['logloss']-best_single['logloss']:+.4f}")
    if abs(delta_auc) < 0.003:
        print("  → 合并几乎无提升 (两预测器高度同源/同向)")
    elif delta_auc > 0:
        print("  → 合并有小幅提升")
    else:
        print("  → 合并反而劣化 (sys 拉低了强基线)")

    # ---- 导出 ----
    out = dict(
        n=len(y), single=dict(sys=S, gh=G, nv=N),
        diversity=dict(sys_vs_gh=dict(disagree=dg_sg, cos=cs_sg), sys_vs_nv=dict(disagree=dg_sn, cos=cs_sn)),
        sweep_sys_gh=dict(best_auc=dict(w=ba_sg[0], **ba_sg[1]),
                          best_logloss=dict(w=bl_sg[0], **bl_sg[1])),
        sweep_sys_nv=dict(best_auc=dict(w=ba_sn[0], **ba_sn[1]),
                          best_logloss=dict(w=bl_sn[0], **bl_sn[1])),
        confidence_select=dict(metrics=m_sel_sg, pick_gh_frac=frac_sg),
        delta_auc_vs_best_single=delta_auc,
    )
    outp = r"D:\Architecture\deliverables\ensemble_sweep_result.json"
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[ok] 集成扫描结果已存 {outp}")

if __name__ == "__main__":
    main()
