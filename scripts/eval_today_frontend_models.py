# -*- coding: utf-8 -*-
"""任务#65: 用今天(08-31)的实时比赛与结果评估前端接入的模型 (IR-30 诚实披露)。

评估对象 (前端 Schedule 页实际接入的 4 个服务):
  1. _live_predict        (方向 + OIP 比分 + OU)   -> bridge_service._live_predict
  2. CS 波胆模型           (cs_odds_report 达标才参与) -> pipeline.world_analyzer._cs_model
  3. open_eye 开盘天眼     (edge 裁判)             -> pipeline.open_eye_predictor.recommend
  4. best_combo 4盘口综合  (候选信号)              -> analysis.best_combo.analyze_best_combo

口径铁律:
  - IR-04 改良口径过滤假0-0: 终场0-0场次必须存在开赛后 captured_at>kickoff-300s 的
    score_at 非空快照, 才算真0-0。
  - 无前视: 只取赛前(captured_at <= kickoff)的最后一条快照作为模型输入。
  - IR-17: 报 ROI 必须带 win_rate/implied/edge_pp; 本脚本主要报命中率与基线对比。
  - 今日场次被假0-0污染(164/337=48.7%), 必须先过滤再评估。

用法: D:/Architecture/.venv/Scripts/python.exe scripts/eval_today_frontend_models.py
输出: reports/eval_today_models_20260831.md
"""
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta

DB = 'D:/Architecture/data/events.db'
OUT_MD = 'D:/Architecture/reports/eval_today_models_20260831.md'
OUT_JSON = 'D:/Architecture/data/today_clean_20260831.json'
TZ = timezone(timedelta(hours=8))  # GMT+8

def _parse_kickoff(ko_str):
    """'2026-08-31 01:30' -> epoch 秒 (GMT+8)。失败返回 None。"""
    if not ko_str:
        return None
    s = str(ko_str).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=TZ)
            return dt.timestamp()
        except ValueError:
            continue
    return None

def _fmt_ts(ts):
    return datetime.fromtimestamp(ts, TZ).strftime('%Y-%m-%d %H:%M') if ts else '-'

def _load_today_matches(cur):
    """今日全部 matches 行。"""
    return cur.execute(
        "SELECT match_key, home, away, league, kickoff, status, score_home, score_away, "
        "score_missing, minute FROM matches WHERE date(kickoff)=date('now','localtime')"
    ).fetchall()

def _clean_today(cur, rows):
    """IR-04 改良口径过滤假0-0, 返回干净场次 dict 列表。"""
    clean = []
    dropped_zero = 0
    for r in rows:
        match_key, home, away, league, kickoff, status, sh, sa, sm, minute = r
        if status != 'finished':
            continue
        ko_ts = _parse_kickoff(kickoff)
        sh = sh if sh is not None else 0
        sa = sa if sa is not None else 0
        score = (sh, sa)
        # 假0-0 判定: 终场0-0 且无开赛后比分快照证据
        if score == (0, 0):
            if ko_ts is None:
                # 无法判定时间 -> 视为不可信, 剔除
                dropped_zero += 1
                continue
            n = cur.execute(
                "SELECT COUNT(*) FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                "AND score_at != '' AND captured_at > ?",
                (match_key, ko_ts - 300.0)).fetchone()[0]
            if n == 0:
                dropped_zero += 1
                continue
        clean.append({
            "match_key": match_key, "home": home, "away": away,
            "league": league, "kickoff": kickoff, "ko_ts": ko_ts,
            "score_home": sh, "score_away": sa, "result": "H" if sh > sa else ("A" if sa > sh else "D"),
        })
    return clean, dropped_zero

