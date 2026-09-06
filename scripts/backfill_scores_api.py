# -*- coding: utf-8 -*-
"""
events.db 比分回填脚本 (standalone, 不 import gq.auto_collector)

用途: 补齐"有赔率但无有效 match_outcomes.result"的两类比赛的最终比分:
  - live-only (无初盘快照, 纯滚球): 当滚球标签用, 回填终比分即可
  - has-prematch (有初盘快照, 赛果未归档): 补终比分 + 触发 record_match_outcome 归档初盘→赛果

设计铁律 (对齐深度研究报告 + result.md):
  1. 复制 auto_collector.py 的【最小 API client】逐字副本 (fetch_match_structure / _score_from_msc
     / _status_minute / _api_post / _build_headers / _get_request_id / _decode),
     **绝不 import gq.auto_collector** —— 避开 msvcrt 单例文件锁 (会与运行中的采集器争锁)。
  2. 只 import gq.db 的 conn / DB_PATH / record_match_outcome (这些无单例副作用, 安全)。
  3. 以 mid 为锚 UPSERT; WAL + 事务 + 幂等; 不写库除非 --apply; 默认 dry-run。
  4. apply 前做【针对性行备份】(仅本操作涉及的 matches + match_outcomes 行), 轻量可回滚,
     不复制整个 10.8GB 主库 (避免与采集器写入争锁)。
  5. 只回填"已完场且有终比分"的比赛; 仍在进行/未开赛/API 无比分的, 跳过不强填。
  6. 尊重人工纠偏锁 is_override (被锁定的比赛一律跳过, 不覆盖)。

用法:
  python scripts/backfill_scores_api.py                 # dry-run (默认, 不写库, 仍会打 API)
  python scripts/backfill_scores_api.py --apply        # 真正写库
  python scripts/backfill_scores_api.py --test-mid 12345   # 单 mid API 实测 + 打印原始响应
  python scripts/backfill_scores_api.py --limit 10     # 只处理前 N 个目标 (调试)
"""
from __future__ import annotations

import argparse, base64, gzip, json, os, re, sys, time, uuid
# 让 `import gq` 可解析 (脚本在 scripts/, gq 在项目根)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone, timedelta
from typing import Optional

import urllib.request

# ───────────────────────── 最小 API client (逐字复制自 gq/auto_collector.py) ─────────────────────────
# 仅复制回填所需函数; 不引入 singleton / collector 主循环 / safe_log 等副作用。

HOST = "https://api.wnbtmel.com"
CUID = "526002076777845380"
STRUCT_PATH = "/yewu11/v1/w/structureMatchBaseInfoByMidsPB"
_COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

_FENG_KONG_UNTIL = 0.0  # 全局风控截止时间 (本脚本基本用不到, 保留以兼容 _api_post)


def _safe_print(*args, **kw):
    """ASCII 护栏: 控制台编码异常时 backslashreplace, 绝不抛 UnicodeEncodeError 崩进程。"""
    try:
        print(*args, **kw, flush=True)
    except UnicodeEncodeError:
        s = " ".join(str(a) for a in args)
        enc = (sys.stdout.encoding or "utf-8")
        print(s.encode(enc, "backslashreplace").decode(enc, "ignore"), flush=True)


