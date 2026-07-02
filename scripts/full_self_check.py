#!/usr/bin/env python
"""哨响AI v5.2.14 全面自检"""
import os, sys, json, importlib.util, warnings, sqlite3
from collections import Counter
warnings.filterwarnings('ignore')

ROOT = 'D:/Architecture v4.0'
checks = []

def check(cat, name, ok, detail=''):
    checks.append((cat, name, ok, detail))

# ═══ 1-2. 模型 + 推理引擎 ═══
for name, path in {
    'v4.1 Stacking':'saved_models/football_v4.1_production.joblib',
    'DrawExpert v1':'saved_models/draw_expert_v1.joblib',
    'DE Scaler':'saved_models/draw_expert_scaler.joblib',
    'NN .pth':'saved_models/football_nn_20260616_125617.pth',
}.items():
    p = os.path.join(ROOT, path)
    ok = os.path.exists(p)
    sz = os.path.getsize(p)//1024 if ok else 0
    check('MODEL', name, ok, f'{sz}KB')

for name, path in {
    'UnifiedPredictor':'predictors/unified_predictor.py',
    'SKY Predictor':'predictors/sky/sky_predictor.py',
    'SixLayer引擎':'modules/six_layer_conversation.py',
    'EnsembleTrainer':'predictors/components/ensemble_trainer.py',
    'FeatureAligner':'features/feature_aligner.py',
}.items():
    check('ENGINE', name, os.path.exists(os.path.join(ROOT, path)))

# ═══ 3. 风控引擎 ═══
for name, path in {
    'DrawGate v5.3':'rules/drawgate_v53.py',
    'D-Gate v5.3':'rules/d_gate_utils.py',
    'D-Gate Engine':'rules/d_gate_engine.py',
    '操盘手陷阱':'bookmaker_sim/risk_barrier_engine.py',
    'Bayesian λ':'bookmaker_sim/margin_likelihood_bridge.py',
    'DrawGate规则':'config/drawgate_v53_rules.json',
}.items():
    check('RISK', name, os.path.exists(os.path.join(ROOT, path)))

