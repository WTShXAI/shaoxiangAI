"""
哨响AI — 真比分模型 (Dixon-Coles + 赔率隐含 Poisson)
====================================================
数据源: wc_all_matches (328场, 其中 106 场含 oh/od/oa 赔率 + hg/ag 真实比分)
  - 2022: 18 场含赔率
  - 2026: 88 场含赔率
  (2014/2018 无赔率, 仅用作"无赔率基线"对照, 不进有赔率训练集)

双模型:
  A) OIP (Odds-Implied Poisson): 逐场去抽水→P(H/D/A)→数值解 λ_h,λ_a, 无训练, 天然OOS安全
  B) DC  (Dixon-Coles MLE): 队攻击/防守 + 主场优势 + rho低比分修正, L2正则

产出:
  - saved_models/dc_score_model.joblib  (DC参数, 供引擎调用)
  - deliverables/dc_score_predictions_sample.csv
  - 控制台报告: log-loss / Top1·Top3 比分命中 / H-D-A 还原 / 校准

作者: 赵统筹(总工)  | 2026-07-07
"""
import os, sys, math, json, sqlite3
import numpy as np
from scipy.optimize import minimize, root
import joblib

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, '..', 'data', 'football_data.db')
SAVED = os.path.join(ROOT, '..', 'saved_models')
os.makedirs(SAVED, exist_ok=True)

MAX_GOAL = 8  # 比分矩阵维度 0..8

# ───────────────────────── 数据加载 ─────────────────────────
def load_matches():
    c = sqlite3.connect(DB)
    rows = c.execute(
        "select edition,home,away,hg,ag,oh,od,oa from wc_all_matches "
        "where oh is not null and od is not null and oa is not null "
        "and hg is not null and ag is not null"
    ).fetchall()
    c.close()
    data = []
    for ed, h, a, hg, ag, oh, od, oa in rows:
        data.append(dict(edition=str(ed), home=h, away=a, hg=int(hg), ag=int(ag),
                         oh=float(oh), od=float(od), oa=float(oa)))
    return data

# ───────────────────────── 工具: 去抽水 ─────────────────────────
def deoverround(oh, od, oa):
    o = 1/oh + 1/od + 1/oa
    return (1/oh)/o, (1/od)/o, (1/oa)/o

def poisson_marginal(lh, la, maxg=MAX_GOAL):
    """返回 P(H),P(D),P(A) 基于独立Poisson"""
    ph = 0.0; pd_ = 0.0; pa = 0.0
    for i in range(maxg+1):
        pi = math.exp(-lh)*lh**i/math.factorial(i)
        for j in range(maxg+1):
            pj = math.exp(-la)*la**j/math.factorial(j)
            p = pi*pj
            if i>j: ph += p
            elif i==j: pd_ += p
            else: pa += p
    return ph, pd_, pa

def score_matrix(lh, la, maxg=MAX_GOAL):
    """独立Poisson 比分概率矩阵 [i,j] = P(i,j)"""
    col = [math.exp(-lh)*lh**i/math.factorial(i) for i in range(maxg+1)]
    row = [math.exp(-la)*la**j/math.factorial(j) for j in range(maxg+1)]
    M = np.outer(col, row)
    return M

# ───────────────────────── 模型A: OIP ─────────────────────────
def solve_oip(ph, pd, pa):
    """数值解 λ_h,λ_a 使Poisson边缘匹配P(H/D/A)"""
    def eq(x):
        lh, la = x
        if lh<=0 or la<=0: return [1e6,1e6]
        eh, ed, ea = poisson_marginal(lh, la)
        return [eh-ph, ed-pd]
    sol = root(eq, [1.3, 1.1], method='hybr')
    if sol.success and sol.x[0]>0 and sol.x[1]>0:
        return sol.x[0], sol.x[1]
    # 兜底: 网格粗搜
    best=None; bestr=1e9
    for lh in np.arange(0.3,4.5,0.1):
        for la in np.arange(0.3,4.5,0.1):
            eh,ed,ea=poisson_marginal(lh,la)
            r=(eh-ph)**2+(ed-pd)**2
            if r<bestr: bestr=r; best=(lh,la)
    return best

# ───────────────────────── 模型B: Dixon-Coles MLE ─────────────────────────
def tau(lh, la, i, j, rho):
    if i>0 and j>0: return 1.0
    if i==0 and j==0: return 1.0 - lh*la*rho
    if i==0 and j>0: return 1.0 + lh*rho
    return 1.0 + la*rho  # i>0,j==0

def dc_neg_loglik(params, teams_idx, matches, rho_idx, reg=0.3):
    n_teams = len(teams_idx)
    # params: att[0..n-1], def[0..n-1], mu, home, rho
    att = params[:n_teams]
    deff = params[n_teams:2*n_teams]
    mu = params[2*n_teams]
    home = params[2*n_teams+1]
    rho = params[rho_idx]
    nll = 0.0
    for (hi, ai, hg, ag) in matches:
        lh = math.exp(mu + home + att[hi] - deff[ai])
        la = math.exp(mu + att[ai] - deff[hi])
        t = tau(lh, la, hg, ag, rho)
        if t<=0: t=1e-12
        nll -= (math.log(t) + hg*math.log(lh) - lh + ag*math.log(la) - la
                - math.lgamma(hg+1) - math.lgamma(ag+1))
    # L2 正则 (att/def 向0收缩; 约束 sum(att)=0 由初始化近似)
    nll += reg*0.5*(np.sum(att**2)+np.sum(deff**2))
    return nll

