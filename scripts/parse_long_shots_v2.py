# -*- coding: utf-8 -*-
"""
long/ 截图结构化解析器 v2 — 基于行序锚点, 修复 v1 的队名/比分/赔率错配。
核心思路: 找 "全场独赢/全场让球/全场大小" 等锚点行号, 按相对行号取值。
  - 队名 = "全场独赢" 后第1、第3行
  - 独赢赔率 = 队名段后连续3个 X.XX
  - 让球盘/赔率 = "全场让球" 后取盘口+赔率
  - 大小球盘/赔率 = "全场大小" 后取盘口+赔率
  - 当前比分 = 形如 "4 - 3" (带空格), 排除时间 HH:MM 和 "上半场X-Y"
"""
import os, json, re

OCR_ROOT = r"D:\Architecture\data\leisu_capture\long_ocr"
STATS = r"D:\Architecture\data\long_classify_stats_v3.json"
OUT = r"D:\Architecture\data\long_features"
os.makedirs(OUT, exist_ok=True)

def load_txts(item_file):
    for sub in os.listdir(OCR_ROOT):
        p = os.path.join(OCR_ROOT, sub, item_file + ".json")
        if os.path.exists(p):
            return [d["text"] for d in json.load(open(p, encoding="utf-8"))]
    return []

def date_from(fname):
    m = re.match(r"(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})", fname)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}" if m else None

def fnum(s):
    """安全转 float, 失败返回 None。"""
    try:
        s = s.replace(",", "").strip()
        return float(s) if re.fullmatch(r"-?\d+(\.\d+)?", s) else None
    except Exception:
        return None

def find_idx(txts, *keys):
    """找第一个含任一 key 的行号。"""
    for i, t in enumerate(txts):
        for k in keys:
            if k in t:
                return i
    return -1

# ═══════════════════ B 滚球盘口 (行序锚点) ═══════════════════
NOISE = {"全场独赢","全场让球","全场大小","和局","主胜","客胜","视频直播","动画直播",
         "真人主播","投注","赛况","首发","前瞻","波胆","角球","让球&大小","进球",
         "特色组合","全部","未结算","已结算","永久禁言中","所有投注","全场平局","半场平局",
         "罚牌","15分钟","全场大小-附加盘","即将开赛","进行中","中场休息","未开场",
         "主","客","大","小","赛果查询","设置菜单","未结注单","已结注单","刷新"}

def is_time_or_noise(s):
    """是否为时间串(上半场52:20)或噪声, 不应作队名。"""
    s = s.strip()
    if s in NOISE or s in ("和局",): return True
    if re.search(r"\d{1,2}:\d{2}", s): return True  # 含时间
    if re.match(r"[上下]半场", s): return True
    return False

