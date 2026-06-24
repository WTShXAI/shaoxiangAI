# 哨响AI — Prompt 模板文档

> 版本: v4.1 | 更新: 2026-06-12

---

# LAMF 模型使用指南 (v4.1.0)

## 模型总览表

| 模型名 | 角色 | 核心职责 | 调用方式 |
| :--- | :--- | :--- | :--- |
| **gemma4:12b** | 指挥官 (Commander) | 理解意图、制定计划、指派Agent、汇总结果 | `OllamaLLM(model="gemma4:12b")` |
| **deepseek-r1:8b** | 数据分析师 (DataAnalyst) | 分析比赛数据、识别趋势、代码生成 | `OllamaLLM(model="deepseek-r1:8b")` |
| **phi4:14b** | 数学家 (MathAgent) | 概率计算、风险评估、Kelly准则 | `OllamaLLM(model="phi4:14b")` |
| **qwen3:8b** | 解释器 (Explainer) | 中文通俗解释、用户交互、文案润色 | `OllamaLLM(model="qwen3:8b")` |

## 单模型详细规范

### gemma4:12b (指挥官)
- **禁止做**: 直接回答数值计算、生成具体代码逻辑。
- **必须做**: 输出结构化指令 (JSON/YAML)，例如:

```json
{
  "task_type": "predict",
  "assigned_agents": ["data_analyst", "math_agent"],
  "reason": "用户询问比赛结果，需先分析数据再算概率"
}
```

### deepseek-r1:8b (数据分析师)
- **输入**: 清洗后的比赛特征数据 (90+维)。
- **输出**: 数据洞察、异常指标、趋势描述。

### phi4:14b (数学家)
- **输入**: 分析师提供的数据 + 规则。
- **输出**: 胜/平/负概率 (格式化字符串)，风险等级 (INVEST/WATCH/PASS)。

### qwen3:8b (解释器)
- **输入**: 数学家的结论 + 分析师的洞察。
- **输出**: 面向用户的 3-5 句通俗中文解释。

---

## 概述

本文档记录了哨响AI（footballAI）多智能体系统中所有 Agent 使用的 Prompt 模板。系统采用 LangGraph 多智能体工作流，Prompt 逻辑内嵌于各 Agent 类的方法中，**没有独立的 PromptTemplate 定义文件**。

### 系统架构

```
用户输入 → CommanderAgent(路由) → DataAgent(数据分析) → MathAgent(概率计算) → Explainer(中文解释) → CommanderAgent(汇总) → 最终输出
```

### 降级策略总览

所有 Agent 均遵循 **LLM 优先 → 规则/模板降级** 的策略。当 Ollama 服务不可用时，系统自动切换到基于规则的硬逻辑，确保功能不中断。

| Agent | LLM 模型 | 降级方案 |
|-------|----------|----------|
| CommanderAgent | gemma4:12b | 关键词匹配 + 规则路由 |
| DataAnalystAgent | deepseek-r1:8b | 阈值规则特征分析 |
| MathAgent | phi4:14b | ModelBridge ML → 规则计算（三层降级） |
| ExplainerAgent | qwen3:8b | 字符串模板拼接 |

---

## Agent 1: CommanderAgent

**源文件**: `agents/commander.py`
**模型**: `gemma4:12b`
**职责**: 分析用户输入的任务类型，决定调用哪些专家，并最终汇总专家结果

### 1.1 任务路由 Prompt（`_analyze_with_llm`）

**触发时机**: `invoke()` → `analyze_task()` → `_analyze_with_llm()`
**用途**: 根据用户输入判断任务类型，决定调用哪些专家

```
你是足球AI系统的总指挥官。根据用户输入，判断任务类型并决定需要调用哪些专家。

可选任务类型: predict(预测比赛), analyze(数据分析), explain(中文解释), risk(风险评估), general(通用)

可选专家: data_agent(数据分析/特征工程), math_agent(概率/风险计算), explainer(中文解释)

用户输入: {user_input}

请用JSON格式回复，包含以下字段:
{"task_type": "类型", "experts_to_call": ["专家列表"], "reasoning": "判断理由"}

只输出JSON，不要其他内容。
```

**变量占位符**:

