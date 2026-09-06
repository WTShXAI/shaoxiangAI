"""Task A 验证 (事故④ 根治): persist 步骤抛异常时, live 翻页(live_flip) 步骤仍必定执行.

完全不触碰真实 events.db —— 所有 DB 写入/读取步骤用 spy / no-op 替换, 仅验证
core.collector_step.CollectorRound 的"单步失败不中断后续步骤"不变量.

运行: python scripts/verify_task_a_liveflip.py
退出码 0 = 通过; 非 0 = 失败.
"""
import sys
import traceback

sys.path.insert(0, r"D:/Architecture")

import gq.auto_collector as ac  # noqa: E402


def main() -> int:
    # 构造采集器
    col = ac.GQCollector()

    # 记录 live_flip 是否执行 (通过 spy 替换 _sweep_finished, 不碰真实 DB)
    state = {"live_flip_executed": 0}

    def spy_sweep_finished():
        # step_live_flip 唯一调用方就是 self._sweep_finished(), 故此处即 live 翻页执行点
        state["live_flip_executed"] += 1

    # 替换 DB 相关步骤为 no-op / spy, 杜绝真实写入
    col._sweep_finished = spy_sweep_finished
    col._freeze_scheduled_cs = lambda: None
    col._capture_prematch_conclusions = lambda: None

    # 伪造 fetch 列表 (绕过真实 GQ API)
    ac.fetch_match_list = lambda: [{"mid": "M_TEST", "mhn": "A队", "man": "B队"}]
    ac.fetch_match_odds = lambda mid: {"mid": mid}

    # ★ persist 步骤: record_match_odds 必抛异常 (模拟落库失败)
    def boom_record(decoded, it):
        raise RuntimeError("simulated persist failure")

    ac.record_match_odds = boom_record

    # 跑一轮
    n = col.collect_round()
    _ = n  # 返回值无关, 关键是逐步结果

    results = col._last_round_results
    persist = [r for r in results if r.step == "persist"]
    live = [r for r in results if r.step == "live_flip"]
    failed = [r.step for r in results if not r.ok]

    print("逐步结果失败步骤:", failed)
    print("live_flip 执行次数:", state["live_flip_executed"])

    assert persist, "应存在 persist 步骤"
    assert not persist[0].ok, "persist 步骤应记为失败 (抛异常)"
    assert live, "应存在 live_flip 步骤"
    assert live[0].ok, "live_flip 步骤必须成功执行 (即便 persist 失败)"
    assert state["live_flip_executed"] == 1, "live_flip 必须恰好执行一次"

    print("TASK_A_ASSERT_OK  (persist 抛异常 → live 翻页仍执行)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print("TASK_A_ASSERT_FAIL:", repr(exc))
        sys.exit(1)
