# -*- coding: utf-8 -*-
"""
P1a · WC 数据 walk-forward 验证 + 弱点迁移检验
============================================
对照 P0b (obscure 联赛+友谊赛 150 场) 暴露的 4 个通用弱点, 用真实 WC 数据 (wc_all_matches, 328 场)
验证是否迁移, 并做按 edition 的 walk-forward 稳定性检验.

阶段:
  PHASE 0  确保 match_outcomes 含 source 列 (gq/db.py init_db 已加)
  PHASE 1  沙箱: copy events.db -> 副本, 导入 3 场, 校验列映射正确, 删副本
  PHASE 2  全量: 真实 events.db 导入 328 场 (source='wc'), 回填 _live_predict + correct_analysis
  PHASE 3  指标: verdict_hit / 平局漏判 / 比分偏差 / 强主爆冷 / stake_pnl(单庄全PASS? 合成双庄价差?)
  PHASE 4  walk-forward: 各届 verdict_hit + 滚动窗口稳定性
  PHASE 5  落盘 metrics JSON (供报告使用)
"""
import os, sys, time, json, shutil, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
GQ_DB = os.path.join(HERE, "data", "events.db")
SRC_DB = os.path.join(HERE, "data", "football_data.db")
OUT_JSON = os.path.join(HERE, "wc_replay_metrics.json")

from gq.db import classify_odds_type, save_analysis, correct_analysis, init_db

RESULT_MAP = {"H": "home", "D": "draw", "A": "away"}
DIR2RES = {"主胜": "home", "平局": "draw", "客胜": "away"}
BETDIR2RES = {"H": "home", "D": "draw", "A": "away"}

# ──────────────────────────────────────────────────────────────────────────
def ensure_source_col(db_path):
    c = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(match_outcomes)").fetchall()]
        if "source" not in cols:
            c.execute("ALTER TABLE match_outcomes ADD COLUMN source TEXT DEFAULT 'gq'")
    except Exception as e:
        print("[ensure_source_col] warn:", e)
    c.commit(); c.close()


def _edition_of(mid):
    # mid = wc_{edition}_{id}
    try:
        return str(mid).split("_")[1]
    except Exception:
        return "?"


