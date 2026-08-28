# 哨响AI 稳定化下一代足球系统 — 规划总览

> 主理人：齐活林（Delivery Director）｜协作：许清楚(PM) → 高见远(Architect)
> 日期：2026-08-22｜状态：规划完成，待涛哥确认 TBC-1~5 后启动实现

## 一、交付物
| 文件 | 作者 | 内容 |
|---|---|---|
| `docs/PRD_哨响AI稳定化下一代足球系统.md` | 许清楚 | 简单 PRD：目标 + 用户故事 + 需求池(P0×9/P1×5/P2×3) + UI 草图 + TBC-1~5 |
| `docs/system_design.md` | 高见远 | 架构设计 + 任务分解（9 节，含 7 类事故根因→模式映射） |
| `docs/class-diagram.mermaid` | 高见远 | 核心类图（错误信封 / DB 管理器 / 采集器 Step / 信号输出 / 低水状态机） |
| `docs/sequence-diagram.mermaid` | 高见远 | 3 张时序图（采集全链路 / API 错误处理 / 滚球破蛋神器） |

## 二、架构根治方案（7 类事故 → 模式）
| # | 事故 | 架构根治 |
|---|---|---|
| ① | bridge 冻结 | 阻塞调用 `run_in_executor` 移出循环 + `/health` 独立端口(:9001) + 下游熔断 + 停机 shim+worker 双杀 |
| ② | GQ 坏页 | PRAGMA 仅初始化一次 + WAL + 单写者 + 连接池 + `integrity_check` 自检 + 备份恢复 SOP |
| ③ | 前端白屏 | 统一错误信封 `{ok:false,error:{code,message:str}}`（message 强制字符串）+ 前端 ErrorBoundary + `setError` 仅收 string |
| ④ | 非 ASCII 崩轮 | `SafeLog` UTF-8 适配层(`backslashreplace`) + 进程 UTF-8 + 采集器 per-step `try/except`（失败不跳步） |
| ⑤ | 低水自相矛盾 | `LowWaterStateMachine` 双态：无开盘价 → `NEUTRAL/待确认`，禁判诱多 |
| ⑥ | 滚球时间窗口 | 6 层逐项修复（死变量/λ 单口径/分钟去污染/方向校正/键名对齐/kickoff 闸门）+ 每项正确单测 |
| ⑦ | 双源 ROI 偏差 | 清洗 7360 脏行 + 写入校验护栏 + 双源样本对齐/置信区间，`|ΔROI|>阈值` 标 `DISPUTED` |

## 三、任务分工（5 阶段 / 15 任务，P0 全覆盖）
- **Phase 0 基础设施**：T01 中央配置 / T02 日志+UTF8(REQ-05,10) / T03 错误信封(REQ-03)
- **Phase 1 数据稳定**：T04 DB 管理器(REQ-02) / T05 采集器隔离(REQ-05,06) / T12 备份 SOP(REQ-12)
- **Phase 2 服务稳定**：T06 bridge 防冻结+health+熔断(REQ-01) / T13 异步队列(REQ-11)
- **Phase 3 业务正确**：T07 低水双态(REQ-07,13) / T08 滚球 6 层修复(REQ-08) / T09 双源 ROI+脏数据(REQ-09,13) / T14 置信度标注(REQ-13)
- **Phase 4 前端韧性**：T10 错误安全消费(REQ-04) / T11 骨架屏+分区边界(REQ-14) / T15 可观测/回放/可解释(P2)
- 依赖与验收点：见 `system_design.md` §5 表格 + §9 依赖图。

## 四、需涛哥拍板的 TBC 决策点（推荐默认值已预埋，确认后无需返工）
1. **TBC-1 bridge 重构边界**：默认=防冻结改造（保留职责，不彻底重写）。是否接受？
2. **TBC-2 SQLite 规模上限**：默认=本期仅保稳定（WAL+单写者+池+自检+备份）；分库/归档留下一代。是否本期就要分库？
3. **TBC-3 双源 ROI 偏差阈值**：默认=清洗+护栏，目标 `<5pp`；超阈值标 `DISPUTED/已知采样差异`。业务可接受阈值？
4. **TBC-4 前端错误信封契约**：默认=全量契约改造 + 单一 `api/client.ts` 收口（先做消费点 grep 审计）。第三方组件适配面 OK？
5. **TBC-5 异步队列选型**：默认=ARQ + Redis；无 Redis 则进程内 asyncio worker 兜底。是否引入 Redis？

## 五、下一步建议
1. 涛哥确认 TBC-1~5（或采用推荐默认）。
2. 确认后启动 **工程师(寇豆码)** 按 T01→T15 顺序实现，每阶段产出后转 **QA(严过关)** 跑 7 类事故回归测试。
3. 建议优先 P0 九条（T01~T10 主干），P1/P2 视资源排期。
4. 实现期沿用铁律：盘口走 `build_opening_lines()`、OU/AH 回测走 `load_clean_outcomes()`、禁原始赔率值作特征、CS=诱导层/OU=诚实锚。
