# -*- coding: utf-8 -*-
"""T42 reports/ 产物二次消费风险审计的单元测试。

纯本地临时目录构造，零生产 I/O：不碰 events.db / 不碰 reports/ / 不碰任何进程。
"""
from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import audit_report_secondary_consumption as A  # noqa: E402


def make_fixture(tmp_path):
    """建一个最小仓库：1 个生产者 / 1 个抄值的消费者 / 1 个伪活引用 / 3 份共享开关的产物。"""
    root = str(tmp_path)
    rdir = os.path.join(root, "reports")
    os.makedirs(rdir)
    sdir = os.path.join(root, "scripts")
    os.makedirs(sdir)

    def w(name, text):
        with open(os.path.join(sdir, name), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(text))

    with open(os.path.join(rdir, "a_report.json"), "w", encoding="utf-8") as f:
        json.dump({"task": "a", "retrain_gate": {"suggest": True}}, f)
    with open(os.path.join(rdir, "b_report.json"), "w", encoding="utf-8") as f:
        json.dump({"task": "b", "suggest": True}, f)
    with open(os.path.join(rdir, "c_report.md"), "w", encoding="utf-8") as f:
        f.write('# c\n\n- 结论抄自上游："suggest": true\n')
    with open(os.path.join(rdir, "d_report.md"), "w", encoding="utf-8") as f:
        f.write("# d\n\n- pass=true\n")
    with open(os.path.join(rdir, "e_report.json"), "w", encoding="utf-8") as f:
        json.dump({"task": "e", "pass": True, "note": "2026-09-27"}, f)
    # f：手写快照——没有任何脚本写它，却被 consumer 读（上游变了它永远不变）
    with open(os.path.join(rdir, "f_snapshot.md"), "w", encoding="utf-8") as f:
        f.write("# f\n\n- 手写快照，无生产脚本\n")

    # 生产者：直写 a_report.json
    w("prod1.py", """
        import json, os
        REPORTS = os.path.join(os.getcwd(), "reports")
        OUT_JSON = os.path.join(REPORTS, "a_report.json")
        def run():
            with open(OUT_JSON, "w", encoding="utf-8") as f:
                json.dump({"retrain_gate": {"suggest": True}}, f)
    """)
    # 消费者：读 a 写 b，并把 suggest=true 抄进自己的结论（不重算）
    w("consumer.py", """
        import json, os
        REPORTS = os.path.join(os.getcwd(), "reports")
        def run():
            with open(os.path.join(REPORTS, "f_snapshot.md"), "r", encoding="utf-8") as f:
                snap = f.read()
            with open(os.path.join(REPORTS, "a_report.json"), "r", encoding="utf-8") as f:
                a = json.load(f)
            out = {"suggest": True, "from": "monitor"}
            with open(os.path.join(REPORTS, "b_report.json"), "w", encoding="utf-8") as f:
                json.dump(out, f)
    """)
    # 伪活：读 newer.json 写 older.json（同一脚本），产物落后于输入
    with open(os.path.join(rdir, "newer.json"), "w", encoding="utf-8") as f:
        json.dump({"stamp": 2}, f)
    with open(os.path.join(rdir, "older.json"), "w", encoding="utf-8") as f:
        json.dump({"stamp": 1}, f)
    w("lagmaker.py", """
        import json, os
        REPORTS = os.path.join(os.getcwd(), "reports")
        def run():
            with open(os.path.join(REPORTS, "newer.json"), "r", encoding="utf-8") as f:
                a = json.load(f)
            with open(os.path.join(REPORTS, "older.json"), "w", encoding="utf-8") as f:
                json.dump({"copied": a.get("stamp")}, f)
    """)
    # a(新) 必须晚于 b(旧)，否则 copy 方向不成立
    os.utime(os.path.join(rdir, "a_report.json"), (1_770_000_000, 1_770_000_000))
    os.utime(os.path.join(rdir, "b_report.json"), (1_760_000_000, 1_760_000_000))
    os.utime(os.path.join(rdir, "newer.json"), (1_770_000_000, 1_770_000_000))
    os.utime(os.path.join(rdir, "older.json"), (1_760_000_000, 1_760_000_000))
    return root


