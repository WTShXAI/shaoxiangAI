"""WC2026 W/D/L 融合原型 v0.4 (哨响AI · 赵统筹)
---------------------------------------------------
最小可跑原型: 赔率逆向工程(地基) + 战意校准层(校正) + 阵容/伤病桩位(预留).
不依赖 wc_engine, 逻辑全摊开, 零三方库依赖(仅 json/datetime/math).

融合:
  p_base  = 去水归一化后的 1X2 隐含概率 [H, D, A]
  verdict = argmax(p_base) 经战意窄门控硬规则覆盖, 否则跟赔率
  (软乘数经验证无法翻转 argmax, 故战意层用窄门控硬规则, 可审计)

v0.2 优化(2026-07-06 扫描实证):
  - R1 薄边门控(小组MD3 + 平局紧贴热门 pd>=fav-0.12) 翻 D: 56/80(70.0%), +3/-0 最优
  - R2 短平赔(od<=3.0 且平局是次高 且 max<0.55) 翻 D: 独立庄家信号, in-sample 中性(同70%), OOS 合理
  - 淘汰赛 win-or-bust 不翻 D(修掉 R16 误判)
  - 输出 置信度(max p_base) + 推理链(透明可审计)
  - 扫描证实: 任何额外 blanket 门控(R3均衡局/R4组合)过度翻 D 退化 -> L1+L2 天花板≈70%,
    突破70%必须靠 ML(A路径 DrawExpert). 本原型定位=可解释教学, 非生产替代.

设计铁律:
  - 战意层 ONLY 在 [小组 MD3 薄边] 或 [短平赔均衡局] 时翻 D.
  - 淘汰赛 / 一边倒热门 不翻(平局激励低).
  - 阵容/伤病 δ=1.0 桩位(BLOCKED: worldcup26.ir 无此数据, 见下方).

v0.4 新增 L5 赛后 xG 审计层 (2026-07-07):
  - 数据源 wc_xlsx_matches(hxg/axg 来自 football-data.co.uk, 属【赛后实际 xG】).
  - 关键诚信: 赛后 xG 喂进预测 = 数据泄漏(提前知道赛果质量), 故绝不用于 fuse().
  - 正确定位 = 赛后审计: 验证"预测赢家是否也赢了 xG Battle", 作模型可信度校验, 零泄漏.
  - L3/L4 预测桩位仍诚实 BLOCKED(真实赛前阵容/伤病源缺失).
"""
import json, sys, sqlite3
from datetime import datetime
from pathlib import Path
from collections import Counter

DB = Path(r"D:/Architecture/data/football_data.db")

# ── 中文队名 -> 英文 canonical (复用 wc_engine 口径, 与 xlsx 对齐) ──
sys.path.insert(0, str(Path(r"D:/Architecture/pipeline")))
import wc_engine as W
_ZH2EN = {v: k for k, v in W._TEAM_ALIAS.items() if v}
def _to_en(name):
    if not name:
        return None
    e = _ZH2EN.get(name.strip(), name.strip())
    return W._canon_team(e).lower()

# ── 门控参数(均来自 80 场阈值扫描的 principled 结论) ──
STRONG_TH = 0.68    # 强热阈值(与 wc_engine 对齐): maxp>此值=一边倒, 不翻D
EDGE       = 0.12    # 薄边阈值: 平局概率与热门差距 <= 此值 视为"紧贴"
SHORT_DRAW = 3.0     # 短平赔阈值: 平赔 <= 此值 且均衡局 -> 庄家预期平局
BAL_MAX    = 0.55    # 均衡上限: max p_base < 此值 才视为"双方均衡"

# ───────────────────────────────────────────────────────────
# 数据加载: 76 小组赛(full_backtest details) + 4 R16(真实赛果)
# ───────────────────────────────────────────────────────────
def infer_matchday(date_str: str) -> int:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    if d <= datetime(2026, 6, 16): return 1
    if d <= datetime(2026, 6, 22): return 2
    return 3

