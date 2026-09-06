#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Thorough inspection of a .lnk to find any embedded target path."""
import os
import re

lnk = r"C:\Users\ShXAI\Desktop\世界杯.lnk"
with open(lnk, 'rb') as f:
    data = f.read()

print("FILE_SIZE=%d bytes" % len(data))
print("MAGIC=%r" % data[:4])
link_flags = int.from_bytes(data[0x14:0x18], 'little')
print("LINK_FLAGS=0x%08X" % link_flags)
print("  HasLinkTargetIDList = %s" % bool(link_flags & 0x01))
print("  HasLinkInfo         = %s" % bool(link_flags & 0x02))
print("  HasName             = %s" % bool(link_flags & 0x04))
print("  HasRelativePath     = %s" % bool(link_flags & 0x08))
print("  HasWorkingDir       = %s" % bool(link_flags & 0x10))
print("  HasArguments        = %s" % bool(link_flags & 0x20))
print("  HasIconLocation     = %s" % bool(link_flags & 0x40))

# --- ASCII runs ---
print("\n=== ASCII RUNS (len>=4) ===")
for m in re.finditer(rb'[\x20-\x7e]{4,}', data):
    run = m.group(0).decode('ascii')
    if any(c in run for c in ':\\/.' ) or 'Desktop' in run or 'Users' in run or 'ShXAI' in run:
        print("  %r" % run)

# --- UTF-16LE runs ---
print("\n=== UTF-16LE RUNS (len>=2 chars) ===")
for m in re.finditer(rb'(?:[\x20-\x7e\x80-\xff]\x00){2,}', data):
    raw = m.group(0)
    s = raw.decode('utf-16-le', 'ignore')
    if any(ch in s for ch in ':\\/.') or '世界杯' in s or '截图' in s or 'Desktop' in s or 'Users' in s:
        print("  %r" % s)

# --- search for drive letters anywhere ---
print("\n=== DRIVE-LIKE (X:\\) ASCII & UTF16 ===")
for m in re.finditer(rb'[A-Za-z]:\\', data):
    start = m.start()
    # grab forward
    end = data.find(b'\x00', start)
    if end == -1 or end - start > 260:
        end = start + 200
    print("  ASCII @%d: %r" % (start, data[start:end].decode('ascii','ignore')))
for m in re.finditer(rb'[A-Za-z]:\\\x00', data):
    start = m.start()
    end = start
    while end + 1 < len(data) and not (data[end] == 0 and data[end+1] == 0):
        end += 2
    print("  UTF16 @%d: %r" % (start, data[start:end].decode('utf-16-le','ignore')))

# --- search for UNC ---
print("\n=== UNC-LIKE (\\\\) ===")
for m in re.finditer(rb'\\\\', data):
    start = m.start()
    end = data.find(b'\x00', start)
    if end == -1 or end - start > 260:
        end = start + 200
    s = data[start:end].decode('ascii','ignore')
    if s.count('\\') >= 2:
        print("  @%d: %r" % (start, s))
