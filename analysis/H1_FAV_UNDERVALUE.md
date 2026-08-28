# H1 — 赛前 Favorite 被低估检测器：实测结论

> 哨响AI · 2026-08-13 · 验证"涛哥洞察：庄家在 1X2 开盘隐藏强队真实战力，赛前可识别 +EV"

## 0. 一句话结论


真 +EV 只来自 **跨庄 / 跨市场软线价差**（见 `pipeline/leyu_value_signal.py`），不来自对单庄赔率的建模。

---

## 1. 做了什么（按方案 A 落地）

| 组件 | 文件 | 状态 |
|---|---|---|
| Dixon-Coles 队力公平模型 | `analysis/dixon_coles.py` | ✅ 稳定收敛（参考队锚定 + 正确 NLL 最小化） |
| H1 检测器 | `analysis/h1_fav_undervalue_detector.py` | ✅ 含 `detect` / `backtest` / `backtest_drift` / 引擎挂载 |
| 引擎集成 | `pipeline/reverse_odds_engine.py` | ✅ 挂载 `detect_prematch_fav_undervalue`（try/except 不阻断其他功能） |
| bank 缓存 | `analysis/_h1_bank.pkl` | ✅ 214 个联赛 DC 模型 |
| 回测结果 | `analysis/h1_backtest_result.json` | ✅ 已写 |
| paper-trading 台账脚本 | `analysis/h1_paper_trading.py` | ⚠️ 机械可运行，但**信号未验证为 +EV，不得用于真钱** |

**关键修复（本轮回测闸门）：**
- 源数据 `historical_matches.league_name` 跨年份命名不一致——训练(<2023)为干净名（`英超`），测试(≥2023)为 `22/23英超第18轮`。新增 `normalize_league()` 去赛季前缀+轮次/阶段后缀，使训练/测试落到同一 canonical key，bank 命中率从 0% 升至 **85.5%**（33651/39371）。
- 修复 `load_historical` 因加列导致的索引错位（train/test 切分误用 `close_odds` 浮点比较）与 `__main__` 的 `res["und"]`→`res["under"]` 崩溃。

---

## 2. 回测结果（out-of-time，训练<2023 / 测试≥2023，39,371 场）

### 2.1 H1 检测器本身（Dixon-Coles 公平概率 vs 开盘去水隐含）

| 策略 | 样本 | 胜率 | ROI | 解读 |
|---|---|---|---|---|
| 无脑买 favorite @开盘 | 33,651 | 51.0% | **−4.62%** | 单庄 margin，符合预期 |
| **H1 undervalued**（fair−implied > 1.5%） | 3,283 | 39.5% | **−7.39%** | 比无脑更差 |
| edge → favorite 胜 | — | — | AUC **0.380** | **反预测**（<0.5） |

→ DC 公平模型说"更该赢"的 favorite，实际赢得更少。模型在单庄赔率面前**没有信息优势**。

### 2.2 对照探针：先验 +4.4% 到底从哪来？

先验 `PREMATCH_DEVIATION` 报的 +4.4% 描述是"favorite 开盘被低估**变短**时"。"变短"= 开盘→收盘漂移，属**赛前可得**（收盘在开赛前已确定）。补做探针：

| 策略 | 样本 | 胜率 | ROI | 可部署？ |
|---|---|---|---|---|
| 全 favorite @开盘 | 39,348 | 51.7% | −4.70% | 是（但亏） |
| 全 favorite @收盘 | 39,348 | 51.7% | −4.63% | 是（但亏） |
| favorite **变短** @开盘（用收盘选） | 17,438 | 57.4% | **+6.31%** | ❌ look-ahead 假象 |
| favorite **变短** @收盘（真实可下注版） | 17,438 | 57.4% | **−3.08%** | ✅ 可部署 → 仍亏 |

**决定性发现：**
- +6.31% 是**假象**——它用"未来收盘"去筛选，却按"更早的开盘价"结算。你不可能在开盘时预知谁会变短。
- 真正能下注的版本（临近收盘、favorite 变短时按**收盘价**下）ROI = **−3.08%**，边缘完全消失。
- 即：市场把"favorite 被确认"的信息有效计入了收盘价，赛前无残留 +EV。

---

## 3. 与项目口径的互证


本次独立 out-of-time 验证给出量化印证：
- 模型(DC) vs 单庄开盘 → AUC **0.38**（模型比单庄**差**，且方向反了）。
- 跨庄共识（尖庄源）才是项目保留的真 edge 通道（`leyu_value_signal.py` 已实现，需注册尖庄源点亮）。

---

## 4. 对部署的影响（重要）

1. **不**把 `h1_paper_trading.py` 的输出当投注清单。其 flag 来自 DC-undervalue，实测 **−7.39% / AUC 0.38**，是反预测信号。若误当 +EV 下注会稳定亏损。
2. 单庄 1X2 赛前信号线**关闭**。H1 检测器降级为"研究/护栏"工具：DC-undervalue 实测反预测，可作为"**勿下**"过滤（与项目口径一致），而非"**下注**"信号。
3. **真 +EV 路径**：跨庄/跨市场软线价差。即 `pipeline/leyu_value_signal.py`（分歧 ≥ 本庄 margin≈11% 触发，甜点 15–25%，仅押 H/A，赔率 ≤6.5，白名单联赛，FLAT 注码）。当前乐鱼纯单庄分歧恒 0 → 需注册尖庄源点亮 +EV。

---

## 5. 复现命令

```bash
# 重建 bank + 回测 (含 drift 探针)
python analysis/h1_fav_undervalue_detector.py

# 仅用缓存重跑回测
python -c "from h1_fav_undervalue_detector import *; \
  bank=get_or_build_bank(False); _,t=load_historical(); \
  print(backtest(t,bank)); print(backtest_drift(t))"

# paper-trading 台账 (机械可跑, 但 flag 非 +EV 验证, 勿真钱)
python analysis/h1_paper_trading.py --league 中超
```

---

## 6. 下一步建议

- 关闭"单庄 1X2 赛前 +EV"线；将研发精力集中在**跨庄共识价差**（尖庄源接入）这一项目保留通道。
- 若仍想挖掘"赛前偏差"，唯一合规方向是 **跨市场**（不同庄家的开盘价差异），而非"模型 vs 单庄"。
- H1 模块保留作研究基线 + 反预测护栏；其结论已同步进项目记忆（单庄模型反预测量化印证）。
