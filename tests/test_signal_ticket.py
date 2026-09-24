"""P-TICKET 信号审批工单 — 不可变追加 + IR-32 守卫 (SYSTEM_BLUEPRINT §2.1)."""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import signal_ticket as st  # noqa: E402


def setup_module():
    st.init()


def test_create_and_append_only():
    before = st.count()
    tid = st.create_ticket(
        match_id="unittest_match", stage="prematch_1h", model_version="v7.4",
        signal_type="pass", decision="PASS", reason="OU无edge不追",
        approver="auto", evidence_links='{"match_id":"unittest_match"}',
    )
    assert tid
    assert st.count() == before + 1
    # 不可变: 触发器禁止 UPDATE/DELETE, 篡改须失败
    con = sqlite3.connect(st.DB)
    import pytest
    with pytest.raises(sqlite3.Error):
        con.execute("UPDATE signal_approval_ticket SET decision='APPROVE' WHERE ticket_id=?", (tid,))
        con.commit()
    with pytest.raises(sqlite3.Error):
        con.execute("DELETE FROM signal_approval_ticket WHERE ticket_id=?", (tid,))
        con.commit()
    con.close()


def test_ir32_banned_field_rejected():
    import pytest
    with pytest.raises(ValueError):
        st.create_ticket(
            match_id="x", stage="opening", model_version="v1", signal_type="cross_book_edge",
            decision="PASS", reason="bad",
        )


def test_list_filter():
    st.create_ticket(match_id="unittest_match2", stage="opening", model_version="v1",
                     signal_type="value_gap", decision="PASS", reason="r")
    rows = st.list_tickets(match_id="unittest_match2")
    assert any(r[1] == "unittest_match2" for r in rows)
