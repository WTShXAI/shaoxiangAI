"""端到端验证 事故⑦ cleaner: dry-run(清单+行数+脏型) → apply(备份+删脏行) → rollback(恢复).

用合成 unified_history.db (含 5 类已知脏行 + 2 干净行), 不触碰任何真实赛果/赔率数据。
"""
import os
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))


def _build_dirty_db(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.execute(
        """CREATE TABLE unified_history (
            match_id TEXT, market TEXT, timestamp TEXT,
            source TEXT, roi REAL, odds REAL, n INTEGER)"""
    )
    rows = [
        ("m1", "ah", "2026-01-01", "LEYU", 10.0, 1.9, 100),   # 干净
        ("m2", "ah", "2026-01-02", "LEISU", 8.0, 2.0, 100),    # 干净
        ("m3", "ou", "2026-01-03", None, 5.0, 1.8, 50),        # bad_source
        ("m4", "cs", "2026-01-04", "LEYU", "abc", 1.7, 30),     # roi_not_numeric
        ("m5", "ah", "2026-01-05", "LEYU", 5_000_000.0, 1.9, 40),  # roi_out_of_range
        ("", "ah", "2026-01-06", "LEISU", 6.0, 2.1, 20),       # missing match_id
        ("m6", "ou", "2026-01-07", "LEYU", 7.0, -1.0, 15),     # nonpositive_odds
    ]
    c.executemany("INSERT INTO unified_history VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()


def _run(*args):
    return subprocess.run([sys.executable, os.path.join(HERE, "clean_unified_history.py"),
                           *args], capture_output=True, text=True)


def main() -> None:
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "unified_history.db")
    _build_dirty_db(db)

    # 1) dry-run: 出清单 + 行数 + 脏型, 不写
    r1 = _run("--db", db)
    print(r1.stdout)
    assert r1.returncode == 0, r1.stderr
    assert "脏行=5" in r1.stdout, f"dry-run 应报 5 脏行, got:\n{r1.stdout}"
    assert "默认不写" in r1.stdout, "dry-run 须标注不写"
    n0 = sqlite3.connect(db).execute("SELECT COUNT(*) FROM unified_history").fetchone()[0]
    assert n0 == 7, f"dry-run 不应删除, got {n0}"

    # 2) apply: 先备份, 再删脏行
    r2 = _run("--db", db, "--apply")
    print(r2.stdout)
    assert r2.returncode == 0, r2.stderr
    assert "已备份" in r2.stdout, "apply 须先备份"
    n1 = sqlite3.connect(db).execute("SELECT COUNT(*) FROM unified_history").fetchone()[0]
    assert n1 == 2, f"apply 后应剩 2 干净行, got {n1}"
    backups = os.listdir(os.path.join(tmp, "backups"))
    assert len(backups) == 1, f"应生成 1 个备份, got {backups}"

    # 3) rollback: 从备份恢复
    r3 = _run("--db", db, "--rollback", os.path.join(tmp, "backups", backups[0]))
    assert r3.returncode == 0, r3.stderr
    n2 = sqlite3.connect(db).execute("SELECT COUNT(*) FROM unified_history").fetchone()[0]
    assert n2 == 7, f"rollback 应恢复 7 行, got {n2}"

    print("PASS clean_unified_history: dry-run(5脏/不写) -> apply(删5/备份) -> rollback(恢复7)")
    print("CLEAN UNIFIED HISTORY DRY-RUN + APPLY + ROLLBACK OK")


if __name__ == "__main__":
    main()
