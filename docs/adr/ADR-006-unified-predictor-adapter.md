# ADR-006: UnifiedPredictor 统一预测接口设计

**状态**: ✅ 已实施

**日期**: 2026-06-25

**决策者**: 算法团队

---

## 背景

系统中存在多种预测引擎调用方式：Direct Predictor、D-Gate、AutoPipeline、外部 API 等。缺乏统一调用接口导致调用方代码重复，且难以做请求级监控和审计。

## 决策

设计 UnifiedPredictor 适配器模式，为所有预测引擎提供统一调用接口。所有外部预测请求通过 UnifiedPredictor 路由，内置审计、监控和降级逻辑。

## 选项对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **UnifiedPredictor (适配器)** | 统一监控/审计/降级、调用方无需感知引擎细节 | 引入间接层 |
| 直接调用各引擎 | 路径短、无间接开销 | 调用方代码重复、难以统一监控 |
| 事件驱动总线 | 松耦合 | 复杂度高、延迟增加 |

## 接口设计

```python
class UnifiedPredictor:
    def predict(match_id, engine_type, params) -> PredictionResult
    def get_engine_info(engine_type) -> EngineMetadata
    def list_engines() -> List[EngineInfo]
```

## 后果

**正面**:
- 所有预测请求经过统一审计和监控
- 调用方只需指定 engine_type，无需关心实现
- 支持 A/B 测试（同一请求路由到不同引擎）

**负面**:
- 适配器层引入轻微性能开销
- 需维护引擎注册表
