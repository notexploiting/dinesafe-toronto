"""Download the DineSafe datasets from Toronto Open Data.

Two inputs are fetched:

1. **Current feed** - the datastore-active "Dinesafe" resource (rolling ~3 years of
   inspections, refreshed daily). Saved as ``data/raw/dinesafe_YYYYMMDD_HHMMSS.csv``.
2. **Historical archive** - "Dinesafe Historical Data" ZIP with one CSV per year
   (2001-2023, older schema). Extracted to ``data/raw/historical/``. It rarely changes,
   so it is only re-downloaded when the City updates it.

Every snapshot gets a ``.manifest.json`` next to it recording where it came from, when,
its SHA-256 and row count. The manifest is what lets later runs skip downloads when the
City hasn't published anything new.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from dinesafe.ckan import CkanClient, CkanError
from dinesafe.config import CURRENT_REQUIRED_COLUMNS, HISTORICAL_COLUMNS, PACKAGE_ID, Paths

log = logging.getLogger(__name__)

SNAPSHOT_RE = re.compile(r"^dinesafe_(\d{8}_\d{6})\.csv$")
HISTORICAL_FILE_RE = re.compile(r"dinesafe_hist_(\d{4})\.csv$", re.IGNORECASE)


class SchemaError(RuntimeError):
    """The City's data no longer has the columns this pipeline expects."""


