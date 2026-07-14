import sqlite3, numpy as np, pandas as pd
DB = 'data/football_data.db'
c = sqlite3.connect(DB)
df = pd.read_sql_query(
    "SELECT drift_h, drift_d, drift_a, cimp_h, cimp_d, cimp_a, outcome "
    "FROM odds_features WHERE open_h>0 AND close_h>0 AND outcome IS NOT NULL "
    "AND drift_h IS NOT NULL AND drift_d IS NOT NULL AND drift_a IS NOT NULL",
    c)
c.close()
n = len(df)
print(f"有效全量行(含drift+outcome) = {n:,}")

TH = 0.02
def sign(x):
    return 0 if abs(x) < TH else (-1 if x < 0 else 1)
PM = {(-1,1,1):'honest_defH', (1,1,-1):'honest_defA',
      (-1,-1,1):'fake_defH', (1,-1,-1):'fake_defA',
      (-1,-1,-1):'all_down', (1,1,1):'all_up'}
TARGET = {'honest_defH':'H', 'honest_defA':'A', 'fake_defH':'A', 'fake_defA':'H'}
o2i = {'H':0, 'D':1, 'A':2}
y = df['outcome'].map(o2i).values
cimp = df[['cimp_h','cimp_d','cimp_a']].values
argmax = np.argmax(cimp, axis=1)
base_acc = float((argmax == y).mean())
print(f"基线 cimp-argmax 命中率 = {base_acc:.4f}")

rows = df.to_dict('records')
hon_h = hon_a = fak_h = fak_a = 0
hon_h_ok = hon_a_ok = fak_h_ok = fak_a_ok = 0
for r in rows:
    s = (sign(r['drift_h']), sign(r['drift_d']), sign(r['drift_a']))
    pat = PM.get(s)
    if pat == 'honest_defH':
        hon_h += 1;  hon_h_ok += (r['outcome'] == 'H')
    elif pat == 'honest_defA':
        hon_a += 1;  hon_a_ok += (r['outcome'] == 'A')
    elif pat == 'fake_defH':
        fak_h += 1;  fak_h_ok += (r['outcome'] == 'A')
    elif pat == 'fake_defA':
        fak_a += 1;  fak_a_ok += (r['outcome'] == 'H')
print(f"honest_defH: n={hon_h:,}  对齐={hon_h_ok/hon_h:.4f}") if hon_h else print("honest_defH: n=0")
print(f"honest_defA: n={hon_a:,}  对齐={hon_a_ok/hon_a:.4f}") if hon_a else print("honest_defA: n=0")
print(f"fake_defH:   n={fak_h:,}  对齐(应=A)={fak_h_ok/fak_h:.4f}") if fak_h else print("fake_defH: n=0")
print(f"fake_defA:   n={fak_a:,}  对齐(应=H)={fak_a_ok/fak_a:.4f}") if fak_a else print("fake_defA: n=0")

rng = np.random.default_rng(42)
idx = np.arange(n)
hon_mask = np.array([PM.get((sign(r['drift_h']), sign(r['drift_d']), sign(r['drift_a'])))
                     in ('honest_defH', 'honest_defA') for r in rows])
hon_idx = idx[hon_mask]
hn = len(hon_idx)
if hn > 0:
    tgt = np.array([TARGET[PM[(sign(r['drift_h']), sign(r['drift_d']), sign(r['drift_a']))]]
                    for r in rows])[hon_idx]
    obs = np.array([r['outcome'] for r in rows])[hon_idx]
    aligns = (tgt == obs)
    boot = np.array([aligns[rng.integers(0, hn, hn)].mean() for _ in range(2000)])
    lo, hi = np.percentile(boot, [2.5, 97.5])
    print(f"\nhonest_def 合并: n={hn:,} 对齐={aligns.mean():.4f}  95%CI=[{lo:.4f},{hi:.4f}]")
    print(f"  vs 基线 {base_acc:.4f} -> +{(aligns.mean()-base_acc)*100:.2f}pp")
    # 设定低权重目标概率: 取条件胜率 0.559, 权重由置信度(样本量)决定
    print(f"  建议次级修正目标条件胜率=0.559, 低权重 W 取 0.20~0.25")
PY = None
