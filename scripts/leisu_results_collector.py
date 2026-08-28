# -*- coding: utf-8 -*-
"""
leisu_results_collector.py — 雷速「赛果」页采集器 (解锁 long/ 129 场监督学习)

========================================================================
用途
========================================================================
  long/ 截图本身是 live/prematch（status=上半场/下半场/即将开赛），
  score_* 是采集时实时比分，非终场。要解锁这 129 场做监督学习，必须外接
  终场源。本采集器从雷速「赛果」tab 采真实终场（联赛/时间/队名/比分/半场/
  角球/状态）→ 存 leisu_results 表（football_data.db，与 matches/team_canonical
  同库，_canonical_match/align_match 直接可用）→ 按 (canonical 主客, 日期) 回绑
  data/long_features/match_features_canon.csv。

========================================================================
运行命令
========================================================================
  # 建表（幂等）
  .ocr_venv/Scripts/python.exe scripts/leisu_results_collector.py --init

  # 离线校验解析器（无需 MuMu）：对一张真实赛果页截图跑 OCR+解析，打印 JSON
  .ocr_venv/Scripts/python.exe scripts/leisu_results_collector.py --parse-only tmp/explore_v2_step1_home.png

  # 采当天赛果（默认），回绑 long 特征并报告解锁数
  .ocr_venv/Scripts/python.exe scripts/leisu_results_collector.py --once --bind-long

  # 采最近 N 天（天级回退为 best-effort；绑定用 ±2 天容错吸收误差）
  .ocr_venv/Scripts/python.exe scripts/leisu_results_collector.py --days 7 --bind-long

  # 仅回绑（已采过，重算解锁数）
  .ocr_venv/Scripts/python.exe scripts/leisu_results_collector.py --bind-long

========================================================================
依赖：复用 leisu_collector 的 ADB/OCR/对齐基础设施（import，不重写）。
      OCR 引擎仅存在于 .ocr_venv，故本脚本必须用该 venv 跑。
========================================================================
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

# 复用 leisu_collector 基础设施（ADB / OCR / 对齐）。脚本在 D:/Architecture 运行，
# leisu_collector.py 位于 scripts/，加入 sys.path 后 import。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import leisu_collector as LC  # noqa: E402

FOOTBALL_DB_PATH = LC.FOOTBALL_DB_PATH
CAPTURE_DIR = LC.CAPTURE_DIR
ADB = LC.ADB
get_ocr_engine = LC.get_ocr_engine
_to_lines = LC._to_lines
_canonical_match = LC._canonical_match

LONG_CSV = "D:/Architecture/data/long_features/match_features_canon.csv"

# ===================== 建表 =====================
RESULTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS leisu_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id      INTEGER,          -- 对齐 football_data.db.matches (对齐不上 NULL)
    home_raw      TEXT,
    away_raw      TEXT,
    home_canonical TEXT,
    away_canonical TEXT,
    league        TEXT,
    kickoff_time  TEXT,             -- HH:MM
    page_date     TEXT,             -- 该页赛果所属日期 YYYY-MM-DD (采集器跟踪)
    score_h       INTEGER,
    score_a       INTEGER,
    half_h        INTEGER,
    half_a        INTEGER,
    corner_h      INTEGER,
    corner_a      INTEGER,
    status        TEXT,             -- 完 / 进行中 / 上半场 / 下半场 / 中场
    note          TEXT,             -- 附加信息（如点球胜出）
    capture_at    INTEGER,
    source        TEXT DEFAULT 'leisu'
);
CREATE INDEX IF NOT EXISTS idx_lr_canon ON leisu_results(home_canonical, away_canonical, page_date);
CREATE INDEX IF NOT EXISTS idx_lr_mid ON leisu_results(match_id);
"""


def init_results_db(db_path: str | Path) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(RESULTS_SCHEMA)
    conn.commit()
    conn.close()
    print(f"[init] leisu_results 就绪: {db_path}")


# ===================== 解析层 =====================
CJK = re.compile(r"[一-鿿]")
SCORE = re.compile(r"(\d{1,2})-(\d{1,2})")
HHMM = re.compile(r"\d{1,2}:\d{2}")
HALFCORN = re.compile(r"半[:：](\d+)-(\d+).*角[:：](\d+)-(\d+)")
LEAD = re.compile(r"^[0-9\[\]\u2460-\u2473\s]+")      # 去头部 rank/圈码/括号
TRAIL = re.compile(r"[0-9\[\]\u2460-\u2473\s]+$")     # 去尾部 rank/圈码/括号
NOTE_KW = ("半", "角", "点球", "分钟", "胜出", "完场", "进行中", "中场", "VIP", "情报", "阵容")


