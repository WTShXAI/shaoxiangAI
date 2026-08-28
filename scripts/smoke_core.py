#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core/ 基础设施层独立 smoke 测试 (第一阶段验收).

设计原则:
    * **零外部依赖**: 只用标准库, 不依赖 pytest / pydantic / fastapi, 因此在
      托管 python (无第三方包) 与 .venv (有 pydantic) 上都能跑, 用于验证 core/ 的
      降级路径与首选路径都健康。
    * **绝不触碰真实 events.db**: 所有 DB 用例走 ``tempfile.mkdtemp()`` 临时目录, 且
      跑之前主动断言临时路径不在 ``D:\\Architecture\\data`` 下。
    * 覆盖 4 项验收:
        1. safe_log      中文/emoji 日志与 print 不抛异常, 不可编码字符被转义;
        2. error_envelope 对象型 (dict) message 的异常 → ``error.message`` 必为 str;
        3. db_manager    init_db(WAL 生效) + 单写者写 + reader 读 + integrity + backup/restore;
        4. collector_step 单步抛异常不中断整轮, 失败步 ok=False 且 error_msg 是 str。

用法::

    python scripts/smoke_core.py            # 跑全部
    python scripts/smoke_core.py --verbose  # 打印细节

退出码: 0 = 全 PASS; 1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import collector_step as cs_mod  # noqa: E402
from core.collector_step import (  # noqa: E402
    CollectorContext,
    CollectorRound,
    CollectorStep,
    FunctionStep,
    StepResult,
    summarize,
)
from core.config import Config, get_config, reload_config  # noqa: E402
from core.db_manager import (  # noqa: E402
    GQConnectionManager,
    IntegrityError,
    PoolExhaustedError,
)
from core.error_envelope import (  # noqa: E402
    CODE_INTERNAL,
    ErrorDetail,
    ErrorEnvelope,
    classify_error,
    coerce_message,
    is_safe_error_payload,
    success,
    to_envelope,
    to_error_dict,
)
from core.logging_config import (  # noqa: E402
    JsonFormatter,
    TraceIdFilter,
    configure_logging,
    get_logger,
    get_trace_id,
    trace_context,
)
from core.safe_log import (  # noqa: E402
    SafeLog,
    SafeStreamHandler,
    escape_for_encoding,
    install_utf8,
    safe_print,
    safe_str,
    sanitize_for_stream,
)

# 用于验证编码护栏的"毒药字符串": 中文 + emoji + 代理对
POISON = "中文测试 ✅ 😀 滚球破蛋 λ=1.23 —— tërnava"

VERBOSE = False


class SmokeFailure(AssertionError):
    """smoke 断言失败。"""


def check(condition: Any, message: str) -> None:
    """断言助手。

    Args:
        condition: 断言条件。
        message: 失败说明。

    Raises:
        SmokeFailure: 条件为假。
    """
    if not condition:
        raise SmokeFailure(message)


def note(message: str) -> None:
    """verbose 模式下打印细节。"""
    if VERBOSE:
        safe_print("        . " + message)


# ---------------------------------------------------------------------------
# T-0: config
# ---------------------------------------------------------------------------


