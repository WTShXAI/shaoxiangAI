# 赛前赔率相似检索系统 — 验证报告

> 验证时间: 2026-08-08 01:46 UTC | bridge PID 15188 | v7.4 引擎
> 关联文件: `pipeline/prematch_similarity.py` / `bridge_service.py` / `frontend/src/pages/LeagueSchedule/MatchAnalysisModal.tsx`

## 用户 4 大诉求 → 落地验证

| # | 诉求 | 实现 | 验证证据 |
|---|------|------|----------|
| ① | 胜平负只输出**一个**唯一结论 | `verdict` 单字段；`mode_verdict` 透明保留严格众数 | odds 查询返回 `verdict: "D"` 单一值 |
| ② | ROI **三方向全算**(非只取最低赔) | `roi: {H, D, A}` 各按临盘赔率平注1 | `roi: {H:-23.17, D:+17.67, A:-9.17}` |
| ③ | 只查**初盘+临盘**, 滚球盘必须拒 | 只取 `captured_at < kickoff` 快照; live/inplay 拒 | 阿森纳vs曼联(live)→ `applicable:false, reason:in_play` |
| ④ | 查到**平局就显平局** | `draw_upgrade`(freq_D≥0.30 改判平局) + 已结束比赛附 `verify_note` | odds 查询 freq=0.367≥0.30→改判 `D`; 246场回测平局召回 0%→23% |

## 两处根因修复(曾导致 `no_prematch_1x2`)

- **BUG1 帧拼装**: 采集器 H/D/A 同帧 `captured_at` 差 ~4ms, 原精确相等匹配永远只取 home → 全部失败。改 `int(round(captured_at))` 秒级分桶拼帧 (180/180 桶完整)。
- **BUG2 类型陷阱**: `captured_at < ko` 中 ko 为 TEXT, SQLite `REAL<TEXT` 恒真 → finished/live 混入滚球盘。改 `parse_kickoff()` 转 epoch 数值比较。

## draw_upgrade 默认反转(实证驱动)

| 判据 | 命中率 | 平局召回 | 平局精确率 |
|------|--------|----------|------------|
| 严格众数 (旧默认) | 41.5% | **0%** | — |
| draw_upgrade=True (现默认) | **45.9%** | **23%** | 47% (vs 基准 25.7% → 1.8×) |

## 端点实测

```
# ① 统计
GET /api/prematch/stats
→ 311749 场历史库 + 基准率 H/D/A=0.443/0.257/0.301

# ② 按赔率结构查询(触发平局改判 + 三向ROI)
GET /api/prematch/odds?ch=2.10&cd=3.20&ca=3.40&oh=2.05&od=3.30&oa=3.60&k=30
→ verdict="D"(draw_upgrade) | mode_verdict="H" | roi={H:-23.17,D:+17.67,A:-9.17}

# ③ 按比赛查询(已结束比赛附真实赛果对照)
GET /api/prematch/query?match_key=迈拉索尔SP vs 格雷米奥RS&k=30
→ actual_result="D", actual_score="1-1", verify_note="赛前模型判 主胜, 实际 1-1 (平局) → ✗未中"

# ④ 滚球拒识
GET /api/prematch/query?match_key=阿森纳 vs 曼联
→ applicable=false, reason="in_play", "请用滚球系统"
```

## 交付状态

- ✅ 后端 3 端点上线 (`/api/prematch/stats|odds|query`)
- ✅ 前端 `MatchAnalysisModal` 接入 `prematchService` (唯一结论卡 + 平局预警 + 三向ROI网格 + 初→临漂移 + 验证对照)
- ✅ `vite build` 通过 (1102 模块, 0 错误)
- ⏳ 可选: `AnalysisCenter` 页面接入同类检索 (当前仅 LeagueSchedule 模态框)