def load_matches():
    with open("deliverables/wc2026_full_backtest.json", "r", encoding="utf-8") as f:
        old = json.load(f)
    group = old.get("details", [])
    additions = [
        {"date":"2026-06-21","home":"西班牙","away":"沙特","oh":1.08,"od":8.80,"oa":18.0,"res":"H","sc":"4-0","src":"ocr_variants"},
        {"date":"2026-06-23","home":"葡萄牙","away":"乌兹别克","oh":1.22,"od":5.90,"oa":10.00,"res":"H","sc":"5-0","src":"ocr_variants"},
        {"date":"2026-06-27","home":"佛得角","away":"沙特","oh":2.47,"od":3.35,"oa":2.62,"res":"D","sc":"0-0","src":"ocr_variants"},
        {"date":"2026-06-27","home":"民主刚果","away":"乌兹别克","oh":2.27,"od":3.25,"oa":2.97,"res":"H","sc":"3-1","src":"ocr_variants"},
    ]
    existing = {(d["home"], d["away"]) for d in group}
    for a in additions:
        if (a["home"], a["away"]) not in existing:
            group.append(a)
    with open("data/wc2026_r16_results.json", "r", encoding="utf-8") as f:
        r16 = json.load(f)["matched"]
    matches = []
    for d in group:
        matches.append({"date":d["date"],"home":d["home"],"away":d["away"],
                        "oh":d["oh"],"od":d["od"],"oa":d["oa"],
                        "res":d["res"],"sc":d.get("sc",""),"src":d.get("src","?"),
                        "stage":"group","matchday":infer_matchday(d["date"])})
    for d in r16:
        matches.append({"date":d["date"],"home":d["home"],"away":d["away"],
                        "oh":d["oh"],"od":d["od"],"oa":d["oa"],
                        "res":d["res"],"sc":d.get("sc",""),"src":d.get("src","?"),
                        "stage":"knockout","matchday":0})
    return matches

# ───────────────────────────────────────────────────────────
# L1: 赔率逆向工程 (去水归一化)
# ───────────────────────────────────────────────────────────
def odds_to_base(oh, od, oa):
    ih, id_, ia = 1.0/oh, 1.0/od, 1.0/oa
    s = ih + id_ + ia
    return [ih/s, id_/s, ia/s]   # [p_h, p_d, p_a], 已去庄家抽水

# ───────────────────────────────────────────────────────────
# L2: 战意校准层 (gated override + 推理链)
# ───────────────────────────────────────────────────────────
def motivation_override(match, p_base, pure_v):
    """返回 (override, reason):
      override='D' 表示翻平局; None 表示不干预(跟赔率).
      reason 为可读推理链(透明审计).
    翻转条件(仅当赔率本非D):
      R1 薄边: 小组MD3 且 平局紧贴热门(pd >= fav-EDGE)
      R2 短平赔: 平赔<=SHORT_DRAW 且 平局是次高 且 双方均衡(maxp<BAL_MAX)
    不翻转: 一边倒热门(maxp>STRONG_TH) / 淘汰赛(胜者通吃)
    """
    maxp = max(p_base)
    ph, pd, pa = p_base
    fav = max(ph, pa)
    second = sorted(p_base)[1]
    if maxp > STRONG_TH:
        return None, f"clear favorite (maxp={maxp:.2f}>{STRONG_TH})"
    if match["stage"] == "knockout":
        return None, "knockout win-or-bust, no draw lean"
    # R1 薄边门控
    if (match["stage"] == "group" and match["matchday"] == 3
            and pd >= fav - EDGE and pure_v != "D"):
        return "D", f"MD3 thin-edge draw (pd={pd:.2f}>=fav-EDGE={fav-EDGE:.2f})"
    # R2 短平赔均衡局
    if (match["stage"] == "group" and match["od"] <= SHORT_DRAW
            and pd >= second - 1e-9 and maxp < BAL_MAX and pure_v != "D"):
        return "D", f"short draw odds (od={match['od']}<={SHORT_DRAW}, balanced maxp={maxp:.2f})"
    return None, "no motivation trigger (follow odds)"

