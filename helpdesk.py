#!/usr/bin/env python3
"""
Help desk chat — hybrid (bot + human) support.

Flow:
  user asks -> mock-AI bot answers from a small knowledge base (keyword match).
  If the bot can't answer (or the user escalates), the ticket is flagged for a
  human; staff reply from the review page; the user sees replies by polling.

Storage: two tables in the same SQLite DB.
  tickets(id, subject, status, created_at, updated_at, submission_ref)
    status: open | awaiting_staff | answered | closed
  chat_messages(id, ticket_id, sender, text, created_at)
    sender: user | bot | staff
"""

import re
import sqlite3
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_helpdesk_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            subject        TEXT,
            status         TEXT NOT NULL DEFAULT 'open',
            submission_ref TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id   INTEGER NOT NULL,
            sender      TEXT NOT NULL,   -- user | bot | staff
            text        TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------------
# Mock-AI bot: small knowledge base matched by keywords.
# Each entry: (keywords, answer). First match wins.
# ----------------------------------------------------------------------------
KNOWLEDGE = [
    (["เอกสาร", "แนบ", "document", "attach", "file", "ไฟล์", "ต้องใช้"],
     "เอกสารที่ต้องแนบมี: 1) Cover letter 2) Clinical trial agreement (ฉบับร่าง) "
     "3) Protocol 4) SIRB/EC Certificate of approval (ถ้ายังไม่มี ให้แนบจดหมายยืนยันการยื่น SIRB/EC). "
     "รับไฟล์ PDF หรือ Word (.pdf, .doc, .docx)."),
    (["สถานะ", "status", "pending", "in review", "รอ"],
     "ความหมายสถานะ: Pending = รอพิจารณา, In review = บางฝ่ายพิจารณาแล้วแต่ยังไม่ครบ, "
     "Approved = อนุมัติ, Needs revision = ขอให้แก้ไข, Rejected = ไม่อนุมัติ."),
    (["needs revision", "แก้ไข", "revise", "resubmit", "ส่งใหม่", "revision"],
     "ถ้าสถานะเป็น Needs revision หรือ Rejected: เปิดหน้า Check result ด้วยหมายเลขอ้างอิง (ID) "
     "จะมีฟอร์ม 'แก้ไขและส่งใหม่' — อัปโหลดเฉพาะไฟล์ที่ต้องการเปลี่ยน (เว้นว่าง = ใช้ไฟล์เดิม) "
     "และเขียนอธิบายสิ่งที่แก้ แล้วกดส่ง ระบบจะส่งให้พิจารณาใหม่."),
    (["เช็ค", "เช็ก", "ตรวจสอบ", "ผล", "result", "check", "ติดตาม", "id", "อ้างอิง"],
     "ตรวจผลได้ที่หน้า Check submission result โดยกรอกหมายเลขอ้างอิง (ID) ที่ได้ตอนยื่น "
     "จะเห็นสถานะรวมและผลพิจารณาของแต่ละฝ่าย (Legal / Business Development)."),
    (["ฝ่าย", "legal", "business", "review", "พิจารณา", "ใคร"],
     "โครงการจะถูกพิจารณาโดย 2 ฝ่าย: Legal (ด้านกฎหมาย/สัญญา) และ Business Development "
     "(ด้านธุรกิจ/งบประมาณ) แต่ละฝ่ายให้ผลแยกกัน แล้วระบบสรุปเป็นสถานะรวม."),
    (["sponsor", "สปอนเซอร์", "ประเภท", "type"],
     "ประเภท Sponsor เลือกได้: Domestic/Foreign Government Organization และ "
     "Domestic/Foreign Private Organization (เลือกได้มากกว่าหนึ่ง)."),
    (["ยื่น", "submit", "สมัคร", "เริ่ม", "กรอก"],
     "ยื่นเอกสารได้ที่หน้าแรก (Submission form): กรอกข้อมูลผู้ติดต่อ, ข้อมูลโครงการ (Section 1), "
     "แล้วแนบเอกสาร (Section 2) จากนั้นกด Submit จะได้หมายเลขอ้างอิง (ID) ไว้ติดตามผล."),
    (["สวัสดี", "hello", "hi", "ครับ", "ค่ะ", "ช่วย", "help"],
     "สวัสดีครับ 👋 ผมเป็นผู้ช่วยตอบคำถามเกี่ยวกับการยื่นเอกสาร ลองถามได้เลย เช่น "
     "'ต้องแนบเอกสารอะไรบ้าง', 'เช็กผลยังไง', 'สถานะ Needs revision ทำยังไงต่อ'."),
]

ESCALATE_HINTS = ["เจ้าหน้าที่", "คน", "staff", "human", "ติดต่อ", "โทร", "contact", "agent"]

FALLBACK = (
    "ขออภัย ผมยังไม่แน่ใจคำตอบสำหรับคำถามนี้ "
    "คุณสามารถกด 'ติดต่อเจ้าหน้าที่' เพื่อส่งคำถามให้ทีมงานตอบได้ครับ"
)


