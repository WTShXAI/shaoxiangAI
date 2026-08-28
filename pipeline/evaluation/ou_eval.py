"""
pipeline.evaluation.ou_eval — OU (大小球) 大/小方向评估单一事实源 (SSoT)

本模块把"OU 大/小方向"评估从各脚本 (build_ou_validation.py / capture_ou_results.py)
的内联 GRADE_DIR/devig/settle 平行复制中抽离出来, 成为评估框架的唯一事实源:

  - ou_settle(total_goals, line)            -> 'OVER' / 'UNDER' / 'PUSH'
  - ou_devig(over_odds, under_odds)        -> (p_over, p_under)  去抽水隐含概率
  - grade_direction(line, over_odds?, under_odds?) -> ('OVER'/'UNDER'/'NEUTRAL', grade)
                                              **复用 ou_linkage.OU_HONESTY** (杜绝 GRADE_DIR 平行复制)。
                                              v2 市场感知化: 诚实盘跟随市场低赔侧, 仅 trap 盘反向。
  - binary_log_loss / binary_brier / binary_accuracy
                                              二元方向指标 (P(大) vs 实际大/小)
  - ou_direction_stats(records)             汇总命中率:
                                              模型方向 vs 市场favorite vs 永远买大/买小
  - run_ou_eval(db_path, out_path)          读取 ou_validation + ou_live_feed, 出报告

铁律:
  - 当前 ou_validation 仅 29 行且 100% 高比分 (内部 live_odds_raw 有偏样本);
    ou_live_feed 多数赛事尚无赛果 → 报告必须诚实标注样本偏差/不足, 不得宣称信号有效。
  - 方向判定只来自 OU_HONESTY grade (庄家诚实度语义), 不引入任何新的硬编码 line→方向表。
"""
from __future__ import annotations
import json
import math
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# 基础原语 (无外部依赖)
# ─────────────────────────────────────────────────────────────────────────────

def ou_settle(total_goals: int, line: float) -> str:
    """总进球 vs OU线 → 结算方向。total>line→OVER; total<line→UNDER; 相等→PUSH。"""
    if total_goals > line:
        return "OVER"
    if total_goals < line:
        return "UNDER"
    return "PUSH"


def ou_settle_fractional(total_goals, line: float) -> float:
    """买大球(over) 的结算收益倍率: +1全赢 / -1全输 / +0.5半赢 / -0.5半输 / 0走盘。

    正确处理亚洲盘 split line(quarter line, 中点表示):
      - .25 线(1.25=1/1.5 盘): 进球=floor(line) → 半输(-0.5: 低盘走盘+高盘输);
        进球<floor → 全输(-1); 进球>floor → 全赢(+1)。
      - .75 线(0.75=0.5/1 盘): 进球=ceil(line) → 半赢(+0.5: 低盘赢+高盘走盘);
        进球<ceil-1 → 全输; 进球>ceil → 全赢。
      - 整数/半整数线: >line +1 / <line -1 / ==line 0(走盘 PUSH)。

    为什么需要它: ou_settle 只给方向(OVER/UNDER/PUSH), 把 split 线的半赢/半输/走盘
    抹成全额赢输, 导致 ROI/资金结算把"半注"当"全注"(677 场边界球错算)。本函数是
    资金/ROI 结算的单一事实源, 方向命中率仍用 ou_settle。
    """
    if total_goals is None:
        return 0.0
    total = int(total_goals)
    frac = round(float(line) - int(line), 2)
    if abs(frac - 0.25) < 0.01:
        base = int(line)               # 1.25 -> 1
        if total == base:
            return -0.5                # 半输(低盘走盘, 高盘输)
        return 1.0 if total > line else -1.0
    if abs(frac - 0.75) < 0.01:
        base = int(line) + 1           # 0.75 -> 1
        if total == base:
            return 0.5                 # 半赢(低盘赢, 高盘走盘)
        return 1.0 if total > line else -1.0
    if total > line:
        return 1.0
    if total < line:
        return -1.0
    return 0.0                         # PUSH 走盘


def ou_devig(over_odds: float, under_odds: float) -> Tuple[float, float]:
    """大小球盘去抽水 → (P(大), P(小))。赔率无效返回 (0.0, 0.0)。"""
    if not (over_odds and under_odds and over_odds > 1.01 and under_odds > 1.01):
        return 0.0, 0.0
    inv = 1.0 / over_odds + 1.0 / under_odds
    if inv <= 0:
        return 0.0, 0.0
    return (1.0 / over_odds) / inv, (1.0 / under_odds) / inv


