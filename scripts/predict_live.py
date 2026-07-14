# -*- coding: utf-8 -*-
"""
predict_live.py — 真实比赛实时预测编排器 (v6.0 锁定架构)
========================================================
把"真实赔率 → 全链路预测"固化成单一入口。三种真实来源都能喂:
  ① 手动/CLI 真实赔率        : --home 德国 --away 阿根廷 --oh 2.35 --od 3.23 --oa 3.30
  ② 库内 football-data.co.uk 真实赔率回放 : --from-db 2026 --limit 8  (验证模式, 显示真实赛果)
  ③ 你平时贴/截的 OCR 赔率图 : --from-json odds_db/xxx.json

锁定架构链路 (哨响最终裁决, 2026-07-08):
  1. 杜博弈 deoverround  -> 市场隐含概率 + overround(抽水)校验
  2. 市场 argmax = 1X2 方向  (ENABLE_ML_MARKET_OVERRIDE 默认 OFF = 生产默认)
  3. 季泊松 OIP predict_score -> λ_h/λ_a, top3 比分, O/U 概率(Over2.5/1.5/3.5)
  4. 曾均衡/D-Gate: market_draw_prob(od) -> 平局预警(>26%触发)
  5. 荣合众/共识: 若 WH×IW 有这两队(五大联赛) -> consensus_draw_signal; WC无IW -> 回退市场P平
  6. 风控护栏: overround 异常(>12%高抽水警示) / 1X2 恢复一致性

实时 API 拉取 (--live): 经 sp_odds_api 拉取真实在跑比赛赔率并预测。
  ⚠️ 当前 The Odds API key 已过期(401), oddpapi 无 key -> --live 会优雅报错并提示如何修复,
     不编造数据。一旦提供可用 key/源, 同一条链路即对真实未开赛比赛出预测。
"""
from __future__ import annotations
import argparse, json, sys, sqlite3
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "D:/Architecture")
import numpy as np
from pipeline.score_model import predict_score, deoverround
from pipeline.draw_signal import market_draw_prob, consensus_draw_signal

DB = "D:/Architecture/data/football_data.db"
DRAW_ALERT = 0.26          # 平局预警阈值 (记忆: draw_alert > 26% 触发)
HIGH_VIG = 0.12           # overround > 12% 视为高抽水警示
DIRECTION = ["主胜", "平局", "客胜"]


def predict_match(home, away, oh, od, oa,
                  home_norm=None, away_norm=None,
                  date=None, league=None, verbose=True):
    """核心: 真实1X2赔率 -> 全链路预测。返回结构化 dict。"""
    oh = float(oh); od = float(od); oa = float(oa)
    ph, pd, pa = deoverround(oh, od, oa)
    # 抽水(overround)必须用原始赔率倒数和算, deoverround 已去抽水(和为1)不能复用
    overround = (1.0 / oh + 1.0 / od + 1.0 / oa) - 1.0

    # ① 市场隐含概率 + 抽水
    # ② 1X2 方向 = 市场 argmax (生产默认)
    best = max((ph, 0), (pd, 1), (pa, 2))
    direction = DIRECTION[best[1]]
    market_conf = best[0]

    # ③ OIP 比分 / 大小球
    r = predict_score(home_norm or home, away_norm or away, oh, od, oa)
    M = r["matrix"]; mg = M.shape[0] - 1
    lh, la = r["lh"], r["la"]
    ov25 = float(sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j >= 3))
    ov15 = float(sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j >= 2))
    ov35 = float(sum(M[i, j] for i in range(mg + 1) for j in range(mg + 1) if i + j >= 4))
    flat = M.flatten()
    order = np.argsort(-flat)[:3]
    top3 = [tuple(int(x) for x in divmod(int(k), mg + 1)) for k in order]
    top3_prob = [float(flat[k]) for k in order]

    # ④ 平局信号 (操盘手一手定价)
    m_pd = market_draw_prob(oh, od, oa)
    draw_alert = m_pd >= DRAW_ALERT

    # ⑤ 跨庄家共识 (仅五大联赛有 IW; WC 回退市场P平)
    consensus = None
    if home_norm and away_norm and date and league:
        try:
            consensus = consensus_draw_signal(home_norm, away_norm, oh, od, oa, date, league)
        except Exception:
            consensus = None

    # ⑥ 风控护栏
    high_vig = overround > HIGH_VIG

    out = {
        "home": home, "away": away,
        "odds": {"oh": oh, "od": od, "oa": oa},
        "market_prob": {"h": round(ph, 4), "d": round(pd, 4), "a": round(pa, 4)},
        "overround": round(overround, 4),
        "direction": direction,
        "market_conf": round(market_conf, 4),
        "oip": {
            "lambda_h": round(lh, 3), "lambda_a": round(la, 3),
            "top3_scores": [f"{h}-{a}" for (h, a) in top3],
            "top3_prob": [round(p, 4) for p in top3_prob],
            "over25": round(ov25, 4), "over15": round(ov15, 4), "over35": round(ov35, 4),
        },
        "draw_signal": {
            "market_pdraw": round(m_pd, 4),
            "draw_alert": draw_alert,
        },
        "consensus": consensus,
        "risk": {"high_vig": high_vig},
    }
    if verbose:
        print(_format(out))
    return out


