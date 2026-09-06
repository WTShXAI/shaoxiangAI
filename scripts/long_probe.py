# -*- coding: utf-8 -*-
"""Probe: re-OCR a sample of 'other' images and dump (text, conf, bbox) to inspect layout."""
import sqlite3, json, sys
from pathlib import Path
from rapidocr_onnxruntime import RapidOCR

DB = Path("D:/Architecture/data/long_images.db")
LONG = Path("D:/Architecture/long")

ocr = RapidOCR()
N = int(sys.argv[1]) if len(sys.argv) > 1 else 12

conn = sqlite3.connect(str(DB))
rows = conn.execute(
    "SELECT path FROM images WHERE page_type='other' ORDER BY id LIMIT ?", (N,)
).fetchall()
print(f"# probing {len(rows)} 'other' images\n")

for (path,) in rows:
    p = Path(path)
    if not p.exists():
        print(f"## MISSING {path}")
        continue
    result, _ = ocr(str(p))
    print(f"## FILE: {p.name}  (lines={len(result) if result else 0})")
    if result:
        for box, txt, conf in result:
            x0 = min(b[0] for b in box); y0 = min(b[1] for b in box)
            x1 = max(b[0] for b in box); y1 = max(b[1] for b in box)
            cx = (x0+x1)/2; cy=(y0+y1)/2
            print(f"  ({cx:6.0f},{cy:6.0f}) [{float(conf):.2f}] {txt}")
    print()
