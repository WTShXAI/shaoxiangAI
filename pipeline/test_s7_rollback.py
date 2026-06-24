"""Test S7 threshold rollback: v5.2.1 (S7=3.0 away_fav) vs v5.2.2 (S7=3.5 unified)"""
import sys,os,math,warnings
from pathlib import Path
warnings.filterwarnings('ignore')
ARCH_ROOT=Path(r'D:/Architecture v4.0');FAI_ROOT=Path(r'D:/AI/footballAI')
sys.path.insert(0,str(ARCH_ROOT));sys.path.insert(0,str(ARCH_ROOT/'features'))
sys.path.insert(0,str(ARCH_ROOT/'predictors'));sys.path.insert(0,str(FAI_ROOT))
# Use raw odds-implied probabilities (model path incompatible with py3.14)
# This is equivalent to v5.1 baseline, S7/S1 logic is what we're testing

def s7_v52(hcp):
    ah=abs(hcp)
    if ah>=1.75:return 6.0
    elif ah>=1.0:return 4.5
    elif ah>=0.5:return 3.5
    else:return 2.5

def s7_v521(hcp):
    ah=abs(hcp)
    if ah>=1.75:return 6.0
    elif ah>=1.0:return 4.5
    elif ah>=0.5:
        if hcp>0:return 3.0
        return 3.5
    else:return 2.5

def s7_v522(hcp):
    ah=abs(hcp)
    if ah>=1.75:return 6.0
    elif ah>=1.0:return 4.5
    elif ah>=0.5:return 3.5
    else:return 2.5

MATCHES=[['加拿大','波黑',1.84,3.45,4.60,-0.5,2.5,'D','1-1','6.13'],
['美国','巴拉圭',1.66,3.55,5.70,-0.75,2.5,'H','4-1','6.13'],
['卡塔尔','瑞士',5.60,3.75,1.61,1.0,2.5,'D','1-1','6.14'],
['巴西','摩洛哥',1.39,4.50,7.50,-1.5,2.5,'D','1-1','6.14'],
['海地','苏格兰',6.90,4.50,1.40,1.5,2.5,'A','0-1','6.14'],
['澳大利亚','土耳其',4.55,3.35,1.76,0.5,2.5,'H','2-0','6.14'],
['德国','库拉索',1.53,4.15,5.20,-1.0,3.5,'H','7-1','6.15'],
['瑞典','突尼斯',1.76,3.35,4.70,-0.5,2.5,'H','5-1','6.15'],
['科特迪瓦','厄瓜多尔',2.60,3.35,2.60,0.0,2.5,'H','1-0','6.15'],
['荷兰','日本',1.63,3.90,4.70,-0.5,2.5,'D','2-2','6.15'],
['伊朗','新西兰',1.44,4.25,6.30,-1.25,2.5,'D','2-2','6.16'],
['比利时','埃及',1.39,4.50,7.10,-1.5,2.5,'D','1-1','6.16'],
['沙特阿拉伯','乌拉圭',7.10,4.50,1.39,1.5,2.5,'D','1-1','6.16'],
['西班牙','佛得角共和国',1.08,8.80,18.0,-2.5,3.5,'D','0-0','6.16'],
['伊拉克','挪威',3.10,3.40,2.14,0.25,2.5,'A','1-4','6.17'],
['奥地利','约旦',1.46,4.15,6.20,-1.0,2.5,'H','3-1','6.17'],
['法国','塞内加尔',1.08,8.80,20.0,-2.5,3.5,'H','3-1','6.17'],
['阿根廷','阿尔及利亚',1.60,3.85,5.00,-0.5,2.5,'H','3-0','6.17'],
['乌兹别克斯坦','哥伦比亚',5.60,4.05,1.52,1.0,2.5,'A','1-3','6.18'],
['加纳','巴拿马',1.52,3.95,5.70,-1.0,2.5,'H','1-0','6.18'],
['英格兰','克罗地亚',1.30,5.00,8.30,-1.5,2.5,'H','4-2','6.18'],
['葡萄牙','民主刚果',1.22,5.90,10.0,-1.75,3.0,'D','1-1','6.18'],
['加拿大','卡塔尔',1.61,3.75,5.00,-0.5,2.5,'H','6-0','6.19'],
['墨西哥','韩国',1.69,3.45,4.90,-0.5,2.5,'H','1-0','6.19'],
['捷克','南非',1.61,3.40,5.20,-0.75,2.5,'D','1-1','6.19'],
['瑞士','波黑',1.61,3.75,5.00,-0.5,2.5,'H','4-1','6.19'],
['土耳其','巴拉圭',2.03,3.15,3.60,-0.5,2.5,'H','2-0','6.20'],
['巴西','海地',1.06,10.5,17.5,-2.75,3.75,'H','3-0','6.20'],
['美国','澳大利亚',1.55,3.95,5.30,-1.0,2.5,'H','2-0','6.20'],
['苏格兰','摩洛哥',3.70,3.15,2.00,0.5,2.5,'A','0-1','6.20'],
['厄瓜多尔','库拉索',1.19,6.10,12.5,-1.75,2.75,'D','0-0','6.21'],
['德国','科特迪瓦',1.53,4.15,5.20,-1.0,2.75,'H','2-1','6.21'],
['突尼斯','日本',4.90,3.45,1.69,0.75,2.5,'A','1-5','6.21'],
['荷兰','瑞典',1.63,3.90,4.70,-0.5,2.5,'H','5-1','6.21'],
['西班牙','沙特阿拉伯',1.08,8.80,18.0,-2.5,3.5,'H','4-0','6.22'],
['乌拉圭','佛得角共和国',1.44,4.25,6.30,-1.25,2.5,'D','2-2','6.22'],
['比利时','伊朗',1.39,4.50,7.10,-1.5,2.5,'D','0-0','6.22'],
['新西兰','埃及',4.55,3.35,1.76,0.75,2.5,'H','1-0','6.22']]

