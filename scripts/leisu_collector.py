# -*- coding: utf-8 -*-
"""
leisu_collector.py — 雷速体育「第二庄源」采集器（生产版，P2 交付）

========================================================================
里程碑（对应方案 §9）
========================================================================
  W1  POC 验证（1 场全链路）+ OCR 坐标校准 + leisu_odds 建表
  W2  leisu_collector.py 生产版（导航 + OCR + 对齐 + 入库）
  W3  2h/轮节奏化 + 风控自动复位 + 每日人工巡检
  W4+ 必发建模 + ROI 验证 + team_canonical 扩展

========================================================================
运行命令
========================================================================
  # 仅建表（幂等；在既有 leisu_odds 上补 §4.1 列，保留历史数据与其它消费者）
  python scripts/leisu_collector.py --init

  # 单次采集：导航(MuMu) → 截图 → OCR → 对齐 → 入库
  python scripts/leisu_collector.py --once [--home 佛山南狮 --away 无锡吴钩 --kickoff 1719300000]

  # 守护进程：每 2h 一轮（严禁 60s/轮，避免触发升级风控）
  python scripts/leisu_collector.py --daemon

  # Mock 管线验证（不经 MuMu/OCR，直接注入佛山南狮 vs 无锡吴钩 6 庄家示例）
  python scripts/leisu_collector.py --mock

  # 默认行为：建表 + 单次；若 MuMu/ADB 不可达则提示使用 --mock

========================================================================
设计要点（对应方案 §2/§3/§4）
========================================================================
  - OCR 引擎懒加载：优先 rapidocr_onnxruntime.RapidOCR；import 失败则降级 Mock
    类（MockLeisuOCR.ocr_odds_matrix 返回 None，表示无法抽取）。绝不在模块顶层
    import 重引擎，保证脚本可启动（即使本机没装 OCR 引擎）。
  - 坐标体系：MuMu 截图 = 900×1600 实际像素（已验证），禁止视觉估算。
  - 列 x 区间裁剪 + 行切片 + 数字正则清洗 + 返还率一致性校验（仅告警不丢）。
  - 对齐：home/away 经 team_canonical 模糊匹配 → canonical 名 → matches 查 match_id；
    对齐不上存 NULL（仍入库，跨庄跳过）。
  - 风控自动复位：screencap → 检测 aliyunCaptcha → 慢滑复位 → 仍阻塞则暂停 30min。

依赖：仅标准库 + sqlite3 + re + difflib（PIL 仅在 OCR 裁剪时按需懒加载）。
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# ===================== 路径与常量 =====================
# football_data.db 绝对路径（与方案约定一致）
FOOTBALL_DB_PATH = "D:/Architecture/data/football_data.db"

# MuMu ADB（实机验证路径与端口）
ADB_PATH = "C:/Program Files/Netease/MuMu/nx_main/adb.exe"
ADB_DEVICE = "127.0.0.1:5555"
LEISU_PKG = "com.leisu.sports"

# 截图落盘目录
CAPTURE_DIR = Path("D:/Architecture/data/leisu_capture")

# 1X2 列 x 区间（900 宽，POC 实测校准于真实雷速 1X2 实时页 2026-07-25）
# 实测(金泉尚武 vs 大田市民 韩K联)：庄名 cx≈55 | 主胜 cx≈426 | 平局 cx≈598 | 客胜 cx≈770
#   1X2 实时页仅"即时/初始"双列，无独立"返还率/时间戳"列 → ret/ts 区间留空(返回 None)
#   ⚠ 注意：早前 _1x2_v2.png 为旧版布局(庄名112/主274/平370/客466/返还率562/时间戳706)，
#   与当前线上布局不同；以当前实时页为准（雷速改版需重标定，见方案 §8）。
COL_X = [
    ("book", 20, 110),
    ("h", 380, 470),
    ("d", 550, 650),
    ("a", 720, 820),
    ("ret", 820, 900),
    ("ts", 900, 1000),
]
# 多市场列布局 (2026-07-30 OU 截图实测 900x1600):
#   1X2 胜平负: book 庄名 | h 胜 | d 平 | a 负 | ret 返还率 | ts 时间
#   OU  总进球: book 庄名 | h 大球(COL3~263) | d 大球(COL4~368) | a 小球(~473)
#   AH  让球/亚盘: book | h 主 | d 盘口线(可负) | a 客 (与 OU 同列布局)
# 注: OU/AH 只取 3 个数值列(book/h/d/a), h/d/a 角色由市场语义决定。
COLS = {
    "1X2": COL_X,
    "OU":   [("book", 20, 130), ("h", 340, 425), ("d", 235, 325), ("a", 555, 650)],
    "AH":   [("book", 20, 130), ("h", 340, 425), ("d", 235, 325), ("a", 555, 650)],
    "CORNER":[("book", 20, 130), ("h", 340, 425), ("d", 235, 325), ("a", 555, 650)],
}
# 赔率表区 y 范围：副 tab 栏 y≈640，1X2 表头 y≈682，
# 汇总行(最高/最低/平均) y≈726-936 需排除；真庄家行从 y≈978 起到屏底 1600。
Y_TOP, Y_BOT = 970, 1600


# ===================== §4.1 建表 SQL（幂等） =====================
# 注意：本库 football_data.db 已存在一张「旧版 leisu_odds」(不同 schema：
# open_h/d/a、close_h/d/a、bookmakers_json 等)。init_db 会在检测到列不兼容时
# 将其改名为 leisu_odds_legacy_<时间戳> 以保留历史数据，再建本表。
LEISU_SCHEMA = """
CREATE TABLE IF NOT EXISTS leisu_odds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id    INTEGER,          -- 对齐到 matches.match_id (对齐不上则 NULL)
    home_raw    TEXT,             -- OCR 原始主队名
    away_raw    TEXT,             -- OCR 原始客队名
    kickoff_ts  INTEGER,          -- 开赛时间戳(从详情页头部拿)
    market      TEXT,             -- '1X2' / 'AH' / 'OU' / 'CORNER'
    book        TEXT,             -- 庄家标准缩写
    odds_h      REAL,             -- 主胜(1X2) / 主队让球赔(AH)
    odds_d      REAL,             -- 平局(1X2) / 让球盘口(AH,可负)
    odds_a      REAL,             -- 客胜(1X2) / 客队赔(AH)
    line        REAL,             -- AH/OU 盘口线
    ret         REAL,             -- 返还率 %
    ts_raw      TEXT,             -- 原始时间戳文本
    capture_at  INTEGER,          -- 采集时间戳
    source      TEXT DEFAULT 'leisu'
);
CREATE INDEX IF NOT EXISTS idx_leisu_match ON leisu_odds(match_id, market, capture_at);
"""

# §4.1 表列定义（当前 leisu_odds 已为纯 §4.1 模型）。
# 历史说明：football_data.db 中曾存在一张「旧版 leisu_odds」(open_h/d/a、bookmakers_json 等，
# 405 行)，其真实消费者全部走独立的 data/leisu_odds.db，与本 P2 表零重叠，故 P2 将其
# 隔离至 leisu_odds_legacy 并重建纯 §4.1 表。init_db 仅作幂等兜底（列已齐则跳过 ALTER）。
SECTION1_COLDEF = {
    "match_id": "INTEGER",
    "home_raw": "TEXT",
    "away_raw": "TEXT",
    "kickoff_ts": "INTEGER",
    "market": "TEXT",
    "book": "TEXT",
    "odds_h": "REAL",
    "odds_d": "REAL",
    "odds_a": "REAL",
    "line": "REAL",
    "ret": "REAL",
    "ts_raw": "TEXT",
    "capture_at": "INTEGER",
    "source": "TEXT DEFAULT 'leisu'",
}
SECTION1_COLS = set(SECTION1_COLDEF) | {"id"}  # id 已存在，跳过 ALTER


def _rebuild_if_unique_constraint(cur: sqlite3.Cursor) -> None:
    """若 leisu_odds 存在 UNIQUE(home_team,away_team) 约束(auto-index)，重建表去掉它。

    SQLite 不允许 DROP 约束型 auto-index，只能重建表。重建保留全部列与数据，
    重建前校验行数一致，避免丢数据。
    （注：旧版 leisu_odds 的 UNIQUE 约束已随隔离迁至 leisu_odds_legacy，本函数仅作兜底。）
    """
    need = False
    for row in cur.execute("PRAGMA index_list(leisu_odds)").fetchall():
        iname = row[1]
        is_unique = row[2]
        origin = row[3] if len(row) > 3 else ""
        if is_unique and origin == "u":
            cols = [c[2] for c in cur.execute(f"PRAGMA index_info({iname})").fetchall()]
            if "home_team" in cols and "away_team" in cols:
                need = True
                break
    if not need:
        return
    info = cur.execute("PRAGMA table_info(leisu_odds)").fetchall()
    col_defs = []
    for _cid, name, ctype, notnull, dflt, pk in info:
        d = f'"{name}" {ctype or "TEXT"}'
        if pk:
            d += " PRIMARY KEY"
        else:
            if notnull:
                d += " NOT NULL"
            if dflt is not None:
                d += f" DEFAULT ({dflt})"
        col_defs.append(d)
    cur.execute("CREATE TABLE leisu_odds_new (" + ", ".join(col_defs) + ")")
    cur.execute("INSERT INTO leisu_odds_new SELECT * FROM leisu_odds")
    n_old = cur.execute("SELECT COUNT(*) FROM leisu_odds").fetchone()[0]
    n_new = cur.execute("SELECT COUNT(*) FROM leisu_odds_new").fetchone()[0]
    if n_new != n_old:
        raise RuntimeError(f"重建表行数不一致 old={n_old} new={n_new}，中止以免丢数据")
    cur.execute("DROP TABLE leisu_odds")
    cur.execute("ALTER TABLE leisu_odds_new RENAME TO leisu_odds")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_leisu_league ON leisu_odds(league_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_leisu_team ON leisu_odds(home_team, away_team)")
    print("[init_db] 已重建 leisu_odds 并移除 UNIQUE(home_team,away_team) 约束")


def init_db(db_path: str | Path) -> None:
    """建/补 leisu_odds 表（幂等，方案 §4.1）。

    当前 leisu_odds 已是纯 §4.1 模型（旧版 405 行已隔离至 leisu_odds_legacy）。
    本函数仅在表完全不存在时按 §4.1 全量建表；否则补列（列已齐则跳过 ALTER），
    并对罕见的 UNIQUE(home_team,away_team) 约束作重建兜底。索引幂等创建。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    exists = cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='leisu_odds'"
    ).fetchone() is not None
    if not exists:
        cur.executescript(LEISU_SCHEMA)
        print(f"[init_db] 全量新建 leisu_odds(§4.1): {db_path}")
    else:
        # 已有表：补列到 §4.1（SQLite 追加在末尾，不影响既有列/消费者）
        have = {r[1] for r in cur.execute("PRAGMA table_info(leisu_odds)")}
        added = 0
        for col, typ in SECTION1_COLDEF.items():
            if col not in have:
                cur.execute(f"ALTER TABLE leisu_odds ADD COLUMN {col} {typ}")
                added += 1
        # 旧 schema 对 (home_team,away_team) 有 UNIQUE（约束型 auto-index，无法 DROP INDEX 删除），
        # 与 §4.1「一庄一行」模型冲突 → 原地重建表去掉该约束（保留全部数据与消费者）。
        _rebuild_if_unique_constraint(cur)
        print(f"[init_db] leisu_odds 已就绪 (§4.1 列 + 保留 {len(have)} 个原有列)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_leisu_match ON leisu_odds(match_id, market, capture_at)"
    )
    conn.commit()
    conn.close()
    print(f"[init_db] leisu_odds 就绪: {db_path}")


