"""Cleaning and normalization of the raw DineSafe files into three tidy tables.

Data-quality issues handled here (all observed in the real data, Nov 2026):

Current feed (post-migration schema)
* the string ``"None"`` is used instead of empty values in most columns
* addresses are ``"<street> <unit|None> <postal code|None>"``, e.g.
  ``"1871 O'Connor Dr None M4A 1X1"``; a few postal codes are truncated (``"M1P 22"``)
* **duplicate establishments**: after the late-2025 system migration, ~1,300
  establishments appear under both their legacy numeric ID (``estId == oldEstId``,
  upper-case address) *and* a new Salesforce-style ID, with the same inspections listed
  twice. They are merged on a canonical key and duplicate inspections are dropped
* there is no inspection ID; an inspection is identified by (establishment, date). The
  status is consistent within that grouping
* infraction text differs only by trailing whitespace / capitalization
* outcome spelled two ways (``"Conviction - Fined"`` / ``"Conviction: Fined"``)
* placeholder phone numbers (``0000000000``, ``4160000000``)
* ``deficiencyDesc`` mixes two coding schemes (``"5C. ..."`` and ``"05. ..."``); the
  leading digit is the same top-level category in both, which is what we keep

Historical archive (2001-2023, pre-migration schema)
* the 2020-2022 files have a stray ``"`` at the start of the header and the end of the
  final line, which breaks standard CSV parsers
* 2023 uses ``MM/DD/YYYY`` dates, earlier years ISO dates
* some names contain embedded newlines
* the archive almost never records Conditional Pass and never Closed (0.4% of rows vs
  ~16% in the current feed), so archive *statuses* are not used for modelling;
  severities look consistent and are used
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from dinesafe.config import CURRENT_REQUIRED_COLUMNS, FOLLOWUP_WINDOW_DAYS, HISTORICAL_COLUMNS, TORONTO_BBOX

log = logging.getLogger(__name__)

NULL_TOKENS = {"", "none", "null", "nan", "n/a", "undefined"}

SEVERITY_MAP = {
    "M - MINOR": "Minor",
    "S - SIGNIFICANT": "Significant",
    "C - CRUCIAL": "Crucial",
    "NA - NOT APPLICABLE": "Not Applicable",
    "NA": "Not Applicable",
}
SEVERITY_ORDER = ["Crucial", "Significant", "Minor", "Not Applicable", "Unknown"]

STATUS_RANK = {"Closed": 3, "Conditional Pass": 2, "Pass": 1, "Temporarily Not Operating": 0}
NOT_PASS_STATUSES = {"Conditional Pass", "Closed"}

OUTCOME_MAP = {"CONVICTION: FINED": "Conviction - Fined"}

CATEGORY_LABELS = {
    "1": "Food temperature control",
    "2": "Food protection & contamination",
    "3": "Hand washing & staff hygiene",
    "4": "Food-contact surfaces & dishwashing",
    "5": "Cleaning & maintenance",
    "6": "Washrooms & sanitary facilities",
    "7": "Waste storage & removal",
    "8": "Pest control",
    "9": "Closure conditions",
    "A": "Administrative",
    "O": "Other",
}

# Keyword rules used (a) to put anything about pests/animals under "Pest control" even
# though the City files e.g. "Fail to protect against entry of pests" under premises
# maintenance, and (b) for archive infractions that have no deficiency code at all.
# Order matters: the first match wins.
PEST_RE = re.compile(r"pest|rodent|insect|cockroach|\bmice\b|\brats?\b|vermin|harbou?r|animal", re.IGNORECASE)
_CATEGORY_KEYWORDS = [
    ("8", PEST_RE.pattern),
    ("3", r"hand ?wash|wash(es|ing)? (their )?hands|headgear|hair|outer garment|hygien|soap|detergent|towel"),
    ("1", r"temperature|thermometer|hazardous food|frozen|reheat|thaw|refrigerat|cool(ed|ing)? "),
    ("6", r"wash ?room|toilet|lavator|sanitary facilit|urinal"),
    ("7", r"garbage|waste|refuse"),
    ("A", r"certificate|food handler|record|permit|licen[cs]e|photo identification|municipal code|"
          r"post(ed)? |display|notice|sign(age)?|obstruct|insp(ector)?|presence of|food safety plan|report"),
    ("2", r"contamina|adulterat|protect.*food|food.*protect|potable water|\bice\b|cover|rack|shel(f|ves)|pallet|"
          r"store food|storage|procedure|approved|ungraded|eggs?\b|meat|napkin|serviette|toxic|poison|pre-?packaged"),
    ("4", r"saniti[sz]|utensil|dish|mechanical (dish)?washer|food[- ]contact|equipment|cutting board|sink|"
          r"water under pressure|running water|cloth|chipped|cracked|scoop"),
    ("5", r"floor|wall|ceiling|clean|maintain|repair|light|illuminat|ventilat|premise|surface|not regularly used|"
          r"unsanitary|sanitary condition"),
]
_CATEGORY_KEYWORDS = [(code, re.compile(rx, re.IGNORECASE)) for code, rx in _CATEGORY_KEYWORDS]

POSTAL_RE = re.compile(r"^([A-Z]\d[A-Z]) ?(\d[A-Z]\d)$")
# "<street> <unit|None> <postal|None>"; the postal alternative also accepts truncated codes
# ("M1P 22") so they don't get mistaken for a unit number.
ADDRESS_RE = re.compile(r"^(?P<street>.+?)\s+(?P<unit>\S+)\s+(?P<postal>[A-Za-z]\d[A-Za-z](?: ?\w{1,3})?|None)$")
LEGACY_ID_RE = r"\d+"


@dataclass
class CleanTables:
    establishments: pd.DataFrame
    inspections: pd.DataFrame
    infractions: pd.DataFrame
    archive_establishments: pd.DataFrame
    report: dict = field(default_factory=dict)


# =============================================================================== I/O
def read_current(path: Path) -> pd.DataFrame:
    """Read a current-feed snapshot with every column as a string (no NA guessing)."""
    df = pd.read_csv(path, dtype="string", keep_default_na=False, encoding="utf-8-sig")
    missing = [c for c in CURRENT_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} is missing columns {missing}")
    return df


def repair_csv_text(text: str) -> tuple[str, int]:
    """Fix the stray quote characters found in some historical files.

    Only two very specific defects are repaired (and counted):
    * a header line beginning with ``""`` (an extra leading quote)
    * a final line ending in three quote characters (one too many)
    Anything else is left for the CSV parser to judge.
    """
    lines = text.splitlines()
    fixes = 0
    if lines and lines[0].startswith('""') and lines[0].count('"') % 2:
        lines[0] = lines[0][1:]
        fixes += 1
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].endswith('"""') and lines[-1].count('"') % 2:
        lines[-1] = lines[-1][:-1]
        fixes += 1
    return "\n".join(lines) + "\n", fixes


