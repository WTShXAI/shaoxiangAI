"""Investigate candidate tables that may hold OU line + actual result pairs."""
import sqlite3
DB = r"D:\Architecture\data\football_data.db"
con = sqlite3.connect(DB)
cur = con.cursor()

def q(sql):
    return cur.execute(sql).fetchall()

print("===== world_cup_2026_predictions =====")
print("total rows:", q("SELECT COUNT(*) FROM world_cup_2026_predictions")[0][0])
print("rows with actual_score not null:", q("SELECT COUNT(*) FROM world_cup_2026_predictions WHERE actual_score IS NOT NULL")[0][0])
print("distinct ou_line values:", q("SELECT DISTINCT ou_line FROM world_cup_2026_predictions WHERE ou_line IS NOT NULL ORDER BY ou_line"))
print("sample (ou_line,ou_over,ou_under,actual_score,recommended_direction):")
for r in q("SELECT ou_line,ou_over,ou_under,actual_score,recommended_direction,recommended_market FROM world_cup_2026_predictions WHERE actual_score IS NOT NULL LIMIT 8"):
    print("  ", r)

print("\n===== live_odds_raw =====")
print("total rows:", q("SELECT COUNT(*) FROM live_odds_raw")[0][0])
print("rows with actual_score not null:", q("SELECT COUNT(*) FROM live_odds_raw WHERE actual_score IS NOT NULL")[0][0])
print("sample (totals, actual_score):")
for r in q("SELECT totals, actual_score FROM live_odds_raw WHERE actual_score IS NOT NULL LIMIT 5"):
    print("  ", r)

print("\n===== submarket_bets =====")
print("total rows:", q("SELECT COUNT(*) FROM submarket_bets")[0][0])
print("distinct market values:", q("SELECT DISTINCT market FROM submarket_bets"))
print("rows with actual_score not null:", q("SELECT COUNT(*) FROM submarket_bets WHERE actual_score IS NOT NULL")[0][0])
print("market counts:", q("SELECT market, COUNT(*) FROM submarket_bets GROUP BY market ORDER BY COUNT(*) DESC LIMIT 15"))

print("\n===== bet_records =====")
print("total rows:", q("SELECT COUNT(*) FROM bet_records")[0][0])
print("distinct bet_type:", q("SELECT DISTINCT bet_type FROM bet_records"))
print("rows with actual_score not null:", q("SELECT COUNT(*) FROM bet_records WHERE actual_score IS NOT NULL")[0][0])

con.close()
