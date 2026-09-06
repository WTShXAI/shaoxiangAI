#!/usr/bin/env python3
"""
哨响AI 全员注册脚本 — 35人从 roster 到 WorkBuddy 专家系统。
Usage: python register_all_experts.py --session-id <id>
"""
import sys, os, subprocess, json
from pathlib import Path

# ── Config ──
BUILTIN_DIR = Path(r"C:\Users\ShXAI\AppData\Local\Programs\WorkBuddy\resources\app.asar.unpacked\resources\builtin-skills\expert-manager\scripts")
EXPERT_PLUGINS = Path.home() / ".workbuddy" / "plugins" / "marketplaces" / "my-experts" / "plugins"
ROSTER_FILE = Path(r"D:\Architecture\.workbuddy\backup\expert_roster_2026-07-01.md")

# ── Expert definitions from roster ──
EXPERTS = [
    # 智囊团 (6)
    {"name": "ai-intel-officer",    "displayName": "魏监听",     "profession": "赛前情报官",   "categoryId": "08-FinanceInvestment", "tags": ["足球情报","赛前分析","赔率监控"], "dept": "智囊团"},
    {"name": "ai-strategist",       "displayName": "何执策",     "profession": "首席策略官",   "categoryId": "08-FinanceInvestment", "tags": ["策略决策","博弈分析","战术推演"], "dept": "智囊团"},
    {"name": "ai-mathematician",    "displayName": "毕正验",     "profession": "数学建模师",   "categoryId": "04-DataAI",            "tags": ["概率统计","模型验证","假设检验"], "dept": "智囊团"},
    {"name": "ai-game-theorist",    "displayName": "渡庄生",     "profession": "博弈分析师",   "categoryId": "08-FinanceInvestment", "tags": ["博弈论","庄家行为","市场微观"], "dept": "智囊团"},
    {"name": "ai-architect",        "displayName": "费深谋",     "profession": "系统架构师",   "categoryId": "02-Engineering",       "tags": ["架构设计","系统优化","技术选型"], "dept": "智囊团"},
    {"name": "ai-data-scientist",   "displayName": "贾证数",     "profession": "数据科学家",   "categoryId": "04-DataAI",            "tags": ["特征工程","因果推断","实验设计"], "dept": "智囊团"},

    # 算法部 (6)
    {"name": "ai-algo-poisson",     "displayName": "季泊松",     "profession": "JEPA建模师",   "categoryId": "04-DataAI",            "tags": ["泊松模型","比分预测","进球率"],  "dept": "算法部"},
    {"name": "ai-algo-game",        "displayName": "杜博弈",     "profession": "赔率逆向专家",  "categoryId": "08-FinanceInvestment", "tags": ["赔率解码","逆向工程","市场分析"], "dept": "算法部"},
    {"name": "ai-algo-ensemble",    "displayName": "荣合众",     "profession": "集成专家",     "categoryId": "04-DataAI",            "tags": ["集成学习","模型融合","Stacking"], "dept": "算法部"},
    {"name": "ai-algo-temporal",    "displayName": "施时序",     "profession": "时序专家",     "categoryId": "04-DataAI",            "tags": ["时间序列","LSTM","趋势预测"],    "dept": "算法部"},
    {"name": "ai-algo-math",        "displayName": "毕建模",     "profession": "数学专家",     "categoryId": "04-DataAI",            "tags": ["数学优化","数值计算","ML基础"],   "dept": "算法部"},
    {"name": "ai-algo-draw",        "displayName": "曾均衡",     "profession": "平局专家",     "categoryId": "08-FinanceInvestment", "tags": ["平局建模","Focal Loss","类别不平衡"], "dept": "算法部"},

    # 数据部 (4)
    {"name": "ai-data-lead",        "displayName": "数定规",     "profession": "数据架构师",   "categoryId": "04-DataAI",            "tags": ["数据治理","ETL管道","数据质量"],   "dept": "数据部"},
    {"name": "ai-data-collector",   "displayName": "采集员",     "profession": "数据采集员",   "categoryId": "04-DataAI",            "tags": ["API采集","爬虫","数据源管理"],    "dept": "数据部"},
    {"name": "ai-data-cleaner",     "displayName": "清洗员",     "profession": "数据清洗员",   "categoryId": "04-DataAI",            "tags": ["数据清洗","异常检测","格式统一"],  "dept": "数据部"},
    {"name": "ai-data-pipeline",    "displayName": "管道员",     "profession": "管道工程师",   "categoryId": "04-DataAI",            "tags": ["数据管道","自动化","调度"],       "dept": "数据部"},

    # 训练部 (2)
    {"name": "ai-train-trainer",    "displayName": "训练师",     "profession": "模型训练师",   "categoryId": "04-DataAI",            "tags": ["LGB/XGB训练","超参调优","GPU加速"], "dept": "训练部"},
    {"name": "ai-train-validator",  "displayName": "验证师",     "profession": "验证师",       "categoryId": "04-DataAI",            "tags": ["OOS验证","交叉验证","AB测试"],    "dept": "训练部"},

    # 质检部 (3)
    {"name": "ai-qa-reviewer",      "displayName": "严审明",     "profession": "代码审查官",    "categoryId": "10-ProjectQuality",    "tags": ["代码审查","规范检查","最佳实践"],  "dept": "质检部"},
    {"name": "ai-qa-security",      "displayName": "固安生",     "profession": "安全卫士",     "categoryId": "11-SecurityCompliance", "tags": ["安全审计","漏洞检测","数据安全"],  "dept": "质检部"},
    {"name": "ai-qa-validator",     "displayName": "测必过",     "profession": "质量门神",     "categoryId": "10-ProjectQuality",    "tags": ["自动测试","回归测试","质量门禁"],  "dept": "质检部"},

    # DevOps部 (3)
    {"name": "ai-devops-lead",      "displayName": "稳如山",     "profession": "SRE运维官",    "categoryId": "02-Engineering",       "tags": ["运维稳定性","监控告警","灾难恢复"], "dept": "DevOps部"},
    {"name": "ai-train-ops",        "displayName": "运维师",     "profession": "运维工程师",   "categoryId": "02-Engineering",       "tags": ["Docker部署","服务管理","CI/CD"],   "dept": "DevOps部"},
    {"name": "ai-qa-investigator",  "displayName": "究根源",     "profession": "故障排障手",   "categoryId": "02-Engineering",       "tags": ["故障排查","根因分析","日志分析"],   "dept": "DevOps部"},

    # 设计部 (6)
    {"name": "ai-design-lead",      "displayName": "画统筹",     "profession": "设计主理人",   "categoryId": "01-ProductDesign",     "tags": ["UI/UX设计","设计系统","产品体验"], "dept": "设计部"},
    {"name": "ai-design-discovery", "displayName": "许明需",     "profession": "需求分析师",   "categoryId": "01-ProductDesign",     "tags": ["需求分析","用户研究","竞品分析"],   "dept": "设计部"},
    {"name": "ai-design-system",    "displayName": "彩格调",     "profession": "设计系统专家",  "categoryId": "01-ProductDesign",     "tags": ["Design Tokens","组件库","视觉规范"], "dept": "设计部"},
    {"name": "ai-design-prototype", "displayName": "筑原型",     "profession": "原型构建师",   "categoryId": "01-ProductDesign",     "tags": ["原型设计","交互DEMO","快速迭代"],  "dept": "设计部"},
    {"name": "ai-design-critique",  "displayName": "严过审",     "profession": "质量审查官",   "categoryId": "01-ProductDesign",     "tags": ["设计评审","规范检查","一致性"],    "dept": "设计部"},
    {"name": "ai-design-export",    "displayName": "交付达",     "profession": "导出交付师",   "categoryId": "01-ProductDesign",     "tags": ["设计交付","标注规范","资源导出"],   "dept": "设计部"},

    # 指挥部 (2)
    {"name": "ai-code-driver",      "displayName": "钱代驾",     "profession": "代码代写/提示词","categoryId": "02-Engineering",      "tags": ["代码生成","Agent开发","提示词工程"], "dept": "指挥部"},
    {"name": "ai-pm",               "displayName": "孙策",       "profession": "产品经理",     "categoryId": "10-ProjectQuality",    "tags": ["产品规划","需求管理","里程碑推进"], "dept": "指挥部"},

    # 资产+风控 (2)
    {"name": "ai-recorder",         "displayName": "史为鉴",     "profession": "知识图谱官",   "categoryId": "04-DataAI",            "tags": ["知识管理","记忆归档","经验沉淀"],  "dept": "资产+风控"},
    {"name": "ai-compliance",       "displayName": "法如山",     "profession": "风控合规官",   "categoryId": "11-SecurityCompliance", "tags": ["合规审查","风险控制","铁律执行"],  "dept": "资产+风控"},

    # 独立补充 (5)
    {"name": "ai-tech-writer",      "displayName": "文载道",     "profession": "工程保障部文档", "categoryId": "06-ContentCreative",  "tags": ["技术文档","说明书","交付归档"],    "dept": "独立"},
    {"name": "ai-football-analyst", "displayName": "贾战术",     "profession": "足球战术分析师", "categoryId": "12-IndustryConsultant","tags": ["战术分析","阵型研究","教练双路径"],  "dept": "独立"},
    {"name": "ai-odds-decoder",     "displayName": "赔率解码",   "profession": "赔率解码专家",  "categoryId": "08-FinanceInvestment","tags": ["赔率结构","庄家分析","操盘解码"],  "dept": "独立"},
    {"name": "ai-points-path",      "displayName": "积分路径",   "profession": "积分路径分析师","categoryId": "12-IndustryConsultant","tags": ["积分预测","出线形势","赛程影响"],   "dept": "独立"},
    {"name": "ai-tournament-architect","displayName": "赛制架构","profession": "赛制架构分析师","categoryId": "12-IndustryConsultant","tags": ["赛制研究","规则解码","世界杯"],    "dept": "独立"},
]

