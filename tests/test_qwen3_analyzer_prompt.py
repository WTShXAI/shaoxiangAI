from odds_db.qwen3_analyzer import build_analysis_prompt


def test_build_analysis_prompt_includes_final_result_calibration():
    prompt, system = build_analysis_prompt(
        "乌兰巴托 vs 新北航源",
        {"home_team": "乌兰巴托", "away_team": "新北航源", "final_result": "1-2"},
    )

    assert "结果回校（已知赛果）" in prompt
    assert "乌兰巴托 vs 新北航源" in prompt
    assert "1-2" in prompt
    assert "反向爆点" in prompt
    assert "结果回校" in system