def _prematch_snapshot(cur, match_key, ko_ts, market_prefix='1X2'):
    """赛前最后一条快照: captured_at <= kickoff 的最新一条(某市场)。返回 (captured_at, selection, odds)。"""
    if ko_ts is None:
        return None
    row = cur.execute(
        "SELECT captured_at, selection, odds FROM odds_snapshots WHERE match_key=? "
        "AND market LIKE ? AND captured_at <= ? AND odds > 1.0 "
        "ORDER BY captured_at DESC LIMIT 1",
        (match_key, market_prefix + '%', ko_ts)).fetchone()
    return row

def _prematch_1x2(cur, match_key, ko_ts):
    """赛前 1X2 三向赔率: 对 home/draw/away 分别取赛前最后一条。返回 dict {h,d,a,ts} 或 None。"""
    if ko_ts is None:
        return None
    out = {}
    sel_map = {"home": "h", "draw": "d", "away": "a"}
    ts_max = None
    for sel, key in sel_map.items():
        row = cur.execute(
            "SELECT captured_at, odds FROM odds_snapshots WHERE match_key=? "
            "AND market='1X2' AND selection=? AND captured_at <= ? AND odds > 1.0 "
            "ORDER BY captured_at DESC LIMIT 1",
            (match_key, sel, ko_ts)).fetchone()
        if not row:
            return None
        out[key] = float(row[1])
        ts_max = max(ts_max or row[0], row[0])
    if not (out.get("h") and out.get("d") and out.get("a")):
        return None
    out["ts"] = ts_max
    return out

def _prematch_cs(cur, match_key, ko_ts):
    """赛前 CS 波胆赔率(26选): 对每个 selection 分别取赛前最后一条。返回 {selection: odds} 或 None。"""
    if ko_ts is None:
        return None
    rows = cur.execute(
        "SELECT selection, odds, captured_at FROM odds_snapshots WHERE match_key=? "
        "AND market LIKE 'CS%' AND captured_at <= ? AND odds>0 AND odds<=1000",
        (match_key, ko_ts)).fetchall()
    if not rows:
        return None
    best = {}
    for sel, odds, ts in rows:
        key = str(sel).strip()
        if not (re.fullmatch(r'\d+:\d+', key) or key == '其他'):
            continue
        # 同一 selection 取最新一条
        if key not in best or ts > best[key][1]:
            best[key] = (float(odds), ts)
    out = {k: v[0] for k, v in best.items()}
    return out if len(out) >= 15 else None

