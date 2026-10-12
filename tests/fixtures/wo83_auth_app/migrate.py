import os
import sqlite3

conn = sqlite3.connect(os.environ.get("APP_DB", "app.db"))
conn.executescript("""
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY,
  password TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  kind TEXT NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_keys (
  id TEXT PRIMARY KEY,
  secret TEXT NOT NULL
);
""")
conn.commit()
print("migrated")
