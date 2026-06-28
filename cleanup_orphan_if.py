"""清理孤立的 `if ... not in sys.path:` 语句 — sys.path.insert 已被删除后留下的空if块"""
import os, re

ROOT = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_DIRS = {'__pycache__', '.git', 'node_modules', 'deliverables', 'docs',
                'footballai-core', 'archive_backup', '.venv', '.venv_314_backup'}
EXCLUDE_FILES = {os.path.abspath(__file__)}

# 匹配 if ... not in sys.path: 后面紧跟着空行
# 或者是复合语句 if ... not in sys.path: sys.path.insert(...) — 这个已经被清理
# 所以剩下的是孤立的 if ... not in sys.path: (后面空行或什么都没有)
PAT_ORPHAN = re.compile(
    r'^(\s*)if\s+.*?\s+not\s+in\s+sys\.path\s*:\s*\n(\s*\n)*',
    re.MULTILINE,
)

# 额外匹配 if ... not in sys.path: 且后面跟着 import 或 from (孤立if块)
PAT_ORPHAN2 = re.compile(
    r'^(\s*)if\s+.*?\s+not\s+in\s+sys\.path\s*:\s*$',
    re.MULTILINE,
)

stats = {'checked': 0, 'modified': 0, 'removed': 0}

for dirpath, dirnames, fnames in os.walk(ROOT):
    rel = os.path.relpath(dirpath, ROOT)
    parts = rel.replace('\\', '/').split('/')
    if any(e in parts for e in EXCLUDE_DIRS):
        continue
    for fn in fnames:
        if not fn.endswith('.py'):
            continue
        fp = os.path.join(dirpath, fn)
        ap = os.path.abspath(fp)
        if ap in EXCLUDE_FILES:
            continue
        stats['checked'] += 1

        with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        original = content

        content, n2 = PAT_ORPHAN2.subn('', content)
        content, n1 = PAT_ORPHAN.subn('', content)

        n = n1 + n2
        if n > 0:
            # 清除连续空行
            content = re.sub(r'\n{3,}', '\n\n', content)
            content = content.rstrip() + '\n'
            with open(fp, 'w', encoding='utf-8') as f:
                f.write(content)
            stats['modified'] += 1
            stats['removed'] += n
            print(f"  [{n}行] {os.path.relpath(fp, ROOT)}")

print(f"\n检查 {stats['checked']} 个文件")
print(f"修改 {stats['modified']} 个文件")
print(f"删除 {stats['removed']} 个孤立if块")
print("完成！")