def read_historical(files: list[Path]) -> tuple[pd.DataFrame, dict]:
    frames, repairs = [], {}
    for f in files:
        text = Path(f).read_text(encoding="utf-8-sig", errors="replace")
        text, fixes = repair_csv_text(text)
        if fixes:
            repairs[Path(f).name] = fixes
        df = pd.read_csv(io.StringIO(text), dtype="string", keep_default_na=False)
        df.columns = [c.strip() for c in df.columns]
        missing = [c for c in HISTORICAL_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"{Path(f).name} is missing columns {missing}")
        df["source_file"] = Path(f).name
        frames.append(df[HISTORICAL_COLUMNS + ["source_file"]])
    if not frames:
        return pd.DataFrame(columns=HISTORICAL_COLUMNS + ["source_file"], dtype="string"), repairs
    if repairs:
        log.info("Repaired malformed quoting in historical files: %s", repairs)
    return pd.concat(frames, ignore_index=True), repairs


# ====================================================================== field helpers
def normalize_text(s: pd.Series) -> pd.Series:
    """Trim, collapse internal whitespace/newlines, and turn null tokens into <NA>."""
    s = s.astype("string").str.replace(r"\s+", " ", regex=True).str.strip()
    return s.mask(s.str.lower().isin(NULL_TOKENS))


