# -*- coding: utf-8 -*-
"""
long/ 截图结构化解析器 (基于已存盘 OCR JSON)。
解析三类核心数据:
  A_bill   投注账单 → 注单(联赛/队名/投注项/赔率/真实结果比分/输赢)  ← 监督标签源
  B_live_odds 滚球盘口 → 比赛盘口(联赛/队名/当前比分/让球/大小/独赢赔率/进行状态)
  E_odds_trend 走势页 → 必发资金面(主/和/客 交易量+指数+盈亏指数)

设计原则: 容忍 OCR 行序错乱, 用正则锚定字段而非依赖行序。
输出: data/long_features/ 下三个 JSONL, 每行一条结构化记录。
"""
import os, json, re, glob

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

# ── 日期从文件名提取 ──
def date_from(fname):
    m = re.match(r"(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})", fname)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}"
    return None

# ═══════════════════ A 账单: 提取每张图里的所有注单 ═══════════════════
def parse_bill(txts, fname):
    """一张账单图可能含多笔注单。用关键锚点切片提取。"""
    joined = "\n".join(txts)
    bets = []
    # 真实结果比分: "结果比分 (全场比分 2-0)" 或 "（全场比分1-0）"
    results = re.findall(r"全场比分[（(]?\s*(\d+)\s*[-—]\s*(\d+)\s*[)）]?", joined)
    # 输赢: "输/赢：赢" / "输/赢: 输"
    winlose = re.findall(r"输/赢[：:]\s*(赢|输|走|和)", joined)
    # 投注项+赔率: "全场波胆 1-0 @12.00" / "全场大小 ... @1.94"
    bet_items = re.findall(r"投注项[：:]\s*\[?足球?\]?[^\n]*?(波胆|大小|独赢|让球|角球)[^\n]*?(\d+-\d+)?\s*@?\s*(\d+\.\d+)?", joined)
    # 联赛: "[足球]XXX联赛"
    leagues = re.findall(r"\[足球\]([^\n]{2,30}?(?:联赛|锦标赛|杯赛|超级|甲级|乙级|英超|德甲|西甲|意甲)[^\n]*?)(?:结果比分|投注额|$)", joined)
    # 投注额 / 结算额
    stake = re.findall(r"投注额[：:]\s*([\d,]+\.\d+)", joined)
    settle = re.findall(r"结算[：:]\s*(-?[\d,]+\.\d+)", joined)
    # 投注单号
    billno = re.findall(r"投注单号[：:]\s*(\d{10,})", joined)

    n = max(len(results), len(winlose), len(bet_items), 1)
    for i in range(n):
        bet = {"source": fname, "date": date_from(fname), "bill_idx": i}
        if i < len(results):
            bet["result_home"], bet["result_away"] = int(results[i][0]), int(results[i][1])
            bet["result_total"] = bet["result_home"] + bet["result_away"]
            s = bet["result_home"] - bet["result_away"]
            bet["result_1x2"] = "H" if s > 0 else ("A" if s < 0 else "D")
        if i < len(winlose):
            bet["bet_outcome"] = winlose[i]
        if i < len(bet_items):
            kind, score, odds = bet_items[i]
            bet["bet_kind"] = kind
            if score: bet["bet_pick_score"] = score
            if odds: bet["bet_odds"] = float(odds)
        if i < len(leagues): bet["league"] = leagues[i].strip()
        if i < len(stake): bet["stake"] = float(stake[i].replace(",", ""))
        if i < len(settle): bet["settle"] = float(settle[i].replace(",", ""))
        if i < len(billno): bet["bill_no"] = billno[i]
        bets.append(bet)
    return bets

# ═══════════════════ B 滚球盘口 ═══════════════════
TEAM_NOISE = {"全场独赢", "全场让球", "全场大小", "和局", "主胜", "客胜", "视频直播",
              "动画直播", "真人主播", "投注", "赛况", "首发", "前瞻", "波胆", "角球",
              "让球&大小", "进球", "特色组合", "全部", "未结算", "已结算", "永久禁言中"}

def is_team(s):
    """启发式判断一个 OCR 行是不是队名(含中文/字母, 非纯数字盘口赔率)。"""
    s = s.strip()
    if not s or len(s) > 20: return False
    if s in TEAM_NOISE: return False
    if re.fullmatch(r"[\d.+\-/ ]+", s): return False  # 纯数字/盘口/赔率
    if re.search(r"[+\-]\d", s) and len(s) < 8: return False  # 让球盘 "主 -0/0.5"
    if re.fullmatch(r"[大][\d./]+|[小][\d./]+", s): return False  # 大小盘
    return bool(re.search(r"[\u4e00-\u9fa5A-Za-z]", s))

