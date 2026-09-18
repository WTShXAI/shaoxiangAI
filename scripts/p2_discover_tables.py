"""P2-1 发现扫描：枚举各主库全部表 + 敏感列探测 + 轻量行数

只读(uri mode=ro)。慢表(odds_snapshots/odds_changes)行数跳过,标注 large。
输出到 stdout,供构建分类映射。
"""
import os, sqlite3
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBS = [
    ("events",         "data/events.db"),
    ("gq",             "data/GQ.db"),
    ("football_data",  "data/football_data.db"),
    ("rollball_train", "data/rollball_training.db"),
    ("hist_feature",   "data/hist_feature_matrix.db"),
    ("leisu_odds",     "data/leisu_odds.db"),
]
SLOW = {("events", "odds_snapshots"), ("events", "odds_changes")}
SENSITIVE = ("token", "secret", "key", "password", "cuid", "cookie", "auth")

def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)

def cols(cur, t):
    try:
        return [r[1] for r in cur.execute(f'PRAGMA table_info("{t}")').fetchall()]
    except Exception:
        return []

def main():
    for name, rel in DBS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"\n## {name}  [{rel}]  — 缺失")
            continue
        sz = os.path.getsize(path)
        print(f"\n## {name}  [{rel}]  ({sz/1024/1024:.1f} MB)")
        try:
            c = ro(path); cur = c.cursor()
            tabs = [r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
            for t in tabs:
                if (name, t) in SLOW:
                    n = "LARGE(slow)"
                else:
                    try:
                        n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    except Exception as e:
                        n = f"ERR:{type(e).__name__}"
                sens = [cc for cc in cols(cur, t) if any(s in cc.lower() for s in SENSITIVE)]
                tag = f"  ⚠敏感列:{sens}" if sens else ""
                print(f"  - {t:32s} rows={n}{tag}")
            c.close()
        except Exception as e:
            print(f"  [打开失败] {type(e).__name__}: {e}")

if __name__ == "__main__":
    main()
