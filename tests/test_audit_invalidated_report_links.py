"""T24 解析与匹配逻辑的纯函数单测（零生产 I/O，使用临时目录）。"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from audit_invalidated_report_links import (
    build_report,
    classify_reference_type,
    detect_access,
    load_invalidated_reports,
    search_references,
    severity_of,
    stem_of,
)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_stem_of():
    assert stem_of("p0_10_retrain_status.json") == "p0_10_retrain_status"
    assert stem_of("prediction_calibration_report.md") == "prediction_calibration_report"
    assert stem_of("foo.csv") == "foo"
    assert stem_of("noext") == "noext"


def test_classify_reference_type():
    assert classify_reference_type(os.path.join(ROOT, "pipeline/x.py")) == "code_backend"
    assert classify_reference_type(os.path.join(ROOT, "frontend/src/y.tsx")) == "code_frontend"
    assert classify_reference_type(os.path.join(ROOT, "docs/z.md")) == "doc"
    assert classify_reference_type(os.path.join(ROOT, "config/a.json")) == "config"
    assert classify_reference_type(os.path.join(ROOT, "misc/b.txt")) == "other"
    # tests/ 下引用归为 test 类型（夹具字符串，非生产加载）
    assert classify_reference_type(os.path.join(ROOT, "tests/test_x.py")) == "test"


def test_severity_of():
    assert severity_of("code_backend") == "high"
    assert severity_of("code_frontend") == "medium"
    assert severity_of("doc") == "low"
    assert severity_of("config") == "low"
    assert severity_of("other") == "low"


def test_detect_access():
    assert detect_access('open("reports/x.json", "w")') == "write"
    assert detect_access("json.dump(d, f)") == "write"
    assert detect_access("json.load(open('reports/x.json'))") == "read"
    assert detect_access("data = read_text('reports/x.md')") == "read"
    assert detect_access('print("reports/x.json 完成")') == "unknown"


def test_load_invalidated_reports(tmp_path):
    audit = {
        "entries": [
            {"name": "a.json", "status": "INVALIDATED_BY_RETRAIN"},
            {"name": "b.md", "status": "INVALIDATED_BY_RETRAIN"},
            {"name": "c.json", "status": "OK"},
            {"name": "d.json", "status": "ORPHANED"},
        ]
    }
    p = tmp_path / "stale.json"
    p.write_text(json.dumps(audit), encoding="utf-8")
    names, n = load_invalidated_reports(str(p))
    assert set(names) == {"a.json", "b.md"}
    assert n == 2


def test_load_invalidated_reports_missing():
    names, n = load_invalidated_reports("nonexistent_path_xyz.json")
    assert names == []
    assert n == 0


def test_search_references_full_and_stem(tmp_path):
    # 构造：后端代码 full 命中、前端 stem 命中（省略扩展）、doc 命中、噪声不匹配
    _write(os.path.join(tmp_path, "scripts", "loader.py"),
           'data = json.load(open("reports/p0_10_retrain_status.json"))\n')
    _write(os.path.join(tmp_path, "frontend", "src", "panel.tsx"),
           'const url = "/reports/p0_10_retrain_status";  // 省略扩展\n')
    _write(os.path.join(tmp_path, "docs", "note.md"),
           'see [report](reports/cs_odds_model_20260831.md) for detail\n')
    _write(os.path.join(tmp_path, "docs", "noise.md"),
           'p0_10_retrain_statusXjson 不是真实引用\n')  # stem 后跟 X，不命中

    names = ["p0_10_retrain_status.json", "cs_odds_model_20260831.md"]
    # 直接传 tmp_path 作为扫描根；classify_reference_type 依赖扩展名/JS 家族判定，
    # 与本测试构造的 .py/.tsx/.md 分类一致（不依赖 ROOT 前缀）。
    refs = search_references(str(tmp_path), names)
    # 期望 3 条命中（loader.py full / panel.tsx stem / note.md full）；noise 不命中
    reports_hit = {r["report"] for r in refs}
    files_hit = {r["file"] for r in refs}
    assert len(refs) == 3, refs
    assert "p0_10_retrain_status.json" in reports_hit
    assert "cs_odds_model_20260831.md" in reports_hit
    assert any(r["match_kind"] == "stem" for r in refs)
    assert "docs/noise.md" not in files_hit


def test_search_references_excludes_reports_dir(tmp_path):
    # 在 reports/ 下放同名文件，应被排除，不命中
    _write(os.path.join(tmp_path, "reports", "self.json"),
           'p0_10_retrain_status.json')
    _write(os.path.join(tmp_path, "scripts", "real.py"),
           'open("reports/p0_10_retrain_status.json")')
    refs = search_references(str(tmp_path), ["p0_10_retrain_status.json"])
    files = {r["file"] for r in refs}
    # reports/ 子目录被排除：self.json 不应出现在任何命中文件里
    assert not any("reports" in f and "self.json" in f for f in files)
    # 仅 scripts/real.py 命中
    assert len(refs) == 1
    assert any(f.endswith("scripts/real.py") or f.endswith("scripts\\real.py")
               for f in files)


def test_search_references_skip_live_companion(tmp_path):
    # 仅 .md 失效，但代码引用的是同 stem 的 .json（LIVE）→ 不应误伤
    _write(os.path.join(tmp_path, "pipeline", "svc.py"),
           'return open("reports/prediction_calibration_report.json")  # live\n')
    refs = search_references(str(tmp_path), ["prediction_calibration_report.md"])
    assert refs == [], refs  # companion .json 未失效 → 跳过

    # 同 stem 的 .csv 确实失效 → 应命中
    _write(os.path.join(tmp_path, "pipeline", "svc2.py"),
           'path = "reports/paper_track_ou_fusion.csv"\n')
    refs2 = search_references(str(tmp_path), ["paper_track_ou_fusion.csv"])
    assert len(refs2) == 1
    assert refs2[0]["match_kind"] == "full"


def test_search_references_bare_stem_kept(tmp_path):
    # 省略扩展名的族引用（裸 stem）→ 保留
    _write(os.path.join(tmp_path, "scripts", "x.py"),
           'name = "reports/p0_10_retrain_status"  # 省略扩展\n')
    refs = search_references(str(tmp_path), ["p0_10_retrain_status.json"])
    assert len(refs) == 1
    assert refs[0]["match_kind"] == "stem"


def test_build_report_structure():
    names = ["a.json", "b.md"]
    refs = [
        {"report": "a.json", "file": "scripts/x.py", "line_no": 1,
         "line": "open(a.json)", "reference_type": "code_backend",
         "match_kind": "full", "access": "write", "severity": "high"},
        {"report": "b.md", "file": "docs/y.md", "line_no": 3,
         "line": "see b.md", "reference_type": "doc",
         "match_kind": "full", "access": "unknown", "severity": "low"},
    ]
    report = build_report(names, refs, "reports/stale.json")
    s = report["summary"]
    assert s["invalidated_reports"] == 2
    assert s["reports_with_references"] == 2
    assert s["total_references"] == 2
    assert s["by_reference_type"]["code_backend"] == 1
    assert s["by_reference_type"]["doc"] == 1
    assert s["by_severity"]["high"] == 1
    assert s["by_severity"]["low"] == 1
    assert s["by_access"]["write"] == 1
    assert s["by_access"]["unknown"] == 1
    # 排序：high 在前
    assert report["references"][0]["severity"] == "high"
    assert "references" in report and "generated_at" in report
