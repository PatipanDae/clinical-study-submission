#!/usr/bin/env python3
"""
Event-driven mock AI document analysis.

Architecture (in-process event-driven):

    server (submit handler)
        │  emit("submission.created", {id, files...})
        ▼
    EventBus (thread-safe queue)
        ▼
    AnalysisWorker (background daemon thread)
        │  1. extract text from each attached file
        │  2. section completeness check
        │  3. per-section summary (mock AI)
        │  4. institutional-regulation compliance check
        ▼
    writes result to `analyses` table + emits "analysis.completed"

This is a MOCK: the "AI" uses deterministic, explainable heuristics (keyword
detection, length-based summaries, rule matching) instead of a real LLM, so it
runs locally with no credentials or cost. The event-driven shape is real:
submission returns immediately; analysis happens asynchronously.
"""

import os
import re
import json
import queue
import sqlite3
import threading
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# Domain config: required sections + institutional regulation rules
# ----------------------------------------------------------------------------
# Each section: a canonical name + keywords/aliases used to detect it in text.
REQUIRED_SECTIONS = [
    {"key": "objective",       "label": "Objective / Aims",
     "keywords": ["objective", "aim", "purpose", "goal", "วัตถุประสงค์"]},
    {"key": "background",      "label": "Background / Rationale",
     "keywords": ["background", "rationale", "introduction", "ที่มา", "เหตุผล"]},
    {"key": "methodology",     "label": "Methodology / Study design",
     "keywords": ["method", "methodology", "study design", "procedure", "ระเบียบวิธี", "วิธีการ"]},
    {"key": "participants",    "label": "Participants / Eligibility",
     "keywords": ["participant", "subject", "eligibility", "inclusion", "exclusion", "อาสาสมัคร", "ผู้เข้าร่วม"]},
    {"key": "informed_consent","label": "Informed consent",
     "keywords": ["informed consent", "consent form", "ความยินยอม", "ยินยอม"]},
    {"key": "risk",            "label": "Risk assessment",
     "keywords": ["risk", "adverse", "safety", "harm", "ความเสี่ยง", "ความปลอดภัย"]},
    {"key": "data_management", "label": "Data management / Confidentiality",
     "keywords": ["data management", "confidential", "privacy", "data protection", "ข้อมูล", "ความลับ"]},
]

# Institutional regulation rules. Each rule is checked against the full document
# text and/or the detected sections. `check` returns (passed: bool, detail: str).
def _rule_has_section(section_key):
    def check(ctx):
        present = ctx["sections"].get(section_key, {}).get("present", False)
        return present, ("Section present" if present else "Required section missing")
    return check

def _rule_regex(pattern, human):
    rx = re.compile(pattern, re.IGNORECASE)
    def check(ctx):
        ok = bool(rx.search(ctx["text"]))
        return ok, (human + (" found" if ok else " NOT found"))
    return check

REGULATION_RULES = [
    {"id": "REG-01", "title": "Informed consent must be documented",
     "severity": "critical", "check": _rule_has_section("informed_consent")},
    {"id": "REG-02", "title": "Risk assessment must be included",
     "severity": "critical", "check": _rule_has_section("risk")},
    {"id": "REG-03", "title": "Study objective must be stated",
     "severity": "major", "check": _rule_has_section("objective")},
    {"id": "REG-04", "title": "Data management / confidentiality plan required",
     "severity": "major", "check": _rule_has_section("data_management")},
    {"id": "REG-05", "title": "Protocol must carry a version identifier",
     "severity": "minor",
     "check": _rule_regex(r"\b(version|ver\.?|v\d|เวอร์ชัน)\b", "Protocol version")},
    {"id": "REG-06", "title": "Participant eligibility criteria must be defined",
     "severity": "major", "check": _rule_has_section("participants")},
]


