import sqlite3

# 连接数据库（如果不存在会自动创建）
conn = sqlite3.connect('football_data.db')
cursor = conn.cursor()

# 创建表（直接复制我们之前讨论的表结构）
cursor.execute('''
    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY,
        match_date TEXT,
        home_team TEXT,
        away_team TEXT,
        league TEXT
    )
''')

# 插入一条测试数据
cursor.execute("INSERT INTO matches (match_date, home_team, away_team, league) VALUES (?, ?, ?, ?)",
               ('2026-05-25', 'Team A', 'Team B', 'Premier League'))

conn.commit()
conn.close()
print("数据库创建成功！数据已写入 football_data.db 文件")