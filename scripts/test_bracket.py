"""Test TournamentArchitect bracket tree"""
import sys; sys.path.insert(0,'D:/Architecture v4.0')
from pipeline.reverse_engine import TournamentArchitect

ta = TournamentArchitect()

print("=" * 60)
print("Third Slot Lookup")
print("=" * 60)
thirds = ['A','C','E','F','H','I','J','K']
result = ta.lookup_third_slot(thirds)
for k,v in result.items():
    slot = ta.THIRD_SLOTS[k]
    print(f"  {k}: {v} (pool={slot['pool']})")

print()
print("=" * 60)
print("Opponent Path (Group Winners)")
print("=" * 60)
for g in 'ABCDEFGHIJKL':
    path = ta.get_opponent_path(g, 1)
    print(f"  {g}1: {path['note']}")

print()
print("=" * 60)
print("Motivation Conflicts")
print("=" * 60)
for g, team in [('C','Brazil'),('E','Germany'),('I','France'),('J','Argentina')]:
    c = ta.check_motivation_conflict(team, g, 1)
    print(f"  {team}: conflict={c['has_conflict']} action={c['suggested_action']}")
    for r in c['reasoning']:
        print(f"    -> {r}")

print()
print("=" * 60)
print("Half Distribution")
print("=" * 60)
for g in 'ABCDEFGHIJKL':
    print(f"  Group {g}: {ta.get_half(g)}")
