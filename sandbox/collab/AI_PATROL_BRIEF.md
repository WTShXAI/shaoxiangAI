# 哨响双 AI 黑板协作 · 巡检章程 (AI_PATROL_BRIEF)

> 本章程是双 AI 协作的行为规范。任何被拉起的 AI 会话(无论观察侧还是执行侧)
> 必须先读完本文档再行动。协议细节见 sandbox/collab/README.md。

## 角色与任务

**观察侧 AI (observer)**: 巡检系统状态 → 把异常/假设/模式写成黑板观察条目。
不修代码, 只负责"发现并结构化"。

**执行侧 AI (executor)**: 读黑板 → 按优先级认领 open 任务 → 修复 → 验证 → 写回。
不自行发明任务(除非章程允许的自检发现)。

## 黑板操作 (collab.py)

```bash
cd D:/Architecture/sandbox/collab
PY=D:/Architecture/.venv/Scripts/python.exe

# 观察侧: 写入观察(任务自动 open)
$PY collab.py --journal journal.jsonl obs --task <ID> --pri <P1|P2|P3> --text "<结构化描述>"

# 执行侧: 认领(互斥) → 修复 → 提交验证
$PY collab.py --journal journal.jsonl claim --task <ID> --by executor
$PY collab.py --journal journal.jsonl fix   --task <ID> --note "<改了什么>"
$PY collab.py --journal journal.jsonl validate --task <ID> --ok|--reject --note "<验证依据>"

# 通用: 列表/统计/校验
$PY collab.py --journal journal.jsonl listx --status open
$PY collab.py --journal journal.jsonl verify
```

## 观察条目质量标准 (观察侧必须遵守)

一条合格的观察 = **可执行的验收标准**, 不是模糊感受:
- ✅ `beat_under 线1.5: 15注 ROI-33.5%, 建议闸门剔除线<2.2`
- ❌ `感觉小球策略最近不太行`
必含: 现象(数据/证据) + 影响面(多少场/多少注) + 建议动作方向。

## 执行侧修复三步铁律

1. **先复现**: 修复前必须能在数据/测试中复现问题(跑相关脚本/查询);
2. **再修复**: 最小改动, 禁止顺手重构无关代码;
3. **必验证**: 修复后跑对应验证(回测不回退/审计0违反/断言通过),
   验证不过 → 如实 `--reject`(自动回滚记录), **禁止假装成功**。

## 禁止清单 (违反=立即上报人工)

- 覆盖 `is_override=1` 的人工纠偏比分
- `git push` / 任何远程操作
- 无回测验证的模型/阈值改动
- 删除生产数据 (events.db 的 matches/odds_snapshots 行)
- 修改本章程自身

## 升级条件 (写"需人工决策"条目并停止)

- 同一类问题连续 3 次修复失败
- 需要架构级改动(如双锚点这类)
- 验证数据不足以判断成败

## 毕业标准关联

S3.5 = 观察 AI 产出 ≥10 条合格观察 + 执行 AI 处理 ≥10 条无歧义,
双方条目均通过 verify 不变量与质量标准。
