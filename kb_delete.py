# -*- coding: utf-8 -*-
"""
物理删除已合并的原 .md 文件 (仅在 kb_verify_backup 校验通过后运行)
=================================================================
- 仅删除 _kb_filelist.json 中的文件 (166 个知识/研究报告类)
- 铁律/架构/专家/venv/其他工具 memory 绝不在清单内, 受多重保护
- 删除前再次防御性检查 PROTECTED 集合
"""
import os, json

ROOT = r"D:\Architecture"
LIST = os.path.join(ROOT, "deliverables", "_kb_filelist.json")
PROTECTED = {"docs/IRON_RULES.md", "docs/IRON_LAWS.md", "ARCHITECTURE.md", "DESIGN_TOKENS.md"}

def main():
    files = json.load(open(LIST, encoding="utf-8"))
    # 防御: 若清单含受保护文件, 中止
    bad = [f for f in files if f in PROTECTED]
    assert not bad, f"安全中止: 清单含受保护文件 {bad}"

    deleted = 0
    skipped = 0
    for rel in files:
        full = os.path.join(ROOT, rel)
        if os.path.isfile(full):
            os.remove(full)
            deleted += 1
        else:
            skipped += 1
    print(f"[delete] 已删除 {deleted} 个文件, 跳过(不存在) {skipped} 个")

    # 校验受保护文件仍在
    for p in PROTECTED:
        assert os.path.isfile(os.path.join(ROOT, p)), f"灾难: 受保护文件被删 {p}"
    print(f"[verify] 受保护文件全部存活: OK")

    # 报告合并集内残留 .md (理论上应为 0, 除受保护)
    leftover = []
    for r in ["analysis", "data", "deliverables", "reports", "odds_db", "分析报告", "docs"]:
        base = os.path.join(ROOT, r)
        if not os.path.isdir(base):
            continue
        for dp, _, fns in os.walk(base):
            for fn in fns:
                if fn.lower().endswith(".md"):
                    rel = os.path.relpath(os.path.join(dp, fn), ROOT).replace("\\", "/")
                    if rel not in PROTECTED:
                        leftover.append(rel)
    print(f"[verify] 合并集内残留 .md (应仅受保护): {len(leftover)}")
    for x in leftover:
        print("  残留:", x)

if __name__ == "__main__":
    main()
