# AGENTS.md

## 双 AI 协作黑板 (2026-09-13 生产接入, 必读)

哨响(观察 AI)与执行 AI 通过共享黑板协作: `data/collab_journal.jsonl`。
**任何会话开始时**: 若黑板有 open 条目 → 先读 `sandbox/collab/AI_PATROL_BRIEF.md`
章程 → 按优先级认领处理 → 写回结果。

```bash
PY=.venv/Scripts/python.exe
$PY sandbox/collab/collab.py --journal data/collab_journal.jsonl listx --status open   # 待办
$PY sandbox/collab/collab.py --journal data/collab_journal.jsonl claim --task <ID> --by executor_zcode
$PY sandbox/collab/collab.py --journal data/collab_journal.jsonl fix   --task <ID> --note "..."
$PY sandbox/collab/collab.py --journal data/collab_journal.jsonl validate --task <ID> --ok|--reject --note "..."
$PY sandbox/collab/collab.py --journal data/collab_journal.jsonl verify    # 不变量校验
```

铁律(详见章程): 先复现→最小修复→必验证(不过则如实 reject); 禁止覆盖
is_override / git push / 无回测的模型改动 / 删生产数据。
前端"协作面板"Tab 可视化全部条目; ⚡一键检测上报可将页面异常直写黑板。