def write_agent_md(expert_dir, name, display_name, profession, dept):
    """Write the agent MD file with correct frontmatter."""
    agents_dir = expert_dir / "agents"
    agents_dir.mkdir(exist_ok=True)
    md_path = agents_dir / f"{name}.md"
    # Find any existing MD generated by init
    for f in agents_dir.glob("*.md"):
        if f.stem != name:
            f.unlink()  # Remove init template
    content = f"""---
name: {name}
description: "哨响AI团队成员 — {display_name}，{profession}。{dept}部门。"
displayName:
  en: "{display_name}"
  zh: "{display_name}"
profession:
  en: "{profession}"
  zh: "{profession}"
maxTurns: 50
---

# {display_name} — {profession}

哨响AI团队 {dept} 成员。

## 核心能力
- {profession}：负责足球赔率预测系统中与{profession}相关的工作
- 协同赵统筹（总工）完成团队交付的任务

## 工作原则
- 数据驱动，结构输出
- 与团队同步，信息不孤岛
"""
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)
    return md_path

def write_plugin_json(expert_dir, name, display_name, profession, category_id, tags):
    """Write plugin.json matching WorkBuddy spec."""
    pj_path = expert_dir / ".codebuddy-plugin" / "plugin.json"
    pj_path.parent.mkdir(exist_ok=True)
    config = {
        "name": name,
        "version": "1.0.0",
        "description": f"哨响AI {profession} — {display_name}",
        "author": {"name": "ShXAI", "email": "shxai@footballai.dev"},
        "agents": [f"./agents/{name}.md"],
        "expertType": "agent",
        "agentName": name,
        "displayName": {"en": display_name, "zh": display_name},
        "profession": {"en": profession, "zh": profession},
        "displayDescription": {
            "en": f"哨响AI team {profession}, expert in football odds prediction.",
            "zh": f"哨响AI团队{profession}，专注足球赔率预测系统。"
        },
        "avatar": "avatars/expert.png",
        "categoryId": category_id,
        "defaultInitPrompt": {
            "zh": f"哨响团队{display_name}就位。请下达任务。",
            "en": f"哨响 team {display_name} ready. Assign task."
        },
        "plugin": name,
        "tags": [
            {"en": tag, "zh": tag} for tag in tags
        ],
        "quickPrompts": [
            {"en": f"As {display_name}, start analysis", "zh": f"作为{display_name}，开始分析"},
            {"en": "Evaluate and give recommendations", "zh": "评估并给出优化建议"},
            {"en": "Report status and next steps", "zh": "汇报当前状态和下一步计划"}
        ]
    }
    with open(pj_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return pj_path

def run_script(script_name, args):
    """Run a builtin script and return success."""
    cmd = [sys.executable, str(BUILTIN_DIR / script_name)] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.stdout.strip():
        print(f"   {result.stdout.strip()[:200]}")
    if result.returncode != 0 and result.stderr.strip():
        print(f"   ⚠ {result.stderr.strip()[:200]}")
    return result.returncode == 0

def main():
    session_id = None
    for i, a in enumerate(sys.argv):
        if a == '--session-id' and i + 1 < len(sys.argv):
            session_id = sys.argv[i + 1]

    if not session_id:
        session_id = os.environ.get("WORKBUDDY_SESSION_ID", os.environ.get("SESSION_ID", ""))
    
    if not session_id:
        # Dummy session for development
        print("⚠ No --session-id provided, using dev session")
        session_id = "dev-session"

    total = len(EXPERTS)
    passed = []
    failed = []
    skipped = []

    for idx, expert in enumerate(EXPERTS):
        name = expert["name"]
        print(f"\n[{idx+1}/{total}] {expert['displayName']} ({name}) — {expert['profession']} [{expert['dept']}]")
        
        # Init
        expert_path = str(EXPERT_PLUGINS / name)
        if (EXPERT_PLUGINS / name / ".workbuddy-plugin").exists():
            print(f"   ⏭ 已存在, 跳过init")
        else:
            if not run_script("init_expert.py", [name, "--type", "agent", "--path", str(EXPERT_PLUGINS)]):
                failed.append(name)
                continue
        
        # Fill content
        expert_dir = EXPERT_PLUGINS / name
        write_agent_md(expert_dir, name, expert["displayName"], expert["profession"], expert["dept"])
        write_plugin_json(expert_dir, name, expert["displayName"], expert["profession"],
                         expert["categoryId"], expert["tags"])
        print(f"   ✅ 内容填充完成")
        
        # Validate
        if not run_script("validate_expert.py", [expert_path]):
            failed.append(name)
            continue
        
        # Register
        reg_args = [expert_path]
        if session_id:
            reg_args.extend(["--session-id", session_id])
        if run_script("register_expert.py", reg_args):
            passed.append(name)
            print(f"   ✅ 注册成功")
        else:
            failed.append(name)

    print(f"\n{'═'*40}")
    print(f"📊 结果: {len(passed)}/{total} 成功 | {len(failed)} 失败 | {len(skipped)} 跳过")
    if failed:
        print(f"   失败: {', '.join(failed)}")
    return 0 if not failed else 1

if __name__ == "__main__":
    sys.exit(main())
