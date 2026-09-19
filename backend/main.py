"""
SahayAI Backend
================
FastAPI service that runs locally on the CSC operator's PC. Every endpoint
here is designed to work with zero internet connectivity — sync to
government systems is a separate, best-effort background process
(see /sync/* endpoints), never a blocking dependency of the core workflow.

Multi-tenant: one PC, many operators. Every request that touches a
submission is tied to an authenticated operator session, and role
(operator vs supervisor) gates a small number of sensitive actions
(conflict resolution, viewing all operators' queues).
"""

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from services import ocr_service, fraud_detector, scheme_matcher, model_registry
import database as db

app = FastAPI(title="SahayAI", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_db()

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@app.get("/")
def serve_landing():
    return FileResponse(FRONTEND_DIR / "landing.html")


@app.get("/console")
def serve_console():
    return FileResponse(FRONTEND_DIR / "console.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def get_current_operator(authorization: str | None = Header(default=None)) -> dict:
    """Every protected endpoint depends on this. Session token is a bearer
    token issued at login; there is no implicit trust — a request with no
    token, an expired token, or a deactivated operator's token is rejected."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    operator = db.get_session_operator(token)
    if not operator:
        raise HTTPException(401, "Session expired or invalid — please sign in again")
    return operator


def require_supervisor(operator: dict = Depends(get_current_operator)) -> dict:
    if operator["role"] != "supervisor":
        raise HTTPException(403, "This action requires a supervisor account")
    return operator


# ---------------------------------------------------------------------------
# Health & model status
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "offline-capable", "audit_chain_valid": db.verify_audit_chain()}


@app.get("/api/models")
def get_models():
    """Exposes the active model manifest — which version of each on-device
    model produced the results the operator is looking at, and whether any
    model has tripped its circuit breaker after repeated failures."""
    return {"models": model_registry.get_manifest()}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.post("/api/auth/login")
def login(name: str = Form(...), pin: str = Form(...)):
    operator = db.authenticate(name, pin)
    if not operator:
        raise HTTPException(401, "Incorrect name or PIN")
    session = db.create_session(operator["id"])
    return {"token": session["token"], "expires_at": session["expires_at"], "operator": operator}


@app.get("/api/auth/me")
def me(operator: dict = Depends(get_current_operator)):
    return {"operator": operator}


@app.get("/api/operators")
def get_operators(_: dict = Depends(require_supervisor)):
    """Supervisor-only: see every operator account on this PC."""
    return {"operators": db.list_operators()}


@app.post("/api/operators")
def add_operator(name: str = Form(...), pin: str = Form(...), role: str = Form("operator"),
                  _: dict = Depends(require_supervisor)):
    """Supervisor-only: provision a new operator account on this PC."""
    try:
        operator = db.create_operator(name, pin, role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"operator": operator}


# ---------------------------------------------------------------------------
# Schemes
# ---------------------------------------------------------------------------

@app.get("/api/schemes")
def get_schemes():
    return {"schemes": scheme_matcher.list_all_schemes()}


@app.post("/api/schemes/match")
def match_scheme(query: str = Form(...), operator: dict = Depends(get_current_operator)):
    """Used by the voice copilot: takes transcribed operator speech (or
    typed query) and returns the best-matching government scheme."""
    return scheme_matcher.match_scheme(query)


# ---------------------------------------------------------------------------
# Consent (captured before any document is processed — DPDP-style)
# ---------------------------------------------------------------------------

DEFAULT_CONSENT_TEXT = (
    "The citizen has been informed their document will be processed on this "
    "device only, used solely to complete this application, and will not be "
    "shared except with the relevant government scheme, per applicable "
    "data protection law."
)


@app.post("/api/consent")
def give_consent(scheme_id: str = Form(None), operator: dict = Depends(get_current_operator)):
    """Must be called before /api/documents/process for a given citizen
    interaction. The consent_id it returns is stored against the resulting
    submission, so every processed document is traceable to an explicit,
    timestamped consent event — not implied consent."""
    result = db.log_consent(operator["id"], scheme_id, DEFAULT_CONSENT_TEXT)
    return {"consent_id": result["consent_id"], "given_at": result["given_at"], "consent_text": DEFAULT_CONSENT_TEXT}


# ---------------------------------------------------------------------------
# Document processing
# ---------------------------------------------------------------------------

@app.post("/api/documents/process")
async def process_document(
    file: UploadFile = File(...),
    scheme_id: str = Form(...),
    consent_id: str = Form(...),
    operator: dict = Depends(get_current_operator),
):
    """Core loop: scan a document -> OCR -> fraud check -> completeness
    check -> write to local offline queue with a hash-chained audit entry.

    Requires a valid consent_id from /api/consent — processing a citizen's
    document without a logged consent event is rejected outright, not just
    discouraged by convention.
    """
    consent = db.get_consent(consent_id)
    if not consent:
        raise HTTPException(400, "No matching consent record — call /api/consent before processing a document")

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(400, "Empty file")

    doc_hash = ocr_service.hash_document(image_bytes)
    dup = db.is_duplicate_document(doc_hash)
    if dup:
        return {
            "duplicate": True,
            "message": "This exact document was already processed.",
            "original_submission_id": dup["submission_id"],
            "first_seen_at": dup["first_seen_at"],
        }

    # Each service degrades gracefully on its own — a model failure never
    # raises up to this handler, it returns a manual_fallback result instead.
    ocr_result = ocr_service.extract_fields(image_bytes)
    fraud_result = fraud_detector.analyze(image_bytes)
    completeness = scheme_matcher.check_completeness(scheme_id, ocr_result["fields"])

    manual_fallback = ocr_result.get("manual_fallback", False) or fraud_result.get("manual_fallback", False)
    model_versions = {
        "ocr": ocr_result.get("model_version"),
        "fraud_detection": fraud_result.get("model_version"),
    }

    submission = db.create_submission(
        operator_id=operator["id"],
        scheme_id=scheme_id,
        extracted_fields=ocr_result["fields"],
        confidence=ocr_result["ocr_confidence"],
        fraud_flags=fraud_result["flags"],
        doc_hash=doc_hash,
        consent_id=consent_id,
        model_versions=model_versions,
        manual_fallback=manual_fallback,
    )

    return {
        "duplicate": False,
        "submission": submission,
        "ocr": {
            "fields": ocr_result["fields"],
            "confidence": ocr_result["ocr_confidence"],
            "raw_ocr_confidence": ocr_result.get("raw_ocr_confidence"),
            "calibration_provider": ocr_result.get("calibration_provider"),
            "field_count_found": ocr_result["field_count_found"],
            "manual_fallback": ocr_result.get("manual_fallback", False),
            "fallback_reason": ocr_result.get("fallback_reason"),
        },
        "fraud": fraud_result,
        "completeness": completeness,
        "model_versions": model_versions,
    }


@app.get("/api/submissions")
def get_submissions(sync_status: str | None = None, all_operators: bool = False,
                     operator: dict = Depends(get_current_operator)):
    """Operators see only their own queue by default. Supervisors can pass
    all_operators=true to see the whole PC's queue across every operator."""
    if all_operators and operator["role"] != "supervisor":
        raise HTTPException(403, "Only supervisors can view all operators' submissions")
    filter_operator = None if (all_operators and operator["role"] == "supervisor") else operator["id"]
    return {"submissions": db.list_submissions(sync_status=sync_status, operator_id=filter_operator)}


@app.get("/api/submissions/{submission_id}/audit")
def get_audit(submission_id: str, operator: dict = Depends(get_current_operator)):
    trail = db.get_audit_trail(submission_id)
    if not trail:
        raise HTTPException(404, "No audit trail for this submission")
    return {"submission_id": submission_id, "audit_trail": trail, "chain_valid": db.verify_audit_chain()}


# ---------------------------------------------------------------------------
# Sync: idempotent, retrying, conflict-aware
# ---------------------------------------------------------------------------

@app.post("/api/sync/run")
def run_sync(operator: dict = Depends(get_current_operator)):
    """Attempts to sync every queued, ready submission for this operator.
    Each attempt is idempotent and retried with exponential backoff on
    transient failure — see database.attempt_sync for the state machine."""
    queued = db.list_submissions(sync_status="queued", operator_id=operator["id"])
    results = {"synced": [], "retrying": [], "failed": [], "blocked": [], "conflict": []}

    for s in queued:
        if s["status"] != "ready_to_submit":
            results["blocked"].append(s["id"])
            continue
        outcome = db.attempt_sync(s["id"])
        status = outcome.get("status", "failed" if not outcome["ok"] else "synced")
        results.setdefault(status, []).append(s["id"])

    return {
        "results": results,
        "note": "Submissions needing manual review are held back from sync until an operator confirms them.",
    }


@app.post("/api/sync/retry/{submission_id}")
def retry_sync(submission_id: str, operator: dict = Depends(get_current_operator)):
    """Manually retry a single submission's sync immediately, rather than
    waiting for its backoff window — useful once the operator knows
    connectivity is back."""
    submission = db.get_submission(submission_id)
    if not submission or submission["operator_id"] != operator["id"]:
        raise HTTPException(404, "Submission not found")
    return db.attempt_sync(submission_id)


@app.post("/api/sync/{submission_id}/resolve-conflict")
def resolve_conflict(submission_id: str, resolution: str = Form(...),
                      _: dict = Depends(require_supervisor)):
    """Supervisor-only: a sync conflict (same document synced under two
    submissions) must be explicitly resolved, never auto-merged."""
    try:
        return db.resolve_conflict(submission_id, resolution)
    except ValueError as e:
        raise HTTPException(400, str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
