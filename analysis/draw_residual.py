# -*- coding: utf-8 -*-
"""单庄跨市场残差找平局 (numpy 向量化, 全量秒级)
   1X2胜/负隐含概率 -> 反解 λ_h/λ_a(泊松) -> 卷积得"纯比分平局率"
   残差 = 庄家报出平局率 p_d - 泊松比分平局率. >0 = 庄家额外定价平局.
"""
import sqlite3, numpy as np, math

con = sqlite3.connect("data/rollball_training.db"); con.row_factory=sqlite3.Row
rows=[dict(r) for r in con.execute(
  "SELECT p_h,p_d,p_a,ou_line,ou_over,ou_under,is_draw,src FROM rb_matches WHERE p_h IS NOT NULL AND p_d IS NOT NULL AND p_a IS NOT NULL")]
con.close()
N=len(rows)
ph=np.array([r["p_h"] for r in rows]); pa=np.array([r["p_a"] for r in rows])
pdq=np.array([r["p_d"] for r in rows]); isd=np.array([r["is_draw"] for r in rows])
MAXG=10
K=np.arange(MAXG+1)

def pmf_vec(lam):
    lam=np.maximum(lam,1e-6)
    lk=lam[:,None]**K[None,:]
    ef=np.exp(-lam[:,None])
    fac=np.array([math.factorial(k) for k in K])
    return lk*ef/fac[None,:]   # (N, MAXG+1)

def match_probs(lh,la):
    Ph=pmf_vec(lh); Pa=pmf_vec(la)
    H=D=A=np.zeros(N)
    for i in range(MAXG+1):
        for j in range(MAXG+1):
            p=Ph[:,i]*Pa[:,j]
            if i>j: H+=p
            elif i<j: A+=p
            else: D+=p
    return H,D,A

lh=np.full(N,1.3); la=np.full(N,1.3)
for _ in range(120):
    H,D,A=match_probs(lh,la)
    lh=np.where(H<ph-1e-4, lh+0.03, np.where(H>ph+1e-4, lh-0.03, lh))
    la=np.where(A<pa-1e-4, la+0.03, np.where(A>pa+1e-4, la-0.03, la))

_,Dpois,_=match_probs(lh,la)
res=pdq-Dpois   # 跨市场残差
base=isd.mean()
print(f"样本 N={N}, 平局基线={base:.4f}")
print(f"[跨市场残差] 1X2内部(报出平局率 - 泊松比分平局率):")

def auc_skl(probs,labels):
    # 手动AUC
    order=np.argsort(-probs)
    ranks=probs[order]; lbls=labels[order]
    pos=int(labels.sum()); neg=N-pos
    if pos==0 or neg==0: return float('nan')
    rank=np.cumsum(lbls)
    return (rank[-1]-pos*(pos+1)/2)/(pos*neg)

a=auc_skl(res,isd)
print(f"  AUC={a:.4f}")

# 校准分箱
print("  [校准] res 分箱 -> 实际平局率:")
edges=[-0.2,-0.1,-0.05,0.0,0.05,0.1,0.2,0.5]
for i in range(len(edges)-1):
    m=(res>=edges[i])&(res<edges[i+1])
    if m.sum()==0: continue
    print(f"    res[{edges[i]:+.2f},{edges[i+1]:+.2f}): n={int(m.sum()):6d}, 平局率={isd[m].mean():.3f}")

# 组合阈值
for thr in [0.03,0.05,0.08]:
    m=res>thr
    if m.sum()==0: continue
    print(f"  res>{thr}: 标记{int(m.sum())}场, 平局率={isd[m].mean():.3f}, 召回={isd[m].sum()/isd.sum():.3f}")
