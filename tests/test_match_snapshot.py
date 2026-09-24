"""P-SNAPSHOT 反偷看快照 — 冻结不可变 + 只读源 (SYSTEM_BLUEPRINT §2.4)."""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import match_snapshot as ms  # noqa: E402


def _a_real_match_key():
    con = sqlite3.connect(f"file:{ms.EVENTS}?mode=ro", uri=True)
    k = con.execute("SELECT match_key FROM matches WHERE score_home IS NOT NULL LIMIT 1").fetchone()
    con.close()
    return k[0] if k else None


def setup_module():
    # 重置表保证隔离( DROP 是 DDL, 不被行级 DELETE 触发器拦截)
    con = sqlite3.connect(ms.DB)
    con.execute("DROP TABLE IF EXISTS match_snapshot")
    con.close()
    ms.init()


def test_freeze_immutable():
    mk = _a_real_match_key()
    if not mk:
        import pytest
        pytest.skip("events.db 无可用 match_key")
    sid1 = ms.freeze(mk)
    assert sid1, "首次冻结应成功"
    snap = ms.get(mk)
    assert snap, "应能读回快照"
    # 不可变: 二次冻结返回 None(不覆盖)
    sid2 = ms.freeze(mk)
    assert sid2 is None, "已冻结场不应被覆盖"
    # 触发器拒绝 DELETE
    import pytest
    con = sqlite3.connect(ms.DB)
    with pytest.raises(sqlite3.Error):
        con.execute("DELETE FROM match_snapshot WHERE match_key=?", (mk,))
        con.commit()
    con.close()


def test_hash_present():
    mk = _a_real_match_key()
    if not mk:
        import pytest
        pytest.skip("events.db 无可用 match_key")
    ms.freeze(mk)
    snap = ms.get(mk)
    # fields_json + hash 列存在且非空
    assert snap[-1], "hash 应非空"
    assert snap[-2], "fields_json 应非空"