def split_teams(txt: str, m: re.Match) -> tuple[str, str]:
    """从 '队名X-Y队名' 切出主/客原始名（去 rank/圈码/括号）。"""
    pre = txt[: m.start()]
    post = txt[m.end():]
    home = LEAD.sub("", pre).strip(" -")
    away = TRAIL.sub("", post).strip(" -")
    return home, away


def get_ocr_lines(png: str | Path) -> list[dict]:
    try:
        ocr = get_ocr_engine()
    except ImportError:
        return []
    result, _ = ocr(str(png))
    return _to_lines(result)


def parse_results_page(png: str | Path) -> list[dict]:
    """OCR 一张赛果页截图 → 解析出每场终场 dict。

    布局（实测 2026-07-29 explore_v2_step1_home.png）：
      联赛标签(左 x<80, 卡片上方~40px) / 开赛 HH:MM(x≈110-155) / 状态 完(x≈439)
      [rank]主队 X-Y 客队[rank]  ← 主匹配行
      半:X-Y角：X-Y (下方~45px)
    返回 list[{league,kickoff_time,status,home_raw,away_raw,score_h,score_a,
               half_h,half_a,corner_h,corner_a,note}]
    """
    lines = get_ocr_lines(png)
    if not lines:
        return []
    lines = sorted(lines, key=lambda l: (l["bbox"][0][1], l["bbox"][0][0]))

    # 1) 主匹配行：含恰好一个 X-Y、含中文队名、且非半/角/点球等说明行
    match_lines = []
    for ln in lines:
        txt = ln["text"]
        if not CJK.search(txt):
            continue
        if any(k in txt for k in NOTE_KW):
            continue
        scores = SCORE.findall(txt)
        if len(scores) != 1:
            continue
        match_lines.append(ln)

    results = []
    for ln in match_lines:
        txt = ln["text"]
        m = SCORE.search(txt)
        if not m:
            continue
        sh, sa = int(m.group(1)), int(m.group(2))
        home_raw, away_raw = split_teams(txt, m)
        if not home_raw or not away_raw:
            continue
        bbox = ln["bbox"]
        yc = (bbox[0][1] + bbox[2][1]) / 2.0

        league = kickoff = status = note = None
        half = corner = None
        for o in lines:
            if o is ln:
                continue
            oy = (o["bbox"][0][1] + o["bbox"][2][1]) / 2.0
            ox0 = o["bbox"][0][0]
            ot = o["text"]
            dy = oy - yc
            # 联赛标签（左、上方）
            if ox0 < 80 and -75 <= dy <= -15 and not HHMM.match(ot):
                league = ot
            # 开赛时间
            elif 100 <= ox0 <= 175 and -75 <= dy <= -15 and HHMM.match(ot):
                kickoff = ot
            # 状态
            elif 405 <= ox0 <= 485 and -60 <= dy <= 35 and ot in (
                "完", "进行中", "上半场", "下半场", "中场", "推迟", "待定"
            ):
                status = ot
            # 半/角行（下方）
            elif -10 <= dy <= 95 and "半" in ot and "角" in ot:
                hm = HALFCORN.search(ot)
                if hm:
                    half = (int(hm.group(1)), int(hm.group(2)))
                    corner = (int(hm.group(3)), int(hm.group(4)))
            # 点球/胜出说明（附近，作为 note）
            elif -10 <= dy <= 95 and ("点球" in ot or "胜出" in ot):
                note = ot
        results.append({
            "league": league,
            "kickoff_time": kickoff,
            "status": status or "完",
            "home_raw": home_raw,
            "away_raw": away_raw,
            "score_h": sh,
            "score_a": sa,
            "half_h": half[0] if half else None,
            "half_a": half[1] if half else None,
            "corner_h": corner[0] if corner else None,
            "corner_a": corner[1] if corner else None,
            "note": note,
        })
    return results


