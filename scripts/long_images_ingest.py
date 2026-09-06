# -*- coding: utf-8 -*-
"""
long_images_ingest.py — 从 D:/Architecture/long/ 提取 iPhone 博彩 App 截图的结构化数据.

设计目标
========
1. 增量: 已处理路径跳过(UNIQUE on path)
2. 失败可见: issues 表记录 parse/ocr 异常, 不静默吞
3. 解析分版型: settlement / live_odds / cs_grid / other, 不同版型不同字段
4. DB 输出: D:/Architecture/data/long_images.db (images + image_odds + issues 三表)
5. 可重入: --reset 清空, --limit N 调试, --skip-classify 跳过版型判定

依赖
====
- paddleocr + paddlepaddle + opencv-python-headless (managed venv: ocr)
- 不用 sklearn / pandas, 仅 sqlite3 + re + json

CLI
===
  python long_images_ingest.py --reset
  python long_images_ingest.py --limit 5
  python long_images_ingest.py                # 全量
"""
from __future__ import annotations
import argparse, json, os, re, sqlite3, sys, time, hashlib
from pathlib import Path
from typing import Any, Iterable

LONG_DIR = Path("D:/Architecture/long")
DB_PATH  = Path("D:/Architecture/data/long_images.db")

# ============== DB schema ==============
SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  path          TEXT UNIQUE NOT NULL,
  filename      TEXT NOT NULL,
  ext           TEXT,
  size_bytes    INTEGER,
  mtime         TEXT,
  captured_at   TEXT,                 -- 从文件名解析 (YYYY_MM_DD_HH_MM_SS)
  page_type     TEXT,                 -- settlement / live_odds / cs_grid / other
  parse_status  TEXT,                 -- ok / partial / failed
  confidence_avg REAL,
  league        TEXT,
  match_date    TEXT,                 -- 推断的比赛日期 (YYYY-MM-DD)
  home_team     TEXT,
  away_team     TEXT,
  home_score    INTEGER,
  away_score    INTEGER,
  half_home     INTEGER,
  half_away     INTEGER,
  match_minute  INTEGER,
  win_loss      TEXT,                 -- settlement 推断: 赢/输/平 (OCR 红色"赢"字不可读, 用 payout vs stake 推)
  raw_ocr_json  TEXT,                 -- PaddleOCR 完整结果
  parsed_json   TEXT,                 -- 解析出的结构化字段
  ocr_ms        INTEGER,              -- OCR 耗时 ms
  processed_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_images_page_type ON images(page_type);
CREATE INDEX IF NOT EXISTS idx_images_match_date ON images(match_date);
CREATE INDEX IF NOT EXISTS idx_images_home_away ON images(home_team, away_team);

CREATE TABLE IF NOT EXISTS image_odds (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  image_id  INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  market    TEXT,                     -- 1X2 / AH / OU / CS / CORNER / EXTRA / SETTLEMENT
  line      TEXT,                     -- 盘口线 (4 / 0.5/1 / 4/4.5 / 1-0)
  selection TEXT,                     -- H/D/A / over/under / 具体比分 / 主/客 / 大/小
  odds      REAL,
  raw_text  TEXT                      -- OCR 原文(便于复核)
);
CREATE INDEX IF NOT EXISTS idx_image_odds_image_id ON image_odds(image_id);
CREATE INDEX IF NOT EXISTS idx_image_odds_market ON image_odds(market);

