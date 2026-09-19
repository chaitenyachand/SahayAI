"""
Fraud & Tamper Detection
========================
Demo build: classical image-forensics heuristics (error-level analysis proxy,
edge-consistency check, resolution/blur check). Fully on-device, no network.

PRODUCTION / SNAPDRAGON SWAP:
  Replace with a Qualcomm AI Hub image-forensics / tamper-classification
  model (e.g. a fine-tuned MobileNet or EfficientNet forgery detector,
  quantized and compiled for the Hexagon NPU). Keep the flag schema below
  the same so `database.py` and the frontend don't need to change.
"""

import cv2
import logging
import numpy as np
from PIL import Image
import io

from services import model_registry

logger = logging.getLogger("sahayai.fraud")

_TASK = "fraud_detection"
_consecutive_failures = 0
_FAILURE_THRESHOLD = 3


def _blur_score(gray: np.ndarray) -> float:
    """Variance of Laplacian — low value suggests a blurry/re-photographed
    (potentially screen-recaptured) document."""
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def _double_compression_proxy(image_bytes: bytes) -> float:
    """Recompresses the image at a known quality and measures pixel
    divergence — a cheap proxy for JPEG double-compression artifacts that
    often appear in tampered/re-saved documents. Not a substitute for a
    trained ELA model, but catches obvious re-saves in a demo."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    recompressed = np.array(Image.open(buf))
    original = np.array(img)
    if original.shape != recompressed.shape:
        return 0.0
    diff = np.abs(original.astype(int) - recompressed.astype(int))
    return float(diff.mean())


def _edge_consistency(gray: np.ndarray) -> float:
    """Checks whether edge density is unusually uniform/low — flat, uniform
    regions can indicate a pasted/cloned patch on the document."""
    edges = cv2.Canny(gray, 50, 150)
    return float(np.count_nonzero(edges)) / edges.size


def analyze(image_bytes: bytes) -> dict:
    """Graceful degradation: if the fraud model pipeline throws, this
    returns a 'needs_manual_review' result rather than crashing the request
    or — worse — silently reporting 'low risk' on a document it never
    actually analysed. A failed fraud check must never look identical to
    a passed one."""
    global _consecutive_failures

    if not model_registry.is_model_healthy(_TASK):
        return _manual_review_result("Fraud detection model marked unhealthy after repeated failures")

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        arr = np.array(img)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

        blur = _blur_score(gray)
        compression_delta = _double_compression_proxy(image_bytes)
        edge_density = _edge_consistency(gray)

        flags = []
        if blur < 40:
            flags.append({
                "code": "LOW_SHARPNESS",
                "severity": "medium",
                "message": "Image is unusually blurry — may be a re-photograph of a screen or printout.",
            })
        if compression_delta > 12:
            flags.append({
                "code": "COMPRESSION_ANOMALY",
                "severity": "medium",
                "message": "Compression artifacts suggest the image may have been edited and re-saved.",
            })
        if edge_density < 0.01:
            flags.append({
                "code": "LOW_DETAIL",
                "severity": "low",
                "message": "Unusually low detail/texture for an ID document — verify manually.",
            })

        _consecutive_failures = 0

        return {
            "flags": flags,
            "metrics": {
                "blur_score": round(blur, 2),
                "compression_delta": round(compression_delta, 2),
                "edge_density": round(edge_density, 4),
            },
            "risk_level": "high" if len(flags) >= 2 else ("medium" if flags else "low"),
            "model_version": model_registry.get_model_version(_TASK),
            "manual_fallback": False,
        }

    except Exception as exc:
        _consecutive_failures += 1
        logger.error("Fraud detection pipeline failure (%d consecutive): %s", _consecutive_failures, exc)
        if _consecutive_failures >= _FAILURE_THRESHOLD:
            model_registry.set_model_health(_TASK, False)
        return _manual_review_result(str(exc))


def _manual_review_result(reason: str) -> dict:
    return {
        "flags": [{
            "code": "MODEL_UNAVAILABLE",
            "severity": "high",
            "message": "Automated fraud check could not run — this document requires manual review.",
        }],
        "metrics": {},
        "risk_level": "high",
        "model_version": model_registry.get_model_version(_TASK),
        "manual_fallback": True,
        "fallback_reason": reason,
    }