def _load_request_id() -> str:
    """优先环境变量 GQ_REQUEST_ID, 否则读 gq/.env (与采集器共用同一 token)。"""
    env_tok = os.environ.get("GQ_REQUEST_ID")
    if env_tok:
        return env_tok
    try:
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gq", ".env")
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("GQ_REQUEST_ID="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "22f9755cba6b14eac450b4d2e537072607fac7a3"


_RID_CACHE = {"val": None, "ts": 0.0}
_RID_TTL = 30.0
def _get_request_id() -> str:
    global _RID_CACHE
    now = time.time()
    if _RID_CACHE["val"] is None or now - _RID_CACHE["ts"] >= _RID_TTL:
        _RID_CACHE["val"] = _load_request_id()
        _RID_CACHE["ts"] = now
    return _RID_CACHE["val"]


def _build_headers() -> dict:
    checkid = f"pc-{uuid.uuid4().hex}-{CUID}-{int(time.time() * 1000)}"
    h = dict(_COMMON_HEADERS)
    h["checkid"] = checkid
    h["requestid"] = _get_request_id()
    return h


def _api_post(path: str, body: dict, timeout: int = 20) -> Optional[dict]:
    global _FENG_KONG_UNTIL
    url = HOST + path
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_build_headers(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                _safe_print(f"[WARN] {path} HTTP {resp.status}")
                return None
            raw = resp.read().decode("utf-8", errors="ignore")
        if any(k in raw for k in ("天级流控", "aliyun", "captcha", "滑动验证")):
            _FENG_KONG_UNTIL = time.time() + 1800
            _safe_print(f"[风控] 阿里云流控触发 -> 暂停30分钟")
            return None
        return json.loads(raw)
    except Exception as e:
        _safe_print(f"[WARN] {path} 请求失败: {e}")
        return None


def _decode(raw) -> Optional[dict]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(gzip.decompress(base64.b64decode(raw)).decode("utf-8"))
    except Exception as e:
        _safe_print(f"[WARN] 解码失败: {e}")
        return None


def fetch_match_structure(mids: list) -> list:
    """批量拉比赛基础信息(早盘/未开赛也返回队名+开赛时间+比分)。

    走 STRUCT_PATH: 传 mids 逗号串, 返回解码后的比赛 dict 列表; 失败/空返回 []。
    自动分批(每批 <=25)。
    """
    if not mids:
        return []
    out = []
    batch_size = 25
    try:
        mid_list = [str(x).strip() for x in mids if str(x).strip()]
    except Exception:
        return []
    for i in range(0, len(mid_list), batch_size):
        batch = mid_list[i:i + batch_size]
        body = {"mids": ",".join(batch), "cuid": CUID,
                "cos": 0, "orpt": 0, "euid": "3020101"}
        js = _api_post(STRUCT_PATH, body)
        if not js or js.get("code") != "0000000":
            continue
        data = _decode(js.get("data", ""))
        if isinstance(data, dict):
            arr = data.get("data") or []
        elif isinstance(data, list):
            arr = data
        else:
            arr = []
        for m in arr:
            if isinstance(m, dict):
                out.append(m)
        time.sleep(0.05)
    return out


def _score_from_msc(msc):
    """msc 统计标记集合(list) -> (sh, sa, ht_sh, ht_sa) 整数元组; 解析失败返回 (None,)*4。"""
    if not msc:
        return None, None, None, None

    def _split(it):
        if not isinstance(it, str) or "|" not in it:
            return None, None
        s = it.split("|")[-1]
        try:
            h, a = s.split(":")
            return int(h), int(a)
        except Exception:
            return None, None

    full = half = None
    for item in msc:
        if isinstance(item, str):
            if item.startswith("S0|") and full is None:
                full = item
            elif item.startswith("S1|") and half is None:
                half = item
    if full is None and msc:
        full = msc[-1]
    return _split(full) + _split(half)


def _status_minute(mlet, kickoff_ts, now_ts):
    """从 mlet 推断状态(scheduled/live/finished) + 分钟数。kickoff 毫秒, now 秒。"""
    elapsed = (now_ts - kickoff_ts / 1000) if kickoff_ts else None
    if not mlet:
        if elapsed is not None and elapsed >= 0:
            st = "live"
            minute = max(0, int(elapsed / 60))
        else:
            st = "scheduled"
            minute = 0
    else:
        st = "live"
        mm = re.match(r"(\d+)(?:\+(\d+))?", mlet)
        if mm:
            minute = int(mm.group(1)) + (int(mm.group(2)) if mm.group(2) else 0)
        else:
            minute = 0
        m = re.match(r"(\d+):\d+", mlet)
        if (m and int(m.group(1)) >= 90) or "FT" in mlet or "完" in mlet:
            st = "finished"
    if kickoff_ts and now_ts < kickoff_ts / 1000:
        st = "scheduled"
        minute = 0
    if elapsed is not None and elapsed > 3.5 * 3600:
        st = "finished"
    if elapsed is not None:
        est = max(0, int(elapsed / 60))
        if minute is not None and abs(est - minute) > 10:
            if minute < 90 and elapsed < 3.5 * 3600:
                pass
            else:
                minute = est
                if est >= 90:
                    st = "finished"
    if minute == 45 and elapsed is not None and elapsed > 60 * 60:
        minute = max(90, min(125, int(elapsed / 60)))
    return st, minute


# ───────────────────────── DB 访问 (只 import gq.db 安全 API) ─────────────────────────
from gq.db import conn as _gq_conn, DB_PATH, record_match_outcome


def get_targets(limit: Optional[int] = None) -> list:
    """返回回填目标列表 (dict), 含 match_key/mid/home/away/league/kickoff/score_home/score_away/bucket。"""
    t0 = time.time()
    valid_mids = set()
    with _gq_conn(readonly=True) as c:
        for r in c.execute(
            "SELECT mid FROM match_outcomes WHERE result IN ('home','draw','away') AND is_valid=1 AND mid IS NOT NULL"):
            valid_mids.add(r["mid"])
        odds_map = {}
        for r in c.execute(
            "SELECT match_key, COUNT(*) AS n,"
            " SUM(CASE WHEN minute_at IS NULL OR minute_at=0 THEN 1 ELSE 0 END) AS n_pre,"
            " SUM(CASE WHEN minute_at>0 THEN 1 ELSE 0 END) AS n_in"
            " FROM odds_snapshots GROUP BY match_key"):
            odds_map[r["match_key"]] = (r["n"], r["n_pre"], r["n_in"])
        targets = []
        for r in c.execute(
            "SELECT match_key, mid, home, away, league, kickoff, score_home, score_away "
            "FROM matches WHERE mid IS NOT NULL"):
            mk = r["match_key"]
            if mk not in odds_map:
                continue
            if r["mid"] in valid_mids:
                continue
            n, n_pre, n_in = odds_map[mk]
            targets.append({
                "match_key": mk, "mid": r["mid"], "home": r["home"], "away": r["away"],
                "league": r["league"], "kickoff": r["kickoff"],
                "score_home": r["score_home"], "score_away": r["score_away"],
                "bucket": "live-only" if n_pre == 0 else "has-prematch",
            })
            if limit and len(targets) >= limit:
                break
    _safe_print(f"[targets] 共 {len(targets)} 个目标 ({time.time()-t0:.1f}s)")
    return targets


def iso_from_ms(mgt_ms) -> str:
    try:
        return datetime.fromtimestamp(float(mgt_ms) / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def derive_result(sh, sa) -> str:
    if sh > sa:
        return "home"
    if sh == sa:
        return "draw"
    return "away"


def backup_rows(targets: list, path: str):
    """针对性备份: 把将涉及的 matches + match_outcomes 行导出为 JSON, 供回滚。"""
    mids = [t["mid"] for t in targets]
    with _gq_conn(readonly=True) as c:
        mrows = [dict(r) for r in c.execute(
            "SELECT * FROM matches WHERE mid IN ({})".format(",".join("?" * len(mids))), mids)]
        orows = [dict(r) for r in c.execute(
            "SELECT * FROM match_outcomes WHERE mid IN ({})".format(",".join("?" * len(mids))), mids)]
    payload = {"ts": time.time(), "mids": mids, "matches": mrows, "match_outcomes": orows}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)
    _safe_print(f"[backup] 已备份 {len(mrows)} 行 matches + {len(orows)} 行 match_outcomes -> {path}")


def write_one(t, sh, sa, ht_sh, ht_sa, kickoff_iso):
    """写单场: matches 终比分+finished; match_outcomes 归档(insert 或 补 NULL-score 行)。

    事务由调用方控制 (conn 上下文)。返回 ('inserted'|'filled'|'skipped_existing'|'error', msg)。
    """
    mid = t["mid"]; mk = t["match_key"]
    home = t["home"]; away = t["away"]; league = t["league"] or ""
    result = derive_result(sh, sa)

    # 友谊赛: 系统 P0a 规则明确跳过(不污染复盘库), 非错误
    if "友谊" in league:
        return ("skipped_friendly", f"友谊赛排除(系统规则) mid={mid} {home} vs {away}")

    with _gq_conn() as c:
        # 1) matches: 终比分 + 状态 (尊重 is_override 锁)
        c.execute(
            "UPDATE matches SET score_home=?, score_away=?, ht_score_home=?, ht_score_away=?,"
            " status='finished', minute=90, last_seen=? "
            "WHERE mid=? AND (is_override IS NULL OR is_override=0)",
            (sh, sa, ht_sh, ht_sa, time.time(), mid))

        # 2) match_outcomes
        exist = c.execute("SELECT score_home, is_valid, is_override FROM match_outcomes WHERE mid=?", (mid,)).fetchone()
        if exist is None:
            # 新行: 走 record_match_outcome (自动补初盘 + 校验 + 幂等)
            rec = record_match_outcome(mid, home, away, league, kickoff_iso,
                                       sh, sa, ht_sh, ht_sa)
            if rec is None:
                return ("error", f"record_match_outcome 返回 None (mid={mid})")
            return ("inserted", f"{home} vs {away} {sh}-{sa} ({result})")
        else:
            if exist["is_override"]:
                return ("skipped_existing", f"被人工锁定, 跳过 mid={mid}")
            if exist["score_home"] is not None:
                return ("skipped_existing", f"已有终比分, 跳过 mid={mid}")
            # 已存在 NULL-score 行 -> 补比分 + result (不动初盘列)
            c.execute(
                "UPDATE match_outcomes SET score_home=?, score_away=?, ht_score_home=?,"
                " ht_score_away=?, result=?, is_valid=1, source='backfill', archived_at=? "
                "WHERE mid=?",
                (sh, sa, ht_sh, ht_sa, result, time.time(), mid))
            return ("filled", f"{home} vs {away} {sh}-{sa} ({result}) [补 NULL 行]")


def run_validation(targets: list) -> dict:
    """回填后校验。"""
    mids = [t["mid"] for t in targets]
    lo_mids = [t["mid"] for t in targets if t["bucket"] == "live-only"]
    ph = ",".join("?" * len(mids)) if mids else "''"
    with _gq_conn(readonly=True) as c:
        null_score = c.execute(
            f"SELECT COUNT(*) FROM match_outcomes WHERE mid IN ({ph}) AND (score_home IS NULL OR result IS NULL)",
            mids).fetchone()[0] if mids else 0
        # 2026-08-27 修复: result 列混用两套词表 (gq/wc/backfill 用 'home'/'draw'/'away',
        # forced_status_recovery 用 'H'/'D'/'A')。任一词表下 score↔result 一致即判一致,
        # 否则会因词表不匹配误报 593 条"不一致"(实则数据干净, 0 条真冲突)。
        inconsistent = c.execute(
            f"""SELECT COUNT(*) FROM match_outcomes WHERE mid IN ({ph})
                AND score_home IS NOT NULL AND score_away IS NOT NULL AND result IS NOT NULL
                AND NOT (
                  (score_home>score_away AND result IN ('H','home'))
               OR (score_home<score_away AND result IN ('A','away'))
               OR (score_home=score_away AND result IN ('D','draw'))
                )""",
            mids).fetchone()[0] if mids else 0
        live_only_with_result = 0
        if lo_mids:
            ph2 = ",".join("?" * len(lo_mids))
            live_only_with_result = c.execute(
                f"SELECT COUNT(*) FROM match_outcomes WHERE mid IN ({ph2}) "
                f"AND result IN ('home','draw','away') AND is_valid=1",
                lo_mids).fetchone()[0]
    return {
        "targets": len(targets),
        "null_score_cnt": null_score,
        "score_result_inconsistent": inconsistent,
        "live_only_with_result": live_only_with_result,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库 (默认 dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 个目标 (调试)")
    ap.add_argument("--test-mid", type=str, default=None, help="单 mid API 实测, 打印原始响应后退出")
    ap.add_argument("--delay", type=float, default=0.2, help="批间限速(秒)")
    args = ap.parse_args()

    if args.test_mid:
        raw = fetch_match_structure([args.test_mid])
        _safe_print(f"[TEST] mid={args.test_mid} 返回 {len(raw)} 条")
        for m in raw:
            _safe_print(f"  raw keys: {sorted(m.keys())}")
            _safe_print(f"  mhn={m.get('mhn')!r} man={m.get('man')!r} mgt={m.get('mgt')!r} tnjc={m.get('tnjc')!r} mststi={m.get('mststi')!r}")
            msc = m.get("msc")
            _safe_print(f"  msc={msc!r} (type={type(msc).__name__})")
            sh, sa, ht_sh, ht_sa = _score_from_msc(msc)
            _safe_print(f"  -> parsed score: {sh}-{sa} (HT {ht_sh}-{ht_sa})")
        return

    targets = get_targets(limit=args.limit)
    if not targets:
        _safe_print("[done] 无回填目标")
        return

    # ── 数据源 = 本地 matches 表 ──
    # 实测结论: 乐鱼 GQ 的 STRUCT/ODDS 端点只服务"进行中/即将开赛"的比赛, 历史完场(>~2天)
    # 已被清库, 无法经 API 取回终比分。但 collector 在完场时往往已把终比分写入 matches
    # (status='finished', score_home/away 已填), 只是因"状态-比分耦合 bug"未能归档进
    # match_outcomes。故本回填以 matches 为权威源, 把已知终比分 propagation 到 match_outcomes。
    by_mid = {t["mid"]: t for t in targets}
    plan = []          # 将写入的 (target, sh, sa, ht_sh, ht_sa, kickoff_iso)
    skipped = []       # 跳过原因 (mid, reason)

    _safe_print(f"[src] 从本地 matches 读取 {len(targets)} 个目标的终比分 ...")
    with _gq_conn(readonly=True) as c:
        for t in targets:
            mid = t["mid"]
            m = c.execute(
                "SELECT status, score_home, score_away, ht_score_home, ht_score_away "
                "FROM matches WHERE mid=?", (mid,)).fetchone()
            if not m:
                skipped.append((mid, "matches 无此行"))
                continue
            status, sh, sa, hth, hta = m["status"], m["score_home"], m["score_away"], m["ht_score_home"], m["ht_score_away"]
            if status != "finished":
                skipped.append((mid, f"状态非完场(status={status})"))
                continue
            if sh is None or sa is None:
                # 完场但 matches 也无比分 = GQ 已彻底丢失, API 亦清库 -> 需外部数据源
                skipped.append((mid, "matches 无终比分(GQ历史已丢, 需外部源)"))
                continue
            # HT 健全性: 半场进球数不可能超过全场; 违者视为 msc S1 污染, 置 NULL 不传播
            if hth is None or hta is None or hth > sh or hta > sa:
                hth = hta = None
            plan.append((t, sh, sa, hth, hta, t["kickoff"] or ""))

    _safe_print(f"\n[plan] 将写入 {len(plan)} 场, 跳过 {len(skipped)} 场")
    if skipped:
        _safe_print("[plan] 跳过明细(前 15):")
        for mid, why in skipped[:15]:
            _safe_print(f"   {mid}: {why}")

    # 按 bucket 统计
    from collections import Counter
    wb = Counter(p[0]["bucket"] for p in plan)
    sb = Counter()
    for mid, why in skipped:
        sb[by_mid[mid]["bucket"]] += 1
    _safe_print(f"[plan] 写入分布: live-only={wb.get('live-only',0)}, has-prematch={wb.get('has-prematch',0)}")
    _safe_print(f"[plan] 跳过分布: live-only={sb.get('live-only',0)}, has-prematch={sb.get('has-prematch',0)}")

    if not args.apply:
        _safe_print("\n[DRY-RUN] 未写库。示例写入 (前 10):")
        for t, sh, sa, ht_sh, ht_sa, ki in plan[:10]:
            _safe_print(f"   {t['bucket']:12s} {t['home']} vs {t['away']} -> {sh}-{sa} (HT {ht_sh}-{ht_sa}) kickoff={ki}")
        _safe_print("\n[DRY-RUN] 加 --apply 才真正写库。")
        return

    # ── apply ──
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"backfill_backup_{ts}.json")
    backup_rows(targets, backup_path)

    inserted = filled = errors = skipped_friendly = skipped_existing = 0
    for t, sh, sa, ht_sh, ht_sa, ki in plan:
        try:
            status, msg = write_one(t, sh, sa, ht_sh, ht_sa, ki)
        except Exception as e:
            errors += 1
            _safe_print(f"[ERROR] mid={t['mid']}: {e}")
            continue
        if status == "inserted":
            inserted += 1
        elif status == "filled":
            filled += 1
        elif status == "skipped_friendly":
            skipped_friendly += 1
        elif status == "skipped_existing":
            skipped_existing += 1
        else:
            errors += 1
        _safe_print(f"  [{status}] {msg}")

    _safe_print(f"\n[APPLY] inserted={inserted}, filled={filled}, skipped_friendly={skipped_friendly}, skipped_existing={skipped_existing}, errors={errors}")
    v = run_validation(targets)
    _safe_print(f"[VALIDATION] {json.dumps(v, ensure_ascii=False)}")
    if v["score_result_inconsistent"] > 0:
        _safe_print(f"[VALIDATION][WARN] 发现 {v['score_result_inconsistent']} 条 score/result 不一致!")
    _safe_print(f"[APPLY] 备份文件: {backup_path}")


if __name__ == "__main__":
    main()
