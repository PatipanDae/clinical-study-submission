#!/usr/bin/env python3
"""
Authentication & role-based authorization (stdlib only).

Roles:
  - admin : full access (view all, delete, re-run AI, help desk, audit, both review tracks)
  - legal : review panel + audit view; may edit ONLY the "Legal" review track
  - bd    : review panel + audit view; may edit ONLY the "Business Development" track

Applicants are NOT users — the public Submission and Result pages require no login.

Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes. Sessions are opaque
tokens kept in memory (cleared on restart) — fine for a demo/internal deployment.
"""

import os
import sqlite3
import secrets
import hashlib
from datetime import datetime, timezone

# role -> which review track they may edit ("*" = all)
ROLE_TRACK = {
    "admin": "*",
    "legal": "Legal",
    "bd": "Business Development",
}

# Default seed users. Passwords overridable via env (ADMIN_PW / LEGAL_PW / BD_PW).
DEFAULT_USERS = [
    ("admin", os.environ.get("ADMIN_PW", "Admin@123"),  "admin", "System Administrator"),
    ("legal", os.environ.get("LEGAL_PW", "Legal@123"),  "legal", "Legal Reviewer"),
    ("bd",    os.environ.get("BD_PW",    "Bd@123"),      "bd",    "Business Development Reviewer"),
]

# In-memory sessions: token -> {username, role, name, created}
SESSIONS = {}

_PBKDF_ROUNDS = 120_000


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF_ROUNDS)
    return salt, dk.hex()


def _verify_password(password, salt, expected_hex):
    _, computed = _hash_password(password, salt)
    return secrets.compare_digest(computed, expected_hex)


def init_auth_db(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username    TEXT PRIMARY KEY,
                salt        TEXT NOT NULL,
                pw_hash     TEXT NOT NULL,
                role        TEXT NOT NULL,
                name        TEXT,
                created_at  TEXT NOT NULL
            )
            """
        )
        # Seed default users only if missing (never overwrite existing).
        for username, password, role, name in DEFAULT_USERS:
            exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
            if not exists:
                salt, pw_hash = _hash_password(password)
                conn.execute(
                    "INSERT INTO users (username, salt, pw_hash, role, name, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, salt, pw_hash, role, name, _now()),
                )
        conn.commit()
    finally:
        conn.close()


def authenticate(db_path, username, password):
    """Return a new session token on success, else None."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", ((username or "").strip().lower(),)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if not _verify_password(password or "", row["salt"], row["pw_hash"]):
        return None
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {
        "username": row["username"],
        "role": row["role"],
        "name": row["name"] or row["username"],
        "created": _now(),
    }
    return token


def session_for(token):
    return SESSIONS.get(token) if token else None


def logout(token):
    SESSIONS.pop(token, None)


def can_edit_track(session, track):
    """True if the session's role may edit the given review track."""
    if not session:
        return False
    allowed = ROLE_TRACK.get(session["role"])
    return allowed == "*" or allowed == track


def is_admin(session):
    return bool(session and session["role"] == "admin")
