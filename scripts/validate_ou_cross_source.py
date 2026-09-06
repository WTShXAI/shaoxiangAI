"""
scripts/validate_ou_cross_source.py — 用 football-data.org 独立赛果交叉验证 OU 派生线 +5pp 结论

两路验证(纯 stdlib + sqlite3, 不涉及任何 key):
  A) 独立基线: 从 fd_matches 独立赛果算 P(total>L) per std line (non-push 样本),
     对比本地 ou_validation_local 内置基线 -> 验证基线非本地库假象.
  B) 独立样本 edge 复现: fd_matches(英文队名) 经 team_canonical 别名归一 ->
     匹配本地 match_features(odds_close_h/d/a) -> Poisson 反推 E[total] ->
     对 fd_matches 实际总进球结算 -> 重算中段线 edge, 对比本地 +5.04pp.

依赖: scripts/build_ou_validation_local.py 的 Poisson 查表(import 复用, 无副作用).
"""
import os
import re
import json
import sqlite3
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "football_data.db")
OUT = os.path.join(ROOT, "data", "ou_cross_source_report.json")
LOCAL_REPORT = os.path.join(ROOT, "data", "ou_local_validation_report.json")
STD_LINES = [2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5]


def load_bovl():
    spec = importlib.util.spec_from_file_location(
        "bovl", os.path.join(ROOT, "scripts", "build_ou_validation_local.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.build_lookup(), m


def norm_name(s):
    if not s:
        return ""
    s = s.lower().strip()
    s = s.replace(".", " ").replace("-", " ").replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(fc|afc|sc|ac|cf|ssc|ud|rk|fk|sk|tc|as|rc|caf|sp)\s+", "", s)
    s = re.sub(r"\s+(fc|afc|sc|ac|cf|ssc|ud|rk|fk|sk|tc|as|rc|caf|sp)\s*$", "", s)
    return s


def build_canon_index(con):
    """norm_alias -> canonical_key(中英文均可, 取 team_canonical 首别名)."""
    idx = {}
    for canon_key, val, _ in con.execute(
            "SELECT canonical, aliases_json, note FROM team_canonical"):
        try:
            aliases = json.loads(val)
        except Exception:
            aliases = [val]
        if not aliases:
            aliases = [canon_key]
        for a in aliases:
            n = norm_name(a)
            if n and n not in idx:
                idx[n] = norm_name(canon_key) or n
    return idx


def main():
    bovl_tbl, bovl = load_bovl()
    TABLE, FLAT = bovl_tbl
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # ---- canon index: norm_name -> canonical norm key ----
    idx = build_canon_index(con)

    # ---- local matches: (home_canon, away_canon, date) -> (ph,pd,pa) ----
    local = {}
    for r in con.execute("""
        SELECT m.match_date, m.home_team_name, m.away_team_name,
               f.odds_close_h, f.odds_close_d, f.odds_close_a
        FROM matches m JOIN match_features f ON f.match_id = m.match_id
        WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
          AND f.odds_close_h > 1.0 AND f.odds_close_d > 1.0 AND f.odds_close_a > 1.0
    """):
        hc = idx.get(norm_name(r["home_team_name"]))
        ac = idx.get(norm_name(r["away_team_name"]))
        if not hc or not ac:
            continue
        d = r["match_date"][:10]
        if not d:
            continue
        local[(hc, ac, d)] = (float(r["odds_close_h"]),
                              float(r["odds_close_d"]),
                              float(r["odds_close_a"]))
    print(f"[local] 可匹配本地比赛(含canon别名) = {len(local)}")

    # ---- fd_matches ----
    fd_rows = list(con.execute(
        "SELECT competition_code, home_team, away_team, utc_date, "
        "home_score, away_score FROM fd_matches"))
    print(f"[fd] fd_matches 总行 = {len(fd_rows)}")

    # ===== A) 独立基线: fd 总进球分布 =====
    base = {L: [0, 0, 0] for L in STD_LINES}  # over, under, push
    for r in fd_rows:
        tg = int(r["home_score"]) + int(r["away_score"])
        for L in STD_LINES:
            if tg > L:
                base[L][0] += 1
            elif tg < L:
                base[L][1] += 1
            else:
                base[L][2] += 1
    base_over = {L: (base[L][0] / (base[L][0] + base[L][1])
                     if (base[L][0] + base[L][1]) else None)
                 for L in STD_LINES}

    # ===== B) 匹配本地 odds -> 复现 edge =====
    model = {L: [0, 0] for L in STD_LINES}   # correct, n (non-push, model有方向)
    baseB = {L: [0, 0] for L in STD_LINES}   # over, nonpush (同线基线)
    matched = 0
    for r in fd_rows:
        hc = idx.get(norm_name(r["home_team"]))
        ac = idx.get(norm_name(r["away_team"]))
        if not hc or not ac:
            continue
        d = (r["utc_date"] or "")[:10]
        if not d:
            continue
        # 日期 ±1 天容忍时区
        cand = None
        for dd in (d,):
            if (hc, ac, dd) in local:
                cand = local[(hc, ac, dd)]
                break
        if cand is None:
            import datetime as _dt
            try:
                dt = _dt.date.fromisoformat(d)
                for off in (-1, 1):
                    dd = (dt + _dt.timedelta(days=off)).isoformat()
                    if (hc, ac, dd) in local:
                        cand = local[(hc, ac, dd)]
                        break
            except Exception:
                pass
        if cand is None:
            continue
        matched += 1
        ph, pd, pa = cand
        lh, la = bovl.lookup(TABLE, FLAT, ph, pd)
        e_total = lh + la
        tg = int(r["home_score"]) + int(r["away_score"])
        for L in STD_LINES:
            if tg == L:  # push, 不计基线也不计模型
                continue
            actual_over = (tg > L)
            baseB[L][0] += (1 if actual_over else 0)
            baseB[L][1] += 1
            if e_total == L:
                continue  # 模型无方向, 不下注
            model_over = (e_total > L)
            model[L][0] += (1 if model_over == actual_over else 0)
            model[L][1] += 1

    edge = {}
    for L in STD_LINES:
        macc = (model[L][0] / model[L][1]) if model[L][1] else None
        bacc = (baseB[L][0] / baseB[L][1]) if baseB[L][1] else None
        edge[L] = {
            "model_acc": macc,
            "baseline_over": bacc,
            "edge_pp": (round((macc - bacc) * 100, 2) if (macc is not None and bacc is not None) else None),
            "model_n": model[L][1],
            "base_n": baseB[L][1],
        }

    # ---- 本地报告基线对比 ----
    local_base = {}
    if os.path.exists(LOCAL_REPORT):
        rep = json.load(open(LOCAL_REPORT, encoding="utf-8"))
        if isinstance(rep, dict) and "per_line" in rep:
            for k, it in rep["per_line"].items():
                if isinstance(it, dict):
                    local_base[str(k)] = it

    mid_lines = [2.25, 2.5, 2.75]
    mid_edge = [edge[L]["edge_pp"] for L in mid_lines if edge[L]["edge_pp"] is not None]
    summary = {
        "fd_total_matches": len(fd_rows),
        "local_matchable": len(local),
        "matched_games": matched,
        "match_rate_pct": round(100.0 * matched / len(fd_rows), 2) if fd_rows else 0,
        "A_independent_baseline_over_freq": {str(L): (round(base_over[L], 4) if base_over[L] else None) for L in STD_LINES},
        "B_edge_per_line_pp": {str(L): edge[L] for L in STD_LINES},
        "B_midline_avg_edge_pp": round(sum(mid_edge) / len(mid_edge), 2) if mid_edge else None,
        "B_confidence": ("LOW (matched sample too small / biased -> inconclusive)"
                         if matched < 500 else "OK"),
        "local_baseline_for_compare": {str(L): local_base.get(str(L), {}).get("always_over_pct") for L in STD_LINES},
        "local_edge_for_compare": {str(L): local_base.get(str(L), {}).get("delta_vs_majority") for L in STD_LINES},
        "note": "派生OU线(1X2反推)非真实市场OU盘口; B路用本地odds+独立fd赛果复现edge, 验非本地库假象.",
    }
    json.dump(summary, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # ---- 打印 ----
    print("\n===== A) 独立基线 P(total>L) [fd_matches] =====")
    for L in STD_LINES:
        b = base_over[L]
        lb = local_base.get(str(L), {}).get("always_over_pct")
        print(f"  L={L}: fd={None if b is None else round(b,4)}  local={lb}")
    print("\n===== B) 匹配本地odds -> edge 复现 [fd赛果] =====")
    for L in STD_LINES:
        e = edge[L]
        le = local_base.get(L, {}).get("edge_pp")
        print(f"  L={L}: model_acc={e['model_acc']} base_over={e['baseline_over']} "
              f"edge={e['edge_pp']}pp (n={e['model_n']}) | local_edge={le}")
    print(f"\nmatched_games={matched} ({summary['match_rate_pct']}%)  "
          f"midline_avg_edge={summary['B_midline_avg_edge_pp']}pp")
    print(f"-> {OUT}")
    con.close()


if __name__ == "__main__":
    main()