# ──────────────────────────────────────────────────────────────────────────
def import_wc(target_db, src_db, limit=None, only_ids=None, verbose=True):
    """将 wc_all_matches 导入 target_db.match_outcomes (source='wc'). 幂等(按 mid UNIQUE)."""
    ensure_source_col(target_db)
    src = sqlite3.connect(src_db); src.row_factory = sqlite3.Row
    tgt = sqlite3.connect(target_db)
    rows = src.execute("SELECT * FROM wc_all_matches ORDER BY id").fetchall()
    if only_ids is not None:
        rows = [r for r in rows if r["id"] in only_ids]
    elif limit is not None:
        rows = rows[:limit]

    ins = skip = 0
    cur = tgt.cursor()
    for r in rows:
        ed = str(r["edition"])
        mid = f"wc_{ed}_{r['id']}"
        res = RESULT_MAP.get(r["final_result"])
        league = f"FIFA World Cup {ed}"
        oh, od, oa = r["oh"], r["od"], r["oa"]
        odds_type = classify_odds_type(oh, od, oa)
        now = time.time()
        cur.execute("""INSERT OR IGNORE INTO match_outcomes
            (mid, home, away, league, kickoff, score_home, score_away, result,
             op_1x2_h, op_1x2_d, op_1x2_a,
             op_ah_line, op_ah_home, op_ah_away,
             op_ou_line, op_ou_over, op_ou_under,
             op_cs, odds_type, is_valid, source, captured_at, archived_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, r["home"], r["away"], league, None,
             r["hg"], r["ag"], res,
             oh, od, oa,
             None, None, None, None, None, None,
             "[]", odds_type, 1, "wc", now, now))
        if cur.rowcount == 0:
            skip += 1
        else:
            ins += 1
    tgt.commit(); tgt.close(); src.close()
    if verbose:
        print(f"[import_wc] target={os.path.basename(target_db)} inserted={ins} skipped(已存在)={skip}")
    return ins, skip


# ──────────────────────────────────────────────────────────────────────────
def backfill(target_db, limit=None, progress=True):
    """对 source='wc' 且有开盘赔率的行, 调 _live_predict -> save_analysis -> correct_analysis."""
    from bridge_service import _live_predict
    tgt = sqlite3.connect(target_db); tgt.row_factory = sqlite3.Row
    rows = tgt.execute("SELECT * FROM match_outcomes WHERE source='wc' ORDER BY mid").fetchall()
    if limit:
        rows = rows[:limit]
    done = skipped = 0
    for r in rows:
        mid = r["mid"]
        oh, od, oa = r["op_1x2_h"], r["op_1x2_d"], r["op_1x2_a"]
        if oh is None or od is None or oa is None:
            skipped += 1
            continue
        try:
            pred = _live_predict(r["home"], r["away"], oh, od, oa,
                                 sport_key="soccer_fifa_world_cup", mid=mid)
            save_analysis(mid, pred)      # 幂等: _live_predict 内已写过则为 no-op
            correct_analysis(mid)
            done += 1
        except Exception as e:
            print(f"[backfill] ERR {mid}: {e}")
        if progress and done % 40 == 0:
            print(f"  backfill {done}... (skip_null_odds={skipped})")
    tgt.close()
    print(f"[backfill] done={done} skipped_null_odds={skipped}")
    return done, skipped


# ──────────────────────────────────────────────────────────────────────────
def gather_rows(target_db):
    """取已修正的 wc 分析行 (join match_outcomes). 返回 list[dict]."""
    tgt = sqlite3.connect(target_db); tgt.row_factory = sqlite3.Row
    sql = """
        SELECT c.mid, c.verdict, c.verdict_hit, c.score_err, c.stake_pnl,
               c.stake_suggestion, c.pred_score_home, c.pred_score_away,
               o.home, o.away, o.score_home, o.score_away, o.result,
               o.op_1x2_h, o.op_1x2_d, o.op_1x2_a, o.odds_type
        FROM match_analysis_cache c
        JOIN match_outcomes o ON o.mid = c.mid
        WHERE o.source='wc' AND c.corrected_at IS NOT NULL
    """
    rows = [dict(r) for r in tgt.execute(sql).fetchall()]
    for r in rows:
        r["edition"] = _edition_of(r["mid"])
    tgt.close()
    return rows


def _rate(rows, pred_key, actual_key, hit_cond):
    n = len(rows)
    if n == 0:
        return {"n": 0, "hit": 0, "rate": None}
    h = sum(1 for r in rows if hit_cond(r))
    return {"n": n, "hit": h, "rate": round(100.0 * h / n, 1)}


def compute_metrics(rows):
    total = len(rows)
    # verdict_hit 分布
    vh = {"hit": 0, "miss": 0, "miss_draw": 0}
    for r in rows:
        vh[r["verdict_hit"]] = vh.get(r["verdict_hit"], 0) + 1
    verdict_hit_rate = round(100.0 * vh["hit"] / total, 1) if total else None

    # 平局漏判
    actual_draws = [r for r in rows if r["result"] == "draw"]
    nd = len(actual_draws)
    drawn_called = sum(1 for r in actual_draws if r["verdict"] == "平局")
    drawn_hit = sum(1 for r in actual_draws if r["verdict_hit"] == "hit")
    drawn_miss = nd - drawn_hit
    draw_miss_rate = round(100.0 * drawn_miss / nd, 1) if nd else None

    # 比分偏差
    def avg(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None
    pred_h = avg("pred_score_home"); pred_a = avg("pred_score_away")
    act_h = avg("score_home"); act_a = avg("score_away")
    # score_err 均值
    se = [r["score_err"] for r in rows if r["score_err"] is not None]
    avg_score_err = round(sum(se) / len(se), 3) if se else None

    # 强主爆冷 (深盘 h<1.8)
    strong = [r for r in rows if r["op_1x2_h"] is not None and r["op_1x2_h"] < 1.8]
    sh_rate = round(100.0 * sum(1 for r in strong if r["verdict_hit"] == "hit") / len(strong), 1) if strong else None

    # 单庄全 PASS 检验
    pass_all = all(r["stake_suggestion"] == "PASS" for r in rows) if rows else None
    pnl = [r["stake_pnl"] for r in rows if r["stake_pnl"] is not None]
    sum_pnl = round(sum(pnl), 3) if pnl else 0.0

    # verdict 方向分布 (模型选择)
    dir_dist = {}
    for r in rows:
        dir_dist[r["verdict"]] = dir_dist.get(r["verdict"], 0) + 1

    # 实际赛果分布
    res_dist = {}
    for r in rows:
        res_dist[r["result"]] = res_dist.get(r["result"], 0) + 1

    # odds_type 分布
    ot_dist = {}
    for r in rows:
        ot_dist[r["odds_type"]] = ot_dist.get(r["odds_type"], 0) + 1

    return {
        "total": total,
        "verdict_hit": verdict_hit_rate,
        "verdict_hit_dist": vh,
        "draw": {
            "actual_draws": nd,
            "model_called_draw": drawn_called,
            "drawn_hit": drawn_hit,
            "drawn_miss": drawn_miss,
            "draw_miss_rate": draw_miss_rate,
        },
        "score_bias": {
            "pred_home": pred_h, "actual_home": act_h, "home_bias": (round(pred_h-act_h,3) if pred_h is not None and act_h is not None else None),
            "pred_away": pred_a, "actual_away": act_a, "away_bias": (round(pred_a-act_a,3) if pred_a is not None and act_a is not None else None),
            "avg_score_err": avg_score_err,
        },
        "strong_home": {
            "n": len(strong), "hit_rate": sh_rate,
            "threshold": "op_1x2_h < 1.8",
        },
        "stake": {
            "all_pass": pass_all,
            "sum_pnl": sum_pnl,
            "single_book_note": "全部行仅单庄开盘 -> value_layer 强制 PASS",
        },
        "verdict_dir_dist": dir_dist,
        "result_dist": res_dist,
        "odds_type_dist": ot_dist,
    }


def per_edition(rows):
    eds = sorted({r["edition"] for r in rows})
    out = {}
    for ed in eds:
        sub = [r for r in rows if r["edition"] == ed]
        m = compute_metrics(sub)
        out[ed] = {k: m[k] for k in ("total", "verdict_hit", "verdict_hit_dist", "draw", "score_bias", "strong_home")}
    return out


def rolling_windows(rows):
    """walk-forward: 各届作为验证集, 之前届为训练(观察)集; 模型固定 v7.4 不重训, 检验 OOS 稳定性."""
    eds = sorted({r["edition"] for r in rows})
    wins = []
    # 累计式: 截至届 t 的验证
    cum = []
    for i, ed in enumerate(eds):
        val = [r for r in rows if r["edition"] == ed]
        train = [r for r in rows if r["edition"] in eds[:i]]   # 之前届
        val_m = compute_metrics(val)
        wins.append({
            "validate": ed,
            "train": eds[:i],
            "n_val": len(val),
            "verdict_hit": val_m["verdict_hit"],
            "draw_miss_rate": val_m["draw"]["draw_miss_rate"],
            "strong_home_rate": val_m["strong_home"]["hit_rate"],
            "away_bias": val_m["score_bias"]["away_bias"],
        })
        # 累计验证: 截至 ed (包含 ed 之前的全部)
        if i == 0:
            cum_m = compute_metrics(val)
            cum_label = ed
        else:
            cum_all = [r for r in rows if r["edition"] in eds[:i+1]]
            cum_m = compute_metrics(cum_all)
            cum_label = f"{eds[0]}-{ed}"
        cum.append({"window": cum_label, "n": cum_m["total"], "verdict_hit": cum_m["verdict_hit"]})
    return {"holdout_2026_vs_hist": wins, "cumulative": cum}


# ──────────────────────────────────────────────────────────────────────────
def synthetic_dual_book_test(rows, sample=None):
    """合成双庄价差敏感性测试 (清晰标注: 非真实盘口).
    对单庄行合成一个"更便宜的第二庄" (全体赔率 -3%), 看 value_layer 是否脱离 PASS,
    并核算理论 PnL(非真实收益). 直接返回 r (不写库, mid=None)."""
    from bridge_service import _live_predict
    sub = rows if sample is None else rows[:sample]
    bet = 0; total = 0; pnl = 0.0
    for r in sub:
        oh, od, oa = r["op_1x2_h"], r["op_1x2_d"], r["op_1x2_a"]
        if oh is None or od is None or oa is None:
            continue
        total += 1
        # 合成第二庄: 赔率 -3% (更便宜/更sharp), 制造跨庄价差
        oh2, od2, oa2 = round(oh*0.97, 3), round(od*0.97, 3), round(oa*0.97, 3)
        try:
            rr = _live_predict(r["home"], r["away"], oh, od, oa,
                              sport_key="soccer_fifa_world_cup",
                              extra_bookmakers=[["syn_sharp", oh2, od2, oa2]])
        except Exception as e:
            print(f"[dual_book] ERR {r['mid']}: {e}")
            continue
        bd = rr["value_layer"].get("best_direction")
        if bd in ("H", "D", "A"):
            bet += 1
            res = r["result"]
            # 理论硬结算 PnL (用 best_odds 中该方向赔率)
            best_odds = rr["value_layer"].get("best_odds", [oh, od, oa])
            _om = {"H": best_odds[0], "D": best_odds[1], "A": best_odds[2]}
            used = _om.get(bd)
            hit = (BETDIR2RES[bd] == res)
            pnl += (float(used)-1.0) if hit else -1.0
    return {
        "synthetic": True,
        "note": "合成第二庄 = 单庄赔率全体 -3% 模拟跨庄价差; 理论 PnL 非真实收益",
        "n": total,
        "bet": bet,
        "bet_rate": round(100.0*bet/total, 1) if total else None,
        "theo_pnl": round(pnl, 2),
    }


# ──────────────────────────────────────────────────────────────────────────
def main():
    t0 = time.time()
    init_db()  # 确保 source 列 / 表存在

    # PHASE 1 沙箱
    print("=== PHASE 1: 沙箱导入 3 场 (copy events.db) ===")
    sandbox = os.path.join(HERE, "data", "_sandbox_wc_gq.db")
    if os.path.exists(sandbox):
        os.remove(sandbox)
    shutil.copy(GQ_DB, sandbox)
    import_wc(sandbox, SRC_DB, limit=3, verbose=True)
    sc = sqlite3.connect(sandbox); sc.row_factory = sqlite3.Row
    for r in sc.execute("SELECT mid,home,away,league,score_home,score_away,result,op_1x2_h,op_1x2_d,op_1x2_a,odds_type,is_valid,source FROM match_outcomes WHERE source='wc' ORDER BY mid").fetchall():
        print("  SANDBOX ROW:", dict(r))
    sc.close()
    os.remove(sandbox)
    print("  SANDBOX 校验完成, 副本已删除\n")

    # PHASE 2 全量导入 + 回填
    print("=== PHASE 2: 全量导入 328 场 (真实 events.db) ===")
    ins, skip = import_wc(GQ_DB, SRC_DB, limit=None, verbose=True)
    print("=== PHASE 2b: 回填 _live_predict + correct_analysis ===")
    done, skipped = backfill(GQ_DB, limit=None, progress=True)

    # PHASE 3/4 指标
    print("=== PHASE 3/4: 指标 + walk-forward ===")
    rows = gather_rows(GQ_DB)
    print(f"  已修正分析行数 = {len(rows)}")
    overall = compute_metrics(rows)
    per_ed = per_edition(rows)
    wf = rolling_windows(rows)
    dual = synthetic_dual_book_test(rows)

    OUT = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_rows_imported": ins,
        "source_rows_skipped_existing": skip,
        "backfilled": done,
        "backfill_skipped_null_odds": skipped,
        "analyzed_rows": len(rows),
        "overall": overall,
        "per_edition": per_ed,
        "walk_forward": wf,
        "synthetic_dual_book": dual,
        "p0b_baseline": {
            "verdict_hit": 42.0,
            "draw": "21/21 全漏判",
            "away_bias": "pred_away 0.85 vs actual 1.03 (-0.18 低估)",
            "strong_home_hit": 40.9,
            "stake": "单庄全 PASS 拒注",
        },
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=2)
    print(f"\n=== DONE in {time.time()-t0:.1f}s | metrics -> {OUT_JSON} ===")

    # 控制台速览
    print("\n[OVERALL] verdict_hit=%.1f%%  draw_miss=%.1f%%  strong_home=%.1f%%  away_bias=%s  all_pass=%s  sum_pnl=%s"
          % (overall["verdict_hit"], overall["draw"]["draw_miss_rate"],
             overall["strong_home"]["hit_rate"], overall["score_bias"]["away_bias"],
             overall["stake"]["all_pass"], overall["stake"]["sum_pnl"]))
    print("[PER EDITION verdict_hit]:", {ed: per_ed[ed]["verdict_hit"] for ed in per_ed})


if __name__ == "__main__":
    main()