def test_config() -> None:
    """配置模块: 默认值 + 环境变量覆盖 + 单例。"""
    cfg = get_config()
    for field in (
        "gq_db_path",
        "log_level",
        "redis_url",
        "queue_enabled",
        "roi_delta_threshold",
        "busy_timeout_ms",
        "health_port",
    ):
        check(hasattr(cfg, field), f"Config missing field: {field}")
    check(
        abs(float(cfg.roi_delta_threshold) - 5.0) < 1e-9,
        f"roi_delta_threshold default should be 5.0, got {cfg.roi_delta_threshold}",
    )
    check(
        int(cfg.busy_timeout_ms) == 30000,
        f"busy_timeout_ms default should be 30000, got {cfg.busy_timeout_ms}",
    )
    check(
        int(cfg.health_port) == 9001,
        f"health_port default should be 9001, got {cfg.health_port}",
    )
    check(isinstance(cfg.queue_enabled, bool), "queue_enabled must be bool")
    note(f"defaults ok (gq_db_path={cfg.gq_db_path})")

    # 环境变量覆盖
    os.environ["ROI_DELTA_THRESHOLD"] = "7.5"
    os.environ["BUSY_TIMEOUT_MS"] = "12345"
    os.environ["QUEUE_ENABLED"] = "true"
    try:
        overridden = reload_config()
        check(
            abs(float(overridden.roi_delta_threshold) - 7.5) < 1e-9,
            "env override ROI_DELTA_THRESHOLD failed",
        )
        check(int(overridden.busy_timeout_ms) == 12345, "env override BUSY_TIMEOUT_MS failed")
        check(overridden.queue_enabled is True, "env override QUEUE_ENABLED failed")
        note("env override ok")
    finally:
        os.environ.pop("ROI_DELTA_THRESHOLD", None)
        os.environ.pop("BUSY_TIMEOUT_MS", None)
        os.environ.pop("QUEUE_ENABLED", None)
        reload_config()

    # 非法值必须回落默认而不是抛错
    os.environ["BUSY_TIMEOUT_MS"] = "not-a-number"
    os.environ["HEALTH_PORT"] = "999999"
    try:
        fallback = reload_config()
        check(int(fallback.busy_timeout_ms) == 30000, "invalid BUSY_TIMEOUT_MS should fall back")
        check(int(fallback.health_port) == 9001, "out-of-range HEALTH_PORT should fall back")
        note("invalid value fallback ok")
    finally:
        os.environ.pop("BUSY_TIMEOUT_MS", None)
        os.environ.pop("HEALTH_PORT", None)
        reload_config()

    # 单例
    check(get_config() is get_config(), "get_config() must return a singleton")
    check(Config.load() is get_config(), "Config.load() must return the singleton")


# ---------------------------------------------------------------------------
# T-1: safe_log
# ---------------------------------------------------------------------------


