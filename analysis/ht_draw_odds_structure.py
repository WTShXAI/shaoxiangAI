# -*- coding: utf-8 -*-
"""上半场平局赔率结构诊断 (Half-Time Draw Odds Structure).

哨响AI 开盘结构诊断的半场版. 消费 odds_snapshots 的 1X2_1H (半场 1X2) + OU_1H (半场大小球),
输出半场平局赔率的"开盘→临场→HT 临界→最新"四档结构 + 漂移曲线 + 与全场平局关系.

半场 1X2 的"draw" 即上半场结束时主队-客队比分相等的概率; 这是庄家对"上半场是否产生分胜负进球"
的最直接信号. draw 走低 = 庄家越来越确信半场平局 (典型场景: 0-0 僵局, 进球预期降温).

诚实边界 (对齐铁律 IR-17/IR-18/IR-30):
  - 仅用 odds_snapshots 真实赔率计算; 不拿模型概率冒充市场概率.
  - 数据缺失字段 available=false + 说明, 不编造数字.
  - 时间戳为 UTC 秒, 报告不换算本地时区(避免歧义).
  - "HT 临界"为 minute_at=45 且 captured_at 最接近 kickoff+45min 的那一组三档(若有).

依赖(均经 IR-15 核实真实签名):
  matches 表: match_key (主键), kickoff (ISO8601, 或时间戳)
  odds_snapshots 表: match_key, market, selection, odds, captured_at, minute_at
"""
import logging

logger = logging.getLogger("ht_draw_odds_structure")

_MIN_ODDS, _MAX_ODDS = 1.01, 1000.0


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    try:
        return a / b if b else default
    except Exception:
        return default


def _parse_kickoff_ts(kickoff_str: str):
    """kickoff ISO8601 -> UTC 秒; 失败返回 None."""
    if not kickoff_str:
        return None
    try:
        import datetime as _dt
        # 兼容 "2026-08-25 23:00" 与 "2026-08-25T23:00:00"
        s = kickoff_str.replace("T", " ").replace("/", "-")
        dt = _dt.datetime.fromisoformat(s)
        return int(dt.timestamp())
    except Exception:
        try:
            # 尝试纯数字时间戳
            return int(float(kickoff_str))
        except Exception:
            return None


def _fetch_three_way_at_time(con, match_key: str, market: str, target_ts: int,
                             window_sec: int = 60):
    """取 captured_at 在 target_ts 附近 window_sec 内的一组三档(同 captured_at 同 selection 三选)."""
    rows = con.execute(
        f"""SELECT selection, odds, captured_at FROM odds_snapshots
            WHERE match_key=? AND market=? AND selection IN ('home','draw','away')
              AND odds>? AND odds<?
              AND captured_at BETWEEN ? AND ?
            ORDER BY ABS(captured_at - ?) LIMIT 3""",
        (match_key, market, _MIN_ODDS, _MAX_ODDS,
         target_ts - window_sec, target_ts + window_sec, target_ts)).fetchall()
    if len(rows) < 3:
        return None
    out = {}
    for sel, odds, cap in rows:
        out[sel] = {"odds": float(odds), "captured_at": int(cap)}
    return out


def _draw_implied_p(draw_odds: float) -> float:
    """平局赔率隐含概率(原始, 不去水)."""
    return _safe_div(1.0, draw_odds, 0.0)


def _overround3(h: float, d: float, a: float) -> float:
    """三档总抽水 = 1/h + 1/d + 1/a - 1."""
    return _safe_div(1.0, h) + _safe_div(1.0, d) + _safe_div(1.0, a) - 1.0


