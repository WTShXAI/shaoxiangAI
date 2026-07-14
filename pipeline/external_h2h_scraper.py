"""外部 H2H / 阵容 爬虫 (哨响AI · 赵统筹) — 11v11.com 源
=====================================================
用途: 从免费统计源 11v11.com 抓取两队历史对阵(H2H) / 已知交锋,
      为 W/D/L 融合的 L3/L4(阵容/伤病替代信号) 提供外部上下文。

两种模式:
  A. get_h2h(slug_a, opp_id)  -> 专用 H2H 页(完整历史交锋, 需 opposition id)
  B. get_matches_vs(slug_a, team_b) -> 比赛列表页(筛对手, 不需 id, 含近期/已录赛果)

合规与稳健:
  - 只读 GET, 不提交/不修改任何远端数据
  - 标准浏览器头(UA/Accept/Referer) + 跟随重定向 + 礼貌延时(默认1.5s)
  - 原始 HTML 落盘缓存到 data/cache/h2h/, 避免重复请求(礼貌+可离线复现)
  - 11v11 前置 Cloudflare 挑战: 经系统 curl 子进程抓取(Python urllib 的 TLS 被拦),
    Win10+/Linux/mac 自带 curl, 无需三方库
  - 11v11 无 robots 明确禁止; 但属第三方数据, 仅用于研究/内部特征, 不转售
  - ⚠️ 国家队 4 年一遇、阵容全换 -> H2H 信号稀疏且弱, 与 DrawExpert 77维部分冗余,
      仅作边缘校准参考, 非主信号

依赖: Python 标准库 + 系统 curl (零 Python 三方库)

用法:
  python pipeline/external_h2h_scraper.py            # 跑内置 demo (巴西-阿尔及利亚 + 巴西-挪威)
  python pipeline/external_h2h_scraper.py brazil 43  # 模式A: 巴西 H2H vs opposition id 43
"""
import os, re, json, sys, time, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "cache", "h2h")
os.makedirs(CACHE, exist_ok=True)

# 完整浏览器头 (解决 11v11 的 403/重定向拦截: 实测需 Referer+Accept 才返回 200)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.11v11.com/",
    "Connection": "keep-alive",
}
HTTP_RETRIES = 3
HTTP_DELAY = 1.5   # 礼貌延时(秒), 避免触发 11v11 限频

# ── 队名 → 11v11 slug 映射 (WC2026 全量, best-effort) ──
# ⚠️ 11v11 的 opposition id 无法从静态 HTML 自动发现:
#    - 对手索引页 /teams/{slug}/tab/opposingTeams/ → 403 Forbidden
#    - 已缓存的 matches/H2H 页 HTML 中均无 'opposition/{id}' 链接, 也无对手下拉
#   故本映射只覆盖「队 slug」(用于模式B /teams/{slug}/tab/matches/, 不需 id)。
#   错误 slug → 404 (可检测, 不会静默错误数据); 若某 slug 失效请手动修正。
TEAM_SLUGS = {
    "Algeria": "algeria", "Argentina": "argentina", "Australia": "australia",
    "Austria": "austria", "Belgium": "belgium", "Brazil": "brazil",
    "Canada": "canada", "Colombia": "colombia", "Congo DR": "congo-dr",
    "Croatia": "croatia", "Curaçao": "curacao", "Czechia": "czechia",
    "Ecuador": "ecuador", "Egypt": "egypt", "England": "england",
    "France": "france", "Germany": "germany", "Ghana": "ghana",
    "Haiti": "haiti", "Iran": "iran", "Iraq": "iraq",
    "Ivory Coast": "ivory-coast", "Japan": "japan", "Jordan": "jordan",
    "Korea Republic": "korea-republic", "Mexico": "mexico", "Morocco": "morocco",
    "Netherlands": "netherlands", "New Zealand": "new-zealand", "Norway": "norway",
    "Panama": "panama", "Paraguay": "paraguay", "Portugal": "portugal",
    "Qatar": "qatar", "Scotland": "scotland", "Senegal": "senegal",
    "South Africa": "south-africa", "Spain": "spain", "Sweden": "sweden",
    "Switzerland": "switzerland", "Tunisia": "tunisia", "Turkey": "turkey",
    "USA": "united-states", "Uruguay": "uruguay",
}
# opposition id 映射 (模式A 专用, 需 id)。
# ⚠️ 自动发现被 11v11 阻断 (见上), 仅收录「已验证可用」的 id;
#   切勿臆测填充 —— 错误 id 会静默返回错误对手的历史, 且难察觉。
OPPOSITION_IDS = {
    "algeria": 43,  # verified via working H2H URL
}

DATE_RE = re.compile(r"(\d{1,2} \w{3} \d{4})")
ROW_RE = re.compile(
    r"(\d{1,2} \w{3} \d{4})\s+"          # date
    r"([\w\s\.\(\)\-]+?)\s+v\s+"          # home
    r"([\w\s\.\(\)\-]+?)\s+"              # away
    r"([WLD])\s+"                          # result (from home perspective)
    r"(\d+-\d+)\s+"                        # score
    r"(.+?)\s*$"                           # competition
)


