"""Verify the premise: is live (in-play) OU margin structurally fixed?
And A/B: de-vig probabilities vs raw 1/odds as OU model features.
Read-only analysis, no model save.
"""
import sqlite3, time, numpy as np, json, os, warnings
warnings.filterwarnings("ignore")
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, log_loss
from sklearn.model_selection import GroupKFold

t0 = time.time()
DB = "data/events.db"
OUTCOMES_CACHED = {}

def load_results():
    c = sqlite3.connect(DB)
    rows = c.execute("SELECT home, away, result, score_home, score_away FROM match_outcomes").fetchall()
    c.close()
    d = {}
    for h, a, res, sh, sa in rows:
        d[(h, a)] = (res, sh, sa)
    return d

def devig(a, b):
    if not a or not b or a <= 1.01 or b <= 1.01:
        return None
    ia, ib = 1.0 / a, 1.0 / b
    s = ia + ib
    if s <= 1.0:
        return None
    return ia / s, ib / s

def parse_score(s):
    if not s:
        return (0, 0)
    s = str(s).strip()
    for sep in [":", "-"]:
        if sep in s:
            try:
                p = s.split(sep)
                return (int(p[0]), int(p[1]))
            except Exception:
                return (0, 0)
    return (0, 0)

print("[1/4] loading match results ...")
RES = load_results()
print("   outcomes:", len(RES))

print("[2/4] folding in-play OU snapshots (may take ~1-2 min) ...")
c = sqlite3.connect(DB)
cur = c.execute(
    "SELECT match_key, line, selection, odds, score_at, minute_at "
    "FROM odds_snapshots WHERE minute_at>0 AND market LIKE 'OU_%' "
    "AND selection IN ('over','under')"
)
fold = {}  # (match_key, minute, line) -> {over, under, score}
n = 0
for mk, line, sel, odds, score_at, minute in cur:
    key = (mk, int(minute), str(line))
    d = fold.setdefault(key, {"over": None, "under": None, "score": score_at})
    if sel == "over":
        d["over"] = odds
    else:
        d["under"] = odds
    n += 1
c.close()
print("   raw OU in-play rows:", n, "| folded snapshots:", len(fold), "elapsed %.1fs" % (time.time() - t0))

# margin distribution + build rows
margins = []
rows = []  # dict per usable snapshot
unlabeled = 0
for (mk, minute, line), d in fold.items():
    if not d["over"] or not d["under"]:
        continue
    o, u = d["over"], d["under"]
    margin = 1.0 / o + 1.0 / u
    margins.append(margin)
    if " vs " not in mk:
        continue
    h, a = mk.split(" vs ", 1)
    lab = RES.get((h, a))
    if not lab:
        unlabeled += 1
        continue
    res, sh, sa = lab
    if sh is None or sa is None:
        continue
    sc = parse_score(d["score"])
    dev = devig(o, u)
    raw_over = 1.0 / o
    raw_under = 1.0 / u
    over_label = 1 if (sh + sa) > float(line) else 0
    rows.append({
        "mk": mk, "minute": int(minute), "line": float(line),
        "score_h": sc[0], "score_a": sc[1], "lead": sc[0] - sc[1],
        "dev_over": dev[0] if dev else np.nan, "dev_under": dev[1] if dev else np.nan,
        "raw_over": raw_over, "raw_under": raw_under,
        "margin": margin, "label": over_label,
    })

margins = np.array(margins)
print("\n[3/4] LIVE OU MARGIN DISTRIBUTION (1/over + 1/under)")
print("   n snapshots:", len(margins))
print("   mean=%.4f std=%.4f min=%.4f p5=%.4f p50=%.4f p95=%.4f max=%.4f"
      % (margins.mean(), margins.std(), margins.min(),
         np.percentile(margins,5), np.percentile(margins,50),
         np.percentile(margins,95), margins.max()))
tight = np.mean((margins >= 1.02) & (margins <= 1.07))
print("   %% within [1.02,1.07] (tight fixed band): %.1f%%" % (100*tight))
print("   %% within [1.00,1.10]: %.1f%%" % (100*np.mean((margins>=1.0)&(margins<=1.10))))

print("\n   usable OU rows (labeled):", len(rows), "| unlabeled matches skipped:", unlabeled)

# A/B model
def cv_train(X, y, groups, name):
    gkf = GroupKFold(n_splits=4)
    aucs, lls, accs = [], [], []
    for tr, te in gkf.split(X, y, groups):
        Xtr, Xte, ytr, yte, gtr = X[tr], X[te], y[tr], y[te], groups[tr]
        nval = max(500, int(len(Xtr)*0.1))
        Xv, yv = Xtr[-nval:], ytr[-nval:]
        Xtr2, ytr2 = Xtr[:-nval], ytr[:-nval]
        cfg = dict(num_leaves=31, min_child_samples=60, subsample=0.85,
                   colsample_bytree=0.85, reg_lambda=5.0, reg_alpha=0.5,
                   learning_rate=0.02, n_estimators=800, early_stopping_rounds=40,
                   random_state=42, n_jobs=-1, verbose=-1)
        clf = lgb.LGBMClassifier(objective="binary", **cfg)
        clf.fit(Xtr2, ytr2, eval_set=[(Xv, yv)], eval_metric="binary_logloss")
        p = clf.predict_proba(Xte)[:, 1]
        aucs.append(roc_auc_score(yte, p))
        lls.append(log_loss(yte, p, labels=[0,1]))
        accs.append(((p > 0.5).astype(int) == yte).mean())
    return (np.mean(aucs), np.std(aucs)), (np.mean(lls), np.std(lls)), (np.mean(accs), np.std(accs))

X = np.array([[r["minute"]/90.0, r["score_h"], r["score_a"], r["lead"],
              r["dev_over"], r["dev_under"], r["line"]] for r in rows], dtype=float)
Xraw = np.array([[r["minute"]/90.0, r["score_h"], r["score_a"], r["lead"],
                 r["raw_over"], r["raw_under"], r["line"]] for r in rows], dtype=float)
y = np.array([r["label"] for r in rows])
groups = np.array([r["mk"] for r in rows])

print("\n[4/4] A/B OU model: DE-VIG vs RAW 1/odds")
(a_d, l_d, c_d) = cv_train(X, y, groups, "devig")
(a_r, l_r, c_r) = cv_train(Xraw, y, groups, "raw")
print("   DE-VIG : AUC=%.4f±%.4f  logloss=%.4f±%.4f  acc=%.4f±%.4f" % (a_d[0],a_d[1],l_d[0],l_d[1],c_d[0],c_d[1]))
print("   RAW    : AUC=%.4f±%.4f  logloss=%.4f±%.4f  acc=%.4f±%.4f" % (a_r[0],a_r[1],l_r[0],l_r[1],c_r[0],c_r[1]))
print("   delta AUC(raw-devig)=%+.4f  delta logloss=%+.4f" % (a_r[0]-a_d[0], l_r[0]-l_d[0]))
print("\nelapsed %.1fs" % (time.time() - t0))