def parse_live_odds(txts, fname):
    rec = {"source": fname, "date": date_from(fname), "_raw_n": len(txts)}
    joined = "\n".join(txts)
    # 联赛
    lm = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,25}?(?:联赛|锦标赛|杯赛|超级联赛|甲级|乙级|英超|德甲|西甲|意甲|甲|超|联盟)[\u4e00-\u9fa5A-Za-z0-9U]*)", joined)
    if lm: rec["league"] = re.sub(r"[.。]+$", "", lm.group(1).strip())
    # 状态 + 分钟
    sm = re.search(r"(上半场|下半场|中场休息|即将开赛|未开场|中场)\s*(\d{1,3})?:?(\d{1,2})?", joined)
    if sm:
        rec["status"] = sm.group(1)
        if sm.group(2): rec["match_minute"] = int(sm.group(2))
    # 当前比分: "4 - 3" 带空格连字符, 严格约束 (排除时间/半场比分/列表页跨行误拼)
    # 要求: 两侧都是 0-9 的合理比分, 且附近无 "分:秒" 时间结构
    for bm in re.finditer(r"(?<![\d:])(\d)\s+-\s*(\d)(?![\d:])", joined):
        h, a = int(bm.group(1)), int(bm.group(2))
        ctx = joined[max(0, bm.start()-8):bm.end()+8]
        # 排除带时间结构 HH:MM 或 "上半场X-Y" 半场比分
        if re.search(r"\d{1,2}:\d{2}", ctx): continue
        if re.search(r"[半场]", ctx): continue
        rec["score_home"], rec["score_away"] = h, a
        rec["score_total"] = h + a
        break

    # ── 识别页面布局: 单场详情 vs 让球&大小列表 ──
    # 列表页特征: 有 "主胜"/"客胜" 标签 + 队名行紧邻盘口赔率
    idx_zhusheng = find_idx(txts, "主胜")
    idx_kesheng = find_idx(txts, "客胜")
    is_list_page = (idx_zhusheng >= 0 and idx_kesheng >= 0 and idx_kesheng > idx_zhusheng)

    if is_list_page:
        # ══ 让球&大小列表页: 主胜[队名|比分|让球盘|大小盘|赔率...] 客胜[...] ══
        # 主胜段: idx_zhusheng 到 idx_kesheng 之间
        seg_h = txts[idx_zhusheng+1: idx_kesheng]
        seg_a = txts[idx_kesheng+1: idx_kesheng+15]
        rec["layout"] = "handicap_ou_list"
        # 主队名 = seg_h 第一个非数字非盘口的中文/字母行
        for t in seg_h:
            ts = t.strip()
            if is_time_or_noise(ts) or ts in ("主胜","客胜"): continue
            if re.fullmatch(r"[\d.+\-/\s]+", ts): continue
            if re.search(r"[+\-]\d", ts) and len(ts)<10: continue
            if re.search(r"[大小][\d./]+", ts): continue
            if len(ts) > 18: continue
            rec["home"] = re.sub(r"[.。]+$", "", ts); break
        for t in seg_a:
            ts = t.strip()
            if is_time_or_noise(ts) or ts in ("主胜","客胜"): continue
            if re.fullmatch(r"[\d.+\-/\s]+", ts): continue
            if re.search(r"[+\-]\d", ts) and len(ts)<10: continue
            if re.search(r"[大小][\d./]+", ts): continue
            if len(ts) > 18: continue
            rec["away"] = re.sub(r"[.。]+$", "", ts); break
        # 让球盘: seg_h 内 "+0/0.5"/"-0.5" 等 (主队让球)
        def extract_hcap(seg):
            for t in seg:
                hm = re.search(r"([+\-]\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)", t)
                if hm:
                    base = hm.group(1).split("/")[0]
                    v = fnum(base)
                    if v is not None: return v
            return None
        hc_h = extract_hcap(seg_h)
        if hc_h is not None:
            rec["handicap_home"] = hc_h
            rec["handicap_away"] = -hc_h  # 客队让球 = 主队的相反
            rec["is_home_fav"] = 1 if hc_h < 0 else 0
        # 大小球盘: "大3/3.5" 取线
        ou_m = re.search(r"大\s*(\d+(?:\.\d+)?(?:/\d+)?)", "\n".join(seg_h))
        if ou_m: rec["ou_line"] = ou_m.group(1)
        # 赔率归类: 列表页每队一行的赔率是 [让球赔率? 大球赔率 小球赔率] 或 [大球 小球]
        # 实测: 大邱FC行 → 0(比分) +0.5(让球) 大4.5/5(大小盘) 1.93 1.98 2.07
        # 其中 1.93/1.98 是让球主/客赔, 2.07 是大小球某侧. 但跨行难精确归类.
        # 稳健做法: 收集 seg_h+seg_a 内所有 X.XX 赔率, 标记为 list_odds 供下游
        all_o = []
        for t in seg_h + seg_a:
            v = fnum(t)
            if v is not None and 1.0 < v < 100: all_o.append(v)
        rec["list_odds"] = all_o[:8]
        # 让球赔率 (前2个通常是让球主客赔)
        if len(all_o) >= 2:
            rec["handicap_odds_h"], rec["handicap_odds_a"] = all_o[0], all_o[1]
    else:
        # ══ 单场详情页: 有真正独赢三赔 ══
        rec["layout"] = "single_match_detail"
        idx_1x2 = find_idx(txts, "全场独赢")
        if idx_1x2 >= 0:
            teams = []
            for t in txts[idx_1x2+1: idx_1x2+15]:
                ts = t.strip()
                if is_time_or_noise(ts): continue
                if re.fullmatch(r"[\d.+\-/\s]+", ts): continue
                if re.search(r"[+\-]\d", ts) and len(ts) < 10: continue
                if re.search(r"[大小][\d./]+", ts): continue
                if len(ts) > 18: continue
                teams.append(re.sub(r"[.。]+$", "", ts))
                if len(teams) == 2: break
            if len(teams) == 2:
                rec["home"], rec["away"] = teams[0], teams[1]
            # 独赢三赔: 队名段后连续3个, 校验跨度(真实独赢 d赔率通常>=3.0 或与h/a差距大)
            odds_after = []
            for t in txts[idx_1x2+1: idx_1x2+20]:
                v = fnum(t)
                if v is not None and 1.0 < v < 100:
                    odds_after.append(v)
                    if len(odds_after) == 3: break
            # 校验: 若三赔都<2.5 (太接近), 多半抓错; 单赔>30 异常; 不采信
            if len(odds_after) == 3 and (max(odds_after) >= 3.0 or odds_after[1] >= 2.8) and max(odds_after) <= 30:
                rec["odds_h"], rec["odds_d"], rec["odds_a"] = odds_after
            # 让球/大小 (单场页)
            idx_h = find_idx(txts, "全场让球")
            if idx_h >= 0:
                seg = txts[idx_h+1: idx_h+12]
                for t in seg:
                    hm = re.search(r"([+\-]\d+(?:\.\d+)?(?:/\d+)?)", t)
                    if hm:
                        v = fnum(hm.group(1).split("/")[0])
                        if v is not None:
                            rec["handicap_home"] = v
                            rec["is_home_fav"] = 1 if v < 0 else 0
                            break
                ho = [fnum(t) for t in seg if fnum(t) and 1.0 < fnum(t) < 100][:2]
                if len(ho) == 2: rec["handicap_odds_h"], rec["handicap_odds_a"] = ho
            idx_ou = -1
            for i, t in enumerate(txts):
                if t.strip() == "全场大小" or (t.strip().startswith("全场大小") and "附加" not in t and len(t.strip()) <= 6):
                    idx_ou = i; break
            if idx_ou >= 0:
                seg = txts[idx_ou+1: idx_ou+10]
                ou = re.search(r"[大小]\s*(\d+(?:\.\d+)?(?:/\d+)?)", "\n".join(seg))
                if ou: rec["ou_line"] = ou.group(1)
                ouo = [fnum(t) for t in seg if fnum(t) and 1.0 < fnum(t) < 100][:2]
                if len(ouo) == 2: rec["ou_odds_over"], rec["ou_odds_under"] = ouo
    return rec

