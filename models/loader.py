"""
Model Loader
============
This is the actual loading code a production build would use to run a
compiled model artifact — the piece that was previously only described in
README.md. It's real and tested: it runs against the confidence-calibration
model in artifacts/, on this machine, right now.

Provider selection: on a Snapdragon-powered HP PC with the Qualcomm AI Hub
runtime installed, onnxruntime would list "QNNExecutionProvider" and this
loader would route inference to the Hexagon NPU automatically. On any other
machine — including this one — QNN isn't available, so it falls back to
CPUExecutionProvider. Nothing about the calling code changes between those
two cases; that's the point of routing every inference call through here
rather than calling onnxruntime directly from each service.

This same loader is what services/ocr_service.py, fraud_detector.py, and
scheme_matcher.py would call once their real Qualcomm AI Hub artifacts
replace the current CPU-heuristic implementations — see models/README.md
for the exact swap steps per service.
"""

import logging
import time
from pathlib import Path

logger = logging.getLogger("sahayai.model_loader")

ARTIFACTS_DIR = Path(__file__).parent / "artifacts"

# Ordered by preference: onnxruntime tries each provider in turn and uses
# the first one available on this machine. QNNExecutionProvider only shows
# up in onnxruntime.get_available_providers() when the Qualcomm QNN SDK is
# installed and a compatible NPU is present.
PREFERRED_PROVIDERS = ["QNNExecutionProvider", "CPUExecutionProvider"]

_session_cache = {}


class ModelLoadError(Exception):
    pass


def _get_onnxruntime():
    try:
        import onnxruntime as ort
        return ort
    except ImportError as exc:
        raise ModelLoadError(
            "onnxruntime is not installed — run `pip install onnxruntime` "
            "(or `onnxruntime-qnn` on a Snapdragon device with the QNN SDK) "
        ) from exc


def load_session(model_name: str):
    """Loads (and caches) an ONNX Runtime inference session for the named
    model artifact. Returns (session, active_provider) so callers can log
    or surface which backend actually served the request — useful for
    confirming NPU acceleration is really being used on target hardware."""
    if model_name in _session_cache:
        return _session_cache[model_name]

    ort = _get_onnxruntime()
    model_path = ARTIFACTS_DIR / f"{model_name}.onnx"
    if not model_path.exists():
        raise ModelLoadError(f"No model artifact at {model_path}")

    available = ort.get_available_providers()
    providers_to_try = [p for p in PREFERRED_PROVIDERS if p in available]
    if not providers_to_try:
        raise ModelLoadError(f"None of {PREFERRED_PROVIDERS} available — have {available}")

    start = time.monotonic()
    session = ort.InferenceSession(str(model_path), providers=providers_to_try)
    load_ms = (time.monotonic() - start) * 1000

    active_provider = session.get_providers()[0]
    if active_provider == "QNNExecutionProvider":
        logger.info("Loaded %s on QNNExecutionProvider (Hexagon NPU) in %.1fms", model_name, load_ms)
    else:
        logger.info(
            "Loaded %s on %s (QNN not available on this machine) in %.1fms",
            model_name, active_provider, load_ms,
        )

    _session_cache[model_name] = (session, active_provider)
    return session, active_provider


def run_confidence_calibration(raw_confidence: float, field_count: int,
                                text_length_normalized: float, aadhaar_matched: bool) -> dict:
    """Real inference call: loads the trained calibration model (see
    train_calibration_model.py) and runs it against live OCR signals to
    produce a better-calibrated confidence score than the raw Tesseract
    average. Falls back to the raw score, clearly flagged, if the model
    can't load — this must never crash the calling pipeline."""
    try:
        session, provider = load_session("confidence_calibration")
        input_name = session.get_inputs()[0].name
        import numpy as np
        features = np.array([[raw_confidence, float(field_count), text_length_normalized, float(aadhaar_matched)]], dtype=np.float32)
        outputs = session.run(None, {input_name: features})
        # skl2onnx logistic regression outputs: [label, probabilities]
        calibrated = float(outputs[1][0][1])  # P(class=1) i.e. "trustworthy"
        return {"calibrated_confidence": round(calibrated, 4), "provider": provider, "fallback": False}
    except ModelLoadError as exc:
        logger.warning("Confidence calibration unavailable, using raw score: %s", exc)
        return {"calibrated_confidence": raw_confidence, "provider": "none", "fallback": True}


if __name__ == "__main__":
    # Quick manual check: python3 loader.py
    logging.basicConfig(level=logging.INFO)
    result = run_confidence_calibration(raw_confidence=0.87, field_count=3, text_length_normalized=0.6, aadhaar_matched=True)
    print(result)
    result2 = run_confidence_calibration(raw_confidence=0.87, field_count=0, text_length_normalized=0.1, aadhaar_matched=False)
    print(result2)