| 变量 | 来源 | 说明 |
|------|------|------|
| `{user_input}` | `state["input"]` | 用户原始输入文本 |

**预期输出格式**:
```json
{
  "task_type": "predict | analyze | explain | risk | general",
  "experts_to_call": ["data_agent", "math_agent", "explainer"],
  "reasoning": "判断理由"
}
```

**任务类型 → 专家映射规则**（`TASK_EXPERT_MAP`）:

| 任务类型 | 调用专家 |
|----------|----------|
| predict | data_agent, math_agent, explainer |
| analyze | data_agent, math_agent |
| explain | explainer |
| risk | math_agent, explainer |
| general | data_agent, math_agent, explainer |

### 1.2 结果汇总 Prompt（`_synthesize_with_llm`）

**触发时机**: `synthesize()` → `_synthesize_with_llm()`
**用途**: 汇总各专家的分析结果，生成最终决策

```
你是足球AI系统的总指挥官。请汇总以下专家的分析结果，做出最终决策。

数据分析专家结果: {data_analysis}

数值分析专家结果: {math_analysis}

中文解释专家结果: {explanation}

请用JSON格式回复:
{"final_prediction": {"home": 概率, "draw": 概率, "away": 概率}, "confidence": 置信度, "decision": "INVEST/WATCH/PASS", "summary": "一段话总结", "key_factors": ["关键因素列表"]}

只输出JSON。
```

**变量占位符**:

| 变量 | 来源 | 说明 |
|------|------|------|
| `{data_analysis}` | `expert_results["data_analysis"]` | DataAgent 的分析结果 JSON（截断至 500 字符） |
| `{math_analysis}` | `expert_results["math_analysis"]` | MathAgent 的分析结果 JSON（截断至 500 字符） |
| `{explanation}` | `expert_results["explanation"]` | Explainer 的解释结果 JSON（截断至 500 字符） |

**预期输出格式**:
```json
{
  "final_prediction": {"home": 0.45, "draw": 0.28, "away": 0.27},
  "confidence": 0.7,
  "decision": "INVEST | WATCH | PASS",
  "summary": "一段话总结",
  "key_factors": ["关键因素1", "关键因素2"]
}
```

**决策阈值说明**:

| 决策 | 条件 |
|------|------|
| INVEST | confidence >= 0.6 且 max_prob >= 0.45 |
| WATCH | confidence >= 0.4 或 max_prob >= 0.38 |
| PASS | 其他情况 |

### 1.3 规则降级方案（`_analyze_with_rules` / `_synthesize_with_rules`）

**触发条件**: Ollama 不可用或 LLM 调用失败

**路由规则** — 基于关键词匹配：

| 任务类型 | 匹配关键词 |
|----------|-----------|
| risk | 风险、价值、赔率、凯利、risk、value、odds、kelly |
| predict | 预测、谁赢、比分、胜负、推荐、投注、predict、bet |
| analyze | 分析、数据、特征、趋势、analyze、data、feature |
| explain | 解释、为什么、说明、explain、why、reason |
| general | 默认（有比赛数据时自动升级为 predict） |

> 优先级：risk > predict > analyze > explain > general

---

## Agent 2: DataAnalystAgent

**源文件**: `agents/data_agent.py`
**模型**: `deepseek-r1:8b`
**职责**: 数据分析与特征工程，解读比赛数据中的关键指标

### 2.1 数据分析 Prompt（`_analyze_with_llm`）

**触发时机**: `invoke()` → `analyze_features()` → `_analyze_with_llm()`
**用途**: 分析比赛数据和特征指标，识别异常信号和趋势

```
你是足球数据分析专家。请分析以下比赛数据和特征指标。

比赛信息:
{match_info}

特征指标:
{feature_desc}

请分析:
1. 哪些特征指标最值得关注？
2. 数据中是否有异常信号？
3. 对比赛走势的判断？

用JSON格式回复:
{"analysis": "整体分析", "key_findings": ["发现1", "发现2"], "feature_insights": {"特征名": "解读"}, "confidence": 0.7}

只输出JSON。
```

**变量占位符**:

| 变量 | 来源 | 生成方式 |
|------|------|----------|
| `{match_info}` | `match_data` | `_describe_match()` 格式化输出（主队/客队/联赛） |
| `{feature_desc}` | `features` | `_describe_features()` 格式化输出（特征中文名 + 值） |

