"""Inference: load the latest versioned artifact, score vehicles, explain with local sensitivity analysis."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np

from cvi.common.config import get_settings
from cvi.ml.features import FEATURE_LABELS, FEATURES, to_matrix


class MaintenanceModel:
    def __init__(self, bundle: dict):
        self.model = bundle["model"]
        self.version: str = bundle["version"]
        self.medians = np.array(bundle["medians"])

    def predict(self, rows: list[dict]) -> np.ndarray:
        return self.model.predict_proba(to_matrix(rows))[:, 1] if rows else np.array([])

    def explain(self, row: dict, top: int = 3) -> list[dict]:
        """Local sensitivity: replace each feature by the training median and measure the drop in failure probability.
        Features whose replacement lowers the risk the most are the factors pushing this vehicle's risk up."""
        x = to_matrix([row])[0]
        base = self.model.predict_proba(x.reshape(1, -1))[0, 1]
        variants = np.tile(x, (len(FEATURES), 1))
        for i in range(len(FEATURES)):
            variants[i, i] = self.medians[i]
        drops = base - self.model.predict_proba(variants)[:, 1]
        order = np.argsort(-drops)[:top]
        return [{"feature": FEATURES[i], "label": FEATURE_LABELS[FEATURES[i]], "value": round(float(x[i]), 2),
                 "risk_contribution": round(float(drops[i]), 4)} for i in order if drops[i] > 0.002]


@lru_cache
def load_latest(model_dir: str | None = None) -> MaintenanceModel | None:
    root = Path(model_dir or get_settings().model_dir)
    latest = root / "LATEST"
    if not latest.exists():
        return None
    return MaintenanceModel(joblib.load(root / latest.read_text().strip() / "model.joblib"))
