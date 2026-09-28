"""audit_orphan_dbs 单测（临时 data 目录 + 临时 repo，不碰 events.db 生产）。

验证: list_dbs 枚举 / find_references grep 命中 / find_holders 检测 /
      classify 分类 / build_report 聚合 + 归档候选判定。
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import audit_orphan_dbs as a  # noqa: E402


def _mk_repo_with_refs(d, referenced):
    """在 d 下建一个伪代码库：referenced 中的 db 名会被某 .py 引用。"""
    os.makedirs(os.path.join(d, "pipeline"))
    os.makedirs(os.path.join(d, "data"))
    # 引用文件
    for name in referenced:
        with open(os.path.join(d, "pipeline", f"use_{name}.py"), "w", encoding="utf-8") as fh:
            fh.write(f"DB = 'data/{name}'  # 引用 {name}\n")
    # 一个不引用任何 db 的噪音文件
    with open(os.path.join(d, "pipeline", "noise.py"), "w", encoding="utf-8") as fh:
        fh.write("x = 1\n")
    # 受保护 db 不应因名字命中而被误判（classify 用 PROTECTED_DBS 覆盖）
    return d


def _mk_db(d, name):
    data_dir = os.path.join(d, "data")
    os.makedirs(data_dir, exist_ok=True)
    p = os.path.join(data_dir, name)
    # 建一个最小 sqlite 文件以模拟真实 .db
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE t(id INTEGER)")
    con.commit()
    con.close()
    return p


def test_list_dbs_enumerates():
    with tempfile.TemporaryDirectory() as d:
        _mk_db(d, "events.db")
        _mk_db(d, "orphan_a.db")
        dbs = a.list_dbs(os.path.join(d, "data"))
        names = {x["name"] for x in dbs}
        assert names == {"events.db", "orphan_a.db"}
        # 含 size_bytes
        for x in dbs:
            assert x["size_bytes"] > 0


def test_find_references_hits():
    with tempfile.TemporaryDirectory() as d:
        _mk_repo_with_refs(d, ["orphan_a.db"])
        refs = a.find_references(d, {"orphan_a.db", "never_ref.db"})
        assert refs["orphan_a.db"], "应命中 use_orphan_a.db.py"
        assert refs["never_ref.db"] == []
        # sample 行应含文件名
        assert "orphan_a.db" in refs["orphan_a.db"][0]["sample"]


def test_classify_archive_candidate():
    # 无引用、无持有、非保护 → 归档候选
    assert a.classify("orphan_a.db", [], []) == "ARCHIVE_CANDIDATE"
    # 保护集 → 生产保留
    assert a.classify("events.db", [], []) == "PRODUCTION_KEEP"
    # 有引用 → 使用中
    assert a.classify("x.db", [{"file": "p/y.py", "count": 1, "sample": ""}], []) == "IN_USE_REFERENCED"
    # 被持有 → 使用中
    assert a.classify("x.db", [], [{"pid": 1, "name": "python.exe"}]) == "IN_USE_HELD"


def test_build_report_aggregate():
    with tempfile.TemporaryDirectory() as d:
        # repo 引用 orphan_a.db；orphan_b.db 零引用；events.db 受保护
        _mk_repo_with_refs(d, ["orphan_a.db"])
        _mk_db(d, "events.db")
        _mk_db(d, "orphan_a.db")
        _mk_db(d, "orphan_b.db")
        rep = a.build_report(os.path.join(d, "data"), d)
        names = {e["name"] for e in rep["entries"]}
        assert names == {"events.db", "orphan_a.db", "orphan_b.db"}
        byname = {e["name"]: e for e in rep["entries"]}
        assert byname["events.db"]["status"] == "PRODUCTION_KEEP"
        assert byname["orphan_a.db"]["status"] == "IN_USE_REFERENCED"
        assert byname["orphan_b.db"]["status"] == "ARCHIVE_CANDIDATE"
        assert "orphan_b.db" in rep["summary"]["archive_candidates"]
        assert "events.db" in rep["summary"]["production_keep"]


def test_find_holders_safe_no_kill():
    # 打开一个 db 文件并让 psutil 能识别；验证返回持有者且脚本不 kill
    with tempfile.TemporaryDirectory() as d:
        p = _mk_db(d, "held.db")
        con = sqlite3.connect(p)
        holders = a.find_holders(p)
        con.close()
        # 至少我们自己的 python 进程可能持有（取决于平台）；不强制，但不得抛错
        assert isinstance(holders, list)
        # 关键：不发送任何信号
        for h in holders:
            assert "pid" in h and "name" in h
