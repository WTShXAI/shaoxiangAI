#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""薄包装: 以 500 金币运行权威全链路回测(full_chain_backtest, 单庄分析内容仿真)."""
import importlib.util, sys, os
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for p in (_ROOT, os.path.join(_ROOT, "scripts"), os.path.join(_ROOT, "pipeline")):
    if p not in sys.path:
        sys.path.insert(0, p)
spec = importlib.util.spec_from_file_location("fcb", os.path.join(_ROOT, "pipeline", "evaluation", "full_chain_backtest.py"))
fcb = importlib.util.module_from_spec(spec)
sys.modules["fcb"] = fcb
spec.loader.exec_module(fcb)
fcb.INIT_BANKROLL = 500.0
fcb.main()
print("\n[FCB500] DONE ->", fcb.OUT_JSON)