def parse_date(s: pd.Series) -> pd.Series:
    """Parse ISO (YYYY-MM-DD) or US (MM/DD/YYYY) dates; anything else becomes NaT."""
    s = normalize_text(s)
    iso = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
    us = pd.to_datetime(s, format="%m/%d/%Y", errors="coerce")
    return iso.fillna(us)


def normalize_postal(s: pd.Series) -> pd.Series:
    s = normalize_text(s).str.upper().str.replace(" ", "", regex=False)
    ok = s.str.fullmatch(r"[A-Z]\d[A-Z]\d[A-Z]\d").fillna(False).astype(bool)
    out = (s.str[:3] + " " + s.str[3:]).where(ok)
    return out.astype("string")


def parse_address(s: pd.Series) -> pd.DataFrame:
    """Split ``"<street> <unit|None> <postal|None>"`` into street / unit / postal code."""
    s = normalize_text(s)
    parts = s.str.extract(ADDRESS_RE)
    parsed = parts["street"].notna()
    street = parts["street"].where(parsed, s)
    unit = normalize_text(parts["unit"].where(parsed))
    postal = normalize_postal(parts["postal"].where(parsed))
    # Strip any leftover "None" tokens and legacy ", Unit-0" style suffixes from the street.
    street = (street.str.replace(r"\b(None|undefined)\b", " ", regex=True)
                    .str.replace(r",?\s*Unit-0\b", "", regex=True)
                    .str.replace(r"\s+", " ", regex=True).str.strip(" ,"))
    unit = unit.str.replace(r"^(Unit|Bldg|Flr|Ste|Suite)-+$", "", regex=True)  # "Bldg--" -> empty
    unit = unit.str.replace(r"^(Unit|Bldg|Flr|Ste|Suite)-0$", "", regex=True)
    unit = unit.str.replace("-", " ", n=1, regex=False).str.strip()
    unit = unit.mask((unit == "").fillna(False).astype(bool))
    return pd.DataFrame({"street": street.astype("string"), "unit": unit.astype("string"),
                         "postal_code": postal}, index=s.index)


def smart_title(s: pd.Series) -> pd.Series:
    """Title-case ALL-CAPS street names from the legacy system ("1 YONGE ST" -> "1 Yonge St")."""
    upper = s.str.fullmatch(r"[^a-z]*").fillna(False).astype(bool)
    titled = s.str.title().str.replace(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), regex=True)
    return s.where(~upper, titled)


def format_address(street: pd.Series, unit: pd.Series, postal: pd.Series) -> pd.Series:
    out = smart_title(street.fillna(""))
    out = out + unit.fillna("").map(lambda u: f", {u}" if u else "")
    out = out + ", Toronto ON"
    out = out + postal.fillna("").map(lambda p: f" {p}" if p else "")
    return out.astype("string").str.strip(", ")


def clean_phone(s: pd.Series) -> pd.Series:
    digits = normalize_text(s).str.replace(r"\D", "", regex=True)
    eleven = ((digits.str.len() == 11) & digits.str.startswith("1")).fillna(False).astype(bool)
    digits = digits.where(~eleven, digits.str[1:])
    valid = (digits.str.len() == 10) & ~digits.str.fullmatch(r"(\d)\1{9}") & ~digits.str.endswith("0000000")
    valid = valid.fillna(False).astype(bool)
    out = ("(" + digits.str[:3] + ") " + digits.str[3:6] + "-" + digits.str[6:]).where(valid)
    return out.astype("string")


def normalize_severity(s: pd.Series) -> pd.Series:
    s = normalize_text(s)
    up = s.str.upper()
    mapped = up.map(SEVERITY_MAP)
    # Unknown spellings: fall back on the leading letter code.
    lead = up.str.extract(r"^(NA|[MSC])\b", expand=False).map(
        {"M": "Minor", "S": "Significant", "C": "Crucial", "NA": "Not Applicable"})
    return mapped.fillna(lead).astype("string")


