"""
B1 bridge 端点使用率只读审计（仅报告，不删不改任何代码/DB/进程）。

- 解析 bridge_service.py 的所有路由装饰器，提取端点路径与方法。
- 对每个端点，取其"最深静态前缀"（去掉首个 `{param}` 及其后段），
  在 frontend/ 目录 grep 该前缀的引用（含 /api 前缀、去前缀、路径片段三种变体）。
- 标记"零调用候选"（前端无任何引用），供人工复核是否可废弃（脚本不删）。

方法论注意：
- 模板字符串端点（如 `/api/leagues/${key}/fixtures`）按最深静态前缀 `/api/leagues`
  探测，避免把"参数化子路径"误判为零调用。
- 根路径 `/` 与通配 `/{full_path:path}` 非业务端点，从候选清单中排除，仅作信息统计。
- 这是"候选"清单，最终废弃决策须人工复核（可能仍有内部路由/定时任务调用）。

红线：只读。不写 events.db、不杀进程、不跑 schema 变更。
"""
import argparse
import json
import os
import re
from collections import OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(ROOT, "bridge_service.py")
FRONTEND = os.path.join(ROOT, "frontend")
REPORT_JSON = os.path.join(ROOT, "reports", "audit_bridge_endpoint_usage.json")
REPORT_MD = os.path.join(ROOT, "reports", "audit_bridge_endpoint_usage.md")

DECO_RE = re.compile(
    r'@app\.(get|post|put|delete)\(\s*["\']([^"\']+)["\']', re.IGNORECASE
)


def extract_endpoints():
    with open(BRIDGE, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    eps = OrderedDict()
    for m in DECO_RE.finditer(text):
        method, path = m.group(1).upper(), m.group(2)
        # 去掉首个参数化段及其后所有内容，得到最深静态前缀
        static = re.split(r"/\{", path)[0]
        eps.setdefault(path, {"methods": set(), "static_prefix": static})
        eps[path]["methods"].add(method)
    return [(p, sorted(v["methods"]), v["static_prefix"]) for p, v in eps.items()]


def count_refs(prefix):
    """前端对最深静态前缀的引用次数（含 /api 前缀、去前缀、路径片段三种变体）。"""
    variants = {prefix, prefix.lstrip("/")}
    if prefix.startswith("/api/"):
        variants.add(prefix[4:].lstrip("/"))
    total = 0
    matched_files = set()
    for variant in variants:
        if not variant or variant in ("/", "api"):
            continue
        pat = re.compile(re.escape(variant))
        for dirpath, _, files in os.walk(FRONTEND):
            if "node_modules" in dirpath or ".git" in dirpath:
                continue
            for fn in files:
                if not fn.endswith((".ts", ".tsx", ".js", ".jsx", ".vue", ".json")):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception:
                    continue
                c = len(pat.findall(content))
                if c:
                    total += c
                    matched_files.add(os.path.relpath(fp, ROOT))
    return total, sorted(matched_files)


def main():
    eps = extract_endpoints()
    rows = []
    for path, methods, static in eps:
        # 排除根路径与通配（非业务端点）
        if path in ("/", "") or path.startswith("/{") or static in ("", "/"):
            rows.append(
                {
                    "path": path,
                    "methods": methods,
                    "static_prefix": static,
                    "ref_count": None,
                    "ref_files": [],
                    "category": "infra",  # 基础设施，不参与候选
                }
            )
            continue
        refs, files = count_refs(static)
        rows.append(
            {
                "path": path,
                "methods": methods,
                "static_prefix": static,
                "ref_count": refs,
                "ref_files": files,
                "zero_candidate": refs == 0,
                "category": "api",
            }
        )
    api_rows = [r for r in rows if r["category"] == "api"]
    zero = [r for r in api_rows if r.get("zero_candidate")]
    used = [r for r in api_rows if not r.get("zero_candidate")]
    infra = [r for r in rows if r["category"] == "infra"]

    report = {
        "total_api_endpoints": len(api_rows),
        "zero_call_candidates": len(zero),
        "used_endpoints": len(used),
        "infra_paths_excluded": len(infra),
        "endpoints": rows,
    }
    os.makedirs(os.path.dirname(REPORT_JSON), exist_ok=True)
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    lines = []
    lines.append("# B1 Bridge 端点使用率只读审计\n")
    lines.append(f"- 端点总数(业务): {len(api_rows)}｜零调用候选: {len(zero)}｜"
                 f"使用中: {len(used)}｜基础设施(已排除): {len(infra)}")
    lines.append("\n> 方法：取每端点最深静态前缀，在 frontend/ grep 引用。"
                 "模板字符串端点按前缀探测，避免误判。此为**候选清单**，"
                 "最终废弃须人工复核（可能仍有内部路由/定时任务调用）。\n")
    lines.append("## 零调用候选（前端无引用，待人工复核是否可废弃）\n")
    if zero:
        for r in zero:
            lines.append(f"- `{r['path']}` [{','.join(r['methods'])}] "
                         f"(probe=`{r['static_prefix']}`)")
    else:
        lines.append("（无）")
    lines.append("\n## 使用中端点（前端有引用，按引用降序）\n")
    for r in sorted(used, key=lambda x: -x["ref_count"]):
        lines.append(
            f"- `{r['path']}` [{','.join(r['methods'])}] refs={r['ref_count']} "
            f"files={len(r['ref_files'])}"
        )
    lines.append("\n## 已排除的基础设施路径（非业务端点）\n")
    for r in infra:
        lines.append(f"- `{r['path']}` [{','.join(r['methods'])}]")
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"api={len(api_rows)} zero={len(zero)} used={len(used)} infra={len(infra)}")
    print(f"reports: {os.path.basename(REPORT_JSON)}, {os.path.basename(REPORT_MD)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.parse_args()
    main()