# ===================== OCR 层 =====================
# 全局缓存，避免重复加载重引擎
_OCR = None


def get_ocr_engine():
    """懒加载 OCR 引擎：优先 rapidocr_onnxruntime.RapidOCR。失败抛 ImportError。

    绝不在模块顶层 import，保证脚本在没有 OCR 引擎的环境也能启动。
    """
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR  # 懒加载；import 失败即抛 ImportError
        _OCR = RapidOCR()
    return _OCR


class MockLeisuOCR:
    """OCR 引擎不可用时的降级桩。

    其 ocr_odds_matrix 固定返回 None（表示无法抽取）——真正的 mock 数据由
    --mock 模式在入库层注入，绝不在 OCR 层造假。
    """

    def ocr_odds_matrix(self, png_path, market: str = "1X2"):
        return None


def _to_lines(result) -> list[dict]:
    """把 RapidOCR 的 result([[box,text,conf],...]) 转成统一 lines 结构。"""
    lines = []
    if not result:
        return lines
    for item in result:
        try:
            box, txt, conf_raw = item[0], item[1], item[2]
            conf = float(conf_raw) if conf_raw is not None else 0.0
        except Exception:
            continue
        lines.append({
            "text": txt,
            "conf": conf,
            "bbox": [[float(p[0]), float(p[1])] for p in box],
        })
    return lines


def parse_odds_cell(text: str):
    """清洗赔率单元格：只保留数字+小数点（AH 盘口线允许前导负号），过滤噪声。失败返回 None。"""
    m = re.search(r"-?\d+\.\d+|-?\d+", (text or "").replace(" ", ""))
    return float(m.group()) if m else None


def validate_row(book: str, h, d, a, ret) -> tuple[bool, list[str]]:
    """返还率/赔率合理性校验（方案 §3.4）。返回 (是否有效, 告警列表)。

    仅告警不丢弃：返回 True 也允许带告警（一致性偏差 ≤5pp 容错）。
    """
    warns: list[str] = []
    if not (1.01 <= h <= 50 and 1.01 <= d <= 50 and 1.01 <= a <= 50):
        warns.append("赔率越界[1.01,50]")
    if ret is not None and not (80 <= ret <= 99):
        warns.append(f"返还率越界 ret={ret}")
    if ret is not None and h and d and a:
        # 返还率一致性：sum(1/o) ≈ 100/ret（ret 为返还率%，overround=100/ret）。容错 ±0.05
        implied = 1.0 / h + 1.0 / d + 1.0 / a
        if abs(implied - 100.0 / ret) > 0.05:
            warns.append(f"返还率不一致 implied={implied:.3f} 100/ret={100.0 / ret:.3f}")
    return (len(warns) == 0), warns


