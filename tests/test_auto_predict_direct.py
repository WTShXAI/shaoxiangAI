"""
tests/test_auto_predict_direct.py — auto_predict.py 直接 pytest + IR-30 诚实断言
====================================================================================
来源: 自主 owner 任务 T27 (基于 T21 规格 §2.1-2.4 切片)。
闭环 T20 HIGH 缺口剩余切片 (T25 已落 calibration 纯函数, 本文件落 auto_predict 直接触点)。

范围 (零生产 I/O):
  pipeline.auto_predict.plugin_predict / predict_date / predict_range /
  plugin_risk_assess / _get_fallback_schedule / get_schedule(经临时库)
- 用 monkeypatch 隔离: get_schedule 返回确定性 8 元组 + PLUGINS 仅留 [predict],
  绝不触发 load_standings/odds_fetcher(网络/DB) 插件。
- 用临时 sqlite 测试 get_schedule 的 norm 解析 + BETWEEN 查询 (零生产库)。
- IR-30 诚实断言: devig_power 幂法方向护栏 / G6 零信息机械对照比例法翻正·幂法塌缩
  / 无 edge 不宣称盈利 (auto_predict 不产出任何 profitability 字段)。

不触碰生产代码、不依赖 events.db、不改被测模块源码。
"""
import os
import sys
import math
import sqlite3
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline.auto_predict as ap
from pipeline.odds_math import devig3, devig_power


# 确定性 8 元组赛程: (date, home, away, oh, od, oa, hcp, ou)
SCHED_ONE = [('6.25', 'A', 'B', 2.0, 3.0, 4.0, -0.5, 2.5)]
SCHED_TWO = [
    ('6.25', 'A', 'B', 2.0, 3.0, 4.0, -0.5, 2.5),
    ('6.25', 'C', 'D', 1.5, 4.0, 8.0, -1.0, 2.5),
]
SCHED_MULTI = [
    ('6.25', 'A', 'B', 2.0, 3.0, 4.0, -0.5, 2.5),
    ('6.26', 'E', 'F', 2.5, 3.0, 3.0, 0.0, 2.5),
    ('6.27', 'G', 'H', 1.25, 5.0, 10.0, -1.5, 2.5),
]

RESULT_KEYS = {'date', 'home', 'away', 'odds', 'hcp', 'ou', 'verdict',
               'winner', 'mode', 'scores', 'signals', 'lambda_h', 'lambda_a'}


def _force_predict_fallback(monkeypatch):
    """保证 plugin_predict 走 fallback (predict_with_scores 不可用)。
    无 rules 包时自然失败; 用 sys.modules=None 固化该行为, 防未来 rules 被引入误判。"""
    monkeypatch.setitem(sys.modules, 'rules.tournament_dynamics', None)
    monkeypatch.setattr(ap, 'PLUGINS',
                        [{'name': 'predict', 'fn': ap.plugin_predict, 'priority': 30}])


def _implied(oh, od, oa):
    rh, rd, ra = 1.0 / oh, 1.0 / od, 1.0 / oa
    margin = rh + rd + ra
    return rh / margin, rd / margin, ra / margin


# ─────────────────────────────────────────────────────────────────────────────
# §2.1 plugin_predict 纯函数 (predict_with_scores 不可用 → fallback)
# ─────────────────────────────────────────────────────────────────────────────
def test_plugin_predict_structure_and_keys(monkeypatch):
    _force_predict_fallback(monkeypatch)
    results, standings, matchday = ap.plugin_predict(SCHED_TWO, {}, None)
    assert len(results) == 2
    for r in results:
        assert set(r.keys()) == RESULT_KEYS
        assert r['mode'] == 'unavailable'        # fallback lambda 标记
        assert r['verdict'] == '?'
        assert r['winner'] == '?'
        assert r['scores'] == []
        assert isinstance(r['lambda_h'], (int, float))
        assert isinstance(r['lambda_a'], (int, float))


