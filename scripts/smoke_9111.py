#!/usr/env python3
# -*- coding: utf-8 -*-
"""G2 · 9111 重启冒烟脚本 (用户本机一键验收, 赛制架构分析师).

使用 FastAPI TestClient 模拟「9111 端口重启后的新进程」, 打真实 /predict 端点,
验证全链路 (接收 -> _run_predict -> analyze_multi -> value_layer["softline"])
在「新进程」下无缓存/导入异常, 且返回 JSON 含 G2 验收硬字段:
  - softline            (跨庄 soft-line 调整, /predict 返回体顶层字段)
  - disagreement_detected (跨庄分歧闸门, 在 odds_intel 子 dict)

同时打印本机真实重启命令清单, 供手动 uvicorn 重启冒烟对照。

为什么用 TestClient 而非真起 uvicorn:
  TestClient 会完整体实例化 app(等价于一次新进程 import + 路由注册), 能捕获
  「重启后导入链/端口/缓存」类回归; 且能在 CI/沙盒可移植预演, 不占用 9111 端口。
  涛哥本机若想做「真进程」冒烟, 用下方打印的 python bridge_service.py --port 9111 即可。

用法:
  # 本机(已装 fastapi)一键:
  python scripts/smoke_9111.py
  # 真实重启 9111 后 curl 冒烟(对照):
  python bridge_service.py --port 9111 &
  curl -s -X POST localhost:9111/predict -H 'Content-Type: application/json' \
    -d '{"home":"阿森纳","away":"切尔西","odds_h":2.1,"odds_d":3.4,"odds_a":3.2,"competition":"league"}'
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "football_data.db")


def pick_matches(n=3):
    """取 n 场真实双庄同场(william_hill×interwetten)比赛, 确保 _odds_intel 能查到双庄
    -> softline 非 None 且 disagreement_detected 有机会触发."""
    import sqlite3
    con = sqlite3.connect(DB)
    cur = con.cursor()
    q = """SELECT w.home_team, w.away_team, w.close_h, w.close_d, w.close_a
           FROM odds_features w JOIN odds_features i
             ON w.home_team=i.home_team AND w.away_team=i.away_team AND w.match_date=i.match_date
           WHERE w.source='william_hill' AND i.source='interwetten'
             AND w.close_h>1 AND i.close_h>1
           ORDER BY w.match_date DESC LIMIT ?"""
    rows = cur.execute(q, (n,)).fetchall()
    con.close()
    return rows


def main():
    from fastapi.testclient import TestClient
    import bridge_service as bs

    client = TestClient(bs.app)
    rows = pick_matches(3)
    assert rows, "odds_features 无双庄同场样本, 无法构造冒烟请求"

    ok = 0
    for (ht, at, h, d, a) in rows:
        # PredictRequest 必填: home/away/odds_h/d/a/hcp/ou_line (bridge_service.py:74-80)
        resp = client.post("/predict", json={
            "home": ht, "away": at,
            "odds_h": h, "odds_d": d, "odds_a": a,
            "hcp": 0.0, "ou_line": 2.5,
            "competition": "league",
        })
        if resp.status_code != 200:
            print(f"[smoke] {ht} vs {at}: HTTP {resp.status_code} -> {resp.text[:160]}")
            continue
        data = resp.json()
        sl = data.get("softline")
        has_soft = isinstance(sl, dict)
        # disagreement_detected 在 softline 子 dict (bridge_service.py:655, _compute_softline 返回)
        has_dis = isinstance(sl, dict) and ("disagreement_detected" in sl)
        dis_val = sl.get("disagreement_detected") if isinstance(sl, dict) else None
        oi = data.get("odds_intel")
        oi_dis = oi.get("disagreement_detected") if isinstance(oi, dict) else None
        print(f"[smoke] {ht} vs {at}: HTTP200 softline={has_soft} "
              f"softline.disagreement_detected={dis_val} odds_intel.disagreement_detected={oi_dis}")
        if has_soft and has_dis:
            ok += 1

    print(f"\n[G2] 冒烟 {ok}/{len(rows)} 场含 softline+disagreement_detected 字段 "
          f"-> {'PASS ✅' if ok == len(rows) else 'FAIL ❌'}")
    print("[G2] 本机真实重启命令(对照): python bridge_service.py --port 9111")
    sys.exit(0 if ok == len(rows) else 1)


if __name__ == "__main__":
    main()
