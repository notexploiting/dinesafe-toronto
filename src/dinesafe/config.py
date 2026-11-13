"""Project-wide configuration: file locations, API endpoints and dataset constants.

Everything that a future schema change on the City's side would touch lives here,
so when Toronto Open Data renames a column again there is one place to update.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# CKAN / Toronto Open Data
# ---------------------------------------------------------------------------
CKAN_BASE_URL = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
PACKAGE_ID = "dinesafe"

# (connect timeout, read timeout) in seconds. The full dump is ~50 MB, so the read
# timeout applies per chunk, not to the whole transfer.
HTTP_TIMEOUT = (15, 120)
HTTP_RETRIES = 5

# Columns the pipeline needs from the current ("live") DineSafe feed. Introduced when
# the City migrated DineSafe to a new system in late 2025. If any of these disappear
# the download step stops with a clear error instead of producing a broken dataset.
CURRENT_REQUIRED_COLUMNS = [
    "estId",
    "oldEstId",
    "estName",
    "address",
    "inspectionStatus",
    "phone",
    "inspectionDate",
    "observation",
    "typeDesc",
    "deficiencyDesc",
    "severity",
    "OutcomeDate",
    "OutcomeDesc",
    "amountFined",
    "latitude",
    "longitude",
]

# Columns of the yearly files in the "Dinesafe Historical Data" ZIP (2001-2023), which
# use the pre-migration schema.
HISTORICAL_COLUMNS = [
    "Rec #",
    "Establishment ID",
    "Inspection ID",
    "Establishment Name",
    "Establishment Type",
    "Establishment Address",
    "Latitude",
    "Longitude",
    "Establishment Status",
    "Min. Inspections Per Year",
    "Infraction Details",
    "Inspection Date",
    "Severity",
    "Action",
    "Outcome",
    "Amount Fined",
]

# Rough bounding box for the City of Toronto; coordinates outside it are treated as bad.
TORONTO_BBOX = {"lat_min": 43.55, "lat_max": 43.90, "lon_min": -79.70, "lon_max": -79.05}

# An inspection that happens within this many days after a Conditional Pass / Closed
# result is treated as a follow-up re-inspection rather than a routine inspection.
# (In the data, 96% of inspections after a Conditional Pass happen within 30 days,
# median 4 days, while the median gap after a Pass is ~150 days.)
FOLLOWUP_WINDOW_DAYS = 30


@dataclass(frozen=True)
class Paths:
    """All filesystem locations used by the pipeline, rooted at the project directory."""

    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def historical(self) -> Path:
        return self.raw / "historical"

    @property
    def processed(self) -> Path:
        return self.data / "processed"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def dashboard_data(self) -> Path:
        return self.root / "dashboard" / "data"

    def ensure(self) -> "Paths":
        for p in (self.raw, self.historical, self.processed, self.reports, self.dashboard_data):
            p.mkdir(parents=True, exist_ok=True)
        return self


def default_root() -> Path:
    """Project root: $DINESAFE_ROOT if set, else the repository containing this file."""
    env = os.environ.get("DINESAFE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    # src/dinesafe/config.py -> repository root is two levels above the package.
    candidate = here.parents[2]
    if (candidate / "src" / "dinesafe").is_dir():
        return candidate
    return Path.cwd()
