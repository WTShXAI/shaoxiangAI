r"""p3_handoff_drill.py — 哨响AI 交接演练脚本（P3-4）

目的: 让接手人**无口头指导**跑一条命令, 即知系统是否处于可交接(GO)状态。
覆盖 环境复现指南 §9 自检 + §11 交接清单的自动化部分。

默认(快, ~秒级): 环境 + 依赖 + 数据 + git + 模型冒烟 + bridge 健康(信息性)。
--deep: 额外跑 P0-10 walk-forward 复现(约 1 分钟), 验证"能力验证结论可重现"。

退出码: 0=核心项全过(服务可不在跑) / 2=有核心项失败。
用法(铁律前缀):
  D:\Architecture\.venv\Scripts\python.exe scripts\p3_handoff_drill.py [--deep]
"""
import os, sys, json, subprocess, urllib.request, shutil
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "reports")
DEEP = "--deep" in sys.argv

checks = []


def add(name, ok, detail, core=True):
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail), "core": core})


# 1) venv python 版本
ver = sys.version_info
add("venv Python 3.12.x", (ver.major, ver.minor) == (3, 12), f"{ver.major}.{ver.minor}.{ver.micro}")
add("运行于项目 venv", os.path.abspath(sys.executable).lower().startswith(os.path.join(ROOT, ".venv").lower()),
    os.path.abspath(sys.executable))

# 2) 关键依赖可 import
try:
    import sklearn, lightgbm, joblib, pandas, numpy, torch  # noqa
    add("核心依赖可 import (sklearn/lightgbm/joblib/pandas/numpy/torch)", True,
        f"sklearn={sklearn.__version__} lightgbm={lightgbm.__version__}")
except Exception as e:
    add("核心依赖可 import", False, f"import 失败: {e}")

# 3) 数据层核心库存在
for db in ["events.db", "GQ.db", "football_data.db", "indep_features_gq.db"]:
    p = os.path.join(DATA, db)
    add(f"数据层 {db} 存在", os.path.exists(p), f"{os.path.getsize(p)/1e9:.2f}GB" if os.path.exists(p) else "缺失")

# 4) git 远程/分支
try:
    rem = subprocess.run(["git", "-C", ROOT, "remote", "-v"], capture_output=True, text=True, timeout=20)
    br = subprocess.run(["git", "-C", ROOT, "branch", "--show-current"], capture_output=True, text=True, timeout=20)
    add("git 远程含 shaoxiangAI", "shaoxiangai" in rem.stdout.lower(), rem.stdout.strip().replace("\n", " | "))
    add("git 在 main 分支", br.stdout.strip() == "main", br.stdout.strip())
except Exception as e:
    add("git 信息可读", False, f"git 失败(可能 IPv6/未装): {e}")

# 5) 模型冒烟
try:
    sys.path.insert(0, ROOT)
    from pipeline import william_inter_model as wi  # noqa
    r = wi.predict_1x2(2.1, 3.3, 3.1, 1.9, 3.4, 3.6)
    proba = r.get("proba") if isinstance(r, dict) else None
    ok = isinstance(proba, (list, tuple)) and len(proba) == 3 and abs(sum(proba) - 1.0) < 0.05
    add("william_inter_model 冒烟", ok, f"proba={[round(x,3) for x in proba]}" if proba else str(r))
except Exception as e:
    add("william_inter_model 冒烟", False, f"异常: {e}")

# 6) bridge 健康(信息性, 服务可未起)
try:
    with urllib.request.urlopen("http://localhost:9000/health", timeout=5) as resp:
        txt = resp.read().decode("utf-8", "ignore")
    add("bridge /health 可达 (信息性)", "healthy" in txt.lower(), txt[:120], core=False)
except Exception as e:
    add("bridge /health 可达 (信息性)", False, f"未运行或不可达(正常: 未启动时) — {type(e).__name__}", core=False)

# 7) 数据健全性(信息性): GQ 有比分场 / events 快照新鲜度
try:
    import sqlite3
    gq = sqlite3.connect(f"file:{os.path.join(DATA,'GQ.db')}?mode=ro", uri=True, timeout=30)
    n = gq.execute("SELECT COUNT(*) FROM matches WHERE score_home IS NOT NULL").fetchone()[0]
    gq.close()
    add("GQ.db 有比分场 > 5000 (信息性)", n > 5000, f"{n} 场", core=False)
except Exception as e:
    add("GQ.db 有比分场 (信息性)", False, f"{e}", core=False)

# 8) --deep: P0-10 复现
if DEEP:
    try:
        r = subprocess.run([VENV_PY, os.path.join(ROOT, "scripts", "p0_10_retrain_independent_gq.py")],
                           capture_output=True, text=True, timeout=300)
        ok = "ROI=" in r.stdout and "+EV=False" in r.stdout
        add("P0-10 walk-forward 可复现 (结论=FAILED, 信息性)", ok,
            "见 reports/p0_10_retrain_status.md" if ok else r.stdout[-300:], core=False)
    except Exception as e:
        add("P0-10 复现 (信息性)", False, f"{e}", core=False)

# 汇总
core_fail = [c for c in checks if c["core"] and not c["ok"]]
print("=" * 64)
print("哨响AI 交接演练 (P3-4)  ", datetime.now(timezone.utc).astimezone().isoformat())
print("=" * 64)
for c in checks:
    tag = "✅" if c["ok"] else ("❌" if c["core"] else "⚠️")
    print(f"  {tag} [{ '核心' if c['core'] else '信息' }] {c['name']}")
    print(f"       {c['detail']}")
print("-" * 64)
if core_fail:
    print(f"结果: ❌ 有 {len(core_fail)} 项核心检查失败 → 未达 GO")
    print("       逐项修复后重跑本脚本。服务未运行属正常(见 环境复现指南 §7 启动)。")
    rc = 2
else:
    print("结果: ✅ 核心项全过 → 环境达 GO (服务未运行不影响此项判定)")
    print("       接手人按 环境复现指南 §7 启动服务后即完整可交接。")
    rc = 0

os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "p3_handoff_drill_status.json"), "w", encoding="utf-8") as f:
    json.dump({"generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
               "deep": DEEP, "core_fail": len(core_fail), "checks": checks}, f, ensure_ascii=False, indent=2)
print(f"-> {os.path.join(OUT,'p3_handoff_drill_status.json')}")
sys.exit(rc)
