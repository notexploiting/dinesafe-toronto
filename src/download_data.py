import os
import requests
from datetime import datetime

# ----------------------------
# CONSTANTS
# ----------------------------
BASE_URL = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
PACKAGE_ID = "dinesafe"
DATA_DIR = "data"
RAW_DIR = "data/raw"
CSV_FILENAME = "dinesafe_raw.csv"

# ----------------------------
# Get metadata for the package
# ----------------------------
def fetch_package_metadata(package_id):
    """
    Retrieve the dinesafe dataset metadata, including its resources from CKAN.
    """
    print(f"Fetching metadata for package with ID '{package_id}'")
    url = BASE_URL + "/api/3/action/package_show"
    p = {"id": package_id}
    response = requests.get(url, params=p)
    response.raise_for_status()
    return response.json()["result"]



# ----------------------------
# Show all available resources, and then handle them accordingly
# ----------------------------
def handle_resources(resources):
    print("\nAvailable resources:")
    for idx, resource in enumerate(resources):
        print(f"\n---- RESOURCE {idx} -----")
        print("\t" + "Name: " + resource["name"])
        print("\t" + "ID: " + resource["id"])
        print("\t" + "Format: " + resource.get("format", "unknown"))
        print(f"\tDatastore Active: {resource["datastore_active"]}")

        if resource["datastore_active"]:
            print("!! YES This is a CKAN datastore-active resource")
            print("Download URL: " + BASE_URL + "/datastore/dump/" + resource["id"])
            handle_datastore_resource(resource)
            # break # if you only want to download the first one
        else:
            print("This is a direct file resource")
            print("Download URL: " + resource["url"])

# ----------------------------
# Handle a datastore-active resource
# ----------------------------
def handle_datastore_resource(resource):
    # Attempt to get all records in CSV format
    resource_dump_url = BASE_URL + "/datastore/dump/" + resource["id"]
    print("Downloading CSV from CKAN. This may take a few seconds.")
    resource_dump_response = requests.get(resource_dump_url)

    if resource_dump_response.status_code == 200:
        print("Successfully downloaded the full CSV dump.")
        preview = resource_dump_response.text[:500]
        print("Truncated preview:" + preview)

        os.makedirs(RAW_DIR, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"dinesafe_{timestamp}.csv"
        save_path = os.path.join(RAW_DIR, filename)

        with open(save_path, "w", encoding="utf-8") as f:
            f.write(resource_dump_response.text)

        print("\nSuccesfully saved CSV to: " + save_path)
    else:
        print(f"CSV dump not available (status code : {resource_dump_response.status_code})")

    # Show the first few records using datastore_search
    datastore_search_url = BASE_URL + "/api/3/action/datastore_search"
    p = {"id": resource["id"]}
    resource_search_response = requests.get(datastore_search_url, params=p).json()
    resource_search_records = resource_search_response["result"]["records"]

    print(f"Previewing first {len(resource_search_records)} records:")
    for record in resource_search_records[:3]:
        print(record)



if __name__ == "__main__":
    package_metadata = fetch_package_metadata(PACKAGE_ID)
    handle_resources(package_metadata["resources"])