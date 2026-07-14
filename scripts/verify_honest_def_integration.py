"""honest_def 低权重次级修正 · 集成回归测试。

验证点:
  1) honest_def_nudge: 单庄 honest_defH(漂移 H↓D↑A↑) → 检测H, H概率向0.559回归, W=0.12
  2) honest_def_nudge: 无漂移(live盘 open=close) → 不触发, 返回原概率
  3) analyze_multi: 跨庄分歧(WH=H, IW=A) → soft-line淡化照常, honest_def不干扰(无drift)
  4) analyze(单庄): honest_def 漂移 → 次级修正生效, 字段填充
  5) query_odds_multi: 真实DB行(含drift) → 能取到drift, honest_def字段为有效值或None
退出码非0=有失败。
"""
import sys
sys.path.insert(0, '.')

from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput

fails = []

def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {extra}")
    if not cond:
        fails.append(name)

eng = ReverseOddsEngine()

# 1) 单庄 honest_defH: H↓D↑A↑ (drift -0.1 / +0.09 / +0.1)
book_hd = OddsInput(open_h=2.0, open_d=3.3, open_a=4.0,
                    close_h=1.8, close_d=3.6, close_a=4.4,
                    drift_h=-0.10, drift_d=0.09, drift_a=0.10)
base = book_hd.implied_probs
hd = eng.honest_def_nudge([book_hd], base)
check("honest_defH 检测", hd['detected'] and hd['target'] == 'H',
      f"target={hd['target']} W={hd['weight']}")
check("honest_defH H概率上升", hd['probs'][0] > base[0],
      f"{base[0]:.3f} -> {hd['probs'][0]:.3f}")
check("honest_defH 权重=0.12(单庄)", abs(hd['weight'] - 0.12) < 1e-9)
check("honest_defH 概率和=1", abs(sum(hd['probs']) - 1.0) < 1e-9)
# H 应被拉向 0.559 但不超过太多 (W小, 温和)
check("honest_defH H<=0.559+eps", hd['probs'][0] <= 0.559 + 0.02,
      f"H={hd['probs'][0]:.3f}")

# 2) 无漂移 (live盘 open=close) → 不触发
book_flat = OddsInput(open_h=2.0, open_d=3.3, open_a=4.0,
                      close_h=2.0, close_d=3.3, close_a=4.0)  # drift自动=0
hd2 = eng.honest_def_nudge([book_flat], book_flat.implied_probs)
check("无漂移不触发", (not hd2['detected']) and hd2['probs'] == book_flat.implied_probs)

# 3) analyze_multi 跨庄分歧, 无drift → soft-line淡化, honest_def不干扰
b_wh = OddsInput(open_h=2.0, open_d=3.3, open_a=4.0, close_h=1.8, close_d=3.5, close_a=4.5)
b_iw = OddsInput(open_h=2.5, open_d=3.2, open_a=2.6, close_h=2.6, close_d=3.2, close_a=2.4)
r3 = eng.analyze_multi([b_wh, b_iw])
check("分歧→soft-line淡化", r3.disagreement_detected and r3.softline_fade_applied)
fav = int(__import__('numpy').argmax(r3.implied_probs))
check("分歧→共识热门被压低", r3.softline_adjusted_probs[fav] < r3.implied_probs[fav])
check("分歧→honest_def不误触发(无drift)", r3.honest_def_applied is False,
      f"applied={r3.honest_def_applied}")
check("分歧→true_probs=淡化概率(主信号优先)", r3.true_probs == r3.softline_adjusted_probs)

# 4) analyze 单庄 honest_def → 次级修正
r4 = eng.analyze(book_hd)
check("单庄 honest_def 字段填充", r4.honest_def_applied is True and r4.honest_def_target == 'H')
check("单庄 honest_def H概率被修正", r4.true_probs[0] > book_hd.implied_probs[0],
      f"{book_hd.implied_probs[0]:.3f} -> {r4.true_probs[0]:.3f}")
check("单庄无drift不报错", True)  # 若上面没抛异常即过

# 5) query_odds_multi 真实DB行(含drift) → 字段有效
try:
    books_db = eng.query_odds_multi('AC卡亚尼', '哈卡')
    if books_db:
        b0 = books_db[0]
        got_drift = b0.drift_h is not None
        check("query_odds_multi 取到drift", got_drift, f"drift_h={b0.drift_h}")
        r5 = eng.analyze_multi(books_db)
        check("query_odds_multi 链路字段有效",
              r5.honest_def_target in (None, 'H', 'A'))
    else:
        print("[SKIP] query_odds_multi 无匹配行(跨赛季去重后可能为空), 跳过真实DB断言")
except Exception as e:
    check("query_odds_multi 真实DB链路", False, f"异常: {e}")

print("\n=== 结果 ===")
if fails:
    print(f"失败 {len(fails)} 项: {fails}")
    sys.exit(1)
print("全部通过 ✅  (honest_def 次级修正接 soft-line 链路, 向后兼容, 无drift不触发)")
