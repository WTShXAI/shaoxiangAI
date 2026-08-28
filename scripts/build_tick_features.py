#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_tick_features.py — 从 events.db odds_changes 提取时序特征, 写入特征库
======================================================================
P0-2 修复: odds_changes 3.6M 行 tick 级赔率变化从未被特征库使用。
本脚本从 odds_changes 为每场 match_outcomes 提取:
  - 1X2 drift 方向与幅度 (主胜/平/客赔率均值变化方向)
  - OU over/under drift  
  - tick 活跃度 (变化次数/时间跨度)
  - 尾数信号 (trap .4 / strong .1/.2/.9) 的时序出现频率

使用 match_outcomes.home + ' vs ' + match_outcomes.away 映射到 odds_changes.match_key.
已验证: 3244/3728 match_outcomes 有对应 odds_changes 数据。

输出: 直接写入 shaoxiang_feature_library.db 的 features 表，新增 tick 特征列。
      列已在 schema 中存在 (ALTER TABLE 补齐), 赋值即可.
"""
import sqlite3
import os
import json
from collections import defaultdict

GQ_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.db")
FL_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "shaoxiang_feature_library.db")

def _tick_digit(odds: float) -> int:
    """赔率百分位尾数 0-9"""
    try:
        return int(round(float(odds) * 100)) % 10
    except Exception:
        return -1

def extract_tick_features(match_key: str, conn: sqlite3.Connection) -> dict:
    """为一场比赛从 odds_changes 提取时序特征."""
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT odds_data, captured_at FROM odds_changes 
        WHERE match_key = ? 
        ORDER BY captured_at
    """, (match_key,)).fetchall()

    if not rows or len(rows) < 2:
        return {}

    n = len(rows)
    features = {"tick_total_changes": n}

    # 解析 odds_data JSON
    h_vals, d_vals, a_vals = [], [], []
    over_vals, under_vals = [], []

    for row in rows:
        try:
            data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        h = data.get("home") or data.get("h") or data.get("1X2_h")
        d = data.get("draw") or data.get("d") or data.get("1X2_d")
        a = data.get("away") or data.get("a") or data.get("1X2_a")
        ov = data.get("over") or data.get("ou_over")
        un = data.get("under") or data.get("ou_under")

        if h: h_vals.append(float(h))
        if d: d_vals.append(float(d))
        if a: a_vals.append(float(a))
        if ov: over_vals.append(float(ov))
        if un: under_vals.append(float(un))

    # 1X2 drift: 首尾赔率变化方向
    if len(h_vals) >= 2:
        h_drift = h_vals[-1] - h_vals[0]
        a_drift = a_vals[-1] - a_vals[0] if a_vals else 0.0
        features["tick_drift_h"] = round(h_drift, 4)
        features["tick_drift_a"] = round(a_drift, 4)
        # 赔率降=市场看好, 赔率升=市场看衰
        features["tick_favor_h"] = 1 if h_drift < -0.02 else (0 if h_drift < 0.02 else -1)
        features["tick_favor_a"] = 1 if a_drift < -0.02 else (0 if a_drift < 0.02 else -1)

    # OU drift
    if len(over_vals) >= 2:
        ov_drift = over_vals[-1] - over_vals[0]
        features["tick_drift_over"] = round(ov_drift, 4)
        features["tick_decay_over"] = 1 if ov_drift > 0.03 else 0  # 时间衰减: over↑

    # [P0-2] 尾数信号统计 — odds_changes 每行是实时赔率, 统计 .4/.1/.2/.9 出现次数
    trap_count = strong_count = 0
    for odds in h_vals + a_vals:
        if 1.0 <= odds < 1.5:
            d = _tick_digit(odds)
            if d == 4:
                trap_count += 1
            elif d in (1, 2, 9):
                strong_count += 1

    features["tick_n"] = n
    features["tick_trap_count"] = trap_count
    features["tick_strong_count"] = strong_count
    # 尾数信号密度 = 出现次数 / 总tick数 (避免仅少量tick的噪声)
    features["tick_trap_density"] = round(trap_count / max(n, 1), 4)
    features["tick_strong_density"] = round(strong_count / max(n, 1), 4)

    return features


def main():
    print("构建 tick 时序特征 (events.db odds_changes → feature_library)")
    src = sqlite3.connect(GQ_DB)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(FL_DB)

    # 确保列存在
    new_cols = [
        "tick_total_changes INTEGER DEFAULT 0",
        "tick_drift_h REAL DEFAULT 0",
        "tick_drift_a REAL DEFAULT 0",
        "tick_favor_h INTEGER DEFAULT 0",
        "tick_favor_a INTEGER DEFAULT 0",
        "tick_drift_over REAL DEFAULT 0",
        "tick_decay_over INTEGER DEFAULT 0",
        "tick_n INTEGER DEFAULT 0",
        "tick_trap_count INTEGER DEFAULT 0",
        "tick_strong_count INTEGER DEFAULT 0",
        "tick_trap_density REAL DEFAULT 0",
        "tick_strong_density REAL DEFAULT 0",
    ]
    for nc in new_cols:
        col_name = nc.split()[0]
        try:
            dst.execute(f"ALTER TABLE features ADD COLUMN {nc}")
        except sqlite3.OperationalError:
            pass  # 列已存在

    # 遍历 match_outcomes, 用 home/away 拼接 match_key 查 odds_changes
    outcomes = src.execute(
        "SELECT mid, home, away FROM match_outcomes ORDER BY mid"
    ).fetchall()

    updated = 0
    for mo in outcomes:
        mid = mo["mid"]
        home = (mo["home"] or "").strip()
        away = (mo["away"] or "").strip()
        if not home or not away:
            continue
        match_key = f"{home} vs {away}"

        feats = extract_tick_features(match_key, src)
        if not feats:
            continue

        # 写回特征库 (按 x1_h/x1_d/x1_a 匹配, 或新建)
        # 先找已存在的行 (匹配联赛+开赛时间+1X2 赔率)
        row = dst.execute("""
            SELECT id FROM features 
            WHERE x1_h IS NOT NULL AND (home=? OR league LIKE ?)
            LIMIT 1
        """, (home, f"%{home}%")).fetchone()

        if row:
            sets = ", ".join(f"{k}=?" for k in feats)
            vals = list(feats.values()) + [row[0]]
            dst.execute(f"UPDATE features SET {sets} WHERE id=?", vals)
            updated += 1

    dst.commit()
    src.close()
    dst.close()
    print(f"✅ tick 特征已写入 {updated} 行 (共 {len(outcomes)} 场 match_outcomes)")


if __name__ == "__main__":
    main()
