# 哨响AI 系统目标蓝图（Target Blueprint）— owner 采纳决策

> 建立于 2026-09-24，owner（赵统筹）据董事长战略建议采纳。
> 本文件 = 系统演进的**目标态**与**优先级路线图**，是 DISCIPLINE.md（运行纪律 SSoT）的**能力补充**，
> 不替代纪律。任何落地不得违反 DISCIPLINE.md（尤其 §9 跨庄禁区、§0/§1 诚实底线、§4 数据资产保全）。

## §0 owner 决断摘要
- **采纳**：文档 7 节 + 4 个拆解目标全部纳入目标态。
- **现状吻合度 ≈80%**：诚实底线、+EV 铁律、校准>准确率、IR-32 禁区、walkforward 门禁、
  反偷看（query_match applicable=False）、模块化（FastAPI+Vite+采集+守护分离）均已就位。
- **真实新增缺口**（按价值/安全排序，见 §3）：数据完整性日检、tick 级审计、反偷看快照 SOP、
  信号审批工单、评估看板、LLM Agent 权限边界。
- **红线不变**：LLM 不直接给买/卖；执行/建仓须人工+二次确认；绝不跨庄共识（IR-32）。

## §1 现状交叉比对（7 节 → DONE / PARTIAL / GAP）

| 文档节 | 要求 | 现状 | 判定 |
|---|---|---|---|
| 1 数据资产与审计链 | tick 可追溯(source/时间/延迟/原JSON哈希/清洗ID) | odds_changes 有 source+captured_at+延迟；**原 JSON 哈希/清洗 ID 缺** | PARTIAL |
| 1 | 事件字段冻结防偷看 | query_match 对 finished 硬性 applicable=False；prematch 字段采集时落库 | PARTIAL |
| 1 | 模型版本锁定(权重/超参/特征/窗口/种子) | model_catalog 存在；D 类模型未注册(B4) | PARTIAL |
| 1 | 信号日志不可变追加(谁/何时/为何) | 无信号审批流（系统已去喊单）；黑板 collab_journal 部分覆盖 | GAP |
| 1 | 每日自动数据完整性检查 | autonomous_monitor 含采集活性/校准漂移；**缺盘/跳tick/ID冲突/时区/补时 专项未覆盖** | GAP |
| 1 | 自愈重启只做工程恢复 | prod_guardian 只拉起不杀、不覆盖历史 | DONE |
| 2 三段框架 | 初盘/临场/滚盘 + 否定条件 | opening_line SSoT + prematch_candles + 滚球 decode 存在；**未形式化为带否定条件的框架** | PARTIAL |
| 3 LLM Agent 边界 | 读/提醒/归档/草稿；禁执行 | qwen3-analyzer 存在；**无正式权限边界文档/守卫** | PARTIAL |
| 4 工程架构 | 模块隔离+消息总线+特征库+模型注册 | FastAPI/Vite/采集/守护已隔离；**消息总线/特征存储缺失** | PARTIAL |
| 4 | 回测=生产同特征管线 | walkforward 门禁；**train-serve skew 无自动校验** | PARTIAL |
| 4 | 推卡片带可追溯链接 | 验证台报告含 match_id/model_ver；**前端未全量串联** | PARTIAL |
| 5 模型与方法 | Poisson/DC/Logistic/XGB/LightGBM/Elo/贝叶斯 | 现役含 K线LightGBM/HT锚/static；**DC/Elo/贝叶斯未接入** | PARTIAL |
| 5 | +EV 唯一判据 + ROI 带胜率/隐含/edge | verification 平台 G1–G6 + G6 零信息机械对照 | DONE |
| 5 | OU 无 edge 作真相→跨市场方向结构 | 已证 OU 全局无 edge；跨市场方向探索未系统做 | PARTIAL |
| 6 合规与定位 | 禁"稳赚/破解/必中"；定位研究工具 | DISCIPLINE.md §1/前端去喊单化 | DONE |
| 7 转化方向 | 作品集/研究平台/风控案例/内容 | 系统即资产；内容未系统产出 | PARTIAL |

## §2 四个拆解目标规格（owner 先定结构，落地排期见 §3）

### 2.1 信号审批工单表 `signal_approval_ticket`（仅分析信号/PASS 决策，非下注）
```sql
CREATE TABLE signal_approval_ticket (
  ticket_id      TEXT PRIMARY KEY,          -- uuid
  match_id       TEXT,
  stage          TEXT,                       -- opening | prematch_1h | inplay
  model_version  TEXT,
  signal_type    TEXT,                       -- value_gap | pass | exit | flag
  decision       TEXT,                       -- APPROVE | REJECT | PASS
  reason         TEXT,                       -- 结构化否定/边缘理由
  approver       TEXT,                       -- 人工账号 或 'auto'(仅草稿)
  approved_at    TIMESTAMP,
  data_snapshot_id TEXT,                     -- → match_snapshot.snapshot_id
  evidence_links TEXT,                       -- match_id,model_ver,feature_refs JSON
  note           TEXT
);  -- 不可变追加: 仅 INSERT, 禁 UPDATE/DELETE
```
- LLM 生成草稿(approver='auto')；执行/建仓须人工+二次确认（§3 红线）。