**match_info 格式示例**:
```
主队: 曼城
客队: 利物浦
联赛: 英超
```

**feature_desc 格式示例**:
```
- 实力差值(a1): 0.25
- 主场优势(a2): 0.65
- 近期状态差(a3): 0.55
- 赔率背离度(a4): 0.12
```

**预期输出格式**:
```json
{
  "analysis": "整体分析文本",
  "key_findings": ["发现1", "发现2", "发现3"],
  "feature_insights": {"a1": "主队略占优", "a4": "赔率与模型背离"},
  "confidence": 0.7
}
```

### 2.2 特征名称映射表（`FEATURE_NAMES`）

Prompt 中使用的特征中文映射，供 `_describe_features()` 方法引用：

| 特征键 | 中文名 | 类别 |
|--------|--------|------|
| a1 | 实力差值 | 核心6维 |
| a2 | 主场优势 | 核心6维 |
| a3 | 近期状态差 | 核心6维 |
| a4 | 赔率背离度 | 核心6维 |
| a5 | 进球期望差 | 核心6维 |
| a6 | 攻防效率差 | 核心6维 |
| sigma_trap | 市场陷阱信号 | 市场信号 |
| lambda_crush | 赔率压缩度 | 市场信号 |
| epsilon_senti | 市场情绪偏移 | 市场信号 |
| rank_diff_factor | 排名差距因子 | 辅助因子 |
| form_momentum | 状态动能 | 辅助因子 |
| h2h_factor | 交锋历史因子 | 辅助因子 |
| rank_factor | 排名因子 | 辅助因子 |
| form_factor | 状态因子 | 辅助因子 |
| aerial_advantage | 空中优势 | 战术因子 |
| press_intensity | 逼抢强度 | 战术因子 |
| card_risk | 红黄牌风险 | 战术因子 |
| beta_dev | 盘口偏差 | 市场信号 |
| delta_fatigue | 疲劳度差异 | 辅助因子 |

### 2.3 规则降级方案（`_analyze_with_rules`）

**触发条件**: Ollama 不可用或 LLM 调用失败

基于阈值的规则分析：

| 特征 | 阈值 | 触发发现 |
|------|------|----------|
| a1（实力差值） | \|a1\| > 0.3 | "实力差距较大" |
| a4（赔率背离度） | \|a4\| > 0.2 | "赔率背离显著" |
| sigma_trap（市场陷阱） | \|sigma\| > 0.15 | "市场陷阱信号" |
| h2h_factor（交锋历史） | \|h2h\| > 0.3 | "交锋历史偏向" |
| form_momentum（状态动能） | \|form\| > 0.2 | 仅写入 feature_insights |
| delta_fatigue（疲劳度） | fatigue > 1.5 | "客队疲劳度较高" |

置信度计算：`min(0.85, 0.4 + n_signals * 0.08)`

---

## Agent 3: MathAgent

**源文件**: `agents/math_agent.py`
**模型**: `phi4:14b`
**职责**: 概率计算、赔率分析、风险评估

### 三层降级策略

```
Level 1: Ollama LLM (phi4:14b)     ← 自然语言推理 + 概率输出
    ↓ LLM 不可用时
Level 2: ModelBridge → EnsembleTrainer  ← 真实 ML 集成模型
    ↓ ModelBridge 不可用时 (Fail-Fast!)
Level 3: 规则计算                    ← 硬编码基础值 + 特征微调（仅最后兜底）
```

> **注意**: Level 2 (ModelBridge) 不可用时采用 **Fail-Fast** 策略，会抛出 `RuntimeError` 终止启动，而非静默降级到规则计算。Level 3 规则计算仅在 LLM 不可用且作为最后保底时使用。

### 3.1 Level 1 — 概率分析 Prompt（`_analyze_with_llm`）

**触发时机**: `invoke()` → `calculate_probabilities()` → `_analyze_with_llm()`
**用途**: 使用 LLM 计算主胜/平局/客胜概率和风险评估