def fit_dc(data, reg=0.3):
    teams = sorted(set([d['home'] for d in data]+[d['away'] for d in data]))
    tidx = {t:i for i,t in enumerate(teams)}
    matches = [(tidx[d['home']], tidx[d['away']], d['hg'], d['ag']) for d in data]
    n = len(teams)
    x0 = np.concatenate([np.zeros(n), np.zeros(n), [0.3, 0.1]])  # att,def,mu,home
    rho_idx = 2*n+2
    x0 = np.append(x0, -0.1)  # rho
    bounds = ([(-3,3)]*(2*n) + [(0.0,1.5),(0.0,1.0)] + [(-0.5,0.5)])
    res = minimize(dc_neg_loglik, x0, args=(tidx, matches, rho_idx, reg),
                   method='L-BFGS-B', bounds=bounds,
                   options={'maxiter':2000, 'ftol':1e-9})
    att = dict(zip(teams, res.x[:n]))
    deff = dict(zip(teams, res.x[n:2*n]))
    mu = res.x[2*n]; home = res.x[2*n+1]; rho = res.x[rho_idx]
    return dict(att=att, deff=deff, mu=mu, home=home, rho=rho, teams=teams)

def dc_predict(model, home, away):
    mu=model['mu']; home_adv=model['home']; rho=model['rho']
    ah=model['att'].get(home,0.0); da=model['deff'].get(away,0.0)
    aa=model['att'].get(away,0.0); dh=model['deff'].get(home,0.0)
    lh=math.exp(mu+home_adv+ah-da)
    la=math.exp(mu+aa-dh)
    # DC 修正比分矩阵
    base=score_matrix(lh,la)
    M=np.zeros_like(base)
    for i in range(base.shape[0]):
        for j in range(base.shape[1]):
            M[i,j]=base[i,j]*tau(lh,la,i,j,rho)
    M/=M.sum()
    return M, lh, la

# ───────────────────────── 评估 ─────────────────────────
def evaluate(pred_matrix_list, actual_list):
    """pred_matrix_list: 每个是 (M, lh, la, method); actual_list: (hg,ag)"""
    logloss=0.0; top1=0; top3=0; n=len(actual_list)
    hda_hit=0
    for (M,lh,la,meth), (hg,ag) in zip(pred_matrix_list, actual_list):
        hg=min(hg,MAX_GOAL); ag=min(ag,MAX_GOAL)
        p=M[hg,ag]
        p=max(p,1e-6)
        logloss += -math.log(p)
        flat=M.flatten()
        order=np.argsort(-flat)
        top=[(divmod(k,MAX_GOAL+1)) for k in order[:3]]
        if (hg,ag) in top: top3+=1
        if (hg,ag)==top[0]: top1+=1
        # H-D-A 还原
        ph=sum(M[i,j] for i in range(MAX_GOAL+1) for j in range(MAX_GOAL+1) if i>j)
        pd_=sum(M[i,i] for i in range(MAX_GOAL+1))
        pa=1-ph-pd_
        pred_res='H' if ph==max(ph,pd_,pa) else ('D' if pd_==max(ph,pd_,pa) else 'A')
        act_res='H' if hg>ag else ('D' if hg==ag else 'A')
        if pred_res==act_res: hda_hit+=1
    return dict(n=n, logloss=logloss/n, top1=top1/n, top3=top3/n, hda=hda_hit/n)

