# 遗留问题清账登记册（老板指令：稳定运营 + 盈利 + 清遗留）

> 建立于 2026-09-24 20:2x，owner（赵统筹）执行。源清单 `docs/pending_cleanup_backlog.md`
> （原标注"有意不动留待后续窗口"）经老板指令转为**主动清账**。
> 纪律边界：`docs/DISCIPLINE.md` §4（数据资产保全：events.db 不 rm/不在线 VACUUM）、
> §5（高风险待办须专用窗口 + 回归）。盈利主引擎见 §P。

## 处置图例
- **NOW**：安全，立即执行（归档/删草稿/只读导出）。
- **WINDOW**：须停机窗口或回归测试，排期执行。
- **DROP**：确定为诚实损失/无价值，接受不处理。
- **PROFIT**：直接产生营收，最高优先。

---

## P. 盈利主引擎（最高优先）
| ID | 项 | 处置 | 说明 |
|----|----|------|------|
| P1 | **Package B 组装**（赔率 2.32亿行：odds_changes 43.8M + odds_snapshots 122.8M + bak 12.1M/41.8M） | NOW(只读导出) → 交老板外部法律确认(P2-5) → 另售 | 全系统最高价值资产，保底锚 ¥85–110万 的实体支撑。**导出器已建** `scripts/p2_build_package_b.py`（严格只读 events.db）。样本验证通过：抽样 40万行 → 33MB `deliverables/p2_package_b/p2_package_B.sqlite`(4.2s)。**全量导出待老板确认 + 法律门禁 P2-5**（~2.2亿行/多 GB，重型）。 | ✅ 导出器就绪+样本验证 2026-09-24；全量未跑(门禁) |
| P2 | Package A+C 已交付（p2_package_A_C.sqlite 110MB/21表/503,610行） | DONE | 硬排除 users/赔率表/match_outcomes，无 PII。可即时挂牌。 |
| P3 | 诊断服务产品化（概率偏差解释，非喊单） | WINDOW | 前端"预测中心"已在，需包装成对外交付物。 |

## A. 安全可立即执行（NOW）
| ID | 项 | 动作 | 风险 |
|----|----|------|------|
| A1 | `scripts/efootball_deep_dive.py`（游离未跟踪，仅自引用） | 归档 archive/ | 零（efootball 深潜已证伪 NO EDGE，脚本无复用价值） | ✅ 2026-09-24 已归档 |
| A2 | `scripts/_start_bridge.ps1`（Py3.13+9111，与生产不符） | 归档 archive/ | 零 | ✅ 已在 `archive/temp_debris_20260919/`（09-19 清理时归档，非 scripts/ 下） |
| A3 | `models/cs_rank_lgbm_v1.joblib`（全库零引用孤儿权重） | 归档 archive/models/ | 零（grep 确认零引用） | ✅ 2026-09-24 已归档 archive/models/ |
| A4 | `deploy/ci.yml` 与主 ci.yml 双份草稿 | 删草稿 | 低 | ✅ n/a — `deploy/ci.yml` 不存在（仅主 .github/workflows 有 CI），无需删 |

## B. 须专用窗口（WINDOW）
| ID | 项 | 风险/价值 | 窗口要求 |
|----|------|-----------|----------|
| B1 | bridge ~65 个前端零调用端点瘦身 | 中/低（误删内部调用会崩） | 前端全量 grep + 7天访问统计 + bridge 重启回归 |
| B2 | 前端废弃类型（Prediction/ModelComparison/... 655行） | 低/低 | `tsc --noEmit` + grep 确认引用后删 |
| B3 | ShaoxiangVite 计划任务常驻 `npm run dev`（应改 bridge 托管 dist） | 低/中 | 构建 dist + 任务改手动/禁用 |
| B4 | D 类模型未注册（cs_empirical/ht_break_model/inplay_*_isotonic×23） | 低/低 | 补登 model_catalog 或 RESEARCH 区块 |
| B5 | M3/M4 无独立 OOS 复验（M4 理论根基已被证伪） | 低/低 | 补验或降级 |
| B6 | events.db 亿级 bak 表（odds_snapshots_bak 41.8M / odds_changes_bak 12.1M） | 中/低 | **37GB 库须停机窗口 + VACUUM**，禁在线 |
| B7 | data/ 零引用遗留 db（bets.db/odds_vectors.db/quant_trading.db/_verify_sandbox.db/electronic_poll_*/live_poll_*） | 中/低 | 二次确认无进程持有后归档/删 |

## C. 诚实接受不处理（DROP）
| ID | 项 | 原因 |
|----|----|------|
| C1 | 15095 finished+score 无结论缺口 | 采集器崩溃期终场未回填；`query_match` 对 finished 硬性 applicable=False 防未来信息泄漏，违规回填污染账本。诚实损失。 |
| C2 | verification.db ledger 表名差异 | 报告正常产出（KNN n 可读），仅内部表名非 `ledger`，不影响结论。 cosmetic。 |

---

## 执行顺序（owner）
1. P1 Package B 只读导出（盈利，NOW）。
2. A1–A4 安全归档/删（NOW）。
3. B 类排期窗口（不停运前提下分批）。
4. 持续：稳定运营 + 样本累积（验证台 156b2814）。
