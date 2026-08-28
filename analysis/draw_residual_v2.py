# -*- coding: utf-8 -*-
"""最后一条跨市场残差(铁律原文举例): OU隐含总球 vs 1X2泊松翻译总球 之差。
   仅GQ(有OU线)。残差<0 = OU隐含总球少于比分总球 = 偏防守/平局倾向。"""
import sqlite3, math, numpy as np

con = sqlite3.connect("data/rollball_training.db"); con.row_factory=sqlite3.Row
rows=[dict(r) for r in con.execute(
  "SELECT p_h,p_d,p_a,ou_line,ou_over,ou_under,is_draw FROM rb_matches "
  "WHERE src='gq' AND p_h IS NOT NULL AND ou_line>0 AND ou_over>0 AND ou_under>0")]
con.close()
N=len(rows)
ph=np.array([r["p_h"] for r in rows]); pa=np.array([r["p_a"] for r in rows])
pdq=np.array([r["p_d"] for r in rows]); isd=np.array([r["is_draw"] for r in rows])
ou_l=np.array([r["ou_line"] for r in rows])
io=np.array([1.0/r["ou_over"] for r in rows]); iu=np.array([1.0/r["ou_under"] for r in rows])
s=io+iu; p_over=io/s
MAXG=10; K=np.arange(MAXG+1); fac=np.array([math.factorial(k) for k in K])

def pmf_vec(lam):
    lam=np.maximum(lam,1e-6)
    return (lam[:,None]**K[None,:])*np.exp(-lam[:,None])/fac[None,:]

def match_probs(lh,la):
    Ph=pmf_vec(lh); Pa=pmf_vec(la); H=D=A=np.zeros(N)
    for i in range(MAXG+1):
        for j in range(MAXG+1):
            p=Ph[:,i]*Pa[:,j]
            if i>j:H+=p
            elif i<j:A+=p
            else:D+=p
    return H,D,A

lh=np.full(N,1.3); la=np.full(N,1.3)
for _ in range(120):
    H,_,_=match_probs(lh,la)
    lh=np.where(H<ph-1e-4,lh+0.03,np.where(H>ph+1e-4,lh-0.03,lh))
    la=np.where(H<pa-1e-4,la+0.03,np.where(H>pa+1e-4,la-0.03,la))  # 注: 用A需重算, 简化用H近似调la
# 重算用A精确调la
for _ in range(120):
    _,_,A=match_probs(lh,la)
    la=np.where(A<pa-1e-4,la+0.03,np.where(A>pa+1e-4,la-0.03,la))
lsum=lh+la  # 1X2泊松翻译总球

# OU隐含总球: 反解 λ_ou 使 P(total>ou_line)=p_over
def poisson_over(lam,line):
    # P(total>line) = 1 - CDF(line)
    pmf=pmf_vec(np.full(N,lam))[:,:int(line)+1]
    return 1-pmf.sum(axis=1)
lam_ou=np.full(N,2.5)
for _ in range(50):
    pov=poisson_over(lam_ou,0)
    lam_ou=np.where(pov<p_over,lam_ou+0.05,np.where(pov>p_over,lam_ou-0.05,lam_ou))
res=lsum-lam_ou   # >0 比分总球多于OU隐含 -> 偏进攻; <0 偏防守/平局
print(f"GQ样本 N={N}, 平局基线={isd.mean():.4f}")
print(f"[OU×1X2跨市场残差] (1X2比分总球 - OU隐含总球) AUC={auc(res,isd):.4f}" if False else "")
def auc(probs,labels):
    order=np.argsort(-probs); lbls=labels[order]; pos=int(labels.sum()); neg=N-pos
    if pos==0 or neg==0: return float('nan')
    return (np.cumsum(lbls)[-1]-pos*(pos+1)/2)/(pos*neg)
a=auc(-res,isd)  # 残差越小(偏防守)越像平局 -> 取负
print(f"[OU×1X2跨市场残差] (OU隐含总球 - 1X2比分总球) AUC={a:.4f}")
print("  校准: 残差分箱 -> 平局率")
edges=[-3,-2,-1,-0.5,0,0.5,1,2,4]
for i in range(len(edges)-1):
    m=(res>=edges[i])&(res<edges[i+1])
    if m.sum()==0: continue
    print(f"    res[{edges[i]:+.1f},{edges[i+1]:+.1f}): n={int(m.sum()):5d}, 平局率={isd[m].mean():.3f}")