@pytest.fixture()
def fx(tmp_path):
    return make_fixture(tmp_path)


# ---------------------------------------------------------------------------
def test_collect_report_index_only_top_level_reports(fx):
    idx = A.collect_report_index(fx)
    names = {i["name"] for i in idx}
    assert "a_report.json" in names
    assert "d_report.md" in names
    assert all(i["ext"] in (".json", ".md") for i in idx)


def test_scan_source_refs_captures_direct_refs_and_self_excludes(fx):
    refs = A.scan_source_refs(fx)
    hits = [(r["src"], r["report"]) for r in refs]
    assert ("scripts/prod1.py", "reports/a_report.json") in hits
    assert ("scripts/consumer.py", "reports/a_report.json") in hits
    assert ("scripts/consumer.py", "reports/b_report.json") in hits
    # 本脚本自身（及测试）对 reports/ 的引用属自指，不计入
    assert not [h for h in hits if "audit_report_secondary_consumption" in h[0]]


def test_scan_source_refs_detects_write_mode_open(fx):
    refs = A.scan_source_refs(fx)
    prod = [r for r in refs if r["src"] == "scripts/prod1.py"][0]
    assert prod["op"] == "WRITE"


def test_classify_op_priorities():
    assert A.classify_op('json.dump(x, f)') == "WRITE"
    assert A.classify_op('open(p + ".json", "w")') == "WRITE"
    assert A.classify_op('open("x.json", "r")') == "READ"
    assert A.classify_op("x = os.path.join(R, 'y.json')", "WRITE") == "WRITE"
    assert A.classify_op("x = os.path.join(R, 'y.json')", "") == "UNKNOWN"


def test_scan_source_refs_handles_fstring_path(tmp_path):
    """`f"{stem}_sample_growth.json"` 这类 f-string 路径不能漏抓。"""
    root = str(tmp_path)
    sdir = os.path.join(root, "scripts")
    os.makedirs(sdir)
    rdir = os.path.join(root, "reports")
    os.makedirs(rdir)
    with open(os.path.join(rdir, "stem_sample_growth.json"), "w", encoding="utf-8") as f:
        json.dump({"n": 1}, f)
    with open(os.path.join(sdir, "stem_job.py"), "w", encoding="utf-8") as f:
        f.write('import os\nREPORTS = os.path.join(os.getcwd(), "reports")\n'
                'def run():\n    '
                'p = os.path.join(out_dir, f"{stem}_sample_growth.json")\n'
                '    with open(p, "w", encoding="utf-8") as fh:\n'
                '        json.dump({"n": 1}, fh)\n')
    refs = A.scan_source_refs(root)
    assert any(r["report"].endswith("_sample_growth.json") for r in refs)


def test_build_propagation_strong_vs_weak_flags(fx):
    idx = A.collect_report_index(fx)
    prop, _ = A.build_propagation(idx, fx)
    flags = prop["flags"]
    # suggest=true 在 a/b/c 三份产物里共享 → 强控制键，必须进 flags
    assert any(k.startswith("FLAG:suggest=true") and len(v) >= 3 for k, v in flags.items())
    # pass=true 只有 2 份（<6）→ 不进传播集（属弱判定噪声）
    assert not [k for k, v in flags.items() if k.startswith("FLAG:pass") and len(v) < 6]


def test_norm_flag_ignores_monitor_prefix():
    assert A.norm_flag("monitor_retrain_suggest") == ("retrain", "suggest")
    assert A.norm_flag("retrain_suggest") == ("retrain", "suggest")
    assert A.norm_flag("suggest") == ("suggest",)


