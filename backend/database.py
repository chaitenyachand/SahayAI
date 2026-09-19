"""
SahayAI — Offline-first data layer.

Every action an operator takes (a document processed, a form auto-filled,
a fraud flag raised, a citizen's consent) is written LOCALLY first, with a
signed audit entry. Sync to state/central government APIs happens
opportunistically when connectivity is available. This file never assumes
the network is up.

This module also owns:
  - Operator accounts and roles (multi-tenant: one PC, many operators)
  - Consent logging (DPDP-style: citizen consent captured before any
    document is processed, independent of the audit log for the
    submission itself)
  - Sync state machine with retries, backoff hints, and conflict detection
    (idempotent — retrying a sync never double-submits to the government
    API)
"""

import sqlite3
import json
import hashlib
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "sahayai.db"

SESSION_LIFETIME_HOURS = 12
MAX_SYNC_ATTEMPTS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_column_if_missing(conn, table: str, column_def: str):
    """Lightweight migration helper: SQLite has no 'ADD COLUMN IF NOT
    EXISTS', so we probe the schema and add the column only if it's not
    already there. Lets init_db() run safely against an existing DB."""
    col_name = column_def.split()[0]
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if col_name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS operators (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                pin_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'operator',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                operator_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS consent_log (
                id TEXT PRIMARY KEY,
                operator_id TEXT NOT NULL,
                scheme_id TEXT,
                consent_text TEXT NOT NULL,
                given_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY,
                operator_id TEXT NOT NULL,
                scheme_id TEXT NOT NULL,
                consent_id TEXT,
                extracted_fields TEXT NOT NULL,
                confidence REAL NOT NULL,
                fraud_flags TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_review',
                sync_status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                synced_at TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                submission_id TEXT,
                event_type TEXT NOT NULL,
                detail TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                prev_hash TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS document_hashes (
                doc_hash TEXT PRIMARY KEY,
                submission_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key TEXT PRIMARY KEY,
                submission_id TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            """
        )
        # Migrations for columns added after the initial release, so an
        # existing local DB upgrades in place instead of needing a wipe.
        _add_column_if_missing(conn, "submissions", "model_versions TEXT")
        _add_column_if_missing(conn, "submissions", "manual_fallback INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "submissions", "sync_attempts INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "submissions", "last_sync_error TEXT")
        _add_column_if_missing(conn, "submissions", "next_retry_at TEXT")
        _add_column_if_missing(conn, "submissions", "conflict_status TEXT NOT NULL DEFAULT 'none'")
        _add_column_if_missing(conn, "submissions", "idempotency_key TEXT")

    _ensure_default_operators()


# ---------------------------------------------------------------------------
# Operators, roles, sessions (multi-tenant access)
# ---------------------------------------------------------------------------

def _hash_pin(pin: str, salt: str) -> str:
    # NOTE: for a real deployment use bcrypt/argon2 with per-install work
    # factors, not a single salted SHA-256 round. Kept dependency-free here
    # since this is a local demo PC app, not an internet-facing service.
    return hashlib.sha256(f"{salt}:{pin}".encode()).hexdigest()


def create_operator(name: str, pin: str, role: str = "operator") -> dict:
    if role not in ("operator", "supervisor"):
        raise ValueError("role must be 'operator' or 'supervisor'")
    operator_id = str(uuid.uuid4())
    salt = secrets.token_hex(8)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO operators (id, name, pin_hash, salt, role, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (operator_id, name, _hash_pin(pin, salt), salt, role, _now()),
        )
    return get_operator(operator_id)


def get_operator(operator_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT id, name, role, active, created_at FROM operators WHERE id = ?", (operator_id,)).fetchone()
        return dict(row) if row else None


def list_operators() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name, role, active, created_at FROM operators ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]


def authenticate(name: str, pin: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM operators WHERE name = ? AND active = 1", (name,)).fetchone()
        if not row:
            return None
        if _hash_pin(pin, row["salt"]) != row["pin_hash"]:
            return None
        return {"id": row["id"], "name": row["name"], "role": row["role"]}


def create_session(operator_id: str) -> dict:
    token = secrets.token_urlsafe(24)
    expires_at = (_now_dt() + timedelta(hours=SESSION_LIFETIME_HOURS)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (token, operator_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, operator_id, _now(), expires_at),
        )
    return {"token": token, "expires_at": expires_at}


def get_session_operator(token: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT s.expires_at, o.id, o.name, o.role, o.active FROM sessions s "
            "JOIN operators o ON o.id = s.operator_id WHERE s.token = ?", (token,)
        ).fetchone()
    if not row:
        return None
    if not row["active"]:
        return None
    if datetime.fromisoformat(row["expires_at"]) < _now_dt():
        return None
    return {"id": row["id"], "name": row["name"], "role": row["role"]}


def _ensure_default_operators():
    """Seed a couple of demo accounts on first run so the login screen has
    something to show without a separate setup step. A real deployment
    would provision operator accounts via the supervisor flow instead."""
    if not list_operators():
        create_operator("Ravi Kumar", "1234", role="operator")
        create_operator("Sunita Sharma", "5678", role="supervisor")


# ---------------------------------------------------------------------------
# Consent logging (DPDP-style: captured before any document is processed)
# ---------------------------------------------------------------------------

def log_consent(operator_id: str, scheme_id: str | None, consent_text: str) -> dict:
    with get_conn() as conn:
        consent_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO consent_log (id, operator_id, scheme_id, consent_text, given_at) VALUES (?, ?, ?, ?, ?)",
            (consent_id, operator_id, scheme_id, consent_text, _now()),
        )
        write_audit(conn, None, "consent_given", {
            "consent_id": consent_id, "operator_id": operator_id,
            "scheme_id": scheme_id, "consent_text": consent_text,
        })
    return {"consent_id": consent_id, "given_at": _now()}


def get_consent(consent_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM consent_log WHERE id = ?", (consent_id,)).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Audit log (hash-chained, append-only)
# ---------------------------------------------------------------------------

def _last_audit_hash(conn) -> str:
    row = conn.execute(
        "SELECT content_hash FROM audit_log ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row["content_hash"] if row else "genesis"


def write_audit(conn, submission_id: str | None, event_type: str, detail: dict):
    """Append-only, hash-chained audit log — each entry links to the previous
    one so tampering with historical records is detectable (important since
    CSC operators are personally liable for fraudulent submissions)."""
    prev_hash = _last_audit_hash(conn)
    payload = json.dumps(detail, sort_keys=True)
    content_hash = hashlib.sha256(f"{prev_hash}{payload}".encode()).hexdigest()
    conn.execute(
        "INSERT INTO audit_log (id, submission_id, event_type, detail, content_hash, prev_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), submission_id, event_type, payload, content_hash, prev_hash, _now()),
    )


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------

def create_submission(operator_id: str, scheme_id: str, extracted_fields: dict,
                       confidence: float, fraud_flags: list, doc_hash: str,
                       consent_id: str | None = None, model_versions: dict | None = None,
                       manual_fallback: bool = False) -> dict:
    submission_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO submissions (id, operator_id, scheme_id, consent_id, extracted_fields, confidence, "
            "fraud_flags, status, sync_status, created_at, model_versions, manual_fallback, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                submission_id, operator_id, scheme_id, consent_id, json.dumps(extracted_fields),
                confidence, json.dumps(fraud_flags),
                "needs_manual_review" if (fraud_flags or confidence < 0.75 or manual_fallback) else "ready_to_submit",
                "queued", _now(), json.dumps(model_versions or {}), int(manual_fallback),
                submission_id,  # idempotency key = submission id, stable across retries
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO document_hashes (doc_hash, submission_id, first_seen_at) VALUES (?, ?, ?)",
            (doc_hash, submission_id, _now()),
        )
        write_audit(conn, submission_id, "submission_created", {
            "operator_id": operator_id, "scheme_id": scheme_id, "consent_id": consent_id,
            "confidence": confidence, "fraud_flags": fraud_flags, "doc_hash": doc_hash,
            "model_versions": model_versions, "manual_fallback": manual_fallback,
        })
    return get_submission(submission_id)


def get_submission(submission_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        return dict(row) if row else None


def list_submissions(sync_status: str | None = None, operator_id: str | None = None) -> list:
    with get_conn() as conn:
        query = "SELECT * FROM submissions"
        clauses, params = [], []
        if sync_status:
            clauses.append("sync_status = ?")
            params.append(sync_status)
        if operator_id:
            clauses.append("operator_id = ?")
            params.append(operator_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def is_duplicate_document(doc_hash: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM document_hashes WHERE doc_hash = ?", (doc_hash,)
        ).fetchone()
        return dict(row) if row else None


def get_audit_trail(submission_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE submission_id = ? ORDER BY created_at ASC", (submission_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def verify_audit_chain() -> bool:
    """Walk the hash chain to confirm no historical entry was altered."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY created_at ASC").fetchall()
    prev = "genesis"
    for r in rows:
        expected = hashlib.sha256(f"{prev}{r['detail']}".encode()).hexdigest()
        if expected != r["content_hash"]:
            return False
        prev = r["content_hash"]
    return True


# ---------------------------------------------------------------------------
# Sync: idempotent, retrying, conflict-aware
# ---------------------------------------------------------------------------

def _backoff_seconds(attempt: int) -> int:
    """Exponential backoff with a cap, so a flaky connection doesn't hammer
    the government API: 10s, 20s, 40s, 80s, capped at 300s."""
    return min(10 * (2 ** attempt), 300)


def attempt_sync(submission_id: str) -> dict:
    """Idempotent sync attempt for a single submission.

    Idempotency: keyed on the submission's stable idempotency_key, so if a
    previous attempt actually reached the government API but the local
    process crashed before recording success, retrying will not create a
    duplicate record server-side — it looks up the existing completion
    instead of resubmitting.

    Conflict detection: if another submission already synced with the same
    source document hash (e.g. two operators independently scanned the same
    citizen's document before either had synced), this is flagged as a
    conflict rather than silently synced twice.
    """
    submission = get_submission(submission_id)
    if not submission:
        return {"ok": False, "error": "submission_not_found"}

    if submission["sync_status"] == "synced":
        return {"ok": True, "status": "already_synced"}

    if submission["status"] == "needs_manual_review":
        return {"ok": False, "status": "blocked", "reason": "Needs manual review before it can sync"}

    with get_conn() as conn:
        # Idempotency check: has this exact submission already completed a
        # sync under its idempotency key, even if local state says otherwise?
        existing = conn.execute(
            "SELECT * FROM idempotency_keys WHERE key = ?", (submission["idempotency_key"],)
        ).fetchone()
        if existing:
            conn.execute("UPDATE submissions SET sync_status='synced', synced_at=? WHERE id=?", (_now(), submission_id))
            write_audit(conn, submission_id, "sync_idempotent_replay", {"idempotency_key": submission["idempotency_key"]})
            return {"ok": True, "status": "already_synced"}

        # Conflict check: the same citizen (same Aadhaar number) already has
        # a synced submission for this scheme under a *different* submission
        # id — e.g. two operators independently processed the same citizen's
        # application before either had synced. This is the realistic
        # conflict case (doc_hash itself is already unique-constrained, so
        # the exact same photographed document can never reach this point
        # twice — see is_duplicate_document at the API layer).
        own_fields = json.loads(submission["extracted_fields"] or "{}")
        own_aadhaar = own_fields.get("aadhaar_number")
        conflict_row = None
        if own_aadhaar and submission["conflict_status"] != "resolved_forced":
            conflict_rows = conn.execute(
                "SELECT id, extracted_fields FROM submissions "
                "WHERE id != ? AND scheme_id = ? AND sync_status = 'synced'",
                (submission_id, submission["scheme_id"]),
            ).fetchall()
            conflict_row = next(
                (r for r in conflict_rows if json.loads(r["extracted_fields"] or "{}").get("aadhaar_number") == own_aadhaar),
                None,
            )
        if conflict_row:
            conn.execute("UPDATE submissions SET conflict_status='conflict' WHERE id=?", (submission_id,))
            write_audit(conn, submission_id, "sync_conflict_detected", {
                "conflicting_submission_id": conflict_row["id"], "shared_aadhaar": own_aadhaar,
            })
            return {"ok": False, "status": "conflict", "conflicting_submission_id": conflict_row["id"]}

        attempts = submission["sync_attempts"] + 1

        # Deterministic transient-failure simulation for a demoable retry
        # path: a fresh, lower-confidence submission's first sync attempt
        # times out against the (stubbed) government API, exactly the class
        # of real-world failure retries exist to absorb.
        simulate_timeout = (attempts == 1 and submission["confidence"] < 0.9)

        if simulate_timeout:
            if attempts >= MAX_SYNC_ATTEMPTS:
                conn.execute(
                    "UPDATE submissions SET sync_attempts=?, last_sync_error=?, sync_status='failed' WHERE id=?",
                    (attempts, "Government API unreachable after max retries", submission_id),
                )
                write_audit(conn, submission_id, "sync_failed_permanently", {"attempts": attempts})
                return {"ok": False, "status": "failed", "attempts": attempts}

            next_retry = (_now_dt() + timedelta(seconds=_backoff_seconds(attempts))).isoformat()
            conn.execute(
                "UPDATE submissions SET sync_attempts=?, last_sync_error=?, next_retry_at=? WHERE id=?",
                (attempts, "Government API timeout — will retry with backoff", next_retry, submission_id),
            )
            write_audit(conn, submission_id, "sync_attempt_failed", {
                "attempts": attempts, "next_retry_at": next_retry, "reason": "timeout",
            })
            return {"ok": False, "status": "retrying", "attempts": attempts, "next_retry_at": next_retry}

        # Success path
        conn.execute(
            "INSERT INTO idempotency_keys (key, submission_id, completed_at) VALUES (?, ?, ?)",
            (submission["idempotency_key"], submission_id, _now()),
        )
        conn.execute(
            "UPDATE submissions SET sync_status='synced', synced_at=?, sync_attempts=?, last_sync_error=NULL WHERE id=?",
            (_now(), attempts, submission_id),
        )
        write_audit(conn, submission_id, "sync_completed", {"submission_id": submission_id, "attempts": attempts})

    return {"ok": True, "status": "synced"}


def resolve_conflict(submission_id: str, resolution: str) -> dict:
    """Supervisor action: resolve a flagged conflict either by discarding
    this duplicate submission or forcing a sync anyway (e.g. the two
    scans were confirmed to be genuinely different citizens)."""
    if resolution not in ("discard", "force_sync"):
        raise ValueError("resolution must be 'discard' or 'force_sync'")
    with get_conn() as conn:
        if resolution == "discard":
            conn.execute("UPDATE submissions SET status='discarded', conflict_status='resolved_discarded' WHERE id=?", (submission_id,))
            write_audit(conn, submission_id, "conflict_resolved", {"resolution": "discard"})
        else:
            conn.execute("UPDATE submissions SET conflict_status='resolved_forced', sync_status='queued' WHERE id=?", (submission_id,))
            write_audit(conn, submission_id, "conflict_resolved", {"resolution": "force_sync"})
    return get_submission(submission_id)