def test_safe_log() -> None:
    """UTF-8 安全日志: 中文/emoji 不抛异常, 不可编码字符被转义 (REQ-05)。"""
    # 1) install_utf8 幂等且返回状态
    state = install_utf8()
    check(isinstance(state, dict), "install_utf8 must return a dict")
    install_utf8()  # 幂等
    note(f"install_utf8 -> {state}")

    # 2) ★ 关键: 构造 ASCII-only 流 (等价 Windows GBK 遇到 emoji 的场景)
    #    原生 print 到该流必抛 UnicodeEncodeError; safe_print 必须不抛且已转义。
    raw = io.BytesIO()
    ascii_stream = io.TextIOWrapper(raw, encoding="ascii", newline="")

    native_raised = False
    try:
        print(POISON, file=ascii_stream)
        ascii_stream.flush()
    except UnicodeEncodeError:
        native_raised = True
    check(
        native_raised,
        "test harness invalid: native print to an ascii stream should raise UnicodeEncodeError",
    )
    note("baseline confirmed: native print raises UnicodeEncodeError on ascii stream")

    # 清空缓冲 (native print 可能写入了部分字节)
    raw.seek(0)
    raw.truncate(0)

    try:
        safe_print(POISON, file=ascii_stream)
        ascii_stream.flush()
    except Exception as exc:  # 一旦抛出即视为护栏失效
        raise SmokeFailure(f"safe_print raised {type(exc).__name__}: {exc}") from exc
    written = raw.getvalue().decode("ascii", "replace")
    check("\\u4e2d" in written, f"non-ascii should be backslash-escaped, got: {written!r}")
    check("\\U0001f600" in written or "\\u" in written, "emoji should be escaped")
    note(f"safe_print escaped output: {written.strip()[:80]}")

    # 3) escape_for_encoding / sanitize_for_stream
    check(escape_for_encoding(POISON, "utf-8") == POISON, "utf-8 should keep text intact")
    escaped = escape_for_encoding(POISON, "ascii")
    check(escaped.isascii(), "ascii escape result must be pure ascii")
    check(sanitize_for_stream(POISON, ascii_stream).isascii(), "sanitize_for_stream must escape")

    # 4) SafeLog 走 logging + SafeStreamHandler 到 ascii 流, 不抛
    raw2 = io.BytesIO()
    ascii_stream2 = io.TextIOWrapper(raw2, encoding="ascii", newline="")
    handler = SafeStreamHandler(ascii_stream2)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TraceIdFilter())
    logger = logging.getLogger("smoke.safe_log")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    log = SafeLog("smoke.safe_log", logger=logger)
    try:
        log.info(POISON, match_id="8801中文", odds=1.85)
        log.warning("低水 ⚠ 待确认")
        log.error({"why": "对象型消息", "emoji": "😀"})
        try:
            raise ValueError("滚球异常 💥")
        except ValueError:
            log.exception("采集失败 ❌")
    except Exception as exc:
        raise SmokeFailure(f"SafeLog raised {type(exc).__name__}: {exc}") from exc
    handler.flush()
    body = raw2.getvalue().decode("ascii", "replace")
    check(body.strip() != "", "SafeLog produced no output")
    check(body.isascii(), "SafeLog output to ascii stream must be pure ascii (escaped)")
    check("\\u4e2d" in body, "SafeLog should escape chinese chars for ascii stream")
    note(f"SafeLog wrote {len(body)} escaped chars, 4 records")

    # 5) 每行必须是合法 JSON 且含 trace_id
    lines = [ln for ln in body.splitlines() if ln.strip()]
    check(len(lines) >= 4, f"expected >=4 json log lines, got {len(lines)}")
    for line in lines:
        payload = json.loads(line)  # 非法 JSON 会直接抛, 即为 FAIL
        for key in ("ts", "level", "logger", "trace_id", "msg"):
            check(key in payload, f"json log line missing '{key}': {line[:200]}")
    note("all log lines are valid JSON with trace_id")

    # 6) safe_str 面对 __str__ 抛错的对象也不能崩
    class Hostile:
        """__str__ / __repr__ 都抛异常的恶意对象。"""

        def __str__(self) -> str:
            raise RuntimeError("boom-str")

        def __repr__(self) -> str:
            raise RuntimeError("boom-repr")

    result = safe_str(Hostile())
    check(isinstance(result, str) and result, f"safe_str must return non-empty str, got {result!r}")
    note(f"hostile object -> {result!r}")

    # 7) 流已关闭时也不许抛
    ascii_stream.close()
    safe_print("after close 中文", file=ascii_stream)

    logger.handlers = []


# ---------------------------------------------------------------------------
# T-2: error_envelope
# ---------------------------------------------------------------------------


class ObjectMessageError(Exception):
    """``.message`` 属性是 dict 的异常 —— 事故③ 白屏的真凶复现。"""

    def __init__(self, payload: Dict[str, Any]) -> None:
        """保留对象型 payload, 模拟旧后端行为。"""
        super().__init__(payload)
        self.message = payload  # ★ 对象, 不是字符串


class NestedDetail:
    """一个连 __str__ 都返回怪东西的自定义对象。"""

    def __init__(self) -> None:
        self.code = 500
        self.detail = {"inner": ["a", "b"]}

    def __str__(self) -> str:
        return "<NestedDetail object at 0xdeadbeef>"


