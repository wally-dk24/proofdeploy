import os
import sqlite3

conn = sqlite3.connect(os.environ.get("APP_DB", "app.db"))
conn.execute(
    "INSERT OR IGNORE INTO users (username, password) VALUES (?, ?)",
    ("seeduser", "seedpass"),
)
uid = conn.execute(
    "SELECT id FROM users WHERE username = ?", ("seeduser",)
).fetchone()[0]
conn.execute(
    "INSERT OR IGNORE INTO notes (id, user_id, title, done) VALUES (1, ?, ?, 0)",
    (uid, "seeded open note"),
)
conn.execute(
    "INSERT OR IGNORE INTO notes (id, user_id, title, done) VALUES (2, ?, ?, 1)",
    (uid, "seeded done note"),
)
conn.commit()
print("seeded")
