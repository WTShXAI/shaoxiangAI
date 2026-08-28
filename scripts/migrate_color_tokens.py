"""哨响AI 前端 颜色 token 统一化迁移脚本。

替换范围:
1. 原始 hex 色值 → 设计 token (gradient/border/background)
2. Tailwind raw palette (emerald/blue/amber/red/purple) → 语义 token (field/frost/ember/danger/accent)

约束:
- 只在 className / class= 字符串中替换, 不动注释和字符串字面量
- 排除 tailwind.config.ts (它本来就是定义 token 的源头)
- 替换前先预览变更数, 让执行者确认
"""
import os
import re
import sys

ROOT = "D:/Architecture/frontend/src"
EXCLUDE_FILES = {"tailwind.config.ts"}  # tailwind.config 自己定义 token, 不能改

# ── 1. 原始 hex 替换 (无歧义) ──
HEX_REPLACEMENTS = [
    # 渐变 (高优先级, 先匹配长的)
    (r"from-\[#1d39c4\] to-\[#096dd9\]",  "from-frost-700 to-frost-500"),
    (r"from-\[#1890ff\] to-\[#096dd9\]",  "from-frost-500 to-frost-600"),
    (r"from-\[#5be85a\]/30 to-\[#1890ff\]/30", "from-field-300/30 to-frost-400/30"),
    # bare hex (在 from-/to- 渐变里)
    (r"\[#1d39c4\]", "frost-700"),
    (r"\[#096dd9\]", "frost-500"),
    (r"\[#1890ff\]", "frost-500"),
    (r"\[#5be85a\]", "field-300"),
    (r"\[#1a2e42\]", "surface-border"),
    (r"\[#ff4d4f\]", "danger-500"),
]

# ── 2. Tailwind raw palette → 语义 token ──
# 注意: Tailwind 的 bg-emerald-500/15 中 `/15` 是 alpha 修饰符
# 我们要把色名部分 (emerald-500) 改成 field-500, 保留 /15 等后缀
PALETTE_REPLACEMENTS = [
    # ── emerald-XXX → field-XXX (绿色: live/正盈/winning team) ──
    (r"\bemerald-50\b",  "field-50"),
    (r"\bemerald-100\b", "field-100"),
    (r"\bemerald-200\b", "field-200"),
    (r"\bemerald-300\b", "field-300"),
    (r"\bemerald-400\b", "field-400"),
    (r"\bemerald-500\b", "field-500"),
    (r"\bemerald-600\b", "field-600"),
    # ── blue-XXX → frost-XXX (蓝色: 已结束/info/中性) ──
    (r"\bblue-50\b",  "frost-50"),
    (r"\bblue-100\b", "frost-100"),
    (r"\bblue-200\b", "frost-200"),
    (r"\bblue-300\b", "frost-300"),
    (r"\bblue-400\b", "frost-400"),
    (r"\bblue-500\b", "frost-500"),
    (r"\bblue-600\b", "frost-600"),
    # ── amber-XXX → ember-XXX (琥珀: 警告/待处理/暂停/历史回放) ──
    (r"\bamber-50\b",  "ember-50"),
    (r"\bamber-100\b", "ember-100"),
    (r"\bamber-200\b", "ember-200"),
    (r"\bamber-300\b", "ember-300"),
    (r"\bamber-400\b", "ember-400"),
    (r"\bamber-500\b", "ember-500"),
    (r"\bamber-600\b", "ember-600"),
    # ── red-XXX → danger-XXX (红色: 失败/负盈/危险) ──
    (r"\bred-50\b",  "danger-50"),
    (r"\bred-100\b", "danger-100"),
    (r"\bred-200\b", "danger-200"),
    (r"\bred-300\b", "danger-300"),
    (r"\bred-400\b", "danger-400"),
    (r"\bred-500\b", "danger-500"),
    (r"\bred-600\b", "danger-600"),
    # ── rose-XXX → danger-XXX (rose 是 Tailwind 的偏粉红; 用于 fade 信号) ──
    (r"\brose-400\b", "danger-400"),
    (r"\brose-500\b", "danger-500"),
    # ── purple-XXX → frost-300 (紫色: 单场分析/历史回放 → 改成 frost 系冰蓝更统一) ──
    (r"\bpurple-300\b", "frost-300"),
    (r"\bpurple-400\b", "frost-400"),
    (r"\bpurple-500\b", "frost-500"),
]

ALL_REPLACEMENTS = HEX_REPLACEMENTS + PALETTE_REPLACEMENTS


def should_skip(path: str) -> bool:
    base = os.path.basename(path)
    if base in EXCLUDE_FILES:
        return True
    return False


def transform(content: str) -> tuple[str, dict]:
    """Apply replacements, return (new_content, change_counts_by_pattern)."""
    new = content
    counts = {}
    for pattern, repl in ALL_REPLACEMENTS:
        sub_count = len(re.findall(pattern, new))
        if sub_count:
            counts[f"{pattern} → {repl}"] = sub_count
        new = re.sub(pattern, repl, new)
    return new, counts


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="写入磁盘 (默认 dry-run 预览)")
    parser.add_argument("--path", default=ROOT, help="扫描根目录")
    args = parser.parse_args()
    scan_root = args.path

    targets = []
    for root, dirs, files in os.walk(scan_root):
        for f in files:
            if f.endswith((".tsx", ".ts", ".css")):
                p = os.path.join(root, f)
                if not should_skip(p):
                    targets.append(p)

    if not targets:
        print("no target files found")
        return

    # ── 第一遍: 仅预览 ──
    total_changes = 0
    for p in targets:
        with open(p, "r", encoding="utf-8") as fh:
            content = fh.read()
        new_content, counts = transform(content)
        if new_content != content:
            file_total = sum(counts.values())
            total_changes += file_total
            print(f"\n[UPDATE] {p}  ({file_total} 处)")
            for k, v in sorted(counts.items(), key=lambda x: -x[1])[:10]:
                print(f"    {v:3d}×  {k}")

    if total_changes == 0:
        print("\n没有匹配到任何需要替换的内容")
        return

    if not args.apply:
        print(f"\n[DRY-RUN] 预览完成: 共 {total_changes} 处替换待写入, 加 --apply 真正写入")
        return

    # ── 第二遍: 写入磁盘 ──
    print(f"\n{'='*60}")
    print(f"写入 {total_changes} 处替换…")
    print(f"{'='*60}")
    written = 0
    for p in targets:
        with open(p, "r", encoding="utf-8") as fh:
            content = fh.read()
        new_content, counts = transform(content)
        if new_content != content:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(new_content)
            written += 1
    print(f"\n✓ 完成 {total_changes} 处替换 ({written} 个文件)")


if __name__ == "__main__":
    main()