def test_plugin_predict_implied_prob_drives_narrow_spread(monkeypatch):
    """赔率→隐含概率计算正确 (ph=1/oh/(1/oh+1/od+1/oa))。
    通过窄 spread 判定 (由 ph/pa 公式驱动) 行为化验证: 同接近赔率应触发窄spread,
    差异大赔率不应触发。"""
    _force_predict_fallback(monkeypatch)
    # 窄 spread: (2.5,3.0,3.0) ph=0.375 pa=0.3125 diff=0.0625 < 0.15
    narrow = [('6.25', 'A', 'B', 2.5, 3.0, 3.0, 0.0, 2.5)]
    # 宽 spread: (1.5,4.0,8.0) ph=0.584 pa=0.109 diff=0.475 > 0.15
    wide = [('6.25', 'C', 'D', 1.5, 4.0, 8.0, -1.0, 2.5)]
    rn, _, _ = ap.plugin_risk_assess(ap.plugin_predict(narrow, {}, None)[0], {}, None)
    rw, _, _ = ap.plugin_risk_assess(ap.plugin_predict(wide, {}, None)[0], {}, None)
    # 独立重算, 与 auto_predict 内部公式契约一致
    ph_n, _, pa_n = _implied(2.5, 3.0, 3.0)
    ph_w, _, pa_w = _implied(1.5, 4.0, 8.0)
    assert abs((ph_n - pa_n) - 0.0625) < 1e-6
    assert (ph_w - pa_w) > 0.15
    assert any('窄spread' in s for s in rn[0]['risks'])
    assert not any('窄spread' in s for s in rw[0]['risks'])


def test_plugin_predict_standings_group_table_no_crash(monkeypatch):
    _force_predict_fallback(monkeypatch)
    standings = {'A': {'group': 'X'}, 'B': {'group': 'X'}}
    results, st, md = ap.plugin_predict(SCHED_ONE, standings, 1)
    assert len(results) == 1
    assert set(results[0].keys()) == RESULT_KEYS
    # standings 原样回传
    assert st == standings and md == 1


# ─────────────────────────────────────────────────────────────────────────────
# §2.2 predict_date / predict_range
# ─────────────────────────────────────────────────────────────────────────────
def test_predict_date_equals_range_single(monkeypatch):
    _force_predict_fallback(monkeypatch)
    monkeypatch.setattr(ap, 'get_schedule', lambda s, e=None: SCHED_ONE)
    d = ap.predict_date('6.25')
    r = ap.predict_range('6.25', '6.25')
    assert d == r
    assert len(d) == 1
    assert d[0]['home'] == 'A' and d[0]['away'] == 'B'


def test_predict_range_empty_schedule_returns_empty(monkeypatch):
    _force_predict_fallback(monkeypatch)
    monkeypatch.setattr(ap, 'get_schedule', lambda s, e=None: [])
    out = ap.predict_range('6.25', '6.25')
    assert out == []          # 对照源码 print+return [] 分支, 不抛


def test_predict_range_multi_day_grouped(monkeypatch):
    _force_predict_fallback(monkeypatch)
    monkeypatch.setattr(ap, 'get_schedule', lambda s, e=None: SCHED_MULTI)
    out = ap.predict_range('6.25', '6.27')
    assert len(out) == 3
    dates = {r['date'] for r in out}
    assert dates == {'6.25', '6.26', '6.27'}


# ─────────────────────────────────────────────────────────────────────────────
# §2.3 plugin_risk_assess (直接构造 result 验证风险判定; fallback verdict='?' 不触发 D 分支)
# ─────────────────────────────────────────────────────────────────────────────
def _mk_result(odds, verdict, signals=None):
    return {'date': '6.25', 'home': 'A', 'away': 'B', 'odds': odds, 'hcp': -1.0,
            'ou': 2.5, 'verdict': verdict, 'winner': '?', 'mode': 'unavailable',
            'scores': [], 'signals': signals or [], 'lambda_h': 0, 'lambda_a': 0}


def test_risk_mode_c_super_favorite():
    res, _, _ = ap.plugin_risk_assess([_mk_result('1.25/5/10', 'D')], {}, None)
    assert any('🔴 Mode C' in s for s in res[0]['risks'])


def test_risk_narrow_spread():
    res, _, _ = ap.plugin_risk_assess([_mk_result('2.5/3.0/3.0', 'H')], {}, None)
    risks = res[0]['risks']
    assert any('🟡 窄spread' in s for s in risks)
    assert not any('🔴' in s for s in risks)


def test_risk_safe_path():
    res, _, _ = ap.plugin_risk_assess([_mk_result('1.5/4.0/8.0', 'H')], {}, None)
    assert res[0]['risks'] == ['⚪ 安全']


# ─────────────────────────────────────────────────────────────────────────────
# §2.4 _get_fallback_schedule / get_schedule(norm 解析, 经临时库)
# ─────────────────────────────────────────────────────────────────────────────
def test_fallback_schedule_range_filter():
    one = ap._get_fallback_schedule('6.25')
    assert len(one) == 6 and all(d == '6.25' for d, *_ in one)
    multi = ap._get_fallback_schedule('6.25', '6.28')
    days = {d for d, *_ in multi}
    assert days == {'6.25', '6.26', '6.27', '6.28'}
    # end_date=None → 等同单日
    assert ap._get_fallback_schedule('6.26') == ap._get_fallback_schedule('6.26', '6.26')


