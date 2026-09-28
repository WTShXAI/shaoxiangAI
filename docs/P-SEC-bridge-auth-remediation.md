# P-SEC bridge 鉴权缺口整改规格（WINDOW · 纯规格不落地）

> 状态：**WINDOW 备料项**。本文件仅设计整改方案，**不本周期执行**——涉及 `bridge_service.py`
> 路由/中间件/WS 握手变更 + 前端联调，须停机窗口 + 全量回归 + 守卫断言通过后，由人工/运维在
> 维护窗口启用。AI 仅备料，不碰生产服务、不改 events.db、不杀进程。
> 关联审计：[`reports/audit_bridge_auth.{json,md}`]（T22，2026-09-26）。

---

## 1. 真实缺口（来自 T22 只读审计，非推测）

源文件 `D:\Architecture\bridge_service.py`，74 端点 / 15 写 / 59 读。

| method | path | line | 现状姿态 |
|--------|------|------|----------|
| WEBSOCKET | `/ws/odds_ingest` | 1373 | UNGUARDED_no_auth_mechanism |
| POST | `/predict` | 1499 | UNGUARDED_prefix_not_api |
| POST | `/predict/simple` | 1519 | UNGUARDED_prefix_not_api |
| POST | `/predict/single` | 1533 | UNGUARDED_prefix_not_api |

既有鉴权：`api_key_middleware`（H4 2026-07-30，line 451）。激活条件=**仅当 `API_KEY` 环境变量非空**；
默认关闭（单机私有兼容）。覆盖判定（line 457）：

```python
if path.startswith("/api") and is_write:   # ← /predict 非 /api 前缀 → 即使开启 API_KEY 也不覆盖
```

后果：
- 3 个 `/predict` 端点**非 `/api` 前缀** → 即便部署设 `API_KEY`，也不被中间件覆盖（前缀漏匹配）。
- `/ws/odds_ingest` 是写类 WebSocket，**无任何鉴权机制**（采集器→bridge 内部通道）。
- 既有守卫 `tests/test_bridge_health_security.py` 仅测 `/health` 结构，**未断言写端点 401** → 鉴权策略无回归保护。

与 IR-32 跨庄禁区无关：本整改只缩暴露面/鉴权，不触跨庄共识逻辑。

---

## 2. 整改方案

### 2.1 REST 写端点（`/predict` 三端点）— 二选一

**方案 A（推荐）：前缀统一 `/api` 化**
- 路由改 `@app.post("/api/predict")` `/api/predict/simple` `/api/predict/single`。
- 既有的 `api_key_middleware`（`path.startswith("/api") and is_write`）**自动覆盖**，零中间件改动。
- 前端 `frontend/` 中调用 `/predict*` 处（api.ts）同步改 `/api/predict*`；旧链接保留 301 兼容层。
- 风险：路由变更须停机窗口 + 前端联调回归；bailongma 容器 `:8000/predict` 调用方须同步。

**方案 B：中间件前缀放宽**
- 放宽 line 457 判定为 `path.startswith("/api") or path.startswith("/predict")`。
- 不改路由、不改前端调用。
- 风险：须确认无公开读场景误伤；bailongma 容器若开启 `API_KEY` 须带 `X-API-Key`。

> 推荐 A：中间件改动面小但语义耦合（把 /predict 隐式纳入写集合），A 让路由与鉴权口径一致，长期可维护。

### 2.2 WebSocket `/ws/odds_ingest` 鉴权

属**采集器→bridge 本机内部通道**，不应对公网/局域网暴露。防御纵深两道：

1. **部署层（首选·零代码）**：bridge 监听绑定 `127.0.0.1`（或防火墙限源），不暴露 `0.0.0.0`；
   T13 环境巡检未报告此暴露面，落实前须核实监听地址。
2. **握手令牌（代码·可选）**：WS 握手校验 `?token=XXX`（与采集器共享 secret，复用 `gq/.env` 既有
   token 体系，不并入 `API_KEY` 字段）；无令牌 → 立即 `await websocket.close(code=4401)`。

> 两道同时上：部署层兜底 + 令牌防本机越权。令牌值一律 `XXX` 对外。

### 2.3 回归守卫补强 `tests/test_bridge_health_security.py`

既有守卫只测 `/health`。新增断言族（**fail-closed**）：

| case | 前设 | 断言 |
|------|------|------|
| REST 无 key | `API_KEY` 设值 | `POST /api/predict` 返 **401** + `{"success":false,"error":{"code":"unauthorized"}}` |
| REST 错 key | `API_KEY` 设值 + 错 `X-API-Key` | 401 |
| REST 正 key | `API_KEY` 设值 + 正确 `X-API-Key` | 2xx（结构正确，不验预测内容） |
| WS 无 token | `WS /ws/odds_ingest?token=` 空 | 握手拒绝（close code 4401 / 非 101） |
| 默认关闭态 | `API_KEY` 空 | 既有 `/health` 结构断言仍过（向后兼容） |

守卫须跑 `pytest` 通过才算整改闭环（与 T25/T27 同门禁纪律）。

---

## 3. WINDOW 执行前置清单（须停机窗口，非本周期）

- [ ] 选 A 或 B（建议 A），变更 `bridge_service.py` + 前端 api.ts 同步
- [ ] WS 令牌握手 + 部署层绑定 127.0.0.1 核实
- [ ] 补 `test_bridge_health_security.py` 5 条断言
- [ ] 停 bridge → 应用变更 → `pytest tests/test_bridge_health_security.py -q` 全绿
- [ ] 全量回归 `pytest tests/ -q --timeout=120`（基线 ~196 passed，见 T27）不退化
- [ ] 老链接 301 兼容（若选 A）验证
- [ ] 重启服务后健康检查 + 采集器 `ws_odds_ingest` 连通性验证

## 4. 不本周期执行声明

本文件为**规格备料**。本回合仅产出此文档。以下动作**未做**：未改 `bridge_service.py`、
未改 `tests/`、未动 `events.db`、未重启/杀任何进程、未触生产服务。落地须走 §3 WINDOW 清单 +
人工/运维停机窗口。

---

*生成：自主 owner 自动化 dbda4380 · T28 · 2026-09-26*
*红线：IR-30 诚实 / IR-32 跨庄禁区（本整改不触）/ §4 数据资产保全 / §6 不杀进程*
