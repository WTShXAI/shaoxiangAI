"""C方案优化诊断: 在80场上扫描候选精炼规则, 选不退化的最优.
候选:
  R0 基线: 纯赔率 argmax (去水归一化)
  R1 薄边门控: group MD3 且 p_d >= max-fav EDGE -> D  (现原型)
  R2 短平赔: od <= SHORT 且 p_d 是次高 且 max<0.55 -> D
  R3 均衡局: max(p) < BAL 且 p_d >= 次高-EDGE2 -> D
  R4 组合 R1|R2|R3 (任一触发即 D, 仅当原argmax!=D)
只统计 optimized 类(非argmax)的准确率与翻转(修正+/退化-).
"""
import json
from datetime import datetime

def infer_matchday(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    if dt <= datetime(2026,6,16): return 1
    if dt <= datetime(2026,6,22): return 2
    return 3

with open("deliverables/wc2026_full_backtest.json", encoding="utf-8") as f:
    old = json.load(f)
group = old.get("details", [])
adds = [
    ("2026-06-21","西班牙","沙特",1.08,8.80,18.0,"H"),
    ("2026-06-23","葡萄牙","乌兹别克",1.22,5.90,10.00,"H"),
    ("2026-06-27","佛得角","沙特",2.47,3.35,2.62,"D"),
    ("2026-06-27","民主刚果","乌兹别克",2.27,3.25,2.97,"H"),
]
seen = {(d["home"],d["away"]) for d in group}
for a in adds:
    if (a[1],a[2]) not in seen:
        group.append({"date":a[0],"home":a[1],"away":a[2],"oh":a[3],"od":a[4],"oa":a[5],"res":a[6]})
r16 = json.load(open("data/wc2026_r16_results.json", encoding="utf-8"))["matched"]

M = []
for d in group:
    M.append((d["date"],d["home"],d["away"],d["oh"],d["od"],d["oa"],d["res"],"group",infer_matchday(d["date"])))
for d in r16:
    M.append((d["date"],d["home"],d["away"],d["oh"],d["od"],d["oa"],d["res"],"knockout",0))

def base(oh,od,oa):
    s=1/oh+1/od+1/oa
    return [1/oh/s,1/od/s,1/oa/s]
def am(oh,od,oa):
    p=base(oh,od,oa); i=p.index(max(p)); return ["H","D","A"][i]

pure=sum(1 for m in M if am(m[3],m[4],m[5])==m[6])
print(f"纯赔率 argmax: {pure}/{len(M)} ({pure/len(M)*100:.1f}%)")
print()

def verdict(m, ed, short, bal, edge2, use_r1, use_r2, use_r3):
    oh,od,oa=m[3],m[4],m[5]; res=m[6]; stage=m[7]; md=m[8]
    p=base(oh,od,oa); pv=am(oh,od,oa)
    ph,pd,pa=p
    mx=max(p); fav=max(ph,pa)
    if pv=="D": return "D"
    out=None
    if use_r1 and stage=="group" and md==3 and pd>=fav-ed:
        out="D"
    if use_r2 and od<=short and pd>=sorted(p)[1]-1e-9 and mx<0.55:
        out="D" if out is None else out
    if use_r3 and mx<bal and pd>=sorted(p)[1]-edge2:
        out="D" if out is None else out
    return out if out else pv

def eval_rule(name, **kw):
    cor=0; flip_up=0; flip_dn=0; flips=[]
    for m in M:
        v=verdict(m, **kw)
        if v==m[6]: cor+=1
        if v!=am(m[3],m[4],m[5]):
            if am(m[3],m[4],m[5])!=m[6] and v==m[6]: flip_up+=1
            elif am(m[3],m[4],m[5])==m[6] and v!=m[6]: flip_dn+=1
            flips.append((m[1],m[2],am(m[3],m[4],m[5]),v,m[6]))
    print(f"{name}: {cor}/{len(M)} ({cor/len(M)*100:.1f}%) | 修正+{flip_up} 退化-{flip_dn}")

print("=== 候选规则扫描 ===")
eval_rule("R0 纯argmax", ed=0,short=99,bal=9,edge2=9, use_r1=False,use_r2=False,use_r3=False)
eval_rule("R1 薄边EDGE=0.12", ed=0.12,short=99,bal=9,edge2=9, use_r1=True,use_r2=False,use_r3=False)
for sh in [2.8,3.0,3.2,3.5]:
    eval_rule(f"R2 短平赔od<={sh}", ed=0.12,short=sh,bal=9,edge2=9, use_r1=True,use_r2=True,use_r3=False)
for bl in [0.45,0.48,0.50]:
    eval_rule(f"R3 均衡max<{bl}", ed=0.12,short=99,bal=bl,edge2=0.05, use_r1=True,use_r2=False,use_r3=True)
# 组合: R1 + R2(short=3.0) + R3(bal=0.48)
eval_rule("R4 组合(R1+R2@3.0+R3@0.48)", ed=0.12,short=3.0,bal=0.48,edge2=0.05, use_r1=True,use_r2=True,use_r3=True)
eval_rule("R4b 组合(R1+R2@3.2)", ed=0.12,short=3.2,bal=9,edge2=9, use_r1=True,use_r2=True,use_r3=False)
