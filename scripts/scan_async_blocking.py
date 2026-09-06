# -*- coding: utf-8 -*-
"""静态扫描 bridge_service.py: 找出所有 async def 函数体内可能阻塞事件循环的同步调用.
用法: python _scan_async_blocking.py [bridge_service.py]
输出: 每个 async 函数命中危险模式的 行号+模式; 以及高危模式清单.
"""
import re, sys, os

SRC = sys.argv[1] if len(sys.argv) > 1 else r"D:\Architecture\bridge_service.py"
with open(SRC, encoding="utf-8", errors="replace") as f:
    lines = f.readlines()

# 危险模式 (行内匹配, 忽略注释行)
PATTERNS = [
    (r"\bsqlite3\.connect\(", "sqlite-connect"),
    (r"\brequests\.(get|post|put|delete|head)\(", "requests-http"),
    (r"\burllib\.request\b", "urllib-http"),
    (r"\bjoblib\.load\(", "joblib-load"),
    (r"\b(open|open)\(", "open-file"),
    (r"\bread_csv\b|\bread_sql\b|\bread_excel\b", "pandas-read"),
    (r"\bsubprocess\b|\bos\.system\b", "subprocess"),
    (r"\btime\.sleep\(", "time-sleep"),
    (r"\.predict\(", "predict-call"),
    (r"\.predict_proba\(", "predict-proba"),
    (r"query_by_odds\b|query_neighbors\b|match_similarity\b", "vector-lib"),
    (r"batch_confidence\b|ranked_predict\b|_live_predict\b|_ou_only_card\b", "heavy-fn"),
    (r"backfill_all\b|scan_live_matches\b|scan_by_league\b|run_scan\b", "scan-backfill"),
    (r"\.fit\(", "fit-call"),
    (r"pickle\.load\b|load_model\b", "model-load"),
    (r"llm\b|chat_completion|generate\b|ollama\b", "llm-call"),
    (r"for .* in .*fetchall\(\)|\.fetchall\(\)", "fetchall"),
]

# 提取函数: async def 开头, 到下一个顶层 def/class/@ 装饰器
funcs = []  # (start_line, end_line, name)
cur = None
for i, ln in enumerate(lines, 1):
    m = re.match(r"^async def (\w+)", ln)
    if m:
        cur = [i, None, m.group(1)]
        continue
    if cur and (re.match(r"^@app\.", ln) or re.match(r"^async def ", ln)
                or re.match(r"^def ", ln) or re.match(r"^class ", ln)):
        cur[1] = i - 1
        funcs.append(tuple(cur))
        cur = None
if cur:
    cur[1] = len(lines)
    funcs.append(tuple(cur))

print(f"total async funcs: {len(funcs)}")
print("=" * 90)

risk_total = 0
for start, end, name in funcs:
    hits = []
    for i in range(start, min(end, len(lines))):
        ln = lines[i - 1]
        stripped = ln.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        # 跳过纯注释/docstring 行
        for pat, label in PATTERNS:
            if re.search(pat, ln):
                hits.append((i, label, ln.strip()[:110]))
                break
    if hits:
        risk_total += 1
        print(f"\n### async def {name}  (L{start}-{end}, {end-start+1}行)")
        for ln_no, label, code in hits[:25]:
            print(f"  L{ln_no} [{label}] {code}")
        if len(hits) > 25:
            print(f"  ... 共 {len(hits)} 处")

print("=" * 90)
print(f"risk async funcs: {risk_total}/{len(funcs)}")