```
你是足球概率分析专家。请根据以下数据进行概率计算和风险评估。

比赛信息:
{match_info}

特征数据:
{feature_desc}

请计算:
1. 主胜/平局/客胜概率
2. 风险等级(LOW/MEDIUM/HIGH)
3. 是否有价值投注

用JSON格式回复:
{"probabilities": {"home": 0.45, "draw": 0.28, "away": 0.27}, "risk_level": "MEDIUM", "value_bets": [{"outcome": "home", "reason": "..."}], "confidence": 0.7}

只输出JSON。概率之和必须等于1.0。
```

**变量占位符**:

| 变量 | 来源 | 生成方式 |
|------|------|----------|
| `{match_info}` | `match_data` | `_describe_match()` 格式化输出 |
| `{feature_desc}` | `features` | 逐行 `key: value` 格式化 |

**预期输出格式**:
```json
{
  "probabilities": {"home": 0.45, "draw": 0.28, "away": 0.27},
  "risk_level": "LOW | MEDIUM | HIGH",
  "value_bets": [{"outcome": "home", "reason": "模型概率显著高于隐含概率"}],
  "confidence": 0.7
}
```

> **后处理**: LLM 输出的概率会经过归一化处理，确保 `home + draw + away = 1.0`。

**风险等级阈值**:

| 等级 | 条件 |
|------|------|
| LOW | max_prob >= 0.50 |
| MEDIUM | max_prob >= 0.38 |
| HIGH | max_prob < 0.38 |

### 3.2 Level 1 — 风险评估 Prompt（`_assess_risk_with_llm`）

**触发时机**: `assess_risk()` → `_assess_risk_with_llm()`
**用途**: 根据模型概率和赔率进行 Kelly 准则风险评估

```
你是足球投注风险评估专家。根据模型概率和赔率评估风险。

模型概率: {prediction}
赔率: {odds}

用JSON格式回复:
{"risk_level": "LOW/MEDIUM/HIGH", "kelly_fraction": 0.05, "value_bets": [{"outcome": "home", "edge": 0.08}], "recommendation": "建议"}

只输出JSON。
```

**变量占位符**:

| 变量 | 来源 | 说明 |
|------|------|------|
| `{prediction}` | `prediction` 字典 | 模型概率 JSON |
| `{odds}` | `odds` 字典 | 赔率数据 JSON |

**预期输出格式**:
```json
{
  "risk_level": "LOW | MEDIUM | HIGH",
  "kelly_fraction": 0.05,
  "value_bets": [{"outcome": "home", "edge": 0.08}],
  "recommendation": "建议文本"
}
```

### 3.3 Level 2 — ModelBridge（ML 集成模型，无 Prompt）

**触发时机**: LLM 不可用时自动降级
**实现方式**: `model_bridge.predict(match_data)` — 纯 ML 推理
**后处理**: `domain_rules.apply_domain_knowledge()` — 领域知识修正

此层不使用文本 Prompt，而是直接将特征输入预训练的 EnsembleTrainer 模型获取概率预测。输出概率会经过领域知识库（DomainKB）修正后返回。

### 3.4 Level 3 — 规则计算（无 Prompt，纯算法逻辑）

**触发时机**: LLM 和 ModelBridge 均不可用时（仅作最后保底）

基础概率值：

| 结果 | 基础概率 |
|------|----------|
| 主胜 | 0.40 |
| 平局 | 0.28 |
| 客胜 | 0.32 |

特征微调系数：

| 特征 | 主胜调整 | 客胜调整 |
|------|----------|----------|
| a1（实力差值） | +a1 * 0.35 | -a1 * 0.25 |
| a2（主场优势） | +(a2-0.5) * 0.15 | — |
| a3（近期状态差） | +(a3-0.5) * 0.12 | — |
| h2h_factor（交锋历史） | +h2h * 0.10 | -h2h * 0.08 |
| form_momentum（状态动能） | +form * 0.08 | — |
| rank_diff_factor（排名差距） | +rank_diff * 0.12 | — |

> **安全约束**: 规则 Fallback 的输出会经过 `_check_hardcoded_probability_in_rules()` 检测。若最终概率接近 H=0.40/D=0.28/A=0.32（±0.02 容差，≥2个匹配），直接抛出 `RuntimeError` 终止（Fail-Fast），拒绝产出不可靠概率。

