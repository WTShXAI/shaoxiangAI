# 哨响AI 运行纪律 v3.0（按实际重建，2026-09-24）

> **本文件为唯一权威纪律源（SSoT）。** IR-xx 编号体系与 sentinel 路由仪式已于 2026-09-24
> 由董事长最高权限指令废止，按系统**实际运行现实**逐条重建。旧文件快照见
> `archive/rules_voided_20260924/`（IRON_RULES.md / AGENTS.md / sentinel_ai_router.md /
> init.md / 钱代驾·安全卫士·智囊团·记录员 四模板）。
>
> 重建原则：旧纪律分三类——**承重**（安全/诚实/资产保全，按事实重建）、**累赘**
> （单 owner 系统下的流程仪式开销，删）、**过时**（已被现实推翻，删）。详见 §10/§11。

---

## 0. 定位锚（事实，不可 override 除非董事长书面）
哨响AI = 足球数据托管 + 概率诊断透镜。
截至 2026-09-24，六道关诚实门禁判定：
- candles_ensemble = INCONCLUSIVE（n=181，样本不足）
- market_baseline = NO EDGE（n=6081，ROI -0.60%，CI[-3.10%,+1.96%]）
- KNN = NO EDGE（n=2671，表观 +2.31% 但 G6 配对差 +2.41pp、CI 下限 -1.80pp ≤ 0 → 伪影）

**全系统无已验证可交易边缘**（与 2026-09-17 P0 FAILED、2026-09-23 KNN EDGE 证伪一致）。
任何 "edge / 稳胆 / 必赢 / 跟单" 输出一律视为伪造。

---

## 1. 诚实底线（承重 · 第一风险）
- 不伪造信号；不把未过六道关的模型当 edge 喊单；宁 PASS 不编造。
- 输出只解释概率偏差，不喊单（产品层已去喊单化）。
- 队名 / 比分 / 赛果只透传真实值，缺失则跳过，不补造。
- 直播中比赛，用户的实时比分是地面真相，网络查证只作辅证，绝不覆盖。
- 分析只做赔率结构解读，禁用"预测/必赢/稳胆"等确定性词。

## 2. 系统存活纪律（承重）
- 采集器 + prod_guardian + KNN 弹性写入器三者必须常在（计划任务 / detached 守护）。
- events.db tick 滞后 >1h 即告警；断流先查三守护（ws_collector / prod_guardian /
  efootball_probe）+ token，不盲目重启。
- 验证台自动化 156b2814 周期巡检：三态翻转 / 异常 / KNN 增长才上报，其余静默。

## 3. 外部依赖透明（承重）
- 乐鱼 token 只能由董事长浏览器登录产生，AI 不可自造；这是唯一未消除的 bus factor。
- 换号走 `gq-token-rotate` SOP：写 `gq/.env` → 守护 env 指纹检测 → ≤5min 自愈。
- 不得假装自治；缺 token 时如实上报，不编造"系统正常"。

## 4. 数据资产保全（承重）
- events.db 是 ¥85–110万 保底锚，**永不 `rm` / 覆盖 / 在线 VACUUM**（37GB 库须停机窗口 + VACUUM）。
- 分析只读生产库；写入只走采集器 / KNN 弹性写入器既有路径，不另开写方。
- 盘口线 SSoT = `opening_line.build_opening_lines()`；OU/AH 回测走
  `clean_outcomes` + `build_opening_lines`，禁直读 `match_outcomes` 盘口列。
- 假 0-0 守卫：`pipeline/settle.py::credible_1x2` 为结算唯一真源，断流定格 0-0 一律剔除。

## 5. 变更安全（承重）
- 改代码先 grep / DB 实测；`py_compile` + 回归测试 pass 才算 verified。
- 不擅动高风险待办（删 65 端点 / VACUUM / 亿级 bak 表清理）须专用窗口 + 回归
  （`docs/pending_cleanup_backlog.md` 标"有意不动"）。
- git push 须本地验证通过；不 push 未验证改动。
- 去水唯一入口 `pipeline/odds_math.py`（devig_power 幂法）；新代码禁再写本地去水。

## 6. 进程安全（承重 · 来自 09-24 实战教训）
- 停服务须 shim+worker 整树杀或交守护；**绝只杀 system Python312 worker**
  （留 shim 空转持锁 → 0 采集器）。
- 判双实例看 **PPID 链 + CPU 时间**，非进程数（venv shim CPU=0 父 + 系统 Python312 worker CPU 增长 子）。
- 守护铁律：只拉起、从不杀进程（09-19 杀拉循环教训）。

## 7. 日志 ASCII（承重）
- 关键路径禁 emoji / ⚠；GBK 曾崩整轮致 scheduled 不翻 live、前端"刚开赛不显示"。
- 日志一律 ASCII（`[!]`/`[WARN]`），`log()` 须 `try/except UnicodeEncodeError` 兜底。

## 8. 密钥不出（承重）
- token / key 对外一律 `XXX` 替换；真实值只落本地 gitignore 的 `.env`。

## 9. 已知坏逻辑禁区（承重 · 原 IR-32）
- 跨庄共识 / cross_book / multibook / leyu_value / bet_split **永久禁入**生产判定 / API / 前端 / 测试。
- 背景：单庄（乐鱼/GQ）宇宙下跨庄"真 edge"叙事均被证伪（+773% 伪 edge 事故、派生市场三轮全败、
  soft-line OOS 0.41 无效）；跨庄路线已随量化系统整体归档（`archive/quant_system_20260919/`）。
- `tests/test_no_crossbook.py` 自动守卫保留；看到上述字样进生产 import 图即违规。

## 10. 已删的累赘（明示 · 单 owner 系统下无谓开销）
- sentinel 六插件路由仪式（ORCH/DATA/ARCH/CODE/OPS/A11Y）+ 8 部门虚构架构。
- 钱代驾 / 安全卫士 / 智囊团 / 记录员 四层强制签核（代码须过安全卫士 P0-P3 才放行、
  任务须记录员复盘归档）。
- IR-31 强制 build_kb（每个 .md 必须跑 build_kb 进知识库）。
- 沙箱隔离强制（`~/哨响AI/沙箱/`）——本系统操作限定在 D:\Architecture，无需额外沙箱层。
- 改为：owner 自判 + 测试过即放行，不再走四层签核。

## 11. 已删的过时项（明示 · 现实已推翻）
- IR-04 OU 三件套"可下庄家定大小结论"——现实是全模型 NO EDGE，无结论可下。
- IR-17/18/19 价值层 edge 口径（margin 公式 / 单庄打折 / 校准可追溯）——价值层随量化系统删除，
  无 edge 可算；margin 数学 hygiene 并入 §5 变更安全。
- IR-26 操盘手三段框架——无实盘。
- pre_match_sync 赛前情报对齐协议——情报搜集（伤停/首发/天气）仍按需使用，但 D-Gate 已删，
  不再走 T-4h/T-1h/T-15min 三次刷新仪式。

## 12. 版本
- v3.0 2026-09-24 按董事长指令重建，废止 IR-xx 与路由仪式，SSoT 收敛至本文件。
