import sqlite3, pandas as pd
DB = r"D:\Architecture\data\football_data.db"
con = sqlite3.connect(DB)
df = pd.read_sql_query("SELECT league_name, COUNT(*) c, MIN(match_date) mn, MAX(match_date) mx "
                       "FROM william_ht WHERE league_name LIKE '%英超%' OR league_name LIKE '%西甲%' "
                       "OR league_name LIKE '%意甲%' OR league_name LIKE '%德甲%' OR league_name LIKE '%法甲%' "
                       "GROUP BY league_name ORDER BY c DESC", con)
print(df.to_string())
print("---- 英超 分年 ----")
y = pd.read_sql_query("SELECT substr(match_date,1,4) y, COUNT(*) c FROM william_ht "
                      "WHERE league_name='英超' GROUP BY y ORDER BY y", con)
print(y.to_string())
con.close()
