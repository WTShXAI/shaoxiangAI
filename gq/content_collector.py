#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gq/content_collector.py — 赛事内容采集 (赛事前瞻 / 伤病 / 赛果 / 情报)

独立于 WS 实时盘口流, 走乐鱼 HTTP 端点 getMatchAnalysiseDataPB。
由 ws_collector 在发现新比赛时(节流)后台调用, 结果写入 events.db 的 content(前瞻/伤病/情报)
与 h2h(赛果页挖掘的两队历史交锋)。events.db 是融合全部维度的唯一完整赛事库。

逆向要点 (2026-08-27 抓包 + SPA JS 逆向, 实测通过):
- 端点: POST https://api.wnbtmel.com/yewu11/v1/w/matchAnalysise/getMatchAnalysiseDataPB
- 鉴权: 复用 auto_collector._build_headers() (checkid/requestid), 不用 cookie。
- ★ 关键坑: sonMenuId 必须是【数字 tab 索引】, 不是字符串 id!
    原逆向误用 sonMenuId="football_match_preview"(字符串) → 恒返 0400500(参数错误)。
    parentMenuId=2 (主分析 tab):
        sonMenuId=1 → 赛事前瞻(future statistics) + 伤病(sidelined)   ← 本模块主用
        sonMenuId=2 → 赛果(已完场有数据, 未开赛/无数据返 0401038)
        sonMenuId=0 → 阵容(lineup, 多数场次返 0408006=未公布, 暂未接入)
    parentMenuId=4 (情报 tab): sonMenuId=2 → 第三方情报 sThirdMatchInformationDTOList
- 返回 data 为 gzip+base64 (与 WS C105 同编码, 以 "H4sI" 开头), 解码后结构:
    { "inParam": {...}, "basicInfoMap": {
        "sThirdMatchFutureStatisticsDTOMap": {"1":[未来赛程...], "2":[...]},  # 前瞻(赛程密度/休整)
        "sThirdMatchSidelinedDTOMap": {...}                                   # 伤病(稀疏, 无则空 dict)
    }}
- 阵容专用端点 getMatchLineupListPB / getPCMatchLineupListPB 无论传 standardMatchId/matchId
  均返 0400500(参数受 SPA 会话保护), 需更深逆向, 本期未接入(用户未显式要求阵容)。
"""

import sys
import os
import json
import gzip
import base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gq.auto_collector as ac

# 完整赛事库 events.db: 内容(前瞻/伤病/情报)经 gq.db 落 match_meta; 赛果页 H2H 落 h2h。
from gq.db import upsert_match_meta
# 赛果页 H2H 落地 events.db.h2h (容错导入)
try:
    import gq.event_db as event_db
    event_db.init_event_db()
except Exception:
    event_db = None

CONTENT_PATH = "/yewu11/v1/w/matchAnalysise/getMatchAnalysiseDataPB"
PREVIEW_MENU = (2, 1)   # parentMenuId, sonMenuId → 前瞻 + 伤病
RESULT_MENU = (2, 2)    # 赛果(完场可用)
INFO_MENU = (4, 2)      # 第三方情报


def _decode(b64):
    """解码 gzip+base64 的 data 字段 → dict; 失败返回 None。"""
    if not isinstance(b64, str) or not b64.startswith("H4sI"):
        return None
    try:
        return json.loads(gzip.decompress(base64.b64decode(b64)).decode("utf-8"))
    except Exception:
        return None


def _fetch(mid, parent_menu: int, son_menu: int):
    """单次内容请求; 成功返回解码 dict, 否则 None。"""
    r = ac._api_post(CONTENT_PATH,
                     {"parentMenuId": parent_menu, "sonMenuId": son_menu,
                      "standardMatchId": int(mid)})
    if not r or r.get("code") != "0000000":
        return None
    return _decode(r.get("data"))


def fetch_match_content(mid) -> dict:
    """抓取某比赛的全部可获取内容。

    返回 dict: {preview, injuries, result, info, ok_count}
      preview   : JSON(未来赛程, 前瞻) 或 ""
      injuries  : JSON(伤病原始 map) 或 "" (无伤病则不写)
      result    : JSON(赛果) 或 ""
      info      : JSON(第三方情报) 或 ""
      ok_count  : 成功抓到的内容项数
    """
    out = {"preview": "", "injuries": "", "result": "", "info": "", "h2h": "", "ok_count": 0}

    # 主: 前瞻 + 伤病 (parentMenuId=2, sonMenuId=1)
    d = _fetch(mid, *PREVIEW_MENU)
    if d:
        bim = (d.get("basicInfoMap") or {})
        futs = bim.get("sThirdMatchFutureStatisticsDTOMap")
        if futs is not None:
            out["preview"] = json.dumps(futs, ensure_ascii=False)
            out["ok_count"] += 1
        sid = bim.get("sThirdMatchSidelinedDTOMap")
        if sid:  # 仅在有伤病时写(空 dict 不写, 保留原值)
            out["injuries"] = json.dumps(sid, ensure_ascii=False)
            out["ok_count"] += 1

    # 赛果 (parentMenuId=2, sonMenuId=2, 完场可用)
    # 注意: 赛果页返回的是【两队历史交锋 H2H】, 非单场终比分! 单场终比分来自 WS C103 / matches 表。
    d2 = _fetch(mid, *RESULT_MENU)
    if d2:
        out["result"] = json.dumps(d2, ensure_ascii=False)
        out["ok_count"] += 1
        bim2 = (d2.get("basicInfoMap") or {})
        h2h = bim2.get("matchHistoryBattleDTOMap")
        if h2h:   # 落地到 events.db 的 h2h (总进球维度历史交锋)
            out["h2h"] = json.dumps(h2h, ensure_ascii=False)
            out["ok_count"] += 1

    # 第三方情报 (parentMenuId=4, sonMenuId=2)
    d3 = _fetch(mid, *INFO_MENU)
    if d3:
        out["info"] = json.dumps(d3, ensure_ascii=False)
        out["ok_count"] += 1

    return out


def collect_and_store(match_key: str, mid) -> bool:
    """抓取并写入 events.db(非阻塞友好, 可在守护线程调用)。成功返回 True。
    内容(前瞻/伤病/情报)落地 match_meta 表, 赛果页挖掘的两队历史交锋(H2H, 总进球维度)落地 h2h 表。"""
    try:
        c = fetch_match_content(mid)
        if c["ok_count"] == 0:
            return False
        upsert_match_meta(match_key, mid=str(mid),
                          preview=c["preview"],
                          injuries_home=c["injuries"],
                          news=c["info"])
        if event_db is not None and c["h2h"]:
            event_db.record_h2h(match_key, str(mid), c["h2h"])
        return True
    except Exception:
        return False
