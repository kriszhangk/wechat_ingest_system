from app.db import Database

db = Database()

items = [
    ("机器之心", "机器之心", 10, 180),
    ("新智元", "新智元", 9, 180),
    ("量子位", "量子位", 8, 180),
]

with db.connect() as conn:
    for account_name, keyword, priority, interval in items:
        exists = conn.execute("SELECT 1 FROM targets WHERE keyword=?", (keyword,)).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO targets(account_name,keyword,enabled,priority,check_interval_minutes) VALUES(?,?,?,?,?)",
            (account_name, keyword, 1, priority, interval)
        )

print("seed done")