# ═══ 4. 数据层 ═══
db_path = os.path.join(ROOT, 'data/football_data.db')
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    n_tables = len(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    mc = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    oc = conn.execute("SELECT COUNT(*) FROM odds").fetchone()[0]
    fc = conn.execute("SELECT COUNT(*) FROM match_features").fetchone()[0]
    conn.close()
    check('DATA', 'SQLite主库', True, f'{os.path.getsize(db_path)//(1024*1024)}MB {n_tables}表')
    check('DATA', 'matches/odds/features', True, f'{mc:,}/{oc:,}/{fc:,}')
else:
    check('DATA', 'SQLite主库', False, '缺失')

pq = os.path.join(ROOT, 'data/matches.parquet')
check('DATA', 'matches.parquet', os.path.exists(pq), f'{os.path.getsize(pq)//1024}KB' if os.path.exists(pq) else '')
check('DATA', 'Parquet同步器', os.path.exists(os.path.join(ROOT, 'data/parquet_syncer.py')))

api_dir = os.path.join(ROOT, 'data/wc2026_api')
api_files = ['results.json','matches.json','standings.json','odds.json','stats.json']
check('DATA', 'SportsAPI数据', all(os.path.exists(os.path.join(api_dir,f)) for f in api_files), f'{len(api_files)}文件')

# ═══ 5. 后端 ═══
for name, path in {
    'FastAPI主应用':'backend/main.py',
    '安全模块':'backend/core/security.py',
    '数据质量API':'backend/api/v1/endpoints/data_quality.py',
    '比赛API':'backend/api/v1/endpoints/matches.py',
}.items():
    check('API', name, os.path.exists(os.path.join(ROOT, path)))

# ═══ 6-7. 配置 + 安全 ═══
for name, path in {
    'settings.yaml':'config/settings.yaml',
    '.env':'.env','.gitignore':'.gitignore',
    'requirements.txt':'requirements.txt',
    'start_server.bat':'start_server.bat',
}.items():
    check('CONFIG', name, os.path.exists(os.path.join(ROOT, path)))

with open(os.path.join(ROOT, 'api/ocr.py'), encoding='utf-8') as f:
    c = f.read()
    check('SEC', 'OCR密钥去硬编码', 'AKLTN2FkMmY5' not in c and 'os.getenv' in c)
with open(os.path.join(ROOT, 'start_server.bat'), encoding='utf-8') as f:
    cb = f.read()
    check('SEC', 'SECRET_KEY移除', 'set SECRET_KEY' not in cb)
    check('SEC', '绑定127.0.0.1', '127.0.0.1' in cb)
with open(os.path.join(ROOT, 'backend/main.py'), encoding='utf-8') as f:
    check('SEC', 'CORS收紧', 'allow_methods=["GET", "POST"' in f.read())
with open(os.path.join(ROOT, 'backend/api/v1/endpoints/matches.py'), encoding='utf-8') as f:
    check('SEC', 'SSL恢复', 'verify=False' not in f.read())
with open(os.path.join(ROOT, '.env'), encoding='utf-8') as f:
    check('SEC', '.env OCR密钥', 'OCR_AK=' in f.read())

# ═══ 8. 依赖 ═══
for d in ['fastapi','uvicorn','lightgbm','xgboost','sklearn','numpy','pandas','torch','httpx','joblib','fastparquet','sqlalchemy']:
    try:
        __import__(d.replace('-','_'))
        check('DEP', d, True)
    except ImportError:
        check('DEP', d, False)

# ═══ 9. 采集器 ═══
check('FETCH', 'SportsAPI采集器', os.path.exists(os.path.join(ROOT, 'data_collector/sportsapi_wc2026.py')))

# ═══ 10. 模型可加载 ═══
import joblib as jl
try:
    m = jl.load(os.path.join(ROOT, 'saved_models/football_v4.1_production.joblib'))
    check('LOAD', 'v4.1可加载', True, f'{len(m)}keys')
except Exception as e:
    check('LOAD', 'v4.1加载', False, str(e)[:50])
try:
    jl.load(os.path.join(ROOT, 'saved_models/draw_expert_v1.joblib'))
    check('LOAD', 'DrawExpert可加载', True)
except Exception as e:
    check('LOAD', 'DrawExpert加载', False)

# ═══ 11. DrawExpert实时推理 ═══
try:
    from predictors.components.ensemble_trainer import EnsembleTrainer
    trainer = EnsembleTrainer.load_pipeline(os.path.join(ROOT, 'saved_models/football_v4.1_production.joblib'))
    spec = importlib.util.spec_from_file_location('fa', os.path.join(ROOT, 'features/feature_aligner.py'))
    fa_mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(fa_mod)
    aligner = fa_mod.FeatureAligner.from_trainer(trainer)
    v = aligner.build(home='Germany', away='Ecuador', oh=1.36, od=5.50, oa=7.00, asian_handicap=-1.25)
    de = trainer.draw_expert_model.model.predict_proba(v.reshape(1,-1))
    check('INFER', 'DrawExpert实时推理', True, f'P(draw)={float(de[0,1]):.3f}')
except Exception as e:
    check('INFER', 'DrawExpert推理', False, str(e)[:60])

# ═══ 12. DrawGate ═══
try:
    from rules.drawgate_v53 import apply_drawgate, imp_from_odds
    imp_h, imp_d, imp_a = imp_from_odds(1.36, 5.50, 7.00)
    dg = apply_drawgate(imp_h, imp_d, imp_a, {'home':1.36,'draw':5.50,'away':7.00}, match_type='tournament')
    check('INFER', 'DrawGate v5.3加载', True, f'mode={dg["dgate_mode"]} tag={dg["risk_tag"]}')
except Exception as e:
    check('INFER', 'DrawGate加载', False, str(e)[:60])

# ═══ 打印 ═══
print('=== 哨响AI v5.2.14 全面自检 ===')
print()
for cat, name, ok, detail in checks:
    mark = '✅' if ok else '❌'
    d = f' ({detail})' if detail else ''
    print(f'  {mark} [{cat:<5s}] {name:<25s}{d}')

total = len(checks)
passed = sum(1 for _, _, ok, _ in checks if ok)
print()
print(f'  {"─"*48}')
print(f'  总计: {total} | 通过: {passed} | 失败: {total-passed} | {passed/total*100:.0f}%')
if total - passed == 0:
    print(f'  🟢 全系统就绪, 可上线')
elif total - passed <= 3:
    print(f'  🟡 {total-passed}项待处理, 可降级上线')
else:
    print(f'  🔴 {total-passed}项阻塞')
