"""P-AUDIT 前向迁移脚本单测(内存/临时库, 不碰 events.db)。"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import p_audit_migrate as m  # noqa: E402


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    sqlite3.connect(path).executescript(
        "CREATE TABLE odds_changes(id INTEGER, match_key TEXT);"
    ).close()
    return path


def _safe_remove(p):
    # Windows 下 sqlite 关闭后文件锁可能短暂滞留, 忽略清理错误(临时文件无害)
    try:
        os.remove(p)
    except OSError:
        pass


def test_plan_lists_missing_columns():
    p = _tmp_db()
    todo = m.plan(p)
    assert todo == [("raw_json_hash", "TEXT"), ("cleaned_id", "TEXT")], todo
    _safe_remove(p)


def test_apply_adds_columns_and_index():
    p = _tmp_db()
    ok = m.migrate(p, apply=True, allow_production=False)
    assert ok
    con = sqlite3.connect(p)
    cols = [r[1] for r in con.execute("PRAGMA table_info(odds_changes)").fetchall()]
    assert "raw_json_hash" in cols and "cleaned_id" in cols
    idx = con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_oc_raw_json_hash'"
    ).fetchone()
    assert idx, "索引 ix_oc_raw_json_hash 应存在"
    con.close()
    _safe_remove(p)


def test_production_refused_without_flag():
    # 直接验证生产库守卫: 路径含 events.db 且无 --allow-production 须拒绝(不真连生产)
    refused = m.migrate("D:/Architecture/data/events.db", apply=True, allow_production=False)
    assert refused is False, "生产库无 --allow-production 须拒绝"


def test_dry_run_no_modify():
    p = _tmp_db()
    m.migrate(p, apply=False, allow_production=False)
    con = sqlite3.connect(p)
    cols = [r[1] for r in con.execute("PRAGMA table_info(odds_changes)").fetchall()]
    con.close()
    assert "raw_json_hash" not in cols, "dry-run 不得改表"
    _safe_remove(p)
