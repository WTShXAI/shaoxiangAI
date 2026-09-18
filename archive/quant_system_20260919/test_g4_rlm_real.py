"""
G4 单测 — 验证 analyze_multi 消费 rlm_real (真 bet-split) 替代 rlm_proxy 代理
自包含, 不依赖外部 key / 452MB 库 (CI 可移植)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # 仓库根: 使 scripts 包 (scripts.bet_core) 可解析
sys.path.insert(0, os.path.join(ROOT, 'pipeline'))
from reverse_odds_engine import ReverseOddsEngine, OddsInput
from bet_split_source import BetSplit


def _chk(name, cond):
    print(('PASS' if cond else 'FAIL') + ' ' + name)
    assert cond, name


def test_g4_rlm_real_overrides_proxy():
    eng = ReverseOddsEngine()
    wh = OddsInput(open_h=2.0, open_d=3.2, open_a=3.8, close_h=2.0, close_d=3.2, close_a=3.8)
    iw = OddsInput(open_h=2.4, open_d=3.0, open_a=3.0, close_h=2.4, close_d=3.0, close_a=3.0)
    books = [wh, iw]
    # 无 rlm_real → 用代理 (rlm_proxy 非None, verdict 含 'RLM代理')
    r0 = eng.analyze_multi(books)
    _chk('G4-1 无rlm_real时 rlm_real=None', r0.rlm_real is None)
    _chk('G4-2 代理文本出现', 'RLM代理' in r0.verdict)
    # 有 rlm_real → 真源覆盖 (verdict 含 'RLM真源', rlm_real 字段存 dict)
    bs = BetSplit(home_pct=0.55, draw_pct=0.25, away_pct=0.20)  # 投注集中 H
    r1 = eng.analyze_multi(books, rlm_real=bs)
    _chk('G4-3 rlm_real 字段存 dict', isinstance(r1.rlm_real, dict))
    _chk('G4-4 sharp_side=H', r1.rlm_real['sharp_side'] == 'H')
    _chk('G4-5 真源文本覆盖代理', 'RLM真源' in r1.verdict and 'RLM代理' not in r1.verdict)
    # dict 形式也接受
    r2 = eng.analyze_multi(books, rlm_real={'home_pct': 0.20, 'draw_pct': 0.25, 'away_pct': 0.55})
    _chk('G4-6 dict 形式接受 + sharp=A', r2.rlm_real['sharp_side'] == 'A')


if __name__ == '__main__':
    test_g4_rlm_real_overrides_proxy()
    print('\n=== G4 单测全部 PASS ===')