def parse_live_odds(txts, fname):
    rec = {"source": fname, "date": date_from(fname)}
    joined = "\n".join(txts)
    # 进行状态/时间
    m = re.search(r"(上半场|下半场|中场|即将开赛|未开场)(\d{1,3})?[:：]?(\d{1,2})?", joined)
    if m:
        rec["status"] = m.group(1)
        if m.group(2): rec["match_minute"] = int(m.group(2))
    # 联赛 (第一个含联赛关键词的行, 在队名之前)
    lm = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,25}?(?:联赛|锦标赛|杯赛|超级联赛|甲级|乙级|英超|德甲|西甲|意甲|甲|超|冠|联盟)[\u4e00-\u9fa5A-Za-z0-9]*)", joined)
    if lm: rec["league"] = lm.group(1).strip()
    # 当前比分 "4 - 3" / "2:1" / "0-0"
    bm = re.search(r"(\d+)\s*[-:：]\s*(\d+)", joined)
    if bm:
        rec["score_home"], rec["score_away"] = int(bm.group(1)), int(bm.group(2))
        rec["score_total"] = rec["score_home"] + rec["score_away"]
    # 让球盘 "主 -0/0.5" "客+0/0.5" / "+0/0.5" "-0.5/1"
    hm = re.findall(r"([+\-]\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)", joined)
    handicap_vals = []
    for h in hm:
        if "/" in h or re.match(r"[+\-]0\.\d|0/0\.5", h):
            # 取斜杠第一个值或单值
            base = h.split("/")[0]
            try: handicap_vals.append(float(base))
            except: pass
    if handicap_vals:
        rec["handicap_home"] = handicap_vals[0]
        if len(handicap_vals) > 1: rec["handicap_away"] = handicap_vals[1]
    # 大小球盘 "大 7.5" "小3" "大3.5/4"
    ou = re.findall(r"大\s?(\d+(?:\.\d+)?(?:/\d+)?)", joined)
    if ou: rec["ou_line"] = ou[0]
    # 赔率 (所有形如 X.XX 的数, 排除盘口)
    odds_all = [float(x) for x in re.findall(r"(?<![\d.\-])(\d{1,2}\.\d{2})(?![\d])", joined)]
    # 独赢三赔: 通常在 "全场独赢" 后最先出现且数值跨度过大(主低客高或反之)
    # 让球两赔, 大小两赔
    rec["all_odds"] = odds_all[:12]  # 保留前12个供后续清洗
    # 队名 (前两个非噪声的中文/字母行)
    teams = [t for t in txts if is_team(t)]
    if len(teams) >= 2:
        rec["home"] = teams[0]
        rec["away"] = teams[1]
    return rec

# ═══════════════════ E 走势/必发资金面 ═══════════════════
def parse_odds_trend(txts, fname):
    rec = {"source": fname, "date": date_from(fname)}
    joined = "\n".join(txts)
    # 联赛 + 队名: "挪超" "萨普斯堡" "罗森博格"
    lm = re.search(r"(挪超|瑞典超|波兰甲|日本|澳超|韩K|美职|墨西超|英超|德甲|西甲|意甲|法甲|中超|欧冠|英冠)[\u4e00-\u9fa5A-Z]*", joined)
    if lm: rec["league"] = lm.group(0)
    # 必发交易量: "主队 146,851 2.6" → 交易量+指数
    # 模式: 主队/和局/客队 后跟 数字(交易量) 再跟 小数(指数=赔率)
    def extract_role(role):
        m = re.search(role + r"[^\n]*?([\d,]{3,})\s*\n?\s*(\d+\.\d+)", joined)
        if m:
            return {"volume": int(m.group(1).replace(",", "")), "bf_index_odds": float(m.group(2))}
        return {}
    rec["home_betfair"] = extract_role("主队")
    rec["draw_betfair"] = extract_role("和局")
    rec["away_betfair"] = extract_role("客队")
    # 盈亏指数: "主队 -65,778 -22"
    pm = re.findall(r"(主队|和局|客队)[^\n]*?(-?[\d,]+)\s*\n?\s*(-?\d+)", joined)
    pnl = {}
    for role, amt, idx in pm:
        pnl[role] = {"pnl": int(amt.replace(",", "")), "pnl_index": int(idx)}
    rec["pnl"] = pnl
    # 距离开赛倒计时 (判断是赛前还是赛中)
    cm = re.search(r"距离开赛\s*(\d{2}:\d{2}:\d{2})", joined)
    if cm:
        rec["pre_match_countdown"] = cm.group(1)
        rec["is_prematch"] = True
    else:
        rec["is_prematch"] = False
    return rec

def main():
    manifest = json.load(open(STATS, encoding="utf-8"))["manifest"]
    out = {"A_bill": [], "B_live_odds": [], "E_odds_trend": []}
    parse_fail = {"A_bill": 0, "B_live_odds": 0, "E_odds_trend": 0}
    for item in manifest:
        t = item["type"]
        if t not in out: continue
        txts = load_txts(item["file"])
        if not txts: continue
        try:
            if t == "A_bill":
                recs = parse_bill(txts, item["file"])
            elif t == "B_live_odds":
                recs = [parse_live_odds(txts, item["file"])]
            elif t == "E_odds_trend":
                recs = [parse_odds_trend(txts, item["file"])]
            out[t].extend(recs)
        except Exception as e:
            parse_fail[t] += 1
            print(f"  [PARSE ERR] {t} {item['file']}: {e}")
    # 写 JSONL
    for t, recs in out.items():
        with open(os.path.join(OUT, t + ".jsonl"), "w", encoding="utf-8") as fp:
            for r in recs:
                fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("=== 解析完成 ===")
    for t, recs in out.items():
        print(f"  {t:14s} → {len(recs):4d} 条记录  (解析失败 {parse_fail[t]})")
    print(f"输出目录: {OUT}")

if __name__ == "__main__":
    main()
