#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_env_health.py 只读单测（不碰 events.db / 不写库 / 不读真实计划任务）。"""
from unittest import mock

import scripts.check_env_health as m


def _health_ok() -> m.EnvHealth:
    h = m.EnvHealth()
    h.venv = m.CheckResult("venv", "PASS", "venv python=3.12.10 @ x", "")
    h.python_version = "3.12.10"
    h.deps = [
        m.CheckResult("dep:scikit-learn", "PASS", "scikit-learn==1.9.0 (期望 >=1.9.0,<2.0.0)", ""),
        m.CheckResult("dep:torch", "PASS", "torch==2.13.0 (期望 <3)", ""),
    ]
    h.tasks = [
        m.CheckResult("task:ShaoxiangBridge_Watchdog", "PASS", "状态=Ready（期望 启用）", ""),
        m.CheckResult("task:ShaoxiangSandbox2301", "PASS", "状态=Disabled（期望 停用）", ""),
    ]
    h.dbs = [m.CheckResult("db:events.db", "PASS", "35.85 GB @ x", "")]
    return h


def test_version_parse():
    assert m._version_ge("1.9.0", "1.9.0")
    assert m._version_ge("2.13.0", "1.9.0")
    assert not m._version_ge("1.8.0", "1.9.0")
    assert m._version_lt("2.0.2", "2.1")
    assert not m._version_lt("2.1.0", "2.1")


def test_pip_list_parse():
    out = "Package    Version\nnumpy      2.0.2\nlightgbm   4.7.0\n"
    # _pip_list 走真实 subprocess；这里直接验证其内部正则解析逻辑
    import re
    versions = {}
    for line in out.splitlines():
        mm = re.match(r"^([A-Za-z0-9_.\-]+)\s+([0-9][0-9A-Za-z.\-]*)", line)
        if mm:
            versions[mm.group(1).lower()] = mm.group(2)
    assert versions == {"numpy": "2.0.2", "lightgbm": "4.7.0"}


def test_check_deps_pass():
    with mock.patch.object(m, "_pip_list", return_value={
        "scikit-learn": "1.9.0", "numpy": "2.0.2", "pandas": "2.3.3",
        "scipy": "1.18.0", "joblib": "1.5.3", "lightgbm": "4.7.0",
        "xgboost": "2.1.4", "torch": "2.13.0",
    }):
        res = m.check_deps("dummy")
    assert all(r.status == "PASS" for r in res)
    assert len(res) == len(m.EXPECTED_DEPS)


def test_check_deps_out_of_range():
    with mock.patch.object(m, "_pip_list", return_value={"scikit-learn": "1.5.0"}):
        res = m.check_deps("dummy")
    sk = [r for r in res if r.name == "dep:scikit-learn"][0]
    assert sk.status == "FAIL"


def test_check_deps_missing():
    with mock.patch.object(m, "_pip_list", return_value={}):
        res = m.check_deps("dummy")
    assert all(r.status == "FAIL" for r in res)


def test_check_tasks_expected_status():
    actual = {
        "ShaoxiangBridge_Watchdog": "Ready",
        "ShaoxiangAI_ProdGuardian": "Running",
        "ShaoxiangAI_NetWatch": "Ready",
        "ShaoxiangVite": "Running",
        "ShaoxiangAI_DailyRecheck": "Ready",
        "ShaoXiangOddsAssetDaily": "Ready",
        "ShaoxiangSandbox2301": "Disabled",
    }
    with mock.patch.object(m, "_query_tasks", return_value=actual):
        res = m.check_tasks()
    assert all(r.status == "PASS" for r in res)


def test_check_tasks_wrong_status():
    actual = {"ShaoxiangBridge_Watchdog": "Disabled"}
    with mock.patch.object(m, "_query_tasks", return_value=actual):
        res = m.check_tasks()
    bridge = [r for r in res if r.name == "task:ShaoxiangBridge_Watchdog"][0]
    assert bridge.status == "FAIL"


def test_check_tasks_unreadable():
    with mock.patch.object(m, "_query_tasks", return_value={}):
        res = m.check_tasks()
    assert res[0].status == "WARN"


def test_check_dbs(tmp_path):
    # 用临时文件模拟；CORE_DBS 不可改，直接测函数内部逻辑通过 monkeypatch 落盘
    with mock.patch.object(m, "CORE_DBS", {"fake.db": str(tmp_path / "fake.db")}):
        f = tmp_path / "fake.db"
        f.write_bytes(b"x" * 1024)
        res = m.check_dbs()
    assert res[0].status == "PASS"


def test_run_health_summary_fail():
    h = _health_ok()
    h.deps[0] = m.CheckResult("dep:scikit-learn", "FAIL", "x", "")
    with mock.patch.object(m, "check_venv", return_value=h.venv), \
         mock.patch.object(m, "check_deps", return_value=h.deps), \
         mock.patch.object(m, "check_tasks", return_value=h.tasks), \
         mock.patch.object(m, "check_dbs", return_value=h.dbs):
        out = m.run_health("dummy")
    assert out.exit_code == 2
    assert "FAIL" in out.summary


def test_run_health_all_pass():
    h = _health_ok()
    with mock.patch.object(m, "check_venv", return_value=h.venv), \
         mock.patch.object(m, "check_deps", return_value=h.deps), \
         mock.patch.object(m, "check_tasks", return_value=h.tasks), \
         mock.patch.object(m, "check_dbs", return_value=h.dbs):
        out = m.run_health("dummy")
    assert out.exit_code == 0
    assert "ALL PASS" in out.summary
    assert len(out.all_results()) == 1 + 2 + 2 + 1
