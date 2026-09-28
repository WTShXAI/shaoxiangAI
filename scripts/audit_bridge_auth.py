"""
scripts/audit_bridge_auth.py — bridge_service.py 端点鉴权只读审计
========================================================================
对照点：
  - bridge_service.py::api_key_middleware (H4 2026-07-30) —— 唯一鉴权层
  - tests/test_bridge_health_security.py —— 既有安全守卫（仅测 /health 结构）

审计目标（仅报告不修）：
  1. 枚举全部 HTTP/WebSocket 路由，分类 读/写。
  2. 对每个写类端点判定鉴权姿态：
     - api_key_middleware 仅当 API_KEY 环境变量非空时启用（默认关闭），
       且仅覆盖 path.startswith("/api") 且 method∈{POST,PUT,DELETE,PATCH}。
     - 非 /api 前缀的写端点（/predict*）即使设了 API_KEY 也不被覆盖 → UNGUARDED。
     - 写类 WebSocket（/ws/odds_ingest）无 HTTP 方法鉴权机制 → UNGUARDED。
  3. 检测写类端点函数体内「异常吞没」模式：except 块返回
     success:True / status_code=200（而非 200→错误信封 / 重新抛出）。
  4. 对照既有守卫：test_bridge_health_security.py 未断言 401 行为 → 守卫缺口。

红线：只读解析源码（不 import bridge_service，避免启动 app / 触碰 events.db /
不杀进程 / 不碰生产服务）。仅产出 reports/audit_bridge_auth.{json,md}。

用法：
  python scripts/audit_bridge_auth.py                # 默认打 bridge_service.py
  python scripts/audit_bridge_auth.py --bridge X.py --out reports/audit_bridge_auth
"""
import argparse
import ast
import json
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def parse_endpoints(source_text):
    """AST 解析全部路由定义 → [(method, path, line, func_name)]。

    只识别 @app.<verb>("<path>") / @app.websocket("<path>") 装饰器。
    """
    tree = ast.parse(source_text)
    endpoints = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            method, path = _extract_route(dec)
            if method and path:
                endpoints.append((method.upper(), path, node.lineno, node.name))
    return endpoints


def _extract_route(dec):
    """从装饰器节点提取 (method, path)。"""
    # @app.get("/x")  或  @app.websocket("/ws")
    if not isinstance(dec, ast.Call):
        return None, None
    func = dec.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None, None
    if func.value.id != "app":
        return None, None
    verb = func.attr.lower()
    method_map = {
        "get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE",
        "patch": "PATCH", "head": "HEAD", "options": "OPTIONS",
        "websocket": "WEBSOCKET",
    }
    if verb not in method_map:
        return None, None
    method = method_map[verb]
    if not dec.args:
        return None, None
    p = dec.args[0]
    if isinstance(p, ast.Constant) and isinstance(p.value, str):
        return method, p.value
    return None, None


def classify_auth(method, path):
    """返回鉴权姿态分类。"""
    if method == "WEBSOCKET":
        if "ingest" in path:
            return "UNGUARDED_no_auth_mechanism"  # 写类 WS，无 HTTP 方法鉴权
        return "n/a_read_websocket"              # 广播/只读 WS
    if method in WRITE_METHODS:
        if path.startswith("/api"):
            # 仅当 API_KEY 非空时 api_key_middleware 才拦截；默认关闭
            return "CONDITIONAL_api_key_middleware"
        return "UNGUARDED_prefix_not_api"        # 非 /api 前缀，即使设 API_KEY 也不覆盖
    return "n/a_read"


def detect_swallow(func_node, source_lines):
    """检测写类端点函数体内 except 块返回 success:True / status_code=200 的吞没模式。"""
    findings = []
    for handler in ast.walk(func_node):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        for stmt in handler.body:
            ret = _find_return_success(stmt)
            if ret is not None:
                findings.append({"line": ret, "pattern": "except_returns_success_or_200"})
    return findings