### 3.5 泊松分布比分预测

**方法**: `_poisson_predict(features)`

泊松参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| BASE_LAMBDA | 2.72 | 基础进球期望 |
| HOME_ADVANTAGE | 1.08 | 主场优势系数 |
| MAX_GOALS | 6 | 最大进球数上界 |

进球期望计算：
- `lambda_home = BASE_LAMBDA * HOME_ADVANTAGE / 2 + a1 * 0.5 + a5 * 0.3`
- `lambda_away = BASE_LAMBDA / 2 - a1 * 0.3 - a5 * 0.2`
- 范围限制：`[0.3, 4.0]`

输出：Top 5 最可能比分及其概率。

### 3.6 价值投注检测

**方法**: `_detect_value_bets(probs, match_data)`

从 `match_data` 中提取赔率（优先使用 `odds_home/draw/away`，其次使用 `b365_h/d/a`），计算 Edge：

```
implied_prob = 1.0 / odd
edge = model_prob - implied_prob
```

当 `edge > 0.05`（5% 以上优势）时标记为价值投注。

---

## Agent 4: ExplainerAgent

**源文件**: `agents/explainer.py`
**模型**: `qwen3:8b`
**职责**: 将预测结果翻译为用户友好的中文，生成推荐理由和投注建议

### 4.1 中文解释 Prompt（`_explain_with_llm`）

**触发时机**: `invoke()` → `explain()` → `_explain_with_llm()`
**用途**: 用通俗易懂的中文解释预测结果

```
你是足球分析解说员，擅长用通俗易懂的中文解释预测结果。

比赛信息:
{match_info}

数据分析:
{data_text}

概率分析:
{math_text}

请用中文生成:
1. 一段话解释预测结果（100字以内）
2. 投注建议
3. 3-5个关键要点

用JSON格式回复:
{"explanation": "中文解释", "recommendation": "建议", "key_points": ["要点1", "要点2", "要点3"]}

只输出JSON。
```

**变量占位符**:

| 变量 | 来源 | 生成方式 |
|------|------|----------|
| `{match_info}` | `match_data` | `_describe_match()` 格式化输出 |
| `{data_text}` | `data_analysis` | JSON 序列化，截断至 400 字符 |
| `{math_text}` | `math_analysis` | JSON 序列化，截断至 400 字符 |

**预期输出格式**:
```json
{
  "explanation": "中文解释文本（100字以内）",
  "recommendation": "投注建议文本",
  "key_points": ["要点1", "要点2", "要点3"]
}
```

### 4.2 模板降级方案（`_explain_with_template`）

**触发条件**: Ollama 不可用或 LLM 调用失败

使用字符串拼接模板生成中文解释，不依赖 LLM。关键模板片段：

**解释模板**:
```
【{league}】{home_name} vs {away_name}：模型预测{best_cn}概率最高({best_prob:.0%})。关键信号: {key_findings}; 最可能比分: {top_score}(概率{prob:.1%})。
```

**建议模板**:
```
【{decision_cn}】{best_cn} (置信度{confidence:.0%}，{risk_cn})
价值投注: {outcome_cn} (优势{edge:.1%})
```

**中文映射常量**:

| 键 | 映射 |
|----|------|
| OUTCOME_CN | home→主胜, draw→平局, away→客胜 |
| DECISION_CN | INVEST→建议投资, WATCH→建议观望, PASS→建议放弃 |
| RISK_CN | LOW→低风险, MEDIUM→中等风险, HIGH→高风险 |

---

## 工作流层（无 Prompt）

**源文件**: `agents/workflow.py`, `agents/nodes.py`

工作流层仅负责 Agent 调度与状态传递，**不包含任何 Prompt 逻辑**。工作流中的条件路由（`_route_after_commander`、`_should_call_math`、`_should_call_explainer`）基于 Commander 输出的 `experts_to_call` 列表决定执行路径，不涉及 LLM 调用。

### 最终回答格式化（`_commander_synthesize`）

工作流汇总节点将各专家结果格式化为最终用户可见的回答：

