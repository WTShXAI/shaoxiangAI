"""
scripts/probe_football_data_odds.py — 探测 football-data.org key 档位 + OU 盘口可用性

从 .env 读 FOOTBALL_DATA_API_KEY (不回显). 测:
  1) /v4/competitions      -> 鉴权是否通过 + 可见联赛数(档位代理)
  2) /v4/matches?status=FINISHED&limit=1 -> 拿一个已结束 match id
  3) /v4/matches/{id}/odds -> 是否返回 overUnderOdds (真实 OU 盘口)
只打印非敏感结构信息, 绝不打印 key.
"""
import os
import sys
import json
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def api_get(key, url):
    req = urllib.request.Request(url, headers={"X-Auth-Token": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        return e.code, {"error": body}
    except Exception as e:  # noqa
        return -1, {"error": str(e)}


def main():
    env = load_env(os.path.join(ROOT, ".env"))
    key = env.get("FOOTBALL_DATA_API_KEY")
    if not key:
        print("NO_KEY: FOOTBALL_DATA_API_KEY 未在 .env 找到")
        return
    print("KEY_PRESENT: yes (长度 %d, 已脱敏不回显)" % len(key))
    base = "https://api.football-data.org/v4"

    # 1) competitions
    st, comp = api_get(key, f"{base}/competitions?limit=100")
    if st != 200:
        print(f"COMPETITIONS: HTTP {st} -> {comp.get('error','')[:200]}")
        return
    comps = comp.get("competitions", [])
    print(f"COMPETITIONS: HTTP {st}, 可见联赛数={len(comps)}")
    # 免费档通常只有少数联赛; 打印前几个名字看档位
    names = [c.get("name") for c in comps[:8]]
    print("  样例联赛:", names)

    # 2) 一个已结束比赛
    st, m = api_get(key, f"{base}/matches?status=FINISHED&limit=1")
    if st != 200 or "matches" not in m or not m["matches"]:
        print(f"MATCHES: HTTP {st} -> {m.get('error','')[:200]}")
        return
    mid = m["matches"][0]["id"]
    print(f"MATCHES: HTTP {st}, 取样例 match id={mid}")

    # 3) odds (overUnderOdds)
    st, od = api_get(key, f"{base}/matches/{mid}/odds")
    if st != 200:
        print(f"ODDS: HTTP {st} -> {od.get('error','')[:200]}")
        print("  => 该 key 档位大概率不含 odds (免费档/低档)")
        return
    # 成功取回 odds
    books = od.get("odds", {}).get("bookmakers", [])
    print(f"ODDS: HTTP {st}, bookmakers 数={len(books)}")
    if books:
        ou = books[0].get("overUnderOdds", {})
        print("  首个庄家 overUnderOdds 键:", list(ou.keys()) if isinstance(ou, dict) else ou)
        # 找一个含 over/under 的具体线
        for b in books[:3]:
            ou = b.get("overUnderOdds", {})
            if isinstance(ou, dict) and ou:
                print(f"    庄家 {b.get('name')}: 样例 = {json.dumps(ou)[:240]}")
                break


if __name__ == "__main__":
    main()