def _find_return_success(stmt):
    """若 stmt 是直接 return 一个 success:True / status_code=200 的值，返回行号。"""
    if not isinstance(stmt, ast.Return) or stmt.value is None:
        return None
    val = stmt.value
    # dict 字面量含 "success": True
    if isinstance(val, ast.Dict):
        for k, v in zip(val.keys, val.values):
            if (isinstance(k, ast.Constant) and k.value == "success"
                    and isinstance(v, ast.Constant) and v.value is True):
                return stmt.lineno
    # Call(..., status_code=200, ...)  或含 success=True 关键字
    if isinstance(val, ast.Call):
        for kw in val.keywords:
            if kw.arg == "status_code" and isinstance(kw.value, ast.Constant) and kw.value.value == 200:
                # 仅当同时返回 success（JSONResponse(content={"success": True...})）才算吞没
                if _call_has_success_true(val):
                    return stmt.lineno
            if kw.arg == "content" and _dict_has_success_true(kw.value):
                return stmt.lineno
    return None


def _call_has_success_true(call_node):
    for kw in call_node.keywords:
        if kw.arg == "content" and _dict_has_success_true(kw.value):
            return True
    return False


def _dict_has_success_true(node):
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "success"
                    and isinstance(v, ast.Constant) and v.value is True):
                return True
    return False


def run_audit(bridge_path):
    with open(bridge_path, "r", encoding="utf-8") as f:
        src = f.read()
    source_lines = src.splitlines()
    endpoints = parse_endpoints(src)

    write_endpoints = []
    swallow_candidates = []
    for method, path, line, name in endpoints:
        posture = classify_auth(method, path)
        is_write = (method in WRITE_METHODS) or (
            method == "WEBSOCKET" and "ingest" in path)
        entry = {
            "method": method,
            "path": path,
            "line": line,
            "func": name,
            "auth_posture": posture,
            "is_write": is_write,
        }
        if is_write:
            write_endpoints.append(entry)
            # 异常吞没检测：解析该函数节点
            tree = ast.parse(src)
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.lineno == line:
                    sw = detect_swallow(fn, source_lines)
                    if sw:
                        swallow_candidates.append({
                            "method": method, "path": path, "line": line,
                            "func": name, "swallow": sw,
                        })
                    break

    unauthenticated = [e for e in write_endpoints
                       if e["auth_posture"].startswith("UNGUARDED")]

    # 对照既有守卫：test_bridge_health_security.py 是否断言 401
    guard_path = os.path.join(PROJECT_ROOT, "tests", "test_bridge_health_security.py")
    guard_has_auth_assert = False
    if os.path.exists(guard_path):
        gsrc = open(guard_path, "r", encoding="utf-8").read()
        guard_has_auth_assert = ("401" in gsrc) or ("unauthorized" in gsrc.lower()) \
            or ("X-API-Key" in gsrc)

    summary = {
        "total_endpoints": len(endpoints),
        "write_endpoints": len(write_endpoints),
        "read_endpoints": len(endpoints) - len(write_endpoints),
        "unauthenticated_candidates": len(unauthenticated),
        "swallow_candidates": len(swallow_candidates),
        "guard_covers_auth": guard_has_auth_assert,
        "auth_model": {
            "layer": "api_key_middleware(H4 2026-07-30)",
            "activation": "仅当 API_KEY 环境变量非空时启用；默认关闭(单机私有兼容)",
            "scope": "path 以 /api 开头 且 method∈{POST,PUT,DELETE,PATCH}",
        },
    }

    return {
        "bridge_path": os.path.abspath(bridge_path),
        "summary": summary,
        "write_endpoints": write_endpoints,
        "unauthenticated_candidates": unauthenticated,
        "swallow_candidates": swallow_candidates,
        "guard_coverage": {
            "guard_file": "tests/test_bridge_health_security.py",
            "asserts_auth_401": guard_has_auth_assert,
            "note": "既有守卫仅测 /health 结构，未断言写端点 401 行为 → 鉴权缺口无回归保护",
        },
    }


