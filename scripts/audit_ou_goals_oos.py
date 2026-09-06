"""
FootballAI — OU/goals 诚实 OOS 审计 (P0-② 补全数据续, Path A)

背景: OU/goals 模型(multi_ou_totals / multi_goals_total)此前因"标签源缺失"诚实
NOT_VERIFIABLE. 考古证明: 68 场 WC2026 比赛在 match_features 中已有 X(模型特征向量),
在 betting_markets.totals 中已有 OU 盘口线, 但真实比分(y)缺失. 这 68 场的比分实际
存于 matches 表, 仅因 ID/队名桥断裂无法直接取. 用户选定 Path A: 补齐这 68 场真实比分.

本脚本消费 `deliverables/ou_goals_scores.json`(match_id -> {hs, as}), 对原装 OU/goals
模型做**真·时间外样本 OOS**:
  - X = match_features 中该 match_id 的模型 feature_names 列 (特征已存在, 无需重算)
  - y = 由真实比分推导:
      * OU 模型(multi_ou_totals): 取该场 betting_markets.totals 的参考盘口线(优先 2.5),
        总进球 > 线 -> over(1), 否则 under(0)
      * 总进球模型(multi_goals_total): 总进球数 -> 分档(5档: 0-1/2/3/4/5+; 3档: 0-2/3/4+)
  - 跑 model.predict_proba(X) -> 与 y 比, 出 OOS accuracy / macro-F1 / AUC(ovr)
  - 对比模型自带随机切分指标(metrics), 出 honest verdict

诚实边界:
  - 模型自带指标是随机切分(n_train 24729 / n_test 8631), 本脚本是真正 unseen 新时代
    比赛的外样本, 二者对比直接暴露随机切分乐观偏差.
  - 模型训练时的精确标签定义(盘口线/分档边界)未知, 本脚本用标准 2.5 线 / 标准分档
    作近似, 已在产物中标注 assumption. 若模型训练用不同线, OU 标签需相应调整.
  - 样本量 < 30 时统计无力, 产物标 insufficient_sample, 仅作方法验证.

用法: 填好 deliverables/ou_goals_scores.json 后 `python scripts/audit_ou_goals_oos.py`
"""
import joblib, sqlite3, os, json, warnings
import numpy as np, pandas as pd
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
warnings.filterwarnings("ignore")

DB = "data/football_data.db"
SCORES = "deliverables/ou_goals_scores.json"
OUT = "deliverables/model_oos_ou_goals.json"
SM = "saved_models"

OU_MODELS = sorted(
    [os.path.basename(f) for f in __import__("glob").glob(os.path.join(SM, "multi_ou_totals_*.joblib"))]
)
GOALS_MODELS = sorted(
    [os.path.basename(f) for f in __import__("glob").glob(os.path.join(SM, "multi_goals_total_*.joblib"))]
)


def load_scores():
    d = json.load(open(SCORES))
    return d.get("scores", {}), d.get("identities", {}), d.get("_validated", False)


