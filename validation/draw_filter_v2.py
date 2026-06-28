"""Draw quality filter grid search - direct module (no API)"""
import sys, os, json, math, numpy as np

import torch, pandas as pd
from models.jepa import JEPALite

# ── Load model once ──
ckpt = torch.load('models/jepa/checkpoints/best_model_lite.pt', map_location='cpu', weights_only=False)
model = JEPALite(); model.load_state_dict(ckpt['model'], strict=True); model.eval()

# ── Training stats ──
train = np.load('data/jepa_train.npz', allow_pickle=True)
mean = train['static'].mean(axis=0).astype(np.float32); std = train['static'].std(axis=0).astype(np.float32)
std[std < 1e-8] = 1.0
tHo = train['static'][:,0]*20; tDo = train['static'][:,1]*20; tAo = train['static'][:,2]*20
t_labels = train['labels']

# ── Feature builder (minimal) ──
def knn_prob(ho,do,oa,k=500):
    imp=1/ho+1/do+1/oa; qih=(1/ho)/imp; qid=(1/do)/imp; qia=(1/oa)/imp
    timp=1/tHo+1/tDo+1/tAo; tih=(1/tHo)/timp; tid=(1/tDo)/timp; tia=(1/tAo)/timp
    d=np.sqrt((tih-qih)**2+(tid-qid)**2+(tia-qia)**2)
    top=np.argsort(d)[:k]; l=t_labels[top]
    return np.array([(l==0).mean(),(l==1).mean(),(l==2).mean()])

# Feature builder
COLS=['close_home_odds','close_draw_odds','close_away_odds','open_home_odds','open_draw_odds','open_away_odds','real_home_odds','real_draw_odds','real_away_odds','odds_imp_h','odds_imp_d','odds_imp_a','prob_h','prob_d','prob_a','imp_h','imp_d','imp_a','odds_overround','odds_balance','odds_confidence','odds_ratio','odds_spread','odds_entropy','odds_move_h','odds_move_d','odds_move_a','odds_move_magnitude','odds_fav_move','market_fav_strength','market_disagreement','odds_model_diverge','draw_odds_attract','draw_with_ht_draw','home_points_avg_10','home_points_avg_5','home_win_avg_10','away_points_avg_10','h_team_draw_rate','a_team_draw_rate','league_draw_rate','league_avg_goals','ht_draw_composite','ht_draw_prob','ht_00_prob','ht_goal_pressure','ht_h_lead_prob','ht_scoring_diff','exp_ht_goals','exp_total_goals','drift_h','drift_d','drift_a','drift_h_val','drift_a_val','drift_divergence','imp_d_norm','a1','a5','a6','a7','a8','sigma_trap','lambda_crush','epsilon_senti','rank_diff_factor','form_momentum','h2h_factor','rank_factor','form_factor','is_cold_start','feat_coverage_ratio']
CIDX={n:i for i,n in enumerate(COLS)}
def build_f(ho,do,oa):
    v=mean.copy(); imp=1/ho+1/do+1/oa; ih=(1/ho)/imp; id_=(1/do)/imp; ia_=(1/oa)/imp
    for k in ['close_home_odds','open_home_odds','real_home_odds']: v[CIDX[k]]=ho
    for k in ['close_draw_odds','open_draw_odds','real_draw_odds']: v[CIDX[k]]=do
    for k in ['close_away_odds','open_away_odds','real_away_odds']: v[CIDX[k]]=oa
    for k in ['odds_imp_h','prob_h','imp_h']: v[CIDX[k]]=ih
    for k in ['odds_imp_d','prob_d','imp_d']: v[CIDX[k]]=id_
    for k in ['odds_imp_a','prob_a','imp_a']: v[CIDX[k]]=ia_
    v[CIDX['odds_overround']]=imp-1; v[CIDX['odds_balance']]=abs(ih-ia_)
    v[CIDX['odds_confidence']]=math.sqrt((ih-1/3)**2+(id_-1/3)**2+(ia_-1/3)**2)*3
    v[CIDX['odds_ratio']]=(1/ho)/(1/oa)if oa>0 else 1; v[CIDX['odds_spread']]=oa-ho
    v[CIDX['odds_entropy']]=-sum(p*math.log(max(p,1e-9))for p in[ih,id_,ia_])
    v[CIDX['market_fav_strength']]=max(1/ho,1/do,1/oa)/imp; v[CIDX['odds_model_diverge']]=ih-.33
    v[CIDX['draw_odds_attract']]=max(0,min(1,1-(do-3)/2))
    v[CIDX['league_draw_rate']]=.35; v[CIDX['league_avg_goals']]=2.5; v[CIDX['imp_d_norm']]=id_
    a1=ih;a5=min(id_,1);a6=min(1-abs(ih-ia_),1);a7=min(ih*.5+ia_*.5,1);a8=min(abs(id_-1/3)*3,1)
    v[CIDX['a1']]=a1;v[CIDX['a5']]=a5;v[CIDX['a6']]=a6;v[CIDX['a7']]=a7;v[CIDX['a8']]=a8
    v[CIDX['lambda_crush']]=min(a1*a5*2,1);v[CIDX['epsilon_senti']]=min(a1*a6*2,1)
    v[CIDX['rank_diff_factor']]=(ih-ia_)*3;v[CIDX['is_cold_start']]=1;v[CIDX['feat_coverage_ratio']]=.5
    v=(v-mean)/std; v=np.clip(v,-5,5); return v.astype(np.float32)

