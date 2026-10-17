"use strict";
const Database = require("better-sqlite3");
const db = new Database(process.env.APP_DB || "app.db");
const user = db
  .prepare("INSERT OR IGNORE INTO users (username, password) VALUES (?, ?)")
  .run("seeduser", "seedpass");
const uid = db
  .prepare("SELECT id FROM users WHERE username = ?")
  .get("seeduser").id;
db.prepare("INSERT OR IGNORE INTO notes (id, user_id, title, done) VALUES (1, ?, ?, 0)")
  .run(uid, "seeded open note");
db.prepare("INSERT OR IGNORE INTO notes (id, user_id, title, done) VALUES (2, ?, ?, 1)")
  .run(uid, "seeded done note");
console.log("seeded");
