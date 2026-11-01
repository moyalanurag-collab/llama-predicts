"""Train the final models on all history, save them, and load them back."""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import sklearn

from .core import Explainer, MarketBlend, MethodModel, StatsModel
from .walkforward import WalkForward, labelled

MODEL_VERSION = "3.0"
MIN_BLEND_FIGHTS = 500  # need this many out-of-sample fights with odds to learn blend weights


@dataclass
class ModelBundle:
    stats: StatsModel
    explainer: Explainer
    methods: MethodModel
    blend: MarketBlend
    trained_at: str
    data_through: str
    training_fights: int
    sklearn_version: str = sklearn.__version__
    version: str = MODEL_VERSION
    evaluation: dict = field(default_factory=dict)


def train_final(df: pd.DataFrame, wf: WalkForward, evaluation: dict | None = None) -> ModelBundle:
    data = labelled(df)
    blend = MarketBlend()  # default: trust the market fully until there is evidence otherwise
    oos = wf.predictions
    if "market_p_a" in oos:
        oos = oos.dropna(subset=["market_p_a", "p_stats", "a_wins"])
        if len(oos) >= MIN_BLEND_FIGHTS:
            blend.fit(oos["market_p_a"], oos["p_stats"], oos["a_wins"])  # out-of-sample predictions only
    return ModelBundle(
        stats=StatsModel().fit(data),
        explainer=Explainer().fit(data),
        methods=MethodModel().fit(data),
        blend=blend,
        trained_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        data_through=f"{data['date'].max():%Y-%m-%d}",
        training_fights=len(data),
        evaluation=evaluation or {},
    )


def save(bundle: ModelBundle, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(bundle, fh)
    tmp.replace(path)


def load(path: str | Path) -> ModelBundle:
    """Load a saved model. Only load files this program wrote: pickle can run code."""
    with open(path, "rb") as fh:
        bundle = pickle.load(fh)
    if not isinstance(bundle, ModelBundle):
        raise ValueError(f"{path} is not a UFC Edge model file.")
    if bundle.sklearn_version != sklearn.__version__:
        print(f"Note: model was trained with scikit-learn {bundle.sklearn_version}, now {sklearn.__version__}. "
              "Retrain if predictions look odd: python -m ufc_edge train")
    return bundle
