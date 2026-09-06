# -*- coding: utf-8 -*-
"""
scripts.build_cs_dataset — 从时间线(events.db odds_snapshots)提取 CS 赔率并构建波胆训练集

数据源: data/events.db (与 /api/timeline/today 的 get_latest_odds 同源)
  - CS 市场 = 波胆 (selection 格式 "0:0".."4:4" + "其他", IR-02 英文冒号铁律)
  - 仅赛前快照 (score_at 空, captured_at <= kickoff) — CS 铁律"仅赛前采集"
  - 每场取"最接近开赛"的快照 (临场价, IR-26 三段框架临场平衡)

清洗 (IR-04 假0-0铁律, 双重过滤):
  - 仅 status='finished' 且 score_missing!=1 且比分非空
  - 仅标准 26 选集合 (0:0..4:4 + 其他) 齐全的场次
  - 赔率合法性 (0, 1000] 且去水后总和 0.9~1.6 (过round sanity)
  - 假0-0交叉验证: 终场 0:0 的场次必须存在"开赛后 (captured_at > kickoff-300s)
    的 score_at 非空快照" (=采集器跟踪过比赛); 否则判假 0-0 剔除
    (2026-08-31 实测: 测试集(最近场次) 0:0 场次 85% 无滚球佐证 = 假 0-0 污染)

特征:
  - cs_p[26]: CS 赔率去水隐含概率向量 (核心, 来自时间线)
  - cs_cheapest_p: 最低赔率档(rank-0)的去水概率 — "庄家最便宜档"诱盘信号
  - cs_overround: CS 抽水
  - h2h_devig[3]: 1X2 去水 (临场)
  - ou_line / ou_over_devig / ah_line / ah_home_devig: 大小/让球 (临场, 可选)
  - league 前缀统计: 场均进球 / 主胜率 / 平局率 (仅用开赛早于本场的完赛场次, 无前视)
标签: 完场比分 → 0:0..4:4 (25类) + 其他 (1类) = 26 类
切分: 时间外切分 (按 kickoff 排序, 后 20% 为测试集) — 项目铁律"时间外切分"
输出: data/cs_odds_dataset.csv + data/cs_odds_dataset_meta.json
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(PROJECT_ROOT, "data", "events.db")
OUT_CSV = os.path.join(PROJECT_ROOT, "data", "cs_odds_dataset.csv")
OUT_META = os.path.join(PROJECT_ROOT, "data", "cs_odds_dataset_meta.json")

TZ8 = timezone(timedelta(hours=8))

# 标准 26 选集合 (IR-02: 英文冒号)
SCORELINES = [f"{h}:{a}" for h in range(5) for a in range(5)]  # 0:0..4:4
SCORELINE_TO_IDX = {s: i for i, s in enumerate(SCORELINES)}
OTHER_IDX = 25
N_CLASSES = 26

# 时间线同源 odds_snapshots 的市场列: market/selection/odds/line/captured_at/score_at
def parse_kickoff(k: str) -> Optional[float]:
    """kickoff → epoch 秒。两种格式: '2026-07-15 03:00'(GMT+8 本地) / '2026-07-14T15:59:08Z'(UTC)."""
    if not k:
        return None
    k = k.strip()
    for fmt, tz in [("%Y-%m-%d %H:%M", TZ8), ("%Y-%m-%dT%H:%M:%SZ", timezone.utc),
                    ("%Y-%m-%dT%H:%M:%S", TZ8), ("%Y-%m-%d", TZ8)]:
        try:
            dt = datetime.strptime(k, fmt)
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def has_inplay_score(c: sqlite3.Connection, match_key: str, ko_ts: float) -> bool:
    """开赛后(容差5分钟)存在 score_at 非空快照 → 采集器跟踪过比分 (真 0-0 佐证, IR-04)."""
    return c.execute(
        "SELECT 1 FROM odds_snapshots WHERE match_key=? "
        "AND score_at IS NOT NULL AND score_at!='' AND captured_at > ? LIMIT 1",
        (match_key, ko_ts - 300),
    ).fetchone() is not None


def load_matches(c: sqlite3.Connection) -> List[dict]:
    """完场 + 干净比分 + 有 kickoff 的比赛 (含假 0-0 交叉验证)."""
    rows = c.execute("""
        SELECT match_key, home, away, league, kickoff, score_home, score_away, first_seen
        FROM matches
        WHERE status='finished'
          AND (score_missing IS NULL OR score_missing != 1)
          AND score_home IS NOT NULL AND score_away IS NOT NULL
          AND kickoff IS NOT NULL AND kickoff != ''
    """).fetchall()
    out = []
    for mk, h, a, lg, ko, sh, sa, fs in rows:
        ko_ts = parse_kickoff(ko)
        if ko_ts is None:
            continue
        # IR-04 假0-0: 终场 0:0 须有开赛后比分快照佐证, 否则判假 (默认写入占位)
        if int(sh) == 0 and int(sa) == 0 and not has_inplay_score(c, mk, ko_ts):
            continue
        out.append({"match_key": mk, "home": h, "away": a, "league": lg or "",
                    "kickoff_ts": ko_ts, "score_home": int(sh), "score_away": int(sa)})
    return out


def latest_prematch(c: sqlite3.Connection, match_key: str, ko_ts: float,
                    market_filter: str) -> Dict[str, Dict[str, float]]:
    """取某场某市场(前缀)最接近开赛的赛前快照: {selection: {odds, line}}.
    market_filter: 'CS' / '1X2' / 'OU_' / 'AH_' — SQL 前缀匹配, 参数化拼接。
    """
    rows = c.execute(
        f"""SELECT market, selection, odds, line, captured_at FROM odds_snapshots
            WHERE match_key=? AND market LIKE ? ESCAPE '\\'
              AND (score_at IS NULL OR score_at='')
              AND captured_at <= ?
            ORDER BY captured_at DESC""",
        (match_key, market_filter.replace("_", "\\_") + "%", ko_ts),
    ).fetchall()
    out: Dict[str, Dict[str, float]] = {}
    for mkt, sel, odds, line, cap in rows:
        key = sel
        if key not in out:
            try:
                o = float(odds)
            except (TypeError, ValueError):
                continue
            if not (0 < o <= 1000):
                continue
            out[key] = {"odds": o, "line": float(line) if line is not None else None}
    return out


def devig(odds_map: Dict[str, float]) -> Optional[Dict[str, float]]:
    """去水: 各选择 1/odds 归一化. 需至少 2 个有效选择."""
    if not odds_map or len(odds_map) < 2:
        return None
    imp = {}
    total = 0.0
    for sel, o in odds_map.items():
        if not (0 < o <= 1000):
            return None
        p = 1.0 / o
        imp[sel] = p
        total += p
    if not (0.9 <= total <= 1.6):  # overround sanity
        return None
    return {sel: p / total for sel, p in imp.items()}


def label_of(sh: int, sa: int) -> int:
    s = f"{sh}:{sa}"
    return SCORELINE_TO_IDX.get(s, OTHER_IDX)


def main() -> None:
    c = sqlite3.connect(DB)
    matches = load_matches(c)
    print(f"完场+干净比分+有kickoff+非假0-0(IR-04双重): {len(matches)} 场")

    # 按 kickoff 排序 → 前缀联赛统计 (无前视)
    matches.sort(key=lambda m: m["kickoff_ts"])
    league_running: Dict[str, Dict[str, float]] = {}  # league -> {n, goals, home_win, draw}
    league_hist: Dict[str, Dict[str, float]] = {}

    records: List[dict] = []
    skipped = {"no_cs": 0, "no_26": 0, "bad_odds": 0, "no_ko": 0}

    for i, m in enumerate(matches):
        mk = m["match_key"]
        ko_ts = m["kickoff_ts"]

        # CS 赔率 (时间线同源)
        cs = latest_prematch(c, mk, ko_ts, "CS")
        if not cs or not all(s in cs for s in SCORELINES) or "其他" not in cs:
            skipped["no_26"] += 1
            continue
        cs_odds = {s: cs[s]["odds"] for s in SCORELINES + ["其他"]}
        cs_dv = devig(cs_odds)
        if cs_dv is None:
            skipped["bad_odds"] += 1
            continue

        # 1X2 / OU / AH (可选特征)
        h2h = latest_prematch(c, mk, ko_ts, "1X2")
        ou = latest_prematch(c, mk, ko_ts, "OU_")
        ah = latest_prematch(c, mk, ko_ts, "AH_")
        h2h_dv = devig({k: v["odds"] for k, v in h2h.items()}) if "home" in h2h and "draw" in h2h and "away" in h2h else None
        ou_line, ou_over_dv = None, None
        if ou and "over" in ou and "under" in ou:
            ov = devig({"over": ou["over"]["odds"], "under": ou["under"]["odds"]})
            if ov:
                ou_over_dv = ov["over"]
                ou_line = ou.get("line") if "line" in ou.get("over", {}) else None
                # line 存在与否: 从快照里取
                for sel in ("over", "under"):
                    if sel in ou and ou[sel].get("line") is not None:
                        ou_line = ou[sel]["line"]
                        break
        ah_line, ah_home_dv = None, None
        if ah and "home" in ah and "away" in ah:
            av = devig({"home": ah["home"]["odds"], "away": ah["away"]["odds"]})
            if av:
                ah_home_dv = av["home"]
                for sel in ("home", "away"):
                    if sel in ah and ah[sel].get("line") is not None:
                        ah_line = ah[sel]["line"]
                        break

        # 联赛前缀统计 (仅开赛早于本场)
        lg = m["league"]
        st = league_hist.get(lg)
        league_avg_goals = st["avg_goals"] if st else float("nan")
        league_home_win = st["home_win"] if st else float("nan")
        league_draw = st["draw"] if st else float("nan")

        rec = {
            "match_key": mk, "home": m["home"], "away": m["away"], "league": lg,
            "kickoff_ts": ko_ts, "label": label_of(m["score_home"], m["score_away"]),
            "score_home": m["score_home"], "score_away": m["score_away"],
            "cs_overround": sum(1.0 / o for o in cs_odds.values()),
        }
        # CS 概率向量 + rank-0 (最便宜档)
        for s in SCORELINES:
            rec[f"cs_p_{s}"] = cs_dv[s]
        rec["cs_p_其他"] = cs_dv["其他"]
        cheapest_sel = min(cs_odds, key=cs_odds.get)
        rec["cs_cheapest_p"] = cs_dv[cheapest_sel]
        # 1X2 / OU / AH
        if h2h_dv:
            rec["h2h_h"], rec["h2h_d"], rec["h2h_a"] = h2h_dv["home"], h2h_dv["draw"], h2h_dv["away"]
        else:
            rec["h2h_h"], rec["h2h_d"], rec["h2h_a"] = float("nan"), float("nan"), float("nan")
        rec["ou_line"] = ou_line if ou_line is not None else float("nan")
        rec["ou_over_devig"] = ou_over_dv if ou_over_dv is not None else float("nan")
        rec["ah_line"] = ah_line if ah_line is not None else float("nan")
        rec["ah_home_devig"] = ah_home_dv if ah_home_dv is not None else float("nan")
        rec["lg_avg_goals"] = league_avg_goals
        rec["lg_home_win"] = league_home_win
        rec["lg_draw"] = league_draw
        records.append(rec)

        # 更新联赛前缀统计
        rr = league_running.setdefault(lg, {"n": 0, "goals": 0.0, "hw": 0, "dr": 0})
        rr["n"] += 1
        rr["goals"] += m["score_home"] + m["score_away"]
        if m["score_home"] > m["score_away"]:
            rr["hw"] += 1
        elif m["score_home"] == m["score_away"]:
            rr["dr"] += 1
        league_hist[lg] = {
            "avg_goals": rr["goals"] / rr["n"],
            "home_win": rr["hw"] / rr["n"],
            "draw": rr["dr"] / rr["n"],
        }

    c.close()

    df = pd.DataFrame(records)
    print(f"有效样本: {len(df)} 场 | 跳过: {skipped}")
    if df.empty:
        raise SystemExit("无有效样本")

    # 时间外切分: 后 20% 为测试
    df = df.sort_values("kickoff_ts").reset_index(drop=True)
    n_test = max(1, int(len(df) * 0.2))
    split_ts = df.iloc[-n_test]["kickoff_ts"]
    df["split"] = np.where(df["kickoff_ts"] >= split_ts, "test", "train")
    print(f"训练 {int((df['split']=='train').sum())} / 测试 {int((df['split']=='test').sum())}  | 切分时间 {datetime.fromtimestamp(split_ts, TZ8)}")

    # 标签分布
    print("标签分布 (top 8):")
    for s, n in df["label"].value_counts().head(8).items():
        name = SCORELINES[s] if s < 25 else "其他"
        print(f"  {name}: {n}")

    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    meta = {
        "source": "events.db odds_snapshots (时间线同源, 赛前快照, 临场价)",
        "n_total": int(len(df)),
        "n_train": int((df["split"] == "train").sum()),
        "n_test": int((df["split"] == "test").sum()),
        "split_ts": split_ts,
        "split_ts_str": datetime.fromtimestamp(split_ts, TZ8).isoformat(),
        "classes": SCORELINES + ["其他"],
        "label_dist_train": {SCORELINES[i] if i < 25 else "其他": int(n) for i, n in df[df["split"] == "train"]["label"].value_counts().items()},
    }
    with open(OUT_META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"写出: {OUT_CSV} / {OUT_META}")


if __name__ == "__main__":
    main()