def main():
    ap = argparse.ArgumentParser(description="bridge 端点鉴权只读审计")
    ap.add_argument("--bridge", default=os.path.join(PROJECT_ROOT, "bridge_service.py"))
    ap.add_argument("--out", default=os.path.join(PROJECT_ROOT, "reports", "audit_bridge_auth"))
    args = ap.parse_args()

    result = run_audit(args.bridge)

    out_base = args.out
    os.makedirs(os.path.dirname(out_base), exist_ok=True)
    with open(out_base + ".json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # Markdown 报告
    s = result["summary"]
    lines = []
    lines.append("# bridge_service 端点鉴权只读审计报告\n")
    lines.append(f"- 源文件: `{result['bridge_path']}`")
    lines.append(f"- 总端点: {s['total_endpoints']} | 写类: {s['write_endpoints']} | 读类: {s['read_endpoints']}")
    lines.append(f"- 未加鉴权候选(即使设 API_KEY 也不覆盖): **{s['unauthenticated_candidates']}**")
    lines.append(f"- 异常吞没候选: {s['swallow_candidates']}")
    lines.append(f"- 鉴权模型: `{s['auth_model']['layer']}` | 激活: {s['auth_model']['activation']}")
    lines.append(f"- 既有守卫断言 401: {'是' if result['guard_coverage']['asserts_auth_401'] else '否'} "
                 f"({result['guard_coverage']['note']})\n")

    lines.append("## 写类端点鉴权姿态\n")
    lines.append("| method | path | line | auth_posture |")
    lines.append("|--------|------|------|--------------|")
    for e in result["write_endpoints"]:
        lines.append(f"| {e['method']} | {e['path']} | {e['line']} | {e['auth_posture']} |")

    lines.append("\n## 未加鉴权候选 (UNGUARDED)\n")
    if result["unauthenticated_candidates"]:
        for e in result["unauthenticated_candidates"]:
            lines.append(f"- `{e['method']} {e['path']}` (line {e['line']}, {e['func']}) — {e['auth_posture']}")
    else:
        lines.append("- 无")

    lines.append("\n## 异常吞没候选 (except→success/200)\n")
    if result["swallow_candidates"]:
        for c in result["swallow_candidates"]:
            sw = ", ".join(str(x["line"]) for x in c["swallow"])
            lines.append(f"- `{c['method']} {c['path']}` (line {c['line']}, {c['func']}) 吞没点位于 {sw}")
    else:
        lines.append("- 无（写端点均正确返回错误信封 / 重新抛出）")

    lines.append("\n## 结论与建议\n")
    lines.append("- 所有写类端点默认均无鉴权（API_KEY 默认空，单机私有部署设计如此）。")
    lines.append("- 真实缺口：非 /api 前缀写端点（/predict, /predict/simple, /predict/single）即使启用 API_KEY 也不被 api_key_middleware 覆盖 —— 中间件前缀判断漏匹配，建议改为「若非明确公开读端点则写必鉴权」。")
    lines.append("- /ws/odds_ingest 为写类 WebSocket，无任何鉴权机制；属采集器→bridge 内部通道，建议在部署层(防火墙/绑定 localhost)限制暴露面。")
    lines.append("- 既有安全守卫未断言 401 行为，鉴权策略无回归保护；如需收紧，应补 test 断言 + 将 API_KEY 纳入部署默认。")
    lines.append("\n> 本报告仅只读审计，未修改 bridge_service.py / events.db / 任何生产服务。")

    with open(out_base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[audit_bridge_auth] 端点={s['total_endpoints']} 写={s['write_endpoints']} "
          f"未鉴权候选={s['unauthenticated_candidates']} 吞没={s['swallow_candidates']}")
    print(f"[audit_bridge_auth] 报告: {out_base}.json / {out_base}.md")
    return result


if __name__ == "__main__":
    main()
