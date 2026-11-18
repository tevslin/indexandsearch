"""
lambda_updater/process_upsert.py
Purpose: AWS Lambda handler triggered by S3 ObjectCreated events when a new or
updated transcript.html is uploaded. Materializes optional GCP service account
credentials in /tmp and invokes ingest_meeting() to upsert structured clips to Vertex AI.
BoM: Event-Driven S3-to-Vertex AI Ingestion Lambda Handler.
"""
import argparse
import json
import logging
import os
import sys
import tempfile
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from ingestion.ingest_meeting import ingest_meeting

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _configure_gcp_credentials():
    """Writes GCP_SERVICE_ACCOUNT_JSON env var to a temporary file if GOOGLE_APPLICATION_CREDENTIALS is not set."""
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return

    raw_sa = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not raw_sa:
        return

    tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json")
    tmp.write(raw_sa)
    tmp.close()
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name


def lambda_handler(event, context):
    """Main AWS Lambda entry point for S3 ObjectCreated triggers on transcript.html."""
    logger.info(f"Received S3 event: {json.dumps(event)}")

    try:
        record = event["Records"][0]
        bucket_name = record["s3"]["bucket"]["name"]
        object_key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
    except Exception as e:
        logger.error(f"Failed to parse S3 event payload: {e}")
        return {"statusCode": 400, "body": "Invalid S3 event structure"}

    jurisdiction = os.environ.get("JURISDICTION", "example-legislature")
    target_index = os.environ.get("TARGET_INDEX")
    project_id = os.environ.get("GCP_PROJECT_ID")

    if not target_index or not project_id:
        logger.error("Missing required environment variables: TARGET_INDEX and GCP_PROJECT_ID")
        return {
            "statusCode": 500,
            "body": "Server configuration error: TARGET_INDEX and GCP_PROJECT_ID must be set",
        }

    _configure_gcp_credentials()

    s3_uri = f"s3://{bucket_name}/{object_key}"
    try:
        logger.info(f"Starting Vertex AI ingestion for '{jurisdiction}' from {s3_uri}")
        ingest_meeting(
            jurisdiction=jurisdiction,
            target_index=target_index,
            s3_uri=s3_uri,
            project_id=project_id,
        )
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Ingestion successful",
                    "s3_uri": s3_uri,
                    "target_index": target_index,
                }
            ),
        }
    except Exception as e:
        logger.error(f"Ingestion failed: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"message": "Ingestion failed", "error": str(e)}),
        }


def run_local_mock(event_file_path):
    """Loads a local S3 event JSON file and invokes lambda_handler for local testing."""
    with open(event_file_path, "r", encoding="utf-8") as f:
        mock_event = json.load(f)
    result = lambda_handler(mock_event, None)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test S3-to-Vertex Lambda handler locally.")
    parser.add_argument("event_json", help="Path to sample S3 event JSON file")
    args = parser.parse_args()
    run_local_mock(args.event_json)
