# GQ.db `odds_snapshots` 字段定义 + 上半场大小球(OU_1H) 泊松校准方法

> 数据来源:`D:\Architecture\data\GQ.db`(14.4 GB, `odds_snapshots` 约 **3115 万行** / **3137 个** distinct `match_key`)
> 配套文件:`GQ_OU1H_feature_extract.sql`(可直接跑的特征抽取 SQL)
> 实测日期:2026-08-27

---

## 0. 直接回答:可以透露,这是你自己库的表结构

`odds_snapshots` 是乐鱼(GQ)赔率长表,一行 = 某时刻某盘口的某选项赔率。真实字段如下:

| 字段 | 类型 | 约束 | 含义 |
|------|------|------|------|
| `id` | INTEGER | PK | 自增主键 |
| `match_key` | TEXT | NOT NULL | 场次键,中文 `主队 vs 客队`,如 `'加的斯B队 vs 狮城水手'` |
| `captured_at` | REAL | NOT NULL | 抓取时间戳(Unix,高精度;同批次快照同秒) |
| `market` | TEXT | NOT NULL | 盘口类别,如 `OU_1H_1.50` / `OU_2H_1.25` / `1X2` / `1X2_1H` / `AH_1H_-0.25` / `CS` |
| `selection` | TEXT | NOT NULL | `over`/`under`(OU); `home`/`draw`/`away`(1X2); `1:0`...`其他`(CS); `yes`/`no`/`odd`/`even` 等 |
| `odds` | REAL | NOT NULL | 赔率 |
| `line` | REAL | 可空 | 盘口线。`OU_1H_1.50` -> `1.5`。**OU 类 line 永不为 NULL 且与 market 后缀严格一致**(已核验) |
| `score_at` | TEXT | 默认 `''` | 抓取时比分,如 `'0-0'`/`'1-0'`;开盘期多为空 |
| `minute_at` | INTEGER | 默认 `0` | 抓取时分钟;开盘期=0,滚球期>0 |

**关键确认(已用 SQL 实测):**
- `market` 直接编码了线:`OU_1H_0.50` ~ `OU_1H_8.50`,含半线(.5)与 Quart 线(.25/.75)。`line` 列与后缀一致,可直接用,不必解析字符串。
- OU_1H 行中 `line IS NULL` = 0 条 → 用 `line` 列安全。
- 开盘期 `score_at=''` / `minute_at=0`;约 **20%** 的 OU_1H 行带实时比分(`score_at` 非空、`minute_at>0`)→ 可做滚球漂移分析。
- 关联表:`matches.match_key` 有 `ht_score_home/away`(半场比分真值);`match_outcomes` 有 `mid`+`ht_score_home/away`(经 `mid` 关联)。

---

## 1. 上半场大小球泊松校准方法(设计)

### 1.1 建模假设
半场总进球 `G ~ Poisson(λ_1H)`。对每个盘口线 `L`,大球胜出概率(含 Quart 线半赢半输)为:

| 线型 | `P(大球胜)` 模型式 |
|------|------|
| 半线 `L = k.5`(如 1.5) | `1 - CDF(k) = P(G ≥ k+1)` |
| 全线 `L = k.0`(如 2.0) | `1 - CDF(k) = P(G ≥ k+1)`(走盘退本金,胜率同上半线) |
| Quart `L = k.25`(如 1.25) | `(1-CDF(k)) + 0.5·PMF(k) = P(G≥k+1) + 0.5·P(G=k)` |
| Quart `L = k.75`(如 1.75) | `(1-CDF(k+1)) + 0.5·PMF(k+1) = P(G≥k+2) + 0.5·P(G=k+1)` |

其中 `CDF`/`PMF` 为泊松累积/质量函数。

### 1.2 从赔率到隐含概率(去水)
对每条线 `(L)` 取同刻 over/under 赔率,比例法去水:
```
p_over_raw = 1/over_o ; p_under_raw = 1/under_o
p_over = p_over_raw / (p_over_raw + p_under_raw)   # 隐式 P(G > L)
```

### 1.3 λ_1H 拟合:多线联合最小二乘(推荐)
同一时刻一场比赛会挂出多条 OU_1H 线(1.0/1.25/1.5/...)。**用全部线联合拟合 λ**,比单线反解更稳:
```
min_λ  Σ_L  [ P_over_model(λ, L) - p_over_implied(L) ]²
```
λ 在 `[0.1, 4.0]` 网格(步长 0.01)扫描取 SSE 最小。纯 SQL 无 Poisson CDF,此步用 Python(见 §3 参考实现)。

> 单线法(备选):只取主流动性线(如 `OU_1H_1.50`),去水后对 `P(G≥2)=p_over` 反解 λ —— 信息少、抖动大,不推荐作主方案。