# --------------------------------------------------------------------------- utilities
def _write_manifest(path: Path, manifest: dict) -> Path:
    mpath = path.with_name(path.name + ".manifest.json")
    tmp = mpath.with_name(mpath.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(mpath)
    return mpath


def read_manifest(path: Path) -> dict | None:
    mpath = path.with_name(path.name + ".manifest.json")
    if not mpath.exists():
        return None
    try:
        return json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_snapshots(raw_dir: Path) -> list[Path]:
    """Current-feed snapshots, oldest first (sorted by the timestamp in the file name)."""
    if not raw_dir.exists():
        return []
    return sorted((p for p in raw_dir.iterdir() if SNAPSHOT_RE.match(p.name)), key=lambda p: p.name)


def latest_snapshot(raw_dir: Path) -> Path | None:
    snaps = list_snapshots(raw_dir)
    return snaps[-1] if snaps else None


def check_columns(found: list[str], required: list[str], what: str) -> None:
    missing = [c for c in required if c not in found]
    if missing:
        raise SchemaError(
            f"The {what} is missing expected column(s) {missing}. Found columns: {found}. "
            "Toronto Open Data has probably changed the DineSafe schema; update "
            "dinesafe/config.py and the cleaning code to match."
        )


def count_csv_rows(path: Path) -> tuple[list[str], int]:
    """Return (header, number of data rows), handling quoted fields with newlines."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, [])
        n = sum(1 for _ in reader)
    return header, n


# ---------------------------------------------------------------- resource selection
def select_current_resource(resources: list[dict]) -> dict:
    """Pick the resource that holds the live inspection table.

    Preference order:
    1. a datastore-active resource (exported with /datastore/dump - CKAN's canonical copy)
    2. otherwise the most recently modified CSV file that is not the historical archive
    """
    active = [r for r in resources if r.get("datastore_active")]
    if active:
        return active[0]
    csvs = [
        r for r in resources
        if str(r.get("format", "")).upper() == "CSV" and "historical" not in str(r.get("name", "")).lower()
    ]
    if not csvs:
        raise CkanError("No datastore-active or CSV resource found in the DineSafe package")
    return max(csvs, key=lambda r: r.get("last_modified") or r.get("created") or "")


def select_historical_resource(resources: list[dict]) -> dict | None:
    for r in resources:
        if "historical" in str(r.get("name", "")).lower() and str(r.get("format", "")).upper() == "ZIP":
            return r
    return None


# --------------------------------------------------------------------- current feed
def download_current(client: CkanClient, paths: Paths, *, force: bool = False, keep: int | None = None) -> Path:
    """Download the current DineSafe feed, unless the newest snapshot is already up to date."""
    paths.ensure()
    package = client.package_show(PACKAGE_ID)
    refreshed = package.get("last_refreshed") or package.get("metadata_modified")
    resource = select_current_resource(package.get("resources", []))
    log.info("Package '%s' last refreshed %s; using resource '%s' (%s)",
             PACKAGE_ID, refreshed, resource.get("name"), resource["id"])

    latest = latest_snapshot(paths.raw)
    if latest and not force:
        manifest = read_manifest(latest) or {}
        if manifest.get("resource_id") == resource["id"] and manifest.get("package_last_refreshed") == refreshed:
            log.info("Latest snapshot %s already matches the published data; skipping download "
                     "(use --force to download anyway)", latest.name)
            return latest

    expected_rows = None
    if resource.get("datastore_active"):
        expected_rows, fields = client.datastore_info(resource["id"])
        check_columns(fields, CURRENT_REQUIRED_COLUMNS, "DineSafe datastore table")
        url = client.datastore_dump_url(resource["id"])
    else:
        url = resource["url"]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = paths.raw / f"dinesafe_{stamp}.csv"
    result = client.download(url, dest)

    header, n_rows = count_csv_rows(dest)
    try:
        check_columns(header, CURRENT_REQUIRED_COLUMNS, "downloaded CSV")
        if n_rows == 0:
            raise SchemaError("Downloaded CSV has a header but no rows")
    except SchemaError:
        dest.unlink(missing_ok=True)
        raise
    if expected_rows is not None and expected_rows >= 0 and n_rows != expected_rows:
        # The datastore can be refreshed between the two calls; a small drift is
        # harmless, a big one means a truncated or garbled file.
        drift = abs(n_rows - expected_rows) / max(expected_rows, 1)
        if drift > 0.01:
            dest.unlink(missing_ok=True)
            raise CkanError(f"Row count mismatch: datastore reports {expected_rows}, file has {n_rows}")
        log.warning("Row count differs slightly from datastore total (%d vs %d)", n_rows, expected_rows)

    _write_manifest(dest, {
        "kind": "current",
        "package_id": PACKAGE_ID,
        "package_last_refreshed": refreshed,
        "resource_id": resource["id"],
        "resource_name": resource.get("name"),
        "source_url": url,
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bytes": result.bytes,
        "sha256": result.sha256,
        "rows": n_rows,
        "columns": header,
    })
    log.info("Saved %d rows to %s", n_rows, dest)
    if keep:
        prune_snapshots(paths.raw, keep)
    return dest


def prune_snapshots(raw_dir: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` current-feed snapshots (and their manifests)."""
    snaps = list_snapshots(raw_dir)
    removed = []
    for p in snaps[:-keep] if keep > 0 else []:
        p.unlink(missing_ok=True)
        p.with_name(p.name + ".manifest.json").unlink(missing_ok=True)
        removed.append(p)
    if removed:
        log.info("Pruned %d old snapshot(s)", len(removed))
    return removed


# ----------------------------------------------------------------- historical archive
def download_historical(client: CkanClient, paths: Paths, *, force: bool = False,
                        package: dict | None = None) -> list[Path]:
    """Download and extract the yearly historical CSVs (only when the ZIP has changed)."""
    paths.ensure()
    package = package or client.package_show(PACKAGE_ID)
    resource = select_historical_resource(package.get("resources", []))
    if resource is None:
        log.warning("No historical ZIP resource found; continuing without the archive")
        return existing_historical_files(paths.historical)

    marker = paths.historical / "archive.zip"  # manifest anchor (the ZIP itself is deleted)
    manifest = read_manifest(marker) or {}
    existing = existing_historical_files(paths.historical)
    if (not force and existing and manifest.get("resource_id") == resource["id"]
            and manifest.get("last_modified") == resource.get("last_modified")):
        log.info("Historical archive unchanged since %s; skipping download", resource.get("last_modified"))
        return existing

    tmp_zip = paths.historical / "archive.zip.download"
    result = client.download(resource["url"], tmp_zip)
    try:
        extracted = safe_extract_csvs(tmp_zip, paths.historical)
    finally:
        tmp_zip.unlink(missing_ok=True)
    if not extracted:
        raise CkanError("Historical ZIP contained no dinesafe_hist_YYYY.csv files")
    _write_manifest(marker, {
        "kind": "historical",
        "resource_id": resource["id"],
        "resource_name": resource.get("name"),
        "last_modified": resource.get("last_modified"),
        "source_url": resource["url"],
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bytes": result.bytes,
        "sha256": result.sha256,
        "files": [p.name for p in extracted],
    })
    log.info("Extracted %d historical files to %s", len(extracted), paths.historical)
    return extracted


def existing_historical_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if HISTORICAL_FILE_RE.search(p.name))


def safe_extract_csvs(zip_path: Path, dest: Path) -> list[Path]:
    """Extract only ``dinesafe_hist_YYYY.csv`` members, flattened into ``dest``.

    Member paths are never joined onto ``dest`` (only their validated basename is), so a
    malicious or odd archive can't write outside the target directory ("zip slip").
    """
    out = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename.replace("\\", "/")).name
            m = HISTORICAL_FILE_RE.search(name)
            if not m:
                continue
            target = dest / f"dinesafe_hist_{m.group(1)}.csv"
            tmp = target.with_name(target.name + ".part")
            with zf.open(info) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
            tmp.replace(target)
            out.append(target)
    return sorted(out)


def check_historical_header(path: Path) -> None:
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        first = fh.readline()
    cols = [c.strip().strip('"') for c in first.strip().split(",")]
    check_columns(cols, HISTORICAL_COLUMNS, f"historical file {path.name}")
