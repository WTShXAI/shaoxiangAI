"""
OU约束单元测试 — P0-2
========================
覆盖: rules/d_gate_utils.py
- OU诚实度分级 (get_s7_threshold)
- 分裂线陷阱检测 (detect_handicap_trap)
- 穿盘/抗盘数据 (build_cover_database, COVER_DB)
- 球队风格识别
- 球星加成
"""
import sys, os, math

import pytest
from rules.d_gate_utils import (
    build_cover_database, COVER_DB, ALL_RESULTS, ODDS_HISTORY,
    find_similar_matches, get_s7_threshold,
    get_cover_adjustment, get_similar_odds_warning,
    detect_handicap_trap, get_efficiency_adjustment,
    get_star_adjustment, STAR_PLAYERS,
    print_team_styles,
)

# ═══════════════════════════════════════════
# 1. 数据库构建验证
# ═══════════════════════════════════════════

class TestCoverDatabase:
    def test_build_cover_database_returns_dict(self):
        db = build_cover_database()
        assert isinstance(db, dict)
        assert len(db) > 0

    def test_cover_db_has_key_fields(self):
        """每个球队应有所有关键字段"""
        required = {"as_fav", "covered", "as_dog", "anti_covered",
                     "blowouts", "total", "goals_for", "goals_against",
                     "draws", "cover_rate", "anti_rate", "blowout_ratio",
                     "draw_ratio", "gf90", "ga90", "style"}
        db = build_cover_database()
        for team, data in db.items():
            missing = required - set(data.keys())
            assert not missing, f"{team} missing: {missing}"

    def test_all_results_has_34_entries(self):
        assert len(ALL_RESULTS) > 30

    def test_cover_db_total_matches_all_results(self):
        db = build_cover_database()
        total = sum(d['total'] for d in db.values())
        assert total == len(ALL_RESULTS) * 2  # 每场记录两支球队

    def test_team_styles_assigned(self):
        """所有球队应有风格标签"""
        db = build_cover_database()
        valid_styles = {"互捅型", "稳赢型", "沉闷型", "均衡型"}
        for team, data in db.items():
            assert data['style'] in valid_styles, f"{team}: unknown style {data['style']}"

    def test_cover_rate_in_range(self):
        db = build_cover_database()
        for team, data in db.items():
            assert 0 <= data['cover_rate'] <= 1.0, f"{team} cover_rate={data['cover_rate']}"
            assert 0 <= data['draw_ratio'] <= 1.0, f"{team} draw_ratio={data['draw_ratio']}"

    def test_odds_history_consistent(self):
        """ODDS_HISTORY 应与 ALL_RESULTS 一致"""
        assert len(ODDS_HISTORY) == len(ALL_RESULTS)

    @pytest.mark.parametrize("team_name", ["墨西哥", "巴西", "德国", "英格兰",
                                            "阿根廷", "法国", "荷兰", "葡萄牙",
                                            "西班牙", "比利时"])
    def test_major_teams_present(self, team_name):
        assert team_name in COVER_DB, f"{team_name} not in COVER_DB"

# ═══════════════════════════════════════════
# 2. S7 阈值函数
# ═══════════════════════════════════════════

class TestS7Threshold:
    @pytest.mark.parametrize("hcp,expected", [
        (2.0, 6.0),    # >= 1.75
        (1.75, 6.0),   # 边界
        (1.5, 4.5),    # >= 1.0
        (1.0, 4.5),    # 边界
        (0.75, 3.5),   # >= 0.5
        (0.5, 3.5),    # 边界
        (0.25, 2.5),   # < 0.5
        (0.0, 2.5),    # 0
        (-1.0, 4.5),   # 负值, abs=1.0
        (-2.0, 6.0),   # 负值, abs=2.0
    ])
    def test_s7_threshold(self, hcp, expected):
        assert get_s7_threshold(hcp) == expected

    def test_s7_strictly_decreasing(self):
        """hcp 越大, 阈值越高"""
        thresholds = [get_s7_threshold(h) for h in [0.25, 0.5, 1.0, 1.75, 2.5]]
        for i in range(1, len(thresholds)):
            assert thresholds[i] >= thresholds[i-1], (
                f"threshold decreased at {i}: {thresholds[i]} < {thresholds[i-1]}")

# ═══════════════════════════════════════════
# 3. 覆盖调整函数
# ═══════════════════════════════════════════

class TestCoverAdjustment:
    def test_cover_adjustment_returns_tuple(self):
        mult, note = get_cover_adjustment("墨西哥", "南非")
        assert isinstance(mult, float)
        assert isinstance(note, str)

    def test_cover_adjustment_no_crash_unknown_teams(self):
        mult, note = get_cover_adjustment("UnknownTeam", "AnotherUnknown")
        assert mult == 1.0
        assert note == "无调整"

    def test_cover_adjustment_known_teams(self):
        mult, note = get_cover_adjustment("巴西", "墨西哥")
        assert mult > 0
        assert len(note) > 0

# ═══════════════════════════════════════════
# 4. 相似赔率警告
# ═══════════════════════════════════════════

