# -*- coding: utf-8 -*-
"""
autopilot.py — 哨响AI 实时分析 → 模拟投注 → 完场回测 → 反馈微调 全自动闭环守护进程
=====================================================================================

设计要点
--------
1. 纯 stdlib + 既有 项目依赖(numpy/scipy 已在环境中)，不引入任何新第三方依赖。
2. 预测逻辑直接复用 SSoT：
     - 1X2:  pipeline.predictors.unified_predictor.UnifiedPredictor (v7.4 盘口锚定,
             默认 100% 跟盘, 即「主源去水概率 argmax」, 与需求一致)。
     - 比分: pipeline.score_model.predict_score (OIP Poisson 赔率隐含比分管线, 取 top1 比分)。
   import 依赖过重/不可用时，自动降级到 stdlib 简化版(见 _fallback_* , 已注释标明)。
3. events.db 全程只读 URI (file:...?mode=ro) 防锁库；所有 DB 读写包 try/except，单场失败不断轮。
4. 铁律：本脚本只操作「autopilot 注码层」与 data/autopilot_params.json，
   绝不修改 pipeline/ 任何代码或 FADE 阈值。
5. 账本幂等风格对齐 scripts/prospective_score_eval.py：按 (match_key, bet_type) 去重。

落地产物
--------
  data/sim_bets.db            模拟账本(sqlite, 表 sim_bets)
  data/autopilot_alerts.json  采集器健康检查告警(追加)
  data/autopilot_feedback.json 预测概率 vs 实际结果反馈(追加)
  data/autopilot_params.json   仅允许微调的两个参数(cs_stake_scale / kelly_frac)
  _autopilot.log              每轮摘要日志(时间戳, print+flush)

运行
----
  python scripts/autopilot.py --once            # 单轮(测试)
  python scripts/autopilot.py --daemon -i 300   # 守护, 默认 5 分钟一轮
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ── 路径解析 (相对项目根 = scripts/ 的上一级) ──────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
GQ_DB = ROOT / "data" / "events.db"
FOOTBALL_DB = ROOT / "data" / "football_data.db"
SIM_DB = ROOT / "data" / "sim_bets.db"
ALERTS_JSON = ROOT / "data" / "autopilot_alerts.json"
FEEDBACK_JSON = ROOT / "data" / "autopilot_feedback.json"
PARAMS_JSON = ROOT / "data" / "autopilot_params.json"
LOG_PATH = ROOT / "_autopilot.log"

# 让 pipeline 包可被 import (UnifiedPredictor / predict_score)
for _p in (str(ROOT), str(ROOT / "pipeline"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 操盘手盘口稳定性置信度(优化方向: 盘口锚定/庄家意图) — 仅缩放注码, 绝不改 pick
try:
    from bookmaker_confidence import bookmaker_confidence, stake_scale as _bm_stake_scale
except Exception:
    bookmaker_confidence = None
    _bm_stake_scale = lambda conf: 1.0

# ── 可调参数(铁律: 仅这两个, 且范围/步长受限) ───────────────────────────────
PARAM_BOUNDS = {
    "cs_stake_scale": (0.5, 2.0),   # 比分注注额缩放
    "kelly_frac": (0.1, 0.5),       # 1X2 注注额缩放基准(相对默认 100)
}
DEFAULT_PARAMS = {"cs_stake_scale": 1.0, "kelly_frac": 0.5}
TUNE_STEP = 1.10          # 单次微调步长 10% (上限)
TUNE_MIN_N = 30           # n<30 只记录指标, 不微调
ROLL_WINDOW = 50          # 滚动最近 N 场已结算 1X2 用于反馈微调

# H/D/A <-> home/draw/away 映射
_HDA = {"H": "home", "D": "draw", "A": "away"}
_HDA_REV = {"home": "H", "draw": "D", "away": "A"}


# ───────────────────────────── 日志 ─────────────────────────────
def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ───────────────────────────── 预测器(复用 SSoT, 带 stdlib 降级) ─────────────────────────────
def _devig3(h: float, d: float, a: float):
    """去水(去抽水)隐含概率。返回 (pH, pD, pA)。"""
    inv = 1.0 / h + 1.0 / d + 1.0 / a
    return (1.0 / h) / inv, (1.0 / d) / inv, (1.0 / a) / inv


# 尝试复用正式预测器；失败则降级到 stdlib 简化版
_USE_REAL_PREDICTOR = False
_UnifiedPredictor = None
_predict_score = None
try:
    from pipeline.predictors.unified_predictor import UnifiedPredictor as _UP
    from pipeline.score_model import predict_score as _PS
    _UnifiedPredictor = _UP()
    _predict_score = _PS
    _USE_REAL_PREDICTOR = True
    log("[init] 复用正式预测器: UnifiedPredictor(v7.4) + score_model(OIP Poisson)")
except Exception as e:  # pragma: no cover - 仅在环境缺依赖时触发
    log(f"[init] 正式预测器 import 失败, 降级 stdlib 简化版: {type(e).__name__}: {e}")


def predict_1x2(home: str, away: str, oh: float, od: float, oa: float):
    """返回 (pick, prob_of_pick, {H,D,A})。
    pick ∈ {'home','draw','away'} = 去水概率 argmax 方向(v7.4 默认 100% 跟盘)。"""
    if _USE_REAL_PREDICTOR:
        r = _UnifiedPredictor.predict(home, away, oh, od, oa)
        pick = _HDA[r["prediction"]]
        probs = r["probabilities"]  # {H,D,A} 已温度校准
        return pick, float(probs[r["prediction"]]), probs
    # ── stdlib 简化版(去水 argmax) ──
    ph, pd, pa = _devig3(oh, od, oa)
    probs = {"H": ph, "D": pd, "A": pa}
    pick = _HDA[max(probs, key=probs.get)]
    return pick, float(probs[pick[0].upper()]), probs


def predict_cs(home: str, away: str, oh: float, od: float, oa: float):
    """返回 (pick_str, prob)。pick_str 如 '1-2' (主-客)。取 top1 比分。"""
    if _USE_REAL_PREDICTOR:
        s = _predict_score(home, away, oh, od, oa)
        i, j, p = s["top_scores"][0]
        return f"{i}-{j}", float(p)
    # ── stdlib 简化版 OIP(无 scipy, 纯网格解 λ, 已注释: 简化版) ──
    return _fallback_cs(home, away, oh, od, oa)


def _fallback_cs(home, away, oh, od, oa):
    """简化版比分模型: 1X2 去水 -> 网格解独立 Poisson λ_h/λ_a -> 取 top1。
    与 score_model.OIP 同思路, 仅缺失 scipy 时的降级实现(goal_scale 取通用联赛 1.2)。"""
    ph, pd, pa = _devig3(oh, od, oa)
    lh, la = _solve_oip_grid(ph, pd, pa, maxg=8)
    lh, la = lh * 1.2, la * 1.2  # GENERAL_OIP_GOAL_SCALE
    M = _score_matrix(lh, la, 8)
    flat = M.flatten()
    order = sorted(range(flat.size), key=lambda k: -flat[k])[:1]
    i, j = divmod(order[0], 9)
    return f"{i}-{j}", float(flat[order[0]])


def _solve_oip_grid(ph, pd, pa, maxg=8):
    best, berr = (1.3, 1.1), 1e9
    for lh in (x * 0.1 for x in range(3, 45)):
        for la in (x * 0.1 for x in range(3, 45)):
            eh = ed = ea = 0.0
            for i in range(maxg + 1):
                pi = math.exp(-lh) * lh ** i / math.factorial(i)
                for j in range(maxg + 1):
                    pj = math.exp(-la) * la ** j / math.factorial(j)
                    p = pi * pj
                    if i > j:
                        eh += p
                    elif i == j:
                        ed += p
                    else:
                        ea += p
            err = (eh - ph) ** 2 + (ed - pd) ** 2
            if err < berr:
                berr, best = err, (lh, la)
    return best


def _score_matrix(lh, la, maxg=8):
    col = [math.exp(-lh) * lh ** i / math.factorial(i) for i in range(maxg + 1)]
    row = [math.exp(-la) * la ** j / math.factorial(j) for j in range(maxg + 1)]
    M = [[col[i] * row[j] for j in range(maxg + 1)] for i in range(maxg + 1)]
    s = sum(sum(r) for r in M)
    return [[M[i][j] / s for j in range(maxg + 1)] for i in range(maxg + 1)]


# ───────────────────────────── DB 连接辅助 ─────────────────────────────
def _ro_uri(path: Path) -> str:
    return f"file:{path.resolve().as_posix()}?mode=ro"


def _log_err(ctx: str, e: Exception):
    log(f"[warn] {ctx}: {type(e).__name__}: {e}")


# ───────────────────────────── 采集器健康检查 ─────────────────────────────
def _safe_decode(b: bytes) -> str:
    """Windows 下 wmic/powershell 输出可能是 GBK(中文系统)或 UTF-8/UTF-16, 统一安全解码。"""
    if not b:
        return ""
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            return b.decode(enc, errors="strict")
        except Exception:
            continue
    return b.decode("utf-8", errors="replace")


def collector_running() -> bool:
    """检查 GQ 采集器进程(gq/auto_collector.py)是否存在。Windows: wmic, 失败回退 powershell。
    仅做检测, 不自动重启(铁律)。"""
    patterns = ("auto_collector",)
    # 1) wmic
    try:
        out = subprocess.run(
            "wmic process where \"name='python.exe'\" get processid,commandline",
            shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        ).stdout
        text = _safe_decode(out)
        for line in text.splitlines():
            if all(p in line for p in patterns):
                return True
    except Exception:
        pass
    # 2) powershell 回退
    try:
        ps = "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Select-Object -ExpandProperty CommandLine"
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        ).stdout
        text = _safe_decode(out)
        for line in text.splitlines():
            if "auto_collector" in line:
                return True
    except Exception:
        pass
    return False


def maybe_alert_collector_down():
    try:
        running = collector_running()
    except Exception as e:
        _log_err("collector check", e)
        return
    if running:
        return
    # 去重: 若 60 分钟内已告警过则不再追加
    now = datetime.now()
    try:
        arr = json.loads(ALERTS_JSON.read_text(encoding="utf-8")) if ALERTS_JSON.exists() else []
    except Exception:
        arr = []
    if arr:
        last = arr[-1]
        try:
            last_ts = datetime.strptime(last.get("ts", "1970-01-01 00:00:00"), "%Y-%m-%d %H:%M:%S")
            if (now - last_ts).total_seconds() < 3600:
                return
        except Exception:
            pass
    arr.append({
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "collector_down",
        "message": "GQ 采集器进程 (gq/auto_collector.py) 未检测到, 请人工排查; autopilot 不自动重启。",
    })
    try:
        ALERTS_JSON.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
        log("[alert] 采集器未运行 -> 已写入 autopilot_alerts.json")
    except Exception as e:
        _log_err("write alerts", e)


# ───────────────────────────── 模拟账本 (sim_bets.db) ─────────────────────────────
def ensure_sim_db():
    try:
        con = sqlite3.connect(str(SIM_DB))
        con.execute(
            """CREATE TABLE IF NOT EXISTS sim_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mid TEXT,
                match_key TEXT NOT NULL,
                home TEXT,
                away TEXT,
                kickoff TEXT,
                bet_type TEXT NOT NULL CHECK(bet_type IN ('1x2','cs')),
                pick TEXT,
                stake REAL,
                odds REAL,
                prob_model REAL,
                prob_consensus REAL,
                placed_at TEXT,
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','settled')),
                result TEXT,
                pnl REAL,
                settled_at TEXT,
                UNIQUE(match_key, bet_type)
            )"""
        )
        con.commit()
        con.close()
    except Exception as e:
        _log_err("ensure sim db", e)


def bet_exists(con, match_key: str, bet_type: str) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM sim_bets WHERE match_key=? AND bet_type=?", (match_key, bet_type)
        ).fetchone()
        return row is not None
    except Exception:
        return False


def insert_bet(con, **kw):
    cols = ["mid", "match_key", "home", "away", "kickoff", "bet_type", "pick",
            "stake", "odds", "prob_model", "prob_consensus", "placed_at", "status"]
    vals = [kw.get(c) for c in cols]
    ph = ",".join("?" for _ in cols)
    sql = f"INSERT OR IGNORE INTO sim_bets ({','.join(cols)}) VALUES ({ph})"
    con.execute(sql, vals)


# ───────────────────────────── 赔率读取 (events.db 只读) ─────────────────────────────
def latest_1x2(con, match_key: str):
    """返回 {'home':o,'draw':o,'away':o} 或 None。取该场最新快照的 3 个 selection。

    注意: GQ 采集器每个 selection 的 captured_at 精确到毫秒且互不相同,
    故不能用 =(MAX) 精确匹配, 改用 >= 最近0.5s窗口取全3个selection。
    """
    try:
        # 方案: 取最新的 captured_at 值, 再取该值±0.5s内的所有行(覆盖同一次采集)
        max_ts = con.execute(
            "SELECT MAX(captured_at) FROM odds_snapshots WHERE match_key=? AND market='1X2'",
            (match_key,),
        ).fetchone()[0]
        if max_ts is None:
            return None
        rows = con.execute(
            """SELECT selection, odds FROM odds_snapshots
               WHERE match_key=? AND market='1X2' AND captured_at >= ? AND captured_at <= ?""",
            (match_key, max_ts - 0.5, max_ts + 0.5),
        ).fetchall()
    except Exception as e:
        _log_err(f"latest_1x2 {match_key}", e)
        return None
    m = {}
    for sel, o in rows:
        s = (sel or "").lower()
        if s in ("home", "h", "1"):
            m["home"] = o
        elif s in ("draw", "d", "x", "0"):
            m["draw"] = o
        elif s in ("away", "a", "2"):
            m["away"] = o
    return m if len(m) == 3 else None


def latest_cs_odds(con, match_key: str, selection: str):
    """返回该场最新快照中指定比分的 CS 赔率, 或 None。"""
    try:
        row = con.execute(
            """SELECT odds FROM odds_snapshots
               WHERE match_key=? AND market='CS' AND selection=?
                 AND captured_at = (SELECT MAX(captured_at) FROM odds_snapshots
                                     WHERE match_key=? AND market='CS')""",
            (match_key, selection, match_key),
        ).fetchone()
        return float(row[0]) if row else None
    except Exception as e:
        _log_err(f"latest_cs_odds {match_key} {selection}", e)
        return None


# ───────────────────────────── 多庄共识 (leisu_odds) ─────────────────────────────
def _norm_team(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "")


def leisu_consensus_for(home: str, away: str, gq_kickoff_date) -> dict | None:
    """在 football_data.db.leisu_odds 中模糊匹配同场(队名包含 + 同日±1),
    各庄去水概率取均值 -> {H,D,A} 共识概率。无匹配返回 None。"""
    if not FOOTBALL_DB.exists():
        return None
    try:
        con = sqlite3.connect(_ro_uri(FOOTBALL_DB), uri=True)
        rows = con.execute(
            "SELECT home_raw, away_raw, kickoff_ts, book, odds_h, odds_d, odds_a "
            "FROM leisu_odds"
        ).fetchall()
        con.close()
    except Exception as e:
        _log_err("leisu read", e)
        return None

    hn, an = _norm_team(home), _norm_team(away)
    best_group = None
    best_score = 0
    for hr, ar, ts, book, oh, od, oa in rows:
        if not (oh and od and oa):
            continue
        hn2, an2 = _norm_team(hr), _norm_team(ar)
        # 队名双向包含匹配
        name_ok = ((hn in hn2 or hn2 in hn) and (an in an2 or an2 in an)) or \
                  ((hn in an2 or an2 in hn) and (an in hn2 or hn2 in an))
        if not name_ok:
            continue
        # 日期 ±1 天
        date_ok = False
        if gq_kickoff_date and ts:
            try:
                lk = datetime.fromtimestamp(int(ts))
                if abs((lk.date() - gq_kickoff_date).days) <= 1:
                    date_ok = True
            except Exception:
                date_ok = True
        else:
            date_ok = True
        if not date_ok:
            continue
        best_group = (hr, ar)  # 取第一个匹配组即可(同场多庄已同行)
        best_score += 1
    if best_group is None:
        return None

    # 重新聚合该组的各庄概率
    ph = pd = pa = 0.0
    nb = 0
    for hr, ar, ts, book, oh, od, oa in rows:
        if not (oh and od and oa):
            continue
        if (hr, ar) != best_group:
            continue
        try:
            ph_, pd_, pa_ = _devig3(float(oh), float(od), float(oa))
        except Exception:
            continue
        ph += ph_; pd += pd_; pa += pa_
        nb += 1
    if nb == 0:
        return None
    return {"H": ph / nb, "D": pd / nb, "A": pa / nb}


# ───────────────────────────── 单轮: 模拟投注 ─────────────────────────────
def run_betting(now: datetime):
    """对未来 24h 内开赛 / 近2h内刚开赛(live) 且未下注的比赛落模拟注。返回本轮新注数。"""
    if not GQ_DB.exists():
        log("[bet] events.db 不存在, 跳过投注")
        return 0
    new_count = 0
    try:
        gcon = sqlite3.connect(_ro_uri(GQ_DB), uri=True)
        # 扩展: scheduled(未来24h) + live(近2h内开赛, 含刚开赛可投注的live场)
        rows = gcon.execute(
            "SELECT match_key, home, away, mid, kickoff, status FROM matches "
            "WHERE (status='scheduled' AND kickoff IS NOT NULL AND kickoff != '') "
            "   OR (status='live' AND kickoff IS NOT NULL AND kickoff != '')"
        ).fetchall()
        gcon.close()
    except Exception as e:
        _log_err("bet select matches", e)
        return 0

    try:
        scon = sqlite3.connect(str(SIM_DB))
        ensure_sim_db()
        for match_key, home, away, mid, kickoff, status in rows:
            try:
                ko = _parse_kickoff(kickoff)
                if ko is None:
                    continue
                # 窗口: scheduled=(now, now+24h]; live=近2h内开赛(含in-play模拟投注)
                if status == "scheduled":
                    if not (now < ko <= now + timedelta(hours=24)):
                        continue
                elif status == "live":
                    if not (now - timedelta(hours=2) <= ko <= now):
                        continue
                else:
                    continue

                # 取该场最新 1X2 赔率(独立只读连接, 避免与账本事务混用)
                gcon2 = sqlite3.connect(_ro_uri(GQ_DB), uri=True)
                o12 = latest_1x2(gcon2, match_key)
                gcon2.close()
                if not o12:
                    continue  # 无 1X2 赔率无法锚定, 跳过

                # 操盘手盘口稳定性置信度 -> 注码缩放(陷阱线降注, 不覆盖 pick)
                bm_conf = 1.0
                if bookmaker_confidence is not None:
                    try:
                        bm_conf = float(bookmaker_confidence(match_key, kickoff).get("conf", 1.0))
                    except Exception:
                        bm_conf = 1.0
                bm_scale = _bm_stake_scale(bm_conf)

                # 多庄共识(对照列) — 先算, 供 1X2/CS 两注复用
                consensus = None
                try:
                    consensus = leisu_consensus_for(home, away, ko.date())
                except Exception as e:
                    _log_err("consensus", e)

                # —— 1X2 注 ——
                if not bet_exists(scon, match_key, "1x2"):
                    pick, pprob, probs = predict_1x2(
                        home, away, o12["home"], o12["draw"], o12["away"])
                    cons_pick = None
                    if consensus:
                        cons_pick = round(consensus[_HDA_REV[pick]], 4)
                    insert_bet(
                        scon,
                        mid=mid, match_key=match_key, home=home, away=away, kickoff=kickoff,
                        bet_type="1x2", pick=pick, stake=round(_stake_1x2() * bm_scale, 2),
                        odds=round(o12[pick], 3), prob_model=round(pprob, 4),
                        prob_consensus=cons_pick, placed_at=_now_iso(), status="open",
                    )
                    new_count += 1
                    log(f"[bet] 1X2 {home} vs {away} -> {pick} @ {o12[pick]:.2f} "
                        f"(p={pprob:.3f}, cons={cons_pick}, 操盘手conf={bm_conf}, 注码×{bm_scale})")

                # —— 比分(CS)注 ——
                if not bet_exists(scon, match_key, "cs"):
                    cs_pick, cs_prob = predict_cs(
                        home, away, o12["home"], o12["draw"], o12["away"])
                    gcon3 = sqlite3.connect(_ro_uri(GQ_DB), uri=True)
                    cs_odds = latest_cs_odds(gcon3, match_key, cs_pick)
                    gcon3.close()
                    cons_cs = None
                    if consensus:
                        # 比分 -> 1X2 结果映射, 取共识对照
                        hi, ai = (int(x) for x in cs_pick.split("-"))
                        res = "H" if hi > ai else ("D" if hi == ai else "A")
                        cons_cs = round(consensus[res], 4)
                    insert_bet(
                        scon,
                        mid=mid, match_key=match_key, home=home, away=away, kickoff=kickoff,
                        bet_type="cs", pick=cs_pick, stake=round(_stake_cs() * bm_scale, 2),
                        odds=(round(cs_odds, 3) if cs_odds is not None else None),
                        prob_model=round(cs_prob, 4), prob_consensus=cons_cs,
                        placed_at=_now_iso(), status="open",
                    )
                    new_count += 1
                    log(f"[bet] CS  {home} vs {away} -> {cs_pick} "
                        f"(p={cs_prob:.3f}, odds={'%.1f' % cs_odds if cs_odds else 'NULL(仅方向)'}, "
                        f"操盘手conf={bm_conf}, 注码×{bm_scale})")
            except Exception as e:
                _log_err(f"bet match {match_key}", e)
                continue
        scon.commit()
        scon.close()
    except Exception as e:
        _log_err("run_betting", e)
    return new_count


def _parse_kickoff(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
    except Exception:
        return None


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ───────────────────────────── 单轮: 结算回测 ─────────────────────────────
def run_settlement(now: datetime):
    """open 注且该场 events.db 已 finished -> 取真实比分结算 pnl, 更新账本 + 追加 feedback。"""
    if not (GQ_DB.exists() and SIM_DB.exists()):
        return 0
    settled_count = 0
    try:
        scon = sqlite3.connect(str(SIM_DB))
        open_rows = scon.execute(
            "SELECT id, match_key, bet_type, pick, stake, odds, prob_model, prob_consensus "
            "FROM sim_bets WHERE status='open'"
        ).fetchall()
        if not open_rows:
            scon.close()
            return 0
        gcon = sqlite3.connect(_ro_uri(GQ_DB), uri=True)
        feedback = []
        for rid, match_key, bet_type, pick, stake, odds, prob_model, prob_consensus in open_rows:
            try:
                m = gcon.execute(
                    "SELECT status, score_home, score_away FROM matches WHERE match_key=?",
                    (match_key,),
                ).fetchone()
                if not m:
                    continue
                status, sh, sa = m
                if status != "finished" or sh is None or sa is None:
                    continue
                sh, sa = int(sh), int(sa)
                if bet_type == "1x2":
                    outcome = "home" if sh > sa else ("draw" if sh == sa else "away")
                    win = (pick == outcome)
                    pnl = (stake * odds - stake) if win else -stake
                    result = outcome
                else:  # cs
                    outcome = f"{sh}-{sa}"
                    win = (pick == outcome)
                    if odds is None:
                        # 仅记方向, 无赔率 -> pnl 未知
                        pnl = None
                        result = outcome
                    else:
                        pnl = (stake * odds - stake) if win else -stake
                        result = outcome
                scon.execute(
                    "UPDATE sim_bets SET status='settled', result=?, pnl=?, settled_at=? "
                    "WHERE id=?",
                    (result, pnl, _now_iso(), rid),
                )
                settled_count += 1
                feedback.append({
                    "match_key": match_key, "bet_type": bet_type, "pick": pick,
                    "prob_model": prob_model, "prob_consensus": prob_consensus,
                    "actual": outcome, "win": bool(win),
                    "pnl": (round(pnl, 2) if pnl is not None else None),
                    "ts": _now_iso(),
                })
                log(f"[settle] {bet_type} {match_key} pick={pick} actual={outcome} "
                    f"-> {'WIN' if win else 'LOSS'} pnl={pnl}")
            except Exception as e:
                _log_err(f"settle id={rid}", e)
                continue
        gcon.close()
        scon.commit()
        scon.close()
        if feedback:
            _append_feedback(feedback)
    except Exception as e:
        _log_err("run_settlement", e)
    return settled_count


def _append_feedback(records):
    try:
        arr = json.loads(FEEDBACK_JSON.read_text(encoding="utf-8")) if FEEDBACK_JSON.exists() else []
    except Exception:
        arr = []
    arr.extend(records)
    if len(arr) > 2000:
        arr = arr[-2000:]
    try:
        FEEDBACK_JSON.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _log_err("write feedback", e)


# ───────────────────────────── 反馈微调 (仅注码层) ─────────────────────────────
def load_params() -> dict:
    try:
        p = json.loads(PARAMS_JSON.read_text(encoding="utf-8"))
        merged = dict(DEFAULT_PARAMS)
        merged.update({k: p[k] for k in DEFAULT_PARAMS if k in p})
        # 钳制到允许范围
        for k, (lo, hi) in PARAM_BOUNDS.items():
            merged[k] = min(hi, max(lo, float(merged[k])))
        return merged
    except Exception:
        return dict(DEFAULT_PARAMS)


def ensure_params_file():
    """首次运行若参数文件不存在则写出默认值(仅记录, 不微调)。便于运维查看当前注码层参数。"""
    try:
        if not PARAMS_JSON.exists():
            save_params(dict(DEFAULT_PARAMS))
    except Exception:
        pass


def save_params(p: dict):
    try:
        PARAMS_JSON.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        _log_err("save params", e)


def _stake_1x2() -> float:
    # 默认基准 100, 按 kelly_frac 相对 0.5 缩放
    kf = load_params()["kelly_frac"]
    return round(100 * kf / 0.5, 2)


def _stake_cs() -> float:
    cs = load_params()["cs_stake_scale"]
    return round(10 * cs, 2)


# ── 信号推送 (fire-and-forget, 不阻塞 autopilot 主循环) ──
# 支持双通道: Telegram + 企业微信, 哪个配了就推哪个
def _maybe_push_signals(n_new: int) -> None:
    """若有新注, 通过 Telegram / 企业微信 推送信号摘要 (非阻塞)。"""
    if n_new <= 0:
        return
    import threading

    def _push():
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = (
            f"🔔 哨响AI · autopilot 新信号\n"
            f"📊 本轮新增 {n_new} 注\n"
            f"⏰ {now_str}\n\n"
            f"💡 前往 Trading 页面查看详情并确认下单"
        )
        # Telegram 通道
        try:
            from pipeline.notifiers import get_notifier
            tg = get_notifier()
            if tg.enabled:
                tg._send_sync_threadsafe(text)
        except Exception:
            pass
        # 企业微信通道
        try:
            from pipeline.notifiers import get_wecom_notifier
            wecom = get_wecom_notifier()
            if wecom.enabled:
                wecom._send_sync_threadsafe(text)
        except Exception:
            pass
        # 微信推送通道 (Server酱/PushPlus/企业微信webhook通用)
        try:
            from pipeline.notifiers import get_wx_notifier
            wx = get_wx_notifier()
            if wx.enabled:
                wx._send_sync_threadsafe(text)
        except Exception:
            pass

    t = threading.Thread(target=_push, daemon=True, name="signal-notify-autopilot")
    t.start()


def run_tuning() -> dict:
    """滚动最近 50 场已结算 1X2 -> 命中率/Brier/pnl; n>=30 才微调且仅限两个参数, 步长≤10%。"""
    metrics = {"n": 0, "hit_rate": None, "brier": None, "pnl": None, "tuned": False}
    try:
        scon = sqlite3.connect(str(SIM_DB))
        rows = scon.execute(
            "SELECT prob_model, result, pick, pnl FROM sim_bets "
            "WHERE bet_type='1x2' AND status='settled' ORDER BY settled_at DESC LIMIT ?",
            (ROLL_WINDOW,),
        ).fetchall()
        scon.close()
    except Exception as e:
        _log_err("tuning read", e)
        return metrics

    n = len(rows)
    metrics["n"] = n
    if n == 0:
        return metrics
    wins = sum(1 for _, res, pick, _ in rows if res == pick)
    hit = wins / n
    brier = sum((pm - (1 if res == pick else 0)) ** 2 for pm, res, pick, _ in rows) / n
    pnl = sum((p or 0) for *_, p in rows)
    metrics.update(hit_rate=round(hit, 4), brier=round(brier, 4), pnl=round(pnl, 2))

    if n < TUNE_MIN_N:
        return metrics  # 仅记录指标, 不微调

    params = load_params()
    new = dict(params)
    if hit >= 0.55:
        new["cs_stake_scale"] = min(PARAM_BOUNDS["cs_stake_scale"][1], new["cs_stake_scale"] * TUNE_STEP)
    elif hit <= 0.45:
        new["cs_stake_scale"] = max(PARAM_BOUNDS["cs_stake_scale"][0], new["cs_stake_scale"] / TUNE_STEP)
    if pnl > 0:
        new["kelly_frac"] = min(PARAM_BOUNDS["kelly_frac"][1], new["kelly_frac"] * TUNE_STEP)
    elif pnl < 0:
        new["kelly_frac"] = max(PARAM_BOUNDS["kelly_frac"][0], new["kelly_frac"] / TUNE_STEP)
    # 最终钳制
    for k, (lo, hi) in PARAM_BOUNDS.items():
        new[k] = min(hi, max(lo, float(new[k])))
    save_params(new)
    metrics["tuned"] = (new != params)
    return metrics


# ───────────────────────────── 单轮主流程 ─────────────────────────────
def run_once():
    now = datetime.now()
    log("=" * 64)
    log(f"[round] 启动 @ {now.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        ensure_sim_db()
        ensure_params_file()
        maybe_alert_collector_down()           # 1) 采集器健康检查
        n_new = run_betting(now)               # 2) 模拟投注
        n_settle = run_settlement(now)         # 3) 结算回测
        metrics = run_tuning()                 # 4) 反馈微调
        params = load_params()                 # 当前参数
        # 5) 信号推送 (fire-and-forget, 不阻塞主流程; Telegram + 企业微信双通道)
        _maybe_push_signals(n_new)
        # 6) 每轮摘要
        log(f"[summary] 新注={n_new} 结算={n_settle} "
            f"滚动命中率(1X2)={metrics['hit_rate']} pnl={metrics['pnl']} "
            f"brier={metrics['brier']} n={metrics['n']} "
            f"tuned={metrics['tuned']} params={params}")
    except Exception as e:
        _log_err("run_once", e)
    log("[round] 结束")


def main():
    ap = argparse.ArgumentParser(description="哨响AI autopilot 闭环守护进程")
    ap.add_argument("--once", action="store_true", help="单轮运行(测试)")
    ap.add_argument("--daemon", action="store_true", help="守护模式")
    ap.add_argument("-i", "--interval", type=int, default=300, help="守护轮询间隔秒(默认300)")
    args = ap.parse_args()

    if args.once:
        run_once()
        return
    if args.daemon:
        log(f"[daemon] 启动, 间隔 {args.interval}s (Ctrl+C 退出)")
        try:
            while True:
                run_once()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            log("[daemon] 收到中断, 退出")
        return
    # 默认行为: 等同于 --once
    run_once()


if __name__ == "__main__":
    main()
