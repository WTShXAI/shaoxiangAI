"""Analyze existing 72-match backtest results"""
import json
from collections import defaultdict

with open('D:/Architecture v4.0/data/full_backtest_72_matches.json', encoding='utf-8') as f:
    data = json.load(f)

total = len(data)
dir_ok = sum(1 for m in data if m.get('dir', '').strip() == chr(0x2705))
exact_ok = sum(1 for m in data if m.get('exact', '').strip() == chr(0x2705))
top3_ok = sum(1 for m in data if m.get('top3', '').strip() == chr(0x2705))

print(f"Total matches: {total}")
print(f"Direction acc: {dir_ok}/{total} = {dir_ok/total*100:.1f}%")
print(f"Exact score: {exact_ok}/{total} = {exact_ok/total*100:.1f}%")
print(f"Top3 hit: {top3_ok}/{total} = {top3_ok/total*100:.1f}%")

# Per date
by_date = defaultdict(lambda: {'total': 0, 'dir': 0, 'exact': 0})
for m in data:
    dt = m.get('date', '?')
    by_date[dt]['total'] += 1
    if m.get('dir', '').strip() == chr(0x2705):
        by_date[dt]['dir'] += 1
    if m.get('exact', '').strip() == chr(0x2705):
        by_date[dt]['exact'] += 1

print()
print("--- Per Date ---")
for dt in sorted(by_date.keys(), key=lambda x: tuple(map(int, x.split('/')))):
    d = by_date[dt]
    rate = d['dir'] / d['total'] * 100 if d['total'] > 0 else 0
    print(f"  {dt}: dir={d['dir']}/{d['total']} ({rate:.0f}%) exact={d['exact']}/{d['total']}")

# By prediction type
print()
print("--- By Prediction Type ---")
types = defaultdict(lambda: {'total': 0, 'ok': 0})
for m in data:
    pred = m.get('pred', '')
    if '主胜' in pred or '让胜' in pred:
        t = 'home_win'
    elif '客胜' in pred or '让负' in pred:
        t = 'away_win'
    elif '平' in pred:
        t = 'draw'
    else:
        t = 'other'
    types[t]['total'] += 1
    if m.get('dir', '').strip() == chr(0x2705):
        types[t]['ok'] += 1

for t, v in sorted(types.items()):
    if v['total'] > 0:
        rate = v['ok'] / v['total'] * 100
        print(f"  {t}: {v['ok']}/{v['total']} ({rate:.0f}%)")

# Worst performing dates
print()
print("--- Days With Direction <50% ---")
for dt in sorted(by_date.keys(), key=lambda x: tuple(map(int, x.split('/')))):
    d = by_date[dt]
    rate = d['dir'] / d['total'] * 100 if d['total'] > 0 else 0
    if rate < 50:
        print(f"  {dt}: {rate:.0f}%")
