# 自主 owner 任务队列（自动化每小时 pull 一项执行，打勾）

> 机制：自动化 `dbda4380`(HOURLY) 每轮从本队列取**第一个 `[ ]` 任务**执行，完成后标 `[x]` 并写结果。
> **红线铁律（队列内任务均须遵守）**：
> - 只写 `D:\Architecture\docs/` 与 `scripts/` 自有模块；只读探查用 `mode=ro` 打 `events.db`。
> - **绝不**：改 events.db 生产数据 / 杀进程 / 跑 schema 变更或 VACUUM（迁移脚本可写但**不得运行**）/ 碰生产服务。
> - 代码类任务写完须 `pytest` 通过才算完成。
> - 售卖仍须人工+法务双签（AI 不擅售）；WINDOW 项仅备料不执行。
> 优先级：NOW/筹备类在前，WINDOW 备料类在后。队列清空后自动化转只读巡检+建议新任务。

---

## 待执行（[ ] 未做 / [x] 已完成）

- [x] **T01 P-AUDIT 前向迁移脚本** — `scripts/p_audit_migrate.py` 已写(幂等 ALTER + 索引, 默认 dry-run, 生产库须 `--allow-production` 守卫); 单测 4 passed(临时库, 不碰 events.db)。不运行(须维护窗口)。2026-09-25
- [x] **T02 P-SNAPSHOT-data lineup 采集补全规格** — `docs/P-SNAPSHOT-lineup-spec.md` 已写: 根因(0400500 SPA会话保护/0408006未公布) + Tier A(低风险即时, 复用 getMatchAnalysiseDataPB sonMenuId=0) + Tier B(逆向突破 getMatchLineupListPB) + content_collector 改造草案 + match_meta/match_snapshot 集成点(列已就绪零迁移) + 优雅降级 + 防偷看保证(lineup属赛前已知态不破anti-peek) + 回滚检查单 + 验收门禁。纯规格不落地(须维护窗口)。2026-09-25
- [x] **T03 P-SNAPSHOT-data schedule_density 离线派生** — `scripts/derive_schedule_density.py` 已写(只读 events.db matches 表, 按 team+开赛前7日场次计数派生赛程密度, 输出隔离库 data/schedule_density.db; 默认 dry-run, --apply 才写; 不接回 freeze="不应用") + 单测 4 passed(临时库不碰 events.db); 真实库 dry-run 实测 24773 场可派生, 平均密度 0.50/0.49。2026-09-25
- [ ] **T04 B7 零引用 db 只读审计** — 写 `scripts/audit_orphan_dbs.py`：列 data/*.db 被哪些进程持有 + grep 引用 → 产归档安全清单（仅报告）。
- [ ] **T05 B1 bridge 端点使用率只读 grep** — 列出前端零调用端点候选（仅报告，不删）。
- [ ] **T06 B2 前端废弃类型 grep** — 列 Prediction/ModelComparison 等引用（仅报告）。
- [ ] **T07 P-MODEL DC/Elo/贝叶斯 接入可行性规格** — `docs/` 写对照接入方案 + walkforward 门禁。
- [ ] **T08 P-ENG 消息总线评估** — `docs/` 写评估结论（维持现状触发条件）。
- [ ] **T09 方法论内容文** — `docs/methodology_article.md`：把诚实纪律(G6零信息对照/校准优先/去水幂法)写成可对外内容资产。
- [ ] **T10 验证台样本累积追踪** — 写 `scripts/track_sample_growth.py`：只读 verification.db 画 n 趋势(CSV/JSON)。
- [ ] **T11 评估看板新增样本累积面板** — 改 `scripts/build_dashboard.py` 加趋势面板（pytest）。
- [ ] **T12 数据资产 README** — `deliverables/` 下写 Package A/B/C 说明(只读导出物索引)。
- [ ] **T13 环境复现一键校验** — 写 `scripts/check_env_health.py`：核对 venv/依赖/计划任务状态(只读)。
- [ ] **T14 接管核对单** — `docs/handover_checklist.md`：新接手者 30 分钟上线核对单。

---

## 已完成

（自动化每完成一项在此追加 `[x] 日期 结果`）
