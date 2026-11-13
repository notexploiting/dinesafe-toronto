# CKAN Key Concepts

## Introduction
Toronto Open Data is stored in a CKAN instance. Its APIs are documented here: https://docs.ckan.org/en/latest/api/

Toronto Public Health provided a Python code snippet for accessing their dataset via the CKAN API. The content below gives explanations on what their code does, along with some terminology and their definitions. 

## What is a package?
* The main dataset container in CKAN
* It contains metadata (title, description, etc.) and a list of resources (files containing the actual data)
* In this case, the `dinesafe` package is the dataset used

## What are resources?
* A resource is an individual  data file within a package, such as a CSV, PDF, or JSON API endpoint
* Each resource has metadata (name, type, format, size)

## What is resource data?
* The actual data inside a resource
* If `datastore` is `True`, the resource can be queried through CKAN's `datastore_search` action

## Accessing the Dataset via the API

This defines the base CKAN API URL. 
```python
base_url = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
```

The following fetches all metadata about the `dinesafe` dataset, including its resources, from CKAN.
* The URL is the endpoint to retrieve a full metadata description of a dataset
* The ID parameter tells CKAN which dataset to retrieve
* Our retrieved JSON product is a Python dictionary, containing fields like  `title` and `notes`.

```python
url = base_url + "/api/3/action/package_show"
params = { "id": "dinesafe"}
package = requests.get(url, params = params).json()
```

In order to find the specific resource(s) to download/query, a loop through each resource within the `dinesafe` dataset package is used.

```python
for idx, resource in enumerate(package["result"]["resources"]):
```

To be able to query structured table via CKAN's datastore API, the resource must be "datastore_active".

```python
if resource["datastore_active"]:
```

Then, the full dataset can be downloaded as a CSV. 
* The `dump` action returns *all* records for a resource (not the package) with a specified, unique CKAN ID 
* `.text` returns it as a long CSV string, but typically it'd be written to a `.csv` file

```python
url = base_url + "/datastore/dump/" + resource["id"]
           resource_dump_data = requests.get(url).text
           print(resource_dump_data)

```

Finally, the `datastore_search` API can be used to get a preview (or filtered records). This allows for the data to be searched like a database (such as filtering by fields, or limiting results). The basic query here fetches the first page of records (`limit=100`, by default). You can add parameters `p` such as:
* `limit`
* `offset`
* `filters`
* `q`, for search

Full documentation: [CKAN Datastore Search API](https://docs.ckan.org/en/latest/maintaining/datastore.html)

```python
url = base_url + "/api/3/action/datastore_search"
           p = { "id": resource["id"] }
           resource_search_data = requests.get(url, params = p).json()["result"]
           print(resource_search_data)
```

Alternatively, if a resource is *non* `datastore_active`, you can use `/resource_show` to get basic metadata. These can't be queried like a database; they must be downloaded and parsed manually.

```python
if not resource["datastore_active"]:
           url = base_url + "/api/3/action/resource_show?id=" + resource["id"]
           resource_metadata = requests.get(url).json()
           print(resource_metadata)
           # From here, you can use the "url" attribute to download this file
```

## Update (2026): what changed, and how the download was hardened

The original script looped over every resource and downloaded each `datastore_active` one
with a bare `requests.get`. That worked, but a few things have changed since:

* **The dataset now has several resources**: `Dinesafe.csv`, `Dinesafe.json` and `Dinesafe.xml` (file
  copies), older `Dinesafe™.*` copies, a `Dinesafe Historical Data` ZIP (2001-2023), and one
  datastore-active `Dinesafe` table. The pipeline now *chooses* a resource explicitly
  (`select_current_resource` in `src/dinesafe/download.py`) instead of downloading whatever
  is datastore-active.
* **The schema changed** during the City's late-2025 system migration
  (`Establishment ID` became `estId`, `Inspection ID` and `Establishment Type` disappeared, and so on). A
  download can now be checked *before* fetching the file, with `datastore_search` and
  `limit=0`, which returns just the column list and the row count:

  ```python
  url = base_url + "/api/3/action/datastore_search"
  result = requests.get(url, params={"id": resource_id, "limit": 0}).json()["result"]
  total = result["total"]                        # e.g. 126678
  fields = [f["id"] for f in result["fields"]]   # the column names
  ```

What `src/dinesafe/ckan.py` adds on top of plain `requests.get`:

| Problem | Fix |
|---|---|
| A flaky connection or a 502 from the server fails the whole run | `urllib3.Retry` with exponential backoff on connection errors and 429/5xx (honours `Retry-After`) |
| A stalled connection hangs forever | `timeout=(connect, read)` on every request |
| CKAN can answer HTTP 200 with `{"success": false, "error": ...}` | `action()` checks the `success` flag |
| `requests.get(url).text` holds 50 MB in memory and a dropped connection leaves a half-written CSV | Streamed in 1 MB chunks to a `.part` file, renamed into place only when complete; retried if cut off mid-transfer; `Content-Length` checked |
| No record of what was downloaded | `.manifest.json` next to each file: source URL, time, SHA-256, rows, the City's `last_refreshed` |
| Re-downloading identical data every run | Skips the download when `last_refreshed` matches the newest manifest |
| A truncated or garbled file goes unnoticed | Row count compared with the datastore's `total` |
| A ZIP member named `../../something` could write outside the target folder ("zip slip") | Only `dinesafe_hist_YYYY.csv` basenames are extracted |