def _format(o):
    lines = []
    lines.append(f"┌─ {o['home']} vs {o['away']}")
    lines.append(f"│ 赔率  H={o['odds']['oh']}  D={o['odds']['od']}  A={o['odds']['oa']}   "
                 f"抽水(overround)={o['overround']*100:.2f}%"
                 f"{'  ⚠️高抽水' if o['risk']['high_vig'] else ''}")
    lines.append(f"│ 市场隐含: H={o['market_prob']['h']*100:.1f}%  D={o['market_prob']['d']*100:.1f}%  "
                 f"A={o['market_prob']['a']*100:.1f}%")
    lines.append(f"│ ➤ 方向(市场argmax): {o['direction']}  (置信 {o['market_conf']*100:.1f}%)")
    oip = o["oip"]
    lines.append(f"│ OIP λ: 主{oip['lambda_h']} / 客{oip['lambda_a']}")
    scores = "  ".join(f"{s}({p*100:.1f}%)" for s, p in zip(oip['top3_scores'], oip['top3_prob']))
    lines.append(f"│ 比分Top3: {scores}")
    lines.append(f"│ 大小球 OIP: Over1.5={oip['over15']*100:.1f}%  Over2.5={oip['over25']*100:.1f}%  "
                 f"Over3.5={oip['over35']*100:.1f}%")
    ds = o["draw_signal"]
    flag = " 🔴平局预警" if ds["draw_alert"] else ""
    lines.append(f"│ 平局信号: 市场P(平)={ds['market_pdraw']*100:.1f}%{flag}")
    if o["consensus"]:
        c = o["consensus"]
        if c.get("available"):
            lines.append(f"│ 双庄共识: 均值P(平)={c['consensus']*100:.1f}%  分歧={c['agreement']:.3f}  "
                         f"强信号={c['strong']} (匹配:{c['join']})")
        else:
            lines.append(f"│ 双庄共识: 无IW精确匹配 -> 回退市场P(平) (WC/非五大联赛)")
    lines.append("└─")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# 来源适配
# ─────────────────────────────────────────────
def replay_db(edition=2026, limit=10, report=None):
    """从 wc_xlsx_matches 读真实赔率回放(验证模式, 显示真实赛果)。"""
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute(
        """SELECT home_norm, away_norm, date, oh, od, oa, hg, ag, stage
           FROM wc_xlsx_matches WHERE edition=? AND oh IS NOT NULL
           ORDER BY date LIMIT ?""", (edition, limit))
    rows = cur.fetchall(); con.close()
    print(f"=== 真实赔率回放 (edition={edition}, n={len(rows)}) ===\n")
    hits = 0
    results = []
    for (h, a, d, oh, od, oa, hg, ag, stage) in rows:
        o = predict_match(h, a, oh, od, oa, home_norm=h, away_norm=a, date=d, league=None, verbose=False)
        actual = f"{hg}-{ag}" if hg is not None and ag is not None else "未知"
        correct = (o["direction"] == "主胜" and hg > ag) or \
                  (o["direction"] == "平局" and hg == ag) or \
                  (o["direction"] == "客胜" and hg < ag)
        hits += 1 if correct else 0
        mark = "✓" if correct else "✗"
        print(_format(o))
        print(f"  真实赛果: {actual}   {mark}\n")
        o["actual"] = actual; o["direction_correct"] = correct
        results.append(o)
    acc = hits / len(rows) if rows else 0
    print(f"方向命中: {hits}/{len(rows)} = {acc*100:.1f}%  (市场argmax基线, 验证模式)")
    if report:
        with open(report, "w", encoding="utf-8") as f:
            json.dump({"edition": edition, "n": len(rows), "direction_acc": round(acc, 4),
                       "matches": results}, f, ensure_ascii=False, indent=2)
        print(f"报告已写: {report}")


