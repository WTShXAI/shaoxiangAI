"""
CI OOS 铁律守卫 (P0)

不可谈判规则：
  1. 任何 *production*.joblib 必须可加载（防止 BROKEN 模型回潮到生产路径）
  2. config/settings.yaml 中指向的模型文件必须可加载（防止配置指向坏模型）
  3. 审计产物若存在，须干净：无 broken / 无 live_path_broken；且 draw_expert 家族
     诚实 OOS 必须标记为 IN_SAMPLE_ONLY（我们不把 in-sample 幻觉当 edge 合入）

运行：pytest tests/test_oos_guard.py -q
"""
import os, glob, json, joblib
import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SM = os.path.join(ROOT, "saved_models")


def _load(p):
    joblib.load(p)


def test_no_broken_production_models():
    prod = glob.glob(os.path.join(SM, "*production*.joblib"))
    assert prod, "未找到任何 production 模型（预期至少 football_balanced_production）"
    broken = []
    for f in prod:
        try:
            _load(f)
        except Exception as e:
            broken.append((os.path.basename(f), f"{type(e).__name__}: {e}"))
    assert not broken, f"存在无法加载的 production 模型: {broken}"


def test_config_model_paths_loadable():
    cfg_path = os.path.join(ROOT, "config", "settings.yaml")
    if not os.path.exists(cfg_path):
        pytest.skip("config/settings.yaml 不存在")
    cfg = yaml.safe_load(open(cfg_path))
    paths = cfg.get("paths", {})
    model_files = [v for v in paths.values() if isinstance(v, str) and v.endswith(".joblib")]
    broken = []
    for mf in model_files:
        p = mf if os.path.isabs(mf) else os.path.join(ROOT, mf)
        if not os.path.exists(p):
            broken.append((mf, "FILE_MISSING"))
            continue
        try:
            _load(p)
        except Exception as e:
            broken.append((mf, f"{type(e).__name__}: {e}"))
    assert not broken, f"config 指向的模型不可加载: {broken}"


def test_audit_artifact_clean():
    audits = glob.glob(os.path.join(ROOT, "deliverables", "model_oos_audit_*.json"))
    if not audits:
        pytest.skip("审计产物未生成；先运行 scripts/audit_all_models_oos.py")
    latest = max(audits, key=os.path.getmtime)
    d = json.load(open(latest))
    s = d["summary"]
    assert s["broken_count"] == 0, f"审计发现 {s['broken_count']} 个坏模型: {s['broken_models']}"
    assert not s.get("live_path_broken"), f"仍有生产路径坏模型: {s['live_path_broken']}"
    # draw_expert 家族不得被标记为有真实 OOS（防 in-sample 幻觉当 edge）
    bad = [r for r in s.get("draw_expert_oos", []) if r.get("verdict") != "IN_SAMPLE_ONLY"]
    assert not bad, f"draw_expert 家族出现非 IN_SAMPLE_ONLY 声明(疑似 in-sample 幻觉): {bad}"


def test_timeseries_oos_evidence():
    """P0-② 诚实时序 OOS 证据必须存在, 且生产可验证模型不得是 in-sample 幻觉.

    读取 scripts/audit_timeseries_oos.py 产物。任何 verified 模型若被判定为
    IN_SAMPLE_ONLY / NOT_RECONSTRUCTABLE / OOS_UNCOMPUTABLE, 即视为"用随机切分
    当 edge", 禁止合入。OU/goals 因盘口线无数据源, 必须诚实保留 NOT_VERIFIABLE 标注。
    """
    p = os.path.join(ROOT, "deliverables", "model_oos_timeseries_20260715.json")
    assert os.path.exists(p), "缺失时序OOS审计产物(先运行 scripts/audit_timeseries_oos.py)"
    d = json.load(open(p))
    banned = ("IN_SAMPLE_ONLY", "NOT_RECONSTRUCTABLE_SCHEMA_DRIFT", "OOS_UNCOMPUTABLE")
    bad = [r["model"] for r in d.get("verified_models", []) if r.get("verdict") in banned]
    assert not bad, f"以下模型时序OOS判定为幻觉/不可重建, 禁止当edge合入: {bad}"
    nv = d.get("not_verifiable_label_source", [])
    kinds = " ".join(r.get("label_kind", "") for r in nv)
    assert "OU大小球" in kinds and "总进球分档" in kinds, \
        "OU/goals 标签源仍缺失, 必须诚实保留 NOT_VERIFIABLE 标注(不得假装已验证)"
