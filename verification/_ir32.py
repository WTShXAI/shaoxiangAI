"""IR-32 永久禁区守卫 (跨庄共识类字样零容忍).

用途: 报告/输出文本在落盘与打印前必过 assert_clean, 任何禁区词出现即抛错。
禁区词表字面量仅定义在 verification.constants.FORBIDDEN_TOKENS; 本模块只引用该列表名,
不含字面量 (test_ir32_guard 扫描时豁免 constants.py 与本文件)。
"""
from __future__ import annotations

from typing import List

from verification.constants import FORBIDDEN_TOKENS


class IR32Violation(RuntimeError):
    """IR-32 禁区词泄漏。"""


def find_violations(text: str) -> List[str]:
    """返回在 text 中命中的禁区词列表 (空列表 = 干净)。"""
    if not text:
        return []
    low = text.lower()
    return [tok for tok in FORBIDDEN_TOKENS if tok.lower() in low]


def assert_clean(text: str, context: str = "") -> None:
    """命中禁区词立即抛 IR32Violation; 干净则静默返回。"""
    hits = find_violations(text)
    if hits:
        where = f" @ {context}" if context else ""
        raise IR32Violation(f"IR-32 禁区词泄漏{where}: {hits}")


__all__ = ["IR32Violation", "find_violations", "assert_clean"]
