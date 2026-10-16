#!/usr/bin/env python3
"""
Study Submission backend.

A dependency-free HTTP server (Python standard library only) that:
  - Serves the submission form (submission.html)
  - Accepts multipart/form-data submissions with file uploads
  - Stores text fields in a SQLite database (submissions.db)
  - Saves uploaded files to the uploads/ directory and records them in the DB
  - Exposes a JSON API to list stored submissions

Run:
    python3 server.py
Then open http://localhost:8000 in a browser.
"""

import os
import re
import cgi
import json
import sqlite3
import uuid
import secrets
import mimetypes
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ai_worker
import helpdesk
import auth

# Event bus handle, set in main() once the worker starts.
EVENT_BUS = None

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "submissions.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
FORM_PATH = os.path.join(BASE_DIR, "submission.html")
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))

# Optional URL prefix this app is served under (e.g. "/app" behind a reverse
# proxy). Requests are normalized by stripping this prefix before routing.
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")

# Admin credentials. Override via environment variables ADMIN_USER / ADMIN_PASS.
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")
ADMIN_PATH = os.path.join(BASE_DIR, "admin.html")
REVIEW_PATH = os.path.join(BASE_DIR, "review.html")
RESULT_PATH = os.path.join(BASE_DIR, "result.html")
AUDIT_PATH = os.path.join(BASE_DIR, "audit.html")
HELPDESK_PATH = os.path.join(BASE_DIR, "helpdesk-admin.html")

# In-memory session tokens (token -> created_at). Cleared on server restart.
SESSIONS = {}

# Allowed upload types
ALLOWED_EXT = {".pdf", ".doc", ".docx"}
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB per file

# Text fields captured from the form (name -> db column)
TEXT_FIELDS = [
    "contactNameTH",
    "contactNameEN",
    "email",
    "phone",
    "titleTH",
    "titleEN",
    "protocolNo",
    "sirbNo",
    "sponsorName",
    "croName",
]

# File fields: form field name -> human label
FILE_FIELDS = {
    "docCover": "Cover letter",
    "docCTA": "Clinical trial agreement (Draft)",
    "docProtocol": "Protocol",
    "docSIRB": "SIRB/EC Certificate of approval",
    "sirbSubmittedLetter": "Letter - submitted to SIRB/EC",
}

REQUIRED_TEXT = {"contactNameTH", "email", "titleTH", "titleEN", "protocolNo", "sponsorName"}
REQUIRED_FILES = {"docCover", "docCTA", "docProtocol"}

