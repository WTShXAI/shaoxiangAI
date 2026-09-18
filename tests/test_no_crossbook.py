"""IR-32 跨庄共识永久禁令 — 反向守卫 (2026-09-19).

生产源码 (bridge_service.py / gq/ / pipeline/ 顶层模块) 不得 import 已归档的
跨庄/量化模块; 违规即 FAIL。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANNED = (
    'cross_book_edge', 'multibook_consensus', 'leyu_value_signal',
    'bet_split_source', 'compute_value_layer', 'from scripts.bet_core',
    'cross_book_alert',
)
SCAN_FILES = [
    os.path.join(ROOT, 'bridge_service.py'),
    os.path.join(ROOT, 'gq', 'auto_collector.py'),
    os.path.join(ROOT, 'gq', 'db.py'),
    os.path.join(ROOT, 'pipeline', 'prematch_similarity.py'),
    os.path.join(ROOT, 'pipeline', 'predict_export.py'),
    os.path.join(ROOT, 'pipeline', 'odds_candles_predict.py'),
    os.path.join(ROOT, 'pipeline', 'ht_anchor_predict.py'),
    os.path.join(ROOT, 'pipeline', 'score_model.py'),
    os.path.join(ROOT, 'pipeline', 'ranked_predictor.py'),
]


def test_no_crossbook_imports_in_production():
    violations = []
    for path in SCAN_FILES:
        if not os.path.exists(path):
            continue
        src = open(path, encoding='utf-8', errors='replace').read()
        for banned in BANNED:
            # 只匹配 import 语句 (注释中提及允许)
            if re.search(rf'^\s*(from\s+\S*{banned}\S*\s+|import\s+\S*{banned})', src, re.M):
                violations.append((os.path.relpath(path, ROOT), banned))
    assert not violations, f'IR-32 违规: 生产代码 import 跨庄/量化模块 {violations}'
