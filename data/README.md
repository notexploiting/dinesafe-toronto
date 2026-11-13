# Data

Everything in this folder is generated. Run `python -m dinesafe run` to (re)create it.

## raw/

| File | Contents |
|---|---|
| `dinesafe_YYYYMMDD_HHMMSS.csv` | A snapshot of the City's current DineSafe feed (datastore dump, one row per infraction, or one row per inspection with no infractions) |
| `dinesafe_YYYYMMDD_HHMMSS.csv.manifest.json` | Where and when it was downloaded, SHA-256, row count, the City's refresh timestamp |
| `historical/dinesafe_hist_YYYY.csv` | Yearly archive files 2001-2023 (pre-migration schema) |
| `historical/archive.zip.manifest.json` | Manifest of the archive download |

## processed/

Read ID columns (`est_key`, `new_id`, `legacy_id`) as **text**. Legacy IDs are numeric, new IDs are not.
Dates are `YYYY-MM-DD`. "3y" columns cover the City's current feed (about the last three years).

### establishments.csv (one row per establishment)

| Column | Meaning |
|---|---|
| `est_key` | Canonical ID: the legacy DineSafe ID if the establishment has one, otherwise the new-system ID |
| `new_id`, `legacy_id` | IDs in the City's new (Nov 2025+) and old systems |
| `name`, `address`, `street`, `unit`, `postal_code`, `fsa`, `phone`, `lat`, `lon` | Cleaned contact/location fields (`fsa` = first 3 characters of the postal code) |
| `category` | Restaurant & take-out, Bar & club, Bakery, Grocery & retail, Catering & banquet, Institutional, Food processing, or Unknown |
| `category_source`, `category_confidence` | `archive` (from the historical type), `inferred` (name classifier, with its confidence) or `unknown` |
| `type` | Detailed establishment type from the archive (e.g. "Food Take Out"), if linked |
| `min_inspections` | City-assigned minimum inspections per year (1-3, a risk tier) from the archive, if linked |
| `chain_size` | Number of establishments sharing the same (normalized) name |
| `in_new_system`, `likely_inactive` | Whether it was carried into the City's new system; if not, it has probably closed |
| `first_inspection`, `n_inspections_total` | Earliest inspection and total inspections incl. the archive |
| `last_inspection_date`, `last_status`, `last_n_infractions`, `last_has_serious` | Most recent inspection |
| `reinspection_pending` | Latest result was Conditional Pass/Closed and no re-inspection is recorded yet |
| `n_inspections_3y`, `n_conditional_3y`, `n_closed_3y`, `n_minor_3y`, `n_significant_3y`, `n_crucial_3y`, `fines_3y`, `convicted_3y` | Current-feed summary |
| `p_any`, `p_serious`, `p_not_pass` | Model estimates for the next routine inspection: any infraction / significant or crucial infraction / Conditional Pass or Closed |
| `rr_any`, `rr_serious`, `rr_not_pass` | The same divided by the recent city-wide rate |
| `risk_band` | Low / Average / Elevated / High, based on `rr_serious` |
| `top_factors` | Factor groups that raise (+) or lower (-) `p_serious`, with the facts behind them |

### inspections.csv (one row per establishment and inspection date)

| Column | Meaning |
|---|---|
| `est_key`, `inspection_date` | Key |
| `source` | `current` (City feed) or `archive` (2001-2023 files, only kept before the current feed starts) |
| `status` | Pass, Conditional Pass, Closed, Temporarily Not Operating. Archive statuses are unreliable |
| `n_infractions`, `n_minor`, `n_significant`, `n_crucial`, `n_not_applicable` | Infraction counts by severity |
| `has_infraction`, `has_serious`, `not_pass` | Flags (serious = significant or crucial) |
| `fine_total`, `has_conviction` | Prosecution results attached to the inspection |
| `days_since_previous` | Days since the establishment's previous inspection |
| `is_followup` | Re-inspection within 30 days after a Conditional Pass/Closed |

### infractions.csv (one row per infraction)

| Column | Meaning |
|---|---|
| `est_key`, `inspection_date` | Links to inspections.csv |
| `source` | `current` or `archive` |
| `infraction` | Infraction text (spacing/case variants merged) |
| `category` | Food temperature control, Food protection & contamination, Hand washing & staff hygiene, Food-contact surfaces & dishwashing, Cleaning & maintenance, Washrooms & sanitary facilities, Waste storage & removal, Pest control, Closure conditions, Administrative |
| `severity` | Crucial, Significant, Minor, Not Applicable |
| `action` | Enforcement action (archive only; the current feed no longer publishes it) |
| `outcome`, `outcome_date`, `fine` | Prosecution outcome, if any |
