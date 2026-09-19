"""
Model Registry
==============
Every inference call in SahayAI goes through a versioned model entry rather
than a hardcoded implementation. This means:
  - Every submission can record *exactly* which model versions produced it
    (required for audit, dispute resolution, and reproducing a decision).
  - Swapping the CPU demo backend for a Qualcomm AI Hub / QNN-compiled model
    is a manifest change, not a rewrite of calling code.
  - A model can be marked unavailable without deleting code — callers check
    `is_healthy` and fall back to manual entry rather than guessing.

In production this manifest would be loaded from a signed config file pushed
alongside model artifacts, with checksums verified before load.
"""

from datetime import datetime, timezone
import threading

_lock = threading.Lock()

MODEL_MANIFEST = {
    "ocr": {
        "version": "ocr-tesseract-cpu-0.3.1",
        "backend": "cpu-demo",
        "production_target": "qai-hub/paddleocr-det-rec-int8 (QNN, Hexagon NPU)",
        "is_healthy": True,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    },
    "fraud_detection": {
        "version": "fraud-heuristics-cv2-0.2.0",
        "backend": "cpu-demo",
        "production_target": "qai-hub/forgery-classifier-mobilenetv3-int8 (QNN, Hexagon NPU)",
        "is_healthy": True,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    },
    "scheme_matching": {
        "version": "scheme-keyword-match-0.1.4",
        "backend": "cpu-demo",
        "production_target": "qai-hub/llama-3.2-1b-instruct-int4 (QNN, Hexagon NPU)",
        "is_healthy": True,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    },
    "speech_to_text": {
        "version": "browser-webspeech-0.1.0",
        "backend": "browser-demo",
        "production_target": "qai-hub/whisper-tiny-en-int8 (QNN, Hexagon NPU)",
        "is_healthy": True,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    },
    "confidence_calibration": {
        "version": "confidence-calibration-logreg-onnx-1.0.0",
        "backend": "onnxruntime-cpu",
        "production_target": "same ONNX artifact, loaded via onnxruntime-qnn on Hexagon NPU — no retraining needed, only the execution provider changes",
        "is_healthy": True,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    },
}


def get_model_version(task: str) -> str:
    entry = MODEL_MANIFEST.get(task)
    return entry["version"] if entry else "unknown"


def is_model_healthy(task: str) -> bool:
    entry = MODEL_MANIFEST.get(task)
    return bool(entry and entry["is_healthy"])


def set_model_health(task: str, healthy: bool):
    """Circuit-breaker style toggle: a service can mark its own model
    unhealthy after repeated failures so callers degrade gracefully
    instead of retrying a broken pipeline indefinitely."""
    with _lock:
        if task in MODEL_MANIFEST:
            MODEL_MANIFEST[task]["is_healthy"] = healthy


def get_manifest() -> dict:
    return MODEL_MANIFEST
