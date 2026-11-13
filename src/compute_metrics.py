"""
compute_metrics.py

Headline metrics for reporting (resume bullets, slides, the Excel workbook), computed from
the tables written by `python -m dinesafe build`:

    data/processed/inspections.csv     one row per inspection
    data/processed/infractions.csv     one row per infraction
    data/processed/establishments.csv  one row per establishment

Usage:
    $ python src/compute_metrics.py

Output:
    reports/metrics.txt

"Recent" metrics use the City's current feed (about the last 3 years). The COVID comparison
uses the 2001-2023 archive (for establishments still operating today) and is based on
severities, since the archive does not record Conditional Pass / Closed results reliably.
"""

from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REPORTS_DIR = PROJECT_ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

KEY_DTYPES = {"est_key": str, "new_id": str, "legacy_id": str, "postal_code": str}


def load(name: str, dates: list[str]) -> pd.DataFrame:
    path = PROCESSED_DIR / f"{name}.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - run `python -m dinesafe run` first")
    return pd.read_csv(path, dtype=KEY_DTYPES, parse_dates=dates, low_memory=False)


ins = load("inspections", ["inspection_date"])
inf = load("infractions", ["inspection_date"])
est = load("establishments", ["last_inspection_date", "first_inspection"])

recent = ins[ins["source"] == "current"]
recent_inf = inf[inf["source"] == "current"]
bullets = []
period = f"{recent['inspection_date'].min():%b %Y} to {recent['inspection_date'].max():%b %Y}"
bullets.append(f"Period analysed: {period}: {len(recent):,} inspections of {recent['est_key'].nunique():,} "
               f"establishments, {len(recent_inf):,} infractions.")

# 1. Concentration of risk (Pareto)
per_est = recent_inf.groupby("est_key").size().reindex(recent["est_key"].unique(), fill_value=0)
per_est = per_est.sort_values(ascending=False)
total = per_est.sum()
n = len(per_est)
top10 = per_est.iloc[: int(np.ceil(0.10 * n))].sum() / total if total else 0
top20 = per_est.iloc[: int(np.ceil(0.20 * n))].sum() / total if total else 0
bullets.append(f"Concentration of risk: the top 10% of establishments account for {top10:.1%} of all recorded "
               f"infractions; the top 20% account for {top20:.1%}.")

# 2. Inspection outcomes
routine = recent[~recent["is_followup"]]
bullets.append(f"{routine['has_infraction'].mean():.1%} of routine inspections found at least one infraction; "
               f"{routine['has_serious'].mean():.1%} found a significant or crucial one; "
               f"{routine['not_pass'].mean():.1%} ended in a Conditional Pass or closure.")
fu = recent[recent["is_followup"]]
if len(fu):
    bullets.append(f"{(fu['status'] == 'Pass').mean():.1%} of follow-up re-inspections (median "
                   f"{fu['days_since_previous'].median():.0f} days after a failed inspection) ended in a Pass.")

# 3. Repeat offenders
serious_counts = routine.groupby("est_key")["has_serious"].sum()
repeat = (serious_counts >= 2).sum()
bullets.append(f"{repeat:,} establishments ({repeat / max(len(serious_counts), 1):.1%}) had significant or crucial "
               "infractions at two or more routine inspections.")

# 4. COVID impact (monthly inspection volume and serious-infraction rate). Archive rows are
# only kept for establishments that are still in the current feed, so this compares the same
# surviving establishments before and during COVID. A post-COVID volume comparison would be
# inflated by establishments that opened later, so it is not reported.
hist = ins[ins["inspection_date"] < "2022-03-01"]
monthly = (hist.assign(month=hist["inspection_date"].dt.to_period("M"))
               .groupby("month").agg(inspections=("est_key", "size"), serious=("has_serious", "mean")))
pre = monthly.loc[pd.Period("2018-01", "M"):pd.Period("2020-02", "M")]
cov = monthly.loc[pd.Period("2020-03", "M"):pd.Period("2022-02", "M")]
if len(pre) and len(cov):
    bullets.append(f"Among establishments still operating today, monthly inspection volume changed "
                   f"{(cov['inspections'].mean() - pre['inspections'].mean()) / pre['inspections'].mean():+.1%} during COVID "
                   f"(Mar 2020 to Feb 2022) vs. 2018-2019; the share of inspections with a significant/crucial "
                   f"infraction went from {pre['serious'].mean():.1%} to {cov['serious'].mean():.1%}.")

# 5. Establishment categories
cat = (routine.merge(est[["est_key", "category"]], on="est_key")
              .groupby("category").agg(inspections=("est_key", "size"), serious_rate=("has_serious", "mean")))
cat = cat[cat["inspections"] >= 200].sort_values("serious_rate", ascending=False)
if len(cat):
    hi, lo = cat.index[0], cat.index[-1]
    bullets.append(f"Highest serious-infraction rate by category: {hi} ({cat.loc[hi, 'serious_rate']:.1%}); "
                   f"lowest: {lo} ({cat.loc[lo, 'serious_rate']:.1%}).")

# 6. Inspection cadence
gaps = routine["days_since_previous"].dropna()
if len(gaps):
    bullets.append(f"Median time between routine inspections: {gaps.median():.0f} days "
                   f"(90th percentile: {gaps.quantile(0.9):.0f} days).")

# 7. Infraction types
top_cats = recent_inf["category"].value_counts(normalize=True).head(3)
bullets.append("Most common infraction categories: " +
               ", ".join(f"{c} ({s:.0%})" for c, s in top_cats.items()) + ".")
pest_share = recent.merge(recent_inf[recent_inf["category"] == "Pest control"][["est_key", "inspection_date"]]
                          .drop_duplicates(), on=["est_key", "inspection_date"]).shape[0] / len(recent)
bullets.append(f"{pest_share:.1%} of inspections cited a pest-control infraction.")

# 8. Enforcement
fined = recent_inf[recent_inf["fine"].fillna(0) > 0]
if len(fined):
    bullets.append(f"{len(fined):,} infractions led to fines (median ${fined['fine'].median():,.0f}, "
                   f"total ${fined['fine'].sum():,.0f}).")

text = "\n".join(f"- {b}" for b in bullets)
print(text)
out = REPORTS_DIR / "metrics.txt"
out.write_text(text + "\n", encoding="utf-8")
print(f"\nSaved: {out}")