def from_json(path):
    """读取 OCR 赔率图 JSON (odds_db/*.json 结构)。"""
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    o1x2 = d.get("odds_1x2", {})
    teams = d.get("teams", {})
    home = teams.get("home") or d.get("match", "").split(" vs ")[0]
    away = teams.get("away") or d.get("match", "").split(" vs ")[-1]
    oh = o1x2.get("home") or o1x2.get("h")
    od = o1x2.get("draw") or o1x2.get("d")
    oa = o1x2.get("away") or o1x2.get("a")
    if not all([home, away, oh, od, oa]):
        raise ValueError(f"JSON 缺少 1X2 赔率字段: home={home} away={away} oh={oh} od={od} oa={oa}")
    predict_match(home, away, oh, od, oa, home_norm=home, away_norm=away,
                  date=d.get("date"), league=None, verbose=True)


def live_feed(sport_key="soccer_fifa_world_cup", report=None):
    """实时拉取在跑比赛赔率并预测 (经 sp_odds_api)。key 失效时优雅报错。"""
    try:
        from pipeline.collectors.sp_odds_api import SPOddsAPI
    except Exception as e:
        print(f"[ERROR] 采集器加载失败: {e}"); return
    try:
        api = SPOddsAPI()
        matches = api.get_odds(sport_key)
    except Exception as e:
        print(f"[ERROR] 实时拉取失败 (key 可能已过期/无额度): {type(e).__name__}: {e}")
        print("   修复: 在 pipeline/collectors/config.ini 填入有效 The Odds API key,")
        print("         或配置环境变量 ODDPAPi_API_KEY 后改用 oddpapi 源。")
        print("   当前 predict_live.py 的 --from-db / --from-json / 手动CLI 仍可用真实赔率出预测。")
        return
    if not matches:
        print("实时接口返回 0 场 (该赛事当前可能无在跑比赛)。")
        return
    print(f"=== 实时预测 ({sport_key}, n={len(matches)}) ===\n")
    results = []
    for m in matches:
        h2h = m.get("best_h2h") or {}
        if not h2h:
            continue
        o = predict_match(m.get("home_team"), m.get("away_team"),
                          h2h.get("home"), h2h.get("draw"), h2h.get("away"),
                          home_norm=m.get("home_team"), away_norm=m.get("away_team"),
                          date=m.get("commence_time"), league=None, verbose=True)
        # 持久化实时盘口快照到 live_odds_raw (审计 + 可复跑)
        try:
            api.save_to_db(m)
        except Exception:
            pass
        o["fixture"] = {"home": m.get("home_team"), "away": m.get("away_team"),
                        "commence_time": m.get("commence_time"), "sport_key": sport_key}
        results.append(o)
        print()
    if report and results:
        with open(report, "w", encoding="utf-8") as f:
            json.dump({"sport_key": sport_key, "n": len(results), "predictions": results,
                       "captured_at": datetime.now(timezone(timedelta(hours=8))).isoformat()},
                      f, ensure_ascii=False, indent=2)
        print(f"报告已写: {report}")


def main():
    p = argparse.ArgumentParser(description="真实比赛实时预测编排器 (v6.0 锁定架构)")
    p.add_argument("--home", help="主队名")
    p.add_argument("--away", help="客队名")
    p.add_argument("--oh", type=float, help="主胜赔率")
    p.add_argument("--od", type=float, help="平局赔率")
    p.add_argument("--oa", type=float, help="客胜赔率")
    p.add_argument("--from-db", type=int, metavar="EDITION", help="从 wc_xlsx_matches 真实赔率回放 (如 2026)")
    p.add_argument("--limit", type=int, default=10, help="--from-db 条数")
    p.add_argument("--from-json", metavar="PATH", help="OCR 赔率图 JSON (odds_db/*.json)")
    p.add_argument("--live", nargs="?", const="soccer_fifa_world_cup", metavar="SPORT_KEY",
                   help="实时拉取在跑比赛赔率并预测")
    p.add_argument("--report", metavar="PATH", help="回放报告输出 JSON 路径")
    args = p.parse_args()

    if args.live is not None:
        live_feed(args.live, args.report); return
    if args.from_json:
        from_json(args.from_json); return
    if args.from_db is not None:
        replay_db(args.from_db, args.limit, args.report); return
    if all([args.home, args.away, args.oh, args.od, args.oa]):
        predict_match(args.home, args.away, args.oh, args.od, args.oa,
                      home_norm=args.home, away_norm=args.away, verbose=True); return
    p.print_help()


if __name__ == "__main__":
    main()
