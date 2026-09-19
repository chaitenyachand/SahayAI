"""
Document Intelligence Service
==============================
Demo build: OpenCV preprocessing + Tesseract OCR, runs fully on-device (CPU),
no network calls — this already satisfies the "offline" requirement.

PRODUCTION / SNAPDRAGON SWAP:
  Replace `_run_ocr()` with a Qualcomm AI Hub-optimized document OCR model
  (e.g. a PaddleOCR / TrOCR variant exported to .tflite/.onnx and compiled
  via `qai_hub` for the Hexagon NPU). The rest of this module — the field
  extraction regexes and confidence scoring — is model-agnostic and does
  not need to change. See /models/README.md for the exact swap steps.
"""

import re
import hashlib
import logging
import sys
from pathlib import Path
import cv2
import numpy as np
import pytesseract
from PIL import Image
import io

from services import model_registry

logger = logging.getLogger("sahayai.ocr")

# The confidence-calibration model lives in /models (project root), a
# sibling of /backend — add it to the path once, at import time, rather
# than duplicating loader logic in every service that wants it.
_MODELS_DIR = Path(__file__).resolve().parent.parent.parent / "models"
if str(_MODELS_DIR) not in sys.path:
    sys.path.insert(0, str(_MODELS_DIR))
try:
    from loader import run_confidence_calibration
    _CALIBRATION_AVAILABLE = True
except ImportError:
    _CALIBRATION_AVAILABLE = False
    logger.warning("Confidence calibration model unavailable — using raw OCR confidence only.")

AADHAAR_RE = re.compile(r"\b(\d{4}\s?\d{4}\s?\d{4})\b")
DOB_RE = re.compile(r"\b(\d{2}[/-]\d{2}[/-]\d{4})\b")
PHONE_RE = re.compile(r"\b([6-9]\d{9})\b")

_TASK = "ocr"
_consecutive_failures = 0
_FAILURE_THRESHOLD = 3  # trips the circuit breaker after this many failures in a row


def hash_document(image_bytes: bytes) -> str:
    """Perceptual-ish hash for duplicate-submission detection (exact-bytes
    hash here; production should use pHash so re-scans/re-compressions of
    the same physical document still match)."""
    return hashlib.sha256(image_bytes).hexdigest()


def _preprocess(image_bytes: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    # Adaptive threshold copes with uneven lighting from a webcam/phone photo
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
    )
    denoised = cv2.medianBlur(thresh, 3)
    return denoised


def _run_ocr(processed_img: np.ndarray) -> dict:
    data = pytesseract.image_to_data(
        processed_img, output_type=pytesseract.Output.DICT,
        config="--psm 6",
    )
    words, confs = [], []
    for text, conf in zip(data["text"], data["conf"]):
        text = text.strip()
        if text and str(conf) != "-1":
            words.append(text)
            try:
                confs.append(float(conf))
            except ValueError:
                pass
    full_text = " ".join(words)
    avg_conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    return {"text": full_text, "confidence": avg_conf}


def extract_fields(image_bytes: bytes) -> dict:
    """Returns extracted structured fields + an overall confidence score.
    Low confidence routes the submission to manual review rather than
    silently auto-filling a government form with a guess.

    Graceful degradation: if the OCR pipeline itself fails (corrupt image,
    unexpected format, model exception) this never raises up to the API
    layer. It returns a manual_fallback result instead, and the operator
    types the fields in by hand rather than the UI silently showing
    nothing or crashing. Three consecutive failures trip a circuit
    breaker that marks the model unhealthy so the frontend can warn
    operators proactively instead of failing scan after scan."""
    global _consecutive_failures

    if not model_registry.is_model_healthy(_TASK):
        return _manual_fallback_result(image_bytes, reason="OCR model marked unhealthy after repeated failures")

    try:
        processed = _preprocess(image_bytes)
        ocr_result = _run_ocr(processed)
        text = ocr_result["text"]

        fields = {}

        # DOB extracted first, and its matched span excluded from the text before
        # Aadhaar-number search — otherwise a 4-digit year immediately adjacent to
        # a 12-digit number in the OCR stream can get pulled into a false match
        # (e.g. "...1985 4521 8890 3312..." matching "1985" + "4521" + "8890").
        dob_match = DOB_RE.search(text)
        search_text = text
        if dob_match:
            fields["dob"] = dob_match.group(1)
            start, end = dob_match.span()
            search_text = text[:start] + " " * (end - start) + text[end:]

        aadhaar_match = AADHAAR_RE.search(search_text)
        if aadhaar_match:
            fields["aadhaar_number"] = aadhaar_match.group(1).replace(" ", "")

        phone_match = PHONE_RE.search(text)
        if phone_match:
            fields["phone"] = phone_match.group(1)

        # Name heuristic: longest line of capitalised alphabetic words, common
        # on ID cards. Production model would use a proper NER model instead.
        name_candidate = ""
        for line in text.split("\n"):
            words_in_line = [w for w in line.split() if w.isalpha()]
            if len(words_in_line) >= 2 and len(" ".join(words_in_line)) > len(name_candidate):
                name_candidate = " ".join(words_in_line)
        if name_candidate:
            fields["full_name"] = name_candidate.title()

        _consecutive_failures = 0  # reset breaker on success

        # Real inference call: calibrate the raw Tesseract confidence using
        # the trained logistic-regression model in /models/artifacts. Falls
        # back to the raw score (clearly flagged) if the model can't load —
        # this must never block a scan that would otherwise have succeeded.
        raw_conf = ocr_result["confidence"]
        calibration = {"calibrated_confidence": raw_conf, "provider": "none", "fallback": True}
        if _CALIBRATION_AVAILABLE:
            calibration = run_confidence_calibration(
                raw_confidence=raw_conf,
                field_count=len(fields),
                text_length_normalized=min(len(text) / 500, 1.0),
                aadhaar_matched=bool(aadhaar_match),
            )

        return {
            "raw_text": text,
            "fields": fields,
            "field_count_found": len(fields),
            "ocr_confidence": round(calibration["calibrated_confidence"], 3),
            "raw_ocr_confidence": round(raw_conf, 3),
            "calibration_provider": calibration["provider"],
            "doc_hash": hash_document(image_bytes),
            "model_version": model_registry.get_model_version(_TASK),
            "manual_fallback": False,
        }

    except Exception as exc:
        _consecutive_failures += 1
        logger.error("OCR pipeline failure (%d consecutive): %s", _consecutive_failures, exc)
        if _consecutive_failures >= _FAILURE_THRESHOLD:
            model_registry.set_model_health(_TASK, False)
        return _manual_fallback_result(image_bytes, reason=str(exc))


def _manual_fallback_result(image_bytes: bytes, reason: str) -> dict:
    try:
        doc_hash = hash_document(image_bytes)
    except Exception:
        doc_hash = hashlib.sha256(b"unhashable").hexdigest()
    return {
        "raw_text": "",
        "fields": {},
        "field_count_found": 0,
        "ocr_confidence": 0.0,
        "doc_hash": doc_hash,
        "model_version": model_registry.get_model_version(_TASK),
        "manual_fallback": True,
        "fallback_reason": reason,
    }
