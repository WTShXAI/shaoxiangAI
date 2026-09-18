"""
Task #3+4 — 多时点 score 模型: 训练 + 诚实 walk-forward 验证.
统一模型(跨 checkpoint): 特征=[imp_home,imp_draw,imp_away,cur_sh,cur_sa,minute] -> 终场1X2(ridx).
基线:
  - market : 该 checkpoint 去水隐含概率(即模型看到的同一份 odds) -> 公平 +EV 对照
  - scoreonly : 纯机械(当前比分定胜负, 平->draw)
验证: 扩窗 walk-forward 3 折(按 kickoff 时间序), 报告每折 + 加权聚合(禁 pooled 重复计数, 见 P0-10).
指标: log-loss(越低越好) + top1 命中率, 分 checkpoint.
诚实口径(IR-20/30): 模型 vs 市场隐含概率; 若模型 LL >= 市场 -> 无 beat-market 信号.
"""
import csv, math, collections
import numpy as np
import lightgbm as lgb

ROWS = list(csv.DictReader(open("data/mh_dataset.csv", encoding="utf-8")))
FEATS = ["imp_home", "imp_draw", "imp_away", "cur_sh", "cur_sa", "minute"]

def to_f(v): return float(v)
samples = []
for r in ROWS:
    x = [to_f(r[f]) for f in FEATS]
    y = int(r["ridx"])
    mk = r["match_key"]; kt = float(r["kickoff_epoch"]); cp = int(r["cp"])
    imp = [to_f(r["imp_home"]), to_f(r["imp_draw"]), to_f(r["imp_away"])]
    csh, csa = int(r["cur_sh"]), int(r["cur_sa"])
    samples.append({"x": x, "y": y, "mk": mk, "kt": kt, "cp": cp,
                    "imp": imp, "csh": csh, "csa": csa})

def ll(probs, y):
    p = max(min(probs[y], 0.999), 0.001)
    return -math.log(p)

def scoreonly_pred(csh, csa):
    return 0 if csh > csa else (2 if csa > csh else 1)

def evaluate(preds_model, items):
    """preds_model: list of [p0,p1,p2]; items: 对应样本. 返回 market/scoreonly 同口径."""
    m_ll = s_ll = mo_ll = 0.0
    m_acc = s_acc = mo_acc = 0
    n = len(items)
    for pm, it in zip(preds_model, items):
        y = it["y"]
        m_ll += ll(pm, y)
        mo_ll += ll(it["imp"], y)            # market baseline
        sp = scoreonly_pred(it["csh"], it["csa"])
        svec = [0, 0, 0]; svec[sp] = 1.0
        s_ll += ll(svec, y)
        m_acc += (int(np.argmax(pm)) == y)
        mo_acc += (int(np.argmax(it["imp"])) == y)
        s_acc += (sp == y)
    return {
        "n": n,
        "model_ll": m_ll / n, "market_ll": mo_ll / n, "scoreonly_ll": s_ll / n,
        "model_acc": m_ll and m_acc / n, "market_acc": mo_acc / n, "scoreonly_acc": s_acc / n,
    }

# 按 match 时间序分组, 避免同场多 checkpoint 跨折泄漏
mks = sorted({s["mk"] for s in samples}, key=lambda m: min(s["kt"] for s in samples if s["mk"] == m))
kt_of = {m: min(s["kt"] for s in samples if s["mk"] == m) for m in mks}
mks_sorted = sorted(mks, key=lambda m: kt_of[m])
by_mk = collections.defaultdict(list)
for s in samples: by_mk[s["mk"]].append(s)

# 扩窗 walk-forward 3 折: train 截止 60/80/90%, test 其后 20%
cuts = [0.60, 0.80, 0.90]
folds = []
for c in cuts:
    k = int(len(mks_sorted) * c)
    train_mk = mks_sorted[:k]
    test_mk = mks_sorted[k:]
    if not test_mk: continue
    folds.append((train_mk, test_mk))

print(f"[INFO] matches={len(mks_sorted)} samples={len(samples)} folds={len(folds)}")
agg = {"model_ll": 0, "market_ll": 0, "scoreonly_ll": 0,
       "model_acc": 0, "market_acc": 0, "scoreonly_acc": 0, "n": 0}

for fi, (train_mk, test_mk) in enumerate(folds, 1):
    tr = [s for m in train_mk for s in by_mk[m]]
    te = [s for m in test_mk for s in by_mk[m]]
    Xtr = np.array([s["x"] for s in tr]); Ytr = np.array([s["y"] for s in tr])
    Xte = np.array([s["x"] for s in te])
    clf = lgb.LGBMClassifier(n_estimators=400, max_depth=5, learning_rate=0.05,
                             num_leaves=31, min_child_samples=20, subsample=0.9,
                             collinear_treatment="dropy", verbose=-1,
                             random_state=42)
    clf.fit(Xtr, Ytr)
    P = clf.predict_proba(Xte)
    ev = evaluate([p.tolist() for p in P], te)
    print(f"\n=== Fold {fi}: train={len(tr)} test={len(te)} "
          f"(test kickoff {min(kt_of[m] for m in test_mk):.0f}..{max(kt_of[m] for m in test_mk):.0f}) ===")
    print(f"  log-loss  model={ev['model_ll']:.4f}  market={ev['market_ll']:.4f}  scoreonly={ev['scoreonly_ll']:.4f}")
    print(f"  top1-acc  model={ev['model_acc']*100:.1f}%  market={ev['market_acc']*100:.1f}%  scoreonly={ev['scoreonly_acc']*100:.1f}%")
    # 分 checkpoint
    for cp in [0, 45, 60, 75, 85]:
        sub = [s for s in te if s["cp"] == cp]
        if not sub: continue
        idx = [te.index(s) for s in sub]
        evc = evaluate([P[i].tolist() for i in idx], sub)
        delta = evc["model_ll"] - evc["market_ll"]
        flag = "BEAT" if delta < -0.001 else ("~" if abs(delta) <= 0.001 else "WORSE")
        print(f"   cp={cp:>2}: n={evc['n']:>4} modelLL={evc['model_ll']:.4f} mktLL={evc['market_ll']:.4f} "
              f"Δ={delta:+.4f}[{flag}] modelAcc={evc['model_acc']*100:.1f}% scoreonlyAcc={evc['scoreonly_acc']*100:.1f}%")
    for kk in ("model_ll", "market_ll", "scoreonly_ll", "model_acc", "market_acc", "scoreonly_acc"):
        agg[kk] += ev[kk] * ev["n"]   # 按 N 加权, 禁 pooled
    agg["n"] += ev["n"]

print("\n=== AGGREGATE (N-weighted across folds, honest) ===")
for kk in ("model_ll", "market_ll", "scoreonly_ll"):
    agg[kk] /= agg["n"]
for kk in ("model_acc", "market_acc", "scoreonly_acc"):
    agg[kk] /= agg["n"]
print(f"  log-loss  model={agg['model_ll']:.4f}  market={agg['market_ll']:.4f}  scoreonly={agg['scoreonly_ll']:.4f}")
print(f"  top1-acc  model={agg['model_acc']*100:.1f}%  market={agg['market_acc']*100:.1f}%  scoreonly={agg['scoreonly_acc']*100:.1f}%")
print(f"  total test samples={agg['n']}")
delta = agg["model_ll"] - agg["market_ll"]
print(f"  VERDICT: model vs market ΔLL={delta:+.4f} -> "
      f"{'MODEL BEATS MARKET' if delta < -0.001 else ('TIE' if abs(delta) <= 0.001 else 'NO EDGE (model >= market)')}")
