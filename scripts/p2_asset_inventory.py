"""P2-1 数据资产全量盘点与分类 — 资产目录生成器

只读(uri mode=ro)。动态枚举各主库全部表 + 真实行数 + 策划分类映射,
输出:
  reports/p2_asset_catalog.json   机器可读(逐表: 库/表/行数/分类/理由/敏感/质量标志)
  reports/p2_asset_catalog.md     人工可读(按 可售/须脱敏/不可售 分组 + 汇总)

分类口径(三态):
  SELLABLE  可售     — 公开体育数据/参考表,无 PII,可独立变现
  REDACT    须脱敏   — 含庄家赔率时间序列(再分发合规边界待确认) 或 含真实密钥/PII
  INTERNAL  不可售   — 内部模型产物/缓存/日志/归档,非数据产品

用法:
  .venv/Scripts/python.exe scripts/p2_asset_inventory.py            # 快(慢表标 LARGE)
  .venv/Scripts/python.exe scripts/p2_asset_inventory.py --deep     # 含慢表 COUNT(数分钟)
"""
import argparse, json, os, sqlite3
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "reports")
DBS = [
    ("events",         "data/events.db"),
    ("gq",             "data/GQ.db"),
    ("football_data",  "data/football_data.db"),
    ("rollball_train", "data/rollball_training.db"),
    ("hist_feature",   "data/hist_feature_matrix.db"),
    ("leisu_odds",     "data/leisu_odds.db"),
]
SLOW = {("events", "odds_snapshots"), ("events", "odds_changes")}
# 真实密钥/PII 列(排除 match_key/sport_key/league_key 这类标识符)
REAL_SECRET = ("password", "password_hash", "token", "secret", "cookie", "cuid",
               "access_key", "api_key", "auth_token")
# 赔率相关表(再分发合规边界待确认 → 须脱敏)
ODDS_TABLES = {
    "odds_snapshots", "odds_changes", "odds", "odds_history", "odds_features",
    "live_odds_raw", "ou_live_feed", "ou_validation", "ou_validation_local",
    "interwetten_odds", "leisu_odds", "leisu_odds_legacy", "william_ht",
}

