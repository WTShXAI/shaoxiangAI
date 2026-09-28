"""T30 tests — audit_crossbook_live_surface 纯逻辑单测。

全部使用 tmp_path 构造源码片段，零生产 I/O：不读 events.db、不写任何生产库、
不改守卫、不改生产代码。断言聚焦「符号 → kind / 守卫覆盖 / 悬空 / 禁用」判定。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import audit_crossbook_live_surface as A  # noqa: E402


# --------------------------------------------------------------------------
# 1. import 路判定（复算现守卫语义）
# --------------------------------------------------------------------------
def test_guard_import_matches_import_form():
    assert A.guard_import_matches("from pipeline.cross_book_edge import edge")
    assert A.guard_import_matches("import cross_book_edge")


def test_guard_import_misses_dict_and_def():
    """现守卫只认 import：dict 字段与函数定义均漏网（T06/T15 缺口核心）。"""
    assert not A.guard_import_matches('"multibook_consensus": _lookup(1, 2)')
    assert not A.guard_import_matches("def _lookup_multibook_consensus(home, away):")


def test_guard_import_is_line_anchored():
    """守卫正则锚行首，行内字符串提及不误报。"""
    assert not A.guard_import_matches('    alert = "cross_book_alert"')


# --------------------------------------------------------------------------
# 2. kind 分类
# --------------------------------------------------------------------------
@pytest.mark.parametrize("line,symbol,expected", [
    ("<MultiBookConsensus card={card} />", "MultiBookConsensus", "JSX_MOUNT"),
    ("export interface MultibookConsensus {", "MultibookConsensus", "TYPE_DECL"),
    ("export function MultiBookConsensus(", "MultiBookConsensus", "COMPONENT"),
    ("def _lookup_multibook_consensus(home: str, away: str):",
     "_lookup_multibook_consensus", "FUNC_DEF"),
    ("from pipeline.prematch_similarity import multibook_consensus",
     "multibook_consensus", "IMPORT"),
    ('"multibook_consensus": _lookup_multibook_consensus(a, b),',
     "multibook_consensus", "DICT_FIELD"),
    ("const mb = card.multibook_consensus", "multibook_consensus", "FIELD_ACCESS"),
    ("signal_type=\"cross_book_edge\"", "cross_book_edge", "STRING"),
])
def test_classify_line(line, symbol, expected):
    assert A.classify_line(line, symbol, False) == expected


def test_classify_line_treats_comment_as_comment():
    assert A.classify_line("# 跨庄共识已禁 (IR-32)", "跨庄共识", False) == "COMMENT"


def test_classify_line_value_literal_quoted_key_is_dict_field():
    """键名带引号+冒号属 DICT_FIELD（守卫 import 路抓不到的典型面）。"""
    assert A.classify_line("'softline': vl.get(\"softline\"),", "softline", False) == "DICT_FIELD"


# --------------------------------------------------------------------------
# 3. 扫描器与排除目录
# --------------------------------------------------------------------------
def test_iter_text_files_skips_excluded_dir(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "src" / "a.tsx").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules" / "b.tsx").write_text("y", encoding="utf-8")
    files = [p for p, _ in A.iter_text_files(str(tmp_path), A.FE_EXTS)]
    assert [os.path.basename(p) for p in files] == ["a.tsx"]


def test_scan_path_reports_line_and_guard_flag(tmp_path):
    p = tmp_path / "b.py"
    p.write_text('from x import multibook_consensus\n"multibook_consensus": None\n',
                 encoding="utf-8")
    hits = A.scan_path(str(p), p.read_text(encoding="utf-8"), "multibook_consensus")
    assert [h["line"] for h in hits] == [1, 2]
    # 诚实口径：守卫的窄 import 正则不认 `from x import <banned-as-name>` 这一形态
    assert hits[0]["kind"] == "IMPORT"
    assert hits[0]["guard_caught"] is False
    assert hits[1]["guard_caught"] is False
    assert hits[1]["kind"] == "DICT_FIELD"


def test_scan_path_marks_file_outside_guard_scanlist(tmp_path):
    p = tmp_path / "unlisted.py"
    p.write_text("import cross_book_edge\n", encoding="utf-8")
    hits = A.scan_path(str(p), p.read_text(encoding="utf-8"), "cross_book_edge")
    assert hits[0]["in_guard_scan"] is False


# --------------------------------------------------------------------------
# 4. 悬空引用与禁用定义
# --------------------------------------------------------------------------
def test_dangling_symbol_detected_when_no_definition(tmp_path):
    (tmp_path / "bridge.py").write_text("start_cruise(cross_book=_get_cross_book_signal)\n",
                                        encoding="utf-8")
    all_text = (tmp_path / "bridge.py").read_text(encoding="utf-8")
    defs = A.find_definitions([str(tmp_path)], "_get_cross_book_signal")
    assert defs == []
    assert "_get_cross_book_signal" in all_text


def test_func_is_disabled_on_return_none_body():
    src = 'def _lookup_multibook_consensus(home: str, away: str):\n    """[已禁用 2026-09-19 IR-32]"""\n    return None\n'
    disabled, snippet = A.func_is_disabled(src, "_lookup_multibook_consensus")
    assert disabled is True
    assert snippet


def test_func_not_disabled_on_real_body():
    src = "def f(a):\n    return a * 2\n"
    assert A.func_is_disabled(src, "f")[0] is False


# --------------------------------------------------------------------------
# 4b. 悬空判定的反误报护栏
# --------------------------------------------------------------------------
def test_is_call_or_kwarg_ref_kwarg_value_only():
    """`cross_book=_get_cross_book_signal` 属 kwarg 引用面（会 NameError）。"""
    assert A.is_call_or_kwarg_ref("start_cruise(cross_book=_get_cross_book_signal)",
                                  "_get_cross_book_signal")


def test_is_call_or_kwarg_ref_param_decl_not_a_call():
    """参数声明 `cross_book: bool = False` 不是调用面（避免把贯通残骸误判为悬空）。"""
    assert not A.is_call_or_kwarg_ref("cross_book: bool = False", "cross_book")


def test_param_declared_true_only_for_def_signature():
    code = "def f(a, cross_book: bool = False):\n    return cross_book\n"
    assert A.param_declared(code, "cross_book") is True
    assert A.param_declared("x = _get_cross_book_signal\n", "_get_cross_book_signal") is False


# --------------------------------------------------------------------------
# 5. 汇总与渲染（纯聚合）
# --------------------------------------------------------------------------
def test_build_summary_aggregates_kinds_and_files():
    hits = [
        {"file": "a.py", "line": 1, "symbol": "multibook_consensus", "kind": "DICT_FIELD",
         "risk": "critical", "guard_caught": False, "in_guard_scan": False},
        {"file": "a.py", "line": 9, "symbol": "multibook_consensus", "kind": "COMMENT",
         "risk": "critical", "guard_caught": False, "in_guard_scan": False},
        {"file": "b.py", "line": 3, "symbol": "cross_book_edge", "kind": "IMPORT",
         "risk": "critical", "guard_caught": True, "in_guard_scan": True},
    ]
    s = A.build_summary(hits, [], [], {"guard_caught": 1, "guard_missed": 2,
                                       "guard_scan_files_hit": 1}, 7)
    assert s["total_hits"] == 3
    assert s["code_hits"] == 2  # 注释不计入代码面
    assert s["by_kind"]["DICT_FIELD"] == 1
    assert s["by_file"]["a.py"] == {"total": 2, "risk": "critical"}


def test_render_markdown_contains_key_sections():
    s = A.build_summary([], [], [], {"guard_caught": 0, "guard_missed": 0,
                                     "guard_scan_files_hit": 0}, 3)
    md = A.render_markdown(s, [])
    for token in ("# IR-32 跨庄共识活跃面只读盘点（T30）", "## 1. 总览", "## 6. 结论（诚实口径）"):
        assert token in md


# --------------------------------------------------------------------------
# 6. IR-32 纪律断言（守卫不得被本脚本削弱）
# --------------------------------------------------------------------------
def test_symbol_table_is_non_empty_and_has_critical_family():
    names = {s[0] for s in A.SYMBOLS}
    assert "multibook_consensus" in names
    assert "MultiBookConsensus" in names
    assert any(s[1] == "crossbook_core" for s in A.SYMBOLS)


def test_audit_never_writes_databases(monkeypatch, tmp_path):
    """脚本对外只暴露报告落盘；run_scan 必须零写入（tmp 校验不写库）。"""
    monkeypatch.setattr(A, "read_text", lambda p: "")
    (tmp_path / "x.py").write_text("cross_book_edge\n", encoding="utf-8")
    s, hits = A.run_scan(str(tmp_path))
    assert isinstance(s, dict) and isinstance(hits, list)
    assert not any(str(tmp_path) in str(h["file"]) and h["file"].endswith(".db")
                   for h in hits)
