"""验证操盘手(收盘盘口锚定)解析度是否还能提高.
基线: 盲跟收盘 argmax(devig close) = 51.9% (IW 140729)
测试杠杆:
  A. 纯 Elo 实力 (独立特征)
  B. Elo + 收盘赔率 多分类逻辑回归 (模型)
  C. 分歧混合: 仅当模型与操盘手看法不同且模型conf高时跟模型, 否则跟操盘手
  D. 诱盘条件: 诱盘组跟开盘看好方
  E. 阻盘条件: 阻盘组跟非开盘看好方(反向下注)
时间切分 train<2023 / test>=2023.
"""
import sqlite3, numpy as np

DB = "data/football_data.db"
con = sqlite3.connect(DB)
rows = con.execute("""
    SELECT home_team, away_team, match_date,
           open_home_odds,open_draw_odds,open_away_odds,
           close_home_odds,close_draw_odds,close_away_odds, final_result
    FROM interwetten_odds
    WHERE close_home_odds>0 AND open_home_odds>0 AND final_result IN ('H','D','A')
""").fetchall()
print(f"样本: {len(rows)}")

def devig(h,d,a):
    inv = 1/h+1/d+1/a
    return np.array([(1/h)/inv,(1/d)/inv,(1/a)/inv])

lbl = ["H","D","A"]
y = np.array([{"H":0,"D":1,"A":2}[r[9]] for r in rows])

# ---- Elo 回放 (按日期顺序, 全时间线, 部署态合理) ----
teams = {}
def get_r(t): return teams.get(t, 1500.0)
def upd(h,a,res):
    rh, ra = get_r(h), get_r(a)
    eh = 1/(1+10**((ra-rh)/400)); ea = 1-eh
    s = {0:1.0,1:0.5,2:0.0}[res]  # home score
    K=30
    teams[h]=rh+K*(s-eh); teams[a]=ra+K*((1-s)-ea)

order = sorted(range(len(rows)), key=lambda i: rows[i][2] or "")
eh_=np.zeros(len(rows)); ea_=np.zeros(len(rows)); ed_=np.zeros(len(rows))
for i in order:
    h,a = rows[i][0], rows[i][1]
    eh_[i]=get_r(h); ea_[i]=get_r(a); ed_[i]=eh_[i]-ea_[i]
    upd(h,a,y[i])

# ---- 特征 ----
oh = np.array([r[3] for r in rows], float); od=np.array([r[4] for r in rows],float); oa=np.array([r[5] for r in rows],float)
ch = np.array([r[6] for r in rows], float); cd=np.array([r[7] for r in rows],float); ca=np.array([r[8] for r in rows],float)
close_p = np.stack([devig(ch[i],cd[i],ca[i]) for i in range(len(rows))])  # (n,3)
elo_win = 1/(1+10**(-ed_/400))

# 时间切分
cut = "2023-01-01"
tr = np.array([(rows[i][2] or "") < cut for i in range(len(rows))])
te = ~tr
print(f"train={tr.sum()} test={te.sum()}")

# ---- 基线: 盲跟收盘 ----
def acc(mask, pred): return (pred[mask]==y[mask]).mean()
base_pred = close_p.argmax(1)
print(f"\n[基线] 盲跟收盘 (全): {acc(np.ones(len(y),bool), base_pred)*100:.1f}%")
print(f"[基线] 盲跟收盘 (test): {acc(te, base_pred)*100:.1f}%")

# ---- A. 纯 Elo ----
elo_pred = np.where(ed_>0,0, np.where(ed_<0,2,1))
print(f"[A] 纯 Elo (test): {acc(te, elo_pred)*100:.1f}%")

# ---- B. 多分类逻辑回归 (Elo + 收盘赔率) ----
def train_mlr(Xtr, ytr, iters=300, lr=0.1):
    n,d = Xtr.shape; W = np.zeros((d,3))
    for _ in range(iters):
        logits = Xtr@W
        logits -= logits.max(1,keepdims=True)
        e=np.exp(logits); p=e/e.sum(1,keepdims=True)
        p[np.arange(n),ytr]-=1
        W -= lr* (Xtr.T@p)/n
    return W
Xtr = np.column_stack([close_p[tr], elo_win[tr,None], ed_[tr,None]/400.0, (eh_[tr]-1500)/400.0, (ea_[tr]-1500)/400.0])
mu, sd = Xtr.mean(0), Xtr.std(0)+1e-8
Xtr = (Xtr-mu)/sd
W = train_mlr(Xtr, y[tr])
Xte = np.column_stack([close_p[te], elo_win[te,None], ed_[te,None]/400.0, (eh_[te]-1500)/400.0, (ea_[te]-1500)/400.0])
Xte = (Xte-mu)/sd
mlp = (Xte@W); mlp -= mlp.max(1,keepdims=True); ep=np.exp(mlp); p_te=ep/ep.sum(1,keepdims=True)
model_pred = p_te.argmax(1)
# 对齐到全长度, 便于与 te 掩码一致
model_pred_full = np.full(len(y), -1, dtype=int); model_pred_full[te] = model_pred
print(f"[B] Elo+赔率模型 (test): {acc(te, model_pred_full)*100:.1f}%  (模型conf均值={p_te.max(1).mean():.3f})")

# ---- C. 分歧混合: 模型≠操盘手 且 模型conf>thr 时跟模型 ----
op_pred = base_pred[te]
disagree = model_pred != op_pred
for thr in [0.40,0.45,0.50]:
    blend = op_pred.copy()
    take = disagree & (p_te.max(1)>thr)
    blend[take] = model_pred[take]
    blend_full = np.full(len(y), -1, dtype=int); blend_full[te] = blend
    print(f"[C] 分歧混合(conf>{thr}, 覆盖{(take.sum()/te.sum()*100):.1f}%): {acc(te, blend_full)*100:.1f}%")

# ---- D/E 条件: 阻/诱 (均在 test 上评估) ----
ro_all = np.stack([devig(oh[i],od[i],oa[i]) for i in range(len(rows))]).argmax(1)
rise = ch>oh   # 阻盘(升赔)
fall = ch<oh   # 诱盘(降赔)
for name, mask, rule in [
    ("D-诱盘跟开盘看好方", fall, ro_all),
    ("E-阻盘跟非开盘看好方(反向)", rise, np.where(ro_all==0,1, np.where(ro_all==1,2,0))),
]:
    m = mask & te
    print(f"[{name}] test覆盖{int(m.sum())}={ (m.sum()/te.sum()*100):.1f}%: {acc(m, rule)*100:.1f}%")
