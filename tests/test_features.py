"""Feature engineering must never look into the future."""

import numpy as np
import pandas as pd

from dinesafe import clean, features
from dinesafe.categorize import chain_sizes


def tables(current_df):
    t = clean.build_tables(current_df.astype("string"))
    est = t.establishments.copy()
    est["category"] = "Unknown"
    est["chain_size"] = chain_sizes(est["name"])
    return t.inspections, t.infractions, est


def test_states_do_not_depend_on_future_inspections(current_df):
    ins, inf, est = tables(current_df)
    cutoff = pd.Timestamp("2025-06-01")
    states_all = features.build_states(ins, inf, est, city_rates={"any": .5, "serious": .2, "not_pass": .07})
    # Corrupt everything after the cutoff: flip outcomes and statuses, drop infractions.
    future = ins["inspection_date"] >= cutoff
    ins2 = ins.copy()
    ins2.loc[future, "has_infraction"] = ~ins2.loc[future, "has_infraction"]
    ins2.loc[future, "has_serious"] = ~ins2.loc[future, "has_serious"]
    ins2.loc[future, "status"] = "Closed"
    ins2.loc[future, "not_pass"] = True
    ins2.loc[future, "n_infractions"] = 99
    inf2 = inf[inf["inspection_date"] < cutoff]
    states_mod = features.build_states(ins2, inf2, est, city_rates={"any": .5, "serious": .2, "not_pass": .07})
    cols = features.feature_columns(states_all)
    past_a = states_all[states_all["inspection_date"] < cutoff].set_index(["est_key", "inspection_date"])[cols]
    past_b = states_mod[states_mod["inspection_date"] < cutoff].set_index(["est_key", "inspection_date"])[cols]
    pd.testing.assert_frame_equal(past_a.sort_index(), past_b.sort_index())


def test_training_pairs_target_next_routine_inspection(current_df):
    ins, inf, est = tables(current_df)
    states = features.build_states(ins, inf, est)
    pairs = features.build_training_pairs(states)
    assert (pairs["target_date"] > pairs["inspection_date"]).all()
    # targets are never follow-ups
    tgt = ins.set_index(["est_key", "inspection_date"])["is_followup"]
    keys = pd.MultiIndex.from_arrays([pairs["est_key"], pairs["target_date"]])
    assert not tgt.reindex(keys).any()
    # each target equals the actual outcome of that inspection
    actual = ins.set_index(["est_key", "inspection_date"])["has_serious"].reindex(keys).astype(int).to_numpy()
    assert (actual == pairs["y_serious"].to_numpy()).all()
    # a failed inspection is paired with the routine inspection after its follow-up
    failed = pairs[pairs["_not_pass"]]
    assert len(failed) and (failed["gap_days"] > 3).all()


def test_trailing_count_window():
    # window is (date - 730 days, date]: 2024-01-01 -> 2025-12-31 is exactly 730 days (excluded),
    # 2024-01-02 -> 2025-12-31 is 729 days (included); groups never mix.
    g = pd.Series(["a", "a", "b", "b", "c"])
    d = pd.to_datetime(pd.Series(["2024-01-01", "2025-12-31", "2024-01-02", "2025-12-31", "2025-12-31"]))
    out = features._trailing_count(g, d, np.ones(5), 730)
    assert out.tolist() == [1, 1, 1, 2, 1]


def test_next_eligible():
    groups = np.array(["a", "a", "a", "b", "b"])
    eligible = np.array([True, False, True, True, True])
    assert features._next_eligible(groups, eligible).tolist() == [2, 2, -1, 4, -1]
