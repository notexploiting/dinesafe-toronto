"""Train, evaluate and apply the next-inspection risk models.

Evaluation is **out-of-time**: models are trained on inspections before a cutoff date
and evaluated on the 12 months after it, which mimics how the model is used (predicting
inspections that haven't happened yet). Each model is compared with simple baselines so
it's clear what the ML adds:

* ``city_average``   - everyone gets the training-period average rate
* ``last_inspection`` - rate conditional on whether the previous routine inspection had the
  same kind of problem ("did they fail last time?")
* ``history_rate``   - the establishment's own smoothed historical rate
* ``logistic``       - standardized logistic regression on all features
* ``gbm``            - histogram gradient boosting on all features

The production model for each target is the logistic regression unless gradient boosting
is clearly better (by ``GBM_MIN_AUC_GAIN``); logistic regression is preferred on ties
because its per-establishment explanations are exact (coefficient x standardized value).
Final models are refit on all labelled data before scoring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from dinesafe.features import FEATURE_LABELS, TARGETS

log = logging.getLogger(__name__)

GBM_MIN_AUC_GAIN = 0.01
VALIDATION_MONTHS = 12
RECENCY_HALF_LIFE_DAYS = 365  # older training rows count less (rates drift over time)

TARGET_INFO = {
    "y_any": {"label": "Any infraction", "baseline_feature": "last_n_infractions", "rate_feature": "rate_any"},
    "y_serious": {"label": "Significant or crucial infraction", "baseline_feature": "last_serious",
                  "rate_feature": "rate_serious"},
    "y_not_pass": {"label": "Conditional Pass or Closed", "baseline_feature": "last_not_pass",
                   "rate_feature": "rate_not_pass"},
}


@dataclass
class TargetModel:
    target: str
    kind: str
    model: object
    logistic: object
    base_rate: float
    metrics: dict = field(default_factory=dict)


# ------------------------------------------------------------------------- metrics
def evaluate_scores(y: np.ndarray, p: np.ndarray) -> dict:
    from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, dtype=int)
    out = {"n": int(len(y)), "observed_rate": round(float(y.mean()), 4), "mean_prediction": round(float(p.mean()), 4),
           "brier": round(float(brier_score_loss(y, p)), 5), "log_loss": round(float(log_loss(y, p, labels=[0, 1])), 5)}
    if 0 < y.sum() < len(y):
        out["roc_auc"] = round(float(roc_auc_score(y, p)), 4)
        out["average_precision"] = round(float(average_precision_score(y, p)), 4)
    out["ece"] = round(expected_calibration_error(y, p), 4)
    return out


def calibration_table(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    """Observed vs predicted rate by decile of predicted probability."""
    df = pd.DataFrame({"y": y, "p": p})
    df["bin"] = pd.qcut(df["p"].rank(method="first"), q=bins, labels=False)
    t = df.groupby("bin").agg(n=("y", "size"), mean_predicted=("p", "mean"), observed=("y", "mean")).reset_index()
    return [{k: (round(float(v), 4) if k != "bin" and k != "n" else int(v)) for k, v in r.items()}
            for r in t.to_dict("records")]


def expected_calibration_error(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    df = pd.DataFrame({"y": y, "p": p})
    df["bin"] = pd.qcut(df["p"].rank(method="first"), q=bins, labels=False)
    t = df.groupby("bin").agg(n=("y", "size"), p=("p", "mean"), o=("y", "mean"))
    return float((t["n"] * (t["p"] - t["o"]).abs()).sum() / t["n"].sum())


# -------------------------------------------------------------------------- models
def make_logistic():
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(StandardScaler(), LogisticRegression(C=0.5, max_iter=5000))


def make_gbm(random_state: int = 0):
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(learning_rate=0.05, max_iter=500, max_leaf_nodes=31,
                                          min_samples_leaf=100, l2_regularization=1.0, early_stopping=True,
                                          validation_fraction=0.15, n_iter_no_change=30,
                                          random_state=random_state)


def recency_weights(dates: pd.Series, reference: pd.Timestamp) -> np.ndarray:
    age = (reference - pd.to_datetime(dates)).dt.days.clip(lower=0).to_numpy(dtype=float)
    w = 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)
    return w / w.mean()


def _fit(model, x, y, w):
    name = type(model).__name__
    if name == "Pipeline":
        return model.fit(x, y, logisticregression__sample_weight=w)
    return model.fit(x, y, sample_weight=w)


def _last_inspection_baseline(train: pd.DataFrame, test: pd.DataFrame, feature: str, target: str) -> np.ndarray:
    flag_tr = (train[feature] > 0).to_numpy()
    flag_te = (test[feature] > 0).to_numpy()
    y = train[target].to_numpy()
    p1 = y[flag_tr].mean() if flag_tr.any() else y.mean()
    p0 = y[~flag_tr].mean() if (~flag_tr).any() else y.mean()
    return np.where(flag_te, p1, p0)


def split_by_time(pairs: pd.DataFrame, months: int = VALIDATION_MONTHS) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    cutoff = pairs["target_date"].max() - pd.DateOffset(months=months)
    train = pairs[pairs["target_date"] < cutoff]
    valid = pairs[pairs["target_date"] >= cutoff]
    return train, valid, pd.Timestamp(cutoff)


def train_models(pairs: pd.DataFrame, feature_cols: list[str], *, evaluate: bool = True) -> tuple[dict, dict]:
    """Return ({target: TargetModel}, report)."""
    report: dict = {"features": feature_cols, "n_pairs": int(len(pairs))}
    models: dict[str, TargetModel] = {}
    train, valid, cutoff = split_by_time(pairs)
    report["validation"] = {
        "cutoff": str(cutoff.date()),
        "train_period": [str(train["target_date"].min().date()), str(train["target_date"].max().date())],
        "valid_period": [str(valid["target_date"].min().date()), str(valid["target_date"].max().date())],
        "n_train": int(len(train)), "n_valid": int(len(valid)),
    }
    x_all = pairs[feature_cols].to_numpy(dtype=float)
    x_tr, x_va = train[feature_cols].to_numpy(dtype=float), valid[feature_cols].to_numpy(dtype=float)
    w_tr = recency_weights(train["target_date"], cutoff)
    w_all = recency_weights(pairs["target_date"], pairs["target_date"].max())

    for target in TARGETS:
        info = TARGET_INFO[target]
        y_tr, y_va = train[target].to_numpy(), valid[target].to_numpy()
        res: dict = {"label": info["label"]}
        choice = "logistic"
        if evaluate and len(valid) and 0 < y_va.sum() < len(y_va):
            scores = {
                "city_average": np.full(len(y_va), np.average(y_tr, weights=w_tr)),
                "last_inspection": _last_inspection_baseline(train, valid, info["baseline_feature"], target),
                "history_rate": valid[info["rate_feature"]].to_numpy(),
            }
            lr = _fit(make_logistic(), x_tr, y_tr, w_tr)
            gbm = _fit(make_gbm(), x_tr, y_tr, w_tr)
            scores["logistic"] = lr.predict_proba(x_va)[:, 1]
            scores["gbm"] = gbm.predict_proba(x_va)[:, 1]
            res["models"] = {k: evaluate_scores(y_va, v) for k, v in scores.items()}
            gain = res["models"]["gbm"].get("roc_auc", 0) - res["models"]["logistic"].get("roc_auc", 0)
            choice = "gbm" if gain >= GBM_MIN_AUC_GAIN else "logistic"
            res["calibration"] = calibration_table(y_va, scores[choice])
            log.info("%s: AUC logistic %.3f, gbm %.3f, last-inspection %.3f -> using %s",
                     target, res["models"]["logistic"].get("roc_auc", np.nan),
                     res["models"]["gbm"].get("roc_auc", np.nan),
                     res["models"]["last_inspection"].get("roc_auc", np.nan), choice)
        res["chosen_model"] = choice

        # ---- refit on everything for scoring
        y_all = pairs[target].to_numpy()
        final_lr = _fit(make_logistic(), x_all, y_all, w_all)
        final = final_lr if choice == "logistic" else _fit(make_gbm(), x_all, y_all, w_all)
        base = float(np.average(y_all, weights=w_all))
        res["base_rate_recent"] = round(base, 4)
        coefs = final_lr[-1].coef_[0]
        res["logistic_coefficients"] = {f: round(float(c), 4) for f, c in
                                        sorted(zip(feature_cols, coefs), key=lambda t: -abs(t[1]))}
        models[target] = TargetModel(target=target, kind=choice, model=final, logistic=final_lr,
                                     base_rate=base, metrics=res)
        report[target] = res
    return models, report


# ------------------------------------------------------------------- scoring
# Features are correlated (e.g. "last inspection was serious" and "share of past
# inspections that were serious"), so individual logistic contributions can have
# counter-intuitive signs. Explanations therefore sum contributions within groups of
# related features, which nets those effects out.
FACTOR_GROUPS = {
    "Recent inspection results": ["last_n_infractions", "last_serious", "last_crucial", "last_not_pass",
                                  "last_was_followup", "ewm_any", "ewm_serious", "ewm_not_pass",
                                  "recent_crucial", "recent_significant"],
    "Kinds of recent infractions": ["recent_pest", "recent_hygiene", "recent_temperature", "recent_protection",
                                    "recent_food_contact"],
    "Long-term track record": ["rate_any", "rate_serious", "rate_not_pass", "ever_closed", "ever_convicted"],
    "Length of inspection history": ["log_n_inspections", "history_years", "has_archive_history"],
    "Inspection frequency / City risk tier": ["routine_per_year", "min_inspections", "min_inspections_unknown"],
    "Type of establishment": [f for f in FEATURE_LABELS if f.startswith("cat_")],
    "Chain or independent": ["log_chain_size"],
}

_KIND_NAMES = {"recent_pest": "pest control", "recent_hygiene": "hand washing/hygiene",
               "recent_temperature": "temperature control", "recent_protection": "food protection",
               "recent_food_contact": "dishwashing/food-contact surfaces"}


def _detail(group: str, v: dict) -> str:
    """Plain-language facts behind a factor group, from one establishment's feature values."""
    cnt = lambda f: int(round(np.expm1(v[f])))  # noqa: E731  (features stored as log1p counts)
    if group == "Recent inspection results":
        n = cnt("last_n_infractions")
        txt = f"Last routine inspection: {n} infraction{'s' if n != 1 else ''}"
        if v["last_crucial"] >= 0.5:
            txt += " incl. crucial"
        elif v["last_serious"] >= 0.5:
            txt += " incl. significant"
        if v["last_not_pass"] >= 0.5:
            txt += ", not passed"
        sig, cru = cnt("recent_significant"), cnt("recent_crucial")
        return txt + f". Last 3 routine inspections: {sig} significant, {cru} crucial infraction(s)."
    if group == "Kinds of recent infractions":
        kinds = [f"{_KIND_NAMES[f]} ({cnt(f)})" for f in _KIND_NAMES if cnt(f) > 0]
        return ("Recent infractions involved " + ", ".join(kinds) + ".") if kinds else \
            "No pest, hygiene, temperature, food-protection or dishwashing infractions recently."
    if group == "Long-term track record":
        txt = (f"Estimated long-run rates: {v['rate_serious']:.0%} of inspections with significant/crucial "
               f"infractions, {v['rate_any']:.0%} with any infraction (blended with the city average "
               f"when the history is short)")
        extras = [t for f, t in (("ever_closed", "has been closed before"),
                                 ("ever_convicted", "has a food-safety conviction")) if v[f] >= 0.5]
        return txt + ("; " + " and ".join(extras) if extras else "") + "."
    if group == "Length of inspection history":
        n = cnt("log_n_inspections")
        if n <= 1:
            return "Only one inspection on record (new or newly registered establishment)."
        span = f"over {v['history_years']:.1f} years" if v["history_years"] >= 0.1 else "within the last month"
        txt = f"{n} inspections on record {span}"
        return txt + (" (incl. pre-2023 archive)." if v["has_archive_history"] >= 0.5 else ".")
    if group == "Inspection frequency / City risk tier":
        tier = "unknown" if v["min_inspections_unknown"] >= 0.5 else f"{v['min_inspections']:.0f} per year"
        return f"About {v['routine_per_year']:.1f} routine inspections per year; City minimum: {tier}."
    if group == "Type of establishment":
        cat = next((f[4:] for f in FACTOR_GROUPS[group] if v.get(f, 0) >= 0.5), "Unknown")
        return f"Category: {cat}."
    if group == "Chain or independent":
        n = int(round(np.expm1(v["log_chain_size"]) + 1))
        return f"Name shared by {n} locations." if n > 1 else "Single location (not a chain)."
    return ""


