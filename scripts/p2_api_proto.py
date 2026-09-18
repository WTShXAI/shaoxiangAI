"""P2-6 对外数据 API 原型 — 只读 JSON 接口(无新依赖, stdlib http.server)

安全边界:
  - 仅放行 P2-1 目录中 非INTERNAL 且非 users 的表(白名单)
  - 仅允许 SELECT; 拒绝含 ';' / 写关键词(INSERT/UPDATE/DELETE/DROP/ALTER/ATTACH) 的请求
  - 只读 uri mode=ro 打开

端点:
  GET /health
  GET /tables                       列出可售表(库.表 + 行数 + 价值)
  GET /query?db=&table=&limit=10    安全查询(白名单表, 仅 SELECT, 默认 LIMIT 10)

用法:
  .venv/Scripts/python.exe scripts/p2_api_proto.py --port 9100
"""
import argparse, json, os, sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAT = os.path.join(ROOT, "reports", "p2_asset_catalog.json")
DB_MAP = {
    "events": "data/events.db", "gq": "data/GQ.db",
    "football_data": "data/football_data.db",
    "rollball_train": "data/rollball_training.db",
    "leisu_odds": "data/leisu_odds.db",
}
WRITE_KW = ("insert", "update", "delete", "drop", "alter", "attach",
            "create", "replace", "pragma", "vacuum", "exec")

# 白名单: 非INTERNAL 且非 users
ALLOW = {}
for a in json.load(open(CAT, encoding="utf-8"))["assets"]:
    if a["category"] == "INTERNAL":
        continue
    if a["db"] == "football_data" and a["table"] == "users":
        continue
    ALLOW[f"{a['db']}.{a['table']}"] = a


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            return self._send(200, {"status": "ok", "tables_allowed": len(ALLOW)})
        if u.path == "/tables":
            return self._send(200, {"count": len(ALLOW),
                                    "tables": [{"name": k, "rows": v["rows"],
                                                "category": v["category"]}
                                               for k, v in ALLOW.items()]})
        if u.path == "/query":
            return self._handle_query(q)
        return self._send(404, {"error": "not_found", "path": u.path})

    def _handle_query(self, q):
        key = f"{q.get('db',[''])[0]}.{q.get('table',[''])[0]}"
        if key not in ALLOW:
            return self._send(403, {"error": "table_not_allowed", "table": key,
                                     "hint": "见 /tables"})
        try:
            limit = min(int(q.get("limit", ["10"])[0]), 1000)
        except ValueError:
            return self._send(400, {"error": "bad_limit"})
        db = q.get("db", [""])[0]
        if db not in DB_MAP:
            return self._send(400, {"error": "bad_db"})
        tbl = q.get("table", [""])[0]
        sql = f'SELECT * FROM "{tbl}" LIMIT {limit}'
        # 双重保险: 拒绝任何写意图
        low = sql.lower()
        if ";" in sql or any(w in low for w in WRITE_KW):
            return self._send(403, {"error": "write_rejected"})
        try:
            c = ro(os.path.join(ROOT, DB_MAP[db]))
            rows = c.execute(sql).fetchall()
            cols = [d[0] for d in c.execute(f'PRAGMA table_info("{tbl}")').fetchall()]
            c.close()
            out = [dict(zip(cols, r)) for r in rows]
            return self._send(200, {"table": key, "returned": len(out), "rows": out})
        except Exception as e:
            return self._send(500, {"error": str(e)[:160]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9100)
    args = ap.parse_args()
    srv = HTTPServer(("127.0.0.1", args.port), H)
    print(f"[p2_api_proto] 只读API @ http://127.0.0.1:{args.port} | 白名单表={len(ALLOW)}")
    print("  端点: /health  /tables  /query?db=&table=&limit=")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
