"""CLI 入口: `python -m verification report` / `ingest` / `export`.

report = 先 ingest (增量累积可信账本) 再渲染报告 (JSON + Markdown + CLI 表格)。
只读 events.db; 写 verification.db (append-only) 与 reports/。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import uuid

from verification.schema import REPO_ROOT
from verification.ledger import VerificationLedger
from verification import report as _report

EVENTS_DB: str = os.path.join(REPO_ROOT, "data", "events.db")


def _events_con() -> sqlite3.Connection:
    if not os.path.exists(EVENTS_DB):
        print(f"[fatal] events.db 不存在: {EVENTS_DB}", file=sys.stderr)
        raise SystemExit(2)
    con = sqlite3.connect(f"file:{EVENTS_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _ledger(args) -> VerificationLedger:
    """按 --db 选择账本库 (默认 verification.db; 口径重建期用 v2 库)。"""
    db = getattr(args, "db", None) or os.path.join(REPO_ROOT, "verification.db")
    led = VerificationLedger(db)
    led.ensure_schema()
    return led


def cmd_ingest(args) -> int:
    led = _ledger(args)
    con = _events_con()
    try:
        n = led.ingest_new(con, uuid.uuid4().hex)
    finally:
        con.close()
    print(f"[ingest] 本轮新增可信账本行: {n}  ({led.db_path})")
    return 0


def cmd_report(args) -> int:
    led = _ledger(args)
    con = _events_con()
    try:
        n = led.ingest_new(con, uuid.uuid4().hex)
    finally:
        con.close()

    bundles = _report._build_bundles(led)
    _report.print_cli(bundles)
    print(f"\n[ingest] 本轮新增可信账本行: {n}")
    jp, mp = _report.write_reports(_report.REPORT_DIR, bundles)
    print(f"→ {jp}")
    print(f"→ {mp}")
    return 0


def cmd_export(args) -> int:
    led = _ledger(args)
    path = led.export_ledger(args.fmt, args.out)
    print(f"→ {path}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="verification", description="哨响AI 盈利验证台")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _with_db(p):
        p.add_argument("--db", default=None, help="账本库路径 (默认 verification.db)")

    p_ing = sub.add_parser("ingest", help="增量累积可信账本 (只读 events.db)")
    _with_db(p_ing)

    p_rep = sub.add_parser("report", help="ingest + 渲染报告")
    p_rep.add_argument("--no-ingest", action="store_true", help="跳过 ingest (快)")
    _with_db(p_rep)

    p_exp = sub.add_parser("export", help="导出账本")
    p_exp.add_argument("--fmt", choices=("json", "csv"), default="csv")
    p_exp.add_argument("--out", required=True)
    _with_db(p_exp)

    args = ap.parse_args(argv)

    if args.cmd == "ingest":
        return cmd_ingest(args)
    if args.cmd == "report":
        return cmd_report_no_ingest(args) if args.no_ingest else cmd_report(args)
    if args.cmd == "export":
        return cmd_export(args)
    return 1


def cmd_report_no_ingest(args) -> int:
    """仅渲染 (不 ingest) — 用于快速查看当前账本判定。"""
    led = _ledger(args)
    bundles = _report._build_bundles(led)
    _report.print_cli(bundles)
    jp, mp = _report.write_reports(_report.REPORT_DIR, bundles)
    print(f"\n→ {jp}")
    print(f"→ {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