# (db, table) -> (分类, 价值档, 理由)
# 价值档: HIGH / MED / LOW / NONE
C = {
 # ===== events.db =====
 ("events","matches"):("SELLABLE","HIGH","赛事主表(队名/联赛/开赛/比分),公开体育数据;⚠假0-0污染需clean_outcomes过滤"),
 ("events","match_outcomes"):("SELLABLE","HIGH","赛果+赛前盘口快照(赔率已内嵌),产品化赛事记录;⚠比分需clean_outcomes"),
 ("events","odds_snapshots"):("REDACT","HIGH","庄家赔率时间序列(快照差分),核心市场数据;再分发合规待确认"),
 ("events","odds_changes"):("REDACT","HIGH","庄家赔率变动序列,核心市场数据;再分发合规待确认"),
 ("events","odds_snapshots_bak"):("REDACT","MED","赔率快照归档(同性质,历史副本)"),
 ("events","odds_changes_bak"):("REDACT","MED","赔率变动归档(同性质,历史副本)"),
 ("events","prediction_ledger"):("INTERNAL","NONE","模型预测流水,内部产物"),
 ("events","prematch_conclusion"):("INTERNAL","NONE","赛前模型结论,内部产物"),
 ("events","prematch_candles_verdict"):("INTERNAL","NONE","K线模型判定,内部产物"),
 ("events","match_analysis_cache"):("INTERNAL","NONE","分析缓存,内部产物"),
 ("events","match_analysis_cache_bak_20260808"):("INTERNAL","NONE","分析缓存归档"),
 ("events","match_meta"):("INTERNAL","NONE","衍生元数据,内部产物"),
 ("events","cs_supplement"):("INTERNAL","NONE","波胆补充分析,内部产物"),
 ("events","cs_verification"):("INTERNAL","NONE","波胆验证,内部产物"),
 ("events","pre_match_cs"):("INTERNAL","NONE","赛前波胆,内部产物"),
 ("events","halftime_conclusion"):("INTERNAL","NONE","半场结论,内部产物"),
 ("events","analysis_snapshot"):("INTERNAL","NONE","分析快照,内部产物"),
 ("events","beat_under_log"):("INTERNAL","NONE","内部日志"),
 ("events","sixline_log"):("INTERNAL","NONE","内部日志(空)"),
 ("events","strategy_log"):("INTERNAL","NONE","策略日志,内部产物"),
 ("events","user_bets"):("INTERNAL","NONE","下注流水(空),内部"),
 ("events","h2h"):("INTERNAL","NONE","交锋历史(空),内部"),
 ("events","matches_bak_20260831_2305"):("INTERNAL","NONE","matches归档"),
 ("events","interface_doc"):("REDACT","LOW","接口文档含auth字段,须检查是否内嵌密钥再决定"),
 # ===== gq.db =====
 ("gq","matches"):("SELLABLE","MED","赛事表(乐鱼源),公开体育数据;⚠假0-0污染"),
 ("gq","match_outcomes"):("SELLABLE","MED","赛果+盘口快照(乐鱼源);⚠比分需过滤"),
 ("gq","odds_snapshots"):("REDACT","HIGH","乐鱼赔率快照(31M行),核心市场数据;合规待确认"),
 ("gq","odds_changes"):("REDACT","HIGH","乐鱼赔率变动(5.4M行),核心市场数据;合规待确认"),
 ("gq","prediction_ledger"):("INTERNAL","NONE","模型预测流水"),
 ("gq","prematch_conclusion"):("INTERNAL","NONE","赛前结论"),
 ("gq","match_analysis_cache"):("INTERNAL","NONE","分析缓存"),
 ("gq","match_analysis_cache_bak_20260808"):("INTERNAL","NONE","分析缓存归档"),
 ("gq","match_meta"):("INTERNAL","NONE","衍生元数据"),
 ("gq","cs_supplement"):("INTERNAL","NONE","波胆补充"),
 ("gq","cs_verification"):("INTERNAL","NONE","波胆验证"),
 ("gq","pre_match_cs"):("INTERNAL","NONE","赛前波胆"),
 # ===== football_data.db =====
 ("football_data","historical_matches"):("SELLABLE","HIGH","31.2万场历史赛果,核心结果资产;⚠假0-0污染(clean_outcomes)"),
 ("football_data","matches"):("SELLABLE","MED","赛事主表;⚠假0-0污染"),
 ("football_data","fd_matches"):("SELLABLE","MED"," Football-Data 赛事参考(23.8k)"),
 ("football_data","wc_all_matches"):("SELLABLE","LOW","WC2026赛事(328),公开"),
 ("football_data","wc_xlsx_matches"):("SELLABLE","LOW","WC2026 xlsx赛事(280)"),
 ("football_data","wc_lineups"):("SELLABLE","LOW","WC2026首发(公开)"),
 ("football_data","team_canonical"):("SELLABLE","MED","队名规范映射(610),参考资产"),
 ("football_data","teams"):("SELLABLE","MED","球队主表(375),参考资产"),
 ("football_data","handicap_labels"):("SELLABLE","MED","让球盘口标签(18k),参考资产"),
 ("football_data","handicap_depth_profile"):("SELLABLE","MED","同盘口统计剖面(21档),足球AI迁移独家资产"),
 ("football_data","stadiums"):("SELLABLE","LOW","球场参考(342)"),
 ("football_data","standings"):("SELLABLE","LOW","积分榜(1073),公开派生"),
 ("football_data","form_trends"):("SELLABLE","LOW","球队状态趋势(72k),公开派生"),
 ("football_data","upset_matches"):("SELLABLE","LOW","冷门场次(5497),公开派生"),
 ("football_data","fd_competitions"):("SELLABLE","LOW","赛事参考(13)"),
 ("football_data","betting_markets"):("SELLABLE","LOW","市场定义参考(564)"),
 ("football_data","betfair_market"):("SELLABLE","LOW","Betfair市场ID参考(103)"),
 ("football_data","leisu_results"):("SELLABLE","LOW","雷速赛果(10,小)"),
 ("football_data","weather_data"):("SELLABLE","LOW","天气数据(4,小)"),
 ("football_data","william_ht"):("REDACT","HIGH","William Hill半场(45.8万):含真实半场比分+庄家盘口;比分部分可分离为可售,盘口部分须脱敏"),
 ("football_data","interwetten_odds"):("REDACT","MED","Interwetten赔率(14万),合规待确认"),
 ("football_data","leisu_odds"):("REDACT","LOW","雷速赔率(290)"),
 ("football_data","leisu_odds_legacy"):("REDACT","LOW","雷速赔率遗留(405)"),
 ("football_data","live_odds_raw"):("REDACT","MED","原始滚球赔率(67k),合规待确认"),
 ("football_data","odds"):("REDACT","LOW","赔率(18k)"),
 ("football_data","odds_history"):("REDACT","LOW","赔率历史(18k)"),
 ("football_data","odds_features"):("REDACT","MED","赔率衍生特征(32.6万),含盘口"),
 ("football_data","ou_validation_local"):("REDACT","MED","OU验证(19.6万),源自定义盘口"),
 ("football_data","ou_live_feed"):("REDACT","LOW","OU实时feed(325)"),
 ("football_data","ou_validation"):("REDACT","LOW","OU验证(29)"),
 ("football_data","users"):("REDACT","NONE","含password_hash,真实密钥/安全资产,严禁外发"),
 ("football_data","indep_features"):("INTERNAL","NONE","模型特征(57k)"),
 ("football_data","match_features"):("INTERNAL","NONE","模型特征(33k)"),
 ("football_data","match_features_otsm"):("INTERNAL","NONE","模型特征(50k)"),
 ("football_data","training_extended"):("INTERNAL","NONE","模型训练数据(31万)"),
 ("football_data","model_training"):("INTERNAL","NONE","训练记录(14)"),
 ("football_data","predictions"):("INTERNAL","NONE","模型预测(141)"),
 ("football_data","predictions_old"):("INTERNAL","NONE","模型预测旧(589)"),
 ("football_data","prediction_challenges"):("INTERNAL","NONE","预测挑战(1)"),
 ("football_data","world_cup_2026_predictions"):("INTERNAL","NONE","WC预测(4)"),
 ("football_data","wc_features"):("INTERNAL","NONE","WC模型特征(328)"),
 ("football_data","bet_records"):("INTERNAL","NONE","下注记录(166),内部"),
 ("football_data","submarket_bets"):("INTERNAL","NONE","子市场下注(41)"),
 ("football_data","cross_market_consistency"):("INTERNAL","NONE","内部(空)"),
 ("football_data","fd_match_results"):("INTERNAL","NONE","内部(空)"),
 ("football_data","fd_standings"):("INTERNAL","NONE","内部(空)"),
 ("football_data","fd_upcoming_fixtures"):("INTERNAL","NONE","内部(空)"),
 ("football_data","odds_timeline"):("INTERNAL","NONE","内部(空)"),
 ("football_data","score_distribution_params"):("INTERNAL","NONE","内部(空)"),
 ("football_data","gate_params_history"):("INTERNAL","NONE","内部"),
 ("football_data","evaluation_runs"):("INTERNAL","NONE","内部(空)"),
 ("football_data","data_sync_status"):("INTERNAL","NONE","同步状态"),
 ("football_data","historical_data_imported"):("INTERNAL","NONE","导入标记"),
 ("football_data","schema_migrations"):("INTERNAL","NONE","迁移记录"),
 ("football_data","task_logs"):("INTERNAL","NONE","任务日志"),
 # ===== rollball_train.db =====
 ("rollball_train","rb_matches"):("SELLABLE","MED","31.9万场赛事结果(滚球训练源);质量待核"),
 # ===== hist_feature.db =====
 ("hist_feature","features_hist"):("INTERNAL","NONE","历史特征矩阵(31万),模型内部"),
 # ===== leisu_odds.db =====
 ("leisu_odds","live_scores"):("SELLABLE","LOW","实时比分(4197),公开"),
 ("leisu_odds","odds_snapshots"):("REDACT","LOW","雷速赔率快照(27k),合规待确认"),
 ("leisu_odds","water_signals"):("INTERNAL","NONE","水位信号(11),内部"),
}
CAT_CN = {"SELLABLE":"可售", "REDACT":"须脱敏", "INTERNAL":"不可售"}


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def sens_cols(cur, t):
    try:
        return [r[1] for r in cur.execute(f'PRAGMA table_info("{t}")').fetchall()
                if any(s in r[1].lower() for s in REAL_SECRET)]
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).astimezone()

    rows = []
    for name, rel in DBS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        try:
            c = ro(path); cur = c.cursor()
            tabs = [r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
            for t in tabs:
                if t in ("sqlite_sequence",):
                    continue
                if (name, t) in SLOW and not args.deep:
                    n = None
                else:
                    try:
                        n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    except Exception:
                        n = None
                sc = sens_cols(cur, t)
                cat, val, why = C.get((name, t), ("INTERNAL", "NONE", "未入策划映射,默认不可售(需复核)"))
                rows.append({
                    "db": name, "table": t, "rows": n,
                    "category": cat, "category_cn": CAT_CN[cat],
                    "value": val, "rationale": why,
                    "has_secret_col": bool(sc), "secret_cols": sc,
                })
            c.close()
        except Exception as e:
            print(f"[WARN] {name} 打开失败: {e}")

    # 汇总
    summ = {"SELLABLE": {"n":0,"rows":0}, "REDACT": {"n":0,"rows":0}, "INTERNAL": {"n":0,"rows":0}}
    for r in rows:
        if r["rows"]:
            summ[r["category"]]["rows"] += r["rows"]
        summ[r["category"]]["n"] += 1

    catalog = {
        "generated_at": now.isoformat(), "deep": args.deep,
        "total_tables": len(rows),
        "summary": summ,
        "assets": rows,
    }
    os.makedirs(OUT, exist_ok=True)
    jp = os.path.join(OUT, "p2_asset_catalog.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

    # MD 报告
    md = []
    md.append(f"# 哨响AI 数据资产目录（P2-1）\n")
    md.append(f"> 生成: {now.isoformat()} | 模式: {'deep' if args.deep else 'fast(慢表标LARGE)'} | 主库 6 个\n")
    md.append(f"> 分类三态: **可售**(公开体育数据/参考) / **须脱敏**(庄家赔率序列·合规待确认 或 含真实密钥) / **不可售**(内部模型产物/缓存/日志)\n")
    md.append("")
    md.append("## 汇总")
    md.append("")
    md.append("| 分类 | 表数 | 总行数 |")
    md.append("|---|---|---|")
    for k in ("SELLABLE","REDACT","INTERNAL"):
        md.append(f"| {CAT_CN[k]} | {summ[k]['n']} | {summ[k]['rows']:,} |")
    md.append(f"| **合计** | {len(rows)} | — |")
    md.append("")
    for k, cn in (("SELLABLE","可售"),("REDACT","须脱敏"),("INTERNAL","不可售")):
        md.append(f"## {cn}（{summ[k]['n']} 张表）\n")
        md.append("| 库 | 表 | 行数 | 价值 | 理由 |")
        md.append("|---|---|---|---|---|")
        for r in rows:
            if r["category"] != k:
                continue
            rn = f"{r['rows']:,}" if r["rows"] is not None else "LARGE"
            sec = " ⚠密钥" if r["has_secret_col"] else ""
            md.append(f"| {r['db']} | {r['table']}{sec} | {rn} | {r['value']} | {r['rationale']} |")
        md.append("")
    mp = os.path.join(OUT, "p2_asset_catalog.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print(f"[p2_asset_inventory] 表={len(rows)}  可售={summ['SELLABLE']['n']} 须脱敏={summ['REDACT']['n']} 不可售={summ['INTERNAL']['n']}")
    print(f"  -> {jp}")
    print(f"  -> {mp}")


if __name__ == "__main__":
    main()
