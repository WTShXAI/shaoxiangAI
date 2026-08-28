# -*- coding: utf-8 -*-
"""
构建单一知识库 知识库.md (合并项目知识/研究报告类 .md)
=======================================================
合并范围(递归, 全 .md): analysis, data, deliverables, reports, odds_db, 分析报告, docs
  + 根目录零散: 足球实时预测页面诊断报告.md
排除(保留, 不合并不删除):
  - docs/IRON_RULES.md, docs/IRON_LAWS.md  (铁律 SSoT 权威源)
  - ARCHITECTURE.md, DESIGN_TOKENS.md       (架构/设计 token)
  - .experts/.venv/.codebuddy/.zcode/.github/.edge_agent_profile/.workbuddy (非知识报告)
  - config/README.md, deploy/README.md, gq/README.md (运维 README, 不在合并集)
输出: D:/Architecture/知识库.md  +  deliverables/_kb_filelist.json (扫描清单)

【知识库规则 IR-31 · 2026-08-28 用户制定】
  以后生成的 .md 文件(报告/诊断/分析/复盘/交付物)一律进知识库:
  1) 落盘位置须在合并集目录内: analysis|data|deliverables|reports|odds_db|分析报告|docs
     (禁止散落根目录; 根目录散文件只有 ROOT_STRAY 白名单才合并);
  2) 生成/修改 .md 后运行 `python build_kb.py` 重建 知识库.md (全量扫描, 自动纳入新文件);
  3) 受保护文件(IRON_RULES/IRON_LAWS/ARCHITECTURE/DESIGN_TOKENS/运维README) 不入库;
  4) 模式=【保留原文件 + 聚合视图】: 原 .md 保留在源目录(勿删), 知识库.md 是聚合产物可反复重建;
     (2026-08-28 已从 _kb_backup_20260828.zip 恢复 166 篇原文件, 否则全量扫描=0 会把知识库清空)
  5) 该脚本为全量重建式, 幂等, 可直接重复运行。
"""
import os, json, glob

ROOT = r"D:\Architecture"
KB_OUT = os.path.join(ROOT, "知识库.md")
LIST_OUT = os.path.join(ROOT, "deliverables", "_kb_filelist.json")

MERGE_ROOTS = ["analysis", "data", "deliverables", "reports", "odds_db", "分析报告", "docs"]
ROOT_STRAY = ["足球实时预测页面诊断报告.md"]

# 受保护(绝不碰)的精确相对路径
PROTECTED_EXACT = {
    "docs/IRON_RULES.md", "docs/IRON_LAWS.md",
    "ARCHITECTURE.md", "DESIGN_TOKENS.md",
    "config/README.md", "deploy/README.md", "gq/README.md",
}

def collect():
    files = []
    for r in MERGE_ROOTS:
        base = os.path.join(ROOT, r)
        if not os.path.isdir(base):
            continue
        for dp, _, fns in os.walk(base):
            for fn in fns:
                if fn.lower().endswith(".md"):
                    full = os.path.join(dp, fn)
                    rel = os.path.relpath(full, ROOT).replace("\\", "/")
                    if rel in PROTECTED_EXACT:
                        continue
                    files.append(rel)
    for fn in ROOT_STRAY:
        full = os.path.join(ROOT, fn)
        if os.path.isfile(full):
            files.append(fn)
    files.sort()
    return files

def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        try:
            with open(path, encoding="utf-8-sig") as f:
                return f.read()
        except Exception:
            with open(path, encoding="gbk", errors="replace") as f:
                return f.read()

def main():
    files = collect()
    print(f"[scan] 待合并文件数: {len(files)}")

    # 分类 TOC
    cats = {}
    for rel in files:
        top = rel.split("/")[0]
        cats.setdefault(top, []).append(rel)

    toc = ["# 哨响AI 项目知识库", "",
           f"> 自动整合自项目知识/研究报告类 .md（共 {len(files)} 篇）。",
           "> 保留核心 SSoT（IRON_RULES / IRON_LAWS / 架构 / 专家定义 / venv）未并入。",
           "", "## 目录", ""]
    for cat in sorted(cats):
        toc.append(f"- **{cat}** ({len(cats[cat])} 篇)")
    toc.append("")

    body = []
    for cat in sorted(cats):
        body.append(f"# 分类：{cat}")
        body.append("")
        for rel in cats[cat]:
            body.append(f"## {rel}")
            body.append("")
            txt = read_text(os.path.join(ROOT, rel))
            txt = txt.strip("\n")
            # 若原文件已有 H1, 避免与章节标题冲突: 简单保留
            body.append(txt)
            body.append("")
            body.append("---")
            body.append("")

    full = "\n".join(toc + body)
    with open(KB_OUT, "w", encoding="utf-8") as f:
        f.write(full)

    with open(LIST_OUT, "w", encoding="utf-8") as f:
        json.dump(files, f, ensure_ascii=False, indent=2)

    size = os.path.getsize(KB_OUT)
    print(f"[ok] 知识库 -> {KB_OUT}")
    print(f"     章节数(##): {full.count('## ')}  大小: {size/1024:.1f} KB")
    print(f"[ok] 文件清单 -> {LIST_OUT}")

if __name__ == "__main__":
    main()
