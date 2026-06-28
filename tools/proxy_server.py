"""
哨响AI — Football-Data.org CORS 代理服务器
绕过浏览器 CORS 策略，支持最大 9 天赛事数据拉取
"""
import os
import sys
import json
import logging
from datetime import datetime, timedelta, date
from urllib.parse import urlencode

# 确保能找到 .env
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
ENV_PATH = os.path.join(PARENT_DIR, "windows哨响AI", ".env")

# 优先从 windows哨响AI/.env 读，其次当前目录
if os.path.exists(ENV_PATH):
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)
else:
    from dotenv import load_dotenv
    load_dotenv()

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
if not API_KEY:
    print("="*60)
    print("❌ FOOTBALL_DATA_API_KEY 未设置！")
    print("请在 windows哨响AI/.env 中填写你的 API Key：")
    print("  FOOTBALL_DATA_API_KEY=你的Key")
    print("="*60)

import http.server
import ssl
import urllib.request

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Phase 2A: 统一从 config 读取，支持环境变量覆盖
try:
    from config.api_config import EXTERNAL_SERVICES
    BASE_URL = EXTERNAL_SERVICES.get("football_data", {}).get(
        "base_url", "https://api.football-data.org/v4")
except ImportError:
    BASE_URL = os.getenv("FOOTBALL_DATA_BASE_URL", "https://api.football-data.org/v4")

# ============ 支持的联赛 ID ============
COMPETITIONS = {
    "PL": "英超",
    "PD": "西甲",
    "SA": "意甲",
    "BL1": "德甲",
    "FL1": "法甲",
    "CL": "欧冠",
    "ELC": "英冠",
    "BSA": "巴甲",
    "DED": "荷甲",
    "PPL": "葡超",
    "WC": "世界杯",
    "EC": "欧洲杯",
}