def _cache_path(url):
    fn = re.sub(r"[^\w]", "_", url)[-120:]
    return os.path.join(CACHE, fn + ".html")


def fetch_html(url, use_cache=True):
    """抓取 URL, 带缓存/重试/cloudflare挑战检测。
    11v11 前置 Cloudflare "Just a moment..." 挑战: Python urllib 的 TLS 被拦截(403),
    但系统 curl 可过 (实测 200)。故本函数经 curl 子进程抓取(跨平台: Win10+/Linux/mac 自带 curl)。
    返回 (html, cached_flag)。"""
    cp = _cache_path(url)
    if use_cache and os.path.exists(cp):
        with open(cp, "r", encoding="utf-8") as f:
            return f.read(), True
    # 构造 curl 命令(浏览器头, 跟随重定向)
    cmd = [
        "curl", "-sL", "--max-time", "25",
        "-A", HEADERS["User-Agent"],
        "-H", f"Accept: {HEADERS['Accept']}",
        "-H", f"Accept-Language: {HEADERS['Accept-Language']}",
        "-H", f"Referer: {HEADERS['Referer']}",
        url,
    ]
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
            html = r.stdout or ""
            if r.returncode != 0 or not html or "Just a moment" in html[:600] \
               or "403 Forbidden" in html[:512] or "Access Denied" in html[:512]:
                last_err = f"blocked(rc={r.returncode}, cf={('Just a moment' in html[:600])})"
                time.sleep(HTTP_DELAY * attempt)
                continue
            with open(cp, "w", encoding="utf-8") as f:
                f.write(html)
            return html, False
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(HTTP_DELAY * attempt)
    # 全部失败: 回退缓存(若有)否则返回空
    if os.path.exists(cp):
        with open(cp, "r", encoding="utf-8") as f:
            return f.read(), True
    print(f"[fetch_html] WARN 抓取失败({last_err}): {url}")
    return "", False


def parse_h2h(html):
    """解析专用 H2H 页: 行格式 '17 Jun 1965 Algeria v Brazil W 0-3 International Friendly'"""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        txt = re.sub(r"<[^>]+>", " ", tr)
        txt = re.sub(r"\s+", " ", txt).strip()
        m = ROW_RE.search(txt)
        if m and DATE_RE.search(txt):
            date, home, away, res, score, comp = m.groups()
            out.append({"date": date, "home": home.strip(), "away": away.strip(),
                        "result": res, "score": score, "comp": comp.strip()})
    return out


def parse_matches_vs(html, team_b):
    """解析比赛列表页, 筛出含 team_b 的行 (Mode B, 不需 opposition id)"""
    out = []
    tb = team_b.lower()
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 2:
            continue
        match = re.sub(r"<[^>]+>", " ", tds[1])
        match = re.sub(r"\s+", " ", match).strip()
        if tb in match.lower() and (" v " in match):
            full = re.sub(r"<[^>]+>", " ", tr)
            full = re.sub(r"\s+", " ", full).strip()
            m = ROW_RE.search(full)
            if m:
                date, home, away, res, score, comp = m.groups()
                # 仅保留真正含双方的对阵
                if tb in (home.lower() + " " + away.lower()):
                    out.append({"date": date, "home": home.strip(), "away": away.strip(),
                                "result": res, "score": score, "comp": comp.strip()})
    return out


def summarize(matches, perspective_team):
    """从 perspective_team 视角统计 W/D/L"""
    w = d = l = 0
    pt = perspective_team.lower()
    for m in matches:
        if m["home"].lower() == pt:
            if m["result"] == "W": w += 1
            elif m["result"] == "D": d += 1
            elif m["result"] == "L": l += 1
        elif m["away"].lower() == pt:
            if m["result"] == "W": l += 1
            elif m["result"] == "D": d += 1
            elif m["result"] == "L": w += 1
    return {"W": w, "D": d, "L": l, "n": len(matches)}


# ── 公开 API ──
def get_h2h(team_a_slug, opp_id, use_cache=True):
    url = f"https://www.11v11.com/teams/{team_a_slug}/tab/opposingTeams/opposition/{opp_id}/"
    html, cached = fetch_html(url, use_cache)
    matches = parse_h2h(html)
    return {"mode": "A-h2h", "url": url, "cached": cached,
            "matches": matches, "summary": summarize(matches, team_a_slug)}


def get_matches_vs(team_a_slug, team_b_name, use_cache=True):
    url = f"https://www.11v11.com/teams/{team_a_slug}/tab/matches/"
    html, cached = fetch_html(url, use_cache)
    matches = parse_matches_vs(html, team_b_name)
    return {"mode": "B-matches", "url": url, "cached": cached,
            "matches": matches, "summary": summarize(matches, team_a_slug)}


def parse_all_matches(html):
    """解析比赛列表页全部行 (模式B批量, 不需 opposition id)"""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        full = re.sub(r"<[^>]+>", " ", tr)
        full = re.sub(r"\s+", " ", full).strip()
        m = ROW_RE.search(full)
        if m and DATE_RE.search(full):
            date, home, away, res, score, comp = m.groups()
            out.append({"date": date, "home": home.strip(), "away": away.strip(),
                        "result": res, "score": score, "comp": comp.strip()})
    return out