def grade_direction(line: float,
                    over_odds: Optional[float] = None,
                    under_odds: Optional[float] = None) -> Tuple[str, str]:
    """由 OU线 (+ 可选赔率) → (方向, grade)。方向派生自 ou_linkage.OU_HONESTY 语义, 杜绝平行复制。

    v2 (市场感知化, 2026-07-28):
      诚实盘 (honest_*) 跟随市场低赔侧 (赔率隐含概率) 方向 —— 公平盘无 exploitable edge,
      故不押固定方向, 用庄家自己定出的概率走。仅 trap_* 盘保持反向(庄家诱导方向的反面),
      那是模型唯一的 edge 来源。
      未提供赔率时回退到纯线查表 (向后兼容 capture_ou_results.py 等旧调用方)。

    语义映射 (与 ou_linkage v5.1 注释一致):
      honest_low / trap_low          -> UNDER   基线 (诚实小球 / 诱导买大→实际小)
      honest_mid                     -> NEUTRAL  (诚实中球, 不押方向)
      honest_high / trap_high / trap_high_side -> OVER 基线 (诚实大球 / 诱导买小→实际大)
    """
    from pipeline.predictors.ou_linkage import OULinkageEngine  # 延迟导入, 保持模块轻量
    info = OULinkageEngine.get_ou_honesty(line)
    grade = info["grade"]
    if grade in ("honest_low", "trap_low"):
        base = "UNDER"
    elif grade == "honest_mid":
        base = "NEUTRAL"
    else:  # honest_high / trap_high / trap_high_side
        base = "OVER"

    # 市场感知化: 诚实盘跟随市场低赔侧; trap 盘保持反向(不跟市场)
    if (grade.startswith("honest") and over_odds and under_odds
            and over_odds > 1.01 and under_odds > 1.01):
        mkt = "OVER" if over_odds < under_odds else "UNDER"
        return mkt, grade + "_mkt"
    return base, grade


def ou_confidence(line: float,
                  over_odds: Optional[float] = None,
                  under_odds: Optional[float] = None) -> float:
    """OU 方向置信度 (0-1, 2026-08-01 校准): 市场一边倒 + 盘口线极端 → 高置信。

    验证 (GQ match_outcomes 926样本, 用所有OU数据): 置信度分桶模型准确率单调分层 —
      conf<0.3: 46.6% / 0.3-0.5: 52.3% / 0.5-0.7: 77.0% / conf>=0.7: 97.7%。
    用途: 选择性投注闸门, 仅高置信方向下注, 低置信弃权。
    """
    p_over, _ = ou_devig(over_odds, under_odds)
    p_fav = max(p_over, 1.0 - p_over) if p_over > 0 else 0.5
    conf_market = (p_fav - 0.5) * 2.0              # 市场低赔侧强度 0-1
    conf_line = min(abs(line - 2.75) / 2.0, 1.0)   # 盘口线极端度 0-1 (距主流中心2.75)
    return round(0.5 * conf_market + 0.5 * conf_line, 3)


# ─────────────────────────────────────────────────────────────────────────────
# 下盘校准 (0-2 球段, 2026-08-03 用户批准) — 用赛果真值软校准庄家隐含下盘概率
# ─────────────────────────────────────────────────────────────────────────────

# 全局真值回退: 历史库 31.2万场 total_goals 真值 under2.5 ≈ 49.91%
# (见 scripts/analyze_lowscore_structure.py 双口径分析). 联赛缺失时用.
_GLOBAL_UNDER_FALLBACK = 0.4991
_LEAGUE_OU_PROB_CACHE: Optional[Dict[str, Dict[str, float]]] = None