def get_reference_line(con, mid):
    lines = [r[0] for r in con.execute(
        "SELECT DISTINCT market_line FROM betting_markets WHERE match_id=? AND market_type='totals'",
        (mid,))]
    if not lines:
        return 2.5
    # 优先 2.5, 否则取中位数
    if 2.5 in lines:
        return 2.5
    return float(sorted(lines)[len(lines) // 2])


def build_Xy(con, model_file, scores, ref_line_cache):
    m = joblib.load(os.path.join(SM, model_file))
    fn = m.get("feature_names") or m.get("feature_cols")
    est = m["model"]
    metrics = m.get("metrics", {})
    n_classes = metrics.get("n_classes")
    is_ou = "ou_totals" in model_file

    # X from match_features (only match_ids present in scores)
    sids = [int(k) for k in scores.keys()]
    actual = [r[1] for r in con.execute("PRAGMA table_info(match_features)")]
    valid = [c for c in fn if c in actual]
    dropped = [c for c in fn if c not in actual]
    if not valid:
        return None, f"无可用特征列({len(fn)}列均不在match_features)"
    ph = ",".join(str(i) for i in sids)
    mf = pd.read_sql(f"SELECT match_id, {','.join(valid)} FROM match_features WHERE match_id IN ({ph})", con)
    avail = set(mf["match_id"].tolist())

    Xrows, yrows, used_ids = [], [], []
    for mid in sids:
        if mid not in avail:
            continue
        sc = scores[str(mid)]
        hs, a_s = sc["hs"], sc["as"]
        total = hs + a_s
        if is_ou:
            line = ref_line_cache.get(mid) or get_reference_line(con, mid)
            ref_line_cache[mid] = line
            y = 1 if total > line else 0
        else:
            if n_classes == 5:
                y = 0 if total <= 1 else (1 if total == 2 else (2 if total == 3 else (3 if total == 4 else 4)))
            elif n_classes == 3:
                y = 0 if total <= 2 else (1 if total == 3 else 2)
            else:
                y = total  # fallback
        row = mf[mf["match_id"] == mid][valid].values.astype(float)
        if np.isnan(row).any():
            continue
        Xrows.append(row[0])
        yrows.append(y)
        used_ids.append(mid)

    if len(Xrows) < 2:
        return None, f"有效样本不足({len(Xrows)})"
    X = np.array(Xrows)
    y = np.array(yrows)
    return (X, y, valid, dropped, est, metrics, is_ou, n_classes, used_ids), None


def main():
    scores, identities, validated = load_scores()
    if not validated:
        print("[FATAL] ou_goals_scores.json 未通过对齐验证 (_validated=false).")
        print("        证据: match_id 序列 29 缺口 + 仅4个内部锚点 + 置换检验 p≈0.09 (X2/OU均不显著).")
        print("        这批比分无法可靠对齐到 570357 空间 match_id, 禁止作为 OOS 真分消费,")
        print("        否则会产出误导性指标. 若要启用, 须先获得可靠对齐键(队名/日期桥)并令置换检验 p<0.05.")
        return
    if not scores:
        print("[FATAL] deliverables/ou_goals_scores.json 无 scores; 请先补 68 场真实比分")
        return
    con = sqlite3.connect(DB)
    ref_cache = {}
    results = []
    for mf in OU_MODELS + GOALS_MODELS:
        try:
            out, err = build_Xy(con, mf, scores, ref_cache)
            if out is None:
                results.append({"model": mf, "status": "SKIPPED", "reason": err})
                print(f"[SKIP] {mf}: {err}")
                continue
            X, y, valid, dropped, est, metrics, is_ou, n_classes, used = out
            p = est.predict_proba(X)
            acc = accuracy_score(y, np.argmax(p, 1))
            f1 = f1_score(y, np.argmax(p, 1), average="macro", zero_division=0)
            try:
                auc = float(roc_auc_score(y, p, multi_class="ovr"))
            except Exception:
                auc = None
            stored_auc = metrics.get("auc")
            stored_acc = metrics.get("accuracy")
            gap = round(float(stored_auc) - auc, 4) if (stored_auc and auc is not None) else None
            # verdict
            if auc is None:
                verdict = "OOS_UNCOMPUTABLE"
            elif len(y) < 30:
                verdict = "INSUFFICIENT_SAMPLE"
            elif stored_auc is None:
                verdict = "HAS_OOS" if auc > 0.6 else "WEAK_OOS"
            else:
                if auc < 0.55 and gap > 0.1:
                    verdict = "IN_SAMPLE_ONLY"
                else:
                    verdict = "HAS_OOS" if gap < 0.1 else "LEAK_SUSPECTED"
            rec = {
                "model": mf, "type": "OU" if is_ou else "GOALS",
                "n_classes": n_classes, "n_test": int(len(y)),
                "dropped_columns": dropped, "n_features_used": len(valid),
                "oos_accuracy": round(float(acc), 4),
                "oos_macro_f1": round(float(f1), 4),
                "oos_roc_auc_ovr": round(auc, 4) if auc is not None else None,
                "stored_auc_random_split": round(float(stored_auc), 4) if stored_auc else None,
                "stored_accuracy_random_split": round(float(stored_acc), 4) if stored_acc else None,
                "auc_gap": gap, "verdict": verdict,
            }
            results.append(rec)
            print(f"[OK] {mf}: stored_auc={rec['stored_auc_random_split']} -> OOS_auc={rec['oos_roc_auc_ovr']} "
                  f"acc={rec['oos_accuracy']} n={rec['n_test']} verdict={verdict}")
        except Exception as e:
            results.append({"model": mf, "error": f"{type(e).__name__}: {str(e)[:160]}"})
            print(f"[ERR] {mf}: {e}")
    con.close()
    out = {
        "method": "原装OU/goals模型对 68场WC(match_features X + 真实比分 y)的真·时间外样本OOS",
        "label_assumption": "OU: 参考盘口线优先2.5, 总进球>线=over(1); GOALS: 5档(0-1/2/3/4/5+)或3档(0-2/3/4+)",
        "n_total_matches_with_ou_odds": 68,
        "n_scores_provided": len(scores),
        "results": results,
        "note": "样本<30 标 INSUFFICIENT_SAMPLE(仅方法验证). 补齐68场比分后重跑得统计有意义结论.",
    }
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved -> {OUT}")


if __name__ == "__main__":
    main()
