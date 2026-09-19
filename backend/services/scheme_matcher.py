"""
Scheme Matching & Form Completeness
====================================
Demo build: keyword/embedding-free matching against the local scheme
config — deliberately simple and fully on-device.

PRODUCTION / SNAPDRAGON SWAP:
  Replace `match_scheme()`'s scoring with a small on-device LLM (via
  Qualcomm AI Hub, e.g. a quantized Llama-3.2-1B or Phi-3-mini compiled
  for QNN) that takes the operator's transcribed speech and returns the
  best-matching scheme_id + confidence. The speech transcription itself
  should come from an AI Hub Whisper/Conformer ASR model rather than the
  browser's Web Speech API used in this demo's frontend.
"""

import json
from pathlib import Path

from services import model_registry

SCHEMES_PATH = Path(__file__).parent.parent.parent / "data" / "schemes.json"

with open(SCHEMES_PATH, encoding="utf-8") as f:
    SCHEMES = json.load(f)

_TASK = "scheme_matching"


def match_scheme(query_text: str) -> dict:
    query_lower = query_text.lower()
    scored = []
    for scheme_id, scheme in SCHEMES.items():
        score = sum(1 for kw in scheme["keywords"] if kw.lower() in query_lower)
        if score > 0:
            scored.append((scheme_id, score))
    scored.sort(key=lambda x: x[1], reverse=True)

    if not scored:
        return {"matched": False, "candidates": list_all_schemes(), "model_version": model_registry.get_model_version(_TASK)}

    best_id, best_score = scored[0]
    return {
        "matched": True,
        "scheme_id": best_id,
        "scheme": SCHEMES[best_id],
        "confidence": min(1.0, best_score / 3),
        "alternatives": [s for s, _ in scored[1:3]],
        "model_version": model_registry.get_model_version(_TASK),
    }


def list_all_schemes() -> list:
    return [{"scheme_id": k, "name": v["name"]} for k, v in SCHEMES.items()]


def check_completeness(scheme_id: str, extracted_fields: dict) -> dict:
    scheme = SCHEMES.get(scheme_id)
    if not scheme:
        return {"error": "unknown scheme_id"}
    required = scheme["required_fields"]
    present = [f for f in required if extracted_fields.get(f)]
    missing = [f for f in required if not extracted_fields.get(f)]
    return {
        "required_fields": required,
        "present_fields": present,
        "missing_fields": missing,
        "completeness_pct": round(len(present) / len(required) * 100, 1) if required else 100.0,
    }