CREATE TABLE IF NOT EXISTS issues (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  image_id  INTEGER REFERENCES images(id) ON DELETE CASCADE,
  severity  TEXT,                     -- warn / error
  stage     TEXT,                     -- ocr / classify / parse / insert
  message   TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
"""

# 幂等迁移: 老库升级时补列 (不破坏数据)
MIGRATIONS = [
    "ALTER TABLE images ADD COLUMN win_loss TEXT",
]

# ============== OCR 初始化 (懒加载, 用 RapidOCR ONNX 版) ==============
# 选 RapidOCR 的原因: paddlepaddle 3.3.1 在 Windows + 当前 CPU 触发 OneDNN 静态图 bug
# (ConvertPirAttribute2RuntimeAttribute). RapidOCR 走 ONNX Runtime, 跨平台稳, 接口与
# PaddleOCR 2.x 兼容 (result = [[box, text, conf], ...]).
_OCR = None
def get_ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        # params.rec_show_num 可以抑制冗长日志; 第一次运行会下载 ~50MB ONNX 模型到 ~/.rapidocr
        _OCR = RapidOCR()
    return _OCR

# ============== 文件名解析 ==============
FN_RE = re.compile(r"^(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_")
def parse_filename(path: Path):
    """返回 captured_at(ISO)、match_date(YYYY-MM-DD), 或 (None, None)."""
    m = FN_RE.match(path.name)
    if not m:
        return None, None
    y, mo, d, h, mi, s = m.groups()
    iso = f"{y}-{mo}-{d}T{h}:{mi}:{s}"
    return iso, f"{y}-{mo}-{d}"

# ============== 分类 (基于 OCR 文本关键词) ==============
KW_SETTLEMENT = ["投注单号", "结算时间", "投注额", "注单状态", "已结注单", "投注成功", "投注失败", "结果比分"]
KW_LIVE       = ["上半时", "上半场", "全场大小", "全场独赢", "全场让球", "角球大小", "附加盘", "让球&大小", "大", "小"]
KW_CS_GRID    = ["全场平局", "半场平局", "即将开赛", "今日", "波胆", "全场比分"]
KW_LANDING    = ["全部", "英超", "德甲", "西甲", "意甲", "热门", "收藏", "已结注单", "未结注单"]

def classify_page(text_joined: str) -> str:
    s = KW_SETTLEMENT; l = KW_LIVE; c = KW_CS_GRID; ln = KW_LANDING
    # 优先判定: 同时出现 3+ 个结算关键字 且 没有 in-play 时间标记 -> settlement
    s_hit = sum(1 for k in s if k in text_joined)
    has_live_time = bool(re.search(r"上半[时场]\s*\d{1,2}[:：]\s*\d{2}", text_joined)) or "上半场" in text_joined
    l_hit = sum(1 for k in l if k in text_joined)
    c_hit = sum(1 for k in c if k in text_joined)
    if s_hit >= 4 and not has_live_time:
        return "settlement"
    if c_hit >= 2 and ("1-0" in text_joined or "0-0" in text_joined):
        return "cs_grid"
    if l_hit >= 3 and (has_live_time or re.search(r"\d{1,2}-\d{1,2}\b", text_joined)):
        return "live_odds"
    if sum(1 for k in ln if k in text_joined) >= 4:
        return "landing"
    return "other"

# ============== 解析: settlement ==============
SETTLEMENT_RE = {
    "bet_id":      re.compile(r"投注单号[:：]?\s*(\d{15,20})"),
    "settle_time": re.compile(r"结算时间[:：]?\s*([\d\-:\s(]{8,40}欧洲盘)"),
    "league":      re.compile(r"\[[^]]{2,30}?\]([^\n\r]{2,40})"),
    "result":      re.compile(r"全场比分\s*(\d{1,2})\s*[-:]\s*(\d{1,2})"),
    "market_label":re.compile(r"投注项[:：]?\s*(\[[^]]{2,30}?\][^\n\r]{2,30})"),
    "stake":       re.compile(r"投注额[:：]?\s*([\d.]+)\s*元"),
    "payout":      re.compile(r"结算[:：]?\s*([\d.]+)\s*元"),
    "status":      re.compile(r"注单状态[:：]?\s*(\S{2,8})"),
}
def _infer_win_loss(parsed: dict) -> str | None:
    try:
        s, p = float(parsed.get("stake", 0)), float(parsed.get("payout", 0))
    except (TypeError, ValueError):
        return None
    if s <= 0: return None
    if p > s: return "赢"
    if p < s: return "输"
    return "平"

NAV_TOKENS = {
    # 顶部/底部导航/tab
    "今日","昨日","近7日","自定义","时间","日间","夜间","专业版","新手版","热门","全部",
    "英超","德甲","西甲","意甲","法甲","葡超","俄超","荷甲","美职","墨超",
    "进行中","即将开赛","已开赛","未开赛","收起","刷新",
    "赛果查询","设置菜单","未结注单","已结注单","赛果","菜单",
    "主","客","大","小","和局","主胜","客胜","VS","vs",
    "投注","赛况","首发","前瞻","投注单","角球","特色组合","波胆","15分钟",
    "上半","下半","全场","半场","上半场","下半场",
    "总投注单数","总投注额","总输赢","百家乐","等你来赢","暂无主播解说",
    "真人主播","视频直播","动画直播","主播","收藏","进球","红牌","黄牌","加时","点球",
    "无网络","永久禁言中","永结禁言",
    "进行","进行中","整","半","完","未","已",
    # 结算单标签 (含/不含冒号)
    "结果比分","投注单号","结算时间","注单状态","投注项","全场比分","投注额","结算","输/赢",
    "投注成功","投注失败","结果","分","比","欧洲盘",
    "结果比分：","投注单号：","结算时间：","注单状态：","投注项：","全场比分：","投注额：","结算：","输/赢：",
    # OCR 误读 / 切碎
    "空进行","空进","空进有","进有",
    # 滚球/导航其它
    "串关","冠军","滚球","早盘","加时赛","点球大战","让球&大小","所有投注","包网",
    "足球","篮球","电竞","网球","VR体育","电竞赛事","热门赛事",
    "赔率","水位","水位变化","实时","暂停",
    # 单字/单数字
    "0","1","2","3","4","5","6","7","8","9",
}

def find_team_pair_by_coords(lines: list[dict]) -> tuple[str|None, str|None]:
    """按坐标配对 settlement/live_odds 团队: 同 y 附近, x 距离 200-600, 长度 2-15, 含中文/字母."""
    cands = []
    for ln in lines:
        t = ln["text"].strip()
        if not (3 <= len(t) <= 15): continue   # 团队至少 3 字符 (排除 足球/VR体育)
        if t in NAV_TOKENS: continue
        if not re.search(r"[\u4e00-\u9fa5A-Za-z]", t): continue
        if re.fullmatch(r"\d+", t): continue
        if re.fullmatch(r"[\d\-\.\:/]+", t): continue
        if t.endswith("元") or t.endswith("元)") or t.endswith(")"): continue
        if re.search(r"^(上半|下半|全场|半场|让球|大小|平局|进球|角球|波胆|红牌|黄牌|加时|点球|串关|冠军)", t): continue
        cands.append(ln)
    best = None
    OU_CELL = re.compile(r"^[大小]\s*\d+(\.\d+)?$")
    for i in range(len(cands)):
        for j in range(i+1, len(cands)):
            a, b = cands[i], cands[j]
            ta, tb = a["text"], b["text"]
            if OU_CELL.match(ta) and OU_CELL.match(tb): continue
            if ta in NAV_TOKENS or tb in NAV_TOKENS: continue
            ax = (a["bbox"][0][0]+a["bbox"][2][0])/2; ay = (a["bbox"][0][1]+a["bbox"][2][1])/2
            bx = (b["bbox"][0][0]+b["bbox"][2][0])/2; by = (b["bbox"][0][1]+b["bbox"][2][1])/2
            dx, dy = abs(ax-bx), abs(ay-by)
            if 200 <= dx <= 700 and dy < 30:
                # 优先: dx 最小 (横向最近的一对, 通常是同行的 team labels)
                if best is None or dx < best[4]:
                    best = (ax, ta, bx, tb, dx)
    if not best: return None, None
    ax, at, bx, bt, _ = best
    if ax < bx: return at, bt
    return bt, at

def parse_settlement(text: str, lines: list[dict]) -> dict:
    out = {}
    for k, rx in SETTLEMENT_RE.items():
        m = rx.search(text)
        if m:
            if k == "result":
                out["home_score"] = int(m.group(1))
                out["away_score"] = int(m.group(2))
            else:
                out[k] = m.group(1).strip()
    # 团队配对 (按坐标, 已过滤 NAV)
    h, a = find_team_pair_by_coords(lines)
    if h: out["home_team"] = h
    if a: out["away_team"] = a
    # 输赢推断 (用 payout vs stake; OCR 把"赢"字读成乱七八糟字)
    inferred = _infer_win_loss(out)
    if inferred: out["win_loss"] = inferred
    return out

# ============== 解析: live_odds ==============
SCORE_RE = re.compile(r"(\d{1,2})\s*[-:]\s*(\d{1,2})")
MIN_RE   = re.compile(r"上半[时场]\s*(\d{1,2})[:：]\s*(\d{2})")
ODDS_RE  = re.compile(r"^[0-9]+(\.[0-9]+)?$|^[▲▼][0-9]+(\.[0-9]+)?$")
LEAGUE_HINT = ["联赛", "杯", "赛", "League", "Cup"]

def find_top_team_lines(lines: list[dict], score_box: dict | None) -> tuple[str|None, str|None]:
    """根据 bbox 找位于 score 左右两侧的团队标签 (live_odds 页面版型: 上方团队, 中间比分, 下方半场信息)."""
    if not score_box: return None, None
    sx = (score_box["x1"] + score_box["x2"]) / 2
    sy = (score_box["y1"] + score_box["y2"]) / 2
    home, away = None, None
    for ln in lines:
        bb = ln["bbox"]; cx = (bb[0][0] + bb[2][0]) / 2; cy = (bb[0][1] + bb[2][1]) / 2
        # 与 score 同 y 区域, x 偏离
        if abs(cy - sy) < 80 and abs(cx - sx) > 30:
            t = ln["text"].strip()
            if 2 <= len(t) <= 20 and not t.isdigit() and t not in ("VS", "vs"):
                if cx < sx and home is None: home = t
                elif cx > sx and away is None: away = t
    return home, away

def parse_live_odds(text: str, lines: list[dict]) -> dict:
    out = {}
    # 联赛: 顶部居中, 排除 NAV token
    cand = []
    for ln in lines:
        t = ln["text"].strip()
        if t in NAV_TOKENS: continue
        if any(k in t for k in LEAGUE_HINT) and 2 <= len(t) <= 20:
            y = ln["bbox"][0][1]
            cand.append((y, t))
    if cand:
        cand.sort()
        out["league"] = cand[0][1]
    # 比分 + 半场比分: 找最大的 X-Y 模式
    scores = []
    for m in SCORE_RE.finditer(text):
        try:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 <= a <= 20 and 0 <= b <= 20 and not (a == 0 and b == 0):
                scores.append((a, b, m.start()))
        except Exception:
            pass
    if scores:
        big = [s for s in scores if s[0] + s[1] > 0]
        if big:
            a, b, _ = big[0]
            out["home_score"], out["away_score"] = a, b
    # 半场: 含"半场"附近找 X-Y
    half = re.search(r"半场[^\d]{0,8}(\d{1,2})\s*[-:]\s*(\d{1,2})", text)
    if half:
        out["half_home"] = int(half.group(1))
        out["half_away"] = int(half.group(2))
    # 分钟
    mm = MIN_RE.search(text)
    if mm:
        out["match_minute"] = int(mm.group(1)) * 60 + int(mm.group(2))
    # 球队: 优先用坐标配对, 排除 NAV
    h, a = find_team_pair_by_coords(lines)
    if h: out["home_team"] = h
    if a: out["away_team"] = a
    return out

# ============== 解析: cs_grid ==============
def parse_cs_grid(text: str, lines: list[dict]) -> dict:
    """抽取 1X2 + 让球 + 大小 + 全部波胆赔率 (格式: '1-0\\n9.60' 等)."""
    out = {}
    # 联赛 / 球队: 通常顶部有"联赛名"和"主队 VS 客队"
    ml = re.search(r"([\u4e00-\u9fa5A-Za-z0-9·\s]{2,15}联赛|[\u4e00-\u9fa5A-Za-z0-9·\s]{2,15}杯)", text)
    if ml: out["league"] = ml.group(1).strip()
    mv = re.search(r"([\u4e00-\u9fa5A-Za-z0-9·\s]{2,20})\s*VS\s*([\u4e00-\u9fa5A-Za-z0-9·\s]{2,20})", text)
    if mv:
        out["home_team"] = mv.group(1).strip()
        out["away_team"] = mv.group(2).strip()
    return out

# ============== 抽取 odds cells (按 market 段落) ==============
MARKET_LABELS = ["全场大小", "全场独赢", "全场让球", "上半场大小", "上半场独赢", "上半场让球",
                 "半场大小", "半场独赢", "半场让球", "角球大小", "上半场角球大小",
                 "全场大小-附加盘", "全场让球-附加盘", "全场平局", "半场平局"]
def extract_market_odds(text: str, lines: list[dict]) -> list[dict]:
    """按 Y 坐标分组, 每个 market section 抽取 (line, selection, odds)."""
    out = []
    if not lines: return out
    # 按 Y 排序
    sorted_lines = sorted(lines, key=lambda x: x["bbox"][0][1])
    # 找到所有 market label 的 bbox
    sections = []
    for i, ln in enumerate(sorted_lines):
        for ml in MARKET_LABELS:
            if ln["text"].strip() == ml:
                y = ln["bbox"][0][1]
                sections.append((y, ml, i))
                break
    sections.sort()
    # 段间: 对每段从 label 向下到下一段顶部 / 80 行
    for idx, (y, ml, li) in enumerate(sections):
        next_y = sections[idx+1][0] if idx+1 < len(sections) else 10**9
        # 收集段内
        seg = []
        for j in range(li+1, len(sorted_lines)):
            ly = sorted_lines[j]["bbox"][0][1]
            if ly > next_y: break
            if ly - y > 220: break   # 段最长 ~220 px
            seg.append(sorted_lines[j])
        if not seg: continue
        # 在段内抽取: line (文本) + odds (数字) 配对
        # 启发: 一行是 line label (如 "4" / "+0.5/1" / "4/4.5"), 一行是 odds (1.90)
        line_text = None
        odds_candidates = []
        for s in seg:
            t = s["text"].strip()
            if re.fullmatch(r"[+\-]?\d+(\.\d+)?(/\d+(\.\d+)?)?", t) and not t.startswith("▲") and not t.startswith("▼"):
                # 可能是 line (含 /) 或 odds. 含 / 一定是 line, 否则尝试 float
                if "/" in t:
                    line_text = t
                else:
                    try:
                        v = float(t)
                    except ValueError:
                        continue
                    if v >= 1.01:
                        odds_candidates.append((s["bbox"][0][1], v, t))
                    else:
                        line_text = t
            elif t in ("大", "小", "主", "客", "主胜", "客胜", "和局"):
                odds_candidates.append((s["bbox"][0][1], None, t))
            elif re.fullmatch(r"\d+(\.\d+)?", t):
                try:
                    v = float(t)
                except ValueError:
                    continue
                if v >= 1.01: odds_candidates.append((s["bbox"][0][1], v, t))
                else: line_text = t
        # 简单落库: line_text + 每个 odds 都记一条
        market_code = {"全场大小":"OU","全场独赢":"1X2","全场让球":"AH",
                       "半场大小":"OU","半场独赢":"1X2","半场让球":"AH",
                       "上半场大小":"OU","上半场独赢":"1X2","上半场让球":"AH",
                       "角球大小":"CORNER","上半场角球大小":"CORNER",
                       "全场大小-附加盘":"OU","全场让球-附加盘":"AH",
                       "全场平局":"CS","半场平局":"CS"}.get(ml, "EXTRA")
        if not odds_candidates:
            out.append({"market_label": ml, "market_code": market_code, "line": line_text,
                        "selection": None, "odds": None, "raw": " | ".join(s["text"] for s in seg)})
        else:
            for y2, odds_v, raw in odds_candidates:
                sel = raw
                if market_code == "OU" or market_code == "CORNER":
                    if raw in ("大","小"): sel = "over" if raw=="大" else "under"
                if market_code == "1X2":
                    if raw in ("主胜","主"): sel = "H"
                    elif raw in ("客胜","客"): sel = "A"
                    elif raw == "和局": sel = "D"
                if market_code == "AH":
                    if raw in ("主","客"): sel = "H" if raw=="主" else "A"
                out.append({"market_label": ml, "market_code": market_code, "line": line_text,
                            "selection": sel, "odds": odds_v, "raw": raw})
    return out

# ============== OCR + 主流程 ==============
def ocr_one(ocr, img_path: Path) -> tuple[list[dict], float, int]:
    """返回 (lines, avg_conf, ms). lines = [{text, conf, bbox:[[x,y]x4]}, ...]
    RapidOCR 引擎: engine(path) -> (result, elapse). result = [[box, text, conf_str], ...]
    """
    t0 = time.time()
    try:
        result, elapse = ocr(str(img_path))
    except Exception as e:
        raise RuntimeError(f"RapidOCR failed: {e}") from e
    ms = int((time.time()-t0)*1000)
    lines = []
    if not result: return lines, 0.0, ms
    confs = []
    for item in result:
        try:
            box, txt, conf_raw = item[0], item[1], item[2]
            conf = float(conf_raw) if conf_raw is not None else 0.0
        except Exception:
            continue
        lines.append({"text": txt, "conf": conf, "bbox": [[float(p[0]), float(p[1])] for p in box]})
        confs.append(conf)
    avg = sum(confs)/len(confs) if confs else 0.0
    return lines, avg, ms

def process_one(conn: sqlite3.Connection, ocr, img_path: Path) -> None:
    cur = conn.cursor()
    # skip if already processed
    row = cur.execute("SELECT id, parse_status FROM images WHERE path=?", (str(img_path),)).fetchone()
    if row:
        return
    try:
        lines, avg_conf, ms = ocr_one(ocr, img_path)
    except Exception as e:
        # record image with failure
        stat = img_path.stat()
        cur.execute("""INSERT INTO images(path,filename,ext,size_bytes,mtime,page_type,parse_status,confidence_avg,raw_ocr_json)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (str(img_path), img_path.name, img_path.suffix.lower(), stat.st_size,
                     time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stat.st_mtime)),
                     "failed", "failed", 0.0, json.dumps({"error": str(e)}, ensure_ascii=False)))
        img_id = cur.lastrowid
        cur.execute("INSERT INTO issues(image_id,severity,stage,message) VALUES(?,?,?,?)",
                    (img_id, "error", "ocr", str(e)[:500]))
        conn.commit()
        return
    text_joined = "\n".join(l["text"] for l in lines)
    page_type = classify_page(text_joined)
    parsed = {}
    if page_type == "settlement":
        parsed = parse_settlement(text_joined, lines)
        # settlement also has odds (the "settlement amount") - log as 1X2-equivalent record
    elif page_type == "live_odds":
        parsed = parse_live_odds(text_joined, lines)
    elif page_type == "cs_grid":
        parsed = parse_cs_grid(text_joined, lines)
    # extract market odds regardless for live_odds / cs_grid
    market_odds = extract_market_odds(text_joined, lines) if page_type in ("live_odds", "cs_grid") else []
    # file stat
    stat = img_path.stat()
    iso, match_date = parse_filename(img_path)
    # decide parse_status
    have_team = bool(parsed.get("home_team") and parsed.get("away_team"))
    have_score = ("home_score" in parsed and "away_score" in parsed)
    if page_type in ("live_odds","cs_grid") and have_team and have_score:
        status = "ok"
    elif page_type == "settlement" and parsed.get("bet_id") and parsed.get("stake"):
        status = "ok"
    elif page_type in ("other","landing"):
        status = "skipped"
    else:
        status = "partial"
    # insert image
    cur.execute("""
        INSERT INTO images(path,filename,ext,size_bytes,mtime,captured_at,page_type,parse_status,
                           confidence_avg,league,match_date,home_team,away_team,
                           home_score,away_score,half_home,half_away,match_minute,win_loss,
                           raw_ocr_json,parsed_json,ocr_ms)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (str(img_path), img_path.name, img_path.suffix.lower(), stat.st_size,
          time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stat.st_mtime)),
          iso, page_type, status, round(avg_conf,4),
          parsed.get("league"), match_date, parsed.get("home_team"), parsed.get("away_team"),
          parsed.get("home_score"), parsed.get("away_score"),
          parsed.get("half_home"), parsed.get("half_away"),
          parsed.get("match_minute"),
          parsed.get("win_loss"),
          json.dumps({"raw_lines": [(l["text"], round(l["conf"],3)) for l in lines]}, ensure_ascii=False),
          json.dumps({k: parsed[k] for k in parsed if k not in ("raw_lines",)}, ensure_ascii=False),
          ms))
    img_id = cur.lastrowid
    # insert market odds
    for mo in market_odds:
        cur.execute("""INSERT INTO image_odds(image_id,market,line,selection,odds,raw_text)
                       VALUES(?,?,?,?,?,?)""",
                    (img_id, mo["market_code"], mo["line"], mo["selection"], mo["odds"],
                     f'{mo["market_label"]} | {mo.get("raw","")}'))
    # insert settlement as 1 record in image_odds for traceability
    if page_type == "settlement" and parsed.get("market_label"):
        cur.execute("""INSERT INTO image_odds(image_id,market,line,selection,odds,raw_text)
                       VALUES(?,?,?,?,?,?)""",
                    (img_id, "SETTLEMENT", None, parsed.get("market_label",""),
                     parsed.get("payout") and float(parsed["payout"]), None))
    # issues
    if status == "partial":
        miss = [k for k in ("home_team","away_team","home_score","away_score") if k not in parsed]
        cur.execute("INSERT INTO issues(image_id,severity,stage,message) VALUES(?,?,?,?)",
                    (img_id, "warn", "parse", f"page_type={page_type} missing={miss}"))
    if avg_conf < 0.7 and lines:
        cur.execute("INSERT INTO issues(image_id,severity,stage,message) VALUES(?,?,?,?)",
                    (img_id, "warn", "ocr", f"low_conf={avg_conf:.3f}"))
    conn.commit()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空 images/image_odds/issues 三表")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 张 (0=全量)")
    ap.add_argument("--src", default=str(LONG_DIR))
    ap.add_argument("--db",  default=str(DB_PATH))
    args = ap.parse_args()
    src = Path(args.src); dbp = Path(args.db)
    dbp.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(dbp))
    conn.executescript(SCHEMA)
    # 幂等迁移
    for mig in MIGRATIONS:
        try:
            conn.execute(mig)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower() and "no such table" not in str(e).lower():
                raise
    conn.commit()
    if args.reset:
        conn.execute("DELETE FROM image_odds"); conn.execute("DELETE FROM issues"); conn.execute("DELETE FROM images")
        conn.commit()
        print("[reset] all rows cleared")
    exts = {".png",".jpg",".jpeg",".bmp",".webp"}
    files = sorted([p for p in src.iterdir() if p.is_file() and p.suffix.lower() in exts])
    if args.limit: files = files[:args.limit]
    print(f"[scan] {len(files)} files in {src}")
    if not files: return
    print(f"[ocr] initializing PaddleOCR (ch) ...", flush=True)
    ocr = get_ocr()
    print(f"[ocr] ready. processing {len(files)} files ...", flush=True)
    t0 = time.time()
    n_ok = n_part = n_fail = n_skip = 0
    for i, p in enumerate(files, 1):
        try:
            process_one(conn, ocr, p)
            row = conn.execute("SELECT parse_status FROM images WHERE path=?", (str(p),)).fetchone()
            st = row[0] if row else "?"
        except Exception as e:
            n_fail += 1
            print(f"[{i}/{len(files)}] {p.name} EXC: {e}", flush=True)
            continue
        if st == "ok": n_ok += 1
        elif st == "partial": n_part += 1
        elif st in ("failed","skipped"): n_skip += 1
        if i % 10 == 0 or i == len(files):
            dt = time.time()-t0
            rate = i/dt if dt>0 else 0
            eta = (len(files)-i)/rate if rate>0 else 0
            print(f"[{i}/{len(files)}] ok={n_ok} partial={n_part} skip/fail={n_skip}  rate={rate:.2f}/s  eta={eta:.0f}s", flush=True)
    dt = time.time()-t0
    print(f"\n[done] {len(files)} files in {dt:.1f}s ({len(files)/dt:.2f}/s)  ok={n_ok} partial={n_part} skip/fail={n_skip}")
    print(f"[db]   {dbp}")
    # quick stats
    stats = {}
    for row in conn.execute("SELECT page_type, COUNT(*) FROM images GROUP BY page_type").fetchall():
        stats[row[0] or "null"] = row[1]
    print(f"[stats] {stats}")

if __name__ == "__main__":
    main()