# ═══════════════════ E 必发资金面 (行序锚点) ═══════════════════
def parse_odds_trend(txts, fname):
    rec = {"source": fname, "date": date_from(fname), "_raw_n": len(txts)}
    joined = "\n".join(txts)
    # 联赛
    for kw in ("挪超","瑞典超","波兰甲","日本","澳超","韩K","美职","墨西超","英超","德甲","西甲","意甲","法甲","中超","欧冠","英冠","丹超","芬超","以超"):
        if kw in joined:
            mm = re.search(kw + r"[\u4e00-\u9fa5A-Za-z]*", joined)
            rec["league"] = mm.group(0) if mm else kw
            break
    # 赛前/赛中
    cm = re.search(r"距离开赛\s*(\d{2}:\d{2}:\d{2})", joined)
    rec["is_prematch"] = bool(cm)
    if cm: rec["countdown"] = cm.group(1)
    if "中场" in joined or re.search(r"[上下]半场\d", joined): rec["is_prematch"] = False
    # 必发交易量: "主队" 行号 → 后续连续2个数字行(交易量, 指数)
    def betfair_block(role):
        idx = find_idx(txts, role)
        if idx < 0: return {}
        nums = []
        for t in txts[idx+1: idx+6]:
            v = fnum(t.replace(",", ""))
            if v is not None:
                nums.append(v)
            elif nums:  # 已开始收集数字, 遇非数字停止
                break
            if len(nums) >= 2: break
        if len(nums) >= 2:
            return {"volume": int(nums[0]), "bf_odds": nums[1]}
        return {}
    rec["home_betfair"] = betfair_block("主队")
    rec["draw_betfair"] = betfair_block("和局")
    rec["away_betfair"] = betfair_block("客队")
    # 盈亏指数: 在 "计算盈亏" 后, "主队/和局/客队" 各跟 盈亏额+指数
    idx_pnl = find_idx(txts, "计算盈亏")
    if idx_pnl >= 0:
        pnl = {}
        for role in ("主队", "和局", "客队"):
            ri = find_idx(txts[idx_pnl:], role)
            if ri >= 0:
                ri_abs = idx_pnl + ri
                nums = []
                for t in txts[ri_abs+1: ri_abs+5]:
                    v = fnum(t.replace(",", ""))
                    if v is not None: nums.append(v)
                    elif nums: break
                    if len(nums) >= 2: break
                if len(nums) >= 2:
                    pnl[role] = {"pnl": int(nums[0]), "pnl_index": int(nums[1])}
        rec["pnl"] = pnl
    return rec

