import os
import secrets
import sqlite3

from flask import Flask, g, jsonify, request

DB_PATH = os.environ.get("APP_DB", "app.db")

app = Flask(__name__)
TOKENS = {}


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def _close(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def issue_token(user_id):
    token = "pdcanary-" + secrets.token_hex(8)
    TOKENS[token] = user_id
    return token


def auth_user():
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    return TOKENS.get(token)


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/register")
def register():
    data = request.get_json(force=True, silent=True) or {}
    username, password = data.get("username"), data.get("password")
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    try:
        cur = db().execute(
            "INSERT INTO users (username, password) VALUES (?, ?)",
            (username, password),
        )
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "user exists"}), 409
    return jsonify({"token": issue_token(cur.lastrowid)})


@app.post("/login")
def login():
    data = request.get_json(force=True, silent=True) or {}
    row = db().execute(
        "SELECT id FROM users WHERE username = ? AND password = ?",
        (data.get("username"), data.get("password")),
    ).fetchone()
    if row is None:
        return jsonify({"error": "bad credentials"}), 401
    return jsonify({"token": issue_token(row["id"])})


@app.post("/notes")
def create_note():
    user_id = auth_user()
    if user_id is None:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    cur = db().execute(
        "INSERT INTO notes (user_id, title, done) VALUES (?, ?, 0)",
        (user_id, data.get("title", "")),
    )
    db().commit()
    return jsonify({"id": cur.lastrowid, "title": data.get("title", ""), "done": False})


@app.get("/notes")
def list_notes():
    user_id = auth_user()
    if user_id is None:
        return jsonify({"error": "unauthorized"}), 401
    rows = db().execute(
        "SELECT id, title, done FROM notes WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    return jsonify({
        "notes": [
            {"id": r["id"], "title": r["title"], "done": r["done"] == 1}
            for r in rows
        ]
    })


@app.get("/notes/<int:note_id>")
def get_note(note_id):
    user_id = auth_user()
    if user_id is None:
        return jsonify({"error": "unauthorized"}), 401
    row = db().execute(
        "SELECT id, title, done FROM notes WHERE id = ? AND user_id = ?",
        (note_id, user_id),
    ).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": row["id"],
        "title": row["title"],
        "done": row['done'] == 1,
        "tag": "general",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
