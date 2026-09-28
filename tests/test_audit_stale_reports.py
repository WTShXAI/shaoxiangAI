"""audit_stale_reports 单测（临时目录 + 临时 monitor json，不碰 events.db / 源报告）。

验证: days_old / is_stale / is_model_eval / is_active_infra / classify_report
     / suggest_regen / load_monitor_status / build_report 聚合 + 分类落盘。
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import audit_stale_reports as a  # noqa: E402


def test_days_old_basic():
    now = 1_000_000.0
    assert abs(a.days_old(now - 86400.0, now) - 1.0) < 1e-9
    assert abs(a.days_old(now - 15 * 86400.0, now) - 15.0) < 1e-9


def test_is_stale_threshold():
    now = 2_000_000.0
    assert a.is_stale(now - 31 * 86400.0, now, threshold_days=30) is True
    assert a.is_stale(now - 29 * 86400.0, now, threshold_days=30) is False
    # 边界: 正好 30d 不算陈旧（严格大于）
    assert a.is_stale(now - 30 * 86400.0, now, threshold_days=30) is False


def test_is_model_eval_and_infra():
    assert a.is_model_eval("candles_walkforward_round1.json") is True
    assert a.is_model_eval("p0_9_model_ev_status.json") is True
    assert a.is_model_eval("ht_anchor_round1.json") is True
    assert a.is_model_eval("audit_orphan_dbs.json") is False
    assert a.is_active_infra("monitor_status.json") is True
    assert a.is_active_infra("verification_report.json") is True
    assert a.is_active_infra("candles_walkforward_round1.json") is False


def test_classify_report_stale_hard():
    now = 3_000_000.0
    age = 40.0  # >30d
    # 模型类过期 → STALE_HARD（优先于 INVALIDATED 判定，因为已过期）
    assert a.classify_report("candles_window_70d.json", age, None, 30, 14) == "STALE_HARD"
    # 普通静态过期 → STALE_HARD
    assert a.classify_report("old_note.md", age, None, 30, 14) == "STALE_HARD"


def test_classify_report_live_and_model_fresh():
    now = 3_000_000.0
    age = 1.0  # 新鲜
    # 活动校准报告 → LIVE
    assert a.classify_report("prediction_calibration_report.json", age, None, 30, 14) == "LIVE"
    # 模型类新鲜且无 monitor 信号 → OK
    assert a.classify_report("candles_window_70d.json", age, None, 30, 14) == "OK"


def test_classify_report_invalidated_by_retrain():
    now = 3_000_000.0
    age = 5.0
    monitor = {"retrain_gate": {"suggest": True},
               "calibration": {"drift": {"drift_alert": False}}}
    assert a.classify_report("candles_walkforward_round1.json", age, monitor, 30, 14) == \
        "INVALIDATED_BY_RETRAIN"


def test_classify_report_drift_impacted():
    now = 3_000_000.0
    age = 5.0
    monitor = {"retrain_gate": {"suggest": False},
               "calibration": {"drift": {"drift_alert": True}}}
    # retrain 优先于 drift；此处 retrain=False → 落到 drift
    assert a.classify_report("candles_walkforward_round1.json", age, monitor, 30, 14) == \
        "DRIFT_IMPACTED"


def test_classify_report_orphaned():
    now = 3_000_000.0
    age = 20.0  # 介于 orphan_window(14) 与 30 之间，非模型/非基础设施 → ORPHANED
    assert a.classify_report("some_static_report.json", age, None, 30, 14) == "ORPHANED"
    # age <= orphan_window → OK（新鲜静态）
    assert a.classify_report("some_static_report.json", 10.0, None, 30, 14) == "OK"


def test_suggest_regen_text():
    assert "重生成" in a.suggest_regen("STALE_HARD", "candles_x.json")
    assert "归档" in a.suggest_regen("STALE_HARD", "old_note.md")
    assert "重训" in a.suggest_regen("INVALIDATED_BY_RETRAIN", "p0_x.json")
    assert "漂移" in a.suggest_regen("DRIFT_IMPACTED", "candles_x.json")
    assert "归档" in a.suggest_regen("ORPHANED", "static.json")
    assert a.suggest_regen("OK", "x.json") == ""


def test_load_monitor_status_missing_and_valid():
    with tempfile.TemporaryDirectory() as d:
        # 缺失 → None
        assert a.load_monitor_status(os.path.join(d, "nope.json")) is None
        # 损坏 → None
        p = os.path.join(d, "bad.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{not valid")
        assert a.load_monitor_status(p) is None
        # 有效
        good = os.path.join(d, "ok.json")
        payload = {"retrain_gate": {"suggest": True},
                   "calibration": {"drift": {"drift_alert": False}}}
        with open(good, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        assert a.load_monitor_status(good) == payload


def test_build_report_aggregates_and_writes():
    with tempfile.TemporaryDirectory() as d:
        rep_dir = os.path.join(d, "reports")
        os.makedirs(rep_dir)
        # 制造几份文件
        now = 5_000_000.0
        # 过期模型类（>30d）
        _touch(os.path.join(rep_dir, "candles_old.json"), now - 40 * 86400.0)
        # 新鲜模型类
        _touch(os.path.join(rep_dir, "candles_new.json"), now - 1 * 86400.0)
        # 新鲜静态（<=orphan_window）
        _touch(os.path.join(rep_dir, "static_fresh.json"), now - 5 * 86400.0)
        # 孤儿静态（>14d, <30d）
        _touch(os.path.join(rep_dir, "static_old.json"), now - 20 * 86400.0)
        # 子目录不应被计入
        os.makedirs(os.path.join(rep_dir, "_model_archives"))

        monitor = os.path.join(d, "monitor_status.json")
        with open(monitor, "w", encoding="utf-8") as fh:
            json.dump({"retrain_gate": {"suggest": False},
                       "calibration": {"drift": {"drift_alert": False}}}, fh)

        oj = os.path.join(d, "out.json")
        om = os.path.join(d, "out.md")
        report, _, _ = a.build_report(rep_dir, monitor, threshold_days=30,
                                      orphan_window=14, now=now,
                                      output_json=oj, output_md=om)

        names = {e["name"] for e in report["entries"]}
        assert names == {"candles_old.json", "candles_new.json",
                         "static_fresh.json", "static_old.json"}
        byname = {e["name"]: e for e in report["entries"]}
        assert byname["candles_old.json"]["status"] == "STALE_HARD"
        assert byname["candles_new.json"]["status"] == "OK"
        assert byname["static_fresh.json"]["status"] == "OK"
        assert byname["static_old.json"]["status"] == "ORPHANED"
        assert report["summary"]["stale_hard_count"] == 1
        # 落盘校验
        assert os.path.exists(oj) and os.path.exists(om)
        with open(oj, encoding="utf-8") as fh:
            reloaded = json.load(fh)
        assert reloaded["summary"]["total_reports"] == 4


def _touch(path, mtime):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("x")
    os.utime(path, (mtime, mtime))