class CORSProxyHandler(http.server.BaseHTTPRequestHandler):
    """CORS 代理处理器"""

    def do_OPTIONS(self):
        """预检请求 — 直接返回 CORS 头"""
        self.send_cors_headers()
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        """代理 GET 请求到 football-data.org"""
        if not API_KEY:
            self.send_json(503, {"error": "API Key 未配置，请在 .env 中设置 FOOTBALL_DATA_API_KEY"})
            return

        # 解析路径
        path = self.path

        # ===== 首页/状态页 =====
        if path == "/" or path == "/status":
            self.send_html(self.render_status_page())
            return

        # ===== 健康检查 =====
        if path == "/health":
            self.send_json(200, {
                "status": "ok",
                "api_key_set": bool(API_KEY),
                "api_key_prefix": API_KEY[:4] + "..." if API_KEY else "N/A",
                "base_url": BASE_URL,
            })
            return

        # ===== 特殊端点: 多日比赛聚合（最大9天） =====
        if path.startswith("/matches/multi-day"):
            self.handle_multi_day_matches(path)
            return

        # ===== 今日所有联赛比赛 =====
        if path.startswith("/matches/today-all"):
            self.handle_today_all_matches(path)
            return

        # ===== 模型准确率评估 =====
        if path.startswith("/evaluate/accuracy"):
            self.handle_evaluate_accuracy(path)
            return

        # ===== 通用代理：转发到 football-data.org =====
        target_path = path
        if target_path.startswith("/proxy/"):
            target_path = target_path[len("/proxy"):]

        url = f"{BASE_URL}{target_path}"
        self.proxy_request(url)

    def handle_multi_day_matches(self, path: str):
        """聚合多日比赛（最多9天，默认包含近30天历史+未来N天）"""
        # 解析参数: /matches/multi-day?competition_ids=PL,PD&days=7&dateFrom=2026-05-01
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(path)
        params = parse_qs(parsed.query)

        comp_ids_raw = params.get("competition_ids", [""])[0]
        days = min(int(params.get("days", [7])[0]), 9)  # 限制最大9天
        comp_ids = [c.strip() for c in comp_ids_raw.split(",") if c.strip()] if comp_ids_raw else list(COMPETITIONS.keys())

        # 支持自定义 dateFrom，默认回溯 30 天以覆盖赛季末空窗期
        custom_from = params.get("dateFrom", [""])[0]
        if custom_from:
            date_from = custom_from
        else:
            date_from = (date.today() - timedelta(days=30)).isoformat()
        date_to = (date.today() + timedelta(days=days)).isoformat()

        all_matches = []
        errors = []

        for comp_id in comp_ids:
            try:
                url = f"{BASE_URL}/competitions/{comp_id}/matches?dateFrom={date_from}&dateTo={date_to}"
                data = self.fetch_json(url)
                matches = data.get("matches", [])
                for m in matches:
                    m["_competition_id"] = comp_id
                    m["_competition_name"] = COMPETITIONS.get(comp_id, comp_id)
                all_matches.extend(matches)
                logger.info(f"  [{comp_id}] {COMPETITIONS.get(comp_id, comp_id)}: {len(matches)} 场比赛")
            except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
                logger.warning(f"  [{comp_id}] 拉取失败: {e}")
                errors.append(f"{comp_id}: {e}")

        all_matches.sort(key=lambda m: m.get("utcDate", ""))

        self.send_json(200, {
            "success": True,
            "date_range": {"from": date_from, "to": date_to},
            "days": days,
            "competitions_requested": comp_ids,
            "competitions_failed": errors,
            "total_matches": len(all_matches),
            "matches": all_matches,
        })

    def handle_today_all_matches(self, path: str):
        """拉取今日所有联赛比赛"""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(path)
        params = parse_qs(parsed.query)
        comp_ids_raw = params.get("competition_ids", [""])[0]
        comp_ids = [c.strip() for c in comp_ids_raw.split(",") if c.strip()] if comp_ids_raw else list(COMPETITIONS.keys())

        today = date.today().isoformat()
        all_matches = []

        for comp_id in comp_ids:
            try:
                url = f"{BASE_URL}/competitions/{comp_id}/matches?dateFrom={today}&dateTo={today}"
                data = self.fetch_json(url)
                matches = data.get("matches", [])
                for m in matches:
                    m["_competition_id"] = comp_id
                    m["_competition_name"] = COMPETITIONS.get(comp_id, comp_id)
                all_matches.extend(matches)
            except (Exception, KeyError, IndexError, requests.exceptions.RequestException) as e:
                logger.warning(f"  [{comp_id}] 拉取失败: {e}")

        all_matches.sort(key=lambda m: m.get("utcDate", ""))

        self.send_json(200, {
            "success": True,
            "date": today,
            "competitions": comp_ids,
            "total_matches": len(all_matches),
            "matches": all_matches,
        })

    def generate_prediction_verdict(self, seed: int) -> str:
        """复刻前端 generatePrediction 逻辑，返回预测判决（主胜/客胜/平局）"""
        pseudo_seed = seed % 100
        probs_home = 25 + (pseudo_seed % 35)
        probs_draw = 20 + ((pseudo_seed * 7) % 20)
        probs_away = 100 - probs_home - probs_draw

        if probs_home >= probs_away + 8:
            return "HOME_TEAM"
        elif probs_away >= probs_home + 8:
            return "AWAY_TEAM"
        else:
            return "DRAW"

    def handle_evaluate_accuracy(self, path: str):
        """评估模型预测准确率：对比已完赛比赛的预测 vs 实际结果"""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(path)
        params = parse_qs(parsed.query)

        lookback_days = min(int(params.get("days", ["14"])[0]), 30)
        comp_ids_raw = params.get("competition_ids", [""])[0]
        comp_ids = [c.strip() for c in comp_ids_raw.split(",") if c.strip()] if comp_ids_raw else list(COMPETITIONS.keys())

        date_from = (date.today() - timedelta(days=lookback_days)).isoformat()
        date_to = date.today().isoformat()

        correct = 0
        total = 0
        details = []
        by_league = {}
        by_confidence = {"high": [0, 0], "medium": [0, 0], "low": [0, 0]}

        for comp_id in comp_ids:
            try:
                url = f"{BASE_URL}/competitions/{comp_id}/matches?dateFrom={date_from}&dateTo={date_to}&status=FINISHED"
                data = self.fetch_json(url)
                matches = data.get("matches", [])

                for m in matches:
                    # 只评估有明确比分的比赛
                    if not m.get("score") or not m["score"].get("winner"):
                        continue

                    actual = m["score"]["winner"]  # HOME_TEAM / AWAY_TEAM / DRAW
                    match_id = m.get("id", 0)
                    predicted = self.generate_prediction_verdict(match_id)

                    is_correct = (predicted == actual)
                    if is_correct:
                        correct += 1
                    total += 1

                    # 按联赛统计
                    league_name = COMPETITIONS.get(comp_id, comp_id)
                    if league_name not in by_league:
                        by_league[league_name] = [0, 0]
                    by_league[league_name][1 if is_correct else 0] += 1

                    # 按置信度统计（复刻前端置信度逻辑）
                    pseudo_seed = match_id % 100
                    confidence = 55 + (pseudo_seed % 40)
                    if confidence >= 80:
                        level = "high"
                    elif confidence < 65:
                        level = "low"
                    else:
                        level = "medium"
                    by_confidence[level][1 if is_correct else 0] += 1

                    details.append({
                        "match_id": match_id,
                        "home": m["homeTeam"]["shortName"] or m["homeTeam"]["name"],
                        "away": m["awayTeam"]["shortName"] or m["awayTeam"]["name"],
                        "league": league_name,
                        "predicted": "主胜" if predicted == "HOME_TEAM" else ("客胜" if predicted == "AWAY_TEAM" else "平局"),
                        "actual": "主胜" if actual == "HOME_TEAM" else ("客胜" if actual == "AWAY_TEAM" else "平局"),
                        "correct": is_correct,
                        "date": m["utcDate"][:10]
                    })

                logger.info(f"  [Eval][{comp_id}] {len(matches)} matches evaluated")
            except (Exception, KeyError, IndexError) as e:
                logger.warning(f"  [Eval][{comp_id}] 评估失败: {e}")

        accuracy = round(correct / total * 100, 1) if total > 0 else 0

        # 计算各联赛准确率
        league_stats = {}
        for league, scores in by_league.items():
            correct_count = scores[1]
            league_total = sum(scores)
            league_stats[league] = {
                "total": league_total,
                "correct": correct_count,
                "accuracy": round(correct_count / league_total * 100, 1) if league_total > 0 else 0
            }

        # 计算各置信度准确率
        confidence_stats = {}
        for level, scores in by_confidence.items():
            c_total = sum(scores)
            confidence_stats[level] = {
                "total": c_total,
                "correct": scores[1],
                "accuracy": round(scores[1] / c_total * 100, 1) if c_total > 0 else 0
            }

        self.send_json(200, {
            "success": True,
            "date_range": {"from": date_from, "to": date_to},
            "total_matches": total,
            "correct_predictions": correct,
            "accuracy": accuracy,
            "by_league": league_stats,
            "by_confidence": confidence_stats,
            "recent": details[-20:]  # 最近20场详情
        })

    def proxy_request(self, url: str):
        """通用代理：转发请求并返回"""
        try:
            data = self.fetch_json(url)
            # 写入响应头
            self.send_cors_headers()
            if "errorCode" in data:
                self.send_response(400)
            else:
                self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        except (Exception, IOError, FileNotFoundError, json.JSONDecodeError) as e:
            self.send_json(502, {"error": f"API 代理失败: {str(e)}"})

    def fetch_json(self, url: str) -> dict:
        """向 football-data.org 发起请求"""
        req = urllib.request.Request(url)
        req.add_header("X-Auth-Token", API_KEY)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                # 处理限流
                remaining = resp.headers.get("X-Requests-Available-Minute", "?")
                logger.info(f"  HTTP {resp.status} | {url.split('?')[0]} | 剩余配额: {remaining}")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8") if e.fp else str(e)
            logger.error(f"  HTTP {e.code} | {url} | {error_body[:200]}")
            if e.code == 429:
                return {"errorCode": 429, "message": "请求过于频繁（20次/分钟限制），请稍后再试"}
            return {"errorCode": e.code, "message": error_body}

    # ============ 响应工具 ============

    def send_cors_headers(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def send_html(self, html: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} - {format % args}")

    # ============ 状态页 ============

    def render_status_page(self) -> str:
        """渲染状态页"""
        league_rows = "\n".join(
            f'<tr><td><code>{cid}</code></td><td>{cname}</td>'
            f'<td><button onclick="fetchLeague(\'{cid}\')">拉取</button></td></tr>'
            for cid, cname in COMPETITIONS.items()
        )
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>哨响AI — API 代理中心</title>
<style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ font-family: 'Segoe UI', system-ui, sans-serif; background:#0d1117; color:#e6edf3; padding:32px; }}
    h1 {{ font-size:1.5rem; margin-bottom:8px; }}
    .sub {{ color:#7a8ba0; font-size:0.85rem; margin-bottom:24px; }}
    .card {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:20px; margin-bottom:16px; }}
    .card h2 {{ font-size:1rem; margin-bottom:12px; color:#58a6ff; }}
    table {{ width:100%; border-collapse:collapse; }}
    th, td {{ text-align:left; padding:8px 12px; border-bottom:1px solid #21262d; font-size:0.85rem; }}
    th {{ color:#7a8ba0; font-weight:500; }}
    button {{ background:#238636; color:#fff; border:none; padding:4px 12px; border-radius:4px; cursor:pointer; font-size:0.8rem; }}
    button:hover {{ background:#2ea043; }}
    input, select {{ background:#0d1117; border:1px solid #30363d; color:#e6edf3; padding:6px 10px; border-radius:4px; font-size:0.85rem; }}
    .row {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }}
    pre {{ background:#0d1117; border:1px solid #30363d; border-radius:4px; padding:12px; overflow-x:auto; font-size:0.78rem; max-height:300px; overflow-y:auto; }}
    .tag {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:0.7rem; background:#1f6feb33; color:#58a6ff; }}
    .ok {{ color:#3fb950; }}
    .err {{ color:#f85149; }}
</style>
</head>
<body>
<h1>⚽ 哨响AI — API 代理中心</h1>
<div class="sub">
    API Key: <span class="{'ok' if API_KEY else 'err'}">{API_KEY[:8] + '...' if API_KEY else '未配置'}</span>
    | 代理地址: <code>localhost:{PORT}</code>
    | 数据源: <code>api.football-data.org/v4</code>
</div>

<div class="card">
    <h2>📅 多日赛事拉取（最大9天）</h2>
    <div class="row">
        <label>联赛（多选用逗号分隔）:</label>
        <input type="text" id="compInput" value="PL,PD,SA,BL1,FL1" style="width:300px;">
        <label>天数:</label>
        <input type="number" id="daysInput" value="7" min="1" max="9" style="width:60px;">
        <button onclick="fetchMultiDay()">🔍 拉取赛事</button>
    </div>
    <div class="row">
        <button onclick="fetchAllLeagues()">全部联赛 (14个)</button>
        <button onclick="fetchTodayAll()">仅今日比赛</button>
    </div>
    <pre id="multiDayResult">点击按钮拉取数据...</pre>
</div>

<div class="card">
    <h2>🏆 联赛列表</h2>
    <table>
        <tr><th>ID</th><th>名称</th><th>操作</th></tr>
        {league_rows}
    </table>
</div>

<div class="card">
    <h2>🔗 API 端点速查</h2>
    <table>
        <tr><td><code>GET /health</code></td><td>健康检查</td></tr>
        <tr><td><code>GET /matches/multi-day?competition_ids=PL,PD&days=7</code></td><td>多日赛事聚合</td></tr>
        <tr><td><code>GET /matches/today-all?competition_ids=PL,PD</code></td><td>今日各联赛赛事</td></tr>
        <tr><td><code>GET /proxy/competitions/PL/matches?dateFrom=2026-05-20&dateTo=2026-05-27</code></td><td>通用代理</td></tr>
    </table>
</div>

<script>
const BASE = `http://localhost:{PORT}`
async function fetchMultiDay() {{
    const comps = document.getElementById('compInput').value
    const days = document.getElementById('daysInput').value
    const el = document.getElementById('multiDayResult')
    el.textContent = '加载中...'
    try {{
        const resp = await fetch(`${{BASE}}/matches/multi-day?competition_ids=${{encodeURIComponent(comps)}}&days=${{days}}`)
        const data = await resp.json()
        el.textContent = JSON.stringify(data, null, 2)
    }} catch(e) {{
        el.textContent = '❌ 请求失败: ' + e.message
    }}
}}

async function fetchAllLeagues() {{
    const el = document.getElementById('multiDayResult')
    el.textContent = '加载中...（14个联赛，可能需要几秒）'
    try {{
        const resp = await fetch(`${{BASE}}/matches/multi-day?days=9`)
        const data = await resp.json()
        el.textContent = `✅ 共 ${{data.total_matches}} 场比赛\\n` + JSON.stringify(data, null, 2)
    }} catch(e) {{
        el.textContent = '❌ 请求失败: ' + e.message
    }}
}}

async function fetchTodayAll() {{
    const el = document.getElementById('multiDayResult')
    el.textContent = '加载中...'
    try {{
        const resp = await fetch(`${{BASE}}/matches/today-all`)
        const data = await resp.json()
        el.textContent = `✅ 今日共 ${{data.total_matches}} 场比赛\\n` + JSON.stringify(data, null, 2)
    }} catch(e) {{
        el.textContent = '❌ 请求失败: ' + e.message
    }}
}}

async function fetchLeague(cid) {{
    const el = document.getElementById('multiDayResult')
    el.textContent = `加载 ${{cid}} 数据中...`
    try {{
        const resp = await fetch(`${{BASE}}/proxy/competitions/${{cid}}/matches`)
        const data = await resp.json()
        el.textContent = JSON.stringify(data, null, 2)
    }} catch(e) {{
        el.textContent = '❌ 请求失败: ' + e.message
    }}
}}
</script>
</body>
</html>"""

PORT = int(os.getenv("PROXY_PORT", "5001"))

if __name__ == "__main__":
    if not API_KEY:
        print("\n⚠️  API Key 未设置，代理将返回 503 错误。")
        print("请在 windows哨响AI/.env 中设置: FOOTBALL_DATA_API_KEY=你的Key\n")

    server = http.server.HTTPServer(("0.0.0.0", PORT), CORSProxyHandler)
    import sys
    sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None
    print(f"\n{'='*60}")
    print(f"[XiangXiaoAI] API Proxy Server started")
    print(f"   URL: http://localhost:{PORT}")
    print(f"   Status: http://localhost:{PORT}/")
    print(f"   API Key: {'[OK] ' + API_KEY[:4] + '***' if API_KEY else '[MISSING]'}")
    print(f"   Press Ctrl+C to stop")
    print(f"{'='*60}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止。")
        server.server_close()
