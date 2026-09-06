"""AH 开盘定价与打错校验表.
读取 ah_opening_analysis.db (ah_clean), 按 AH 盘口线(含正负)统计:
  - 让球方(负线=主让)开盘赔率均值 fav_odds / 受让方 dog_odds  (特征值赔率)
  - 去水隐含 P(让球方覆盖) = 市场公平概率
  - 实际让球方覆盖率 (fav_cover 均值)
  - edge = 实际 - 隐含 (正=让球方被低估有值博, 负=被高估)
直接回答: AH 赔率定价各是多少 / 这样判断对不对(市场校准度).
"""
import sqlite3, csv, os

DB = 'data/ah_opening_analysis.db'
OUT_CSV = 'analysis/ah_calibration_by_line.csv'
OUT_MD = 'analysis/ah_calibration_by_line.md'

con = sqlite3.connect(DB, timeout=60)
cur = con.cursor()

row = cur.execute("""
SELECT COUNT(*), AVG(fav_odds), AVG(dog_odds), AVG(implied_p_fav),
       AVG(CAST(fav_cover AS REAL)), SUM(fav_cover)
FROM ah_clean
""").fetchone()
n, avg_fav, avg_dog, avg_impl, avg_hit, n_cov = row
print("=== 总体 ===")
print(f"有效样本 n={n}")
print(f"让球方开盘均赔 {avg_fav:.3f}  受让方开盘均赔 {avg_dog:.3f}")
print(f"去水隐含P(让球方覆盖)={avg_impl:.4f}  实际覆盖率={avg_hit:.4f}  edge={avg_hit-avg_impl:+.4f}")
print(f"让球方覆盖次数 {n_cov}/{n} = {n_cov/n:.4f}")

print("\n=== 按 AH 盘口线 (n>=30) ===")
rows = cur.execute("""
SELECT line,
       COUNT(*) n,
       ROUND(AVG(fav_odds),3) avg_fav,
       ROUND(AVG(dog_odds),3) avg_dog,
       ROUND(AVG(implied_p_fav),4) impl_p_fav,
       ROUND(AVG(CAST(fav_cover AS REAL)),4) actual_cov,
       SUM(fav_cover) n_cov
FROM ah_clean
GROUP BY line HAVING COUNT(*)>=30
ORDER BY line
""").fetchall()

os.makedirs('analysis', exist_ok=True)
with open(OUT_CSV,'w',newline='',encoding='utf-8') as f:
    w=csv.writer(f)
    w.writerow(['line','n','avg_fav_odds','avg_dog_odds','implied_p_fav_cover','actual_cover_rate','edge','n_cover'])
    for line,n_,af,ad,ip,ac,nc in rows:
        w.writerow([line,n_,af,ad,ip,ac,round(ac-ip,4),nc])
with open(OUT_MD,'w',encoding='utf-8') as f:
    f.write("# AH 开盘定价与打错校验表\n\n")
    f.write(f"- 有效样本 n={n}\n")
    f.write(f"- 让球方开盘均赔 {avg_fav:.3f} / 受让方开盘均赔 {avg_dog:.3f}\n")
    f.write(f"- 去水隐含P(让球方覆盖)=**{avg_impl:.4f}**，实际覆盖率=**{avg_hit:.4f}**，edge={avg_hit-avg_impl:+.4f}\n\n")
    f.write("| AH线 | 样本n | 让球方赔 | 受让方赔 | 去水P(覆盖) | 实际覆盖率 | edge(实际-隐含) |\n")
    f.write("|---|---|---|---|---|---|---|\n")
    for line,n_,af,ad,ip,ac,nc in rows:
        f.write(f"| {line} | {n_} | {af} | {ad} | {ip} | {ac} | {ac-ip:+.4f} |\n")

print(f"\n{'line':>7} {'n':>6} {'fav':>6} {'dog':>6} {'implP':>7} {'actual':>7} {'edge':>7}")
for line,n_,af,ad,ip,ac,nc in rows:
    print(f"{line:>7} {n_:>6} {af:>6} {ad:>6} {ip:>7} {ac:>7} {ac-ip:>+7.4f}")

con.close()
print(f"\nCSV -> {OUT_CSV}\nMD  -> {OUT_MD}")
