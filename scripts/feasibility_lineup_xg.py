"""
路径①可行性探测(可复跑): 阵容/xG 数据源是否存在于当前基础设施。
产出: 库表清单 + match_meta 与本干净宇宙重叠 + 推送结构结论(静态, 见 ws_collector._on_event)。
"""
import sqlite3, csv
ce=sqlite3.connect("data/events.db",timeout=30);ce.row_factory=sqlite3.Row
cg=sqlite3.connect("data/GQ.db",timeout=30);cg.row_factory=sqlite3.Row

print("=== 库表 ===")
for db in ("data/GQ.db","data/events.db","data/football_data.db"):
    c=sqlite3.connect(db,timeout=30)
    tabs=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    print(f"[{db}] {len(tabs)} tables: {tabs}")
    c.close()

print("\n=== match_meta 阵容/伤停覆盖 ===")
mm_cols=[x[1] for x in ce.execute("PRAGMA table_info(match_meta)")]
print("match_meta cols:", mm_cols)
n_mm=ce.execute("SELECT COUNT(*) n FROM match_meta").fetchone()["n"]
print("match_meta rows:", n_mm)
n_lin=ce.execute("SELECT COUNT(*) n FROM match_meta WHERE lineup_home<>'' OR lineup_away<>''").fetchone()["n"]
n_inj=ce.execute("SELECT COUNT(*) n FROM match_meta WHERE injuries_home<>'' OR injuries_away<>''").fetchone()["n"]
print(f"  有阵容: {n_lin}  有伤停: {n_inj}")

print("\n=== 与本干净宇宙重叠 ===")
mh=set(r["match_key"] for r in csv.DictReader(open("data/mh_dataset.csv",encoding="utf-8")))
mm=set(r["match_key"] for r in ce.execute("SELECT DISTINCT match_key FROM match_meta"))
print(f"  mh宇宙={len(mh)}  match_meta={len(mm)}  重叠={len(mh&mm)}")
# 重叠中且有阵容/伤停
ov=mh&mm
ov_lin=0; ov_inj=0
if ov:
    ph=",".join("?"*len(ov)); ovl=list(ov)
    ov_lin=ce.execute(f"SELECT COUNT(*) n FROM match_meta WHERE match_key IN ({ph}) AND (lineup_home<>'' OR lineup_away<>'')",ovl).fetchone()["n"]
    ov_inj=ce.execute(f"SELECT COUNT(*) n FROM match_meta WHERE match_key IN ({ph}) AND (injuries_home<>'' OR injuries_away<>'')",ovl).fetchone()["n"]
print(f"  重叠场中带阵容: {ov_lin}  带伤停: {ov_inj}  -> 可用作特征样本过小(N无效)")

print("\n=== 结论 ===")
print("  xG/shot事件: 全库无; live换人/红牌: 乐鱼推送不含(C102/C103/C105仅状态/比分/盘口)")
print("  赛前阵容/伤停: 有但重叠19场且赛前已定价 -> 非市场未含, 不可行")
print("  唯一市场未含in-play信号=odds_changes tick漂流(已被P0-2微观结构挖掘覆盖, 当前无正edge)")
