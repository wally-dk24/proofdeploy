"use strict";
const express = require("express");
const Database = require("better-sqlite3");
const crypto = require("crypto");

const DB_PATH = process.env.APP_DB || "app.db";
const PORT = parseInt(process.env.PORT || "8000", 10);

const db = new Database(DB_PATH);
const app = express();
app.use(express.json());

const tokens = new Map();

function issueToken(userId) {
  const token = "pdcanary-" + crypto.randomBytes(8).toString("hex");
  tokens.set(token, userId);
  return token;
}

function auth(req, res, next) {
  const header = req.headers.authorization || "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : "";
  const userId = tokens.get(token);
  if (userId === undefined) {
    return res.status(401).json({ error: "unauthorized" });
  }
  req.userId = userId;
  next();
}

app.get("/health", (req, res) => res.json({ ok: true }));

app.post("/register", (req, res) => {
  const { username, password } = req.body || {};
  if (!username || !password) {
    return res.status(400).json({ error: "username and password required" });
  }
  try {
    const info = db
      .prepare("INSERT INTO users (username, password) VALUES (?, ?)")
      .run(username, password);
    return res.json({ token: issueToken(info.lastInsertRowid) });
  } catch (e) {
    return res.status(409).json({ error: "user exists" });
  }
});

app.post("/login", (req, res) => {
  const { username, password } = req.body || {};
  const user = db
    .prepare("SELECT id FROM users WHERE username = ? AND password = ?")
    .get(username, password);
  if (!user) {
    return res.status(401).json({ error: "bad credentials" });
  }
  return res.json({ token: issueToken(user.id) });
});

app.post("/notes", auth, (req, res) => {
  const title = (req.body || {}).title || "";
  const info = db
    .prepare("INSERT INTO notes (user_id, title, done) VALUES (?, ?, 0)")
    .run(req.userId, title);
  return res.json({ id: info.lastInsertRowid, title, done: false });
});

app.get("/notes/:id", auth, (req, res) => {
  const note = db
    .prepare("SELECT id, title, done FROM notes WHERE id = ? AND user_id = ?")
    .get(req.params.id, req.userId);
  if (!note) {
    return res.status(404).json({ error: "not found" });
  }
  return res.json({
    id: note.id,
    title: note.title,
    done: note.done === 1,

  });
});

app.listen(PORT, "0.0.0.0", () => {
  console.log("listening on " + PORT);
});