def explain(model: TargetModel, x: np.ndarray, feature_cols: list[str], top: int = 4,
            min_effect: float = 0.05) -> list[list[dict]]:
    """Per-row factor groups ranked by how much they raise (or lower) the risk score.

    contribution_j = coef_j * (x_j - mean_j) / sd_j is the change in log-odds versus an
    establishment with average feature values; groups sum their members' contributions.
    """
    scaler, lr = model.logistic[0], model.logistic[-1]
    z = (x - scaler.mean_) / np.where(scaler.scale_ == 0, 1, scaler.scale_)
    contrib = z * lr.coef_[0]
    idx = {f: i for i, f in enumerate(feature_cols)}
    groups = {g: [idx[f] for f in fs if f in idx] for g, fs in FACTOR_GROUPS.items()}
    groups = {g: js for g, js in groups.items() if js}
    names = list(groups)
    gsum = np.column_stack([contrib[:, js].sum(axis=1) for js in groups.values()])
    out = []
    for i in range(len(x)):
        order = [j for j in np.argsort(-np.abs(gsum[i])) if abs(gsum[i, j]) >= min_effect][:top]
        order.sort(key=lambda j: -gsum[i, j])
        values = {f: float(x[i, k]) for f, k in idx.items()}
        out.append([{"factor": names[j], "effect": round(float(gsum[i, j]), 3),
                     "detail": _detail(names[j], values)} for j in order])
    return out


def score(models: dict, states: pd.DataFrame, feature_cols: list[str], *, explain_target: str = "y_serious") -> pd.DataFrame:
    """Predict each target for the given states (one row per establishment)."""
    x = states[feature_cols].to_numpy(dtype=float)
    out = pd.DataFrame({"est_key": states["est_key"].to_numpy()})
    for target, m in models.items():
        p = m.model.predict_proba(x)[:, 1]
        out[f"p_{target[2:]}"] = p.round(4)
        out[f"rr_{target[2:]}"] = (p / m.base_rate).round(3)
    out["factors"] = explain(models[explain_target], x, feature_cols)
    return out


def risk_band(relative_risk: pd.Series) -> pd.Series:
    """Bands on relative risk (prediction / recent city average)."""
    bins = [-np.inf, 0.75, 1.25, 2.0, np.inf]
    labels = ["Low", "Average", "Elevated", "High"]
    return pd.cut(relative_risk, bins=bins, labels=labels).astype("string")