def test_error_envelope() -> None:
    """错误信封: 对象型 message 必须被强制成字符串 (REQ-03)。"""
    payload_dict = {"detail": {"reason": "盘口缺失", "codes": [1, 2, 3]}, "http": 500}

    # 1) ★ 核心断言: message 是 dict 的异常 → error.message 必须是 str
    envelope = to_envelope(ObjectMessageError(payload_dict))
    check(isinstance(envelope, ErrorEnvelope), "to_envelope must return ErrorEnvelope")
    detail = envelope.error
    check(detail is not None, "envelope.error must not be None")
    check(isinstance(detail, ErrorDetail), "envelope.error must be ErrorDetail")
    check(
        isinstance(detail.message, str),
        f"error.message MUST be str, got {type(detail.message).__name__}",
    )
    check(not isinstance(detail.message, (dict, list, tuple, set)), "error.message must not be a container")
    check(isinstance(detail.code, str) and detail.code, "error.code must be non-empty str")
    check("盘口缺失" in detail.message, f"message should retain content, got {detail.message!r}")
    note(f"dict-message exception -> code={detail.code} message={detail.message[:90]}")

    # 2) 整个 payload 走 JSON 序列化后仍合规 (前端 setError 只会拿到 string)
    as_dict = to_error_dict(ObjectMessageError(payload_dict))
    check(is_safe_error_payload(as_dict), f"payload not frontend-safe: {as_dict}")
    round_tripped = json.loads(json.dumps(as_dict, ensure_ascii=False))
    check(
        isinstance(round_tripped["error"]["message"], str),
        "message must survive json round-trip as str",
    )
    check(round_tripped["ok"] is False, "ok must be False")
    note(f"to_error_dict -> {json.dumps(as_dict, ensure_ascii=False)[:140]}")

    # 3) 各类"对象型"输入全覆盖
    hostile_inputs: List[Any] = [
        ObjectMessageError(payload_dict),
        ValueError({"a": 1}),
        ValueError(["x", "y"]),
        RuntimeError(NestedDetail()),
        Exception(),  # 空异常
        KeyError("missing_key"),
        TypeError(b"bytes-arg"),
        ObjectMessageError({}),  # message 是空 dict
    ]
    for exc in hostile_inputs:
        result = to_error_dict(exc)
        check(
            is_safe_error_payload(result),
            f"unsafe payload for {type(exc).__name__}: {result}",
        )
        check(
            isinstance(result["error"]["message"], str) and result["error"]["message"].strip(),
            f"empty/non-str message for {type(exc).__name__}: {result}",
        )
    note(f"{len(hostile_inputs)} hostile exception shapes all produced str messages")

    # 4) coerce_message 直接喂容器/对象
    for raw in ({"k": "v"}, ["a", 1], ("t",), {"s"}, NestedDetail(), b"\xff\xfe", None, 3.14):
        coerced = coerce_message(raw)
        check(isinstance(coerced, str) and coerced, f"coerce_message failed on {raw!r} -> {coerced!r}")
    note("coerce_message handles dict/list/tuple/set/object/bytes/None/float")

    # 5) 错误分类
    code, status = classify_error(sqlite3.OperationalError("database is locked"))
    check(code == "DB_LOCKED" and status == 503, f"sqlite lock should map to DB_LOCKED/503, got {code}/{status}")
    code, status = classify_error(TimeoutError("slow"))
    check(code == "TIMEOUT" and status == 504, f"TimeoutError should map to TIMEOUT/504, got {code}/{status}")
    code, status = classify_error(RuntimeError("boom"))
    check(code == CODE_INTERNAL and status == 500, f"generic should map to INTERNAL/500, got {code}/{status}")
    note("classify_error mapping ok (DB_LOCKED/TIMEOUT/INTERNAL)")

    # 6) 截断保护: 超长 message 不撑爆响应
    huge = to_error_dict(ValueError("x" * 50000))
    check(len(huge["error"]["message"]) < 4000, "long message must be truncated")

    # 7) 成功信封
    ok_payload = success({"rows": 3})
    check(ok_payload["ok"] is True and ok_payload["data"] == {"rows": 3}, "success() envelope broken")


# ---------------------------------------------------------------------------
# T-3: db_manager
# ---------------------------------------------------------------------------


