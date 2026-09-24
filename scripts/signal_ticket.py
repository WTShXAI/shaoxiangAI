r"""signal_ticket.py — 信号审批工单 (仅分析信号 / PASS 决策, 非下注)

隔离库 data/signal_tickets.db (与 events.db 解耦, 零主库耦合)。
不可变追加: 仅暴露 INSERT, 不提供 UPDATE/DELETE。
纪律(DISCIPLINE.md):
  - §9 禁跨庄共识(IR-32): 工单不含任何 cross_book/multibook/投注占比 字段。
  - LLM 仅生成草稿(approver='auto'); 执行/建仓须人工+二次确认(不在本模块内)。
  - 本模块只记录"谁/何时/为何 批准或否决"的审计链。

用法:
  from scripts.signal_ticket import create_ticket, list_tickets
  tid = create_ticket(match_id='...', stage='prematch_1h', model_version='v7.4',
                      signal_type='pass', decision='PASS', reason='OU无edge, 不追',
                      approver='auto', evidence_links='{"match_id":"...","model_ver":"v7.4"}')
"""
import os
import sqlite3
import uuid
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "signal_tickets.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_approval_ticket (
  ticket_id      TEXT PRIMARY KEY,
  match_id       TEXT,
  stage          TEXT,
  model_version  TEXT,
  signal_type    TEXT,
  decision       TEXT,
  reason         TEXT,
  approver       TEXT,
  approved_at    REAL,
  data_snapshot_id TEXT,
  evidence_links TEXT,
  note           TEXT
);
"""


TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS tr_signal_no_update BEFORE UPDATE ON signal_approval_ticket
  BEGIN SELECT RAISE(ABORT, 'signal_approval_ticket 不可变: 禁止 UPDATE'); END;
CREATE TRIGGER IF NOT EXISTS tr_signal_no_delete BEFORE DELETE ON signal_approval_ticket
  BEGIN SELECT RAISE(ABORT, 'signal_approval_ticket 不可变: 禁止 DELETE'); END;
"""


def _con():
    return sqlite3.connect(DB)


def init():
    con = _con()
    con.execute(SCHEMA)
    con.executescript(TRIGGERS)
    con.commit()
    con.close()


def create_ticket(match_id, stage, model_version, signal_type, decision, reason,
                  approver="auto", data_snapshot_id="", evidence_links="", note=""):
    """追加一条工单(不可变)。返回 ticket_id。
    approver='auto' 表示 LLM 草稿; 人工批准须传真实账号。
    """
    # IR-32 守卫: 拒绝任何跨庄语义混入
    blob = f"{signal_type}|{decision}|{reason}|{note}|{evidence_links}"
    for banned in ("cross_book", "multibook", "leyu_value", "bet_split"):
        if banned in blob.lower():
            raise ValueError(f"IR-32 禁区字段混入工单: {banned}")
    tid = str(uuid.uuid4())
    con = _con()
    con.execute(
        """INSERT INTO signal_approval_ticket
           (ticket_id, match_id, stage, model_version, signal_type, decision,
            reason, approver, approved_at, data_snapshot_id, evidence_links, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tid, match_id, stage, model_version, signal_type, decision, reason,
         approver, datetime.now(timezone.utc).timestamp(), data_snapshot_id,
         evidence_links, note),
    )
    con.commit()
    con.close()
    return tid


def list_tickets(match_id=None, decision=None):
    con = _con()
    sql = "SELECT ticket_id, match_id, stage, signal_type, decision, approver, approved_at FROM signal_approval_ticket WHERE 1=1"
    args = []
    if match_id:
        sql += " AND match_id=?"; args.append(match_id)
    if decision:
        sql += " AND decision=?"; args.append(decision)
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


def count():
    con = _con()
    n = con.execute("SELECT COUNT(*) FROM signal_approval_ticket").fetchone()[0]
    con.close()
    return n


if __name__ == "__main__":
    init()
    print("signal_tickets DB ready:", DB, "| tickets:", count())
