"""G6 单测 — live 初盘 drift 补完 (自包含, 无重DB依赖, CI 可移植).

用临时 sqlite 库(team_canonical + odds_features 小样本)验证完整逻辑链:
  G6-1: 跨语言归一 — 英文 live 队名经 team_canonical 解析为中文, 命中 odds_features 初盘(open/close).
  G6-2: 无初盘数据 -> query_odds_by_teams 返回 None (调用方走 open=close 兜底并标 drift_available=False).
  G6-3: honest_def 闸门 — drift 缺失时绝不误触发 (不可把"无数据"当"无陷阱").
  G6-4: _resolve_canonical 直接验证 EN->ZH 桥接 (含中英混排 / obscure 兜底).
  G6-5: _latin_key 提取.

不依赖 452MB football_data.db, 毫秒级. 真实库增强已在本地人工核验(Arsenal->阿森纳 746 命中).
"""
import os
import sys
import tempfile
import sqlite3

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from pipeline.reverse_odds_engine import (
    ReverseOddsEngine, OddsInput,
    _latin_key, _build_alias_map, _resolve_canonical,
)

ENGINE = ReverseOddsEngine()


def _build_temp_db() -> str:
    """构造自包含临时库: team_canonical(EN别名->中文) + odds_features(含open/close)."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="g6_test_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE team_canonical (canonical TEXT, aliases_json TEXT)")
    conn.execute(
        "CREATE TABLE odds_features ("
        "home_team TEXT, away_team TEXT, open_h REAL, open_d REAL, open_a REAL, "
        "close_h REAL, close_d REAL, close_a REAL, match_date TEXT)"
    )
    # 中文 canonical + 英文别名
    canon = [
        ("阿森纳", '["Arsenal","阿森纳"]'),
        ("切尔西", '["Chelsea","切尔西"]'),
        ("利物浦", '["Liverpool","利物浦"]'),
        ("曼城", '["Manchester City","曼城"]'),
    ]
    conn.executemany("INSERT INTO team_canonical VALUES (?,?)", canon)
    # odds_features: 中文队名 + 初盘/终盘 (open!=close -> drift 非零)
    feats = [
        # Arsenal vs Chelsea -> 阿森纳/切尔西, 初盘 1.75 -> 终盘 1.70 (主胜被压)
        ("阿森纳", "切尔西", 1.75, 3.40, 4.50, 1.70, 3.60, 4.80, "2024-03-01"),
        # 利物浦 vs 曼城, 初盘 2.10 -> 终盘 2.30 (主胜被抬)
        ("利物浦", "曼城", 2.10, 3.20, 3.30, 2.30, 3.10, 3.10, "2024-03-02"),
    ]
    conn.executemany(
        "INSERT INTO odds_features VALUES (?,?,?,?,?,?,?,?,?)", feats
    )
    conn.commit()
    conn.close()
    return path


def _chk(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {extra}")
    if not cond:
        raise AssertionError(f"{name} FAILED {extra}")


def test_g6_1_canonical_resolves_open():
    """英文队名 -> 中文 canonical -> odds_features 初盘命中 (open!=close -> drift 可算)."""
    db = _build_temp_db()
    try:
        oi = ENGINE.query_odds_by_teams("Arsenal", "Chelsea", db_path=db)
        _chk("G6-1 query_odds_by_teams(Arsenal,Chelsea) 非空", oi is not None)
        if oi is not None:
            _chk("G6-1 open_h>0", oi.open_h > 0, f"open_h={oi.open_h}")
            _chk("G6-1 close_h>0", oi.close_h > 0, f"close_h={oi.close_h}")
            _chk("G6-1 drift_h 已计算(float, 非零)", isinstance(oi.drift_h, float) and oi.drift_h != 0.0,
                 f"drift_h={oi.drift_h}")
        # 对照: 旧逻辑(纯精确匹配英文)在中文库必查不到 -> 凸显增强价值
        conn = sqlite3.connect(db)
        exact = conn.execute(
            "SELECT 1 FROM odds_features WHERE home_team='Arsenal' AND away_team='Chelsea' LIMIT 1"
        ).fetchone()
        conn.close()
        _chk("G6-1 旧精确匹配英文确查不到(增强生效)", exact is None)
    finally:
        os.remove(db)


def test_g6_2_no_open_returns_none():
    """不存在的队名 -> 无初盘 -> 返回 None (drift_available 应为 False)."""
    db = _build_temp_db()
    try:
        oi = ENGINE.query_odds_by_teams("ZZTopNonExistentX", "ZZBottomNonExistentY", db_path=db)
        _chk("G6-2 无初盘返回 None", oi is None)
    finally:
        os.remove(db)


def test_g6_3_honest_def_gate_no_false_trigger():
    """drift 缺失时 honest_def 闸门必须关闭, 不应误触发."""
    # drift_h=None -> 闸门 has_drift=False -> detected=False
    oi = OddsInput(open_h=2.0, open_d=3.0, open_a=3.5,
                   close_h=2.0, close_d=3.0, close_a=3.5,
                   drift_h=None, drift_d=None, drift_a=None)
    res = ENGINE.honest_def_nudge([oi], (0.5, 0.25, 0.25))
    _chk("G6-3 drift=None 时 honest_def 不触发", res["detected"] is False)
    # open=close (drift 自动为 0) 也低于阈值 -> 不触发
    oi2 = OddsInput(open_h=2.0, open_d=3.0, open_a=3.5,
                    close_h=2.0, close_d=3.0, close_a=3.5)  # drift 自动 =0
    res2 = ENGINE.honest_def_nudge([oi2], (0.5, 0.25, 0.25))
    _chk("G6-3 open=close(drift=0) 不触发", res2["detected"] is False,
         f"drift_h={oi2.drift_h}")


def test_g6_4_resolve_canonical_direct():
    """_resolve_canonical 直接桥接验证 (用临时库 alias_map)."""
    db = _build_temp_db()
    try:
        amap = _build_alias_map(db)
        _chk("G6-4 Arsenal -> 阿森纳", _resolve_canonical("Arsenal", amap) == "阿森纳")
        # 中英混排(含英文别名的 live 常见写法) -> 经 latin_key 提取解析
        _chk("G6-4 中英混排 阿森纳(Arsenal) -> 阿森纳",
             _resolve_canonical("阿森纳(Arsenal)", amap) == "阿森纳")
        _chk("G6-4 纯中文 利物浦 -> 利物浦", _resolve_canonical("利物浦", amap) == "利物浦")
        # obscure 联赛队(不在 team_canonical 内) -> 正确返回 None (走 open=close 兜底, drift_available=False)
        _chk("G6-4 obscure 阿什杜德(Ashdod) -> None(预期兜底)",
             _resolve_canonical("阿什杜德(Ashdod)", amap) is None)
        _chk("G6-4 不存在名 -> None", _resolve_canonical("ZZNoSuchTeam", amap) is None)
    finally:
        os.remove(db)


def test_g6_5_latin_key():
    _chk("G6-5 _latin_key 中英混排", _latin_key("阿什杜德(Ashdod)") == "ashdod")
    _chk("G6-5 _latin_key 纯英文", _latin_key("Arsenal FC") == "arsenalfc")
    _chk("G6-5 _latin_key 纯中文 -> 空", _latin_key("曼城") == "")


if __name__ == "__main__":
    test_g6_1_canonical_resolves_open()
    test_g6_2_no_open_returns_none()
    test_g6_3_honest_def_gate_no_false_trigger()
    test_g6_4_resolve_canonical_direct()
    test_g6_5_latin_key()
    print("\n✅ G6 全部单测 PASS")
