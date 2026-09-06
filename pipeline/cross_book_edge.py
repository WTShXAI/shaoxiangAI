"""跨庄软线偏离检测引擎 — 哨响AI 真 edge 信号源 (SSoT)

为什么存在
----------
MEMORY 铁律: 真 edge 来源 = 跨庄/跨市场 soft line 价差; 单庄去水不具此条件。
unified_predictor 已于 v7.2 恢复真训练模型 (Chain3 Revival, OOF AUC宏=0.706/LogLoss=0.958, 独立于市场);
本模块为其外的跨庄真 edge 补充源 (P0-2: 待接入独立实时赔率源以扩到 live)。
本模块把 long_images.db 中 OCR 抽出的「多机构 1X2 赔率」变成可下注信号:
  1. 逐庄 devig(去水) → 各庄隐含概率
  2. 共识 = 各庄概率中位数 (抗离群)
  3. 软线检测: 单庄某选项概率与共识差 > 阈值 → 该庄该选项偏离(可价值下注/避险)
  4. 最佳价: 每选项全市场最高赔(实际可下注的最优于共识之价)

数据来源
--------
data/long_images.db.cross_book_odds (由 scripts/long_images_v2.py 抽取, 419 张 obscure 联赛截图)
仅 obscure 联赛(日联/芬超/挪超/美职联/丹超/瑞典超…), 主流联赛不在本数据集。

用法
----
  from pipeline.cross_book_edge import analyze_all, to_report
  edges = analyze_all()                      # list[MatchEdge]
  report = to_report(edges)                  # 可序列化 dict
  # 或命令行: python -m pipeline.cross_book_edge
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from statistics import median

DB_PATH = Path("data/long_images.db")

# 偏离阈值(pp): 单庄某选项隐含概率与共识差超过此值即标为软线
DEFAULT_SOFT_LINE_PP = 5.0

# 严重度分级(pp) — 与铁律告警阈值一致 (HIGH≥15 / MED≥10 / LOW≥5)
SEV_HIGH_PP = 15.0
SEV_MED_PP = 10.0
SEV_LOW_PP = 5.0


def classify_severity(spread_pp: float) -> str:
    """软线离散度 → 严重度 (HIGH/MED/LOW/空)。"""
    if spread_pp >= SEV_HIGH_PP:
        return "HIGH"
    if spread_pp >= SEV_MED_PP:
        return "MED"
    if spread_pp >= SEV_LOW_PP:
        return "LOW"
    return ""


# 让球盘行(官方(+1)/官方(-1)/让球 等)被采集器误存进 market='1X2' 桶,
# devig 出荒谬单选项概率(如 57.5%)污染软线信号。load_leisu_matches 做 1X2 时须剔除。
_HANDICAP_RE = re.compile(r"[\(（]|[+\-]\s*\d|让|盘")


def _is_invalid_1x2_book(book: str, oh, od, oa) -> bool:
    """1X2 行合法性闸门: 剔除让球盘行 + 赔率缺失 + 单选项隐含概率>0.9(列偏移/AH残留)。"""
    if not book:
        return True
    if _HANDICAP_RE.search(book):
        return True
    try:
        oh, od, oa = float(oh), float(od), float(oa)
    except (TypeError, ValueError):
        return True
    if not (oh > 1.0 and od > 1.0 and oa > 1.0):
        return True
    p = devig(oh, od, oa)
    if p is None:
        return True
    if max(p) > 0.90:          # 真实 1X2 单选项不可能>90%(去水后)
        return True
    return False


def devig(odds_h: float, odds_d: float, odds_a: float):
    """朴素去水: 1/o 归一化 → [p_h, p_d, p_a] (和≈1)。非正赔率返回 None。"""
    if odds_h <= 0 or odds_d <= 0 or odds_a <= 0:
        return None
    ih, id_, ia = 1.0 / odds_h, 1.0 / odds_d, 1.0 / odds_a
    s = ih + id_ + ia
    return ih / s, id_ / s, ia / s


def devig2(odds_h: float, odds_a: float):
    """2 项去水(OU 大/小, AH 主/客): 1/o 归一化 → [p_h, p_a]。非正赔率返回 None。"""
    if odds_h <= 0 or odds_a <= 0:
        return None
    ih, ia = 1.0 / odds_h, 1.0 / odds_a
    s = ih + ia
    return ih / s, ia / s


# 各市场选项规格: (selection 标签, BookRow 属性)。
#   1X2: h=主胜, d=平, a=客胜
#   OU : h=大球赔, a=小球赔  (d 位存盘口 line, 非概率)
#   AH : h=主队赔, a=客队赔  (d 位存盘口 line, 非概率)
SEL_SPECS = {
    "1X2": [("H", "h"), ("D", "d"), ("A", "a")],
    "OU":  [("O", "h"), ("U", "a")],
    "AH":  [("H", "h"), ("A", "a")],
}


def _robust_filter(books: list, attrs: tuple = ("h", "d", "a")) -> list:
    """剔除跨庄 per-selection 极端离群(列偏移/OCR 污染):

    某庄某选项赔率 > 3× 或 < 1/3 该选项中位数赔率 → 判为污染剔除。
    真实 sharp 偏差极少超 3×; 真 cross-book edge(本模块的捕捉目标)通常在
    5~50% 区间, 远小于 3×, 故不会被误杀。少于 3 庄时跳过(样本不足不滤)。
    attrs 指定参与过滤的属性: 1X2 用 (h,d,a); OU/AH 用 (h,a) 跳过盘口线 d
    (盘口线可能为负/跨值, 不应做 3× 中位数离群剔除)。
    """
    if len(books) < 3:
        return books
    med = {}
    for attr in attrs:
        vals = sorted(getattr(b, attr) for b in books if getattr(b, attr) > 0)
        if vals:
            med[attr] = vals[len(vals) // 2]
    keep = []
    for b in books:
        ok = True
        for attr in attrs:
            m = med.get(attr)
            if not m:
                continue
            v = getattr(b, attr)
            if v <= 0 or v > 3.0 * m or v < m / 3.0:
                ok = False
                break
        if ok:
            keep.append(b)
    return keep if keep else books   # 全剔除则保底返回原样, 防空场


@dataclass
class BookRow:
    bookmaker: str
    h: float
    d: float
    a: float
    h_prob: float = 0.0
    d_prob: float = 0.0
    a_prob: float = 0.0


@dataclass
class MatchEdge:
    league: str
    home: str
    away: str
    n_books: int
    books: list = field(default_factory=list)
    market: str = "1X2"                                  # 1X2 / OU / AH
    consensus: dict = field(default_factory=dict)        # {sel: 中位数概率}
    best: dict = field(default_factory=dict)             # {sel: 最佳价 {odds, bookmaker}}
    soft_lines: list = field(default_factory=list)       # 偏离>阈值: {book,sel,pp,prob,consensus}
    max_spread_pp: float = 0.0


def load_matches(db: Path = DB_PATH) -> list[dict]:
    """从 cross_book_odds 聚合为 match 列表 (过滤 league 空值/脏行)"""
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        """SELECT league, home, away, bookmaker, h_odds, d_odds, a_odds
           FROM cross_book_odds
           WHERE league IS NOT NULL AND home IS NOT NULL AND away IS NOT NULL
           ORDER BY league, home, away, bookmaker"""
    ).fetchall()
    conn.close()
    matches: dict = {}
    for lg, h, a, bk, oh, od, oa in rows:
        key = (lg, h, a)
        matches.setdefault(key, []).append(BookRow(bk, oh, od, oa))
    return [{"league": k[0], "home": k[1], "away": k[2], "books": v} for k, v in matches.items()]


def _best(book_rows: list[BookRow], sel: str):
    """返回某选项全市场最高赔 + 庄家"""
    attr = {"h": "h", "d": "d", "a": "a"}[sel]
    top = max(book_rows, key=lambda b: getattr(b, attr))
    return {"odds": round(getattr(top, attr), 3), "bookmaker": top.bookmaker}


def analyze_match(m: dict, soft_pp: float = DEFAULT_SOFT_LINE_PP, market: str = "1X2") -> MatchEdge:
    """逐场软线检测。market 决定选项规格(SEL_SPECS):
       1X2 → 3 项去水(H/D/A); OU/AH → 2 项去水(大/小 或 主/客), 盘口线不参与概率。
    """
    specs = SEL_SPECS.get(market, SEL_SPECS["1X2"])
    books = m["books"]
    valid = []
    for b in books:
        if market == "1X2":
            p = devig(b.h, b.d, b.a)
            if p is None:
                continue
            b.h_prob, b.d_prob, b.a_prob = p
        else:
            p = devig2(b.h, b.a)
            if p is None:
                continue
            b.h_prob, b.a_prob = p
            b.d_prob = 0.0
        valid.append(b)
    if not valid:
        return MatchEdge(league=m["league"], home=m["home"], away=m["away"],
                         n_books=0, books=[], market=market)
    filter_attrs = ("h", "d", "a") if market == "1X2" else ("h", "a")
    books = _robust_filter(valid, attrs=filter_attrs)   # 剔除列偏移/OCR 污染离群

    # 共识 = 各选项概率中位数(抗离群) — 必须用 *_prob(概率), 非原始赔率
    cons = {}
    for sel, attr in specs:
        vals = sorted(getattr(b, attr + "_prob") for b in books if getattr(b, attr + "_prob") > 0)
        cons[sel] = vals[len(vals) // 2] if vals else 0.0

    best = {sel: _best(books, attr) for sel, attr in specs}

    soft = []
    for b in books:
        for sel, attr in specs:
            bp = cons[sel]
            op = getattr(b, attr + "_prob")
            if bp <= 0 or op <= 0:
                continue
            diff = abs(bp - op) * 100.0
            if diff >= soft_pp:
                soft.append({
                    "book": b.bookmaker, "sel": sel,
                    "pp": round(diff, 2),
                    "prob": round(op, 4), "consensus": round(bp, 4),
                })

    spreads = []
    for sel, attr in specs:
        ps = [getattr(b, attr + "_prob") for b in books if getattr(b, attr + "_prob") > 0]
        if len(ps) >= 2:
            spreads.append(max(ps) - min(ps))
    max_spread = max(spreads) if spreads else 0.0

    return MatchEdge(
        league=m["league"], home=m["home"], away=m["away"],
        n_books=len(books), books=books, market=market,
        consensus=cons, best=best, soft_lines=soft,
        max_spread_pp=round(max_spread * 100, 2),
    )


def analyze_all(db: Path = DB_PATH, soft_pp: float = DEFAULT_SOFT_LINE_PP,
                source: str = "long_images", include_mock: bool = False,
                market: str = "1X2") -> list[MatchEdge]:
    """逐场软线检测。source='leisu' 时改走雷速 leisu_odds 路径（保持默认 long_images 不变）。

    leisu 数据固定在 LEISU_DB_PATH（与 long_images 分库），故 leisu 分支忽略 db 参数，
    仅透传 include_mock（P3-8：明确 db 的取值边界，避免误用 long_images 库查询 leisu 表）。
    market 指定检测市场(1X2/OU/AH)。
    """
    if source == "leisu":
        return analyze_all_leisu(db=LEISU_DB_PATH, soft_pp=soft_pp,
                                 include_mock=include_mock, market=market)
    return [analyze_match(m, soft_pp, market="1X2") for m in load_matches(db)]


def to_report(edges: list[MatchEdge], soft_pp: float = DEFAULT_SOFT_LINE_PP,
              with_actionable: bool = False) -> dict:
    matches = []
    n_soft = sum(1 for e in edges if e.soft_lines)
    for e in edges:
        m = {
            "league": e.league, "home": e.home, "away": e.away,
            "market": e.market,
            "n_books": e.n_books,
            "consensus": e.consensus,
            "best": e.best,
            "max_spread_pp": e.max_spread_pp,
            "severity": classify_severity(e.max_spread_pp),
            "n_soft_lines": len(e.soft_lines),
            "soft_lines": e.soft_lines,
        }
        if with_actionable:
            m["actionable"] = actionable_bets(e)
        matches.append(m)
    matches.sort(key=lambda m: m["max_spread_pp"], reverse=True)
    return {
        "module": "cross_book_edge",
        "source": "data/long_images.db.cross_book_odds",
        "soft_line_threshold_pp": soft_pp,
        "n_matches": len(edges),
        "n_matches_with_soft_lines": n_soft,
        "matches": matches,
    }


def actionable_bets(edge: MatchEdge) -> list:
    """每场可下注的跨庄 edge: 取各选项全市场最佳价(最高赔)庄, 与共识公平赔比较价格优势。

    正 price_edge_pp = 该庄报价优于共识隐含公平赔(真 edge, 可在该庄下注获利);
    负 = 该庄报价劣于共识, 无 edge。返回按 price_edge_pp 降序。
    选项规格随 market 变化(1X2→H/D/A; OU→O/U; AH→H/A)。
    """
    out = []
    if edge.n_books < 2 or not edge.consensus:
        return out
    for sel, attr in SEL_SPECS.get(edge.market, SEL_SPECS["1X2"]):
        best = edge.best.get(sel)
        cons_prob = edge.consensus.get(sel)
        if not best or not cons_prob or cons_prob <= 0:
            continue
        cons_odds = 1.0 / cons_prob
        price_edge_pp = (best["odds"] - cons_odds) / cons_odds * 100.0
        if price_edge_pp <= 0:
            continue
        out.append({
            "selection": sel,
            "book": best["bookmaker"],
            "odds": best["odds"],
            "consensus_odds": round(cons_odds, 3),
            "price_edge_pp": round(price_edge_pp, 2),
            "severity": classify_severity(price_edge_pp),
        })
    out.sort(key=lambda b: b["price_edge_pp"], reverse=True)
    return out


_SEV_RANK = {"": 0, "LOW": 1, "MED": 2, "HIGH": 3}


def scan_actionable(edges: list[MatchEdge], min_severity: str = "HIGH") -> list:
    """gate 输出: 仅返回达到 min_severity 的可下注跨庄 edge (默认 HIGH≥15pp 才放注)。"""
    min_r = _SEV_RANK.get(min_severity, 3)
    out = []
    for e in edges:
        bets = [b for b in actionable_bets(e)
                if _SEV_RANK.get(b["severity"], 0) >= min_r]
        if bets:
            out.append({
                "home": e.home, "away": e.away, "league": e.league,
                "n_books": e.n_books, "max_spread_pp": e.max_spread_pp,
                "bets": bets,
            })
    out.sort(key=lambda m: m["max_spread_pp"], reverse=True)
    return out


def _console_summary(report: dict) -> str:
    lines = []
    lines.append(f"跨庄软线检测 — {report['n_matches']} 场, "
                 f"{report['n_matches_with_soft_lines']} 场存在软线(阈值 {report['soft_line_threshold_pp']}pp)")
    lines.append("=" * 72)
    for m in report["matches"]:
        c = m["consensus"]
        market = m.get("market", "1X2")
        specs = SEL_SPECS.get(market, SEL_SPECS["1X2"])
        cons_str = " ".join(f"{sel} {c[sel]*100:.1f}%" for sel, _ in specs if sel in c)
        best_str = " ".join(f"{sel} {m['best'][sel]['odds']}@{m['best'][sel]['bookmaker']}"
                            for sel, _ in specs if sel in m["best"])
        lines.append(f"[{m['league']}] {m['home']} vs {m['away']}  | {m['n_books']} 庄 | "
                     f"{market} | 离散 {m['max_spread_pp']}pp")
        lines.append(f"   共识 {cons_str}")
        lines.append(f"   最佳价: {best_str}")
        if m["soft_lines"]:
            for s in m["soft_lines"][:4]:
                lines.append(f"   ⚠ 软线 {s['book']} {s['sel']} 偏离 {s['pp']}pp "
                             f"(该庄 {s['prob']*100:.1f}% vs 共识 {s['consensus']*100:.1f}%)")
    return "\n".join(lines)


# ============================================================
# 雷速体育「第二庄源」扩展（P2，对应方案 §5）
#   从 leisu_odds 聚合多机构赔率 → 复用 analyze_match 软线检测。
#   向后兼容：默认 analyze_all() 仍走 long_images 路径。
# ============================================================
# 雷速数据落在 football_data.db（与 matches / team_canonical 同库）
LEISU_DB_PATH = Path("D:/Architecture/data/football_data.db")  # P3-7: 绝对路径，避免相对路径歧义


def load_leisu_matches(db: Path = LEISU_DB_PATH, market: str = "1X2", include_mock: bool = False) -> list[dict]:
    """从 leisu_odds 聚合为 match 列表（与 load_matches 同构）。

    分组键：match_id 非空用 match_id，否则用 (home_raw, away_raw, market)。
    market 默认 '1X2'。同一 (分组, 庄家) 只保留 capture_at 最新的快照。
    books 为 BookRow 列表；AH 市场用 odds_d 存盘口 line，odds_h/odds_a 存主/客赔。
    """
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            """SELECT match_id, home_raw, away_raw, market, book,
                      odds_h, odds_d, odds_a, line, capture_at
               FROM leisu_odds
               WHERE market = ?
                 AND (1 = ? OR source != 'mock')   -- P2-2: 默认排除 mock，避免污染软线信号
               ORDER BY COALESCE(match_id, -1), home_raw, away_raw, book, capture_at""",
            (market, 1 if include_mock else 0),
        ).fetchall()
    finally:
        conn.close()

    # 取每个 (分组键, 庄家) 最新 capture_at 的快照
    latest: dict = {}
    for mid, hr, ar, mk, bk, oh, od, oa, line, cap in rows:
        key = (mid if mid is not None else (hr, ar, mk), bk)
        cap = cap or 0
        if key not in latest or cap > latest[key][-1]:
            latest[key] = (mid, hr, ar, mk, bk, oh, od, oa, line, cap)

    # 按 (match_id | (home_raw,away_raw,market)) 聚合 BookRow
    groups: dict = {}
    for (_gk, _bk), (mid, hr, ar, mk, _bk2, oh, od, oa, line, _cap) in latest.items():
        grp = (mid, hr, ar, mk)  # mid 可能为 None；始终 4 元组(match_id,home,away,market) 便于统一解包
        # 数据质量闸门: 1X2 剔除让球盘污染行/AH残留/列偏移(单选项>90%)
        # ⚠ 用本层解包的 _bk2, 绝不可用外层循环的 bk(已泄漏为末值, 会导致全部庄家同名)
        if market == "1X2" and _is_invalid_1x2_book(_bk2, oh, od, oa):
            continue
        if market == "AH":
            # AH：h=主赔, d=盘口line(可负), a=客赔
            book_row = BookRow(_bk2, oh or 0.0, line or 0.0, oa or 0.0)
        elif market == "OU":
            # OU：h=大球赔, d=盘口line(如2.5), a=小球赔
            book_row = BookRow(_bk2, oh or 0.0, line or 0.0, oa or 0.0)
        else:
            book_row = BookRow(_bk2, oh or 0.0, od or 0.0, oa or 0.0)
        groups.setdefault(grp, []).append(book_row)

    out = []
    for grp, books in groups.items():
        _mid, hr, ar, _mk = grp   # grp 始终为 (match_id, home, away, market)
        out.append({"league": "", "home": hr, "away": ar, "books": books, "market": _mk})
    return out


def analyze_all_leisu(db: Path = LEISU_DB_PATH, soft_pp: float = DEFAULT_SOFT_LINE_PP,
                      include_mock: bool = False, market: str = "1X2") -> list[MatchEdge]:
    """复用 analyze_match（BookRow 结构一致）对雷速矩阵做软线检测。market 指定市场。"""
    return [analyze_match(m, soft_pp, market)
            for m in load_leisu_matches(db, market=market, include_mock=include_mock)]


def to_report_leisu(edges: list[MatchEdge], soft_pp: float = DEFAULT_SOFT_LINE_PP,
                    with_actionable: bool = False) -> dict:
    """与 to_report 同构，但 source 指向 leisu_odds。"""
    report = to_report(edges, soft_pp, with_actionable=with_actionable)
    report["source"] = "data/football_data.db.leisu_odds"
    report["module"] = "cross_book_edge_leisu"
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="long_images", choices=["long_images", "leisu"])
    ap.add_argument("--include-mock", action="store_true",
                    help="包含 mock 数据（仅 --source leisu 生效；默认排除，避免污染软线信号）")
    ap.add_argument("--min-severity", default="LOW", choices=["LOW", "MED", "HIGH"],
                    help="仅输出达到该严重度的可下注 edge (默认 LOW=全部; HIGH=仅≥15pp放注级)")
    ap.add_argument("--actionable-only", action="store_true",
                    help="只打印可下注 gate 结果(scan_actionable), 不打印完整软线报告")
    ap.add_argument("--market", default="1X2", choices=["1X2", "OU", "AH"],
                    help="检测市场(仅 --source leisu 生效; 默认 1X2; OU=大小, AH=让球)")
    args = ap.parse_args()
    if args.source == "leisu":
        edges = analyze_all(source="leisu", soft_pp=DEFAULT_SOFT_LINE_PP,
                            include_mock=args.include_mock, market=args.market)
        report = to_report_leisu(edges, DEFAULT_SOFT_LINE_PP, with_actionable=True)
        out = Path(f"data/cross_book_edge_leisu_{args.market}_report.json")
    else:
        edges = analyze_all(market="1X2")
        report = to_report(edges, with_actionable=True)
        out = Path("data/cross_book_edge_report.json")
    if args.actionable_only:
        scan = scan_actionable(edges, min_severity=args.min_severity)
        print(f"跨庄可下注 edge (≥{args.min_severity}): {len(scan)} 场")
        for m in scan:
            print(f"  [{m['league']}] {m['home']} vs {m['away']} | {m['n_books']}庄 | 离散 {m['max_spread_pp']}pp")
            for b in m["bets"]:
                print(f"    ▶ 下 {b['selection']} @ {b['odds']} ({b['book']})  价格优势 {b['price_edge_pp']}pp [{b['severity']}]")
    else:
        print(_console_summary(report))
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {out}")
