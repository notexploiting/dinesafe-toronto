"""Shared fixtures: a small synthetic DineSafe dataset that mimics the real feed's quirks
(legacy/new duplicate IDs, "None" strings, follow-up inspections, malformed archive CSVs)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dinesafe.config import CURRENT_REQUIRED_COLUMNS, HISTORICAL_COLUMNS, Paths  # noqa: E402

INFRACTIONS = [
    ("FAIL TO ENSURE EQUIPMENT SURFACE SANITIZED AS NECESSARY - SEC. 22", "05. MAINTENANCE / SANITATION OF NON-FOOD CONTACT SURFACES / EQUIPMENT", "M - Minor"),
    ("Fail to protect against entry of pests - Sec. 13(1)", "5C. Proper maintenance / washing of rooms (including washrooms)", "S - Significant"),
    ("FAIL TO MAINTAIN HANDWASHING STATIONS (LIQUID SOAP AND PAPER TOWELS) - SEC. 7(3)(C)", "3A. Separate and convenient hand washing sink(s)", "S - Significant"),
    ("Store potentially hazardous foods at internal temperature between 4°C and 60°C - Sec. 27(1)", "01. FOOD TEMPERATURE CONTROL / FOOD PROCESSING", "C - Crucial"),
    ("Fail to ensure presence of food handler or supervisor during all hours of operation - Sec. 32", "ADMINISTRATIVE REQUIREMENTS", "NA"),
]
TYPES = ["Restaurant", "Food Take Out", "Bakery", "Supermarket", "Child Care - Catered"]
NAME_WORDS = {"Restaurant": "GRILL", "Food Take Out": "PIZZA", "Bakery": "BAKERY", "Supermarket": "SUPERMARKET",
              "Child Care - Catered": "DAYCARE"}


def make_current(n_est: int = 240, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    start, end = pd.Timestamp("2023-11-10"), pd.Timestamp("2026-11-01")
    for i in range(n_est):
        legacy = str(10_000_000 + i) if i < 200 else None
        new_id = f"001SYN{i:06d}AA"
        kind = TYPES[i % len(TYPES)]
        name = f"{NAME_WORDS[kind]} NUMBER {i}"
        risk = rng.beta(2, 4)
        lat, lon = 43.65 + rng.normal(0, 0.03), -79.38 + rng.normal(0, 0.05)
        d = start + pd.Timedelta(days=int(rng.integers(0, 120)))
        while d < end:
            n_inf = rng.poisson(risk * 3)
            picks = [INFRACTIONS[k] for k in rng.integers(0, len(INFRACTIONS), n_inf)]
            serious = any(p[2] in ("S - Significant", "C - Crucial") for p in picks)
            status = "Conditional Pass" if serious and rng.random() < 0.6 else "Pass"
            if any(p[2] == "C - Crucial" for p in picks) and rng.random() < 0.1:
                status = "Closed"
            visits = [(d, status, picks)]
            if status != "Pass":
                visits.append((d + pd.Timedelta(days=3), "Pass", []))
            for vd, vs, vp in visits:
                # Before the late-2025 migration, linked establishments also appear under their
                # legacy ID with identical inspections (the duplication the cleaner must remove).
                ids = [new_id] + ([legacy] if legacy and i < 60 and vd < pd.Timestamp("2025-11-07") else [])
                for est_id in ids:
                    addr = f"{100 + i} Queen St W None M5V 1J4" if est_id == new_id else f"{100 + i} QUEEN ST W None M5V 1J4"
                    base = {"estId": est_id, "oldEstId": legacy or "None", "estName": name, "address": addr,
                            "inspectionStatus": vs, "phone": "4165550123" if i % 7 else "0000000000",
                            "inspectionDate": vd.strftime("%Y-%m-%d"), "observation": "x", "OutcomeDate": "",
                            "OutcomeDesc": "None", "amountFined": "", "latitude": f"{lat:.6f}", "longitude": f"{lon:.6f}"}
                    if not vp:
                        rows.append({**base, "typeDesc": "None", "deficiencyDesc": "None", "severity": "None"})
                    for text, defi, sev in vp:
                        rows.append({**base, "typeDesc": text + "  ", "deficiencyDesc": defi, "severity": sev})
            d = vd + pd.Timedelta(days=int(rng.integers(90, 240)))
    df = pd.DataFrame(rows)
    df.insert(0, "_id", range(1, len(df) + 1))
    df.insert(1, "unique_id", [f"u{k}" for k in range(len(df))])
    return df[["_id", "unique_id"] + CURRENT_REQUIRED_COLUMNS]


def make_historical(n_est: int = 240, seed: int = 1) -> dict[int, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    by_year: dict[int, list] = {}
    rec = 0
    for i in range(n_est):
        kind = TYPES[i % len(TYPES)]
        for year in (2019, 2021, 2023):
            rec += 1
            text, _, sev = INFRACTIONS[int(rng.integers(0, len(INFRACTIONS)))]
            date = f"{year}-06-15" if year != 2023 else "06/15/2023"
            by_year.setdefault(year, []).append({
                "Rec #": rec, "Establishment ID": 10_000_000 + i, "Inspection ID": 100_000 + rec,
                "Establishment Name": f"{NAME_WORDS[kind]} NUMBER {i}", "Establishment Type": kind,
                "Establishment Address": f"{100 + i} QUEEN ST W", "Latitude": 43.65, "Longitude": -79.38,
                "Establishment Status": "Pass", "Min. Inspections Per Year": [1, 2, 3][i % 3],
                "Infraction Details": text, "Inspection Date": date,
                "Severity": "NA - Not Applicable" if sev == "NA" else sev, "Action": "Notice to Comply",
                "Outcome": "", "Amount Fined": ""})
    return {y: pd.DataFrame(r)[HISTORICAL_COLUMNS] for y, r in by_year.items()}


def write_historical(directory: Path, frames: dict[int, pd.DataFrame], break_quotes_year: int | None = 2021) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for year, df in frames.items():
        path = directory / f"dinesafe_hist_{year}.csv"
        df.to_csv(path, index=False, quoting=1)  # QUOTE_ALL, like the City's files
        if year == break_quotes_year:  # reproduce the 2020-2022 defect: extra quote at both ends
            text = path.read_text(encoding="utf-8").rstrip("\n")
            path.write_text('"' + text + '"\n', encoding="utf-8")


@pytest.fixture
def current_df() -> pd.DataFrame:
    return make_current()


@pytest.fixture
def project(tmp_path: Path) -> Paths:
    """A project directory with one raw snapshot and a historical archive."""
    paths = Paths(tmp_path).ensure()
    make_current().to_csv(paths.raw / "dinesafe_20261101_120000.csv", index=False)
    write_historical(paths.historical, make_historical())
    return paths