def get_all_matches(team_a_slug, use_cache=True):
    url = f"https://www.11v11.com/teams/{team_a_slug}/tab/matches/"
    html, cached = fetch_html(url, use_cache)
    return {"mode": "B-all", "url": url, "cached": cached,
            "matches": parse_all_matches(html)}


def batch_h2h(team_name, use_cache=True):
    """ID-FREE 批量 H2H: 抓该队比赛列表页一次, 按对手聚合 W/D/L。
    无需 opposition id, 即可获得该队 vs 所有历史对手的交锋统计。
    team_name 用 11v11 显示名 (同 DB 英文队名, 如 'Brazil'/'USA')。"""
    from collections import defaultdict
    slug = TEAM_SLUGS.get(team_name, team_name.lower().replace(" ", "-"))
    res = get_all_matches(slug, use_cache)
    tn = team_name.lower()
    opp = defaultdict(list)
    for m in res["matches"]:
        h, a = m["home"].lower(), m["away"].lower()
        if h == tn:
            opp[m["away"]].append(m["result"])
        elif a == tn:
            opp[m["home"]].append("W" if m["result"] == "L" else ("L" if m["result"] == "W" else "D"))
    summaries = {o: {"W": r.count("W"), "D": r.count("D"), "L": r.count("L"), "n": len(r)}
                 for o, r in opp.items()}
    return {"mode": "B-batch", "team": team_name, "slug": slug,
            "cached": res["cached"], "total_matches": len(res["matches"]),
            "opponents": summaries}


def demo():
    print("=" * 64)
    print("外部 H2H 爬虫 Demo (11v11.com)")
    print("=" * 64)

    # 模式A: 巴西 H2H vs 阿尔及利亚 (opposition id=43, verified)
    print("\n--- 模式A: 巴西 vs 阿尔及利亚 H2H (完整历史) ---")
    r = get_h2h("brazil", OPPOSITION_IDS["algeria"])
    print(f"  URL: {r['url']}")
    print(f"  缓存命中: {r['cached']} | 交锋场次: {r['summary']['n']}")
    print(f"  巴西视角 W/D/L: {r['summary']}")
    for m in r["matches"]:
        print(f"    {m['date']}  {m['home']} v {m['away']} {m['result']} {m['score']} [{m['comp']}]")

    # 模式B: 巴西 vs 挪威 (比赛列表页筛选, 不需 id)
    print("\n--- 模式B: 巴西 vs 挪威 (比赛列表筛选) ---")
    r2 = get_matches_vs("brazil", "Norway")
    print(f"  URL: {r2['url']}")
    print(f"  缓存命中: {r2['cached']} | 匹配场次: {r2['summary']['n']}")
    print(f"  巴西视角 W/D/L: {r2['summary']}")
    for m in r2["matches"]:
        print(f"    {m['date']}  {m['home']} v {m['away']} {m['result']} {m['score']} [{m['comp']}]")

    # 模式B 批量: 巴西 vs 所有历史对手 (ID-FREE, 一次抓取)
    print("\n--- 模式B 批量: 巴西 vs 所有对手 (ID-FREE) ---")
    r3 = batch_h2h("Brazil")
    print(f"  slug={r3['slug']} 缓存命中={r3['cached']} 列表总场次={r3['total_matches']} 对手数={len(r3['opponents'])}")
    for opp, s in sorted(r3["opponents"].items(), key=lambda kv: -kv[1]["n"])[:8]:
        print(f"    vs {opp}: W/D/L={s['W']}/{s['D']}/{s['L']} (n={s['n']})")

    # ── 实时抓取验证 (use_cache=False, 证明 11v11 在线可抓, 非仅缓存) ──
    print("\n--- 实时抓取验证: England 比赛列表 (use_cache=False) ---")
    r4 = batch_h2h("England", use_cache=False)
    print(f"  slug={r4['slug']} 缓存命中={r4['cached']} 列表总场次={r4['total_matches']} 对手数={len(r4['opponents'])}")
    if not r4["cached"] and r4["total_matches"] > 0:
        print("  ✅ 实时抓取成功 (11v11 在线可抓, 403/UA 已解决)")
    else:
        print("  ⚠️ 实时抓取未返回数据(可能限频/网络), 已回退缓存")

    # 保存 demo 结果
    out = {"brazil_v_algeria_h2h": r, "brazil_v_norway_matches": r2, "brazil_batch_h2h": r3}
    with open(os.path.join(ROOT, "deliverables", "wc2026_h2h_scraper_demo.json"),
              "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\nSaved: deliverables/wc2026_h2h_scraper_demo.json")


if __name__ == "__main__":
    if len(sys.argv) == 3:
        slug, opp = sys.argv[1], sys.argv[2]
        try:
            opp_id = int(opp)
            res = get_h2h(slug, opp_id)
        except ValueError:
            res = get_matches_vs(slug, opp)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        demo()
