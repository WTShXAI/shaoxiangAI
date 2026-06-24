"""Draw quality filter grid search on 2022+2026 combined"""
import pandas as pd, requests, json, time, math

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
all_matches = []
for _,row in wc22.iterrows():
    ho=float(row['close_home_odds']); do=float(row['close_draw_odds']); ao=float(row['close_away_odds'])
    hs=int(row['home_score']); aw=int(row['away_score'])
    a='H' if hs>aw else ('D' if hs==aw else 'A')
    r=requests.post('http://localhost:8000/api/v1/v5/predict',json={'home_odds':ho,'draw_odds':do,'away_odds':ao},timeout=10).json()
    imp=1/ho+1/do+1/ao; ih=(1/ho)/imp; id_=(1/do)/imp; ia_=(1/ao)/imp
    all_matches.append({'ho':ho,'do':do,'ao':ao,'act':a,'imp_h':ih,'imp_a':ia_,'imp_d':id_,
                        'imp_gap':abs(ih-ia_),'overround':imp-1,
                        'jd':r['jepa_draw_prob'],'knn_d':r['knn_probabilities']['D'],
                        'knn_h':r['knn_probabilities']['H'],'knn_a':r['knn_probabilities']['A']})
    time.sleep(0.03)

for m in matches26:
    k=m['home'].replace(' ','').replace('-','')+'_'+m['away'].replace(' ','').replace('-','')
    if k not in ODDS26: continue
    ho,do,ao=ODDS26[k]; a=m['result']
    r=requests.post('http://localhost:8000/api/v1/v5/predict',json={'home_odds':ho,'draw_odds':do,'away_odds':ao},timeout=10).json()
    imp=1/ho+1/do+1/ao; ih=(1/ho)/imp; ia_=(1/ao)/imp; id_=(1/do)/imp
    all_matches.append({'ho':ho,'do':do,'ao':ao,'act':a,'imp_h':ih,'imp_a':ia_,'imp_d':id_,
                        'imp_gap':abs(ih-ia_),'overround':imp-1,
                        'jd':r['jepa_draw_prob'],'knn_d':r['knn_probabilities']['D'],
                        'knn_h':r['knn_probabilities']['H'],'knn_a':r['knn_probabilities']['A']})
    time.sleep(0.03)

n=len(all_matches)
print(f'Total: {n} matches')

# Grid search
best_score=-1; best_config=None
all_scores=[]

for jd_min in [0.5,0.55,0.6,0.65,0.7]:
 for kd_min in [0.15,0.20,0.22,0.25,0.28,0.30]:
  for imp_gap_max in [0.15,0.18,0.20,0.25,0.30,0.40,0.99]:
   for do_min_val in [0,2.5,2.8,3.0,3.2,3.5]:
    for or_max in [0.08,0.10,0.12,0.15,0.99]:
     crt=0; tp=fp=fn=0
     for m in all_matches:
        knn_p='H' if m['knn_h']>=m['knn_a'] else 'A'
        if m['knn_d']>m['knn_h'] and m['knn_d']>m['knn_a']: knn_p='D'
        ok=(m['jd']>jd_min and m['knn_d']>kd_min and 
            m['imp_gap']<imp_gap_max and m['do']>=do_min_val and m['overround']<or_max)
        pred='D' if ok else knn_p
        if pred==m['act']: crt+=1
        if pred=='D' and m['act']=='D': tp+=1
        elif pred=='D': fp+=1
        elif m['act']=='D': fn+=1
     acc=crt/n
     dp=tp/(tp+fp) if(tp+fp)>0 else 0; dr=tp/(tp+fn) if(tp+fn)>0 else 0
     df=2*dp*dr/(dp+dr) if(dp+dr)>0 else 0
     # ROI
     ret=0
     for m in all_matches:
        ok2=(m['jd']>jd_min and m['knn_d']>kd_min and 
             m['imp_gap']<imp_gap_max and m['do']>=do_min_val and m['overround']<or_max)
        p2='D' if ok2 else ('H' if m['knn_h']>=m['knn_a'] else 'A')
        if p2==m['act']: ret+=({'H':m['ho'],'D':m['do'],'A':m['ao']}[p2])
     roi=(ret-n)/n
     sc=0.4*acc+0.3*df+0.3*max(0,roi)
     all_scores.append((jd_min,kd_min,imp_gap_max,do_min_val,or_max,acc,df,tp+fp,roi,sc))
     if sc>best_score: best_score=sc; best_config=(jd_min,kd_min,imp_gap_max,do_min_val,or_max,acc,df,tp+fp,roi)

# Report
jd,kd,ig,do_min_v,orm,acc,df,dpred,roi=best_config
print(f'\nBEST: jd>{jd} kd>{kd} gap<{ig} do>{do_min_v} over<{orm}')
print(f'  Acc={acc:.1%} DrawF1={df:.4f} D_pred={dpred} ROI={roi:+.1%}')

all_scores.sort(key=lambda x:x[9],reverse=True)
print('\nTop 8 by score:')
for a in all_scores[:8]:
    print(f'  jd>{a[0]} kd>{a[1]} gap<{a[2]} do>{a[3]} over<{a[4]} Acc={a[5]:.1%} DF1={a[6]:.3f} D={a[7]} ROI={a[8]:+.1%} sc={a[9]:.4f}')
