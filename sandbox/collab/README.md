# 双 AI 黑板协作 · 沙箱

哨响(分析 AI) 与 执行 AI 通过**共享协作日志**(黑板)沟通协作——事件驱动,
非定时驱动。本沙箱用真实历史异常回放验证协作协议, 生产零接触。

## 文件
| 文件 | 角色 |
|---|---|
| collab.py | 黑板 CLI(状态机+不变量校验) —— 唯一将来接入生产的组件 |
| journal.jsonl | 协作黑板(追加式事件日志) |
| mock_observer.py | 模拟哨响分析 AI: 回放真实历史异常(22 条实测案例) |
| mock_executor.py | 模拟执行 AI: 认领→修复→验证 全自动 |
| run_sandbox_test.py | 端到端场景运行器(S1 机制/S2 回放/S3 失败路径) |

## 协作协议
事件: {eid, ts, act, task, author, pri/text/note/ok}
状态机: observation → **open** → claim → **claimed** → fix → **fixed**
        → validate(ok→**done** / reject→**rejected**+自动回滚记录)
        → observer reopen → open (复核重开闭环)

不变量 (verify 强制):
- I1 无重复认领  I2 未认领不可修复  I3 done 必有修复+验证前置
- I4 rejected 必有回滚记录  I5 全事件带 author 身份

## 毕业标准 (全部满足才进入生产接入阶段)
- S2 端到端 3 轮 × 50+ 条目零人工干预、零协议违规
- S3 失败/回滚路径全部正确流转
- S3.5 真实执行 AI 按章程处理沙箱任务 ≥10 条无歧义
- 协议 schema 经历至少一次真实迭代仍向后兼容

## 生产接入(毕业后, 摘自已批准方案)
collab.py 日志路径指向生产 + agent_cruise.py 观察写入 + AGENTS.md 章程
+ auto_train_pipeline 冠军/挑战者闸门 + auto_collector --maintenance-only
