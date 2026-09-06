#!/usr/bin/env python3
"""
哨响AI · 自主巡航 Agent (MVP v1)
================================
后台常驻循环, 订阅 events.db 数据流, 按「操盘手三段框架」自主扫描:
  - 临场窗口(scheduled, kickoff 在未来数小时内) → 初盘立锚 + 临场漂移
  - 滚盘(live) → 动态水位/比分异动

对每场调用硬工具(操盘手卡 + 交叉庄信号), 达阈值后用本地 qwen3 把信号写成人话,
经 ws_manager.broadcast 推送到前端。LLM 只叙事、不决策; 建仓仍走 /api/execute/confirm 人审批。

护栏(铁律):
  1. LLM 不参与下注决策 —— 决策用 value_layer 硬规则(decision/trap_score/软线价差)
  2. 虚拟电子足球/友谊赛不进扫描(league 含 8分钟/友谊/瓦尔基里/瓦尔哈拉)
  3. 去重: 同一 match_key 在 TTL 内且比分未变 → 不重复推送
  4. 单轮限扫 N 场 + run_in_executor 包裹同步重函数, 防事件循环冻结

依赖注入(由 bridge_service 传入, 避免循环 import):
  broadcast  = ws_manager.broadcast
  analyze    = _live_operator_card_compute
  cross_book = _get_cross_book_signal
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger("agent_cruise")

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
GQ_DB = os.path.join(PROJECT_ROOT, "data", "events.db")

# ── 扫描参数 ──
PREM_WINDOW_HOURS = 3.0    # 临场窗口: 未来 N 小时内开赛的 scheduled 场
LIVE_ACTIVE_SECONDS = 3600  # live 场最近 N 秒内活跃才扫(避免反复扫挂死场)
MAX_SCAN_PER_ROUND = 12     # 单轮最多深挖场数(防过载)
ROUND_INTERVAL = 60         # 每轮间隔秒
ALERT_DEDUP_TTL = 1800      # 同场去重窗口(秒)

# ── 硬阈值(LLM 不参与) ──
TRAP_SCORE_HI = 50          # 陷阱评分阈值(0-100)
SOFT_LINE_MIN = 1           # 跨庄软线最少条数
SPREAD_MIN_PP = 3.0         # 跨庄最大价差(百分点)

# 叙事引擎开关: False=模板叙事(调试期零依赖, 不调模型), True=本地 qwen3(接模型后打开)
USE_LLM_NARRATE = False

# 虚拟电子足球 / 友谊赛 / 娱乐盘 —— 不进建模与扫描
_EXCLUDE_SUBSTR = ("8分钟", "友谊", "瓦尔基里", "瓦尔哈拉")


def _scan_targets():
    """从 events.db 取本轮候选比赛, 返回 [(match_key, home, away, league, status, score_home, score_away, minute), ...]"""
    now = datetime.now()
    lo_str = now.strftime("%Y-%m-%d %H:%M")
    hi_str = (now + timedelta(hours=PREM_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M")
    live_cutoff = time.time() - LIVE_ACTIVE_SECONDS
    conn = sqlite3.connect(GQ_DB, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        rows = conn.execute(
            "SELECT match_key, home, away, league, status, score_home, score_away, minute "
            "FROM matches WHERE (status='scheduled' AND kickoff BETWEEN ? AND ?) "
            "   OR (status='live' AND last_seen >= ?) "
            "ORDER BY (status='live') DESC, kickoff ASC",
            (lo_str, hi_str, live_cutoff),
        ).fetchall()
    finally:
        conn.close()

    targets = []
    for mk, home, away, league, status, sh, sa, minute in rows:
        if not (home and away):
            continue
        if league and any(x in league for x in _EXCLUDE_SUBSTR):
            continue
        targets.append((mk or f"{home} vs {away}", home, away, league, status, sh, sa, minute))
    return targets[:MAX_SCAN_PER_ROUND]


def _template_narrate(card, cross_book):
    """模板叙事(确定性, 零依赖): 用硬信号拼一句可读结论。接模型前的降级/调试形态。"""
    parts = []
    decision = card.get("decision")
    verdict = card.get("verdict")
    trap = card.get("trap_score")
    if decision and str(decision).upper() != "PASS":
        parts.append(f"价值层 {decision}")
    if verdict:
        parts.append(str(verdict))
    if isinstance(trap, (int, float)) and trap >= TRAP_SCORE_HI:
        parts.append(f"陷阱 {trap}/100")
    if cross_book and cross_book.get("n_soft_lines"):
        parts.append(f"跨庄软线 {cross_book.get('n_soft_lines')}条 {cross_book.get('max_spread_pp')}pp")
    return " · ".join(parts) if parts else (card.get("stake") or verdict or "异动")


def _narrate(match_title, card, cross_book):
    """把结构化信号写成人话告警。USE_LLM_NARRATE=False 走模板(零依赖); True 走本地 qwen3。"""
    if not USE_LLM_NARRATE:
        return _template_narrate(card, cross_book)
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "odds_db"))
        from qwen3_analyzer import call_ollama
        sig = {
            "verdict": card.get("verdict"),
            "decision": card.get("decision"),
            "confidence": card.get("confidence"),
            "trap_score": card.get("trap_score"),
            "evidence": card.get("evidence"),
            "cross_book": cross_book,
        }
        prompt = (
            "你是哨响AI操盘手助理。下面是量化硬规则算出的比赛信号, 你只负责用一句不超过40字的中文把它讲清楚, "
            "不要改动结论, 不要建议下注, 不要输出JSON。\n"
            f"比赛: {match_title}\n信号: {json.dumps(sig, ensure_ascii=False)}"
        )
        resp = call_ollama(prompt, "", temperature=0.3, max_tokens=128)
        txt = (resp.get("response") or "").strip()
        if txt:
            return txt.replace("\n", " ")[:80]
    except Exception as e:
        logger.warning("[cruise] narrate failed: %s", e)
    return _template_narrate(card, cross_book)


async def _run_round(broadcast, analyze, cross_book):
    """扫描一轮, 返回推送条数。所有同步重函数走 run_in_executor。"""
    global _LAST_PUSH
    targets = _scan_targets()
    if not targets:
        return 0
    loop = asyncio.get_running_loop()
    pushed = 0

    for mk, home, away, league, status, sh, sa, minute in targets:
        # 去重: 同场 TTL 内且比分未变 → 跳过
        last = _LAST_PUSH.get(mk)
        if last and (time.time() - last["ts"]) < ALERT_DEDUP_TTL and last["score"] == (sh, sa):
            continue

        try:
            card_res = await loop.run_in_executor(None, analyze, mk, home, away, league)
        except Exception as e:
            logger.warning("[cruise] analyze failed %s: %s", mk, e)
            continue

        data = (card_res or {}).get("data") if isinstance(card_res, dict) else None
        if not data or data.get("found") is False:
            continue

        decision = data.get("decision")
        trap = data.get("trap_score")
        reasons = []

        if decision and str(decision).upper() != "PASS":
            reasons.append(f"value-layer {decision}")
        if isinstance(trap, (int, float)) and trap >= TRAP_SCORE_HI:
            reasons.append(f"trap {trap}")

        cb = None
        if cross_book:
            try:
                cb = await loop.run_in_executor(None, cross_book, home, away, league)
            except Exception as e:
                logger.warning("[cruise] cross-book failed %s: %s", mk, e)
        if cb:
            n_soft = cb.get("n_soft_lines") or 0
            spread = cb.get("max_spread_pp") or 0
            if n_soft >= SOFT_LINE_MIN or spread >= SPREAD_MIN_PP:
                reasons.append(f"cross-book softline n={n_soft} spread={spread}pp")

        if not reasons:
            continue

        narrative = await loop.run_in_executor(None, _narrate, mk, data, cb)
        alert = {
            "type": "agent_alert",
            "match_key": mk,
            "home": home,
            "away": away,
            "league": league,
            "status": status,
            "score": f"{sh}-{sa}" if sh is not None and sa is not None else "--",
            "minute": minute,
            "verdict": data.get("verdict"),
            "decision": decision,
            "confidence": data.get("confidence"),
            "trap_score": trap,
            "evidence": data.get("evidence") or [],
            "reasons": reasons,
            "cross_book": cb,
            "narrative": narrative or data.get("stake") or data.get("verdict") or "异动",
            "ts": time.time(),
        }
        try:
            await broadcast(alert)
            _LAST_PUSH[mk] = {"ts": time.time(), "score": (sh, sa)}
            _ALERT_HISTORY.append(alert)
            pushed += 1
        except Exception as e:
            logger.warning("[cruise] broadcast failed %s: %s", mk, e)

    return pushed


_LAST_PUSH = {}
_ALERT_HISTORY = deque(maxlen=200)


def get_recent_alerts(n: int = 50):
    """返回最近 n 条告警(最新在前), 供前端轮询 /api/agent/alerts。"""
    return list(_ALERT_HISTORY)[-n:][::-1]


async def start_cruise(broadcast, analyze, cross_book=None, round_interval=ROUND_INTERVAL):
    """常驻巡航循环(供 bridge 在 startup 时 create_task). 参数均为依赖注入。"""
    logger.info("[cruise] autonomous cruise agent started (interval=%ss)", round_interval)
    while True:
        try:
            n = await _run_round(broadcast, analyze, cross_book)
            if n:
                logger.info("[cruise] round pushed %s alerts", n)
        except Exception as e:
            logger.error("[cruise] round error (non-fatal): %s", e)
        await asyncio.sleep(round_interval)