def main():
    data = load_matches()
    print(f"[load] 含赔率+比分的WC场次: {len(data)}")
    by_ed={}
    for d in data: by_ed.setdefault(d['edition'],[]).append(d)
    for ed in sorted(by_ed): print(f"   {ed}: {len(by_ed[ed])} 场")

    # 逐场 OIP
    oip_preds=[]; dc_data=None; actual=[]
    oip_details=[]
    for d in data:
        ph,pd,pa=deoverround(d['oh'],d['od'],d['oa'])
        lh,la=solve_oip(ph,pd,pa)
        M=score_matrix(lh,la)
        oip_preds.append((M,lh,la,'OIP'))
        actual.append((d['hg'],d['ag']))
        oip_details.append(dict(edition=d['edition'],home=d['home'],away=d['away'],
                                oh=d['oh'],od=d['od'],oa=d['oa'],hg=d['hg'],ag=d['ag'],
                                lh=round(lh,3),la=round(la,3)))

    # DC 全量拟合
    dc_model=fit_dc(data, reg=0.3)
    print(f"[DC] 拟合完成: mu={dc_model['mu']:.3f} home={dc_model['home']:.3f} rho={dc_model['rho']:.3f} teams={len(dc_model['teams'])}")
    dc_preds=[]
    for d in data:
        M,lh,la=dc_predict(dc_model,d['home'],d['away'])
        dc_preds.append((M,lh,la,'DC'))

    # 集成: 平均两个比分矩阵
    ens_preds=[]
    for (Mo,_,_,_),(Md,_,_,_) in zip(oip_preds,dc_preds):
        Me=(Mo+Md)/2
        Me/=Me.sum()
        ens_preds.append((Me,0,0,'ENS'))

    print("\n=== 全量评估 (106场含赔率) ===")
    for name,preds in [('OIP',oip_preds),('DC',dc_preds),('ENS',ens_preds)]:
        r=evaluate(preds,actual)
        print(f"  {name:4s}: logloss={r['logloss']:.4f}  Top1={r['top1']:.3f}  Top3={r['top3']:.3f}  H-D-A={r['hda']:.3f}")

    # 1X2 argmax 基线
    base_preds=[]
    for d in data:
        ph,pd,pa=deoverround(d['oh'],d['od'],d['oa'])
        M=np.zeros((MAX_GOAL+1,MAX_GOAL+1))
        # 用argmax赔率构造退化矩阵: 全压胜平负之一
        res='H' if ph==max(ph,pd,pa) else ('D' if pd==max(ph,pd,pa) else 'A')
        M[ (1,0) if res=='H' else ((0,0) if res=='D' else (0,1)) ]=1.0
        base_preds.append((M,0,0,'BASE'))
    rb=evaluate(base_preds,actual)
    print(f"  BASE : logloss={rb['logloss']:.4f}  Top1={rb['top1']:.3f}  Top3={rb['top3']:.3f}  H-D-A={rb['hda']:.3f}  (1X2 argmax)")

    # ───────── OOS: 训练2026 → 测2022 ─────────
    print("\n=== 跨届 OOS: 训练 2026(88) → 测 2022(18) ===")
    train=[d for d in data if d['edition']=='2026']
    test=[d for d in data if d['edition']=='2022']
    if train and test:
        mdl=fit_dc(train,reg=0.3)
        tpreds=[]
        for d in test:
            M,_,_=dc_predict(mdl,d['home'],d['away'])
            tpreds.append((M,0,0,'DC-OOS'))
        # OIP 在测试集(无训练)
        oip_t=[]
        for d in test:
            ph,pd,pa=deoverround(d['oh'],d['od'],d['oa'])
            lh,la=solve_oip(ph,pd,pa)
            oip_t.append((score_matrix(lh,la),0,0,'OIP'))
        ens_t=[((a[0]+b[0])/2) for a,b in zip(oip_t,[ (M,0,0,'x') for M,_,_,_ in tpreds])]
        ens_t=[(m/m.sum(),0,0,'ENS') for m in ens_t]
        act_t=[(d['hg'],d['ag']) for d in test]
        for name,preds in [('OIP',oip_t),('DC',tpreds),('ENS',ens_t)]:
            r=evaluate(preds,act_t)
            print(f"  {name:4s}: logloss={r['logloss']:.4f}  Top1={r['top1']:.3f}  Top3={r['top3']:.3f}  H-D-A={r['hda']:.3f}")

    # ───────── 保存 ─────────
    joblib.dump(dc_model, os.path.join(SAVED,'dc_score_model.joblib'))
    print(f"\n[save] DC模型 → {os.path.join(SAVED,'dc_score_model.joblib')}")

    # 样例CSV (ENS集成的前若干场)
    import csv
    out=[]
    for (M,_,_,_),d in zip(ens_preds,data):
        flat=M.flatten()
        order=np.argsort(-flat)[:5]
        top=[(int(divmod(k,MAX_GOAL+1)[0]),int(divmod(k,MAX_GOAL+1)[1]),round(float(flat[k]),4)) for k in order]
        out.append(dict(edition=d['edition'],home=d['home'],away=d['away'],
                        actual=f"{d['hg']}-{d['ag']}",
                        top1=f"{top[0][0]}-{top[0][1]}@{top[0][2]:.3f}",
                        top2=f"{top[1][0]}-{top[1][1]}@{top[1][2]:.3f}",
                        top3=f"{top[2][0]}-{top[2][1]}@{top[2][2]:.3f}",
                        lh_oip=oip_details[len(out)]['lh'], la_oip=oip_details[len(out)]['la']))
    with open(os.path.join(ROOT,'..','deliverables','dc_score_predictions_sample.csv'),'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['edition','home','away','actual','top1','top2','top3','lh_oip','la_oip'])
        w.writeheader(); w.writerows(out)
    print(f"[save] 样例预测 → deliverables/dc_score_predictions_sample.csv ({len(out)} 场)")

    # 返还关键数字供报告
    return dict(n_total=len(data), oip=evaluate(oip_preds,actual),
                dc=evaluate(dc_preds,actual), ens=evaluate(ens_preds,actual),
                base=rb, dc_model=dc_model)

if __name__=='__main__':
    res=main()
    json.dump({k:v for k,v in res.items() if k!='dc_model'},
              open(os.path.join(ROOT,'..','deliverables','dc_score_metrics.json'),'w'),
              ensure_ascii=False, indent=2)
