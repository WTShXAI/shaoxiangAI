# dt_vote 22维硬编码 bug 修复验证报告

**日期**: 2026-08-08  
**影响**: `fl_model_1x2`（及 AH）此前线上从未真正推理，dt_vote 字段恒为 None

## 根因
`bridge_service.py` 的 `_live_predict()` 内 dt_vote 块原代码：
```python
_feats = _np_dt.array([[ph, pd, pa, 0]*7 + [0]*4)[:, :22]  # 22维伪造特征
_p = _dt.predict_proba(_feats)[0]   # 模型已是37维 → 抛异常被 except 吞
```
- 手搓 28 列数组截断到 22 维，**与训练时 37 维特征结构不对应**
- `predict_proba` 必然抛维度异常，被 `except Exception: pass` 静默吞掉
- 结果：`dt_vote` 永远为 None，`fl_model_1x2.joblib` 线上形同虚设

## 修复
dt_vote 块改用 `pipeline.fl_predictor.predict_from_odds(...)` —— 它内部走
`odds_structure_db.render_structure` + `odds_feature_library.extract_features`（动态 `N_FEAT=37`），
与训练时特征工程完全一致：

```python
_dt_out = _fl_pfo(
    h=oh, d=od, a=oa,                       # 1X2 原始赔率
    ou_line=ou_line, ou_over=None, ou_under=None,  # 无真实OU赔率, 不伪造水位
    ah_line=hcp_line, ah_home=hcp_home_odds, ah_away=hcp_away_odds,  # AH 真实赔率
    league=league, kickoff=None,
)
if _dt_out and _dt_out.get("1x2"):
    _p = _dt_out["1x2"]
    dt_vote = {"h_prob":..., "d_prob":..., "a_prob":..., "model":"fl_model_1x2", ...}
```

## 验证结果
重启 bridge（PID 31240），`POST /api/predict/live` 返回：

```json
"dt_model": {
  "h_prob": 0.3642, "d_prob": 0.2312, "a_prob": 0.4046,
  "agrees_poisson": false,
  "model": "fl_model_1x2",
  "note": "真实37维特征推理(已修22维硬编码bug)",
  "ah_prob": [0.5458, 0.4542]
}
```

- 数值与本地 `fl_predictor.predict_from_odds(2.10,3.30,3.60)` 输出**逐位一致** → 确认是真实推理，非伪造
- `ah_prob` 字段出现 → 用户刚增强的 AH 模型也一并真实生效

## 端点说明
- `dt_model` 字段在走 `_live_predict` 的端点（`/api/predict/live` 等）暴露
- `/api/predict/ranked` 走 `ranked_predictor`，响应结构不同、无 `dt_model` 字段（误测此端点会得到 null，非修复失败）

## 附带更新
- `pipeline/model_catalog.py`：M5（FL 结构库模型）状态 `degraded` → `active`，note 更新
- 融合策略不变：`fl_structure_weight` 默认 `0.0` → dt_vote 仅透明展示、不参与 ranked 融合（零回归风险）
