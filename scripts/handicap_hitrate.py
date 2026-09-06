# -*- coding: utf-8 -*-
"""
让球方向 vs 1X2 方向 命中率验证
链接 handicap_db.json (71场截图转录) 与 wc_all_matches 真实赛果。
"""
import json, sqlite3

DB_PATH = "data/football_data.db"
HC_PATH = "odds_db/handicap_db.json"

# 截图队名 -> DB队名 规范化
NORM = {
    "乌兹别克斯坦": "乌兹别克",
    "佛得角共和国": "佛得角",
    "沙特阿拉伯": "沙特",
}

def norm(t):
    return NORM.get(t, t)

# ---- 载入真实赛果 ----
con = sqlite3.connect(DB_PATH)
cur = con.cursor()
cur.execute("SELECT home, away, hg, ag, final_result, oh, od, oa FROM wc_all_matches")
rows = cur.fetchall()
con.close()

# 建索引: (norm_home, norm_away) -> (hg, ag, final_result)
by_pair = {}
for h, a, hg, ag, fr, oh, od, oa in rows:
    by_pair[(norm(h), norm(a))] = (hg, ag, fr)
    by_pair[(norm(a), norm(h))] = (hg, ag, fr)  # 备用反向

# ---- 载入截图转录 ----
db = json.load(open(HC_PATH, encoding="utf-8"))
recs = db["records"]

matched, unmatched = [], []
for r in recs:
    nh, na = norm(r["home"]), norm(r["away"])
    key = (nh, na)
    res = by_pair.get(key)
    if res is None:
        # 尝试反向
        res = by_pair.get((na, nh))
        oriented = (na, nh) if res else None
    else:
        oriented = key
    rec = dict(r)
    if res:
        hg, ag, fr = res
        rec["hg"], rec["ag"] = hg, ag
        rec["actual"] = "H" if hg > ag else ("D" if hg == ag else "A")
        rec["matched"] = True
        matched.append(rec)
    else:
        rec["matched"] = False
        unmatched.append(rec)
    rec["_oriented"] = oriented

print(f"总记录={len(recs)}  链接成功={len(matched)}  未链接={len(unmatched)}")
if unmatched:
    print("未链接:", [(u['home'], u['away']) for u in unmatched])

# ---- 评估函数 ----
X12MAP = {"home": "H", "draw": "D", "away": "A"}

def hcp_to_wdl(rec):
    """把让球方向映射为胜平负预测: home->H, away->A (让球本身不预测平)"""
    d = rec.get("hcp_dir")
    if d == "home": return "H"
    if d == "away": return "A"
    return None

def hcp_cover_hit(rec):
    """亚洲让球'方向方'是否击穿盘口 (AH-native 指标)"""
    L = rec.get("hcp_line")
    if L is None or rec.get("hcp_dir") not in ("home", "away"):
        return None
    hg, ag = rec["hg"], rec["ag"]
    # 主队调整后 = hg + L ; 主队cover若 hg+L > ag
    if rec["hcp_dir"] == "home":
        return (hg + L) > ag
    else:
        return (ag + L) > hg  # 客队cover: ag+L > hg

# ---- 统计 ----
def tally(subset, label):
    n = len(subset)
    if n == 0:
        return {"n": 0}
    x12_hit = sum(1 for r in subset if X12MAP.get(r["x12_dir"]) == r["actual"])
    hcp_wdl = [r for r in subset if hcp_to_wdl(r)]
    hcp_hit = sum(1 for r in hcp_wdl if r["actual"] == hcp_to_wdl(r))
    # 让球cover命中
    cov = [r for r in subset if hcp_cover_hit(r) is not None]
    cov_hit = sum(1 for r in cov if hcp_cover_hit(r))
    return {
        "n": n,
        "x12_acc": x12_hit / n,
        "hcp_wdl_acc": hcp_hit / len(hcp_wdl) if hcp_wdl else None,
        "hcp_cover_acc": cov_hit / len(cov) if cov else None,
    }

print("\n=== 总体命中率 ===")
overall = tally(matched, "overall")
for k, v in overall.items():
    print(f"  {k}: {v if not isinstance(v, float) else round(v*100,1)}")

# 分歧 vs 一致
agree = [r for r in matched if r["hcp_dir"] == r["x12_dir"] or (r["x12_dir"]=="draw" and r["hcp_dir"] in ("home","away"))]
# 注意: 一致=让球方向与1X2方向同为home/away; 若x12=draw则视为与任何hcp都'分歧'
disagree = [r for r in matched if not (r["hcp_dir"] == r["x12_dir"] and r["x12_dir"] in ("home","away"))]
# 更严谨的分歧定义: hcp_dir(home/away) 与 x12_dir(home/away) 不同, 或 x12=draw
disagree = [r for r in matched if not (r["hcp_dir"]==r["x12_dir"] and r["x12_dir"] in ("home","away"))]