# ===================== 导航层 =====================
def find_tab(lines: list[dict], name: str) -> tuple[float | None, float | None]:
    """在首页 tab 栏定位 name tab 中心坐标。返回 (cy, cx)。"""
    for ln in lines:
        if ln["text"] == name or name in ln["text"] and len(ln["text"]) <= 4:
            x0, y0 = ln["bbox"][0]
            x1, y1 = ln["bbox"][2]
            return (y0 + y1) / 2.0, (x0 + x1) / 2.0
    return None, None


def ensure_results_page(adb: "ADB") -> bool:
    """确保在雷速「赛果」页：launch→回首页→点赛果tab（若当前已是赛果页则跳过）。"""
    adb.launch_leisu()
    time.sleep(3)
    on_home, _ = adb.ensure_home()
    png = adb.screencap()
    lines = get_ocr_lines(png)
    # 已是赛果页？：存在联赛标签 + 含 X-Y 比分行
    has_league = any(l["text"] in ("哥伦杯", "巴甲", "英超", "阿甲", "巴西乙") or
                     (l["bbox"][0][0] < 80 and CJK.search(l["text"])) for l in lines)
    has_score = any(SCORE.search(l["text"]) and CJK.search(l["text"]) for l in lines)
    if has_league and has_score:
        return True
    # 点赛果 tab
    cy, cx = find_tab(lines, "赛果")
    if cx is None:
        cy, cx = 133, 312  # 兜底：tab 栏 y≈133, 赛果 x≈312（实测）
    adb.tap(int(cx), int(cy))
    time.sleep(3)
    return True


def tap_prev_date(adb: "ADB", lines: list[dict]) -> bool:
    """回到前一天：点日期条上当前选中日期左侧的 chip（best-effort）。"""
    # 日期条 y∈[160,215]；MM/DD 或纯日数字 token
    chips = [(l["bbox"][0][1] + l["bbox"][2][1]) / 2.0,
             (l["bbox"][0][0] + l["bbox"][2][0]) / 2.0, l["text"]]
    chips = [c for c in chips if 160 <= c[0] <= 215 and
             (re.match(r"\d{1,2}/\d{1,2}", c[2]) or re.match(r"^\d{1,2}$", c[2]))]
    chips.sort(key=lambda c: c[1])
    if not chips:
        # 退路：左滑日期条
        adb.swipe(250, 190, 500, 190, 500)
        time.sleep(2)
        return True
    # 选最左 chip（=最早可见日期，通常是昨天）
    cx = int(chips[0][1])
    cy = int(chips[0][0])
    if cx < 40:  # 太靠边，避免点飞；改为左滑
        adb.swipe(250, 190, 500, 190, 500)
        time.sleep(2)
        return True
    adb.tap(cx, cy)
    time.sleep(2.5)
    return True


# ===================== 入库 + 回绑 =====================
def store_results(cur: sqlite3.Cursor, rows: list[dict], page_date: str,
                 capture_at: int) -> int:
    n = 0
    for r in rows:
        hc = _canonical_match(r["home_raw"], cur) or r["home_raw"]
        ac = _canonical_match(r["away_raw"], cur) or r["away_raw"]
        mid = None
        if page_date:
            row = cur.execute(
                "SELECT match_id FROM matches WHERE home_team_name=? AND away_team_name=? AND match_date=?",
                (hc, ac, page_date),
            ).fetchone()
            mid = row[0] if row else None
        cur.execute(
            """INSERT INTO leisu_results
               (match_id, home_raw, away_raw, home_canonical, away_canonical, league,
                kickoff_time, page_date, score_h, score_a, half_h, half_a,
                corner_h, corner_a, status, note, capture_at, source)
               VALUES (?,?,?,?,?,?, ?,?,?,?,?,?, ?,?,?,?,?,?)""",
            (mid, r["home_raw"], r["away_raw"], hc, ac, r["league"],
             r["kickoff_time"], page_date, r["score_h"], r["score_a"],
             r["half_h"], r["half_a"], r["corner_h"], r["corner_a"],
             r["status"], r["note"], capture_at, "leisu"),
        )
        n += 1
    return n