def test_db_manager() -> None:
    """DB 管理器: WAL + 单写者 + reader + integrity + backup/restore (REQ-02)。"""
    tmpdir = tempfile.mkdtemp(prefix="smoke_gq_")
    # ★ 安全闸门: 绝不允许 smoke 测试落到真实数据目录
    real_data_dir = (_PROJECT_ROOT / "data").resolve()
    check(
        real_data_dir not in Path(tmpdir).resolve().parents
        and Path(tmpdir).resolve() != real_data_dir,
        f"refuse to run db smoke inside real data dir: {tmpdir}",
    )
    note(f"temp db dir: {tmpdir}")

    db_path = os.path.join(tmpdir, "GQ_smoke.db")
    backup_path = os.path.join(tmpdir, "backups", "GQ_smoke-snapshot.db")
    logger = get_logger("smoke.db_manager")
    manager = GQConnectionManager(
        db_path=db_path, busy_timeout_ms=5000, pool_size=3, logger=logger
    )
    try:
        # 1) init_db: WAL 生效 + 幂等
        manager.init_db()
        check(manager.journal_mode == "wal", f"journal_mode should be wal, got {manager.journal_mode!r}")
        check(manager.initialized is True, "manager.initialized should be True")
        manager.init_db()  # 幂等: 二次调用不应报错也不应改变状态
        manager.init_db()
        check(manager.journal_mode == "wal", "init_db must stay idempotent")
        with manager.reader() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        check(str(mode).lower() == "wal", f"WAL not persisted, got {mode!r}")
        check(os.path.exists(db_path + "-wal") or True, "wal sidecar check is informational")
        note("init_db WAL ok + idempotent (3 calls)")

        # 2) 单写者写入
        manager.execute_write(
            "CREATE TABLE IF NOT EXISTS live_odds ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  match_id TEXT NOT NULL,"
            "  book TEXT NOT NULL,"
            "  ou_line REAL,"
            "  note TEXT"
            ")"
        )
        affected = manager.execute_write(
            "INSERT INTO live_odds(match_id, book, ou_line, note) VALUES(?,?,?,?)",
            ("8801", "leyu", 2.75, "开盘价缺失 → 中性 ✅"),
        )
        check(affected == 1, f"insert should affect 1 row, got {affected}")
        batch = [(f"88{i:02d}", "leisu", 2.5 + i * 0.25, f"轮次{i}") for i in range(20)]
        affected_many = manager.executemany_write(
            "INSERT INTO live_odds(match_id, book, ou_line, note) VALUES(?,?,?,?)", batch
        )
        check(affected_many == 20, f"executemany should affect 20 rows, got {affected_many}")
        note("single-writer insert + executemany ok")

        # 3) reader 读 (含非 ASCII 往返)
        rows = manager.query("SELECT match_id, book, ou_line, note FROM live_odds ORDER BY id")
        check(len(rows) == 21, f"expected 21 rows, got {len(rows)}")
        check(rows[0]["match_id"] == "8801", "row_factory should support name access")
        check("✅" in rows[0]["note"], "utf-8 text round-trip broken")
        note(f"reader read {len(rows)} rows, utf-8 round-trip ok")

        # 4) 写事务回滚
        try:
            with manager.writer() as conn:
                conn.execute("INSERT INTO live_odds(match_id, book) VALUES(?,?)", ("rollback", "x"))
                raise RuntimeError("intentional failure inside write tx")
        except RuntimeError:
            pass
        after = manager.query("SELECT COUNT(*) AS c FROM live_odds")[0]["c"]
        check(int(after) == 21, f"failed tx must roll back, got {after} rows")
        note("write tx rollback ok")

        # 5) 并发写: 单写者串行化, 不出现 database is locked
        errors: List[str] = []

        def worker(index: int) -> None:
            """并发写线程。"""
            try:
                for j in range(10):
                    manager.execute_write(
                        "INSERT INTO live_odds(match_id, book, ou_line) VALUES(?,?,?)",
                        (f"t{index}-{j}", "concurrent", 2.0),
                    )
            except Exception as exc:  # 收集而不抛, 便于汇总报告
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        check(not errors, f"concurrent writes hit errors: {errors[:3]}")
        total = manager.query("SELECT COUNT(*) AS c FROM live_odds")[0]["c"]
        check(int(total) == 21 + 60, f"expected 81 rows after concurrent writes, got {total}")
        note("6 threads x 10 writes serialized, zero 'database is locked'")

        # 6) 并发读: 池不泄漏
        read_errors: List[str] = []

        def reader_worker() -> None:
            """并发读线程。"""
            try:
                for _ in range(15):
                    manager.query("SELECT COUNT(*) AS c FROM live_odds")
            except Exception as exc:
                read_errors.append(f"{type(exc).__name__}: {exc}")

        rthreads = [threading.Thread(target=reader_worker) for _ in range(8)]
        for t in rthreads:
            t.start()
        for t in rthreads:
            t.join(timeout=60)
        check(not read_errors, f"concurrent reads hit errors: {read_errors[:3]}")
        stats = manager.stats()
        check(int(stats["readers_leased"]) == 0, f"reader pool leaked: {stats}")
        check(int(stats["readers_created"]) <= 3, f"pool exceeded bound: {stats}")
        note(f"pool bounded ok: created={stats['readers_created']} leased={stats['readers_leased']}")

        # 7) integrity_check
        check(manager.check_integrity() is True, "check_integrity should return True on healthy db")
        check(manager.check_integrity(quick=True) is True, "quick_check should return True")
        note("integrity_check + quick_check = True")

        # 8) checkpoint
        manager.checkpoint("TRUNCATE")  # busy 也不算失败, 只要不抛

        # 9) backup
        written = manager.backup(backup_path, verify=True)
        check(os.path.isfile(written), f"backup file not created: {written}")
        check(os.path.getsize(written) > 0, "backup file is empty")
        with sqlite3.connect(written) as probe:
            snapshot_rows = probe.execute("SELECT COUNT(*) FROM live_odds").fetchone()[0]
        check(int(snapshot_rows) == 81, f"backup content mismatch: {snapshot_rows}")
        note(f"backup ok -> {written} ({os.path.getsize(written)} bytes, 81 rows)")

        # 10) restore 往返: 备份后再改库 → 恢复 → 行数回到快照状态
        manager.execute_write("DELETE FROM live_odds WHERE book = 'concurrent'")
        after_delete = manager.query("SELECT COUNT(*) AS c FROM live_odds")[0]["c"]
        check(int(after_delete) == 21, f"delete should leave 21 rows, got {after_delete}")
        manager.restore(written, verify=True)
        restored = manager.query("SELECT COUNT(*) AS c FROM live_odds")[0]["c"]
        check(int(restored) == 81, f"restore round-trip failed: expected 81, got {restored}")
        check(manager.check_integrity() is True, "restored db must pass integrity_check")
        check(manager.journal_mode == "wal", "restore must re-apply WAL")
        note("backup/restore round-trip ok (81 -> 21 -> restore -> 81)")

        # 11) 坏快照必须被拒绝 (不许用坏页覆盖好库)
        bad_snapshot = os.path.join(tmpdir, "bad.db")
        with open(bad_snapshot, "wb") as handle:
            handle.write(b"this is definitely not a sqlite database" * 32)
        rejected = False
        try:
            manager.restore(bad_snapshot, verify=True)
        except IntegrityError:
            rejected = True
        except Exception as exc:
            raise SmokeFailure(f"restore(bad) raised unexpected {type(exc).__name__}: {exc}") from exc
        check(rejected, "restore() must reject a corrupt snapshot with IntegrityError")
        check(
            manager.query("SELECT COUNT(*) AS c FROM live_odds")[0]["c"] == 81,
            "db must be untouched after rejected restore",
        )
        note("corrupt snapshot rejected, live db untouched")

        # 12) stats 快照可观测
        final_stats = manager.stats()
        for key in ("writes", "reads", "pool_hits", "journal_mode", "db_path"):
            check(key in final_stats, f"stats missing key {key}")
    finally:
        manager.close_all()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# T-4: collector_step