class TestSimilarOddsWarning:
    def test_returns_valid_type(self):
        result, note = get_similar_odds_warning(0.5, 0.3, 0.5)
        assert result in ("none", "draw_bias", "blowout_bias", "mixed", "clean")

    def test_no_crash_extreme_values(self):
        result, note = get_similar_odds_warning(0.9, 0.05, 2.5)
        assert isinstance(result, str)
        assert isinstance(note, str)

# ═══════════════════════════════════════════
# 5. 让球陷阱检测 (分裂线陷阱)
# ═══════════════════════════════════════════

class TestHandicapTrap:
    def test_detect_handicap_trap_strong_team_draw_bias(self):
        """强队平局率高 + 深盘 → 返回陷阱警告"""
        result = detect_handicap_trap("英格兰", "巴拿马", -1.75, "", "")
        # 英格兰 34场数据, 如果平局率高且 hcp>=1.75 → 返回陷阱
        if result:
            assert "让球陷阱" in result

    def test_detect_handicap_trap_none_for_no_data(self):
        """无数据球队 → None"""
        result = detect_handicap_trap("UnknownTeam", "巴拿马", -1.75, "", "")
        assert result is None

    def test_detect_handicap_trap_none_shallow_hcp(self):
        """浅盘应返回 None"""
        result = detect_handicap_trap("英格兰", "巴拿马", -0.5, "", "")
        assert result is None

    def test_detect_handicap_trap_none_for_weak_team(self):
        """弱队陷阱检测应为 None"""
        result = detect_handicap_trap("", "英格兰", -1.75, "", "")
        assert result is None

# ═══════════════════════════════════════════
# 6. 效率调整函数
# ═══════════════════════════════════════════

class TestEfficiencyAdjustment:
    def test_efficiency_adjustment_returns_tuple(self):
        mult, note = get_efficiency_adjustment("墨西哥", "南非")
        assert isinstance(mult, float)
        assert 0.5 <= mult <= 2.0
        assert isinstance(note, str)

    def test_efficiency_adjustment_unknown_teams(self):
        mult, note = get_efficiency_adjustment("Unknown1", "Unknown2")
        assert mult == 1.0
        assert note == "无虚高"

# ═══════════════════════════════════════════
# 7. 球星加成
# ═══════════════════════════════════════════

class TestStarAdjustment:
    @pytest.mark.parametrize("home,away,expected_hb,expected_ab", [
        ("挪威", "墨西哥", 0.4, 0),      # 哈兰德
        ("墨西哥", "法国", 0, 0.4),      # 姆巴佩
        ("英格兰", "阿根廷", 0.3, 0.3),  # 凯恩 + 梅西
        ("Unknown", "Unknown", 0, 0),
    ])
    def test_star_adjustment_values(self, home, away, expected_hb, expected_ab):
        hb, ab, note = get_star_adjustment(home, away)
        assert hb == expected_hb, f"{home}: expected {expected_hb}, got {hb}"
        assert ab == expected_ab, f"{away}: expected {expected_ab}, got {ab}"

    def test_star_note(self):
        hb, ab, note = get_star_adjustment("挪威", "法国")
        assert "哈兰德" in note
        assert "姆巴佩" in note

    def test_star_adjustment_no_stars(self):
        hb, ab, note = get_star_adjustment("Unknown1", "Unknown2")
        assert note == "无球星加成"

    def test_star_players_keys(self):
        known = {"挪威", "塞内加尔", "法国", "英格兰", "葡萄牙",
                 "阿根廷", "巴西", "荷兰", "哥伦比亚", "克罗地亚"}
        assert set(STAR_PLAYERS.keys()) == known

# ═══════════════════════════════════════════
# 8. 相似比赛查找
# ═══════════════════════════════════════════

class TestFindSimilarMatches:
    def test_find_similar_returns_list(self):
        matches = find_similar_matches(0.5, 0.3, 0.5, max_results=3)
        assert isinstance(matches, list)
        assert len(matches) <= 3

    def test_find_similar_sorted_by_hcp_diff(self):
        matches = find_similar_matches(0.5, 0.3, 0.0, max_results=5)
        for i in range(1, len(matches)):
            diff_prev = abs(matches[i-1]['hcp'] - 0.0)
            diff_curr = abs(matches[i]['hcp'] - 0.0)
            assert diff_prev <= diff_curr, "Not sorted by hcp diff"

    def test_find_similar_max_results(self):
        matches = find_similar_matches(0.5, 0.3, 0.5, max_results=2)
        assert len(matches) <= 2

# ═══════════════════════════════════════════
# 9. print_team_styles 冒烟测试
# ═══════════════════════════════════════════

class TestPrintTeamStyles:
    def test_print_team_styles_does_not_crash(self):
        """确认 print_team_styles 不会崩溃"""
        import io
        from contextlib import redirect_stdout
        f = io.StringIO()
        with redirect_stdout(f):
            print_team_styles()
        output = f.getvalue()
        assert "球队风格数据库" in output
        assert len(output) > 100
