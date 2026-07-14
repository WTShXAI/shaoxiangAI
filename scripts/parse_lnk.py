#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure-Python .lnk shortcut target resolver (no COM, no external deps).

Parses the Shell Link Binary File Format (.lnk) to extract the target
path (local or UNC) without using WScript.Shell / COM. Falls back to a
binary string scan if the structured parse yields nothing.
"""
import struct
import re
import sys
import os


def parse_lnk(lnk_path):
    with open(lnk_path, 'rb') as f:
        data = f.read()

    if data[:4] != b'\x4c\x00\x00\x00':
        return None, "not a .lnk file (bad magic)"

    link_flags = struct.unpack('<I', data[0x14:0x18])[0]
    has_id_list = bool(link_flags & 0x01)
    has_link_info = bool(link_flags & 0x02)

    offset = 0x4C  # 76-byte fixed header
    if has_id_list:
        id_list_size = struct.unpack('<H', data[offset:offset + 2])[0]
        offset += id_list_size

    candidates = []

    if has_link_info:
        li_size = struct.unpack('<I', data[offset:offset + 4])[0]
        li_header_size = struct.unpack('<I', data[offset + 4:offset + 8])[0]
        li_flags = struct.unpack('<I', data[offset + 8:offset + 12])[0]

        # --- ANSI LocalBasePath ---
        if li_flags & 0x01:
            # LocalBasePathOffset field lives at offset+0x10 (header >= 0x1C)
            lbp_off = struct.unpack('<I', data[offset + 0x10:offset + 0x14])[0]
            start = offset + lbp_off if lbp_off else offset + li_header_size
            end = data.find(b'\x00', start)
            if end == -1:
                end = len(data)
            s = data[start:end].decode('ascii', 'ignore').strip()
            if s:
                candidates.append(('ansi', s))

        # --- Unicode LocalBasePath ---
        if li_header_size >= 0x24:
            ubp_off = struct.unpack('<I', data[offset + 0x14:offset + 0x18])[0]
            if ubp_off:
                start = offset + ubp_off
                end = start
                while end + 1 < len(data) and not (data[end] == 0 and data[end + 1] == 0):
                    end += 2
                s = data[start:end].decode('utf-16-le', 'ignore').strip()
                if s:
                    candidates.append(('unicode', s))

    # --- Fallback: scan for path-like strings in the binary ---
    if not candidates:
        for m in re.finditer(rb'[A-Za-z]:\\\\[^\x00-\x1f]{3,240}', data):
            s = m.group(0).decode('ascii', 'ignore').strip()
            if s:
                candidates.append(('scan_ascii', s))
        for m in re.finditer(rb'(?:[A-Za-z]:\\\\)(?:[^\x00-\x1f]\x00){3,240}', data):
            s = m.group(0).decode('utf-16-le', 'ignore').strip()
            if s:
                candidates.append(('scan_utf16', s))
        for m in re.finditer(rb'\\\\\\\\[^\x00-\x1f]{3,240}', data):
            s = m.group(0).decode('ascii', 'ignore').strip()
            if s:
                candidates.append(('scan_unc', s))

    return candidates, None


def main():
    lnk = r"C:\Users\ShXAI\Desktop\世界杯.lnk"
    if not os.path.exists(lnk):
        print("SHORTCUT_MISSING: %s" % lnk)
        return
    cands, err = parse_lnk(lnk)
    if err:
        print("PARSE_ERROR: %s" % err)
        return
    print("CANDIDATE_TARGETS:")
    for kind, path in cands:
        exists = os.path.exists(path)
        print("  [%s]%s  exists=%s" % (kind, path, exists))
    # Prefer an existing directory/file
    for kind, path in cands:
        if os.path.exists(path):
            print("RESOLVED: %s" % path)
            if os.path.isdir(path):
                items = sorted(os.listdir(path))
                print("DIR_CONTENTS(%d):" % len(items))
                for it in items:
                    full = os.path.join(path, it)
                    tag = 'D' if os.path.isdir(full) else 'F'
                    print("  %s  %s" % (tag, it))
            return
    print("NO_EXISTING_TARGET_FOUND")


if __name__ == '__main__':
    main()
