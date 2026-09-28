"""track_sample_growth.py 单测（临时库，绝不触碰 verification.db / events.db）。"""
import os
import sqlite3
import tempfile

from scripts.track_sample_growth import (
    build_summary,
    compute_trend,
    fetch_ledger,
    open_readonly,
)


def _make_tmp_db(rows):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE verification_ledger (
            model_source TEXT, match_date TEXT, devig_method TEXT, is_credible INTEGER
        )
        """
    )
    con.executemany(
        "INSERT INTO verification_ledger VALUES (?,?,?,?)", rows
    )
    con.commit()
    con.close()
    return path


def test_readonly_cannot_write():
    path = _make_tmp_db([("KNN", "2026-09-20", "power", 1)])
    con = open_readonly(path)
    try:
        # query_only 下写操作必须失败
        try:
            con.execute("CREATE TABLE should_fail (x INT)")
            raise AssertionError("query_only 未阻止写操作")
        except sqlite3.OperationalError:
            pass
    finally:
        con.close()
    os.remove(path)


def test_compute_trend_cumulative():
    rows = [
        ("KNN", "2026-09-20", "power", 1),
        ("KNN", "2026-09-20", "power", 1),
        ("KNN", "2026-09-21", "power", 1),
        ("market_baseline", "2026-09-20", "power", 1),
    ]
    path = _make_tmp_db(rows)
    con = open_readonly(path)
    try:
        ledger = fetch_ledger(con)
    finally:
        con.close()

    assert len(ledger) == 4
    trend, sources, cum = compute_trend(ledger)
    assert set(sources) == {"KNN", "market_baseline"}
    # KNN 累计应为 3，且最后一日 cum==3
    assert cum["KNN"] == 3
    assert trend["KNN"][-1]["cum_n"] == 3
    # market_baseline 累计应为 1
    assert cum["market_baseline"] == 1
    # 每日新增正确：KNN 首日 2
    assert trend["KNN"][0]["daily_n"] == 2
    os.remove(path)


def test_is_credible_filter():
    rows = [
        ("KNN", "2026-09-20", "power", 1),
        ("KNN", "2026-09-20", "power", 0),  # 不可信，须被排除
    ]
    path = _make_tmp_db(rows)
    con = open_readonly(path)
    try:
        ledger = fetch_ledger(con)
    finally:
        con.close()
    assert len(ledger) == 1
    os.remove(path)


def test_build_summary_g1_gap():
    rows = [("KNN", "2026-09-20", "power", 1)] * 100
    path = _make_tmp_db(rows)
    con = open_readonly(path)
    try:
        ledger = fetch_ledger(con)
    finally:
        con.close()
    trend, sources, cum = compute_trend(ledger)
    summary = build_summary(trend, cum, len(ledger))
    assert summary["total_credible_rows"] == 100
    knn = summary["by_source"]["KNN"]
    assert knn["cum_n"] == 100
    assert knn["g1_reached"] is False
    assert knn["gap_to_g1"] == 2500 - 100
    os.remove(path)