def ocr_odds_matrix(png_path: str | Path, market: str = "1X2") -> list | None:
    """从雷速指数截图抽取赔率矩阵。

    返回 list[(book_raw, h, d, a, ret, ts_raw)]；AH 市场用 d 位存盘口 line。
    引擎不可用 / 抽取失败返回 None。

    流程：OCR 整图 → 按 bbox 的 x 中心指派到列(§3.5 区间) → 按 y 中心分组为行
          → 逐行 parse_odds_cell 清洗 + validate_row 一致性校验(仅告警)。
    """
    try:
        ocr = get_ocr_engine()
    except ImportError:
        # 引擎不可用 → 降级 Mock（返回 None，表示抽不出）
        return MockLeisuOCR().ocr_odds_matrix(png_path, market)

    result, _ = ocr(str(png_path))
    lines = _to_lines(result)
    if not lines:
        return []

    # 两遍法：先把赔率列(h/d/a/ret/ts)按 cy 聚成行（同行业 cy 几乎一致,
    # 行间 cy 差约 74px), 再把每个庄名(book)归到最近赔率行。
    # 原因: 庄名在每行顶部、赔率在行中, 二者 cy 差 4~44px 且行距不规整.
    tokens = []  # (col, cy, text)
    xmap = COLS.get(market, COL_X)
    for ln in lines:
        x0, y0 = ln["bbox"][0][0], ln["bbox"][0][1]
        x1, y1 = ln["bbox"][2][0], ln["bbox"][2][1]
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if not (Y_TOP <= cy <= Y_BOT):
            continue
        col = None
        for name, xa, xb in xmap:
            if xa <= cx < xb:
                col = name
                break
        if col is None:
            continue
        tokens.append((col, cy, ln["text"]))

    book_toks = [(cy, t) for col, cy, t in tokens if col == "book"]
    odds_toks = [(col, cy, t) for col, cy, t in tokens if col != "book"]

    # 庄家分段法（鲁棒，替代"固定35px聚行 + 庄名就近归位"）：
    # 雷速每庄家块 = [初始行] + [庄名] + [即时行]，块间由"庄名"天然分隔。
    # 问题：固定35px阈值在 OCR 抖动下会把相邻两庄的行误合并→交叉污染(h 取自甲、
    # d/a 取自乙)；且庄名就近归位会在初始/即时间随机选→同场主客倒挂。
    # 解决：用庄名把页面切成互不重叠的垂直窗口，每窗只归该庄赔率；窗内取
    # cy > 庄名 的"即时(下方)"行作为该行赔率，彻底杜绝跨庄污染与初始/即时混采。
    book_toks.sort(key=lambda x: x[0])
    if not book_toks:
        return []
    out = []
    n = len(book_toks)
    for i, (bcy, bt) in enumerate(book_toks):
        lo = Y_TOP if i == 0 else (book_toks[i - 1][0] + bcy) / 2.0
        hi = Y_BOT if i == n - 1 else (bcy + book_toks[i + 1][0]) / 2.0
        win = [(col, cy, t) for (col, cy, t) in odds_toks if lo <= cy <= hi]
        if not win:
            continue
        win.sort(key=lambda x: x[1])
        rows_i: list[list] = []
        cur = [win[0]]
        for tok in win[1:]:
            if tok[1] - cur[-1][1] > 35:
                rows_i.append(cur)
                cur = [tok]
            else:
                cur.append(tok)
        rows_i.append(cur)
        # 即时行 = cy > 庄名 的那一行（下方）；否则退回最近行
        live = None
        for r in rows_i:
            rc = sorted(t[1] for t in r)[len(r) // 2]
            if rc > bcy:
                live = r
                break
        if live is None:
            live = min(rows_i, key=lambda r: abs(sorted(t[1] for t in r)[len(r) // 2] - bcy))
        d: dict = {}
        for col, _cy, t in live:
            if col not in d or len(t) > len(d[col]):
                d[col] = t
        h = parse_odds_cell(d.get("h", ""))
        dd = parse_odds_cell(d.get("d", ""))
        a = parse_odds_cell(d.get("a", ""))
        if None in (h, dd, a):
            continue
        ret = parse_odds_cell(d.get("ret", ""))
        ts_raw = (d.get("ts") or "").strip()
        _ok, warns = validate_row(bt, h, dd, a, ret)
        for w in warns:
            print(f"[validate] {bt}: {w}")
        # 硬失败: 赔率越界 = OCR 结构性错读(如把比分/让球数当赔率), 必须丢弃不入库。
        # 2026-08-01 根因: 旧代码丢弃 _ok 只打告警, 导致 (1.0/51/67)、(1.01/151/151)
        # 这类垃圾行进入 leisu_odds, 直接污染 multibook_consensus 的去水共识。
        # 软告警(返还率不一致 ≤5pp)仍按 §3.4 容错保留。
        if any("赔率越界" in w for w in warns):
            print(f"[validate] {bt}: 丢弃该行 (h={h} d={dd} a={a})")
            continue
        if market in ("AH", "OU"):
            # AH：h=主赔, d=盘口line(可负), a=客赔；ret 不适用
            # OU：h=大球赔, d=盘口line(如2.5), a=小球赔；ret 不适用
            out.append((bt, h, dd, a, None, ts_raw))
        else:
            out.append((bt, h, dd, a, ret, ts_raw))
    return out


# ===================== 导航层（ADB 封装） =====================
class ADB:
    """MuMu 模拟器 ADB 封装（方案 §2）。坐标均为 900×1600 实际像素。"""

    def __init__(self, device: str = ADB_DEVICE, adb_path: str = ADB_PATH):
        self.device = device
        self.adb = adb_path

    def _run(self, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
        cmd = [self.adb, "-s", self.device] + args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def connect(self) -> None:
        """adb connect 127.0.0.1:5555"""
        self._run(["connect", self.device])

    def tap(self, x: int, y: int) -> None:
        self._run(["shell", "input", "tap", str(x), str(y)])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, dur: int = 1500) -> None:
        self._run(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(dur)])

    def launch_leisu(self) -> None:
        """am start 启动雷速体育(用真实 launcher activity, 避免 monkey 误开游戏中心)。"""
        self._run(["shell", "am", "start", "-n",
                   f"{LEISU_PKG}/.ui.splash.SplashActivity",
                   "-c", "android.intent.category.LAUNCHER"])

    def is_black_screen(self, png: str | Path, threshold: float = 0.85) -> bool:
        """检测截图为纯黑/接近全黑(页面加载中/跳转过渡)。

        黑屏 = 绝大多数字(采样)亮度 < 16。MuMu 过渡页常出现(截图像素仅 ~11KB)。
        返回 True 表示疑似黑屏(需重等/重试)。
        """
        try:
            from PIL import Image
            img = Image.open(str(png)).convert("L")
            small = img.resize((90, 160))
            # Pillow >=14: get_flattened_data; fallback: getdata
            try:
                px = list(small.get_flattened_data())
            except AttributeError:
                px = list(small.getdata())
            dark = sum(1 for v in px if v < 16)
            return (dark / len(px)) >= threshold
        except Exception:
            return False

    def enter_detail_1x2(self, max_retry: int = 3) -> bool:
        """进详情页「指数」tab → 动态定位「胜平负」sub-tab 并点击。

        修复(2026-07-27):
          - 旧坐标(276,563)点不中 tab bar(实测 y≈578)，改为 OCR 动态定位"指数"。
          - 点"指数"几乎必触发天级流控滑块，点完即检测+自动复位；复位后重试。
          - 胜平负 sub-tab 仍动态 OCR + 兜底(235,624)。
        返回 True=已落到 胜平负 赔率页(尽力); False=彻底失败。
        """
        for attempt in range(max_retry):
            png = self.screencap()
            if self.is_black_screen(png):
                time.sleep(2.0)
                continue

            # 动态定位并点击「指数」tab
            try:
                ocr = get_ocr_engine()
                res, _ = ocr(str(png))
                lines = _to_lines(res)
            except Exception:
                lines = []
            cy, cx = find_index_tab(lines)
            if cx is None:
                cy = 578
                cx = 270  # 5 tab 居中；6 tab 时偏左但仍在可触发区域
            self.tap(int(cx), int(cy))
            time.sleep(3.0)

            # 点指数几乎必触发天级流控：检测+复位
            png2 = self.screencap()
            if self.detect_aliyun_captcha(png2):
                print("[enter_detail_1x2] 指数页触发天级流控，自动复位")
                self.solve_captcha()
                time.sleep(3.0)
                continue  # 重试点指数

            # 动态定位 胜平负 sub-tab
            try:
                ocr = get_ocr_engine()
                res, _ = ocr(str(png2))
                lines = _to_lines(res)
                cy2, cx2 = find_1x2_subtab(lines)
            except Exception:
                cy2, cx2 = None, None
            if cx2 is None:
                self.tap(235, 624)  # 兜底
            else:
                self.tap(int(cx2), int(cy2))
            time.sleep(4.0)

            png3 = self.screencap()
            if not self.is_black_screen(png3):
                return True
            if self.detect_aliyun_captcha(png3):
                print("[enter_detail_1x2] 胜平负页触发天级流控，自动复位")
                self.solve_captcha()
                time.sleep(3.0)
            time.sleep(2.0)
        return False

    def enter_detail_market(self, market: str = "1X2", max_retry: int = 3) -> bool:
        """进详情页「指数」tab → 动态定位指定市场 sub-tab 并点击 (通用版)。

        market: 1X2(胜平负) / AH(让球) / OU(总进球) / CORNER(角球)。
        复用 enter_detail_1x2 的流控复位 + 黑屏重试逻辑, 仅 subtab 定位与兜底坐标按市场切换。
        返回 True=已落到对应赔率页; False=彻底失败。
        """
        finder = {
            "1X2": find_1x2_subtab,
            "AH": find_ah_subtab,
            "OU": find_ou_subtab,
            "CORNER": find_corner_subtab,
        }.get(market, find_1x2_subtab)
        # ⚠ 兜底坐标须 POC 校准(雷速改版即失效); 优先走动态 OCR 定位。
        fallback = {
            "1X2": (235, 624), "AH": (140, 624),
            "OU": (420, 624), "CORNER": (560, 624),
        }.get(market, (235, 624))
        for attempt in range(max_retry):
            png = self.screencap()
            if self.is_black_screen(png):
                time.sleep(2.0)
                continue
            try:
                ocr = get_ocr_engine()
                res, _ = ocr(str(png))
                lines = _to_lines(res)
            except Exception:
                lines = []
            cy, cx = find_index_tab(lines)
            if cx is None:
                # ⚠️ 兜底坐标校准 (2026-07-30): 5-tab 布局 (直播/聊天/指数/数据/会员) 下
                #    "指数" 中心是 (530, 450); 6-tab 布局 (含"专家") 时 cx=270 更准。
                #    因当前屏幕宽 900, 多数比赛是 5-tab, 用 450 命中最高。
                cy, cx = 530, 450
            self.tap(int(cx), int(cy))
            time.sleep(3.0)
            # ⭐ 验证 tap 是否真切到了"指数"页: 指数页有"让球/胜平负/总进球/角球"4 个 sub-tab,
            #    OCR 检测到任一即确认。失败则重 tap, 最多 2 次。
            for _ in range(2):
                png_chk = self.screencap()
                try:
                    ocr = get_ocr_engine()
                    res_chk, _ = ocr(str(png_chk))
                    lines_chk = _to_lines(res_chk)
                except Exception:
                    lines_chk = []
                if any(any(k in ln.get("text", "") for k in ("让球", "胜平负", "总进球", "角球"))
                       for ln in lines_chk):
                    break  # 切到指数页成功
                # 否则再 tap 一次 (兜底)
                self.tap(450, 530)
                time.sleep(2.5)
            png2 = self.screencap()
            if self.detect_aliyun_captcha(png2):
                print(f"[enter_detail_{market}] 指数页触发天级流控，自动复位")
                self.solve_captcha()
                time.sleep(3.0)
                continue
            try:
                ocr = get_ocr_engine()
                res, _ = ocr(str(png2))
                lines = _to_lines(res)
                cy2, cx2 = finder(lines)
            except Exception:
                cy2, cx2 = None, None
            if cx2 is None:
                self.tap(*fallback)
            else:
                self.tap(int(cx2), int(cy2))
            time.sleep(4.0)
            png3 = self.screencap()
            if not self.is_black_screen(png3):
                return True
            if self.detect_aliyun_captcha(png3):
                print(f"[enter_detail_{market}] {market} 页触发天级流控，自动复位")
                self.solve_captcha()
                time.sleep(3.0)
            time.sleep(2.0)
        return False

    def screencap(self) -> Path:
        """截图并落盘到 CAPTURE_DIR，返回 png 路径。"""
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        png = CAPTURE_DIR / f"leisu_{int(time.time() * 1000)}.png"
        proc = subprocess.run(
            [self.adb, "-s", self.device, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=60,  # P2-1: 防止 ADB 无响应时永久挂起 run_daemon
        )
        if not proc.stdout:
            raise RuntimeError("[screencap] 截图数据为空，ADB 可能无响应")
        png.write_bytes(proc.stdout)
        return png

    def detect_aliyun_captcha(self, png: str | Path) -> bool:
        """检测阿里云滑块/天级流控验证码（方案 §2.3）。

        用 OCR 文本启发：截图含「验证/滑块/拖动/流控/aliyun/captcha」等关键词即判定。
        无 OCR 引擎时无法检测，乐观返回 False（假定无滑块）。
        """
        try:
            ocr = get_ocr_engine()
        except ImportError:
            return False
        try:
            result, _ = ocr(str(png))
        except Exception:
            return False
        if not result:
            return False
        text = " ".join(str(it[1]) for it in result)
        tokens = ["验证", "滑块", "拖动", "aliyun", "captcha", "滑动",
                  "安全验证", "验证通过", "流控", "天级", "手动刷新"]
        return any(t in text for t in tokens)

    def _uiautomator_dump(self) -> str | None:
        """dump 当前 UI 层级到 XML，返回内容；失败返回 None。"""
        try:
            proc = subprocess.run(
                [self.adb, "-s", self.device, "shell", "uiautomator", "dump",
                 "/sdcard/window_dump.xml"],
                capture_output=True, timeout=15,
            )
            if proc.returncode != 0:
                return None
            proc2 = subprocess.run(
                [self.adb, "-s", self.device, "shell", "cat",
                 "/sdcard/window_dump.xml"],
                capture_output=True, timeout=10,
            )
            return proc2.stdout.decode("utf-8", errors="replace")
        except Exception:
            return None

    def _find_slider_bounds(self, xml: str) -> tuple | None:
        """从 uiautomator dump XML 中定位滑块控件 bounds。

        匹配含 slider/滑块/verify/captcha 特征节点，提取 bounds。
        返回 (cx, cy, width) 或 None。
        """
        import re as _re_sl
        pat = _re_sl.compile(
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*?'
            r'(?:(?:class|resource-id|text)="[^"]*(?:[Ss]lider|[Vv]erify|滑块|captcha)[^"]*")',
            _re_sl.DOTALL,
        )
        m = pat.search(xml)
        if m:
            x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            return ((x1 + x2) // 2, (y1 + y2) // 2, x2 - x1)
        # 兜底: 屏幕下半部分最宽的可点击元素
        track_pat = _re_sl.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
        best, best_w = None, 0
        for tm in track_pat.finditer(xml):
            tx1, ty1, tx2, ty2 = int(tm.group(1)), int(tm.group(2)), int(tm.group(3)), int(tm.group(4))
            tw, tcy = tx2 - tx1, (ty1 + ty2) // 2
            if tw > 200 and tcy > 800 and tw > best_w:
                best_w, best = tw, ((tx1 + tx2) // 2, tcy, tw)
        return best

    def solve_captcha(self) -> None:
        """阿里云滑块/天级流控自动复位（根源修复 v2 — uiautomator 精确定位）。

        防线:
          1. uiautomator dump → 定位真实滑块控件 bounds（非猜坐标）
          2. 从把手左边缘慢滑到右边缘（模拟人类拖拽，时长∝距离）
          3. 关弹窗 + 下拉刷新（天级流控要求「通过后需手动刷新」）
          4. 等 4s 页面重载完成
        """
        # Step 1: 尝试精确定位滑块控件
        xml = self._uiautomator_dump()
        slider = None
        if xml:
            slider = self._find_slider_bounds(xml)
            if slider:
                print(f"[solve_captcha] uiauto 定位滑块: center=({slider[0]},{slider[1]}) w={slider[2]}")
            else:
                print("[solve_captcha] uiauto 未找到滑块控件, 用兜底坐标")

        # Step 2: 精确滑动 or 兜底
        # ⚠️ 关键：必须用 `input touchscreen swipe` (不是 `input swipe`),
        #    `input swipe` 在 aliyun webview 滑块上无效（被 JS 层机器人检测拒绝）。
        #    滑块 → 槽完整 bounds 是 aliyunCaptcha-sliding-* 系列,
        #    起点取把手左边缘, 终点取槽右边缘, duration 必须 ≥ 1500ms 模拟人类.
        if slider:
            sx, sy = slider[0] - slider[2] // 2, slider[1]
            ex, ey = slider[0] + slider[2] // 2, slider[1]
            duration = max(1800, int(slider[2] * 2))  # ← 加下限 1800ms 防太快被拒
        else:
            # 兜底实测坐标（2026-07-30 重测 touchscreen swipe 起效坐标）
            sx, sy, ex, ey, duration = 233, 937, 660, 937, 2000

        # ⭐ 唯一可用 swipe 命令: touchscreen swipe (input swipe / sendevent 都无效)
        self._run(["shell", "input", "touchscreen", "swipe",
                   str(sx), str(sy), str(ex), str(ey), str(duration)])

        time.sleep(2.5)

        # Step 3: 关弹窗 + 下拉刷新（注意: 这里仍用 input swipe, 是普通滚动非滑块）
        self.tap(100, 300)
        time.sleep(1.0)
        self.swipe(450, 500, 450, 900, 400)
        time.sleep(4.0)

    def ensure_home(self, max_back: int = 6) -> tuple[bool, bool]:
        """确保当前处于雷速首页(赛程列表 tab)。返回 (on_home, solved_captcha)。

        处理三类干扰并回到首页:
          - 阿里云滑块/天级流控 → 自动复位(solve_captcha)
          - 活动弹窗(如「球王评选/立即参与」) → 点右上关闭(实测 X≈900,320)
          - 误入详情页(解滑块后常见漂移) → 连按 back 回列表
        首页识别: 顶部同时出现 ≥3 个 tab 词(全部/进行中/赛程/赛果/关注)。
        兜底: 仍不在首页则 am start 重启雷速。无 OCR 时乐观返回(已在首页),
        避免误退。
        """
        solved = False
        for _ in range(max_back + 1):
            png = self.screencap()
            if self.detect_aliyun_captcha(png):
                print("[ensure_home] 检测到滑块, 自动复位")
                self.solve_captcha()
                solved = True
                time.sleep(3)
                continue
            try:
                ocr = get_ocr_engine()
                res, _ = ocr(str(png))
                text = " ".join(str(it[1]) for it in res)
            except Exception:
                # 无 OCR 无法验证, 乐观认为已在首页(避免误退)
                return True, solved
            markers = ["全部", "进行中", "赛程", "赛果", "关注"]
            if sum(m in text for m in markers) >= 3:
                return True, solved
            # 活动弹窗: 点右上关闭
            if "立即参与" in text or "参与投票" in text or "球王" in text:
                print("[ensure_home] 关闭活动弹窗")
                self.tap(900, 320)
                time.sleep(1.5)
                continue
            # 不在首页 ⇒ 返回
            self._run(["shell", "input", "keyevent", "4"])
            time.sleep(1.5)
        # 兜底重启
        print("[ensure_home] 兜底 am start 重启雷速")
        self.launch_leisu()
        time.sleep(4)
        png = self.screencap()
        if self.detect_aliyun_captcha(png):
            self.solve_captcha()
            solved = True
            time.sleep(3)
        return True, solved


# ===================== 对齐层 =====================
def _canonical_match(name: str, cur: sqlite3.Cursor):
    """team_canonical 模糊匹配 OCR 队名 → canonical 名。

    策略：精确 → 包含(双向) → 编辑距离(ratio>0.85)。命中即返回 canonical。
    """
    if not name:
        return None
    name = name.strip()
    cur.execute("SELECT canonical, aliases_json FROM team_canonical")
    best = None
    best_ratio = 0.85
    for canon, aj in cur.fetchall():
        try:
            aliases = json.loads(aj) if aj else []
        except Exception:
            aliases = []
        pool = [canon] + list(aliases)
        for al in pool:
            al = str(al).strip()
            if not al:
                continue
            if name == al:
                return canon
            if name in al or al in name:   # 包含匹配（OCR 常切碎/截断）
                return canon
            ratio = difflib.SequenceMatcher(None, name, al).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = canon
    return best


def align_match(home_raw: str, away_raw: str, kickoff_ts, cur: sqlite3.Cursor):
    """对齐到 matches.match_id（方案 §4.2）。对齐不上返回 None（仍入库，跨庄跳过）。

    canonical 或 raw 在 matches 按 (home_team_name, away_team_name, date(match_date)) 查。
    kickoff_ts 为 unix 秒；为空则仅按队名查。
    """
    h_c = _canonical_match(home_raw, cur) or (home_raw.strip() if home_raw else None)
    a_c = _canonical_match(away_raw, cur) or (away_raw.strip() if away_raw else None)
    if not h_c or not a_c:
        return None
    date_str = None
    if kickoff_ts:
        try:
            date_str = datetime.datetime.fromtimestamp(
                int(kickoff_ts), tz=datetime.timezone.utc
            ).strftime("%Y-%m-%d")
        except Exception:
            date_str = None
    if date_str:
        row = cur.execute(
            "SELECT match_id FROM matches WHERE home_team_name=? AND away_team_name=? AND match_date=?",
            (h_c, a_c, date_str),
        ).fetchone()
    else:
        row = cur.execute(
            "SELECT match_id FROM matches WHERE home_team_name=? AND away_team_name=?",
            (h_c, a_c),
        ).fetchone()
    return row[0] if row else None


# ===================== 入库层 =====================
def insert_leisu_odds(match_id, home_raw, away_raw, kickoff_ts, market,
                      rows: list, cur: sqlite3.Cursor, capture_at: int | None = None,
                      source: str = "leisu") -> None:
    """INSERT 一行/庄家到 leisu_odds（纯 §4.1 列）。

    rows: list[(book_raw, h, d, a, ret, ts_raw)]。
    AH 市场：d 位存盘口 line，h/a 存主/客赔；1X2：d 位存平赔。
    注：旧 schema 的 home_team/away_team/scrape_time 等列已在 P2 隔离时迁至
    leisu_odds_legacy，本表只承载 §4.1 模型，故此处不再写入旧列。
    """
    # capture_at 单位铁律 = **毫秒**。2026-08-01 根因: 此处曾写 int(time.time()) 秒级
    # (10位), 而历史 287 行为毫秒(13位) → max(capture_at)/ORDER BY 永远选中陈旧的
    # 毫秒行, multibook_consensus 的"取最新快照"逻辑被彻底击穿(新采数据永不生效)。
    capture_at = capture_at or int(time.time() * 1000)
    if capture_at < 10_000_000_000:          # 传入了秒级 → 归一到毫秒
        capture_at *= 1000
    for (book_raw, h, d, a, ret, ts_raw) in rows:
        if market in ("AH", "OU"):
            # AH: h=主赔, d=盘口line, a=客赔; OU: h=大球赔, d=盘口line, a=小球赔
            line, odds_h, odds_d, odds_a = d, h, None, a
        else:
            line, odds_h, odds_d, odds_a = None, h, d, a
        cur.execute(
            """INSERT INTO leisu_odds
               (match_id, home_raw, away_raw, kickoff_ts, market, book,
                odds_h, odds_d, odds_a, line, ret, ts_raw, capture_at, source)
               VALUES (?,?,?,?,?,?, ?,?,?,?,?,?,?,?)""",
            (match_id, home_raw, away_raw, kickoff_ts, market, book_raw,
             odds_h, odds_d, odds_a, line, ret, ts_raw, capture_at, source),
        )


# ===================== 风控自动复位 + 采集轮 =====================
def collect_round(adb: ADB, cur: sqlite3.Cursor, market: str = "1X2",
                  home_raw: str | None = None, away_raw: str | None = None,
                  kickoff_ts=None) -> int:
    """采集一轮（方案 §6.2）。返回本轮回库行数；被风控暂停返回 0。

    流程：screencap → 若检测到 aliyunCaptcha 则复位+等6s → 仍阻塞则暂停30min
          仅发心跳；否则进 1X2 → 截图 OCR → 对齐 → 入库。
    """
    png = adb.screencap()
    if adb.detect_aliyun_captcha(png):
        print("[captcha] 检测到阿里云滑块，尝试自动复位")
        adb.solve_captcha()
        time.sleep(6)
        png2 = adb.screencap()
        if adb.detect_aliyun_captcha(png2):
            print("[captcha] 仍被拦截，暂停 30min 仅发心跳")
            time.sleep(1800)   # 暂停 30min，不采
            return 0
    adb.enter_detail_market(market)
    time.sleep(4)
    png = adb.screencap()
    rows = ocr_odds_matrix(png, market)
    if rows is None:
        print("[ocr] 引擎不可用或抽取失败，本轮跳过")
        return 0
    mid = align_match(home_raw, away_raw, kickoff_ts, cur)
    cap = int(time.time())
    insert_leisu_odds(mid, home_raw, away_raw, kickoff_ts, market, rows, cur, cap, source="leisu")
    print(f"[collect] 入库 {len(rows)} 行 (match_id={mid})")
    return len(rows)


# ===================== 首页今日比赛解析 + 一键采集 =====================
import re as _re_home

_DATE_PAT = _re_home.compile(r"(\d{4})[/\-年.](\d{1,2})[/\-月.](\d{1,2})")
_MATCH_PAT = _re_home.compile(r"\[(\d+)\]\s*(.+?)\s*(?:VS|[\d]+-[\d]+)\s*(.+?)\s*\[(\d+)\]")
_HHMM_PAT = _re_home.compile(r"(\d{1,2}):(\d{2})")


def find_index_tab(lines: list[dict]) -> tuple[float | None, float | None]:
    """从详情页 tab 栏 OCR 行中定位「指数」tab 中心坐标。

    雷速详情页 tab 栏常被 OCR 合并成单行(如"直播 聊天 指数 数据 会员")。
    优先：找单独 bbox 包含"指数"的小 box；否则按合并文本内字符比例内插。
    返回 (cy, cx)；找不到返回 (None, None)。
    """
    # 1) 精确：单个 bbox 只含"指数"(或含少量噪声)
    for ln in lines:
        txt = ln.get("text", "")
        if "指数" in txt and len(txt) <= 4:
            x0, y0 = ln["bbox"][0]
            x1, y1 = ln["bbox"][2]
            return (y0 + y1) / 2.0, (x0 + x1) / 2.0

    # 2) 合并同行文本，按"指数"在合并串中的字符位置内插
    rows: dict[float, list] = {}
    for ln in lines:
        cy = ln["bbox"][0][1]
        key = min(rows.keys(), key=lambda k: abs(k - cy)) if rows else None
        if key is not None and abs(key - cy) <= 20:
            rows[key].append(ln)
        else:
            rows[cy] = [ln]
    for cy, ls in rows.items():
        ls.sort(key=lambda l: l["bbox"][0][0])
        full = "".join(l["text"] for l in ls)
        if "指数" not in full:
            continue
        # 只有常见 tab 词才认为是 tab 栏，避免误触正文
        if not any(t in full for t in ("直播", "聊天", "数据", "会员", "让球", "欧赔", "必发")):
            continue
        idx = full.index("指数")
        left = ls[0]["bbox"][0][0]
        right = ls[-1]["bbox"][2][0]
        width = right - left
        if width <= 0:
            continue
        # 按前面字符数估算"指数"中心 x(每个字符均分宽度)
        cx = left + (idx + 1.0) * (width / max(len(full), 1))
        return cy, cx
    return None, None


def find_1x2_subtab(lines: list[dict]) -> tuple[float | None, float | None]:
    """从 指数 页 OCR 行中定位「胜平负」sub-tab 坐标。

    雷速 指数页 sub-tab 行形如: 让球 | 胜平负 | 总进球 | 角球 | 必发 (cy≈624)。
    该页默认显示方案/专家卡, 必须显式点 胜平负 才出赔率表。
    返回 (cy, 胜平负_cx); 找不到返回 (None, None)。
    """
    # 按 cy(±15px) 归并同行文本
    rows: dict[float, list] = {}
    for ln in lines:
        cy = ln["bbox"][0][1]
        key = min(rows.keys(), key=lambda k: abs(k - cy)) if rows else None
        if key is not None and abs(key - cy) <= 15:
            rows[key].append(ln)
        else:
            rows[cy] = [ln]
    for cy, ls in rows.items():
        txt = "".join(l["text"] for l in ls)
        if "胜平负" in txt and ("让球" in txt or "总进球" in txt):
            for l in ls:
                if "胜平负" in l["text"]:
                    return cy, l["bbox"][0][0]
    return None, None


def _find_subtab_by_kw(lines: list[dict], kw: str) -> tuple[float | None, float | None]:
    """通用 subtab 定位: 在 sub-tab 行(含 胜平负/让球/总进球 等)中找含 kw 的单元格坐标。"""
    rows: dict[float, list] = {}
    for ln in lines:
        cy = ln["bbox"][0][1]
        key = min(rows.keys(), key=lambda k: abs(k - cy)) if rows else None
        if key is not None and abs(key - cy) <= 15:
            rows[key].append(ln)
        else:
            rows[cy] = [ln]
    for cy, ls in rows.items():
        txt = "".join(l["text"] for l in ls)
        # sub-tab 行须含多个市场词, 避免误触正文
        if kw in txt and any(t in txt for t in ("胜平负", "让球", "总进球", "角球", "必发")):
            for l in ls:
                if kw in l["text"]:
                    return cy, l["bbox"][0][0]
    return None, None


def find_ah_subtab(lines: list[dict]) -> tuple[float | None, float | None]:
    """定位「让球」sub-tab (AH 市场)。"""
    return _find_subtab_by_kw(lines, "让球")


def find_ou_subtab(lines: list[dict]) -> tuple[float | None, float | None]:
    """定位「总进球」sub-tab (OU 大小球市场)。"""
    return _find_subtab_by_kw(lines, "总进球")


def find_corner_subtab(lines: list[dict]) -> tuple[float | None, float | None]:
    """定位「角球」sub-tab。"""
    return _find_subtab_by_kw(lines, "角球")


def parse_home_matches(png: str | Path, today: datetime.date) -> list[dict]:
    """从雷速首页截图解析「今日」比赛列表。

    返回 list[dict]: {home_raw, away_raw, kickoff_ts, card_cy}
      - home_raw/away_raw: OCR 原始队名(已去 [rank])
      - kickoff_ts: 今日日期 + 卡头 HH:MM (本地 +08); 解析失败为 None
      - card_cy: 卡片中点 y, 用于 tap 进详情

    首页结构(POC 实测 2026-07-25): 每场 = [联赛+HH:MM]行 → [rank]主VS客[rank]行
      → 周六20X 行; 明日比赛前出现「YYYY/MM/DD星期X」分隔行, 其 cy 以下为明日。
    部分比赛文本在同 cy 拆成多 OCR 行(如 [3]巴拉纳竞技 / VS / 巴西国际[14]),
    故先按 cy(±15px) 合并同行文本再正则。
    """
    try:
        ocr = get_ocr_engine()
    except ImportError:
        return []
    result, _ = ocr(str(png))
    lines = _to_lines(result)
    if not lines:
        return []

    # 1) 按 cy 分组(±15px) → 合并同 cy 文本(x 升序)
    lines.sort(key=lambda ln: ln["bbox"][0][1])
    rows: list[tuple[float, str]] = []
    cur_cy = None
    cur_lines: list = []
    for ln in lines:
        cy = ln["bbox"][0][1]
        if cur_cy is None or abs(cy - cur_cy) <= 15:
            cur_lines.append(ln)
            cur_cy = cy if cur_cy is None else (cur_cy + cy) / 2.0
        else:
            cur_lines.sort(key=lambda l: l["bbox"][0][0])
            rows.append((cur_cy, "".join(l["text"] for l in cur_lines)))
            cur_lines = [ln]
            cur_cy = cy
    if cur_lines:
        cur_lines.sort(key=lambda l: l["bbox"][0][0])
        rows.append((cur_cy, "".join(l["text"] for l in cur_lines)))

    # 2) 找日期分隔行(>今日) → 其 cy 以下为明日, 跳过
    cutoff_cy = None
    for cy, text in rows:
        m = _DATE_PAT.search(text)
        if m:
            try:
                d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if d > today:
                    cutoff_cy = cy
                    break
            except Exception:
                pass

    # 3) 比赛行 + kickoff
    matches: list[dict] = []
    seen: set = set()
    for cy, text in rows:
        if cutoff_cy is not None and cy > cutoff_cy:
            continue
        mm = _MATCH_PAT.search(text)
        if not mm:
            continue
        home = mm.group(2).strip()
        away = mm.group(3).strip()
        key = (home, away)
        if key in seen:
            continue
        seen.add(key)
        # kickoff: 同卡 league 头(cy-80 ~ cy-10) 中 HH:MM
        kickoff = None
        for rcy, rtext in rows:
            if cy - 80 <= rcy <= cy - 10:
                tm = _HHMM_PAT.search(rtext)
                if tm:
                    kickoff = (int(tm.group(1)), int(tm.group(2)))
                    break
        kickoff_ts = None
        if kickoff:
            try:
                dt = datetime.datetime(
                    today.year, today.month, today.day, kickoff[0], kickoff[1],
                    tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
                )
                kickoff_ts = int(dt.timestamp())
            except Exception:
                pass
        matches.append({
            "home_raw": home, "away_raw": away,
            "kickoff_ts": kickoff_ts, "card_cy": cy,
        })
    return matches


def collect_today(adb: "ADB", cur: sqlite3.Cursor, market: str,
                  today: datetime.date, limit: int | None = None) -> int:
    """采集首页今日所有比赛: 首页→逐卡 tap→详情→指数→胜平负→OCR→入库→返回。

    带 captcha 检测复位 + 已采去重 + 失败卡不再重试(防死循环) + 滚动加载更多。
    返回采集场数。
    """
    total = 0
    collected: set = set()      # 成功入库
    attempted: set = set()      # 已尝试(成功/失败均记录, 不再重试)
    scroll_rounds = 0
    max_scroll = 4
    max_iter = 60               # 总循环上限, 杜绝任何意外死循环
    it = 0
    # 场间参数(根源修复 2026-07-30 雷速天级流控):
    #   DETAIL_INTERVAL 6→60s   每场间隔大幅延长, 让会话级风控有窗口自然衰减
    #   MAX_DAILY_DETAIL 40→12 单日详情页预算下调, 宁可少跑也别触发连坐封禁
    #   COOLDOWN 3→5          每解一次滑块后冷却 5 场
    #   BACKOFF_FACTOR 指数衰减因子, 连续 N 次触发滑块就把间隔乘以 factor**N
    DETAIL_INTERVAL   = 120     # 秒, 等自然解禁后首跑更保守(旧60/6)
    # 2026-08-01 修正预算语义: 预算 = **详情页实际打开次数**(含 OCR 失败),
    # 因为对服务端风控而言"打开详情页"才是请求, OCR 成败是客户端的事。
    # 旧写法 `total + len(collected)` 把成功场次算了两遍(两者恒等) → 名义 4 实际只允许
    # 2 场成功; 同时 OCR 失败完全不计数 → 今日实开 10 次详情页却"没花预算",
    # 既卡死产出又没挡住封禁风险, 两头落空。
    MAX_DAILY_DETAIL  = 12      # 详情页日预算(按实开次数计, 对齐注释既定的 40→12)
    COOLDOWN_AFTER_CAP = 5      # 解滑块后冷却场数 (旧 3 不够)
    _BACKOFF_FACTOR   = 1.6     # 连续触发滑块时 interval *= 1.6**consecutive
    adb.ensure_home()           # 进入采集前确保首页(解滑块/关弹窗/回列表)
    captcha_cooldown = 0        # 滑块冷却计数器 (进入 while 前必须初始化, 不然 UnboundLocalError)
    consecutive_captchas = 0    # 连续触发滑块计数(给 BACKOFF 用)
    detail_opens = 0            # 详情页实际打开次数(含OCR失败) = 真正的风控预算口径
    budget_hit = False          # 预算耗尽标志, 用于跳出外层 while(旧 break 只跳内层for)
    while it < max_iter and not budget_hit:
        it += 1
        on_home, solved = adb.ensure_home()   # 每轮确保首页(防解滑块后漂移)
        if solved:
            captcha_cooldown = 3              # 冷却3场(防连续触发天级流控)
        png = adb.screencap()
        matches = parse_home_matches(png, today)
        # 首页兜底: 若一屏未解析到比赛(可能在详情页), 返回一次再试
        if not matches and scroll_rounds == 0:
            adb._run(["shell", "input", "keyevent", "4"])
            time.sleep(1.5)
            scroll_rounds = 1
            continue
        todo = [m for m in matches
                if (m["home_raw"], m["away_raw"]) not in collected
                and (m["home_raw"], m["away_raw"]) not in attempted]
        if not todo:
            if scroll_rounds >= max_scroll:
                break
            # 滚动加载更多今日赛程
            adb.swipe(450, 1450, 450, 350, 600)
            time.sleep(2.0)
            scroll_rounds += 1
            continue
        for m in todo:
            if limit is not None and total >= limit:
                break
            # 防线: 日预算上限(按详情页实开次数)
            if detail_opens >= MAX_DAILY_DETAIL:
                print(f"[today] 达到日详情页预算({MAX_DAILY_DETAIL} 次实开), 停止采集")
                budget_hit = True     # 同时终止外层 while, 否则会空转到 max_iter
                break
            # 防线: 滑块冷却期(解完滑块后跳过N场, 让 session 恢复)
            if captcha_cooldown > 0:
                captcha_cooldown -= 1
                attempted.add((m["home_raw"], m["away_raw"]))
                print(f"[today] 冷却期跳过: {m['home_raw']} vs {m['away_raw']}")
                continue
            detail_opens += 1                    # 计入风控预算(无论后续 OCR 成败)
            adb.tap(450, int(m["card_cy"]))      # 进详情
            time.sleep(3.5)                       # 详情页加载(原2.5s偏短)
            if adb.detect_aliyun_captcha(adb.screencap()):
                print("[today] 详情页触发天级流控, 自动复位")
                adb.solve_captcha()
                time.sleep(6)
                captcha_cooldown = 3  # 解完滑块后冷却3场
            adb.enter_detail_market(market)     # 指数 → 对应市场 subtab(动态定位 + 黑屏重试)
            png2 = adb.screencap()
            rows = ocr_odds_matrix(png2, market)
            if not rows:
                # 单次重试: 赔率页可能仍在渲染/黑屏
                time.sleep(2.5)
                png2 = adb.screencap()
                rows = ocr_odds_matrix(png2, market)
            if rows:
                mid = align_match(m["home_raw"], m["away_raw"], m["kickoff_ts"], cur)
                insert_leisu_odds(mid, m["home_raw"], m["away_raw"], m["kickoff_ts"],
                                  market, rows, cur, source="leisu")
                total += 1
                collected.add((m["home_raw"], m["away_raw"]))
                # 增量提交: 旧代码只在流程末尾 commit, 一旦中途被杀/崩溃, 本轮
                # 已采数据全部随事务回滚丢失(2026-08-01 空转 20min 期间即处于此风险)。
                try:
                    cur.connection.commit()
                except Exception as _e:
                    print(f"[today] 增量提交失败(继续): {_e}")
                print(f"[today] 入库 {m['home_raw']} vs {m['away_raw']}: "
                      f"{len(rows)} 行 (match_id={mid})")
            else:
                print(f"[today] OCR 失败(跳过): {m['home_raw']} vs {m['away_raw']}")
            attempted.add((m["home_raw"], m["away_raw"]))
            adb._run(["shell", "input", "keyevent", "4"])   # 返回首页
            time.sleep(DETAIL_INTERVAL)          # 场间间隔(根源修复: 防 天级流控)
        if limit is not None and total >= limit:
            break
    return total


# ===================== Mock 管线验证 =====================
# 佛山南狮 vs 无锡吴钩 6 庄家示例（方案 §7）。主客平赔合理值，返回率 89.89~90.88%。
# 末家「立*」刻意偏离(主胜压到 2.05)，用于验证 soft_line 检测可触发。
MOCK_FIXTURE = {
    "home_raw": "佛山南狮",
    "away_raw": "无锡吴钩",
    "kickoff_ts": int(datetime.datetime(2026, 7, 26, 19, 30).timestamp()),
    "market": "1X2",
    "rows": [
        ("3*",   2.30, 3.10, 2.80, 89.9, "19:26"),
        ("星*",   2.28, 3.15, 2.85, 90.1, "19:25"),
        ("厝***", 2.35, 3.05, 2.75, 90.0, "19:24"),
        ("澳*",   2.32, 3.08, 2.82, 90.2, "19:26"),
        ("10*",   2.25, 3.12, 2.88, 89.9, "19:23"),
        ("立*",   2.05, 3.00, 3.60, 90.9, "19:20"),  # 偏离家：触发 soft_line
    ],
}


def run_mock(db_path: str | Path) -> None:
    """--mock：不经 MuMu/OCR，直接注入 MOCK_FIXTURE 到 leisu_odds（用于管线验证）。"""
    db_path = Path(db_path)
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    n = 0
    try:  # P3-2: 等价 with/finally，异常仍释放连接
        cur = conn.cursor()
        fx = MOCK_FIXTURE
        # 幂等：先清该 fixture 历史行，避免重复累积（同一场每次 --mock 只保留最新 6 行）
        cur.execute(
            "DELETE FROM leisu_odds WHERE home_raw=? AND away_raw=? AND market=?",
            (fx["home_raw"], fx["away_raw"], fx["market"]),
        )
        mid = align_match(fx["home_raw"], fx["away_raw"], fx.get("kickoff_ts"), cur)
        cap = int(time.time())
        insert_leisu_odds(mid, fx["home_raw"], fx["away_raw"], fx.get("kickoff_ts"),
                          fx["market"], fx["rows"], cur, cap, source="mock")  # P2-2: 标注 mock
        conn.commit()
        n = cur.execute(
            "SELECT COUNT(*) FROM leisu_odds WHERE home_raw=? AND away_raw=? AND market=?",
            (fx["home_raw"], fx["away_raw"], fx["market"]),
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"[mock] 已注入 {n} 行 (match_id={mid}, source=mock) -> {db_path}")


# ===================== 运行模式 =====================
def run_once(db_path: str | Path, args: argparse.Namespace) -> None:
    adb = ADB()
    try:
        adb.connect()
    except Exception as e:
        print(f"[once] ADB 连接失败: {e}；无 MuMu 环境请用 --mock")
        return
    conn = sqlite3.connect(str(db_path))
    try:  # P3-2: with/finally 等价保护，确保异常也释放连接
        cur = conn.cursor()
        collect_round(adb, cur, market=args.market,
                      home_raw=args.home, away_raw=args.away, kickoff_ts=args.kickoff)
        conn.commit()
    finally:
        conn.close()


def run_daemon(db_path: str | Path, args: argparse.Namespace) -> None:
    """守护进程：每 2h 一轮（严禁 60s/轮，避免升级风控）。"""
    adb = ADB()
    while True:
        try:
            adb.connect()
        except Exception:
            print("[daemon] ADB 不可达，60s 后重试")
            time.sleep(60)
            continue
        # P3-3/4: 连接移入 try，异常时 rollback，finally 释放
        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            collect_round(adb, cur, market=args.market,
                          home_raw=args.home, away_raw=args.away, kickoff_ts=args.kickoff)
            conn.commit()
        except Exception as e:
            print(f"[daemon] 本轮异常: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        print("[daemon] 完成一轮，2h 后再次采集")
        time.sleep(7200)   # 2h/轮


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="雷速体育第二庄源采集器 (P2)")
    ap.add_argument("--init", action="store_true", help="仅建表（幂等）")
    ap.add_argument("--mock", action="store_true", help="注入 MOCK_FIXTURE 验证管线")
    ap.add_argument("--once", action="store_true", help="单次采集（导航→截图→OCR→对齐→入库）")
    ap.add_argument("--daemon", action="store_true", help="2h/轮守护进程")
    ap.add_argument("--market", default="1X2", help="市场: 1X2 / AH / OU / CORNER")
    ap.add_argument("--parse-only", default=None,
                    help="离线校验: 对给定截图 PNG 跑 ocr_odds_matrix(market) 并打印解析结果(无需 MuMu/ADB)")
    ap.add_argument("--home", default=None, help="主队原始名(用于对齐，可选)")
    ap.add_argument("--away", default=None, help="客队原始名(用于对齐，可选)")
    ap.add_argument("--kickoff", type=int, default=None, help="开赛 unix 时间戳(用于对齐，可选)")
    ap.add_argument("--today", action="store_true",
                    help="一键采集首页「今日」所有比赛(自动迭代, 不再需 --home/--away/--kickoff)")
    ap.add_argument("--limit", type=int, default=None,
                    help="--today 模式单轮最多采集场数(安全/调试用, 默认全部)")
    ap.add_argument("--db", default=FOOTBALL_DB_PATH, help="football_data.db 路径")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    dbp = Path(args.db)
    # P1-1: 路径围栏——仅允许写入 D:/Architecture/data 目录内，杜绝任意路径写库
    root = Path("D:/Architecture/data").resolve()
    dbp = dbp.resolve()
    if dbp != root and root not in dbp.parents:
        sys.exit(f"[安全] --db 越界：仅允许写入 {root} 内，收到 {dbp}")
    if args.init:
        init_db(dbp)
        return
    if args.parse_only:
        # 离线校验: 仅 OCR 解析给定 PNG, 不连 ADB/不入库
        from pathlib import Path as _P
        png = _P(args.parse_only)
        if not png.exists():
            sys.exit(f"[parse-only] 文件不存在: {png}")
        print(f"[parse-only] 解析 {png} (market={args.market})")
        rows = ocr_odds_matrix(png, market=args.market)
        if rows is None:
            print("[parse-only] 引擎不可用或抽取失败 (无 rapidocr 时静默 None)")
        elif not rows:
            print("[parse-only] 未抽取到任何行")
        else:
            print(f"[parse-only] 抽取 {len(rows)} 行:")
            for bt, h, d, a, ret, ts in rows:
                line_s = f" line={d}" if args.market in ("AH", "OU") else ""
                print(f"   {bt}: {h} / {d} / {a}{line_s}  ret={ret} ts={ts}")
        return
    if args.mock:
        run_mock(dbp)
        return
    if args.once:
        init_db(dbp)
        run_once(dbp, args)
        return
    if args.today:
        init_db(dbp)
        adb = ADB()
        try:
            adb.connect()
        except Exception as e:
            print(f"⚠️ MuMu/ADB 不可达({e})，无法采集今日比赛")
            return
        adb.launch_leisu()           # 确保在前台
        time.sleep(3)
        conn = sqlite3.connect(str(dbp))
        try:
            cur = conn.cursor()
            today = datetime.date.today()
            n = collect_today(adb, cur, args.market, today, limit=args.limit)
            conn.commit()
            print(f"✅ 今日({(today.strftime('%Y-%m-%d'))}) 共采集 {n} 场 → leisu_odds")
        finally:
            conn.close()
        return
    if args.daemon:
        init_db(dbp)
        run_daemon(dbp, args)
        return
    # 默认：建表 + 单次；若 MuMu/ADB 不可达则提示 --mock
    init_db(dbp)
    adb = ADB()
    try:
        adb.connect()
    except Exception as e:
        print(f"⚠️ MuMu/ADB 不可达({e})，请用 --mock 注入验证数据")
        return
    run_once(dbp, args)


if __name__ == "__main__":
    main()
