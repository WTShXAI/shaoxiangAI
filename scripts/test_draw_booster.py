#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G5 · consensus booster 接 draw_alert 单元测试 (trust but verify).

覆盖:
  - 单庄/WC无IW (consensus=None / strong=False) → 回退 m_pd>=0.26
  - 双庄共识 strong 且 m_pd 在 [0.24,0.26) → booster 触发 (阈值降到0.24)
  - 双庄共识 strong 且 m_pd<0.24 → 不触发
  - 双庄共识 strong 且 m_pd>=0.26 → 仍触发(基础)
"""
import sys
sys.path.insert(0, "D:/Architecture")

from pipeline.draw_signal import draw_alert_with_booster

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}")


def run():
    # 1) 无共识 → 回退基础阈值 0.26
    check("consensus=None, m_pd=0.25 → 不触发", draw_alert_with_booster(0.25, None) is False)
    check("consensus=None, m_pd=0.27 → 触发", draw_alert_with_booster(0.27, None) is True)

    # 2) 共识可用但非 strong (如 WC 无 IW) → 回退基础阈值
    weak = {"strong": False, "available": False}
    check("weak共识, m_pd=0.25 → 不触发(回退)", draw_alert_with_booster(0.25, weak) is False)
    check("weak共识, m_pd=0.27 → 触发", draw_alert_with_booster(0.27, weak) is True)

    # 3) 双庄共识 strong, m_pd 落在 [0.24,0.26) → booster 触发 (阈值降到0.24)
    strong = {"strong": True, "available": True, "consensus": 0.31}
    check("strong共识, m_pd=0.25 → booster触发(0.24阈值)", draw_alert_with_booster(0.25, strong) is True)
    check("strong共识, m_pd=0.24 → booster触发(边界)", draw_alert_with_booster(0.24, strong) is True)

    # 4) strong 但 m_pd<0.24 → 不触发
    check("strong共识, m_pd=0.23 → 不触发", draw_alert_with_booster(0.23, strong) is False)

    # 5) strong 且 m_pd>=0.26 → 仍触发(基础路径)
    check("strong共识, m_pd=0.30 → 触发(基础)", draw_alert_with_booster(0.30, strong) is True)

    print(f"\n[G5 test] PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(run())
