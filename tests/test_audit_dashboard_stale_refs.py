"""T29 解析与挂载检测逻辑的纯函数单测（零生产 I/O，使用临时目录）。"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from audit_dashboard_stale_refs import (
    build_report,
    classify_file_type,
    detect_embed_kind,
    load_invalidated_reports,
    risk_of,
    search_dashboard_refs,
    stem_of,
)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_stem_of():
    assert stem_of("p0_10_retrain_status.json") == "p0_10_retrain_status"
    assert stem_of("cs_odds_model_20260831.md") == "cs_odds_model_20260831"
    assert stem_of("foo.csv") == "foo"
    assert stem_of("noext") == "noext"


def test_classify_file_type():
    assert classify_file_type(os.path.join(ROOT, "deliverables/dashboard/x.html")) == "dashboard_html"
    assert classify_file_type(os.path.join(ROOT, "frontend/src/y.tsx")) == "code_frontend"
    assert classify_file_type(os.path.join(ROOT, "frontend/z.md")) == "doc"
    # tests/ 下引用排除，返回 test 类型
    assert classify_file_type(os.path.join(ROOT, "tests/test_x.py")) == "test"


def test_detect_embed_kind():
    # iframe 内嵌渲染
    assert detect_embed_kind('<iframe src="reports/p0_10_retrain_status.json"></iframe>',
                             "p0_10_retrain_status.json") == "iframe_embed"
    # 数据固化进 script（对象字面量赋值）
    assert detect_embed_kind('<script>var d = {edge: "p0_10_retrain_status.json"}</script>',
                             "p0_10_retrain_status.json") == "data_embed"
    # SVG 内嵌
    assert detect_embed_kind('<svg><text>p0_10_retrain_status.json</text></svg>',
                             "p0_10_retrain_status.json") == "data_embed"
    # fetch 运行时加载
    assert detect_embed_kind('fetch("reports/p0_10_retrain_status.json")',
                             "p0_10_retrain_status.json") == "fetch_load"
    # markdown 链接
    assert detect_embed_kind('see [r](reports/cs_odds_model_20260831.md)',
                             "cs_odds_model_20260831.md") == "link_ref"
    # 仅字符串出现
    assert detect_embed_kind('// 历史产物 p0_10_retrain_status.json 已归档',
                             "p0_10_retrain_status.json") == "incidental"


def test_risk_of():
    assert risk_of("iframe_embed") == "high"
    assert risk_of("data_embed") == "high"
    assert risk_of("fetch_load") == "medium"
    assert risk_of("link_ref") == "low"
    assert risk_of("incidental") == "low"


def test_load_invalidated_reports(tmp_path):
    audit = {
        "entries": [
            {"name": "a.json", "status": "INVALIDATED_BY_RETRAIN"},
            {"name": "b.md", "status": "INVALIDATED_BY_RETRAIN"},
            {"name": "c.json", "status": "OK"},
        ]
    }
    p = tmp_path / "stale.json"
    p.write_text(json.dumps(audit), encoding="utf-8")
    names, n = load_invalidated_reports(str(p))
    assert set(names) == {"a.json", "b.md"}
    assert n == 2


def test_load_invalidated_reports_missing():
    names, n = load_invalidated_reports("nonexistent_xyz.json")
    assert names == []
    assert n == 0


def test_search_dashboard_refs_iframe_high(tmp_path):
    dash = tmp_path / "deliverables" / "dashboard"
    dash.mkdir(parents=True)
    _write(os.path.join(str(dash), "evaluation_dashboard.html"),
           '<iframe src="reports/p0_10_retrain_status.json"></iframe>\n')
    fe = tmp_path / "frontend"
    fe.mkdir()
    # 前端 fetch 加载（medium）
    _write(os.path.join(str(fe), "src", "panel.tsx"),
           'const u = fetch("/reports/cs_odds_model_20260831.md");\n')
    # 噪声：同 stem 但 .json 未失效（companion 跳过）
    _write(os.path.join(str(fe), "src", "noise.ts"),
           'open("reports/prediction_calibration_report.json")  // live companion\n')

    names = ["p0_10_retrain_status.json", "cs_odds_model_20260831.md",
             "prediction_calibration_report.md"]
    refs = search_dashboard_refs(str(dash), str(fe), names)
    reports_hit = {r["report"] for r in refs}
    assert "p0_10_retrain_status.json" in reports_hit
    assert "cs_odds_model_20260831.md" in reports_hit
    # companion .json 未失效 → 跳过，prediction_calibration_report.md 不应命中
    assert "prediction_calibration_report.md" not in reports_hit

    by_report = {r["report"]: r for r in refs}
    assert by_report["p0_10_retrain_status.json"]["embed_kind"] == "iframe_embed"
    assert by_report["p0_10_retrain_status.json"]["risk"] == "high"
    assert by_report["cs_odds_model_20260831.md"]["embed_kind"] == "fetch_load"
    assert by_report["cs_odds_model_20260831.md"]["risk"] == "medium"


def test_search_dashboard_refs_excludes_tests(tmp_path):
    dash = tmp_path / "deliverables" / "dashboard"
    dash.mkdir(parents=True)
    _write(os.path.join(str(dash), "d.html"), 'p0_10_retrain_status.json\n')
    # tests 下的夹具字符串应被排除
    _write(os.path.join(str(tmp_path), "tests", "t.py"),
           'NAME = "reports/p0_10_retrain_status.json"\n')
    refs = search_dashboard_refs(str(dash), str(tmp_path), ["p0_10_retrain_status.json"])
    files = [r["file"] for r in refs]
    assert any(f.endswith("dashboard\\d.html") or f.endswith("dashboard/d.html")
               for f in files)
    assert not any("tests" in f for f in files)


def test_build_report_structure():
    names = ["a.json", "b.md"]
    refs = [
        {"report": "a.json", "stem": "a", "file": "deliverables/dashboard/d.html",
         "line_no": 1, "line": "x", "file_type": "dashboard_html",
         "embed_kind": "iframe_embed", "risk": "high", "match_kind": "full"},
        {"report": "b.md", "stem": "b", "file": "frontend/src/p.tsx",
         "line_no": 2, "line": "y", "file_type": "code_frontend",
         "embed_kind": "fetch_load", "risk": "medium", "match_kind": "full"},
    ]
    report = build_report(names, refs, "reports/stale.json")
    s = report["summary"]
    assert s["invalidated_reports"] == 2
    assert s["reports_with_refs"] == 2
    assert s["total_refs"] == 2
    assert s["by_risk"]["high"] == 1
    assert s["by_risk"]["medium"] == 1
    assert s["dashboards_affected"] == 1
    da = report["dashboards_affected"]
    assert "deliverables/dashboard/d.html" in da
    assert da["deliverables/dashboard/d.html"]["top_risk"] == "high"
    # 排序：high 在前
    assert report["references"][0]["risk"] == "high"
    assert "dashboards_affected" in report and "references" in report
