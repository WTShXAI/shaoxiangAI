# -*- coding: utf-8 -*-
"""
统一 OOS 对决 · 修正版 (用「真实融合组件」, 非泊松碎片)
=========================================================
纠正 unified_inplay_duel.py 的致命错误: 上一轮误把 live_goal_probe 的泊松重建层
当成「本系统」, 但其 AUC 0.747 / LogLoss 5.88 是校准崩坏的碎片。真实的本系统融合体是:
  - 静态(赛前): pipeline/william_inter_model.py 的 WI LightGBM 教师 (data/wi_1x2_model.joblib)
  - 滚球(in-play): data/live_1x2_model.joblib (LGBMClassifier, 7维, 文档 AUC 0.885)
  - 二者经 model_ensemble / model_dispatcher 融合, 盘口锚定由 unified_predictor 兜底。

本 harness 用正确组件测算「本系统融合 vs GitHub(HeuristicPredictor) vs 去水隐含基线」:
  静态集: football_data.db matches JOIN match_features (赛前盘+赛果), 时间切分 OOS
  滚球集: events.db odds_snapshots in-play 1X2 + match_outcomes.result

指标: 宏平均 AUC(OvR) / LogLoss / 多类 Brier / Top-1 Acc / 每类 AUC。
"""
import os, sys, json, sqlite3, random, math
from collections import Counter
import numpy as np

ROOT = r"D:\Architecture"
GH   = r"C:\Users\ShXAI\Documents\GitHub\shaoxiangAI"
DB_STATIC = os.path.join(ROOT, "data", "football_data.db")
DB_INPLAY = os.path.join(ROOT, "data", "events.db")
SEED = 20260828

sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(GH, "agents"))
import joblib
from analysis.live_goal_probe import _dewater_1x2
from analysis.live_rollball_features import build_1x2_features
from pipeline.william_inter_model import derive_features as wi_derive
from heuristic_predictor import HeuristicPredictor

LABELS = ["H", "D", "A"]
LIDX = {"H": 0, "D": 1, "A": 2}

# ── 加载真实融合组件 ──
WI   = joblib.load(os.path.join(ROOT, "data", "wi_1x2_model.joblib"))
LIVE = joblib.load(os.path.join(ROOT, "data", "live_1x2_model.joblib"))
GH_P = HeuristicPredictor()

def sys_static(home, draw, away, open_h=None, open_d=None, open_a=None):
    """本系统-静态: WI 教师 (赛前概率主干)。缺初盘时回退收盘。"""
    oh, od, oa = open_h or home, open_d or draw, open_a or away
    try:
        feats = wi_derive(oh, od, oa, home, draw, away)
    except Exception:
        return None
    if any(math.isnan(x) for x in feats):
        return None
    p = WI.predict_proba(np.array([feats], dtype=float))[0]
    return [float(x) for x in p]

def sys_inplay(h, d, a, sh, sa, minute):
    """本系统-滚球: live_1x2_model (文档 AUC 0.885)。"""
    x2 = _dewater_1x2(h, d, a)
    if x2 is None:
        return None
    feats = build_1x2_features(minute, sh, sa, x2[0], x2[1], x2[2])
    p = LIVE.predict_proba(np.array([feats], dtype=float))[0]
    return [float(x) for x in p]

def github_probs(h, d, a):
    p = GH_P.predict_proba(np.zeros((1, 1)), feature_names=[],
                           odds_data={"home": h, "draw": d, "away": a}, league_name="")
    return [float(p[0][0]), float(p[0][1]), float(p[0][2])]

def naive_probs(h, d, a):
    x2 = _dewater_1x2(h, d, a)
    return [x2[0], x2[1], x2[2]] if x2 else [1/3, 1/3, 1/3]

def _norm(M):
    M = np.asarray(M, dtype=float)
    M = np.where(np.isfinite(M), M, 0.0)
    s = M.sum(axis=1, keepdims=True)
    bad = int(np.sum((~np.isfinite(s)) | (s <= 0) | (np.abs(s - 1.0) > 1e-6)))
    M = np.where(s > 0, M / s, np.full_like(M, 1/3))
    return M, bad

def metrics(y, M):
    from sklearn.metrics import roc_auc_score, log_loss
    M, _ = _norm(M)
    auc = float(roc_auc_score(y, M, multi_class="ovr", average="macro", labels=[0,1,2]))
    ll  = float(log_loss(y, M, labels=[0,1,2]))
    oh  = np.zeros_like(M); oh[np.arange(len(y)), y] = 1.0
    brier = float(((oh - M)**2).sum(axis=1).mean())
    acc = float((M.argmax(axis=1) == y).mean())
    pc = {}
    for i, lab in enumerate(LABELS):
        try:
            pc[lab] = float(roc_auc_score((y == i).astype(int), M[:, i]))
        except Exception:
            pc[lab] = float("nan")
    return dict(auc=auc, logloss=ll, brier=brier, acc=acc, per_class_auc=pc)

def show(tag, n, maj, S, G, N):
    print(f"\n===== {tag}  (n={n}, 多数类基线 acc={maj:.3f}) =====")
    print(f"{'系统':<26}{'AUC':>8}{'LogLoss':>10}{'Brier':>9}{'Acc':>8}")
    print("-"*61)
    for name, m in [("本系统(WI/LIVE 真实组件)", S), ("GitHub(HeuristicPredictor)", G), ("基线(去水隐含)", N)]:
        print(f"{name:<24}{m['auc']:>8.4f}{m['logloss']:>10.4f}{m['brier']:>9.4f}{m['acc']:>8.3f}")
    print(f"  每类AUC 本系统 H={S['per_class_auc']['H']:.3f}/D={S['per_class_auc']['D']:.3f}/A={S['per_class_auc']['A']:.3f}"
          f" | GitHub H={G['per_class_auc']['H']:.3f}/D={G['per_class_auc']['D']:.3f}/A={G['per_class_auc']['A']:.3f}"
          f" | 基线 H={N['per_class_auc']['H']:.3f}/D={N['per_class_auc']['D']:.3f}/A={N['per_class_auc']['A']:.3f}")