def load_league_ou_prob(csv_path: Optional[str] = None) -> Dict[str, Dict[str, float]]:
    """加载 data/league_ou_prob.csv → {league: {under_0..under_4, over_0..over_4, matches}}.

    用途: 0-2 球段(下盘)校准的真值锚. 每联赛真实 under_L 概率来自 31万场赛果分布
    (scripts/league_ou_prob.py 已生成). 联赛缺失时回退到全局真值. 模块级缓存避免重复IO.
    """
    global _LEAGUE_OU_PROB_CACHE
    if _LEAGUE_OU_PROB_CACHE is not None:
        return _LEAGUE_OU_PROB_CACHE
    if csv_path is None:
        csv_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data", "league_ou_prob.csv")
    out: Dict[str, Dict[str, float]] = {}
    if os.path.exists(csv_path):
        import csv as _csv
        with open(csv_path, encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                lg = row.get("league")
                if not lg:
                    continue
                d: Dict[str, float] = {}
                for k, v in row.items():
                    if k == "league":
                        continue
                    try:
                        d[k] = float(v)
                    except (TypeError, ValueError):
                        pass
                out[lg] = d
    _LEAGUE_OU_PROB_CACHE = out
    return out


def calibrate_ou_under(p_under_implied: float, line: float,
                        league: Optional[str] = None,
                        csv_path: Optional[str] = None,
                        k: float = 0.18, cap_pp: float = 0.04) -> Dict[str, Any]:
    """0-2 球段下盘校准: 当庄家隐含下盘概率被系统性低估时, 温和上修.

    依据 (2026-08-03 双口径分析, 真值底=31万场赛果 + 19.6万行 OU):
      乐鱼在 0-2 球段(线≤2.5) 对下盘概率系统性低估 ~ +10~25pp,
      按隐含分段: 30-40%→+24.6 / 40-50%→+16.3 / 50-60%→+9.6 / 60-70%→+3.9 / 70%+→-2.6(不动).

    方法 (守纪律):
      - 仅 line<=2.5 (0-2 球清晰段) 启用; 更高盘口分析未覆盖, 不动作.
      - 真值来源: 该联赛 league_ou_prob.under_{floor(line)} (无则全局回退 49.91%).
      - 仅当 p_under_implied<0.70 且 真值>隐含 (庄家低估) 时校准.
      - 上修幅度 = min(bias*K, cap_pp): 部分信任真值(K=0.35), 单次上限10pp防过冲/小样本噪声.
      - 高隐含段(>70%)庄家反而准, 不动作.

    与 ou_eval 铁律(第21行"不引入硬编码 line→方向表")不冲突:
      本函数不输出方向, 只软校准概率幅度, 且真值来自数据(非硬编码);
      方向判定仍由 grade_direction (OU_HONESTY 语义) 负责. 亦不脱离盘口锚定
      (庄家去水仍是主成分, 仅上修≤10pp).

    返回: {p_over, p_under, calibrated, delta_pp, truth, league_used}.
    """
    res = {"p_over": 1.0 - p_under_implied, "p_under": p_under_implied,
           "calibrated": False, "delta_pp": 0.0,
           "truth": None, "league_used": None}

    if line <= 0 or line > 2.5:
        return res  # 非 0-2 球段, 不动

    # 取真值 under (line=2.5→under_2; 2.25/2.75→under_2; 1.5→under_1; 0.5→under_0)
    col = f"under_{int(math.floor(line))}"
    truth = None
    lg_used = None
    table = load_league_ou_prob(csv_path)
    if league and league in table and col in table[league]:
        truth = table[league][col]
        lg_used = league
    else:
        truth = _GLOBAL_UNDER_FALLBACK
        lg_used = "<global>"

    if truth is None:
        return res

    res["truth"] = truth
    res["league_used"] = lg_used

    bias = truth - p_under_implied
    # 仅校"庄家低估"且隐含不算太高(>70% 庄家准, 不动作)
    if bias <= 0 or p_under_implied >= 0.70:
        return res

    delta = min(bias * k, cap_pp)
    # 防过冲: 校准后不超过赛果真值(避免用偏高全局值对小样本联赛过度上修)
    p_under_cal = min(p_under_implied + delta, truth)
    res["p_under"] = round(p_under_cal, 4)
    res["p_over"] = round(1.0 - p_under_cal, 4)
    res["calibrated"] = True
    res["delta_pp"] = round(delta * 100, 2)
    return res


def grade_direction_gated(line: float,
                          over_odds: Optional[float] = None,
                          under_odds: Optional[float] = None,
                          min_conf: float = 0.0) -> Tuple[str, str, float]:
    """grade_direction + 置信度闸门 (2026-08-01 OU校准, 用所有OU数据验证)。

    OU校准核心发现: 模型在高置信区(市场一边倒/盘口线极端)准确率 77-98%, 低置信区(<0.3)仅 46.6%。
    故输出置信度供选择性投注: 只在有把握时下注, 模糊带弃权 — 对ROI真正有用的OU校准。
    返回 (direction, grade, confidence); 置信度<min_conf 或 NEUTRAL 时 direction='PASS'。
    """
    direction, grade = grade_direction(line, over_odds, under_odds)
    conf = ou_confidence(line, over_odds, under_odds)
    if conf < min_conf or direction == 'NEUTRAL':
        return 'PASS', grade, conf
    return direction, grade, conf


# ─────────────────────────────────────────────────────────────────────────────
# 二元方向指标
# ─────────────────────────────────────────────────────────────────────────────

def binary_log_loss(probs_over: List[float], y_over: List[int]) -> Optional[float]:
    """二元 LogLoss。probs_over=P(大); y_over=1 表示实际大球 否则 0。空返回 None。"""
    if not probs_over:
        return None
    tot = 0.0
    for p, y in zip(probs_over, y_over):
        pp = max(min(p, 1.0 - 1e-12), 1e-12)
        tot += -(y * math.log(pp) + (1 - y) * math.log(1 - pp))
    return tot / len(probs_over)


def binary_brier(probs_over: List[float], y_over: List[int]) -> Optional[float]:
    """二元 Brier = mean((p - y)^2)。空返回 None。"""
    if not probs_over:
        return None
    return sum((p - y) ** 2 for p, y in zip(probs_over, y_over)) / len(probs_over)


def binary_accuracy(probs_over: List[float], y_over: List[int]) -> Optional[float]:
    """二元准确率 = (argmax(p) >= 0.5 ? 大 : 小) 与实际一致比例。空返回 None。"""
    if not probs_over:
        return None
    ok = sum(1 for p, y in zip(probs_over, y_over) if (1 if p >= 0.5 else 0) == y)
    return ok / len(probs_over)


# ─────────────────────────────────────────────────────────────────────────────
# 方向评估汇总
# ─────────────────────────────────────────────────────────────────────────────

def _norm_over_under(row: Dict[str, Any]) -> Tuple[float, float]:
    """从一行记录提取 (over_odds, under_odds), 兼容 ou_validation(over_odds) 与
    ou_live_feed(over) 两种列名。"""
    over = row.get("over_odds")
    if over is None:
        over = row.get("over")
    under = row.get("under_odds")
    if under is None:
        under = row.get("under")
    return float(over) if over is not None else 0.0, float(under) if under is not None else 0.0


def ou_direction_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一批 OU 记录评估大/小方向信号。

    每条 record 至少含: {ou_point, total_goals, [settle 可选], [over_odds/under_odds 或 over/under]}。

    返回命中率汇总:
      - model_hit      : OU_HONESTY 方向 vs 实际结算
      - market_fav_hit : 低赔一侧 (OVER赔<UNDER赔→大) vs 实际
      - always_over / always_under : 朴素基线命中率
      - binary metrics : LogLoss / Brier / Accuracy (P大 vs 实际)
      - per_line / trap_vs_honest 细分
    """
    if not records:
        return {
            "n_records": 0, "n_scored": 0,
            "model_hit_pct": None, "market_fav_hit_pct": None,
            "always_over_hit_pct": None, "always_under_hit_pct": None,
            "binary_metrics": {"log_loss": None, "brier": None, "accuracy": None, "n": 0},
            "per_line": {}, "trap_lines": {"n": 0, "model_hit_pct": None},
            "honest_lines": {"n": 0, "model_hit_pct": None},
            "note": "无记录",
        }

    model_hits, market_hits = [], []
    always_over_hits, always_under_hits = [], []
    probs_over, y_over = [], []
    per_line: Dict[float, Dict[str, Any]] = {}
    trap_stats, hon_stats = [], []

    for r in records:
        line = float(r["ou_point"])
        over_odds, under_odds = _norm_over_under(r)
        total = r.get("total_goals")
        if total is None:
            continue
        total = int(total)

        # 结算方向 (优先用已存 settle, 否则现算)
        settle = r.get("settle") or ou_settle(total, line)
        if settle == "PUSH":
            continue  # 整数盘走水, 不计入方向命中

        mdir, grade = grade_direction(line, over_odds, under_odds)
        mktd = "OVER" if over_odds < under_odds else "UNDER"
        p_over, _ = ou_devig(over_odds, under_odds)

        mh = 1 if (mdir in ("OVER", "UNDER") and mdir == settle) else 0
        kh = 1 if mktd == settle else 0
        aoh = 1 if settle == "OVER" else 0
        auh = 1 if settle == "UNDER" else 0

        if mdir in ("OVER", "UNDER"):
            model_hits.append(mh)
        market_hits.append(kh)
        always_over_hits.append(aoh)
        always_under_hits.append(auh)
        if p_over > 0:
            probs_over.append(p_over)
            y_over.append(1 if settle == "OVER" else 0)

        # 细分
        ln = per_line.setdefault(line, {"n": 0, "model_hit": 0, "market_hit": 0})
        ln["n"] += 1
        ln["model_hit"] += mh
        ln["market_hit"] += kh
        slot = trap_stats if "trap" in grade else hon_stats
        slot.append((mdir, settle, mh))

    n = len(model_hits) + 0  # model_hits 仅含非 NEUTRAL
    # 注意: market/always 计数覆盖全部 (含 NEUTRAL 行的市场方向), 用各自列表长度
    n_mkt = len(market_hits)

    def _rate(lst):
        return round(100.0 * sum(lst) / len(lst), 2) if lst else None

    per_line_out = {}
    for ln, d in sorted(per_line.items()):
        per_line_out[str(ln)] = {
            "n": d["n"],
            "model_hit_pct": _rate([1] * d["model_hit"] + [0] * (d["n"] - d["model_hit"])) if d["model_hit"] or d["n"] else None,
            "market_hit_pct": _rate([1] * d["market_hit"] + [0] * (d["n"] - d["market_hit"])) if d["market_hit"] or d["n"] else None,
        }

    trap_n = len(trap_stats)
    hon_n = len(hon_stats)
    trap_hit = _rate([h for _, _, h in trap_stats]) if trap_stats else None
    hon_hit = _rate([h for _, _, h in hon_stats]) if hon_stats else None

    return {
        "n_records": len(records),
        "n_scored": n_mkt,
        "model_hit_pct": _rate(model_hits),
        "market_fav_hit_pct": _rate(market_hits),
        "always_over_hit_pct": _rate(always_over_hits),
        "always_under_hit_pct": _rate(always_under_hits),
        "binary_metrics": {
            "log_loss": round(binary_log_loss(probs_over, y_over), 4) if probs_over else None,
            "brier": round(binary_brier(probs_over, y_over), 4) if probs_over else None,
            "accuracy": round(binary_accuracy(probs_over, y_over), 4) if probs_over else None,
            "n": len(probs_over),
        },
        "per_line": per_line_out,
        "trap_lines": {"n": trap_n, "model_hit_pct": trap_hit},
        "honest_lines": {"n": hon_n, "model_hit_pct": hon_hit},
    }


# ─────────────────────────────────────────────────────────────────────────────
# 数据库读取 + 报告
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "football_data.db"
)
REPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data"
)


def _rows_to_records(con: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info('{table}')")]
    if not cols:
        return []
    want = {"ou_point", "over_odds", "under_odds", "over", "under", "total_goals", "settle"}
    sel = ", ".join(c for c in cols if c in want)
    if not sel:
        return []
    rows = con.execute(f"SELECT {sel} FROM {table}").fetchall()
    return [dict(r) for r in rows]


def run_ou_eval(db_path: str = DEFAULT_DB,
                out_path: Optional[str] = None) -> Dict[str, Any]:
    """读取 ou_validation (内部有偏样本) + ou_live_feed (前向无偏采集) 已结算记录,
    用 ou_direction_stats 出 OU 方向评估报告, 写 data/eval_ou_report.json。

    诚实声明: 因样本偏差/不足, 本报告仅作为评估仪器与数据管道验证, 不宣称信号有效。
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    val_records = _rows_to_records(con, "ou_validation")
    live_records = _rows_to_records(con, "ou_live_feed")
    # ou_live_feed 仅取已结算 (result='done')
    live_records = [r for r in live_records if r.get("settle")]

    con.close()

    val_stats = ou_direction_stats(val_records)
    live_stats = ou_direction_stats(live_records)

    report = {
        "meta": {
            "module": "pipeline.evaluation.ou_eval",
            "source_tables": ["ou_validation (internal, biased)", "ou_live_feed (forward, unbiased)"],
            "direction_source": "pipeline.predictors.ou_linkage.OU_HONESTY (SSoT)",
            "honesty_note": (
                "ou_validation 仅 29 行且 100% 高比分 (内部 live_odds_raw 有偏, 非无偏验证集); "
                "ou_live_feed 随赛事结束经 automation 累积。当前样本不足/有偏, "
                "OU 方向信号**尚未可验证**, 本报告仅验证评估仪器与数据管道正确性。"
            ),
        },
        "ou_validation_internal_biased": val_stats,
        "ou_live_feed_forward": live_stats,
    }
    out_path = out_path or os.path.join(REPORT_DIR, "eval_ou_report.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    report["_out_path"] = out_path
    return report
