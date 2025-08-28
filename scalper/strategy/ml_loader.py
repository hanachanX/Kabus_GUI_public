# scalper/strategy/ml_loader.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, glob
import numpy as np
import pandas as pd
from joblib import load

def load_latest_model(models_dir: str) -> tuple:
    """最新の joblib を探して (predict_proba_fn, feature_names) を返す"""
    files = sorted(glob.glob(os.path.join(models_dir, "*.joblib")))
    if not files:
        raise FileNotFoundError("no model files")
    latest = files[-1]
    obj = load(latest)
    model = obj["model"]
    features = obj["features"]

    def proba_fn(feats: dict) -> float:
        x = pd.DataFrame([{c: feats.get(c, 0.0) for c in features}], dtype=float)
        p = model.predict_proba(x.values)[0, 1]
        return float(p)

    return proba_fn, features, latest
