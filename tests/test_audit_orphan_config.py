"""audit_orphan_config 单测（临时目录 + 临时代码根，不碰 events.db / 源 config）。

验证: days_old / is_backup / build_needles / count_references
     / detect_internal_marks / classify_config / suggest_action / build_report 聚合 + 落盘。
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import audit_orphan_config as a  # noqa: E402


def test_days_old_basic():
    now = 1_000_000.0
    assert abs(a.days_old(now - 86400.0, now) - 1.0) < 1e-9


def test_is_backup_patterns():
    assert a.is_backup("lead_result_prior.json.bak_20260830_dirty") is True
    assert a.is_backup("foo.yaml.bak_2026") is True
    assert a.is_backup("bar.json._old") is True
    assert a.is_backup("settings.yaml") is False
    assert a.is_backup("model_weights.json") is False


def test_build_needles_includes_config_prefix():
    n = a.build_needles("api_budget.yaml")
    assert "api_budget.yaml" in n
    assert "config/api_budget.yaml" in n
    assert "api_budget" in n
    # 短主干仅完整文件名
    n2 = a.build_needles("ou.json")
    assert n2 == ["ou.json", "config/ou.json"]


def _write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_count_references_found_and_self_excluded():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config")
        src = os.path.join(d, "pipeline")
        os.makedirs(cfg)
        os.makedirs(src)
        target = os.path.join(cfg, "my_settings.yaml")
        _write(target, "noop: true")
        # 引用者：config/<名>
        _write(os.path.join(src, "runner.py"),
               "load_config('config/my_settings.yaml')")
        # 引用者：主干大小写不敏感
        _write(os.path.join(src, "util.py"),
               "MY_SETTINGS is referenced")
        # 无关
        _write(os.path.join(cfg, "other.json"), "x")
        _write(os.path.join(src, "x.py"), "nothing")

        count, hits = a.count_references("my_settings.yaml", target, d)
        assert count == 2
        assert len(hits) == 2


def test_count_references_zero():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config")
        src = os.path.join(d, "pipeline")
        os.makedirs(cfg)
        os.makedirs(src)
        target = os.path.join(cfg, "orphan_cfg.json")
        _write(target, "{}")
        _write(os.path.join(src, "x.py"), "no mention")
        count, _ = a.count_references("orphan_cfg.json", target, d)
        assert count == 0


def test_detect_internal_marks():
    # 禁用标记
    m1 = a.detect_internal_marks("enabled: false\nfoo: 1")
    assert m1["disabled"] is True
    # 已废止纪律引用
    m2 = a.detect_internal_marks("参考 IR-09 与 IR 32 的规则已废止")
    assert m2["disabled"] is False
    assert "IR-09" in m2["voided_rules"] or "IR-9" in m2["voided_rules"]
    assert any(v in m2["voided_rules"] for v in ("IR-32", "IR 32", "IR32"))
    # 干净
    m3 = a.detect_internal_marks("normal config without markers")
    assert m3["disabled"] is False
    assert m3["voided_rules"] == []


def test_classify_config_priority():
    # BACKUP 优先
    assert a.classify_config(5, True, {"disabled": False, "voided_rules": []}) == "BACKUP"
    # VOIDED_REF 次之
    assert a.classify_config(5, False, {"disabled": False, "voided_rules": ["IR-09"]}) == "VOIDED_REF"
    # DISABLED_MARK
    assert a.classify_config(5, False, {"disabled": True, "voided_rules": []}) == "DISABLED_MARK"
    # ORPHAN
    assert a.classify_config(0, False, {"disabled": False, "voided_rules": []}) == "ORPHAN"
    # ACTIVE
    assert a.classify_config(3, False, {"disabled": False, "voided_rules": []}) == "ACTIVE"


def test_suggest_action():
    assert "备份" in a.suggest_action("BACKUP", [])
    assert "废止" in a.suggest_action("VOIDED_REF", ["IR-09"])
    assert "弃用" in a.suggest_action("DISABLED_MARK", [])
    assert "孤儿" in a.suggest_action("ORPHAN", [])
    assert a.suggest_action("ACTIVE", []) == ""


def test_build_report_aggregates_and_writes():
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "config")
        src = os.path.join(d, "pipeline")
        os.makedirs(cfg)
        os.makedirs(src)
        # ACTIVE: 被引用
        _write(os.path.join(cfg, "active.json"), "{}")
        _write(os.path.join(src, "r.py"), "config/active.json used")
        # ORPHAN: 零引用
        _write(os.path.join(cfg, "orphan.json"), "{}")
        # BACKUP: 备份后缀
        _write(os.path.join(cfg, "old.json.bak_2026"), "{}")
        # VOIDED_REF: 残留废止纪律引用
        _write(os.path.join(cfg, "legacy.yaml"), "rule: IR-11\nx: 1")

        oj = os.path.join(d, "out.json")
        om = os.path.join(d, "out.md")
        report, _, _ = a.build_report(cfg, d, voided_dir=None, now=time.time(),
                                      output_json=oj, output_md=om)
        byname = {e["name"]: e for e in report["entries"]}
        assert byname["active.json"]["status"] == "ACTIVE"
        assert byname["orphan.json"]["status"] == "ORPHAN"
        assert byname["old.json.bak_2026"]["status"] == "BACKUP"
        assert byname["legacy.yaml"]["status"] == "VOIDED_REF"
        assert "IR-11" in byname["legacy.yaml"]["voided_rules"]
        assert report["summary"]["total_configs"] == 4
        assert report["summary"]["orphan_count"] == 1
        assert report["summary"]["backup_count"] == 1
        assert report["summary"]["voided_ref_count"] == 1
        assert os.path.exists(oj) and os.path.exists(om)
        with open(oj, encoding="utf-8") as fh:
            reloaded = json.load(fh)
        assert reloaded["summary"]["disabled_count"] == 0