def normalize_outcome(s: pd.Series) -> pd.Series:
    s = normalize_text(s)
    return s.str.upper().map(OUTCOME_MAP).fillna(s).astype("string")


def deficiency_code(s: pd.Series) -> pd.Series:
    """Top-level deficiency category code from ``deficiencyDesc`` ('1'..'9', 'A')."""
    s = normalize_text(s)
    digit = s.str.extract(r"^0?([1-9])[A-Z]?\.", expand=False)
    admin = s.str.contains(r"^[A-D]\.|ADMINISTRATIVE|HEALTH PROTECTION", case=False, regex=True, na=False)
    return digit.where(digit.notna(), pd.Series(np.where(admin, "A", pd.NA), index=s.index)).astype("string")


def keyword_category(text: pd.Series) -> pd.Series:
    """Best-effort category for infraction text without a deficiency code."""
    uniq = text.dropna().unique()
    lookup = {}
    for t in uniq:
        lookup[t] = next((code for code, rx in _CATEGORY_KEYWORDS if rx.search(t)), "O")
    return text.map(lookup).astype("string")


def canonical_text(s: pd.Series) -> pd.Series:
    """Collapse variants of the same infraction text that differ only in case/spacing.

    The most frequent spelling of each variant group is used for display.
    """
    s = normalize_text(s)
    key = s.str.casefold()
    counts = pd.DataFrame({"key": key, "text": s}).dropna().value_counts().reset_index(name="n")
    best = counts.sort_values(["key", "n", "text"], ascending=[True, False, True]).drop_duplicates("key")
    return key.map(dict(zip(best["key"], best["text"]))).astype("string")


def valid_coords(lat: pd.Series, lon: pd.Series) -> pd.Series:
    b = TORONTO_BBOX
    return lat.between(b["lat_min"], b["lat_max"]) & lon.between(b["lon_min"], b["lon_max"])


