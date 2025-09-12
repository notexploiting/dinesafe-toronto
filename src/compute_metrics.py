import pandas as pd
import numpy as np
from pathlib import Path

# Set up paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
REPORTS_DIR = PROJECT_ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

# Find latest processed file
csv_files = list(PROCESSED_DIR.glob("dinesafe_*.csv"))
if not csv_files:
    raise FileNotFoundError(f"No processed DineSafe CSV files found in {PROCESSED_DIR.resolve()}")
latest_file = max(csv_files, key=lambda f: f.stat().st_mtime)
print(f"Loading {latest_file.name}")
df = pd.read_csv(latest_file)

# Ensure types
if "Inspection Date" in df:
    df["Inspection Date"] = pd.to_datetime(df["Inspection Date"], errors="coerce")
for col in ["has_infraction", "is_serious", "was_fined"]:
    if col in df:
        df[col] = df[col].fillna(0).astype(int)

bullets = []

# 1. Pareto: Top X% account for Y% of infractions
est = (df.groupby("Establishment ID")
         .agg(name=("Establishment Name","first"),
              infractions=("has_infraction","sum"),
              serious=("is_serious","sum"),
              inspections=("Inspection ID","count"))
         .reset_index())
est = est.sort_values("infractions", ascending=False).reset_index(drop=True)
est["cum_infractions"] = est["infractions"].cumsum()
total_inf = est["infractions"].sum()
est["cum_share"] = est["cum_infractions"] / total_inf if total_inf else 0
n = len(est)
top10_cut = int(np.ceil(0.10 * n))
top20_cut = int(np.ceil(0.20 * n))
top10_share = est.loc[:top10_cut-1, "infractions"].sum() / total_inf if total_inf else 0
top20_share = est.loc[:top20_cut-1, "infractions"].sum() / total_inf if total_inf else 0
bullets.append(f"Identified concentration of risk: top 10% of establishments account for {top10_share:.1%} of all recorded infractions; top 20% account for {top20_share:.1%}.")

# 2. Repeat offenders & high-severity contribution
repeat_mask = est["inspections"] > 1
repeat_share = est.loc[repeat_mask, "infractions"].sum() / total_inf if total_inf else 0
repeat_pct = repeat_mask.mean()
serious_share = df["is_serious"].sum() / len(df) if len(df) else 0
bullets.append(f"Repeat-inspection venues ({repeat_pct:.1%} of establishments) contribute {repeat_share:.1%} of total infractions.")
bullets.append(f"Serious infractions represent {serious_share:.1%} of inspections citywide.")

# 3. COVID impact (monthly volume & serious rate)
if "Inspection Date" in df:
    monthly = (df.dropna(subset=["Inspection Date"])
                 .assign(Period=lambda x: x["Inspection Date"].values.astype("datetime64[M]"))
                 .groupby("Period")
                 .agg(inspections=("Inspection ID","count"),
                      serious=("is_serious","mean"))
                 .reset_index())
    pre = monthly[(monthly["Period"] >= "2018-01") & (monthly["Period"] < "2020-03")]
    cov = monthly[(monthly["Period"] >= "2020-03") & (monthly["Period"] < "2022-03")]
    post = monthly[monthly["Period"] >= "2022-03"]
    def safe_mean(s): 
        return float(s.mean()) if len(s) else np.nan
    pre_i, cov_i, post_i = map(safe_mean, [pre["inspections"], cov["inspections"], post["inspections"]])
    pre_s, cov_s, post_s = map(safe_mean, [pre["serious"], cov["serious"], post["serious"]])
    if not np.isnan(pre_i) and not np.isnan(cov_i):
        vol_drop = (cov_i - pre_i) / pre_i
        bullets.append(f"Monthly inspection volume changed {vol_drop:+.1%} during COVID vs. pre-COVID baseline.")
    if not np.isnan(pre_s) and not np.isnan(cov_s):
        serious_shift = (cov_s - pre_s)
        bullets.append(f"Serious-infraction rate shifted {serious_shift:+.1%} during COVID vs. pre-COVID.")
    if not np.isnan(post_i) and not np.isnan(pre_i):
        post_recovery = (post_i - pre_i) / pre_i
        bullets.append(f"Post-COVID inspection volume is {post_recovery:+.1%} vs. pre-COVID baseline.")

# 4. Establishment-type risk
if "Establishment Type" in df:
    type_serious = (df.groupby("Establishment Type")
                      .agg(inspections=("Inspection ID","count"),
                           serious=("is_serious","sum"))
                      .assign(serious_rate=lambda x: x["serious"]/x["inspections"])
                      .sort_values("serious", ascending=False))
    top_type = type_serious.head(1)
    if len(top_type):
        t_name = top_type.index[0]
        t_share = top_type["serious"].iloc[0] / df["is_serious"].sum() if df["is_serious"].sum() else 0
        bullets.append(f"One category ('{t_name}') contributes {t_share:.1%} of all serious infractions.")

# 5. Inspection cadence
if "Inspection Date" in df:
    def gaps_days(g):
        s = g.sort_values().diff().dt.days.dropna()
        return s
    gaps = (df.dropna(subset=["Inspection Date"])
              .groupby("Establishment ID")["Inspection Date"]
              .apply(gaps_days))
    median_gap = float(gaps.median()) if len(gaps) else np.nan
    p90_gap = float(gaps.quantile(0.90)) if len(gaps) else np.nan
    if not np.isnan(median_gap):
        bullets.append(f"Median time between inspections across repeat-visit venues is {median_gap:.0f} days (90th percentile: {p90_gap:.0f} days).")

# 6. Enforcement outcomes
fined_rate = df["was_fined"].mean() if len(df) else 0
avg_fine_all = df["Amount Fined"].fillna(0).mean() if "Amount Fined" in df else 0
avg_fine_if_fined = df.loc[df["was_fined"]==1, "Amount Fined"].mean() if (df["was_fined"]==1).any() else np.nan
bullets.append(f"Fines issued in {fined_rate:.1%} of inspections; mean fine across all visits ${avg_fine_all:,.0f}" + (f"; mean fine when issued ${avg_fine_if_fined:,.0f}" if not np.isnan(avg_fine_if_fined) else "."))

# Clean and save
bullets_clean = [b.replace("~0.0%", "~0%") for b in dict.fromkeys(bullets)]
print("\n".join(f"• {b}" for b in bullets_clean))
out = REPORTS_DIR / "metrics.txt"
out.write_text("\n".join(f"- {b}" for b in bullets_clean), encoding="utf-8")
print(f"\nSaved: {out}")
