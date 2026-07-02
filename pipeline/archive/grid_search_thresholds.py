"""Grid search threshold combinations to find Canada fix with minimal FP impact"""
import sys,os,math,warnings;from pathlib import Path
warnings.filterwarnings('ignore')
p1=Path(r'D:/Architecture');p2=Path(r'D:/AI/footballAI')
from predictors.unified_predictor import UnifiedPredictor
up=UnifiedPredictor(model_path=str(p2/'saved_models'/'football_v4.1_production.joblib'),enable_trap=False,enable_dh=False,use_threshold=False)
from rules.d_gate_v52 import get_cover_adjustment, COVER_DB

MATCHES=[['加拿大','波黑',1.84,3.45,4.60,-0.5,2.5,'D','1-1'],
['美国','巴拉圭',1.66,3.55,5.70,-0.75,2.5,'H','4-1'],
['卡塔尔','瑞士',5.60,3.75,1.61,1.0,2.5,'D','1-1'],
['巴西','摩洛哥',1.39,4.50,7.50,-1.5,2.5,'D','1-1'],
['海地','苏格兰',6.90,4.50,1.40,1.5,2.5,'A','0-1'],
['澳大利亚','土耳其',4.55,3.35,1.76,0.5,2.5,'H','2-0'],
['德国','库拉索',1.53,4.15,5.20,-1.0,3.5,'H','7-1'],
['瑞典','突尼斯',1.76,3.35,4.70,-0.5,2.5,'H','5-1'],
['科特迪瓦','厄瓜多尔',2.60,3.35,2.60,0.0,2.5,'H','1-0'],
['荷兰','日本',1.63,3.90,4.70,-0.5,2.5,'D','2-2'],
['伊朗','新西兰',1.44,4.25,6.30,-1.25,2.5,'D','2-2'],
['比利时','埃及',1.39,4.50,7.10,-1.5,2.5,'D','1-1'],
['沙特阿拉伯','乌拉圭',7.10,4.50,1.39,1.5,2.5,'D','1-1'],
['西班牙','佛得角共和国',1.08,8.80,18.0,-2.5,3.5,'D','0-0'],
['伊拉克','挪威',3.10,3.40,2.14,0.25,2.5,'A','1-4'],
['奥地利','约旦',1.46,4.15,6.20,-1.0,2.5,'H','3-1'],
['法国','塞内加尔',1.08,8.80,20.0,-2.5,3.5,'H','3-1'],
['阿根廷','阿尔及利亚',1.60,3.85,5.00,-0.5,2.5,'H','3-0'],
['乌兹别克斯坦','哥伦比亚',5.60,4.05,1.52,1.0,2.5,'A','1-3'],
['加纳','巴拿马',1.52,3.95,5.70,-1.0,2.5,'H','1-0'],
['英格兰','克罗地亚',1.30,5.00,8.30,-1.5,2.5,'H','4-2'],
['葡萄牙','民主刚果',1.22,5.90,10.0,-1.75,3.0,'D','1-1'],
['加拿大','卡塔尔',1.61,3.75,5.00,-0.5,2.5,'H','6-0'],
['墨西哥','韩国',1.69,3.45,4.90,-0.5,2.5,'H','1-0'],
['捷克','南非',1.61,3.40,5.20,-0.75,2.5,'D','1-1'],
['瑞士','波黑',1.61,3.75,5.00,-0.5,2.5,'H','4-1'],
['土耳其','巴拉圭',2.03,3.15,3.60,-0.5,2.5,'H','2-0'],
['巴西','海地',1.06,10.5,17.5,-2.75,3.75,'H','3-0'],
['美国','澳大利亚',1.55,3.95,5.30,-1.0,2.5,'H','2-0'],
['苏格兰','摩洛哥',3.70,3.15,2.00,0.5,2.5,'A','0-1'],
['厄瓜多尔','库拉索',1.19,6.10,12.5,-1.75,2.75,'D','0-0'],
['德国','科特迪瓦',1.53,4.15,5.20,-1.0,2.75,'H','2-1'],
['突尼斯','日本',4.90,3.45,1.69,0.75,2.5,'A','1-5'],
['荷兰','瑞典',1.63,3.90,4.70,-0.5,2.5,'H','5-1'],
['西班牙','沙特阿拉伯',1.08,8.80,18.0,-2.5,3.5,'H','4-0'],
['乌拉圭','佛得角共和国',1.44,4.25,6.30,-1.25,2.5,'D','2-2'],
['比利时','伊朗',1.39,4.50,7.10,-1.5,2.5,'D','0-0'],
['新西兰','埃及',4.55,3.35,1.76,0.75,2.5,'H','1-0']]

