"""
WC 全管道 walk-forward 泛化验证 (optimized: 赔率+ML+规则, 时序外推)
=====================================================================
目的: 验证生产「optimized」模式 (wc_engine._predict_optimized) 在
   时序外推下的真实准确率 —— 即 96.2% 是否靠重叠训练数据虚高。

做法:
- 备份生产模型 → 按日期切分 3 个 expanding-window 折
- 每折: 用 DB 中 match_date<=cut 的比赛训练 ML, dump 覆盖 saved_models,
        重置 wc_engine 模型缓存, 对该折测试窗内的 backtest 真实比赛
        跑 W.predict(mode="optimized"), 统计准确率
- 同时统计同批比赛的 argmax(纯赔率) 基线对照
- 结束恢复生产模型

关键事实: _get_wc_features 按队名查 DB 特定比赛特征(95%命中),
  故 ML 层对 backtest 比赛会激活; 但模型权重按折时序训练 → 真外推。

用法:
  .venv/Scripts/python.exe scripts/wc_walkforward_optimized.py
"""
import sqlite3, os, sys, json, warnings, shutil
from datetime import datetime
warnings.filterwarnings("ignore")
import numpy as np
import joblib

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import wc_train_pipeline as TP
sys.path.insert(0, os.path.join(SCRIPTS, "..", "pipeline"))
import wc_engine as W

ROOT = os.path.dirname(SCRIPTS)
DB = os.path.join(ROOT, "data", "football_data.db")
SAVED = os.path.join(ROOT, "saved_models")
BACKUP = os.path.join(SAVED, "_wf_backup")
OUT_JSON = os.path.join(ROOT, "deliverables", "wc2026_walkforward_optimized.json")


def load_backtest():
    with open("deliverables/wc2026_full_backtest.json", encoding="utf-8") as f:
        old = json.load(f)
    group = old.get("details", [])
    additions = [
        {"date":"2026-06-21","home":"西班牙","away":"沙特","oh":1.08,"od":8.80,"oa":18.0,"hcp":-2.5,"ou":3.5,"res":"H","sc":"4-0"},
        {"date":"2026-06-23","home":"葡萄牙","away":"乌兹别克","oh":1.22,"od":5.90,"oa":10.00,"hcp":-1.5,"ou":3.0,"res":"H","sc":"5-0"},
        {"date":"2026-06-27","home":"佛得角","away":"沙特","oh":2.47,"od":3.35,"oa":2.62,"hcp":0.0,"ou":2.5,"res":"D","sc":"0-0"},
        {"date":"2026-06-27","home":"民主刚果","away":"乌兹别克","oh":2.27,"od":3.25,"oa":2.97,"hcp":0.0,"ou":2.5,"res":"H","sc":"3-1"},
    ]
    existing = {(d["home"], d["away"]) for d in group}
    for a in additions:
        if (a["home"], a["away"]) not in existing:
            group.append(a)
    with open("data/wc2026_r16_results.json", encoding="utf-8") as f:
        r16 = json.load(f)["matched"]

    def infer_md(s):
        d = datetime.strptime(s, "%Y-%m-%d")
        if d <= datetime(2026,6,16): return 1
        if d <= datetime(2026,6,22): return 2
        return 3
    matches = []
    for d in group:
        matches.append({"date":d["date"],"home":d["home"],"away":d["away"],
                        "oh":d["oh"],"od":d["od"],"oa":d["oa"],
                        "hcp":d.get("hcp") or 0.0,"ou":d.get("ou") or 2.5,
                        "res":d["res"],"stage":"group","matchday":infer_md(d["date"])})
    for d in r16:
        matches.append({"date":d["date"],"home":d["home"],"away":d["away"],
                        "oh":d["oh"],"od":d["od"],"oa":d["oa"],
                        "hcp":d.get("hcp") or 0.0,"ou":d.get("ou") or 2.5,
                        "res":d["res"],"stage":"knockout","matchday":0})
    return matches


def load_train_Xy(cut_date):
    conn = sqlite3.connect(DB); cur = conn.cursor()
    cur.execute("PRAGMA table_info(match_features)")
    all_cols = [r[1] for r in cur.fetchall()]
    skip = {"feature_id","match_id","created_at"}
    cur.execute("""SELECT m.match_id, m.match_date, m.final_result FROM matches m
      WHERE m.league_name='世界杯' AND m.final_result IS NOT NULL AND m.match_date<=?""", (cut_date,))
    rows = cur.fetchall()
    mids = [r[0] for r in rows]
    ymap = {"H":0,"D":1,"A":2}
    y_all = np.array([ymap[r[2]] for r in rows])
    placeholders = ",".join("?"*len(mids)); col_str = ",".join(all_cols)
    cur.execute(f"SELECT match_id,{col_str} FROM match_features WHERE match_id IN ({placeholders})", mids)
    raw = {r[0]: dict(zip(all_cols, r[1:])) for r in cur.fetchall()}
    conn.close()
    feat_cols = [c for c in all_cols if c not in skip]
    clean_cols = []
    for c in feat_cols:
        sample = next((raw[m][c] for m in mids if m in raw and raw[m][c] is not None), None)
        if sample is None: continue
        try: float(sample); clean_cols.append(c)
        except: continue
    valid = [m for m in mids if m in raw]
    X = np.array([[float(raw[m][c]) for c in clean_cols] for m in valid], dtype=float)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    y = y_all[[mids.index(m) for m in valid]]
    return X, y, clean_cols


