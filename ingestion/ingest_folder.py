"""
ingestion/ingest_folder.py
Purpose: Batch-ingests transcript.html files from either a local directory tree
or an AWS S3 bucket prefix into Vertex AI Search, supporting incremental date
cutoffs, item limits, and dry-run previews.
BoM: Batch Directory & S3 Bucket Transcript Ingestion CLI.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from ingestion.ingest_meeting import ingest_meeting


def parse_start_date(start_date_str):
    """Parses a relative day offset ('0', '-7'), 'YYYY-MM-DD', or 'None' into a timezone-aware datetime."""
    if start_date_str is None or str(start_date_str).lower() == "none":
        return None

    et_tz = ZoneInfo("America/New_York")
    now_et = datetime.now(et_tz)

    if start_date_str.lstrip("-").isdigit():
        offset_days = int(start_date_str)
        target_date = now_et.date() + timedelta(days=offset_days)
        return datetime.combine(target_date, datetime.min.time(), tzinfo=et_tz)

    fmt = "%Y-%m-%d_%H:%M" if "_" in start_date_str else "%Y-%m-%d"
    dt = datetime.strptime(start_date_str, fmt)
    return dt.replace(tzinfo=et_tz)


def list_s3_transcripts(bucket_name, prefix=""):
    """Lists all (key, LastModified) pairs ending with 'transcript.html' in an S3 bucket."""
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    results = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("transcript.html"):
                results.append((obj["Key"], obj["LastModified"]))
    return results


def ingest_s3_bucket(
    jurisdiction,
    bucket_path,
    target_index,
    project_id=None,
    limit=None,
    start_date=None,
    dry_run=False,
):
    """Scans an S3 bucket for transcript.html files and upserts each matching meeting."""
    path_clean = bucket_path.replace("s3://", "")
    parts = path_clean.split("/", 1)
    bucket_name = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""

    print(f"Scanning S3 Bucket '{bucket_name}' under prefix '{prefix}'...")
    start_dt = parse_start_date(start_date)
    transcript_items = list_s3_transcripts(bucket_name, prefix)

    count, skipped = 0, 0
    et_tz = ZoneInfo("America/New_York")
    for key, mtime_utc in transcript_items:
        if start_dt and mtime_utc.astimezone(et_tz) < start_dt:
            skipped += 1
            continue

        s3_uri = f"s3://{bucket_name}/{key}"
        if dry_run:
            print(f"[DRY-RUN] Selected: {s3_uri}")
        else:
            ingest_meeting(
                jurisdiction,
                target_index,
                s3_uri=s3_uri,
                project_id=project_id,
                dry_run=False,
            )
        count += 1
        if limit and count >= limit:
            break

    print(f"S3 batch complete. Processed: {count}, Skipped: {skipped}")


def ingest_local_folder(
    jurisdiction,
    folder_path,
    target_index,
    project_id=None,
    limit=None,
    start_date=None,
    dry_run=False,
):
    """Scans a local folder tree for *.html transcript files and upserts each matching meeting."""
    path_obj = Path(folder_path)
    if not path_obj.exists() or not path_obj.is_dir():
        raise FileNotFoundError(f"Local folder not found: {folder_path}")

    start_dt = parse_start_date(start_date)
    et_tz = ZoneInfo("America/New_York")
    count, skipped = 0, 0

    for root, _, files in os.walk(folder_path):
        for name in files:
            if name.endswith("transcript.html"):
                file_path = os.path.join(root, name)
                if start_dt:
                    mtime_utc = datetime.fromtimestamp(
                        os.path.getmtime(file_path), tz=ZoneInfo("UTC")
                    )
                    if mtime_utc.astimezone(et_tz) < start_dt:
                        skipped += 1
                        continue

                if dry_run:
                    print(f"[DRY-RUN] Selected: {file_path}")
                else:
                    ingest_meeting(
                        jurisdiction,
                        target_index,
                        file_path=file_path,
                        project_id=project_id,
                        dry_run=False,
                    )
                count += 1
                if limit and count >= limit:
                    print(f"Local batch complete. Processed: {count}, Skipped: {skipped}")
                    return

    print(f"Local batch complete. Processed: {count}, Skipped: {skipped}")


def run_batch_ingestion(args):
    """Routes batch ingestion to either S3 or local folder handler based on --folder URI."""
    if args.folder.startswith("s3://"):
        ingest_s3_bucket(
            jurisdiction=args.jurisdiction,
            bucket_path=args.folder,
            target_index=args.target_index,
            project_id=args.project,
            limit=args.limit,
            start_date=args.start_date,
            dry_run=args.dry_run,
        )
    else:
        ingest_local_folder(
            jurisdiction=args.jurisdiction,
            folder_path=args.folder,
            target_index=args.target_index,
            project_id=args.project,
            limit=args.limit,
            start_date=args.start_date,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Batch ingest local or S3 transcripts into Vertex AI Search."
    )
    parser.add_argument("--jurisdiction", required=True, help="Jurisdiction slug")
    parser.add_argument(
        "--target-index",
        required=True,
        help="Target index in format region:index-name (e.g., us:example-legislature-v1)",
    )
    parser.add_argument(
        "--folder",
        required=True,
        help="Local folder path or S3 URI (e.g., s3://my-bucket/meetings)",
    )
    parser.add_argument("--project", help="Google Cloud Project ID")
    parser.add_argument("--limit", type=int, help="Maximum number of transcripts to ingest")
    parser.add_argument(
        "--start-date",
        default="0",
        help="Day offset ('0' for today, '-7' for last week), 'YYYY-MM-DD', or 'None' for all history",
    )
    parser.add_argument("--dry-run", action="store_true", help="List matching files without upserting")

    parsed_args = parser.parse_args()
    run_batch_ingestion(parsed_args)