# ----------------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------------
def init_analysis_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analyses (
            submission_id   INTEGER PRIMARY KEY,
            status          TEXT NOT NULL DEFAULT 'queued',  -- queued|processing|done|error
            result_json     TEXT,
            error           TEXT,
            queued_at       TEXT,
            started_at      TEXT,
            completed_at    TEXT,
            FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def set_analysis_status(db_path, submission_id, status, **extra):
    conn = sqlite3.connect(db_path)
    try:
        cols = ["submission_id", "status"]
        vals = [submission_id, status]
        for k, v in extra.items():
            cols.append(k)
            vals.append(v)
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join("%s=excluded.%s" % (c, c) for c in cols if c != "submission_id")
        conn.execute(
            "INSERT INTO analyses (%s) VALUES (%s) "
            "ON CONFLICT(submission_id) DO UPDATE SET %s"
            % (",".join(cols), placeholders, updates),
            vals,
        )
        conn.commit()
    finally:
        conn.close()


def _audit(db_path, action, submission_id, details=None):
    """Append an audit record from the background worker without importing server."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO audit_logs (created_at, actor, action, submission_id, details_json) "
            "VALUES (?, 'System', ?, ?, ?)",
            (_now(), action, submission_id, json.dumps(details or {}, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def get_analysis(db_path, submission_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM analyses WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"],
            "queuedAt": row["queued_at"],
            "startedAt": row["started_at"],
            "completedAt": row["completed_at"],
        }
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Mock AI analysis
# ----------------------------------------------------------------------------
def _extract_docx(path):
    """Extract visible text from a .docx (Office Open XML) using stdlib only.
    A .docx is a ZIP; the body text lives in word/document.xml inside <w:t>
    runs, with <w:p> marking paragraphs and <w:tab>/<w:br> as whitespace."""
    import zipfile
    from xml.etree import ElementTree as ET

    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "word/document.xml" not in names:
                return ""
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, OSError, KeyError):
        return ""

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""

    lines = []
    # Each <w:p> is a paragraph; gather its <w:t> text, honoring tabs/breaks.
    for para in root.iter(W + "p"):
        buf = []
        for node in para.iter():
            tag = node.tag
            if tag == W + "t":
                buf.append(node.text or "")
            elif tag == W + "tab":
                buf.append("\t")
            elif tag in (W + "br", W + "cr"):
                buf.append("\n")
        line = "".join(buf).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def extract_text(upload_dir, stored_name):
    """Extract readable text from an uploaded file.
    - .docx  → parsed via Office Open XML (zip + document.xml)
    - others → best-effort UTF-8 decode (works for plain-text uploads)
    """
    path = os.path.join(upload_dir, stored_name)
    ext = os.path.splitext(stored_name)[1].lower()
    if ext == ".docx":
        text = _extract_docx(path)
        if text:
            return text
        # fall through to raw decode if the docx had no extractable text
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        return raw.decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _summarize(text, max_len=180):
    """Mock extractive summary: first meaningful sentence(s), trimmed."""
    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return "(no readable text)"
    # take up to 2 sentences
    parts = re.split(r"(?<=[.!?])\s+", clean)
    summary = " ".join(parts[:2])
    if len(summary) > max_len:
        summary = summary[: max_len - 1].rstrip() + "…"
    return summary


def analyze_document(docs):
    """Run the three mock-AI steps.

    docs: list of {"label": <doc name>, "text": <extracted text>} — one per
    attached file. Analyzing per file lets us report WHICH document each
    section was found in, and generate targeted recommendations for staff.
    """
    # Combined text for whole-document checks (e.g. version regex)
    full_text = "\n\n".join("[%s]\n%s" % (d["label"], d["text"]) for d in docs)
    lower_full = full_text.lower()

    # 1+2. For each required section, find which document(s) contain it + summary
    sections = {}
    section_report = []
    for sec in REQUIRED_SECTIONS:
        found_in = []          # document labels where this section appears
        summary = None
        for d in docs:
            dl = d["text"].lower()
            hit_kw = next((kw for kw in sec["keywords"] if kw.lower() in dl), None)
            if hit_kw:
                found_in.append(d["label"])
                if summary is None:
                    idx = dl.find(hit_kw.lower())
                    window = d["text"][max(0, idx - 20): idx + 220] if idx >= 0 else d["text"][:220]
                    summary = _summarize(window)
        present = bool(found_in)
        sections[sec["key"]] = {"present": present}
        section_report.append({
            "key": sec["key"],
            "label": sec["label"],
            "present": present,
            "summary": summary,
            "sourceDocuments": found_in,   # <-- where it was found
        })

    present_count = sum(1 for s in section_report if s["present"])
    completeness = round(100 * present_count / len(REQUIRED_SECTIONS))
    missing = [s["label"] for s in section_report if not s["present"]]

    # 3. Regulation compliance (uses combined text + detected sections)
    ctx = {"text": full_text, "sections": sections}
    rule_report = []
    for rule in REGULATION_RULES:
        passed, detail = rule["check"](ctx)
        rule_report.append({
            "id": rule["id"],
            "title": rule["title"],
            "severity": rule["severity"],
            "passed": passed,
            "detail": detail,
        })
    failed = [r for r in rule_report if not r["passed"]]
    critical_fail = any(r["severity"] == "critical" for r in failed)

    if critical_fail:
        verdict = "Non-compliant"
    elif failed:
        verdict = "Needs attention"
    else:
        verdict = "Compliant"

    overall_summary = _summarize(full_text, max_len=240)

    # 4. Recommendations for the reviewing officer
    recommendations = _build_recommendations(section_report, rule_report, docs, verdict)

    return {
        "completeness": completeness,
        "sectionsPresent": present_count,
        "sectionsTotal": len(REQUIRED_SECTIONS),
        "missingSections": missing,
        "sections": section_report,
        "regulation": {
            "verdict": verdict,
            "rules": rule_report,
            "failedCount": len(failed),
        },
        "recommendations": recommendations,
        "analyzedDocuments": [d["label"] for d in docs],
        "documentSummary": overall_summary,
        "engine": "mock-ai/heuristic-v2",
    }


def _build_recommendations(section_report, rule_report, docs, verdict):
    """Generate actionable notes for the officer based on gaps found."""
    recs = []

    missing = [s for s in section_report if not s["present"]]
    if missing:
        recs.append({
            "level": "action",
            "text": "ขอให้ผู้ยื่นเพิ่มหัวข้อที่ขาด: " +
                    ", ".join(s["label"] for s in missing) +
                    " (แนะนำตั้งสถานะ 'Needs revision')",
        })

    crit = [r for r in rule_report if (not r["passed"]) and r["severity"] == "critical"]
    if crit:
        recs.append({
            "level": "critical",
            "text": "พบประเด็นร้ายแรงด้านกฎระเบียบ: " +
                    "; ".join(r["title"] for r in crit) +
                    " — ควรทบทวนอย่างละเอียดก่อนอนุมัติ",
        })

    major = [r for r in rule_report if (not r["passed"]) and r["severity"] == "major"]
    if major:
        recs.append({
            "level": "warn",
            "text": "ข้อควรตรวจเพิ่ม (major): " + "; ".join(r["title"] for r in major),
        })

    # Note any attachments that yielded no readable text (e.g. scanned PDF)
    empty_docs = [d["label"] for d in docs if len((d["text"] or "").strip()) < 20]
    if empty_docs:
        recs.append({
            "level": "warn",
            "text": "อ่านข้อความไม่ได้/เนื้อหาน้อยจากไฟล์: " + ", ".join(empty_docs) +
                    " (อาจเป็น PDF สแกนหรือไฟล์ว่าง) — แนะนำตรวจไฟล์เหล่านี้ด้วยตนเอง",
        })

    if verdict == "Compliant" and not recs:
        recs.append({
            "level": "ok",
            "text": "เอกสารครบถ้วนและผ่านเกณฑ์เบื้องต้นทั้งหมด — พร้อมสำหรับการพิจารณาอนุมัติ",
        })

    return recs


# ----------------------------------------------------------------------------
# Event bus + worker
# ----------------------------------------------------------------------------
class EventBus:
    """Minimal thread-safe pub/sub backed by a queue."""
    def __init__(self):
        self._q = queue.Queue()
        self._subscribers = {}

    def emit(self, event_type, payload):
        self._q.put({"type": event_type, "payload": payload, "ts": _now()})

    def subscribe(self, event_type, handler):
        self._subscribers.setdefault(event_type, []).append(handler)

    def _dispatch(self, event):
        for handler in self._subscribers.get(event["type"], []):
            try:
                handler(event["payload"])
            except Exception as exc:  # noqa: BLE001
                print("[eventbus] handler error for %s: %s" % (event["type"], exc))

    def run_forever(self):
        while True:
            event = self._q.get()
            self._dispatch(event)
            self._q.task_done()


class AnalysisWorker:
    """Subscribes to submission.created and runs mock AI analysis."""
    def __init__(self, bus, db_path, upload_dir):
        self.bus = bus
        self.db_path = db_path
        self.upload_dir = upload_dir
        bus.subscribe("submission.created", self.on_submission_created)

    def on_submission_created(self, payload):
        sid = payload["id"]
        files = payload.get("files", [])  # list of {field, stored_name, label}
        try:
            set_analysis_status(self.db_path, sid, "processing", started_at=_now())
            # Extract text per attached document (keep the source label).
            docs = []
            for f in files:
                text = extract_text(self.upload_dir, f["stored_name"])
                docs.append({"label": f.get("label", f.get("field", "Document")), "text": text})
            result = analyze_document(docs)
            set_analysis_status(
                self.db_path, sid, "done",
                result_json=json.dumps(result, ensure_ascii=False),
                completed_at=_now(),
            )
            _audit(self.db_path, "ai_analysis_completed", sid, {
                "completeness": result["completeness"],
                "verdict": result["regulation"]["verdict"],
                "failedRules": result["regulation"]["failedCount"],
                "engine": result["engine"],
            })
            self.bus.emit("analysis.completed", {"id": sid, "result": result})
            print("[worker] analysis done for submission #%s (completeness %s%%)"
                  % (sid, result["completeness"]))
        except Exception as exc:  # noqa: BLE001
            set_analysis_status(self.db_path, sid, "error", error=str(exc), completed_at=_now())
            _audit(self.db_path, "ai_analysis_failed", sid, {"error": str(exc)})
            print("[worker] analysis error for #%s: %s" % (sid, exc))


def start_worker(db_path, upload_dir):
    """Initialize the bus + worker and start the background thread. Returns bus."""
    init_analysis_db(db_path)
    bus = EventBus()
    AnalysisWorker(bus, db_path, upload_dir)
    t = threading.Thread(target=bus.run_forever, name="analysis-worker", daemon=True)
    t.start()
    return bus
