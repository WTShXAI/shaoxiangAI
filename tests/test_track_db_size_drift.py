"""track_db_size_drift.py 单测（临时目录 + 假 db 文件，绝不 open 任何真实库）。"""
import os
import tempfile

from scripts.track_db_size_drift import (
    collect_snapshot,
    compute_size_delta,
    detect_anomalies,
    snapshot_rows,
)


def _mk_db(dirpath, name, size):
    p = os.path.join(dirpath, name)
    with open(p, "wb") as f:
        f.write(b"\0" * size)
    return p


def test_collect_snapshot_stat_only(tmp_path):
    # data 子目录放一个 events.db，archive 子目录放一个归档库
    data = tmp_path / "data"
    arch = tmp_path / "archive"
    data.mkdir()
    arch.mkdir()
    _mk_db(str(data), "events.db", 1024 * 1024 * 100)   # 100 MB
    _mk_db(str(data), "other.db", 1024 * 1024 * 10)    # 10 MB
    _mk_db(str(arch), "old_dump.db", 1024 * 1024 * 50)  # 50 MB

    dbs, agg = collect_snapshot([str(data), str(arch)])
    names = {d["name"] for d in dbs}
    assert names == {"events.db", "other.db", "old_dump.db"}
    # events 体积应为 100 MB
    assert abs(agg["events_size_mb"] - 100.0) < 0.01
    # archive 总体积应为 50 MB
    assert abs(agg["archive_total_mb"] - 50.0) < 0.01
    # 总体积 160 MB
    assert abs(agg["total_mb"] - 160.0) < 0.01


def test_compute_size_delta_and_threshold(tmp_path):
    cur = [
        {"name": "events.db", "path": "x", "size_bytes": 200 * 1024 * 1024, "size_mb": 200.0, "mtime": 1},
        {"name": "new.db", "path": "y", "size_bytes": 10 * 1024 * 1024, "size_mb": 10.0, "mtime": 1},
    ]
    prev = [
        {"name": "events.db", "path": "x", "size_bytes": 100 * 1024 * 1024, "size_mb": 100.0, "mtime": 1},
        # new.db 无前次记录
    ]
    deltas = compute_size_delta(cur, prev)
    # events 增长 100 MB
    assert abs(deltas["events.db"]["delta_mb"] - 100.0) < 0.01
    # new.db 无前次 → delta None
    assert deltas["new.db"]["delta_mb"] is None


def test_detect_anomalies_events_spike(tmp_path):
    # events 增长 6000 MB → 越过 5000 阈值 → anomaly
    deltas = {"events.db": {"delta_mb": 6000.0, "delta_bytes": 6000 * 1024 * 1024,
                            "previous_mb": 35000.0}}
    agg_now = {"events_size_mb": 41000.0, "archive_total_mb": 100.0, "total_mb": 42000.0}
    agg_prev = {"archive_total_mb": 50.0}
    drift = detect_anomalies(deltas, agg_now, agg_prev,
                             {"events_spike_mb": 5000, "archive_spike_mb": 2000})
    assert drift["events"]["anomaly"] is True
    # archive 增长 50 MB < 2000 → 不异常
    assert drift["archive"]["anomaly"] is False
    assert drift["archive"]["delta_mb"] == 50.0


def test_detect_anomalies_no_spike(tmp_path):
    # events 增长 100 MB → 正常
    deltas = {"events.db": {"delta_mb": 100.0, "delta_bytes": 100 * 1024 * 1024,
                            "previous_mb": 35000.0}}
    agg_now = {"events_size_mb": 35100.0, "archive_total_mb": 50.0, "total_mb": 35200.0}
    agg_prev = {"archive_total_mb": 50.0}
    drift = detect_anomalies(deltas, agg_now, agg_prev,
                             {"events_spike_mb": 5000, "archive_spike_mb": 2000})
    assert drift["events"]["anomaly"] is False
    assert drift["events"]["delta_mb"] == 100.0


def test_archive_anomaly_flag(tmp_path):
    deltas = {}  # 无单库增量事件
    agg_now = {"events_size_mb": 35000.0, "archive_total_mb": 5000.0, "total_mb": 40000.0}
    agg_prev = {"archive_total_mb": 1000.0}
    drift = detect_anomalies(deltas, agg_now, agg_prev,
                             {"events_spike_mb": 5000, "archive_spike_mb": 2000})
    # archive 增长 4000 MB > 2000 → anomaly
    assert drift["archive"]["anomaly"] is True
    assert drift["archive"]["delta_mb"] == 4000.0


def test_snapshot_rows_format(tmp_path):
    dbs = [{"name": "events.db", "path": "x", "size_bytes": 100, "size_mb": 0.001, "mtime": 123.0}]
    rows = snapshot_rows(dbs, "2026-09-26T09:00:00Z")
    assert len(rows) == 1
    assert rows[0][0] == "2026-09-26T09:00:00Z"
    assert rows[0][1] == "events.db"
    assert rows[0][2] == 100
    assert rows[0][3] == 0.001
    assert rows[0][4] == 123.0
