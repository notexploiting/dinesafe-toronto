"""End-to-end build: raw files -> clean tables -> features -> models -> outputs.

Outputs
-------
data/processed/establishments.csv   one row per establishment: location, category, latest
                                    status, 3-year summary, risk predictions + top factors
data/processed/inspections.csv      one row per inspection (establishment + date)
data/processed/infractions.csv      one row per infraction
reports/data_quality.json           what the cleaning step found and fixed
reports/model_report.json           validation metrics, calibration, coefficients
reports/model_card.md               human-readable summary of the above
dashboard/data/dinesafe-data.js     compact data bundle for the map dashboard
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from dinesafe import categorize, clean, export, features, model
from dinesafe.config import Paths
from dinesafe.download import existing_historical_files, latest_snapshot, read_manifest

log = logging.getLogger(__name__)


@dataclass
class BuildResult:
    establishments: pd.DataFrame
    inspections: pd.DataFrame
    infractions: pd.DataFrame
    data_report: dict
    model_report: dict


def _pre_cutoff_city_rates(inspections: pd.DataFrame, cutoff: pd.Timestamp) -> dict:
    r = inspections[(inspections["source"] == "current") & ~inspections["is_followup"]
                    & (inspections["inspection_date"] < cutoff)]
    if r.empty:
        r = inspections[inspections["source"] == "current"]
    return {"any": float(r["has_infraction"].mean()), "serious": float(r["has_serious"].mean()),
            "not_pass": float(r["not_pass"].mean())}


def summarize_establishments(est: pd.DataFrame, ins: pd.DataFrame, inf: pd.DataFrame) -> pd.DataFrame:
    """Add latest-status and 3-year (current feed) summary columns."""
    cur = ins[ins["source"] == "current"]
    last = cur.sort_values("inspection_date").drop_duplicates("est_key", keep="last").set_index("est_key")
    agg = cur.groupby("est_key").agg(
        n_inspections_3y=("inspection_date", "size"),
        first_inspection_3y=("inspection_date", "min"),
        n_conditional_3y=("status", lambda s: int((s == "Conditional Pass").sum())),
        n_closed_3y=("status", lambda s: int((s == "Closed").sum())),
        n_minor_3y=("n_minor", "sum"),
        n_significant_3y=("n_significant", "sum"),
        n_crucial_3y=("n_crucial", "sum"),
        fines_3y=("fine_total", "sum"),
        convicted_3y=("has_conviction", "any"),
    )
    allins = ins.groupby("est_key").agg(n_inspections_total=("inspection_date", "size"),
                                        first_inspection=("inspection_date", "min"))
    out = est.set_index("est_key").join(agg).join(allins)
    out["last_inspection_date"] = last["inspection_date"]
    out["last_status"] = last["status"]
    out["last_n_infractions"] = last["n_infractions"]
    out["last_has_serious"] = last["has_serious"]
    # A Conditional Pass/Closed with no later visit means a re-inspection is pending.
    out["reinspection_pending"] = last["not_pass"].reindex(out.index).fillna(False).astype(bool)
    # Establishments never carried into the City's new (Nov 2025+) system are most likely
    # closed or relocated; they are kept but flagged.
    out["likely_inactive"] = ~out["in_new_system"].astype(bool)
    for c in ["n_inspections_3y", "n_conditional_3y", "n_closed_3y", "n_minor_3y", "n_significant_3y",
              "n_crucial_3y", "n_inspections_total"]:
        out[c] = out[c].fillna(0).astype(int)
    out["fines_3y"] = out["fines_3y"].fillna(0.0)
    out["convicted_3y"] = out["convicted_3y"].fillna(False).astype(bool)
    return out.reset_index()


def build(paths: Paths, *, snapshot: Path | None = None, use_historical: bool = True,
          evaluate: bool = True) -> BuildResult:
    paths.ensure()
    t0 = time.time()
    snapshot = snapshot or latest_snapshot(paths.raw)
    if snapshot is None:
        raise FileNotFoundError(f"No DineSafe snapshot in {paths.raw}. Run `python -m dinesafe download` first.")
    log.info("Reading %s", snapshot.name)
    current_raw = clean.read_current(snapshot)
    hist_files = existing_historical_files(paths.historical) if use_historical else []
    historical_raw, repairs = clean.read_historical(hist_files) if hist_files else (None, {})
    if use_historical and not hist_files:
        log.warning("No historical files in %s; building from the current feed only", paths.historical)

    # ---- clean
    tables = clean.build_tables(current_raw, historical_raw)
    data_report = {"snapshot": snapshot.name, "historical_files": len(hist_files),
                   "historical_quote_repairs": repairs, **tables.report}
    manifest = read_manifest(snapshot) or {}
    data_report["source_last_refreshed"] = manifest.get("package_last_refreshed")
    log.info("Cleaned: %d establishments, %d inspections, %d infractions (%.0fs)",
             len(tables.establishments), len(tables.inspections), len(tables.infractions), time.time() - t0)

    # ---- establishment categories and chain sizes
    est, cat_report = categorize.assign_categories(tables.establishments, tables.archive_establishments,
                                                   evaluate=evaluate)
    est["chain_size"] = categorize.chain_sizes(est["name"])
    data_report["categories"] = cat_report

    # ---- features
    ins, inf = tables.inspections, tables.infractions
    cutoff = ins.loc[ins["source"] == "current", "inspection_date"].max() - pd.DateOffset(
        months=model.VALIDATION_MONTHS)
    city_rates = _pre_cutoff_city_rates(ins, cutoff)
    states = features.build_states(ins, inf, est, city_rates=city_rates)
    pairs = features.build_training_pairs(states)
    feat_cols = features.feature_columns(states)
    log.info("Built %d inspection states and %d training pairs (%.0fs)", len(states), len(pairs), time.time() - t0)

    # ---- models
    models, model_report = model.train_models(pairs, feat_cols, evaluate=evaluate)
    model_report["city_rates_prior"] = city_rates
    latest = features.latest_states(states)
    scores = model.score(models, latest, feat_cols)
    scores["risk_band"] = model.risk_band(scores["rr_serious"])
    snapshot_cols = feat_cols  # feature values at scoring time, used by the dashboard
    feat_snapshot = latest[["est_key"] + snapshot_cols].reset_index(drop=True)
    model_report["base_rates_recent"] = {t: round(m.base_rate, 4) for t, m in models.items()}
    model_report["risk_band_counts"] = scores["risk_band"].value_counts().to_dict()

    # ---- assemble outputs
    est = summarize_establishments(est, ins, inf)
    est = est.merge(scores, on="est_key", how="left")
    est["top_factors"] = est["factors"].map(
        lambda fs: " | ".join(f"{'+' if f['effect'] > 0 else '-'} {f['factor']}: {f['detail']}" for f in fs)
        if isinstance(fs, list) else "")

    write_outputs(paths, est, ins, inf, data_report, model_report)
    export.write_dashboard_data(paths.dashboard_data / "dinesafe-data.js", est, ins, inf, feat_snapshot,
                                data_report=data_report, model_report=model_report)
    log.info("Build finished in %.0fs", time.time() - t0)
    return BuildResult(est, ins, inf, data_report, model_report)


ESTABLISHMENT_CSV_COLUMNS = [
    "est_key", "new_id", "legacy_id", "name", "address", "street", "unit", "postal_code", "fsa", "phone",
    "lat", "lon", "category", "category_source", "category_confidence", "type", "min_inspections", "chain_size",
    "in_new_system", "likely_inactive", "first_inspection", "n_inspections_total", "n_inspections_3y",
    "last_inspection_date", "last_status", "last_n_infractions", "last_has_serious", "reinspection_pending",
    "n_conditional_3y", "n_closed_3y", "n_minor_3y", "n_significant_3y", "n_crucial_3y", "fines_3y", "convicted_3y",
    "p_any", "p_serious", "p_not_pass", "rr_any", "rr_serious", "rr_not_pass", "risk_band", "top_factors",
]


def write_outputs(paths: Paths, est: pd.DataFrame, ins: pd.DataFrame, inf: pd.DataFrame,
                  data_report: dict, model_report: dict) -> None:
    def to_csv(df: pd.DataFrame, path: Path) -> None:
        tmp = path.with_name(path.name + ".tmp")
        df.to_csv(tmp, index=False, date_format="%Y-%m-%d")
        tmp.replace(path)

    to_csv(est[ESTABLISHMENT_CSV_COLUMNS], paths.processed / "establishments.csv")
    to_csv(ins, paths.processed / "inspections.csv")
    to_csv(inf, paths.processed / "infractions.csv")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (paths.reports / "data_quality.json").write_text(
        json.dumps({"generated_at": stamp, **data_report}, indent=2, default=_json_default), encoding="utf-8")
    (paths.reports / "model_report.json").write_text(
        json.dumps({"generated_at": stamp, **model_report}, indent=2, default=_json_default), encoding="utf-8")
    (paths.reports / "model_card.md").write_text(render_model_card(data_report, model_report), encoding="utf-8")
    log.info("Wrote processed tables to %s and reports to %s", paths.processed, paths.reports)


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    return str(o)


def render_model_card(data_report: dict, rep: dict) -> str:
    v = rep.get("validation", {})
    lines = [
        "# DineSafe risk model card",
        "",
        "_Generated by `python -m dinesafe build`; numbers refer to the data snapshot "
        f"`{data_report.get('snapshot')}` (inspections {data_report.get('current_first_date')} to "
        f"{data_report.get('current_last_date')})._",
        "",
        "## What it predicts",
        "",
        "For each establishment, the probability that its **next routine inspection** will have:",
        "",
        "* **any infraction** (`p_any`)",
        "* **a significant or crucial infraction** (`p_serious`), which also drives the risk band",
        "* **a Conditional Pass or Closed result** (`p_not_pass`)",
        "",
        "Follow-up re-inspections (within 30 days after a Conditional Pass or Closed) are not "
        "prediction targets. They are scheduled checks that almost always pass.",
        "",
        "## Data and features",
        "",
        f"* Training pairs: {rep.get('n_pairs', 0):,} (state after an inspection paired with the next "
        "routine inspection's outcome)",
        "* Features use only information available at the time: past inspection counts, "
        "smoothed and recency-weighted infraction rates, results and infraction categories of recent "
        "inspections, inspection frequency, the City's minimum-inspections tier (pre-2024 "
        "establishments), establishment category, chain size",
        "* Pass/fail history comes from the current feed only (the historical archive barely "
        "records Conditional Pass / Closed); severity history goes back to 2001 for linked "
        "establishments",
        "",
        "## Out-of-time validation",
        "",
        f"Trained on targets from {v.get('train_period', ['?', '?'])[0]} to {v.get('train_period', ['?', '?'])[1]} "
        f"({v.get('n_train', 0):,} rows) and evaluated on {v.get('valid_period', ['?', '?'])[0]} to "
        f"{v.get('valid_period', ['?', '?'])[1]} ({v.get('n_valid', 0):,} rows). Lower Brier / log loss is "
        "better; ROC AUC 0.5 = no better than chance.",
        "",
    ]
    for target in features.TARGETS:
        res = rep.get(target, {})
        if "models" not in res:
            continue
        lines += [f"### {res['label']}  (chosen: {res['chosen_model']})", "",
                  "| model | ROC AUC | avg precision | Brier | log loss | calib. error | mean pred | observed |",
                  "|---|---|---|---|---|---|---|---|"]
        for name, m in res["models"].items():
            lines.append(f"| {name} | {m.get('roc_auc', float('nan')):.3f} | {m.get('average_precision', float('nan')):.3f} "
                         f"| {m['brier']:.4f} | {m['log_loss']:.4f} | {m['ece']:.3f} | {m['mean_prediction']:.3f} "
                         f"| {m['observed_rate']:.3f} |")
        lines.append("")
        if res.get("calibration"):
            lines += ["Calibration by decile of predicted risk (validation period):", "",
                      "| decile | n | mean predicted | observed |", "|---|---|---|---|"]
            for row in res["calibration"]:
                lines.append(f"| {row['bin'] + 1} | {row['n']:,} | {row['mean_predicted']:.3f} | {row['observed']:.3f} |")
            lines.append("")
    lines += [
        "## Risk bands",
        "",
        "Bands use the predicted chance of a significant/crucial infraction relative to the recent "
        f"city-wide rate ({rep.get('base_rates_recent', {}).get('y_serious', float('nan')):.1%}): "
        "Low < 0.75x, Average 0.75 to 1.25x, Elevated 1.25 to 2x, High >= 2x.",
        "",
        "Current counts: " + ", ".join(f"{k}: {v:,}" for k, v in rep.get("risk_band_counts", {}).items()),
        "",
        "## Limitations",
        "",
        "* These are statistical estimates from past inspection records, **not** official "
        "ratings. The official result is the latest inspection status posted by Toronto Public Health.",
        "* Establishments that close permanently disappear from the City's feed, so the training "
        "data over-represents survivors.",
        "* Inspection practice changed with the late-2025 system migration (Conditional Pass rates "
        "went up). Recent data is weighted more heavily (one-year half-life), but some drift remains.",
        "* Categories for establishments without an archive record are inferred from their name "
        f"(grouped cross-validated accuracy "
        f"{data_report.get('categories', {}).get('cross_validation', {}).get('accuracy_at_threshold', float('nan')):.0%}"
        " on confident predictions).",
        "",
    ]
    return "\n".join(lines)


def run_metrics_summary(result: BuildResult) -> str:
    rep = result.model_report
    parts = []
    for t in features.TARGETS:
        m = rep.get(t, {}).get("models", {}).get(rep.get(t, {}).get("chosen_model", ""), {})
        if m:
            parts.append(f"{rep[t]['label']}: AUC {m.get('roc_auc', float('nan')):.3f}")
    return "; ".join(parts)
