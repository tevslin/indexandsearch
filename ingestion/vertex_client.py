"""
ingestion/vertex_client.py
Purpose: Provides Direct API Upsert and Document Listing operations against
Google Cloud Vertex AI Search (Discovery Engine) with regional endpoint support.
BoM: Vertex AI Search Document Upsert & Inspection Client.
"""
import argparse
import json
import logging
import os
from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1beta as discoveryengine

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _build_document_client_and_parent(target_index, project_id=None):
    """Creates a regional DocumentServiceClient and resolves the default_branch path."""
    if ":" not in target_index:
        raise ValueError(
            f"Invalid target_index '{target_index}'. Expected format 'region:index-name' (e.g., 'us:example-v1')."
        )

    loc, data_store_id = target_index.split(":", 1)
    resolved_project = project_id or os.environ.get("GCP_PROJECT_ID") or os.environ.get("PROJECT_ID")
    if not resolved_project:
        raise ValueError("GCP Project ID is required via --project or GCP_PROJECT_ID env var.")

    api_endpoint = (
        f"{loc}-discoveryengine.googleapis.com"
        if loc != "global"
        else "discoveryengine.googleapis.com"
    )
    client_options = ClientOptions(api_endpoint=api_endpoint)
    client = discoveryengine.DocumentServiceClient(client_options=client_options)
    parent = client.branch_path(
        project=resolved_project,
        location=loc,
        data_store=data_store_id,
        branch="default_branch",
    )
    return client, parent, resolved_project, loc, data_store_id


def upsert_documents(target_index, file_path, batch_size=100, project_id=None):
    """Reads a JSONL file of structured records and upserts them incrementally to Vertex AI Search."""
    client, parent, resolved_project, loc, data_store_id = _build_document_client_and_parent(
        target_index, project_id
    )

    logger.info("--- Direct API Upsert ---")
    logger.info(f"Project: {resolved_project} | Location: {loc} | Store: {data_store_id}")
    logger.info(f"Source JSONL: {file_path}")

    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return

    documents = []
    with open(file_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            if not line.strip():
                continue
            data = json.loads(line)
            doc_id = data.get("id")
            struct_data = data.get("structData", {k: v for k, v in data.items() if k != "id"})
            documents.append(discoveryengine.Document(id=doc_id, struct_data=struct_data))

    logger.info(f"Upserting {len(documents)} documents in batches of {batch_size}...")
    for i in range(0, len(documents), batch_size):
        batch = documents[i : i + batch_size]
        batch_num = i // batch_size + 1
        logger.info(f"Upserting batch {batch_num}...")

        request = discoveryengine.ImportDocumentsRequest(
            parent=parent,
            inline_source=discoveryengine.ImportDocumentsRequest.InlineSource(documents=batch),
            reconciliation_mode=discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL,
        )
        operation = client.import_documents(request=request)
        operation.result()
        logger.info(f"Batch {batch_num} confirmed.")

    logger.info("Upsert complete.")


def list_documents(target_index, project_id=None, limit=10):
    """Lists document IDs currently indexed in the target Vertex AI Data Store."""
    client, parent, _, _, data_store_id = _build_document_client_and_parent(
        target_index, project_id
    )
    logger.info(f"--- Listing Documents in Store: {data_store_id} ---")
    request = discoveryengine.ListDocumentsRequest(parent=parent, page_size=limit)

    count = 0
    for response in client.list_documents(request=request):
        count += 1
        print(f"[{count}] ID: {response.id}")
        if count >= limit:
            break
    logger.info(f"Listed {count} documents (limit={limit}).")


def run_cli(args):
    """Dispatches CLI subcommands to upsert_documents or list_documents."""
    if args.command == "upsert":
        if not args.path:
            raise ValueError("--path is required when command is 'upsert'")
        upsert_documents(args.target_index, args.path, project_id=args.project)
    elif args.command == "list":
        list_documents(args.target_index, project_id=args.project, limit=args.limit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vertex AI Search Document Control Utility")
    parser.add_argument("command", choices=["upsert", "list"])
    parser.add_argument(
        "--target-index",
        required=True,
        help="Target index in format region:index-name (e.g., us:example-legislature-v1)",
    )
    parser.add_argument("--path", help="Local JSONL file path for upsert")
    parser.add_argument("--project", help="Google Cloud Project ID (overrides GCP_PROJECT_ID)")
    parser.add_argument("--limit", type=int, default=10, help="Max documents to list")

    parsed_args = parser.parse_args()
    run_cli(parsed_args)
