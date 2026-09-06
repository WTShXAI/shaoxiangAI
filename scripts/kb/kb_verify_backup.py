# -*- coding: utf-8 -*-
"""
校验知识库完整性 + 备份待删文件 (不做删除)
=========================================
1) 校验: 知识库.md 含每个待合并文件的 `## <rel>` 章节; 铁律文件不在清单/不在 KB。
2) 备份: 将 166 个待删文件打包到 _kb_backup_20260828.zip (安全网, 删除后可手动清)。
3) 不执行删除。删除由独立步骤在确认校验通过后进行。
"""
import os, json, zipfile, datetime

ROOT = r"D:\Architecture"
KB = os.path.join(ROOT, "知识库.md")
LIST = os.path.join(ROOT, "deliverables", "_kb_filelist.json")
BACKUP = os.path.join(ROOT, "_kb_backup_" + datetime.date.today().isoformat().replace("-", "") + ".zip")

PROTECTED = {"docs/IRON_RULES.md", "docs/IRON_LAWS.md", "ARCHITECTURE.md", "DESIGN_TOKENS.md"}

def main():
    files = json.load(open(LIST, encoding="utf-8"))
    kb = open(KB, encoding="utf-8").read()

    # 1) 完整性
    missing = [f for f in files if f"\n## {f}\n" not in kb]
    # 允许文件内已以 # 开头导致前面无空行的情况, 宽松再查一次
    missing = [f for f in missing if f"## {f}" not in kb]
    assert not missing, f"知识库缺失章节: {missing[:10]}"

    # 2) 铁律保护
    leak = [p for p in PROTECTED if p in files]
    assert not leak, f"铁律文件误入删除清单: {leak}"
    for p in PROTECTED:
        assert f"## {p}" not in kb, f"铁律内容泄漏进 KB: {p}"

    print(f"[verify] 合并文件数: {len(files)}  全部章节存在: OK")
    print(f"[verify] 铁律/架构保护: OK (未并入KB, 未入删除清单)")

    # 3) 备份
    n = 0
    with zipfile.ZipFile(BACKUP, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            full = os.path.join(ROOT, rel)
            if os.path.isfile(full):
                z.write(full, rel)
                n += 1
    print(f"[backup] 已打包 {n} 个文件 -> {BACKUP}  ({os.path.getsize(BACKUP)/1024:.1f} KB)")
    print("[done] 校验+备份完成, 未删除任何文件。下一步: 运行 kb_delete.py 物理删除原文件。")

if __name__ == "__main__":
    main()
