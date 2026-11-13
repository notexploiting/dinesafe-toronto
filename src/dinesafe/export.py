"""Write the compact data bundle that the map dashboard loads.

The bundle is a JavaScript file (``window.DINESAFE_DATA = {...};``) rather than JSON so
that ``dashboard/index.html`` works when opened straight from disk (browsers block
``fetch`` of local JSON files), as well as from any static web host.

Layout (all arrays are aligned; strings are dictionary-encoded to keep the file small):

* ``est``   - one row per establishment (``cols`` + ``rows``)
* ``feat``  - model feature values per establishment (same order), used to explain scores
* ``insp``  - inspections grouped by establishment in ``est`` order: ``count[i]`` rows
  belong to establishment i. ``dday`` holds dates as days since 1970-01-01, delta-encoded
  within each establishment (the first value is absolute). ``flags`` packs
  ``(status + 1) | archive << 3 | followup << 4 | n_infractions << 5``
* ``inf``   - infractions grouped by inspection in ``insp`` order; ``text`` indexes
  ``dict.infraction`` and ``code`` packs ``severity | category << 3``
* ``outcomes`` - sparse list of [infraction index, outcome, outcome date, fine]
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from dinesafe.categorize import CATEGORIES, UNKNOWN
from dinesafe.clean import CATEGORY_LABELS
from dinesafe.model import FACTOR_GROUPS

log = logging.getLogger(__name__)

EPOCH = pd.Timestamp("1970-01-01")
STATUSES = ["Pass", "Conditional Pass", "Closed", "Temporarily Not Operating"]
SEVERITIES = ["Crucial", "Significant", "Minor", "Not Applicable", "Unknown"]
BANDS = ["Low", "Average", "Elevated", "High"]
CAT_SOURCES = ["archive", "inferred", "unknown"]
FEATURE_SNAPSHOT = [
    "last_n_infractions", "last_serious", "last_crucial", "last_not_pass", "recent_significant",
    "recent_crucial", "recent_pest", "recent_hygiene", "recent_temperature", "recent_protection",
    "recent_food_contact", "rate_any", "rate_serious", "rate_not_pass", "ever_closed", "ever_convicted",
    "log_n_inspections", "history_years", "has_archive_history", "routine_per_year", "min_inspections",
    "min_inspections_unknown", "log_chain_size",
]


def _days(s: pd.Series) -> list:
    d = pd.to_datetime(s)
    out = ((d - EPOCH).dt.days).astype("Int64")
    return [None if pd.isna(v) else int(v) for v in out]


def _codes(s: pd.Series, vocab: list[str]) -> list[int]:
    m = {v: i for i, v in enumerate(vocab)}
    return [m.get(v, -1) if isinstance(v, str) else -1 for v in s.astype(object)]


def _num(v, nd: int = 3):
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    r = round(f, nd)
    return int(r) if r == int(r) and abs(r) < 1e15 else r


def _str(v):
    return None if v is None or v is pd.NA or (isinstance(v, float) and math.isnan(v)) else str(v)


def build_bundle(est: pd.DataFrame, ins: pd.DataFrame, inf: pd.DataFrame, feat: pd.DataFrame,
                 *, data_report: dict | None = None, model_report: dict | None = None) -> dict:
    est = est.sort_values(["name", "est_key"], key=lambda s: s.astype("string").str.casefold()).reset_index(drop=True)
    order = {k: i for i, k in enumerate(est["est_key"])}

    ins = ins[ins["est_key"].isin(order)].copy()
    ins["_o"] = ins["est_key"].map(order)
    ins = ins.sort_values(["_o", "inspection_date"]).reset_index(drop=True)
    ins_key = pd.MultiIndex.from_frame(ins[["est_key", "inspection_date"]])

    inf = inf[inf["est_key"].isin(order)].copy()
    pos = pd.Series(np.arange(len(ins)), index=ins_key)
    inf["_i"] = pos.reindex(pd.MultiIndex.from_frame(inf[["est_key", "inspection_date"]])).to_numpy()
    if inf["_i"].isna().any():
        raise ValueError("Infractions reference inspections that don't exist")
    sev_rank = {s: i for i, s in enumerate(SEVERITIES)}
    inf["_s"] = inf["severity"].map(sev_rank).fillna(len(SEVERITIES))
    inf = inf.sort_values(["_i", "_s"], kind="stable").reset_index(drop=True)
    ninf = np.bincount(inf["_i"].astype(int), minlength=len(ins))

    texts = sorted(inf["infraction"].dropna().unique().tolist())
    inf_cats = sorted(set(CATEGORY_LABELS.values()))
    est_types = sorted(est["type"].dropna().unique().tolist())
    outcomes_vocab = sorted(inf["outcome"].dropna().unique().tolist())
    est_cats = CATEGORIES + [UNKNOWN]
    factor_names = list(FACTOR_GROUPS)

    # ---- establishments
    cols = ["id", "name", "address", "postal", "phone", "lat", "lon", "cat", "catSrc", "catConf", "type",
            "minInsp", "legacyId", "newId", "inactive", "lastDate", "lastStatus", "pending", "nInsp3y",
            "nInspTotal", "firstDate", "nCond3y", "nClosed3y", "nCrucial3y", "nSig3y", "nMinor3y", "fines3y",
            "convicted3y", "chain", "pAny", "pSerious", "pNotPass", "band", "factors"]
    cat_i, src_i = _codes(est["category"], est_cats), _codes(est["category_source"], CAT_SOURCES)
    type_i, status_i = _codes(est["type"], est_types), _codes(est["last_status"], STATUSES)
    band_i = _codes(est["risk_band"], BANDS)
    last_d, first_d = _days(est["last_inspection_date"]), _days(est["first_inspection"])
    rows = []
    for i, r in enumerate(est.itertuples(index=False)):
        factors = r.factors if isinstance(r.factors, list) else []
        rows.append([
            r.est_key, _str(r.name), _str(r.address), _str(r.postal_code), _str(r.phone),
            _num(r.lat, 5), _num(r.lon, 5), cat_i[i], src_i[i], _num(r.category_confidence, 2), type_i[i],
            _num(r.min_inspections, 0), _str(r.legacy_id) if _str(r.legacy_id) != r.est_key else None, _str(r.new_id), int(bool(r.likely_inactive)),
            last_d[i], status_i[i], int(bool(r.reinspection_pending)), int(r.n_inspections_3y),
            int(r.n_inspections_total), first_d[i], int(r.n_conditional_3y), int(r.n_closed_3y),
            int(r.n_crucial_3y), int(r.n_significant_3y), int(r.n_minor_3y), _num(r.fines_3y, 2),
            int(bool(r.convicted_3y)), int(r.chain_size), _num(r.p_any), _num(r.p_serious), _num(r.p_not_pass),
            band_i[i], [[factor_names.index(f["factor"]), _num(f["effect"], 2)] for f in factors],
        ])

    feat = feat.set_index("est_key").reindex(est["est_key"])
    feat_rows = [[_num(v, 3) for v in row] for row in feat[FEATURE_SNAPSHOT].round(3).itertuples(index=False)]

    # ---- inspections and infractions
    counts = ins.groupby("_o").size().reindex(range(len(est)), fill_value=0).astype(int).tolist()
    days = np.array(_days(ins["inspection_date"]), dtype=np.int64)
    first_of_est = ins["_o"].ne(ins["_o"].shift()).to_numpy()
    dday = np.where(first_of_est, days, days - np.roll(days, 1))
    status = np.array(_codes(ins["status"], STATUSES)) + 1  # 0 = unknown
    flags = (status | (ins["source"] == "archive").to_numpy().astype(int) << 3
             | ins["is_followup"].to_numpy().astype(int) << 4 | ninf.astype(int) << 5)
    sev_c = np.array(_codes(inf["severity"], SEVERITIES)).clip(min=0)
    cat_c = np.array(_codes(inf["category"], inf_cats)).clip(min=0)
    text_i = {t: k for k, t in enumerate(texts)}
    sparse = []
    for k, r in enumerate(inf[["outcome", "outcome_date", "fine"]].itertuples(index=False)):
        if isinstance(r.outcome, str) or (r.fine is not None and not pd.isna(r.fine)):
            od = None if pd.isna(r.outcome_date) else int((pd.Timestamp(r.outcome_date) - EPOCH).days)
            sparse.append([k, outcomes_vocab.index(r.outcome) if isinstance(r.outcome, str) else -1, od,
                           _num(r.fine, 2)])

    cur = ins[ins["source"] == "current"]
    meta = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataFrom": str(cur["inspection_date"].min().date()) if len(cur) else None,
        "dataTo": str(cur["inspection_date"].max().date()) if len(cur) else None,
        "sourceRefreshed": (data_report or {}).get("source_last_refreshed"),
        "nEstablishments": int(len(est)),
        "nInspections": int(len(ins)),
        "nInfractions": int(len(inf)),
    }
    if model_report:
        meta["baseRates"] = {k[2:]: v for k, v in model_report.get("base_rates_recent", {}).items()}
        meta["validation"] = model_report.get("validation", {})
        meta["auc"] = {t[2:]: model_report.get(t, {}).get("models", {}).get(
            model_report.get(t, {}).get("chosen_model", ""), {}).get("roc_auc") for t in
            ("y_any", "y_serious", "y_not_pass")}
    return {
        "meta": meta,
        "dict": {"status": STATUSES, "severity": SEVERITIES, "infCategory": inf_cats, "estCategory": est_cats,
                 "catSource": CAT_SOURCES, "type": est_types, "infraction": texts, "outcome": outcomes_vocab,
                 "band": BANDS, "factor": factor_names},
        "est": {"cols": cols, "rows": rows},
        "feat": {"cols": FEATURE_SNAPSHOT, "rows": feat_rows},
        "insp": {"count": counts, "dday": dday.tolist(), "flags": flags.astype(int).tolist()},
        "inf": {"text": [text_i[t] for t in inf["infraction"]], "code": (sev_c | cat_c << 3).astype(int).tolist()},
        "outcomes": sparse,
    }


def write_dashboard_data(path: Path, est: pd.DataFrame, ins: pd.DataFrame, inf: pd.DataFrame, feat: pd.DataFrame,
                         *, data_report: dict | None = None, model_report: dict | None = None) -> Path:
    bundle = build_bundle(est, ins, inf, feat, data_report=data_report, model_report=model_report)
    payload = json.dumps(bundle, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("window.DINESAFE_DATA=" + payload + ";\n", encoding="utf-8")
    tmp.replace(path)
    log.info("Wrote dashboard data (%.1f MB) to %s", path.stat().st_size / 1e6, path)
    return path