### 2.2 回测/纸盘评估看板指标（源=reports/verification_report.json，渲染层）
每模型: `n, LogLoss, Brier, ECE, vs_market_LL, ROI, ROI_CI_low, ROI_CI_high,
G6_pairing_diff, G6_CI_low, max_drawdown, CLV, calibration_curve`。
门禁: G1 n≥2500 / G2 ROI_CI_low>0 / G5 方向二项 / **G6 配对差 CI_low>0（09-23 后新增）**。

### 2.3 LLM Agent 权限与提示词边界
- **允许**: 读(库/日志)、提醒(异常)、归档、生成草稿(证据卡/复盘/文案)。
- **禁止**: 执行/建仓、直接 buy/sell、无 walkforward 的模型改动、git push、events.db 写、跨庄共识(IR-32)。
- **提示词边界模板**: 系统提示固定"你是分析透镜，不是开处方机"；输出须含依据链接；禁收益承诺措辞。

### 2.4 防未来信息数据快照方案
- 冻结点 T=kickoff: 首发/伤停/天气/裁判/赛程密度 → `match_snapshot(match_id, frozen_at, fields_json, hash)`。
- 现役 `query_match` 对 finished 硬性 applicable=False 已防回填；快照表补"赛前可见字段"不可变锚。
- 回测/生产共用同一 snapshot 读取路径，杜绝 train-serve skew。

## §3 优先级路线图（owner 决策的执行顺序）— 执行状态
> 图例: ✅已落地 / 🟡部分(表/机制就绪, 数据层阻塞) / ⬜待启动

1. ✅ **P-INTEGRITY（NOW, 安全）**: `scripts/data_integrity_check.py`（只读）→ reports/data_integrity.json；首跑 overall=OK。
   **已接入 prod_guardian JOBS**（3600s 周期, 回归 10 passed）；每日 00:00 守护例行重启后生效（§6 不杀进程）。
2. 🟡 **P-SNAPSHOT（NOW, 安全）**: `scripts/match_snapshot.py` + 隔离库 data/snapshots.db + 不可变触发器 + 测试 ✅。
   **阻塞 P-SNAPSHOT-data**: events.db `matches` 当前**无** 阵容/伤停/天气/裁判/赛程密度 字段（2026-09-24 核查），
   快照表含这些列但默认 NULL，待采集层补齐后写入（不改表结构）。
3. ✅ **P-TICKET（NOW, 安全）**: `scripts/signal_ticket.py` + 隔离库 data/signal_tickets.db + 不可变触发器 + IR-32 守卫 + 测试。
4. ✅ **P-LLM（NOW, 文档）**: `docs/LLM_AGENT_BOUNDARY.md`（权限矩阵 + 提示词边界模板 + 守卫）。
5. ⬜ **P-AUDIT（WINDOW）**: odds_changes 加 raw_json_hash + cleaned_id 列（schema 变更，须窗口+回填）。
6. ✅ **P-DASH（NOW, 已落地）**: `scripts/build_dashboard.py` → `deliverables/dashboard/evaluation_dashboard.html`（只读渲染 verification_report + data_integrity，零主库，file:// 直接开）。
7. ⬜ **P-ENG（WINDOW）**: 消息总线/特征存储（重架构，低紧急）。
8. ⬜ **P-MODEL（WINDOW）**: Dixon-Coles/Elo/贝叶斯 接入对照（walkforward 门禁）。
9. ⬜ **P-SNAPSHOT-data（阻塞派生）**: 采集 阵容/伤停/天气/裁判/赛程密度 并写入 match_snapshot（解 P-SNAPSHOT 阻塞）。

## §4 纪律约束（落地不得违反）
- §9 跨庄禁区：任何跨庄共识/投注占比字样禁入生产（tests/test_no_crossbook.py 守卫）。
- §0/§1 诚实：无已验证 edge 前不宣称盈利；ROI 必带胜率/隐含/edge。
- §4 数据资产：events.db 不 rm / 不在线 VACUUM；完整性检查只读。
- LLM 不直接给买/卖；执行须人工+二次确认。

## §6.1 产品价值 判定（owner 决断, 2026-09-24）— 答"产品能否为客户带来效益"
**诚实二分**：
- **作为"稳赚/必中/喊单"产品 → 不能，且严禁。** 六道关全 NO EDGE / INCONCLUSIVE，全系统无已验证可交易边缘；以此定位既违法（§6 合规）又欺诈（§0/§1 诚实）。
- **作为"体育量化研究 / 概率建模 / 赔率市场结构透明化 / 个人研究工具" → 能，且是真实价值**：
  1. **决策质量**：把"猜胜负"转为"校准概率 + 风险披露"，帮客户识别并避开 -EV 行为（不被割）。
  2. **审计透明**：tick 可追溯 / 不可变工单 / 反偷看快照 / 每日完整性 → 客户拿到可核查证据链，非黑箱口号。
  3. **数据资产**：Package B 2.2亿行赔率时序 = 量化研究者硬通货（保底锚 ¥85–110万）。
  4. **方法论资产**：walkforward 门禁 + G6 零信息机械对照 + 校准优先 → 可复用研究纪律，可内容化输出。
- **客户效益本质 = 少亏 + 想清楚 + 可核查，不是"带飞赚钱"。** 定位严守 §6。
- **商业化路径（§7）**：数据资产变现（P1/P2）+ 研究平台/方法论内容 + 风控概率决策案例；**不卖收益承诺**。
- 本看板（P-DASH）即该价值的对外透明载体：客户一眼看到"系统诚实说无 edge + 数据可核查"。