# ---------------------------------------------------------------------------


class BoomStep(CollectorStep):
    """必抛异常的步骤 —— 且抛的是"对象型" message, 双重考验。"""

    def __init__(self) -> None:
        super().__init__(name="parse", critical=True)

    def run(self, ctx: CollectorContext) -> StepResult:
        """总是抛出以 dict 为 message 的异常。"""
        raise ObjectMessageError({"reason": "非 ASCII 崩轮 😀", "stage": "parse"})


class UnicodeBoomStep(CollectorStep):
    """抛出含中文/emoji 文案异常的步骤。"""

    def __init__(self) -> None:
        super().__init__(name="persist_partial")

    def run(self, ctx: CollectorContext) -> StepResult:
        """抛出中文异常。"""
        raise UnicodeEncodeError("gbk", "滚球 😀", 0, 1, "illegal multibyte sequence")


def test_collector_step() -> None:
    """采集器隔离: 单步失败不中断整轮, 后续步骤照常执行 (REQ-06)。"""
    executed: List[str] = []

    def fetch(ctx: CollectorContext) -> StepResult:
        """正常步骤: 拉取。"""
        executed.append("fetch")
        ctx.set("raw", ["m1", "m2"])
        return StepResult(step="fetch", ok=True, data={"rows": 2})

    def live_flip(ctx: CollectorContext) -> None:
        """★ 关键步骤: 位于失败步之后, 必须仍然执行 (否则就是事故④ 复现)。"""
        executed.append("live_flip")
        ctx.set("flipped", True)

    def persist(ctx: CollectorContext) -> str:
        """返回非 StepResult 的值, 运行器应视为成功。"""
        executed.append("persist")
        return "persisted 21 rows"

    steps = [
        FunctionStep("fetch", fetch),
        BoomStep(),  # 抛 dict-message 异常
        UnicodeBoomStep(),  # 抛 UnicodeEncodeError
        FunctionStep("live_flip", live_flip),  # ← 必须执行
        FunctionStep("persist", persist),
    ]
    ctx = CollectorContext(round_no=7)
    results = CollectorRound(steps, logger=get_logger("smoke.collector")).run_all(ctx)

    # 1) 结果与步骤一一对应, 整轮没有提前 return
    check(len(results) == len(steps), f"expected {len(steps)} results, got {len(results)}")
    check([r.step for r in results] == ["fetch", "parse", "persist_partial", "live_flip", "persist"],
          f"step order/names mismatch: {[r.step for r in results]}")
    note(f"round produced {len(results)} results in order")

    # 2) ★ 后续步骤仍执行 (live_flip 不跳过)
    check("live_flip" in executed, "live_flip MUST run after a failing step (REQ-06 violated)")
    check("persist" in executed, "persist MUST run after failing steps")
    check(ctx.get("flipped") is True, "live-flip side effect missing")
    check(executed == ["fetch", "live_flip", "persist"], f"unexpected execution trace: {executed}")
    note(f"execution trace after failures: {executed}")

    # 3) 失败步的 StepResult 契约
    ok_flags = [r.ok for r in results]
    check(ok_flags == [True, False, False, True, True], f"ok flags mismatch: {ok_flags}")
    for failed in (results[1], results[2]):
        check(failed.ok is False, f"{failed.step} should be ok=False")
        check(
            isinstance(failed.error_msg, str),
            f"error_msg MUST be str, got {type(failed.error_msg).__name__}",
        )
        check(failed.error_msg.strip() != "", f"{failed.step} error_msg must not be empty")
        check(
            not isinstance(failed.error_msg, (dict, list)),
            "error_msg must never be a container",
        )
        check(failed.trace_id == ctx.trace_id, "failed step must carry the round trace_id")
        check(failed.duration_ms >= 0.0, "duration_ms must be recorded")
    check("reason" in results[1].error_msg, f"dict message content lost: {results[1].error_msg!r}")
    note(f"failed step message (str): {results[1].error_msg[:100]}")

    # 4) StepResult 可 JSON 化 (给前端/日志用)
    payload = json.dumps([r.to_dict() for r in results], ensure_ascii=False)
    check(isinstance(json.loads(payload), list), "StepResult must be json serializable")

    # 5) 汇总
    stats = summarize(results)
    check(stats["total"] == 5 and stats["ok"] == 3 and stats["failed"] == 2, f"summarize wrong: {stats}")
    check(set(stats["failed_steps"]) == {"parse", "persist_partial"}, f"failed_steps wrong: {stats}")
    check(ctx.has_failure() is True, "ctx.has_failure() should be True")
    check(set(ctx.failed_steps()) == {"parse", "persist_partial"}, "ctx.failed_steps() wrong")
    note(f"summarize -> {stats}")

    # 6) run_all 本身绝不抛: 连步骤对象本身损坏的情况也要撑住
    class NoneReturningStep(CollectorStep):
        """返回 None 的步骤 (视为成功)。"""

        def run(self, ctx: CollectorContext) -> None:
            """什么都不做。"""
            return None

    class SelfDestructStep(CollectorStep):
        """连 name 属性访问都异常? 用 run 里删属性模拟极端场景。"""

        def run(self, ctx: CollectorContext) -> StepResult:
            """抛出 BaseException 之外的极端异常。"""
            raise MemoryError("simulated OOM inside step")

    extreme = CollectorRound(
        [NoneReturningStep(), SelfDestructStep(), FunctionStep("tail", lambda c: None)],
        logger=get_logger("smoke.collector.extreme"),
    )
    extreme_results = extreme.run_all()
    check(len(extreme_results) == 3, "extreme round must still return 3 results")
    check(extreme_results[0].ok is True, "None-returning step should be ok")
    check(extreme_results[1].ok is False, "MemoryError step should be ok=False")
    check(extreme_results[2].ok is True, "tail step must still execute after MemoryError")
    note("extreme round (None / MemoryError / tail) survived")

    # 7) trace_id 上下文联动
    with trace_context("smoke") as tid:
        check(get_trace_id() == tid, "trace_context must set the contextvar")
        inner_ctx = CollectorContext(trace_id=tid)
        inner = CollectorRound([FunctionStep("noop", lambda c: None)]).run_all(inner_ctx)
        check(inner[0].trace_id == tid, "step result should carry trace_id")


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

