"""取数 → 结算 → 去水 → 写账本 (T02).

严格复用既有 SSoT (零重写):
  - 结算真源:        pipeline.settle  (result_1x2 / credible_1x2 假0-0守卫)
  - 去水唯一入口:    pipeline.odds_math.devig_power

去水口径变更 (2026-09-23): devig3(比例法) → devig_power(幂法)。
  实证: 比例法按概率比例均摊抽水, 系统性高估热门概率(热门赔率虚高 +4.20%),
  使"无脑买最短赔率"这一零信息策略在 KNN 同批 2562 场上 ROI +5.15%(CI下限>0),
  并让 KNN 表观 ROI +5.86% 通过旧五道关误判 EDGE。换幂法后 KNN ROI 降至
  +2.37% 且 CI 含 0。证据: reports/knn_edge_devig_audit.json +
  reports/devig_power_recheck.json。详见 docs/verification_devig_fix.md。
  - 预测源 / 赔率:   pipeline.predict_export.latest_prematch_1x2
  - kickoff→epoch:   pipeline.odds_candles.parse_kickoff_ts

硬性纪律:
  - 只纳 is_credible=1 行 (credible_1x2 守卫), 假 0-0 不可信场一律剔除, 不污染计数/CI
  - 每条纸盘 payoff 由去水 decimal odds 计算, 经 ledger.append 仅 INSERT (预检去重)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional, Tuple

from pipeline.settle import result_1x2, credible_1x2
from pipeline.odds_math import devig_power
from pipeline.odds_candles import parse_kickoff_ts
from pipeline.predict_export import latest_prematch_1x2

from verification.constants import KNN_OUTCOME_MAP, PAPER_STAKE

# 去水口径标记 (写入账本, 供跨口径追溯)
DEVIG_METHOD: str = "power"


# ── 时间 / 赔率 辅助 ────────────────────────────────────────────────────────
def _kickoff_utc(kickoff_str: str) -> str:
    """本地 kickoff 字符串 → ISO 8601 UTC."""
    ts = parse_kickoff_ts(kickoff_str or "")
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_odds_ts(con, match_key: str) -> Optional[float]:
    """该场最后一条赔率 tick 的 captured_at (用于假0-0守卫)."""
    row = con.execute(
        "SELECT MAX(captured_at) FROM odds_changes WHERE match_key=?", (match_key,)
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _devig_dec_odds(oh, od, oa) -> Optional[Tuple[float, float, float, float, float, float]]:
    """去水唯一入口: 调 devig_power 得概率, 再换算 decimal odds.
    返回 (p_h, p_d, p_a, dec_h, dec_d, dec_a); 非法赔率返回 None.

    幂法 (而非比例法) 的理由见本模块 docstring: 比例法高估热门概率, 制造伪 EDGE。
    """
    probs = devig_power([float(oh), float(od), float(oa)])
    if probs is None:
        return None
    ph, pd, pa = probs[0], probs[1], probs[2]
    return ph, pd, pa, 1.0 / ph, 1.0 / pd, 1.0 / pa


def _compute_payoff(chosen_outcome: str, dec_odds_chosen: float,
                    settled_outcome: Optional[str], stake: float) -> Optional[float]:
    """纸盘 1X2 清算: 所选胜 = dec_odds-1, 负 = -stake; 未结算返回 None."""
    if settled_outcome is None:
        return None
    if chosen_outcome == settled_outcome:
        return dec_odds_chosen - 1.0
    return -stake


# ── 主流程 ──────────────────────────────────────────────────────────────────
def ingest_all(con, ledger, run_id: str) -> int:
    """本轮全量 ingest: daily_predictions + KNN. 返回新增行数."""
    n1 = ingest_daily_predictions(con, ledger, run_id)
    n2 = ingest_knn(con, ledger, run_id)
    return n1 + n2


def ingest_daily_predictions(con, ledger, run_id: str) -> int:
    """candles_ensemble + market_baseline: 遍历 daily_predictions 已完赛可信行."""
    rows = con.execute(
        """
        SELECT d.match_key, d.payload, d.match_date, d.kickoff,
               m.score_home, m.score_away
        FROM daily_predictions d
        JOIN matches m ON m.match_key = d.match_key
        WHERE d.status='finished' AND m.score_home IS NOT NULL
          AND d.model_source IN ('candles_ensemble','market_baseline')
        ORDER BY d.match_key
        """
    ).fetchall()
    added = 0
    for mk, payload, mdate, ko, fsh, fsa in rows:
        try:
            p = json.loads(payload)
        except Exception:
            continue
        act = result_1x2(fsh, fsa)
        if act is None:
            continue
        # 假0-0守卫: 不可信场剔除 (不污染计数/CI)
        lodge = _last_odds_ts(con, mk)
        ko_ts = parse_kickoff_ts(ko or "")
        if not credible_1x2(fsh, fsa, lodge, ko_ts):
            continue
        mi = p.get("market_implied") or {}
        odds = mi.get("odds_1x2")
        if not odds or len(odds) != 3:
            continue
        dv = _devig_dec_odds(odds[0], odds[1], odds[2])
        if dv is None:
            continue
        ph, pd, pa, dh, dd, da = dv
        # 模型概率 (candles / market 均来自 payload)
        phm = float(p["p_home"])
        pdm = float(p["p_draw"])
        pam = float(p["p_away"])
        probs = {"home": phm, "draw": pdm, "away": pam}
        chosen = max(probs, key=probs.get)
        chosen_dec = {"home": dh, "draw": dd, "away": da}[chosen]
        payoff = _compute_payoff(chosen, chosen_dec, act, PAPER_STAKE)
        rec = {
            "run_id": run_id,
            "match_id": mk,
            "model_source": p.get("model_source") or "candles_ensemble",
            "kickoff_utc": _kickoff_utc(ko),
            "match_date": mdate or (ko[:10] if ko else ""),
            "chosen_outcome": chosen,
            "predicted_prob": probs[chosen],
            "p_home": phm, "p_draw": pdm, "p_away": pam,
            "chosen_dec_odds": chosen_dec,
            "devig_h": dh, "devig_d": dd, "devig_a": da,
            "paper_stake": PAPER_STAKE,
            "settled_home": fsh, "settled_away": fsa,
            "settled_outcome": act,
            "payoff": payoff,
            "is_credible": 1,
            "created_at": _now_utc(),
            "devig_method": DEVIG_METHOD,
        }
        if ledger.append(rec):
            added += 1
    return added


def ingest_knn(con, ledger, run_id: str) -> int:
    """KNN 方向-only: 遍历 prematch_conclusion (verdict_code∈H/D/A 且可信).
    KNN 无概率模型 → predicted_prob/p_home/p_draw/p_away 全为 NULL;
    赔率取同场赛前1X2 去水 decimal odds 计纸盘方向对照.
    """
    rows = con.execute(
        """
        SELECT p.match_key, p.verdict_code, m.score_home, m.score_away,
               m.kickoff, d.match_date
        FROM prematch_conclusion p
        JOIN matches m ON m.match_key = p.match_key
        LEFT JOIN daily_predictions d ON d.match_key = p.match_key
        WHERE m.status='finished' AND m.score_home IS NOT NULL
          AND p.verdict_code IN ('H','D','A')
        ORDER BY p.match_key
        """
    ).fetchall()
    added = 0
    for mk, code, fsh, fsa, ko, mdate in rows:
        act = result_1x2(fsh, fsa)
        if act is None:
            continue
        lodge = _last_odds_ts(con, mk)
        ko_ts = parse_kickoff_ts(ko or "")
        if not credible_1x2(fsh, fsa, lodge, ko_ts):
            continue
        chosen = KNN_OUTCOME_MAP.get(code)
        if chosen is None:
            continue
        # 同场赛前1X2 去水赔率 (KNN 无概率, 用市场去水 odds 计纸盘方向)
        odds = None
        if ko_ts is not None:
            odds = latest_prematch_1x2(con, mk, ko_ts)
        if odds is None:
            continue
        dv = _devig_dec_odds(odds[0], odds[1], odds[2])
        if dv is None:
            continue
        ph, pd, pa, dh, dd, da = dv
        chosen_dec = {"home": dh, "draw": dd, "away": da}[chosen]
        payoff = _compute_payoff(chosen, chosen_dec, act, PAPER_STAKE)
        rec = {
            "run_id": run_id,
            "match_id": mk,
            "model_source": "KNN",
            "kickoff_utc": _kickoff_utc(ko),
            "match_date": mdate or (ko[:10] if ko else ""),
            "chosen_outcome": chosen,
            "predicted_prob": None,
            "p_home": None, "p_draw": None, "p_away": None,
            "chosen_dec_odds": chosen_dec,
            "devig_h": dh, "devig_d": dd, "devig_a": da,
            "paper_stake": PAPER_STAKE,
            "settled_home": fsh, "settled_away": fsa,
            "settled_outcome": act,
            "payoff": payoff,
            "is_credible": 1,
            "created_at": _now_utc(),
            "devig_method": DEVIG_METHOD,
        }
        if ledger.append(rec):
            added += 1
    return added


__all__ = [
    "ingest_all",
    "ingest_daily_predictions",
    "ingest_knn",
    "_devig_dec_odds",
    "_compute_payoff",
]
