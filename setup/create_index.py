"""
setup/create_index.py
Purpose: Programmatically provisions a regional Google Cloud Vertex AI Search
(Discovery Engine) Data Store in structured NO_CONTENT mode and applies the
"Quad of Success" schema mapping so search queries return native bolded snippets.
BoM: Index Provisioning & Schema Configuration CLI.
"""
import argparse
import json
import logging
import subprocess
import sys
import urllib.request
from google.api_core import exceptions
from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1beta as discoveryengine

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def create_data_store(project_id, location, datastore_id, display_name):
    """Creates a structured (NO_CONTENT) Vertex AI Search Data Store in the target region."""
    api_endpoint = (
        f"{location}-discoveryengine.googleapis.com"
        if location != "global"
        else "discoveryengine.googleapis.com"
    )
    client_options = ClientOptions(api_endpoint=api_endpoint)
    client = discoveryengine.DataStoreServiceClient(client_options=client_options)
    parent = f"projects/{project_id}/locations/{location}/collections/default_collection"

    data_store = discoveryengine.DataStore(
        display_name=display_name,
        industry_vertical=discoveryengine.IndustryVertical.GENERIC,
        solution_types=[discoveryengine.SolutionType.SOLUTION_TYPE_SEARCH],
        content_config=discoveryengine.DataStore.ContentConfig.NO_CONTENT,
    )

    logging.info(f"Creating Data Store '{datastore_id}' in region '{location}' (takes 1-3 minutes)...")
    try:
        operation = client.create_data_store(
            request={
                "parent": parent,
                "data_store": data_store,
                "data_store_id": datastore_id,
            }
        )
        response = operation.result()
        logging.info("Success! Data Store created.")
        return response
    except exceptions.AlreadyExists:
        logging.warning(f"Data store '{datastore_id}' already exists (skipping creation).")
        return None
    except Exception as e:
        logging.error(f"Error creating Data Store: {e}")
        sys.exit(1)


def configure_schema(project_id, location, datastore_id, token):
    """
    Patches the Data Store's default_schema via REST to map 'content' -> 'description',
    'meeting_title' -> 'title', and 'deep_link' -> 'uri' without illegal indexable flags.
    """
    domain = (
        f"{location}-discoveryengine.googleapis.com"
        if location != "global"
        else "discoveryengine.googleapis.com"
    )
    url = (
        f"https://{domain}/v1beta/projects/{project_id}/locations/{location}"
        f"/collections/default_collection/dataStores/{datastore_id}/schemas/default_schema"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Goog-User-Project": project_id,
        "Content-Type": "application/json",
    }

    # CRITICAL INVARIANT ("Quad of Success"):
    # Properties with 'keyPropertyMapping' MUST NOT declare 'indexable' or 'searchable'.
    schema_body = {
        "structSchema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "keyPropertyMapping": "description",
                },
                "meeting_title": {
                    "type": "string",
                    "keyPropertyMapping": "title",
                },
                "deep_link": {
                    "type": "string",
                    "keyPropertyMapping": "uri",
                },
                "date": {"type": "string", "indexable": True},
                "chamber": {"type": "string", "indexable": True},
                "committee": {"type": "string", "indexable": True},
                "meeting_time": {"type": "string", "indexable": True},
                "sort_timestamp": {"type": "number", "indexable": True},
                "agenda_item": {"type": "string", "indexable": True},
                "meeting_id": {"type": "string", "indexable": True},
                "meeting_path": {"type": "string", "indexable": True},
                "jurisdiction": {"type": "string"},
            },
        }
    }

    logging.info("Configuring schema for native bolded snippets...")
    req = urllib.request.Request(
        url,
        data=json.dumps(schema_body).encode("utf-8"),
        headers=headers,
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req):
            logging.info("Success! Schema configured (`content` mapped to `description`).")
    except Exception as e:
        error_info = getattr(e, "read", lambda: getattr(e, "reason", str(e)))()
        logging.error(f"Error configuring schema: {error_info}")
        sys.exit(1)


def get_auth_token():
    """Retrieves an OAuth2 access token from the active gcloud CLI session."""
    gcloud_bin = "gcloud.cmd" if sys.platform.startswith("win") else "gcloud"
    try:
        logging.info("Retrieving gcloud access token...")
        result = subprocess.run(
            [gcloud_bin, "auth", "print-access-token"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception as e:
        logging.error(f"Failed to retrieve gcloud auth token: {e}")
        sys.exit(1)


def provision_index(project_id, region, jurisdiction, version):
    """Orchestrates data store creation and REST schema patching."""
    datastore_id = f"{jurisdiction}-{version}"
    display_name = f"{jurisdiction.replace('-', ' ').title()} Search ({version})"

    create_data_store(project_id, region, datastore_id, display_name)
    token = get_auth_token()
    configure_schema(project_id, region, datastore_id, token)
    logging.info(f"Complete. Datastore '{datastore_id}' is ready in region '{region}'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Provision a structured Vertex AI Search index with snippet support."
    )
    parser.add_argument("--project", required=True, help="Google Cloud Project ID")
    parser.add_argument("--jurisdiction", required=True, help="RFC-1034 slug (e.g., example-legislature)")
    parser.add_argument("--version", default="v1", help="Index version suffix (default: v1)")
    parser.add_argument("--region", default="us", help="GCP Region: 'us', 'eu', or 'global' (default: us)")

    args = parser.parse_args()
    provision_index(args.project, args.region, args.jurisdiction, args.version)