print(f"\n=== 一致 vs 分歧 (共识子集) ===")
print(f"一致(同指home/away): {len(agree)}  分歧: {len(disagree)}")
ag_t = tally(agree, "agree")
dg_t = tally(disagree, "disagree")
print("  一致子集:", {k:(round(v*100,1) if isinstance(v,float) else v) for k,v in ag_t.items()})
print("  分歧子集:", {k:(round(v*100,1) if isinstance(v,float) else v) for k,v in dg_t.items()})

# 分歧局: 谁更准?
print("\n=== 分歧局: 让球方向 vs 1X2方向 谁命中真实结果 ===")
both = same = x_only = h_only = neither = 0
x_wins = h_wins = 0
for r in disagree:
    x = X12MAP.get(r["x12_dir"]) == r["actual"]
    hdir = hcp_to_wdl(r)
    h = (hdir is not None) and (r["actual"] == hdir)
    if x and h: both += 1
    elif x and not h: x_only += 1
    elif h and not x: h_only += 1
    else: neither += 1
print(f"  都中={both}  仅1X2中={x_only}  仅让球中={h_only}  都不中={neither}")
print(f"  1X2在分歧局命中数={x_only}  让球在分歧局命中数={h_only}")

# 按深度分层
print("\n=== 按让球深度分层 (x12_acc / hcp_wdl_acc / hcp_cover_acc) ===")
for depth in ["level","shallow","medium","deep"]:
    sub = [r for r in matched if r.get("hcp_depth")==depth]
    t = tally(sub, depth)
    if t["n"]==0: continue
    print(f"  {depth:8s} n={t['n']:3d}  x12={round(t['x12_acc']*100,1)}%  "
          f"hcp_wdl={('-' if t['hcp_wdl_acc'] is None else round(t['hcp_wdl_acc']*100,1))}%  "
          f"hcp_cover={('-' if t['hcp_cover_acc'] is None else round(t['hcp_cover_acc']*100,1))}%")

# 额外: 真实平局频率
draws = sum(1 for r in matched if r["actual"]=="D")
print(f"\n真实平局频率: {draws}/{len(matched)} = {round(draws/len(matched)*100,1)}%")

# 分歧局子类型拆解
print("\n=== 分歧局子类型 (x12预案 vs 让球方向) ===")
sub_draw = [r for r in disagree if r["x12_dir"]=="draw"]      # 1X2说平, 让球说某队
sub_flip = [r for r in disagree if r["x12_dir"] in ("home","away")]  # 真方向相反
print(f"  类型A (1X2=平 / 让球=某队): {len(sub_draw)} 场")
if sub_draw:
    d_draws = sum(1 for r in sub_draw if r["actual"]=="D")
    print(f"    其中真实平局={d_draws} ({round(d_draws/len(sub_draw)*100,1)}%)  -> 1X2'平'命中")
print(f"  类型B (1X2主客 / 让球反向, 纯方向对决): {len(sub_flip)} 场")
b_x = sum(1 for r in sub_flip if X12MAP[r['x12_dir']]==r['actual'])
b_h = sum(1 for r in sub_flip if hcp_to_wdl(r)==r['actual'])
print(f"    1X2命中={b_x}  让球命中={b_h}")

# 1X2'平'召唤准确率 (全样本)
draw_calls = [r for r in matched if r["x12_dir"]=="draw"]
if draw_calls:
    dc_hit = sum(1 for r in draw_calls if r["actual"]=="D")
    print(f"\n1X2召唤'平局'共 {len(draw_calls)} 场, 真实平局 {dc_hit} 场 "
          f"({round(dc_hit/len(draw_calls)*100,1)}% 准确率)")

# 一致局中'主客一致'是否高命中
ag_homeaway = [r for r in agree if r["x12_dir"] in ("home","away")]
if ag_homeaway:
    ag_hit = sum(1 for r in ag_homeaway if X12MAP[r['x12_dir']]==r['actual'])
    print(f"一致局(主/客同向) {len(ag_homeaway)} 场, 该队赢 {ag_hit} 场 ({round(ag_hit/len(ag_homeaway)*100,1)}%)")

# ---- 写回带赛果的JSON ----
out = {"records": matched, "unmatched": [{"home":u["home"],"away":u["away"]} for u in unmatched]}
json.dump(out, open("odds_db/handicap_db_matched.json","w",encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("\n已写出 odds_db/handicap_db_matched.json")