def grid_test(spread_th, spread_mult, def_th, modeA_th, s7_mult):
    c=0;dr=0;fp=0;tp=0;canada_hit=False
    for m in MATCHES:
        h,a,oh,od,oa,hcp,ou,act,sc=m
        try:
            r=up.predict(home=h,away=a,odds_h=oh,odds_d=od,odds_a=oa,asian_handicap=hcp,ou_line=ou)
            p=r.get('probabilities',{});ph=p.get('H',0);pd=p.get('D',0);pa=p.get('A',0)
        except:t=1/oh+1/od+1/oa;ph=1/oh/t;pd=1/od/t;pa=1/oa/t
        spread=abs(ph-pa);max_imp=max(ph,pa)
        s1=od/math.sqrt(oh*oa);s7=ou/max(abs(hcp),0.25)
        cover_mult,_=get_cover_adjustment(h,a) if h else(1.0,'')
        d_boost=None;mode='normal'
        
        if max_imp>=0.70:
            d=pd*1.08
            d*=2.2 if(max_imp>0.75 or abs(hcp)>=1.75) else 1.8
            if od>9.5 and ou>=3.5 and abs(hcp)>=2.5:d*=0.3
            elif od>9.5 and abs(hcp)>=2.5:d*=0.5
            d*=cover_mult
            if d>0.14:mode,d_boost='C',d
        if pa>0.65 and max_imp<0.70 and d_boost is None:
            d=pd*1.08*2.0;d*=cover_mult
            if d>0.14:mode,d_boost='C-away',d
        if 0.48<=max_imp<=0.70 and d_boost is None:
            d=pd*1.08*max(0.80,1-spread*0.30)
            if ou<=2.5:d*=1.05
            s7_th=6.0 if abs(hcp)>=1.75 else(4.5 if abs(hcp)>=1.0 else(3.5 if abs(hcp)>=0.5 else 2.5))
            if s7>=s7_th and s1<1.35:d*=s7_mult
            d*=cover_mult
            if d>modeA_th:mode,d_boost='A',d
        if spread<0.15 and d_boost is None:
            d=pd*1.08*1.20
            if d>0.44:mode,d_boost='B',d
        if d_boost is None:
            d=pd*1.08
            if spread>0.40:d*=0.70
            elif spread>spread_th:d*=spread_mult
            s7_th=6.0 if abs(hcp)>=1.75 else(4.5 if abs(hcp)>=1.0 else(3.5 if abs(hcp)>=0.5 else 2.5))
            if s7>=s7_th and s1<1.35:d*=s7_mult
            d*=cover_mult
            if d>def_th:mode,d_boost='default',d
        
        th={'C':0.14,'C-away':0.14,'A':modeA_th,'B':0.44,'default':def_th}
        v='D' if mode in th and d_boost>th[mode] else('H'if ph>pa else'A')
        if v==act:c+=1
        if act=='D' and v=='D':dr+=1;tp+=1
        if v=='D' and act!='D':fp+=1
        if h=='加拿大' and a=='波黑' and v=='D':canada_hit=True
    ad=sum(1 for m in MATCHES if m[6]=='D')
    return c,dr,fp,canada_hit

# Grid search - key combinations
configs=[
    (0.20, 0.85, 0.32, 0.28, 0.70, 'v5.2.2 (baseline)'),
    (0.20, 0.85, 0.30, 0.28, 0.70, 'def_th 0.32->0.30'),
    (0.20, 0.85, 0.32, 0.26, 0.70, 'modeA_th 0.28->0.26'),
    (0.20, 0.88, 0.32, 0.28, 0.70, 'spread 0.85->0.88'),
    (0.20, 0.90, 0.32, 0.28, 0.70, 'spread 0.85->0.90'),
    (0.20, 0.85, 0.32, 0.28, 0.78, 'S7 0.70->0.78'),
    (0.20, 0.85, 0.32, 0.28, 0.80, 'S7 0.70->0.80'),
    (0.20, 0.88, 0.32, 0.28, 0.78, 'spread88+S778'),
    (0.20, 0.90, 0.32, 0.28, 0.80, 'spread90+S780'),
]

print(f'{"Config":<22} {"Acc":>5} {"D-Recall":>8} {"FP":>4} {"Canada":>7}')
print('-'*54)
baseline_c, baseline_fp = 0, 0
for sp_th,sp_mult,def_th,ma_th,s7_mult,label in configs:
    c,dr,fp,ch=grid_test(sp_th,sp_mult,def_th,ma_th,s7_mult)
    if label=='v5.2.2 (baseline)': baseline_c,baseline_fp=c,fp
    canada_mark='✅'if ch else'❌'
    delta=c-baseline_c
    d_str=f'+{delta}'if delta>0 else str(delta)
    fp_delta=fp-baseline_fp
    fp_str=f'+{fp_delta}'if fp_delta>0 else str(fp_delta)
    print(f'{label:<22} {c:>3}/38 {dr:>3}/13  {fp:>4}({fp_str}) {canada_mark:>7}')

# Find the minimal FP increase that saves Canada
print(f'\n=== 最优解搜索 ===')
best=None
for sp_th,sp_mult,def_th,ma_th,s7_mult,label in configs:
    if label=='v5.2.2 (baseline)': continue
    c,dr,fp,ch=grid_test(sp_th,sp_mult,def_th,ma_th,s7_mult)
    fp_delta=fp-baseline_fp
    if ch:  # Canada saved
        if best is None or fp_delta<best[0]:
            best=(fp_delta,c,dr,fp,label)
if best:
    print(f'最优: {best[4]} (FP+{best[0]}, Acc={best[1]})')
else:
    print('无解: 所有方案都无法同时拯救Canada且不增FP')
