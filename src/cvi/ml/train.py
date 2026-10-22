"""Train + evaluate the predictive-maintenance model against a rule-based baseline and register a versioned artifact.

  python -m cvi.ml.train            -> ml/artifacts/<version>/{model.joblib, metrics.json} and ml/artifacts/LATEST

Evaluation protocol: stratified train/validation/test split (60/20/20, fixed seed); the baseline and the model are scored
on the SAME untouched test set. Metrics are computed, never hard-coded. Data is synthetic (see cvi.ml.synthetic)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split

from cvi.common.config import get_settings
from cvi.ml.features import FEATURES, HORIZON_DAYS
from cvi.ml.synthetic import generate


def baseline_scores(X: np.ndarray) -> np.ndarray:
    """What a fleet does today without ML: a service-interval rule on distance and time since service."""
    km, days = X[:, FEATURES.index("km_since_service")], X[:, FEATURES.index("days_since_service")]
    return np.maximum(km / 30_000.0, days / 365.0)


def _at_budget(y: np.ndarray, score: np.ndarray, budget: float = 0.10) -> dict:
    """Precision/recall if the workshop can only inspect the top `budget` share of vehicles."""
    k = max(1, int(len(y) * budget))
    pred = np.zeros(len(y), dtype=int)
    pred[np.argsort(-score)[:k]] = 1
    return {"precision": float(precision_score(y, pred, zero_division=0)), "recall": float(recall_score(y, pred, zero_division=0))}


def evaluate(y: np.ndarray, score: np.ndarray) -> dict:
    return {"roc_auc": float(roc_auc_score(y, score)), "pr_auc": float(average_precision_score(y, score)),
            "top10pct": _at_budget(y, score)}


def train(n: int = 60_000, seed: int = 11, out_dir: str | None = None) -> dict:
    X, y, _ = generate(n, seed)
    X_tr, X_tmp, y_tr, y_tmp = train_test_split(X, y, test_size=0.4, random_state=seed, stratify=y)
    X_val, X_te, y_val, y_te = train_test_split(X_tmp, y_tmp, test_size=0.5, random_state=seed, stratify=y_tmp)

    model = HistGradientBoostingClassifier(max_depth=4, learning_rate=0.08, max_iter=300, early_stopping=True,
                                           validation_fraction=0.15, random_state=seed)
    model.fit(X_tr, y_tr)
    p_val, p_te = model.predict_proba(X_val)[:, 1], model.predict_proba(X_te)[:, 1]

    metrics = {
        "data": {"kind": "SYNTHETIC (hidden-wear generator, see cvi/ml/synthetic.py)", "n": n, "positive_rate": float(y.mean()),
                 "split": {"train": len(y_tr), "val": len(y_val), "test": len(y_te)}},
        "horizon_days": HORIZON_DAYS,
        "baseline_rule_test": evaluate(y_te, baseline_scores(X_te)),
        "model_validation": evaluate(y_val, p_val),
        "model_test": evaluate(y_te, p_te),
        "model_test_brier": float(brier_score_loss(y_te, p_te)),
    }
    version = "pm-" + time.strftime("%Y%m%d") + "-" + hashlib.sha256(json.dumps({"n": n, "seed": seed, "f": FEATURES}).encode()).hexdigest()[:6]
    root = Path(out_dir or get_settings().model_dir)
    d = root / version
    d.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "features": FEATURES, "version": version, "medians": np.median(X_tr, axis=0).tolist()}, d / "model.joblib")
    (d / "metrics.json").write_text(json.dumps({"version": version, **metrics}, indent=2))
    (root / "LATEST").write_text(version)
    return {"version": version, **metrics}


if __name__ == "__main__":
    print(json.dumps(train(), indent=2))
