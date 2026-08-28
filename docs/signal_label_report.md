# 三色情绪标签系统（红绿灯 + 关键词）落地报告

> 把"最强信号"压缩成一眼能懂、带情绪色彩的短标签，已照你给的体系实现为系统级 SSoT。

## 一、已落地的标签体系（直接照用）

| 色 | 类别 | 标签 | 触发（方向 + 偏差 + ROI） |
|----|------|------|---------------------------|
| 🔴 | 高风险/冷门 | 冷门预警 | 跑输预期 + 高偏差 |
| 🔴 | 高风险/冷门 | 平局陷阱 | 平局跑输 + 平局风险高 |
| 🔴 | 高风险/冷门 | 反买信号 | ROI偏低 + 偏差极大 |
| 🔴 | 高风险/冷门 | 庄家无视 | 跑输预期 + 无资金支撑 |
| 🟡 | 中性/观察 | 拉锯格局 | 势均力敌 + 平局信号 |
| 🟡 | 中性/观察 | 信号冲突 | 信号与基本面不符 |
| 🟡 | 中性/观察 | 博冷区 | 高偏差 + ROI≈0 |
| 🟢 | 顺向/价值 | 价值方向 | 跑赢预期 + ROI为正 |
| 🟢 | 顺向/价值 | 庄家防范 | 跑赢预期 + 资金集中 |
| 🟢 | 顺向/价值 | 顺势跟进 | 偏差小 + 趋势一致 |

**速查表**（填 3 个字段自动出标签）：
- 平局 `-60pp+` → 🔴 冷平预警
- 主胜 `+30pp+` → 🟢 主胜价值
- 客胜 `-40pp+` → 🟡 客胜诱盘

`style` 参数在"高偏差+ROI≈0"边界切换措辞（不改变红黄绿主色）：
`aggressive`=最醒目(红) / `conservative`=稳健(黄) / `strategy`=老手(反买小注博冷) / `balanced`=默认。

## 二、怎么用（3 个入口）

1. **分析中心实时表格**：每场 Top 信号自动带红绿灯标签——由"近邻经验频率 vs 市场隐含概率"算**有符号偏差**后生成。
   - 实测：弗罗瑞特 vs 科克本市 → 🔴 庄家无视；圣乔治城U20 vs 洛克达尔U20 → 🟢 客胜价值。
2. **比赛分析弹窗**：赛前相似检索结论旁实时显示标签（由近邻频率 vs 市场概率算偏差，调 `/api/signal-label`）。
3. **速查表端点（手动填字段）**：
   `GET /api/signal-label?direction=平局&deviation=-60.4&roi=0`
   → `{"tag":"🔴 冷平预警","meaning":"平局跑输预期极深, 势均力敌局面, 典型冷平温床"}`

## 三、改动文件

- `pipeline/signal_label.py`（新增，SSoT）— `compute_signal_label` + `signed_deviation_from_freq`
- `pipeline/analysis_center.py` — `_run_scan_impl` 注入 `signal_tag`（含 `_devig_3way`）
- `bridge_service.py` — 新增 `GET /api/signal-label`
- `gq/db.py` — `match_analysis_cache` 幂等加 `signal_color/signal_tag/signal_meaning`，`correct_analysis` 填充（按有符号偏差；缺失留 NULL 不伪造）
- `frontend/.../SignalTag.tsx`（新增）、`AnalysisCenter/index.tsx`、`MatchAnalysisModal.tsx` — 渲染

## 四、诚实说明

- 偏差统一采用**有符号**口径：`+=跑赢庄家预期`，`-=跑输庄家预期`。复盘库 `deviation_pct` 来自 `bet_records.value_gap`，WS3 数据缺口下多为 NULL → 复盘表标签多为空（不伪造）。
- 实时表格的偏差是"经验频率 − 市场概率"，是真实可计算的信号偏离，非臆造。
- 训练/推理端无改动；bridge 已重启（PID 28728）全量生效，`vite build` 通过。
