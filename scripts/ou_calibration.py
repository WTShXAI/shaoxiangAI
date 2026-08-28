"""OU 开盘定价与打错校验表.
读取 ou_opening_analysis.db (ou_clean), 按盘口线统计:
  - 大小球开盘赔率均值 (over_odds / under_odds)
  - 去水隐含 P(大) = 市场公平概率
  - 实际大球打出率 (over_win 均值)
  - edge = 实际 - 隐含 (正=大球被低估有值博, 负=大球被高估)
直接回答: 大小球赔率定价各是多少 / 这样判断对不对(市场校准度).
"""
import sqlite3, csv, os

DB = 'data/ou_opening_analysis.db'
OUT_CSV = 'analysis/ou_calibration_by_line.csv'
OUT_MD = 'analysis/ou_calibration_by_line.md'

con = sqlite3.connect(DB, timeout=60)
cur = con.cursor()

# 总体
row = cur.execute("""
SELECT COUNT(*), AVG(over_odds), AVG(under_odds), AVG(implied_p_over),
       AVG(CAST(over_win AS REAL)), SUM(over_win), SUM(CASE WHEN over_win IS NULL THEN 1 ELSE 0 END)
FROM ou_clean WHERE over_win IS NOT NULL
""").fetchone()
n, avg_o, avg_u, avg_impl, avg_hit, n_over, n_push = row
print("=== 总体 ===")
print(f"有效样本 n={n}  走水(剔除) n={n_push}")
print(f"大球开盘均赔 {avg_o:.3f}  小球开盘均赔 {avg_u:.3f}")
print(f"去水隐含P(大)={avg_impl:.4f}  实际大球打出率={avg_hit:.4f}  edge={avg_hit-avg_impl:+.4f}")
print(f"大球打出次数 {n_over}/{n} = {n_over/n:.4f}")

# 按盘口线
print("\n=== 按盘口线 (n>=30) ===")
rows = cur.execute("""
SELECT line,
       COUNT(*) n,
       ROUND(AVG(over_odds),3) avg_over,
       ROUND(AVG(under_odds),3) avg_under,
       ROUND(AVG(implied_p_over),4) impl_p_over,
       ROUND(AVG(CAST(over_win AS REAL)),4) actual_over_hit,
       SUM(over_win) n_over
FROM ou_clean WHERE over_win IS NOT NULL
GROUP BY line HAVING COUNT(*)>=30
ORDER BY line
""").fetchall()

# 写 CSV + MD
os.makedirs('analysis', exist_ok=True)
with open(OUT_CSV,'w',newline='',encoding='utf-8') as f:
    w=csv.writer(f)
    w.writerow(['line','n','avg_over_odds','avg_under_odds','implied_p_over','actual_over_hit','edge','n_over'])
    for line,n_,ao,au,ip,ah,no in rows:
        w.writerow([line,n_,ao,au,ip,ah,round(ah-ip,4),no])
with open(OUT_MD,'w',encoding='utf-8') as f:
    f.write("# OU 开盘定价与打错校验表\n\n")
    f.write(f"- 有效样本 n={n}，走水(整数盘平局)剔除 n={n_push}\n")
    f.write(f"- 大球开盘均赔 {avg_o:.3f} / 小球开盘均赔 {avg_u:.3f}\n")
    f.write(f"- 去水隐含P(大)=**{avg_impl:.4f}**，实际大球打出率=**{avg_hit:.4f}**，edge={avg_hit-avg_impl:+.4f}\n\n")
    f.write("| 盘口线 | 样本n | 大球开盘赔 | 小球开盘赔 | 去水P(大) | 实际大球打出率 | edge(实际-隐含) |\n")
    f.write("|---|---|---|---|---|---|---|\n")
    for line,n_,ao,au,ip,ah,no in rows:
        f.write(f"| {line} | {n_} | {ao} | {au} | {ip} | {ah} | {ah-ip:+.4f} |\n")

print(f"\n{'line':>6} {'n':>6} {'over':>6} {'under':>6} {'implP':>7} {'actual':>7} {'edge':>7}")
for line,n_,ao,au,ip,ah,no in rows:
    print(f"{line:>6} {n_:>6} {ao:>6} {au:>6} {ip:>7} {ah:>7} {ah-ip:>+7.4f}")

con.close()
print(f"\nCSV -> {OUT_CSV}\nMD  -> {OUT_MD}")