```
📊 预测结果: {best_cn}
   主胜: {home_prob:.1%} | 平局: {draw_prob:.1%} | 客胜: {away_prob:.1%}
   置信度: {confidence:.0%} | 决策: {decision}

💡 分析: {explanation}

📌 要点:
   1. {key_point_1}
   2. {key_point_2}
   3. {key_point_3}

🎯 建议: {recommendation}
```

---

## Prompt 最佳实践

### 变量占位符说明

本项目的 Prompt 使用 Python f-string 进行变量替换（`f"...{variable}..."`），而非 LangChain PromptTemplate 的 `{variable}` 语法。所有变量在 Prompt 构建时直接求值。

**常用占位符汇总**:

| 占位符 | 用途 | 出现位置 |
|--------|------|----------|
| `{user_input}` | 用户原始输入 | CommanderAgent 任务路由 |
| `{match_info}` | 比赛基本信息（主队/客队/联赛） | DataAgent、MathAgent、Explainer |
| `{feature_desc}` | 特征指标列表（含中文名和数值） | DataAgent、MathAgent |
| `{data_analysis}` | DataAgent 分析结果 JSON | Commander 汇总 |
| `{math_analysis}` | MathAgent 分析结果 JSON | Commander 汇总、Explainer |
| `{explanation}` | Explainer 解释结果 JSON | Commander 汇总 |
| `{prediction}` | 模型概率 JSON | MathAgent 风险评估 |
| `{odds}` | 赔率数据 JSON | MathAgent 风险评估 |

### JSON 输出约束

所有 LLM Prompt 均要求 **"只输出JSON"**，并在代码中通过 `_parse_json_response()` 统一解析。解析策略：

1. 先尝试直接 `json.loads()`
2. 失败则去除 markdown 代码块（```...```）后再尝试
3. 再失败则提取第一个 `{` 到最后一个 `}` 之间的子串再尝试
4. 全部失败则降级到规则方案

### 截断策略

为防止 Prompt 过长导致 LLM 性能下降或 token 超限：

| 数据 | 截断长度 | 处理方式 |
|------|----------|----------|
| Commander 汇总中的专家结果 | 500 字符 | `json.dumps(...)[:500]` |
| Explainer 中的数据分析/概率分析 | 400 字符 | `json.dumps(...)[:400]` |

### 截断风险评估

| Agent | 当前截断 | 风险等级 | 风险描述 | 建议 |
|-------|---------|---------|---------|------|
| Commander (汇总) | 500 字符 | 🔴 高 | 多专家结果合并后截断可能丢失关键决策信息 | 提升至 2000 字符 (Ollama 默认 128k 上下文完全够用) |
| Explainer (解释) | 400 字符 | 🟡 中 | 复杂比赛的多维度分析可能被截断导致解释不完整 | 提升至 800 字符 |

> **注意**: 截断是为了防止 Token 溢出，但当前 Ollama 模型默认上下文窗口为 128k tokens，远大于实际 Prompt 长度。**优先保证信息完整性**，仅当实测出现 Token 超限时再考虑截断。

### 降级策略说明

1. **LLM 调用失败** → 自动降级到规则/模板方案，不抛异常
2. **ModelBridge 加载失败** → **Fail-Fast**，抛出 `RuntimeError` 终止启动（不允许静默降级到规则计算）
3. **DomainKB 缺失** → **Fail-Fast**，抛出 `RuntimeError` 终止启动
4. **规则 Fallback 硬编码检测** → 若概率接近 40/28/32 模式，抛出 `RuntimeError` 终止

### 注意事项

- **不要修改 JSON 输出格式要求**：所有 Prompt 的 JSON 格式与代码中的 `_parse_json_response()` 解析逻辑强耦合，修改格式需同步更新解析代码
- **模型切换需验证**：不同 Ollama 模型对中文 Prompt 和 JSON 输出的遵循度不同，切换模型后需验证输出质量
- **截断可能丢失关键信息**：专家结果被截断到 400-500 字符，可能导致 Commander 汇总时信息不完整
- **规则降级概率不可靠**：Level 3 规则计算的基础值 H=0.40/D=0.28/A=0.32 是硬编码的，仅作最后保底，生产环境必须确保 ModelBridge 正常工作
- **Prompt 无版本控制**：当前 Prompt 内嵌在代码中，无法独立管理和版本化；未来可考虑抽取为独立的 PromptTemplate 文件
