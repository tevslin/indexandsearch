"""
ingestion/ingest_meeting.py
Purpose: Ingests a single local or S3-hosted transcript.html file into Vertex AI Search
by parsing utterances/agendas, formatting structured clips, and invoking batched upserts.
BoM: Single-Meeting Transcript Ingestion Utility.
"""
import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import boto3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from ingestion.indexing import (
    format_indexing_record,
    group_utterances_by_agenda,
    parse_meeting_path,
    parse_transcript_html,
)
from ingestion.vertex_client import upsert_documents

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _load_s3_transcript(s3_uri):
    """Downloads a transcript.html from S3 and derives its relative path metadata."""
    s3 = boto3.client("s3")
    logger.info(f"Downloading from S3: {s3_uri}")
    parts = s3_uri[5:].split("/", 1)
    bucket_name, key = parts[0], parts[1]

    rel_key = key[len("meetings/") :] if key.startswith("meetings/") else key
    chamber, committee, meeting_folder = parse_meeting_path(rel_key)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html")
    tmp_path = tmp.name
    tmp.close()

    try:
        s3.download_file(bucket_name, key, tmp_path)
        with open(tmp_path, "r", encoding="utf-8") as f:
            content = f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    rel_path = f"/{key}" if key.startswith("meetings/") else f"/meetings/{key}"
    return content, chamber, committee, meeting_folder, rel_path


def _load_local_transcript(file_path):
    """Reads a local transcript.html and infers chamber, committee, and meeting folder."""
    logger.info(f"Reading local file: {file_path}")
    path_obj = Path(file_path)
    chamber, committee, meeting_folder = "Senate", "Agriculture", path_obj.parent.name

    if "meetings" in path_obj.parts:
        idx = path_obj.parts.index("meetings")
        rel_key = "/".join(path_obj.parts[idx + 1 :])
        chamber, committee, meeting_folder = parse_meeting_path(rel_key)

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    rel_path = f"/meetings/{chamber}/{committee}/{meeting_folder}/transcript.html"
    return content, chamber, committee, meeting_folder, rel_path


def ingest_meeting(
    jurisdiction,
    target_index,
    file_path=None,
    s3_uri=None,
    project_id=None,
    dry_run=False,
):
    """Parses a single transcript from local disk or S3 and upserts its clips to Vertex AI Search."""
    if s3_uri:
        content, chamber, committee, meeting_folder, rel_path = _load_s3_transcript(s3_uri)
    elif file_path:
        content, chamber, committee, meeting_folder, rel_path = _load_local_transcript(file_path)
    else:
        raise ValueError("Either file_path or s3_uri must be provided.")

    parsed_data = parse_transcript_html(content)
    clips = group_utterances_by_agenda(
        parsed_data["utterances"], parsed_data.get("agendas", []), parsed_data
    )

    record_index_name = (
        target_index.split(":", 1)[1] if ":" in target_index else target_index
    )

    records = [
        format_indexing_record(
            clip, record_index_name, chamber, committee, meeting_folder, rel_path
        )
        for clip in clips
    ]

    if not records:
        logger.warning("No indexable utterance clips generated from transcript.")
        return

    logger.info(f"Generated {len(records)} structured clip records for target '{target_index}'.")
    if dry_run:
        logger.info("[DRY-RUN] Skipping Vertex AI upsert. Sample record payload:")
        print(json.dumps(records[0], indent=2))
        return

    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, suffix=".jsonl", encoding="utf-8-sig"
    ) as tmp:
        for rec in records:
            tmp.write(json.dumps(rec) + "\n")
        tmp_path = tmp.name

    try:
        upsert_documents(target_index, tmp_path, project_id=project_id)
        logger.info("Ingestion successful.")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest a single meeting transcript into Vertex AI Search."
    )
    parser.add_argument("--jurisdiction", required=True, help="Jurisdiction identifier")
    parser.add_argument(
        "--target-index",
        required=True,
        help="Target index in format region:index-name (e.g., us:example-legislature-v1)",
    )
    parser.add_argument("--file", help="Local path to transcript.html")
    parser.add_argument("--s3-uri", help="S3 URI to transcript.html (s3://bucket/key)")
    parser.add_argument("--project", help="Google Cloud Project ID")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print a sample record without upserting to Vertex AI",
    )

    args = parser.parse_args()
    ingest_meeting(
        jurisdiction=args.jurisdiction,
        target_index=args.target_index,
        file_path=args.file,
        s3_uri=args.s3_uri,
        project_id=args.project,
        dry_run=args.dry_run,
    )