def run_fold(cut, test_lo, test_hi, matches):
    Xtr, ytr, cols = load_train_Xy(cut)
    main_pkg, *_ = TP.train_main_model(Xtr, ytr, cols)
    de_pkg, _ = TP.train_draw_expert(Xtr, ytr, cols)
    # 覆盖生产模型
    joblib.dump(main_pkg, os.path.join(SAVED, "wc_main_v1.joblib"))
    joblib.dump(de_pkg, os.path.join(SAVED, "draw_expert_v3_focal.joblib"))
    # 强制 wc_engine 下次 predict 重新从盘加载
    W._MAIN_LOADED = False
    W._DE_LOADED = False

    ok = arg_ok = tot = 0
    for m in matches:
        if not (test_lo <= m["date"] <= test_hi):
            continue
        tot += 1
        res = m["res"]
        odds = [m["oh"], m["od"], m["oa"]]
        ai = odds.index(min(odds)); ap = ["H","D","A"][ai]
        arg_ok += (ap == res)
        mi = W.MatchInput(home=m["home"], away=m["away"], odds_h=m["oh"], odds_d=m["od"],
                         odds_a=m["oa"], hcp=m["hcp"], ou_line=m["ou"],
                         stage=m["stage"], matchday=m["matchday"])
        pred = W.predict(mi, mode="optimized").prediction
        ok += (pred == res)
    return {"n_train": int(len(ytr)), "n_test": tot,
            "opt_ok": ok, "opt_acc": round(ok/tot, 4) if tot else None,
            "argmax_ok": arg_ok, "argmax_acc": round(arg_ok/tot, 4) if tot else None}


if __name__ == "__main__":
    print("=" * 64)
    print("WC 全管道 OPTIMIZED walk-forward (时序外推)")
    print("=" * 64)
    # 备份生产模型
    os.makedirs(BACKUP, exist_ok=True)
    for fn in ("wc_main_v1.joblib", "draw_expert_v3_focal.joblib"):
        shutil.copy(os.path.join(SAVED, fn), os.path.join(BACKUP, fn))
    print("[backup] 生产模型已备份到", BACKUP)

    matches = load_backtest()
    print(f"[backtest] 总比赛数={len(matches)}")

    FOLDS = [
        ("2026-06-16", "2026-06-17", "2026-06-22", "Fold1 训练≤MD1 → 测试MD2"),
        ("2026-06-22", "2026-06-23", "2026-06-27", "Fold2 训练≤MD1+2 → 测试MD3"),
        ("2026-06-27", "2026-06-28", "2026-12-31", "Fold3 训练≤小组赛 → 测试淘汰赛"),
    ]
    results = []
    tot_test = wopt = warg = 0
    try:
        for cut, lo, hi, label in FOLDS:
            print(f"\n[{label}]")
            r = run_fold(cut, lo, hi, matches)
            r["label"] = label
            results.append(r)
            tot_test += r["n_test"]
            wopt += r["opt_ok"]; warg += r["argmax_ok"]
            print(f"  n_train={r['n_train']} n_test={r['n_test']} "
                  f"opt_acc={r['opt_acc']} argmax_acc={r['argmax_acc']}")
    finally:
        # 恢复生产模型
        for fn in ("wc_main_v1.joblib", "draw_expert_v3_focal.joblib"):
            shutil.copy(os.path.join(BACKUP, fn), os.path.join(SAVED, fn))
        W._MAIN_LOADED = False; W._DE_LOADED = False
        print("\n[restore] 生产模型已恢复")

    agg_opt = wopt / tot_test if tot_test else 0
    agg_arg = warg / tot_test if tot_test else 0
    print("\n" + "=" * 64)
    print(f"汇总(时序外推): optimized={agg_opt:.4f} | argmax(纯赔率)={agg_arg:.4f} | 测试样本={tot_test}")
    print(f"对照: 原报告 in-sample optimized=0.962 (重叠训练数据)")
    print("=" * 64)
    out = {
        "method": "expanding-window walk-forward of FULL optimized pipeline (odds+ML+rules)",
        "in_sample_optimized_reported": 0.962,
        "walkforward_optimized_acc": round(agg_opt, 4),
        "walkforward_argmax_acc": round(agg_arg, 4),
        "total_oos_test": tot_test,
        "folds": results,
        "caveat": "all WC matches dated 2026; within-tournament temporal only; ML features static per match (match-specific), model weights temporal",
    }
    json.dump(out, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"Saved {OUT_JSON}")
