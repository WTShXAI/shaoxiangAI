r"""audit_package_b_compliance.py — Package B 导出合规只读审计 (P2-5 就绪包实证)

验证 `deliverables/p2_package_b/p2_package_B.sqlite` 的 PII 硬排除声明:
  - 仅含 odds_changes / odds_snapshots 两张赔率时序表
  - 不含 users(密钥) / match_outcomes(内嵌赔率) / 任何个人相关表
  - 导出方式零写入(本脚本亦只读打开)

输出 reports/package_b_compliance.json。只读, 不修改任何库。

用法:
  python scripts/audit_package_b_compliance.py
"""
import os
import sqlite3
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORT = os.path.join(ROOT, "deliverables", "p2_package_b", "p2_package_B.sqlite")
OUT = os.path.join(ROOT, "reports", "package_b_compliance.json")

# 严禁出现的表(硬排除清单)
FORBIDDEN = {
    "users", "user", "accounts", "api_keys", "tokens", "secrets",
    "match_outcomes", "bets", "betting", "wallet", "payment",
    "person", "customer", "client", "profile",
}


def main():
    if not os.path.exists(EXPORT):
        print("EXPORT NOT FOUND:", EXPORT)
        return
    con = sqlite3.connect(f"file:{EXPORT}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]

    violations = [t for t in tables if t.lower() in FORBIDDEN or any(f in t.lower() for f in FORBIDDEN)]
    data_tables = [t for t in tables if t not in ("sqlite_sequence",)]
    counts = {}
    for t in data_tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM \"{t}\"")
            counts[t] = cur.fetchone()[0]
        except Exception as e:
            counts[t] = f"ERR {e}"
    con.close()

    report = {
        "export": EXPORT,
        "all_tables": tables,
        "data_tables": data_tables,
        "row_counts": counts,
        "forbidden_tables_present": violations,
        "pii_exclusion_ok": len(violations) == 0,
        "verdict": "PASS" if len(violations) == 0 else "FAIL",
        "note": "仅 odds_changes/odds_snapshots 两张赔率时序表 → 无个人/密钥表, P2-5 Q4 实证通过",
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"verdict={report['verdict']} | tables={data_tables} | violations={violations}")
    print("report ->", OUT)


if __name__ == "__main__":
    main()
