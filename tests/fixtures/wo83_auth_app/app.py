"""WO-8.3 golden auth app (test fixture).

A small SQLite-backed HTTP app exercising every WO-8.3 credential
provider mode end to end. Pure standard library, so the same app
runs on the host (unit tests) and in the sandbox (end-to-end proofs)
with no third-party dependencies:

- POST /register {username, password} -> {"token"} (register mode)
- POST /login {email|username, password} ->
  {"access_token", "refresh_token", "expires_in"} (login mode, and
  the login leg of refresh mode)
- POST /auth/refresh {refresh_token} -> a fresh token pair, rotating
  the refresh token (refresh mode)
- GET /me with `Authorization: Bearer <access_token>` -> 200
- GET /admin/site with `Authorization: Ghost <jwt>` -> 200 when the
  JWT is an HS256 Ghost Admin API token (aud "/admin/") signed with
  the admin key the seed wrote to .pd-auth/admin_key (ghost_jwt mode)

Tokens the app mints carry the conformance canary prefix
(pdcanary- + 16 hex) so the kit's case-9-style scan covers them.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.environ.get("APP_DB", "app.db")


def _db() -> sqlite3.Connection:
    return sqlite3.connect(DB)


def _ttl() -> int:
    try:
        return int(os.environ.get("TOKEN_TTL_SECONDS", "3600"))
    except ValueError:
        return 3600


def _mint(conn: sqlite3.Connection, username: str, kind: str) -> str:
    token = "pdcanary-" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO tokens (token, username, kind, expires_at) VALUES (?, ?, ?, ?)",
        (token, username, kind, time.time() + _ttl()),
    )
    conn.commit()
    return token


def _pair(conn: sqlite3.Connection, username: str) -> dict:
    return {
        "access_token": _mint(conn, username, "access"),
        "refresh_token": _mint(conn, username, "refresh"),
        "expires_in": _ttl(),
    }


def _check_token(conn: sqlite3.Connection, token, kind: str):
    if not token:
        return None
    row = conn.execute(
        "SELECT username FROM tokens WHERE token = ? AND kind = ? AND expires_at > ?",
        (token, kind, time.time()),
    ).fetchone()
    return row[0] if row else None


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    # -- routes ----------------------------------------------------

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path == "/me":
            return self._me()
        if self.path == "/admin/site":
            return self._admin_site()
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/register":
            return self._register()
        if self.path == "/login":
            return self._login()
        if self.path == "/auth/refresh":
            return self._refresh()
        return self._send(404, {"error": "not found"})

    def _register(self):
        data = self._json_body()
        username = data.get("username", "")
        password = data.get("password", "")
        if not username or not password:
            return self._send(400, {"error": "username and password required"})
        conn = _db()
        try:
            conn.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, password),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return self._send(409, {"error": "user exists"})
        token = _mint(conn, username, "access")
        conn.close()
        return self._send(200, {"token": token})

    def _login(self):
        data = self._json_body()
        username = data.get("email") or data.get("username") or ""
        password = data.get("password", "")
        conn = _db()
        row = conn.execute(
            "SELECT username FROM users WHERE username = ? AND password = ?",
            (username, password),
        ).fetchone()
        if row is None:
            conn.close()
            return self._send(401, {"error": "invalid credentials"})
        pair = _pair(conn, username)
        conn.close()
        return self._send(200, pair)

    def _refresh(self):
        data = self._json_body()
        conn = _db()
        username = _check_token(conn, data.get("refresh_token"), "refresh")
        if username is None:
            conn.close()
            return self._send(401, {"error": "invalid refresh token"})
        # Rotation: the presented refresh token is single-use.
        conn.execute("DELETE FROM tokens WHERE token = ?", (data.get("refresh_token"),))
        conn.commit()
        pair = _pair(conn, username)
        conn.close()
        return self._send(200, pair)

    def _me(self):
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else None
        conn = _db()
        username = _check_token(conn, token, "access")
        conn.close()
        if username is None:
            return self._send(401, {"error": "unauthorized"})
        return self._send(200, {"username": username})

    def _admin_site(self):
        """Ghost Admin API shape: `Authorization: Ghost <jwt>` (HS256)."""
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Ghost "):
            return self._send(401, {"error": "unauthorized"})
        parts = auth[len("Ghost "):].split(".")
        if len(parts) != 3:
            return self._send(401, {"error": "unauthorized"})
        try:
            header = json.loads(_b64url_decode(parts[0]))
            payload = json.loads(_b64url_decode(parts[1]))
            signature = _b64url_decode(parts[2])
        except Exception:
            return self._send(401, {"error": "unauthorized"})
        if header.get("alg") != "HS256" or payload.get("aud") != "/admin/":
            return self._send(401, {"error": "unauthorized"})
        if float(payload.get("exp", 0)) <= time.time():
            return self._send(401, {"error": "unauthorized"})
        conn = _db()
        row = conn.execute(
            "SELECT secret FROM admin_keys WHERE id = ?", (header.get("kid", ""),)
        ).fetchone()
        conn.close()
        if row is None:
            return self._send(401, {"error": "unauthorized"})
        signing_input = f"{parts[0]}.{parts[1]}".encode()
        expected = hmac.new(
            bytes.fromhex(row[0]), signing_input, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, signature):
            return self._send(401, {"error": "unauthorized"})
        return self._send(200, {"ok": True, "site": "pd-golden-auth"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