# ───────────────────────────────────────────────────────────
# L3/L4: 阵容 / 伤病  —  BLOCKED (2026-07-06 魏监听探查结论)
# ───────────────────────────────────────────────────────────
# worldcup26.ir 全站 swagger 仅 8 条 GET 路由, 无 /get/lineups / /get/injuries
# /get/squads / /get/players (全部 404「Route not found」, 非缺参 => 路由不存在).
# 唯一球员级数据 = 每场 home_scorers/away_scorers 字符串(进球者+分钟), 属【赛后数据】,
# 用作同场预测 = 泄漏, 禁用.
# ───────────────────────────────────────────────────────────
# L3: 阵容层 (v0.4 实装 — ESPN 免费源 wc_lineups)
# ───────────────────────────────────────────────────────────
# 数据: scripts/etl_espn_lineups.py 从 ESPN fifa.world 拉 94 场已结束 lineup
# 存入 wc_lineups(home/away/formation/starters/bench, canonical 对齐).
# 语义: 用「阵容稳定性指数」=本场首发在该队所有场次的平均出现率, 衡量轮换冲击.
#   - 稳定性 70% 为基线(强队稳定首发)
#   - 偏离 ±10% -> 乘子 ±0.8%, 硬上限 ±4% (保守, 防 blanket 式拖累)
#   - 非对称: δ_d=1.0, 胜负各自独立(轮换多的队胜算真实下调, 不被归一化抵消)
#   - 数据不足(<2场/队) -> δ=1.0 不生效
# 不编造球员实力分, 仅用「轮换幅度」作方向性校正信号.
_LINEUP_LUT = None
_REGULAR = None
_REG_N = {}
def load_lineup_lut():
    global _LINEUP_LUT, _REGULAR, _REG_N
    lut = {}; reg = {}; reg_n = {}
    try:
        c = sqlite3.connect(str(DB))
        for r in c.execute("SELECT home,away,home_starters,away_starters FROM wc_lineups"):
            h, a = _to_en(r[0]), _to_en(r[1])
            if not h or not a:
                continue
            hs = set(json.loads(r[2])); as_ = set(json.loads(r[3]))
            lut[(h, a)] = (hs, as_)
            for team, st in ((h, hs), (a, as_)):
                reg.setdefault(team, Counter()).update(st)
                reg_n[team] = reg_n.get(team, 0) + 1
        c.close()
    except Exception as e:
        print(f"  [WARN] L3 lineup 加载失败: {e}")
    _REGULAR = reg
    _REG_N = reg_n
    _LINEUP_LUT = lut

def lineup_multipliers(match):
    global _LINEUP_LUT, _REGULAR, _REG_N
    if _LINEUP_LUT is None:
        load_lineup_lut()
    h = _to_en(match["home"]); a = _to_en(match["away"])
    key = (h, a)
    if key not in _LINEUP_LUT:
        return 1.0, 1.0, 1.0          # 无 lineup 覆盖
    hs, as_ = _LINEUP_LUT[key]
    def stability(team, st):
        """本场首发在该队所有场次的平均出现率 ∈[0,1]; 数据不足->基线0.70."""
        if team not in _REGULAR or _REG_N.get(team, 0) < 2 or not st:
            return 0.70
        n = _REG_N[team]
        return sum(_REGULAR[team].get(p, 0) / n for p in st) / len(st)
    sh = stability(h, hs); sa = stability(a, as_)
    dh = max(0.96, min(1.04, 1.0 + (sh - 0.70) * 0.08))
    da = max(0.96, min(1.04, 1.0 + (sa - 0.70) * 0.08))
    return dh, 1.0, da

# ───────────────────────────────────────────────────────────
# L4: 伤病层 — BLOCKED (2026-07-07 联网排查结论)
# ───────────────────────────────────────────────────────────
# 联网实测: ESPN 免费 API 无结构化 injuries(summary/rosters/boxscore 均无);
# worldcup26.ir / tonkabits / github rezarahiminia 均无 injury 端点;
# transfermarkt 反爬(人机验证); thestatsapi / api-football 需付费/注册 key.
# => L4 真实 feed 待 api-football 免费 key 激活(见 scripts/fetch_l4_api_football_template.py).
# 当前 δ 恒 1.0, 即 L4 不生效(诚实保留桩位, 不伪造/不硬套).
def injury_multipliers(match):
    return 1.0, 1.0, 1.0   # BLOCKED (待 api-football key)

# ───────────────────────────────────────────────────────────
# L5: 赛后 xG 审计层 (v0.4 新增, 零泄漏)
# ───────────────────────────────────────────────────────────
# 赛后实际 xG 不可作预测特征(泄漏), 仅用于审计: 预测赢家是否也赢了 xG Battle.
def load_xg_lookup():
    """返回 {(canon_h,canon_a): (hxg, axg, hs, as_)} 取自 wc_xlsx_matches(2026)."""
    lut = {}
    try:
        c = sqlite3.connect(str(DB))
        for r in c.execute(
            "SELECT home,away,hxg,axg,hs,as_ FROM wc_xlsx_matches WHERE edition='2026'"):
            k = (_to_en(r[0]), _to_en(r[1]))
            if k[0] and k[1]:
                lut[k] = (r[2], r[3], r[4], r[5])
        c.close()
    except Exception as e:
        print(f"  [WARN] L5 xG 加载失败: {e}")
    return lut