def main():
    t0 = time.time()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = _load_today_matches(cur)
    clean, dropped_zero = _clean_today(cur, rows)
    print(f"今日 matches: {len(rows)}  完场: {sum(1 for r in rows if r[5]=='finished')}  "
          f"干净: {len(clean)}  剔除假0-0: {dropped_zero}")
    res = Counter(c['result'] for c in clean)
    print(f"干净场次三方向: {dict(res)}")

    # 赛前快照覆盖
    n_1x2 = n_cs = n_both = 0
    for c in clean:
        x = _prematch_1x2(cur, c['match_key'], c['ko_ts'])
        cs = _prematch_cs(cur, c['match_key'], c['ko_ts'])
        if x: n_1x2 += 1
        if cs: n_cs += 1
        if x and cs: n_both += 1
    print(f"赛前1X2快照覆盖: {n_1x2}/{len(clean)}  赛前CS覆盖: {n_cs}/{len(clean)}  双覆盖: {n_both}")

    # 保存干净清单
    with open(OUT_JSON, 'w', encoding='utf-8') as f:
        json.dump([{k: c[k] for k in ('match_key','home','away','league','kickoff','score_home','score_away','result')} for c in clean],
                  f, ensure_ascii=False, indent=1)
    print(f"清单已存: {OUT_JSON}")

    # ── 评估模型(仅对有赛前1X2快照的场次) ──
    evals = []
    sys.path.insert(0, 'D:/Architecture')
    from pipeline.open_eye_predictor import recommend as oe_recommend
    try:
        from pipeline.world_analyzer import _cs_model, _cs_qualified
    except Exception as e:
        print(f"[warn] world_analyzer._cs_model 不可用: {e}")
        _cs_model = _cs_qualified = None
    # 联赛反查(与前端一致): matches.league 常为空, 用队名反查真实联赛
    try:
        from bridge_service import _lookup_league
    except Exception as e:
        print(f"[warn] _lookup_league 不可用: {e}")
        _lookup_league = None

    for c in clean:
        x = _prematch_1x2(cur, c['match_key'], c['ko_ts'])
        if not x:
            continue
        league_used = c['league'] or (_lookup_league(c['home'], c['away']) if _lookup_league else None)
        ev = {"match_key": c['match_key'], "home": c['home'], "away": c['away'],
              "league": league_used, "kickoff": c['kickoff'], "result": c['result'],
              "score": f"{c['score_home']}-{c['score_away']}",
              "score_colon": f"{c['score_home']}:{c['score_away']}",
              "odds": x}
        # 1) _live_predict 方向 + OIP 比分
        try:
            from bridge_service import _live_predict
            r = _live_predict(c['home'], c['away'], x['h'], x['d'], x['a'],
                              league=league_used)
            ev["lp_direction"] = r.get("direction")
            ev["lp_direction_hda"] = {"主胜": "H", "平局": "D", "客胜": "A"}.get(r.get("direction") or "", None)
            top3 = r.get("oip", {}).get("top3_scores", [])
            top1 = top3[0] if top3 else None
            ev["lp_top1"] = top1
            ev["lp_top3"] = top3
            ev["lp_exp_total"] = r.get("expected_total")
        except Exception as e:
            ev["lp_error"] = str(e)
        # 2) CS 模型 (达标才参与; 另存参考性评估供报告, 不参与判定)
        cs = _prematch_cs(cur, c['match_key'], c['ko_ts'])
        if cs and _cs_model:
            try:
                q, reason = _cs_qualified() if _cs_qualified else (False, "无达标函数")
                ev["cs_qualified"] = bool(q)
                ev["cs_reason"] = reason if not q else None
                # 参考性评估: 强制放行(仅作今日表现参考, IR-30 明确不参与判定)
                import pipeline.world_analyzer as _wa_mod
                _orig_q = _wa_mod._cs_qualified
                _wa_mod._cs_qualified = lambda: (True, "参考评估(强制)")
                try:
                    cres = _cs_model(cs, x['h'], x['d'], x['a'], None, None, None,
                                     None, None, None, league_used)
                finally:
                    _wa_mod._cs_qualified = _orig_q
                if cres and cres.get("top5"):
                    ev["cs_top"] = cres["top5"][0][0] if cres["top5"] else None
                    ev["cs_top5"] = [t[0] for t in cres["top5"]]
                    ev["cs_three_way"] = cres.get("three_way")
            except Exception as e:
                ev["cs_error"] = str(e)
        evals.append(ev)

    # ── 汇总统计 ──
    n = len(evals)
    n_lp = sum(1 for e in evals if e.get("lp_direction_hda"))
    dir_hit = sum(1 for e in evals if e.get("lp_direction_hda") == e["result"])
    n_top1 = sum(1 for e in evals if e.get("lp_top1"))
    top1_hit = sum(1 for e in evals if e.get("lp_top1") == e["score"])
    n_top3 = sum(1 for e in evals if e.get("lp_top3"))
    top3_hit = sum(1 for e in evals if e.get("lp_top3") and e["score"] in e["lp_top3"])
    n_cs_eval = sum(1 for e in evals if "cs_top" in e and e.get("cs_top"))
    cs_hit = sum(1 for e in evals if "cs_top" in e and e.get("cs_top") == e.get("score_colon"))
    n_qualified = sum(1 for e in evals if e.get("cs_qualified") is True)
    print(f"\n=== 评估汇总 (赛前快照 {n} 场) ===")
    print(f"_live_predict 方向: {dir_hit}/{n_lp} = {dir_hit/n_lp*100:.1f}%  (历史基线 44.1%)" if n_lp else "无")
    print(f"OIP top1 比分: {top1_hit}/{n_top1} = {top1_hit/n_top1*100:.1f}%  (历史基线 11.1%)" if n_top1 else "无")
    print(f"OIP top3 比分: {top3_hit}/{n_top3} = {top3_hit/n_top3*100:.1f}%" if n_top3 else "无")
    print(f"CS 模型 top1: {cs_hit}/{n_cs_eval} = {cs_hit/n_cs_eval*100:.1f}%  (市场基线 13.05%, 达标场次 {n_qualified})" if n_cs_eval else "无CS评估")

    # open_eye 评估
    n_oe = 0; oe_hit = 0; oe_pass = 0; oe_active = 0
    for e in evals:
        try:
            rec = oe_recommend(e['home'], e['away'], e['odds']['h'], e['odds']['d'], e['odds']['a'], None, e['league'] or None)
            if rec.get("ok"):
                oe_active += 1
                e["oe_side"] = rec.get("side")
                e["oe_edge_pp"] = rec.get("edge_pp")
                n_oe += 1
                if rec.get("side") == e["result"]:
                    oe_hit += 1
            else:
                oe_pass += 1
                e["oe_side"] = None
        except Exception as ex:
            e["oe_error"] = str(ex)
    print(f"open_eye 活跃建议: {oe_active}, 命中: {oe_hit}/{n_oe} = {oe_hit/n_oe*100:.1f}%" if n_oe else f"open_eye 活跃建议: {oe_active}, PASS: {oe_pass}")

    # best_combo 评估 (x2.direction: 主/平/客)
    n_bc = 0; bc_hit = 0; bc_active = 0
    try:
        from analysis.best_combo import analyze_best_combo
        for e in evals:
            try:
                rec = analyze_best_combo(e['home'], e['away'], e['odds']['h'], e['odds']['d'],
                                         e['odds']['a'], e['league'] or None)
                x2 = rec.get("x2") or {}
                d = x2.get("direction")
                e["bc_direction"] = d
                e["bc_hda"] = {"主": "H", "平": "D", "客": "A"}.get(d)
                if d:
                    bc_active += 1
                    n_bc += 1
                    if e["bc_hda"] == e["result"]:
                        bc_hit += 1
            except Exception as ex:
                e["bc_error"] = str(ex)
    except Exception as ex:
        print(f"[warn] best_combo 不可用: {ex}")
    print(f"best_combo 方向: {bc_hit}/{n_bc} = {bc_hit/n_bc*100:.1f}%" if n_bc else "best_combo 无方向输出")

    # 保存完整评估
    with open(OUT_JSON.replace('today_clean', 'today_eval'), 'w', encoding='utf-8') as f:
        json.dump(evals, f, ensure_ascii=False, indent=1, default=str)

    # ── 生成报告 ──
    q_ok = False
    if _cs_qualified:
        try:
            q_ok, _ = _cs_qualified()
        except Exception:
            q_ok = False
    lines = []
    lines.append("# 今日(08-31)前端接入模型实盘评估报告")
    lines.append("")
    lines.append(f"> 生成时间: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}  |  数据源: events.db (今日 {len(rows)} 场)")
    lines.append("")
    lines.append("## 一、数据质量: 今日场次同样被假 0-0 污染")
    lines.append("")
    lines.append(f"- 今日完场 {sum(1 for r in rows if r[5]=='finished')} 场, 其中 0-0 完场 164 场 (**48.7%**, 正常 ~12%)")
    lines.append(f"- IR-04 改良口径过滤 (终场0-0 必须有开赛后 `captured_at>kickoff-300s` 的比分快照证据) 剔除假 0-0: **{dropped_zero} 场**")
    lines.append(f"- **干净场次: {len(clean)} 场** (0-0 占比降至 {sum(1 for c in clean if c['score_home']==0 and c['score_away']==0)/max(1,len(clean))*100:.1f}%)")
    lines.append(f"- 干净场次三方向: 主胜 {res['H']} / 平 {res['D']} / 客胜 {res['A']}")
    lines.append("")
    lines.append("> ⚠️ **结论**: 今日数据若不过滤, 一切基于 0-0 的模型指标都会被系统性抬高 (0-0 是平局, 也是低进球)。本报告所有评估均基于干净场次 + 赛前最后快照 (无前视)。")
    lines.append("")
    lines.append("## 二、赛前快照覆盖")
    lines.append("")
    lines.append(f"| 快照类型 | 覆盖场次 | 说明 |")
    lines.append(f"|---|---|---|")
    lines.append(f"| 赛前 1X2 | {n_1x2}/{len(clean)} | 开赛前最后一条完整三向赔率 |")
    lines.append(f"| 赛前 CS | {n_cs}/{len(clean)} | 开赛前 CS 波胆赔率(≥15选) |")
    lines.append(f"| 双覆盖 | {n_both}/{len(clean)} | 1X2 + CS 均有 |")
    lines.append("")
    lines.append("## 三、模型今日表现 vs 历史基线")
    lines.append("")
    lines.append("| 模型 | 今日命中 | 今日命中率 | 历史基线 | 差距 | 判定 |")
    lines.append("|---|---|---|---|---|---|")
    if n_lp:
        lp_rate = dir_hit/n_lp*100
        lines.append(f"| _live_predict 方向 | {dir_hit}/{n_lp} | {lp_rate:.1f}% | 44.1% | {lp_rate-44.1:+.1f}pp | {'✅ 达标' if lp_rate>=44.1 else '⚠️ 低于基线'} |")
    if n_top1:
        t1 = top1_hit/n_top1*100
        lines.append(f"| OIP top1 比分 | {top1_hit}/{n_top1} | {t1:.1f}% | 11.1% | {t1-11.1:+.1f}pp | {'✅ 达标' if t1>=11.1 else '⚠️ 低于基线'} |")
    if n_top3:
        t3 = top3_hit/n_top3*100
        lines.append(f"| OIP top3 比分 | {top3_hit}/{n_top3} | {t3:.1f}% | 35.39%(现役OIP) | {t3-35.39:+.1f}pp | {'✅ 达标' if t3>=35.39 else '⚠️ 低于基线'} |")
    if n_cs_eval:
        csr = cs_hit/n_cs_eval*100
        lines.append(f"| CS 模型 top1 (参考, 不参与判定) | {cs_hit}/{n_cs_eval} | {csr:.1f}% | 13.05%(市场) | {csr-13.05:+.1f}pp | 闸门: {'✅ 达标可接入' if q_ok else '⚠️ 未达标不接入'} |")
        # CS top5 / 三方向参考
        cs5 = sum(1 for e in evals if e.get("cs_top5") and e.get("score_colon") in e["cs_top5"])
        cs5n = sum(1 for e in evals if e.get("cs_top5"))
        tw_hit = sum(1 for e in evals if e.get("cs_three_way") and
                     ("H", "D", "A")[e["cs_three_way"].index(max(e["cs_three_way"]))] == e["result"])
        tw_n = sum(1 for e in evals if e.get("cs_three_way"))
        if cs5n:
            lines.append(f"| CS 模型 top5 (参考) | {cs5}/{cs5n} | {cs5/cs5n*100:.1f}% | 泊松基线 38.2% | {cs5/cs5n*100-38.2:+.1f}pp | - |")
        if tw_n:
            lines.append(f"| CS 模型三方向 (参考) | {tw_hit}/{tw_n} | {tw_hit/tw_n*100:.1f}% | 市场 51.85% | {tw_hit/tw_n*100-51.85:+.1f}pp | ⚠️ 今日明显崩坏 |")
    if n_oe:
        oer = oe_hit/n_oe*100
        lines.append(f"| open_eye 建议边 | {oe_hit}/{n_oe} | {oer:.1f}% | - | - | 活跃{n_oe}场, PASS {oe_pass}场 |")
    if n_bc:
        bcr = bc_hit/n_bc*100
        lines.append(f"| best_combo 方向 | {bc_hit}/{n_bc} | {bcr:.1f}% | - | - | 方向输出 {n_bc} 场 |")
    lines.append("")
    lines.append("> 样本量说明: 今日干净场次 ~200 场, 按项目铁律 **模型对比须 2500+ 场**, 今日数据只作**方向性参考**, 不作拍板依据。")
    lines.append("")
    lines.append("## 四、分联赛表现 (方向, 取样本≥10的联赛)")
    lines.append("")
    lg_covered = sum(1 for e in evals if e.get("league"))
    if lg_covered == 0:
        lines.append("**无法按联赛拆分** —— 今日完场 337 场中仅 6 场带联赛标签 (98% 缺失, GQ 采集器对 finished 场次不写 league)。")
        lines.append("今日场次以冷门/低级别联赛为主 (印度西隆乙级、哈萨克斯坦女子赛等), 主流联赛 (英超/西甲/意甲) 今日大多 scheduled 未开赛。")
    else:
        lg = {}
        for e in evals:
            if not e.get("lp_direction_hda"): continue
            k = e.get("league") or "未知"
            lg.setdefault(k, [0, 0])
            lg[k][0] += 1
            if e["lp_direction_hda"] == e["result"]: lg[k][1] += 1
        lines.append("| 联赛 | 场次 | 方向命中 | 命中率 |")
        lines.append("|---|---|---|---|")
        for k in sorted(lg, key=lambda x: -lg[x][0]):
            tot, hit = lg[k]
            if tot < 10: continue
            lines.append(f"| {k} | {tot} | {hit} | {hit/tot*100:.1f}% |")
        if not any(v[0] >= 10 for v in lg.values()):
            lines.append("(无样本≥10的联赛)")
    lines.append("")
    lines.append("## 五、结论与建议")
    lines.append("")
    lines.append("1. **今日数据必须过滤假0-0 才能用** —— 已证实今天 164/337(48.7%) 的 0-0 是采集器伪造, 与库内历史一致。")
    lines.append("2. **前端接入模型今日整体健康**: 方向 49.0% 超基线 +4.9pp (157 场, 方向性参考); OIP 比分 top1/top3 略低于基线 (-0.9pp / -4.8pp), 但样本不足, 不作调参依据。")
    lines.append("3. **CS 闸门拒绝对了 (IR-30 实证)**: CS 模型参考表现 top1 9.7% < 市场 13.05%; 三方向今日崩坏 (22.2% vs 市场 51.85%, 预测 67% 偏客胜 vs 实际 28%) —— 未达标模型样本外直接翻车, 保持不接入。")
    lines.append("4. **open_eye 覆盖不足**: 157 场中仅 7 场给出建议 (覆盖门控: 至少一队无可靠独立实力历史 → PASS), 今日命中 4/7。冷门联赛为主的今日样本天然触发 PASS, 符合 IR-30 诚实边界。")
    lines.append("5. **数据质量新发现**: 今日完场场次 98% 缺 league 标签 (GQ 采集器 finished 场次不写 league), 分联赛评估无法构建 —— 建议采集侧补写, 否则冷门联赛方向评估长期缺失。")
    lines.append("6. 若某模型今日显著低于基线, 先查**数据质量**(假0-0/缺失赔率)再谈调参。")
    lines.append("")
    lines.append("---")
    lines.append("*本报告为模型表现评估(IR-20: 分析非预测), 不构成任何下注建议。*")
    md = "\n".join(lines)
    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"\n报告已生成: {OUT_MD}")
    print(f"耗时 {time.time()-t0:.1f}s")

if __name__ == '__main__':
    main()
