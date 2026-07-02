# 变更日志

> 产品版本: v4.1.0 → v6.0.0
> D-Gate 引擎版本: v5.0 → v5.3

---

## [v6.0.0] — 2026-07-01

### 新增
- **全管线升级 v5.15-v5.23**: DC λ + HCP template scores (GitHub f4e94fe)
- **React 18 前端**: TypeScript + Vite + TanStack Query, 5页面, 暗黑主题
- **WorkBuddy 专家团**: 35人团队, 12部门, plugin.json 完整注册
- **模型注册中心**: model_registry.json (4模型: v4.1 + ensemble + NN + JEPA)
- **72场WC2026数据**: SQLite导入, 33,498场比赛, 5 upcoming ready

### 修复
- P0: Token认证中间件验证 (backend/main.py)
- P0: `_asyncio` 导入 (WebSocket修复)
- P0: 数据库路径错误 (6个Mixin的DB_PATH)
- P0: `get_db()` 函数恢复 (database/db_manager.py)
- P0: `get_next_scheduled_match()` 补充 (crud_match_mixin)
- P0: 版本号硬编码 → settings.APP_VERSION (monitor.py)
- P1: Swagger生产模式禁用, README全面更新, 配置DEPRECATED标记
- P1: 5个缺失依赖安装, 模型路径修正, 40个__pycache__清理

### 变更
- 版本号统一: v5.10.0 → v6.0.0 (与GitHub对齐)
- 配置中心: config/README.md 文档化

---

## [v5.10.0] — 2026-06-28

### 新增
- **让球盘口代码恢复**: 恢复 sporttery_hcp/hcp_depth/LINKAGE_MATRIX/classify_hcp/HCP联动块/_hcp_disp 模块
- **屠杀预警优化**: 优先于已出线保守策略
- **容器化部署**: 添加 Dockerfile + docker-compose.yml 支持
- **SQLite WAL 模式**: 添加数据库备份脚本和 WAL 模式支持

### 修复
- 清理 `run_6_28_predictions.py` 中 name_trap 残留
- P0 级别改进全部保留

---

## [v5.3] — 2026-06-27

### 新增
- **DrawGate + DrawExpert 合并**: D-F1 突破 0.31 阈值
- **全链路联动管道 (Full Linkage Predictor)**: `pipeline/full_linkage_predictor.py` — 集成 DrawGate、HCP、OU、Poisson 等多种预测引擎
- **哨响AI 专家团 v2.2**: 多智能体协同预测

### 变更
- 从 v5.2.14 的 D-Gate 五层引擎演进
- Tournament Dynamics R1-R4 集成
- 淘汰赛预测器 (Knockout Predictor) 增强

### 修复
- D-Gate S7 penalty 移除
- 代码库清理

---

## [v5.2.14] — 2026-06-26

### 新增
- **D-Gate 五层引擎**: `rules/d_gate_v52.py` — 分层规则引擎架构
- **Tournament Dynamics**: 赛事阶段动态调整 (R1-R4)
- **AutoPipeline**: 自动预测 + 回测管道
- **知识库 (KB)**: `rules/football_kb.yaml` 知识库系统
- **DrawExpert v1**: 独立平局预测模型 `draw_expert_v1.joblib`

### 变更
- 全量优化 — 多信号引擎 (`multi_signal_engine.py`)
- rules 规则层重构
- 配置参数标准化 (rule_params.json)

---

## [v5.2.13] — 2026-06-25

### 修复
- D-Gate S7 penalty 移除
- 代码库清理
- 依赖冲突解决

### 变更
- 模型注册表 v2b 引入
- 特征压缩

---

## [v5.2.7] — 2026-06-24

### 新增
- **D-Gate 引擎引入**: 基于 Elo 的动态门控决策引擎
- **大小球 (OU) 校准**: Over/Under 概率校准系统
- **API 积分系统**: 预测工作流积分追踪
- **ModelBridge v2.0**: 模型锁定、硬编码检测、审计字段

### 变更
- Elo 排名系统上线
- API v1 路由规范化
- 预测工作流重构

---

## [v4.1.0] — 2026-06-20

### 新增
- **Flask → FastAPI 迁移**: 异步 API 框架，自动 OpenAPI 文档
- **双层架构**: 后端 API + 前端 SPA 分离
- **LAMF 多智能体工作流**: LangGraph StateGraph 编排
  - Commander (gemma4:12b): 意图路由 + 结果汇总
  - DataAgent (deepseek-r1:8b): 数据分析 + 特征计算
  - MathAgent (phi4:14b): 概率计算 + 三层降级
  - Explainer (qwen3:8b): 中文解释 + 用户交互
- **前端 v5.0 深空暗黑主题**: SPA 纯静态前端
- **工程团队扩展**: 正式组建架构/SRE/测试/文档工程团队
- **UnifiedPredictor**: 统一预测接口

### 架构决策记录
- ADR-001: Flask → FastAPI 迁移
- ADR-002: D-Gate v5 引擎设计
- ADR-003: 全链路联动管道设计
- ADR-004: 双层架构保留决策

---

> 完整架构决策记录请参阅 `docs/adr/` 目录。
