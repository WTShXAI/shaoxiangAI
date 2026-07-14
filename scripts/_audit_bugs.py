# -*- coding: utf-8 -*-
"""Root-cause bug audit v2: clean structured output to JSON file.
Suppresses noisy INFO logging + import side-effects from flooding stdout.
"""
import importlib, os, sys, sqlite3, json, logging

logging.disable(logging.CRITICAL)  # kill INFO backtest spam from imports
PROJ = r'D:\Architecture'
OUT = r'D:\Architecture\logs\audit_result.json'
sys.path.insert(0, PROJ)

result = {"import_walk": {"ok": [], "fail": []},
          "service_modules": {},
          "engines": {},
          "league_data": {}}

# ---------- 1) import walk ----------
import pipeline
pkgdir = os.path.dirname(pipeline.__file__)
for root, dirs, files in os.walk(pkgdir):
    for f in files:
        if not f.endswith('.py') or f.startswith('_'):
            continue
        rel = os.path.relpath(os.path.join(root, f), pkgdir)
        mod = 'pipeline.' + rel[:-3].replace(os.sep, '.')
        try:
            importlib.import_module(mod)
            result["import_walk"]["ok"].append(mod)
        except Exception as e:
            result["import_walk"]["fail"].append({"module": mod, "error": "%s: %s" % (type(e).__name__, e)})

# ---------- 2) top-level service modules ----------
for name in ['bridge_service', 'start_backend', 'app', 'main', 'api']:
    fp = os.path.join(PROJ, name + '.py')
    if not os.path.exists(fp):
        result["service_modules"][name] = "absent"
        continue
    try:
        importlib.import_module(name)
        result["service_modules"][name] = "ok"
    except Exception as e:
        result["service_modules"][name] = "%s: %s" % (type(e).__name__, e)

# ---------- 3) engine registry ----------
try:
    from pipeline.engine import create_engine
    for kind in ('wc', 'league'):
        try:
            eng = create_engine(kind)
            result["engines"][kind] = "ok:%s" % getattr(eng, 'name', eng.__class__.__name__)
        except Exception as e:
            result["engines"][kind] = "FAIL %s: %s" % (type(e).__name__, e)
except Exception as e:
    result["engines"]["_import"] = "FAIL %s" % e

# ---------- 4) five-major-league real data ----------
DB = r'D:\Architecture\data\football_data.db'
con = sqlite3.connect(DB); cur = con.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
result["league_data"]["tables"] = tables

# william_ht league tags
if 'william_ht' in tables:
    cur.execute("SELECT name FROM pragma_table_info('william_ht')")
    cols = [r[0] for r in cur.fetchall()]
    lc = [c for c in cols if 'league' in c.lower()]
    result["league_data"]["william_ht_cols"] = cols
    result["league_data"]["william_ht_league_col"] = lc
    if lc:
        cur.execute("SELECT COUNT(*) FROM william_ht")
        result["league_data"]["william_ht_total"] = cur.fetchone()[0]
        # search for the 5 major leagues by keyword
        for kw in ['英超', '西甲', '意甲', '德甲', '法甲', '超级', 'Premier', 'La Liga', 'Serie A', 'Bundesliga', 'Ligue 1']:
            cur.execute("SELECT COUNT(*) FROM william_ht WHERE %s LIKE ?" % lc[0], ('%' + kw + '%',))
            n = cur.fetchone()[0]
            if n:
                result["league_data"]["kw_%s" % kw] = n

# odds table provider coverage
if 'odds' in tables:
    cur.execute("SELECT provider, COUNT(*) FROM odds GROUP BY provider")
    result["league_data"]["odds_providers"] = {p: c for p, c in cur.fetchall()}

# ---------- 5) detect import side-effect: any module running backtest on import ----------
# (heuristic: check if a known backtest string appears -- already silenced, skip)

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print("WROTE", OUT)
print("import OK=%d FAIL=%d" % (len(result["import_walk"]["ok"]), len(result["import_walk"]["fail"])))
for x in result["import_walk"]["fail"]:
    print("  FAIL", x["module"], "->", x["error"])
print("engines:", result["engines"])
print("service:", result["service_modules"])
