"""T30-C tests — audit_crossbook_guard_gap 纯逻辑单测。

全部使用 tmp_path 构造源码片段（经 run_gap_scan(root) 走），零生产 I/O：
不读 events.db、不写任何库、不改守卫、不改生产代码。
断言聚焦「现守卫语义复刻 / 扩展守卫语义 / 差集 = 新增 FAIL 面 / 风险与盲区分级」。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import audit_crossbook_guard_gap as G  # noqa: E402


def _write(root, rel, text):
    p = os.path.join(str(root), rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


# --------------------------------------------------------------------------
# 1. 现守卫语义复刻
# --------------------------------------------------------------------------
def test_current_guard_only_hits_files_in_scanlist():
    src = "import cross_book_edge\n"
    assert G.current_guard_hits("core/hidden.py", src) == []
    hit = G.current_guard_hits("bridge_service.py", src)
    assert len(hit) == 1 and hit[0]["rule"] == "IMPORT"


def test_current_guard_ignores_symbol_usage_outside_import():
    src = '"multibook_consensus": _lookup_multibook_consensus(a, b)\n'
    assert G.current_guard_hits("pipeline/predict_export.py", src) == []


def test_line_anchored_import_match():
    assert G.line_anchored_import_match("from x.cross_book_edge import e")
    assert not G.line_anchored_import_match('alert = "cross_book_alert"')


# --------------------------------------------------------------------------
# 2. 扩展守卫语义（T15 §3.4 两路）
# --------------------------------------------------------------------------
def test_word_boundary_is_not_substring():
    """整词边界：multibook_consensus 不误伤 _lookup_multibook_consensus（诚实口径）。"""
    assert not G.word_boundary_hit("def _lookup_multibook_consensus(a):", "multibook_consensus")
    assert G.word_boundary_hit("x = multibook_consensus", "multibook_consensus")


def test_word_boundary_hits_own_symbol():
    assert G.word_boundary_hit("def _lookup_multibook_consensus(a):",
                               "_lookup_multibook_consensus")


def test_extended_guard_catches_dict_field_and_def():
    src = ('def _lookup_multibook_consensus(a):\n    return None\n'
           '\n"multibook_consensus": _lookup_multibook_consensus(1),\n')
    hits = G.extended_guard_hits("bridge_service.py", src)
    kinds = {h["kind"] for h in hits}
    assert "FUNC_DEF" in kinds and "DICT_FIELD" in kinds
    # 同一行同时被 `multibook_consensus` 与 `_lookup_multibook_consensus` 命中
    assert len(hits) == 3
    assert all(h["kind"] != "OTHER" for h in hits)  # OTHER 已被归一为 STRING


def test_extended_guard_catches_frontend_component_and_mount():
    src = ("export interface MultibookConsensus { a: number }\n"
           "export function MultiBookConsensus(props: any) { return null }\n"
           "<MultiBookConsensus card={card} />\n")
    hits = G.extended_guard_hits("frontend/src/x.tsx", src)
    kinds = {h["kind"] for h in hits}
    # `MultiBookConsensus`（规格拼写，大写 B）命中 COMPONENT + JSX_MOUNT；
    # TYPE_DECL 需按真实类型名 `MultibookConsensus`（小写 b，见 EXTRA_SYMBOLS）才命中。
    assert kinds == {"COMPONENT", "JSX_MOUNT"}
    hits2 = G.extended_guard_hits("frontend/src/x.tsx",
                                  "export interface MultibookConsensus { a: number }\n",
                                  G.EXTRA_SYMBOLS)
    assert hits2 and hits2[0]["kind"] == "TYPE_DECL"


def test_extended_guard_keeps_import_rule_inside_symbol_path():
    hits = G.extended_guard_hits("pipeline/a.py", "from p.cross_book_edge import e\n")
    assert hits and hits[0]["rule"] == "IMPORT"


# --------------------------------------------------------------------------
# 3. 分级与去重
# --------------------------------------------------------------------------
def test_risk_level_by_kind_and_family():
    assert G.risk_of({"kind": "DICT_FIELD"}, "crossbook_core") == "critical"
    assert G.risk_of({"kind": "DICT_FIELD"}, "quant_archived") == "high"
    assert G.risk_of({"kind": "STRING"}, "crossbook_core") == "high"
    assert G.risk_of({"kind": "STRING"}, "softline_divergence") == "medium"
    assert G.risk_of({"kind": "COMMENT"}, "crossbook_core") == "low"


def test_dedupe_keeps_first():
    a = {"file": "f.py", "line": 1, "symbol": "s"}
    b = dict(a)
    assert len(G.dedupe([a, b])) == 1


def test_scope_bucket_classification():
    assert G.scope_bucket("pipeline/a.py") == "spec_scope"
    assert G.scope_bucket("frontend/src/a.tsx") == "spec_scope"
    assert G.scope_bucket("core/model.py") == "outside_spec"
    assert G.scope_bucket("agent_cruise.py") == "outside_spec"
    assert G.scope_bucket("data_collector/c.py") == "outside_spec"
    # 测试面单列：审计脚本自身测试与守卫文件属自指命中，不算生产规格盲区
    assert G.scope_bucket("tests/test_x.py") == "test_surface"
    assert G.scope_bucket("frontend/e2e/a.spec.ts") == "test_surface"


def test_aggregate_newly_vs_already_and_noise():
    ext = [
        {"file": "pipeline/a.py", "line": 1, "symbol": "s", "kind": "DICT_FIELD",
         "risk": "critical", "_scope": "spec_scope", "rule": "SYMBOL", "code": ""},
        {"file": "pipeline/a.py", "line": 2, "symbol": "s", "kind": "COMMENT",
         "risk": "low", "_scope": "spec_scope", "rule": "SYMBOL", "code": ""},
        {"file": "core/m.py", "line": 3, "symbol": "s", "kind": "IMPORT",
         "risk": "critical", "_scope": "outside_spec", "rule": "IMPORT", "code": ""},
    ]
    cur = [{"file": "pipeline/a.py", "line": 1, "symbol": "s", "kind": "IMPORT"}]
    s = G.aggregate(ext, cur)
    assert s["extended_newly_caught"] == 2          # 注释 1 + 盲区 1
    assert s["extended_already_caught"] == 1
    assert len(s["runtime_facing_hits"]) == 1        # 只有 DICT_FIELD 真 FAIL
    assert len(s["comment_noise_hits"]) == 1
    assert len(s["outside_spec_hits"]) == 1
    # pipeline/a.py 内 2 处命中中 1 处现守卫已抓，故新增仅 1（COMMENT 那条）
    assert s["by_file"]["pipeline/a.py"]["new"] == 1
    assert s["by_file"]["core/m.py"]["new"] == 1


# --------------------------------------------------------------------------
# 4. 扫描主流程（tmp 根，零生产 I/O）
# --------------------------------------------------------------------------
def test_run_gap_scan_on_synthetic_tree(tmp_path):
    _write(tmp_path, "pipeline/prod.py",
           'from p.cross_book_edge import e\ndef _lookup_multibook_consensus(a):\n'
           '    return None\n"multibook_consensus": None\n')
    _write(tmp_path, "frontend/src/panel.tsx",
           "export function MultiBookConsensus() { return null }\n")
    _write(tmp_path, "core/legacy.py", "compute_value_layer(data)\n")
    summary, hits = G.run_gap_scan(str(tmp_path))
    assert summary["current_guard_caught"] == 1     # 只 1 条 import 面
    assert summary["extended_guard_total"] >= 4
    files = {h["file"] for h in hits}
    assert any("core/legacy.py" in f for f in files)   # 规格盲区被记账
    assert any("frontend/src/panel.tsx" in f for f in files)
    for h in hits:                                    # 不得出现数据库文件
        assert not h["file"].endswith(".db")


def test_run_gap_scan_on_empty_tree(tmp_path):
    summary, hits = G.run_gap_scan(str(tmp_path))
    assert hits == [] and summary["extended_newly_caught"] == 0


# --------------------------------------------------------------------------
# 5. IR-32 纪律断言（不得被本脚本削弱）
# --------------------------------------------------------------------------
def test_spec_symbol_table_still_contains_core_banned_names():
    names = {s for s, _ in G.SPEC_SYMBOLS}
    assert "multibook_consensus" in names
    assert "cross_book_edge" in names
    assert "compute_value_layer" in names
    assert "MultiBookConsensus" in names


def test_render_markdown_has_conclusion_and_honesty_section():
    summary, _ = G.run_gap_scan(os.path.join(str(os.path.dirname(__file__)), ".."))
    if not summary["newly_caught"]:
        summary["newly_caught"] = [{
            "file": "x.py", "line": 1, "symbol": "s", "kind": "DICT_FIELD",
            "rule": "SYMBOL", "risk": "critical", "code": '"s": None',
            "_scope": "spec_scope", "family": "crossbook_core"}]
    md = G.render_markdown(summary)
    for t in ("## 1. 结论速览", "## 3. 受影响文件清单", "## 7. 诚实口径声明"):
        assert t in md


def test_main_writes_json_only_report(tmp_path):
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                          "scripts", "audit_crossbook_guard_gap.py")
    _write(tmp_path, "pipeline/prod.py", '"multibook_consensus": None\n')
    r = subprocess.run(
        [sys.executable, os.path.abspath(script),
         "--root", str(tmp_path), "--out-dir", str(tmp_path / "out")],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    p = tmp_path / "out" / "crossbook_guard_gap_audit.json"
    assert p.exists()                       # --json-only 时不写 md
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    assert "extended_newly_caught" in data


def test_main_writes_markdown_by_default(tmp_path):
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                          "scripts", "audit_crossbook_guard_gap.py")
    _write(tmp_path, "bridge_service.py", "import cross_book_edge\n")
    r = subprocess.run(
        [sys.executable, os.path.abspath(script),
         "--root", str(tmp_path), "--out-dir", str(tmp_path / "out")],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "out" / "crossbook_guard_gap_audit.md").exists()