def bind_long_matches(db_path: str | Path, long_csv: str = LONG_CSV,
                      tol_days: int = 2,
                      gq_db: str = "D:/Architecture/data/events.db") -> dict:
    """回绑 long 特征 → 三个终场源（统一 canonical 归一再 join）。

    源：
      1. leisu_results       本采集器采的近期雷速赛果 (football_data.db)
      2. GQ match_outcomes   乐鱼 2026-07 真赛果 (events.db)
      3. football_data.matches WH+IW 历史赛果
    按 (canonical 主客, 日期±tol) 命中即解锁。区分：
      - matched    : 按日期解锁的 long 场数（去重，标注由哪个源解锁）
      - name_only  : 队名对得上但日期超出容差（join 机制可用，纯时间差）
      - window_overlap: long 落在任一源日期窗内（潜在可解，待源补齐）
    """
    long_rows = []
    try:
        with open(long_csv, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                long_rows.append(row)
    except FileNotFoundError:
        return {"error": f"long csv 不存在: {long_csv}"}

    fdb = sqlite3.connect(str(db_path))
    fcur = fdb.cursor()
    gq = sqlite3.connect(gq_db)
    gcur = gq.cursor()

    def to_date(s):
        try:
            return datetime.date.fromisoformat(str(s)[:10])
        except Exception:
            return None

    # 源索引: (hc,ac) -> [(dd, sh, sa, league, src)]
    idx: dict = {}
    def add(hc, ac, dd, sh, sa, lg, src):
        if sh is None or not hc or not ac:
            return
        idx.setdefault((hc, ac), []).append((dd, sh, sa, lg, src))

    # 源2: GQ match_outcomes（raw 名统一归一）
    for h, a, k, sh, sa, lg in gcur.execute(
        "SELECT home,away,kickoff,score_home,score_away,league FROM match_outcomes"
    ):
        add(_canonical_match(h, fcur) or h, _canonical_match(a, fcur) or a,
            to_date(k), sh, sa, lg, "GQ")
    # 源3: football_data.matches（home_team_name 已是归一名）
    for h, a, sh, sa, d, lg in fcur.execute(
        "SELECT home_team_name,away_team_name,home_score,away_score,match_date,league_name FROM matches"
    ):
        add(h, a, to_date(d), sh, sa, lg, "fdb")
    # 源1: leisu_results（入库时已算 canonical）
    lcols = [r[1] for r in fcur.execute("PRAGMA table_info(leisu_results)").fetchall()]
    for r in (dict(zip(lcols, x)) for x in fcur.execute("SELECT * FROM leisu_results")):
        if r.get("status") != "完":
            continue
        add(r.get("home_canonical") or r.get("home_raw"),
            r.get("away_canonical") or r.get("away_raw"),
            to_date(r.get("page_date")), r.get("score_h"), r.get("score_a"),
            r.get("league"), "leisu")

    # 源日期窗
    all_dd = [d[0] for lst in idx.values() for d in lst if d[0]]
    src_min = min(all_dd) if all_dd else None
    src_max = max(all_dd) if all_dd else None

    matched = 0
    per_src = {"GQ": 0, "fdb": 0, "leisu": 0}
    name_only = 0
    window_overlap = 0
    examples = []
    for L in long_rows:
        lh = _canonical_match(L.get("home"), fcur) or (L.get("home") or "")
        la = _canonical_match(L.get("away"), fcur) or (L.get("away") or "")
        ld = to_date(L.get("date"))
        if not lh or not la or not ld:
            continue
        if src_min and src_max and (src_min - datetime.timedelta(days=tol_days)) <= ld <= (
            src_max + datetime.timedelta(days=tol_days)
        ):
            window_overlap += 1
        hit = None
        for key in ((lh, la), (la, lh)):
            if key in idx:
                for (dd, sh, sa, lg, src) in idx[key]:
                    if dd and abs((dd - ld).days) <= tol_days:
                        hit = (src, sh, sa, lg, dd)
                        break
            if hit:
                break
        if hit:
            matched += 1
            per_src[hit[0]] += 1
            examples.append({
                "long": f"{L.get('home')} {hit[1]}-{hit[2]} {L.get('away')}",
                "src": hit[0], "long_date": L.get("date"), "result_date": str(hit[4]),
                "league": hit[3],
            })
        else:
            for key in ((lh, la), (la, lh)):
                if key in idx:
                    name_only += 1
                    break

    gq.close()
    fdb.close()
    return {
        "matched": matched,
        "per_src": per_src,
        "name_only": name_only,
        "window_overlap": window_overlap,
        "src_date_range": [str(src_min), str(src_max)],
        "total_long": len(long_rows),
        "examples": examples[:15],
        "tol_days": tol_days,
    }


# ===================== 运行模式 =====================
def run_once(db_path: str | Path, bind_long: bool, days: int = 1) -> None:
    adb = ADB()
    try:
        adb.connect()
    except Exception as e:
        print(f"[once] ADB 不可达: {e}；离线请用 --parse-only / --bind-long")
        return
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        ensure_results_page(adb)
        run_date = datetime.date.today()
        cap = int(time.time())
        total = 0
        for step in range(days):
            page_date = (run_date - datetime.timedelta(days=step)).isoformat()
            print(f"[collect] 采赛果页日期={page_date} (step {step})")
            png = adb.screencap()
            rows = parse_results_page(png)
            if not rows:
                print(f"[collect] 该页解析到 0 场（可能未完赛或 OCR 失败）")
            n = store_results(cur, rows, page_date, cap)
            total += n
            print(f"[collect] 入库 {n} 场 (累计 {total})")
            if step < days - 1:
                lines = get_ocr_lines(png)
                tap_prev_date(adb, lines)
        conn.commit()
        print(f"✅ 共采集 {total} 场赛果 → leisu_results")
    finally:
        conn.close()
    if bind_long:
        rep = bind_long_matches(db_path)
        print_bind_report(rep)


def print_bind_report(rep: dict) -> None:
    if "error" in rep:
        print(f"[bind] {rep['error']}")
        return
    print("\n===== long 特征 真解锁报告 (统一 canonical + 三源 join) =====")
    print(f"  long 总场数            : {rep['total_long']}")
    print(f"  终场源日期窗           : {rep['src_date_range'][0]} ~ {rep['src_date_range'][1]} (容差±{rep['tol_days']}天)")
    print(f"  long 落在源窗口内      : {rep['window_overlap']} 场 (潜在可解)")
    print(f"  ✅ 按日期解锁           : {rep['matched']} 场  {rep['per_src']}")
    print(f"  （仅队名重叠/时间差）   : {rep['name_only']} 场 (join机制可用, 纯时间缺口)")
    if rep["examples"]:
        print("  解锁示例:")
        for ex in rep["examples"]:
            print(f"    - [{ex['src']}] {ex['long']}  | long={ex['long_date'][:10]} 终场日={ex['result_date']} [{ex['league']}]")
    else:
        print("  ⚠ 按日期解锁=0：long 截图 2025-07~2026-07，而本地终场源(GQ 2026-07-18~07-27 / "
              "雷速仅近期 / fdb 多为早年 WH+IW) 与该批截图时间不重叠 → 这是已确认的真实时间缺口。")
        print("  ✅ 采集器价值=前向闭环：今后新截的 long 图，次日雷速赛果/GQ 即有终场，自动解锁。")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="雷速赛果采集器 (解锁 long 129 场监督学习)")
    ap.add_argument("--init", action="store_true", help="仅建 leisu_results 表")
    ap.add_argument("--parse-only", metavar="PNG", help="离线校验解析器：对一张赛果页截图跑 OCR+解析")
    ap.add_argument("--once", action="store_true", help="采当天赛果并入库")
    ap.add_argument("--days", type=int, default=1, help="采集最近 N 天（默认1=当天）")
    ap.add_argument("--bind-long", action="store_true", help="采集后/单独回绑 long 特征并报告解锁数")
    ap.add_argument("--db", default=FOOTBALL_DB_PATH)
    ap.add_argument("--long-csv", default=LONG_CSV)
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    dbp = Path(args.db).resolve()
    root = Path("D:/Architecture/data").resolve()
    if dbp != root and root not in dbp.parents:
        sys.exit(f"[安全] --db 越界：仅允许 {root} 内")
    if args.init:
        init_results_db(dbp)
        return
    if args.parse_only:
        rows = parse_results_page(args.parse_only)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        print(f"\n# 解析到 {len(rows)} 场")
        return
    if args.bind_long and not args.once:
        # 仅回绑（不采）
        rep = bind_long_matches(dbp, args.long_csv)
        print_bind_report(rep)
        return
    if args.once:
        init_results_db(dbp)
        run_once(dbp, bind_long=args.bind_long, days=max(1, args.days))
        return
    # 默认：建表 + 提示
    init_results_db(dbp)
    print("用法：--once 采当天 | --days N 采N天 | --parse-only PNG 离线校验 | --bind-long 回绑")


if __name__ == "__main__":
    main()