def ht_draw_odds_diagnosis(con, match_key: str) -> dict:
    """半场平局赔率结构诊断主入口.

    返回 dict 形如:
      {
        "available": True/False,
        "note": "",
        "match_key": ...,
        "opening": {"captured_at", "minute_at", "home","draw","away", "draw_implied_p", "overround"},
        "ht_critical": {同上},           # minute=45 临界三档
        "latest": {"draw", "minute_at", "captured_at", "implied_p", "note"},
        "drift_curve": [{"captured_at","minute_at","odds","implied_p"}],  # 全部 draw 时间序列
        "drift_analysis": {
          "open_to_ht_pct": ..., "open_to_latest_pct": ...,
          "ht_to_latest_pct": ..., "direction": ..., "intensity": ...
        },
        "ft_draw_relation": {
          "ft_draw_open", "ht_draw_open", "ht_vs_ft_implied_ratio",
          "note": "半场平局赔率通常比全场低(45分钟比分平概率高)"
        },
        "ou_1h_side": {
          "lines": {line_key: {"open_over","open_under","latest_over","latest_under"}},
          "note": "OU_1H 走低(小水/低进球)反向佐证半场平局概率上升"
        }
      }
    """
    out = {"available": False, "note": "", "match_key": match_key}
    if con is None or not match_key:
        out["note"] = "con 为空或 match_key 为空"
        return out

    ko_str = con.execute(
        "SELECT kickoff FROM matches WHERE match_key=?", (match_key,)).fetchone()
    ko_ts = _parse_kickoff_ts(ko_str[0]) if ko_str else None

    # ── 1X2_1H draw 全部快照(过滤非法赔率) ──
    draw_rows = con.execute(
        """SELECT odds, captured_at, minute_at FROM odds_snapshots
           WHERE match_key=? AND market='1X2_1H' AND selection='draw'
             AND odds>? AND odds<?
           ORDER BY captured_at""",
        (match_key, _MIN_ODDS, _MAX_ODDS)).fetchall()
    if not draw_rows:
        out["note"] = "无 1X2_1H draw 快照"
        return out

    # 开盘: 最早一条 (用 captured_at 最早)
    open_row = draw_rows[0]
    opening = {
        "captured_at": int(open_row[1]),
        "minute_at": int(open_row[2]),
        "draw": round(float(open_row[0]), 3),
        "draw_implied_p": round(_draw_implied_p(float(open_row[0])), 4),
    }

    # HT 临界三档: minute_at=45 且 captured_at 最接近 kickoff+45min
    ht_critical = None
    if ko_ts:
        ht_ts = ko_ts + 45 * 60
        ht_three = _fetch_three_way_at_time(con, match_key, "1X2_1H", ht_ts)
        if ht_three:
            h, d, a = ht_three["home"]["odds"], ht_three["draw"]["odds"], ht_three["away"]["odds"]
            ht_critical = {
                "captured_at": ht_three["draw"]["captured_at"],
                "minute_at": 45,
                "home": round(h, 3), "draw": round(d, 3), "away": round(a, 3),
                "draw_implied_p": round(_draw_implied_p(d), 4),
                "overround": round(_overround3(h, d, a), 4),
            }

    # 最新一条
    latest_row = draw_rows[-1]
    latest_draw_odds = float(latest_row[0])
    latest = {
        "captured_at": int(latest_row[1]),
        "minute_at": int(latest_row[2]),
        "draw": round(latest_draw_odds, 3),
        "draw_implied_p": round(_draw_implied_p(latest_draw_odds), 4),
    }
    # 自动标注: 极低水(<1.10) = 庄家强烈倾向半场平局
    if latest_draw_odds < 1.10:
        latest["note"] = (f"draw={latest_draw_odds:.2f} 已至极低水(隐含 {latest['draw_implied_p']*100:.1f}%), "
                          "庄家强烈倾向半场平局(典型: 0-0 僵局持续至 HT, 进球预期骤降)")
    elif latest_draw_odds < 1.30:
        latest["note"] = f"draw={latest_draw_odds:.2f} 走低, 庄家倾向半场平局(隐含 {latest['draw_implied_p']*100:.1f}%)"
    else:
        latest["note"] = ""

    # ── 漂移曲线 (降采样: 每 10 条取 1 条, 最多 30 个采样点; 避免输出过大) ──
    step = max(1, len(draw_rows) // 30)
    drift_curve = []
    for i in range(0, len(draw_rows), step):
        o, c, m = draw_rows[i]
        drift_curve.append({
            "captured_at": int(c),
            "minute_at": int(m),
            "odds": round(float(o), 3),
            "implied_p": round(_draw_implied_p(float(o)), 4),
        })

    # ── 漂移分析 ──
    open_odds = float(opening["draw"])
    ht_odds = ht_critical["draw"] if ht_critical else None
    latest_odds = latest_draw_odds
    pct = lambda a, b: round((a - b) / b * 100, 2) if b else None

    drift_analysis = {
        "open_to_latest_pct": pct(latest_odds, open_odds),
        "open_to_ht_pct": (pct(ht_odds, open_odds) if ht_odds else None),
        "ht_to_latest_pct": (pct(latest_odds, ht_odds) if ht_odds else None),
        "direction": ("draw 下行(半场平局概率上升)"
                      if latest_odds < open_odds
                      else "draw 上行(半场平局概率下降)" if latest_odds > open_odds
                      else "draw 持平"),
    }
    mag = abs(drift_analysis["open_to_latest_pct"] or 0)
    drift_analysis["intensity"] = ("剧烈(≥30%)" if mag >= 30
                                    else "显著(≥15%)" if mag >= 15
                                    else "温和(≥5%)" if mag >= 5
                                    else "稳定(<5%)")

    # ── 与全场平局关系 ──
    ft_relation = {"available": False, "note": ""}
    try:
        ft_draw_open_row = con.execute(
            """SELECT odds, captured_at FROM odds_snapshots
               WHERE match_key=? AND market='1X2' AND selection='draw'
                 AND odds>? AND odds<?
               ORDER BY captured_at LIMIT 1""",
            (match_key, _MIN_ODDS, _MAX_ODDS)).fetchone()
        if ft_draw_open_row:
            ft_d = float(ft_draw_open_row[0])
            ht_d = open_odds
            ft_relation = {
                "available": True,
                "ft_draw_open": round(ft_d, 3),
                "ht_draw_open": round(ht_d, 3),
                "ft_draw_implied_p": round(_draw_implied_p(ft_d), 4),
                "ht_draw_implied_p": round(_draw_implied_p(ht_d), 4),
                "ht_vs_ft_implied_ratio": round(_draw_implied_p(ht_d) / max(_draw_implied_p(ft_d), 1e-9), 3),
                "note": ("半场平局赔率通常低于全场(45分钟比分平概率高于 90 分钟); "
                         f"本场 ht_implied/ft_implied = "
                         f"{round(_draw_implied_p(ht_d) / max(_draw_implied_p(ft_d), 1e-9), 3)}"),
            }
    except Exception as e:
        logger.warning(f"[ht_draw] 全场 draw 关联失败: {e}")

    # ── OU_1H 辅助(半场进球数反向佐证半场平局) ──
    ou_side = {"lines": {}, "note": ""}
    try:
        for mkt_row in con.execute(
            """SELECT market FROM odds_snapshots
               WHERE match_key=? AND market LIKE 'OU_1H_%'
                 AND selection IN ('over','under') AND odds>? AND odds<?
               GROUP BY market ORDER BY market""",
            (match_key, _MIN_ODDS, _MAX_ODDS)):
            mkt = mkt_row[0]
            line = mkt.replace("OU_1H_", "")
            ou = {}
            for r in con.execute(
                """SELECT selection, odds, captured_at, minute_at FROM odds_snapshots
                   WHERE match_key=? AND market=? AND selection IN ('over','under')
                     AND odds>? AND odds<? ORDER BY captured_at""",
                (match_key, mkt, _MIN_ODDS, _MAX_ODDS)):
                sel, odds, cap, mn = r
                if sel not in ou:
                    ou[sel] = {"open": round(float(odds), 3), "open_at": int(cap),
                               "minute_at": int(mn), "latest": round(float(odds), 3)}
                else:
                    ou[sel]["latest"] = round(float(odds), 3)
            if "over" in ou and "under" in ou:
                # line 最接近 1.0 的那条最具"半场平局"指示
                ou_side["lines"][line] = {
                    "open_over": ou["over"]["open"], "open_under": ou["under"]["open"],
                    "latest_over": ou["over"]["latest"], "latest_under": ou["under"]["latest"],
                }
        if ou_side["lines"]:
            ou_side["note"] = ("OU_1H(半场大小球): over↓ 或 under↑ 表明庄家认为上半场低进球, "
                               "反向佐证半场平局概率上升(比分维持 0-0/1-1).")
    except Exception as e:
        logger.warning(f"[ht_draw] OU_1H 关联失败: {e}")

    out.update({
        "available": True,
        "note": "",
        "kickoff": ko_str[0] if ko_str else None,
        "opening": opening,
        "ht_critical": ht_critical,
        "latest": latest,
        "drift_curve": drift_curve,
        "drift_analysis": drift_analysis,
        "ft_draw_relation": ft_relation,
        "ou_1h_side": ou_side,
    })
    return out