def fetch_static(limit=20000):
    con = sqlite3.connect(DB_STATIC); con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()
    cur.execute("""
        SELECT m.match_date, m.final_result,
               f.odds_open_h, f.odds_open_d, f.odds_open_a,
               f.odds_close_h, f.odds_close_d, f.odds_close_a
        FROM matches m JOIN match_features f ON m.match_id=f.match_id
        WHERE m.final_result IN ('H','D','A')
          AND f.odds_close_h>1.01 AND f.odds_close_d>1.01 AND f.odds_close_a>1.01
          AND m.match_date > '2023-01-01'
        ORDER BY RANDOM() LIMIT ?
    """, (limit,))
    rows = cur.fetchall(); con.close()
    out = []
    for date, res, ooh, ood, ooa, oh, od, oa in rows:
        out.append(dict(y=LIDX[res], oh=oh, od=od, oa=oa, ooh=ooh, ood=ood, ooa=ooa))
    return out

def fetch_inplay(limit=6000):
    con = sqlite3.connect(DB_INPLAY); con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()
    cur.execute("SELECT home||' vs '||away, result FROM match_outcomes WHERE result IN ('home','draw','away')")
    # match_outcomes.result 存小写 'home'/'draw'/'away', 须映射回 LIDX 的 'H'/'D'/'A'
    mo = {r[0]: LIDX[r[1][0].upper()] for r in cur.fetchall()}
    q = """
        SELECT match_key, minute_at, score_at,
               AVG(CASE WHEN selection='home' THEN odds END) AS h,
               AVG(CASE WHEN selection='draw' THEN odds END) AS d,
               AVG(CASE WHEN selection='away' THEN odds END) AS a
        FROM odds_snapshots
        WHERE market='1X2' AND minute_at>0 AND selection IN ('home','draw','away')
        GROUP BY match_key, minute_at, score_at
        HAVING h IS NOT NULL AND d IS NOT NULL AND a IS NOT NULL
    """
    rows = cur.execute(q).fetchall(); con.close()
    out = []
    for mk, minute, score_at, h, d, a in rows:
        if mk not in mo: continue
        if not (1 <= minute <= 95): continue
        if not (h > 1.01 and d > 1.01 and a > 1.01): continue
        sh = sa = 0
        if score_at and '-' in score_at:
            try: sh, sa = (int(x) for x in score_at.split('-'))
            except Exception: sh = sa = 0
        out.append(dict(y=mo[mk], h=h, d=d, a=a, sh=sh, sa=sa, minute=int(minute)))
    random.seed(SEED)
    if len(out) > limit: out = random.sample(out, limit)
    return out

def run_static():
    rows = fetch_static()
    y = np.array([x["y"] for x in rows])
    S = []; G = []; N = []
    for x in rows:
        sp = sys_static(x["oh"], x["od"], x["oa"], x["ooh"], x["ood"], x["ooa"])
        S.append(sp if sp else [1/3,1/3,1/3])
        G.append(github_probs(x["oh"], x["od"], x["oa"]))
        N.append(naive_probs(x["oh"], x["od"], x["oa"]))
    maj = Counter(x["y"] for x in rows).most_common(1)[0][1] / len(rows)
    return "STATIC(赛前, football_data.db OOS>2023)", len(rows), maj, metrics(y, np.array(S)), metrics(y, np.array(G)), metrics(y, np.array(N))

def run_inplay():
    rows = fetch_inplay()
    y = np.array([x["y"] for x in rows])
    S = []; G = []; N = []
    for x in rows:
        sp = sys_inplay(x["h"], x["d"], x["a"], x["sh"], x["sa"], x["minute"])
        S.append(sp if sp else [1/3,1/3,1/3])
        G.append(github_probs(x["h"], x["d"], x["a"]))
        N.append(naive_probs(x["h"], x["d"], x["a"]))
    maj = Counter(x["y"] for x in rows).most_common(1)[0][1] / len(rows)
    return "IN-PLAY(滚球, events.db)", len(rows), maj, metrics(y, np.array(S)), metrics(y, np.array(G)), metrics(y, np.array(N))

def main():
    random.seed(SEED)
    print("[start] 用真实融合组件重测: 本系统(WI/LIVE) vs GitHub vs 去水隐含\n")
    tS, nS, majS, SS, GS, NS = run_static()
    tI, nI, majI, SI, GI, NI = run_inplay()

    show(tS, nS, majS, SS, GS, NS)
    show(tI, nI, majI, SI, GI, NI)

    result = {
        "correction_note": "上一轮误用 live_goal_probe 泊松碎片(AUC0.747/LL5.88); 本版用真实组件 wi_1x2_model + live_1x2_model",
        "static": {"n": nS, "majority_acc": round(majS,4), "system": SS, "github": GS, "naive": NS},
        "inplay": {"n": nI, "majority_acc": round(majI,4), "system": SI, "github": GI, "naive": NI},
    }
    outp = os.path.join(ROOT, "deliverables", "unified_corrected_duel_result.json")
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[ok] 结果 -> {outp}")

if __name__ == "__main__":
    main()