def run_backtest(s7_fn, s1_thresh):
    c=0;dr=0;fp=0;tp=0;changes=[]
    for m in MATCHES:
        home,away,oh,od,oa,hcp,ou,act,score,date=m
        t=1/oh+1/od+1/oa;ph=1/oh/t;pd=1/od/t;pa=1/oa/t
        spread=abs(ph-pa);max_imp=max(ph,pa)
        s1=od/math.sqrt(oh*oa);s7=ou/max(abs(hcp),0.25)
        s7_thresh=s7_fn(hcp)
        d_boost=None;mode='normal'
        if max_imp>=0.70:
            d=pd*1.08
            d*=2.2 if(max_imp>0.75 or abs(hcp)>=1.75) else 1.8
            if od>9.5 and ou>=3.5 and abs(hcp)>=2.5:d*=0.3
            elif od>9.5 and abs(hcp)>=2.5:d*=0.5
            if d>0.14:mode,d_boost='C',d
        if pa>0.65 and max_imp<0.70 and d_boost is None:
            d=pd*1.08*2.0
            if d>0.14:mode,d_boost='C-away',d
        if 0.48<=max_imp<=0.70 and d_boost is None:
            d=pd*1.08*max(0.80,1-spread*0.30)
            if ou<=2.5:d*=1.05
            if s7>=s7_thresh and s1<s1_thresh:d*=0.70
            if d>0.28:mode,d_boost='A',d
        if spread<0.15 and d_boost is None:
            d=pd*1.08*1.20
            if d>0.43:mode,d_boost='B',d
        if d_boost is None:
            d=pd*1.08
            if spread>0.40:d*=0.70
            elif spread>0.20:d*=0.85
            if s7>=s7_thresh and s1<s1_thresh:d*=0.70
            d_boost=d
            if d>0.32:mode='default'
        th_map={'C':0.14,'C-away':0.14,'A':0.28,'B':0.43,'default':0.32}
        v='D' if mode in th_map and d_boost>th_map[mode] else ('H' if ph>pa else 'A')
        if v==act:c+=1
        if act=='D' and v=='D':dr+=1;tp+=1
        if v=='D' and act!='D':fp+=1
        changes.append({'home':home,'away':away,'act':act,'v':v,'mode':mode if v=='D' else 'normal',
                        'd':d_boost,'s7':s7,'s1':s1,'hcp':hcp,'date':date,'score':score})
    df1=2*tp/(tp+fp)*dr/12/(tp/(tp+fp)+dr/12) if tp>0 else 0
    actual_d=sum(1 for x in changes if x['act']=='D')
    return c,dr,fp,tp,df1,actual_d,changes

print('=== S7阈值 + S1宽松 组合回测 (38场) ===')
print()
for name,s7_fn,s1_th in [
    ('v5.2       (S1=1.30, S7统一3.5)', s7_v52, 1.30),
    ('v5.2.1     (S1=1.35, S7客场3.0)', s7_v521, 1.35),
    ('v5.2.2     (S1=1.35, S7回滚3.5)', s7_v522, 1.35),
]:
    c,dr,fp,tp,df1,ad,ch=run_backtest(s7_fn,s1_th)
    print(f'{name}: Acc={c}/38  D-Recall={dr}/{ad}  D-Pred={dr+fp}(FP={fp})  D-F1={df1:.3f}')

print()
print('=== v5.2.1 vs v5.2.2 差异场次 ===')
_,_,_,_,_,_,ch1=run_backtest(s7_v521,1.35)
_,_,_,_,_,_,ch2=run_backtest(s7_v522,1.35)
vmap={'H':'主胜','D':'平局','A':'客胜'}
diff_count=0
for i,(r1,r2) in enumerate(zip(ch1,ch2)):
    if r1['v']!=r2['v']:
        diff_count+=1
        ok1='✅'if r1['v']==r1['act']else'❌'
        ok2='✅'if r2['v']==r2['act']else'❌'
        s7th1=s7_v521(r1['hcp']);s7th2=s7_v522(r2['hcp'])
        print(f'{r1["date"]} {r1["home"]}vs{r1["away"]} {r1["score"]} hcp={r1["hcp"]} S7={r1["s7"]:.1f} S1={r1["s1"]:.2f}')
        print(f'  v5.2.1(S7th={s7th1}): {vmap[r1["v"]]}(d={r1["d"]:.3f}){ok1}  v5.2.2(S7th={s7th2}): {vmap[r2["v"]]}(d={r2["d"]:.3f}){ok2}')

if diff_count==0:
    print('  无差异! v5.2.1和v5.2.2在这38场上完全一致')
