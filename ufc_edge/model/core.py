"""Model components.

How the probability is produced
-------------------------------
1. **Stats model.** A gradient-boosted classifier on the point-in-time
   feature differences (A minus B). It never sees bookmaker odds.
2. **Market blend.** A logistic regression on ``[logit(market), logit(stats)]``,
   fitted only on *out-of-sample* stats predictions from the walk-forward
   evaluation. It learns from data how much to trust the stats model relative
   to the market (historically about 0.2 against 1.0). This replaces a fixed
   50/50 guess.
3. **Explainer.** A regularised logistic regression on the same features. It's
   not used for the probability; it gives each fight's "strongest factors" in
   a form that can be read directly.
4. **Method model.** A six-way classifier (A or B by KO/TKO, submission or
   decision), reported but not bet.

Symmetry
--------
Fighter A versus B is an arbitrary label. Every model is trained on each fight
twice, once from each side (features negated, label flipped), and predictions
average both views. So p(A beats B) = 1 - p(B beats A) exactly.
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ..features.build import NUMERIC_FEATURES

DIFF_FEATURES = [f"diff_{f}" for f in NUMERIC_FEATURES]
# Experience matters in absolute terms too: two debutants are a coin flip in a different way.
PAIR_FEATURES = [("a_n_fights", "b_n_fights"), ("a_minutes_with_stats", "b_minutes_with_stats")]
MODEL_FEATURES = DIFF_FEATURES + [c for pair in PAIR_FEATURES for c in pair]

METHOD_CLASSES = ["A_KO_TKO", "A_SUB", "A_DEC", "B_KO_TKO", "B_SUB", "B_DEC"]
_FLIP_CLASS = {"A_KO_TKO": "B_KO_TKO", "A_SUB": "B_SUB", "A_DEC": "B_DEC",
               "B_KO_TKO": "A_KO_TKO", "B_SUB": "A_SUB", "B_DEC": "A_DEC"}


def feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    return df[MODEL_FEATURES].astype(float)


def flip(X: pd.DataFrame) -> pd.DataFrame:
    """The same fights seen from the other corner."""
    Y = X.copy()
    for c in DIFF_FEATURES:
        Y[c] = -X[c]
    for a, b in PAIR_FEATURES:
        Y[a], Y[b] = X[b].to_numpy(), X[a].to_numpy()
    return Y


def usable_columns(X: pd.DataFrame) -> list[str]:
    """Drop features with no values at all in the training window (the tree binner can't handle them)."""
    return [c for c in X.columns if X[c].notna().any()]


def logit(p: np.ndarray | pd.Series) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, dtype=float)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


class StatsModel:
    """Gradient boosting on feature differences. Odds are never an input."""

    def __init__(self) -> None:
        self.model = HistGradientBoostingClassifier(
            max_iter=300, learning_rate=0.03, max_depth=3, min_samples_leaf=80,
            l2_regularization=1.0, early_stopping=False, random_state=0,
        )

    def fit(self, df: pd.DataFrame) -> "StatsModel":
        X, y = feature_matrix(df), df["a_wins"].to_numpy(dtype=float)
        self.columns = usable_columns(X)
        both = pd.concat([X, flip(X)], ignore_index=True)[self.columns]
        self.model.fit(both, np.r_[y, 1 - y])
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        X = feature_matrix(df)
        p = self.model.predict_proba(X[self.columns])[:, 1]
        pf = self.model.predict_proba(flip(X)[self.columns])[:, 1]
        return 0.5 * (p + 1 - pf)


class Explainer:
    """Readable per-fight factors from a standardised logistic regression."""

    def __init__(self) -> None:
        self.pipe = make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(),
                                  LogisticRegression(C=0.05, max_iter=3000))

    def fit(self, df: pd.DataFrame) -> "Explainer":
        X, y = feature_matrix(df), df["a_wins"].to_numpy(dtype=float)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.pipe.fit(pd.concat([X, flip(X)], ignore_index=True), np.r_[y, 1 - y])
        return self

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        X = feature_matrix(df)
        return 0.5 * (self.pipe.predict_proba(X)[:, 1] + 1 - self.pipe.predict_proba(flip(X))[:, 1])

    def contributions(self, df: pd.DataFrame) -> pd.DataFrame:
        """Log-odds contribution of each feature towards A winning (positive = favours A)."""
        imp, scaler, lr = self.pipe.named_steps.values()
        Z = scaler.transform(imp.transform(feature_matrix(df)))
        return pd.DataFrame(Z * lr.coef_[0], columns=MODEL_FEATURES, index=df.index)

    def top_factors(self, df: pd.DataFrame, n: int = 3) -> list[list[tuple[str, float]]]:
        contrib = self.contributions(df)
        out = []
        for _, row in contrib.iterrows():
            row = row[[c for c in DIFF_FEATURES]]  # differences are the explainable part
            top = row.reindex(row.abs().sort_values(ascending=False).index)[:n]
            out.append([(name.removeprefix("diff_"), float(v)) for name, v in top.items() if abs(v) > 0.02])
        return out


class MarketBlend:
    """p = sigmoid(w_market * logit(market) + w_stats * logit(stats)), with no intercept (symmetric)."""

    def __init__(self) -> None:
        self.w_market = 1.0
        self.w_stats = 0.0

    def fit(self, p_market: np.ndarray, p_stats: np.ndarray, y: np.ndarray) -> "MarketBlend":
        X = np.c_[logit(p_market), logit(p_stats)]
        y = np.asarray(y, dtype=float)
        lr = LogisticRegression(C=1e6, fit_intercept=False, max_iter=1000)
        lr.fit(np.r_[X, -X], np.r_[y, 1 - y])
        self.w_market, self.w_stats = (float(w) for w in lr.coef_[0])
        return self

    def predict(self, p_market: np.ndarray, p_stats: np.ndarray) -> np.ndarray:
        z = self.w_market * logit(p_market) + self.w_stats * logit(p_stats)
        return 1 / (1 + np.exp(-z))


def method_labels(df: pd.DataFrame) -> pd.Series:
    side = np.where(df["a_wins"] == 1, "A", "B")
    mg = df["method_group"].where(df["method_group"].isin(["KO_TKO", "SUB", "DEC"]))
    return pd.Series([f"{s}_{m}" if isinstance(m, str) else None for s, m in zip(side, mg)], index=df.index)


class MethodModel:
    """Six outcomes: A or B by KO/TKO, submission or decision."""

    def __init__(self) -> None:
        self.model = HistGradientBoostingClassifier(
            max_iter=200, learning_rate=0.03, max_depth=3, min_samples_leaf=100,
            l2_regularization=1.0, early_stopping=False, random_state=0,
        )

    def fit(self, df: pd.DataFrame) -> "MethodModel":
        labels = method_labels(df)
        keep = labels.notna()
        X, y = feature_matrix(df[keep]), labels[keep]
        self.columns = usable_columns(X)
        self.model.fit(pd.concat([X, flip(X)], ignore_index=True)[self.columns],
                       pd.concat([y, y.map(_FLIP_CLASS)], ignore_index=True))
        return self

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        X = feature_matrix(df)
        classes = list(self.model.classes_)
        p = pd.DataFrame(self.model.predict_proba(X[self.columns]), columns=classes, index=df.index)
        pf = pd.DataFrame(self.model.predict_proba(flip(X)[self.columns]), columns=classes, index=df.index)
        pf = pf.rename(columns=_FLIP_CLASS)
        return (0.5 * (p + pf[classes])).reindex(columns=METHOD_CLASSES, fill_value=0.0)