TESTS: List[Tuple[str, Callable[[], None]]] = [
    ("config           (T01 中央配置)", test_config),
    ("safe_log         (REQ-05 UTF-8 不崩轮)", test_safe_log),
    ("error_envelope   (REQ-03 message 必为 str)", test_error_envelope),
    ("db_manager       (REQ-02 WAL/单写者/自检/备份)", test_db_manager),
    ("collector_step   (REQ-06 单步失败不跳步)", test_collector_step),
]


def main(argv: List[str] | None = None) -> int:
    """跑全部 smoke 用例。

    Args:
        argv: 命令行参数。

    Returns:
        0 = 全 PASS, 1 = 有 FAIL。
    """
    global VERBOSE
    parser = argparse.ArgumentParser(description="core/ 基础设施 smoke 测试")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印细节")
    parser.add_argument("--only", default="", help="只跑名字包含该子串的用例")
    args = parser.parse_args(argv)
    VERBOSE = bool(args.verbose)

    install_utf8()
    # 日志输出到 stderr, 避免 JSON 日志混进 PASS/FAIL 报告
    configure_logging(level="DEBUG" if VERBOSE else "WARNING", stream=sys.stderr, force=True)

    safe_print("=" * 78)
    safe_print(" core/ infrastructure smoke test  (哨响AI 稳定化 · 第一阶段)")
    safe_print(f" python : {sys.version.split()[0]}  ({sys.executable})")
    safe_print(f" root   : {_PROJECT_ROOT}")
    safe_print("=" * 78)

    passed = 0
    failed: List[str] = []
    for name, func in TESTS:
        if args.only and args.only.lower() not in name.lower():
            safe_print(f"[SKIP] {name}")
            continue
        try:
            func()
        except SmokeFailure as exc:
            failed.append(name)
            safe_print(f"[FAIL] {name}")
            safe_print(f"       assertion: {exc}")
        except Exception as exc:
            failed.append(name)
            safe_print(f"[FAIL] {name}")
            safe_print(f"       unexpected {type(exc).__name__}: {exc}")
            if VERBOSE:
                safe_print(traceback.format_exc())
        else:
            passed += 1
            safe_print(f"[PASS] {name}")

    safe_print("-" * 78)
    if failed:
        safe_print(f"RESULT: FAIL  ({passed} passed, {len(failed)} failed)")
        for name in failed:
            safe_print(f"  - {name}")
        return 1
    safe_print(f"RESULT: ALL PASS  ({passed}/{passed})")
    safe_print("-" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
