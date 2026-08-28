# -*- coding: utf-8 -*-
"""
verify_live_ou_ledger.py  (2026-08-26)
=====================================
终场后自动回查 live OU 解码 ledger：
  - 读取 data/live_ou_decode_ledger.jsonl
  - 对每条 verified=False 的记录，按 match_key(主队 vs 客队) 查 match_outcomes
  - 一旦 result 落地(result IS NOT NULL, 即训练脚本口径的"终场")：
      * 填 final_result / 真实总球 / 大球是否打出(model_correct)
      * 确认下轮 retrain 是否会自动纳入（训练只读 match_outcomes + 该场需有 in-play 快照）
  - 幂等：已 verified 的记录不再动；无 result 则跳过等待下次。

用法: python scripts/verify_live_ou_ledger.py
"""
import os, json, sqlite3

ROOT = r"D:\Architecture"
GQ = os.path.join(ROOT, "data", "events.db")
LEDGER = os.path.join(ROOT, "data", "live_ou_decode_ledger.jsonl")


def load_records():
    if not os.path.exists(LEDGER):
        return []
    out = []
    with open(LEDGER, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def save_records(recs):
    with open(LEDGER, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def lookup_result(home, away):
    c = sqlite3.connect(GQ, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    row = c.execute(
        "SELECT result, score_home, score_away FROM match_outcomes "
        "WHERE home=? AND away=? AND result IS NOT NULL", (home, away)).fetchone()
    c.close()
    return row


def has_inplay(home, away):
    c = sqlite3.connect(GQ, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    n = c.execute(
        "SELECT COUNT(*) FROM odds_snapshots WHERE minute_at>0 AND match_key=?",
        (f"{home} vs {away}",)).fetchone()[0]
    c.close()
    return n > 0


def _total(score):
    """'2-0' -> 2; 解析失败返回 None。"""
    try:
        a, b = str(score).split("-", 1)
        return int(a.strip()) + int(b.strip())
    except Exception:
        return None


def settle_ou(total, line, model_said_over):
    """整数盘走盘处理。返回 (settle, over_hit, model_correct)。

    total == line (整数盘如 2.0/3.0) -> push 走盘: 不算大也不算小, 不计模型胜负。
    半盘/四分盘(.25/.5/.75) 不会命中 push 分支。
    """
    if abs(total - line) < 1e-9:
        return "push", None, None
    over_hit = total > line
    return ("over" if over_hit else "under"), bool(over_hit), bool(over_hit == model_said_over)


def live_evidence(home, away):
    """最新 live 证据: (最大快照总球, 快照比分, matches状态, matches比分, matches总球)

    IR-06: minute_at 45/90 是 GQ 的"上半场/下半场"阶段标记, 不是真实分钟,
    绝不可凭 minute_at==90 判终场。本函数只取"最后一个快照比分"和 matches 行做证据。
    """
    mk = f"{home} vs {away}"
    c = sqlite3.connect(GQ, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    snap = c.execute(
        "SELECT score_at FROM odds_snapshots WHERE match_key=? AND score_at<>'' "
        "ORDER BY captured_at DESC LIMIT 1", (mk,)).fetchone()
    m = c.execute(
        "SELECT status, score_home, score_away FROM matches WHERE match_key=?",
        (mk,)).fetchone()
    c.close()
    snap_score = snap[0] if snap else None
    snap_total = _total(snap_score) if snap_score else None
    m_status = m[0] if m else None
    m_score = None
    m_total = None
    if m and m[1] is not None and m[2] is not None:
        m_score = f"{m[1]}-{m[2]}"
        m_total = (m[1] or 0) + (m[2] or 0)
    best = max([t for t in (snap_total, m_total) if t is not None], default=None)
    return best, snap_score, m_status, m_score, m_total


def demote_premature_labels(recs):
    """闸门: 靠 live 快照"抢跑"定的终场标签, 若后续证据显示还有进球 -> 撤销标签。

    只对 verified=True + reconcile pending + judgement_source 含 snapshot 的记录生效。
    幂等: 已撤销(verified=False)的不再处理; 证据总球不大于已记标签时不写盘。
    """
    changed = False
    for r in recs:
        if not r.get("verified"):
            continue
        if not str(r.get("reconcile_with_match_outcomes", "")).startswith("pending"):
            continue
        src = str(r.get("judgement_source", "")).lower()
        if "snapshot" not in src and "快照" not in src:
            continue
        mk = r.get("match_key", "")
        if " vs " not in mk:
            continue
        home, away = mk.split(" vs ", 1)
        claimed = r.get("final_total")
        if claimed is None:
            claimed = _total(r.get("final_score") or r.get("final_result"))
        if claimed is None:
            continue
        best, snap_score, m_status, m_score, _m_total = live_evidence(home, away)
        if best is None or best <= claimed:
            continue
        # 证据显示后续还有进球 -> 之前的"终场"是抢跑
        archived = {k: r[k] for k in (
            "final_result", "final_score", "final_total", "total_goals",
            "over_hit", "over_hits", "under_wins", "model_correct",
            "model_final_direction", "user_view_correct", "ou_settle",
            "verify_note", "judgement_source") if k in r}
        for k in archived:
            r.pop(k, None)
        r["verified"] = False
        r["disputed_prior_label"] = archived
        r["label_demoted"] = (
            f"live快照抢跑定终场(claimed总球={claimed}), 但后续证据总球={best} "
            f"(最后快照比分={snap_score}, matches={m_score} status={m_status}); "
            f"IR-06: minute_at 45/90 是阶段标记非真实分钟, 不可判终场。"
            f"标签已撤销, 等 match_outcomes 落库后按 SSoT 重填。"
        )
        changed = True
        print(f"  [标签撤销] {mk}: {r['label_demoted']}")
    return changed


def main():
    recs = load_records()
    if not recs:
        print("[verify] ledger 为空, 无待核验记录")
        return
    # ---- 第零遍: 撤销 live 快照抢跑定的终场标签(防训练标签污染) ----
    changed = demote_premature_labels(recs)
    pending = [r for r in recs if not r.get("verified")]
    print(f"[verify] ledger 共 {len(recs)} 条, 待核验 {len(pending)} 条")
    for r in pending:
        mk = r.get("match_key", "")
        if " vs " not in mk:
            continue
        home, away = mk.split(" vs ", 1)
        row = lookup_result(home, away)
        if not row:
            # 终场未落库, 继续等
            print(f"  [等待] {mk}: match_outcomes 尚无 result")
            continue
        result, sh, sa = row
        total = (sh or 0) + (sa or 0)
        line = float(r.get("ou_line", 0))
        model_said_over = (r.get("model_direction") == "over")
        settle, over_hit, model_correct = settle_ou(total, line, model_said_over)
        inplay = has_inplay(home, away)
        r["verified"] = True
        r["final_result"] = result
        r["final_score"] = f"{sh}-{sa}"
        r["final_total"] = total
        r["ou_settle"] = settle            # over / under / push(走盘)
        r["over_hit"] = over_hit           # push 时为 None
        r["model_correct"] = model_correct  # push 时为 None(不计入胜负)
        r["next_retrain_will_ingest"] = bool(inplay)
        if settle == "push":
            verdict = "走盘(总球=盘口, 不计胜负)"
        else:
            verdict = ("大球打出" if settle == "over" else "大球未打出") + \
                      f"; 模型看{'大' if model_said_over else '小'} -> " + \
                      ("判对" if model_correct else "判错")
        r["verify_note"] = (
            f"终场 {sh}-{sa}(总{total}) vs 盘口{line}; {verdict}; "
            f"下轮retrain{'自动纳入' if inplay else '缺in-play快照不纳入'}"
        )
        changed = True
        print(f"  [已核验] {mk}: {r['verify_note']}")
    # ---- 第二遍: 已 verified(靠 live 快照定的真相) 但 match_outcomes 尚未对账的记录 ----
    # 只补 reconcile_* 字段, 绝不覆盖已有 model_correct/final_* 标签(避免污染训练标签)
    recon = [r for r in recs
             if r.get("verified")
             and str(r.get("reconcile_with_match_outcomes", "")).startswith("pending")]
    if recon:
        print(f"[verify] 待与 match_outcomes 对账 {len(recon)} 条")
    for r in recon:
        mk = r.get("match_key", "")
        if " vs " not in mk:
            continue
        home, away = mk.split(" vs ", 1)
        row = lookup_result(home, away)
        inplay = has_inplay(home, away)
        if not row:
            # 标签行仍未落库 -> retrain 拿不到标签, 无论 in-play 快照多少都不会纳入。
            # 不写任何字段: 保持幂等, 避免每小时空跑都重写 ledger。
            print(f"  [对账等待] {mk}: match_outcomes 无行 (in-play快照={'有' if inplay else '无'}) "
                  f"-> 缺标签, 下轮 retrain 不纳入")
            continue
        result, sh, sa = row
        total = (sh or 0) + (sa or 0)
        line = float(r.get("ou_line", 0))
        model_said_over = (r.get("model_direction") == "over")
        db_settle, over_hit, db_correct = settle_ou(total, line, model_said_over)
        prior_score = r.get("final_score") or r.get("final_result")
        db_score = f"{sh}-{sa}"
        agree = (str(prior_score) == db_score)
        r["reconcile_with_match_outcomes"] = "done"
        r["reconcile_db_score"] = db_score
        r["reconcile_db_result"] = result
        r["reconcile_db_total"] = total
        r["reconcile_db_settle"] = db_settle
        r["reconcile_db_over_hit"] = over_hit
        r["reconcile_db_model_correct"] = db_correct
        r["reconcile_agrees_with_live"] = bool(agree)
        r["reconcile_inplay_snapshots"] = bool(inplay)
        r["next_retrain_will_ingest"] = bool(inplay)
        if not agree:
            r["reconcile_conflict"] = (
                f"live快照判定 {prior_score} 与 match_outcomes {db_score} 不一致, "
                f"以 match_outcomes 为训练口径, 人工复核"
            )
            print(f"  [对账冲突] {mk}: live={prior_score} vs DB={db_score} -> 需人工复核")
        else:
            print(f"  [对账完成] {mk}: DB {db_score}(总{total}) 与 live 一致; "
                  f"下轮retrain{'自动纳入' if inplay else '缺in-play快照不纳入'}")
        changed = True

    if changed:
        save_records(recs)
        print("[verify] 已写回 ledger")
    else:
        print("[verify] 本轮无更新 (比赛尚未终场落库)")


if __name__ == "__main__":
    main()