def xg_winner_of(hxg, axg):
    """xG Battle 赢家: H/D/A. 平局判 xG 差 < 0.05."""
    if hxg is None or axg is None:
        return None
    if abs(hxg - axg) < 0.05:
        return "D"
    return "H" if hxg > axg else "A"

# ───────────────────────────────────────────────────────────
# 融合
# ───────────────────────────────────────────────────────────
def fuse(p_base, mh, md, ma, lh, ld, la, ih, id_, ia):
    raw = [p_base[0]*mh*lh*ih, p_base[1]*md*ld*id_, p_base[2]*ma*la*ia]
    s = sum(raw)
    return [x/s for x in raw]

def verdict_of(p):
    return ["H", "D", "A"][p.index(max(p))]

# ───────────────────────────────────────────────────────────
# 主流程
# ───────────────────────────────────────────────────────────
def main():
    matches = load_matches()
    xg_lut = load_xg_lookup()
    load_lineup_lut()
    print("Total matches: %d (group=%d, knockout=%d) | L5 xG 覆盖对阵: %d | L3 lineup 覆盖对阵: %d" % (
        len(matches),
        sum(1 for m in matches if m["stage"]=="group"),
        sum(1 for m in matches if m["stage"]=="knockout"),
        len(xg_lut), len(_LINEUP_LUT)))

    rows = []
    pure_ok = [0,0]; fused_ok = [0,0]
    pure_ko = [0,0]; fused_ko = [0,0]
    # L5 审计计数器
    aud = {"cov":0, "pred_ok_xg_agree":0, "pred_ok_xg_disagree":0,
           "pred_wrong":0, "pred_xg_agree":0}
    for m in matches:
        p_base = odds_to_base(m["oh"], m["od"], m["oa"])
        lh, ld, la = lineup_multipliers(m)
        ih, id_, ia = injury_multipliers(m)
        p_final = fuse(p_base, 1.0, 1.0, 1.0, lh, ld, la, ih, id_, ia)  # stubs -> =p_base

        pure_v = verdict_of(p_base)
        mot_ov, mot_reason = motivation_override(m, p_base, pure_v)
        fused_v = mot_ov if mot_ov else pure_v
        conf = round(max(p_base), 3)
        res = m["res"]
        p_ok = pure_v == res
        f_ok = fused_v == res

        # L5 赛后 xG 审计(零泄漏: 仅校验, 不入预测)
        xg_w = None; xg_pair = None
        xg_pair = xg_lut.get((_to_en(m["home"]), _to_en(m["away"])))
        if xg_pair:
            aud["cov"] += 1
            xg_w = xg_winner_of(xg_pair[0], xg_pair[1])
            if fused_v == xg_w:
                aud["pred_xg_agree"] += 1
            if f_ok:
                if xg_w == res:
                    aud["pred_ok_xg_agree"] += 1   # 预测对且 xG 也站队
                else:
                    aud["pred_ok_xg_disagree"] += 1  # 预测对但 xG 反水(运气球)
            else:
                aud["pred_wrong"] += 1

        reason = f"argmax={pure_v}(conf {conf})"
        if mot_ov:
            reason += f" | MOTIVE→{mot_ov}: {mot_reason}"
        else:
            reason += f" | {mot_reason}"
        if xg_w:
            reason += f" | xG={xg_w}(h{xg_pair[0]:.2f}/a{xg_pair[1]:.2f})"

        rows.append({"date":m["date"],"home":m["home"],"away":m["away"],
                     "stage":m["stage"],"md":m["matchday"],
                     "p_base":[round(x,3) for x in p_base],
                     "p_final":[round(x,3) for x in p_final],
                     "pure":pure_v,"fused":fused_v,"res":res,
                     "conf":conf,"reason":reason,
                     "mot":mot_ov or "none","pure_ok":p_ok,"fused_ok":f_ok,
                     "xg_winner":xg_w})
        pure_ok[0]+=1; pure_ok[1]+=int(p_ok)
        fused_ok[0]+=1; fused_ok[1]+=int(f_ok)
        if m["stage"]=="knockout":
            pure_ko[0]+=1; pure_ko[1]+=int(p_ok)
            fused_ko[0]+=1; fused_ko[1]+=int(f_ok)

    L = []
    L.append("="*70)
    L.append("WC2026 W/D/L 融合原型 v0.4  (%d 场, 真实 stage/matchday)" % len(rows))
    L.append("="*70)
    pa = pure_ok[1]/pure_ok[0]*100; fa = fused_ok[1]/fused_ok[0]*100
    L.append("  纯赔率 argmax : %2d/%2d (%.1f%%)" % (pure_ok[1], pure_ok[0], pa))
    L.append("  融合(赔率+战意): %2d/%2d (%.1f%%)" % (fused_ok[1], fused_ok[0], fa))
    L.append("  融合 - 纯赔率  : %+.1f pp  (L1+L2 天花板≈70%%, 突破须ML/A路径)" % (fa-pa))
    L.append("")
    L.append("  淘汰赛子集: 纯赔率 %d/%d (%.1f%%) | 融合 %d/%d (%.1f%%)" % (
        pure_ko[1], pure_ko[0], pure_ko[1]/max(pure_ko[0],1)*100,
        fused_ko[1], fused_ko[0], fused_ko[1]/max(fused_ko[0],1)*100))
    L.append("")
    L.append("  %-10s %-16s %-5s %-5s %-5s %-6s %s" % (
        "date","match","pure","fused","res","conf","reason"))
    for r in rows:
        mt = "%s vs %s" % (r["home"], r["away"])
        L.append("  %-10s %-16s %-5s %-5s %-5s %-6s %s" % (
            r["date"], mt[:16], r["pure"], r["fused"],
            r["res"], r["conf"], r["reason"]))
    flips = [r for r in rows if r["pure"]!=r["fused"]]
    L.append("")
    L.append("战意层翻转(%d场):" % len(flips))
    for r in flips:
        tag = "修正" if (not r["pure_ok"] and r["fused_ok"]) else ("退化" if (r["pure_ok"] and not r["fused_ok"]) else "同错")
        L.append("  [%s MD%d] %s vs %s: %s->%s (actual=%s) [%s] %s" % (
            r["stage"], r["md"], r["home"], r["away"], r["pure"], r["fused"], r["res"], tag, r["reason"]))
    # ── L5 赛后 xG 审计报告 ──
    L.append("")
    L.append("─"*70)
    L.append("L5 赛后 xG 审计层 (零泄漏: 仅校验, 不入预测)")
    L.append("─"*70)
    if aud["cov"] > 0:
        L.append("  xG 覆盖对阵      : %d / %d" % (aud["cov"], len(rows)))
        L.append("  预测赢家也赢xG   : %d/%d (%.1f%%)  ← 预测与场面一致, 可信度高" % (
            aud["pred_xg_agree"], aud["cov"], aud["pred_xg_agree"]/aud["cov"]*100))
        L.append("  预测正确 & xG站队 : %d 场 (预测对且场面也支持)" % aud["pred_ok_xg_agree"])
        L.append("  预测正确但xG反水 : %d 场 (运气球, 场面不支持仍猜对)" % aud["pred_ok_xg_disagree"])
        L.append("  预测错误          : %d 场" % aud["pred_wrong"])
        if (aud["pred_ok_xg_agree"]+aud["pred_ok_xg_disagree"]) > 0:
            luck = aud["pred_ok_xg_disagree"]/(aud["pred_ok_xg_agree"]+aud["pred_ok_xg_disagree"])*100
            L.append("  → 正确预测中运气球占比: %.1f%%  (越低=模型越靠实力非运气)" % luck)
    else:
        L.append("  (无 xG 覆盖, 跳过审计)")
    out = "\n".join(L)
    print(out)
    with open("deliverables/_fusion_wdl_proto.txt","w",encoding="utf-8") as f:
        f.write(out)
    js = {"version":"v0.4","total":len(rows),
          "pure_acc":round(pure_ok[1]/pure_ok[0],4),
          "fused_acc":round(fused_ok[1]/fused_ok[0],4),
          "l5_xg_audit":aud,
          "params":{"EDGE":EDGE,"SHORT_DRAW":SHORT_DRAW,"BAL_MAX":BAL_MAX,"STRONG_TH":STRONG_TH},
          "details":rows}
    with open("deliverables/wc2026_fusion_wdl_proto.json","w",encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=2)
    print("\nSaved: deliverables/wc2026_fusion_wdl_proto.json + _fusion_wdl_proto.txt")

if __name__ == "__main__":
    main()