# ═══════════════════ A 账单 (切片) ═══════════════════
def parse_bill(txts, fname):
    """按 '结果比分' 锚点切片, 每片一个注单。"""
    joined = "\n".join(txts)
    bets = []
    # 所有结果比分位置
    res_spans = [(m.start(), m.group(1), m.group(2)) for m in re.finditer(r"全场比分[（(]?\s*(\d+)\s*[-—]\s*(\d+)\s*[)）]?", joined)]
    for si, (pos, h, a) in enumerate(res_spans):
        end = res_spans[si+1][0] if si+1 < len(res_spans) else len(joined)
        chunk = joined[pos:end]
        bet = {"source": fname, "date": date_from(fname), "bill_idx": si}
        bet["result_home"], bet["result_away"] = int(h), int(a)
        bet["result_total"] = int(h) + int(a)
        s = int(h) - int(a)
        bet["result_1x2"] = "H" if s > 0 else ("A" if s < 0 else "D")
        # 输赢
        wm = re.search(r"输/赢[：:]\s*(赢|输|走|和)", chunk)
        if wm: bet["bet_outcome"] = wm.group(1)
        # 投注项 + 真实赔率 (@ 后的 X.XX, 排除盘口)
        im = re.search(r"投注项[：:][^\n]*?(波胆|大小|独赢|让球|角球)", chunk)
        if im: bet["bet_kind"] = im.group(1)
        om = re.search(r"@(\d{1,2}\.\d{2})", chunk)
        if om: bet["bet_odds"] = float(om.group(1))
        # 联赛 (排除投注项关键词泄漏)
        lm = re.search(r"\[足球\]([^\n]{2,30})", chunk)
        if lm:
            lg = lm.group(1).strip()[:30]
            if not re.search(r"波胆|大小|独赢|让球|角球|全场", lg):
                bet["league"] = lg
        # 投注额/结算
        st = re.search(r"投注额[：:]\s*([\d,]+\.\d+)", chunk)
        if st: bet["stake"] = float(st.group(1).replace(",", ""))
        se = re.search(r"结算[：:]\s*(-?[\d,]+\.\d+)", chunk)
        if se: bet["settle"] = float(se.group(1).replace(",", ""))
        bets.append(bet)
    return bets

def main():
    manifest = json.load(open(STATS, encoding="utf-8"))["manifest"]
    out = {"A_bill": [], "B_live_odds": [], "E_odds_trend": []}
    for item in manifest:
        t = item["type"]
        if t not in out: continue
        txts = load_txts(item["file"])
        if not txts: continue
        try:
            if t == "A_bill": recs = parse_bill(txts, item["file"])
            elif t == "B_live_odds": recs = [parse_live_odds(txts, item["file"])]
            else: recs = [parse_odds_trend(txts, item["file"])]
            out[t].extend(recs)
        except Exception as e:
            print(f"  [ERR] {t} {item['file']}: {e}")
    for t, recs in out.items():
        with open(os.path.join(OUT, t + ".jsonl"), "w", encoding="utf-8") as fp:
            for r in recs: fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("=== v2 解析完成 ===")
    for t, recs in out.items():
        print(f"  {t:14s} → {len(recs):4d} 条")

if __name__ == "__main__":
    main()