def bot_answer(text):
    """Return (answer, matched_bool). matched_bool=False means fallback used."""
    low = (text or "").lower()
    for keywords, answer in KNOWLEDGE:
        for kw in keywords:
            if kw.lower() in low:
                return answer, True
    return FALLBACK, False


def wants_human(text):
    low = (text or "").lower()
    return any(h.lower() in low for h in ESCALATE_HINTS)


# ----------------------------------------------------------------------------
# Ticket / message operations
# ----------------------------------------------------------------------------
def create_ticket(db_path, subject=None, submission_ref=None):
    conn = sqlite3.connect(db_path)
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO tickets (subject, status, submission_ref, created_at, updated_at) "
            "VALUES (?, 'open', ?, ?, ?)",
            (subject or "General question", submission_ref, now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _add_message(conn, ticket_id, sender, text):
    conn.execute(
        "INSERT INTO chat_messages (ticket_id, sender, text, created_at) VALUES (?, ?, ?, ?)",
        (ticket_id, sender, text, _now()),
    )
    conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (_now(), ticket_id))


def post_user_message(db_path, ticket_id, text, escalate=False):
    """Store the user's message, run the bot, and return the bot/ack reply.
    Returns dict: {reply, matched, status}."""
    conn = sqlite3.connect(db_path)
    try:
        exists = conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not exists:
            return None
        _add_message(conn, ticket_id, "user", text)

        if escalate or wants_human(text):
            conn.execute("UPDATE tickets SET status='awaiting_staff', updated_at=? WHERE id=?",
                         (_now(), ticket_id))
            reply = ("รับเรื่องแล้วครับ ผมได้ส่งคำถามของคุณให้เจ้าหน้าที่ "
                     "กรุณารอการตอบกลับในช่องแชทนี้ (คุณสามารถถามอย่างอื่นต่อได้)")
            _add_message(conn, ticket_id, "bot", reply)
            conn.commit()
            return {"reply": reply, "matched": True, "status": "awaiting_staff", "escalated": True}

        current = exists[0]
        answer, matched = bot_answer(text)
        _add_message(conn, ticket_id, "bot", answer)
        if not matched:
            # Bot could not answer -> needs a human.
            conn.execute("UPDATE tickets SET status='awaiting_staff', updated_at=? WHERE id=?",
                         (_now(), ticket_id))
            status = "awaiting_staff"
            needs_human = True
        else:
            needs_human = False
            # Bot resolved the question. Close it — unless a human is already
            # expected on this ticket (awaiting_staff/answered), then don't override.
            if current in ("awaiting_staff", "answered"):
                status = current
            else:
                follow = ("\n\nหากข้อมูลนี้ตอบคำถามของคุณแล้ว ถือว่าเรื่องนี้เสร็จสิ้นครับ "
                          "— หากยังต้องการความช่วยเหลือเพิ่มเติม พิมพ์ถามต่อ หรือกด 'ติดต่อเจ้าหน้าที่' ได้เลย")
                conn.execute("UPDATE tickets SET status='closed', updated_at=? WHERE id=?",
                             (_now(), ticket_id))
                _add_message(conn, ticket_id, "bot", follow.strip())
                status = "closed"
        conn.commit()
        return {"reply": answer, "matched": matched, "status": status,
                "escalated": False, "needsHuman": needs_human}
    finally:
        conn.close()


def post_staff_message(db_path, ticket_id, text):
    conn = sqlite3.connect(db_path)
    try:
        exists = conn.execute("SELECT 1 FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not exists:
            return False
        _add_message(conn, ticket_id, "staff", text)
        conn.execute("UPDATE tickets SET status='answered', updated_at=? WHERE id=?",
                     (_now(), ticket_id))
        conn.commit()
        return True
    finally:
        conn.close()


def get_messages(db_path, ticket_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        t = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not t:
            return None
        msgs = conn.execute(
            "SELECT sender, text, created_at FROM chat_messages WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
        return {
            "ticketId": t["id"],
            "subject": t["subject"],
            "status": t["status"],
            "submissionRef": t["submission_ref"],
            "messages": [
                {"sender": m["sender"], "text": m["text"], "createdAt": m["created_at"]}
                for m in msgs
            ],
        }
    finally:
        conn.close()


def list_tickets(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT t.*, "
            "(SELECT count(*) FROM chat_messages m WHERE m.ticket_id=t.id) AS msg_count, "
            "(SELECT text FROM chat_messages m WHERE m.ticket_id=t.id ORDER BY m.id DESC LIMIT 1) AS last_text "
            "FROM tickets t ORDER BY "
            "CASE t.status WHEN 'awaiting_staff' THEN 0 WHEN 'open' THEN 1 "
            "WHEN 'answered' THEN 2 ELSE 3 END, t.updated_at DESC"
        ).fetchall()
        return [
            {
                "ticketId": r["id"],
                "subject": r["subject"],
                "status": r["status"],
                "submissionRef": r["submission_ref"],
                "messageCount": r["msg_count"],
                "lastText": r["last_text"],
                "updatedAt": r["updated_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()
