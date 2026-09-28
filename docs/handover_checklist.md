# 接管核对单（新接手者 30 分钟上线）

> 用途：接手人**无口头指导**在 30 分钟内确认系统达 GO 状态并知道红线。
> 配套：自动自检 `scripts/p3_handoff_drill.py` + 完整手册 `环境复现指南.md` + 数据流 `docs/dataflow-diagram.mermaid`。
> 本单是"快速上线核对"，不是培训。细节卡住时回查上述手册，不要凭记忆改生产。

## 0. 前置（开始计时前，约 0 分钟，需老板提供）
- [ ] 拿到 git 访问权（`git@github.com:WTShXAI/shaoxiangAI.git`，分支 `main`）
- [ ] 机器已装 Python 3.12 + 本仓克隆到 `D:\Architecture`
- [ ] 已知晓：本机 DNS 有 IPv6 坑，git 走 IPv4（`git config --global http.curloptResolve "github.com:443:20.205.243.166"`）

## 1. 阶段一 · 环境自检（0–8 分钟）
- [ ] 进入 venv：`.venv\Scripts\python.exe --version` 应为 3.12.x
- [ ] 跑交接演练（核心门禁）：
  ```
  D:\Architecture\.venv\Scripts\python.exe scripts\p3_handoff_drill.py
  ```
- [ ] 退出码 **0** 且核心项全 ✅（venv / 依赖 import / 4 个数据层库 / git 远程+main / william_inter_model 冒烟）
- [ ] 若退出码 2：逐项看 ❌ 修复后重跑，**未达 0 不进入阶段二**
- [ ] （可选）深验：`scripts\p3_handoff_drill.py --deep` 应复现 P0-10 结论 `FAILED`（确认"无 edge"可重现，非口述）

## 2. 阶段二 · 红线与地雷（8–15 分钟，必读，记进脑子）
- [ ] **IR-30 诚实**：无 edge 不宣称盈利；任何 ROI 必带胜率/隐含概率/edge_pp + 零信息机械对照（买热门基准）
- [ ] **IR-32 跨庄禁区**：绝不输出跨庄共识/投注占比/喊单；只做单庄内诊断。守卫 `tests/test_no_crossbook.py`
- [ ] **§4 数据资产保全**：`events.db` 只读，零写入；不在线 VACUUM；任何导出走只读 `mode=ro`
- [ ] **§6 进程安全**：只拉起不杀；进程数≠实例数（看 PPID+CPU 时间，勿误杀）
- [ ] **售卖**：须人工+法务双签，AI 不擅售；Package B 赔率库卡合规（P2-5）
- [ ] **数据地雷**（回测/读盘前必知）：假 0-0（库内 62% 完场比分假）/ events.db 平局膨胀 35–42% / GQ.db 干净宇宙 2540 场才是可信真相源
- [ ] **去水铁律**：涉及赔率度量用 `devig_power`（幂法），禁比例法（虚高热门 +4.20% → 假 +EV）

## 3. 阶段三 · 启动服务（15–22 分钟）
- [ ] 按 `环境复现指南.md §7` 注册/拉起 7 个计划任务（bridge / GQ 采集 / net / vite 等）
- [ ] 或手动拉起 bridge：`.venv\Scripts\python.exe bridge_service.py`（detached，勿前台阻塞）
- [ ] 探活：`curl http://localhost:9000/health` 应返回 `healthy`
- [ ] 采集活性：看 `logs/prod_guardian.log` 末行无 `0401013` 持续失效（换 token 须老板浏览器登录产生，AI 造不出）

## 4. 阶段四 · 产出验证（22–30 分钟）
- [ ] **预测产出**：`GET /api/predictions?date=今天` 有行（或跑 `python -m pipeline.predict_export --date 今天`）
- [ ] **校准**：`GET /api/predictions/calibration` 或 `python scripts/eval_prediction_calibration.py` 出 LogLoss/ECE
- [ ] **盈利三态**：读 `reports/verification_report.json` → candles / market / KNN **全部 NO EDGE / INCONCLUSIVE**（诚实口径，非失败）
- [ ] **看板**：`deliverables/dashboard/evaluation_dashboard.html` 含样本累积面板，candles_ensemble 距 G1=2500 仍缺（如实标注）

## 5. 上线判定（GO / NO-GO）
- **GO**：阶段一退出码 0 + 阶段三 /health healthy + 阶段四三态诚实输出 + 阶段二红线已读。
- **NO-GO**：阶段一未达 0 → 先修复环境；服务起不来 → 查 §7 与 prod_guardian 日志；**任何红线疑问先停手问老板，勿盲改生产**。

## 6. 出事找哪（不靠脑内知识）
- 纪律 SSoT：`docs/DISCIPLINE.md` §1–§9
- 真相源：GQ.db（干净）/ events.db（只读赔率）
- 守护：`scripts/prod_guardian.py`（`ShaoxiangAI_ProdGuardian` 计划任务，断流自愈）
- 自主巡检：自动化 `dbda4380` 每 10 分钟 pull `docs/AUTONOMOUS_BACKLOG.md` 推进
- 老板决策点：售卖 / 大 schema 变更 / 杀进程 —— 以上 AI 不擅作，必须上报

---
*生成：2026-09-25 自动化 T14（纯 docs，不碰库不碰服务）。引用资产均为既有产物，未新增生产改动。*