### 1.4 预测输出
拿到 λ_1H 后即可对任意线 `L` 给大/小概率、`E[G]=λ_1H`,以及比分分布 `P(G=g)=PMF(g;λ)`。

---

## 2. ⚠️ 实测结论(诚实边界,务必先看)

我在真实数据上跑了上述方法并和 `matches.ht_score` 校验,**发现一个系统性重大问题**:

| 指标 | 数值 |
|------|------|
| 拟合场次数 | 2019(开盘含 ≥2 条 OU_1H 线的场次) |
| 开盘隐含 `λ_1H`(均值) | **1.448** |
| 收盘前隐含 `λ_1H`(均值) | **1.448**(开盘≈收盘,几乎零漂移) |
| 真实半场进球均值(同批场次) | **2.927** |
| 拟合组内 SSE(均值) | 0.0256(多线结构内部高度自洽) |
| 模型隐含 `P(HT>1.5)` | 0.421 |
| 真实 `P(HT>1.5)` | **0.746** |

**解读:**
1. **拟合本身正确**(SSE≈0.026,多线隐含概率彼此自洽于单一 λ),所以不是代码 bug。
2. **但开盘/收盘 OU_1H 隐含的 λ≈1.45,只有真实半场进球(~2.9)的一半**。市场把"半场大球"定价得异常保守 —— 真实有 75% 的比赛半场 ≥2 球,市场只给了 42% 的概率。
3. 开盘与收盘 λ 完全一致,说明这个偏差**不是临场调整能解释的**,而是数据源(GQ)对这批(多为 obscure/U20/女足/地区联赛)比赛的半场线**整体定低**。

### 这意味着
- **不能直接把 OU_1H 开盘隐含概率当预测用** —— 会系统性漏掉大球。这是铁律级注意事项(呼应 IR-30 诚实边界)。
- 必须先做**经验校准**(empirical calibration),把 λ 往真实分布抬升,或用**分层 EB 收缩**(参照项目现有 `inplay_calibration.py` 的 `w = per_n/(per_n+K), K=200` 思路)按联赛/层级分别收缩。
- **数据质量待核验**:`matches.ht_score` 是否确为半场比分、该数据集是否混入非常规赛制,需要你确认。全量 `matches` 半场均值 2.29(17.7% 半场 0-0)已偏高,拟合子集 2.93 更高(高流动性场次选择偏差)。无论哪种,都**显著高于市场定价**。

---

## 3. Python 参考实现(已在本机跑通)

```python
import sqlite3, math, statistics
from collections import defaultdict

def pmf(l,k):
    if l<=0: return 1.0 if k==0 else 0.0
    return math.exp(k*math.log(l)-l-math.lgamma(k+1))
def cdf(l,k):
    return 0.0 if k<0 else sum(pmf(l,j) for j in range(0,k+1))
def P_over(l,L):
    k=math.floor(L); f=L-k
    if f in (0.0,0.5): return 1-cdf(l,k)
    if f==0.25: return (1-cdf(l,k))+0.5*pmf(l,k)
    if f==0.75: return (1-cdf(l,k+1))+0.5*pmf(l,k+1)
    return 1-cdf(l,math.floor(L))
def devig(o,u):
    if not o or not u or o<=0 or u<=0: return None,None
    po,pu=1/o,1/u; s=po+pu
    return (None,None) if s<=0 else (po/s,pu/s)

def fit_lambda(obs):  # obs=[(L, p_over), ...]
    best,bl=None,None
    for i in range(5,401):
        lam=i*0.01
        sse=sum((P_over(lam,L)-p)**2 for L,p in obs)
        if best is None or sse<best: best=sse; bl=lam
    return bl
# 取某场开盘批次(最早 captured_at ±5s)的全部 OU_1H 线 → obs → λ
```

---

## 4. 落地建议(给涛哥的决策项)

1. **先核验 `matches.ht_score` 语义**(半场 vs 全场的真实性),再决定是否信任"真实 2.9"。
2. 若确认数据无误 → 这是一个**真实的大球定价偏差信号**,可做"反向跟随大球"的策略回测(市场低估大球 = 潜在 value)。
3. 校准方案二选一或叠加:
   - **经验抬升**:按联赛分层,用历史 `λ_market`→`λ_actual` 的回归/isotonic 映射;
   - **EB 收缩**:`λ_cal = (n·λ_market + K·λ_prior)/(n+K)`,`K=200`,`λ_prior` 取该联赛真实均值。
4. 特征抽取 SQL 已就绪(配套 `.sql`),可直接喂入上述 Python 拟合管线。

---
*注:本分析仅做结构与方法验证,未对任何信号下注;"分析非预测"(IR-20)。*