def test_find_value_copy_detects_and_rejects_inverted_direction(fx):
    idx = A.collect_report_index(fx)
    refs = A.scan_source_refs(fx)
    found = A.find_value_copy(refs, idx, fx)
    pairs = {(c["src"], c["input"], c["output"]) for c in found}
    assert ("scripts/consumer.py", "a_report.json", "b_report.json") in pairs
    # 方向常识：值只能从新流到旧，反向 mtime 必须不成立
    copied = [c for c in found if c["input"] == "newer.json" and c["output"] == "older.json"]
    assert copied == []


def test_find_value_copy_flags_are_honest(fx):
    idx = A.collect_report_index(fx)
    refs = A.scan_source_refs(fx)
    found = A.find_value_copy(refs, idx, fx)
    hit = [c for c in found
           if (c["src"], c["output"]) == ("scripts/consumer.py", "b_report.json")]
    assert hit and hit[0]["copied_flags"]
    assert "suggest=true" in hit[0]["copied_flags"][0]


def test_find_pseudo_live_catches_lagging_output(fx):
    idx = {i["name"]: i for i in A.collect_report_index(fx)}
    refs = A.scan_source_refs(fx)
    pl = A.find_pseudo_live(refs, idx, fx)
    assert any(p["output"] == ["older.json"] and p["input"] in ("newer.json",)
               for p in pl)


def test_classify_reports_marks_snapshot_and_copy(fx):
    idx = A.collect_report_index(fx)
    refs = A.scan_source_refs(fx)
    prop, _ = A.build_propagation(idx, fx)
    pl = A.find_pseudo_live(refs, idx, fx)
    vc = A.find_value_copy(refs, idx, fx)
    reports = {r["name"]: r for r in A.classify_reports(idx, refs, prop, pl, vc)}
    # f_snapshot：无生产者却被消费 → 手写快照，上游变了它不变
    assert reports["f_snapshot.md"]["severity"] == "HIGH"
    assert "NO_PRODUCER" in reports["f_snapshot.md"]["flags"][0]
    assert reports["f_snapshot.md"]["producers"] == []
    # a：有生产者也被消费；b 是被"抄值"造出来的产物
    assert reports["a_report.json"]["producers"]
    assert "VALUE_COPY" in " ".join(reports["b_report.json"]["flags"])
    assert reports["b_report.json"]["severity"] == "HIGH"


def test_build_report_and_render_roundtrip(fx):
    idx = A.collect_report_index(fx)
    refs = A.scan_source_refs(fx)
    prop, _ = A.build_propagation(idx, fx)
    pl = A.find_pseudo_live(refs, idx, fx)
    vc = A.find_value_copy(refs, idx, fx)
    reports = A.classify_reports(idx, refs, prop, pl, vc)
    rep = A.build_report(idx, refs, prop, pl, reports, vc)
    md = A.render_md(rep)
    assert "伪活引用" in md and "值级传播" in md
    assert rep["summary"]["value_copy"] >= 1
    assert rep["summary"]["pseudo_live"] >= 1
    json.dumps(rep, ensure_ascii=False)     # 可序列化


def test_main_writes_only_into_reports_dir(fx, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["t42probe", "--root", fx])
    A.main()                                    # reload 会把 ROOT 复位，故走 --root
    out_json = os.path.join(fx, "reports", "report_secondary_consumption_audit.json")
    assert os.path.exists(out_json)             # 产物必须落在 tmp 的 reports/ 下
    assert os.path.exists(out_json.replace(".json", ".md"))
    with open(out_json, encoding="utf-8") as f:
        data = json.load(f)
    assert data["task"].startswith("T42")
    assert data["scope"]["n_reports"] < 20      # 确实扫的是 tmp 那份迷你 reports/
    # 红线：本任务只做只读盘点，不得出现任何写操作关键词
    assert data["redlines"] and all("只读" in r for r in data["redlines"])
