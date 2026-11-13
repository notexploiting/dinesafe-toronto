"""A small, defensive client for the City of Toronto's CKAN Open Data API.

CKAN concepts (see notes/01_ckan_api_explained.md for a longer walkthrough):

* package  - a dataset ("dinesafe"); its metadata lists one or more resources
* resource - one file or table inside the package (CSV, JSON, ZIP, ...)
* datastore_active resource - a resource loaded into CKAN's database, so it can be
  queried with ``datastore_search`` and exported in full via ``/datastore/dump/<id>``

Compared to calling ``requests.get`` directly, this client adds:

* automatic retries with exponential backoff for flaky connections and 429/5xx answers
* timeouts, so a stalled connection can't hang the pipeline forever
* validation of CKAN's ``success`` flag (CKAN can answer HTTP 200 with an error body)
* streaming downloads written atomically (a ``.part`` file renamed on success), so an
  interrupted download never leaves a truncated CSV that looks valid
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from dinesafe.config import CKAN_BASE_URL, HTTP_RETRIES, HTTP_TIMEOUT

log = logging.getLogger(__name__)


class CkanError(RuntimeError):
    """CKAN returned an error, or a response we could not interpret."""


@dataclass
class DownloadResult:
    path: Path
    bytes: int
    sha256: str
    url: str


def _build_session(retries: int) -> requests.Session:
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=1.0,  # 0s, 2s, 4s, 8s, ... between attempts
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class CkanClient:
    def __init__(
        self,
        base_url: str = CKAN_BASE_URL,
        *,
        timeout: tuple[float, float] = HTTP_TIMEOUT,
        retries: int = HTTP_RETRIES,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.session = session or _build_session(retries)

    # ------------------------------------------------------------------ actions
    def action(self, name: str, **params: Any) -> Any:
        """Call ``/api/3/action/<name>`` and return its ``result`` payload."""
        url = f"{self.base_url}/api/3/action/{name}"
        log.debug("GET %s %s", url, params)
        resp = self.session.get(url, params=params, timeout=self.timeout)
        try:
            body = resp.json()
        except ValueError as exc:
            raise CkanError(f"{name}: expected JSON, got HTTP {resp.status_code}: {resp.text[:200]!r}") from exc
        if resp.status_code != 200 or not body.get("success", False):
            raise CkanError(f"{name} failed (HTTP {resp.status_code}): {body.get('error', body)}")
        return body["result"]

    def package_show(self, package_id: str) -> dict:
        """Metadata for a dataset, including its list of resources."""
        return self.action("package_show", id=package_id)

    def datastore_info(self, resource_id: str) -> tuple[int, list[str]]:
        """Return (total row count, column names) for a datastore-active resource.

        ``limit=0`` asks for no records, just the schema and total, so this is cheap.
        """
        result = self.action("datastore_search", id=resource_id, limit=0)
        fields = [f["id"] for f in result.get("fields", [])]
        return int(result.get("total", -1)), fields

    def datastore_dump_url(self, resource_id: str) -> str:
        return f"{self.base_url}/datastore/dump/{resource_id}"

    # ---------------------------------------------------------------- downloads
    def download(self, url: str, dest: Path, *, attempts: int = 3, chunk_size: int = 1 << 20) -> DownloadResult:
        """Stream ``url`` to ``dest`` atomically, retrying if the transfer is cut off.

        The urllib3 retry policy covers failures before the response starts; this loop
        covers connections dropped mid-transfer (ChunkedEncodingError and friends).
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            digest = hashlib.sha256()
            size = 0
            try:
                with self.session.get(url, stream=True, timeout=self.timeout) as resp:
                    if resp.status_code != 200:
                        raise CkanError(f"Download failed (HTTP {resp.status_code}) for {url}")
                    expected = resp.headers.get("Content-Length")
                    with open(part, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=chunk_size):
                            if chunk:
                                fh.write(chunk)
                                digest.update(chunk)
                                size += len(chunk)
                    if expected is not None and resp.headers.get("Content-Encoding") in (None, "identity"):
                        if int(expected) != size:
                            raise requests.exceptions.ChunkedEncodingError(
                                f"Truncated download: got {size} of {expected} bytes"
                            )
                os.replace(part, dest)
                log.info("Downloaded %s (%.1f MB) -> %s", url, size / 1e6, dest)
                return DownloadResult(path=dest, bytes=size, sha256=digest.hexdigest(), url=url)
            except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.Timeout) as exc:
                last_exc = exc
                part.unlink(missing_ok=True)
                if attempt < attempts:
                    wait = 2 ** attempt
                    log.warning("Download attempt %d/%d failed (%s); retrying in %ds", attempt, attempts, exc, wait)
                    time.sleep(wait)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
        raise CkanError(f"Download failed after {attempts} attempts: {url}") from last_exc
