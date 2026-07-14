import sys
sys.path.insert(0, r'D:\Architecture')
ok = True
try:
    from pipeline.engine import create_engine, _ENGINE_REGISTRY
    print("pipeline.engine import OK")
    print("registry keys:", list(_ENGINE_REGISTRY.keys()) if isinstance(_ENGINE_REGISTRY, dict) else type(_ENGINE_REGISTRY))
except Exception as e:
    ok = False
    print("pipeline.engine import FAIL:", repr(e))

try:
    from pipeline.predictors.data_classes import MatchInput
    print("MatchInput import OK")
except Exception as e:
    ok = False
    print("MatchInput import FAIL:", repr(e))

if ok:
    try:
        e = create_engine("wc")
        print("create_engine('wc') OK ->", e.description)
    except Exception as e:
        print("create_engine('wc') FAIL:", repr(e))
    try:
        e2 = create_engine("league")
        print("create_engine('league') OK ->", e2.description)
    except Exception as e:
        print("create_engine('league') FAIL:", repr(e))