def jepa_raw_draw(ho,do,oa):
    f=build_f(ho,do,oa); x=torch.from_numpy(f).unsqueeze(0).float()
    with torch.no_grad():
        s0=model.encode(x); als=[]
        for _ in range(30):
            sT=model.predictor(s0); sT=sT+torch.randn_like(sT)*0.04
            als.append(model.output_head(s0,sT))
        als=torch.stack(als,dim=0)
        probs=torch.softmax(als,dim=-1).mean(0).squeeze(0).numpy()
    return float(probs[1])

# ── 2022 WC from Parquet ──
df = pd.read_parquet('data/ht_enhanced_training_v6.parquet')
wc_zh = ['卡塔尔','厄瓜多尔','塞内加尔','荷兰','英格兰','伊朗','美国','威尔士',
         '阿根廷','沙特','沙特阿拉伯','墨西哥','波兰','法国','澳大利亚','丹麦','突尼斯',
         '西班牙','哥斯达黎加','德国','日本','比利时','加拿大','摩洛哥','克罗地亚',
         '巴西','塞尔维亚','瑞士','喀麦隆','葡萄牙','加纳','乌拉圭','韩国']
dates = df[(df['match_date']>='2022-11-20')&(df['match_date']<='2022-12-18')]
wc22 = dates[dates['home_team'].isin(wc_zh) & dates['away_team'].isin(wc_zh)].sort_values('match_date')

# ── 2026 WC ──
ODDS26={'Canada_Bosnia':(6.0,2.58,3.0),'USA_Paraguay':(7.8,5.9,1.6),'Qatar_Switzerland':(2.14,1.93,6.7),'Brazil_Morocco':(1.7,3.6,5.3),'Haiti_Scotland':(5.9,4.6,2.07),'Australia_Turkey':(4.95,3.75,1.71),'Germany_Curacao':(1.91,2.03,4.95),'Sweden_Tunisia':(1.92,3.4,4.1),'IvoryCoast_Ecuador':(3.5,2.88,2.36),'Iran_NewZealand':(1.85,3.35,4.55),'Belgium_Egypt':(1.63,2.25,5.2),'France_Senegal':(1.45,4.4,7.5),'Argentina_Algeria':(1.94,1.93,7.9),'Uzbekistan_Colombia':(8.4,1.99,2.01),'England_Croatia':(1.73,3.65,4.95),'Portugal_DRCongo':(1.28,5.6,1.84),'Mexico_SouthKorea':(2.76,3.25,3.95),'Czech_SouthAfrica':(1.82,3.6,4.35),'Switzerland_Bosnia':(1.58,4.05,5.7),'Ecuador_Curacao':(1.7,6.1,2.41),'Tunisia_Japan':(4.9,3.45,1.69),'Netherlands_Sweden':(1.63,2.11,4.7)}
with open('validation/wc2026_results.json') as f: matches26=json.load(f)['matches']

# Build combined
all_m = []
for _,row in wc22.iterrows():
    ho=float(row['close_home_odds']); do=float(row['close_draw_odds']); ao=float(row['close_away_odds'])
    hs=int(row['home_score']); aw=int(row['away_score'])
    a='H' if hs>aw else ('D' if hs==aw else 'A')
    imp=1/ho+1/do+1/ao; ih=(1/ho)/imp; ia_=(1/ao)/imp; id_=(1/do)/imp
    jd=jepa_raw_draw(ho,do,ao); knn=knn_prob(ho,do,ao)
    all_m.append({'ho':ho,'do':do,'ao':ao,'act':a,'imp_gap':abs(ih-ia_),'overround':imp-1,'jd':jd,'knn_h':knn[0],'knn_d':knn[1],'knn_a':knn[2]})

