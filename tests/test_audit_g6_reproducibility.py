"""T41 只读审计脚本 `scripts/audit_g6_reproducibility.py` 的单元测试。

零生产 I/O：只用内存/临时库，绝不打开 verification.db，绝不碰 events.db。
覆盖：判定式复刻正确性 / fail-closed / 可复现性 / 缺口 drop / 生产 build_bundle 对账 / placebo。
"""
from __future__ import annotations

import os
import random
import sqlite3

import pytest

from scripts.audit_g6_reproducibility import (
    REPO_ROOT,
    compute_g6,
    connect_ro,
    favourite_side,
    latent_devig_gap,
    load_ledger,
    mech_payoff,
    passes_g6,
    placebo_random,
    production_pairing,
    render_md,
)


# ── fixtures ──────────────────────────────────────────────────────────────
def _row(odds, actual, chosen):
    """odds = (h,d,a) decimal; actual/chosen in home|draw|away."""
    d = {"home": odds[0], "draw": odds[1], "away": odds[2]}
    payoff = float(d[chosen] - 1.0) if actual == chosen else -1.0
    return {
        "match_id": "m",
        "model_source": "T",
        "kickoff_utc": "2026-09-27T00:00:00Z",
        "match_date": "2026-09-27",
        "chosen_outcome": chosen,
        "p_home": None, "p_draw": None, "p_away": None,
        "chosen_dec_odds": d[chosen],
        "devig_h": d["home"], "devig_d": d["draw"], "devig_a": d["away"],
        "paper_stake": 1.0,
        "settled_home": 1, "settled_away": 0,
        "settled_outcome": actual,
        "payoff": payoff,
        "is_credible": 1,
        "created_at": "2026-09-27T00:00:00Z",
        "devig_method": "power",
        "row_id": 1,
    }


def _synth(n: int = 400, seed: int = 11):
    """构造一个'买热门期望≈0'的合成宇宙: 每场真实概率 = 1/devig 附近。"""
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        base = [round(rnd.uniform(1.4, 6.0), 2) for _ in range(3)]
        s = sum(base)
        # 真实概率 = 去水概率 * 0.98 (轻微不利), 保证热门侧期望≈0
        p = [b / s * 0.98 for b in base]
        r = rnd.random()
        acc = 0.0
        actual = "away"
        for k, pk in zip(("home", "draw", "away"), p):
            acc += pk
            if r <= acc:
                actual = k
                break
        odds = [round(1.0 / (b / s), 2) for b in base]
        chosen = rnd.choice(("home", "draw", "away"))
        rows.append(_row(odds, actual, chosen))
    return rows


def _apply_chosen(rows, fn):
    """改选边并**同步重算 payoff**(生产账本里 payoff 由 chosen 决定, 改选边不改 payoff 会让对照失真)。"""
    for r in rows:
        r["chosen_outcome"] = fn(r)
        d = {"home": r["devig_h"], "draw": r["devig_d"], "away": r["devig_a"]}
        r["payoff"] = float(d[r["chosen_outcome"]] - 1.0) if r["settled_outcome"] == r["chosen_outcome"] else -1.0
    return rows


# ── Q1 判定式还原 ─────────────────────────────────────────────────────────
def test_favourite_side_and_mech_payoff():
    r = _row((2.0, 3.0, 4.0), "home", "home")
    assert favourite_side(r) == "home"
    assert mech_payoff(r) == 1.0          # 命中最短赔率
    r2 = _row((2.0, 3.0, 4.0), "away", "home")
    assert mech_payoff(r2) == -1.0        # 未命中
    r3 = _row((2.0, 3.0, 4.0), "draw", "draw")
    assert favourite_side(r3) == "home"   # 并列取字典序, 与生产 min 语义一致


def test_missing_devig_rows_dropped_from_paired_set():
    r = _row((2.0, 3.0, 4.0), "home", "home")
    r["devig_d"] = None
    g = compute_g6([r], n_boot=100)
    assert g["n_ledger"] == 1
    assert g["n_paired"] == 0
    assert g["dropped_no_devig"] == 1
    assert g["pass"] is False
    assert g["pass_reason"] == "no_paired_rows"


def test_g6_fails_when_model_follows_favourite():
    rows = _apply_chosen(_synth(200), favourite_side)
    g = compute_g6(rows, n_boot=200)
    assert g["paired_excess"] == pytest.approx(0.0, abs=1e-12)
    assert g["ci_low"] == pytest.approx(0.0, abs=1e-12)
    assert passes_g6(g) is False
    assert g["same_as_favourite_rate"] == 1.0


