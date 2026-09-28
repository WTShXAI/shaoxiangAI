"""audit_stale_docs 单测（临时目录 + 临时代码根，不碰 events.db / 源 docs）。

验证: days_old / is_stale / build_needles / count_references
     / classify_doc / suggest_action / build_report 聚合 + 落盘。
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import audit_stale_docs as a  # noqa: E402


def test_days_old_basic():
    now = 1_000_000.0
    assert abs(a.days_old(now - 86400.0, now) - 1.0) < 1e-9
    assert abs(a.days_old(now - 62 * 86400.0, now) - 62.0) < 1e-9


def test_is_stale_threshold():
    now = 2_000_000.0
    assert a.is_stale(now - 61 * 86400.0, now, stale_days=60) is True
    assert a.is_stale(now - 59 * 86400.0, now, stale_days=60) is False
    # 边界: 正好 60d 不算陈旧（严格大于）
    assert a.is_stale(now - 60 * 86400.0, now, stale_days=60) is False


def test_build_needles_short_stem_skipped():
    # 主干 <=3 字符 → 仅完整文件名
    n = a.build_needles("P.md")
    assert n == ["p.md"]
    # 主干较长 → 含主干
    n2 = a.build_needles("methodology_article.md")
    assert "methodology_article.md".lower() in n2
    assert "methodology_article" in n2


def _write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_count_references_found_and_self_excluded():
    with tempfile.TemporaryDirectory() as d:
        docs = os.path.join(d, "docs")
        src = os.path.join(d, "pipeline")
        os.makedirs(docs)
        os.makedirs(src)
        # 被查文档自身（不应计入）
        target = os.path.join(docs, "my_spec.md")
        _write(target, "hello world, no external mention")
        # 引用者：源码中引用完整文件名
        _write(os.path.join(src, "runner.py"),
               "import stuff\n# see my_spec.md for details\nx=1")
        # 引用者：引用主干（大小写不敏感）
        _write(os.path.join(src, "util.py"),
               "MY_SPEC is referenced here")
        # 无关文档
        _write(os.path.join(docs, "other.md"), "unrelated content")
        # 不相关源码
        _write(os.path.join(src, "x.py"), "nothing here")

        count, hits = a.count_references("my_spec.md", target, d)
        assert count == 2
        assert len(hits) == 2


def test_count_references_zero():
    with tempfile.TemporaryDirectory() as d:
        docs = os.path.join(d, "docs")
        src = os.path.join(d, "pipeline")
        os.makedirs(docs)
        os.makedirs(src)
        target = os.path.join(docs, "orphan_doc.md")
        _write(target, "content")
        _write(os.path.join(src, "x.py"), "no mention anywhere")
        count, _ = a.count_references("orphan_doc.md", target, d)
        assert count == 0


def test_classify_doc():
    assert a.classify_doc(70.0, 0, 60) == "STALE_ISLAND"
    assert a.classify_doc(70.0, 3, 60) == "STALE"
    assert a.classify_doc(10.0, 0, 60) == "ISLAND"
    assert a.classify_doc(10.0, 2, 60) == "OK"


def test_suggest_action():
    assert "高优先" in a.suggest_action("STALE_ISLAND")
    assert "评审" in a.suggest_action("STALE")
    assert "归档" in a.suggest_action("ISLAND")
    assert a.suggest_action("OK") == ""


def test_build_report_aggregates_and_writes():
    with tempfile.TemporaryDirectory() as d:
        docs = os.path.join(d, "docs")
        src = os.path.join(d, "pipeline")
        os.makedirs(docs)
        os.makedirs(src)
        now = 1_700_000_000.0  # 2023-11 附近，保证减去 80 天后仍为合法正 epoch
        # 陈旧 + 零引用 → STALE_ISLAND
        _write(os.path.join(docs, "old_orphan.md"), "x")
        _touch_mtime(os.path.join(docs, "old_orphan.md"), now - 80 * 86400.0)
        # 陈旧 + 有引用 → STALE
        _write(os.path.join(docs, "old_refd.md"), "x")
        _touch_mtime(os.path.join(docs, "old_refd.md"), now - 80 * 86400.0)
        _write(os.path.join(src, "r.py"), "old_refd.md mentioned")
        # 新鲜 + 零引用 → ISLAND
        _write(os.path.join(docs, "fresh_orphan.md"), "x")
        _touch_mtime(os.path.join(docs, "fresh_orphan.md"), now - 5 * 86400.0)
        # 新鲜 + 有引用 → OK
        _write(os.path.join(docs, "fresh_refd.md"), "x")
        _touch_mtime(os.path.join(docs, "fresh_refd.md"), now - 5 * 86400.0)
        _write(os.path.join(src, "r2.py"), "fresh_refd.md mentioned")

        oj = os.path.join(d, "out.json")
        om = os.path.join(d, "out.md")
        report, _, _ = a.build_report(docs, d, stale_days=60, now=now,
                                      output_json=oj, output_md=om)
        byname = {e["relpath"]: e for e in report["entries"]}
        assert byname["old_orphan.md"]["status"] == "STALE_ISLAND"
        assert byname["old_refd.md"]["status"] == "STALE"
        assert byname["fresh_orphan.md"]["status"] == "ISLAND"
        assert byname["fresh_refd.md"]["status"] == "OK"
        assert report["summary"]["total_docs"] == 4
        assert os.path.exists(oj) and os.path.exists(om)
        with open(oj, encoding="utf-8") as fh:
            reloaded = json.load(fh)
        assert reloaded["summary"]["stale_count"] == 2
        assert reloaded["summary"]["island_count"] == 2


def _touch_mtime(path, mtime):
    os.utime(path, (mtime, mtime))
