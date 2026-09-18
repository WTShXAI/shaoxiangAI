# 规范化待办清单 (P2, 2026-09-18 审计产出)

> 本轮 (2026-09-18) 已完成的规范化见 `prediction_refactor_checklist.md` §四/§六；
> 本清单是**有意不动**、留待后续窗口的项。处置前先复核现状。

## 一、bridge 端点瘦身 (~65 个前端零调用端点)

处置建议: 逐类确认无内部调用后删除或改 410。全程需 bridge 重启窗口 + 回归。
- 旧链路A: `/predict`、`/predict/simple`、`/predict/single` (engine.py 已挂牌 DEPRECATED, 下线时连文件归档)
- 冷门研究: `/api/cold-door*` ×3 (cold_door_model 保留但可挂 deprecated)
- 滚球族部分: `/api/live-goal-probe/backtest` 等非前端消费端点 (probe 本体保留)
- 组合/执行遗痕: `/api/sixline/analyze` (sixline_log 表已删, 端点现在会空转, 建议下线)
- 双源治理: `/api/water-signals`、`/api/template-deviation`、`/api/cross-book/*` (保留 vs 瘦身待定)
- 验证方法: 前端全量 grep + bridge 日志 7 天访问统计后再动手

## 二、前端废弃类型

`frontend/src/types/index.ts` (655 行): `Prediction`/`ModelComparison`/`TrainingStatus`/
`TeamFeatures`/`FixturePrediction`/`Handicap`/`OverUnder`/`Probabilities`/`ScorePrediction`
疑为旧预测页遗产。处置: `tsc --noEmit` + 全量 grep 确认引用数后删除。

## 三、部署反模式

- `ShaoxiangVite` 计划任务常驻 `npm run dev` (SYSTEM 跑 dev server) → 应改: bridge 托管 dist
  (已具备), 任务禁用。留 3000 端口调试需求时可手动起。
- `deploy/ci.yml` 未启用草稿与主 ci.yml 双份 → 删。
- `scripts/_start_bridge.ps1` (workbuddy Py3.13 + 端口9111) 与生产不符 → 归档。

## 四、模型治理欠账

- **D 类未注册但生产加载** (cs_empirical / ht_break_model / inplay_*_isotonic×23): 补登
  model_catalog 或 RESEARCH 区块 (2026-08-31 盘点建议未执行)
- M3 mispricing_detector (07-03) / M4 operator_* (07-29) 无独立 OOS 复验记录;
  M4 理论根基已被 microstructure R1 证伪 → 补验或降级
- 孤儿权重 `models/cs_rank_lgbm_v1.joblib` (全库零引用) → 归档
- `saved_models/model_registry.json` 已冻结; 引用旧 registry 的 3 个训练脚本已加冻结闸
- events.db 亿级 bak 表 (odds_snapshots_bak 41.7M / odds_changes_bak 12.1M) → 需停机窗口
  + VACUUM (37GB 库, 勿在线 VACUUM)

## 五、数据资产清理候选 (零生产引用, 处置前二次确认)

- `data/bets.db` (81KB, -shm 今日被触碰 → 先确认无进程持有)
- `data/odds_vectors.db`、`data/quant_trading.db`、`data/long_images.db`
- `data/_verify_sandbox.db` (355MB)、`electronic_poll_*.db`(20+)/`live_poll_*.db`(153MB)
- `data/` 根 10 张 png 截图、2 个 xlsx、`_fcb500.log`、`bridge_service.log`(13.8MB)
- `deploy/windows/install_guard.bat` 定义的 ShaoXiangAIGuard 未注册 (孤儿文档)