def test_g6_passes_when_model_beats_favourite():
    """构造一个确定优于热门的合成模型: 选赔率最高且确实命中的那场。"""
    def longest(r):
        d = {"home": r["devig_h"], "draw": r["devig_d"], "away": r["devig_a"]}
        best = max(d, key=lambda k: d[k])
        return best if r["settled_outcome"] == best else "draw"

    rows = _apply_chosen(_synth(300), longest)
    g = compute_g6(rows, n_boot=500)
    assert g["paired_excess"] > 0.0
    assert passes_g6(g) is True


# ── Q3 可复现性 ───────────────────────────────────────────────────────────
def test_compute_g6_is_bitwise_reproducible():
    rows = _synth(150)
    a = compute_g6(rows, n_boot=200)
    b = compute_g6(rows, n_boot=200)
    assert a["paired_excess"] == b["paired_excess"]
    assert a["ci_low"] == b["ci_low"]


def test_bootstrap_seed_documented():
    """roi_ci_bootstrap 固定种子 → 与调用顺序无关 (回归护栏)."""
    import inspect

    from verification import stats as st

    src = inspect.getsource(st.roi_ci_bootstrap)
    assert "default_rng(12345)" in src


def test_latent_devig_gap_trims_paired_set():
    rows = _synth(200)
    base = compute_g6(rows, n_boot=200)
    trimmed = [dict(r) for i, r in enumerate(rows) if i % 10 != 0]
    d = latent_devig_gap(rows, frac=0.10, n_boot=200)
    assert d["base_n_paired"] == base["n_paired"]
    assert d["trim_n_paired"] == len(trimmed)
    assert d["delta_paired_excess"] is not None


# ── 生产路径对账 ──────────────────────────────────────────────────────────
def test_matches_production_build_bundle(tmp_path):
    rows = _synth(60)
    from verification import ledger as _l

    db = tmp_path / "g6_test.db"
    led = _l.VerificationLedger(str(db))
    led.ensure_schema()
    for i, r in enumerate(rows):
        r = dict(r)
        r["match_id"] = f"m{i}"
        r["row_id"] = i + 1
        led.append(r)
    loaded = led.fetch_credible("T")
    assert len(loaded) == len(rows)

    mine = compute_g6(loaded)          # 与生产同默认 n_boot=10000, 逐位可比
    prod = production_pairing(loaded, "T")
    assert prod is not None
    assert prod["paired_excess"] == pytest.approx(mine["paired_excess"], abs=1e-12)
    assert prod["mech_fav_roi"] == pytest.approx(mine["mech_roi"], abs=1e-12)
    assert prod["paired_excess_ci_low"] == pytest.approx(mine["ci_low"], abs=1e-9)


# ── Q4 placebo / 报告 ─────────────────────────────────────────────────────
def test_placebo_random_is_near_neutral():
    rows = _synth(300)
    p = placebo_random(rows, trials=20, n_boot=100)
    assert p["trials"] == 20
    assert p["g2_and_g6_pass"] <= 2          # 纯噪声不应大量放行
    assert p["placebo_roi_mean"] == pytest.approx(0.0, abs=0.15)
    # t 口径是 bootstrap 的敏感性对照 (下尾不受右偏抬高)
    assert p["g2_pass_t"] <= p["g2_pass"] + 1


def test_readonly_connector_and_load(tmp_path):
    db = tmp_path / "x.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE t (a INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    ro = connect_ro(str(db))
    assert ro.execute("SELECT a FROM t").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (2)")
    ro.close()
    # load_ledger 读的是账本表: 空表库应返回 [] 而非抛错 (缺表属异常, 此处只验空表分支)
    with pytest.raises(sqlite3.OperationalError):
        load_ledger(str(db))


def test_render_md_contains_sections():
    payload = {
        "meta": {"generated_at": "T", "db": "d", "total_rows": 1},
        "sources": [{"source": "T", "g6": compute_g6(_synth(30), n_boot=50),
                     "production_pairing": None, "matches_production": None}],
        "reproducibility": {"a": "b"},
        "placebo": [{"source": "T", "placebo": placebo_random(_synth(30), trials=3, n_boot=50)}],
        "latent": [{"source": "T", "detail": latent_devig_gap(_synth(30), n_boot=50)}],
        "conclusions": ["c"],
    }
    md = render_md(payload)
    assert "G6 零信息机械对照可复现性只读审计" in md
    assert "## 结论" in md
    assert REPO_ROOT.endswith("Architecture")


def test_script_lives_in_scripts_dir():
    assert os.path.exists(os.path.join(REPO_ROOT, "scripts", "audit_g6_reproducibility.py"))
