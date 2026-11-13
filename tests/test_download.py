"""The download step, against a fake CKAN server (no network)."""

import io
import json
import zipfile

import pytest
import requests

from dinesafe import download
from dinesafe.ckan import CkanClient, CkanError
from dinesafe.config import CURRENT_REQUIRED_COLUMNS, Paths

HEADER = ",".join(["_id", "unique_id"] + CURRENT_REQUIRED_COLUMNS)
ROW = "1,abc," + ",".join(["x"] * len(CURRENT_REQUIRED_COLUMNS))


class FakeResponse:
    def __init__(self, status=200, body=b"", json_body=None, headers=None, fail_after=None):
        self.status_code = status
        self._body = body
        self._json = json_body
        self.headers = headers or {}
        self._fail_after = fail_after
        self.text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), 4):
            if self._fail_after is not None and i >= self._fail_after:
                raise requests.exceptions.ChunkedEncodingError("connection reset")
            yield self._body[i:i + 4]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    """Routes GETs by URL substring; each route is a list of responses served in order."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.calls.append((url, params))
        for key, responses in self.routes.items():
            if key in url:
                return responses.pop(0) if len(responses) > 1 else responses[0]
        raise AssertionError(f"unexpected URL {url}")


def package(refreshed="2026-11-12", hist_modified="2026-07-06"):
    return {"success": True, "result": {"last_refreshed": refreshed, "resources": [
        {"id": "file-csv", "name": "Dinesafe.csv", "format": "CSV", "datastore_active": False, "url": "https://x/file.csv",
         "last_modified": "2026-11-12"},
        {"id": "hist", "name": "Dinesafe Historical Data", "format": "ZIP", "datastore_active": False,
         "url": "https://x/hist.zip", "last_modified": hist_modified},
        {"id": "ds", "name": "Dinesafe", "format": "JSON", "datastore_active": True, "url": "https://x/ds"},
    ]}}


def datastore_info(total=2, fields=None):
    fields = fields or ["_id", "unique_id"] + CURRENT_REQUIRED_COLUMNS
    return {"success": True, "result": {"total": total, "fields": [{"id": f} for f in fields], "records": []}}


def client_for(routes):
    return CkanClient(base_url="https://ckan.test", session=FakeSession(routes))


def test_action_raises_on_ckan_error_body():
    c = client_for({"package_show": [FakeResponse(200, json_body={"success": False, "error": {"message": "nope"}})]})
    with pytest.raises(CkanError, match="nope"):
        c.package_show("dinesafe")


def test_action_raises_on_non_json():
    c = client_for({"package_show": [FakeResponse(502, body=b"<html>Bad gateway</html>")]})
    with pytest.raises(CkanError, match="expected JSON"):
        c.package_show("dinesafe")


def test_select_current_resource_prefers_datastore():
    assert download.select_current_resource(package()["result"]["resources"])["id"] == "ds"
    no_ds = [r for r in package()["result"]["resources"] if not r["datastore_active"]]
    assert download.select_current_resource(no_ds)["id"] == "file-csv"


def test_download_current_validates_and_writes_manifest(tmp_path):
    body = f"{HEADER}\n{ROW}\n{ROW}\n".encode()
    c = client_for({"package_show": [FakeResponse(json_body=package())],
                    "datastore_search": [FakeResponse(json_body=datastore_info(total=2))],
                    "datastore/dump/ds": [FakeResponse(body=body, headers={"Content-Length": str(len(body))})]})
    paths = Paths(tmp_path)
    path = download.download_current(c, paths)
    assert path.exists() and path.read_bytes() == body
    manifest = download.read_manifest(path)
    assert manifest["rows"] == 2 and manifest["resource_id"] == "ds" and len(manifest["sha256"]) == 64
    assert not list(paths.raw.glob("*.part"))

    # Same refresh timestamp -> no new download.
    before = len(c.session.calls)
    assert download.download_current(c, paths) == path
    assert not any("dump" in u for u, _ in c.session.calls[before:])


def test_download_current_rejects_schema_change(tmp_path):
    c = client_for({"package_show": [FakeResponse(json_body=package())],
                    "datastore_search": [FakeResponse(json_body=datastore_info(fields=["_id", "something_else"]))]})
    with pytest.raises(download.SchemaError, match="missing expected column"):
        download.download_current(c, Paths(tmp_path))
    assert not list((tmp_path / "data" / "raw").glob("dinesafe_*.csv"))


def test_download_current_rejects_row_count_mismatch(tmp_path):
    body = f"{HEADER}\n{ROW}\n".encode()
    c = client_for({"package_show": [FakeResponse(json_body=package())],
                    "datastore_search": [FakeResponse(json_body=datastore_info(total=500))],
                    "datastore/dump/ds": [FakeResponse(body=body)]})
    with pytest.raises(CkanError, match="Row count mismatch"):
        download.download_current(c, Paths(tmp_path))
    assert not list((tmp_path / "data" / "raw").glob("dinesafe_*.csv"))


def test_download_retries_dropped_connection(tmp_path, monkeypatch):
    monkeypatch.setattr("dinesafe.ckan.time.sleep", lambda s: None)
    body = b"0123456789abcdef"
    c = client_for({"file": [FakeResponse(body=body, fail_after=8), FakeResponse(body=body)]})
    res = c.download("https://ckan.test/file", tmp_path / "out.bin")
    assert res.path.read_bytes() == body
    assert not (tmp_path / "out.bin.part").exists()


def test_download_detects_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr("dinesafe.ckan.time.sleep", lambda s: None)
    c = client_for({"file": [FakeResponse(body=b"short", headers={"Content-Length": "100"})]})
    with pytest.raises(CkanError, match="after 3 attempts"):
        c.download("https://ckan.test/file", tmp_path / "out.bin")
    assert not (tmp_path / "out.bin").exists()


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_historical_download_extracts_safely_and_caches(tmp_path):
    blob = _zip({"Dinesafe Historical data/dinesafe_hist_2019.csv": "a,b\n1,2\n",
                 "../../evil/dinesafe_hist_2020.csv": "a,b\n3,4\n",  # path traversal attempt
                 "readme.txt": "ignored"})
    c = client_for({"package_show": [FakeResponse(json_body=package())], "hist.zip": [FakeResponse(body=blob)]})
    paths = Paths(tmp_path)
    files = download.download_historical(c, paths)
    assert [f.name for f in files] == ["dinesafe_hist_2019.csv", "dinesafe_hist_2020.csv"]
    assert all(f.parent == paths.historical for f in files)
    assert not (tmp_path / "evil").exists()
    manifest = json.loads((paths.historical / "archive.zip.manifest.json").read_text())
    assert manifest["last_modified"] == "2026-07-06"

    n = len(c.session.calls)
    download.download_historical(c, paths)  # unchanged -> no second ZIP download
    assert not any("hist.zip" in u for u, _ in c.session.calls[n:])


def test_prune_snapshots(tmp_path):
    raw = tmp_path
    for stamp in ["20260101_000000", "20260102_000000", "20260103_000000"]:
        (raw / f"dinesafe_{stamp}.csv").write_text("x")
        (raw / f"dinesafe_{stamp}.csv.manifest.json").write_text("{}")
    download.prune_snapshots(raw, keep=1)
    assert [p.name for p in download.list_snapshots(raw)] == ["dinesafe_20260103_000000.csv"]
    assert not (raw / "dinesafe_20260101_000000.csv.manifest.json").exists()
