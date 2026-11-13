"""
download_data.py

Fetches the most up-to-date DineSafe data from the City of Toronto's Open Data CKAN API.

This script is kept so the original workflow still works:

    $ python src/download_data.py            # same as: python -m dinesafe download
    $ python src/download_data.py --force    # re-download even if nothing changed

The download logic now lives in the `dinesafe` package (src/dinesafe/ckan.py and
src/dinesafe/download.py). Compared with the first version of this script it:

- picks the right resource explicitly (the dataset now has several CSV/JSON/XML copies)
- retries with backoff and uses timeouts instead of hanging on a bad connection
- streams the ~50 MB file to disk and only renames it into place once it is complete
- checks that the expected columns are present (the City changed the schema in 2025)
- checks the row count against the datastore's own total
- writes a manifest (source, time, SHA-256, rows) and skips the download when the City
  hasn't refreshed the data since the last run
- also fetches the 2001-2023 historical archive (once; it rarely changes)

Output:
    data/raw/dinesafe_YYYYMMDD_HHMMSS.csv (+ .manifest.json)
    data/raw/historical/dinesafe_hist_YYYY.csv
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dinesafe.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["download", *sys.argv[1:]]))
