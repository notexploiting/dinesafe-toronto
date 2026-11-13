"""Feature engineering for the "next inspection" risk model.

Framing
-------
For every inspection *k* of an establishment we compute the establishment's **state
after inspection k**: what an observer would know at that moment (history counts, rates,
recent results, infraction types, ...). The prediction target for that state is the
outcome of the establishment's **next routine inspection** (follow-up re-inspections
right after a Conditional Pass are skipped, because they are scheduled checks that almost
always pass and would otherwise teach the model the wrong lesson).

This makes training and scoring use exactly the same function:

* training rows  = (state after inspection k, outcome of the next routine inspection)
* scoring rows   = state after each establishment's most recent inspection

Every feature is computed only from inspections up to and including *k*, so nothing
from the future leaks into the inputs (tests/test_features.py checks this).

Targets
-------
* ``y_any``      - at least one infraction of any severity
* ``y_serious``  - at least one *significant* or *crucial* infraction
* ``y_not_pass`` - the inspection result is Conditional Pass or Closed

The archive (pre-Nov 2023) contributes history features (counts, severities), but
targets come only from the current feed, and pass/fail-based features only use current
feed inspections, because the archive's status column is unreliable.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from dinesafe.categorize import CATEGORIES

EWM_ALPHA = 0.4  # weight of the newest inspection in exponentially weighted rates
PRIOR_STRENGTH = 3.0  # pseudo-inspections used to shrink rates toward the city average
RECENT_N = 3  # "recent" = the last N routine inspections

CATEGORY_FLAGS = {
    "pest": "Pest control",
    "hygiene": "Hand washing & staff hygiene",
    "temperature": "Food temperature control",
    "protection": "Food protection & contamination",
    "food_contact": "Food-contact surfaces & dishwashing",
}

TARGETS = ["y_any", "y_serious", "y_not_pass"]

# Human-readable descriptions used for model explanations in the dashboard.
FEATURE_LABELS = {
    "log_n_inspections": "Number of past inspections",
    "history_years": "Years of inspection history",
    "rate_any": "Share of past inspections with infractions",
    "rate_serious": "Share of past inspections with significant/crucial infractions",
    "rate_not_pass": "Share of past inspections not passed",
    "ewm_any": "Recent inspections had infractions",
    "ewm_serious": "Recent inspections had significant/crucial infractions",
    "ewm_not_pass": "Recent inspections were not passed",
    "last_n_infractions": "Infractions at the last routine inspection",
    "last_serious": "Last routine inspection had a significant/crucial infraction",
    "last_crucial": "Last routine inspection had a crucial infraction",
    "last_not_pass": "Last routine inspection was not passed",
    "last_was_followup": "Last visit was a re-inspection",
    "recent_crucial": "Crucial infractions in the last 3 routine inspections",
    "recent_significant": "Significant infractions in the last 3 routine inspections",
    "recent_pest": "Pest infractions in the last 3 routine inspections",
    "recent_hygiene": "Hand-washing/hygiene infractions in the last 3 routine inspections",
    "recent_temperature": "Temperature-control infractions in the last 3 routine inspections",
    "recent_protection": "Food-protection infractions in the last 3 routine inspections",
    "recent_food_contact": "Dishwashing/food-contact infractions in the last 3 routine inspections",
    "routine_per_year": "Routine inspections per year (last 2 years)",
    "ever_closed": "Has been closed by Toronto Public Health",
    "ever_convicted": "Has a conviction for food-safety offences",
    "has_archive_history": "Has pre-2023 inspection history",
    "min_inspections": "City-assigned minimum inspections per year",
    "min_inspections_unknown": "City risk tier unknown",
    "log_chain_size": "Part of a chain",
}
for _c in CATEGORIES:
    FEATURE_LABELS[f"cat_{_c}"] = f"Category: {_c}"
FEATURE_LABELS["cat_Unknown"] = "Category unknown"


def _group_ewm(values: pd.Series, groups: pd.Series, alpha: float) -> pd.Series:
    """Inclusive exponentially weighted mean within groups (NaN values are skipped)."""
    res = values.groupby(groups, sort=False).ewm(alpha=alpha, ignore_na=True).mean()
    return res.reset_index(level=0, drop=True).reindex(values.index)


def _trailing_count(groups: pd.Series, dates: pd.Series, weights: np.ndarray, window_days: int) -> np.ndarray:
    """Sum of ``weights`` over rows of the same group dated within the trailing window
    ``(date - window_days, date]``. Rows must be sorted by group, then date."""
    codes = pd.factorize(groups, sort=False)[0].astype(np.int64)
    days = ((dates - pd.Timestamp("1990-01-01")).dt.days).to_numpy(dtype=np.int64)
    key = codes * 1_000_000 + days  # groups are 1e6 days apart, so windows never overlap
    start = np.searchsorted(key, key - (window_days - 1), side="left")
    csum = np.concatenate([[0.0], np.cumsum(weights, dtype=float)])
    return csum[np.arange(len(key)) + 1] - csum[start]


def _ffill_within(values: pd.Series, groups: pd.Series) -> pd.Series:
    return values.groupby(groups, sort=False).ffill()


def build_states(inspections: pd.DataFrame, infractions: pd.DataFrame,
                 establishments: pd.DataFrame, city_rates: dict | None = None) -> pd.DataFrame:
    """One row per inspection: the establishment's state *after* that inspection.

    ``city_rates`` are the prior means used to shrink per-establishment rates; pass the
    rates from the training period to avoid any look-ahead (defaults to the data's own).
    """
    ins = inspections.sort_values(["est_key", "inspection_date"]).reset_index(drop=True).copy()
    g = ins["est_key"]
    is_cur = (ins["source"] == "current").to_numpy()
    routine = ~ins["is_followup"].to_numpy()

    # ---- per-inspection counts of selected infraction categories
    key = ["est_key", "inspection_date"]
    flags = pd.DataFrame({f"n_{short}": (infractions["category"] == label).astype(float)
                          for short, label in CATEGORY_FLAGS.items()})
    flags[key] = infractions[key]
    per_insp = flags.groupby(key, sort=False).sum().reset_index()
    ins = ins.merge(per_insp, on=key, how="left")
    for short in CATEGORY_FLAGS:
        ins[f"n_{short}"] = ins[f"n_{short}"].fillna(0.0)

    any_ = ins["has_infraction"].astype(float)
    ser = ins["has_serious"].astype(float)
    notp = ins["not_pass"].astype(float).where(is_cur)  # archive statuses unreliable

    if city_rates is None:
        r = ins[routine & is_cur]
        city_rates = {"any": float(r["has_infraction"].mean()), "serious": float(r["has_serious"].mean()),
                      "not_pass": float(r["not_pass"].mean())}

    out = pd.DataFrame({"est_key": ins["est_key"], "inspection_date": ins["inspection_date"],
                        "source": ins["source"], "status": ins["status"], "is_followup": ins["is_followup"]})
    gb = lambda s: s.groupby(g, sort=False)  # noqa: E731

    # ---- history size and span
    n_all = gb(pd.Series(1.0, index=ins.index)).cumsum()
    out["log_n_inspections"] = np.log1p(n_all)
    first = ins.groupby("est_key", sort=False)["inspection_date"].transform("min")
    out["history_years"] = (ins["inspection_date"] - first).dt.days / 365.25

    # ---- smoothed rates over routine inspections (empirical-Bayes style shrinkage)
    rmask = pd.Series(routine, index=ins.index).astype(float)
    n_r = gb(rmask).cumsum()
    n_r_cur = gb(rmask * is_cur).cumsum()
    m = PRIOR_STRENGTH
    out["rate_any"] = (gb(any_ * rmask).cumsum() + m * city_rates["any"]) / (n_r + m)
    out["rate_serious"] = (gb(ser * rmask).cumsum() + m * city_rates["serious"]) / (n_r + m)
    out["rate_not_pass"] = (gb(notp.fillna(0) * rmask).cumsum() + m * city_rates["not_pass"]) / (n_r_cur + m)

    # ---- recency-weighted rates (routine inspections only; others are NaN and skipped)
    out["ewm_any"] = _group_ewm(any_.where(routine), g, EWM_ALPHA).fillna(city_rates["any"])
    out["ewm_serious"] = _group_ewm(ser.where(routine), g, EWM_ALPHA).fillna(city_rates["serious"])
    out["ewm_not_pass"] = _group_ewm(notp.where(routine), g, EWM_ALPHA).fillna(city_rates["not_pass"])

    # ---- the last routine inspection (carried forward over follow-ups)
    def last_routine(series: pd.Series, fill: float) -> pd.Series:
        return _ffill_within(series.where(routine), g).fillna(fill)

    out["last_n_infractions"] = np.log1p(last_routine(ins["n_infractions"].astype(float), 0))
    out["last_serious"] = last_routine(ser, 0)
    out["last_crucial"] = last_routine((ins["n_crucial"] > 0).astype(float), 0)
    out["last_not_pass"] = last_routine(notp, 0)
    out["last_was_followup"] = ins["is_followup"].astype(float)

    # ---- rolling sums over the last N routine inspections
    def recent(series: pd.Series) -> pd.Series:
        s = series.astype(float).where(routine).dropna()
        rolled = s.groupby(g[s.index], sort=False).rolling(RECENT_N, min_periods=1).sum()
        rolled = rolled.reset_index(level=0, drop=True).reindex(ins.index)
        return _ffill_within(rolled, g).fillna(0)

    out["recent_crucial"] = np.log1p(recent(ins["n_crucial"]))
    out["recent_significant"] = np.log1p(recent(ins["n_significant"]))
    for short in CATEGORY_FLAGS:
        out[f"recent_{short}"] = np.log1p(recent(ins[f"n_{short}"]))

    # ---- cadence: routine inspections in the trailing two years (proxy for risk tier)
    out["routine_per_year"] = _trailing_count(g, ins["inspection_date"], rmask.to_numpy(), 730) / 2.0

    # ---- serious enforcement history
    out["ever_closed"] = gb((ins["status"] == "Closed").astype(float)).cummax()
    out["ever_convicted"] = gb(ins["has_conviction"].astype(float)).cummax()
    out["has_archive_history"] = gb((ins["source"] == "archive").astype(float)).cummax()

    # ---- static establishment attributes
    est = establishments.set_index("est_key")
    mi = pd.to_numeric(est["min_inspections"], errors="coerce").astype(float)
    out["min_inspections"] = g.map(mi).fillna(2.0).to_numpy()  # 2 = the most common tier
    out["min_inspections_unknown"] = g.map(mi.isna()).fillna(True).astype(float).to_numpy()
    out["log_chain_size"] = np.log1p(g.map(est["chain_size"]).fillna(1).astype(float) - 1).to_numpy()
    cats = g.map(est["category"]).fillna("Unknown")
    for c in CATEGORIES + ["Unknown"]:
        out[f"cat_{c}"] = (cats == c).astype(float).to_numpy()

    # ---- raw outcome of this inspection (used to build targets, never as a feature)
    out["_routine"] = routine
    out["_current"] = is_cur
    out["_any"] = ins["has_infraction"].to_numpy()
    out["_serious"] = ins["has_serious"].to_numpy()
    out["_not_pass"] = ins["not_pass"].to_numpy()
    out.attrs["city_rates"] = city_rates
    return out


def feature_columns(states: pd.DataFrame) -> list[str]:
    return [c for c in states.columns if c in FEATURE_LABELS]


def build_training_pairs(states: pd.DataFrame) -> pd.DataFrame:
    """Pair states with the outcome of the next routine, current-feed inspection.

    A state is paired with target T when
    * T is the very next inspection (the normal case), or
    * the state is a Conditional Pass / Closed result and only follow-up
      re-inspections lie between it and T (so "just failed, re-inspection pending" states
      are represented in training, as they are at scoring time).
    Older states are not paired with distant targets.
    """
    s = states.reset_index(drop=True)
    target_ok = (s["_routine"] & s["_current"]).to_numpy()
    # index of the next eligible target inspection within the same establishment
    nxt = _next_eligible(s["est_key"].to_numpy(), target_ok)
    immediate = nxt == np.arange(len(s)) + 1
    failed = (s["_not_pass"] & s["_current"]).to_numpy()
    has = (nxt >= 0) & (immediate | failed)
    pairs = s.loc[has].copy()
    t = s.iloc[nxt[has]].reset_index(drop=True)
    pairs = pairs.reset_index(drop=True)
    pairs["target_date"] = t["inspection_date"].to_numpy()
    pairs["y_any"] = t["_any"].astype(int).to_numpy()
    pairs["y_serious"] = t["_serious"].astype(int).to_numpy()
    pairs["y_not_pass"] = t["_not_pass"].astype(int).to_numpy()
    pairs["gap_days"] = (pairs["target_date"] - pairs["inspection_date"]).dt.days
    return pairs


def _next_eligible(groups: np.ndarray, eligible: np.ndarray) -> np.ndarray:
    """For each row, the index of the next *later* eligible row in the same group, or -1."""
    n = len(groups)
    out = np.full(n, -1, dtype=np.int64)
    nxt = -1
    for i in range(n - 1, -1, -1):
        if i == n - 1 or groups[i] != groups[i + 1]:
            nxt = -1
        out[i] = nxt
        if eligible[i]:
            nxt = i
    return out


def latest_states(states: pd.DataFrame) -> pd.DataFrame:
    """The state after each establishment's most recent inspection (for scoring)."""
    return states.sort_values(["est_key", "inspection_date"]).drop_duplicates("est_key", keep="last")