# Review tracks (fixed departments that review each submission).
REVIEW_TRACKS = ["Legal", "Business Development"]
VALID_STATUSES = {"Pending", "Approved", "Rejected", "Needs revision"}


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_name_th TEXT,
            contact_name_en TEXT,
            email          TEXT,
            phone          TEXT,
            title_th       TEXT NOT NULL,
            title_en       TEXT NOT NULL,
            protocol_no    TEXT NOT NULL,
            sirb_no        TEXT,
            sponsor_name   TEXT NOT NULL,
            sponsor_type   TEXT,          -- JSON array of selected types
            cro_name       TEXT,
            created_at     TEXT NOT NULL, -- ISO 8601 UTC
            status         TEXT NOT NULL DEFAULT 'Pending',
            review_comment TEXT,
            reviewed_at    TEXT
        )
        """
    )
    # Migration: add columns if the table pre-dates them.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
    for col, ddl in (
        ("contact_name_th", "ALTER TABLE submissions ADD COLUMN contact_name_th TEXT"),
        ("contact_name_en", "ALTER TABLE submissions ADD COLUMN contact_name_en TEXT"),
        ("email", "ALTER TABLE submissions ADD COLUMN email TEXT"),
        ("phone", "ALTER TABLE submissions ADD COLUMN phone TEXT"),
        ("status", "ALTER TABLE submissions ADD COLUMN status TEXT NOT NULL DEFAULT 'Pending'"),
        ("review_comment", "ALTER TABLE submissions ADD COLUMN review_comment TEXT"),
        ("reviewed_at", "ALTER TABLE submissions ADD COLUMN reviewed_at TEXT"),
        ("revision", "ALTER TABLE submissions ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"),
        ("resubmit_note", "ALTER TABLE submissions ADD COLUMN resubmit_note TEXT"),
        ("resubmitted_at", "ALTER TABLE submissions ADD COLUMN resubmitted_at TEXT"),
    ):
        if col not in existing:
            conn.execute(ddl)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at     TEXT NOT NULL,
            actor          TEXT NOT NULL,
            action         TEXT NOT NULL,
            submission_id  INTEGER,
            details_json   TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id  INTEGER NOT NULL,
            field          TEXT NOT NULL,
            label          TEXT NOT NULL,
            original_name  TEXT NOT NULL,
            stored_name    TEXT NOT NULL,
            size_bytes     INTEGER NOT NULL,
            mime_type      TEXT,
            FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id  INTEGER NOT NULL,
            track          TEXT NOT NULL,   -- e.g. 'Legal', 'Business Development'
            status         TEXT NOT NULL DEFAULT 'Pending',
            comment        TEXT,
            reviewer_name  TEXT,
            reviewed_at    TEXT,
            UNIQUE (submission_id, track),
            FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()


def log_audit(action, actor, submission_id=None, details=None):
    """Append a non-destructive audit event. Audit records are never edited/deleted."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO audit_logs (created_at, actor, action, submission_id, details_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                actor or "System",
                action,
                submission_id,
                json.dumps(details or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_audit_logs(limit=250, submission_id=None, action=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        clauses, params = [], []
        if submission_id is not None:
            clauses.append("submission_id = ?")
            params.append(submission_id)
        if action:
            clauses.append("action = ?")
            params.append(action)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        rows = conn.execute(
            "SELECT id, created_at, actor, action, submission_id, details_json "
            "FROM audit_logs" + where + " ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        result = []
        for row in rows:
            result.append({
                "id": row["id"], "createdAt": row["created_at"],
                "actor": row["actor"], "action": row["action"],
                "submissionId": row["submission_id"],
                "details": json.loads(row["details_json"] or "{}"),
            })
        return result
    finally:
        conn.close()



def insert_submission(fields, sponsor_types, saved_files):
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO submissions
                (contact_name_th, contact_name_en, email, phone,
                 title_th, title_en, protocol_no, sirb_no, sponsor_name,
                 sponsor_type, cro_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields.get("contactNameTH", ""),
                fields.get("contactNameEN", ""),
                fields.get("email", ""),
                fields.get("phone", ""),
                fields.get("titleTH", ""),
                fields.get("titleEN", ""),
                fields.get("protocolNo", ""),
                fields.get("sirbNo", ""),
                fields.get("sponsorName", ""),
                json.dumps(sponsor_types, ensure_ascii=False),
                fields.get("croName", ""),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        submission_id = cur.lastrowid
        for f in saved_files:
            cur.execute(
                """
                INSERT INTO attachments
                    (submission_id, field, label, original_name, stored_name,
                     size_bytes, mime_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    f["field"],
                    f["label"],
                    f["original_name"],
                    f["stored_name"],
                    f["size_bytes"],
                    f["mime_type"],
                ),
            )
        conn.commit()
        return submission_id
    finally:
        conn.close()


def compute_overall(track_statuses):
    """Derive the overall status from the per-track statuses.
    track_statuses: list of status strings for all REVIEW_TRACKS."""
    if any(st == "Rejected" for st in track_statuses):
        return "Rejected"
    if any(st == "Needs revision" for st in track_statuses):
        return "Needs revision"
    if track_statuses and all(st == "Approved" for st in track_statuses):
        return "Approved"
    if any(st != "Pending" for st in track_statuses):
        return "In review"
    return "Pending"


def _track_reviews(conn, submission_id):
    """Return a dict track -> review info, filled for every REVIEW_TRACK."""
    rows = conn.execute(
        "SELECT track, status, comment, reviewer_name, reviewed_at "
        "FROM reviews WHERE submission_id = ?",
        (submission_id,),
    ).fetchall()
    by_track = {r["track"]: r for r in rows}
    result = []
    for track in REVIEW_TRACKS:
        r = by_track.get(track)
        result.append({
            "track": track,
            "status": r["status"] if r else "Pending",
            "comment": (r["comment"] if r else "") or "",
            "reviewer": (r["reviewer_name"] if r else "") or "",
            "reviewedAt": r["reviewed_at"] if r else None,
        })
    return result


def _serialize(conn, s):
    atts = conn.execute(
        "SELECT field, label, original_name, stored_name, size_bytes, mime_type "
        "FROM attachments WHERE submission_id = ?",
        (s["id"],),
    ).fetchall()
    reviews = _track_reviews(conn, s["id"])
    overall = compute_overall([r["status"] for r in reviews])
    keys = s.keys()
    return {
        "id": s["id"],
        "contactNameTH": s["contact_name_th"] if "contact_name_th" in keys else None,
        "contactNameEN": s["contact_name_en"] if "contact_name_en" in keys else None,
        "email": s["email"] if "email" in keys else None,
        "phone": s["phone"] if "phone" in keys else None,
        "titleTH": s["title_th"],
        "titleEN": s["title_en"],
        "protocolNo": s["protocol_no"],
        "sirbNo": s["sirb_no"],
        "sponsorName": s["sponsor_name"],
        "sponsorType": json.loads(s["sponsor_type"] or "[]"),
        "croName": s["cro_name"],
        "createdAt": s["created_at"],
        "status": overall,
        "revision": s["revision"] if "revision" in keys else 1,
        "resubmitNote": s["resubmit_note"] if "resubmit_note" in keys else None,
        "resubmittedAt": s["resubmitted_at"] if "resubmitted_at" in keys else None,
        "reviews": reviews,
        "attachments": [
            {
                "field": a["field"],
                "label": a["label"],
                "originalName": a["original_name"],
                "storedName": a["stored_name"],
                "sizeBytes": a["size_bytes"],
                "mimeType": a["mime_type"],
            }
            for a in atts
        ],
        "analysis": ai_worker.get_analysis(DB_PATH, s["id"]),
    }


def list_submissions():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        subs = conn.execute("SELECT * FROM submissions ORDER BY id DESC").fetchall()
        return [_serialize(conn, s) for s in subs]
    finally:
        conn.close()


def get_submission(submission_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        s = conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        return _serialize(conn, s) if s else None
    finally:
        conn.close()


def upsert_review(submission_id, track, status, comment, reviewer):
    """Insert/update a track review and append a matching audit event."""
    conn = sqlite3.connect(DB_PATH)
    try:
        previous = conn.execute(
            "SELECT status, comment, reviewer_name FROM reviews "
            "WHERE submission_id = ? AND track = ?", (submission_id, track)
        ).fetchone()
        exists = conn.execute(
            "SELECT 1 FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if not exists:
            return False
        conn.execute(
            """
            INSERT INTO reviews (submission_id, track, status, comment, reviewer_name, reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(submission_id, track) DO UPDATE SET
                status = excluded.status,
                comment = excluded.comment,
                reviewer_name = excluded.reviewer_name,
                reviewed_at = excluded.reviewed_at
            """,
            (submission_id, track, status, comment, reviewer,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    log_audit(
        "review_updated", reviewer or (track + " Reviewer"), submission_id,
        {"track": track, "previousStatus": previous[0] if previous else "Pending",
         "newStatus": status, "previousComment": previous[1] if previous else "",
         "comment": comment},
    )
    return True


def resubmit_submission(submission_id, note, new_files):
    """Apply a user's revision: bump revision, store the response note, and
    replace ONLY the attachment fields that have a new file (other fields keep
    their existing files). Resets any non-Approved track back to Pending.
    Returns {"revision": n} or None if the submission does not exist.

    new_files: list of saved-file dicts for the fields the user re-uploaded."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT revision FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if not row:
            return None
        new_rev = (row["revision"] or 1) + 1
        now = datetime.now(timezone.utc).isoformat()

        replaced_fields = {f["field"] for f in new_files}

        # Only the OLD files of the fields being replaced should be removed.
        old_stored = [r["stored_name"] for r in conn.execute(
            "SELECT stored_name FROM attachments WHERE submission_id = ? AND field IN (%s)"
            % ",".join("?" for _ in replaced_fields),
            (submission_id, *replaced_fields),
        ).fetchall()] if replaced_fields else []

        # Replace per field: delete old rows for those fields, insert new ones.
        for f in new_files:
            conn.execute(
                "DELETE FROM attachments WHERE submission_id = ? AND field = ?",
                (submission_id, f["field"]),
            )
            conn.execute(
                """INSERT INTO attachments
                   (submission_id, field, label, original_name, stored_name, size_bytes, mime_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (submission_id, f["field"], f["label"], f["original_name"],
                 f["stored_name"], f["size_bytes"], f["mime_type"]),
            )

        # Reset tracks that were not Approved back to Pending for re-review
        conn.execute(
            "UPDATE reviews SET status='Pending', comment='', reviewer_name='', reviewed_at=NULL "
            "WHERE submission_id = ? AND status != 'Approved'",
            (submission_id,),
        )

        conn.execute(
            "UPDATE submissions SET revision = ?, resubmit_note = ?, resubmitted_at = ? WHERE id = ?",
            (new_rev, note, now, submission_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Remove only the replaced fields' old files from disk (after commit)
    for name in old_stored:
        _safe_remove(os.path.join(UPLOAD_DIR, name))
    return {"revision": new_rev}


def delete_submission(submission_id, actor="Staff"):
    """Delete a submission, its attachment rows, and the files on disk.
    Returns the number of submissions deleted (0 or 1)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = conn.execute(
            "SELECT title_en, protocol_no, revision FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        stored = conn.execute(
            "SELECT stored_name FROM attachments WHERE submission_id = ?",
            (submission_id,),
        ).fetchall()
        cur = conn.execute(
            "DELETE FROM submissions WHERE id = ?", (submission_id,)
        )
        conn.execute(
            "DELETE FROM attachments WHERE submission_id = ?", (submission_id,)
        )
        conn.execute(
            "DELETE FROM reviews WHERE submission_id = ?", (submission_id,)
        )
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()
    # Remove files only after the DB rows are gone
    if deleted:
        for row in stored:
            _safe_remove(os.path.join(UPLOAD_DIR, row["stored_name"]))
        log_audit(
            "submission_deleted", actor, submission_id,
            {"titleEN": snapshot["title_en"] if snapshot else "",
             "protocolNo": snapshot["protocol_no"] if snapshot else "",
             "revision": snapshot["revision"] if snapshot else None},
        )
    return deleted


def get_stats():
    conn = sqlite3.connect(DB_PATH)
    try:
        subs = conn.execute("SELECT count(*) FROM submissions").fetchone()[0]
        atts = conn.execute("SELECT count(*) FROM attachments").fetchone()[0]
        size = conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM attachments"
        ).fetchone()[0]
        return {"submissions": subs, "attachments": atts, "totalBytes": size}
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(name):
    name = os.path.basename(name or "")
    name = _SAFE_NAME_RE.sub("_", name).strip("._")
    return name or "file"


def ext_of(name):
    return os.path.splitext(name)[1].lower()


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "StudySubmission/1.0"

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type=None, download_name=None):
        if not os.path.isfile(path):
            self._send_json(404, {"error": "Not found"})
            return
        ctype = content_type or (mimetypes.guess_type(path)[0] or "application/octet-stream")
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            self.send_header(
                "Content-Disposition", 'attachment; filename="%s"' % download_name
            )
        else:
            # HTML pages: avoid stale CDN/browser cache so UI updates show.
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _strip_base(path):
        """Remove the configured BASE_PATH prefix (e.g. '/app') from a path."""
        if BASE_PATH and (path == BASE_PATH or path.startswith(BASE_PATH + "/")):
            stripped = path[len(BASE_PATH):]
            return stripped if stripped else "/"
        return path

    def _bearer_token(self):
        # Prefer Authorization header; fall back to session cookie (for pages).
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            return h[len("Bearer "):].strip()
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("sid="):
                return part[len("sid="):].strip()
        return None

    def _session(self):
        return auth.session_for(self._bearer_token())

    def _require_auth(self):
        """Returns the session dict if authorized, else sends 401 and returns None."""
        s = self._session()
        if s:
            return s
        self._send_json(401, {"error": "Authentication required"})
        return None

    def _require_admin(self):
        s = self._require_auth()
        if s is None:
            return None
        if not auth.is_admin(s):
            self._send_json(403, {"error": "Admin only"})
            return None
        return s

    # ----- GET -----
    def do_GET(self):
        parsed = urlparse(self.path)
        route = self._strip_base(parsed.path)
        if route == "/" or route == "/index.html":
            self._send_file(FORM_PATH, "text/html; charset=utf-8")
            return
        if route == "/login" or route == "/login.html":
            self._send_file(os.path.join(BASE_DIR, "login.html"), "text/html; charset=utf-8")
            return
        if route == "/admin" or route == "/admin.html":
            self._send_file(ADMIN_PATH, "text/html; charset=utf-8")
            return
        if route == "/review" or route == "/review.html":
            self._send_file(REVIEW_PATH, "text/html; charset=utf-8")
            return
        if route == "/audit" or route == "/audit.html":
            self._send_file(AUDIT_PATH, "text/html; charset=utf-8")
            return
        if route == "/helpdesk" or route == "/helpdesk.html":
            self._send_file(HELPDESK_PATH, "text/html; charset=utf-8")
            return
        if route == "/result" or route == "/result.html":
            self._send_file(RESULT_PATH, "text/html; charset=utf-8")
            return
        if route == "/result-preview" or route == "/result-preview.html":
            self._send_file(
                os.path.join(BASE_DIR, "result-preview.html"), "text/html; charset=utf-8"
            )
            return
        if route == "/chat-widget.js":
            self._send_file(
                os.path.join(BASE_DIR, "chat-widget.js"), "application/javascript; charset=utf-8"
            )
            return
        if route == "/theme.css":
            self._send_file(
                os.path.join(BASE_DIR, "theme.css"), "text/css; charset=utf-8"
            )
            return
        # Public: look up a single submission's result by ID
        if route == "/api/submission":
            qs = parse_qs(parsed.query)
            raw = (qs.get("id", [""])[0] or "").strip()
            try:
                sid = int(raw)
            except ValueError:
                self._send_json(400, {"error": "Invalid id"})
                return
            sub = get_submission(sid)
            if not sub:
                self._send_json(404, {"error": "Submission not found"})
                return
            # Only expose review-relevant fields publicly (not attachments/sponsor details)
            self._send_json(200, {
                "id": sub["id"],
                "titleEN": sub["titleEN"],
                "titleTH": sub["titleTH"],
                "protocolNo": sub["protocolNo"],
                "status": sub["status"],
                "createdAt": sub["createdAt"],
                "revision": sub["revision"],
                "resubmittedAt": sub["resubmittedAt"],
                "reviews": [
                    {
                        "track": r["track"],
                        "status": r["status"],
                        "comment": r["comment"],
                        "reviewedAt": r["reviewedAt"],
                    }
                    for r in sub["reviews"]
                ],
            })
            return
        # Current session info (who am I / role)
        if route == "/api/me":
            s = self._session()
            if not s:
                self._send_json(200, {"authenticated": False})
                return
            self._send_json(200, {
                "authenticated": True, "username": s["username"],
                "role": s["role"], "name": s["name"],
            })
            return
        # Append-only audit logs (read-only API; optional submissionId/action filters)
        if route == "/api/audit":
            if self._require_auth() is None:
                return
            qs = parse_qs(parsed.query)
            raw_sid = (qs.get("submissionId", [""])[0] or "").strip()
            try:
                sid = int(raw_sid) if raw_sid else None
            except ValueError:
                self._send_json(400, {"error": "Invalid submissionId"})
                return
            action = (qs.get("action", [""])[0] or "").strip() or None
            raw_limit = (qs.get("limit", ["250"])[0] or "250").strip()
            try:
                limit = int(raw_limit)
            except ValueError:
                limit = 250
            self._send_json(200, {"logs": list_audit_logs(limit, sid, action)})
            return
        # List all submissions (admin — no auth required)
        if route == "/api/submissions":
            if self._require_auth() is None:
                return
            self._send_json(200, {"submissions": list_submissions()})
            return
        # AI analysis result/status for one submission
        if route == "/api/analysis":
            if self._require_auth() is None:
                return
            qs = parse_qs(parsed.query)
            try:
                sid = int((qs.get("id", [""])[0] or "").strip())
            except ValueError:
                self._send_json(400, {"error": "Invalid id"})
                return
            analysis = ai_worker.get_analysis(DB_PATH, sid)
            if analysis is None:
                self._send_json(404, {"error": "No analysis found"})
                return
            self._send_json(200, analysis)
            return
        # Configured review tracks (public — used by forms)
        if route == "/api/tracks":
            self._send_json(200, {"tracks": REVIEW_TRACKS})
            return
        # Stats (staff)
        if route == "/api/admin/stats":
            if self._require_auth() is None:
                return
            self._send_json(200, get_stats())
            return
        # Help desk: fetch one ticket's messages (user polls this — public)
        if route == "/api/chat/messages":
            qs = parse_qs(parsed.query)
            try:
                tid = int((qs.get("ticket", [""])[0] or "").strip())
            except ValueError:
                self._send_json(400, {"error": "Invalid ticket id"})
                return
            data = helpdesk.get_messages(DB_PATH, tid)
            if data is None:
                self._send_json(404, {"error": "Ticket not found"})
                return
            self._send_json(200, data)
            return
        # Help desk: list all tickets (staff)
        if route == "/api/helpdesk/tickets":
            if self._require_auth() is None:
                return
            self._send_json(200, {"tickets": helpdesk.list_tickets(DB_PATH)})
            return
        # Download an uploaded file (staff only — contains applicant documents)
        if route.startswith("/uploads/"):
            if self._require_auth() is None:
                return
            stored = sanitize_filename(route[len("/uploads/"):])
            self._send_file(os.path.join(UPLOAD_DIR, stored), download_name=stored)
            return
        self._send_json(404, {"error": "Not found"})

    # ----- POST -----
    def do_POST(self):
        self.path = self._strip_base(urlparse(self.path).path)

        # Staff login — returns a session token (also set as cookie for pages)
        if self.path == "/api/login":
            body = self._read_json_body()
            token = auth.authenticate(
                DB_PATH, body.get("username", ""), body.get("password", "")
            )
            if not token:
                log_audit("login_failed", (body.get("username") or "?"), None, {})
                self._send_json(401, {"error": "Invalid username or password"})
                return
            s = auth.session_for(token)
            log_audit("login_success", s["username"], None, {"role": s["role"]})
            body_out = json.dumps(
                {"ok": True, "token": token, "username": s["username"],
                 "role": s["role"], "name": s["name"]},
                ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body_out)))
            # Session cookie so page navigations (review/audit) are gated too.
            self.send_header("Set-Cookie",
                             "sid=%s; Path=/; HttpOnly; SameSite=Lax" % token)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body_out)
            return

        # Logout
        if self.path == "/api/logout":
            tok = self._bearer_token()
            s = auth.session_for(tok)
            if s:
                log_audit("logout", s["username"], None, {})
            auth.logout(tok)
            self._send_json(200, {"ok": True})
            return

        # Help desk: start a new chat ticket
        if self.path == "/api/chat/start":
            body = self._read_json_body()
            subject = (body.get("subject") or "").strip() or None
            ref = (body.get("submissionRef") or "").strip() or None
            tid = helpdesk.create_ticket(DB_PATH, subject, ref)
            log_audit("helpdesk_ticket_created", "Applicant", None, {
                "ticketId": tid, "subject": subject or "General question", "submissionRef": ref
            })
            self._send_json(201, {"ok": True, "ticketId": tid})
            return

        # Help desk: user sends a message (bot replies, may escalate)
        if self.path == "/api/chat/send":
            body = self._read_json_body()
            try:
                tid = int(body.get("ticketId"))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "Invalid ticketId"})
                return
            text = (body.get("text") or "").strip()
            if not text:
                self._send_json(400, {"error": "Empty message"})
                return
            escalate = bool(body.get("escalate"))
            result = helpdesk.post_user_message(DB_PATH, tid, text, escalate=escalate)
            if result is None:
                self._send_json(404, {"error": "Ticket not found"})
                return
            log_audit("helpdesk_message_sent", "Applicant", None, {
                "ticketId": tid, "text": text, "escalated": result.get("escalated", False),
                "ticketStatus": result.get("status")
            })
            self._send_json(200, {"ok": True, **result})
            return

        # Help desk: staff replies to a ticket
        if self.path == "/api/helpdesk/reply":
            body = self._read_json_body()
            try:
                tid = int(body.get("ticketId"))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "Invalid ticketId"})
                return
            text = (body.get("text") or "").strip()
            if not text:
                self._send_json(400, {"error": "Empty message"})
                return
            s = self._require_auth()
            if s is None:
                return
            ok = helpdesk.post_staff_message(DB_PATH, tid, text)
            if not ok:
                self._send_json(404, {"error": "Ticket not found"})
                return
            log_audit("helpdesk_staff_replied", s["name"], None, {"ticketId": tid, "text": text})
            self._send_json(200, {"ok": True})
            return

        # Re-run AI analysis for one submission (body {"id": N}) or all (no id)
        if self.path == "/api/admin/reanalyze":
            s = self._require_auth()
            if s is None:
                return
            if EVENT_BUS is None:
                self._send_json(503, {"error": "Analysis worker not running"})
                return
            body = self._read_json_body()
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                if body.get("id") is not None:
                    try:
                        ids = [int(body.get("id"))]
                    except (TypeError, ValueError):
                        self._send_json(400, {"error": "Invalid id"})
                        return
                else:
                    ids = [r["id"] for r in conn.execute("SELECT id FROM submissions")]
                queued = 0
                for sid in ids:
                    files = conn.execute(
                        "SELECT field, label, stored_name FROM attachments WHERE submission_id = ?",
                        (sid,),
                    ).fetchall()
                    if not conn.execute("SELECT 1 FROM submissions WHERE id = ?", (sid,)).fetchone():
                        continue
                    ai_worker.set_analysis_status(
                        DB_PATH, sid, "queued",
                        queued_at=datetime.now(timezone.utc).isoformat(),
                    )
                    log_audit("ai_analysis_queued", "Staff", sid, {"trigger": "manual_reanalyze"})
                    EVENT_BUS.emit("submission.created", {
                        "id": sid,
                        "files": [
                            {"field": f["field"], "label": f["label"], "stored_name": f["stored_name"]}
                            for f in files
                        ],
                    })
                    queued += 1
            finally:
                conn.close()
            self._send_json(200, {"ok": True, "queued": queued})
            return

        # Review a submission for one track (role-gated: Legal/BD per track, Admin any)
        if self.path == "/api/admin/review":
            s = self._require_auth()
            if s is None:
                return
            body = self._read_json_body()
            try:
                sid = int(body.get("id"))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "Invalid id"})
                return
            track = (body.get("track") or "").strip()
            status = (body.get("status") or "").strip()
            comment = (body.get("comment") or "").strip()
            if track not in REVIEW_TRACKS:
                self._send_json(400, {"error": "Invalid review track"})
                return
            if not auth.can_edit_track(s, track):
                self._send_json(403, {"error": "Your role cannot edit the %s track" % track})
                return
            if status not in VALID_STATUSES:
                self._send_json(400, {"error": "Invalid status"})
                return
            # Reviewer identity comes from the session, not the client.
            reviewer = s["name"]
            ok = upsert_review(sid, track, status, comment, reviewer)
            if ok:
                sub = get_submission(sid)
                self._send_json(200, {
                    "ok": True, "id": sid, "track": track,
                    "trackStatus": status, "overall": sub["status"],
                })
            else:
                self._send_json(404, {"error": "Submission not found"})
            return

        # Delete a submission (admin — no auth required)
        if self.path == "/api/admin/delete":
            s = self._require_admin()
            if s is None:
                return
            body = self._read_json_body()
            try:
                sid = int(body.get("id"))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "Invalid id"})
                return
            deleted = delete_submission(sid, actor=s["name"])
            if deleted:
                self._send_json(200, {"ok": True, "deleted": sid})
            else:
                self._send_json(404, {"error": "Submission not found"})
            return

        if self.path not in ("/api/submit", "/api/resubmit"):
            self._send_json(404, {"error": "Not found"})
            return
        is_resubmit = (self.path == "/api/resubmit")

        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._send_json(400, {"error": "Expected multipart/form-data"})
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": ctype,
            },
            keep_blank_values=True,
        )

        # Text fields
        fields = {}
        for key in TEXT_FIELDS:
            fields[key] = (form.getfirst(key, "") or "").strip()

        # Sponsor type (multi-value checkboxes)
        sponsor_types = form.getlist("sponsorType")

        errors = {}
        resubmit_id = None
        resubmit_note = ""
        if is_resubmit:
            # Resubmission: need a valid existing submission id + a note.
            try:
                resubmit_id = int((form.getfirst("id", "") or "").strip())
            except ValueError:
                self._send_json(400, {"error": "Invalid id"})
                return
            if get_submission(resubmit_id) is None:
                self._send_json(404, {"error": "Submission not found"})
                return
            resubmit_note = (form.getfirst("resubmitNote", "") or "").strip()
            if not resubmit_note:
                errors["resubmitNote"] = "Please describe what you changed."
        else:
            # New submission: validate required text + email format.
            for key in REQUIRED_TEXT:
                if not fields.get(key):
                    errors[key] = "This field is required."
            email = fields.get("email", "")
            if email and not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
                errors["email"] = "Enter a valid email address."

        # Collect and validate files
        pending = {}  # field -> fileitem
        for field, label in FILE_FIELDS.items():
            if field in form:
                item = form[field]
                # When a single file field, item is a FieldStorage with .filename
                if isinstance(item, list):
                    item = item[0] if item else None
                if item is not None and getattr(item, "filename", None):
                    pending[field] = item

        # Required files are enforced only on a NEW submission. On resubmit,
        # unchanged fields keep their existing files, so none are mandatory.
        if not is_resubmit:
            for field in REQUIRED_FILES:
                if field not in pending:
                    errors[field] = "A file is required."

        # Validate file extensions / size
        for field, item in pending.items():
            ext = ext_of(item.filename)
            if ext not in ALLOWED_EXT:
                errors[field] = "Only PDF or Word files are allowed."

        if errors:
            self._send_json(422, {"error": "Validation failed", "fields": errors})
            return

        # Persist files to disk
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        saved_files = []
        try:
            for field, item in pending.items():
                raw = item.file.read()
                if len(raw) == 0:
                    continue
                if len(raw) > MAX_FILE_BYTES:
                    # Roll back any files already written
                    for sf in saved_files:
                        _safe_remove(os.path.join(UPLOAD_DIR, sf["stored_name"]))
                    self._send_json(
                        422,
                        {
                            "error": "Validation failed",
                            "fields": {field: "File exceeds 25 MB limit."},
                        },
                    )
                    return
                original = sanitize_filename(item.filename)
                stored = "%s_%s" % (uuid.uuid4().hex, original)
                with open(os.path.join(UPLOAD_DIR, stored), "wb") as out:
                    out.write(raw)
                saved_files.append(
                    {
                        "field": field,
                        "label": FILE_FIELDS[field],
                        "original_name": item.filename,
                        "stored_name": stored,
                        "size_bytes": len(raw),
                        "mime_type": (item.type or mimetypes.guess_type(original)[0] or ""),
                    }
                )

            if is_resubmit:
                resubmit_submission(resubmit_id, resubmit_note, saved_files)
                submission_id = resubmit_id
            else:
                submission_id = insert_submission(fields, sponsor_types, saved_files)
        except Exception as exc:  # noqa: BLE001
            for sf in saved_files:
                _safe_remove(os.path.join(UPLOAD_DIR, sf["stored_name"]))
            self._send_json(500, {"error": "Server error: %s" % exc})
            return

        # --- Append-only audit event ---
        if is_resubmit:
            log_audit(
                "revision_submitted", "Applicant", submission_id,
                {"revision": get_submission(submission_id)["revision"],
                 "note": resubmit_note,
                 "replacedDocuments": [f["label"] for f in saved_files]},
            )
        else:
            log_audit(
                "submission_created", "Applicant", submission_id,
                {"titleEN": fields.get("titleEN"), "protocolNo": fields.get("protocolNo"),
                 "documents": [f["label"] for f in saved_files]},
            )

        # --- Event-driven: queue the submission for async AI analysis ---
        if EVENT_BUS is not None:
            try:
                ai_worker.set_analysis_status(
                    DB_PATH, submission_id, "queued",
                    queued_at=datetime.now(timezone.utc).isoformat(),
                )
                log_audit("ai_analysis_queued", "System", submission_id, {
                    "trigger": "revision" if is_resubmit else "new_submission"
                })
                # On resubmit, analyze the FULL current file set (kept + new),
                # not just the newly uploaded files.
                if is_resubmit:
                    sub_now = get_submission(submission_id)
                    event_files = [
                        {"field": a["field"], "label": a["label"], "stored_name": a["storedName"]}
                        for a in (sub_now["attachments"] if sub_now else [])
                    ]
                else:
                    event_files = [
                        {"field": f["field"], "label": f["label"], "stored_name": f["stored_name"]}
                        for f in saved_files
                    ]
                EVENT_BUS.emit("submission.created", {
                    "id": submission_id,
                    "files": event_files,
                })
            except Exception as exc:  # noqa: BLE001
                print("[server] failed to queue analysis for #%s: %s" % (submission_id, exc))

        resp = {
            "ok": True,
            "id": submission_id,
            "analysis": "queued",
            "attachments": [
                {
                    "field": f["field"],
                    "label": f["label"],
                    "originalName": f["original_name"],
                    "storedName": f["stored_name"],
                    "sizeBytes": f["size_bytes"],
                }
                for f in saved_files
            ],
        }
        if is_resubmit:
            sub = get_submission(submission_id)
            resp["revision"] = sub["revision"] if sub else None
            resp["resubmitted"] = True
        self._send_json(201, resp)

    def log_message(self, fmt, *args):
        # Concise logging
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _backfill_analysis():
    """Queue analysis for any submission that has no analysis row yet."""
    if EVENT_BUS is None:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        subs = conn.execute("SELECT id FROM submissions").fetchall()
        done = {r[0] for r in conn.execute("SELECT submission_id FROM analyses")}
        for s in subs:
            if s["id"] in done:
                continue
            files = conn.execute(
                "SELECT field, label, stored_name FROM attachments WHERE submission_id = ?",
                (s["id"],),
            ).fetchall()
            ai_worker.set_analysis_status(
                DB_PATH, s["id"], "queued",
                queued_at=datetime.now(timezone.utc).isoformat(),
            )
            EVENT_BUS.emit("submission.created", {
                "id": s["id"],
                "files": [
                    {"field": f["field"], "label": f["label"], "stored_name": f["stored_name"]}
                    for f in files
                ],
            })
    finally:
        conn.close()


def main():
    global EVENT_BUS
    init_db()
    helpdesk.init_helpdesk_db(DB_PATH)
    auth.init_auth_db(DB_PATH)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    EVENT_BUS = ai_worker.start_worker(DB_PATH, UPLOAD_DIR)
    _backfill_analysis()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Study Submission server running at http://localhost:%d" % PORT)
    print("Database: %s" % DB_PATH)
    print("Uploads:  %s" % UPLOAD_DIR)
    print("AI analysis worker: started (event-driven, mock engine)")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.server_close()


if __name__ == "__main__":
    main()
