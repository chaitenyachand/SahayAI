"""
Trains and exports the confidence-calibration model.
=======================================================
Raw Tesseract word-confidence averages are poorly calibrated — they don't
reliably reflect the actual probability that extracted fields are correct.
This trains a tiny logistic regression that maps four cheap signals
(raw OCR confidence, fields found, text length, aadhaar-pattern match) onto
a better-calibrated confidence score, and exports it to ONNX so it can be
loaded by models/loader.py exactly the way a real Qualcomm AI Hub model
artifact would be.

This is a genuinely trained model — not a stub — but it's trained on
synthetic data reflecting plausible relationships between these signals,
since no labelled corpus of real Aadhaar-style scans exists here. Retrain
on real labelled extraction outcomes before relying on this in production.

Run: python3 train_calibration_model.py
Produces: artifacts/confidence_calibration.onnx
"""

import numpy as np
from sklearn.linear_model import LogisticRegression
from skl2onnx import to_onnx
from pathlib import Path

RNG = np.random.default_rng(42)
N = 4000

# Synthetic features, each row: [raw_ocr_confidence, field_count_found,
# text_length_normalized, aadhaar_pattern_matched]
raw_conf = RNG.beta(2.5, 1.5, N)                       # skewed toward higher values, like real OCR conf
field_count = RNG.integers(0, 5, N).astype(float)       # 0-4 fields found
text_len_norm = np.clip(RNG.normal(0.5, 0.2, N), 0, 1)  # normalized text length
aadhaar_match = RNG.binomial(1, 0.6, N).astype(float)   # pattern matched or not

X = np.column_stack([raw_conf, field_count, text_len_norm, aadhaar_match])

# Ground truth generation: a genuinely extraction is "trustworthy" (label=1)
# more often when raw confidence is high AND multiple fields were found AND
# the aadhaar pattern matched — reflecting real-world calibration intuition
# (low field-count extractions are untrustworthy even with high raw OCR
# confidence, since it usually means the layout parse failed, not that the
# text was clean).
logit = (
    4.5 * (raw_conf - 0.5)
    + 0.9 * (field_count - 2)
    + 1.2 * (text_len_norm - 0.5)
    + 1.5 * (aadhaar_match - 0.5)
    + RNG.normal(0, 0.6, N)  # label noise
)
y = (logit > 0).astype(int)

model = LogisticRegression()
model.fit(X, y)

train_acc = model.score(X, y)
print(f"Training accuracy on synthetic data: {train_acc:.3f}")
print(f"Coefficients: {model.coef_}, intercept: {model.intercept_}")

onnx_model = to_onnx(model, X[:1].astype(np.float32), target_opset=13)

out_path = Path(__file__).parent / "artifacts" / "confidence_calibration.onnx"
out_path.parent.mkdir(exist_ok=True)
with open(out_path, "wb") as f:
    f.write(onnx_model.SerializeToString())

print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")
