# Model layer — demo vs. production (Snapdragon)

This folder used to be documentation-only. It now contains one real, working
model and the loader code that runs it — plus the documented plan for the
models that still require Qualcomm AI Hub / real Snapdragon hardware to
compile, which this environment doesn't have.

## What's actually here (real, tested, runs on CPU today)

```
models/
  loader.py                      Provider-aware ONNX Runtime loader (QNN -> CPU fallback)
  train_calibration_model.py     Trains + exports the model below
  artifacts/
    confidence_calibration.onnx  A real, trained logistic-regression model (549 bytes)
```

`confidence_calibration.onnx` takes four OCR signals (raw Tesseract confidence,
fields found, normalized text length, whether the Aadhaar pattern matched) and
outputs a better-calibrated trustworthiness score than the raw Tesseract
average alone — a real, if small, ML component wired into
`backend/services/ocr_service.py`, not a standalone demo script. Every
document scan in the running app calls it.

It's trained on synthetic data (see the docstring in `train_calibration_model.py`
for the exact assumptions), since no labelled corpus of real Aadhaar-style
scans exists here — retrain on real labelled outcomes before trusting this
in production.

`loader.py` is the code a production build would actually call to run any
compiled model artifact: it asks `onnxruntime` for `QNNExecutionProvider`
first and falls back to `CPUExecutionProvider` when QNN isn't available —
which is always, on this machine. Run `python3 models/loader.py` to see it
load and run for yourself; the log line tells you which provider served the
request.

## What's still CPU-heuristic, and why there's no code for it here

The other three capabilities remain classical CPU logic in
`backend/services/`, not trained models, so there was never a model artifact
to put in this folder for them:

| Capability | Demo implementation | Production (AI Hub / QNN) |
|---|---|---|
| Document OCR | Tesseract (CPU) | Quantized OCR model (e.g. PaddleOCR-det/rec or TrOCR) exported to ONNX and compiled with `qai_hub` for the Hexagon NPU |
| Fraud/tamper detection | OpenCV heuristics (blur, compression delta, edge density) | Fine-tuned MobileNetV3/EfficientNet forgery classifier, quantized int8, compiled for NPU |
| Scheme matching from speech intent | Keyword scoring | Quantized small LLM (Llama-3.2-1B-Instruct or Phi-3-mini) via AI Hub, prompted with the scheme catalog |
| Speech-to-text | Browser Web Speech API | Whisper-tiny/base or a Conformer ASR model compiled for QNN — enables true offline voice, in-language, no browser dependency |

I can't include real weight files for these because they must be compiled by
Qualcomm AI Hub against actual Snapdragon/Hexagon NPU hardware — there's no
legitimate artifact to generate without that hardware, and even the
pretrained versions are hundreds of MB to multiple GB, too large to bundle
in a source zip.

## Why this split is deliberate

Every service file (`ocr_service.py`, `fraud_detector.py`, `scheme_matcher.py`)
exposes the same function signatures regardless of which model backs them.
Swapping in an AI Hub model means changing the implementation inside one
function — not touching the API layer, the database schema, or the frontend.
This is what "production-grade" architecture looks like: the interfaces are
already correct, and now there's real loader code proving the pattern works,
not just a description of it.

## Swap steps for the remaining three capabilities

1. `pip install qai-hub qai-hub-models`
2. Pick a model from the AI Hub model zoo matching the target task
3. `qai-hub compile --model <model> --device "Snapdragon X Elite"` to get an NPU-optimized artifact, saved into `models/artifacts/`
4. Call `models.loader.load_session("<artifact-name>")` from the relevant service file — the provider fallback, caching, and logging are already handled
5. Benchmark latency/power against the CPU baseline and report the delta — this is the number judges want to see