for m in matches26:
    k=m['home'].replace(' ','').replace('-','')+'_'+m['away'].replace(' ','').replace('-','')
    if k not in ODDS26: continue
    ho,do,ao=ODDS26[k]; a=m['result']
    imp=1/ho+1/do+1/ao; ih=(1/ho)/imp; ia_=(1/ao)/imp
    jd=jepa_raw_draw(ho,do,ao); knn=knn_prob(ho,do,ao)
    all_m.append({'ho':ho,'do':do,'ao':ao,'act':a,'imp_gap':abs(ih-ia_),'overround':imp-1,'jd':jd,'knn_h':knn[0],'knn_d':knn[1],'knn_a':knn[2]})

n=len(all_m); print(f'Total: {n} matches')

# Simple grid: just test the imp_gap + do_min filters
best_sc = -1; best_cfg = None
for jd_min in [0.5,0.55,0.6]:
 for kd_min in [0.20,0.25,0.28,0.30]:
  for gap_max in [0.15,0.20,0.25,0.30,0.99]:
   for do_min_val in [2.5,2.8,3.0,3.2]:
    crt=tp=fp=fn=0
    for m in all_m:
        knn_p='H' if m['knn_h']>=m['knn_a'] else 'A'
        if m['knn_d']>m['knn_h'] and m['knn_d']>m['knn_a']: knn_p='D'
        ok=(m['jd']>jd_min and m['knn_d']>kd_min and m['imp_gap']<gap_max and m['do']>=do_min_val)
        p='D' if ok else knn_p
        if p==m['act']: crt+=1
        if p=='D' and m['act']=='D': tp+=1
        elif p=='D': fp+=1
        elif m['act']=='D': fn+=1
    acc=crt/n; dp=tp/(tp+fp)if(tp+fp)>0 else 0; dr=tp/(tp+fn)if(tp+fn)>0 else 0
    df=2*dp*dr/(dp+dr)if(dp+dr)>0 else 0
    ret=0
    for m in all_m:
        ok2=(m['jd']>jd_min and m['knn_d']>kd_min and m['imp_gap']<gap_max and m['do']>=do_min_val)
        p2='D' if ok2 else ('H' if m['knn_h']>=m['knn_a'] else 'A')
        if p2==m['act']: ret+=({'H':m['ho'],'D':m['do'],'A':m['ao']}[p2])
    roi=(ret-n)/n; sc=0.4*acc+0.3*df+0.3*max(0,roi)
    if sc>best_sc: best_sc=sc; best_cfg=(jd_min,kd_min,gap_max,do_min_val,acc,df,tp+fp,roi)

jd,kd,gap,dmin,acc,df,dpred,roi=best_cfg
print(f'\nBEST: jd>{jd} kd>{kd} imp_gap<{gap} do>{dmin}')
print(f'  Acc={acc:.1%} DrawF1={df:.4f} D_pred={dpred} ROI={roi:+.1%}')

# Non-filtered baseline
bl_crt=bl_tp=bl_fp=bl_fn=0
for m in all_m:
    knn_p='H' if m['knn_h']>=m['knn_a'] else 'A'
    if m['knn_d']>m['knn_h'] and m['knn_d']>m['knn_a']: knn_p='D'
    p='D' if(m['jd']>0.5 and m['knn_d']>0.25) else knn_p
    if p==m['act']: bl_crt+=1
    if p=='D' and m['act']=='D': bl_tp+=1
    elif p=='D': bl_fp+=1
    elif m['act']=='D': bl_fn+=1
bl_acc=bl_crt/n; bl_dp=bl_tp/(bl_tp+bl_fp)if(bl_tp+bl_fp)>0 else 0
bl_dr=bl_tp/(bl_tp+bl_fn)if(bl_tp+bl_fn)>0 else 0; bl_df=2*bl_dp*bl_dr/(bl_dp+bl_dr)if(bl_dp+bl_dr)>0 else 0
print(f'\nBaseline: Acc={bl_acc:.1%} DrawF1={bl_df:.4f} D_pred={bl_tp+bl_fp}')
print(f'Filtered: Acc={acc:.1%} DrawF1={df:.4f} D_pred={dpred}')