def test_get_schedule_norm_roundtrip_via_temp_db(monkeypatch):
    """经临时 sqlite 验证 norm('6.25'→'2026-06-25') + BETWEEN 查询 + 回格式化('2026-06-25'→'6.25')。
    零生产 I/O: 临时库建在 tmp, 不碰 wc2026_timeline.db。"""
    tmp = Path(tempfile.mkdtemp())
    data_dir = tmp / 'data'
    data_dir.mkdir()
    db = data_dir / 'wc2026_timeline.db'
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE wc2026_matches (match_date TEXT, home_team TEXT, away_team TEXT)")
    conn.execute("INSERT INTO wc2026_matches VALUES ('2026-06-25','TeamA','TeamB')")
    conn.execute("INSERT INTO wc2026_matches VALUES ('2026-06-26','TeamC','TeamD')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(ap, 'PROJECT_ROOT', tmp)
    rows = ap.get_schedule('6.25')
    # 回格式化保留零填充: '2026-06-25' → '06.25' (与 fallback 的 '6.25' 不一致, 但 DB 路径契约如此)
    assert rows == [('06.25', 'TeamA', 'TeamB')]   # norm + BETWEEN + 回格式化全链路
    rows2 = ap.get_schedule('6.25', '6.26')
    assert {h for _, h, _ in rows2} == {'TeamA', 'TeamC'}


# ─────────────────────────────────────────────────────────────────────────────
# IR-30 诚实断言 (T27 承重部分)
# ─────────────────────────────────────────────────────────────────────────────
def test_auto_predict_no_profitability_claim():
    """IR-30: 无 edge 不宣称盈利。auto_predict 是预测/风险工具,
    结果 dict 不得含任何 profitability 字段, 信号中也不得出现盈利性措辞。"""
    PROFIT_KEYS = {'edge', 'ev', 'roi', 'profit', '+ev', 'value'}
    PROFIT_WORDS = ('edge', 'ev', 'roi', '盈利', '+ev', '价值', '让球值')
    res, _, _ = ap.plugin_predict(SCHED_TWO, {}, None)
    res, _, _ = ap.plugin_risk_assess(res, {}, None)
    for r in res:
        assert not (set(r.keys()) & PROFIT_KEYS), f"意外盈利字段: {set(r.keys()) & PROFIT_KEYS}"
        blob = ' '.join(r.get('signals', []) + r.get('risks', [])).lower()
        assert not any(w in blob for w in PROFIT_WORDS), f"盈利性措辞泄漏: {blob}"


def test_raw_implied_prob_margin_inflated_requires_devig():
    """IR-30 去水口径: auto_predict 用裸 1/oh 隐含概率 (含 margin, 和>1),
    不能直接当真实概率做 +EV; 必须过 devig_power 幂法 SSoT 才合规。
    本断言固化该契约, 防未来有人用裸隐含概率造虚假 edge。"""
    o = (1.20, 5.0, 12.0)
    raw_sum = (1.0 / o[0]) + (1.0 / o[1]) + (1.0 / o[2])
    assert raw_sum > 1.0                       # margin 未去除 → 裸隐含和>1
    power = devig_power(o)
    prop = devig3(*o)
    # 幂法才是诚实真实概率口径 (和=1)
    assert abs(sum(power) - 1.0) < 1e-6
    assert abs(sum(prop) - 1.0) < 1e-9
    # 裸隐含 > 幂法 → 若用裸值算 edge 必虚高
    assert (1.0 / o[0]) > power[0]


def test_devig_power_direction_guard():
    """09-23 事故护栏 (实测方向, 见 T25 勘误): 幂法 FLB 修正使热门概率
    高于比例法 devig3 (devig_power[0] > devig3[0])。任何'幂法抑热门'反向断言都须失败。"""
    o = (1.20, 5.0, 12.0)
    prop = devig3(*o)
    power = devig_power(o)
    assert power[0] > prop[0]                  # 实测方向
    assert devig_power([1.0, 2.0, 3.0]) is None  # 赔率≤1 非法


def test_g6_zero_info_contraction():
    """G6 零信息机械对照 (防 09-23 复现): 模型零真实 edge(其概率=幂法真相代理) 时,
    比例法 devig3 系统性低估热门 → 制造虚假 +edge; 幂法 devig_power 是诚实口径 → edge≈0。
    等价于把 verification_devig_fix.md 结论固化为单元级护栏。"""
    o = (1.20, 5.0, 12.0)
    power = devig_power(o)
    prop = devig3(*o)
    model_fav = power[0]                # 模型正确估计 = 幂法值, 无真实 edge
    edge_prop = model_fav - prop[0]     # 比例法下虚假 +edge
    edge_power = model_fav - power[0]   # 幂法下 ~0
    assert edge_prop > 0.01
    assert abs(edge_power) < 1e-6