# ==================================================================== current feed
def clean_current(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Return (infraction-level records, establishment attributes, report)."""
    report: dict = {"current_rows_in": int(len(raw))}
    df = pd.DataFrame(index=raw.index)
    df["source_est_id"] = normalize_text(raw["estId"])
    df["legacy_id"] = normalize_text(raw["oldEstId"])
    df["is_legacy_row"] = df["source_est_id"].str.fullmatch(LEGACY_ID_RE).fillna(False).astype(bool)
    # Legacy rows carry estId == oldEstId; new rows carry the legacy id in oldEstId when
    # the establishment existed before the migration. Canonical key: legacy id if known.
    df["est_key"] = df["legacy_id"].fillna(df["source_est_id"])
    df["name"] = normalize_text(raw["estName"])
    df["address_raw"] = normalize_text(raw["address"])
    df["phone"] = clean_phone(raw["phone"])
    df["inspection_date"] = parse_date(raw["inspectionDate"])
    df["status"] = normalize_text(raw["inspectionStatus"])
    df["infraction"] = normalize_text(raw["typeDesc"])
    df["deficiency"] = normalize_text(raw["deficiencyDesc"])
    df["severity"] = normalize_severity(raw["severity"])
    df["action"] = pd.Series(pd.NA, index=raw.index, dtype="string")  # not published any more
    df["outcome"] = normalize_outcome(raw["OutcomeDesc"])
    df["outcome_date"] = parse_date(raw["OutcomeDate"])
    df["fine"] = pd.to_numeric(normalize_text(raw["amountFined"]), errors="coerce")
    df["lat"] = pd.to_numeric(normalize_text(raw["latitude"]), errors="coerce")
    df["lon"] = pd.to_numeric(normalize_text(raw["longitude"]), errors="coerce")

    bad_date = df["inspection_date"].isna() | df["est_key"].isna()
    report["current_rows_dropped_bad_date_or_id"] = int(bad_date.sum())
    df = df[~bad_date].copy()

    bad_xy = ~valid_coords(df["lat"], df["lon"]).fillna(False).astype(bool)
    report["current_rows_bad_coordinates"] = int(bad_xy.sum())
    df.loc[bad_xy, ["lat", "lon"]] = np.nan

    # ---- merge legacy/new duplicates: one source establishment per inspection
    report["current_establishments_with_legacy_and_new_ids"] = int(
        (df.groupby("est_key")["is_legacy_row"].nunique() > 1).sum())
    per_src = (df.groupby(["est_key", "inspection_date", "source_est_id"], observed=True)
                 .agg(n=("name", "size"), legacy=("is_legacy_row", "first")).reset_index())
    per_src = per_src.sort_values(["est_key", "inspection_date", "legacy", "n", "source_est_id"],
                                  ascending=[True, True, True, False, True])
    keep = per_src.drop_duplicates(["est_key", "inspection_date"])
    report["current_duplicate_inspections_removed"] = int(len(per_src) - len(keep))
    before = len(df)
    df = df.merge(keep[["est_key", "inspection_date", "source_est_id"]],
                  on=["est_key", "inspection_date", "source_est_id"], how="inner")
    report["current_duplicate_rows_removed"] = int(before - len(df))

    content = ["est_key", "inspection_date", "status", "infraction", "deficiency", "severity",
               "outcome", "outcome_date", "fine"]
    dup = df.duplicated(content)
    report["current_exact_duplicate_rows_removed"] = int(dup.sum())
    df = df[~dup]

    # ---- establishment attributes: latest row, preferring the new system's record
    order = df.sort_values(["est_key", "is_legacy_row", "inspection_date"], ascending=[True, True, False])
    latest = order.drop_duplicates("est_key").set_index("est_key")
    addr = parse_address(latest["address_raw"])
    phone = (order.dropna(subset=["phone"]).drop_duplicates("est_key").set_index("est_key")["phone"])
    new_ids = order[~order["is_legacy_row"]].drop_duplicates("est_key").set_index("est_key")["source_est_id"]
    legacy_ids = df.dropna(subset=["legacy_id"]).drop_duplicates("est_key").set_index("est_key")["legacy_id"]
    est = pd.DataFrame(index=latest.index)
    est["new_id"] = new_ids.reindex(est.index).astype("string")
    est["legacy_id"] = legacy_ids.reindex(est.index).astype("string")
    est["in_new_system"] = est["new_id"].notna()
    est["name"] = latest["name"]
    est["street"] = addr["street"]
    est["unit"] = addr["unit"]
    est["postal_code"] = addr["postal_code"]
    est["fsa"] = addr["postal_code"].str[:3]
    est["address"] = format_address(addr["street"], addr["unit"], addr["postal_code"])
    est["phone"] = phone.reindex(est.index).astype("string")
    coords = order.dropna(subset=["lat", "lon"]).drop_duplicates("est_key").set_index("est_key")
    est["lat"] = coords["lat"].reindex(est.index)
    est["lon"] = coords["lon"].reindex(est.index)
    est.index.name = "est_key"
    report["current_establishments"] = int(len(est))
    report["current_establishments_without_coordinates"] = int(est["lat"].isna().sum())
    report["current_establishments_without_postal_code"] = int(est["postal_code"].isna().sum())

    df["category_code"] = deficiency_code(df["deficiency"])
    df["source"] = "current"
    records = df[["est_key", "source", "inspection_date", "status", "infraction", "category_code", "severity",
                  "action", "outcome", "outcome_date", "fine"]].reset_index(drop=True)
    return records, est.reset_index(), report


# ============================================================== historical archive
def clean_historical(raw: pd.DataFrame, cutoff: pd.Timestamp | None) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Return (infraction-level records before ``cutoff``, archive establishment attributes, report).

    ``cutoff`` is the first date covered by the current feed; archive rows on or after it
    are dropped because the current feed is the authoritative copy for that period.
    """
    report: dict = {"archive_rows_in": int(len(raw))}
    df = pd.DataFrame(index=raw.index)
    df["est_key"] = normalize_text(raw["Establishment ID"])
    df["name"] = normalize_text(raw["Establishment Name"])
    df["type"] = normalize_text(raw["Establishment Type"])
    df["min_inspections"] = pd.to_numeric(normalize_text(raw["Min. Inspections Per Year"]), errors="coerce")
    df.loc[~df["min_inspections"].isin([1, 2, 3]), "min_inspections"] = np.nan
    df["inspection_date"] = parse_date(raw["Inspection Date"])
    df["status"] = normalize_text(raw["Establishment Status"])
    df["infraction"] = normalize_text(raw["Infraction Details"])
    df["severity"] = normalize_severity(raw["Severity"])
    df["action"] = normalize_text(raw["Action"])
    df["outcome"] = normalize_outcome(raw["Outcome"])
    df["outcome_date"] = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
    df["fine"] = pd.to_numeric(normalize_text(raw["Amount Fined"]), errors="coerce")

    bad = df["inspection_date"].isna() | df["est_key"].isna()
    report["archive_rows_dropped_bad_date_or_id"] = int(bad.sum())
    df = df[~bad]

    # Attributes for every archived establishment (used for type linkage and to train the
    # name -> category classifier), taken from its most recent archive record.
    latest = df.sort_values("inspection_date").drop_duplicates("est_key", keep="last").set_index("est_key")
    mins = (df.dropna(subset=["min_inspections"]).sort_values("inspection_date")
              .drop_duplicates("est_key", keep="last").set_index("est_key")["min_inspections"])
    attrs = pd.DataFrame({
        "name": latest["name"],
        "type": latest["type"],
        "min_inspections": mins.reindex(latest.index),
        "archive_first_inspection": df.groupby("est_key")["inspection_date"].min(),
    })
    attrs.index.name = "est_key"

    if cutoff is not None:
        late = df["inspection_date"] >= cutoff
        report["archive_rows_dropped_overlap_with_current"] = int(late.sum())
        df = df[~late]
    dup = df.duplicated()
    report["archive_exact_duplicate_rows_removed"] = int(dup.sum())
    df = df[~dup].copy()
    df["category_code"] = pd.Series(pd.NA, index=df.index, dtype="string")
    df["source"] = "archive"
    records = df[["est_key", "source", "inspection_date", "status", "infraction", "category_code", "severity",
                  "action", "outcome", "outcome_date", "fine"]].reset_index(drop=True)
    report["archive_rows_kept"] = int(len(records))
    return records, attrs.reset_index(), report


# =============================================================== table assembly
def build_infractions(records: pd.DataFrame) -> pd.DataFrame:
    inf = records[records["infraction"].notna()].copy()
    inf["infraction"] = canonical_text(inf["infraction"])
    inf["severity"] = inf["severity"].fillna("Unknown")
    # Category: deficiency code when available; otherwise the most common code that the
    # same infraction text has in the current feed; otherwise keyword rules.
    coded = inf.dropna(subset=["category_code"])
    text_to_code = (coded.groupby(["infraction", "category_code"]).size().reset_index(name="n")
                         .sort_values(["infraction", "n"], ascending=[True, False])
                         .drop_duplicates("infraction").set_index("infraction")["category_code"])
    missing = inf["category_code"].isna()
    inf.loc[missing, "category_code"] = inf.loc[missing, "infraction"].map(text_to_code)
    missing = inf["category_code"].isna()
    inf.loc[missing, "category_code"] = keyword_category(inf.loc[missing, "infraction"])
    pests = inf["infraction"].str.contains(PEST_RE, na=False)
    inf.loc[pests, "category_code"] = "8"
    inf["category"] = inf["category_code"].map(CATEGORY_LABELS).fillna("Other").astype("string")
    inf = inf.sort_values(["est_key", "inspection_date", "severity"], kind="stable")
    cols = ["est_key", "source", "inspection_date", "infraction", "category", "severity", "action",
            "outcome", "outcome_date", "fine"]
    return inf[cols].reset_index(drop=True)


def build_inspections(records: pd.DataFrame) -> pd.DataFrame:
    """One row per (establishment, inspection date)."""
    r = records.copy()
    r["has_inf"] = r["infraction"].notna()
    sev = r["severity"].where(r["has_inf"])
    for name, label in [("n_minor", "Minor"), ("n_significant", "Significant"), ("n_crucial", "Crucial"),
                        ("n_not_applicable", "Not Applicable")]:
        r[name] = (sev == label).fillna(False).astype(int)
    r["status_rank"] = r["status"].map(STATUS_RANK).fillna(-1)
    r["conviction"] = r["outcome"].str.startswith("Conviction").fillna(False).astype(bool)
    g = r.groupby(["est_key", "inspection_date"], sort=True)
    ins = g.agg(
        source=("source", "first"),
        status_rank=("status_rank", "max"),
        n_infractions=("has_inf", "sum"),
        n_minor=("n_minor", "sum"),
        n_significant=("n_significant", "sum"),
        n_crucial=("n_crucial", "sum"),
        n_not_applicable=("n_not_applicable", "sum"),
        fine_total=("fine", "sum"),
        has_conviction=("conviction", "any"),
    ).reset_index()
    rank_to_status = {v: k for k, v in STATUS_RANK.items()}
    ins["status"] = ins["status_rank"].map(rank_to_status).astype("string")
    ins = ins.drop(columns="status_rank")
    ins["has_infraction"] = ins["n_infractions"] > 0
    ins["has_serious"] = (ins["n_significant"] + ins["n_crucial"]) > 0
    ins["not_pass"] = ins["status"].isin(NOT_PASS_STATUSES)
    ins = ins.sort_values(["est_key", "inspection_date"]).reset_index(drop=True)
    prev_status = ins.groupby("est_key")["status"].shift()
    gap = (ins["inspection_date"] - ins.groupby("est_key")["inspection_date"].shift()).dt.days
    ins["days_since_previous"] = gap
    ins["is_followup"] = (prev_status.isin(NOT_PASS_STATUSES) & (gap <= FOLLOWUP_WINDOW_DAYS)).fillna(False).astype(bool)
    return ins


def build_tables(current_raw: pd.DataFrame, historical_raw: pd.DataFrame | None = None) -> CleanTables:
    cur_records, est, report = clean_current(current_raw)
    cutoff = cur_records["inspection_date"].min()
    report["current_first_date"] = str(cutoff.date())
    report["current_last_date"] = str(cur_records["inspection_date"].max().date())

    if historical_raw is not None and len(historical_raw):
        hist_records, hist_attrs, hist_report = clean_historical(historical_raw, cutoff)
        report.update(hist_report)
        # Only archive history for establishments that are still in the current feed.
        hist_records = hist_records[hist_records["est_key"].isin(set(est["est_key"]))]
        report["archive_rows_linked_to_current_establishments"] = int(len(hist_records))
    else:
        hist_records = cur_records.iloc[0:0]
        hist_attrs = pd.DataFrame(columns=["est_key", "name", "type", "min_inspections", "archive_first_inspection"])

    records = pd.concat([hist_records, cur_records], ignore_index=True)
    inspections = build_inspections(records)
    infractions = build_infractions(records)

    est = est.merge(hist_attrs[["est_key", "type", "min_inspections", "archive_first_inspection"]],
                    on="est_key", how="left")
    report["current_establishments_linked_to_archive"] = int(est["type"].notna().sum())
    report["inspections_total"] = int(len(inspections))
    report["inspections_current"] = int((inspections["source"] == "current").sum())
    report["infractions_total"] = int(len(infractions))
    report["infractions_without_severity"] = int((infractions["severity"] == "Unknown").sum())
    return CleanTables(establishments=est, inspections=inspections, infractions=infractions,
                       archive_establishments=hist_attrs, report=report)
