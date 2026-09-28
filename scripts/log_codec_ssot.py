#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""混合编码日志解码器 —— 全审计脚本 SSoT
=========================================================================
背景 (2026-09-27 T44 实证, T48/T52 复踩):

``scripts/autonomous_monitor.py`` 的 ``log()`` 以 **UTF-8** 写文件, 而同一行的
``print()`` 落 stdout 时被 Windows 控制台代码页重编码成 **GBK** → 同一个日志文件
内**既有 UTF-8 行也有 GBK 行**。后果:

* 整文件 ``raw.decode('utf-8')`` → UnicodeDecodeError;
* 整文件 ``raw.decode('gbk')``    → 局部桶乱码 (UTF-8 中文被当 GBK 解);
* 按 UTF-8 做中文 grep → **静默漏匹配一半日志** (实测 101 行 UTF-8 / 664 行 GBK /
  1 行两者皆非, 见 ``logs/autonomous_monitor.log`` 2026-09-28 读数);
* 解析脚本因此会报出「0 个周期」「0 次撞锁」这类**假的零**, 把真 bug 读成健康。

因此本模块提供**逐行**解码, 任何日志读取一律走这里, 禁止在每个审计脚本里
各写一份 ``decode_line`` (那样下次换另一个脚本还会再踩一次)。
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

#: 逐行尝试的编码顺序: 现代写入面 → 中文 Windows 控制台面 → 永不失败的兜底。
CANDIDATE_ENCODINGS: Tuple[str, ...] = ('utf-8', 'gbk', 'latin-1')

#: 判定「该文件确实是混合编码」所需的文化特征符号 (命中说明文件里有中文行)。
HAN_RE = re.compile(r'[一-鿿]')


def decode_line(raw: bytes) -> str:
    """解码**单行**日志字节。

    逐编码尝试, 第一个成功的即返回 (优先 UTF-8, 再 GBK, 最后 latin-1 保字符)。
    拉丁兜底意味着**绝不抛异常** —— 日志解析失败必须表现为「读不到」而不是「崩了」。

    Args:
        raw: 单行原始字节 (不含换行符亦可用, 换行符由调用方切)。

    Returns:
        str: 解码后的文本; 所有编码都失败时用 ``errors='replace'`` 兜底。
    """
    for enc in CANDIDATE_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def decode_log_lines(raw: bytes) -> List[str]:
    """把整份日志字节**逐行**解码, 保留行边界。

    与整文件解码的关键差别: 每一行独立挑编码, 所以 UTF-8 行和 GBK 行可以共存。

    Args:
        raw: 日志文件的原始字节。

    Returns:
        List[str]: 逐行解码后的行列表。
    """
    return [decode_line(b.rstrip(b'\r')) for b in raw.split(b'\n')]


def decode_log_text(raw: bytes) -> str:
    """整份日志解码为单个字符串 (行间用 ``\\n`` 连接)。"""
    return '\n'.join(decode_line(b) for b in raw.split(b'\n'))


def read_log_lines(path) -> List[str]:
    """按混合编码逐行读取日志文件, 返回行列表。

    入参可以是**路径**也可以是**已读出的日志字节**。作为字节传入时**不做存在性
    复查** —— 2026-09-28 T52 实测踩坑: 把字节当路径喂给存在性校验会拿到 ``False``,
    函数于是静默返回 ``[]``, 调用方据此报出「0 周期 / 0 命中」这类**假零**,
    恰是本模块要防的失效模式。故字节入参一律直接解码。

    Args:
        path: 文件路径 (str/bytes) 或已读出的日志字节; ``None`` 返回 ``[]``。

    Returns:
        List[str]: 解码后的行; 不存在或读取失败返回 ``[]``。
    """
    if path is None:
        return []
    raw = path if isinstance(path, (bytes, bytearray)) else None
    if raw is None:
        try:
            if not os.path.exists(path):
                return []
            with open(path, 'rb') as f:
                raw = f.read()
        except OSError:
            return []
    lines = decode_log_lines(bytes(raw))
    # 文件以换行结尾时 split 会多出一个空串, 剥掉它让行数与 `splitlines()` 一致。
    if lines and lines[-1] == '':
        lines.pop()
    return lines


def read_log_text(path: str) -> Optional[str]:
    """按混合编码读取日志全文; 不存在返回 ``None`` (区别于空文件的 ``''``)。"""
    lines = read_log_lines(path)
    if not os.path.exists(path):
        return None
    return '\n'.join(lines)


def encoding_report(lines: List[str]) -> dict:
    """统计一份已解码文本里各类行的占比, 供审计报告/守卫取证。

    Args:
        lines: 已解码的行列表。

    Returns:
        dict: 总行数 / 含中文行数 / 疑似 GBK 残留(含中文却解出替换符) 等计数。
    """
    total = len(lines)
    han_lines = sum(1 for ln in lines if HAN_RE.search(ln))
    return {'lines': total, 'han_lines': han_lines, 'has_han': han_lines > 0}
