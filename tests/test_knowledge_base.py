"""
KnowledgeBase 单元测试 (pytest 重构版)
========================================
覆盖: knowledge_base 的加载、搜索、分类查询、单例模式
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from knowledge_base import KnowledgeBase, KnowledgeEntry, get_knowledge_base, reset_knowledge_base


class TestKnowledgeBase:
    def test_kb_created(self):
        kb = KnowledgeBase()
        assert kb is not None
        assert not kb.is_loaded()

    def test_load_returns_count(self):
        kb = KnowledgeBase()
        count = kb.load()
        assert count > 0
        assert kb.is_loaded()
        assert count == 29, f"got {count}"

    def test_reload_idempotent(self):
        kb = KnowledgeBase()
        count1 = kb.load()
        count2 = kb.load()
        assert count2 == count1

    def test_search_returns_results(self):
        kb = KnowledgeBase()
        kb.load()
        results = kb.search("spread")
        assert len(results) >= 3

    def test_search_with_category(self):
        kb = KnowledgeBase()
        kb.load()
        results = kb.search("spread", category="pattern")
        assert len(results) >= 2
        assert all(r.category == "pattern" for r in results)

    def test_search_with_domain(self):
        kb = KnowledgeBase()
        kb.load()
        results = kb.search("spread", domain="quantization")
        assert len(results) >= 2
        assert all(r.domain == "quantization" for r in results)

    def test_search_nonexistent(self):
        kb = KnowledgeBase()
        kb.load()
        results = kb.search("nonexistent_keyword_xyz")
        assert len(results) == 0

    def test_search_empty_query_returns_all(self):
        kb = KnowledgeBase()
        kb.load()
        results = kb.search("")
        assert len(results) > 0

    def test_search_empty_with_limit(self):
        kb = KnowledgeBase()
        kb.load()
        results = kb.search("", limit=5)
        assert len(results) == 5

    def test_search_empty_with_category(self):
        kb = KnowledgeBase()
        kb.load()
        results = kb.search("", category="lesson")
        assert len(results) > 0
        assert all(r.category == "lesson" for r in results)

    def test_get_lessons_all(self):
        kb = KnowledgeBase()
        kb.load()
        all_lessons = kb.get_lessons()
        assert len(all_lessons) == 11

    def test_get_lessons_critical(self):
        kb = KnowledgeBase()
        kb.load()
        critical = kb.get_lessons(severity="critical")
        assert len(critical) == 5
        assert all(r.severity == "critical" for r in critical)

    def test_get_lessons_warning(self):
        kb = KnowledgeBase()
        kb.load()
        warning = kb.get_lessons(severity="warning")
        assert len(warning) == 6

    def test_get_lessons_ensemble_domain(self):
        kb = KnowledgeBase()
        kb.load()
        lessons = kb.get_lessons(domain="ensemble")
        assert len(lessons) >= 3

    def test_get_lessons_nonexistent_severity(self):
        kb = KnowledgeBase()
        kb.load()
        none = kb.get_lessons(severity="nonexistent")
        assert len(none) == 0

    def test_get_pattern(self):
        kb = KnowledgeBase()
        kb.load()
        patterns = kb.get_pattern("OOF")
        assert len(patterns) >= 1
        assert all(r.category == "pattern" for r in patterns)

    def test_get_by_domain(self):
        kb = KnowledgeBase()
        kb.load()
        quant = kb.get_by_domain("quantization")
        assert len(quant) > 0
        assert all(r.domain == "quantization" for r in quant)

    def test_get_by_domain_nonexistent(self):
        kb = KnowledgeBase()
        kb.load()
        none_domain = kb.get_by_domain("nonexistent")
        assert len(none_domain) == 0

    def test_get_by_expert(self):
        kb = KnowledgeBase()
        kb.load()
        ji = kb.get_by_expert("季泊松")
        assert len(ji) > 0
        assert all("季泊松" in r.responsible_expert for r in ji)

    def test_get_by_expert_du(self):
        kb = KnowledgeBase()
        kb.load()
        du = kb.get_by_expert("杜博弈")
        assert len(du) > 0
        assert all("杜博弈" in r.responsible_expert for r in du)

    def test_get_by_expert_nonexistent(self):
        kb = KnowledgeBase()
        kb.load()
        none = kb.get_by_expert("不存在的专家")
        assert len(none) == 0

    def test_get_stats(self):
        kb = KnowledgeBase()
        kb.load()
        stats = kb.get_stats()
        assert "total_entries" in stats
        assert "by_category" in stats
        assert "critical_lessons" in stats
        assert stats["total_entries"] == 29
        assert len(stats["by_category"]) == 4

    def test_knowledge_entry_has_fields(self):
        kb = KnowledgeBase()
        kb.load()
        entry = list(kb.entries.values())[0]
        assert entry.key
        assert entry.title
        assert entry.content
        assert entry.category in ["domain", "pattern", "lesson", "feature"]
        assert len(entry.summary()) > 0

    def test_entry_matches_query(self):
        kb = KnowledgeBase()
        kb.load()
        entry = list(kb.entries.values())[0]
        assert entry.matches_query(entry.key)
        assert entry.matches_query(entry.title[:4])
        assert not entry.matches_query("xyz_garbage_12345")

    def test_singleton(self):
        reset_knowledge_base()
        kb1 = get_knowledge_base()
        kb2 = get_knowledge_base()
        assert kb1 is kb2
        assert kb1.is_loaded()
